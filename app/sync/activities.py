"""Garmin Connect -> importer. list -> diff against garmin_activities -> download ORIGINAL ->
unwrap -> importer.import_bytes(). No second code path into the DB.
"""
import io
import json
import random
import re
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

from ..events import event_logger
from ..models import FitFile, GarminAccount, GarminActivity, Meta, Session, db, utcnow
from ..services import importer
from ..settings import get_global_settings
from . import client as gc

PAGE_SIZE = 100          # Garmin caps a list request at 1000; stay well under
KNOWN_STREAK_STOP = 50   # incremental runs stop paging after this many already-known ids in a row
MAX_ATTEMPTS = 3
ID_IN_NAME = re.compile(r'^(\d{6,})_ACTIVITY\.fit$', re.IGNORECASE)


class BudgetExhausted(Exception):
    pass


def _budget_key() -> str:
    return f'garmin_requests:{date.today().isoformat()}'


def spend_request(home: Path, n: int = 1) -> None:
    """Global daily cap across all users: every account leaves from the same IP."""
    row = db.session.get(Meta, _budget_key())
    if row is None:
        row = Meta(key=_budget_key(), value='0')
        db.session.add(row)
    used = int(row.value) + n
    if used > get_global_settings(home)['garmin_daily_request_budget']:
        raise BudgetExhausted()
    row.value = str(used)
    db.session.commit()


def account(user_id: int) -> GarminAccount:
    row = db.session.get(GarminAccount, user_id)
    if row is None:
        row = GarminAccount(user_id=user_id)
        db.session.add(row)
        db.session.commit()
    return row


def extract_fit(raw: bytes) -> bytes | None:
    """ORIGINAL comes back as a zip holding one <id>_ACTIVITY.fit, occasionally as a bare FIT.
    Returns None when there is no FIT inside (manual entries, some third-party uploads)."""
    if raw[:2] == b'PK':
        try:
            for _, data in importer.iter_zip_fits(io.BytesIO(raw)):
                return data
        except (zipfile.BadZipFile, importer.ImportRejected):
            return None
        return None
    return raw if importer.looks_like_fit(raw) else None


def _parse_start(item: dict):
    text = item.get('startTimeGMT') or item.get('startTimeLocal')
    try:
        return datetime.fromisoformat(text.replace(' ', 'T')) if text else None
    except ValueError:
        return None


def preseed_from_archive(user_id: int) -> int:
    """Files imported by hand as '<activityId>_ACTIVITY.fit' already tell us their Garmin id."""
    n = 0
    for f in FitFile.query.filter(FitFile.user_id == user_id, FitFile.garmin_activity_id.is_(None)).all():
        match = ID_IN_NAME.match(f.original_name or '')
        if match:
            f.garmin_activity_id = match.group(1)
            n += 1
    db.session.commit()
    return n


def refresh_listing(home: Path, user_id: int, client, full: bool, limit: int | None, progress) -> int:
    start, streak, seen = 0, 0, 0
    while True:
        spend_request(home)
        batch = client.list_activities(start, PAGE_SIZE)
        if not batch:
            break
        for item in batch:
            aid = str(item.get('activityId'))
            seen += 1
            row = db.session.get(GarminActivity, (user_id, aid))
            if row is None:
                streak = 0
                linked = FitFile.query.filter_by(user_id=user_id, garmin_activity_id=aid).first()
                row = GarminActivity(user_id=user_id, activity_id=aid,
                                     status='imported' if linked else 'new',
                                     file_id=linked.id if linked else None)
                db.session.add(row)
            else:
                streak += 1
            row.name = (item.get('activityName') or '').strip()[:200] or None
            row.type_key = ((item.get('activityType') or {}).get('typeKey') or '')[:64] or None
            row.start_time = _parse_start(item)
            row.list_json = json.dumps(item)[:20000]
            if limit and seen >= limit:
                break
        db.session.commit()
        progress.update(0, f'listing activities... {seen} seen')
        if (limit and seen >= limit) or len(batch) < PAGE_SIZE:
            break
        if not full and streak >= KNOWN_STREAK_STOP:
            break
        start += PAGE_SIZE
    return seen


def _apply_name(row: GarminActivity) -> None:
    if row.file_id and row.name:
        sessions = Session.query.filter_by(file_id=row.file_id).all()
        if len(sessions) == 1 and not sessions[0].name:
            sessions[0].name = row.name


def sync_user(home: Path, user_id: int, progress, full: bool = False, limit: int | None = None,
              client=None, delay: float | None = None) -> dict:
    """Returns counts plus 'stopped': None | rate_limited | login_required | budget."""
    acct = account(user_id)
    result = {'listed': 0, 'imported': 0, 'duplicate': 0, 'replaced': 0, 'no_original': 0,
              'failed': 0, 'stopped': None}
    event_logger.info('sync.started', 'garmin sync started', user_id=user_id, full=full)
    delay = get_global_settings(home)['garmin_delay_seconds'] if delay is None else delay

    try:
        client = client or gc.connect(home, user_id)
        preseed_from_archive(user_id)
        # The first ever run pages the whole account; afterwards stop at the known streak.
        result['listed'] = refresh_listing(home, user_id, client, full or not acct.backfill_done,
                                           limit, progress)
        todo = (GarminActivity.query
                .filter(GarminActivity.user_id == user_id,
                        db.or_(GarminActivity.status == 'new',
                               db.and_(GarminActivity.status == 'failed',
                                       GarminActivity.attempts < MAX_ATTEMPTS)))
                .order_by(GarminActivity.start_time.desc()).all())   # newest first: useful soonest
        progress.update(0, f'{len(todo)} to download', total=len(todo), force=True)

        for i, row in enumerate(todo):
            spend_request(home)
            row.attempts = (row.attempts or 0) + 1
            row.fetched_at = utcnow()
            try:
                raw = client.download_original(row.activity_id)
                fit = extract_fit(raw)
                if fit is None:
                    row.status, row.error = 'no_original', None   # never retried automatically
                else:
                    res = importer.import_bytes(home, user_id, fit, f'{row.activity_id}_ACTIVITY.fit',
                                                source='garmin', garmin_activity_id=row.activity_id)
                    if res['status'] in ('imported', 'partial', 'replaced', 'duplicate'):
                        row.status = 'duplicate' if res['status'] == 'duplicate' else 'imported'
                        row.file_id, row.error = res.get('file_id'), res.get('reason')
                        _apply_name(row)
                    else:
                        row.status, row.error = 'failed', (res.get('reason') or res['status'])[:500]
                key = row.status if row.status in result else 'imported'
                if fit is not None and res['status'] == 'replaced':
                    key = 'replaced'
                result[key] += 1
            except (gc.SyncRateLimited, gc.SyncAuthError):
                row.attempts -= 1      # not this activity's fault
                db.session.commit()
                raise
            except Exception as exc:
                row.status, row.error = 'failed', f'{type(exc).__name__}: {exc}'[:500]
                result['failed'] += 1
            db.session.commit()
            progress.update(i + 1, f'{row.activity_id} {row.name or ""}: {row.status}')
            time.sleep(delay * random.uniform(0.8, 1.4))

        remaining = GarminActivity.query.filter_by(user_id=user_id, status='new').count()
        if not limit and not remaining:
            acct.backfill_done = True
        acct.status, acct.last_error = 'ok', None
        client.persist()
    except gc.SyncAuthError as exc:
        acct.status, acct.last_error = 'login_required', str(exc)[:500]
        result['stopped'] = 'login_required'
        event_logger.warning('sync.auth_required', 'garmin login required', user_id=user_id)
    except gc.SyncRateLimited as exc:
        acct.last_error = str(exc)[:500]
        result['stopped'] = 'rate_limited'
        event_logger.warning('sync.rate_limited', 'garmin rate limit hit - stopping', user_id=user_id)
    except BudgetExhausted:
        result['stopped'] = 'budget'
        event_logger.warning('sync.budget_exhausted', 'daily garmin request budget used up', user_id=user_id)

    acct.last_sync_at = utcnow()
    db.session.commit()
    event_logger.info('sync.finished', 'garmin sync finished', user_id=user_id, **result)
    return result
