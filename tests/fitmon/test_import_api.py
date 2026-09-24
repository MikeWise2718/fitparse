from datetime import datetime
import io
import zipfile

import pytest

from .conftest import SAMPLE_MULTI, SAMPLE_POWER, SAMPLE_RUN, SAMPLE_SWIM, SAMPLE_VO2


def _zip(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def _upload(client, files: dict):
    data = {'files': [(io.BytesIO(raw), name) for name, raw in files.items()]}
    return client.client.post('/api/import/upload', data=data, content_type='multipart/form-data',
                              headers={'X-CSRF-Token': client.csrf})


def test_upload_runs_in_the_worker_and_dedupes(app, alice, drain):
    resp = _upload(alice, {'run.fit': SAMPLE_RUN.read_bytes(), 'notes.txt': b'hello'})
    assert resp.status_code == 202
    job_id = resp.get_json()['job']['id']
    assert alice.get('/api/activities').get_json()['total'] == 0      # nothing parsed in the request
    assert drain() == 1
    job = alice.get(f'/api/jobs/{job_id}').get_json()['job']
    assert job['status'] == 'done' and job['result']['counts'] == {'imported': 1}
    assert alice.get('/api/activities').get_json()['total'] == 1

    _upload(alice, {'renamed.fit': SAMPLE_RUN.read_bytes()})
    drain()
    assert alice.get('/api/jobs').get_json()['jobs'][0]['result']['counts'] == {'duplicate': 1}
    assert not list((app.config['FITMON_HOME'] / 'users' / str(alice.user_id) / 'incoming').iterdir())


def test_zip_with_nested_zip_and_junk(app, home, alice):
    from app.services import importer
    inner = _zip({'DI_CONNECT/inner/b.fit': SAMPLE_VO2.read_bytes()})
    outer = _zip({'a.fit': SAMPLE_RUN.read_bytes(), 'Uploaded_1.zip': inner,
                  '../../evil.fit': SAMPLE_POWER.read_bytes(), 'readme.txt': b'x', 'fake.fit': b'not fit'})
    with app.app_context():
        results = list(importer.import_upload(home, alice.user_id, outer, 'export.zip'))
    statuses = sorted(r['status'] for r in results)
    assert statuses == ['imported', 'imported', 'imported', 'rejected']
    # member names are labels only: nothing is ever written to a path from the archive
    stored = [p for p in (home / 'users').rglob('*.fit')]
    assert len(stored) == 3 and all(len(p.stem) == 64 for p in stored)
    assert not list(home.parent.rglob('evil.fit'))


def test_zip_bomb_is_refused(app, home, alice, monkeypatch):
    from app.services import importer
    monkeypatch.setattr(importer, 'MAX_ZIP_TOTAL_BYTES', 50_000)
    bomb = _zip({f'{i}.fit': SAMPLE_RUN.read_bytes() for i in range(3)})
    with app.app_context():
        results = list(importer.import_upload(home, alice.user_id, bomb, 'bomb.zip'))
    assert results[-1]['status'] == 'rejected' and 'size limit' in results[-1]['reason']


def test_quota(app, home, alice, import_file):
    from app.settings import save_global_settings
    save_global_settings(home, {'user_quota_files': 1})
    assert import_file(alice.user_id, SAMPLE_RUN)['status'] == 'imported'
    res = import_file(alice.user_id, SAMPLE_VO2)
    assert res['status'] == 'rejected' and 'quota' in res['reason']


def test_clean_copy_replaces_a_corrupt_one(app, home, alice):
    """The triathlon case: a truncated local file, later re-fetched intact from Garmin."""
    from app.models import FitFile
    from app.services import importer
    raw = SAMPLE_SWIM.read_bytes()
    with app.app_context():
        first = importer.import_bytes(home, alice.user_id, raw[: int(len(raw) * 0.7)], 'swim.fit')
        assert first['status'] == 'partial'
        second = importer.import_bytes(home, alice.user_id, raw, '11698729774_ACTIVITY.fit', source='garmin')
        assert second['status'] == 'replaced'
        rows = FitFile.query.filter_by(user_id=alice.user_id).all()
        assert len(rows) == 1 and rows[0].parse_status == 'ok'
        # and the worse copy never displaces the better one
        third = importer.import_bytes(home, alice.user_id, raw[: int(len(raw) * 0.6)], 'swim2.fit')
        assert third['status'] == 'duplicate'
    assert len(list((home / 'users').rglob('*.fit'))) == 1


def test_multisport_and_detail_endpoints(alice, import_file):
    import_file(alice.user_id, SAMPLE_MULTI)
    listing = alice.get('/api/activities?hide_transitions=0').get_json()
    assert listing['total'] == 7 and all(a['legs'] == 7 for a in listing['activities'])
    sid = listing['activities'][0]['id']
    detail = alice.get(f'/api/sessions/{sid}').get_json()
    assert len(detail['siblings']) == 7
    series = alice.get(f'/api/sessions/{sid}/records?fields=hr,speed,bogus&downsample=200').get_json()
    assert set(series['series']) == {'t', 'hr', 'speed'} and 0 < series['points'] <= 400


def test_power_file_gets_metrics_curve_and_zones(app, home, alice, import_file):
    from app.settings import save_user_settings
    save_user_settings(home, alice.user_id, {'zones': {'ftp': 250, 'max_hr': 185}})
    import_file(alice.user_id, SAMPLE_POWER)
    act = alice.get('/api/activities').get_json()['activities'][0]
    assert act['has_power'] and act['norm_power'] and act['tss'] and act['load_model'] == 'tss'
    detail = alice.get(f"/api/sessions/{act['id']}").get_json()
    assert {z['kind'] for z in detail['zones']} == {'hr', 'power'}
    windows = [b['window'] for b in detail['best_efforts'] if b['kind'] == 'power']
    assert windows[:4] == [5, 15, 30, 60]
    curve = alice.get('/api/trends/power-curve').get_json()['all_time']
    assert curve[0]['value'] >= curve[-1]['value']                  # shorter windows, higher power
    fitness = alice.get('/api/fitness').get_json()
    assert max(fitness['ctl']) > 0 and len(fitness['days']) == len(fitness['tsb'])


def test_swim_trends_body_and_gear(alice, import_file):
    import_file(alice.user_id, SAMPLE_SWIM)
    point = alice.get('/api/trends/swim').get_json()['points'][0]
    assert 60 < point['pace_100m_s'] < 400 and point['swolf']
    assert alice.get('/api/body').get_json()['latest']['weight_kg']
    gear = alice.get('/api/gear').get_json()['devices']
    assert gear and gear[0]['is_watch']
    assert alice.get('/api/trends/volume?bucket=month').get_json()['sports'] == ['swimming']
    assert alice.get('/api/trends/vo2max').status_code == 200
    assert alice.get('/api/export/activities.csv').data.count(b'\n') == 2


def test_explorer_surfaces_unknown_messages_and_raw_values(alice, import_file):
    res = import_file(alice.user_id, SAMPLE_VO2)
    fid = res['file_id']
    info = alice.get(f'/api/explorer/{fid}/messages?crc=1').get_json()
    names = {m['name']: m for m in info['messages']}
    assert names['unknown_140']['unknown'] and names['record']['count'] > 0
    assert info['header']['signature_ok'] and info['crc']['crc_ok'] is True
    page = alice.get(f'/api/explorer/{fid}/messages/record?per_page=5').get_json()
    assert len(page['rows']) == 5 and len(page['rows'][0]) == len(page['fields'])
    idx = [f['name'] for f in page['fields']].index('position_lat')
    assert page['fields'][idx]['units'] == 'semicircles'
    std = alice.get(f'/api/explorer/{fid}/messages/record?per_page=5&units=standard').get_json()
    assert std['fields'][idx]['units'] == 'deg' and abs(std['rows'][0][idx]) <= 90
    assert abs(page['raw'][0][idx]) > 1000                          # raw semicircles still shown
    assert alice.get(f'/api/explorer/{fid}/series?message=record&field=heart_rate').get_json()['values']
    assert alice.get(f'/api/explorer/{fid}/dump/session.json').get_json()[0]['fields']
    assert b'heart_rate' in alice.get(f'/api/explorer/{fid}/dump/record.csv').data
    assert alice.get('/api/explorer/search?message=unknown_140').get_json()['files'][0]['id'] == fid
    assert alice.get(f'/api/explorer/{fid}/messages/..%2Fetc').status_code in (400, 404)


def test_settings_persist_server_side_per_user(home, alice, bob):
    alice.post('/api/settings', json={'units': 'statute', 'zones': {'ftp': 260}, 'hacker': 1})
    saved = alice.get('/api/settings').get_json()
    assert saved['units'] == 'statute' and saved['zones']['ftp'] == 260 and 'hacker' not in saved
    assert saved['zones']['max_hr'] is None                          # nested defaults survive a partial update
    assert bob.get('/api/settings').get_json()['units'] == 'metric'
    assert (home / 'users' / str(alice.user_id) / 'settings.json').exists()


def test_reparse_delete_download_and_account_export(app, home, alice, import_file):
    res = import_file(alice.user_id, SAMPLE_RUN)
    fid = res['file_id']
    assert alice.post(f'/api/files/{fid}/reparse').get_json()['parse_status'] == 'ok'
    assert alice.get('/api/activities').get_json()['total'] == 1     # reparse replaces, never duplicates
    assert alice.get(f'/api/files/{fid}/download').data == SAMPLE_RUN.read_bytes()
    archive = zipfile.ZipFile(io.BytesIO(alice.get('/api/account/export').data))
    assert sum(n.endswith('.fit') for n in archive.namelist()) == 1 and 'activities.csv' in archive.namelist()
    assert alice.post(f'/api/files/{fid}/delete').status_code == 200
    assert alice.get('/api/activities').get_json()['total'] == 0
    assert not list((home / 'users').rglob('*.fit'))
    with app.app_context():
        from app.models import Record
        assert Record.query.count() == 0                             # cascade reached the samples


def test_unhandled_errors_do_not_leak_details(app, alice, monkeypatch):
    from app.services import fitness
    monkeypatch.setattr(fitness, 'gear', lambda uid: 1 / 0)
    resp = alice.get('/api/gear')
    assert resp.status_code == 500
    body = resp.get_json()
    assert body['error'] == 'internal_error' and 'division' not in resp.get_data(as_text=True)
    assert alice.get('/api/admin/events?prefix=app.').get_json()['events'][-1]['error_id'] == body['error_id']


def test_vo2max_series_plots_only_real_recalculations(app, alice, import_file):
    """Garmin stamps its stored VO2 max into files that never measured one (walks, swims), and
    repeats the last value when it cannot recalculate (cycling without a power meter)."""
    from app.models import Session, db
    from app.services import fitness
    import_file(alice.user_id, SAMPLE_VO2)
    with app.app_context():
        base = Session.query.one()
        # `carried` is what the importer would have computed for each row (see
        # importer._vo2_is_carried); the series filters on the stored flag.
        rows = [('cycling', '2026-01-01', 47.0, False), ('cycling', '2026-01-02', 47.0, True),
                ('cycling', '2026-01-03', 46.0, False), ('walking', '2026-01-04', 46.0, True),
                ('swimming', '2026-01-05', 46.0, True), ('running', '2026-01-06', 44.0, False)]
        for sport, day, value, carried in rows:
            db.session.add(Session(file_id=base.file_id, user_id=alice.user_id, sport=sport,
                                   start_time=datetime.fromisoformat(day + 'T08:00'), vo2max=value,
                                   vo2max_carried=carried))
        db.session.delete(base)
        db.session.commit()
        series = fitness.vo2max_series(alice.user_id, None, None, None, None)
    plotted = [(p['sport'], p['at'].date().isoformat(), p['vo2max']) for p in series]
    assert plotted == [('cycling', '2026-01-01', 47.0), ('cycling', '2026-01-03', 46.0),
                       ('running', '2026-01-06', 44.0)]
    body = alice.get('/api/trends/vo2max').get_json()
    assert len(body['points']) == 3 and body['garmin'] == []
    assert len(alice.get('/api/trends/vo2max?all=1').get_json()['points']) == 4   # incl. the repeat


def test_trim_survives_reindex_and_deletes_nothing(app, home, alice, import_file):
    """A watch left recording after the finish. The trim changes what the figures are computed
    over; it must not delete samples, and must survive the re-index that recreates sessions."""
    from app.models import Record, Session, SessionTrim, db
    res = import_file(alice.user_id, SAMPLE_RUN)
    act = alice.get('/api/activities').get_json()['activities'][0]
    sid, before = act['id'], act['timer_s']
    with app.app_context():
        n_records = Record.query.count()
        last_t = db.session.query(db.func.max(Record.t)).scalar()

    cut = int(last_t * 0.6)
    assert alice.post(f'/api/sessions/{sid}/trim', json={'end_t': cut}).status_code == 200
    trimmed = alice.get('/api/activities').get_json()['activities'][0]
    assert trimmed['timer_s'] < before
    assert trimmed['trimmed_s'] <= cut
    with app.app_context():
        assert Record.query.count() < n_records          # only the indexed samples shrink...
    assert (home / 'users' / str(alice.user_id) / 'fit').exists()

    # a re-index must not silently discard the trim
    alice.post(f"/api/files/{res['file_id']}/reparse")
    after = alice.get('/api/activities').get_json()['activities'][0]
    assert after['timer_s'] == trimmed['timer_s']
    with app.app_context():
        assert SessionTrim.query.count() == 1

    # undo restores the full activity
    sid2 = alice.get('/api/activities').get_json()['activities'][0]['id']
    assert alice.post(f'/api/sessions/{sid2}/trim', json={'end_t': None}).status_code == 200
    restored = alice.get('/api/activities').get_json()['activities'][0]
    assert restored['timer_s'] == before and restored['trimmed_s'] is None
    with app.app_context():
        assert Record.query.count() == n_records and SessionTrim.query.count() == 0


def test_trim_rejects_nonsense_and_is_scoped(alice, bob, import_file):
    import_file(alice.user_id, SAMPLE_RUN)
    sid = alice.get('/api/activities').get_json()['activities'][0]['id']
    assert alice.post(f'/api/sessions/{sid}/trim', json={'end_t': 5}).status_code == 400
    assert alice.post(f'/api/sessions/{sid}/trim', json={'end_t': 'soon'}).status_code == 400
    assert bob.post(f'/api/sessions/{sid}/trim', json={'end_t': 600}).status_code == 404
    assert bob.get(f'/api/sessions/{sid}/trim/suggest').status_code == 404


def test_trim_suggestion_finds_a_stalled_tail():
    """Not stillness: after a finish you still walk about. Forward progress is what collapses."""
    from app.services import metrics
    racing = [{'t': t, 'dist': t * 2.5, 'speed': 2.5, 'hr': 150} for t in range(0, 3600)]
    # 30 min of milling about: occasional movement, almost no ground covered
    tail = [{'t': 3600 + t, 'dist': 9000 + (t // 300) * 20, 'speed': 0.3 if t % 120 else 1.2, 'hr': 110}
            for t in range(0, 1800)]
    sug = metrics.suggest_trim(racing + tail, 'running')
    assert sug and abs(sug['end_t'] - 3600) < 400 and sug['dropped_s'] > 1000
    assert metrics.suggest_trim(racing, 'running') is None          # a clean activity: no suggestion
    assert metrics.suggest_trim(racing[:100], 'running') is None    # too short to judge


def test_activity_range_filters_are_server_side(app, alice, import_file):
    """Filters must apply to every activity, not just the page the browser has loaded, and a
    row with no value is excluded rather than counted as zero."""
    from app.models import Session, db
    import_file(alice.user_id, SAMPLE_RUN)
    with app.app_context():
        base = Session.query.one()
        for day, dist, hr, mx, power in [('2026-02-01', 5000, 120, 150, None),
                                         ('2026-02-02', 12000, 140, 175, 210.0),
                                         ('2026-02-03', 21000, 155, 188, None)]:
            db.session.add(Session(file_id=base.file_id, user_id=alice.user_id, sport='running',
                                   start_time=datetime.fromisoformat(day + 'T09:00'),
                                   distance_m=dist, avg_hr=hr, max_hr=mx, avg_power=power))
        db.session.delete(base)
        db.session.commit()

    def dists(qs):
        return sorted(a['distance_m'] for a in alice.get('/api/activities?' + qs).get_json()['activities'])

    assert dists('') == [5000.0, 12000.0, 21000.0]
    assert dists('min_distance_m=10000') == [12000.0, 21000.0]
    assert dists('min_distance_m=10000&max_distance_m=15000') == [12000.0]
    assert dists('min_max_hr=180') == [21000.0]                  # max HR is its own filter
    assert dists('min_avg_hr=180') == []                         # and distinct from the average
    # a run that recorded no power must not satisfy "power over 100"
    assert dists('min_avg_power=100') == [12000.0]
    assert dists('min_distance_m=nonsense') == [5000.0, 12000.0, 21000.0]   # bad input ignored
    assert alice.get('/api/activities?min_distance_m=10000').get_json()['total'] == 2
    # the CSV export honours them too
    csv_rows = alice.get('/api/export/activities.csv?min_distance_m=10000').data.count(b'\n')
    assert csv_rows == 3                                          # header + 2 rows


def test_max_hr_is_sortable(alice, import_file):
    import_file(alice.user_id, SAMPLE_RUN)
    body = alice.get('/api/activities?sort=max_hr&dir=asc').get_json()
    assert body['total'] == 1


def test_excluded_activities_stay_browsable_but_leave_every_analysis(app, alice, import_file):
    """Sailboat GPS tracking is a real recording but not training: 45.6 h of it would be 15 % of
    a year's volume. Excluding keeps the activity and its samples, and drops it from analyses."""
    from app.models import Session, SessionExclusion, db
    import_file(alice.user_id, SAMPLE_POWER)
    ride = alice.get('/api/activities').get_json()['activities'][0]
    with app.app_context():
        base = Session.query.one()
        db.session.add(Session(file_id=base.file_id, user_id=alice.user_id, sport='generic',
                               sub_sport='track_me', start_time=datetime.fromisoformat('2026-08-30T08:00'),
                               timer_s=49000, distance_m=147000, avg_hr=70, load=40.0, load_model='trimp'))
        db.session.commit()

    before = alice.get('/api/trends/volume?bucket=year').get_json()
    assert 'generic' in before['sports']
    sail = [a for a in alice.get('/api/activities?hide_transitions=0').get_json()['activities']
            if a['sub_sport'] == 'track_me'][0]

    assert alice.post(f"/api/sessions/{sail['id']}/exclude", json={'reason': 'sailing'}).get_json()['excluded']

    after = alice.get('/api/trends/volume?bucket=year').get_json()
    assert 'generic' not in after['sports']                      # out of volume
    assert alice.get('/api/dashboard').get_json()['total_sessions'] == 1
    listed = alice.get('/api/activities?hide_transitions=0').get_json()
    assert [a['sub_sport'] for a in listed['activities']] == [ride['sub_sport']]   # hidden by default
    shown = alice.get('/api/activities?hide_transitions=0&show_excluded=1').get_json()
    assert any(a['sub_sport'] == 'track_me' and a['excluded'] for a in shown['activities'])
    assert alice.get(f"/api/sessions/{sail['id']}").status_code == 200             # still viewable

    assert alice.post(f"/api/sessions/{sail['id']}/exclude", json={'excluded': False}).get_json()['excluded'] is False
    assert 'generic' in alice.get('/api/trends/volume?bucket=year').get_json()['sports']


def test_exclusion_survives_a_reindex(app, alice, import_file):
    """A re-index deletes and recreates session rows, so the exclusion is keyed by
    (file, leg index) - storing it on the session itself would silently lose it."""
    from app.models import SessionExclusion
    res = import_file(alice.user_id, SAMPLE_POWER)
    sid = alice.get('/api/activities').get_json()['activities'][0]['id']
    alice.post(f'/api/sessions/{sid}/exclude', json={'excluded': True})

    alice.post(f"/api/files/{res['file_id']}/reparse")
    with app.app_context():
        assert SessionExclusion.query.count() == 1
    fresh = alice.get('/api/activities?show_excluded=1').get_json()['activities'][0]
    assert fresh['excluded']              # a recreated row, carrying the same decision
    assert alice.get('/api/activities').get_json()['total'] == 0


def test_excluded_sessions_leave_records_and_curves(app, alice, import_file):
    from app.models import Session, db
    import_file(alice.user_id, SAMPLE_POWER)
    assert alice.get('/api/trends/power-curve').get_json()['all_time']
    sid = alice.get('/api/activities').get_json()['activities'][0]['id']
    alice.post(f'/api/sessions/{sid}/exclude', json={'excluded': True})
    assert alice.get('/api/trends/power-curve').get_json()['all_time'] == []
    assert alice.get('/api/fitness').get_json()['days'] == []


def test_carried_forward_vo2max_is_flagged(app, alice, import_file):
    """All 89 of Mike's 2026 gravel rides carry a VO2 max and none recorded power; one value
    repeats across 68 consecutive rides. Such a value is not a measurement of that ride."""
    from app.models import Session, db
    import_file(alice.user_id, SAMPLE_VO2)
    with app.app_context():
        base = Session.query.one()
        assert base.vo2max and base.sport == 'running'
        assert not base.vo2max_carried              # first of its sport: a genuine value

    # importing the same activity again under a cycling sport (no power) marks it carried
    with app.app_context():
        from app.services.importer import _vo2_is_carried
        assert _vo2_is_carried(alice.user_id,
                               {'sport': 'cycling', 'vo2max': base.vo2max, 'has_power': False},
                               datetime.fromisoformat('2027-01-01T08:00'))

    body = alice.get('/api/activities').get_json()['activities'][0]
    assert body['vo2max'] and body['vo2max_carried'] is False    # exposed to the UI


def test_vo2_carried_detection_rules(app, alice, import_file):
    from app.models import Session
    from app.services.importer import _vo2_is_carried
    import_file(alice.user_id, SAMPLE_VO2)
    with app.app_context():
        prev = Session.query.one()
        at = datetime.fromisoformat('2027-01-01T08:00')
        # cycling without power is always carried, whatever the value
        assert _vo2_is_carried(alice.user_id, {'sport': 'cycling', 'vo2max': 99.0, 'has_power': False}, at)
        assert not _vo2_is_carried(alice.user_id, {'sport': 'cycling', 'vo2max': 99.0, 'has_power': True}, at)
        # a sport that cannot produce one
        assert _vo2_is_carried(alice.user_id, {'sport': 'swimming', 'vo2max': 40.0, 'has_power': False}, at)
        # running: carried only when unchanged from the previous run
        assert _vo2_is_carried(alice.user_id, {'sport': 'running', 'vo2max': prev.vo2max, 'has_power': False}, at)
        assert not _vo2_is_carried(alice.user_id, {'sport': 'running', 'vo2max': prev.vo2max + 1, 'has_power': False}, at)
        # no value at all is not "carried"
        assert not _vo2_is_carried(alice.user_id, {'sport': 'running', 'vo2max': None, 'has_power': False}, at)


def test_running_economy_is_metres_per_beat_outdoor_only_in_the_average(app, alice, import_file):
    """Crude on purpose: speed x 60 / HR from the summary. Treadmill runs are plotted but kept
    out of the rolling average; too-short, too-long and excluded runs are left out entirely."""
    from app.models import Session, db
    import_file(alice.user_id, SAMPLE_RUN)
    with app.app_context():
        base = Session.query.one()
        rows = [  # day, sub_sport, gps, minutes, speed m/s, hr, vo2, carried, excluded
            ('2026-06-01', 'generic', True, 50, 2.60, 141, 47.3, False, False),
            ('2026-06-02', 'treadmill', False, 45, 2.50, 126, None, False, False),
            ('2026-06-03', 'generic', True, 55, 2.57, 141, 43.8, False, False),
            ('2026-06-04', 'generic', True, 10, 3.00, 150, None, False, False),    # too short
            ('2026-06-05', 'generic', True, 223, 1.60, 129, 47.3, True, False),    # a race leg
            ('2026-06-06', 'generic', True, 50, 2.80, 140, None, False, True),     # excluded
        ]
        for day, sub, gps, mins, speed, hr, vo2, carried, excluded in rows:
            db.session.add(Session(file_id=base.file_id, user_id=alice.user_id, sport='running', sub_sport=sub,
                                   start_time=datetime.fromisoformat(day + 'T08:00'), timer_s=mins * 60,
                                   avg_speed=speed, avg_hr=hr, has_gps=gps, vo2max=vo2,
                                   vo2max_carried=carried, excluded=excluded))
        db.session.delete(base)
        db.session.commit()

    body = alice.get('/api/trends/economy').get_json()
    pts = body['points']
    assert [p['at'][:10] for p in pts] == ['2026-06-01', '2026-06-02', '2026-06-03']
    assert [p['m_per_beat'] for p in pts] == [round(2.60 * 60 / 141, 3), round(2.50 * 60 / 126, 3), round(2.57 * 60 / 141, 3)]
    assert [p['treadmill'] for p in pts] == [False, True, False]
    assert pts[1]['rolling'] is None                                   # treadmill: not in the average
    assert pts[2]['rolling'] == round((pts[0]['m_per_beat'] + pts[2]['m_per_beat']) / 2, 3)
    assert [p['vo2max'] for p in pts] == [47.3, None, 43.8]
    assert pts[0]['pace_s_per_km'] == round(1000 / 2.60)
