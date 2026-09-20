"""Every module must import and every CLI must at least print its help.

Exists because a syntax error in app/sync/cli.py was once committed and pushed: no test
imported that module, and the commit command did not stop on the failed check before it.
"""
import compileall
import importlib
import pathlib

import pytest

APP = pathlib.Path(__file__).resolve().parents[2] / 'app'
CLIS = ['app.web', 'app.worker', 'app.cli.admin', 'app.cli.importer', 'app.sync.cli']


def test_every_module_compiles():
    assert compileall.compile_dir(str(APP), quiet=1, force=True)


@pytest.mark.parametrize('module', CLIS)
def test_cli_prints_help(module, monkeypatch, capsys):
    monkeypatch.setattr('sys.argv', [module, '--help'])
    with pytest.raises(SystemExit) as exit_info:
        importlib.import_module(module).main()
    assert exit_info.value.code == 0
    assert 'usage' in capsys.readouterr().out.lower()


@pytest.mark.parametrize('error, expected', [
    ('401 Unauthorized (Invalid Username or Password)', 'connect.garmin.com'),
    ('All login strategies rate limited (429).', 'Do not retry right away'),
])
def test_sync_login_failure_explains_what_to_do(app, make_user, monkeypatch, capsys, tmp_path, error, expected):
    from app.sync import cli, client as gc
    make_user('alice')

    def refuse(home, user_id, email, password):
        raise gc.SyncAuthError(error)

    monkeypatch.setattr(gc, 'login_with_password', refuse)
    monkeypatch.setenv('GARMIN_PASSWORD', 'not-the-real-one')
    monkeypatch.setattr(cli, 'console', cli.Console(width=200, force_terminal=False))
    args = type('Args', (), {'username': 'alice', 'email': 'someone@example.com',
                             'credentials': str(tmp_path / 'absent.env')})()
    with app.app_context():
        assert cli.cmd_login(app, args) == 1
    out = capsys.readouterr().out
    assert expected in out and 'not-the-real-one' not in out
    assert not gc.has_tokens(app.config['FITMON_HOME'], 1)


def test_credentials_file_is_data_not_shell(tmp_path):
    from app.sync.cli import read_credentials_file
    path = tmp_path / 'garmin.env'
    path.write_bytes('﻿# comment\r\nGARMIN_EMAIL = me@example.com\r\n'
                     'GARMIN_PASSWORD="p@ss $HOME `x` = with spaces"\r\nOTHER=ignored\r\n'.encode('utf-8'))
    assert read_credentials_file(str(path)) == {'GARMIN_EMAIL': 'me@example.com',
                                                'GARMIN_PASSWORD': 'p@ss $HOME `x` = with spaces'}
    assert read_credentials_file(str(tmp_path / 'absent.env')) == {}


def test_unfilled_template_falls_back_to_the_prompt(tmp_path):
    from app.sync.cli import read_credentials_file
    path = tmp_path / 'garmin.env'
    path.write_text('GARMIN_EMAIL=me@example.com\nGARMIN_PASSWORD=PUT-YOUR-GARMIN-PASSWORD-HERE\n', encoding='utf-8')
    assert read_credentials_file(str(path)) == {'GARMIN_EMAIL': 'me@example.com'}


def test_login_uses_the_credentials_file_and_says_to_delete_it(app, make_user, monkeypatch, capsys, tmp_path):
    from app.sync import cli, client as gc
    make_user('alice')
    seen = {}

    def accept(home, user_id, email, password):
        seen.update(email=email, password=password)
        gc.save_tokens(home, user_id, '{"di_token": "t"}')

    monkeypatch.setattr(gc, 'login_with_password', accept)
    monkeypatch.delenv('GARMIN_EMAIL', raising=False)
    monkeypatch.delenv('GARMIN_PASSWORD', raising=False)
    monkeypatch.setattr(cli, 'console', cli.Console(width=200, force_terminal=False))
    path = tmp_path / 'garmin.env'
    path.write_text('GARMIN_EMAIL=me@example.com\nGARMIN_PASSWORD=s3cret-value\n', encoding='utf-8')
    args = type('Args', (), {'username': 'alice', 'email': None, 'credentials': str(path)})()
    with app.app_context():
        assert cli.cmd_login(app, args) == 0
    out = capsys.readouterr().out
    assert seen == {'email': 'me@example.com', 'password': 's3cret-value'}
    assert 's3cret-value' not in out and 'Delete' in out and 'GARMIN_PASSWORD' in out   # names the key, never the value
