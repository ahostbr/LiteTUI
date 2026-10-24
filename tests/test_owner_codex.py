"""Owner mode on Codex: a driven STEP 1 turn, the gate, then thread/compact/start.

Codex compacts natively and has no STEP 1, so a manual /compact first sends ONE
normal user turn (the owner STEP 1 text) through the engine, waits for it, runs
`owner_gate`, and only then compacts. Its own auto-compaction cannot be
intercepted; a 70% reminder (every backend, owner mode) is the cue to /compact
first. The engine is mocked: scripted chunks at the transport seam, no model, no
backend started.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from test_compaction_ui import _Chunk, _run, _scripted_create, _settle, _Stream, _TC, off_local_lm_studio

from litetui import app as app_mod
from litetui import model_transport, owner_mode, settings
from litetui.agent_launch_context import ordinary
from litetui.settings import Settings

NEW_NOW = "NEW-NOW-LINE-BETA"
REFUSAL = "HANDOFF REFUSED: NOW.md does not pass the template: NOW.md has no 'Doing now' heading. Nothing was committed."


@pytest.fixture
def owner_dir(tmp_path):
    folder = tmp_path / "widgets-memory"
    folder.mkdir()
    for name in ("SOUL.md", "NOW.md", "DOMAIN.md"):
        (folder / name).write_text(name + "\n", encoding="utf-8")
    hour_ago = time.time() - 3600
    for name in ("NOW.md", "DOMAIN.md"):
        os.utime(folder / name, (hour_ago, hour_ago))
    return folder


class Handoff:
    def __init__(self, events, outcomes=(True,)):
        self.events, self.outcomes, self.calls = events, list(outcomes), []

    def __call__(self, memory_dir, agent_id=None, timeout=120.0):
        self.calls.append((memory_dir, agent_id))
        self.events.append("handoff")
        ok = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        return (True, "/compact") if ok else (False, REFUSAL)


@pytest.fixture
def make_app(tmp_path, owner_dir, monkeypatch):
    made = []

    async def ready(self, *, timeout=None):
        return None

    monkeypatch.setattr(app_mod.LiteTUI, "_validate_owned_execution", lambda self, **kw: None)
    monkeypatch.setattr(app_mod.LiteTUI, "_ensure_chat_ready", ready)

    def build(*, owner: bool = True, codex: bool = True) -> app_mod.LiteTUI:
        session = ordinary(tmp_path, settings.load())
        app = app_mod.LiteTUI(agent_session=session, owner_memory_dir=owner_dir if owner else None)
        made.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._materialise_convo()
        app.settings = Settings(clear_screen_after_compact=False, compact_keep_recent=2, wake_after_compact=False)
        off_local_lm_studio(app)
        if codex:
            app.backend.app_server = object()  # what marks a Codex backend in LiteTUI
        return app

    yield build
    for app in reversed(made):
        try:
            app.store.release()
        finally:
            app._agent_session.release()


def _write_call(owner_dir):
    args = json.dumps({"path": str(owner_dir / "NOW.md"), "content": NEW_NOW})
    return [_Chunk(tool_calls=[_TC(0, id="c1", name="write")]), _Chunk(tool_calls=[_TC(0, arguments=args)])]


def _reply(text="NOW.md is written."):
    return [_Chunk(content=text)]


def _model(owner_dir, writes):
    """A mocked model keyed on WHAT IT IS ASKED, not on call order: the turn loop also makes
    side calls (titles, summaries) that would eat a positional script.

    `writes[k]` says whether the k-th driven owner turn rewrites NOW.md (a `write` tool call,
    then a one-line reply) or just replies. Anything else gets a plain reply.
    """
    calls = []

    async def create(**kw):
        calls.append(kw)
        messages = kw["messages"]
        last = messages[-1]
        if last["role"] == "tool":
            return _Stream(_reply())
        driven = [m for m in messages if m["role"] == "user"
                  and ("owner memory folder" in str(m["content"]) or "was NOT committed" in str(m["content"]))]
        if driven and last is driven[-1]:
            wrote = writes[min(len(driven) - 1, len(writes) - 1)]
            return _Stream(_write_call(owner_dir) if wrote else _reply("done"))
        return _Stream(_reply("side call"))
    return create, calls


def _driven(calls):
    """The requests that carried a driven owner turn as their last message."""
    return [c for c in calls if c["messages"][-1]["role"] == "user"
            and ("owner memory folder" in str(c["messages"][-1]["content"])
                 or "was NOT committed" in str(c["messages"][-1]["content"]))]


def _drive(app, writes, owner_dir, monkeypatch, handoff, *, after=None, wrote_files=True):
    """One /compact on a Codex-marked owner app. Returns (events, create calls, system messages)."""
    events = handoff.events
    create, calls = _model(owner_dir, writes) if writes is not None else _scripted_create([_reply()])
    system = []
    real = model_transport.for_app(app)

    class Engine:
        """The Codex transport seam: scripted turn streams, a recorded thread/compact/start."""

        async def create(self, **kwargs):
            events.append("turn")
            return await real.create(**kwargs)

        async def compact(self, **_kw):
            events.append("compact")

    async def execute(name, args, hooks_enabled=True):
        if wrote_files and name == "write":
            (owner_dir / "NOW.md").write_text(NEW_NOW + "\n", encoding="utf-8")
            events.append("write")
        return "ok", True

    monkeypatch.setattr(model_transport, "for_app", lambda _app: Engine())
    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    app._execute_tool = execute
    app._system = lambda text, *a, **k: system.append(text)

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            app.conversation = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "the ancient history"},
                {"role": "assistant", "content": "long reply about it"},
            ]
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot, ticks=30)
            if after:
                after(app)
    _run(body())
    return events, calls, system


def _owner_turns(app):
    return [m for m in app.conversation if m.get("role") == "user" and "owner memory folder" in str(m.get("content"))]


def test_one_driven_turn_then_verify_then_commit_then_compact(make_app, owner_dir, monkeypatch):
    app = make_app()
    handoff = Handoff([])
    seen = {}
    events, calls, system = _drive(app, (True,), owner_dir, monkeypatch, handoff,
                                   after=lambda a: seen.update(turns=len(_owner_turns(a))))
    assert [e for e in events if e != "turn"] == ["write", "handoff", "compact"], "turn, verify, commit, compact"
    assert events.index("handoff") < events.index("compact") and events.index("write") < events.index("handoff")
    assert seen["turns"] == 1, "exactly one driven user turn"
    assert len(handoff.calls) == 1 and not any("NOT committed" in m for m in system)


def test_the_driven_turn_is_the_owner_step_one_without_a_summary(make_app, owner_dir, monkeypatch):
    app = make_app()
    _events, calls, _system = _drive(app, (True,), owner_dir, monkeypatch, Handoff([]))
    text = _driven(calls)[0]["messages"][-1]["content"]
    assert str(owner_dir).replace("\\", "/") in text and "NOW.md" in text and "Do NOT redo" in text
    assert "STEP 2" not in text and "do not write a summary" in text


def test_a_note_not_rewritten_is_retried_once_and_never_committed_stale(make_app, owner_dir, monkeypatch):
    app = make_app()
    handoff = Handoff([])
    events, calls, system = _drive(app, (False, True), owner_dir, monkeypatch, handoff)
    assert [e for e in events if e != "turn"] == ["write", "handoff", "compact"],         "the stale note never reached the handoff; it ran once, after the rewrite"
    retry = _driven(calls)[1]["messages"][-1]["content"]
    assert "was not rewritten during this compaction" in retry and "reply with one short line" in retry
    assert any("was not saved" in m for m in system)


def test_a_refused_handoff_retries_once_then_compacts_and_says_so(make_app, owner_dir, monkeypatch):
    app = make_app()
    handoff = Handoff([], outcomes=(False,))
    events, calls, system = _drive(app, (True, True), owner_dir, monkeypatch, handoff)
    assert [e for e in events if e != "turn"] == ["write", "handoff", "write", "handoff", "compact"]
    assert "no 'Doing now' heading" in _driven(calls)[1]["messages"][-1]["content"], "the retry names the reason"
    assert len(handoff.calls) == 2, "one retry, no more"
    note = [m for m in system if m.startswith("NOW.md was NOT committed:")]
    assert len(note) == 1 and "no 'Doing now' heading" in note[0]
    assert system.index(note[0]) > max(i for i, m in enumerate(system) if "Codex context compacted" in m)


def test_a_stopped_driven_turn_does_not_compact(make_app, owner_dir, monkeypatch):
    app = make_app()
    handoff = Handoff([])
    create, _calls = _scripted_create([_reply()])
    events = handoff.events
    real = model_transport.for_app(app)
    system = []

    class Engine:
        async def create(self, **kwargs):
            app._stop_requested = True  # Esc during the driven turn
            return await real.create(**kwargs)

        async def compact(self, **_kw):
            events.append("compact")

    monkeypatch.setattr(model_transport, "for_app", lambda _app: Engine())
    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    app._system = lambda text, *a, **k: system.append(text)

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            app.conversation = [{"role": "system", "content": "sys"}, {"role": "user", "content": "x"},
                                {"role": "assistant", "content": "y"}]
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot, ticks=30)
    _run(body())
    assert "compact" not in events and handoff.calls == []
    assert any("Stopped; the context was not compacted" in m for m in system)


def test_a_non_owner_codex_compact_is_unchanged(make_app, owner_dir, monkeypatch):
    app = make_app(owner=False)
    handoff = Handoff([])
    events, calls, system = _drive(app, None, owner_dir, monkeypatch, handoff)
    assert events == ["compact"] and _driven(calls) == [] and handoff.calls == []
    assert not any("NOW.md" in m for m in system)


# ===================== the 70% reminder, on every backend =====================

def _reminders(system):
    return [m for m in system if m == owner_mode.CONTEXT_REMINDER]


def _climb(app, values):
    system = []
    app._system = lambda text, *a, **k: system.append(text)
    app.ctx_max = 1000

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            app._system = lambda text, *a, **k: system.append(text)
            app.ctx_max = 1000
            for value in values:
                app.ctx_used = value
                await pilot.pause()
    _run(body())
    return system


@pytest.mark.parametrize("codex", [True, False])
def test_the_reminder_fires_once_per_crossing_not_per_turn(make_app, codex):
    app = make_app(codex=codex)
    system = _climb(app, [100, 500, 699, 700, 700, 750, 900, 900, 950])
    assert len(_reminders(system)) == 1


@pytest.mark.parametrize("codex", [True, False])
def test_dropping_below_seventy_re_arms_it(make_app, codex):
    app = make_app(codex=codex)
    system = _climb(app, [720, 800, 150, 400, 710, 760])
    assert len(_reminders(system)) == 2, "up through 70, compaction, up through 70 again"


def test_the_reminder_text_is_the_agreed_one():
    assert owner_mode.CONTEXT_REMINDER == "context 70%: run /compact to write NOW.md first"


def test_there_is_no_reminder_without_owner_mode(make_app):
    app = make_app(owner=False)
    system = _climb(app, [100, 800, 950])
    assert _reminders(system) == []


def test_an_unknown_window_never_reminds(make_app):
    app = make_app()
    system = []

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            app._system = lambda text, *a, **k: system.append(text)
            app.ctx_max = None
            app.ctx_used = 900_000
            await pilot.pause()
    _run(body())
    assert _reminders(system) == []


def test_a_refused_admission_of_the_driven_turn_runs_no_turn_and_no_compaction(make_app, owner_dir, monkeypatch):
    from litetui import hook_host
    monkeypatch.setattr(hook_host, "accept_prompt", lambda app, item: False)
    app = make_app()
    handoff = Handoff([])
    events, calls, system = _drive(app, (True,), owner_dir, monkeypatch, handoff)
    assert "turn" not in events and "compact" not in events and handoff.calls == []
    assert any("not admitted" in m and "not compacted" in m for m in system)
