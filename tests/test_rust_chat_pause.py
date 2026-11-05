"""Owned host chat pause uses canonical setter and independent revision evidence."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_rust_runtime_controls import app as owner_app
from test_rust_runtime_controls import armed as owner_armed
from test_rust_runtime_controls import switch_app as owner_switch

from litetui import background_input as work
from litetui import command_surface, gui_rpc
from litetui import native_chat_pause as pause
from litetui import native_runtime_controls as controls
from litetui.event_origin import EventOrigin
from litetui.plugins import misc

app, armed, switch_app = owner_app, owner_armed, owner_switch
pytestmark = pytest.mark.asyncio


def command(app, operation, **fields):
    bridge = work.bridge(app)
    return {"type": pause.PREFIX + operation, "id": "pause-" + operation,
            "version": 1, **bridge.identity, "work_instance": bridge.instance, **fields}


def register(app):
    assert app.plugins.commands['/pause'].handler is misc._cmd_pause
    app.set_paused(False, announce=False)


async def test_canonical_toggle_explicit_readback_one_use_and_stale_revision(armed):
    app, _ = armed
    register(app)
    saved = copy.deepcopy(app.conversation)
    bridge = work.bridge(app)
    raw = bridge.raw
    review = gui_rpc.dispatch(app, command(app, "review", origin=None))
    assert review["stage"] == "idle" and not review["paused"]
    receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    assert app.paused and receipt["revision"] == review["revision"] + 1
    with pytest.raises(ValueError, match="exact apply"):
        gui_rpc.dispatch(app, command(app, "readback", token=review["token"], apply_id="wrong"))
    readback = gui_rpc.dispatch(app, command(app, "readback", token=review["token"], apply_id=receipt["apply_id"]))
    assert readback["paused"] and readback["kind"] == "chat_pause_verified"
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    review = gui_rpc.dispatch(app, command(app, "review", origin=None))
    # An external canonical toggle away and back must invalidate the old review.
    app.set_paused(False, announce=False)
    app.set_paused(True, announce=False)
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    review = gui_rpc.dispatch(app, command(app, "review", origin=None))
    receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    readback = gui_rpc.dispatch(app, command(app, "readback", token=review["token"], apply_id=receipt["apply_id"]))
    assert not readback["paused"] and app.conversation == saved and bridge.raw == raw


@pytest.mark.parametrize("fault", ["provider", "registration", "owner", "closing", "overflow"])
async def test_refused_review_has_no_effect(armed, monkeypatch, fault):
    app, _ = armed
    register(app)
    bridge = work.bridge(app)
    cmd = command(app, "review", origin=None)
    if fault == "provider":
        monkeypatch.setattr(bridge, "provider", "codex")
    elif fault == "registration":
        monkeypatch.setitem(app.plugins.commands, "/pause", replace(app.plugins.commands["/pause"], handler=lambda *_: None))
    elif fault == "owner":
        cmd["work_instance"] = "foreign"
    elif fault == "closing":
        app._gui_quitting = True
    else:
        app._chat_pause_revision = pause.MAX_REVISION
    with pytest.raises(ValueError):
        gui_rpc.dispatch(app, cmd)
    assert not app.paused and getattr(bridge, "round_pause", None) is None


async def test_captured_active_owner_held_stage_stop_and_readback_mismatch(armed):
    app, _ = armed
    register(app)
    bridge = work.bridge(app)
    origin = EventOrigin(app.convo_id, "foreground", "fbcac1be-d3b9-4fdd-93b4-90c2f7d61f34", "rpc", "execution")
    worker = SimpleNamespace(is_finished=False, _task=asyncio.current_task())
    bridge.executions[origin.execution_id] = (worker, origin)
    review = gui_rpc.dispatch(app, command(app, "review", origin=origin.envelope_fields()))
    assert review["stage"] == "active"
    receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    assert receipt["paused"] and receipt["stage"] == "active"
    app._chat_pause_waiter = worker._task
    readback = gui_rpc.dispatch(app, command(app, "readback", token=review["token"], apply_id=receipt["apply_id"]))
    assert readback["stage"] == "held"
    review = gui_rpc.dispatch(app, command(app, "review", origin=origin.envelope_fields()))
    app._stop_requested = True
    with pytest.raises(ValueError, match="changed"):
        gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    assert app.paused
    worker.is_finished = True
    # Stop retires the real owner. Explicit idle resume remains possible.
    review = gui_rpc.dispatch(app, command(app, "review", origin=None))
    receipt = gui_rpc.dispatch(app, command(app, "apply", token=review["token"]))
    app.set_paused(True, announce=False)
    with pytest.raises(ValueError, match="independently"):
        gui_rpc.dispatch(app, command(app, "readback", token=review["token"], apply_id=receipt["apply_id"]))
    controls._state(app).bridge.executions.clear()

async def test_registered_f11_pause_routes_without_toggling(armed):
    app, _ = armed
    register(app)
    catalog = command_surface.catalog(app, {"session_id": app.convo_id})
    row = next(row for row in catalog["entries"] if row["tokens"] == ["/pause"])
    assert row["native_action"] == "chat_pause"
    result = command_surface.resolve(app, {"version": 1, "session_id": app.convo_id,
        "agent_id": catalog["agent_id"], "snapshot": catalog["snapshot"], "command": "/pause"})
    assert result["native_action"] == "chat_pause" and not app.paused
