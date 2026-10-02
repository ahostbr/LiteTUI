"""Current registration selects new requests; issued permissions keep their parent."""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from litetui import (
    approval_relay,
    harness,
    runtime_log,
    seat_authority,
    tool_approval,
    tool_policy,
)
from litetui.app import LiteTUI

OLD = "old-parent"
NEW = "new-parent"


@pytest.fixture
def arm(tmp_path, monkeypatch):
    # Only this disposable registry is read. No fleet row is registered or changed.
    agents = tmp_path / "agents"
    agents.mkdir()
    monkeypatch.setattr(harness, "AGENTS_DIR", agents)
    monkeypatch.setattr(harness, "INBOX_ROOT", tmp_path / "inbox")
    app = LiteTUI()
    app._owner_seat = False
    app._spawned_seat = app._agent_launched = True
    app._spawner_id = OLD
    app.seat.spawned_by = OLD
    app.seat.registered = True
    app._active_tool_profile = tool_policy.INTERACTIVE
    app._hook_source = "harness"
    app._system = lambda *_: None
    app._deliver_inbox = lambda *_: None
    row = agents / f"{app.seat.agent_id}.json"

    def reparent(parent):
        row.write_text(json.dumps({"agent_id": app.seat.agent_id, "spawned_by": parent}), encoding="utf-8")

    reparent(OLD)
    calls, logs = [], []
    monkeypatch.setattr(runtime_log, "record", lambda event, **kw: logs.append((event, kw)))
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)

    def cli(argv, **kw):
        if argv[0] == "send":
            argv = [argv[0], argv[1], Path(argv[3]).read_text(encoding="utf-8"), *argv[4:]]
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    # Capture the external transport only: Seat.send and the approval door are real.
    monkeypatch.setattr(harness, "_cli", cli)
    return app, row, reparent, calls, logs


async def door(app, tmp_path):
    return await app._authorize_action("bash", {"command": "rm -rf ./build"},
                                       tool_policy.SHELL_POLICY, workspace=tmp_path)


async def request(calls, count):
    for _ in range(100):
        sent = [argv for argv in calls if argv[0] == "send"]
        if len(sent) >= count:
            argv = sent[count - 1]
            return argv[1], re.search(r"appr-[0-9a-f]{12}", argv[2]).group()
        await asyncio.sleep(0.01)
    raise AssertionError("approval never reached transport")


@pytest.mark.asyncio
async def test_reparent_routes_next_request_but_cannot_answer_an_issued_request(arm, tmp_path):
    app, _row, reparent, calls, logs = arm
    app.settings.relay_approval_timeout_s = 1
    first = asyncio.create_task(door(app, tmp_path))
    destination, ident = await request(calls, 1)
    assert destination == OLD
    reparent(NEW)
    assert not approval_relay.take_answer(app, {"from": NEW, "body": f"APPROVE {ident}"})
    assert approval_relay.take_answer(app, {"from": OLD, "body": f"DENY {ident}"})
    assert (await first)[1] is False

    second = asyncio.create_task(door(app, tmp_path))
    destination, ident = await request(calls, 2)
    try:
        assert destination == NEW, "launch-cached parent must not receive the next request"
        assert not approval_relay.take_answer(app, {"from": OLD, "body": f"APPROVE {ident}"})
        app._receive_mail({"from": NEW, "body": f"APPROVE {ident}"})
        assert await second is None
        assert app._spawner_id == OLD, "launch provenance is not mutable approval authority"
        assert [kw["status"] for event, kw in logs if event == "approval_relay"] == ["denied", "approved"]
    finally:
        if not second.done():
            second.cancel()
            await asyncio.gather(second, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["missing", "corrupt", "wrong-id", "no-parent", "self", "blank", "non-string", "flag", "path"])
async def test_invalid_current_presence_never_falls_back_to_cached_parent(arm, tmp_path, invalid):
    app, row, reparent, calls, logs = arm
    app.settings.relay_approval_timeout_s = 0.01
    if invalid == "missing":
        row.unlink()
    elif invalid == "corrupt":
        row.write_text("{bad", encoding="utf-8")
    elif invalid == "wrong-id":
        row.write_text(json.dumps({"agent_id": "other-seat", "spawned_by": NEW}), encoding="utf-8")
    else:
        reparent({"no-parent": None, "self": app.seat.agent_id, "blank": " ",
                  "non-string": [], "flag": "--force", "path": "../parent"}[invalid])
    assert seat_authority.confirm_route(app) == "spawner", "damaged relay must not gain a human/host fallback"
    result = await door(app, tmp_path)
    assert result[1] is False
    assert calls == [], "no valid current parent means no mail, especially not to the cached parent"
    assert [kw["status"] for event, kw in logs if event == "approval_relay"] == ["absent"]


@pytest.mark.asyncio
async def test_registered_new_parent_without_launch_parent_is_adopted(arm, tmp_path):
    app, _row, reparent, _calls, _logs = arm
    app._spawner_id = None
    reparent(NEW)
    app.settings.relay_approval_timeout_s = 0.01
    assert seat_authority.confirm_route(app) == "spawner"
    assert (await door(app, tmp_path))[1] is False  # no answer, never permission
    assert _calls[0][1] == NEW


@pytest.mark.asyncio
async def test_dead_current_parent_refuses_without_trying_old_parent(arm, tmp_path, monkeypatch):
    app, _row, reparent, calls, logs = arm
    reparent(NEW)
    monkeypatch.setattr(harness, "_cli", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=1))
    assert (await door(app, tmp_path))[1] is False
    assert len(calls) == 1 and calls[0][1] == NEW
    assert [kw["status"] for event, kw in logs if event == "approval_relay"] == ["absent"]


def test_initial_register_records_parent_but_heartbeat_does_not_overwrite_adoption(arm):
    app, row, reparent, calls, _logs = arm
    assert app.seat.register()
    assert calls[-1][calls[-1].index("--spawned-by") + 1] == OLD
    reparent(NEW)
    assert app.seat.heartbeat()
    assert "--spawned-by" not in calls[-1]
    assert json.loads(row.read_text(encoding="utf-8"))["spawned_by"] == NEW


@pytest.mark.asyncio
async def test_rpc_mail_authority_uses_current_parent_and_freezes_each_request(arm):
    app, _row, reparent, _calls, _logs = arm
    emitted = []
    app._rpc_emit = emitted.append
    decision = tool_policy.PolicyDecision(tool_policy.CONFIRM, "interactive", frozenset(), "writes")
    reparent(NEW)
    pending = asyncio.create_task(tool_approval.approve_over_rpc(app, "shell", {}, decision, timeout=0.1))
    await asyncio.sleep(0)
    ident = emitted[-1]["id"]
    try:
        assert not tool_approval.take_spawner_answer(app, {"to": app.seat.agent_id, "from": OLD, "body": f"APPROVE {ident}"})
        reparent(OLD)
        assert tool_approval.take_spawner_answer(app, {"to": app.seat.agent_id, "from": NEW, "body": f"DENY {ident}"})
        assert await pending is tool_approval.DENIED
    finally:
        if not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


def test_root_without_parent_keeps_existing_route(arm):
    app, _row, reparent, _calls, _logs = arm
    app._spawner_id = None
    reparent(None)
    app._agent_launched = False
    assert seat_authority.confirm_route(app) == "hand"
    app._agent_launched = True
    assert seat_authority.confirm_route(app) == "refuse"
    app._rpc = app._approval_host = True
    assert seat_authority.confirm_route(app) == "host"
    app._spawned_seat, app._owner_seat = False, True
    assert seat_authority.confirm_route(app) == "own"
