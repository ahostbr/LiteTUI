"""LiteTUI's own compaction on the Claude backend (Ryan 2026-09-24).

"I would rather keep [LiteTUI]-wise compaction" / "I want to only change
Claude". Fakes stand at the SDK boundary and at the Textual card, so these
drive the real claude_compact / claude_turn / claude_persistence code.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import ClassVar

import pytest

from litetui import claude_cache, claude_compact, widgets
from litetui.claude_events import ClaudeEvent
from litetui.claude_turn import ledger_for


class Card:
    """Only what claude_compact uses of CompactionCard."""

    made: ClassVar[list] = []

    def __init__(self, plan, prompt_text, auto=False):
        self.plan, self.prompt_text, self.auto = plan, prompt_text, auto
        self.body = SimpleNamespace(content=None, set_markdown=lambda text: setattr(self, "markdown", text))
        self.thought, self.tools, self.status = "", [], ""
        self.finished = self.failed = self.markdown = None
        Card.made.append(self)

    def think(self, token):
        self.thought += token

    def thinking_done(self):
        pass

    def add_tool(self, msg):
        self.tools.append(msg)

    def set_status(self, text):
        self.status = text

    def record_round(self, timing):
        pass

    def finish(self, ledger):
        self.finished = ledger

    def fail(self, reason):
        self.failed = reason


class Session:
    def __init__(self, events):
        self.session_id = "native-old"
        self.effort = None
        self.lifecycle = SimpleNamespace(failure=None, active_turn=False)
        self.queried = []
        self._events = events

    async def set_model(self, model):
        pass

    async def query(self, turn_id, prompt):
        self.queried.append(prompt)

    async def interrupt(self):
        pass

    async def events(self):
        for batch in self._events:
            yield batch


class Backend:
    name = "claude"
    owns_native_turns = True

    def __init__(self, session, segment_id):
        self.session, self.segment_id, self.closes = session, segment_id, 0
        self._claude_tools = SimpleNamespace(cancelled=SimpleNamespace(clear=lambda: None), stop=lambda: None)
        self._claude_events = SimpleNamespace(reset_turn=lambda: None, push=lambda batch: batch)

    def reasoning_levels(self, key):
        return []

    async def close(self):
        self.closes += 1
        self.session = None


class ChatLog:
    async def mount(self, card):
        pass


def app_for(tmp_path, events, answer=True, monkeypatch=None):
    app = SimpleNamespace(
        convo_dir=tmp_path, convo_id="convo", model_id="claude-opus-5-5", thinking_level=None,
        settings=SimpleNamespace(model_infer_overrides={}, clear_screen_after_compact=False,
                                 wake_after_compact=False),
        conversation=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}],
        ctx_used=90_000, notices=[], emitted=[], truncated=[], _rpc=None,
        _materialise_convo=lambda: None, _chat_running=lambda: False, _scroll_down=lambda: None,
        _elapsed=SimpleNamespace(ensure_running=lambda: None),
        query_one=lambda selector: ChatLog(),
    )
    app._system = app.notices.append
    app._emit_compaction = lambda reason, **kw: app.emitted.append((reason, kw))
    app._truncate = lambda keep_from, prepend, reason, measurements=None: app.truncated.append(
        (keep_from, prepend, reason, measurements))
    app._msg_chars = lambda msgs: sum(len(str(m.get("content", ""))) for m in msgs)
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(tmp_path))
    ledger.bind_session(segment["id"], "native-old")
    app.backend = Backend(Session(events), segment["id"])
    asked = []

    async def confirm(app_, cold):
        asked.append(cold)
        return answer
    monkeypatch.setattr(claude_cache, "confirm_cold", confirm)
    monkeypatch.setattr(widgets, "CompactionCard", Card)
    Card.made.clear()
    return app, asked


def summary_events(text="Goal: ship X. Files: a.py. Open: tests."):
    return [[ClaudeEvent(kind="text_delta", text=text[:10], message_id="m1")],
            [ClaudeEvent(kind="message", text=text, message_id="m1", reconcile="replace")],
            [ClaudeEvent(kind="result", is_error=False, text=text, data={"subtype": "success"})]]


@pytest.mark.asyncio
async def test_manual_compact_asks_first_and_cancel_changes_nothing(tmp_path, monkeypatch):
    app, asked = app_for(tmp_path, summary_events(), answer=False, monkeypatch=monkeypatch)
    before = ledger_for(app).selected["id"]
    await claude_compact.compact(app, "")
    assert [kind for kind, _ in asked] == ["compact"]
    assert "full input price" in asked[0][1]
    assert app.backend.session.queried == [] and app.backend.closes == 0
    assert ledger_for(app).selected["id"] == before and not Card.made and not app.emitted


@pytest.mark.asyncio
async def test_compact_summarises_in_the_live_session_then_seeds_a_fresh_one(tmp_path, monkeypatch):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    session = app.backend.session
    old = ledger_for(app).selected
    await claude_compact.compact(app, "keep the parser notes")
    assert session.queried == [claude_compact.request("keep the parser notes")], "ours, in Claude's own session"
    card = Card.made[0]
    assert card.markdown.startswith("Goal: ship X") and card.finished and not card.failed
    assert app.backend.closes == 1, "the summarised session is closed"
    new = ledger_for(app).selected
    assert new["id"] != old["id"] and new["session_id"] is None
    assert new["seed"].startswith("Goal: ship X")
    reason, kw = app.emitted[-1]
    assert reason == "compacted" and kw["tokens_before"] == 90_000 and kw["tokens_after_exact"] is False
    assert app.truncated[0][2] == "compact" and app.ctx_used == kw["tokens_after"]
    assert app.conversation[0]["content"].startswith("[Summary of earlier conversation")


@pytest.mark.asyncio
async def test_auto_compact_does_not_ask(tmp_path, monkeypatch):
    app, asked = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    await claude_compact.compact(app, "", auto=True)
    assert asked == [] and Card.made[0].auto and app.emitted[-1][0] == "compacted"


@pytest.mark.asyncio
async def test_a_failed_summary_leaves_the_session_and_segment_alone(tmp_path, monkeypatch):
    events = [[ClaudeEvent(kind="result", is_error=True, detail="overloaded", data={})]]
    app, _ = app_for(tmp_path, events, monkeypatch=monkeypatch)
    before = ledger_for(app).selected["id"]
    await claude_compact.compact(app, "")
    assert Card.made[0].failed and app.backend.closes == 0
    assert ledger_for(app).selected["id"] == before and app.emitted[-1][0] == "failed"
    assert any("overloaded" in n for n in app.notices)


def test_the_seed_rides_in_the_system_prompt_of_every_open_of_its_segment(monkeypatch):
    """In the user channel the fresh session refused the summary as an
    injection (live); the system prompt is the trusted channel."""
    from litetui import claude_backend as cb

    seen = []
    monkeypatch.setattr(cb, "sdk_module", lambda: SimpleNamespace(ClaudeAgentOptions=lambda **kw: seen.append(kw) or kw))

    class FakeSession:
        def __init__(self, options):
            self.options = options

        async def start(self):
            return {}
    monkeypatch.setattr(cb, "ClaudeSession", FakeSession)
    backend = cb.ClaudeBackend(SimpleNamespace(claude_executable=""))
    backend.models = {"sonnet": {"value": "sonnet"}}
    seg = {"id": "s1", "workspace": ".", "session_id": None, "seed": "Goal: ship X."}
    asyncio.run(backend.open_session(seg, "sonnet"))
    append = seen[-1]["system_prompt"]["append"]
    assert append.startswith(cb.APPEND) and append.endswith("Goal: ship X.")
    backend.session = None
    asyncio.run(backend.open_session({**seg, "session_id": "native-new"}, "sonnet"))
    assert seen[-1]["system_prompt"]["append"] == append, "a resume carries the same prefix"
    backend.session = None
    asyncio.run(backend.open_session({"id": "s2", "workspace": ".", "session_id": None}, "sonnet"))
    assert seen[-1]["system_prompt"]["append"] == cb.APPEND


def test_the_request_is_marked_and_carries_litetuis_summary_rules_without_the_store_step():
    from litetui.claude_backend import APPEND, COMPACT_MARKER

    text = claude_compact.request("focus on the parser")
    assert text.startswith(COMPACT_MARKER) and COMPACT_MARKER in APPEND
    assert "Cover: what the user is trying to achieve" in text and text.endswith("focus on the parser")
    assert "soul.md" not in text and "memory.md" not in text


def test_the_seed_survives_a_reload_of_the_ledger(tmp_path):
    from litetui.claude_persistence import ClaudeLedger

    ClaudeLedger(tmp_path).select_segment("ws", new=True, seed="Goal: ship X.")
    assert ClaudeLedger(tmp_path).selected["seed"] == "Goal: ship X."


def test_claudes_own_autocompact_is_off_and_the_append_says_litetui_compacts(monkeypatch):
    from litetui import claude_backend as cb

    seen = {}
    monkeypatch.setattr(cb, "sdk_module", lambda: SimpleNamespace(ClaudeAgentOptions=lambda **kw: seen.update(kw)))
    backend = cb.ClaudeBackend.__new__(cb.ClaudeBackend)
    backend.settings = SimpleNamespace(claude_executable="")
    asyncio.run(backend._options(cwd="."))
    assert seen["env"]["DISABLE_AUTO_COMPACT"] == "1"
    assert seen["env"]["CLAUDE_CODE_PROMPT_CACHE_TTL"] == "1h"
    assert "owns compaction" in seen["system_prompt"]["append"]
    assert "Do not use host compaction" not in seen["system_prompt"]["append"]


def test_litetuis_threshold_now_decides_for_claude():
    from litetui.app import LiteTUI

    host = SimpleNamespace(backend=SimpleNamespace(name="claude", owns_native_turns=True),
                           settings=SimpleNamespace(autocompact_enabled=True, autocompact_at_percent=80),
                           ctx_max=1_000_000, ctx_used=850_000, ctx_loaded=True, _autocompact_failed_at=None)
    assert LiteTUI._autocompact_due(host) == 85
    host.backend = SimpleNamespace(name="codex", app_server=object())
    assert LiteTUI._autocompact_due(host) is None, "the Codex engine still owns its own"


def test_a_compaction_claude_does_on_its_own_uses_the_same_card(monkeypatch):
    monkeypatch.setattr(widgets, "CompactionCard", Card)
    Card.made.clear()
    notes, emitted = [], []
    app = SimpleNamespace(query_one=lambda s: SimpleNamespace(mount=lambda card: None),
                          _system=notes.append, _emit_compaction=lambda r, **kw: emitted.append((r, kw)))
    event = ClaudeEvent(kind="compaction", data={"compact_metadata": {"trigger": "auto", "pre_tokens": 950_000}})
    claude_compact.native_compaction(app, event)
    assert Card.made[0].auto and "950,000" in Card.made[0].finished
    assert emitted == [("compacted", {"tokens_before": 950_000, "tokens_before_exact": True})]
