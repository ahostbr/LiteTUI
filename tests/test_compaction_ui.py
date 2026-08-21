"""Glass-box compaction: the act renders, live, in the house grammar.

Everywhere else compaction is a spinner and a prayer. These tests pin the
whole visible contract: the card with its plan line, the exact prompt in a
collapsed fold, the summary STREAMING into the body, the model's store
writes as real tool cards, the ledger, and failure written on the card
rather than swallowed. The fakes speak the streaming chunk protocol —
_compact streams now, and that is the feature.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as app_mod
import paths
from settings import Settings

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-glassbox-"))


# ── streaming fakes ────────────────────────────────────────────────────────

class _Fn:
    def __init__(self, name=None, arguments=None):
        self.name = name
        self.arguments = arguments


class _TC:
    def __init__(self, index=0, id=None, name=None, arguments=None):
        self.index = index
        self.id = id
        self.function = _Fn(name, arguments)


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


def _scripted_create(rounds):
    """Each call to create() serves the next round's chunk list."""
    calls = []

    async def create(**kw):
        calls.append(kw)
        return _Stream(rounds[min(len(calls) - 1, len(rounds) - 1)])

    return create, calls


def _app(**overrides):
    a = app_mod.LiteTUI()
    base = dict(clear_screen_after_compact=False, compact_keep_recent=2,
                wake_after_compact=False)
    base.update(overrides)
    a.settings = Settings(**base)
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


def _seed(a):
    a.conversation = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "the ancient history"},
        {"role": "assistant", "content": "long reply about it"},
        {"role": "user", "content": "recent question"},
        {"role": "assistant", "content": "recent answer"},
    ]


async def _settle(a, pilot, ticks=8):
    for _ in range(ticks):
        await pilot.pause()
        await asyncio.sleep(0)


def _run(coro):
    return asyncio.run(coro)


def _card(a):
    cards = a.screen.query(app_mod.CompactionCard)
    assert cards, "no CompactionCard mounted — the glass box never appeared"
    return cards.last()


# ── the card ───────────────────────────────────────────────────────────────

def test_compaction_mounts_a_card_with_the_plan():
    async def body():
        create, _ = _scripted_create([[_Chunk(content="a summary")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            card = _card(a)
            plan = str(card._plan.render())
            assert "keeping the last 2" in plan
            assert "messages" in plan
    _run(body())


def test_the_exact_prompt_is_present_and_folded():
    """The prompt users were never allowed to see — present, but folded:
    its default view is the fold line, not a wall of instructions."""
    async def body():
        create, _ = _scripted_create([[_Chunk(content="s")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            fold = _card(a).prompt_fold
            assert isinstance(fold, app_mod.FoldBlock)
            assert not fold.expanded, "the prompt should start folded"
            held = str(fold.scroll.query_one(app_mod.Static).render())
            head = app_mod.COMPACT_PROMPT.strip()[:40]
            assert head in held, "the fold must hold the EXACT prompt sent"

            fold.set_expanded(True)
            assert fold.expanded
    _run(body())


def test_the_summary_streams_into_the_card_and_finalises_as_markdown():
    async def body():
        create, _ = _scripted_create([
            [_Chunk(content="part one, "), _Chunk(content="part two")]
        ])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            card = _card(a)
            assert "part one, part two" in str(card.body.render())
            # and the conversation was actually rebuilt around it
            assert any("part one, part two" in str(m.get("content", ""))
                       for m in a.conversation)
            status = str(card.status.render())
            assert "done" in status and "→" in status, status
    _run(body())


def test_model_thinking_during_compaction_is_shown_in_a_thinking_block():
    async def body():
        create, _ = _scripted_create([
            [_Chunk(reasoning="weighing what to keep"), _Chunk(content="s")]
        ])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            card = _card(a)
            assert card.thinking is not None, "reasoning arrived and no block"
            assert "weighing what to keep" in card.thinking._buffer
    _run(body())


def test_store_writes_render_as_tool_cards_and_reach_the_ledger(tmp_path):
    """The most interesting part of a compaction is the model deciding what
    is durable and writing it to the store — previously invisible."""
    async def body():
        rounds = [
            # round 1: the model calls write (streamed as deltas)
            [
                _Chunk(tool_calls=[_TC(0, id="c1", name="write")]),
                _Chunk(tool_calls=[_TC(0, arguments='{"path": "memory.md", ')]),
                _Chunk(tool_calls=[_TC(0, arguments='"content": "durable"}')]),
            ],
            # round 2: the summary
            [_Chunk(content="the distilled summary")],
        ]
        create, calls = _scripted_create(rounds)
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a.tools_enabled = True
            written = {}
            a._dispatch_for = lambda name: (
                (lambda args: written.update(args) or "ok") if name == "write" else None
            )
            a._compact()
            await _settle(a, pilot, ticks=12)

            card = _card(a)
            tools = card.query(app_mod.ToolMessage)
            assert tools, "the write never rendered as a tool card"
            tool = tools.first()
            assert tool.tool_name == "write"
            assert tool._result == "ok"
            assert written.get("path") == "memory.md", "the tool never ran"

            status = str(card.status.render())
            assert "persisted" in status and "memory.md" in status, status

            # the second round's request carried the tool result back
            assert len(calls) == 2
            roles = [m.get("role") for m in calls[1]["messages"]]
            assert "tool" in roles
    _run(body())


def test_failure_is_written_on_the_card_not_swallowed():
    async def body():
        async def boom(**kw):
            raise RuntimeError("server fell over")

        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            before = list(a.conversation)
            a.client.chat.completions.create = boom
            a._compact()
            await _settle(a, pilot)

            card = _card(a)
            assert card.has_class("failed")
            status = str(card.status.render())
            assert "failed" in status and "unchanged" in status
            assert a.conversation == before, "a failed compact must change nothing"
    _run(body())


def test_no_summary_after_all_rounds_fails_the_card():
    async def body():
        create, _ = _scripted_create([[_Chunk(content="")]])
        a = _app(compact_max_tool_iters=1)
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)

            assert _card(a).has_class("failed")
    _run(body())


def test_autocompact_marks_the_card_automatic():
    async def body():
        create, _ = _scripted_create([[_Chunk(content="s")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact_is_auto = True          # what _maybe_autocompact sets
            a._handle_command("/compact")
            await _settle(a, pilot)

            card = _card(a)
            assert card.auto
            assert "automatic" in str(card._title.render())
            assert a._compact_is_auto is False, "the flag must be consumed"
    _run(body())


def test_a_manual_compact_after_an_aborted_auto_is_not_marked_auto():
    """The flag is read-and-cleared at the METHOD HEAD: an autocompact that
    aborts early (nothing to compact) must not leak its flag onto the next
    manual compact."""
    async def body():
        create, _ = _scripted_create([[_Chunk(content="s")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            a.conversation = [{"role": "user", "content": "hi"}]   # too short
            a.client.chat.completions.create = create
            a._compact_is_auto = True
            a._handle_command("/compact")      # aborts: nothing to compact
            await _settle(a, pilot)

            _seed(a)
            a._handle_command("/compact")      # manual
            await _settle(a, pilot)

            assert _card(a).auto is False, "the auto flag leaked across compacts"
    _run(body())


def test_the_request_actually_streams():
    """The whole feature rests on stream=True — a silent revert to blocking
    calls would keep the card but kill the glass."""
    async def body():
        create, calls = _scripted_create([[_Chunk(content="s")]])
        a = _app()
        async with a.run_test(size=(120, 40)) as pilot:
            _seed(a)
            a.client.chat.completions.create = create
            a._compact()
            await _settle(a, pilot)
            assert calls and calls[0].get("stream") is True
    _run(body())
