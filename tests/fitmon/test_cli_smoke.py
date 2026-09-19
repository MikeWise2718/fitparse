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
