"""Cross-activity series. Reads the `sessions` summary table only - never `records`."""
import math
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import func

from ..models import BestEffort, DailyHealth, Device, ProfileSnapshot, Session, db

CTL_DAYS, ATL_DAYS = 42, 7
VO2_SPORTS = {'running': 'run', 'cycling': 'bike'}


def _sessions(user_id: int, sports: list | None = None):
    q = Session.query.filter(Session.user_id == user_id)
    if sports:
        q = q.filter(Session.sport.in_(sports))
    return q


def fitness_series(user_id: int, start: date | None, end: date | None, load_sports: list | None = None) -> dict:
    """CTL (42 d) / ATL (7 d) exponentially weighted load, TSB = yesterday's CTL - ATL.
    Always integrated from the first activity so the window's left edge is not artificially zero."""
    rows = (_sessions(user_id, load_sports).filter(Session.load.isnot(None))
            .with_entities(func.date(Session.start_time), func.sum(Session.load))
            .group_by(func.date(Session.start_time)).all())
    if not rows:
        return {'days': [], 'load': [], 'ctl': [], 'atl': [], 'tsb': []}
    daily = {date.fromisoformat(d): float(v) for d, v in rows}
    first, last = min(daily), max(max(daily), date.today())
    end = min(end or last, last)
    k_ctl, k_atl = 1 - math.exp(-1 / CTL_DAYS), 1 - math.exp(-1 / ATL_DAYS)
    ctl = atl = 0.0
    out = {'days': [], 'load': [], 'ctl': [], 'atl': [], 'tsb': []}
    day = first
    while day <= end:
        tsb = ctl - atl
        load = daily.get(day, 0.0)
        ctl += (load - ctl) * k_ctl
        atl += (load - atl) * k_atl
        if start is None or day >= start:
            out['days'].append(day.isoformat())
            out['load'].append(round(load, 1))
            out['ctl'].append(round(ctl, 1))
            out['atl'].append(round(atl, 1))
            out['tsb'].append(round(tsb, 1))
        day += timedelta(days=1)
    return out


def _bucket_key(when: datetime, bucket: str, week_start: str) -> str:
    d = when.date()
    if bucket == 'month':
        return d.strftime('%Y-%m')
    if bucket == 'year':
        return d.strftime('%Y')
    offset = d.weekday() if week_start == 'monday' else (d.weekday() + 1) % 7
    return (d - timedelta(days=offset)).isoformat()


def volume(user_id: int, bucket: str, start, end, week_start: str = 'monday') -> dict:
    q = _sessions(user_id).filter(Session.sport != 'transition')
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    data: dict = defaultdict(lambda: defaultdict(lambda: {'hours': 0.0, 'km': 0.0, 'load': 0.0,
                                                         'calories': 0, 'count': 0}))
    for s in q.with_entities(Session.start_time, Session.sport, Session.timer_s, Session.distance_m,
                             Session.load, Session.calories).all():
        cell = data[_bucket_key(s[0], bucket, week_start)][s[1]]
        cell['hours'] += (s[2] or 0) / 3600
        cell['km'] += (s[3] or 0) / 1000
        cell['load'] += s[4] or 0
        cell['calories'] += s[5] or 0
        cell['count'] += 1
    buckets = sorted(data)
    sports = sorted({sp for b in data.values() for sp in b})
    return {'buckets': buckets, 'sports': sports,
            'data': {sp: [{k: round(v, 2) for k, v in data[b][sp].items()} if sp in data[b] else None
                          for b in buckets] for sp in sports}}


# Garmin only estimates VO2 max for running and (with power) cycling. It nevertheless stamps
# its current stored value into other activity files, so walks, swims and transitions carry
# carried-forward numbers that were never measured.
VO2_REAL_SPORTS = ('running', 'cycling')


def vo2max_series(user_id: int, sport: str | None, sub_sport: str | None, start, end,
                  changes_only: bool = True) -> list:
    q = _sessions(user_id).filter(Session.vo2max.isnot(None))
    q = q.filter(Session.sport == sport) if sport else q.filter(Session.sport.in_(VO2_REAL_SPORTS))
    if sub_sport:
        q = q.filter(Session.sub_sport == sub_sport)
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    rows = q.order_by(Session.start_time).all()
    out, last = [], {}
    for s in rows:
        # An unchanged value means the watch did not recalculate (cycling needs a power meter,
        # so a flat run of identical readings is one estimate, not many). Plot the changes.
        if changes_only and last.get(s.sport) == s.vo2max:
            continue
        last[s.sport] = s.vo2max
        out.append({'id': s.id, 'at': s.start_time, 'sport': s.sport, 'sub_sport': s.sub_sport,
                    'vo2max': s.vo2max})
    return out


def garmin_vo2max_series(user_id: int, start=None, end=None) -> list:
    """Garmin Connect's own VO2 max, from the health sync - a smoothed figure, distinct from the
    per-activity value the watch writes into each FIT file."""
    q = DailyHealth.query.filter(DailyHealth.user_id == user_id,
                                 db.or_(DailyHealth.vo2max_run.isnot(None),
                                        DailyHealth.vo2max_bike.isnot(None)))
    if start:
        q = q.filter(DailyHealth.day >= start)
    if end:
        q = q.filter(DailyHealth.day <= end)
    out, last = [], None
    for h in q.order_by(DailyHealth.day).all():
        if (h.vo2max_run, h.vo2max_bike) == last:
            continue
        last = (h.vo2max_run, h.vo2max_bike)
        out.append({'day': h.day.isoformat(), 'run': h.vo2max_run, 'bike': h.vo2max_bike})
    return out


def best_curve(user_id: int, kind: str, sport: str | None, start=None, end=None) -> list:
    """Best value per window: highest power, or lowest time for a distance."""
    q = BestEffort.query.filter(BestEffort.user_id == user_id, BestEffort.kind == kind)
    if sport:
        q = q.filter(BestEffort.sport == sport)
    if start:
        q = q.filter(BestEffort.at >= start)
    if end:
        q = q.filter(BestEffort.at < end + timedelta(days=1))
    best: dict = {}
    for b in q.all():
        cur = best.get(b.window)
        better = cur is None or (b.value > cur.value if kind == 'power' else b.value < cur.value)
        if better:
            best[b.window] = b
    return [{'window': w, 'value': b.value, 'session_id': b.session_id, 'at': b.at}
            for w, b in sorted(best.items())]


def record_progression(user_id: int, kind: str, window: int, sport: str | None) -> list:
    """Every time the all-time best for one window was beaten - the PR log."""
    q = BestEffort.query.filter_by(user_id=user_id, kind=kind, window=window)
    if sport:
        q = q.filter(BestEffort.sport == sport)
    out, best = [], None
    for b in q.order_by(BestEffort.at).all():
        if best is None or (b.value > best if kind == 'power' else b.value < best):
            best = b.value
            out.append({'at': b.at, 'value': b.value, 'session_id': b.session_id})
    return out


def efficiency_series(user_id: int, sport: str, start, end) -> list:
    q = _sessions(user_id, [sport]).filter(Session.efficiency.isnot(None))
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    return [{'id': s.id, 'at': s.start_time, 'efficiency': s.efficiency, 'decoupling': s.decoupling,
             'avg_hr': s.avg_hr, 'has_power': s.has_power}
            for s in q.order_by(Session.start_time).all()]


def swim_series(user_id: int, start, end) -> list:
    q = _sessions(user_id, ['swimming']).filter(Session.distance_m > 0, Session.timer_s > 0)
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    return [{'id': s.id, 'at': s.start_time, 'sub_sport': s.sub_sport, 'distance_m': s.distance_m,
             'pace_100m_s': round(s.timer_s / s.distance_m * 100, 1), 'swolf': s.avg_swolf}
            for s in q.order_by(Session.start_time).all()]


def strength_series(user_id: int, start, end) -> dict:
    from ..models import StrengthSet
    q = (db.session.query(Session.start_time, StrengthSet.category, StrengthSet.reps, StrengthSet.weight_kg)
         .join(StrengthSet, StrengthSet.session_id == Session.id)
         .filter(Session.user_id == user_id, StrengthSet.set_type == 'active'))
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    by_cat: dict = defaultdict(lambda: defaultdict(lambda: {'sets': 0, 'reps': 0, 'volume_kg': 0.0}))
    for at, category, reps, weight in q.all():
        cell = by_cat[category or 'unknown'][at.date().isoformat()]
        cell['sets'] += 1
        cell['reps'] += reps or 0
        cell['volume_kg'] += (reps or 0) * (weight or 0)
    return {cat: [{'day': d, **v} for d, v in sorted(days.items())] for cat, days in by_cat.items()}


def te_distribution(user_id: int, start, end) -> list:
    q = _sessions(user_id).filter(Session.te_aerobic.isnot(None))
    if start:
        q = q.filter(Session.start_time >= start)
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    return [{'id': s.id, 'at': s.start_time, 'sport': s.sport, 'aerobic': s.te_aerobic,
             'anaerobic': s.te_anaerobic} for s in q.order_by(Session.start_time).all()]


def body_series(user_id: int) -> dict:
    snaps = (ProfileSnapshot.query.filter_by(user_id=user_id)
             .order_by(ProfileSnapshot.at).all())
    keys = ['weight_kg', 'resting_hr', 'max_hr', 'threshold_hr', 'ftp']
    series = {k: [] for k in keys}
    for snap in snaps:
        for k in keys:
            value = getattr(snap, k)
            # keep only changes: the watch repeats the same profile in every file
            if value and (not series[k] or series[k][-1]['value'] != value):
                series[k].append({'at': snap.at, 'value': value})
    wkg = []
    weights = [(p['at'], p['value']) for p in series['weight_kg']]
    for point in series['ftp']:
        prior = [w for at, w in weights if at and point['at'] and at <= point['at']]
        if prior:
            wkg.append({'at': point['at'], 'value': round(point['value'] / prior[-1], 2)})
    series['w_per_kg'] = wkg
    health = (DailyHealth.query.filter_by(user_id=user_id).order_by(DailyHealth.day).all())
    series['daily'] = [{'day': h.day.isoformat(), 'resting_hr': h.resting_hr, 'weight_kg': h.weight_kg,
                        'hrv_avg': h.hrv_avg, 'sleep_h': round(h.sleep_s / 3600, 2) if h.sleep_s else None,
                        'sleep_score': h.sleep_score, 'bb_max': h.bb_max, 'bb_min': h.bb_min,
                        'stress_avg': h.stress_avg, 'readiness': h.readiness,
                        'vo2max_run': h.vo2max_run, 'vo2max_bike': h.vo2max_bike} for h in health]
    latest = {k: (series[k][-1]['value'] if series[k] else None) for k in keys}
    return {'series': series, 'latest': latest}


def gear(user_id: int) -> list:
    rows = Device.query.filter_by(user_id=user_id).order_by(Device.seen_at).all()
    items: dict = {}
    for d in rows:
        # Watches list their internal parts (GPS, barometer, ...) as anonymous device_info rows
        # with no serial and no maker. Those are not gear.
        if not d.serial and not (d.manufacturer and d.product):
            continue
        key = d.serial or f'{d.manufacturer}/{d.product}/{d.device_type}'
        item = items.setdefault(key, {
            'key': key, 'manufacturer': d.manufacturer, 'product': d.product, 'serial': d.serial,
            'device_type': d.device_type, 'is_watch': d.device_index == 'creator',
            'first_seen': d.seen_at, 'activities': 0, 'firmware': [], 'battery': []})
        item['activities'] += 1
        item['last_seen'] = d.seen_at
        item['operating_time_s'] = d.operating_time_s or item.get('operating_time_s')
        for field in ('manufacturer', 'product', 'device_type'):
            item[field] = item[field] or getattr(d, field)
        if d.sw_version and (not item['firmware'] or item['firmware'][-1]['version'] != d.sw_version):
            item['firmware'].append({'version': d.sw_version, 'at': d.seen_at})
        if d.battery_status or d.battery_voltage:
            item['battery'].append({'at': d.seen_at, 'status': d.battery_status, 'voltage': d.battery_voltage})
    out = list(items.values())
    for item in out:
        item['battery_now'] = item['battery'][-1] if item['battery'] else None
        item['battery'] = item['battery'][-60:]
    return sorted(out, key=lambda i: (not i['is_watch'], -(i['activities'])))


def dashboard(user_id: int, week_start: str, load_sports: list | None) -> dict:
    today = date.today()
    series = fitness_series(user_id, today - timedelta(days=1), today, load_sports)
    form = {k: (series[k][-1] if series[k] else None) for k in ('ctl', 'atl', 'tsb')}

    def latest_vo2(sport):
        now = (_sessions(user_id, [sport]).filter(Session.vo2max.isnot(None))
               .order_by(Session.start_time.desc()).first())
        if not now:
            return None
        before = (_sessions(user_id, [sport]).filter(Session.vo2max.isnot(None),
                  Session.start_time <= now.start_time - timedelta(days=90))
                  .order_by(Session.start_time.desc()).first())
        return {'value': now.vo2max, 'at': now.start_time,
                'delta_90d': round(now.vo2max - before.vo2max, 1) if before else None}

    offset = today.weekday() if week_start == 'monday' else (today.weekday() + 1) % 7
    week0 = datetime.combine(today - timedelta(days=offset), datetime.min.time())

    def totals(a, b):
        row = (_sessions(user_id).filter(Session.sport != 'transition', Session.start_time >= a,
                                         Session.start_time < b)
               .with_entities(func.coalesce(func.sum(Session.timer_s), 0),
                              func.coalesce(func.sum(Session.distance_m), 0),
                              func.coalesce(func.sum(Session.load), 0), func.count(Session.id)).one())
        return {'hours': round(row[0] / 3600, 2), 'km': round(row[1] / 1000, 1),
                'load': round(row[2], 0), 'count': row[3]}

    this_week = totals(week0, week0 + timedelta(days=7))
    prev4 = totals(week0 - timedelta(days=28), week0)
    last = _sessions(user_id).order_by(Session.start_time.desc()).first()
    health = DailyHealth.query.filter_by(user_id=user_id).order_by(DailyHealth.day.desc()).first()
    return {
        'form': form,
        'vo2max': {name: latest_vo2(sport) for sport, name in VO2_SPORTS.items()},
        'this_week': this_week,
        'avg_4_weeks': {k: round(v / 4, 2) for k, v in prev4.items()},
        'body': body_series(user_id)['latest'],
        'health': ({'day': health.day.isoformat(), 'hrv_avg': health.hrv_avg, 'sleep_score': health.sleep_score,
                    'readiness': health.readiness, 'resting_hr': health.resting_hr,
                    'bb_max': health.bb_max} if health else None),
        'last_activity_at': last.start_time if last else None,
        'total_sessions': _sessions(user_id).count(),
    }


def calendar(user_id: int, year: int) -> list:
    rows = (_sessions(user_id).filter(Session.sport != 'transition',
                                      func.strftime('%Y', Session.start_time) == str(year))
            .with_entities(func.date(Session.start_time), func.sum(Session.timer_s),
                           func.sum(Session.load), func.count(Session.id))
            .group_by(func.date(Session.start_time)).all())
    return [{'day': d, 'hours': round((t or 0) / 3600, 2), 'load': round(l or 0, 0), 'count': n}
            for d, t, l, n in rows]
