"""Durable admission and segment ownership at the TUI boundary."""
from types import SimpleNamespace

import pytest

from litetui.claude_persistence import ClaudeLedger, LedgerError
from litetui.claude_turn import accept_input, command, prepare_input, queue_ready


def app_for(tmp_path):
    app = SimpleNamespace(convo_dir=tmp_path, convo_id="convo", backend=SimpleNamespace(name="claude", session=None),
        _materialise_convo=lambda: None, _chat_running=lambda: False, _pending_input=[], notices=[])
    app._system = app.notices.append
    return app


def test_busy_queue_persisted_and_bound_before_ack(tmp_path):
    app = app_for(tmp_path)
    first = prepare_input(app, "one", "strict", "rpc", "first")
    app._claude_active_input = first
    app._chat_running = lambda: True
    app._claude_ledger.update_delivery(first["_claude_entry"]["id"], "submitted")
    second = prepare_input(app, "two", "strict", "rpc", "second")
    reopened = ClaudeLedger(tmp_path)
    assert [e["content"] for e in reopened.pending(reopened.selected["id"])] == ["one", "two"]
    assert second["_claude_segment"] == first["_claude_segment"]


def test_uncertain_delivery_holds_existing_queue_and_new_input(tmp_path):
    app = app_for(tmp_path)
    first = prepare_input(app, "one", "strict", "rpc")
    second = prepare_input(app, "two", "strict", "rpc")
    ledger = app._claude_ledger
    ledger.update_delivery(first["_claude_entry"]["id"], "submitted")
    ledger.update_delivery(first["_claude_entry"]["id"], "uncertain")
    assert not queue_ready(app, second)
    with pytest.raises(ValueError, match="uncertain"):
        prepare_input(app, "three", "strict", "rpc")
    command(app, "resolve")
    assert queue_ready(app, second)
    assert ledger.pending(ledger.selected["id"])[0]["content"] == "two"


def test_switch_never_migrates_held_input(tmp_path):
    app = app_for(tmp_path)
    item = prepare_input(app, "held", "strict", "rpc")
    app.backend.name = "codex"
    assert not queue_ready(app, item)
    app.backend.name = "claude"
    app._claude_ledger.select_segment("another-workspace", new=True)
    assert not queue_ready(app, item)


def test_image_rejected_before_ledger_creation(tmp_path):
    app = app_for(tmp_path)
    with pytest.raises(TypeError, match="attachments"):
        prepare_input(app, [{"type": "image"}], "strict", "typed")
    assert not (tmp_path / "claude_ledger.json").exists()


def test_corrupt_ledger_refuses_no_silent_reset(tmp_path):
    app = app_for(tmp_path)
    path = tmp_path / "claude_ledger.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(LedgerError):
        prepare_input(app, "hello", "strict", "typed")
    assert path.read_text() == "broken"


def test_status_does_not_echo_prompt(tmp_path):
    app = app_for(tmp_path)
    prepare_input(app, "SENSITIVE-CONTENT", "strict", "typed")
    command(app, "status")
    assert "SENSITIVE-CONTENT" not in app.notices[-1]


def test_accept_input_reuses_durable_id(tmp_path):
    app = app_for(tmp_path)
    item = {"content": "hi", **prepare_input(app, "hi", "strict", "rpc")}
    accepted = accept_input(app, item)
    assert accepted["id"] == item["_claude_entry"]["id"]
    assert len(app._claude_ledger.pending(app._claude_ledger.selected["id"])) == 1


# -- driving a real stream_turn against a fake SDK boundary ----------------

import asyncio
from typing import ClassVar

from litetui import widgets as widgets_mod
from litetui.claude_events import ClaudeEvent, ClaudeUsage
from litetui.claude_turn import stream_turn


class FakeThinking:
    """Only what claude_turn uses: append, mount, remove."""

    instances: ClassVar[list] = []

    def __init__(self):
        self.text = ""
        self.removed = False
        FakeThinking.instances.append(self)

    def append(self, token):
        self.text += token

    def remove(self):
        self.removed = True


class FakeBody:
    def __init__(self):
        self.content = ""


class FakeBubble:
    def __init__(self):
        self.body = FakeBody()
        self.answer = None
        self.settled = False
        self.thinking = None
        self.mounted = []

    def set_answer(self, text):
        self.answer = text

    def mount(self, child, before=None):
        self.mounted.append(child)


class FakeSession:
    def __init__(self, messages=(), block=False):
        self.session_id = "sess-1"
        self.lifecycle = SimpleNamespace(failure=None, active_turn=False)
        self._messages = list(messages)
        self._block = block
        self.queried = []
        self.interrupted = 0

    async def set_model(self, model):
        pass

    async def query(self, entry_id, content):
        self.queried.append(entry_id)

    async def interrupt(self):
        self.interrupted += 1

    async def events(self):
        for message in self._messages:
            yield message
        if self._block:
            await asyncio.Event().wait()


class FakeBackend:
    name = "claude"
    owns_native_turns = True

    def __init__(self, session, segment_id, close_error=None):
        self.session = session
        self.segment_id = segment_id
        self.closes = 0
        self._close_error = close_error

    async def close(self):
        self.closes += 1
        self.session = None
        if self._close_error:
            raise RuntimeError(self._close_error)


class TurnApp:
    """Every attribute here is one stream_turn actually reads."""

    def __init__(self, tmp_path, events):
        self.convo_dir = tmp_path
        self.convo_id = "convo"
        self.model_id = "claude-opus-5"
        self.settings = SimpleNamespace(show_thinking=True)
        self.notices = []
        self.appended = []
        self.emitted = []
        self.sets = []
        self.tps = None
        self._stop_requested = False
        self._stop_reason = None
        self._turn_stop_line_settled = False
        self._turn_abandoned = False
        self._claude_usage = None
        self._active_turn_widget = None
        self._active_turn_started_at = None
        self._thinking_live = None
        self.ctx_used = None
        self.ctx_max = None
        self.ctx_loaded = False
        self.bubble = FakeBubble()
        self._elapsed = SimpleNamespace(start=lambda *a, **k: None, stop_body=lambda: None)
        self._events = events

    def _materialise_convo(self):
        pass

    def _chat_running(self):
        return False

    def _system(self, text):
        self.notices.append(text)

    def _rpc_emit(self, payload):
        self.emitted.append(payload)

    def _assistant_bubble(self):
        return self.bubble

    def _append(self, row):
        self.appended.append(row)

    def _thinking_done(self):
        pass

    def _refresh_ctx_label(self):
        pass

    def _settle_turn_stop_line(self, *a, **k):
        pass

    def _emit_turn_end(self, reason, *a, **k):
        self.emitted.append({"turn_end": reason})

    def query_one(self, selector):
        return SimpleNamespace(mount=lambda *a, **k: None)

    def __setattr__(self, name, value):
        # Order matters for ctx_*: watch_ctx_used reads ctx_max.
        if name in {"ctx_used", "ctx_max", "ctx_loaded"}:
            self.__dict__.setdefault("sets", []).append(name)
        object.__setattr__(self, name, value)


def turn_app(tmp_path, messages=(), block=False, events=None, close_error=None):
    app = TurnApp(tmp_path, events or [])
    item = prepare_input(app, "hello", "strict", "typed")
    app._claude_active_input = {"content": "hello", **item}
    session = FakeSession(messages, block=block)
    backend = FakeBackend(session, item["_claude_segment"], close_error=close_error)
    backend._claude_tools = SimpleNamespace(
        cancelled=SimpleNamespace(clear=lambda: None, set=lambda: None, is_set=lambda: False),
        stop=lambda: None, sdk_options=dict)
    backend._claude_events = SimpleNamespace(
        reset_turn=lambda: None,
        push=lambda message: app._events.pop(0) if app._events else [])
    app.backend = backend
    return app


@pytest.mark.asyncio
async def test_admitted_input_stays_held_when_segment_changes_before_send(tmp_path):
    app = turn_app(tmp_path)
    session = app.backend.session
    old_segment = app._claude_active_input["_claude_segment"]
    ledger = app._claude_ledger
    ledger.select_segment("different-workspace", new=True)
    await stream_turn(app)
    assert session.queried == []
    assert ledger.pending(old_segment)[0]["state"] == "prepared"
    assert any("held" in note for note in app.notices)


def result_event():
    return ClaudeEvent(kind="result", is_error=False, data={"subtype": "success"})


@pytest.mark.asyncio
async def test_worker_cancellation_is_not_swallowed(tmp_path):
    # The turn used to absorb its own teardown: the Textual worker reported
    # SUCCESS and the finally still appended this turn's row and emitted its
    # turn_end while a replacement turn was already running.
    app = turn_app(tmp_path, messages=[], block=True)
    task = asyncio.ensure_future(stream_turn(app))
    for _ in range(80):
        await asyncio.sleep(0.01)
        if app.backend.session and app.backend.session.queried:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert app.backend.closes == 1, "the owned runtime is still closed on the way out"


@pytest.mark.asyncio
async def test_an_ordinary_failure_is_still_absorbed_into_the_turn(tmp_path):
    # The other side of the same clause: a provider error is a turn outcome,
    # not a teardown, and must not propagate out of the worker.
    app = turn_app(tmp_path, messages=["one", "two"])
    app._events = [
        [ClaudeEvent(kind="error", is_error=True, detail="rate limited")],
        [result_event()],
    ]
    await stream_turn(app)
    assert any("rate limited" in n for n in app.notices)


@pytest.mark.asyncio
async def test_a_turn_without_admission_says_so_instead_of_raising(tmp_path):
    app = turn_app(tmp_path, messages=[])
    del app._claude_active_input
    await stream_turn(app)
    assert any("without admission" in n for n in app.notices)
    assert app.backend.session.queried == []


@pytest.mark.asyncio
async def test_a_diverged_thinking_snapshot_rebuilds_instead_of_duplicating(tmp_path, monkeypatch):
    FakeThinking.instances.clear()
    monkeypatch.setattr(widgets_mod, "ThinkingBlock", FakeThinking)
    app = turn_app(tmp_path, messages=["a", "b", "c"])
    app._events = [
        [ClaudeEvent(kind="thinking_delta", text="Let me ", message_id="m1")],
        [ClaudeEvent(kind="thinking", text="Let me check the file.", reconcile="replace",
                     complete=True, message_id="m1")],
        [result_event()],
    ]
    await stream_turn(app)
    live = [block for block in FakeThinking.instances if not block.removed]
    assert len(live) == 1
    assert live[0].text == "Let me check the file."


@pytest.mark.asyncio
async def test_a_continuing_thinking_snapshot_appends_only_its_tail(tmp_path, monkeypatch):
    FakeThinking.instances.clear()
    monkeypatch.setattr(widgets_mod, "ThinkingBlock", FakeThinking)
    app = turn_app(tmp_path, messages=["a", "b", "c"])
    app._events = [
        [ClaudeEvent(kind="thinking_delta", text="Let me ", message_id="m1")],
        [ClaudeEvent(kind="thinking", text="check.", reconcile="append",
                     complete=True, message_id="m1")],
        [result_event()],
    ]
    await stream_turn(app)
    live = [block for block in FakeThinking.instances if not block.removed]
    assert len(live) == 1
    assert live[0].text == "Let me check."


@pytest.mark.asyncio
async def test_the_context_window_is_set_before_the_used_reactive(tmp_path):
    # watch_ctx_used renders "used/total" by reading ctx_max, and a reactive
    # does not fire again for an unchanged value, so setting used first showed
    # "unknown" and stayed there.
    app = turn_app(tmp_path, messages=["a", "b"])
    usage = ClaudeUsage(source="message", input_tokens=10, context_tokens=9500,
                        max_context_tokens=200000)
    app._events = [[ClaudeEvent(kind="usage", usage=usage)], [result_event()]]
    app.sets.clear()  # ignore TurnApp.__init__'s own defaults
    await stream_turn(app)
    assert app.sets.index("ctx_max") < app.sets.index("ctx_used")
    assert (app.ctx_used, app.ctx_max, app.ctx_loaded) == (9500, 200000, True)


# -- /claude new is the documented escape hatch, so it must not lie --------


class _ClosingBackend:
    name = "claude"
    owns_native_turns = True

    def __init__(self, error=None):
        self.session = object()
        self.closes = 0
        self._error = error

    async def close(self):
        self.closes += 1
        self.session = None
        if self._error:
            raise RuntimeError(self._error)


def session_app(tmp_path, error=None):
    app = app_for(tmp_path)
    app.backend = _ClosingBackend(error)
    app.workers = []
    app.run_worker = lambda coro, **kwargs: app.workers.append(coro)
    return app


@pytest.mark.asyncio
async def test_claude_new_selects_a_segment_once_the_close_settles(tmp_path):
    app = session_app(tmp_path)
    prepare_input(app, "hello", "strict", "typed")
    before = app._claude_ledger.selected["id"]
    command(app, "new")
    await app.workers[0]
    assert app.backend.closes == 1
    assert app._claude_ledger.selected["id"] != before
    assert any("New Claude session selected" in n for n in app.notices)


@pytest.mark.asyncio
async def test_claude_new_refuses_when_the_old_runtime_will_not_let_go(tmp_path):
    # exit_on_error=False means an escaping exception is silent, so the user
    # pressed the one command the uncertain-delivery refusal sends them to and
    # got nothing at all. Selecting a new segment anyway would be worse: it
    # would hide a runtime that is still alive behind a UI saying it was
    # replaced.
    app = session_app(tmp_path, error="reader would not drain")
    prepare_input(app, "hello", "strict", "typed")
    before = app._claude_ledger.selected["id"]
    command(app, "new")
    await app.workers[0]
    assert app._claude_ledger.selected["id"] == before
    assert any("did not close cleanly" in n for n in app.notices)
    assert any("may still be alive" in n for n in app.notices)
    assert not any("New Claude session selected" in n for n in app.notices)


def test_the_help_names_every_recovery_verb(tmp_path):
    # resolve and continue were implemented and unreachable: the help listed
    # only status and new, and they are the only way out of an uncertain
    # delivery that keeps the session.
    app = app_for(tmp_path)
    command(app, "explain yourself")
    assert "resolve" in app.notices[-1]
    assert "continue" in app.notices[-1]


def test_the_uncertain_refusal_names_the_way_out(tmp_path):
    app = app_for(tmp_path)
    first = prepare_input(app, "one", "strict", "typed")
    ledger = app._claude_ledger
    ledger.update_delivery(first["_claude_entry"]["id"], "submitted")
    ledger.update_delivery(first["_claude_entry"]["id"], "uncertain")
    with pytest.raises(ValueError) as caught:
        prepare_input(app, "two", "strict", "typed")
    assert "/claude resolve" in str(caught.value)
    assert "/claude new" in str(caught.value)
