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


def test_login_over_plain_http_is_refused_instead_of_silently_not_sticking(app, make_user):
    """Secure cookies are dropped by the browser over http://<lan-host>: the password would be
    accepted, the cookie discarded, and the user bounced to the login form as if it were wrong."""
    from app.models import DeviceToken
    make_user('alice')
    app.config['SESSION_COOKIE_SECURE'] = True
    creds = {'username': 'alice', 'password': PASSWORD, 'remember': True}
    lan = app.test_client().post('/api/auth/login', json=creds, base_url='http://spearow:8640')
    assert lan.status_code == 400 and lan.get_json()['error'] == 'https_required'
    with app.app_context():
        assert DeviceToken.query.count() == 0                        # nothing minted for a login that cannot stick
    for base in ('http://localhost:8640', 'https://munchlax.tailnet.ts.net'):
        assert app.test_client().post('/api/auth/login', json=creds, base_url=base).status_code == 200
    # behind `tailscale serve` the app itself sees plain HTTP plus X-Forwarded-Proto
    proxied = app.test_client().post('/api/auth/login', json=creds, base_url='http://munchlax:8640',
                                     headers={'X-Forwarded-Proto': 'https'})
    assert proxied.status_code == 200
    page = app.test_client().get('/login', base_url='http://spearow:8640').get_data(as_text=True)
    assert 'plain HTTP' in page and 'href="http://localhost:8640/login"' in page


def test_another_apps_session_cookie_on_the_same_host_does_not_break_writes(app, make_user, api):
    """Cookies are per host, not per port. ytsum shares munchlax's Tailscale name and uses
    Flask's default 'session' cookie; under that same name it replaced fitmon's, wiping the
    CSRF token, and every POST failed while the device cookie kept the pages loading."""
    make_user('carol')
    carol = api()
    assert carol.login('carol', remember=True).status_code == 200
    assert carol.client.get_cookie('fitmon_session') is not None
    assert carol.client.get_cookie('session') is None

    # Another app on the host sets its own 'session': fitmon must not even read it.
    carol.client.set_cookie('session', 'ytsum-signed-garbage')
    assert carol.post('/api/settings', json={'units': 'metric'}).status_code == 200

    # Lose fitmon's own session entirely (what the old collision did): the stale token fails,
    # and /api/auth/me - reached on the remember-me cookie alone - hands out a working one.
    carol.client.delete_cookie('fitmon_session')
    assert carol.post('/api/settings', json={'units': 'metric'}).get_json() == {'error': 'csrf_failed'}
    carol.csrf = carol.get('/api/auth/me').get_json()['csrf']
    assert carol.post('/api/settings', json={'units': 'metric'}).status_code == 200


def test_screenshot_is_public_png_without_redirect(app, alice):
    """Fleet standard: the landing page fetches it signed out and does not follow redirects."""
    anon = app.test_client()
    resp = anon.get('/api/screenshot')
    assert resp.status_code == 200 and resp.mimetype == 'image/png'
    assert resp.data[:8] == b'\x89PNG\r\n\x1a\n'
    assert anon.get('/api/activities').status_code == 401        # the exemption is that one route only

    page = alice.get('/').get_data(as_text=True)                   # the About tab shows the same image
    assert 'id="about-shot"' in page and 'src="/api/screenshot"' in page
    assert alice.post('/api/settings', json={'about_screenshot_open': True}).get_json()['about_screenshot_open'] is True


def test_display_name_can_be_changed_and_is_validated(app, alice, bob):
    """Shown in the header and in the corner of every chart; only ever your own."""
    from app.models import User, db
    resp = alice.post('/api/account/profile', json={'display_name': '  Mike \t Wise '})
    assert resp.status_code == 200 and resp.get_json()['user']['display_name'] == 'Mike Wise'
    assert 'id="who-name">Mike Wise<' in alice.get('/').get_data(as_text=True)

    assert alice.post('/api/account/profile', json={'display_name': '   '}).get_json()['error'] == 'display_name_required'
    assert alice.post('/api/account/profile', json={'display_name': 'x' * 61}).get_json()['error'] == 'display_name_too_long'
    assert alice.post('/api/account/profile', json={'display_name': 'a\x00b'}).get_json()['user']['display_name'] == 'ab'

    with app.app_context():                   # nothing in the request can point it at someone else
        assert db.session.get(User, bob.user_id).display_name == 'bob'


def test_activity_layout_and_playback_speed_are_saved_per_user(alice, bob):
    """Panel order, collapsed panels and playback speed follow the person, not the browser."""
    layout = {'order': ['series', 'map', 'zones', 'sport', 'laps', 'devices', 'trim'], 'collapsed': ['zones']}
    saved = alice.post('/api/settings', json={'detail_layout': layout, 'playback_speed': 120}).get_json()
    assert saved['detail_layout'] == layout and saved['playback_speed'] == 120
    assert alice.get('/api/settings').get_json()['detail_layout'] == layout
    assert bob.get('/api/settings').get_json()['detail_layout'] == {'order': [], 'collapsed': []}

    page = alice.get('/').get_data(as_text=True)
    for key in ('map', 'zones', 'series', 'sport', 'laps', 'devices', 'trim'):
        assert f'data-panel="{key}"' in page          # every key the saved order may name exists
    assert 'id="pb-bar"' in page and 'id="pb-scrub"' in page
