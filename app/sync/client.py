"""The only module that touches `garminconnect`.

That library drives Garmin's unofficial mobile API: it breaks when Garmin changes things and it
acts *as the user*. Keeping every call behind this seam means one file to patch, and everything
above it is tested against a fake with the same five methods.

Tokens: garminconnect 0.3.x serialises its session to a JSON string (`client.dumps()`) and can
log in from one (`login(<json>)`). We keep that string Fernet-encrypted at
~/fitmon/users/<id>/garmin/tokens.enc - plaintext tokens never touch the disk. The key lives in
~/fitmon/auth/, so this protects backups and stray copies, not against root on the host.
"""
import os
import threading
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from ..settings import user_dir

PENDING_MFA_SECONDS = 300


class SyncAuthError(Exception):
    """No usable tokens, or Garmin rejected them: the user has to connect again."""


class SyncRateLimited(Exception):
    """HTTP 429. All accounts share one source IP, so this stops the whole run."""


class MfaRequired(Exception):
    pass


def _fernet(home: Path) -> Fernet:
    path = Path(home) / 'auth' / 'garmin_token_key'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(Fernet.generate_key())
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return Fernet(path.read_bytes().strip())


def token_path(home: Path, user_id: int) -> Path:
    folder = user_dir(home, user_id) / 'garmin'
    folder.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(folder, 0o700)
    except OSError:
        pass
    return folder / 'tokens.enc'


def has_tokens(home: Path, user_id: int) -> bool:
    return token_path(home, user_id).exists()


def save_tokens(home: Path, user_id: int, token_json: str) -> None:
    path = token_path(home, user_id)
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(_fernet(home).encrypt(token_json.encode('utf-8')))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def load_tokens(home: Path, user_id: int) -> str:
    path = token_path(home, user_id)
    if not path.exists():
        raise SyncAuthError('not connected to Garmin')
    try:
        return _fernet(home).decrypt(path.read_bytes()).decode('utf-8')
    except InvalidToken as exc:
        raise SyncAuthError('stored Garmin tokens cannot be decrypted') from exc


def delete_tokens(home: Path, user_id: int) -> None:
    token_path(home, user_id).unlink(missing_ok=True)


def _translate(exc: Exception) -> Exception:
    from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError
    text = str(exc).lower()
    if isinstance(exc, GarminConnectTooManyRequestsError) or '429' in text or 'too many requests' in text:
        return SyncRateLimited(str(exc)[:300])
    if isinstance(exc, GarminConnectAuthenticationError) or '401' in text or '403' in text:
        return SyncAuthError(str(exc)[:300])
    return exc


class GarminClient:
    """Thin wrapper. Everything the sync needs, nothing else."""

    def __init__(self, api, home: Path, user_id: int):
        self._api, self._home, self._user_id = api, home, user_id

    def persist(self) -> None:
        """Access tokens get refreshed during a session; store the newest state."""
        save_tokens(self._home, self._user_id, self._api.client.dumps())

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            raise _translate(exc) from exc

    def list_activities(self, start: int, limit: int) -> list:
        return self._call(self._api.get_activities, start, limit) or []

    def download_original(self, activity_id: str) -> bytes:
        return self._call(self._api.download_activity, activity_id,
                          dl_fmt=self._api.ActivityDownloadFormat.ORIGINAL)

    def health(self, method: str, *args):
        """`method` is one of the get_* names listed in sync/health.py."""
        return self._call(getattr(self._api, method), *args)


def connect(home: Path, user_id: int) -> GarminClient:
    """Log in from stored tokens. Never uses a password."""
    from garminconnect import Garmin
    token_json = load_tokens(home, user_id)
    api = Garmin()
    try:
        api.login(token_json)
    except Exception as exc:
        translated = _translate(exc)
        if isinstance(translated, SyncRateLimited):
            raise translated from exc
        raise SyncAuthError(f'Garmin rejected the stored tokens: {str(exc)[:200]}') from exc
    client = GarminClient(api, home, user_id)
    client.persist()
    return client


# --------------------------------------------------------------------------- first login

_pending: dict = {}
_pending_lock = threading.Lock()


def _prune() -> None:
    now = time.time()
    for key in [k for k, v in _pending.items() if now - v[2] > PENDING_MFA_SECONDS]:
        _pending.pop(key, None)


def login_with_password(home: Path, user_id: int, email: str, password: str) -> None:
    """Exchange credentials for tokens. The password is used for this call and then dropped;
    it is never written anywhere. Raises MfaRequired if Garmin wants a code."""
    from garminconnect import Garmin
    api = Garmin(email=email, password=password, return_on_mfa=True)
    try:
        status, state = api.login()
    except Exception as exc:
        raise _translate(exc) from exc
    if status == 'needs_mfa':
        with _pending_lock:
            _prune()
            _pending[user_id] = (api, state, time.time())
        raise MfaRequired()
    api.password = None
    save_tokens(home, user_id, api.client.dumps())


def complete_mfa(home: Path, user_id: int, code: str) -> None:
    with _pending_lock:
        _prune()
        entry = _pending.pop(user_id, None)
    if not entry:
        raise SyncAuthError('no login is waiting for a code (it may have expired) - start again')
    api, state, _ = entry
    try:
        api.resume_login(state, code)
    except Exception as exc:
        raise _translate(exc) from exc
    api.password = None
    save_tokens(home, user_id, api.client.dumps())
