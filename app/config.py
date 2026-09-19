import os
import secrets
from pathlib import Path

PORT = 8640  # reserved in D:\hw\pokeflute\data\ports.json (fitmon / munchlax)


def fitmon_home() -> Path:
    """Runtime data root. Code lives in the repo; everything mutable lives here."""
    return Path(os.environ.get('FITMON_HOME') or Path.home() / 'fitmon')


def _secret_key(home: Path) -> str:
    """Per-install random key, generated on first run. Never a guessable default:
    whoever knows the key can forge a session cookie."""
    path = home / 'auth' / 'secret_key'
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_hex(32), encoding='utf-8')
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path.read_text(encoding='utf-8').strip()


def make_config(home: Path | None = None) -> dict:
    home = Path(home) if home else fitmon_home()
    for sub in ('data', 'logs', 'auth', 'users'):
        (home / sub).mkdir(parents=True, exist_ok=True)
    db_path = (home / 'data' / 'fitmon.db').as_posix()
    return {
        'FITMON_HOME': home,
        'SECRET_KEY': _secret_key(home),
        'SQLALCHEMY_DATABASE_URI': f'sqlite:///{db_path}',
        'SQLALCHEMY_TRACK_MODIFICATIONS': False,
        'MAX_CONTENT_LENGTH': 200 * 1024 * 1024,  # one request; zips of many activities
        'SESSION_COOKIE_HTTPONLY': True,
        'SESSION_COOKIE_SAMESITE': 'Lax',
        'JSON_SORT_KEYS': False,
    }
