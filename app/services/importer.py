"""The single path by which FIT data enters the index - uploads, zips, server-side scans and
the Garmin sync all end in import_bytes(). Runs in the job worker, never in a web request.
"""
import hashlib
import io
import json
import os
import zipfile
from pathlib import Path

from sqlalchemy import func

from ..events import event_logger
from ..models import (BestEffort, Device, FitFile, GarminActivity, Lap, Length, ProfileSnapshot,
                      Record, SCHEMA_VERSION, Session, SessionExclusion, SessionTrim, StrengthSet,
                      ZoneTime, db)
from ..settings import get_global_settings, get_user_settings, user_dir
from . import metrics
from .fit_parser import ParsedFile, parse_fit_file

MAX_FIT_BYTES = 100 * 1024 * 1024
MAX_ZIP_MEMBERS = 20_000
MAX_ZIP_TOTAL_BYTES = 4 * 1024 ** 3
MAX_ZIP_DEPTH = 2          # Garmin's account export nests zips of .fit files inside the main zip


class ImportRejected(Exception):
    pass


def fit_path(home: Path, user_id: int, sha: str) -> Path:
    return user_dir(home, user_id) / 'fit' / sha[:2] / f'{sha}.fit'


def looks_like_fit(raw: bytes) -> bool:
    return len(raw) >= 14 and raw[8:12] == b'.FIT'


def _quota_ok(home: Path, user_id: int, extra: int) -> bool:
    limits = get_global_settings(home)
    count, size = db.session.query(func.count(FitFile.id), func.coalesce(func.sum(FitFile.size), 0)) \
        .filter(FitFile.user_id == user_id).one()
    return (count < limits['user_quota_files'] and
            size + extra <= limits['user_quota_mb'] * 1024 * 1024)


def resolve_zones(home: Path, user_id: int, profile: dict, at=None) -> dict:
    """Zone references for one activity: user override > this file's snapshot > the most
    recent earlier snapshot. Zones are dated - an FTP from 2022 must not score a 2026 ride."""
    overrides = get_user_settings(home, user_id)['zones']
    out = {'female': (profile.get('gender') == 'female')}
    for key in ('max_hr', 'threshold_hr', 'resting_hr', 'ftp'):
        value = overrides.get(key) or profile.get(key)
        if not value:
            q = ProfileSnapshot.query.filter(ProfileSnapshot.user_id == user_id,
                                             getattr(ProfileSnapshot, key).isnot(None))
            if at is not None:
                q = q.filter(ProfileSnapshot.at <= at)
            snap = q.order_by(ProfileSnapshot.at.desc()).first()
            value = getattr(snap, key) if snap else None
        out[key] = value
    return out


def _clean(row: dict, model) -> dict:
    cols = model.__table__.columns.keys()
    return {k: v for k, v in row.items() if k in cols}


def index_parsed(home: Path, file_row: FitFile, parsed: ParsedFile) -> None:
    """(Re)write every derived row for one file inside the caller's transaction."""
    user_id = file_row.user_id
    Session.query.filter_by(file_id=file_row.id).delete()
    Device.query.filter_by(file_id=file_row.id).delete()
    ProfileSnapshot.query.filter_by(file_id=file_row.id).delete()
    db.session.flush()

    file_row.parse_status, file_row.parse_error = parsed.status, parsed.error
    file_row.message_counts = json.dumps(parsed.counts)
    file_row.schema_version = SCHEMA_VERSION
    for key, value in parsed.meta.items():
        setattr(file_row, key, value)
    if parsed.status == 'failed':
        return

    start = parsed.meta.get('start_time')
    if any(parsed.profile.get(k) for k in ('weight_kg', 'resting_hr', 'max_hr', 'threshold_hr', 'ftp')):
        db.session.add(ProfileSnapshot(file_id=file_row.id, user_id=user_id, at=start,
                                       **_clean(parsed.profile, ProfileSnapshot)))
    for dev in parsed.devices:
        db.session.add(Device(file_id=file_row.id, user_id=user_id, **_clean(dev, Device)))

    zones = resolve_zones(home, user_id, parsed.profile, at=start)
    load_model = get_user_settings(home, user_id)['load_model']
    garmin = None
    if file_row.garmin_activity_id:
        garmin = db.session.get(GarminActivity, (user_id, file_row.garmin_activity_id))

    # Manual trims and exclusions survive a re-index: keyed by (file, leg), not session id.
    trims = {t.idx: t.end_t for t in SessionTrim.query.filter_by(file_id=file_row.id).all()}
    excluded = {e.idx for e in SessionExclusion.query.filter_by(file_id=file_row.id).all()}

    for sess in parsed.sessions:
        if sess['idx'] in trims:
            metrics.apply_trim(sess, trims[sess['idx']])
        metrics.compute_session(sess, zones, load_model)
        sess['excluded'] = sess['idx'] in excluded
        row = Session(file_id=file_row.id, user_id=user_id, **_clean(sess, Session))
        if garmin and garmin.name and len(parsed.sessions) == 1:
            row.name = garmin.name
        db.session.add(row)
        db.session.flush()
        sid = row.id
        for model, key in ((Lap, 'laps'), (Length, 'lengths'), (StrengthSet, 'sets')):
            rows = [{**_clean(item, model), 'session_id': sid} for item in sess[key]]
            if rows:
                db.session.execute(model.__table__.insert(), rows)
        if sess['records']:
            db.session.execute(Record.__table__.insert(),
                               [{**rec, 'session_id': sid} for rec in sess['records']])
        if sess['_best']:
            db.session.execute(BestEffort.__table__.insert(), [
                {**b, 'session_id': sid, 'user_id': user_id, 'at': row.start_time, 'sport': row.sport}
                for b in sess['_best']])
        if sess['_zones']:
            db.session.execute(ZoneTime.__table__.insert(),
                               [{**z, 'session_id': sid} for z in sess['_zones']])


def _find_twin(user_id: int, parsed: ParsedFile):
    """Another file of this user holding the same activity: same device + creation time, or the
    same first-leg start second (repair tools tend to rewrite file_id but not the activity)."""
    serial, created = parsed.meta.get('serial_number'), parsed.meta.get('time_created')
    start = parsed.meta.get('start_time')
    clauses = []
    if serial and created:
        clauses.append(db.and_(FitFile.serial_number == serial, FitFile.time_created == created))
    if start:
        clauses.append(FitFile.start_time == start)
    if not clauses:
        return None
    return FitFile.query.filter(FitFile.user_id == user_id, db.or_(*clauses)).first()


def _quality(parsed: ParsedFile) -> tuple:
    """Higher is better: legs that have a duration, then a clean parse, then sample count.
    Legs first, because a 'repaired' file that parses cleanly but has collapsed a five-leg
    triathlon into one 57 km "run" is worse than a truncated original with all five."""
    return (sum(1 for s in parsed.sessions if s.get('timer_s')), parsed.status == 'ok',
            sum(len(s['records']) for s in parsed.sessions))


def _quality_of_row(row: FitFile) -> tuple:
    sessions = Session.query.filter_by(file_id=row.id).all()
    return (sum(1 for s in sessions if s.timer_s), row.parse_status == 'ok',
            sum(s.n_records or 0 for s in sessions))


def import_bytes(home: Path, user_id: int, raw: bytes, name: str, source: str = 'upload',
                 garmin_activity_id: str | None = None) -> dict:
    """Returns {'status': imported|partial|duplicate|replaced|failed|rejected, 'file_id', 'reason'}."""
    name = os.path.basename(name or 'activity.fit')[:255]
    if len(raw) > MAX_FIT_BYTES:
        return {'status': 'rejected', 'name': name, 'reason': 'file too large'}
    if not looks_like_fit(raw):
        return {'status': 'rejected', 'name': name, 'reason': 'not a FIT file'}

    sha = hashlib.sha256(raw).hexdigest()
    existing = FitFile.query.filter_by(user_id=user_id, sha256=sha).first()
    if existing:
        if garmin_activity_id and not existing.garmin_activity_id:
            existing.garmin_activity_id = garmin_activity_id
            db.session.commit()
        return {'status': 'duplicate', 'name': name, 'file_id': existing.id}
    if not _quota_ok(home, user_id, len(raw)):
        return {'status': 'rejected', 'name': name, 'reason': 'storage quota reached'}

    path = fit_path(home, user_id, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)

    parsed = parse_fit_file(path)

    # Same activity, different bytes: a re-export, a clean copy of a corrupt file, or the
    # output of a FIT "repair" tool. Keep exactly one - whichever copy is better - so the
    # activity is never counted twice in load and records.
    replaced = None
    if parsed.status != 'failed':
        twin = _find_twin(user_id, parsed)
        if twin is not None:
            if _quality(parsed) > _quality_of_row(twin):
                replaced = twin.id
                garmin_activity_id = garmin_activity_id or twin.garmin_activity_id
                delete_file(home, twin, commit=False)
            else:
                path.unlink(missing_ok=True)
                if garmin_activity_id and not twin.garmin_activity_id:
                    twin.garmin_activity_id = garmin_activity_id
                    db.session.commit()
                return {'status': 'duplicate', 'name': name, 'file_id': twin.id}

    row = FitFile(user_id=user_id, sha256=sha, original_name=name, size=len(raw), source=source,
                  garmin_activity_id=garmin_activity_id)
    db.session.add(row)
    db.session.flush()
    index_parsed(home, row, parsed)
    db.session.commit()

    status = 'replaced' if replaced else {'ok': 'imported'}.get(parsed.status, parsed.status)
    event_logger.info('import.file', f'{name}: {status}', user_id=user_id, file_id=row.id,
                      source=source, status=status, sessions=len(parsed.sessions), error=parsed.error)
    return {'status': status, 'name': name, 'file_id': row.id, 'reason': parsed.error}


def iter_zip_fits(raw_zip, depth: int = 1, budget: list | None = None):
    """Yield (name, bytes) for every .fit in a zip. Members are read from memory only -
    nothing is ever extracted to a path taken from the archive - and the total is capped."""
    budget = budget if budget is not None else [MAX_ZIP_TOTAL_BYTES, MAX_ZIP_MEMBERS]
    with zipfile.ZipFile(raw_zip) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            lower = info.filename.lower()
            if not lower.endswith(('.fit', '.zip')):
                continue
            budget[1] -= 1
            if budget[1] < 0:
                raise ImportRejected('zip has too many members')
            limit = MAX_FIT_BYTES if lower.endswith('.fit') else MAX_ZIP_TOTAL_BYTES
            if info.file_size > limit:
                continue
            with zf.open(info) as fh:
                data = fh.read(limit + 1)   # the declared size can lie; the read cannot
            if len(data) > limit:
                continue
            budget[0] -= len(data)
            if budget[0] < 0:
                raise ImportRejected('zip expands beyond the size limit')
            if lower.endswith('.fit'):
                yield os.path.basename(info.filename), data
            elif depth < MAX_ZIP_DEPTH:
                try:
                    yield from iter_zip_fits(io.BytesIO(data), depth + 1, budget)
                except zipfile.BadZipFile:
                    continue


def import_upload(home: Path, user_id: int, raw: bytes, name: str, source: str = 'upload'):
    """Yield one result per FIT file contained in an uploaded .fit or .zip."""
    if name.lower().endswith('.zip') or raw[:2] == b'PK':
        try:
            for member, data in iter_zip_fits(io.BytesIO(raw)):
                yield import_bytes(home, user_id, data, member, source='zip')
        except (zipfile.BadZipFile, ImportRejected) as exc:
            yield {'status': 'rejected', 'name': name, 'reason': str(exc)}
    else:
        yield import_bytes(home, user_id, raw, name, source=source)


def scan_paths(directory: str) -> list:
    """Admin-only: .fit and .zip files under a server directory, each path once.
    (Do not glob '*.fit' + '*.FIT': on a case-insensitive filesystem that doubles every file.)"""
    seen, out = set(), []
    for root, _, names in os.walk(directory):
        for name in names:
            if name.lower().endswith(('.fit', '.zip')):
                full = os.path.join(root, name)
                key = os.path.normcase(os.path.abspath(full))
                if key not in seen:
                    seen.add(key)
                    out.append(full)
    return sorted(out)


def delete_file(home: Path, file_row: FitFile, commit: bool = True) -> None:
    path = fit_path(home, file_row.user_id, file_row.sha256)
    Session.query.filter_by(file_id=file_row.id).delete()
    db.session.delete(file_row)
    if commit:
        db.session.commit()
    path.unlink(missing_ok=True)


def reindex_file(home: Path, file_row: FitFile) -> str:
    path = fit_path(home, file_row.user_id, file_row.sha256)
    if not path.exists():
        file_row.parse_status, file_row.parse_error = 'failed', 'original file missing'
        db.session.commit()
        return 'failed'
    index_parsed(home, file_row, parse_fit_file(path))
    db.session.commit()
    return file_row.parse_status
