"""Reviewed queue authority over real owned source, with a model-free SDK client."""
from __future__ import annotations

import asyncio
import copy
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_rust_background_input import app as owned_app
from test_rust_background_input import command
from test_rust_background_input import switch_app as owned_switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import gui_rpc, native_midturn
from litetui import native_runtime_controls as controls
from litetui.claude_backend import ClaudeBackend
from litetui.claude_session import ClaudeSession
from litetui.claude_turn import launch_workspace, ledger_for, prepare_input
from litetui.event_origin import EventOrigin

app, switch_app = owned_app, owned_switch_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def running(app, monkeypatch):
    calls = []
    class Client:
        def __init__(self, options):
            self.frames = asyncio.Queue()
        async def connect(self): pass
        async def get_server_info(self): return {}
        async def query(self, prompt): calls.append(prompt)
        async def receive_messages(self):
            while True: yield await self.frames.get()
        async def disconnect(self): pass

    app.backend = ClaudeBackend(app.settings)
    app._agent_session.update_execution(backend="claude", model=app.model_id, thinking_level="default")
    app._launch_options.work_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app._cli_tool_profile = app._active_tool_profile = "strict"
    app.settings.mcp_enabled = app.settings.enter_interrupts = False
    app._stop_requested = False
    monkeypatch.setattr(app, "_user_bubble", lambda *_a, **_kw: SimpleNamespace(border_title="You"))
    monkeypatch.setattr(app, "_chat_running", lambda: any(w.group == "chat" and not w.is_finished for w in app.workers))
    ledger = ledger_for(app)
    segment = ledger.select_segment(launch_workspace(app))
    native = str(uuid.uuid4())
    ledger.bind_session(segment["id"], native)
    session = ClaudeSession(SimpleNamespace(cwd=launch_workspace(app)), client_factory=Client)
    await session.start()
    session.session_id = native
    app.backend.session, app.backend.segment_id = session, segment["id"]
    metadata = prepare_input(app, "first controlled input", "strict", "rpc", "foreground")
    app._claude_active_input = metadata
    active = metadata["_claude_entry"]["id"]
    ledger.update_delivery(active, "submitted")
    await session.query(active, "first controlled input")
    capture = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_capture",
        execution_version=1, runtime_controls_version=1))
    await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate",
        instance=capture["instance"], activation_token=capture["activation_token"],
        scheduled_transition_version=1, execution_version=1, runtime_controls_version=1))
    hold, entered = asyncio.Event(), asyncio.Event()
    async def stream(origin):
        entered.set()
        await hold.wait()
    monkeypatch.setattr(app, "_stream_worker", stream)
    hook = str(uuid.uuid4())
    app._hook_turn_id, app._hook_source, app._gui_operation_id = hook, "rpc", "foreground"
    worker = app._stream(origin=EventOrigin(session_id=app.convo_id, operation_id="foreground", hook_id=hook, source="rpc"))
    await entered.wait()
    origin = controls._owner(controls._state(app))["origin"]
    def cmd(operation, **extra):
        return command(app, type=operation, id=str(uuid.uuid4()), work_instance=work.bridge(app).instance,
            origin=origin, chord="enter", text="Exact queued draft 雪", profile="strict", policy_epoch=1, **extra)
    try:
        yield app, cmd, calls
    finally:
        hold.set()
        await asyncio.gather(worker.wait(), return_exceptions=True)
        await session.close()
        app.backend.session = None


async def invoke(app, cmd, *, publish=True):
    result = []
    async def run():
        try:
            value = await gui_rpc.async_dispatch(app, cmd)
            if publish:
                from litetui import rpc
                rpc.publish_management_result(app, cmd, value)
            result.append(value)
        except ValueError as exc: result.append(exc)
    await app.run_worker(run(), group="gui-management", exit_on_error=False).wait()
    assert len(result) == 1
    return result[0]


async def test_queue_prepares_once_reads_both_journals_and_keeps_native_query_idle(running):
    app, cmd, calls = running
    bridge = work.bridge(app)
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    assert review["native"]["active_input"] == app.backend.session.lifecycle.active_turn
    request = cmd(native_midturn.SEND_APPLY, review=review["review"], native=review["native"])
    result = await invoke(app, request)
    assert result["kind"] == "midturn_queued", result
    assert calls == ["first controlled input"] and not app._stop_requested
    assert len(app._pending_input) == len(bridge.inputs) == 1
    item = app._pending_input[0]
    native = item["_claude_entry"]
    assert result["native_delivery"] == {"id": native["id"], "segment_id": app.backend.segment_id,
        "state": "prepared", "operation_id": review["review"]}
    assert native["content"] == "Exact queued draft 雪"
    work.validate_document(work.strict_json(bridge.raw))
    assert isinstance(await invoke(app, request), ValueError)
    assert calls == ["first controlled input"] and len(app._pending_input) == 1
    recovered = work.Bridge(app)
    assert len(recovered.recovered) == 1 and recovered.recovered[0]["state"] == "held"
    assert recovered.recovered[0]["input"] == result["input"]
    assert not recovered.inputs[result["input"]["input_id"]].admission


@pytest.mark.parametrize("fault", ["generation", "owner", "finished", "segment", "native", "stop", "held", "wire", "ledger"])
async def test_stale_queue_review_never_prepares_or_sends(running, fault):
    app, cmd, calls = running
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    assert isinstance(review, dict), review
    request = cmd(native_midturn.SEND_APPLY, review=review["review"], native=copy.deepcopy(review["native"]))
    session = app.backend.session
    old_owner = session._owner
    if fault == "owner": session._owner = asyncio.current_task()
    elif fault == "generation": session.lifecycle.generation += 1
    elif fault == "finished": session.lifecycle.finish_turn(session.lifecycle.active_turn)
    elif fault == "segment": app.backend.segment_id = uuid.uuid4().hex
    elif fault == "native": session.session_id = str(uuid.uuid4())
    elif fault == "stop": app._stop_requested = True
    elif fault == "held": controls._state(app).error = "Controlled publication hold"
    elif fault == "wire": request["native"]["active_input"] = uuid.uuid4().hex
    elif fault == "ledger": app._claude_ledger.file.write_text("{}", encoding="utf-8")
    result = await invoke(app, request)
    session._owner = old_owner
    assert isinstance(result, ValueError), result
    assert not app._pending_input and not work.bridge(app).inputs and not work.bridge(app).producers
    assert calls == ["first controlled input"]


async def test_queue_refuses_interrupt_and_closed_native_inventory_without_mutation(running):
    app, cmd, calls = running
    request = cmd(native_midturn.SEND_REVIEW)
    request["chord"] = "alternate"
    result = await invoke(app, request)
    assert isinstance(result, ValueError) and "does not interrupt" in str(result)
    assert not app._pending_input and not app._stop_requested and calls == ["first controlled input"]


async def test_partial_native_prepare_is_retained_without_work_queue_or_success(running, monkeypatch):
    app, cmd, calls = running
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    from litetui import native_claude_queue
    actual = native_claude_queue.prepare_input
    def lost_reply(*args):
        actual(*args)
        raise OSError("Controlled exception after durable native preparation")
    monkeypatch.setattr(native_claude_queue, "prepare_input", lost_reply)
    result = await invoke(app, cmd(native_midturn.SEND_APPLY, review=review["review"], native=review["native"]))
    assert isinstance(result, ValueError) and "uncertain" in str(result)
    assert not app._pending_input and not work.bridge(app).inputs
    assert work.bridge(app).error and calls == ["first controlled input"]
    entries = app._claude_ledger.selected["entries"]
    retained = [e for e in entries if e["operation_id"] == review["review"]]
    assert len(retained) == 1 and retained[0]["state"] == "prepared"



async def test_queue_admission_waits_for_exact_task_response_publication(running):
    from litetui import native_claude_queue, rpc
    app, cmd, calls = running
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    request = cmd(native_midturn.SEND_APPLY, review=review["review"], native=review["native"])
    entered, release = asyncio.Event(), asyncio.Event()
    captured = []
    async def owner():
        result = await gui_rpc.async_dispatch(app, request)
        captured.append(result)
        entered.set()
        await release.wait()
        rpc.publish_management_result(app, request, result)
    worker = app.run_worker(owner(),group="gui-management",exit_on_error=False)
    await entered.wait()
    entry = next(iter(work.bridge(app).inputs.values()))
    assert native_claude_queue.blocking(app) and controls.mutation_hold(app)
    with pytest.raises(ValueError, match="confirmation publication"):
        work._begin(app, entry, False)
    with pytest.raises(ValueError, match="task"):
        native_claude_queue.prepare_publication(app, request, captured[0])
    assert native_claude_queue.blocking(app) and calls == ["first controlled input"]
    release.set()
    await worker.wait()
    assert not native_claude_queue.blocking(app)
    assert entry.state == "queued" and calls == ["first controlled input"]
    with pytest.raises(ValueError, match="foreign|consumed"):
        native_claude_queue.prepare_publication(app, request, captured[0])


async def test_failed_queue_response_retains_hold_and_joined_quit_recovers_without_send(running, monkeypatch):
    from litetui import native_claude_queue, rpc
    app, cmd, calls = running
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    request = cmd(native_midturn.SEND_APPLY, review=review["review"], native=review["native"])
    def failed_emit(row):
        if row.get("id") == request["id"]:
            raise OSError("Controlled failed response emission")
    monkeypatch.setattr(rpc,"rpc_emit",failed_emit)
    async def owner():
        result = await gui_rpc.async_dispatch(app, request)
        with pytest.raises(OSError,match="failed response"):
            rpc.publish_management_result(app, request, result)
    await app.run_worker(owner(),group="gui-management",exit_on_error=False).wait()
    assert native_claude_queue.blocking(app) and controls.mutation_hold(app)
    entry = next(iter(work.bridge(app).inputs.values()))
    assert entry.state == "queued" and entry.item["_claude_entry"]["state"] == "prepared"
    with pytest.raises(ValueError,match="confirmation publication"):
        work._begin(app, entry, False)
    assert calls == ["first controlled input"]
    await work.settle_before_shutdown(app)
    assert entry.state == "held" and entry.admission is None
    recovered = work.Bridge(app)
    assert recovered.recovered[0]["input"] == entry.ref
    assert recovered.recovered[0]["state"] == "held"
    assert calls == ["first controlled input"] and native_claude_queue.blocking(app)
