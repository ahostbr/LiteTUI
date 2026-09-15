"""The native tok/s wiring, driven through the REAL `_stream` loop.

Sentinel's point, and he was right: "a manual footer check is not the only
possible evidence of wiring." The parts were already covered — a clock that must
be started before `final()` can fire, a rebased turn that publishes nothing —
but nothing proved the loop actually reaches them on the Codex path. These tests
run `LiteTUI._stream` itself with a native backend and a scripted chunk stream.

The defect they pin: `native_loop` skipped `TpsState.tick`, so `t0` was never
set, AND the settle sat behind `if not native_loop`, so `final()` was
unreachable. Two guards, one silent outcome — the Codex backend published no
tok/s at all.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-native-tps-"))


class _AppServer:
    """Only what the app touches: presence makes `native_loop` true, and
    `_shutdown` awaits close() (app.py:1575)."""

    async def close(self):
        return None


class _NativeBackend:
    """`native_loop` is `hasattr(self.backend, "app_server")` (app.py:5565)."""

    name = "codex"

    def __init__(self):
        self.app_server = _AppServer()

    def base_url(self):
        return "http://127.0.0.1:0/v1"

    def host(self):
        return "127.0.0.1:0"

    async def ensure_running(self):
        return "ok"

    async def list_models(self):
        return []

    def request_overrides(self, key):
        return {}


class _Delta:
    def __init__(self, content=None):
        self.content = content
        self.reasoning_content = None
        self.reasoning = None
        self.tool_calls = None


class _Chunk:
    def __init__(self, content=None, usage=None):
        self.choices = (
            [] if content is None else [type("C", (), {"delta": _Delta(content)})()]
        )
        self.usage = usage
        self.provider_metadata = None


def _usage(completion, *, context=1200, window=258400):
    """Shaped like NativeUsage's return: only the fields the loop reads."""
    return SimpleNamespace(
        prompt_tokens=1000,
        completion_tokens=completion,
        total_tokens=None if completion is None else 1000 + completion,
        cached_tokens=800,
        cache_write_tokens=0,
        input_tokens_details={"cached_tokens": 800, "cache_write_tokens": 0},
        context_tokens=context,
        max_context_tokens=window,
        latest_request_usage={},
        thread_usage={},
        turn_usage={} if completion is None else {"outputTokens": completion},
        usage_details={},
    )


class _Stream:
    """🔴 `gap` buys REAL wall-clock between chunks, and it is load-bearing.

    `time.monotonic()` on Windows advances in ~15.6 ms steps. Emitting the
    content chunk and the usage chunk inside one step makes `TpsState.final`
    compute `elapsed == 0.0` and decline to divide — so a zero-gap stream tests
    the clock's granularity, not the wiring, and fails for a reason that has
    nothing to do with the code under test. A real turn takes seconds.
    """

    def __init__(self, chunks, gap: float = 0.05):
        self._chunks = chunks
        self._gap = gap

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            chunk = next(self._it)
        except StopIteration:
            raise StopAsyncIteration
        await asyncio.sleep(self._gap)
        return chunk

    async def close(self):
        pass


def _app(chunks):
    a = app_mod.LiteTUI()
    a.settings = Settings(
        tools_enabled=False,
        autocompact_enabled=False,
        wake_after_compact=False,
        clear_screen_after_compact=False,
    )
    a.tools_enabled = False
    a.backend = _NativeBackend()
    a.model_id = "gpt-6-astra"
    a.said = []
    a._system = lambda msg, *x, **k: a.said.append(str(msg))
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None

    async def ready():
        return None

    a._ensure_chat_ready = ready

    class _Transport:
        async def create(self, **kwargs):
            return _Stream(chunks)

    a._transport_for_test = _Transport()
    return a


async def _run(a, monkeypatch, pilot):
    monkeypatch.setattr(
        app_mod.model_transport, "for_app", lambda app: a._transport_for_test
    )
    a.conversation = [{"role": "system", "content": "sys"}]
    a._append({"role": "user", "content": "hello"})
    a._stream()
    for _ in range(200):
        await pilot.pause()


@pytest.mark.asyncio
async def test_the_codex_path_publishes_a_rate_from_the_servers_own_count(monkeypatch):
    """🔴 THE DEFECT. Before the fix this stream published no rate at all:
    the content chunk never ticked the clock, so the usage chunk could not
    settle. Both halves have to be wired for `tps` to move."""
    a = _app([_Chunk(content="OK"), _Chunk(usage=_usage(120))])
    async with a.run_test() as pilot:
        await _run(a, monkeypatch, pilot)
        rate = a.tps
    assert isinstance(rate, (int, float)) and rate > 0, (
        f"the Codex path published no tok/s: {rate!r}"
    )


@pytest.mark.asyncio
async def test_a_rebased_turn_publishes_no_rate_rather_than_a_wrong_one(monkeypatch):
    """After a compaction the turn delta is unknown. Unknown must stay blank —
    a fabricated rate is worse than none."""
    a = _app([_Chunk(content="OK"), _Chunk(usage=_usage(None))])
    async with a.run_test() as pilot:
        before = a.tps
        await _run(a, monkeypatch, pilot)
        # NOT VACUOUS: the usage chunk really was consumed by the loop.
        assert a.last_usage["completion_tokens"] is None
        assert a._tps.t0 is not None, "the clock never started; nothing was tested"
        assert a.tps == before, f"a rebased turn fabricated a rate: {a.tps!r}"


@pytest.mark.asyncio
async def test_a_usage_chunk_with_no_stream_before_it_settles_nothing(monkeypatch):
    """The clock is started by deltas. A turn that produced none has no
    denominator, and `final` must decline rather than divide by a guess."""
    a = _app([_Chunk(usage=_usage(120))])
    async with a.run_test() as pilot:
        before = a.tps
        await _run(a, monkeypatch, pilot)
        # NOT VACUOUS: the usage chunk arrived, there was simply no clock.
        assert a.last_usage["completion_tokens"] == 120
        assert a._tps.t0 is None, "a delta-less turn started a clock anyway"
        assert a.tps == before


@pytest.mark.asyncio
async def test_the_native_live_estimate_is_still_withheld_mid_stream(monkeypatch):
    """Ticking must NOT start publishing the delta-derived live estimate on the
    native path — those deltas are not tokens. Only the settled figure shows.
    Many content chunks, no usage chunk: nothing may be published."""
    a = _app([_Chunk(content=f"tok{i}") for i in range(12)])
    async with a.run_test() as pilot:
        before = a.tps
        await _run(a, monkeypatch, pilot)
        # NOT VACUOUS: the deltas really did tick the clock, well past the 0.4s
        # floor and the 0.25s repaint gate, so a publishing path WOULD have
        # fired had one been wired. 12 chunks at a 0.05s gap is ~0.6s.
        assert a._tps.t0 is not None and a._tps.n >= 10, (
            f"the stream was not consumed: t0={a._tps.t0!r} n={a._tps.n}"
        )
        assert a.tps == before, (
            f"the native loop published a live delta-rate: {a.tps!r}"
        )
