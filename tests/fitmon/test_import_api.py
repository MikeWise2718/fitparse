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
        rows = [('cycling', '2026-01-01', 47.0), ('cycling', '2026-01-02', 47.0),   # carried forward
                ('cycling', '2026-01-03', 46.0), ('walking', '2026-01-04', 46.0),   # never measured
                ('swimming', '2026-01-05', 46.0), ('running', '2026-01-06', 44.0)]
        for sport, day, value in rows:
            db.session.add(Session(file_id=base.file_id, user_id=alice.user_id, sport=sport,
                                   start_time=datetime.fromisoformat(day + 'T08:00'), vo2max=value))
        db.session.delete(base)
        db.session.commit()
        series = fitness.vo2max_series(alice.user_id, None, None, None, None)
    plotted = [(p['sport'], p['at'].date().isoformat(), p['vo2max']) for p in series]
    assert plotted == [('cycling', '2026-01-01', 47.0), ('cycling', '2026-01-03', 46.0),
                       ('running', '2026-01-06', 44.0)]
    body = alice.get('/api/trends/vo2max').get_json()
    assert len(body['points']) == 3 and body['garmin'] == []
    assert len(alice.get('/api/trends/vo2max?all=1').get_json()['points']) == 4   # incl. the repeat
