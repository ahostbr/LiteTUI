"""LiteTUI's free tier: several $0 sources behind one backend, with failover.

Ryan 2026-09-17 (liteask a-ae48d020), on freellmapi: "card a new free provider
backend we recreate based on that repo"; 2026-09-24: "now thats sick ! we got
FREE subagents now lol :) on top of frontier and local !" and "yes send
passlink on the multi-source free tier".

Pure Python, recreated from the freellmapi design (E:/SAS/REPO_CLONES/freellmapi,
server/src/services/ratelimit.ts, lib/fallback-loop.ts), not copied:

- SOURCES: a static table. Keyless sources are live with no setup; keyed ones
  stay inactive until they have a key: the one saved in /settings or the sidecar
  (Ryan 2026-09-24, liteask a-29b8bd60), else their environment variable. Read at
  call time; never printed, logged or sent to the sidecar.
- The same model offered by several sources is ONE picker entry (group_of), and
  a request fails over across its sources inside the HTTP transport, so a
  subagent never sees a busy source. It errors only when every source for that
  model is cooling, and then names the soonest retry.
- Cooldowns per (source, model, key): Retry-After (or the error text) wins; else
  a ladder 90s -> 2m -> 10m -> 1h -> 24h, reset by a success. 402/403 bench for a
  day; 401 marks the key rejected.
- The no-paid-model guard is structural: the router only ever sends a model a
  source's own catalog marks free, so an id that is not in the table is refused
  before a byte leaves the process.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import parsedate_to_datetime

import httpx

from litetui import cline_backend
from litetui.custom_backend import CustomBackend
from litetui.llm_backend import BackendError, ModelRow

CLINE_LOGIN = 'cline-login'  # key_env marker: the Cline OAuth login / CLINE_API_KEY


@dataclass(frozen=True)
class Source:
    id: str
    label: str
    base_url: str
    #: None = keyless; CLINE_LOGIN; otherwise the NAME of the env var holding the key.
    key_env: str | None
    models_url: str
    #: Which of the source's listed models are free (priced 0 by the source).
    is_free: Callable[[dict], bool]
    limits: str


def _openrouter_free(model: dict) -> bool:
    return str(model.get('id', '')).endswith(':free')


_NOT_CHAT = re.compile(r'embed|bge|guard|whisper|tts|rerank', re.IGNORECASE)

SOURCES: tuple[Source, ...] = (
    # ── keyless (live with no setup) ────────────────────────────────────────
    Source('cline', 'Cline', f'{cline_backend.CLINE_API}/api/v1', CLINE_LOGIN,
           f'{cline_backend.CLINE_API}/api/v1/models', _openrouter_free,
           "OpenRouter's shared free pool (upstream 429s); needs `cline auth`"),
    Source('kilo', 'Kilo Gateway', 'https://api.kilo.ai/api/gateway/v1', None,
           'https://api.kilo.ai/api/gateway/models', lambda m: bool(m.get('isFree')),
           '200 requests/hour per IP'),
    Source('ovh', 'OVH AI Endpoints', 'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1', None,
           'https://oai.endpoints.kepler.ai.cloud.ovh.net/v1/models',
           lambda m: (m.get('context_length') or 0) > 0 and not _NOT_CHAT.search(str(m.get('id', ''))),
           '2 requests/minute per IP per model (sends Retry-After)'),
    # ── keyed (inactive until the variable is set; free-tier accounts) ──────
    Source('groq', 'Groq', 'https://api.groq.com/openai/v1', 'GROQ_API_KEY',
           'https://api.groq.com/openai/v1/models', lambda m: True, 'free-tier key limits'),
    Source('cerebras', 'Cerebras', 'https://api.cerebras.ai/v1', 'CEREBRAS_API_KEY',
           'https://api.cerebras.ai/v1/models', lambda m: True, 'free-tier key limits'),
    Source('nvidia', 'NVIDIA NIM', 'https://integrate.api.nvidia.com/v1', 'NVIDIA_API_KEY',
           'https://integrate.api.nvidia.com/v1/models', lambda m: True, 'free developer credits/limits'),
    Source('mistral', 'Mistral', 'https://api.mistral.ai/v1', 'MISTRAL_API_KEY',
           'https://api.mistral.ai/v1/models', lambda m: True, 'free "Experiment" plan limits'),
    Source('github', 'GitHub Models', 'https://models.github.ai/inference', 'GITHUB_MODELS_TOKEN',
           'https://models.github.ai/catalog/models', lambda m: True, 'free per-account limits'),
    Source('openrouter', 'OpenRouter', 'https://openrouter.ai/api/v1', 'OPENROUTER_API_KEY',
           'https://openrouter.ai/api/v1/models', _openrouter_free, ':free models only'),
    Source('gemini', 'Google Gemini', 'https://generativelanguage.googleapis.com/v1beta/openai', 'GEMINI_API_KEY',
           'https://generativelanguage.googleapis.com/v1beta/openai/models', lambda m: True,
           'free-tier key limits'),
)


# ── keys ────────────────────────────────────────────────────────────────────

def source_key(source: Source) -> str | None:
    """'' for a keyless source, the key when one is available, None when the
    source needs a key (or a login) it does not have. Never logged."""
    if source.key_env is None:
        return ''
    if source.key_env == CLINE_LOGIN:
        try:
            return cline_backend.ClineBackend(None).api_key()
        except BackendError:
            return None
    return saved_key(source.key_env) or os.environ.get(source.key_env, '').strip() or None


def saved_key(key_env: str) -> str:
    """The key saved in /settings or the sidecar for `key_env` (the field is
    the var's name lowercased), read from disk at call time, so a save takes
    effect on the next request with no reconnect."""
    from litetui import settings as settings_mod

    return str(getattr(settings_mod.load(), key_env.lower(), '') or '').strip()


def _keyprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12] if key else 'keyless'


# ── catalogs ────────────────────────────────────────────────────────────────

_catalogs: dict[str, list[tuple[str, int | None]]] = {}  # ponytail: once per process per source+key


def _fetch_models(source: Source, key: str) -> list[tuple[str, int | None]]:
    headers = {'Authorization': f'Bearer {key}'} if key and source.key_env != CLINE_LOGIN else {}
    response = httpx.get(source.models_url, headers=headers, timeout=8)
    response.raise_for_status()
    body = response.json()
    rows = body.get('data', body) if isinstance(body, dict) else body
    out = []
    for m in rows if isinstance(rows, list) else []:
        if isinstance(m, dict) and isinstance(m.get('id'), str) and source.is_free(m):
            ctx = m.get('context_length') or (m.get('top_provider') or {}).get('context_length')
            out.append((m['id'].removeprefix('models/'), ctx if isinstance(ctx, int) and ctx > 0 else None))
    return out


def source_models(source: Source) -> list[tuple[str, int | None]]:
    key = source_key(source)
    if key is None:
        return []
    cache_key = f'{source.id}:{_keyprint(key)}'
    if cache_key not in _catalogs:
        try:
            _catalogs[cache_key] = _fetch_models(source, key)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            return []  # not cached: the next connect tries again
    return _catalogs[cache_key]


def group_of(model_id: str) -> str:
    """One picker entry per model, whoever serves it:
    'qwen/qwen3.8-27b:free' (Cline, Kilo) and 'Qwen3.8-27B' (OVH) -> 'qwen3.8-27b'."""
    name = model_id.rsplit('/', 1)[-1].removesuffix(':free').replace('_', '.').lower()
    # Router ids ("kilo-auto/free", "openrouter/free") name a ROUTE, not a model;
    # they keep their full id so two different routes never merge.
    return name if name not in ('', 'free') else model_id.lower()


def catalog() -> dict[str, list[tuple[Source, str, int | None]]]:
    """group -> [(source, that source's id, context window)], in SOURCES order."""
    groups: dict[str, list[tuple[Source, str, int | None]]] = {}
    for source in SOURCES:
        for model_id, ctx in source_models(source):
            groups.setdefault(group_of(model_id), []).append((source, model_id, ctx))
    return groups


# ── cooldowns ───────────────────────────────────────────────────────────────

_LADDER = (90, 120, 600, 3600, 86400)
_DAY = 86400


@dataclass
class _Bench:
    until: float
    level: int
    set_at: float
    reason: str


_benches: dict[tuple[str, str, str], _Bench] = {}


def _retry_hint(response: httpx.Response | None, text: str) -> float | None:
    if response is not None:
        for name in ('retry-after', 'ratelimit-reset', 'x-ratelimit-reset-requests'):
            raw = response.headers.get(name)
            if not raw:
                continue
            try:
                return max(0.0, float(raw.rstrip('s')))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(raw).timestamp() - time.time())
                except (TypeError, ValueError):
                    pass
    match = re.search(r'(?:try again|retry)\s+(?:in|after)\s+(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h)\b', text, re.IGNORECASE)
    if match:
        value, unit = float(match.group(1)), match.group(2).lower()
        return value / 1000 if unit == 'ms' else value * {'s': 1, 'sec': 1, 'm': 60, 'min': 60, 'h': 3600}[unit]
    return None


def bench(key: tuple[str, str, str], status: int, response: httpx.Response | None = None,
          text: str = '', *, now: float | None = None) -> float:
    """Record a failure; return how long this (source, model, key) sits out."""
    now = time.time() if now is None else now
    if status in (401, 402, 403):
        seconds, level = _DAY, 0
        reason = 'key rejected' if status == 401 else 'not available on this key'
    else:
        prev = _benches.get(key)
        level = prev.level + 1 if prev and now - prev.set_at < _DAY else 0
        hint = _retry_hint(response, text)
        seconds = hint if hint is not None else _LADDER[min(level, len(_LADDER) - 1)]
        reason = 'busy'
    _benches[key] = _Bench(now + seconds, level, now, reason)
    return seconds


def clear(key: tuple[str, str, str]) -> None:
    _benches.pop(key, None)


def cooling(key: tuple[str, str, str], now: float | None = None) -> float:
    entry = _benches.get(key)
    left = (entry.until - (time.time() if now is None else now)) if entry else 0.0
    return max(0.0, left)


def fmt_wait(seconds: float) -> str:
    seconds = round(seconds)
    if seconds < 90:
        return f'{seconds}s'
    if seconds < 5400:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds // 3600}h{(seconds % 3600) // 60:02d}m'


# ── routing ─────────────────────────────────────────────────────────────────

class _Replay(httpx.AsyncByteStream):
    """A streamed body whose first bytes were already read (to look for an
    in-stream error event), handed on intact."""

    def __init__(self, prefix: bytes, rest, original: httpx.AsyncByteStream):
        self._prefix, self._rest, self._original = prefix, rest, original

    async def __aiter__(self):
        if self._prefix:
            yield self._prefix
        async for chunk in self._rest:
            yield chunk

    async def aclose(self):
        await self._original.aclose()  # releases the pooled connection


async def _first_event(response: httpx.Response) -> tuple[bytes, object]:
    """Read up to the first SSE event; return (bytes read, remaining iterator)."""
    stream = response.stream.__aiter__()
    head = b''
    while b'\n\n' not in head and len(head) < 8192:
        try:
            head += await stream.__anext__()
        except StopAsyncIteration:
            break
    return head, stream


def _in_stream_error(head: bytes) -> str | None:
    """Cline (and some gateways) answer HTTP 200 and then one `data: {"error": ...}`."""
    for line in head.split(b'\n'):
        if line.startswith(b'data:') and b'"error"' in line:
            return line[5:].strip().decode('utf-8', 'replace')
    return None


def _log(source: Source, model: str, outcome: str, wait: float) -> None:
    """Which source was tried and what it said, so a busy answer can be traced (never the key)."""
    from litetui import runtime_log

    # runtime_log only accepts its allow-listed keys and single-token strings.
    runtime_log.record('free_route', component=source.id, model=model[:128],
                       operation=outcome.replace(' ', '-'), duration_ms=round(wait * 1000))


class FreeRouter(httpx.AsyncBaseTransport):
    """Every chat request picks a source for its model, and fails over."""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith('/chat/completions'):
            raise BackendError('The Free tier only sends chat requests.')
        body = json.loads(request.content or b'{}')
        model = str(body.get('model', ''))
        candidates = (await asyncio.to_thread(catalog)).get(group_of(model))
        if not candidates:
            raise BackendError(f'{model or "That model"} is not a free model; the Free tier only sends free models. '
                               'Pick one with /model.')
        soonest: float | None = None
        for source, source_model, _ctx in candidates:
            key = await asyncio.to_thread(source_key, source)
            if key is None:
                continue
            bench_key = (source.id, source_model, _keyprint(key))
            left = cooling(bench_key)
            if left:
                soonest = left if soonest is None else min(soonest, left)
                continue
            headers = {k: v for k, v in request.headers.items()
                       if k.lower() not in ('authorization', 'host', 'content-length')}
            if key:
                headers['Authorization'] = f'Bearer {key}'
            outbound = httpx.Request('POST', f'{source.base_url}/chat/completions', headers=headers,
                                     content=json.dumps({**body, 'model': source_model}).encode(),
                                     extensions=request.extensions)
            try:
                response = await self._inner.handle_async_request(outbound)
            except httpx.TransportError:
                wait = bench(bench_key, 503)
                _log(source, source_model, 'unreachable', wait)
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            if response.status_code == 429 or response.status_code in (401, 402, 403) or response.status_code >= 500:
                text = (await response.aread()).decode('utf-8', 'replace')
                await response.aclose()
                wait = bench(bench_key, response.status_code, response, text)
                _log(source, source_model, f'http {response.status_code}', wait)
                soonest = wait if soonest is None else min(soonest, wait)
                continue
            if response.status_code == 200 and body.get('stream'):
                head, rest = await _first_event(response)
                error = _in_stream_error(head)
                if error and re.search(r'429|rate.?limit|busy|capacity|overloaded', error, re.IGNORECASE):
                    await response.aclose()
                    wait = bench(bench_key, 429, None, error)
                    _log(source, source_model, 'in-stream 429', wait)
                    soonest = wait if soonest is None else min(soonest, wait)
                    continue
                response = httpx.Response(response.status_code, headers=response.headers,
                                          stream=_Replay(head, rest, response.stream),
                                          extensions=response.extensions)
            clear(bench_key)
            _log(source, source_model, 'served', 0)
            return response
        message = (f'Every free source for {group_of(model)} is busy; soonest retry in {fmt_wait(soonest)}.'
                   if soonest is not None else
                   f'No free source for {group_of(model)} is available (each needs a key or login it does not have).')
        return httpx.Response(429, json={'error': {'message': message, 'code': 'free_tier_exhausted'}},
                              headers={'x-should-retry': 'false'}, request=request)


# ── the backend ─────────────────────────────────────────────────────────────

def status_mark() -> str:
    """The /backend and sidecar readiness mark. Keys and benches only, no network."""
    live = sum(1 for s in SOURCES if source_key(s) is not None)
    need = len(SOURCES) - live
    waits = [c for c in (cooling(k) for k in list(_benches)) if c]
    parts = [f'{live} sources live']
    if waits:
        parts.append(f'{len(waits)} cooling (soonest {fmt_wait(min(waits))})')
    if need:
        parts.append(f'{need} need a key')
    return ' · '.join(parts)


class FreeBackend(CustomBackend):
    name = 'free'
    label = 'Free tier'

    def set_settings(self, settings):
        self._settings = settings
        # Never contacted: FreeRouter rewrites every request to a real source.
        self._base = 'https://free-tier.litetui.invalid/v1'

    def api_key(self):
        return 'free-tier'  # each source's own key is added by the router

    def _rows(self):
        return [ModelRow(key=group, path=None, source='server', loaded=True) for group in catalog()]

    async def ensure_running(self):
        return 'ok'

    async def model_info(self, key):
        entries = (await asyncio.to_thread(catalog)).get(key)
        if not entries:
            return None
        windows = [ctx for _s, _m, ctx in entries if ctx]
        return (min(windows) if windows else None), 'llm', True

    def reasoning_levels(self, key):
        return ['none']

    def http_client(self):
        from openai import DefaultAsyncHttpxClient

        return DefaultAsyncHttpxClient(transport=FreeRouter())

    def error_sentence(self, error: BaseException) -> str | None:
        cause = error.__cause__ or error.__context__
        if isinstance(cause, BackendError):
            return str(cause)
        body = getattr(error, 'body', None)
        detail = body.get('error', body) if isinstance(body, dict) else None
        if isinstance(detail, dict) and detail.get('code') == 'free_tier_exhausted':
            return str(detail.get('message'))
        return cline_backend.ClineBackend.error_sentence(self, error)

    async def load(self, key, *, ctx=None, notice=None):
        raise BackendError('Free-tier models are remote; pick one with /model.')

    async def unload(self, key):
        raise BackendError('Free-tier models are remote; nothing to unload.')

    def empty_state_hint(self):
        return ('No free source answered. Kilo and OVH need no setup; Cline needs `cline auth`; '
                'keyed sources need their variable (GROQ_API_KEY, ...). Then /reconnect.')
