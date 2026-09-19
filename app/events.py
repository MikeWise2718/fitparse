"""Structured JSONL event log: one JSON object per line in ~/fitmon/logs/events.jsonl.

    event_logger.info('auth.login_succeeded', 'mike logged in', user_id=1)
"""
import json
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path

from .config import fitmon_home

# Never let a credential reach the log, whatever a caller passes in.
_REDACT = {'password', 'new_password', 'old_password', 'token', 'code', 'mfa', 'secret', 'csrf'}


class EventLogger:
    def __init__(self):
        self._lock = threading.Lock()
        self._path: Path | None = None

    def configure(self, home: Path | None = None) -> None:
        home = Path(home) if home else fitmon_home()
        self._path = home / 'logs' / 'events.jsonl'
        self._path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        if self._path is None:
            self.configure()
        return self._path

    def log(self, level: str, event: str, message: str, **fields) -> dict:
        record = {
            'timestamp': datetime.now(timezone.utc).isoformat(timespec='milliseconds'),
            'level': level,
            'event': event,
            'message': message,
            'hostname': socket.gethostname(),
        }
        for key, value in fields.items():
            record[key] = '[redacted]' if key.lower() in _REDACT else value
        line = json.dumps(record, default=str, ensure_ascii=False)
        with self._lock:
            with open(self.path, 'a', encoding='utf-8') as fh:
                fh.write(line + '\n')
        return record

    def info(self, event, message, **fields):
        return self.log('info', event, message, **fields)

    def warning(self, event, message, **fields):
        return self.log('warning', event, message, **fields)

    def error(self, event, message, **fields):
        return self.log('error', event, message, **fields)

    def tail(self, n: int = 200, prefix: str | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        with open(self.path, encoding='utf-8') as fh:
            lines = fh.readlines()[-5000:]
        out = []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if prefix and not str(rec.get('event', '')).startswith(prefix):
                continue
            out.append(rec)
        return out[-n:]


event_logger = EventLogger()
