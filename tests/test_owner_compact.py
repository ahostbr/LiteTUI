"""Owner mode: a compaction rewrites NOW.md, commits it, and only then truncates.

The order is the contract: STEP 1 (the seat rewrites NOW.md and appends to
DOMAIN.md), then `owner_gate` (NOW.md changed, `owner-handoff` committed), THEN the
history is truncated. One retry turn names the reason; after it, compaction goes
ahead and a persistent system message says NOW.md was NOT committed. Engines are
mocked; no model is loaded and no backend is started.
"""
from __future__ import annotations

import json
import os
import time

import pytest
from test_claude_compact import Backend, Card, Session, app_for, summary_events
from test_compaction_ui import _Chunk, _run, _scripted_create, _seed, _settle, _Stream, _TC, off_local_lm_studio

from litetui import app as app_mod
from litetui import claude_compact, owner_mode, settings
from litetui.agent_launch_context import ordinary
from litetui.claude_events import ClaudeEvent
from litetui.claude_turn import ledger_for
from litetui.settings import Settings

OLD_NOW = "OLD-NOW-LINE-ALPHA"
NEW_NOW = "NEW-NOW-LINE-BETA"
REFUSAL = "HANDOFF REFUSED: NOW.md does not pass the template: NOW.md has no 'Doing now' heading. Nothing was committed."


@pytest.fixture
def owner_dir(tmp_path):
    folder = tmp_path / "widgets-memory"
    folder.mkdir()
    for name, text in (("SOUL.md", "CHARTER"), ("NOW.md", OLD_NOW), ("DOMAIN.md", "DOMAIN")):
        (folder / name).write_text(text + "\n", encoding="utf-8")
    hour_ago = time.time() - 3600
    for name in ("NOW.md", "DOMAIN.md"):
        os.utime(folder / name, (hour_ago, hour_ago))
    return folder


def _write_now(folder, text=NEW_NOW):
    (folder / "NOW.md").write_text(text + "\n", encoding="utf-8")


class Handoff:
    """A scripted owner-handoff: each call appends to `events` and returns the next outcome."""

    def __init__(self, events, outcomes=(True,)):
        self.events, self.outcomes, self.calls = events, list(outcomes), []

    def __call__(self, memory_dir, agent_id=None, timeout=120.0):
        self.calls.append((memory_dir, agent_id))
        self.events.append("handoff")
        ok = self.outcomes[min(len(self.calls) - 1, len(self.outcomes) - 1)]
        if isinstance(ok, tuple):
            return ok  # (ok, text): lets a test give each refusal its own reason
        return (True, "/compact") if ok else (False, REFUSAL)


# =========================== the local (LM Studio-style) backend ===========================

@pytest.fixture
def make_app(tmp_path, owner_dir):
    made = []

    def build(*, owner: bool = True) -> app_mod.LiteTUI:
        session = ordinary(tmp_path, settings.load())
        app = app_mod.LiteTUI(agent_session=session, owner_memory_dir=owner_dir if owner else None)
        made.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._materialise_convo()
        app.settings = Settings(clear_screen_after_compact=False, compact_keep_recent=2, wake_after_compact=False)
        return off_local_lm_studio(app)

    yield build
    for app in reversed(made):
        try:
            app.store.release()
        finally:
            app._agent_session.release()


def _bypass_owned_seat(monkeypatch) -> None:
    async def ready(self, *, timeout=None):
        return None

    monkeypatch.setattr(app_mod.LiteTUI, "_validate_owned_execution", lambda self, **kw: None)
    monkeypatch.setattr(app_mod.LiteTUI, "_ensure_chat_ready", ready)


def _write_call(owner_dir, call_id="c1"):
    """A streamed `write` tool call that rewrites NOW.md."""
    args = json.dumps({"path": str(owner_dir / "NOW.md"), "content": NEW_NOW})
    return [_Chunk(tool_calls=[_TC(0, id=call_id, name="write")]), _Chunk(tool_calls=[_TC(0, arguments=args)])]


def _summary(text="a summary"):
    return [_Chunk(content=text)]


def _drive(app, rounds, owner_dir, monkeypatch, handoff, *, writes_on=("write",)):
    """Run one local compaction with scripted rounds; returns (events, create_calls, system messages)."""
    events = handoff.events
    create, calls = _scripted_create(rounds)
    system = []

    async def execute(name, args, hooks_enabled=True):
        if name in writes_on:
            _write_now(owner_dir)
            events.append("write")
        return "ok", True

    original_truncate = app._truncate

    def truncate(*a, **k):
        events.append("truncate")
        return original_truncate(*a, **k)

    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    app._execute_tool = execute
    app._truncate = truncate
    app._system = lambda text, *a, **k: system.append(text)

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            _seed(app)
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot, ticks=20)
    _run(body())
    return events, calls, system


def _last_user(call):
    return call["messages"][-1]["content"]


def test_step_one_then_handoff_then_truncate(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    events, calls, system = _drive(app, [_write_call(owner_dir), _summary()], owner_dir, monkeypatch, Handoff([]))
    assert events == ["write", "handoff", "truncate"], "STEP 1, the gate, then the history is cut"
    assert len(calls) == 2, "no retry when the gate passed"
    assert app.conversation[1]["content"].startswith("[Summary of earlier conversation")
    assert not any("NOT committed" in m for m in system)


def test_the_seat_is_told_to_rewrite_now_md_not_the_agent_home_files(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    _events, calls, _system = _drive(app, [_write_call(owner_dir), _summary()], owner_dir, monkeypatch, Handoff([]))
    prompt = _last_user(calls[0])
    assert str(owner_dir).replace("\\", "/") in prompt
    assert "NOW.md" in prompt and "Do NOT redo" in prompt and "DOMAIN.md" in prompt
    assert "pointer line to memory.md" not in prompt and "handoff.md" not in prompt


def test_the_handoff_is_given_the_seats_id_and_the_owner_folder(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([])
    agent_id = app._agent_session.authority.agent_id  # read before the app exits and releases the seat
    _drive(app, [_write_call(owner_dir), _summary()], owner_dir, monkeypatch, handoff)
    assert handoff.calls == [(owner_dir, agent_id)]


def test_t0_is_recorded_at_the_choke_point(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    before = time.time_ns()
    _drive(app, [_write_call(owner_dir), _summary()], owner_dir, monkeypatch, Handoff([]))
    assert isinstance(app._owner_compact_t0, int) and before <= app._owner_compact_t0 <= time.time_ns()


def test_a_note_that_was_not_rewritten_never_reaches_the_handoff(make_app, owner_dir, monkeypatch):
    """Summary first with NOW.md untouched: the mtime check fails, the handoff is not run,
    the seat is asked once, writes, and only then is the handoff run."""
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([])
    rounds = [_summary("first"), _write_call(owner_dir), _summary("second")]
    events, calls, system = _drive(app, rounds, owner_dir, monkeypatch, handoff)
    assert events == ["write", "handoff", "truncate"], "the handoff ran once, after the rewrite"
    assert len(calls) == 3
    assert "was not rewritten during this compaction" in _last_user(calls[1])
    assert any("was not saved" in m for m in system)
    assert not any("NOT committed" in m and "was not saved" not in m for m in system)
    assert "second" in app.conversation[1]["content"]


def test_a_refused_handoff_gets_one_retry_naming_the_reason(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([], outcomes=(False, True))
    rounds = [_write_call(owner_dir), _summary("first"), _write_call(owner_dir, "c2"), _summary("second")]
    events, calls, system = _drive(app, rounds, owner_dir, monkeypatch, handoff)
    assert events == ["write", "handoff", "write", "handoff", "truncate"]
    retry = _last_user(calls[2])
    assert "NOW.md was NOT committed:" in retry and "no 'Doing now' heading" in retry
    assert len(handoff.calls) == 2 and len(calls) == 4
    assert "second" in app.conversation[1]["content"]
    assert not any(m.startswith("NOW.md was NOT committed") for m in system)


def test_after_the_retry_compaction_goes_ahead_and_says_so_persistently(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([], outcomes=(False,))
    rounds = [_write_call(owner_dir), _summary("first"), _write_call(owner_dir, "c2"), _summary("second")]
    events, calls, system = _drive(app, rounds, owner_dir, monkeypatch, handoff)
    assert events == ["write", "handoff", "write", "handoff", "truncate"], "exactly one retry, then it proceeds"
    assert len(calls) == 4 and len(handoff.calls) == 2
    assert app.conversation[1]["content"].startswith("[Summary of earlier conversation"), "compaction went ahead"
    note = [m for m in system if m.startswith("NOW.md was NOT committed:")]
    assert len(note) == 1 and "no 'Doing now' heading" in note[0]
    assert system.index(note[0]) > max(i for i, m in enumerate(system) if m.startswith("Compacted")), \
        "the note comes after the compaction message, where it is seen"


def test_a_retry_that_produces_nothing_keeps_the_first_summary(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([], outcomes=(False,))
    rounds = [_write_call(owner_dir), _summary("first"), [_Chunk(content="")]]
    events, calls, system = _drive(app, rounds, owner_dir, monkeypatch, handoff)
    assert "first" in app.conversation[1]["content"]
    assert any(m.startswith("NOW.md was NOT committed:") for m in system)


def test_no_summary_means_no_gate_and_nothing_committed(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([])
    events, _calls, system = _drive(app, [[_Chunk(content="")]], owner_dir, monkeypatch, handoff)
    assert handoff.calls == [] and "truncate" not in events
    assert any("no summary produced" in m for m in system)


def test_after_a_good_compaction_the_new_note_is_what_gets_injected(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    _drive(app, [_write_call(owner_dir), _summary()], owner_dir, monkeypatch, Handoff([]))
    text = app._request_messages()[0]["content"]
    assert NEW_NOW in text and OLD_NOW not in text and text.count(app_mod.STORE_HEADER) == 1


def test_without_owner_mode_nothing_changes(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app(owner=False)
    handoff = Handoff([])
    events, calls, system = _drive(app, [_summary()], owner_dir, monkeypatch, handoff, writes_on=())
    assert handoff.calls == [] and events == ["truncate"]
    assert _last_user(calls[0]) == app_mod.COMPACT_PROMPT
    assert app._owner_compact_t0 is None and len(calls) == 1
    assert not any("NOT committed" in m for m in system)


# =========================== the Claude backend ===========================

class TurnSession(Session):
    """A Claude session that serves one scripted batch of events per query and can act on each."""

    def __init__(self, turns, on_turn=None):
        super().__init__([])
        self._turns, self.on_turn = list(turns), on_turn

    async def query(self, turn_id, prompt):
        self.queried.append(prompt)
        if self.on_turn is not None:
            self.on_turn(len(self.queried), prompt)

    async def events(self):
        for batch in self._turns[min(len(self.queried), len(self._turns)) - 1]:
            yield batch


def error_events(detail="overloaded"):
    return [[ClaudeEvent(kind="result", is_error=True, detail=detail, data={})]]


def claude_owner_app(tmp_path, owner_dir, monkeypatch, turns, handoff, write_on=(1,)):
    app, _asked = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    app._owner_memory_dir = owner_dir
    app._owner_compact_t0 = time.time_ns()
    events = handoff.events

    def on_turn(n, _prompt):
        if n in write_on:
            _write_now(owner_dir)
            events.append("write")
    app.backend.session = TurnSession(turns, on_turn)
    monkeypatch.setattr(owner_mode, "run_handoff", handoff)

    async def gate(t0):
        events.append(f"gate-closes={app.backend.closes}")
        return await owner_mode.owner_gate(owner_dir, t0, "agent-1")
    app._owner_gate = gate
    original_truncate = app._truncate

    def truncate(*a, **k):
        events.append("truncate")
        return original_truncate(*a, **k)
    app._truncate = truncate
    return app


@pytest.mark.asyncio
async def test_claude_writes_then_the_gate_then_the_session_closes_then_truncation(tmp_path, owner_dir, monkeypatch):
    handoff = Handoff([])
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events()], handoff)
    await claude_compact.compact(app, "", auto=True)
    assert handoff.events == ["write", "gate-closes=0", "handoff", "truncate"], \
        "the gate ran while the session was still open, before the cut"
    assert app.backend.closes == 1 and app.emitted[-1][0] == "compacted"
    assert not any("NOT committed" in n for n in app.notices)


@pytest.mark.asyncio
async def test_claude_is_asked_for_the_owner_step_one(tmp_path, owner_dir, monkeypatch):
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events()], Handoff([]))
    session = app.backend.session
    await claude_compact.compact(app, "keep the parser notes", auto=True)
    asked = session.queried[0]
    assert asked == claude_compact.request("keep the parser notes", owner_dir)
    assert "NOW.md" in asked and "Do NOT redo" in asked and str(owner_dir).replace("\\", "/") in asked
    assert "handoff.md" not in asked and "memory.md" not in asked
    assert claude_compact.request("x") != asked and "NOW.md" not in claude_compact.request("x")


@pytest.mark.asyncio
async def test_claude_unwritten_note_gets_one_retry_in_the_same_session(tmp_path, owner_dir, monkeypatch):
    handoff = Handoff([])
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events("first"), summary_events("second")],
                           handoff, write_on=(2,))
    session = app.backend.session
    await claude_compact.compact(app, "", auto=True)
    queries = session.queried
    assert len(queries) == 2 and "was not rewritten during this compaction" in queries[1]
    assert handoff.events.count("handoff") == 1
    assert ledger_for(app).selected["seed"].startswith("second")
    assert not any(n.startswith("NOW.md was NOT committed") for n in app.notices)


@pytest.mark.asyncio
async def test_claude_refused_twice_goes_ahead_and_says_so(tmp_path, owner_dir, monkeypatch):
    handoff = Handoff([], outcomes=(False,))
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events("first"), summary_events("second")],
                           handoff, write_on=(1, 2))
    session = app.backend.session
    await claude_compact.compact(app, "", auto=True)
    assert len(session.queried) == 2 and handoff.events.count("handoff") == 2
    assert "no 'Doing now' heading" in session.queried[1]
    assert app.backend.closes == 1 and app.emitted[-1][0] == "compacted", "compaction went ahead"
    note = [n for n in app.notices if n.startswith("NOW.md was NOT committed:")]
    assert len(note) == 1 and "no 'Doing now' heading" in note[0]
    assert app.notices.index(note[0]) > max(i for i, n in enumerate(app.notices) if n.startswith("Compacted"))


@pytest.mark.asyncio
async def test_claude_retry_turn_that_errors_keeps_the_first_summary(tmp_path, owner_dir, monkeypatch):
    handoff = Handoff([], outcomes=(False,))
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events("first"), error_events()],
                           handoff, write_on=(1,))
    await claude_compact.compact(app, "", auto=True)
    assert app.emitted[-1][0] == "compacted" and ledger_for(app).selected["seed"].startswith("first")
    note = [n for n in app.notices if n.startswith("NOW.md was NOT committed:")]
    assert len(note) == 1 and "the retry turn failed: overloaded" in note[0]


@pytest.mark.asyncio
async def test_claude_failed_first_turn_never_reaches_the_gate(tmp_path, owner_dir, monkeypatch):
    handoff = Handoff([])
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [error_events()], handoff, write_on=())
    await claude_compact.compact(app, "", auto=True)
    assert handoff.calls == [] and app.backend.closes == 0 and app.emitted[-1][0] == "failed"


@pytest.mark.asyncio
async def test_claude_without_owner_mode_is_unchanged(tmp_path, monkeypatch):
    app, _ = app_for(tmp_path, summary_events(), monkeypatch=monkeypatch)
    session = app.backend.session
    await claude_compact.compact(app, "keep it", auto=True)
    assert session.queried == [claude_compact.request("keep it")]
    assert app.emitted[-1][0] == "compacted" and not any("NOW.md" in n for n in app.notices)


# =========================== the gate itself ===========================

@pytest.mark.asyncio
async def test_the_gate_refuses_a_note_older_than_the_compaction(owner_dir, monkeypatch):
    handoff = Handoff([])
    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    ok, why = await owner_mode.owner_gate(owner_dir, time.time_ns())
    assert not ok and "was not rewritten" in why and handoff.calls == []


@pytest.mark.asyncio
async def test_the_gate_commits_a_note_written_after_t0(owner_dir, monkeypatch):
    handoff = Handoff([])
    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    t0 = time.time_ns()
    _write_now(owner_dir)
    ok, _out = await owner_mode.owner_gate(owner_dir, t0, "agent-1")
    assert ok and handoff.calls == [(owner_dir, "agent-1")]


@pytest.mark.asyncio
async def test_the_gate_reports_the_handoffs_own_reason(owner_dir, monkeypatch):
    monkeypatch.setattr(owner_mode, "run_handoff", Handoff([], outcomes=(False,)))
    t0 = time.time_ns()
    _write_now(owner_dir)
    ok, why = await owner_mode.owner_gate(owner_dir, t0)
    assert not ok and why.startswith("HANDOFF REFUSED") and "Nothing was committed" in why


@pytest.mark.asyncio
async def test_the_gate_reports_a_missing_note(owner_dir):
    (owner_dir / "NOW.md").unlink()
    ok, why = await owner_mode.owner_gate(owner_dir, time.time_ns())
    assert not ok and "cannot be read" in why


# =========================== review round ===========================

SECOND_REASON = "HANDOFF REFUSED: NOW.md has the 'Waiting on' heading 2 times. Nothing was committed."


def _tool_call(name, args, call_id):
    return [_Chunk(tool_calls=[_TC(0, id=call_id, name=name)]),
            _Chunk(tool_calls=[_TC(0, arguments=json.dumps(args))])]


def _probe_model(owner_dir, step1_reads, *, retry="write-then-summary"):
    """STEP 1 spends `step1_reads` read-only tool rounds and then summarises, without writing
    NOW.md. The retry (recognised by its text) writes NOW.md and then summarises, or, for
    retry="write-then-reads", writes and then keeps calling tools without ever summarising."""
    calls = []

    async def create(**kw):
        calls.append(kw)
        messages = kw["messages"]
        last_user = max(i for i, m in enumerate(messages) if m["role"] == "user")
        done = sum(1 for m in messages[last_user + 1:] if m["role"] == "tool")
        call_id = f"call-{len(calls)}"
        if "was NOT committed" in str(messages[last_user]["content"]):
            if done == 0:
                return _Stream(_write_call(owner_dir, call_id))
            if retry == "write-then-reads":
                return _Stream(_tool_call("read", {"path": "x"}, call_id))
            return _Stream(_summary("second summary"))
        if done < step1_reads:
            return _Stream(_tool_call("read", {"path": "x"}, call_id))
        return _Stream(_summary("first summary"))
    return create, calls


def _drive_probe(app, owner_dir, monkeypatch, handoff, step1_reads, retry="write-then-summary"):
    events = handoff.events
    create, calls = _probe_model(owner_dir, step1_reads, retry=retry)
    system = []

    async def execute(name, args, hooks_enabled=True):
        if name == "write":
            _write_now(owner_dir)
            events.append("write")
        return "ok", True

    original_truncate = app._truncate

    def truncate(*a, **k):
        events.append("truncate")
        return original_truncate(*a, **k)

    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    app._execute_tool = execute
    app._truncate = truncate
    app._system = lambda text, *a, **k: system.append(text)

    async def body():
        async with app.run_test(size=(120, 40)) as pilot:
            _seed(app)
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot, ticks=60)
    _run(body())
    return events, calls, system


@pytest.mark.parametrize("step1_reads", [7, 6, 0])
def test_the_local_retry_has_its_own_round_budget_whatever_step_one_spent(make_app, owner_dir, monkeypatch, step1_reads):
    """STEP 1 can use up to compact_max_tool_iters rounds (8 here). The retry must still be SENT,
    its write must execute, and the second gate must run, with the second reason in the note."""
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([], outcomes=((False, SECOND_REASON),))
    events, calls, system = _drive_probe(app, owner_dir, monkeypatch, handoff, step1_reads)
    retries = [c for c in calls
               if any("was NOT committed" in str(m.get("content")) for m in c["messages"] if m["role"] == "user")]
    assert retries, "the retry request was sent"
    assert events == ["write", "handoff", "truncate"], "the retry's write ran, then the SECOND gate, then the cut"
    assert len(handoff.calls) == 1
    note = [m for m in system if m.startswith("NOW.md was NOT committed:")]
    assert len(note) == 1 and "'Waiting on' heading" in note[0] and "was not rewritten" not in note[0], \
        "the note carries the second reason, not the first"


def test_a_retry_that_never_summarises_still_gets_its_gate_after_the_loop(make_app, owner_dir, monkeypatch):
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    handoff = Handoff([], outcomes=((False, SECOND_REASON),))
    events, calls, system = _drive_probe(app, owner_dir, monkeypatch, handoff, 1, retry="write-then-reads")
    assert events == ["write", "handoff", "truncate"], "the gate ran after the retry spent its own budget"
    assert "first summary" in app.conversation[1]["content"], "the first summary stands"
    assert any(m.startswith("NOW.md was NOT committed:") and "'Waiting on' heading" in m for m in system)


# -- Claude: an empty summary or a stop never reaches the gate -------------------------------------

@pytest.mark.asyncio
async def test_claude_empty_summary_never_reaches_the_handoff(tmp_path, owner_dir, monkeypatch):
    empty = [[ClaudeEvent(kind="result", is_error=False, text="", data={"subtype": "success"})]]
    handoff = Handoff([])
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [empty], handoff, write_on=(1,))
    await claude_compact.compact(app, "", auto=True)
    assert handoff.calls == [] and "handoff" not in handoff.events
    assert app.emitted[-1][0] == "failed" and app.backend.closes == 0
    assert any("No summary produced" in n for n in app.notices)


@pytest.mark.asyncio
async def test_claude_stop_during_step_one_runs_no_gate_and_no_retry(tmp_path, owner_dir, monkeypatch):
    handoff = Handoff([])
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events("first"), summary_events("second")],
                           handoff, write_on=(1,))  # NOW.md IS written: an unguarded gate would commit it
    session = app.backend.session

    def stop(n, prompt):
        app._stop_requested = True  # the user pressed Esc during STEP 1
        (owner_dir / "NOW.md").write_text("written before the stop\n", encoding="utf-8")

    session.on_turn = stop
    await claude_compact.compact(app, "", auto=True)
    assert handoff.calls == [] and len(session.queried) == 1, "no gate and no retry query after a stop"
    assert app.emitted[-1][0] == "failed" and app.backend.closes == 0


# -- the gate's edges and the seat id in the subprocess environment -------------------------------

@pytest.mark.asyncio
async def test_a_note_five_seconds_older_than_t0_is_refused(owner_dir, monkeypatch):
    handoff = Handoff([])
    monkeypatch.setattr(owner_mode, "run_handoff", handoff)
    t0 = time.time_ns()
    ago = (t0 - 5_000_000_000) / 1e9
    os.utime(owner_dir / "NOW.md", (ago, ago))
    ok, why = await owner_mode.owner_gate(owner_dir, t0)
    assert not ok and "was not rewritten" in why and handoff.calls == []


def test_the_seat_id_reaches_the_handoff_subprocess_environment(owner_dir, monkeypatch):
    import sys
    code = "import os, sys; print('ID=' + os.environ.get('LITEHARNESS_AGENT_ID', 'unset')); sys.exit(0)"
    monkeypatch.setattr(owner_mode, "handoff_command", lambda: [sys.executable, "-c", code, "--"])
    monkeypatch.delenv("LITEHARNESS_AGENT_ID", raising=False)
    ok, out = owner_mode.run_handoff(owner_dir, agent_id="seat-9")
    assert ok and "ID=seat-9" in out
    ok, out = owner_mode.run_handoff(owner_dir)
    assert ok and "ID=unset" in out, "no id given means the caller's environment is left alone"


@pytest.mark.asyncio
async def test_claude_stop_while_the_handoff_runs_sends_no_retry(tmp_path, owner_dir, monkeypatch):
    holder = {}

    class StoppingHandoff(Handoff):
        def __call__(self, memory_dir, agent_id=None, timeout=120.0):
            holder["app"]._stop_requested = True  # Esc pressed while owner-handoff is running
            return super().__call__(memory_dir, agent_id, timeout)

    handoff = StoppingHandoff([], outcomes=(False,))
    app = claude_owner_app(tmp_path, owner_dir, monkeypatch, [summary_events("first"), summary_events("second")],
                           handoff, write_on=(1,))
    holder["app"] = app
    session = app.backend.session
    await claude_compact.compact(app, "", auto=True)
    assert len(handoff.calls) == 1 and len(session.queried) == 1, "the refusal is not retried after a stop"
    assert app.backend.closes == 0
