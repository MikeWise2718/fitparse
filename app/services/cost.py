"""Ride cost: what each ride cost the heart against what its power says it should have cost.

Both are loads in the usual points (100 = an hour at threshold), scored against the person's
CURRENT thresholds, so rides from different months are on one scale:

- expected: from power - hours x (NP / FTP)^2 x 100, the TSS the ride would have today;
- measured: from heart rate - TRIMP / (TRIMP per hour at threshold HR) x 100.

Their ratio, in %, is the ride's cost. 100 % means heart rate matched what the power predicts
at today's thresholds; above it the same watts cost more heart than they would now, below it
less. Falling over months = fitter. Heat, fatigue, sleep and caffeine move it too, so the line
is a 28-day rolling median, and only steady rides carry it: intervals make heart rate lag the
power, and a ride with patchy heart rate under-counts the measured side.

Unlike `fitness`, this reads `records`: TRIMP has to be recomputed per sample with today's
heart-rate references, not the ones in force on the day.
"""
from datetime import timedelta
from statistics import median

from ..models import ProfileSnapshot, Record, Session
from . import metrics
from .fitness import _sessions
from .importer import resolve_zones

MIN_S = 30 * 60          # shorter rides are mostly warm-up
MAX_VI = 1.25            # NP / average power: above this the ride was intervals or stop-and-go
MIN_IF = 0.5             # below this the expected load is so small that the ratio is noise
MIN_HR_SHARE = 0.8       # share of records with heart rate
ROLLING_DAYS = 28


def reference(home, user_id: int) -> dict:
    """Today's thresholds. FTP from ride files only: run files carry the running-power FTP."""
    z = resolve_zones(home, user_id, {}, ride_file=False)
    snap = (ProfileSnapshot.query.filter(ProfileSnapshot.user_id == user_id, ProfileSnapshot.gender.isnot(None))
            .order_by(ProfileSnapshot.at.desc()).first())
    z['female'] = bool(snap and snap.gender == 'female')
    z['resting_hr'] = z.get('resting_hr') or metrics.DEFAULT_REST_HR
    if z.get('max_hr'):
        z['threshold_hr'] = z.get('threshold_hr') or round(0.9 * z['max_hr'])
    return z


def _why_not_steady(s: Session, hr_share: float, intensity: float) -> str | None:
    if (s.timer_s or 0) < MIN_S:
        return 'under 30 min'
    if s.avg_power and s.norm_power / s.avg_power > MAX_VI:
        return 'intervals or stop-and-go'
    if hr_share < MIN_HR_SHARE:
        return 'patchy heart rate'
    if intensity < MIN_IF:
        return 'very easy'
    return None


def ride_cost_series(home, user_id: int, start, end) -> dict:
    ref = reference(home, user_id)
    out = {'reference': {k: ref.get(k) for k in ('ftp', 'max_hr', 'threshold_hr', 'resting_hr')},
           'rolling_days': ROLLING_DAYS, 'points': []}
    if not ref.get('ftp') or not ref.get('max_hr'):
        return out
    ftp, max_hr, rest, female = ref['ftp'], ref['max_hr'], ref['resting_hr'], ref['female']
    per_hour = metrics.trimp_per_hour_at(ref['threshold_hr'], max_hr, rest, female)

    q = _sessions(user_id, ['cycling']).filter(Session.has_power.is_(True), Session.norm_power > 0,
                                               Session.avg_hr.isnot(None), Session.timer_s > 0)
    if start:   # the rolling median at the left edge needs the rides before it
        q = q.filter(Session.start_time >= start - timedelta(days=ROLLING_DAYS))
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))

    points = []
    for s in q.order_by(Session.start_time).all():
        recs = [{'t': t, 'hr': hr} for t, hr in (Record.query.with_entities(Record.t, Record.hr)
                                                 .filter(Record.session_id == s.id).order_by(Record.idx))]
        if not recs:
            continue
        hr_share = sum(1 for r in recs if r['hr']) / len(recs)
        intensity = s.norm_power / ftp
        expected = s.timer_s / 3600 * intensity ** 2 * 100
        measured = metrics.trimp(recs, metrics.sample_durations(recs), max_hr, rest, female) / per_hour * 100
        why = _why_not_steady(s, hr_share, intensity)
        points.append({'id': s.id, 'at': s.start_time, 'name': s.name, 'minutes': round(s.timer_s / 60),
                       'np': round(s.norm_power), 'avg_hr': s.avg_hr, 'expected': round(expected, 1),
                       'measured': round(measured, 1), 'ratio': round(measured / expected * 100, 1),
                       'steady': why is None, 'why': why})
    steady = [p for p in points if p['steady']]
    for p in steady:
        window = [o['ratio'] for o in steady if p['at'] - timedelta(days=ROLLING_DAYS) < o['at'] <= p['at']]
        p['rolling'] = round(median(window), 1)
    out['points'] = [p for p in points if not start or p['at'] >= start]
    return out
