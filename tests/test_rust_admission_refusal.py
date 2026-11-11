"""Pre-admission refusal serialization keeps exact attempts and legacy parents."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace as NS

import pytest
from test_rust_profile_echo import (
    echo_app as echo_app,  # noqa: PLC0414 - pytest fixture registration
)
from test_rust_profile_echo import prompt

from litetui import background_input, hook_host, seat_authority


def rows(wire):
    return [json.loads(line) for line in wire.getvalue().splitlines()]


def refusal(wire, session, operation="echo-owned"):
    values = rows(wire)
    event = next(row for row in values if row.get("type") == "submit_refused")
    frames = [
        row
        for row in values
        if row.get("type") == "gui.event" and row.get("event") == event
    ]
    assert len(frames) == 1
    frame = frames[0]
    assert frame["session_id"] == session and frame["operation_id"] == operation
    assert (
        frame["source"] == "rpc"
        and frame["execution_id"] is None
        and frame["hook_id"] is None
    )
    assert event["phase"] == "pre_admission" and event["reason"] == "admission"
    assert not any(
        row.get("type") in {"turn_start", "turn_end", "turn_refused"} for row in values
    )
    return event


@pytest.mark.asyncio
async def test_native_floor_refusal_has_no_execution_or_transcript_write(
    echo_app, monkeypatch
):
    app, wire, origins = echo_app
    app._gui_rpc_enabled = True
    monkeypatch.setattr(background_input, "enabled", lambda _: True)
    monkeypatch.setattr(
        seat_authority, "floor_refusal", lambda *_: "Controlled below-floor refusal"
    )
    before = app.convo_path.read_bytes()
    session = app.convo_id
    prompt(app)
    event = refusal(wire, session)
    assert event["detail"] == "Controlled below-floor refusal"
    assert not origins and app.convo_path.read_bytes() == before
    assert app.rejected_prompts[-1]["operation_id"] == "echo-owned"
    assert rows(wire)[-1]["result"] == {"turn": "accepted"}


@pytest.mark.asyncio
@pytest.mark.parametrize("stopped", [False, True])
async def test_async_hook_refusal_keeps_preawait_attempt_after_ambient_drift(
    echo_app, monkeypatch, stopped
):
    app, wire, origins = echo_app
    app._gui_rpc_enabled = True
    monkeypatch.setattr(background_input, "enabled", lambda _: True)
    monkeypatch.setattr(
        hook_host, "snapshot", lambda _: NS(hooks=("controlled",), error=None)
    )
    pending = []
    monkeypatch.setattr(
        app, "run_worker", lambda coroutine, **_: pending.append(coroutine)
    )

    async def drain(_app):
        return None

    async def dispatch(_app, *_, **__):
        app._gui_operation_id = "foreign-operation"
        app._gui_next_operation_id = "foreign-next"
        app._stop_requested = stopped
        # A suspended denial must never borrow the current conversation.
        app.store.convo_id = "foreign-session"
        await asyncio.sleep(0)
        return NS(allowed=False, reason="" if stopped else "Controlled hook denial")

    monkeypatch.setattr(hook_host, "drain_lifecycle", drain)
    monkeypatch.setattr(hook_host, "dispatch", dispatch)
    session, before = app.convo_id, app.convo_path.read_bytes()
    prompt(app)
    assert len(pending) == 1 and not any(
        row.get("type") == "submit_refused" for row in rows(wire)
    )
    try:
        await pending.pop()
        event = refusal(wire, session)
        assert event["detail"] == (
            "Stopped by user" if stopped else "Controlled hook denial"
        )
        assert not origins
    finally:
        app.store.convo_id = session
        app._gui_next_operation_id = None
    assert app.convo_path.read_bytes() == before


@pytest.mark.asyncio
async def test_legacy_floor_keeps_supervising_parent_turn_end(echo_app, monkeypatch):
    app, wire, origins = echo_app
    monkeypatch.setattr(background_input, "enabled", lambda _: False)
    monkeypatch.setattr(
        seat_authority, "floor_refusal", lambda *_: "Controlled legacy floor refusal"
    )
    prompt(app)
    assert not origins
    ends = [row for row in rows(wire) if row.get("type") == "turn_end"]
    assert len(ends) == 1 and ends[0]["stopReason"] == "fleet_floor"
    assert ends[0]["error"] == "Controlled legacy floor refusal"
    assert not any(row.get("type") == "submit_refused" for row in rows(wire))
