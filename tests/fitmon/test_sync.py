import io
import zipfile

import pytest

from .conftest import SAMPLE_POWER, SAMPLE_RUN, SAMPLE_SWIM, SAMPLE_VO2


def _zipped(name, raw):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr(name, raw)
    return buf.getvalue()


class FakeGarmin:
    """Same five methods as sync.client.GarminClient."""
    def __init__(self, activities: dict, fail_on=None, error=None):
        self.activities = activities                  # id -> bytes | Exception
        self.fail_on, self.error = fail_on, error
        self.listed, self.downloaded, self.persisted = 0, [], 0

    def list_activities(self, start, limit):
        self.listed += 1
        ids = sorted(self.activities, reverse=True)[start:start + limit]
        return [{'activityId': int(i), 'activityName': f'Activity {i}', 'activityType': {'typeKey': 'running'},
                 'startTimeGMT': f'2026-01-{(int(i) % 27) + 1:02d} 07:00:00'} for i in ids]

    def download_original(self, activity_id):
        if activity_id == self.fail_on:
            raise self.error
        self.downloaded.append(activity_id)
        payload = self.activities[activity_id]
        if isinstance(payload, Exception):
            raise payload
        return payload

    def persist(self):
        self.persisted += 1


class Quiet:
    def update(self, *a, **k):
        pass


def _sync(app, home, user_id, fake, **kw):
    from app.sync import activities
    with app.app_context():
        return activities.sync_user(home, user_id, Quiet(), client=fake, delay=0, **kw)


def test_zip_bare_fit_and_no_original(app, home, alice):
    from app.models import GarminActivity, Session
    fake = FakeGarmin({'1001': _zipped('1001_ACTIVITY.fit', SAMPLE_RUN.read_bytes()),
                       '1002': SAMPLE_VO2.read_bytes(),              # some come back bare
                       '1003': _zipped('notes.txt', b'manual entry')})
    res = _sync(app, home, alice.user_id, fake)
    assert (res['imported'], res['no_original'], res['failed'], res['stopped']) == (2, 1, 0, None)
    with app.app_context():
        assert {a.activity_id: a.status for a in GarminActivity.query.all()} == \
            {'1001': 'imported', '1002': 'imported', '1003': 'no_original'}
        # the name only exists in Garmin's listing - FIT files do not carry it
        assert {s.name for s in Session.query.all()} == {'Activity 1001', 'Activity 1002'}
    assert fake.persisted == 1

    again = FakeGarmin(fake.activities)
    res = _sync(app, home, alice.user_id, again)
    assert again.downloaded == [] and res['imported'] == 0          # no_original is never retried


def test_rate_limit_stops_the_run_and_the_next_one_resumes(app, home, alice):
    from app.sync.client import SyncRateLimited
    files = {str(2000 + i): _zipped('a.fit', raw.read_bytes())
             for i, raw in enumerate([SAMPLE_RUN, SAMPLE_VO2, SAMPLE_POWER])}
    fake = FakeGarmin(files, fail_on='2001', error=SyncRateLimited('429'))
    res = _sync(app, home, alice.user_id, fake)
    assert res['stopped'] == 'rate_limited' and res['imported'] == 1   # newest first: 2002, then the 429
    assert fake.downloaded == ['2002']

    resumed = FakeGarmin(files)
    res = _sync(app, home, alice.user_id, resumed)
    assert res['stopped'] is None and sorted(resumed.downloaded) == ['2000', '2001']
    with app.app_context():
        from app.models import GarminActivity
        row = GarminActivity.query.filter_by(activity_id='2001').one()
        assert row.status == 'imported' and row.attempts == 1        # the 429 was not counted against it


def test_auth_failure_marks_login_required(app, home, alice):
    from app.models import GarminAccount, db
    from app.sync.client import SyncAuthError
    fake = FakeGarmin({'3001': b''}, fail_on='3001', error=SyncAuthError('token rejected'))
    assert _sync(app, home, alice.user_id, fake)['stopped'] == 'login_required'
    with app.app_context():
        assert db.session.get(GarminAccount, alice.user_id).status == 'login_required'


def test_other_errors_retry_up_to_three_times(app, home, alice):
    from app.models import GarminActivity
    fake = FakeGarmin({'4001': RuntimeError('boom')})
    for _ in range(5):
        _sync(app, home, alice.user_id, fake)
    with app.app_context():
        row = GarminActivity.query.one()
        assert row.status == 'failed' and row.attempts == 3 and 'boom' in row.error


def test_incremental_run_stops_paging_at_known_streak(app, home, alice, monkeypatch):
    from app.sync import activities
    monkeypatch.setattr(activities, 'PAGE_SIZE', 10)
    monkeypatch.setattr(activities, 'KNOWN_STREAK_STOP', 10)
    files = {str(5000 + i): _zipped('x.txt', b'-') for i in range(45)}     # all no_original: cheap
    first = FakeGarmin(files)
    _sync(app, home, alice.user_id, first)
    assert first.listed == 5                                               # back-fill pages everything

    files['9999'] = _zipped('new.fit', SAMPLE_RUN.read_bytes())
    second = FakeGarmin(files)
    res = _sync(app, home, alice.user_id, second)
    assert second.listed == 2 and res['imported'] == 1                     # stopped after one known page
    assert _sync(app, home, alice.user_id, FakeGarmin(files), full=True)['listed'] == 46


def test_hand_imported_archive_is_recognised(app, home, alice, import_file):
    """'<id>_ACTIVITY.fit' files imported before the sync existed must not be downloaded again."""
    import_file(alice.user_id, SAMPLE_SWIM, name='11698729774_ACTIVITY.fit')
    fake = FakeGarmin({'11698729774': _zipped('11698729774_ACTIVITY.fit', SAMPLE_SWIM.read_bytes())})
    res = _sync(app, home, alice.user_id, fake)
    assert fake.downloaded == [] and res['imported'] == 0


def test_daily_request_budget(app, home, alice):
    from app.settings import save_global_settings
    save_global_settings(home, {'garmin_daily_request_budget': 2})
    fake = FakeGarmin({str(6000 + i): _zipped('x.txt', b'-') for i in range(5)})
    res = _sync(app, home, alice.user_id, fake)
    assert res['stopped'] == 'budget' and len(fake.downloaded) == 1         # 1 listing + 1 download


def test_tokens_are_encrypted_at_rest_and_per_user(app, home, alice, bob):
    from app.sync import client as gc
    secret = '{"di_token": "abc", "di_refresh_token": "very-secret-refresh"}'
    gc.save_tokens(home, alice.user_id, secret)
    blob = gc.token_path(home, alice.user_id).read_bytes()
    assert b'very-secret-refresh' not in blob
    assert gc.load_tokens(home, alice.user_id) == secret
    assert gc.has_tokens(home, alice.user_id) and not gc.has_tokens(home, bob.user_id)
    with pytest.raises(gc.SyncAuthError):
        gc.load_tokens(home, bob.user_id)
    gc.delete_tokens(home, alice.user_id)
    assert not gc.has_tokens(home, alice.user_id)


def test_web_connect_is_off_by_default_and_admin_enabled(app, home, alice, bob, monkeypatch):
    from app.sync import client as gc
    assert bob.post('/api/sync/login', json={'email': 'b@x', 'password': 'pw'}).get_json() == {'error': 'not_enabled'}
    alice.post(f'/api/admin/users/{bob.user_id}', json={'garmin_web_connect': True})

    seen = {}

    def fake_login(h, uid, email, password):
        seen.update(uid=uid, email=email)
        raise gc.MfaRequired()

    def fake_mfa(h, uid, code):
        gc.save_tokens(h, uid, '{"di_token": "t"}')

    monkeypatch.setattr(gc, 'login_with_password', fake_login)
    monkeypatch.setattr(gc, 'complete_mfa', fake_mfa)
    assert bob.post('/api/sync/login', json={'email': 'b@x', 'password': 'pw'}).get_json() == {'status': 'needs_mfa'}
    assert bob.post('/api/sync/login/mfa', json={'code': '123456'}).get_json() == {'status': 'ok'}
    assert seen == {'uid': bob.user_id, 'email': 'b@x'}
    assert bob.get('/api/sync/status').get_json()['auth'] == 'ok'
    assert 'pw' not in (home / 'logs' / 'events.jsonl').read_text().split('"sync.connected"')[0][-400:]

    assert bob.post('/api/sync/disconnect').status_code == 200
    assert bob.get('/api/sync/status').get_json()['auth'] == 'disconnected'
    assert bob.post('/api/sync/run').status_code == 409


def test_rate_limit_in_worker_skips_other_users_syncs(app, home, alice, bob, drain, monkeypatch):
    from app.models import Job
    from app.services import jobs
    from app.sync import activities
    monkeypatch.setattr(activities, 'sync_user',
                        lambda *a, **k: {'stopped': 'rate_limited', 'imported': 0})
    with app.app_context():
        jobs.enqueue(alice.user_id, 'sync', {})
        jobs.enqueue(bob.user_id, 'sync', {})
    assert drain() == 1                                                      # bob's never ran
    with app.app_context():
        assert 'skipped' in Job.query.filter_by(user_id=bob.user_id).one().message


def test_health_extract_tolerates_missing_pieces():
    from app.sync.health import extract
    full = extract({
        'get_stats': {'restingHeartRate': 48, 'averageStressLevel': 25, 'bodyBatteryHighestValue': 95,
                      'bodyBatteryLowestValue': 20},
        'get_sleep_data': {'dailySleepDTO': {'sleepTimeSeconds': 27000, 'sleepScores': {'overall': {'value': 82}}}},
        'get_hrv_data': {'hrvSummary': {'lastNightAvg': 61, 'status': 'BALANCED'}},
        'get_training_readiness': [{'timestamp': '2026-09-19T05:00', 'score': 40},
                                   {'timestamp': '2026-09-19T07:00', 'score': 71}],
        'get_max_metrics': [{'generic': {'vo2MaxPreciseValue': 51.3}, 'cycling': None}],
    })
    assert full == {'resting_hr': 48, 'stress_avg': 25, 'bb_max': 95, 'bb_min': 20, 'sleep_s': 27000,
                    'sleep_score': 82, 'hrv_avg': 61, 'hrv_status': 'BALANCED', 'readiness': 71,
                    'vo2max_run': 51.3, 'vo2max_bike': None, 'training_status': None,
                    'load_aerobic_low': None, 'load_aerobic_high': None, 'load_anaerobic': None}
    assert set(extract({'get_hrv_data': None, 'get_stats': {'averageStressLevel': -1}}).values()) == {None}


def test_health_extract_reads_training_status_and_vo2_fallback():
    """Shapes taken from real responses: values nest under a per-device map, and
    get_max_metrics is empty on most days so get_training_status supplies VO2 max."""
    from app.sync.health import extract
    out = extract({
        'get_max_metrics': [],
        'get_training_status': {
            'mostRecentVO2Max': {'generic': {'vo2MaxPreciseValue': 45.1}, 'cycling': None},
            'mostRecentTrainingStatus': {'latestTrainingStatusData': {
                '111': {'trainingStatusFeedbackPhrase': 'RECOVERY_2', 'primaryTrainingDevice': False},
                '3459362545': {'trainingStatusFeedbackPhrase': 'PRODUCTIVE_1', 'primaryTrainingDevice': True}}},
            'mostRecentTrainingLoadBalance': {'metricsTrainingLoadBalanceDTOMap': {
                '3459362545': {'monthlyLoadAerobicLow': 901.7, 'monthlyLoadAerobicHigh': 624.6,
                               'monthlyLoadAnaerobic': 148.0, 'primaryTrainingDevice': True}}},
        }})
    assert out['training_status'] == 'Productive'          # the primary device, not the other one
    assert out['vo2max_run'] == 45.1 and out['vo2max_bike'] is None
    assert (out['load_aerobic_low'], out['load_aerobic_high'], out['load_anaerobic']) == (901.7, 624.6, 148.0)
    empty = extract({})
    assert empty['training_status'] is None and empty['load_anaerobic'] is None


def test_weigh_ins_is_one_range_request_and_converts_grams(app, home, alice):
    from datetime import date
    from app.sync.health import weigh_ins

    class FakeClient:
        def __init__(self):
            self.calls = []

        def health(self, method, *args):
            self.calls.append((method, args))
            return {'dailyWeightSummaries': [
                {'summaryDate': '2026-09-15', 'latestWeight': {'weight': 72029.0}},
                {'summaryDate': '2026-09-13', 'latestWeight': {'weight': None}},   # scale synced, no value
                {'summaryDate': 'not-a-date', 'latestWeight': {'weight': 71000.0}},
            ]}

    client = FakeClient()
    days = [date(2026, 9, 16), date(2026, 9, 15), date(2026, 9, 10)]
    with app.app_context():
        out = weigh_ins(client, home, days)
    assert out == {date(2026, 9, 15): 72.03}                       # grams -> kg, bad rows dropped
    assert client.calls == [('get_weigh_ins', ('2026-09-10', '2026-09-16'))]   # one call for the window
