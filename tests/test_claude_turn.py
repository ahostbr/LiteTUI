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

    def set_markdown(self, src):
        self.content = src


class FakeBubble:
    def __init__(self):
        self.body = FakeBody()
        self.answer = None
        self.settled = False
        self.thinking = None
        self.mounted = []
        self.is_attached = True

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
        # system_prompt_for (a fresh segment's prompt): the host composition, the store, tools.
        self.tools_enabled = False
        self.plugins = SimpleNamespace(compose_prompt=lambda replace=None: "HOST PROMPT")
        self._convo_pending = False

    def _materialise_convo(self):
        pass

    def _all_tools(self):
        return []

    def _scroll_down(self, **kwargs):
        pass

    def set_timer(self, delay, callback):
        # No event loop owner here: a StreamSink render happens at once.
        callback()
        return SimpleNamespace(stop=lambda: None)

    def _read_store_file(self, name, cap):
        return ""

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
async def test_error_result_preserves_native_detail_even_when_subtype_is_success(tmp_path):
    app = turn_app(tmp_path, messages=["terminal"], events=[[
        ClaudeEvent(kind="result", is_error=True, text="Authentication expired", data={"subtype": "success"}),
    ]])
    await stream_turn(app)
    assert any("Authentication expired" in note for note in app.notices)
    assert {"turn_end": "error"} in app.emitted


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


# -- effort changes go through the T911 cache gate (Ryan 2026-09-24) ---------

def effort_app(tmp_path, monkeypatch, answer):
    """A live, warm session built at effort 'high'; the user now wants 'max'."""
    import time

    from litetui import claude_cache

    app = turn_app(tmp_path, messages=["done"], events=[[result_event()]])
    session = app.backend.session
    session.effort = "high"
    session.efforts = []

    async def set_effort(level):
        session.efforts.append(level)
        session.effort = level
    session.set_effort = set_effort
    app.backend.reasoning_levels = lambda key: ["low", "medium", "high", "xhigh", "max"]
    app.thinking_level = "max"
    app.update_header = lambda: None
    clock = claude_cache.clock_for(app, app._claude_active_input["_claude_segment"])
    clock.model, clock.effort = app.model_id, "high"
    clock.observe(ClaudeUsage(source="message", input_tokens=1, cache_read_tokens=100,
                              cache_creation_tokens=0), now=time.time())
    asked = []

    async def confirm(app_, cold):
        asked.append(cold)
        return answer
    monkeypatch.setattr(claude_cache, "confirm_cold", confirm)
    return app, session, asked


@pytest.mark.asyncio
async def test_an_effort_change_raises_the_cache_warning_and_cancel_changes_nothing(tmp_path, monkeypatch):
    app, session, asked = effort_app(tmp_path, monkeypatch, answer=False)
    await stream_turn(app)
    assert [kind for kind, _ in asked] == ["effort"]
    assert "high to max" in asked[0][1]
    assert app.thinking_level == "high", "cancel restores the effort the session runs at"
    assert session.efforts == [] and session.effort == "high", "the session is untouched"
    assert session.queried == [] and app.backend.closes == 0


@pytest.mark.asyncio
async def test_send_anyway_switches_the_live_session_to_the_new_effort(tmp_path, monkeypatch):
    from litetui import claude_cache

    app, session, asked = effort_app(tmp_path, monkeypatch, answer=True)
    await stream_turn(app)
    assert [kind for kind, _ in asked] == ["effort"]
    assert session.efforts == ["max"] and session.queried, "same session, live switch, then sent"
    assert app.backend.closes == 0, "a named level switches live: no restart"
    assert claude_cache.clock_for(app, app._claude_active_input["_claude_segment"]).effort == "max"


@pytest.mark.asyncio
async def test_back_to_default_reopens_the_session_because_it_has_no_live_control(tmp_path, monkeypatch):
    app, session, asked = effort_app(tmp_path, monkeypatch, answer=True)
    app.thinking_level = None
    opened = []

    async def open_session(segment, model, **options):
        opened.append((segment.get("session_id"), options.get("effort", "absent")))
        app.backend.session = session
        return session
    app.backend.open_session = open_session
    monkeypatch.setattr("litetui.claude_tools.ClaudeTools", lambda *a, **k: app.backend._claude_tools)
    monkeypatch.setattr("litetui.claude_events.ClaudeEventStream",
                        lambda **k: app.backend._claude_events)
    await stream_turn(app)
    assert [kind for kind, _ in asked] == ["effort"] and "high to default" in asked[0][1]
    assert app.backend.closes >= 1 and opened and opened[0][1] is None
    assert session.efforts == []


def test_the_think_level_reaches_claude_as_its_effort():
    """/think max on Claude used to be read back as None by the app's
    thinking_level property, so effort_for never saw it."""
    from litetui.app import LiteTUI
    from litetui.claude_turn import effort_for

    backend = SimpleNamespace(name="claude", owns_native_turns=True,
                              reasoning_levels=lambda key: ["low", "high", "max"])
    host = SimpleNamespace(_backend=backend, _thinking_level="max")
    level = LiteTUI.thinking_level.fget(host)
    assert level == "max"
    app = SimpleNamespace(backend=backend, model_id="default", thinking_level=level,
                          settings=SimpleNamespace(model_infer_overrides={}))
    assert effort_for(app) == "max"
    app.thinking_level = "off"          # a local-backend level Claude does not take
    assert effort_for(app) is None


@pytest.mark.asyncio
async def test_the_window_comes_from_the_result_frame_and_is_not_cleared_by_message_frames(tmp_path):
    """Live: message frames carry no window, the result frame's modelUsage does.
    The footer read "ctx 11,264 / ?" and autocompact could never fire."""
    app = turn_app(tmp_path, messages=["a", "b", "c"])
    app.ctx_max, app.ctx_loaded = 1_000_000, True          # known from an earlier turn
    message = ClaudeUsage(source="message", input_tokens=10, context_tokens=9500)
    result = ClaudeUsage(source="result", input_tokens=10, max_context_tokens=1_000_000)
    app._events = [[ClaudeEvent(kind="usage", usage=message)],
                   [ClaudeEvent(kind="usage", usage=result)], [result_event()]]
    await stream_turn(app)
    assert (app.ctx_used, app.ctx_max, app.ctx_loaded) == (9500, 1_000_000, True)


@pytest.mark.asyncio
async def test_a_change_confirmed_in_the_sidecar_is_not_asked_again_at_send(tmp_path, monkeypatch):
    """The sidecar showed the same warning and the user chose Send anyway:
    the send-time gate honours that once, for exactly that effort."""
    app, session, asked = effort_app(tmp_path, monkeypatch, answer=False)
    app._claude_cache_preapproved = ("effort", "max")
    await stream_turn(app)
    assert asked == [], "no second dialog for the change already confirmed"
    assert session.efforts == ["max"] and session.queried
    assert app._claude_cache_preapproved is None, "used once"


def test_effort_change_warning_reads_the_same_gate_without_a_ledger_side_effect(tmp_path):
    from litetui.claude_turn import effort_change_warning

    app = SimpleNamespace(backend=SimpleNamespace(name="claude"), settings=SimpleNamespace())
    assert effort_change_warning(app, "max") is None, "no ledger, no live session: nothing to warn about"
    assert not hasattr(app, "_claude_ledger")


def _png_b64():
    import base64
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(out, format="PNG")
    return base64.b64encode(out.getvalue()).decode()


def image_app(tmp_path):
    from litetui.app import LiteTUI
    app = app_for(tmp_path)
    app._spill_image_for_reclick = LiteTUI._spill_image_for_reclick.__get__(app)
    return app


def test_an_image_becomes_the_path_of_its_conversation_copy(tmp_path):
    """Ryan 2026-09-24: "convert it to a path on disk for claude and paste it to him"."""
    from pathlib import Path

    from litetui.claude_turn import inline_images
    app = image_app(tmp_path)
    b64 = _png_b64()
    text, saved = inline_images(app, [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                                      {"type": "text", "text": "what colour is this?"}])
    assert len(saved) == 1
    path = Path(saved[0])
    assert path.parent == tmp_path / "images" and path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert text.startswith("what colour is this?") and str(path) in text
    # The text is what the ledger keeps and Claude receives: a plain string, admitted as usual.
    item = prepare_input(app, text, "strict", "typed")
    assert item["_claude_entry"]["content"] == text


def test_the_submits_own_spill_is_reused_not_written_twice(tmp_path):
    from litetui.claude_turn import inline_images
    app = image_app(tmp_path)
    b64 = _png_b64()
    spilled = app._spill_image_for_reclick(b64)
    text, saved = inline_images(app, [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}], spilled)
    assert saved == [spilled] and len(list((tmp_path / "images").iterdir())) == 1
    assert spilled in text


def test_an_image_that_cannot_be_saved_is_refused_not_sent_blind(tmp_path):
    from litetui.claude_turn import inline_images
    app = app_for(tmp_path)
    app._spill_image_for_reclick = lambda b64: None
    with pytest.raises(OSError, match="nothing was sent"):
        inline_images(app, [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}])
    assert not (tmp_path / "claude_ledger.json").exists()


def test_text_passes_through_untouched(tmp_path):
    from litetui.claude_turn import inline_images
    assert inline_images(app_for(tmp_path), "hello") == ("hello", [])


def test_a_new_claude_session_works_in_the_folder_litetui_was_launched_from(tmp_path):
    """Plan claude-backend-litetui-identity, phase 3. Ryan: "it should use whatever its cwd
    is i just ran it from there". The same source the Codex backend uses
    (codex_workspace.workspace: the folder captured at launch), not the install folder."""
    project = tmp_path / "LiteBench"
    project.mkdir()
    app = app_for(tmp_path / "store")
    app._hook_workspace = project
    prepare_input(app, "pwd?", "strict", "typed")
    assert ledger_for_path(app).selected["workspace"] == str(project.resolve())


def test_a_segment_is_never_resumed_in_another_folder(tmp_path):
    first, second = tmp_path / "A", tmp_path / "B"
    first.mkdir()
    second.mkdir()
    app = app_for(tmp_path / "store")
    app._hook_workspace = first
    prepare_input(app, "one", "strict", "typed")
    ledger = ledger_for_path(app)
    old = ledger.selected
    ledger.bind_session(old["id"], "native-A")
    app._hook_workspace = second
    prepare_input(app, "two", "strict", "typed")
    new = ledger.selected
    assert new["id"] != old["id"] and new["workspace"] == str(second.resolve()) and new["session_id"] is None
    assert ledger.segment(old["id"])["workspace"] == str(first.resolve()), "the old session stays where it ran"


def ledger_for_path(app):
    from litetui.claude_turn import ledger_for
    return ledger_for(app)


def _compaction_watch(app, due):
    scheduled, commands = [], []
    app.call_after_refresh = scheduled.append
    app._autocompact_due = lambda: due
    app._maybe_autocompact = lambda: commands.append("maybe")
    app._handle_command = commands.append
    return scheduled, commands


@pytest.mark.asyncio
async def test_crossing_the_threshold_mid_turn_compacts_at_the_turn_end_whatever_it_ended_on(tmp_path):
    """Plan claude-backend-litetui-identity, phase 4: LiteTUI's threshold is checked on the
    usage frames mid-turn, not only after a normal stop; the turn is never cut short."""
    app = turn_app(tmp_path, messages=["m1", "m2", "m3", "m4"])   # one SDK message per event batch
    scheduled, commands = _compaction_watch(app, due=91)
    usage = ClaudeUsage(source="message", input_tokens=10, context_tokens=910_000)
    app._events = [[ClaudeEvent(kind="text_delta", text="partial answer", message_id="a1")],
                   [ClaudeEvent(kind="usage", usage=usage)], [ClaudeEvent(kind="usage", usage=usage)],
                   [ClaudeEvent(kind="result", is_error=True, detail="tool loop failed", data={})]]
    await stream_turn(app)
    assert [n for n in app.notices if "mid-turn" in n], app.notices
    assert len([n for n in app.notices if "mid-turn" in n]) == 1
    for callback in scheduled:
        callback()
    assert commands == ["maybe"], "compaction is scheduled after the turn even though it ended in an error"
    assert any(row.get("role") == "assistant" and "partial answer" in row.get("content", "")
               for row in app.appended), "the turn's own answer is kept"


@pytest.mark.asyncio
async def test_an_overflow_says_so_and_liteui_compacts(tmp_path):
    app = turn_app(tmp_path, messages=["m1"])
    scheduled, commands = _compaction_watch(app, due=None)
    app._events = [[ClaudeEvent(kind="result", is_error=True, detail="Prompt is too long", data={})]]
    await stream_turn(app)
    assert any("context window is full" in n for n in app.notices), app.notices
    for callback in scheduled:
        callback()
    assert commands == ["/compact"] and app._compact_is_auto is True


def test_a_folder_switch_never_abandons_an_uncertain_delivery(tmp_path):
    """Review of phase 3: leaving a segment for another folder is the one implicit path to
    a new segment, so it takes the same unresolved-delivery gate the others do; otherwise
    the old segment's uncertain entry is unreachable from /claude status."""
    first, second = tmp_path / "A", tmp_path / "B"
    first.mkdir()
    second.mkdir()
    app = app_for(tmp_path / "store")
    app._hook_workspace = first
    item = prepare_input(app, "one", "strict", "typed")
    ledger = ledger_for_path(app)
    old = ledger.selected
    ledger.update_delivery(item["_claude_entry"]["id"], "submitted")
    ledger.update_delivery(item["_claude_entry"]["id"], "uncertain", error="process died mid-turn")
    app._hook_workspace = second
    with pytest.raises(ValueError, match="/claude resolve"):
        prepare_input(app, "two", "strict", "typed")
    assert ledger.selected["id"] == old["id"], "nothing switched"


@pytest.mark.asyncio
async def test_a_thinking_turn_stores_its_answer_once(tmp_path):
    """The frames a thinking model sent live (CLI 2.1.281, default model, 2026-09-24): the
    thinking block's snapshot arrives BEFORE the text streams, under the same message id.
    The answer landed in conversation[-1] twice ("yes\nno\nyes\n\nyes\nno\nyes"), and a
    thinking-only snapshot left a stray leading blank line. Real normalizer, real turn."""
    from litetui.claude_events import ClaudeEventStream

    def stream(event):
        return {"type": "stream_event", "uuid": "u", "session_id": "sess-1", "event": event}

    def assistant(blocks):
        return {"type": "assistant", "uuid": "u-snap", "session_id": "sess-1",
                "message": {"id": "msg_01", "model": "claude-opus-5-5", "content": blocks}}

    def text(t):
        return stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})

    frames = [
        stream({"type": "message_start", "message": {"id": "msg_01", "model": "claude-opus-5-5"}}),
        stream({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Easy."}}),
        assistant([{"type": "thinking", "thinking": "Easy.", "signature": "sig"}]),
        text("yes\nno"), text("\nyes"),
        assistant([{"type": "text", "text": "yes\nno\nyes"}]),
        stream({"type": "message_stop"}),
        {"type": "result", "uuid": "u-res", "session_id": "sess-1", "is_error": False,
         "subtype": "success", "result": "yes\nno\nyes"},
    ]
    app = turn_app(tmp_path, messages=frames)
    app.backend._claude_events = ClaudeEventStream(session_id="sess-1")
    app._scroll_down = lambda **k: None
    app.settings.show_thinking = False   # no Textual widget; the thinking events still flow
    await stream_turn(app)
    answers = [row["content"] for row in app.appended if row.get("role") == "assistant"]
    assert answers == ["yes\nno\nyes"], (answers, app.notices)


@pytest.mark.asyncio
async def test_a_message_with_no_text_adds_no_blank_line(tmp_path, monkeypatch):
    """Live (image smoke): the first message only called a tool, and the answer began with a
    stray blank line because every message's text was joined, the empty one included."""
    from litetui.claude_events import ClaudeEventStream

    def stream(event):
        return {"type": "stream_event", "uuid": "u", "session_id": "sess-1", "event": event}

    def assistant(message_id, blocks):
        return {"type": "assistant", "uuid": "u-" + message_id, "session_id": "sess-1",
                "message": {"id": message_id, "model": "claude-opus-5-5", "content": blocks}}

    frames = [
        stream({"type": "message_start", "message": {"id": "msg_01", "model": "m"}}),
        assistant("msg_01", [{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x.png"}}]),
        stream({"type": "message_stop"}),
        stream({"type": "message_start", "message": {"id": "msg_02", "model": "m"}}),
        stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "LEFT=red"}}),
        assistant("msg_02", [{"type": "text", "text": "LEFT=red"}]),
        stream({"type": "message_stop"}),
        {"type": "result", "uuid": "u-res", "session_id": "sess-1", "is_error": False,
         "subtype": "success", "result": "LEFT=red"},
    ]
    app = turn_app(tmp_path, messages=frames)
    app.backend._claude_events = ClaudeEventStream(session_id="sess-1")
    app._scroll_down = lambda **k: None
    app.settings.show_thinking = False
    card = SimpleNamespace(set_args=lambda text: None, set_result=lambda text, ok: None)
    monkeypatch.setattr("litetui.widgets.ToolMessage", lambda title: card)   # the Textual boundary
    app.query_one = lambda selector: SimpleNamespace(mount=lambda widget: None)
    await stream_turn(app)
    answers = [row["content"] for row in app.appended if row.get("role") == "assistant"]
    assert answers == ["LEFT=red"], (answers, app.notices)


class _ClockedBody:
    """Records, for every text write, whether the elapsed clock still owned this body."""

    def __init__(self, elapsed):
        self._elapsed = elapsed
        self.writes = []

    @property
    def content(self):
        return self.writes[-1][1] if self.writes else ""

    @content.setter
    def content(self, value):
        self.writes.append((self._elapsed.body is self, str(value)))

    def set_markdown(self, src):
        self.content = src


@pytest.mark.asyncio
async def test_text_after_a_tool_lands_below_it_and_the_clock_never_overwrites_text(tmp_path, monkeypatch):
    """Live on Ryan's screen, 2026-09-25: text written AFTER a tool call rendered in the
    turn's first card, ABOVE the tool cards, and the transcript flickered through the tool
    calls. One card per turn took every message's text; the elapsed clock kept repainting
    that card's body (every 250ms) over the streamed text until the turn ended."""
    from litetui.claude_events import ClaudeEventStream

    def stream(event):
        return {"type": "stream_event", "uuid": "u", "session_id": "sess-1", "event": event}

    def assistant(message_id, blocks):
        return {"type": "assistant", "uuid": "u-" + message_id, "session_id": "sess-1",
                "message": {"id": message_id, "model": "claude-opus-5-5", "content": blocks}}

    def text(t):
        return stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})

    frames = [
        stream({"type": "message_start", "message": {"id": "msg_01", "model": "m"}}),
        text("Let me look."),
        assistant("msg_01", [{"type": "text", "text": "Let me look."},
                             {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a"}}]),
        stream({"type": "message_stop"}),
        stream({"type": "message_start", "message": {"id": "msg_02", "model": "m"}}),
        text("Found it."),
        assistant("msg_02", [{"type": "text", "text": "Found it."}]),
        stream({"type": "message_stop"}),
        {"type": "result", "uuid": "u-res", "session_id": "sess-1", "is_error": False,
         "subtype": "success", "result": "Found it."},
    ]
    app = turn_app(tmp_path, messages=frames)
    app.backend._claude_events = ClaudeEventStream(session_id="sess-1")
    app.settings.show_thinking = False
    elapsed = SimpleNamespace(body=None)
    elapsed.start = lambda body, **k: setattr(elapsed, "body", body)
    elapsed.stop_body = lambda: setattr(elapsed, "body", None)
    app._elapsed = elapsed
    transcript = []

    def bubble():
        b = FakeBubble()
        b.body = _ClockedBody(elapsed)
        transcript.append(b)
        return b

    app._assistant_bubble = bubble

    class Card:
        def __init__(self, title):
            self.title = title

        def set_args(self, text):
            pass

        def set_result(self, text, ok):
            pass

    monkeypatch.setattr("litetui.widgets.ToolMessage", Card)   # the Textual boundary
    app.query_one = lambda selector: SimpleNamespace(mount=transcript.append)
    await stream_turn(app)

    kinds = [type(w).__name__ for w in transcript]
    assert kinds == ["FakeBubble", "Card", "FakeBubble"], kinds
    first, second = transcript[0], transcript[2]
    assert first.answer == "Let me look." and second.answer == "Found it.", (first.answer, second.answer)
    for b in (first, second):
        clocked = [t for owned, t in b.body.writes if owned]
        assert not clocked, f"text written while the clock still repaints this body: {clocked}"
    assert app._active_turn_widget is second, "the stop line settles on the last card"
    answers = [row["content"] for row in app.appended if row.get("role") == "assistant"]
    assert answers == ["Let me look.\n\nFound it."], answers
