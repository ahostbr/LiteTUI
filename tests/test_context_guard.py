"""The context guard: one request never carries the context past the user's auto-compact percent.

Ryan, 2026-11-21, after a single reasoning block ran to the whole 32,768-token output cap inside one turn at
150,000 context: "this thinking turn is gonna run over the 80% auto compact limit ... then it wont have enough
room to compact ... it can never bypass the 80% limit", then: "its not exactly 80% thats what i have it set to
in user settings ... user pick the %".
"""
from types import SimpleNamespace

import pytest
from test_card_summary import _Chunk, _Stream
from test_pause import _agentic_app

from litetui import app as app_mod
from litetui.turn_engine import TurnEngine


# ── the policy, at two settings ──────────────────────────────────────────────

@pytest.mark.parametrize("percent, ceiling", [(80, 120_000), (60, 90_000)])
def test_the_ceiling_is_the_users_percent_of_the_window(percent, ceiling):
    kw = dict(enabled=True, at_percent=percent, ctx_max=150_000, ctx_loaded=True)
    assert TurnEngine.context_guard_ceiling(**kw) == ceiling
    assert TurnEngine.context_guard_ceiling(**{**kw, "enabled": False}) is None      # auto-compact off: no guard
    assert TurnEngine.context_guard_ceiling(**{**kw, "ctx_loaded": False}) is None   # a ceiling, not a window
    assert TurnEngine.context_guard_ceiling(**{**kw, "ctx_max": None}) is None


@pytest.mark.parametrize("percent, prompt, expected", [
    (80, 84_733, (32_768, False)),    # Ryan's turn: 35,267 of room, the normal cap stands
    (80, 100_000, (20_000, False)),   # clamped to what is left under 120,000
    (60, 84_733, (5_267, False)),     # the same prompt under a 60% setting: 90,000 - 84,733
    (60, 60_000, (30_000, False)),
    (80, 119_500, (32_768, True)),    # 500 left: not a reply worth sending, compact first
    (60, 95_000, (32_768, True)),     # already over the line
])
def test_a_request_gets_the_room_under_the_ceiling(percent, prompt, expected):
    ceiling = TurnEngine.context_guard_ceiling(enabled=True, at_percent=percent, ctx_max=150_000, ctx_loaded=True)
    assert TurnEngine.guarded_max_tokens(max_tokens=32_768, ceiling=ceiling, prompt_tokens=prompt) == expected


def test_no_ceiling_or_no_prompt_count_leaves_the_request_alone():
    assert TurnEngine.guarded_max_tokens(max_tokens=4096, ceiling=None, prompt_tokens=10) == (4096, False)
    assert TurnEngine.guarded_max_tokens(max_tokens=4096, ceiling=100, prompt_tokens=None) == (4096, False)
    assert TurnEngine.context_guard_hit(ceiling=None, prompt_tokens=10, streamed_tokens=10**9) is False


@pytest.mark.parametrize("percent, streamed, hit", [(80, 19_999, False), (80, 20_000, True), (60, 0, True)])
def test_the_stream_is_cut_when_prompt_plus_streamed_reaches_the_ceiling(percent, streamed, hit):
    ceiling = TurnEngine.context_guard_ceiling(enabled=True, at_percent=percent, ctx_max=150_000, ctx_loaded=True)
    assert TurnEngine.context_guard_hit(ceiling=ceiling, prompt_tokens=100_000, streamed_tokens=streamed) is hit


# ── the turn loop ────────────────────────────────────────────────────────────

def _guarded_app(monkeypatch, tmp_path, percent, create):
    app = _agentic_app(monkeypatch, tmp_path, on_tool=lambda: None)
    app.settings.autocompact_enabled = True
    app.settings.autocompact_at_percent = percent
    app.settings.wake_after_compact = False
    app.settings.compact_keep_recent = 0
    app.settings.max_tokens_tools = 16_384
    app._kick_card_summary = lambda *args: None
    app._fetch_ctx_window = lambda: None

    async def ready(**kwargs):
        pass

    app._ensure_chat_ready = ready
    monkeypatch.setattr(app_mod.model_transport, "for_app", lambda _: SimpleNamespace(create=create))
    return app


async def _run(app, pilot, done):
    for _ in range(240):
        await pilot.pause(.05)
        if done() and not app._chat_running():
            break


@pytest.mark.parametrize("percent, used, expected", [(80, 110_000, 10_000), (60, 85_000, 5_000)])
@pytest.mark.asyncio
async def test_the_request_is_sent_with_max_tokens_clamped_to_the_room(monkeypatch, tmp_path, percent, used, expected):
    sent = []

    async def create(**kwargs):
        sent.append(kwargs)
        return _Stream([_Chunk(content="done")])

    app = _guarded_app(monkeypatch, tmp_path, percent, create)
    async with app.run_test(size=(100, 40)) as pilot:
        app.ctx_max, app.ctx_used, app.ctx_loaded = 150_000, used, True
        app._append({"role": "user", "content": "hello"})
        app._stream()
        await _run(app, pilot, lambda: len(sent) >= 1)
    assert sent[0].get("purpose", "turn") == "turn"
    assert sent[0]["max_tokens"] == expected            # 16,384 asked, the room under the ceiling sent
    assert not [s for s in app.said if "context guard" in s]


class _Endless:
    """A stream that never ends by itself and ignores max_tokens: 400 characters (100 tokens) a chunk."""

    def __init__(self):
        self.closed = False
        self.sent = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        import asyncio
        await asyncio.sleep(0.001)
        if self.closed or self.sent > 2000:      # 2000 chunks = 200,000 tokens: far past any ceiling here
            raise StopAsyncIteration
        self.sent += 1
        return _Chunk(reasoning_content="x" * 400)

    async def close(self):
        self.closed = True


@pytest.mark.parametrize("percent, used, room", [(80, 118_000, 2_000), (60, 88_000, 2_000)])
@pytest.mark.asyncio
async def test_a_stream_that_ignores_max_tokens_is_cut_at_the_guard_then_compacted(monkeypatch, tmp_path, percent, used, room):
    calls, streams = [], []

    async def create(**kwargs):
        calls.append(kwargs.get("purpose", "turn"))
        if len(calls) == 1:
            streams.append(_Endless())
            return streams[0]
        if calls[-1] == "compaction":
            return _Stream([_Chunk(content="Summary: the task so far.")])
        return _Stream([_Chunk(content="resumed and finished")])

    app = _guarded_app(monkeypatch, tmp_path, percent, create)
    async with app.run_test(size=(100, 40)) as pilot:
        app.ctx_max, app.ctx_used, app.ctx_loaded = 150_000, used, True
        for i in range(6):   # something older for the compaction to summarise
            app._append({"role": "user" if i % 2 == 0 else "assistant", "content": f"earlier message {i} " * 40})
        app._append({"role": "user", "content": "think hard"})
        app._stream()
        await _run(app, pilot, lambda: len(calls) >= 3)
    stream = streams[0]
    assert stream.closed                                         # the stream was ended, as Esc ends one
    # Cut when prompt + streamed reached the ceiling: `room` tokens at 100 a chunk, never the endless rest.
    assert room // 100 <= stream.sent <= room // 100 + 2, stream.sent
    assert f"[stopped at the {percent}% context guard, compacting]" in app.said
    assert not [s for s in app.said if "compacting between tool iterations" in s]   # one line says why, not two
    assert calls[:2] == ["turn", "compaction"], calls            # handed to the existing compaction path


@pytest.mark.asyncio
async def test_too_little_room_compacts_before_sending(monkeypatch, tmp_path):
    calls = []

    async def create(**kwargs):
        calls.append(kwargs.get("purpose", "turn"))
        if calls[-1] == "compaction":
            return _Stream([_Chunk(content="Summary: the task so far.")])
        return _Stream([_Chunk(content="answered after compacting")])

    app = _guarded_app(monkeypatch, tmp_path, 80, create)
    async with app.run_test(size=(100, 40)) as pilot:
        app.ctx_max, app.ctx_used, app.ctx_loaded = 150_000, 119_500, True      # 500 tokens under the line
        for i in range(6):
            app._append({"role": "user" if i % 2 == 0 else "assistant", "content": f"earlier message {i} " * 40})
        app._append({"role": "user", "content": "one more thing"})
        app._stream()
        await _run(app, pilot, lambda: len(calls) >= 2)
    assert calls[0] == "compaction", calls                       # nothing was sent to the model first
    assert "[80% context guard: no room left for a reply, compacting first]" in app.said


@pytest.mark.asyncio
async def test_a_second_stop_with_no_smaller_prompt_sends_nothing_more(monkeypatch, tmp_path):
    """The guard stopped a turn, the compaction made no room, and the next request would stop the same way."""
    calls = []

    async def create(**kwargs):
        calls.append(kwargs.get("purpose", "turn"))
        return _Stream([_Chunk(content="should not be asked")])

    app = _guarded_app(monkeypatch, tmp_path, 80, create)
    async with app.run_test(size=(100, 40)) as pilot:
        app.ctx_max, app.ctx_used, app.ctx_loaded = 150_000, 119_500, True
        app._guard_last_exit = 119_500        # the last guard stop saw this same prompt
        app._append({"role": "user", "content": "one more thing"})
        app._stream()
        await _run(app, pilot, lambda: bool([s for s in app.said if "context guard" in s]))
    assert calls == []
    assert [s for s in app.said if s.startswith("[stopped at the 80% context guard again: compacting made no room")]
    assert app._guard_last_exit is None       # said once; a later attempt starts fresh
