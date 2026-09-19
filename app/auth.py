"""Authentication, CSRF, login throttling and per-user query scoping.

Two ways to be logged in:
  * Flask signed session cookie        - browser session ("Remember me" unchecked)
  * device token cookie, 90 days       - "Remember me" checked; SHA-256 stored server-side,
                                         expiry slides forward on use, revocable per device
"""
import hashlib
import secrets
import threading
import time
from datetime import timedelta
from functools import wraps

from flask import abort, current_app, g, jsonify, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from .events import event_logger
from .models import DeviceToken, Invite, User, db, utcnow

DEVICE_COOKIE = 'fitmon_device'
DEVICE_TOKEN_DAYS = 90
INVITE_DAYS = 7
MIN_PASSWORD_LENGTH = 10

# Endpoints reachable without a login. Everything else is denied by default in
# require_login(), so a new route is private unless it is deliberately added here.
PUBLIC_ENDPOINTS = {
    'static', 'core.ping', 'pages.login_page', 'pages.invite_page',
    'auth.login', 'auth.invite_info', 'auth.invite_accept',
}
CSRF_EXEMPT = {'auth.login', 'auth.invite_accept'}


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def hash_password(password: str) -> str:
    return generate_password_hash(password, method='scrypt')


def normalize_username(username: str) -> str:
    return (username or '').strip().casefold()


def validate_new_password(password: str) -> str | None:
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
    return None


def create_user(username: str, password: str, role: str = 'user', display_name: str | None = None) -> User:
    username = normalize_username(username)
    if not username or not username.replace('_', '').replace('-', '').replace('.', '').isalnum():
        raise ValueError('Username may contain letters, digits, dot, dash and underscore.')
    problem = validate_new_password(password)
    if problem:
        raise ValueError(problem)
    if User.query.filter_by(username=username).first():
        raise ValueError('That username is taken.')
    user = User(username=username, display_name=display_name or username,
                password_hash=hash_password(password), role=role)
    db.session.add(user)
    db.session.commit()
    event_logger.info('auth.user_created', f'user {username} created', user_id=user.id, role=role)
    return user


# --------------------------------------------------------------------------- throttling

class LoginThrottle:
    """Growing back-off per username and per IP. In-memory: a restart forgiving everyone is
    acceptable for <= 10 users behind Tailscale, and it cannot lock the DB."""
    FREE_ATTEMPTS = 5
    BASE_DELAY = 5
    MAX_DELAY = 15 * 60

    def __init__(self):
        self._lock = threading.Lock()
        self._state: dict[str, tuple[int, float]] = {}

    def retry_after(self, *keys: str) -> int:
        now = time.time()
        with self._lock:
            waits = [self._state[k][1] - now for k in keys if k in self._state]
        return max(0, int(max(waits, default=0)) + (1 if max(waits, default=0) > 0 else 0))

    def failure(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                count = self._state.get(key, (0, 0))[0] + 1
                delay = 0
                if count > self.FREE_ATTEMPTS:
                    delay = min(self.MAX_DELAY, self.BASE_DELAY * 2 ** (count - self.FREE_ATTEMPTS - 1))
                self._state[key] = (count, now + delay)

    def success(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._state.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._state.clear()


throttle = LoginThrottle()


# --------------------------------------------------------------------------- device tokens

def _device_name() -> str:
    ua = request.user_agent.string or 'unknown device'
    return ua[:200]


def issue_device_token(user: User) -> str:
    raw = secrets.token_urlsafe(32)
    db.session.add(DeviceToken(
        user_id=user.id, token_sha256=sha256(raw), device_name=_device_name(),
        expires_at=utcnow() + timedelta(days=DEVICE_TOKEN_DAYS)))
    db.session.commit()
    return raw


def set_device_cookie(response, raw_token: str):
    response.set_cookie(
        DEVICE_COOKIE, raw_token, max_age=DEVICE_TOKEN_DAYS * 86400, httponly=True,
        samesite='Lax', secure=current_app.config['SESSION_COOKIE_SECURE'], path='/')
    return response


def clear_device_cookie(response):
    response.delete_cookie(DEVICE_COOKIE, path='/')
    return response


def _user_from_device_cookie() -> User | None:
    raw = request.cookies.get(DEVICE_COOKIE)
    if not raw:
        return None
    token = DeviceToken.query.filter_by(token_sha256=sha256(raw)).first()
    now = utcnow()
    if not token or token.revoked_at or token.expires_at < now:
        return None
    user = db.session.get(User, token.user_id)
    if not user or not user.is_active:
        return None
    # Slide the expiry, but at most hourly so ordinary requests stay read-only.
    if not token.last_used_at or now - token.last_used_at > timedelta(hours=1):
        token.last_used_at = now
        token.expires_at = now + timedelta(days=DEVICE_TOKEN_DAYS)
        db.session.commit()
    g.device_token_id = token.id
    return user


def revoke_all_tokens(user_id: int, except_id: int | None = None) -> int:
    q = DeviceToken.query.filter(DeviceToken.user_id == user_id, DeviceToken.revoked_at.is_(None))
    if except_id:
        q = q.filter(DeviceToken.id != except_id)
    n = q.update({'revoked_at': utcnow()}, synchronize_session=False)
    db.session.commit()
    return n


# --------------------------------------------------------------------------- request hooks

def csrf_token() -> str:
    if 'csrf' not in session:
        session['csrf'] = secrets.token_urlsafe(24)
    return session['csrf']


def load_user() -> None:
    g.user = None
    g.device_token_id = None
    uid = session.get('uid')
    if uid:
        user = db.session.get(User, uid)
        # pw_stamp ties a cookie session to the password it was created under, so a
        # password change logs out every other browser session too.
        if user and user.is_active and session.get('pw') == user.password_hash[-16:]:
            g.user = user
            return
        session.pop('uid', None)
    g.user = _user_from_device_cookie()


def require_login():
    """Deny by default. Runs after load_user for every request."""
    endpoint = request.endpoint
    if endpoint is None or endpoint in PUBLIC_ENDPOINTS:
        return None
    if g.user is None:
        if request.path.startswith('/api/'):
            return jsonify({'error': 'login_required'}), 401
        from flask import redirect, url_for
        return redirect(url_for('pages.login_page'))
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE') and endpoint not in CSRF_EXEMPT:
        sent = request.headers.get('X-CSRF-Token', '')
        if not sent or not secrets.compare_digest(sent, session.get('csrf', '')):
            return jsonify({'error': 'csrf_failed'}), 403
    return None


def start_session(user: User) -> None:
    session.clear()
    session['uid'] = user.id
    session['pw'] = user.password_hash[-16:]
    csrf_token()


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not g.user or not g.user.is_admin:
            # 404, not 403: a non-admin learns nothing about what exists.
            abort(404)
        return fn(*args, **kwargs)
    return wrapper


# --------------------------------------------------------------------------- scoping

def scoped(model):
    """The only way routes should start a query on a user-owned table."""
    return model.query.filter(model.user_id == g.user.id)


def get_owned(model, obj_id: int):
    """Fetch by id within the current user's rows. Someone else's id is a 404, never a 403."""
    obj = scoped(model).filter(model.id == obj_id).first()
    if obj is None:
        abort(404)
    return obj


# --------------------------------------------------------------------------- invites

def create_invite(created_by: int, role: str = 'user', note: str | None = None) -> tuple[Invite, str]:
    raw = secrets.token_urlsafe(24)
    invite = Invite(token_sha256=sha256(raw), role=role if role in ('user', 'admin') else 'user',
                    note=(note or '')[:200], created_by=created_by,
                    expires_at=utcnow() + timedelta(days=INVITE_DAYS))
    db.session.add(invite)
    db.session.commit()
    event_logger.info('auth.invite_created', 'invite created', invite_id=invite.id,
                      created_by=created_by, role=invite.role)
    return invite, raw


def find_valid_invite(raw: str) -> Invite | None:
    invite = Invite.query.filter_by(token_sha256=sha256(raw or '')).first()
    if not invite or invite.used_at or invite.revoked_at or invite.expires_at < utcnow():
        return None
    return invite
