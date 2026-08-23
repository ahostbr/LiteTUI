"""The six glass-box channels, fired from the real turn — plus the ambient one.

Tier 1 of docs/Plans/2026-08-21-glass-box-thermal-brain/wiring.md. The brain's
channels were designed 1:1 against events LiteTUI already produces, so this adds
NO new signal — only a transport. Each test drives the actual stream loop and
asserts the channel fires, because a source-level grep for `_glassbox(` would
pass just as well if the call sat in a branch that never runs.

TWO PROPERTIES THE HELPER MUST HAVE, and both have their own test:

  COSTS NOTHING WHEN NOBODY IS WATCHING. The token branches run hundreds of
  times per turn. If the helper does bookkeeping before checking for observers,
  every user pays for a plugin they have not installed. It short-circuits on an
  empty observer list before touching anything else.

  THROTTLES THE CONTINUOUS CHANNELS AND NEVER THE DISCRETE ONES. thinking and
  output arrive per token; their intensity is tok/s, which cannot meaningfully
  change between two tokens, so emitting per token would be flooding the
  observer to say the same thing. tool_call, store_write, window_fill, context
  and ledger are events — dropping one loses the event itself, so they are never
  throttled. Getting this backwards in either direction is a real bug: throttle
  a discrete channel and the brain misses a tool call; do not throttle a
  continuous one and a plugin gets thousands of duplicate events per turn.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as app_mod
from litetui.plugins import PluginContext
from litetui.settings import Settings


# ── the same stream doubles the compaction tests use ────────────────────────
class _Delta:
    def __init__(self, content=None, reasoning=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning
        self.reasoning = None
        self.tool_calls = tool_calls


class _Chunk:
    def __init__(self, **kw):
        self.choices = [type("C", (), {"delta": _Delta(**kw)})()]
        self.usage = None


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        self._it = iter(self._chunks)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration

    async def close(self):
        pass


def _scripted(chunks):
    async def create(**kw):
        return _Stream(chunks)
    return create


def _app(**overrides):
    a = app_mod.LiteTUI()
    base = dict(wake_after_compact=False, show_thinking=True)
    base.update(overrides)
    a.settings = Settings(**base)
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _watch(a) -> list[dict]:
    """Register an observer the way a plugin would, and collect."""
    seen: list[dict] = []
    PluginContext(app=a, registry=a.plugins, owner="test-watcher").observe(seen.append)
    return seen


def _channels(events: list[dict]) -> set[str]:
    return {e["channel"] for e in events}


async def _settle(pilot, ticks=10):
    for _ in range(ticks):
        await pilot.pause()
        await asyncio.sleep(0)


# ── the continuous pair ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_thinking_delta_fires_the_thinking_channel() -> None:
    a = _app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        seen = _watch(a)
        a.client = type("C", (), {"chat": type("X", (), {"completions": type(
            "Y", (), {"create": staticmethod(_scripted([_Chunk(reasoning="mulling ")]))})()})()})()
        a.conversation = [{"role": "system", "content": "s"}]
        a._append({"role": "user", "content": "go"})
        a._stream()
        await _settle(pilot)

    assert "thinking" in _channels(seen), (
        f"reasoning_content deltas did not fire the thinking channel: {_channels(seen)}"
    )


@pytest.mark.asyncio
async def test_a_content_delta_fires_the_output_channel() -> None:
    a = _app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        seen = _watch(a)
        a.client = type("C", (), {"chat": type("X", (), {"completions": type(
            "Y", (), {"create": staticmethod(_scripted([_Chunk(content="hello")]))})()})()})()
        a.conversation = [{"role": "system", "content": "s"}]
        a._append({"role": "user", "content": "go"})
        a._stream()
        await _settle(pilot)

    assert "output" in _channels(seen), (
        f"content deltas did not fire the output channel: {_channels(seen)}"
    )


@pytest.mark.asyncio
async def test_the_turn_fires_context_before_it_streams() -> None:
    """Prompt assembly is a real event — it is where the messages get folded
    and where the request's size is decided."""
    a = _app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        seen = _watch(a)
        a.client = type("C", (), {"chat": type("X", (), {"completions": type(
            "Y", (), {"create": staticmethod(_scripted([_Chunk(content="hi")]))})()})()})()
        a.conversation = [{"role": "system", "content": "s"}]
        a._append({"role": "user", "content": "go"})
        a._stream()
        await _settle(pilot)

    assert "context" in _channels(seen), f"no context event: {_channels(seen)}"
    ctx = next(e for e in seen if e["channel"] == "context")
    assert ctx["label"], "the context event carries no label to render"


# ── the discrete ones ───────────────────────────────────────────────────────
def test_a_tool_dispatch_fires_tool_call_with_its_name() -> None:
    a = _app()
    seen = _watch(a)
    a._glassbox_tool("bash")

    ev = [e for e in seen if e["channel"] == "tool_call"]
    assert ev, f"no tool_call event: {_channels(seen)}"
    assert "bash" in ev[0]["label"], "the tool name is not in the label"


def test_the_write_tool_fires_store_write_not_tool_call() -> None:
    """A store write is its own channel in the design — it is the agent
    changing durable state, which is not the same event as calling a tool."""
    a = _app()
    seen = _watch(a)
    a._glassbox_tool("write")

    assert "store_write" in _channels(seen), f"write did not route: {_channels(seen)}"
    assert "tool_call" not in _channels(seen), (
        "write fired BOTH channels — the brain would double-count it"
    )


def test_the_context_window_fill_fires_the_ambient_channel() -> None:
    """The brain's base luminance. A filling window literally brightens it."""
    a = _app()
    seen = _watch(a)
    a.ctx_max = 1000
    a.ctx_used = 250

    ev = [e for e in seen if e["channel"] == "window_fill"]
    assert ev, f"ctx_used did not fire window_fill: {_channels(seen)}"
    assert abs(ev[-1]["intensity"] - 0.25) < 0.01, (
        f"fill fraction is wrong: {ev[-1]['intensity']}"
    )


def test_window_fill_is_silent_until_the_window_size_is_known() -> None:
    """Negative control for the test above. Without ctx_max there is no
    fraction to report, and 'used/None' must not become a divide or a zero."""
    a = _app()
    seen = _watch(a)
    a.ctx_max = None
    a.ctx_used = 250

    assert "window_fill" not in _channels(seen), (
        "reported a fill fraction with no window size to divide by"
    )


# ── the helper's two load-bearing properties ────────────────────────────────
def test_the_helper_costs_nothing_when_nobody_is_watching() -> None:
    """No observers means no bookkeeping at all — not even the throttle clock.
    The token branches run hundreds of times a turn and must not pay for a
    plugin that is not installed."""
    a = _app()
    # A real app LOADS the glassbox plugin, so it always has an observer — the
    # full suite caught this while isolation did not. Establish the stated
    # precondition instead of assuming it.
    a.plugins.observers.clear()
    a._gb_last.clear()

    for _ in range(50):
        a._glassbox("output", 1.0, "x")

    assert a._gb_last == {}, (
        "the throttle recorded state with no observers — the short circuit is "
        "after the bookkeeping instead of before it"
    )


def test_the_continuous_channels_are_throttled() -> None:
    a = _app()
    seen = _watch(a)

    for _ in range(50):
        a._glassbox("output", 1.0, "x")

    assert len(seen) < 50, "every token emitted an event — the observer is flooded"
    assert len(seen) >= 1, "the throttle swallowed the channel entirely"


def test_the_discrete_channels_are_never_throttled() -> None:
    """The inverse bug, and the one that loses data: a throttled tool_call
    means the brain misses a tool the agent actually ran."""
    a = _app()
    seen = _watch(a)

    for i in range(5):
        a._glassbox("tool_call", 1.0, f"tool-{i}", discrete=True)

    got = [e for e in seen if e["channel"] == "tool_call"]
    assert len(got) == 5, f"a discrete event was throttled away: {len(got)} of 5"
    assert [e["label"] for e in got] == [f"tool-{i}" for i in range(5)]


def test_the_throttle_opens_again_after_its_interval() -> None:
    """Otherwise 'throttled' would mean 'fires once per turn and then stops',
    and a long answer would show one flicker at the start."""
    a = _app()
    seen = _watch(a)

    a._glassbox("output", 1.0, "first")
    a._gb_last["output"] = time.monotonic() - (app_mod.GLASSBOX_MIN_INTERVAL_S + 0.01)
    a._glassbox("output", 1.0, "second")

    assert len(seen) == 2, "the throttle never reopened"


def test_every_event_carries_the_three_fields_the_brain_reads() -> None:
    """GlassBoxBrain.fire({channel, intensity, label}). A missing key is a
    KeyError inside an observer, which emit() swallows — so the brain would go
    dark with no error anywhere."""
    a = _app()
    seen = _watch(a)
    a._glassbox_tool("bash")
    a._glassbox("output", 0.5, "x")

    for e in seen:
        assert {"channel", "intensity", "label"} <= set(e), f"malformed event: {e}"
        assert isinstance(e["intensity"], (int, float)), e
        assert isinstance(e["label"], str), e


# ── the ledger, driven through a real compaction ─────────────────────────────
@pytest.mark.asyncio
async def test_a_compaction_fires_the_ledger_with_its_real_numbers() -> None:
    """The one channel carrying MEASUREMENTS rather than a level, so it is the
    one where a placeholder would be silently wrong. It is emitted from the
    same locals the card's own line is built from, and this asserts the numbers
    agree with the conversation rather than merely being present."""
    a = _app(clear_screen_after_compact=False, compact_keep_recent=2)
    async with a.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        seen = _watch(a)
        a.conversation = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "the ancient history"},
            {"role": "assistant", "content": "long reply about it"},
            {"role": "user", "content": "recent question"},
            {"role": "assistant", "content": "recent answer"},
        ]
        before = len(a.conversation)
        a.client.chat.completions.create = _scripted([_Chunk(content="a summary")])
        a._compact()
        await _settle(pilot, ticks=14)

        ev = [e for e in seen if e["channel"] == "ledger"]
        assert ev, f"a compaction fired no ledger event: {_channels(seen)}"
        label = ev[-1]["label"]
        assert str(before) in label, f"the BEFORE count is not in the label: {label!r}"
        assert str(len(a.conversation)) in label, (
            f"the AFTER count does not match the conversation: {label!r} "
            f"vs {len(a.conversation)}"
        )
        assert "%" in label, f"the ledger carries no delta: {label!r}"
