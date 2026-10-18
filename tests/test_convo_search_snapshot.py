"""T0503: only temporary databases and mocked process launches; no live refresh."""
import importlib.util
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def search(tmp_path, monkeypatch):
    monkeypatch.setenv('LITETUI_DATA_ROOT', str(tmp_path))
    path = Path(__file__).parents[1] / 'tools/convo_search.py'
    spec = importlib.util.spec_from_file_location('snapshot_search_fixture', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed(search):
    source = search._DATA_ROOT / '.convos' / 'fixture' / 'convo.jsonl'
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({'type': 'meta', 'created': 1}) + '\n' +
                      json.dumps({'type': 'msg', 'message': {'role': 'user', 'content': 'needle'}}) + '\n')
    search.build_index()
    return source


def lookup(search, monkeypatch, capsys, *args):
    monkeypatch.setattr(sys, 'argv', ['convo_search', *args])
    capsys.readouterr()
    code = search.main()
    return code, capsys.readouterr().out


@pytest.mark.parametrize('state', ['missing', 'legacy', 'version', 'root', 'schema', 'corrupt'])
def test_unusable_index_refuses_without_repair(search, monkeypatch, capsys, state):
    if state != 'missing':
        seed(search)
        with sqlite3.connect(search.DB_PATH) as db:
            if state == 'legacy':
                db.execute('DROP TABLE search_meta')
            elif state in ('version', 'root'):
                db.execute('UPDATE search_meta SET value=? WHERE key=?', ('wrong', state))
            elif state == 'schema':
                db.execute('DROP TABLE messages')
        if state == 'corrupt':
            Path(search.DB_PATH).write_bytes(b'not a database')
    before = Path(search.DB_PATH).read_bytes() if Path(search.DB_PATH).exists() else None
    def forbidden(*args, **kwargs):
        pytest.fail('lookup attempted refresh or catalog')
    monkeypatch.setattr(search, 'catalog_paths', forbidden)
    monkeypatch.setattr(search, 'build_index', forbidden)
    code, out = lookup(search, monkeypatch, capsys, 'needle')
    assert code == 1
    assert out.splitlines()[0] == search.FRESHNESS_UNKNOWN
    assert 'Refused:' in out
    assert out.splitlines()[-1] == 'Refresh explicitly: ' + search.refresh_command()
    if before is None:
        assert not list(search._DATA_ROOT.iterdir())
    else:
        assert Path(search.DB_PATH).read_bytes() == before


@pytest.mark.parametrize('stamp', [None, 'invalid', '2000-01-01T00:00:00', 'stale', 'fresh'])
def test_freshness_is_metadata_not_mtime_and_never_age_refusal(search, monkeypatch, capsys, stamp):
    seed(search)
    with sqlite3.connect(search.DB_PATH) as db:
        if stamp is None:
            db.execute("DELETE FROM search_meta WHERE key='refreshed_utc'")
        else:
            value = stamp
            if stamp in ('stale', 'fresh'):
                value = (datetime.now(timezone.utc) - timedelta(hours=25 if stamp == 'stale' else 1)).isoformat()
            db.execute("UPDATE search_meta SET value=? WHERE key='refreshed_utc'", (value,))
    code, out = lookup(search, monkeypatch, capsys, 'needle')
    assert code == 0 and 'needle' in out
    assert ('over 24h' in out) == (stamp == 'stale')
    assert ('UNKNOWN' in out.splitlines()[0]) == (stamp not in ('stale', 'fresh'))
    assert 'Refused' not in out


def test_snapshot_connection_rejects_writes(search, capsys):
    seed(search)
    db = search.open_snapshot()
    try:
        with pytest.raises(sqlite3.OperationalError, match='readonly'):
            db.execute('DELETE FROM messages')
    finally:
        db.close()


def test_failed_refresh_keeps_previous_rows_and_success_stamp(search, monkeypatch):
    source = seed(search)
    with sqlite3.connect(search.DB_PATH) as db:
        before = db.execute('SELECT * FROM search_meta ORDER BY key').fetchall()
    source.write_text(source.read_text().replace('needle', 'changed'))
    original = search.index_convo
    def fail_after_index(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError('injected mid-refresh failure')
    monkeypatch.setattr(search, 'index_convo', fail_after_index)
    with pytest.raises(OSError, match='injected'):
        search.build_index()
    with sqlite3.connect(search.DB_PATH) as db:
        assert db.execute('SELECT * FROM search_meta ORDER BY key').fetchall() == before
        assert db.execute('SELECT text FROM messages').fetchall() == [('needle',)]


def test_explicit_refresh_launch_is_detached_hidden_logged_no_wait(search, monkeypatch, capsys):
    calls = []
    def launch(cmd, **kwargs):
        calls.append((cmd, kwargs))
        assert kwargs['stdout'].name.endswith('.log')
        # No wait method: receipt must not wait for the supervisor/indexer.
        return SimpleNamespace(pid=123)
    monkeypatch.setattr(search.subprocess, 'Popen', launch)
    monkeypatch.setattr(search, 'build_index', lambda **kw: pytest.fail('inline build'))
    monkeypatch.setattr(search, 'catalog_paths', lambda: pytest.fail('inline catalog'))
    code, out = lookup(search, monkeypatch, capsys, '--refresh')
    assert code == 0
    assert out.startswith(search.FRESHNESS_UNKNOWN)
    assert 'not completed' in out and '123' in out and '3600s' in out
    assert 'requesting agent/operator' in out
    cmd, kwargs = calls[0]
    assert cmd[-1] == '--refresh-worker'
    assert cmd[cmd.index('--root') + 1] == str(search._DATA_ROOT.resolve())
    assert kwargs['stdin'] == subprocess.DEVNULL and kwargs['close_fds']
    if sys.platform == 'win32':
        assert kwargs['creationflags'] & subprocess.CREATE_NO_WINDOW
    else:
        assert kwargs['start_new_session']
    assert len(list((search._DATA_ROOT / 'tools' / 'convo-search-refresh').glob('*.log'))) == 1
    assert not Path(search.DB_PATH).exists()


@pytest.mark.parametrize('outcome', ['success', 'failure', 'timeout', 'spawn-error'])
def test_refresh_worker_budget_and_terminal_log(search, monkeypatch, capsys, outcome):
    def run(cmd, **kwargs):
        assert cmd[-1] == '--build-index'
        assert str(search._DATA_ROOT.resolve()) in cmd
        assert kwargs['timeout'] == 3600 and kwargs['stdin'] == subprocess.DEVNULL
        if outcome == 'timeout':
            raise subprocess.TimeoutExpired(cmd, kwargs['timeout'])
        if outcome == 'spawn-error':
            raise OSError('mock launch error')
        return SimpleNamespace(returncode=0 if outcome == 'success' else 7)
    monkeypatch.setattr(search.subprocess, 'run', run)
    from contextlib import redirect_stdout
    log = search._DATA_ROOT / 'mock-worker.log'
    with log.open('w', encoding='utf-8') as output, redirect_stdout(output):
        code = search.refresh_worker()
    out = log.read_text(encoding='utf-8')
    assert code == (0 if outcome == 'success' else 7 if outcome == 'failure' else 1)
    assert {'success': 'exit 0', 'failure': 'exit 7', 'timeout': 'timed out', 'spawn-error': 'failed to launch'}[outcome] in out
    assert not Path(search.DB_PATH).exists()


@pytest.mark.parametrize('flag', ['--index', '--reindex', '--refresh'])
def test_refresh_cannot_be_hidden_in_lookup(search, monkeypatch, capsys, flag):
    with pytest.raises(SystemExit) as exc:
        lookup(search, monkeypatch, capsys, flag, 'needle')
    assert exc.value.code == 2
    assert not list(search._DATA_ROOT.iterdir())


def test_empty_snapshot_stats(search, monkeypatch, capsys):
    search.build_index()
    code, out = lookup(search, monkeypatch, capsys, '--stats')
    assert code == 0 and 'convos: 0' in out


@pytest.mark.parametrize('flag', ['--index', '--reindex'])
def test_legacy_refresh_flags_detach_instead_of_building(search, monkeypatch, capsys, flag):
    calls = []
    monkeypatch.setattr(search, 'start_refresh', lambda force=False: calls.append(force))
    monkeypatch.setattr(search, 'build_index', lambda **kw: pytest.fail('foreground build'))
    code, _ = lookup(search, monkeypatch, capsys, flag)
    assert code == 0 and calls == [flag == '--reindex']
    assert not list(search._DATA_ROOT.iterdir())


def test_supervisor_launch_failure_has_durable_log(search, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError('mock supervisor launch error')
    monkeypatch.setattr(search.subprocess, 'Popen', fail)
    code, out = lookup(search, monkeypatch, capsys, '--refresh')
    assert code == 1 and 'Refresh failed' in out
    log, = (search._DATA_ROOT / 'tools' / 'convo-search-refresh').glob('*.log')
    assert str(log) in out
    assert 'supervisor launch failed' in log.read_text()


@pytest.mark.parametrize('query', ['--index', '--refresh', '--build-index', '--refresh-worker'])
def test_literal_query_never_launches_refresh(search, monkeypatch, capsys, query):
    seed(search)
    monkeypatch.setattr(search, 'start_refresh', lambda **kw: pytest.fail('lookup launched refresh'))
    monkeypatch.setattr(search, 'build_index', lambda **kw: pytest.fail('lookup built index'))
    monkeypatch.setattr(search, 'catalog_paths', lambda: pytest.fail('lookup catalogued fleet'))
    code, out = lookup(search, monkeypatch, capsys, '--', query)
    assert code == 0 and 'no matches' in out


def test_raw_without_index_still_reports_permanent_limitation(search, monkeypatch, capsys):
    code, out = lookup(search, monkeypatch, capsys, '--raw', 'fixture', 'needle')
    assert code == 1
    assert out.startswith(search.FRESHNESS_UNKNOWN)
    assert 'Refresh does not enable raw access' in out
    assert 'Refresh explicitly:' not in out
    assert not list(search._DATA_ROOT.iterdir())


@pytest.mark.parametrize('args', [['--help'], ['--not-a-flag']])
def test_cli_help_and_parse_error_also_start_with_freshness(search, monkeypatch, capsys, args):
    with pytest.raises(SystemExit):
        lookup(search, monkeypatch, capsys, *args)
    assert capsys.readouterr().out.splitlines()[0] == search.FRESHNESS_UNKNOWN
    assert not list(search._DATA_ROOT.iterdir())
