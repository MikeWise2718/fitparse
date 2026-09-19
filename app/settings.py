"""Server-side settings, persisted as JSON under the runtime data dir.

Per-user preferences: ~/fitmon/users/<id>/settings.json
Global (admin):       ~/fitmon/data/settings.json
"""
import json
import os
import threading
from pathlib import Path

USER_DEFAULTS = {
    'units': 'metric',            # metric | statute
    'week_start': 'monday',       # monday | sunday
    'default_range_days': 365,
    'load_model': 'auto',         # auto (TSS when power+FTP, else TRIMP) | trimp | tss
    'load_sports': [],            # empty = all sports count toward load
    'zones': {                    # overrides; null = take from the FIT zones_target snapshots
        'max_hr': None, 'threshold_hr': None, 'resting_hr': None, 'ftp': None,
    },
}

GLOBAL_DEFAULTS = {
    'public_base_url': '',        # e.g. https://munchlax.<tailnet>.ts.net — used for invite links
    'user_quota_mb': 5120,
    'user_quota_files': 5000,
    'garmin_daily_request_budget': 3000,
    'garmin_delay_seconds': 1.0,
}

_lock = threading.Lock()


def _merge(defaults: dict, stored: dict) -> dict:
    out = {}
    for key, default in defaults.items():
        value = stored.get(key, default)
        if isinstance(default, dict) and isinstance(value, dict):
            value = _merge(default, value)
        out[key] = value
    return out


def _load(path: Path, defaults: dict) -> dict:
    stored = {}
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            stored = {}
    return _merge(defaults, stored if isinstance(stored, dict) else {})


def _save(path: Path, defaults: dict, updates: dict) -> dict:
    with _lock:
        current = _load(path, defaults)
        # Unknown keys are dropped: the file only ever holds what the app understands.
        merged = _merge(defaults, {**current, **{k: v for k, v in updates.items() if k in defaults}})
        for key, default in defaults.items():
            if isinstance(default, dict) and isinstance(updates.get(key), dict):
                merged[key] = _merge(default, {**current[key], **updates[key]})
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(merged, indent=2), encoding='utf-8')
        os.replace(tmp, path)
        return merged


def user_dir(home: Path, user_id: int) -> Path:
    path = Path(home) / 'users' / str(int(user_id))
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_user_settings(home: Path, user_id: int) -> dict:
    return _load(user_dir(home, user_id) / 'settings.json', USER_DEFAULTS)


def save_user_settings(home: Path, user_id: int, updates: dict) -> dict:
    return _save(user_dir(home, user_id) / 'settings.json', USER_DEFAULTS, updates)


def get_global_settings(home: Path) -> dict:
    return _load(Path(home) / 'data' / 'settings.json', GLOBAL_DEFAULTS)


def save_global_settings(home: Path, updates: dict) -> dict:
    return _save(Path(home) / 'data' / 'settings.json', GLOBAL_DEFAULTS, updates)
