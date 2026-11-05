"""Native mid-turn ownership and actual source steering without provider access."""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
import pytest_asyncio
from test_rpc_publication_sink import ACTUAL_EMIT, Output
from test_rust_background_input import app, command, switch_app
from test_rust_claude_queue import invoke
from test_rust_claude_queue import running as claude_running
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import gui_rpc, native_claude_queue, native_midturn, rpc, seat_authority
from litetui import native_runtime_controls as controls
from litetui.codex_app_server import AppServer, AppServerTransport
from litetui.codex_steering import HostSteering, RejectedRequest
from litetui.event_origin import EventOrigin
from litetui.model_transport import ProviderError

__all__ = ["app", "claude_running", "switch_app"]
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def codex(app, monkeypatch):
    calls, emitted = [], []
    server = AppServer()
    server.process = SimpleNamespace(returncode=None)
    server.initialized = True
    server.session.ready(server.session.begin())
    transport = AppServerTransport(server, app)
    transport.thread_id, transport.turn_id, transport.process = (
        "native-thread",
        "native-turn",
        server.process,
    )
    monkeypatch.setattr(app.backend, "name", "codex")
    monkeypatch.setattr(app.backend, "app_server", server, raising=False)
    monkeypatch.setattr(app.backend, "_transport", transport, raising=False)
    app._agent_session.update_execution(
        backend="codex", model=app.model_id, thinking_level="default"
    )
    app._launch_options.work_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app._cli_tool_profile = app._active_tool_profile = "strict"
    app.settings.mcp_enabled = app.settings.enter_interrupts = False
    app._stop_requested = False
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    monkeypatch.setattr(rpc, "rpc_emit", lambda row: emitted.append(row))
    monkeypatch.setattr(
        app, "_user_bubble", lambda *_a, **_kw: SimpleNamespace(border_title="You")
    )
    monkeypatch.setattr(
        app,
        "_chat_running",
        lambda: any(w.group == "chat" and not w.is_finished for w in app.workers),
    )
    capture = await published_rpc(
        app,
        monkeypatch,
        command(
            app,
            type="gui.work.native_capture",
            execution_version=1,
            runtime_controls_version=1,
        ),
    )
    await published_rpc(
        app,
        monkeypatch,
        command(
            app,
            type="gui.work.native_activate",
            instance=capture["instance"],
            activation_token=capture["activation_token"],
            scheduled_transition_version=1,
            execution_version=1,
            runtime_controls_version=1,
        ),
    )
    outcome = {"value": "accepted"}

    async def request(method, params):
        calls.append((method, params))
        assert method == "turn/steer"
        assert not native_claude_queue.blocking(app)
        native = app._pending_input[0]["_codex_entry"]
        assert native["state"] == "sending" and native["work_input_id"]
        if outcome["value"] == "rejected":
            raise RejectedRequest("Controlled pre-injection rejection", -32602)
        if outcome["value"] == "uncertain":
            raise ProviderError("Controlled reply loss")
        if outcome["value"] == "wrong-turn":
            return {"turnId": "foreign"}
        return {"turnId": transport.turn_id}

    monkeypatch.setattr(server, "request", request)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def stream(origin):
        async with transport.lock:
            metadata = {
                "provider": "codex",
                "app_server_thread_id": transport.thread_id,
                "app_server_turn_id": transport.turn_id,
                "instructions_digest": "a" * 64,
                "steering": [],
            }
            app.conversation[3]["provider_metadata"] = metadata
            app._edit(3, "Controlled foreground native owner")
            steering = HostSteering(
                app,
                server,
                transport.thread_id,
                transport.turn_id,
                metadata,
                lambda: app._edit(3, "Controlled native journal"),
            )
            child = app.run_worker(
                steering.run(), group="codex-steering", exit_on_error=False
            )
            while getattr(app, "_native_work_provider_lane", None) is None:
                await asyncio.sleep(0)
            entered.set()
            try:
                await finish.wait()
            finally:
                transport.turn_id = None
                child.cancel()
                await asyncio.gather(child.wait(), return_exceptions=True)

    monkeypatch.setattr(app, "_stream_worker", stream)
    hook = str(uuid.uuid4())
    app._hook_turn_id, app._hook_source, app._gui_operation_id = (
        hook,
        "rpc",
        "foreground",
    )
    worker = app._stream(
        origin=EventOrigin(
            session_id=app.convo_id,
            operation_id="foreground",
            hook_id=hook,
            source="rpc",
        )
    )
    await asyncio.wait_for(entered.wait(), 3)
    origin = controls._owner(controls._state(app))["origin"]

    def cmd(operation, **extra):
        return command(
            app,
            type=operation,
            id=str(uuid.uuid4()),
            work_instance=work.bridge(app).instance,
            origin=origin,
            chord="enter",
            text="Native draft 雪",
            profile="strict",
            policy_epoch=1,
            **extra,
        )

    try:
        yield app, cmd, calls, outcome, finish, worker
    finally:
        finish.set()
        await asyncio.gather(worker.wait(), return_exceptions=True)
        server.process = None


@pytest.mark.parametrize(
    "outcome_name", ["accepted", "rejected", "uncertain", "wrong-turn"]
)
async def test_codex_durable_steering_uses_exact_native_turn_and_distinguishes_uncertainty(
    codex, outcome_name
):
    app, cmd, calls, outcome, _, _ = codex
    outcome["value"] = outcome_name
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    assert review["native"]["turn_id"] == "native-turn", review
    request = cmd(
        native_midturn.SEND_APPLY, review=review["review"], native=review["native"]
    )
    result = await invoke(app, request)
    assert result["native_delivery"]["state"] == "queued", result
    for _ in range(100):
        if calls and next(iter(work.bridge(app).inputs.values())).state != "claiming":
            await asyncio.sleep(0.02)
            break
        await asyncio.sleep(0.01)
    assert len(calls) == 1
    assert (
        calls[0][1]["threadId"] == "native-thread"
        and calls[0][1]["expectedTurnId"] == "native-turn"
    )
    entry = next(iter(work.bridge(app).inputs.values()))
    native = entry.item["_codex_entry"]
    assert (
        calls[0][1]["clientUserMessageId"]
        == result["native_delivery"]["id"]
        == native["id"]
    )
    if outcome_name == "accepted":
        assert entry.admission["mode"] == "in_turn"
        assert entry.admission["provider_delivery"] == {
            "id": native["id"],
            "state": "accepted",
        }
        assert not app._pending_input
    elif outcome_name == "rejected":
        assert (
            native["state"] == "next_turn"
            and entry.state == "queued"
            and not entry.admission
        )
    else:
        assert (
            native["state"] == "uncertain"
            and entry.state == "uncertain"
            and not entry.admission
        )
        assert work.bridge(app).error
    work.validate_document(work.strict_json(work.bridge(app).raw))
    assert isinstance(await invoke(app, request), ValueError)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "fault", ["thread", "turn", "process", "generation", "terminal", "policy", "native"]
)
async def test_codex_review_refuses_changed_owner_before_mutation(codex, fault):
    app, cmd, calls, _, _, _ = codex
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    request = cmd(
        native_midturn.SEND_APPLY,
        review=review["review"],
        native=dict(review["native"]),
    )
    transport, server = app.backend._transport, app.backend.app_server
    if fault == "thread":
        transport.thread_id = "foreign"
    elif fault == "turn":
        transport.turn_id = "foreign"
    elif fault == "process":
        transport.process = object()
    elif fault == "generation":
        server.session.generation += 1
    elif fault == "terminal":
        controls._state(app).terminal.add(request["origin"]["execution_id"])
    elif fault == "policy":
        request["policy_epoch"] += 1
    else:
        request["native"]["turn_id"] = "foreign"
    assert isinstance(await invoke(app, request), ValueError)
    assert not calls and not app._pending_input and not work.bridge(app).inputs


@pytest.mark.parametrize("failure", [None, "write", "flush"])
@pytest.mark.parametrize("interrupt", [False, True])
async def test_native_confirmation_blocks_steering_until_same_task_publication(
    codex, monkeypatch, failure, interrupt
):
    app, cmd, calls, _, _, _ = codex
    request = cmd(native_midturn.SEND_REVIEW)
    if interrupt:
        request["chord"] = "alternate"
    review = await invoke(app, request)
    request.update(
        type=native_midturn.SEND_APPLY,
        id=str(uuid.uuid4()),
        review=review["review"],
        native=review["native"],
    )
    captured, ready, release = [], asyncio.Event(), asyncio.Event()

    async def owner():
        value = await gui_rpc.async_dispatch(app, request)
        captured.append(value)
        ready.set()
        await release.wait()
        if failure:
            with monkeypatch.context() as patch:
                patch.setattr(rpc, "rpc_emit", ACTUAL_EMIT)
                patch.setattr(rpc, "_real_stdout", Output(failure))
                with pytest.raises(OSError):
                    rpc.publish_management_result(app, request, value)
        else:
            rpc.publish_management_result(app, request, value)

    worker = app.run_worker(owner(), group="gui-management", exit_on_error=False)
    await asyncio.wait_for(ready.wait(), 3)
    await asyncio.sleep(0.15)
    assert not calls and native_claude_queue.blocking(app)
    assert app._stop_requested == interrupt
    with pytest.raises(ValueError):
        work.prepare_publication(app, request, captured[0])
    release.set()
    await worker.wait()
    await asyncio.sleep(0.15)
    assert native_claude_queue.blocking(app) == bool(failure)
    assert bool(calls) == (not failure and not interrupt)
    if interrupt or failure:
        entry = next(iter(work.bridge(app).inputs.values()))
        assert (
            entry.state == "queued" and entry.item["_codex_entry"]["state"] == "queued"
        )
        assert work.Bridge(app).recovered[0]["state"] == "held"


async def test_codex_terminal_before_publication_wakes_only_after_exact_confirmation(
    codex, monkeypatch
):
    app, cmd, calls, _, finish, parent = codex
    review = await invoke(app, cmd(native_midturn.SEND_REVIEW))
    request = cmd(
        native_midturn.SEND_APPLY, review=review["review"], native=review["native"]
    )
    ready, release, wakes = asyncio.Event(), asyncio.Event(), []
    replies = []
    monkeypatch.setattr(rpc, "rpc_emit", replies.append)
    monkeypatch.setattr(app, "_flush_pending_input", lambda: wakes.append(True))

    async def owner():
        result = await gui_rpc.async_dispatch(app, request)
        ready.set()
        await release.wait()
        rpc.publish_management_result(app, request, result)

    worker = app.run_worker(owner(), group="gui-management", exit_on_error=False)
    await asyncio.wait_for(ready.wait(), 3)
    finish.set()
    await parent.wait()
    assert native_claude_queue.blocking(app) and not calls
    wakes.clear()
    monkeypatch.setattr(
        app, "call_later", lambda callback, *args, **kwargs: wakes.append(callback)
    )
    release.set()
    await worker.wait()
    await asyncio.sleep(0.1)
    assert (
        wakes == [app._flush_pending_input]
        and not calls
        and not native_claude_queue.blocking(app)
    ), replies[-3:]


async def test_codex_review_refuses_retired_native_steering_owner(codex):
    app, cmd, calls, _, _, _ = codex
    # No yield between published applies is guaranteed; hold the first exact
    # native admission independently to exercise only unclaimed FIFO ownership.
    lane = app._native_work_provider_lane
    lane.worker.cancel()
    await asyncio.gather(lane.worker.wait(), return_exceptions=True)
    # A retired provider cannot be borrowed for a new review.
    assert isinstance(await invoke(app, cmd(native_midturn.SEND_REVIEW)), ValueError)
    assert not app._pending_input and not calls


async def test_codex_interrupt_front_order_preserves_prior_prepared_fifo(codex):
    app, cmd, calls, _, _, _ = codex
    results = []

    async def owner():
        for number in range(3):
            request = cmd(native_midturn.SEND_REVIEW)
            request["text"] = f"Prepared input {number}"
            if number == 2:
                request["chord"] = "alternate"
            review = await gui_rpc.async_dispatch(app, request)
            apply = {
                **request,
                "id": str(uuid.uuid4()),
                "type": native_midturn.SEND_APPLY,
                "review": review["review"],
                "native": review["native"],
            }
            result = await gui_rpc.async_dispatch(app, apply)
            rpc.publish_management_result(app, apply, result)
            results.append(result)

    await app.run_worker(owner(), group="gui-management", exit_on_error=False).wait()
    assert results[2]["prior_inputs"] == [r["input"]["input_id"] for r in results[:2]]
    assert [
        work.entry_for(app, item).ref["input_id"] for item in app._pending_input
    ] == [results[i]["input"]["input_id"] for i in (2, 0, 1)]
    assert app._stop_requested and not calls
    work.validate_document(work.strict_json(work.bridge(app).raw))


async def test_claude_interrupt_front_order_preserves_prior_prepared_fifo(
    claude_running,
):
    app, cmd, calls = claude_running
    results = []

    async def owner():
        for number in range(3):
            request = cmd(native_midturn.SEND_REVIEW)
            request["text"] = f"Prepared input {number}"
            if number == 2:
                request["chord"] = "alternate"
            review = await gui_rpc.async_dispatch(app, request)
            apply = {
                **request,
                "id": str(uuid.uuid4()),
                "type": native_midturn.SEND_APPLY,
                "review": review["review"],
                "native": review["native"],
            }
            result = await gui_rpc.async_dispatch(app, apply)
            rpc.publish_management_result(app, apply, result)
            results.append(result)

    await app.run_worker(owner(), group="gui-management", exit_on_error=False).wait()
    assert results[2]["prior_inputs"] == [r["input"]["input_id"] for r in results[:2]]
    assert [
        work.entry_for(app, item).ref["input_id"] for item in app._pending_input
    ] == [results[i]["input"]["input_id"] for i in (2, 0, 1)]
    assert app._stop_requested and calls == ["first controlled input"]
    assert all(
        item["_claude_entry"]["state"] == "prepared" for item in app._pending_input
    )
    work.validate_document(work.strict_json(work.bridge(app).raw))


@pytest.mark.parametrize(
    "closing", [None, "_gui_quitting", "_conversation_rewrite_closing"]
)
async def test_actual_deferred_context_refresh_does_not_start_after_close(
    monkeypatch, closing
):
    from litetui import conversation_rewrite
    from litetui.app import LiteTUI

    started, tracked = [], []
    worker = object()
    owner = SimpleNamespace(
        ctx_loaded=False,
        model_id="fixture",
        _fetch_ctx_window=lambda: started.append(worker) or worker,
    )
    if closing:
        setattr(owner, closing, True)
    monkeypatch.setattr(
        conversation_rewrite,
        "track_descendant",
        lambda app, value: tracked.append((app, value)),
    )
    LiteTUI._resync_ctx_if_stale(owner)
    assert started == ([] if closing else [worker])
    assert tracked == ([] if closing else [(owner, worker)])
