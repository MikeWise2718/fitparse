"""The real guarantee of multi-tenancy: every route that takes an id is tried as the wrong user.

The route list is discovered from the URL map, so a new id-bearing route is covered the day it
is added - and if it cannot be filled in from IDS below, the test fails until someone decides
what it should be tested with.
"""
import pytest

from .conftest import SAMPLE_POWER

# Admin-only routes are covered by test_auth (404 for non-admins); these act on accounts, not data.
SKIP = {'admin.update_user', 'admin.revoke_invite', 'auth.invite_info', 'auth.invite_accept',
        'pages.invite_page', 'static'}


@pytest.fixture
def alices_data(app, alice, import_file):
    from app.models import DeviceToken, GarminActivity, Job, Session, db
    from app.services import jobs
    res = import_file(alice.user_id, SAMPLE_POWER)
    with app.app_context():
        session = Session.query.filter_by(file_id=res['file_id']).first()
        job = jobs.enqueue(alice.user_id, 'reindex', {})
        db.session.add(GarminActivity(user_id=alice.user_id, activity_id='777', status='failed'))
        db.session.commit()
        alice.login('alice', remember=True)
        return {'file_id': res['file_id'], 'session_id': session.id, 'job_id': job.id,
                'token_id': DeviceToken.query.filter_by(user_id=alice.user_id).first().id,
                'activity_id': '777', 'name': 'record', 'fmt': 'json'}


def _routes(app):
    for rule in app.url_map.iter_rules():
        if rule.arguments and rule.endpoint not in SKIP:
            yield rule


def test_other_users_ids_are_404(app, alice, bob, alices_data):
    calls = []
    for rule in _routes(app):
        url = rule.rule
        for arg in rule.arguments:
            assert arg in alices_data, f'add a test id for <{arg}> in {rule.rule}'
            url = url.replace(f'<int:{arg}>', str(alices_data[arg])).replace(f'<{arg}>', str(alices_data[arg]))
        calls += [(method, url) for method in rule.methods - {'HEAD', 'OPTIONS'}]
    assert len(calls) >= 12

    # The intruder goes first, against every route, while all of the owner's data still exists.
    for method, url in calls:
        theirs = (bob.get if method == 'GET' else bob.post)(url)
        assert theirs.status_code == 404, (method, url, theirs.status_code)

    # Then the owner: a 404 above must mean "not yours", not "route is broken".
    # Destructive calls last, so they don't take the data away from the others.
    for method, url in sorted(calls, key=lambda c: ('delete' in c[1], 'revoke' in c[1])):
        mine = (alice.get if method == 'GET' else alice.post)(url)
        assert mine.status_code != 404, (method, url, 'owner should reach their own data')


def test_lists_only_show_own_rows(alice, bob, alices_data):
    for url, key in [('/api/activities', 'activities'), ('/api/explorer/files', 'files'),
                     ('/api/jobs', 'jobs'), ('/api/sports', 'sports'), ('/api/gear', 'devices'),
                     ('/api/sync/activities', 'activities'), ('/api/import/problems', 'files')]:
        assert bob.get(url).get_json()[key] == [], url
    assert alice.get('/api/activities').get_json()['total'] == 1
    assert bob.get('/api/dashboard').get_json()['total_sessions'] == 0
    assert bob.get('/api/trends/power-curve').get_json()['all_time'] == []
    assert alice.get('/api/trends/power-curve').get_json()['all_time']


def test_same_file_can_belong_to_two_users(app, alice, bob, import_file):
    a = import_file(alice.user_id, SAMPLE_POWER)
    b = import_file(bob.user_id, SAMPLE_POWER)
    assert a['status'] == b['status'] == 'imported' and a['file_id'] != b['file_id']
    assert import_file(bob.user_id, SAMPLE_POWER)['status'] == 'duplicate'


def test_deleting_an_account_removes_only_that_users_data(app, home, alice, bob, import_file):
    from app.models import FitFile, Record, Session
    from .conftest import PASSWORD
    import_file(alice.user_id, SAMPLE_POWER)
    import_file(bob.user_id, SAMPLE_POWER)
    assert bob.post('/api/account/delete', json={'password': 'wrong'}).status_code == 401
    assert bob.post('/api/account/delete', json={'password': PASSWORD}).status_code == 200
    with app.app_context():
        assert FitFile.query.filter_by(user_id=bob.user_id).count() == 0
        assert Session.query.filter_by(user_id=bob.user_id).count() == 0
        assert FitFile.query.filter_by(user_id=alice.user_id).count() == 1
        assert Record.query.count() > 0                      # alice's samples survive
    assert not (home / 'users' / str(bob.user_id)).exists()
    assert (home / 'users' / str(alice.user_id) / 'fit').exists()
    assert bob.get('/api/auth/me').status_code == 401
