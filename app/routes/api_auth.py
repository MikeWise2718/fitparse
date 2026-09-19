from flask import Blueprint, current_app, g, jsonify, make_response, request, session
from werkzeug.security import check_password_hash

from .. import auth
from ..events import event_logger
from ..models import DeviceToken, User, db, utcnow

auth_bp = Blueprint('auth', __name__)

# Verified against when the username is unknown, so both failure paths cost one scrypt.
_DUMMY_HASH = auth.hash_password('not-a-real-password')


def _client_ip() -> str:
    return request.remote_addr or 'unknown'


def _audit() -> dict:
    # `tailscale serve` identifies the tailnet user. Audit trail only - never authentication.
    return {'ip': _client_ip(), 'tailscale_user': request.headers.get('Tailscale-User-Login')}


@auth_bp.route('/api/auth/login', methods=['POST'])
def login():
    # Auth cookies are Secure, and a browser silently drops a Secure cookie that arrives over
    # plain HTTP from anything but localhost. Without this check the password is accepted, the
    # cookie is discarded, and the user lands back on the login form convinced it was wrong.
    if current_app.config['SESSION_COOKIE_SECURE'] and not request.is_secure \
            and request.host.split(':')[0] not in ('localhost', '127.0.0.1'):
        return jsonify({'error': 'https_required',
                        'message': 'Sign-in only works over HTTPS (the Tailscale address) or on '
                                   'http://localhost - over plain HTTP the browser would discard '
                                   'the login cookie.'}), 400

    data = request.get_json(silent=True) or {}
    username = auth.normalize_username(data.get('username'))
    password = data.get('password') or ''
    keys = (f'user:{username}', f'ip:{_client_ip()}')

    wait = auth.throttle.retry_after(*keys)
    if wait:
        return jsonify({'error': 'too_many_attempts', 'retry_after': wait}), 429

    user = User.query.filter_by(username=username).first()
    ok = check_password_hash(user.password_hash if user else _DUMMY_HASH, password)
    if not (user and ok and user.is_active):
        auth.throttle.failure(*keys)
        event_logger.warning('auth.login_failed', f'failed login for {username!r}',
                             username=username, **_audit())
        # One message for unknown user, wrong password and disabled account.
        return jsonify({'error': 'invalid_credentials'}), 401

    auth.throttle.success(*keys)
    auth.start_session(user)
    user.last_login_at = utcnow()
    db.session.commit()
    resp = make_response(jsonify({'user': user.to_dict(), 'csrf': auth.csrf_token()}))
    if data.get('remember'):
        auth.set_device_cookie(resp, auth.issue_device_token(user))
    event_logger.info('auth.login_succeeded', f'{username} logged in', user_id=user.id,
                      remember=bool(data.get('remember')), **_audit())
    return resp


@auth_bp.route('/api/auth/me')
def me():
    return jsonify({'user': g.user.to_dict(), 'csrf': auth.csrf_token()})


@auth_bp.route('/api/auth/logout', methods=['POST'])
def logout():
    if g.device_token_id:
        token = db.session.get(DeviceToken, g.device_token_id)
        if token:
            token.revoked_at = utcnow()
            db.session.commit()
    event_logger.info('auth.logout', f'{g.user.username} logged out', user_id=g.user.id)
    session.clear()
    return auth.clear_device_cookie(make_response(jsonify({'ok': True})))


@auth_bp.route('/api/auth/logout-all', methods=['POST'])
def logout_all():
    n = auth.revoke_all_tokens(g.user.id)
    event_logger.info('auth.logout_all', f'{g.user.username} revoked {n} devices', user_id=g.user.id)
    session.clear()
    return auth.clear_device_cookie(make_response(jsonify({'ok': True, 'revoked': n})))


@auth_bp.route('/api/auth/password', methods=['POST'])
def change_password():
    data = request.get_json(silent=True) or {}
    if not check_password_hash(g.user.password_hash, data.get('old_password') or ''):
        return jsonify({'error': 'invalid_credentials'}), 401
    problem = auth.validate_new_password(data.get('new_password') or '')
    if problem:
        return jsonify({'error': 'weak_password', 'message': problem}), 400
    g.user.password_hash = auth.hash_password(data['new_password'])
    db.session.commit()
    # Every remembered device and every other browser session dies with the old password.
    auth.revoke_all_tokens(g.user.id)
    auth.start_session(g.user)
    event_logger.info('auth.password_changed', f'{g.user.username} changed password', user_id=g.user.id)
    resp = make_response(jsonify({'ok': True, 'csrf': auth.csrf_token()}))
    return auth.clear_device_cookie(resp)


@auth_bp.route('/api/auth/devices')
def devices():
    rows = (DeviceToken.query
            .filter(DeviceToken.user_id == g.user.id, DeviceToken.revoked_at.is_(None),
                    DeviceToken.expires_at > utcnow())
            .order_by(DeviceToken.last_used_at.desc()).all())
    return jsonify({'devices': [{
        'id': t.id, 'device_name': t.device_name, 'created_at': t.created_at,
        'last_used_at': t.last_used_at, 'expires_at': t.expires_at,
        'current': t.id == g.device_token_id,
    } for t in rows]})


@auth_bp.route('/api/auth/devices/<int:token_id>/revoke', methods=['POST'])
def revoke_device(token_id):
    token = DeviceToken.query.filter_by(id=token_id, user_id=g.user.id).first_or_404()
    token.revoked_at = utcnow()
    db.session.commit()
    return jsonify({'ok': True})


@auth_bp.route('/api/invite/<token>')
def invite_info(token):
    invite = auth.find_valid_invite(token)
    if not invite:
        return jsonify({'error': 'invalid_invite'}), 404
    return jsonify({'ok': True, 'expires_at': invite.expires_at})


@auth_bp.route('/api/invite/<token>/accept', methods=['POST'])
def invite_accept(token):
    invite = auth.find_valid_invite(token)
    if not invite:
        return jsonify({'error': 'invalid_invite'}), 404
    data = request.get_json(silent=True) or {}
    try:
        user = auth.create_user(data.get('username'), data.get('password') or '',
                                role=invite.role, display_name=data.get('display_name'))
    except ValueError as exc:
        return jsonify({'error': 'invalid_input', 'message': str(exc)}), 400
    invite.used_by, invite.used_at = user.id, utcnow()
    db.session.commit()
    auth.start_session(user)
    event_logger.info('auth.invite_accepted', f'{user.username} joined', user_id=user.id,
                      invite_id=invite.id, **_audit())
    return jsonify({'user': user.to_dict(), 'csrf': auth.csrf_token()})
