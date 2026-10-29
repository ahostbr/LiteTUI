"""Rust-host policy facts and real Python native authority; no provider calls."""
from __future__ import annotations

from uuid import uuid4

import pytest
from _owned_seat import owned_app

from litetui import approval_relay, rpc, seat_authority
from litetui.claude_tools import ClaudeTools


@pytest.mark.asyncio
async def test_native_write_denial_and_allowance_use_real_host_authority(tmp_path, monkeypatch):
    monkeypatch.setenv(approval_relay.APPROVAL_HOST_ENV, "1")
    monkeypatch.delenv("LITETUI_OWNER", raising=False)
    monkeypatch.delenv("LITETUI_SPAWN_IDENTITY", raising=False)
    app = owned_app(rpc=True, tool_profile="strict")
    app.tools_enabled = True
    app.settings.tools_enabled = True
    app._active_tool_profile = "strict"
    app._hook_source = "rpc"
    app._stop_requested = False
    app._system = lambda *_: None
    # Actual owned store and LiteTUI._edit persist the decision, not a mock edit.
    app.store.stage(str(uuid4()))
    app.store.convo_dir.mkdir()
    app.store.pending = False
    app.conversation = [{"role": "user", "content": "controlled authority test"}]
    assert seat_authority.confirm_route(app) == "host"
    backend = app.backend
    backend.segment_id = 'fixture-segment'  # No provider session is started.
    bridge = ClaudeTools(app, backend, backend.segment_id, workspace=tmp_path)
    events: list[dict] = []
    replies: list[dict] = []
    monkeypatch.setattr(rpc, "rpc_emit", replies.append)
    decision = False
    target = tmp_path / "controlled-result.txt"

    def emit(event):
        events.append(event)
        if event["type"] == "tool_approval_requested":
            assert not target.exists()
            assert event["profile"] == "strict"
            rpc._dispatch(app, {"type": "approve", "id": f"decision-{len(events)}",
                               "approval_id": event["id"], "allow": decision, "remember": False})
    app._rpc_emit = emit
    for decision in (False, True):
        outcome = await bridge.pre_tool({
            "tool_name": "Write", "tool_input": {"file_path": str(target), "content": "fixture"},
        }, "fixture-call", None)
        permission = outcome["hookSpecificOutput"]["permissionDecision"]
        assert permission == ("allow" if decision else "deny")
        # Controlled stand-in for the native SDK executor; authorization is real.
        if permission == "allow":
            target.write_text("fixture", encoding="utf-8")
        assert target.exists() is decision
        request = [e for e in events if e["type"] == "tool_approval_requested"][-1]
        closure = [e for e in events if e["type"] == "tool_approval_closed"][-1]
        assert closure["id"] == request["id"]
        assert closure["outcome"] == ("approved" if decision else "denied")
        assert replies[-1]["ok"] is True
        assert replies[-1]["result"]["approved"] == request["id"]
        persisted = app.store.convo_path.read_text(encoding="utf-8")
        assert request["id"] in persisted and closure["outcome"] in persisted
        assert not app._approval_waiters and not app._approval_authority_records
    assert len([e for e in events if e["type"] == "tool_approval_requested"]) == 2
    assert app.settings.tool_always_allow == []
    assert target.read_text(encoding="utf-8") == "fixture"
    bridge.stop()
    app.store.release()
