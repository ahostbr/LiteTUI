import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

import httpx
import pytest
from openai import AsyncOpenAI

from litetui import cline_backend, llm_backend
from litetui.cline_backend import CLINE_MODELS, ClineBackend
from litetui.llm_backend import BackendError, make_backend
from litetui.settings import Settings

REAL_FETCH_FEED = cline_backend._fetch_feed
FEED = {'recommended': [{'id': 'anthropic/claude-opus-5'}],
        'clinePass': [{'id': 'cline-pass/glm-5.3-flash'}, {'id': 'cline-pass/new-model'}],
        'free': [{'id': 'cline-free/gemini-3.8-flash'}, {'id': 'stealth/space-bunny-alpha'}],
        'clineCloud': [{'id': 'cloud/x'}]}


def _unreachable():
    raise httpx.ConnectError('offline')


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    """Every test gets its own Cline store; the real ~/.cline is never read, and
    the model feed is offline unless a test provides one."""
    path = tmp_path / 'providers.json'
    monkeypatch.setenv('CLINE_PROVIDER_SETTINGS_PATH', str(path))
    monkeypatch.delenv('CLINE_API_KEY', raising=False)
    monkeypatch.setattr(cline_backend, '_feed_cache', None)
    monkeypatch.setattr(cline_backend, '_fetch_feed', _unreachable)
    monkeypatch.setattr(cline_backend, '_free_models_cache', None)
    monkeypatch.setattr(cline_backend, '_fetch_model_ids', _unreachable)
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


def test_settings_controls_mark_what_applies_to_clinepass():
    """The TUI settings screen (and the sidecar, from the same call) disables
    what does not apply: ClinePass is a fixed remote endpoint driven by OUR loop."""
    from litetui import codex_settings

    backend = _backend()
    for name in ('custom_base_url', 'lm_host', 'default_context_length', 'llama_host',
                 'ninfer_host', 'codex_native_engine', 'claude_executable'):
        assert codex_settings.control(backend, name).owner == 'unsupported', name
    assert codex_settings.control(backend, 'thinking_level').owner == 'native'
    for name in ('temperature', 'autocompact_enabled', 'compact_keep_recent', 'tools_enabled', 'max_tokens_chat'):
        assert codex_settings.control(backend, name) is None, name



def test_clinepass_backend_saved_on_one_conversation_stays_there(tmp_path):
    """Ryan: separate LiteTUI instances must stay separate. Choosing ClinePass
    in one conversation's settings leaves another conversation's backend alone."""
    from litetui.settings_scope import SETTING_SPECS
    from litetui.settings_service import SettingChange, SettingsService

    svc = SettingsService(tmp_path)
    a, b = svc.snapshot('a'), svc.snapshot('b')
    scope = SETTING_SPECS['backend'].scope.value
    assert svc.save_patch('a', [SettingChange('backend', 'cline', scope)], a.revisions).fully_saved
    assert svc.snapshot('a').saved.backend == 'cline'
    assert svc.snapshot('b').saved.backend == b.saved.backend


@pytest.mark.asyncio
async def test_the_live_feed_is_the_list_its_clinepass_bucket_only(monkeypatch):
    """The "free" bucket is refused outside Cline's own apps (HTTP 403, measured
    2026-09-24), and "recommended" ids are usage-billing: neither is offered."""
    monkeypatch.setattr(cline_backend, '_fetch_feed', lambda: FEED)
    backend = _backend()
    assert [r.key for r in await backend.list_models()] == ['cline-pass/glm-5.3-flash', 'cline-pass/new-model']
    for other in ('cline-free/gemini-3.8-flash', 'anthropic/claude-opus-5'):
        with pytest.raises(BackendError):
            await backend.ensure_chat_ready(other)
    assert await backend.model_info('cline-pass/glm-5.3-flash') == (1310720, 'llm', True)
    assert await backend.model_info('cline-pass/new-model') == (None, 'llm', True)


def test_an_unreachable_feed_falls_back_and_is_retried_next_time(monkeypatch):
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError('offline')
        return FEED

    monkeypatch.setattr(cline_backend, '_fetch_feed', flaky)
    assert cline_backend.clinepass_ids() == list(CLINE_MODELS)
    assert 'cline-pass/new-model' in cline_backend.clinepass_ids()
    assert 'cline-pass/new-model' in cline_backend.clinepass_ids()
    assert len(calls) == 2  # a failure is not cached; a success is


def test_the_feed_is_fetched_keyless_from_cline(monkeypatch):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get('Authorization')))
            body = json.dumps(FEED).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(cline_backend, 'CLINE_API', f'http://127.0.0.1:{server.server_port}')
    try:
        assert REAL_FETCH_FEED() == FEED
    finally:
        server.shutdown()
        server.server_close()
    assert seen == [('/api/v1/ai/cline/recommended-models', None)]


# -- the Free tier: source 1 is Cline, restricted to its $0 models ---------------

SERVED = ['anthropic/claude-opus-5', 'qwen/qwen3.8-27b:free', 'openai/gpt-6-astra', 'openrouter/free:free']


def _free_backend(monkeypatch):
    monkeypatch.setattr(cline_backend, '_fetch_feed', lambda: FEED)
    monkeypatch.setattr(cline_backend, '_fetch_model_ids', lambda: SERVED)
    return make_backend(Settings(backend='free'))


@pytest.mark.asyncio
async def test_the_free_tier_lists_only_free_models(monkeypatch):
    backend = _free_backend(monkeypatch)
    assert 'free' in llm_backend.BACKEND_NAMES and backend.name == 'free'
    assert [r.key for r in await backend.list_models()] == ['qwen/qwen3.8-27b:free', 'openrouter/free:free']
    for paid in ('anthropic/claude-opus-5', 'openai/gpt-6-astra', 'cline-pass/glm-5.3-flash',
                 'cline-free/gemini-3.8-flash', 'stealth/space-bunny-alpha'):
        with pytest.raises(BackendError):
            await backend.ensure_chat_ready(paid)


@pytest.mark.asyncio
async def test_a_paid_id_never_leaves_the_process_on_the_free_tier(store, monkeypatch):
    """The guard sits on the HTTP client app.py builds, so it also covers calls
    that skip ensure_chat_ready (subagent_model, tool_summary_model, a stale
    default_model from another backend)."""
    from litetui.app import _plain_backend_error

    _login(store, 3600)
    reached = []
    sse = 'data: ' + json.dumps({'id': 'c', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm',
                                 'choices': [{'index': 0, 'delta': {'content': 'ok'}, 'finish_reason': None}]}) + '\n\ndata: [DONE]\n\n'
    server, url, _ = _serve(lambda body: (reached.append(body['model']) or 200, 'text/event-stream', sse))
    backend = _free_backend(monkeypatch)
    monkeypatch.setattr(backend, '_base', url + '/api/v1')
    client = AsyncOpenAI(base_url=backend.base_url(), api_key=backend.api_key_provider,
                         http_client=backend.http_client(), max_retries=0)
    try:
        with pytest.raises(Exception) as refused:
            await client.chat.completions.create(model='anthropic/claude-opus-5', stream=True,
                                                 messages=[{'role': 'user', 'content': 'hi'}])
        stream = await client.chat.completions.create(model='qwen/qwen3.8-27b:free', stream=True,
                                                      messages=[{'role': 'user', 'content': 'hi'}])
        text = ''.join([c.choices[0].delta.content or '' async for c in stream])
    finally:
        server.shutdown()
        server.server_close()
    assert reached == ['qwen/qwen3.8-27b:free'] and text == 'ok'
    assert _plain_backend_error(refused.value, backend) == (
        'anthropic/claude-opus-5 is not a free model; the Free tier only sends free models. Pick one with /model.')


def test_the_clinepass_backend_has_no_free_only_guard():
    assert not hasattr(_backend(), 'http_client')


def test_cline_refusals_read_as_plain_sentences():
    """The upstream body is the one measured on 2026-09-24 (qwen/qwen3.8-27b:free):
    Cline sends it as HTTP 200 + one `data: {"error": ...}` event, which openai
    raises as an APIError with no status code."""
    from openai import APIError

    from litetui.app import _plain_backend_error

    backend = _backend()
    request = httpx.Request('POST', 'https://api.cline.bot/api/v1/chat/completions')
    busy = {'code': 'stream_initialization_failed', 'message': (
        "Failed to create stream: ... request failed with status 429: {\"error\":{\"metadata\":{\"raw\":"
        "\"qwen/qwen3.8-27b:free is temporarily rate-limited upstream. Please retry shortly\"}}}")}
    assert _plain_backend_error(APIError(busy['message'], request, body=busy), backend).startswith(
        'This free model is busy upstream')
    limit = 'Free limit reached on model qwen/qwen3.8-27b:free. Try again in 12m.'
    assert _plain_backend_error(APIError(limit, request, body=None), backend) == (
        'Free limit reached on this model; try again in 12m, or pick another free model (/model).')
    product_only = {'code': 'API_REQUEST_ERROR_CODE', 'message': (
        'Error 403: cline-free/deepseek-v4.1-flash is only available via Cline product surfaces.')}
    assert _plain_backend_error(APIError(product_only['message'], request, body=product_only), backend) == (
        "This model is reserved for Cline's own apps; pick another model (/model).")
    assert backend.error_sentence(APIError('something else', request, body=None)) is None
