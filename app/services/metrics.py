"""Per-session derived numbers, computed once at import so dashboards never scan `records`.

Pure functions over the dicts produced by fit_parser: no DB, no Flask.
"""
import math

HR_ZONE_BOUNDS = [0.50, 0.60, 0.70, 0.80, 0.90]            # share of max HR -> zones 1..5
POWER_ZONE_BOUNDS = [0.0, 0.55, 0.75, 0.90, 1.05, 1.20, 1.50]  # share of FTP -> zones 1..7 (Coggan)
POWER_WINDOWS = [5, 15, 30, 60, 300, 600, 1200, 3600]      # seconds
PACE_DISTANCES = [400, 1000, 1609, 5000, 10000, 21097, 42195]  # metres
MAX_GAP_S = 10          # smart recording: a sample stands for at most this long
MAX_PLAUSIBLE_RUN_SPEED = 12.0   # m/s between two samples; sprint world-record pace is ~10.4

# Trim suggestion: how a watch left recording after the finish shows up in the data.
TRIM_WINDOW_S = 300              # progress is judged over 5-minute windows
TRIM_STALLED_FRACTION = 0.25     # under a quarter of the activity's own rate = not moving on
TRIM_MIN_TAIL_S = 300            # never suggest trimming less than 5 minutes
TRIM_MIN_ACTIVITY_S = 900        # too short to have a meaningful "normal" rate
TRIM_MIN_MOVING_RATE = 0.5       # m/s; below this there is no progress signal (indoor, pool)
DEFAULT_MAX_HR = 190
DEFAULT_REST_HR = 60


def zone_of(value: float, reference: float, bounds: list) -> int:
    share = value / reference
    zone = 0
    for i, bound in enumerate(bounds, start=1):
        if share >= bound:
            zone = i
    return zone


def sample_durations(records: list) -> list:
    out = []
    for i, rec in enumerate(records):
        nxt = records[i + 1]['t'] if i + 1 < len(records) else rec['t'] + 1
        out.append(max(0, min(MAX_GAP_S, nxt - rec['t'])))
    return out


def per_second(records: list, key: str) -> list:
    """Forward-fill a channel onto a 1 Hz grid; gaps longer than MAX_GAP_S count as zero
    (a paused timer is not an effort)."""
    if not records:
        return []
    out = []
    for i, rec in enumerate(records):
        value = rec.get(key) or 0
        nxt = records[i + 1]['t'] if i + 1 < len(records) else rec['t'] + 1
        gap = max(1, nxt - rec['t'])
        if gap <= MAX_GAP_S:
            out.extend([value] * gap)
        else:
            out.append(value)
            out.extend([0] * (gap - 1))
        if len(out) > 200_000:   # > 55 h: not a real activity, stop growing
            break
    return out


def normalized_power(power_1hz: list) -> float | None:
    if len(power_1hz) < 30:
        return None
    window = sum(power_1hz[:30])
    total = (window / 30) ** 4
    count = 1
    for i in range(30, len(power_1hz)):
        window += power_1hz[i] - power_1hz[i - 30]
        total += (window / 30) ** 4
        count += 1
    return round((total / count) ** 0.25, 1)


def best_power(power_1hz: list) -> list:
    out = []
    n = len(power_1hz)
    if not n:
        return out
    prefix = [0]
    for p in power_1hz:
        prefix.append(prefix[-1] + p)
    for w in POWER_WINDOWS:
        if n < w:
            break
        best, at = -1, 0
        for i in range(0, n - w + 1):
            s = prefix[i + w] - prefix[i]
            if s > best:
                best, at = s, i
        if best > 0:
            out.append({'kind': 'power', 'window': w, 'value': round(best / w, 1), 'offset_s': at})
    return out


def best_pace(records: list) -> list:
    """Fastest time over each distance, two-pointer over cumulative distance."""
    pts = []
    removed = 0.0          # metres discarded as implausible jumps so far
    last_t = last_raw = None
    for r in records:
        raw = r.get('dist')
        if raw is None:
            continue
        if last_t is not None:
            dt, dd = r['t'] - last_t, raw - last_raw
            if dt <= 0 or dd < 0:
                continue
            if dd / dt > MAX_PLAUSIBLE_RUN_SPEED:
                # A distance jump (corrupt or "repaired" file, GPS glitch) is not a personal
                # best: take the jump out of the distance axis so no window can contain it.
                removed += dd
        last_t, last_raw = r['t'], raw
        pts.append((r['t'], raw - removed))
    out = []
    if len(pts) < 2:
        return out
    total = pts[-1][1] - pts[0][1]
    for d in PACE_DISTANCES:
        if total < d:
            break
        best, at, j = None, 0, 0
        for i in range(len(pts)):
            while j < len(pts) and pts[j][1] - pts[i][1] < d:
                j += 1
            if j >= len(pts):
                break
            # interpolate the moment the distance was completed
            t1, d1 = pts[j - 1]
            t2, d2 = pts[j]
            need = pts[i][1] + d
            frac = (need - d1) / (d2 - d1) if d2 > d1 else 1.0
            elapsed = t1 + frac * (t2 - t1) - pts[i][0]
            if elapsed > 0 and (best is None or elapsed < best):
                best, at = elapsed, pts[i][0]
        if best is not None:
            out.append({'kind': 'pace', 'window': d, 'value': round(best, 1), 'offset_s': at})
    return out


def trimp(records: list, durations: list, max_hr: float, rest_hr: float, female: bool) -> float:
    a, b = (0.86, 1.67) if female else (0.64, 1.92)
    total = 0.0
    span = max(1.0, max_hr - rest_hr)
    for rec, dt in zip(records, durations):
        if rec.get('hr'):
            hrr = min(1.0, max(0.0, (rec['hr'] - rest_hr) / span))
            total += (dt / 60.0) * hrr * a * math.exp(b * hrr)
    return total


def trimp_per_hour_at(hr: float, max_hr: float, rest_hr: float, female: bool) -> float:
    a, b = (0.86, 1.67) if female else (0.64, 1.92)
    hrr = min(1.0, max(0.0, (hr - rest_hr) / max(1.0, max_hr - rest_hr)))
    return 60.0 * hrr * a * math.exp(b * hrr)


def _mean(values: list):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _decoupling(records: list, channel: str) -> tuple:
    """Efficiency (output per heart beat) and its drift between the two halves, in %."""
    usable = [r for r in records if r.get('hr') and r.get(channel)]
    if len(usable) < 600 or usable[-1]['t'] - usable[0]['t'] < 1200:
        return None, None
    half = len(usable) // 2
    ratios = []
    for part in (usable[:half], usable[half:]):
        ratios.append(_mean([r[channel] for r in part]) / _mean([r['hr'] for r in part]))
    overall = _mean([r[channel] for r in usable]) / _mean([r['hr'] for r in usable])
    drift = (ratios[0] - ratios[1]) / ratios[0] * 100 if ratios[0] else None
    return round(overall, 4), round(drift, 2) if drift is not None else None


def suggest_trim(records: list, sport: str | None = None) -> dict | None:
    """Where the activity probably ended, if the watch was left recording afterwards.

    Not stillness: after a race finish you walk about, collect kit, stand talking - speed and
    cadence stay non-zero. What collapses is FORWARD PROGRESS. In the reference case (a half
    triathlon) the athlete covered ~500 m per 5 min while racing and under 100 m afterwards.

    Returns {'end_t', 'dropped_s', 'dropped_m', 'reason'} or None. A suggestion only: the caller
    shows it, the person decides.
    """
    usable = [r for r in records if r.get('dist') is not None]
    if len(usable) < 60:
        return None
    total_s = usable[-1]['t'] - usable[0]['t']
    if total_s < TRIM_MIN_ACTIVITY_S:
        return None

    def gain(from_t, to_t):
        window = [r for r in usable if from_t <= r['t'] <= to_t]
        return (window[-1]['dist'] - window[0]['dist']) if len(window) > 1 else 0.0

    moving_rate = gain(usable[0]['t'], usable[0]['t'] + total_s * 0.8) / max(1, total_s * 0.8)
    if moving_rate < TRIM_MIN_MOVING_RATE:
        return None            # never really moving (indoor, swim): no progress signal to use

    # Find the EARLIEST point after which progress never recovers. Walking back from the end and
    # stopping at the first busy window is wrong: after a finish there are bursts of movement
    # (collecting kit, walking to the car) that look active in isolation.
    cutoff = moving_rate * TRIM_STALLED_FRACTION
    end_t, start_t = usable[-1]['t'], usable[0]['t']
    # Judge a candidate by the WHOLE remaining tail, not window by window: after a finish there
    # are bursts (collecting kit, walking to the car) that look active in isolation but leave
    # the overall rate far below racing. Earliest such point wins.
    candidate = None
    t = start_t + TRIM_WINDOW_S
    while t <= end_t - TRIM_MIN_TAIL_S:
        if gain(t, end_t) / max(1, end_t - t) <= cutoff:
            candidate = t
            break
        t += TRIM_WINDOW_S
    if candidate is None:
        return None

    # Refine backwards to the last sample that was genuinely still progressing.
    end = candidate
    for i in range(len(usable) - 1, 0, -1):
        r, prev = usable[i], usable[i - 1]
        if r['t'] > candidate:
            continue
        if (r['dist'] - prev['dist']) / max(1, r['t'] - prev['t']) > cutoff:
            end = r['t']
            break
    dropped_m = usable[-1]['dist'] - next((r['dist'] for r in usable if r['t'] >= end), usable[-1]['dist'])
    if end_t - end < TRIM_MIN_TAIL_S:
        return None
    return {'end_t': int(end), 'dropped_s': int(end_t - end), 'dropped_m': round(dropped_m),
            'reason': f'covered {dropped_m:.0f} m in the last {(end_t - end) / 60:.0f} min, '
                      f'against {moving_rate * 60:.0f} m/min while active'}


def apply_trim(sess: dict, end_t: int) -> dict:
    """Restrict a parsed session to samples up to end_t, and correct the leg's own totals.

    The FIT file is untouched and no row is deleted: this only changes what the derived figures
    are computed over. Laps are left alone - a lap that straddles the finish is still a lap that
    happened, and its own totals came from the watch.
    """
    kept = [r for r in sess['records'] if r['t'] <= end_t]
    if not kept or len(kept) == len(sess['records']):
        return sess
    first, last = kept[0], kept[-1]
    sess['records'] = kept
    sess['trimmed_s'] = int(sess['records'][-1]['t'])
    # Recompute the summary from the samples: the watch's totals include the dead tail.
    elapsed = last['t'] - first['t']
    if elapsed > 0:
        sess['timer_s'] = float(min(sess.get('timer_s') or elapsed, elapsed))
        sess['elapsed_s'] = float(elapsed)
    if first.get('dist') is not None and last.get('dist') is not None:
        sess['distance_m'] = round(last['dist'] - first['dist'], 2)
    if sess.get('distance_m') and sess.get('timer_s'):
        sess['avg_speed'] = round(sess['distance_m'] / sess['timer_s'], 4)
    hrs = [r['hr'] for r in kept if r.get('hr')]
    if hrs:
        sess['avg_hr'], sess['max_hr'] = int(round(sum(hrs) / len(hrs))), max(hrs)
    return sess


def compute_session(sess: dict, zones: dict, load_model: str = 'auto') -> dict:
    """Mutates and returns `sess`, adding derived columns plus '_best' and '_zones' lists.

    zones: {'max_hr', 'threshold_hr', 'resting_hr', 'ftp', 'female'} - any may be None.
    """
    records = sess.get('records') or []
    durations = sample_durations(records)
    sess['n_records'] = len(records)
    sess['has_gps'] = any(r.get('lat') is not None for r in records)
    sess['has_power'] = any(r.get('power') for r in records)
    best, zone_rows = [], []

    hrs = [r['hr'] for r in records if r.get('hr')]
    if hrs:
        sess['avg_hr'] = sess.get('avg_hr') or int(round(_mean(hrs)))
        sess['max_hr'] = sess.get('max_hr') or max(hrs)

    max_hr = zones.get('max_hr')
    rest_hr = zones.get('resting_hr') or DEFAULT_REST_HR
    ftp = zones.get('ftp')
    female = bool(zones.get('female'))

    if hrs and max_hr:
        seconds = {}
        for rec, dt in zip(records, durations):
            if rec.get('hr'):
                z = zone_of(rec['hr'], max_hr, HR_ZONE_BOUNDS)
                seconds[z] = seconds.get(z, 0) + dt
        zone_rows += [{'kind': 'hr', 'zone': z, 'seconds': s} for z, s in sorted(seconds.items())]

    if hrs:
        mhr = max_hr or DEFAULT_MAX_HR
        value = trimp(records, durations, mhr, rest_hr, female)
        sess['trimp'] = round(value, 1)
        threshold = zones.get('threshold_hr') or 0.9 * mhr
        per_hour = trimp_per_hour_at(threshold, mhr, rest_hr, female)
        hr_tss = value / per_hour * 100 if per_hour else None
    else:
        hr_tss = None

    if sess['has_power']:
        p1 = per_second(records, 'power')
        np_ = normalized_power(p1)
        sess['norm_power'] = sess.get('norm_power') or np_
        powers = [r['power'] for r in records if r.get('power') is not None]
        sess['avg_power'] = sess.get('avg_power') or (round(_mean(powers), 1) if powers else None)
        sess['max_power'] = sess.get('max_power') or (max(powers) if powers else None)
        best += best_power(p1)
        # FTP is a cycling number. Watches also report *running* power, on a different scale:
        # scoring a run against bike FTP gave a 63-minute run a TSS of 285.
        if ftp and sess.get('sport') == 'cycling':
            seconds = {}
            for rec, dt in zip(records, durations):
                if rec.get('power') is not None:
                    z = max(1, zone_of(rec['power'], ftp, POWER_ZONE_BOUNDS))
                    seconds[z] = seconds.get(z, 0) + dt
            zone_rows += [{'kind': 'power', 'zone': z, 'seconds': s} for z, s in sorted(seconds.items())]
            if sess.get('norm_power'):
                duration = sess.get('timer_s') or len(p1)
                sess['intensity_factor'] = round(sess['norm_power'] / ftp, 3)
                sess['tss'] = round(duration * sess['norm_power'] * sess['intensity_factor'] / (ftp * 3600) * 100, 1)

    if sess.get('sport') in ('running', 'walking', 'hiking'):
        best += best_pace(records)

    use_tss = sess.get('tss') is not None and load_model in ('auto', 'tss')
    if use_tss:
        sess['load'], sess['load_model'] = sess['tss'], 'tss'
    elif hr_tss is not None:
        sess['load'], sess['load_model'] = round(hr_tss, 1), 'trimp'

    channel = 'power' if sess['has_power'] else 'speed'
    sess['efficiency'], sess['decoupling'] = _decoupling(records, channel)

    lengths = [l for l in sess.get('lengths') or [] if l.get('length_type') == 'active']
    if lengths:
        sess['total_strokes'] = sess.get('total_strokes') or sum(l['strokes'] or 0 for l in lengths)
        swolfs = [l['swolf'] for l in lengths if l.get('swolf')]
        sess['avg_swolf'] = round(_mean(swolfs), 1) if swolfs else None

    active = [s for s in sess.get('sets') or [] if s.get('set_type') == 'active']
    if active:
        sess['total_sets'] = len(active)
        sess['total_reps'] = sum(s['reps'] or 0 for s in active)
        sess['volume_kg'] = round(sum((s['reps'] or 0) * (s['weight_kg'] or 0) for s in active), 1)

    sess['_best'], sess['_zones'] = best, zone_rows
    return sess
