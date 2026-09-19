from app.services import metrics
from app.services.fit_parser import left_share, parse_fit_file

from .conftest import SAMPLE_MULTI, SAMPLE_OLD, SAMPLE_RUN, SAMPLE_SWIM, SAMPLE_VO2


def test_parses_run():
    parsed = parse_fit_file(SAMPLE_RUN)
    assert parsed.status == 'ok'
    assert len(parsed.sessions) == 1
    sess = parsed.sessions[0]
    assert sess['sport'] == 'running'
    assert sess['distance_m'] > 0 and sess['timer_s'] > 0
    assert len(sess['records']) > 100
    assert [r['idx'] for r in sess['records'][:3]] == [0, 1, 2]
    assert sess['records'][0]['t'] >= 0
    assert all(r['t'] <= nxt['t'] for r, nxt in zip(sess['records'], sess['records'][1:]))
    assert parsed.counts['record'] == len(sess['records'])
    fixes = [(r['lat'], r['lon']) for r in sess['records'] if r['lat'] is not None]
    assert fixes and all(-90 <= lat <= 90 and -180 <= lon <= 180 for lat, lon in fixes)


def test_parses_swim_with_sub_sport_lengths_profile_and_devices():
    parsed = parse_fit_file(SAMPLE_SWIM)
    sess = parsed.sessions[0]
    assert (sess['sport'], sess['sub_sport']) == ('swimming', 'lap_swimming')   # v1 read 'subsport': always None
    assert len(sess['lengths']) == 33
    assert any(l['swolf'] for l in sess['lengths'])
    assert parsed.meta['manufacturer'] == 'garmin'
    assert parsed.profile.get('weight_kg')
    assert any(d.get('device_index') == 'creator' for d in parsed.devices)


def test_vo2max_from_private_message():
    sess = parse_fit_file(SAMPLE_VO2).sessions[0]
    assert sess['vo2max'] == 62.63


def test_multisport_file_keeps_every_leg():
    parsed = parse_fit_file(SAMPLE_MULTI)
    assert len(parsed.sessions) == 7
    assert [s['idx'] for s in parsed.sessions] == list(range(7))
    assert sum(len(s['records']) for s in parsed.sessions) == parsed.counts['record']
    starts = [s['start_time'] for s in parsed.sessions]
    assert starts == sorted(starts)
    assert all(s['records'] for s in parsed.sessions if (s['timer_s'] or 0) > 60)


def test_old_sdk_file_parses():
    parsed = parse_fit_file(SAMPLE_OLD)
    assert parsed.status == 'ok' and parsed.sessions


def test_truncated_file_is_partial_not_lost(tmp_path):
    raw = SAMPLE_RUN.read_bytes()
    broken = tmp_path / 'broken.fit'
    broken.write_bytes(raw[: len(raw) // 2])       # loses the session summary at the end
    parsed = parse_fit_file(broken)
    assert parsed.status == 'partial'
    assert parsed.error
    assert parsed.sessions and parsed.sessions[0]['records']   # rebuilt from the samples


def test_garbage_is_failed(tmp_path):
    junk = tmp_path / 'junk.fit'
    junk.write_bytes(b'\x0e\x10\x00\x00\x00\x00\x00\x00.FIT\x00\x00' + b'\xff' * 64)
    assert parse_fit_file(junk).status == 'failed'


def test_left_right_balance():
    assert left_share(0x80 | 48) == 52.0           # flag set: 48 % is the right leg
    assert left_share(51) == 51.0
    assert left_share(None) is None


def _steady(seconds, power=None, hr=None, speed=None):
    return [{'t': t, 'power': power, 'hr': hr, 'speed': speed,
             'dist': (speed or 0) * t, 'lat': None} for t in range(seconds)]


def test_normalized_power_of_steady_effort_equals_average():
    assert metrics.normalized_power([200] * 600) == 200.0


def test_normalized_power_punishes_variability():
    surges = ([100] * 60 + [300] * 60) * 10
    assert metrics.normalized_power(surges) > sum(surges) / len(surges)


def test_best_power_windows():
    power = [100] * 300 + [400] * 60 + [100] * 300
    best = {b['window']: b for b in metrics.best_power(power)}
    assert best[60]['value'] == 400.0 and best[60]['offset_s'] == 300
    assert best[5]['value'] == 400.0
    assert 100 < best[300]['value'] < 400
    assert 1200 not in best                        # activity shorter than the window


def test_best_pace_finds_fastest_kilometre():
    records, dist = [], 0.0
    for t in range(1200):
        dist += 5.0 if 300 <= t < 500 else 2.5     # 200 s at 5 m/s = exactly 1 km
        records.append({'t': t, 'dist': dist})
    best = {b['window']: b for b in metrics.best_pace(records)}
    assert abs(best[1000]['value'] - 200) <= 1.5
    assert 5000 not in best


def test_tss_is_100_for_an_hour_at_ftp():
    sess = {'sport': 'cycling', 'timer_s': 3600, 'records': _steady(3600, power=250, hr=150)}
    metrics.compute_session(sess, {'ftp': 250, 'max_hr': 185, 'threshold_hr': 165, 'resting_hr': 50})
    assert abs(sess['tss'] - 100) < 1
    assert sess['intensity_factor'] == 1.0
    assert sess['load_model'] == 'tss' and sess['load'] == sess['tss']
    zones = {(z['kind'], z['zone']): z['seconds'] for z in sess['_zones']}
    assert zones[('power', 4)] == 3600             # 100 % of FTP is zone 4 (90-105 %)
    assert zones[('hr', 4)] == 3600                # 150/185 = 81 %


def test_hr_load_is_100_for_an_hour_at_threshold():
    sess = {'sport': 'running', 'timer_s': 3600, 'records': _steady(3600, hr=165, speed=3.0)}
    metrics.compute_session(sess, {'max_hr': 185, 'threshold_hr': 165, 'resting_hr': 50, 'ftp': None})
    assert sess['load_model'] == 'trimp'
    assert abs(sess['load'] - 100) < 1
    assert sess['efficiency'] and abs(sess['decoupling']) < 0.01


def test_gaps_are_not_counted_as_time_in_zone():
    records = _steady(100, hr=150) + [{'t': 5000 + t, 'hr': 150, 'power': None, 'speed': None,
                                       'dist': None, 'lat': None} for t in range(100)]
    sess = {'sport': 'running', 'records': records}
    metrics.compute_session(sess, {'max_hr': 185})
    assert sum(z['seconds'] for z in sess['_zones']) <= 200 + metrics.MAX_GAP_S


def test_strength_and_swim_totals():
    sess = {'sport': 'training', 'records': [],
            'sets': [{'set_type': 'active', 'reps': 10, 'weight_kg': 40.0},
                     {'set_type': 'rest', 'reps': None, 'weight_kg': None},
                     {'set_type': 'active', 'reps': 8, 'weight_kg': 50.0}],
            'lengths': [{'length_type': 'active', 'strokes': 20, 'swolf': 50.0},
                        {'length_type': 'idle', 'strokes': None, 'swolf': None},
                        {'length_type': 'active', 'strokes': 22, 'swolf': 54.0}]}
    metrics.compute_session(sess, {})
    assert (sess['total_sets'], sess['total_reps'], sess['volume_kg']) == (2, 18, 800.0)
    assert sess['total_strokes'] == 42 and sess['avg_swolf'] == 52.0


def test_distance_jump_is_not_a_personal_best():
    """Seen in the wild: a 'repaired' triathlon file whose distance leaps by hundreds of km."""
    records, dist = [], 0.0
    for t in range(1500):
        dist += 3.0
        if t == 700:
            dist += 500_000                         # the glitch
        records.append({'t': t, 'dist': dist})
    best = {b['window']: b['value'] for b in metrics.best_pace(records)}
    assert abs(best[1000] - 1000 / 3.0) < 2          # honest 3 m/s pace, not "1 km in 1 s"
    assert 5000 not in best                          # only 4.5 km was really covered


def test_implausible_leg_totals_are_dropped_not_trusted(tmp_path, monkeypatch):
    from app.services import fit_parser
    monkeypatch.setitem(fit_parser.PLAUSIBLE_AVG_SPEED, 'running', 0.5)   # make a normal run "impossible"
    parsed = fit_parser.parse_fit_file(SAMPLE_RUN)
    sess = parsed.sessions[0]
    assert parsed.status == 'partial' and 'implausible' in parsed.error
    assert sess['distance_m'] is None and sess['avg_speed'] is None
    assert sess['records'] and all(r['dist'] is None for r in sess['records'])
    assert sess['avg_hr'] or sess['timer_s']         # the rest of the activity is kept


def test_running_power_is_never_scored_against_cycling_ftp():
    sess = {'sport': 'running', 'timer_s': 3600, 'records': _steady(3600, power=350, hr=150, speed=3.0)}
    metrics.compute_session(sess, {'ftp': 180, 'max_hr': 185, 'threshold_hr': 165, 'resting_hr': 50})
    assert sess.get('tss') is None and sess['load_model'] == 'trimp' and sess['load'] < 150
    assert not [z for z in sess['_zones'] if z['kind'] == 'power']
    assert any(b['kind'] == 'power' for b in sess['_best'])     # still gets its own (running) power curve
