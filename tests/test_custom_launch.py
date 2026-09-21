import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from litetui.custom_backend import CustomBackend, api_base
from litetui.launch_options import LaunchOptions, prepare
from litetui.llm_backend import BackendError, make_backend
from litetui.settings import Settings


@pytest.fixture
def endpoint():
    requests = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get('Authorization')))
            body = json.dumps({'data': [{'id': 'fixture'}]}).encode()
            self.send_response(200 if self.path == '/v1/models' else 404)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}', requests
    server.shutdown()
    server.server_close()
    thread.join()


@pytest.mark.asyncio
async def test_custom_discovery_auth_and_strict_model(endpoint, monkeypatch):
    url, requests = endpoint
    monkeypatch.setenv('TEST_CUSTOM_KEY', 'fixture-secret')
    backend = make_backend(Settings(backend='custom', custom_base_url=url,
                                   custom_api_key_env='TEST_CUSTOM_KEY', custom_context_length=8192))
    assert isinstance(backend, CustomBackend)
    assert await backend.ensure_running() == 'ok'
    assert (await backend.list_models())[0].key == 'fixture'
    assert await backend.model_info('fixture') == (8192, 'llm', True)
    with pytest.raises(BackendError, match='no model fallback'):
        await backend.ensure_chat_ready('wrong')
    assert requests and all(item == ('/v1/models', 'Bearer fixture-secret') for item in requests)
    with pytest.raises(BackendError, match='no standard load'):
        await backend.load('fixture', ctx=4096)


@pytest.mark.parametrize('url', ['ftp://localhost', 'http://user:secret@localhost', 'http://localhost/?key=x'])
def test_invalid_url(url):
    with pytest.raises(ValueError):
        api_base(url)


@pytest.mark.parametrize('backend,options,message', [
    ('codex', LaunchOptions(context_length=8192), 'Codex manages'),
    ('codex', LaunchOptions(base_url='http://localhost:1'), 'Codex manages'),
    ('ninfer', LaunchOptions(context_length=8192), 'fixed at startup'),
    ('llamacpp', LaunchOptions(context_length=8192), 'requires --load-model'),
    ('custom', LaunchOptions(load_model=True), 'supported by'),
    ('custom', LaunchOptions(server_mode='start', base_url='https://remote.example/v1', server_command=['server']), 'loopback'),
    ('custom', LaunchOptions(server_command=['server']), 'requires --backend'),
    ('lmstudio', LaunchOptions(max_tokens=-1), 'positive'),
])
def test_unsupported_options_fail_before_start(backend, options, message):
    with pytest.raises(ValueError, match=message):
        options.overrides(Settings(), backend, 'fixture')


@pytest.mark.parametrize('backend,field', [('custom', 'custom_base_url'), ('ninfer', 'ninfer_host'), ('llamacpp', 'llama_host'), ('lmstudio', 'lm_host')])
def test_endpoint_override(backend, field):
    values = LaunchOptions(base_url='http://127.0.0.1:1235/v1').overrides(Settings(), backend, 'fixture')
    assert values[field] == 'http://127.0.0.1:1235' + ('/v1' if backend == 'custom' else '')
    if backend == 'llamacpp':
        assert values['llama_attach_hosts'] == []


@pytest.mark.asyncio
@pytest.mark.parametrize('backend', ['llamacpp', 'lmstudio'])
async def test_load_precedes_ready(backend):
    calls = []
    async def ensure():
        calls.append('connect')
        return 'ok'
    async def load(key, *, ctx):
        calls.append(('load', key, ctx))
    async def ready(key):
        calls.append(('ready', key))
    app = SimpleNamespace(backend=SimpleNamespace(name=backend, ensure_running=ensure, load=load, ensure_chat_ready=ready),
                          _cli_initial_model='fixture', _launch_options=LaunchOptions(load_model=True, context_length=8192))
    assert await prepare(app) == 'ok'
    assert calls == ['connect', ('load', 'fixture', 8192), ('ready', 'fixture')]


@pytest.mark.asyncio
async def test_explicit_dead_llama_endpoint_never_spawns(monkeypatch):
    monkeypatch.setattr('litetui.llm_backend._healthy', lambda host: False)
    engine = SimpleNamespace(name='llamacpp', host=lambda: 'http://127.0.0.1:1', ensure_running=AsyncMock())
    app = SimpleNamespace(backend=engine, _launch_options=LaunchOptions(base_url=engine.host()))
    with pytest.raises(BackendError, match='no fallback'):
        await prepare(app)
    engine.ensure_running.assert_not_called()


@pytest.mark.asyncio
async def test_cli_waits_for_actual_connection():
    from litetui.app import LiteTUI
    from litetui.llm_backend import ModelRow
    event = asyncio.Event()
    app = SimpleNamespace(_launch_options=LaunchOptions(timeout=1), _connect_done=event,
        _connect_settled=True, _gui_connection_success=True, available_models=['fixture'],
        model_rows={'fixture': ModelRow('fixture', None, 'server', True)},
        _cli_initial_model='fixture', _cli_system_prompt=None, _first_prompt=None,
        _update_header=lambda: None, _fetch_ctx_window=lambda: None)
    task = asyncio.create_task(LiteTUI._apply_cli_args.__wrapped__(app))
    await asyncio.sleep(0)
    assert not task.done()
    event.set()
    await task
    assert app._model_id == 'fixture'
    assert app._cli_launch_error is None


@pytest.mark.asyncio
async def test_ninfer_start_is_explicit():
    engine = SimpleNamespace(name='ninfer', start_engine=AsyncMock(return_value='started'), ensure_running=AsyncMock(return_value='ok'))
    app = SimpleNamespace(backend=engine, _system=lambda text: None, _launch_options=LaunchOptions(server_mode='connect'))
    await prepare(app)
    engine.start_engine.assert_not_called()
    app._launch_prepared = False
    app._launch_options.server_mode = 'start'
    await prepare(app)
    engine.start_engine.assert_awaited_once()
    assert app._explicit_launch_load is False


@pytest.mark.asyncio
async def test_lmstudio_server_start_uses_requested_port(monkeypatch):
    from litetui import ttyguard
    seen = []
    monkeypatch.setattr('litetui.launch_options.shutil.which', lambda name: 'lms.exe')
    monkeypatch.setattr(ttyguard, 'run', lambda argv, **kw: (seen.append(argv) or SimpleNamespace(returncode=0)))
    engine = SimpleNamespace(name='lmstudio', host=lambda: 'http://127.0.0.1:7777', ensure_running=AsyncMock(return_value='ok'))
    app = SimpleNamespace(backend=engine, _launch_options=LaunchOptions(server_mode='start'))
    await prepare(app)
    assert seen == [['lms.exe', 'server', 'start', '--port', '7777']]


@pytest.mark.asyncio
async def test_lmstudio_thinking_is_probed_for_selected_model(monkeypatch):
    from litetui.app import LiteTUI
    from litetui.llm_backend import ModelRow
    seen = []
    def probe(host, model, seed):
        seen.append(model)
        return ['off', 'low', 'medium', 'xhigh']
    monkeypatch.setattr('litetui.app.thinking_probe.get_effective_levels', probe)
    class App(SimpleNamespace):
        @property
        def model_id(self):
            return self._model_id
    app = App(_model_id='old', backend=SimpleNamespace(name='lmstudio'),
        settings=Settings(lmstudio_graded_thinking_models=['fixture']),
        _cli_thinking_level='medium', _cli_initial_model='fixture',
        _cli_system_prompt=None, _first_prompt=None, available_models=['fixture'],
        model_rows={'fixture': ModelRow('fixture', None, 'server', True)},
        _update_header=lambda: None, _fetch_ctx_window=lambda: None)
    await LiteTUI._apply_cli_args.__wrapped__(app)
    assert seen == ['fixture']
    assert app._cli_effective_thinking == 'medium'
    assert app._cli_launch_error is None


def test_cli_output_limit_beats_saved_model_override():
    from litetui.app import LiteTUI
    app = SimpleNamespace(model_id='fixture', _launch_options=LaunchOptions(max_tokens=32),
        backend=SimpleNamespace(request_overrides=lambda key: {'max_tokens': 999}))
    assert LiteTUI._effective_request_overrides(app)['max_tokens'] == 32


def test_custom_reconnect_keeps_invocation_until_explicit_edit(tmp_path):
    from dataclasses import replace

    from litetui.settings_runtime import prepare_reconnect
    saved = Settings(backend='custom', custom_base_url='http://localhost:9999/v1')
    active = replace(saved, custom_base_url='http://localhost:1235/v1')
    app = SimpleNamespace(settings=active, backend=CustomBackend(active), convo_dir=tmp_path,
        _invocation_saved_values={'custom_base_url': saved.custom_base_url},
        _settings_service=SimpleNamespace(snapshot=lambda name: SimpleNamespace(effective=saved)))
    prepare_reconnect(app)
    assert app.backend.base_url() == 'http://localhost:1235/v1'
    app._invocation_saved_values.clear()
    prepare_reconnect(app)
    assert app.backend.base_url() == 'http://localhost:9999/v1'


@pytest.mark.asyncio
async def test_real_custom_server_start_chat_and_cleanup(tmp_path, monkeypatch):
    import socket
    import sys

    from litetui.agent_launcher import start_headless_child, validate_request
    from litetui.agent_supervisor import AgentProcess

    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    script = tmp_path / 'server with spaces.py'
    script.write_text('''import json, sys
from http.server import BaseHTTPRequestHandler, HTTPServer
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'{"data":[{"id":"fixture"}]}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_POST(self):
        request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        assert request['model'] == 'fixture'
        assert request['reasoning_effort'] == 'none'
        assert request['max_tokens'] == 32
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.end_headers()
        chunk = {'id':'test','object':'chat.completion.chunk','created':1,'model':'fixture',
                 'choices':[{'index':0,'delta':{'content':'fixture ok'},'finish_reason':None}]}
        self.wfile.write(('data: '+json.dumps(chunk)+'\\n\\ndata: [DONE]\\n\\n').encode())
        self.wfile.flush()
    def log_message(self, *args): pass
HTTPServer(('127.0.0.1', int(sys.argv[1])), Handler).serve_forever()
''', encoding='utf-8')
    root = tmp_path / 'data'
    root.mkdir()
    (root / 'settings.json').write_text(json.dumps({'tools_enabled': False, 'mcp_enabled': False,
        'skills_enabled': False, 'default_model': None, 'pin_default_model': False}))
    from pathlib import Path
    monkeypatch.setenv('PYTHONPATH', str(Path(__file__).resolve().parents[1] / 'src'))
    spec = validate_request({'prompt': 'test', 'backend': 'custom', 'model': 'fixture',
        'workspace': str(tmp_path), 'thinking_level': 'off', 'launch': {
            'base_url': f'http://127.0.0.1:{port}/v1', 'server_mode': 'start',
            'server_command': [sys.executable, str(script), '{port}'],
            'context_length': 4096, 'max_tokens': 32, 'timeout': 30}},
        parent_profile='autonomous', depth=0)
    process = AgentProcess()
    try:
        ready = await start_headless_child(spec, process, workspace=tmp_path,
                                          data_root=root, supported_levels=['off'])
        assert ready['backend'] == 'custom'
        assert ready['context_length'] == 4096
        result = await process.collect_turn(timeout=15)
        assert result['summary'] == 'fixture ok', (result, list(process.diagnostics))
        process.process.stdin.write(b'{"type":"shutdown"}\n')
        await process.process.stdin.drain()
        await asyncio.wait_for(process.process.wait(), 20)
        with socket.socket() as sock:
            assert sock.connect_ex(('127.0.0.1', port)) != 0
    finally:
        await process.close()
