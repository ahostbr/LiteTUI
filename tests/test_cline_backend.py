import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import pytest
from openai import AsyncOpenAI

from litetui import cline_backend, llm_backend
from litetui.cline_backend import CLINE_MODELS, ClineBackend
from litetui.llm_backend import BackendError, make_backend
from litetui.settings import Settings


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Every test gets its own Cline store; the real ~/.cline is never read."""
    path = tmp_path / 'providers.json'
    monkeypatch.setenv('CLINE_PROVIDER_SETTINGS_PATH', str(path))
    monkeypatch.delenv('CLINE_API_KEY', raising=False)
    return path


def _login(path, expires_in_s, access='workos:jwt-old'):
    path.write_text(json.dumps({'version': 1, 'providers': {
        'lmstudio': {'settings': {'provider': 'lmstudio'}, 'updatedAt': 'x', 'tokenSource': 'manual'},
        'cline': {'settings': {'provider': 'cline', 'auth': {
            'accessToken': access, 'refreshToken': 'r-old', 'accountId': 'acct',
            'expiresAt': int((time.time() + expires_in_s) * 1000)}},
            'updatedAt': 'x', 'tokenSource': 'oauth'}}}))


def _backend():
    return make_backend(Settings(backend='cline'))


def _serve(handler_body):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            seen.append((self.path, self.headers.get('Authorization'), body))
            status, ctype, payload = handler_body(body)
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server, f'http://127.0.0.1:{server.server_port}', seen


def test_registered_and_built_from_the_one_list():
    assert 'cline' in llm_backend.BACKEND_NAMES
    backend = _backend()
    assert isinstance(backend, ClineBackend) and backend.name == 'cline'
    assert backend.base_url() == 'https://api.cline.bot/api/v1'


def test_oauth_token_is_read_at_call_time_and_sent_workos_prefixed(store):
    backend = _backend()
    with pytest.raises(BackendError, match='cline auth'):
        backend.api_key()
    _login(store, 3600, access='jwt-bare')
    assert backend.api_key() == 'workos:jwt-bare'


def test_api_key_env_is_the_fallback_and_oauth_wins(store, monkeypatch):
    monkeypatch.setenv('CLINE_API_KEY', 'k-test')
    assert _backend().api_key() == 'k-test'
    _login(store, 3600)
    assert _backend().api_key() == 'workos:jwt-old'


def test_near_expiry_refreshes_and_writes_back_preserving_the_store(store, monkeypatch):
    _login(store, 60)  # inside the 5-minute buffer
    server, url, seen = _serve(lambda body: (200, 'application/json', json.dumps({'success': True, 'data': {
        'accessToken': 'jwt-new', 'refreshToken': 'r-new', 'expiresAt': '2099-01-01T00:00:00Z',
        'tokenType': 'Bearer', 'userInfo': {}}})))
    monkeypatch.setattr(cline_backend, 'CLINE_API', url)
    try:
        assert _backend().api_key() == 'workos:jwt-new'
    finally:
        server.shutdown()
        server.server_close()
    path, auth, body = seen[0]
    assert path == '/api/v1/auth/refresh' and auth is None
    assert body == {'refreshToken': 'r-old', 'grantType': 'refresh_token'}
    saved = json.loads(store.read_text())
    cline = saved['providers']['cline']
    assert cline['settings']['auth']['accessToken'] == 'workos:jwt-new'
    assert cline['settings']['auth']['refreshToken'] == 'r-new'
    assert cline['settings']['auth']['expiresAt'] == 4070908800000
    assert cline['settings']['auth']['accountId'] == 'acct' and cline['tokenSource'] == 'oauth'
    assert saved['providers']['lmstudio'] == {'settings': {'provider': 'lmstudio'}, 'updatedAt': 'x', 'tokenSource': 'manual'}
    assert not list(store.parent.glob('*.tmp'))


@pytest.mark.parametrize('status, expires_in_s, outcome', [
    (401, 120, 'cline auth'),         # rejected refresh token: log in again
    (500, 120, 'workos:jwt-old'),     # transient, >30s left: keep the current token
    (500, 10, 'try again'),           # transient, about to expire: fail, keep the store
])
def test_refresh_failures_follow_the_sdk(store, monkeypatch, status, expires_in_s, outcome):
    _login(store, expires_in_s)
    before = store.read_text()
    server, url, _ = _serve(lambda body: (status, 'application/json', '{"success": false}'))
    monkeypatch.setattr(cline_backend, 'CLINE_API', url)
    try:
        if outcome.startswith('workos:'):
            assert _backend().api_key() == outcome
        else:
            with pytest.raises(BackendError, match=outcome):
                _backend().api_key()
    finally:
        server.shutdown()
        server.server_close()
    assert store.read_text() == before


def test_refresh_waits_for_the_sdk_lock_and_reuses_what_the_holder_wrote(store, monkeypatch):
    """Another Cline process holds the SDK's SQLite lock while it rotates the
    token; we wait, re-read under the lock and never call refresh ourselves."""
    import hashlib
    import sqlite3
    import threading

    monkeypatch.setattr(cline_backend, 'CLINE_API', 'http://127.0.0.1:9')  # a refresh would fail
    _login(store, 60)
    lock = f"{store.resolve()}.oauth-{hashlib.sha256(b'cline').hexdigest()}.lock"
    held = threading.Event()

    def holder():
        db = sqlite3.connect(lock, timeout=0, isolation_level=None)
        db.execute('BEGIN EXCLUSIVE')
        held.set()
        time.sleep(0.3)
        _login(store, 3600, access='workos:jwt-from-holder')
        db.execute('ROLLBACK')
        db.close()

    t = threading.Thread(target=holder)
    t.start()
    held.wait()
    assert _backend().api_key() == 'workos:jwt-from-holder'
    t.join()


@pytest.mark.asyncio
async def test_model_list_is_the_catalog_and_needs_no_network(store):
    _login(store, 3600)
    backend = _backend()
    assert await backend.ensure_running() == 'ok'
    assert [r.key for r in await backend.list_models()] == list(CLINE_MODELS)
    await backend.ensure_chat_ready('cline-pass/glm-5.3-flash')
    with pytest.raises(BackendError):
        await backend.ensure_chat_ready('not-a-cline-model')
    assert await backend.model_info('cline-pass/glm-5.3-flash') == (1310720, 'llm', True)
    assert backend.reasoning_levels('cline-pass/glm-5.3-flash') == ['none', 'low', 'high', 'max']
    assert backend.reasoning_levels('cline-pass/minimax-m3') == ['none']


def test_backend_picker_marks_readiness(store, monkeypatch):
    from litetui import gpu_gate
    from litetui.plugins import model_switch

    monkeypatch.setattr(gpu_gate, 'is_rtx_5090', lambda: False)
    seen = {}
    monkeypatch.setattr(model_switch, 'pick', lambda app, title, rows, cb, current=None: seen.update(rows=dict(rows)))
    app = SimpleNamespace(settings=Settings(), backend=SimpleNamespace(name='lmstudio'))
    marks = []
    for step in ('none', 'key', 'oauth'):
        if step == 'key':
            monkeypatch.setenv('CLINE_API_KEY', 'k-test')
        if step == 'oauth':
            _login(store, 3600)
        model_switch._cmd_backend(app, '/backend', '')
        marks.append(seen['rows']['cline'].split('· ')[1])
    assert marks == ['run: cline auth', 'key set', 'OAuth signed in']


@pytest.mark.asyncio
async def test_streamed_turn_sends_the_bearer_from_the_provider_and_the_cline_pass_id(store, monkeypatch):
    """The client app.py builds (base_url + api_key_provider) streams a chat
    turn; the provider is awaited per request, so the header carries the token."""
    _login(store, 3600)
    chunks = [{'id': 'c', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm',
               'choices': [{'index': 0, 'delta': {'content': t}, 'finish_reason': None}]} for t in ('hel', 'lo')]
    sse = ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks) + 'data: [DONE]\n\n'
    server, url, seen = _serve(lambda body: (200, 'text/event-stream', sse))
    backend = _backend()
    monkeypatch.setattr(backend, '_base', url + '/api/v1')
    try:
        client = AsyncOpenAI(base_url=backend.base_url(), api_key=backend.api_key_provider)
        stream = await client.chat.completions.create(
            model='cline-pass/glm-5.3-flash', messages=[{'role': 'user', 'content': 'hi'}], stream=True)
        text = ''.join([c.choices[0].delta.content or '' async for c in stream])
    finally:
        server.shutdown()
        server.server_close()
    assert text == 'hello'
    path, auth, body = seen[0]
    assert path == '/api/v1/chat/completions'
    assert auth == 'Bearer workos:jwt-old'
    assert body['model'] == 'cline-pass/glm-5.3-flash'
