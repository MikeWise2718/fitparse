"""Cost: what each ride or run cost the heart against what its power says it should have cost.

Both are loads in the usual points (100 = an hour at threshold), scored against the person's
CURRENT thresholds, so sessions from different months are on one scale:

- expected: from power - hours x (NP / FTP)^2 x 100, the TSS the session would have today;
- measured: from heart rate - TRIMP / (TRIMP per hour at threshold HR) x 100.

Their ratio, in %, is the session's cost. 100 % means heart rate matched what the power
predicts at today's thresholds; above it the same watts cost more heart than they would now,
below it less. Falling over months = fitter. Heat, fatigue, sleep and caffeine move it too, so
the line is a 28-day rolling median, and only steady sessions carry it: intervals make heart
rate lag the power, and patchy heart rate under-counts the measured side.

Each sport is scored against its own FTP. The watch writes the FTP of the file's sport, so the
cycling one comes from ride files (or Settings) and the running-power one from run files. For
runs, the watch estimates power from pace, slope and the body weight in the watch (rescaled to
the person's own weight on import), so it is an output measure, if a softer one than a power
meter. Treadmill runs take their pace from the wrist and are kept out of the line, and so are
run legs of a multisport recording, which show how drained the person was after a swim and a
ride, not their running fitness.

Unlike `fitness`, this reads `records`: TRIMP has to be recomputed per sample with today's
heart-rate references, not the ones in force on the day.
"""
from datetime import timedelta
from statistics import median

from ..models import ProfileSnapshot, Record, Session, db
from ..settings import get_user_settings
from . import metrics
from .fitness import _sessions
from .importer import resolve_zones

SPORTS = ('cycling', 'running')
MIN_S = 30 * 60          # shorter sessions are mostly warm-up
MAX_VI = 1.25            # NP / average power: above this it was intervals or stop-and-go
MIN_IF = 0.5             # below this the expected load is so small that the ratio is noise
MIN_HR_SHARE = 0.8       # share of records with heart rate
ROLLING_DAYS = 28


def _other_sport_files(sport: str):
    """Files holding a session of another sport (multisport recordings), as a subquery."""
    return db.session.query(Session.file_id).filter(Session.sport.notin_((sport, 'transition')))


def _ftp(home, user_id: int, sport: str):
    """Today's FTP for `sport`: Settings (a cycling number) for rides, else the latest one the
    watch wrote into a file of only that sport."""
    if sport == 'cycling' and get_user_settings(home, user_id)['zones'].get('ftp'):
        return get_user_settings(home, user_id)['zones']['ftp']
    snap = (ProfileSnapshot.query
            .filter(ProfileSnapshot.user_id == user_id, ProfileSnapshot.ftp.isnot(None),
                    ProfileSnapshot.file_id.in_(db.session.query(Session.file_id).filter(Session.sport == sport)),
                    ~ProfileSnapshot.file_id.in_(_other_sport_files(sport)))
            .order_by(ProfileSnapshot.at.desc()).first())
    return snap.ftp if snap else None


def reference(home, user_id: int, sport: str) -> dict:
    """Today's thresholds for scoring `sport`."""
    z = resolve_zones(home, user_id, {}, ride_file=False)
    z['ftp'] = _ftp(home, user_id, sport)
    snap = (ProfileSnapshot.query.filter(ProfileSnapshot.user_id == user_id, ProfileSnapshot.gender.isnot(None))
            .order_by(ProfileSnapshot.at.desc()).first())
    z['female'] = bool(snap and snap.gender == 'female')
    z['resting_hr'] = z.get('resting_hr') or metrics.DEFAULT_REST_HR
    if z.get('max_hr'):
        z['threshold_hr'] = z.get('threshold_hr') or round(0.9 * z['max_hr'])
    return z


def _why_not_steady(s: Session, hr_share: float, intensity: float, multisport: bool) -> str | None:
    if s.sub_sport == 'treadmill':
        return 'treadmill'
    if multisport:
        return 'leg of a multisport recording'
    if (s.timer_s or 0) < MIN_S:
        return 'under 30 min'
    if s.avg_power and s.norm_power / s.avg_power > MAX_VI:
        return 'intervals or stop-and-go'
    if hr_share < MIN_HR_SHARE:
        return 'patchy heart rate'
    if intensity < MIN_IF:
        return 'very easy'
    return None


def cost_series(home, user_id: int, sport: str, start, end) -> dict:
    ref = reference(home, user_id, sport)
    out = {'sport': sport, 'reference': {k: ref.get(k) for k in ('ftp', 'max_hr', 'threshold_hr', 'resting_hr')},
           'rolling_days': ROLLING_DAYS, 'points': []}
    if not ref.get('ftp') or not ref.get('max_hr'):
        return out
    ftp, max_hr, rest, female = ref['ftp'], ref['max_hr'], ref['resting_hr'], ref['female']
    per_hour = metrics.trimp_per_hour_at(ref['threshold_hr'], max_hr, rest, female)

    q = _sessions(user_id, [sport]).filter(Session.has_power.is_(True), Session.norm_power > 0,
                                           Session.avg_hr.isnot(None), Session.timer_s > 0)
    if start:   # the rolling median at the left edge needs the sessions before it
        q = q.filter(Session.start_time >= start - timedelta(days=ROLLING_DAYS))
    if end:
        q = q.filter(Session.start_time < end + timedelta(days=1))
    multisport = {f for (f,) in _other_sport_files(sport).filter(Session.user_id == user_id).distinct()}

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
        why = _why_not_steady(s, hr_share, intensity, s.file_id in multisport)
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
