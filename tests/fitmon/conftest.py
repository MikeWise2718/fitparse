import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
FILES = REPO / 'tests' / 'files'
SAMPLE_SWIM = REPO / 'mike' / '11698729774_ACTIVITY.fit'         # Epix lap swim: lengths, profile, devices
SAMPLE_RUN = FILES / 'activity-small-fenix2-run.fit'             # 2.8k records with GPS
SAMPLE_VO2 = FILES / 'garmin-fenix-5-run.fit'                    # carries the private VO2 max message
SAMPLE_POWER = FILES / 'sample-activity-indoor-trainer.fit'      # power, no GPS
SAMPLE_MULTI = FILES / 'activity-large-fenxi2-multisport.fit'    # 7 sessions in one file
SAMPLE_OLD = FILES / '2013-02-06-12-11-14.fit'
PASSWORD = 'correct horse battery'


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('FITMON_HOME', str(tmp_path / 'fitmon'))
    from app import auth, create_app
    auth.throttle.reset()
    application = create_app(home=tmp_path / 'fitmon', testing=True)
    yield application


@pytest.fixture
def home(app):
    return app.config['FITMON_HOME']


class Api:
    """Test client that behaves like the SPA: keeps the CSRF token and sends it on writes."""
    def __init__(self, app):
        self.client = app.test_client()
        self.csrf = None

    def login(self, username, password=PASSWORD, remember=False):
        resp = self.client.post('/api/auth/login', json={'username': username, 'password': password,
                                                         'remember': remember})
        if resp.status_code == 200:
            self.csrf = resp.get_json()['csrf']
        return resp

    def get(self, url, **kw):
        return self.client.get(url, **kw)

    def post(self, url, json=None, **kw):
        headers = kw.pop('headers', {})
        if self.csrf:
            headers['X-CSRF-Token'] = self.csrf
        return self.client.post(url, json=json if json is not None or 'data' in kw else {}, headers=headers, **kw)


@pytest.fixture
def make_user(app):
    def _make(username, role='user'):
        from app import auth
        with app.app_context():
            return auth.create_user(username, PASSWORD, role=role).id
    return _make


@pytest.fixture
def api(app):
    return lambda: Api(app)


@pytest.fixture
def alice(app, make_user, api):
    uid = make_user('alice', role='admin')
    client = api()
    assert client.login('alice').status_code == 200
    client.user_id = uid
    return client


@pytest.fixture
def bob(app, make_user, api):
    uid = make_user('bob')
    client = api()
    assert client.login('bob').status_code == 200
    client.user_id = uid
    return client


@pytest.fixture
def import_file(app, home):
    def _import(user_id, path=SAMPLE_RUN, name=None):
        from app.services import importer
        with app.app_context():
            return importer.import_bytes(home, user_id, Path(path).read_bytes(), name or Path(path).name)
    return _import


@pytest.fixture
def drain(app):
    def _drain():
        from app.worker import work_loop
        return work_loop(app, once=True)
    return _drain
