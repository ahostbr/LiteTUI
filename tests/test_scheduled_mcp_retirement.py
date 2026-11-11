"""Exact retired Work inspection for a published MCP/scheduled New handoff."""
from __future__ import annotations

import asyncio
import copy

import pytest
from test_rust_background_input import app as _app
from test_rust_background_input import command
from test_rust_background_input import switch_app as _switch_app
from test_rust_scheduled_publication import (
    proposed_job,
    published_rpc,
    readback_command,
)

from litetui import background_input as work
from litetui import native_mcp_lifecycle as mcp
from litetui import native_schedule
from litetui.agent_ownership import OwnershipError
from litetui.llm_backend import BackendError

app, switch_app = _app, _switch_app
pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]


async def negotiated(app, monkeypatch):
    app._launch_options.work_lifecycle_version = 1
    app._launch_options.mcp_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app.settings.mcp_enabled = False
    app._cli_tool_profile = app._active_tool_profile = "strict"
    app._invocation_saved_values["mcp_enabled"] = True
    app._launch_overrides["mcp_enabled"] = False

    def forbidden(*_args, **_kwargs):
        pytest.fail("MCP transition inspection must never create a transport")

    monkeypatch.setattr(app.mcp, "_build", forbidden)
    capture = await published_rpc(app, monkeypatch, command(
        app, type="gui.work.native_capture", runtime_controls_version=1,
        mcp_lifecycle_version=1))
    activate = command(app, type="gui.work.native_activate", instance=capture["instance"],
                       activation_token=capture["activation_token"],
                       scheduled_transition_version=1, runtime_controls_version=1,
                       mcp_lifecycle_version=1)
    return activate


@pytest.mark.parametrize("kind", ["cron", "loop"])
async def test_published_mcp_scheduled_new_retires_once_then_waits_for_readback_and_recapture(app, monkeypatch, kind):
    import test_rust_scheduled_publication as scheduled

    activation = await negotiated(app, monkeypatch)
    monkeypatch.setattr(scheduled, "activation", lambda _app: activation)
    job, proposal, cmd, events = await proposed_job(app, monkeypatch, kind)
    original, history = app.convo_path, app.convo_path.read_bytes()
    old = work.bridge(app)
    old_mcp = mcp._state(app)
    assert mcp.enabled(app) and old_mcp.bridge is old
    mcp.require_inventory(app)
    result = await published_rpc(app, monkeypatch, cmd)
    assert result["status"] == "staged", f"{result.get('reason')}; transition={result.get('transition')}"
    assert old.shutdown_task.done() and old.shutdown_task.result() == old.transition_proof
    assert job.run_count == 1 and job.last_fired_slot == proposal["due_slot"]
    assert app.convo_id == proposal["transition"]["target_session_id"]
    assert original.read_bytes() == history and not app._rewrite_test_requests
    assert mcp._state(app) is None and not app.mcp.servers
    assert app._native_work_deferred.handle is None
    with pytest.raises(ValueError, match="readback"):
        work.capture(app, command(app, runtime_controls_version=1, mcp_lifecycle_version=1))

    def before_read(row):
        assert row["ok"] and app._native_work_deferred.handle is None
        assert not work.enabled(app)

    read = await published_rpc(app, monkeypatch, readback_command(app, result), before_emit=before_read)
    assert read["verified"] and read["deferred_readback"]["record"] == result["deferred"]
    capture = await published_rpc(app, monkeypatch, command(
        app, type="gui.work.native_capture", id="new-capture",
        runtime_controls_version=1, mcp_lifecycle_version=1))
    activate = command(app, type="gui.work.native_activate", id="new-activate",
                       instance=capture["instance"], activation_token=capture["activation_token"],
                       scheduled_transition_version=1, runtime_controls_version=1,
                       mcp_lifecycle_version=1)

    def before_activate(row):
        assert row["ok"] and not work.enabled(app)
        assert app._native_work_deferred.handle is None and mcp._state(app) is None

    await published_rpc(app, monkeypatch, activate, before_emit=before_activate)
    for _ in range(100):
        deferred = app._native_work_deferred
        if deferred is None:
            break
        if deferred.worker is not None:
            await deferred.worker.wait()
        await asyncio.sleep(.01)
    assert app._native_work_deferred is None
    value = work.bridge(app)
    await asyncio.wait_for(asyncio.gather(*(entry.observer for entry in value.inputs.values() if entry.observer)), 5)
    assert len(value.inputs) == job.run_count == 1
    assert len(app._rewrite_test_requests) == 1
    assert mcp.enabled(app) and native_schedule.enabled(app)
    assert mcp._state(app).bridge is value and mcp._state(app) is not old_mcp
    mcp.require_inventory(app)
    assert not app.mcp.servers and any(row["type"] == "work_input" for row in events)


async def retired(app, monkeypatch):
    activation = await negotiated(app, monkeypatch)
    await published_rpc(app, monkeypatch, activation)
    value = work.bridge(app)
    proof = await work.before_identity_transition(app)
    assert value.shutdown_task.done() and proof == value.transition_proof
    assert mcp.transition_refusal(app) is None
    return value, mcp._state(app)


async def test_completed_quiet_retirement_allows_only_transition_inspection(app, monkeypatch):
    from test_rust_mcp_lifecycle import cmd

    value, _state = await retired(app, monkeypatch)
    assert await work.before_identity_transition(app) == value.transition_proof
    with pytest.raises(ValueError, match="admission is closed"):
        mcp.require_inventory(app)
    with pytest.raises(ValueError, match="admission is closed"):
        await mcp.dispatch(app, cmd(app, "snapshot"))
    with pytest.raises(ValueError, match="admission is closed"):
        work.reserve(app)
    assert not app._rewrite_test_requests and not app.mcp.servers


async def test_closing_alone_never_permits_transition_inventory(app, monkeypatch):
    activation = await negotiated(app, monkeypatch)
    await published_rpc(app, monkeypatch, activation)
    work.begin_close(app)
    with pytest.raises(ValueError, match="completed quiet Work retirement"):
        mcp.transition_refusal(app)
    assert work.bridge(app).shutdown_task is None
    await work.settle_before_shutdown(app)


@pytest.mark.parametrize("kind", [
    "missing-proof", "changed-proof", "missing-task", "pending-task", "cancelled-task",
    "failed-task", "copied-result-task", "cutoff", "sequence", "ledger",
    "queue", "reservation", "quitting", "transition-cancel", "mcp-enabled",
    "manager", "state-app", "references", "inventory", "transport", "failed-transport",
    "manager-closing", "manager-maintenance", "mcp-action", "runtime-retirement",
    "runtime-task-owner", "runtime-tickets", "authority",
])
async def test_transition_inventory_rejects_inexact_retirement_or_live_owner(app, monkeypatch, kind):
    from litetui import native_runtime_controls as controls

    value, state = await retired(app, monkeypatch)
    runtime = controls._state(app)
    temporary = None
    original = value.path.read_bytes()
    with monkeypatch.context() as changed:
        if kind == "missing-proof":
            changed.setattr(value, "transition_proof", None)
        elif kind == "changed-proof":
            changed.setattr(value, "transition_proof", {**value.transition_proof, "worker_settled": False})
        elif kind == "missing-task":
            changed.setattr(value, "shutdown_task", None)
        elif kind in {"pending-task", "cancelled-task", "failed-task", "copied-result-task", "mcp-action"}:
            async def substitute():
                if kind == "failed-task":
                    raise ValueError("controlled failed retirement")
                if kind == "copied-result-task":
                    return copy.deepcopy(value.transition_proof)
                await asyncio.Event().wait()
            temporary = asyncio.create_task(substitute())
            if kind == "cancelled-task":
                temporary.cancel()
            if kind in {"cancelled-task", "failed-task", "copied-result-task"}:
                await asyncio.gather(temporary, return_exceptions=True)
            changed.setattr(state if kind == "mcp-action" else value,
                            "action" if kind == "mcp-action" else "shutdown_task", temporary)
        elif kind == "cutoff":
            changed.setattr(value, "close_event", {**value.close_event, "instance": "foreign"})
        elif kind == "sequence":
            changed.setattr(value, "sequence", value.sequence + 1)
        elif kind == "ledger":
            value.path.write_bytes(original + b" ")
        elif kind == "queue":
            changed.setattr(app, "_pending_input", [{"content": "late input"}])
        elif kind == "reservation":
            changed.setattr(value, "reservations", {"late reservation"})
        elif kind == "quitting":
            changed.setattr(app, "_gui_quitting", True, raising=False)
        elif kind == "transition-cancel":
            changed.setattr(app, "_conversation_transition_shutdown_requested", True, raising=False)
        elif kind == "mcp-enabled":
            changed.setattr(app.settings, "mcp_enabled", True)
        elif kind == "manager":
            changed.setattr(state, "manager", object())
        elif kind == "state-app":
            changed.setattr(state, "app", object())
        elif kind == "references":
            changed.setattr(state, "references", (object(), *state.references[1:]))
        elif kind == "inventory":
            changed.setattr(app.mcp, "configs", {"unexpected": {"command": "never-run"}})
        elif kind == "transport":
            changed.setattr(app.mcp, "servers", {"unexpected": object()})
        elif kind == "failed-transport":
            changed.setattr(app.mcp, "_stop_failed", {"unexpected"})
        elif kind == "manager-closing":
            changed.setattr(app.mcp, "_closing", True)
        elif kind == "manager-maintenance":
            changed.setattr(app.mcp, "_maint_active", 1)
        elif kind == "runtime-retirement":
            changed.setattr(value, "runtime_controls_retirement", {})
        elif kind == "runtime-task-owner":
            changed.setattr(runtime, "shutdown", None)
        elif kind == "runtime-tickets":
            changed.setattr(runtime, "tickets", [object()])
        elif kind == "authority":
            changed.setattr(app._agent_session._lease, "pid", -1)
        else:
            raise AssertionError(kind)
        try:
            with pytest.raises((ValueError, BackendError, OwnershipError)):
                mcp.transition_refusal(app)
        finally:
            if kind == "ledger":
                value.path.write_bytes(original)
            if temporary is not None:
                temporary.cancel()
                await asyncio.gather(temporary, return_exceptions=True)
    assert mcp.transition_refusal(app) is None
    with pytest.raises(ValueError, match="admission is closed"):
        mcp.require_inventory(app)
    assert value.path.read_bytes() == original and not app._rewrite_test_requests
