"""Plugin dispatch/policy tests; process boundary is mocked, root is temporary."""
import subprocess
from types import SimpleNamespace

import pytest

from litetui.plugins import convo_search as plugin
from litetui.tool_policy import EXTERNAL_WRITE, PROCESS_EXECUTION, READ_ONLY


@pytest.fixture(autouse=True)
def root(tmp_path, monkeypatch):
    monkeypatch.setenv('LITETUI_DATA_ROOT', str(tmp_path))


@pytest.mark.parametrize('args', [{'query': 'needle'}, {'show': 'fixture'}, {'stats': True},
                                 {'raw': 'fixture', 'query': 'regex'}, {'refresh': True}])
def test_dispatch_never_adds_inline_index(args, tmp_path, monkeypatch):
    header = 'Last successful refresh UTC: 2000-01-01T00:00:00+00:00'
    calls = []
    def run(cmd, **kwargs):
        calls.append(cmd)
        assert kwargs['timeout'] == plugin.TIMEOUT
        assert '--index' not in cmd and '--reindex' not in cmd
        assert cmd[cmd.index('--root') + 1] == str(tmp_path)
        return SimpleNamespace(returncode=0, stdout=header + '\nresult', stderr='')
    monkeypatch.setattr(plugin.ttyguard, 'run', run)
    assert plugin._run(args).splitlines()[0] == header
    assert ('--refresh' in calls[0]) == bool(args.get('refresh'))


@pytest.mark.parametrize('kind', ['refusal', 'timeout', 'spawn-error'])
def test_failure_keeps_freshness_first(monkeypatch, kind):
    def run(cmd, **kwargs):
        if kind == 'timeout':
            raise subprocess.TimeoutExpired(cmd, 120)
        if kind == 'spawn-error':
            raise OSError('mock error')
        return SimpleNamespace(returncode=1, stdout=plugin.UNKNOWN + '\nRefused\nRefresh explicitly: exact-command', stderr='')
    monkeypatch.setattr(plugin.ttyguard, 'run', run)
    out = plugin._run({'query': 'needle'})
    assert out.splitlines()[0] == plugin.UNKNOWN
    assert '[error]' in out
    if kind == 'refusal':
        assert 'Refresh explicitly: exact-command' in out


def test_refresh_is_write_process_authority_not_read_only(tmp_path):
    assert plugin.POLICY.classify({'query': 'needle'}, tmp_path) == frozenset({READ_ONLY})
    assert plugin.POLICY.classify({'refresh': True}, tmp_path) == frozenset({READ_ONLY, EXTERNAL_WRITE, PROCESS_EXECUTION})


def test_mixed_refresh_refuses_without_process(monkeypatch):
    monkeypatch.setattr(plugin.ttyguard, 'run', lambda *a, **k: pytest.fail('must not launch'))
    out = plugin._run({'refresh': True, 'query': 'needle'})
    assert out.startswith(plugin.UNKNOWN)
    assert 'separate action' in out


@pytest.mark.parametrize('query', ['--index', '--reindex', '--refresh', '--build-index', '--refresh-worker'])
def test_lookup_text_cannot_become_refresh_action(monkeypatch, query):
    def run(cmd, **kwargs):
        assert cmd[-2:] == ['--', query]
        assert query not in cmd[:cmd.index('--')]
        return SimpleNamespace(returncode=0, stdout=plugin.UNKNOWN + '\nno matches', stderr='')
    monkeypatch.setattr(plugin.ttyguard, 'run', run)
    assert 'no matches' in plugin._run({'query': query})
