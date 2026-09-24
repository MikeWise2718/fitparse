"""Daily health metrics from Garmin Connect (JSON, not FIT): resting HR, HRV, sleep, body
battery, stress, readiness, Garmin's own VO2 max.

Key names below follow garminconnect 0.3.11's typed response models (typed.py) where one exists;
the rest are read defensively. The raw JSON of every day is kept on disk under
~/fitmon/users/<id>/health/YYYY/ so columns can be added later without re-fetching.

Cost is ~5 requests per day, so a run is capped and works newest-first.
"""
import json
import random
import time
from datetime import date, timedelta
from pathlib import Path

from ..events import event_logger
from ..models import DailyHealth, db
from ..settings import get_global_settings, user_dir
from . import client as gc
from .activities import BudgetExhausted, account, spend_request

MAX_DAYS_PER_RUN = 45
BACKFILL_DAYS = 730
ENDPOINTS = ['get_stats', 'get_sleep_data', 'get_hrv_data', 'get_training_readiness',
             'get_max_metrics', 'get_training_status']
GRAMS_PER_KG = 1000.0


def dig(obj, *path):
    for key in path:
        if isinstance(obj, list):
            obj = obj[key] if isinstance(key, int) and len(obj) > key else None
        elif isinstance(obj, dict):
            obj = obj.get(key)
        else:
            return None
        if obj is None:
            return None
    return obj


def first_device(container, *path):
    """Garmin nests these under a map keyed by device id ({'3459362545': {...}}), and a watch
    replacement changes that key. Take the primary device, else any."""
    values = list((dig(container, *path) or {}).values())
    if not values:
        return {}
    return next((v for v in values if isinstance(v, dict) and v.get('primaryTrainingDevice')),
                values[0] if isinstance(values[0], dict) else {})


def extract(raw: dict) -> dict:
    stats, sleep, hrv = raw.get('get_stats') or {}, raw.get('get_sleep_data') or {}, raw.get('get_hrv_data') or {}
    readiness = raw.get('get_training_readiness') or []
    if isinstance(readiness, list) and readiness:
        readiness = max(readiness, key=lambda r: (r or {}).get('timestamp') or '')
    status = raw.get('get_training_status') or {}
    # get_max_metrics is empty on most days (Garmin only writes one when it recalculates);
    # get_training_status always carries the most recent value, so fall back to it.
    metrics = raw.get('get_max_metrics') or []
    metrics = metrics[0] if isinstance(metrics, list) and metrics else (metrics or {})
    if not metrics:
        metrics = status.get('mostRecentVO2Max') or {}
    latest = first_device(status, 'mostRecentTrainingStatus', 'latestTrainingStatusData')
    balance = first_device(status, 'mostRecentTrainingLoadBalance', 'metricsTrainingLoadBalanceDTOMap')
    return {
        # The phrase ('PRODUCTIVE_1'), not the numeric trainingStatus code, which is undocumented.
        'training_status': (latest.get('trainingStatusFeedbackPhrase') or '').split('_')[0].title() or None,
        'load_aerobic_low': balance.get('monthlyLoadAerobicLow'),
        'load_aerobic_high': balance.get('monthlyLoadAerobicHigh'),
        'load_anaerobic': balance.get('monthlyLoadAnaerobic'),
        'resting_hr': stats.get('restingHeartRate'),
        'stress_avg': stats.get('averageStressLevel') if (stats.get('averageStressLevel') or -1) >= 0 else None,
        'bb_max': stats.get('bodyBatteryHighestValue'),
        'bb_min': stats.get('bodyBatteryLowestValue'),
        'sleep_s': dig(sleep, 'dailySleepDTO', 'sleepTimeSeconds'),
        'sleep_score': dig(sleep, 'dailySleepDTO', 'sleepScores', 'overall', 'value'),
        'hrv_avg': dig(hrv, 'hrvSummary', 'lastNightAvg'),
        'hrv_status': dig(hrv, 'hrvSummary', 'status'),
        'readiness': dig(readiness, 'score') if isinstance(readiness, dict) else None,
        'vo2max_run': dig(metrics, 'generic', 'vo2MaxPreciseValue') or dig(metrics, 'generic', 'vo2MaxValue'),
        'vo2max_bike': dig(metrics, 'cycling', 'vo2MaxPreciseValue') or dig(metrics, 'cycling', 'vo2MaxValue'),
    }


def _raw_path(home: Path, user_id: int, day: date) -> Path:
    folder = user_dir(home, user_id) / 'health' / str(day.year)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f'{day.isoformat()}.json'


def reindex_from_disk(home: Path, user_id: int) -> int:
    """Re-run extract() over the raw JSON already saved, without spending a request. This is
    why every response is kept: a mapping fix costs nothing to apply to old days."""
    n = 0
    for path in sorted((user_dir(home, user_id) / 'health').rglob('*.json')):
        try:
            day = date.fromisoformat(path.stem)
            raw = json.loads(path.read_text(encoding='utf-8'))
        except ValueError:
            continue
        row = db.session.get(DailyHealth, (user_id, day)) or DailyHealth(user_id=user_id, day=day)
        for key, value in extract(raw).items():
            setattr(row, key, value)
        db.session.add(row)
        n += 1
    db.session.commit()
    return n


def days_to_fetch(user_id: int, today: date | None = None, recent_only: bool = False) -> list:
    """Newest first. Yesterday and today are always refreshed (the numbers settle overnight).
    `recent_only` skips the back-fill: an on-demand sync wants today's numbers in seconds, and
    the nightly run carries on filling the history."""
    today = today or date.today()
    if recent_only:
        return [today, today - timedelta(days=1)]
    have = {d for (d,) in db.session.query(DailyHealth.day).filter(DailyHealth.user_id == user_id).all()}
    wanted = [today - timedelta(days=i) for i in range(BACKFILL_DAYS)]
    return [d for d in wanted if d not in have or (today - d).days <= 1][:MAX_DAYS_PER_RUN]


def weigh_ins(client, home: Path, days: list) -> dict:
    """Weight is a range endpoint: one request for the whole window, not one per day.
    Garmin reports grams. A day with no weigh-in simply has none - it is not carried forward."""
    if not days:
        return {}
    spend_request(home)
    raw = client.health('get_weigh_ins', min(days).isoformat(), max(days).isoformat()) or {}
    out = {}
    for summary in (raw.get('dailyWeightSummaries') or []):
        grams = dig(summary, 'latestWeight', 'weight')
        if summary.get('summaryDate') and grams:
            try:
                out[date.fromisoformat(summary['summaryDate'])] = round(grams / GRAMS_PER_KG, 2)
            except ValueError:
                continue
    return out


def sync_user(home: Path, user_id: int, progress, client=None, delay: float | None = None,
              recent_only: bool = False) -> dict:
    result = {'days': 0, 'stopped': None}
    delay = get_global_settings(home)['garmin_delay_seconds'] if delay is None else delay
    try:
        client = client or gc.connect(home, user_id)
        days = days_to_fetch(user_id, recent_only=recent_only)
        progress.update(0, f'health: {len(days)} day(s)', total=len(days), force=True)
        try:
            weights = weigh_ins(client, home, days)
        except (gc.SyncRateLimited, gc.SyncAuthError, BudgetExhausted):
            raise
        except Exception:
            weights = {}        # weight is a nicety; never lose a whole run over it
        for i, day in enumerate(days):
            raw = {}
            for method in ENDPOINTS:
                spend_request(home)
                try:
                    raw[method] = client.health(method, day.isoformat())
                except (gc.SyncRateLimited, gc.SyncAuthError):
                    raise
                except Exception as exc:     # one missing metric must not lose the day
                    raw[method] = None
                    raw[f'{method}_error'] = f'{type(exc).__name__}: {exc}'[:200]
                time.sleep(delay * random.uniform(0.5, 1.0))
            _raw_path(home, user_id, day).write_text(json.dumps(raw, default=str), encoding='utf-8')
            row = db.session.get(DailyHealth, (user_id, day)) or DailyHealth(user_id=user_id, day=day)
            for key, value in extract(raw).items():
                setattr(row, key, value)
            row.weight_kg = weights.get(day)
            db.session.add(row)
            db.session.commit()
            result['days'] += 1
            progress.update(i + 1, f'health {day.isoformat()}')
        acct = account(user_id)
        acct.health_synced_to = date.today()
        db.session.commit()
        client.persist()
    except gc.SyncAuthError:
        result['stopped'] = 'login_required'
    except gc.SyncRateLimited:
        result['stopped'] = 'rate_limited'
        event_logger.warning('sync.rate_limited', 'rate limit during health sync', user_id=user_id)
    except BudgetExhausted:
        result['stopped'] = 'budget'
    event_logger.info('sync.health_finished', 'health sync finished', user_id=user_id, **result)
    return result
