"""One pass over a FIT file -> plain dicts shaped like the DB rows.

Tolerant by design: a file that breaks part-way is returned with status 'partial' and whatever
was read, instead of being thrown away. Where the summaries sit varies by device (some write
`session` last, an Epix Pro multisport file writes them first), so nothing here depends on
message order; if the summary is missing altogether a session is rebuilt from the samples.
"""
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import fitparse

FIT_EPOCH = datetime(1989, 12, 31)
SEMICIRCLE = 180.0 / 2 ** 31

# Inputs come from users, the parser is pure python: bound the work one file can cause.
MAX_SECONDS = 180
MAX_MESSAGES = 3_000_000

RUN_SPORTS = {'running', 'walking', 'hiking', 'trail_running'}

# Average speed (m/s) above which a leg's distance is treated as corrupt, not as a performance.
PLAUSIBLE_AVG_SPEED = {'running': 8.0, 'walking': 4.0, 'hiking': 4.0, 'swimming': 3.0,
                       'cycling': 25.0, 'transition': 12.0}
PLAUSIBLE_AVG_SPEED_DEFAULT = 60.0

# A sample may sit this far past twice the leg's own duration before it is treated as a corrupt
# timestamp rather than data. Generous: a paused-and-resumed activity legitimately spans far
# more wall-clock time than its timer.
STRAY_SAMPLE_SLACK_S = 6 * 3600

# A gap this long between consecutive samples means the tail after it is not part of the
# activity. Well above any real pause: an hour standing at a finish line still records.
STRAY_SAMPLE_GAP_S = 4 * 3600


class ParseLimitError(Exception):
    pass


@dataclass
class ParsedFile:
    status: str = 'ok'                 # ok | partial | failed
    error: str | None = None
    meta: dict = field(default_factory=dict)
    sessions: list = field(default_factory=list)   # each: row dict + 'laps','records','lengths','sets'
    devices: list = field(default_factory=list)
    profile: dict = field(default_factory=dict)
    counts: dict = field(default_factory=dict)


def num(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (tuple, list)):
        for item in value:
            if isinstance(item, (int, float)):
                return float(item)
    return None


def integer(value):
    value = num(value)
    return int(round(value)) if value is not None else None


def text(value):
    if value is None:
        return None
    if isinstance(value, (tuple, list)):
        value = next((v for v in value if v is not None), None)
        if value is None:
            return None
    return str(value)


def first(values: dict, *names):
    for name in names:
        value = values.get(name)
        if value is not None:
            return value
    return None


def degrees(semicircles):
    value = num(semicircles)
    return round(value * SEMICIRCLE, 6) if value is not None else None


def left_share(raw):
    """left_right_balance: low 7 bits = percent, high bit set = that percent is the right leg's."""
    if not isinstance(raw, int):
        return None
    pct = raw & 0x7F
    if pct == 0:
        return None
    return float(100 - pct) if raw & 0x80 else float(pct)


def _summary(v: dict) -> dict:
    """Fields shared by session and lap messages."""
    return {
        'start_time': v.get('start_time'),
        'timer_s': num(v.get('total_timer_time')),
        'elapsed_s': num(v.get('total_elapsed_time')),
        'distance_m': num(v.get('total_distance')),
        'calories': integer(v.get('total_calories')),
        'avg_hr': integer(v.get('avg_heart_rate')),
        'max_hr': integer(v.get('max_heart_rate')),
        'avg_speed': num(first(v, 'enhanced_avg_speed', 'avg_speed')),
        'max_speed': num(first(v, 'enhanced_max_speed', 'max_speed')),
        'ascent_m': num(v.get('total_ascent')),
        'descent_m': num(v.get('total_descent')),
        'avg_power': num(v.get('avg_power')),
        'max_power': num(v.get('max_power')),
        'norm_power': num(v.get('normalized_power')),
    }


def _cadence(v: dict, avg_key: str, sport: str | None):
    value = num(v.get(avg_key))
    if value is None:
        return None
    frac = num(v.get('avg_fractional_cadence')) or 0.0 if avg_key.startswith('avg') else 0.0
    value += frac
    return value * 2 if sport in RUN_SPORTS else value   # FIT counts one foot; show steps/min


def _session_row(v: dict) -> dict:
    sport = text(v.get('sport')) or 'unknown'
    row = _summary(v)
    row.update({
        'sport': sport,
        'sub_sport': text(v.get('sub_sport')),
        'work_j': num(v.get('total_work')),
        'avg_cadence': _cadence(v, first_key(v, 'avg_running_cadence', 'avg_cadence'), sport),
        'max_cadence': _cadence(v, first_key(v, 'max_running_cadence', 'max_cadence'), sport),
        'avg_temp': num(v.get('avg_temperature')),
        'te_aerobic': num(v.get('total_training_effect')),
        'te_anaerobic': num(v.get('total_anaerobic_training_effect')),
        'pool_length_m': num(v.get('pool_length')),
        'num_laps': integer(v.get('num_laps')),
        'start_lat': degrees(v.get('start_position_lat')),
        'start_lon': degrees(v.get('start_position_long')),
        'total_strokes': integer(v.get('total_strokes')) if sport == 'swimming' else None,
    })
    return row


def first_key(v: dict, *names) -> str:
    for name in names:
        if v.get(name) is not None:
            return name
    return names[-1]


def _lap_row(v: dict) -> dict:
    sport = text(v.get('sport'))
    row = _summary(v)
    row.pop('norm_power')
    row.update({
        'norm_power': num(v.get('normalized_power')),
        'avg_cadence': _cadence(v, first_key(v, 'avg_running_cadence', 'avg_cadence'), sport),
        'intensity': text(v.get('intensity')),
        'trigger': text(v.get('lap_trigger')),
        'swim_stroke': text(v.get('swim_stroke')),
        'num_lengths': integer(v.get('num_lengths')),
    })
    return row


def _record_row(v: dict) -> dict:
    return {
        '_ts': v.get('timestamp'),
        'hr': integer(v.get('heart_rate')),
        'speed': num(first(v, 'enhanced_speed', 'speed')),
        'dist': num(v.get('distance')),
        'lat': degrees(v.get('position_lat')),
        'lon': degrees(v.get('position_long')),
        'alt': num(first(v, 'enhanced_altitude', 'altitude')),
        'cad': (num(v.get('cadence')) or 0) + (num(v.get('fractional_cadence')) or 0)
               if v.get('cadence') is not None else None,
        'power': integer(v.get('power')),
        'temp': integer(v.get('temperature')),
        'grade': num(v.get('grade')),
        'lrb': left_share(v.get('left_right_balance')),
        'vo': num(v.get('vertical_oscillation')),
        'vr': num(v.get('vertical_ratio')),
        'gct': num(v.get('stance_time')),
        'gct_bal': num(v.get('stance_time_balance')),
        'step_len': num(v.get('step_length')),
        'l_te': num(v.get('left_torque_effectiveness')),
        'r_te': num(v.get('right_torque_effectiveness')),
        'l_ps': num(v.get('left_pedal_smoothness')),
        'r_ps': num(v.get('right_pedal_smoothness')),
    }


def _length_row(v: dict) -> dict:
    timer = num(v.get('total_timer_time'))
    strokes = integer(v.get('total_strokes'))
    active = text(v.get('length_type')) == 'active'
    return {
        'start_time': v.get('start_time'),
        'timer_s': timer,
        'length_type': text(v.get('length_type')),
        'stroke': text(v.get('swim_stroke')),
        'strokes': strokes,
        'avg_speed': num(v.get('avg_speed')),
        'cadence': integer(v.get('avg_swimming_cadence')),
        'swolf': round(timer + strokes, 1) if active and timer and strokes else None,
    }


def _set_row(v: dict) -> dict:
    return {
        'start_time': first(v, 'start_time', 'timestamp'),
        'duration_s': num(v.get('duration')),
        'set_type': text(v.get('set_type')),
        'category': text(v.get('category')),
        'subtype': text(v.get('category_subtype')),
        'reps': integer(v.get('repetitions')),
        'weight_kg': num(v.get('weight')),
    }


def _device_update(devices: dict, v: dict) -> None:
    key = text(v.get('device_index')) or '?'
    dev = devices.setdefault(key, {'device_index': key})
    updates = {
        'seen_at': v.get('timestamp'),
        'manufacturer': text(v.get('manufacturer')),
        'product': text(first(v, 'garmin_product', 'product', 'product_name')),
        'serial': text(v.get('serial_number')),
        'device_type': text(first(v, 'antplus_device_type', 'ant_device_type', 'device_type')),
        'source_type': text(v.get('source_type')),
        'sw_version': text(v.get('software_version')),
        'battery_status': text(v.get('battery_status')),
        'battery_voltage': num(v.get('battery_voltage')),
        'operating_time_s': integer(v.get('cum_operating_time')),
    }
    dev.update({k: val for k, val in updates.items() if val is not None})


def _assign(items: list, sessions: list, key: str, dest: str) -> None:
    """Put each timestamped item into the leg it happened in: the last session that had
    started by then. Deliberately based on start_time only - the session message's own
    `timestamp` is NOT a reliable end time (an Epix Pro multisport file writes all its session
    summaries up front, every one stamped with the file's start)."""
    if not sessions:
        return
    for item in items:
        ts = item.get(key)
        target = sessions[0] if ts is not None else sessions[-1]
        if ts is not None:
            for sess in sessions:
                if sess['start_time'] <= ts:
                    target = sess
                else:
                    break
        target[dest].append(item)


def parse_fit_file(path: str) -> ParsedFile:
    out = ParsedFile()
    counts: Counter = Counter()
    sessions, laps, records, lengths, sets, vo2 = [], [], [], [], [], []
    devices: dict = {}
    sport_msgs: list = []
    started = time.monotonic()

    try:
        fit = fitparse.UncachedFitFile(str(path))
        for n, msg in enumerate(fit.get_messages()):
            if n % 5000 == 0:
                if time.monotonic() - started > MAX_SECONDS or n > MAX_MESSAGES:
                    raise ParseLimitError(f'parse limit reached after {n} messages')
            if not hasattr(msg, 'get_values'):
                continue
            name = msg.name
            counts[name] += 1
            if name == 'record':
                records.append(_record_row(msg.get_values()))
                continue
            v = msg.get_values()
            if name == 'file_id':
                out.meta.update({
                    'file_type': text(v.get('type')),
                    'manufacturer': text(v.get('manufacturer')),
                    'product': text(first(v, 'garmin_product', 'product', 'product_name')),
                    'serial_number': text(v.get('serial_number')),
                    'time_created': v.get('time_created'),
                })
            elif name == 'activity':
                local, utc = v.get('local_timestamp'), v.get('timestamp')
                if isinstance(local, datetime) and isinstance(utc, datetime):
                    out.meta['utc_offset_s'] = int((local - utc).total_seconds())
            elif name == 'session':
                sessions.append(_session_row(v))
            elif name == 'lap':
                laps.append(_lap_row(v))
            elif name == 'length':
                lengths.append(_length_row(v))
            elif name == 'set':
                sets.append(_set_row(v))
            elif name == 'sport':
                sport_msgs.append((text(v.get('sport')), text(v.get('sub_sport'))))
            elif name == 'device_info':
                _device_update(devices, v)
            elif name == 'user_profile':
                out.profile.update({k: val for k, val in {
                    'weight_kg': num(v.get('weight')), 'height_m': num(v.get('height')),
                    'resting_hr': integer(v.get('resting_heart_rate')) or None,   # 0 = not set
                    'activity_class': num(v.get('activity_class')), 'gender': text(v.get('gender')),
                }.items() if val is not None})
            elif name == 'zones_target':
                out.profile.update({k: val for k, val in {
                    'max_hr': integer(v.get('max_heart_rate')),
                    'threshold_hr': integer(v.get('threshold_heart_rate')),
                    'ftp': integer(v.get('functional_threshold_power')),
                }.items() if val})
            elif name == 'unknown_140':
                # Garmin-private. Field 7 * 3.5 / 65536 = VO2 max (ml/kg/min); field 253 = timestamp.
                raw, ts = v.get('unknown_7'), v.get('unknown_253')
                if isinstance(raw, (int, float)) and raw:
                    when = FIT_EPOCH + timedelta(seconds=ts) if isinstance(ts, (int, float)) else None
                    vo2.append({'_ts': when, 'value': round(raw * 3.5 / 65536, 2)})
    except ParseLimitError as exc:
        out.status, out.error = 'partial', str(exc)
    except Exception as exc:  # fitparse raises several types on corrupt input
        out.status, out.error = 'partial', f'{type(exc).__name__}: {exc}'[:500]

    out.counts = dict(counts)
    out.devices = list(devices.values())

    records = [r for r in records if isinstance(r['_ts'], datetime)]
    sessions = [s for s in sessions if isinstance(s['start_time'], datetime)]
    sessions.sort(key=lambda s: s['start_time'])

    if not sessions and records:
        # The summary was lost with the corrupt tail: rebuild a minimal session from the samples.
        sport, sub = sport_msgs[0] if sport_msgs else ('unknown', None)
        start, end = records[0]['_ts'], records[-1]['_ts']
        dists = [r['dist'] for r in records if r['dist'] is not None]
        sessions = [{**_summary({}), 'sport': sport or 'unknown', 'sub_sport': sub,
                     'start_time': start, 'elapsed_s': (end - start).total_seconds(),
                     'timer_s': (end - start).total_seconds(),
                     'distance_m': dists[-1] if dists else None}]
        out.status = 'partial'
        out.error = out.error or 'no session summary in file; rebuilt from records'

    if not sessions:
        out.status = 'failed'
        out.error = out.error or 'no session or record data (not an activity file?)'
        return out

    for idx, sess in enumerate(sessions):
        sess['idx'] = idx
        for key in ('laps', 'records', 'lengths', 'sets', '_vo2'):
            sess[key] = []
        if sess['sport'] == 'unknown' and idx < len(sport_msgs) and sport_msgs[idx][0]:
            sess['sport'], sess['sub_sport'] = sport_msgs[idx]

    _assign(records, sessions, '_ts', 'records')
    _assign(laps, sessions, 'start_time', 'laps')
    _assign(lengths, sessions, 'start_time', 'lengths')
    _assign(sets, sessions, 'start_time', 'sets')
    _assign(vo2, sessions, '_ts', '_vo2')

    # Longest leg duration in the file, as the yardstick for a plausible sample time. Derived
    # from the session summaries, which a corrupt record cannot influence.
    file_span = max([max((s.get('elapsed_s') or 0), (s.get('timer_s') or 0)) for s in sessions] or [0])
    file_span = max(file_span, sum((s.get('elapsed_s') or 0) for s in sessions))

    for sess in sessions:
        start = sess['start_time']
        # Watches occasionally emit a record with a corrupt timestamp - seen days after the
        # activity in this archive. Left in, it stretches the timeline: decoupling compares
        # halves that are not halves, time-in-zone spans the gap, and per_second() builds a
        # multi-day grid. The window spans the WHOLE FILE, not this leg: in a multisport file
        # a short leg's samples legitimately run on to the next leg's start, and the last leg
        # is open-ended.
        # Samples may also legitimately PRECEDE the session start: this archive has a race file
        # whose recording begins 45 min before the first leg's clock.
        limit = file_span * 2 + STRAY_SAMPLE_SLACK_S
        kept = [r for r in sess['records']
                if -limit <= (r['_ts'] - start).total_seconds() <= limit]
        # Then cut the tail after any implausible gap. In this archive the stray samples sit
        # hours to days past a huge gap at the leg's end, well inside any absolute window a
        # long activity needs - the gap itself is the reliable signal, not the offset.
        for i in range(len(kept) - 1, 0, -1):
            if (kept[i]['_ts'] - kept[i - 1]['_ts']).total_seconds() > STRAY_SAMPLE_GAP_S:
                kept = kept[:i]
                break
        if len(kept) != len(sess['records']):
            out.status = 'partial' if out.status == 'ok' else out.status
            out.error = out.error or (f"dropped {len(sess['records']) - len(kept)} sample(s) timestamped "
                                      f"outside the {sess['sport']} leg")
            sess['records'] = kept
        for i, rec in enumerate(sess['records']):
            rec['idx'] = i
            rec['t'] = int((rec.pop('_ts') - start).total_seconds())
            if sess['sport'] in RUN_SPORTS and rec['cad'] is not None:
                rec['cad'] *= 2
            if rec['cad'] is not None:
                rec['cad'] = int(round(rec['cad']))
        for name in ('laps', 'lengths', 'sets'):
            for i, item in enumerate(sess[name]):
                item['idx'] = i
        values = [x['value'] for x in sess.pop('_vo2')]
        sess['vo2max'] = max(values) if values else None

        if not sess.get('timer_s') and sess['records']:
            sess['timer_s'] = float(sess['records'][-1]['t'])
        duration = sess.get('timer_s') or sess.get('elapsed_s')
        limit = PLAUSIBLE_AVG_SPEED.get(sess['sport'], PLAUSIBLE_AVG_SPEED_DEFAULT)
        if duration and sess.get('distance_m') and sess['distance_m'] / duration > limit:
            # Seen in files run through FIT "repair" tools: totals that no human produced.
            # Keep the activity, drop the numbers that would poison trends and records.
            out.status = 'partial'
            out.error = out.error or (f"implausible distance in {sess['sport']} leg "
                                      f"({sess['distance_m'] / 1000:.0f} km in {duration / 60:.0f} min) ignored")
            sess['distance_m'] = sess['avg_speed'] = sess['max_speed'] = None
            for rec in sess['records']:
                rec['dist'] = rec['speed'] = None

    out.sessions = sessions
    out.meta['start_time'] = sessions[0]['start_time']
    return out
