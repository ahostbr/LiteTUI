"""Owned attribution at real shell/stdio boundaries; no bridge or fleet writes."""
from contextlib import ExitStack
import json
import os
import shlex
import sys
import urllib.request

import pytest

from litetui import agent_ownership, agent_store, app as app_mod, mcp_client
from litetui.plugins import core_tools

OWNER_KEYS = (
    'LITEHARNESS_AGENT_ID', 'LITESUITE_AGENT_ID',
    'CLAUDE_CODE_SESSION_ID', 'CODEX_COMPANION_SESSION_ID',
)
LAUNCH_KEYS = ('LITETUI_SPAWN_IDENTITY', 'LITETUI_OWNER',
               'LITEHARNESS_AGENT_NAME', 'LITEHARNESS_TIER', 'LITETUI_SEAT_NAME')
AID = '11111111-1111-4111-8111-111111111111'
BID = '22222222-2222-4222-8222-222222222222'


@pytest.fixture
def sessions(tmp_path):
    with ExitStack() as stack:
        def acquire(aid=AID, name='OwnerProbe'):
            directory = tmp_path / '.agents' / name
            directory.mkdir(parents=True, exist_ok=True)
            (directory / 'settings.json').write_text(json.dumps({
                'schema_version': 1, 'name': name, 'agent_id': aid,
                'execution': {'backend': 'codex', 'model': 'fixture', 'thinking_level': 'high'},
            }), encoding='utf-8')
            return stack.enter_context(agent_ownership.AgentSession.acquire_existing(
                agent_store.AgentStore(tmp_path), agent_id=aid,
            ))
        yield acquire


def _shell_owner(run, shell, tmp_path):
    script = tmp_path / 'read_owner.py'
    script.write_text(
        'import json, os\nprint(json.dumps({k: os.environ.get(k) for k in '
        + repr(OWNER_KEYS + LAUNCH_KEYS) + '}))\n', encoding='utf-8',
    )
    if shell == 'powershell':
        command = f"& '{sys.executable}' '{script}'"
    else:
        command = f'{shlex.quote(sys.executable.replace(chr(92), "/"))} {shlex.quote(script.as_posix())}'
    return json.loads(run({'command': command, 'timeout': 15}))


def _expected(aid):
    return {key: aid if key == 'LITEHARNESS_AGENT_ID' else None
            for key in OWNER_KEYS + LAUNCH_KEYS}


@pytest.mark.parametrize('shell', ['powershell', 'bash'])
@pytest.mark.parametrize('spawned', [True, False])
def test_owned_seat_reaches_actual_tool_subprocess(sessions, monkeypatch, tmp_path, shell, spawned):
    if (core_tools.powershell_exe() if shell == 'powershell' else core_tools.bash_exe()) is None:
        pytest.skip(f'requires {shell}')
    for key in OWNER_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv('LITETUI_SPAWN_IDENTITY', raising=False)
    if spawned:
        monkeypatch.setenv('LITETUI_SPAWN_IDENTITY', '1')
        monkeypatch.setenv('LITEHARNESS_AGENT_ID', 'unrelated-launch-id')
    app = app_mod.LiteTUI(agent_session=sessions())
    assert app.seat.agent_id == AID
    assert 'LITETUI_SPAWN_IDENTITY' not in os.environ
    app.convo_id = 'not-the-owning-seat'
    assert _shell_owner(app.plugins.dispatch_for(shell), shell, tmp_path) == _expected(AID)
    assert all(key not in os.environ for key in OWNER_KEYS), 'no process-global identity publication'


@pytest.mark.skipif(core_tools.powershell_exe() is None, reason='requires PowerShell')
def test_two_apps_ignore_inherited_ids_without_global_pollution(sessions, monkeypatch, tmp_path):
    for key in OWNER_KEYS:
        monkeypatch.setenv(key, 'unrelated-parent')
    monkeypatch.delenv('LITETUI_SPAWN_IDENTITY', raising=False)
    first = app_mod.LiteTUI(agent_session=sessions())
    second = app_mod.LiteTUI(agent_session=sessions(BID, 'OtherProbe'))
    for app, aid in ((first, AID), (second, BID), (first, AID)):
        assert _shell_owner(app.plugins.dispatch_for('powershell'), 'powershell', tmp_path) == _expected(aid)
    assert all(os.environ[key] == 'unrelated-parent' for key in OWNER_KEYS)


@pytest.mark.skipif(core_tools.powershell_exe() is None, reason='requires PowerShell')
def test_unbound_tool_does_not_inherit_an_owner(monkeypatch, tmp_path):
    for key in OWNER_KEYS + LAUNCH_KEYS:
        monkeypatch.setenv(key, 'unrelated-parent')
    assert _shell_owner(core_tools.tool_powershell, 'powershell', tmp_path) == _expected(None)


@pytest.mark.skipif(core_tools.powershell_exe() is None, reason='requires PowerShell')
def test_reacquired_owned_session_keeps_tool_identity(sessions, tmp_path):
    session = sessions()
    first = app_mod.LiteTUI(agent_session=session)
    session.release()
    resumed = app_mod.LiteTUI(agent_session=sessions())
    assert first.seat.agent_id == resumed.seat.agent_id == AID
    assert _shell_owner(resumed.plugins.dispatch_for('powershell'), 'powershell', tmp_path) == _expected(AID)


@pytest.mark.parametrize('owned', [True, False])
def test_stdio_emits_bound_id_after_config_overrides(sessions, monkeypatch, tmp_path, owned):
    for key in OWNER_KEYS:
        monkeypatch.setenv(key, 'inherited-parent')
    script = tmp_path / 'stdio_owner.py'
    script.write_text('''import json, os, sys
for line in sys.stdin:
    message = json.loads(line)
    if 'id' not in message:
        continue
    if message['method'] == 'tools/call':
        result = {'content': [{'type': 'text', 'text': json.dumps(dict(os.environ))}]}
    elif message['method'] == 'tools/list':
        result = {'tools': []}
    else:
        result = {}
    print(json.dumps({'jsonrpc': '2.0', 'id': message['id'], 'result': result}), flush=True)
''', encoding='utf-8')
    if owned:
        app = app_mod.LiteTUI(agent_session=sessions())
        manager = app.mcp
        manager.root = tmp_path
    else:
        manager = mcp_client.MCPManager(tmp_path)
    server = manager._build('fixture', {
        'command': sys.executable, 'args': [str(script)],
        'env': {**{key: 'config-impostor' for key in OWNER_KEYS + LAUNCH_KEYS},
                **{key.lower(): 'case-impostor' for key in OWNER_KEYS + LAUNCH_KEYS},
                'KEEP_ME': 'yes'},
    })
    try:
        server.start()
        observed = json.loads(server.call('env', {}))
        assert {key: observed.get(key) for key in OWNER_KEYS + LAUNCH_KEYS} == _expected(AID if owned else None)
        assert observed['KEEP_ME'] == 'yes'
    finally:
        server.stop()
        manager.stop_all()


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return b'{"jsonrpc":"2.0","id":1,"result":{}}'


@pytest.mark.parametrize('owned', [True, False])
@pytest.mark.parametrize('url,name,local_suite', [
    ('http://localhost:7423/mcp', 'litesuite-tools', True),
    ('http://127.0.0.1:7423/mcp', 'litesuite-tools', True),
    ('http://[::1]:7423/mcp', 'litesuite-tools', True),
    ('https://example.com/mcp', 'litesuite-tools', False),
    ('http://localhost:7423/other', 'litesuite-tools', False),
    ('http://localhost:7423/mcp', 'other', False),
])
def test_manager_http_attribution_ignores_parent_and_config(sessions, monkeypatch, tmp_path, owned, url, name, local_suite):
    for key in OWNER_KEYS:
        monkeypatch.setenv(key, 'inherited-parent')
    seen = []
    monkeypatch.setattr(mcp_client.urllib.request, 'urlopen', lambda request, **kwargs: seen.append(request) or _Response())
    if owned:
        manager = app_mod.LiteTUI(agent_session=sessions()).mcp
        manager.root = tmp_path
    else:
        manager = mcp_client.MCPManager(tmp_path)
    server = manager._build(name, {'url': url, 'headers': {'x-LiteSuite-AGENT-id': 'config-impostor'}})
    try:
        server.call('fixture', {})
        request = seen[0]
        headers = {k.lower(): v for k, v in request.header_items()}
        assert headers.get('x-litesuite-agent-id') == (AID if owned and local_suite else None)
        redirected = urllib.request.HTTPRedirectHandler().redirect_request(
            request, None, 302, 'redirect', {}, 'https://example.com/mcp',
        )
        assert 'x-litesuite-agent-id' not in {k.lower() for k, _ in redirected.header_items()}
    finally:
        manager.stop_all()
