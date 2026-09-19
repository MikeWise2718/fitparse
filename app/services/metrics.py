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
