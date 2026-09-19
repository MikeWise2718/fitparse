"""The fitparse library, surfaced: every message and field in a file, known or unknown,
with raw values, units and definition numbers. Reads the original FIT on demand."""
import os
import struct
from datetime import datetime
from functools import lru_cache

import fitparse
from fitparse.utils import FitCRCError

MAX_ROWS_CACHED = 500_000


def header_info(path) -> dict:
    with open(path, 'rb') as fh:
        head = fh.read(14)
    if len(head) < 12:
        return {}
    size, protocol, profile, data_size = struct.unpack('<BBHI', head[:8])
    return {
        'header_size': size,
        'protocol_version': f'{protocol >> 4}.{protocol & 0x0F}',
        'profile_version': f'{profile // 100}.{profile % 100:02d}',
        'data_size': data_size,
        'signature_ok': head[8:12] == b'.FIT',
        'file_size': os.path.getsize(path),
    }


def check_crc(path) -> dict:
    """Full pass with CRC validation on. Expensive (a whole parse), so only on request."""
    try:
        for _ in fitparse.UncachedFitFile(str(path), check_crc=True).get_messages():
            pass
        return {'crc_ok': True, 'detail': None}
    except FitCRCError as exc:
        return {'crc_ok': False, 'detail': str(exc)}
    except Exception as exc:
        return {'crc_ok': None, 'detail': f'{type(exc).__name__}: {exc}'}


def _safe(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, (tuple, list)):
        return [_safe(v) for v in value]
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)


@lru_cache(maxsize=6)
def _load(path: str, mtime: float, message: str, standard_units: bool):
    processor = fitparse.StandardUnitsDataProcessor() if standard_units else None
    fit = fitparse.UncachedFitFile(path, data_processor=processor)
    fields: dict = {}
    rows, error = [], None
    try:
        for msg in fit.get_messages(message):
            if not hasattr(msg, 'fields'):
                continue
            row = {}
            for fd in msg.fields:
                if fd.name not in fields:
                    fields[fd.name] = {
                        'name': fd.name, 'units': fd.units, 'def_num': getattr(fd, 'def_num', None),
                        'unknown': fd.name.startswith('unknown_'),
                        'developer': getattr(fd.field, 'dev_data_index', None) is not None
                        if getattr(fd, 'field', None) is not None else False,
                        'numeric': False,
                    }
                if isinstance(fd.value, (int, float)) and not isinstance(fd.value, bool):
                    fields[fd.name]['numeric'] = True
                row[fd.name] = (_safe(fd.value), _safe(fd.raw_value))
            rows.append(row)
            if len(rows) >= MAX_ROWS_CACHED:
                error = f'stopped after {MAX_ROWS_CACHED} rows'
                break
    except Exception as exc:   # corrupt tail: show what was readable
        error = f'{type(exc).__name__}: {exc}'
    return list(fields.values()), rows, error


def _cached(path, message: str, standard_units: bool):
    path = str(path)
    return _load(path, os.path.getmtime(path), message, bool(standard_units))


def message_page(path, message: str, page: int = 1, per_page: int = 100, standard_units: bool = False) -> dict:
    fields, rows, error = _cached(path, message, standard_units)
    per_page = max(1, min(per_page, 1000))
    page = max(1, page)
    chunk = rows[(page - 1) * per_page: page * per_page]
    names = [f['name'] for f in fields]
    return {
        'message': message, 'total': len(rows), 'page': page, 'per_page': per_page,
        'fields': fields, 'error': error,
        'rows': [[row.get(n, (None, None))[0] for n in names] for row in chunk],
        'raw': [[row.get(n, (None, None))[1] for n in names] for row in chunk],
    }


def field_series(path, message: str, field: str, points: int = 3000) -> dict:
    fields, rows, error = _cached(path, message, False)
    values = [row.get(field, (None, None))[0] for row in rows]
    values = [v if isinstance(v, (int, float)) and not isinstance(v, bool) else None for v in values]
    step = max(1, len(values) // points)
    units = next((f['units'] for f in fields if f['name'] == field), None)
    return {'message': message, 'field': field, 'units': units, 'step': step,
            'index': list(range(0, len(values), step)), 'values': values[::step], 'error': error}


def message_dump(path, message: str, standard_units: bool = False) -> list:
    """Same shape as `scripts/fitdump -t json`: one object per message with a fields list."""
    fields, rows, _ = _cached(path, message, standard_units)
    units = {f['name']: f['units'] for f in fields}
    return [{'type': 'data', 'name': message,
             'fields': [{'name': n, 'value': v[0], 'raw_value': v[1], 'units': units.get(n)}
                        for n, v in row.items()]} for row in rows]
