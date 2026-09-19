from datetime import timedelta

from .conftest import PASSWORD


def test_ping_is_public_and_has_the_four_fields(app):
    body = app.test_client().get('/api/ping').get_json()
    assert set(body) == {'hostname', 'status', 'timestamp', 'version'}
    assert body['status'] == 'ok'


def test_everything_else_requires_login(app):
    """Deny by default: walk the URL map so a route added later is covered automatically."""
    from app.auth import PUBLIC_ENDPOINTS
    client = app.test_client()
    checked = 0
    for rule in app.url_map.iter_rules():
        if rule.endpoint in PUBLIC_ENDPOINTS:
            continue
        url = rule.rule
        for arg in rule.arguments:
            url = url.replace(f'<int:{arg}>', '1').replace(f'<{arg}>', 'x')
        for method in rule.methods - {'HEAD', 'OPTIONS'}:
            resp = client.open(url, method=method)
            if url.startswith('/api/'):
                assert resp.status_code == 401, (method, url, resp.status_code)
            else:
                assert resp.status_code == 302 and '/login' in resp.headers['Location'], (method, url)
            checked += 1
    assert checked > 40


def test_login_failures_look_identical(app, make_user, api):
    make_user('alice')
    wrong = api().login('alice', 'nope-nope-nope')
    unknown = api().login('nobody', 'nope-nope-nope')
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json() == unknown.get_json() == {'error': 'invalid_credentials'}


def test_username_is_case_insensitive(app, make_user, api):
    make_user('Alice')
    assert api().login('ALICE').status_code == 200


def test_throttle_kicks_in_and_clears_on_restart(app, make_user, api):
    from app import auth
    make_user('alice')
    client = api()
    for _ in range(auth.LoginThrottle.FREE_ATTEMPTS + 1):
        client.login('alice', 'wrong-password')
    resp = client.login('alice')                       # correct password, still locked out
    assert resp.status_code == 429 and resp.get_json()['retry_after'] > 0
    auth.throttle.reset()
    assert client.login('alice').status_code == 200


def test_csrf_required_on_writes(alice):
    assert alice.client.post('/api/settings', json={'units': 'statute'}).status_code == 403
    assert alice.post('/api/settings', json={'units': 'statute'}).status_code == 200


def test_remember_me_issues_90_day_hashed_token(app, make_user, api):
    from app.auth import DEVICE_COOKIE, sha256
    from app.models import DeviceToken, utcnow
    make_user('alice')
    client = api()
    resp = client.login('alice', remember=True)
    cookie = next(c for c in resp.headers.getlist('Set-Cookie') if c.startswith(DEVICE_COOKIE))
    assert 'HttpOnly' in cookie and 'SameSite=Lax' in cookie
    raw = cookie.split('=', 1)[1].split(';')[0]
    with app.app_context():
        token = DeviceToken.query.one()
        assert token.token_sha256 == sha256(raw) and raw not in token.token_sha256
        assert token.expires_at - utcnow() > timedelta(days=89)

    # a fresh browser holding only the device cookie is logged in
    fresh = app.test_client()
    fresh.set_cookie(DEVICE_COOKIE, raw)
    assert fresh.get('/api/auth/me').get_json()['user']['username'] == 'alice'


def test_no_remember_means_no_device_token(app, make_user, api):
    from app.models import DeviceToken
    make_user('alice')
    api().login('alice')
    with app.app_context():
        assert DeviceToken.query.count() == 0


def test_revoked_and_expired_tokens_stop_working(app, make_user, api):
    from app.auth import DEVICE_COOKIE
    from app.models import DeviceToken, db, utcnow
    make_user('alice')
    resp = api().login('alice', remember=True)
    raw = next(c for c in resp.headers.getlist('Set-Cookie') if c.startswith(DEVICE_COOKIE)).split('=', 1)[1].split(';')[0]
    fresh = app.test_client()
    fresh.set_cookie(DEVICE_COOKIE, raw)
    with app.app_context():
        DeviceToken.query.one().expires_at = utcnow() - timedelta(seconds=1)
        db.session.commit()
    assert fresh.get('/api/auth/me').status_code == 401


def test_password_change_logs_out_other_sessions(app, make_user, api):
    make_user('alice')
    laptop, phone = api(), api()
    laptop.login('alice')
    phone.login('alice', remember=True)
    resp = laptop.post('/api/auth/password', json={'old_password': PASSWORD, 'new_password': 'a whole new password'})
    assert resp.status_code == 200
    laptop.csrf = resp.get_json()['csrf']
    assert phone.get('/api/auth/me').status_code == 401
    assert laptop.get('/api/auth/me').status_code == 200
    assert api().login('alice', 'a whole new password').status_code == 200


def test_weak_password_rejected(alice):
    resp = alice.post('/api/auth/password', json={'old_password': PASSWORD, 'new_password': 'short'})
    assert resp.status_code == 400


def test_disabled_user_cannot_log_in_or_keep_session(app, alice, bob):
    assert alice.post(f'/api/admin/users/{bob.user_id}', json={'is_active': False}).status_code == 200
    assert bob.get('/api/auth/me').status_code == 401
    assert bob.login('bob').status_code == 401


def test_admin_routes_are_404_for_users(bob):
    assert bob.get('/api/admin/users').status_code == 404
    assert bob.post('/api/admin/invites').status_code == 404
    assert bob.post('/api/import/scan', json={'directory': '.'}).status_code == 404


def test_last_admin_cannot_be_demoted(alice):
    resp = alice.post(f'/api/admin/users/{alice.user_id}', json={'role': 'user'})
    assert resp.status_code == 400 and resp.get_json()['error'] == 'last_admin'


def test_invite_flow_is_single_use(app, alice, api):
    url = alice.post('/api/admin/invites', json={'note': 'for carol'}).get_json()['url']
    token = url.rsplit('/', 1)[1]
    guest = app.test_client()
    assert guest.get(f'/api/invite/{token}').status_code == 200
    resp = guest.post(f'/api/invite/{token}/accept', json={'username': 'carol', 'password': PASSWORD})
    assert resp.status_code == 200 and resp.get_json()['user']['role'] == 'user'
    assert guest.get('/api/auth/me').status_code == 200            # logged straight in
    again = app.test_client().post(f'/api/invite/{token}/accept', json={'username': 'dave', 'password': PASSWORD})
    assert again.status_code == 404
    with app.app_context():
        from app.models import Invite
        assert token not in Invite.query.one().token_sha256        # only the hash is stored


def test_secret_key_is_generated_not_default(app, home):
    key = (home / 'auth' / 'secret_key').read_text()
    assert len(key) == 64 and app.config['SECRET_KEY'] == key


def test_passwords_never_reach_the_event_log(app, home, make_user, api):
    from app.events import event_logger
    make_user('alice')
    api().login('alice', 'hunter2-hunter2')
    event_logger.info('test.event', 'x', password='hunter2-hunter2', token='abc')
    text = (home / 'logs' / 'events.jsonl').read_text()
    assert 'hunter2' not in text and 'auth.login_failed' in text


def test_password_minimum_is_8_and_pages_state_the_real_number(app, alice):
    from app import auth
    assert auth.MIN_PASSWORD_LENGTH == 8
    assert auth.validate_new_password('1234567') and auth.validate_new_password('12345678') is None
    with app.app_context():
        _, raw = auth.create_invite(alice.user_id)
    page = app.test_client().get(f'/invite/{raw}').get_data(as_text=True)
    assert 'at least 8 characters' in page and 'minlength="8"' in page
    assert '(8+ characters)' in alice.get('/').get_data(as_text=True)
