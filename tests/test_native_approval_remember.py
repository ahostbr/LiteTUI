"""Standing approval parity against disposable real owned settings and authority."""
from __future__ import annotations

import asyncio
import copy
import json

import pytest
from test_rust_background_input import app as _app
from test_rust_background_input import switch_app as _switch_app

from litetui import native_approval_remember as remember
from litetui import settings_runtime, tool_approval, tool_policy
from litetui.settings_service import SettingChange

app, switch_app = _app, _switch_app
pytestmark = pytest.mark.asyncio
DECISION = tool_policy.PolicyDecision(action=tool_policy.CONFIRM, profile="strict",
    capabilities=frozenset({"workspace_write"}), reason="Private fixture write")


async def request(app, monkeypatch):
    events = []
    monkeypatch.setattr(app, "_rpc_emit", lambda event: events.append(copy.deepcopy(event)))
    task = asyncio.create_task(tool_approval.approve_over_rpc(app, "private_probe", {"text": "fixture"}, DECISION))
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.01)
    assert events and events[0]["type"] == "tool_approval_requested"
    return task, events, events[0]


async def test_review_is_inert_and_exact_rule_is_independently_saved(app, monkeypatch):
    task, events, req = await request(app, monkeypatch)
    try:
        wire = req["remember_scope"]
        assert wire["rule"] == "private_probe:workspace_write"
        assert wire["scope"] == "conversation" and wire["conversation_id"] == app.convo_dir.name
        assert wire["capabilities"] == ["workspace_write"]
        assert not app.settings.tool_always_allow
        before = (app.convo_dir / "settings.json").read_bytes()
        assert tool_approval.resolve_over_rpc(app, req["id"], True, True, remember_scope=wire)
        assert not app.settings.tool_always_allow  # The ACK is not a saved rule.
        assert await task == tool_approval.ALWAYS
        receipt = events[-1]["remembered"]
        assert receipt == {"scope": wire, "saved": True, "session_only": False}
        raw = json.loads((app.convo_dir / "settings.json").read_bytes())
        assert raw["execution"]["tool_always_allow"] == [wire["rule"]]
        assert app.settings.tool_always_allow == [wire["rule"]]
        assert (app.convo_dir / "settings.json").read_bytes() != before
        assert tool_policy.rule_key("private_probe", {"workspace_write", "network"}) not in app.settings.tool_always_allow
        assert not app._native_approval_remember
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("change", ["snapshot", "tool", "capabilities", "conversation_id", "scope"])
async def test_changed_review_cannot_grant_or_save(app, monkeypatch, change):
    task, events, req = await request(app, monkeypatch)
    try:
        wire = copy.deepcopy(req["remember_scope"])
        wire[change] = ["network"] if change == "capabilities" else "changed"
        assert not tool_approval.resolve_over_rpc(app, req["id"], True, True, remember_scope=wire)
        assert not task.done() and not app.settings.tool_always_allow
        assert tool_approval.resolve_over_rpc(app, req["id"], False)
        assert await task == tool_approval.DENIED
        assert "remembered" not in events[-1]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stale_saved_settings_refuse_without_merging_rules(app, monkeypatch):
    task, _, req = await request(app, monkeypatch)
    try:
        service = settings_runtime.service_for(app)
        snap = service.snapshot(app.convo_dir.name, read_only=True)
        assert service.save_patch(app.convo_dir.name, [SettingChange("tool_always_allow", ["other:network"], "conversation")], snap.revisions).fully_saved
        assert not tool_approval.resolve_over_rpc(app, req["id"], True, True, remember_scope=req["remember_scope"])
        assert not app.settings.tool_always_allow
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("allow", [False, True])
async def test_deny_and_once_never_write_a_standing_rule(app, monkeypatch, allow):
    task, events, req = await request(app, monkeypatch)
    try:
        before = (app.convo_dir / "settings.json").read_bytes()
        assert tool_approval.resolve_over_rpc(app, req["id"], allow)
        assert await task == (tool_approval.ONCE if allow else tool_approval.DENIED)
        assert (app.convo_dir / "settings.json").read_bytes() == before
        assert "remembered" not in events[-1] and not app.settings.tool_always_allow
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_saved_readback_failure_is_explicit_session_only(app, monkeypatch):
    task, events, req = await request(app, monkeypatch)
    try:
        scope = app._native_approval_remember[req["id"]]
        original = scope.service.save_patch
        def fail_readback(*args, **kwargs):
            result = original(*args, **kwargs)
            monkeypatch.setattr(scope.service, "snapshot", lambda *a, **k: (_ for _ in ()).throw(OSError("private readback failure")))
            return result
        monkeypatch.setattr(scope.service, "save_patch", fail_readback)
        assert tool_approval.resolve_over_rpc(app, req["id"], True, True, remember_scope=req["remember_scope"])
        assert await task == tool_approval.ALWAYS
        assert events[-1]["remembered"]["saved"] is False
        assert events[-1]["remembered"]["session_only"] is True
        assert app.settings.tool_always_allow == [req["remember_scope"]["rule"]]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_cancel_cleans_scope_and_never_saves(app, monkeypatch):
    task, events, _ = await request(app, monkeypatch)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not app._native_approval_remember and not app.settings.tool_always_allow
    assert "remembered" not in events[-1]

async def test_failed_write_keeps_only_explicit_session_rule(app, monkeypatch):
    from types import SimpleNamespace
    task, events, req = await request(app, monkeypatch)
    try:
        before = (app.convo_dir / "settings.json").read_bytes()
        scope = app._native_approval_remember[req["id"]]
        monkeypatch.setattr(scope.service, "save_patch", lambda *a, **k: SimpleNamespace(fully_saved=False))
        assert tool_approval.resolve_over_rpc(app, req["id"], True, True, remember_scope=req["remember_scope"])
        assert await task == tool_approval.ALWAYS
        assert (app.convo_dir / "settings.json").read_bytes() == before
        assert events[-1]["remembered"]["saved"] is False
        assert app.settings.tool_always_allow == [req["remember_scope"]["rule"]]
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_changed_runtime_after_ack_never_installs_unreviewed_rule(app, monkeypatch):
    task, events, req = await request(app, monkeypatch)
    try:
        assert tool_approval.resolve_over_rpc(app, req["id"], True, True, remember_scope=req["remember_scope"])
        app.settings.tool_always_allow = ["other:network"]
        with pytest.raises(ValueError, match="settings changed"):
            await task
        assert app.settings.tool_always_allow == ["other:network"]
        assert "remembered" not in events[-1] and events[-1]["reason"] == "error"
        assert not app._native_approval_remember
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

async def test_emitted_review_does_not_alias_retained_source_capabilities(app, monkeypatch):
    task, _events, req = await request(app, monkeypatch)
    try:
        scope = app._native_approval_remember[req["id"]]
        # Replace the emitter with one retaining the original supplied object too.
        assert req["remember_scope"] is not scope.wire
        published = remember.capture(
            app, "private-copy", "private_probe", DECISION.capabilities)
        published["capabilities"].append("network")
        assert app._native_approval_remember["private-copy"].wire["capabilities"] == ["workspace_write"]
        app._native_approval_remember.pop("private-copy")
        assert tool_approval.resolve_over_rpc(app, req["id"], False)
        assert await task == tool_approval.DENIED
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
