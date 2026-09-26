"""The multi-source free tier: grouping, failover, cooldowns, and the paid guard.

Ryan 2026-09-24: "now thats sick ! we got FREE subagents now lol :) on top of
frontier and local !" / "yes send passlink on the multi-source free tier".
Every source here is a local fake server; nothing leaves the machine.
"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from openai import AsyncOpenAI, RateLimitError

from litetui import free_tier as ft
from litetui.llm_backend import BackendError, make_backend
from litetui.settings import Settings


def _sse(text: str) -> str:
    chunk = {'id': 'c', 'object': 'chat.completion.chunk', 'created': 0, 'model': 'm',
             'choices': [{'index': 0, 'delta': {'content': text}, 'finish_reason': None}]}
    return f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n'


class Fake:
    """One fake source: a /models list and a scripted /chat/completions."""

    def __init__(self, models, replies):
        self.models, self.replies, self.seen = models, list(replies), []
        self.delay, self.body_delay, self.envelope, self.paths = 0.0, 0.0, 'data', []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, status, body, ctype='application/json', headers=()):
                data = body.encode()
                self.send_response(status)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Length', str(len(data)))
                for k, v in headers:
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.flush()
                time.sleep(fake.body_delay if self.command == 'POST' and status == 200 else 0)
                self.wfile.write(data)

            def do_GET(self):
                fake.paths.append(self.path)
                self._send(200, json.dumps({fake.envelope: fake.models}))

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fake.seen.append({'model': body['model'], 'auth': self.headers.get('Authorization')})
                fake.paths.append(self.path)
                time.sleep(fake.delay)
                status, text, headers = fake.replies.pop(0) if fake.replies else (200, _sse('ok'), ())
                self._send(status, text, 'text/event-stream' if status == 200 else 'application/json', headers)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def sources(monkeypatch, tmp_path):
    """Two keyless fakes and one keyed fake, standing in for SOURCES."""
    monkeypatch.setenv('LITETUI_DATA_ROOT', str(tmp_path))  # saved keys: an empty settings.json
    made = []

    def install(a_models, a_replies, b_models, b_replies, keyed_models=(), keyed_replies=()):
        a, b, k = Fake(a_models, a_replies), Fake(b_models, b_replies), Fake(list(keyed_models), keyed_replies)
        made.extend((a, b, k))
        table = (
            ft.Source('a', 'A', a.url, None, a.url + '/models', lambda m: m['id'].endswith(':free'), ''),
            ft.Source('b', 'B', b.url, None, b.url + '/models', lambda m: True, ''),
            ft.Source('k', 'K', k.url, 'FAKE_FREE_KEY', k.url + '/models', lambda m: True, ''),
        )
        monkeypatch.setattr(ft, 'SOURCES', table)
        return a, b, k

    monkeypatch.setattr(ft, '_catalogs', {})
    monkeypatch.setattr(ft, '_benches', {})
    monkeypatch.delenv('FAKE_FREE_KEY', raising=False)
    yield install
    for fake in made:
        fake.close()


def _backend():
    return make_backend(Settings(backend='free'))


async def _ask(backend, model):
    client = AsyncOpenAI(base_url=backend.base_url(), api_key=backend.api_key(),
                         http_client=backend.http_client(), max_retries=0)
    stream = await client.chat.completions.create(model=model, stream=True,
                                                  messages=[{'role': 'user', 'content': 'hi'}])
    return ''.join([c.choices[0].delta.content or '' async for c in stream])


# ── the list ───────────────────────────────────────────────────────────────

def test_one_entry_per_model_whoever_serves_it_and_only_free_ones(sources):
    sources([{'id': 'qwen/qwen3.8-27b:free', 'context_length': 131072}, {'id': 'openai/gpt-6:paid'}],
            [], [{'id': 'Qwen3.8-27B', 'context_length': 262144}, {'id': 'gpt-oss-120b'}], [])
    groups = ft.catalog()
    assert list(groups) == ['qwen3.8-27b', 'gpt-oss-120b']
    assert [(s.id, m) for s, m, _ in groups['qwen3.8-27b']] == [('a', 'qwen/qwen3.8-27b:free'), ('b', 'Qwen3.8-27B')]
    assert 'gpt-6' not in ' '.join(groups), 'a paid id must never be listed'


@pytest.mark.asyncio
async def test_the_backend_lists_groups_and_reports_the_smallest_window(sources):
    sources([{'id': 'qwen/qwen3.8-27b:free', 'context_length': 131072}], [],
            [{'id': 'Qwen3.8-27B', 'context_length': 262144}], [])
    backend = _backend()
    assert [r.key for r in await backend.list_models()] == ['qwen3.8-27b']
    assert await backend.model_info('qwen3.8-27b') == (131072, 'llm', True)


# ── failover ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_busy_source_fails_over_silently_and_sits_out_its_retry_after(sources):
    a, b, _ = sources([{'id': 'qwen/qwen3.8-27b:free'}], [(429, '{"error":"busy"}', (('Retry-After', '30'),))],
                      [{'id': 'Qwen3.8-27B'}], [])
    backend = _backend()
    assert await _ask(backend, 'qwen3.8-27b') == 'ok'
    assert [s['model'] for s in a.seen] == ['qwen/qwen3.8-27b:free'] and [s['model'] for s in b.seen] == ['Qwen3.8-27B']
    assert 25 < ft.cooling(('a', 'qwen/qwen3.8-27b:free', 'keyless')) <= 30
    assert await _ask(backend, 'qwen3.8-27b') == 'ok'
    assert len(a.seen) == 1, 'a cooling source is not asked again'


@pytest.mark.asyncio
async def test_clines_in_stream_429_fails_over_too(sources):
    """Cline answers HTTP 200 and then one `data: {"error": ...}` event (measured 2026-09-24)."""
    error = 'data: {"error":{"code":"stream_initialization_failed","message":"status 429: rate-limited upstream"}}\n\n'
    a, b, _ = sources([{'id': 'm:free'}], [(200, error + 'data: [DONE]\n\n', ())], [{'id': 'M'}], [])
    assert await _ask(_backend(), 'm') == 'ok'
    assert len(a.seen) == 1 and len(b.seen) == 1
    assert ft.cooling(('a', 'm:free', 'keyless')) == pytest.approx(90, abs=2)


@pytest.mark.asyncio
async def test_a_server_error_fails_over_and_a_success_clears_the_bench(sources):
    sources([{'id': 'm:free'}], [(503, '{}', ()), (200, _sse('from a'), ())], [{'id': 'M'}], [])
    backend = _backend()
    assert await _ask(backend, 'm') == 'ok'
    ft._benches.clear()  # time passes
    assert await _ask(backend, 'm') == 'from a'
    assert ft.cooling(('a', 'm:free', 'keyless')) == 0


@pytest.mark.asyncio
async def test_when_every_source_is_cooling_it_names_the_soonest_retry(sources):
    from litetui.app import _plain_backend_error

    sources([{'id': 'm:free'}], [(429, '{}', (('Retry-After', '40'),))],
            [{'id': 'M'}], [(429, '{"error":"try again in 2m"}', ())])
    backend = _backend()
    with pytest.raises(Exception) as busy:
        await _ask(backend, 'm')
    said = _plain_backend_error(busy.value, backend)
    assert said.startswith('Every free source for m is busy; soonest retry in') and '40s' in said, said


# ── the guard ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_model_no_source_marks_free_never_leaves_the_process(sources):
    from litetui.app import _plain_backend_error

    a, b, _ = sources([{'id': 'm:free'}, {'id': 'anthropic/claude-opus-5'}], [], [{'id': 'M'}], [])
    backend = _backend()
    with pytest.raises(Exception) as refused:
        await _ask(backend, 'anthropic/claude-opus-5')
    assert a.seen == [] and b.seen == []
    assert 'not a free model' in _plain_backend_error(refused.value, backend)


# ── keys ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_keyed_source_is_inactive_until_its_variable_is_set_and_keyless_sends_no_key(sources, monkeypatch):
    a, _b, k = sources([], [], [], [], keyed_models=[{'id': 'llama-4-scout'}])
    assert 'llama-4-scout' not in ft.catalog()
    assert '1 need a key' in ft.status_mark()
    monkeypatch.setenv('FAKE_FREE_KEY', 'sk-test-free')
    ft._catalogs.clear()
    assert await _ask(_backend(), 'llama-4-scout') == 'ok'
    assert k.seen == [{'model': 'llama-4-scout', 'auth': 'Bearer sk-test-free'}]
    a.models = [{'id': 'x:free'}]
    ft._catalogs.clear()
    assert await _ask(_backend(), 'x') == 'ok'
    assert a.seen[-1]['auth'] is None, 'a keyless source must not be sent any key'


@pytest.mark.asyncio
async def test_a_key_saved_in_settings_wins_and_the_env_var_is_the_fallback(sources, monkeypatch, tmp_path):
    """Ryan 2026-09-24 (liteask a-29b8bd60): "Add a key field per source in
    /settings + sidecar". The saved key is read at call time, so saving it
    activates the source on the next request with no reconnect."""
    from dataclasses import replace

    from litetui import settings as settings_mod

    _a, _b, k = sources([], [], [], [], keyed_models=[{'id': 'llama-4-scout'}])
    monkeypatch.setattr(ft, 'SOURCES', ft.SOURCES[:2] + (replace(ft.SOURCES[2], key_env='GROQ_API_KEY'),))
    monkeypatch.delenv('GROQ_API_KEY', raising=False)
    assert 'llama-4-scout' not in ft.catalog()
    s = settings_mod.load(tmp_path)
    s.groq_api_key = 'gsk-saved'
    settings_mod.save(s, tmp_path)
    ft._catalogs.clear()
    assert await _ask(_backend(), 'llama-4-scout') == 'ok'
    assert k.seen[-1]['auth'] == 'Bearer gsk-saved'
    monkeypatch.setenv('GROQ_API_KEY', 'gsk-env')
    assert await _ask(_backend(), 'llama-4-scout') == 'ok'
    assert k.seen[-1]['auth'] == 'Bearer gsk-saved', 'the saved key must win over the env var'
    s.groq_api_key = ''
    settings_mod.save(s, tmp_path)
    ft._catalogs.clear()
    assert await _ask(_backend(), 'llama-4-scout') == 'ok'
    assert k.seen[-1]['auth'] == 'Bearer gsk-env', 'a blank saved key falls back to the env var'


def test_the_cline_source_sits_out_without_a_login(monkeypatch):
    monkeypatch.setattr(ft.cline_backend.ClineBackend, 'api_key',
                        lambda self: (_ for _ in ()).throw(BackendError('no login')))
    cline = next(s for s in ft.SOURCES if s.id == 'cline')
    assert ft.source_key(cline) is None and ft.source_models(cline) == []


# ── cooldown rules ─────────────────────────────────────────────────────────

def test_the_ladder_honours_hints_and_resets_on_success():
    ft._benches.clear()
    key = ('s', 'm', 'keyless')
    assert [ft.bench(key, 429, now=0) for _ in range(6)] == [90, 120, 600, 3600, 86400, 86400]
    ft.clear(key)
    assert ft.bench(key, 429, now=0) == 90
    assert ft.bench(('s', 'm2', 'keyless'), 402, now=0) == 86400
    assert ft.bench(('s', 'm3', 'keyless'), 401, now=0) == 86400
    assert ft._benches[('s', 'm3', 'keyless')].reason == 'key rejected'
    assert ft.bench(('s', 'm4', 'keyless'), 429, text='Free limit reached. Try again in 12m.', now=0) == 720
    assert ft.fmt_wait(30) == '30s' and ft.fmt_wait(125) == '2m05s' and ft.fmt_wait(7200) == '2h00m'


def test_the_status_mark_counts_live_cooling_and_keyless(sources):
    sources([], [], [], [])
    ft.bench(('a', 'm:free', 'keyless'), 429, now=None)
    mark = ft.status_mark()
    assert mark.startswith('2 sources live') and 'cooling (soonest' in mark and '1 need a key' in mark


def test_the_real_table_has_the_four_keyless_sources_first():
    assert [s.id for s in ft.SOURCES[:4]] == ['cline', 'kilo', 'ovh', 'llm7']
    assert all(s.key_env is None for s in ft.SOURCES[1:4])
    assert all(s.key_env for s in ft.SOURCES[4:]), 'keyed sources name the variable that activates them'


@pytest.mark.asyncio
async def test_every_route_is_recorded_in_a_form_the_runtime_log_accepts(sources, monkeypatch):
    """Through the REAL sanitizer: a key it does not allow makes record() a
    silent no-op, which is exactly how a trace goes missing."""
    from litetui import runtime_log

    seen = []
    monkeypatch.setattr(runtime_log, 'record',
                        lambda event, **meta: seen.append(runtime_log.sanitize_event({'event': event, **meta})))
    sources([{'id': 'm:free'}], [(429, '{}', (('Retry-After', '30'),))], [{'id': 'M'}], [])
    assert await _ask(_backend(), 'm') == 'ok'
    assert [(e['component'], e['operation']) for e in seen] == [('a', 'http-429'), ('b', 'served')]
    assert seen[0]['duration_ms'] == 30000


# ── T938: new sources ──────────────────────────────────────────────────────

def _real(source_id):
    return next(s for s in ft.SOURCES if s.id == source_id)


def test_llm7_is_keyless_and_lists_only_the_models_that_need_no_balance(monkeypatch):
    """Row shape measured from one anonymous GET of api.llm7.io/v1/models (2026-09-26):
    every row has a price; `usage_based_only: false` marks the rate-limited free ones."""
    llm7 = _real('llm7')
    assert llm7.key_env is None and ft.source_key(llm7) == '' and llm7.pool
    rows = [{'id': 'codestral-latest', 'model_type': 'chat', 'usage_based_only': False},
            {'id': 'claude-opus-5', 'model_type': 'chat', 'usage_based_only': True},
            {'id': 'no-flag', 'model_type': 'chat'},
            {'id': 'flux', 'model_type': 'image', 'usage_based_only': False}]
    assert [m['id'] for m in rows if llm7.is_free(m)] == ['codestral-latest']


def test_zai_lists_only_its_allowlisted_flash_models(sources):
    """Z.ai's /models also lists paid GLM, and a funded account is charged for it."""
    fake = Fake([{'id': 'glm-4.7-flash'}, {'id': 'glm-4.7'}, {'id': 'glm-5'}, {'id': 'glm-4.6v-flash'},
                 {'id': 'glm-4.5-air'}], [])
    try:
        zai = replace(_real('zai'), models_url=fake.url + '/models')
        assert [m for m, _ in ft._fetch_models(zai, 'zk')] == ['glm-4.7-flash', 'glm-4.6v-flash']
    finally:
        fake.close()


@pytest.mark.asyncio
async def test_a_cloudflare_key_splits_into_account_url_and_token_bearer(sources, monkeypatch):
    real = _real('cloudflare')
    sources([], [], [], [])
    fake = Fake([{'id': '6f1e-uuid', 'name': '@cf/meta/llama-4-scout'}], [])
    fake.envelope = 'result'  # Cloudflare's API envelope
    try:
        cf = replace(real, base_url=fake.url + '/accounts/{account}/ai/v1',
                     models_url=fake.url + '/accounts/{account}/ai/models/search')
        monkeypatch.setattr(ft, 'SOURCES', (cf,))
        monkeypatch.delenv('CLOUDFLARE_API_KEY', raising=False)
        for not_configured in ('', 'just-a-token', ':tok', 'acct:'):
            monkeypatch.setenv('CLOUDFLARE_API_KEY', not_configured)
            assert ft.source_key(cf) is None, not_configured
        assert ft.catalog() == {} and '1 need a key' in ft.status_mark()
        monkeypatch.setenv('CLOUDFLARE_API_KEY', 'acct123:cf-token')
        assert await _ask(_backend(), 'llama-4-scout') == 'ok'
        assert fake.seen == [{'model': '@cf/meta/llama-4-scout', 'auth': 'Bearer cf-token'}]
        assert fake.paths == ['/accounts/acct123/ai/models/search', '/accounts/acct123/ai/v1/chat/completions']
    finally:
        fake.close()


def test_every_new_keyed_source_has_its_settings_field():
    from litetui.settings import Settings as S
    from litetui.sidecar_settings import SECRET_FIELDS

    for sid in ('ollama', 'zai', 'cloudflare', 'longcat', 'sealion'):
        field = _real(sid).key_env.lower()
        assert field in SECRET_FIELDS and hasattr(S(), field), field


# ── T938: router fixes ─────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize('stall', ['delay', 'body_delay'])  # before its headers / before its first event
async def test_a_stalled_source_is_cut_off_at_its_budget_and_fails_over(sources, monkeypatch, stall):
    a, b, _ = sources([{'id': 'm:free'}], [], [{'id': 'M'}], [])
    monkeypatch.setattr(ft, 'SOURCES', (replace(ft.SOURCES[0], timeout=0.3),) + ft.SOURCES[1:])
    setattr(a, stall, 3)
    started = time.monotonic()
    assert await _ask(_backend(), 'm') == 'ok'
    assert time.monotonic() - started < 2, 'the SDK default (600s read) must not apply to an attempt'
    assert len(a.seen) == 1 and len(b.seen) == 1
    assert ft.cooling(('a', 'm:free', 'keyless')) == pytest.approx(90, abs=2)


@pytest.mark.asyncio
async def test_a_429_on_a_pooled_source_benches_every_model_there(sources, monkeypatch):
    """OpenRouter's :free pool, Kilo's and LLM7's per-IP hour: one 429 means the
    next model there is out too, so it must not cost another hop."""
    a, _b, _ = sources([{'id': 'm:free'}, {'id': 'n:free'}], [(429, '{}', ())], [{'id': 'M'}], [])
    monkeypatch.setattr(ft, 'SOURCES', (replace(ft.SOURCES[0], pool=True),) + ft.SOURCES[1:])
    assert await _ask(_backend(), 'm') == 'ok'
    assert ft.cooling(('a', '*', 'keyless')) == pytest.approx(90, abs=2)
    with pytest.raises(RateLimitError):
        await _ask(_backend(), 'n')
    assert [s['model'] for s in a.seen] == ['m:free'], 'a pooled 429 benches the other models too'


@pytest.mark.asyncio
async def test_a_per_model_source_keeps_its_per_model_bench(sources):
    a, _b, _ = sources([{'id': 'm:free'}, {'id': 'n:free'}], [(429, '{}', ())], [{'id': 'M'}], [])
    assert await _ask(_backend(), 'm') == 'ok'
    assert await _ask(_backend(), 'n') == 'ok'
    assert [s['model'] for s in a.seen] == ['m:free', 'n:free']


@pytest.mark.asyncio
async def test_a_402_benches_the_whole_source_for_this_key_for_a_day(sources):
    _a, b, _ = sources([], [], [{'id': 'M'}, {'id': 'N'}], [(402, '{"error":"no credits"}', ())])
    with pytest.raises(RateLimitError):
        await _ask(_backend(), 'm')
    assert ft.cooling(('b', '*', 'keyless')) == pytest.approx(86400, abs=5)
    with pytest.raises(RateLimitError):
        await _ask(_backend(), 'n')
    assert len(b.seen) == 1, 'an out-of-credit account is not asked for another model'


def test_every_retry_hint_is_clamped_to_a_day():
    import httpx

    ft._benches.clear()
    year = httpx.Response(429, headers={'retry-after': '31536000'})
    assert ft.bench(('s', 'h', 'keyless'), 429, year, now=0) == 86400
    date = httpx.Response(429, headers={'retry-after': 'Fri, 01 Jan 2100 00:00:00 GMT'})
    assert ft.bench(('s', 'd', 'keyless'), 429, date) == 86400
    assert ft.bench(('s', 't', 'keyless'), 429, text='Please try again in 9999h.', now=0) == 86400
    assert ft.bench(('s', 'ok', 'keyless'), 429, text='try again in 12m', now=0) == 720
