"""Opt-in per-tool policy uses the real registry, canonical preference and publication."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import pytest_asyncio
from test_rust_background_input import app as owned_app
from test_rust_background_input import command
from test_rust_background_input import switch_app as owned_switch_app
from test_rust_runtime_controls import cmd
from test_rust_scheduled_publication import published_rpc

from litetui import native_runtime_controls as controls
from litetui import paths, settings_runtime, tool_policy

app, switch_app = owned_app, owned_switch_app
pytestmark = pytest.mark.asyncio


def forbidden(*args, **kwargs):
    raise AssertionError("Tool list/review must not run a tool or its gate")


@pytest_asyncio.fixture
async def selected(app, monkeypatch, tmp_path, request):
    monkeypatch.setattr(paths, "PLAN_PROMPT_FILE", tmp_path / "PLAN.md")
    app._launch_options.work_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app.settings.mcp_enabled = False
    app._cli_tool_profile = app._active_tool_profile = "strict"
    if getattr(request, "param", None) == "codex":
        monkeypatch.setattr(app.backend, "app_server", object(), raising=False)
    elif getattr(request, "param", None) == "claude":
        monkeypatch.setattr(app.backend, "owns_native_turns", True, raising=False)
    app.settings.tools_disabled = ["missing-plugin", "missing-plugin"]
    settings_runtime.persist_or_raise(app, app.settings)
    app.plugins.add_tool("selection-fixture", {"type": "function", "function": {
        "name": "selection_probe", "description": "Exact description 雪", "parameters": {"type": "object", "properties": {}}}},
        forbidden, gate=forbidden, policy=tool_policy.ToolPolicy(frozenset({tool_policy.READ_ONLY}), "Exact authority"))
    capture = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_capture",
        execution_version=1, runtime_controls_version=1, tool_selection_version=1))
    activation = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate",
        instance=capture["instance"], activation_token=capture["activation_token"],
        scheduled_transition_version=1, execution_version=1, runtime_controls_version=1, tool_selection_version=1))
    yield app, {"capture": capture, "activation": activation}


async def rejected(app, monkeypatch, command, reason):
    from litetui import rpc
    replies = []
    monkeypatch.setattr(rpc, "rpc_emit", lambda row: replies.append(row) if row.get("id") == command["id"] else None)
    rpc._dispatch(app, command)
    workers = [worker for worker in app.workers if worker.group == "gui-management" and not worker.is_finished]
    await asyncio.wait_for(asyncio.gather(*(worker.wait() for worker in workers)), 5)
    assert len(replies) == 1 and replies[0]["ok"] is False and reason in replies[0]["error"], replies


async def reviewed(app, monkeypatch, enabled=False, name="selection_probe"):
    listing = await published_rpc(app, monkeypatch, cmd(app, "tool_list"))
    snapshot = listing["snapshot"]
    review = await published_rpc(app, monkeypatch, cmd(app, "validate", snapshot=snapshot["snapshot"],
        generation=snapshot["generation"], change={"field": "tool_selection", "value": {"name": name, "enabled": enabled}}))
    return listing, review


def read_request(app, review, result):
    return cmd(app, "readback", action_id=review["action_id"], review_token=review["review_token"],
        apply_operation_id=result["apply_operation_id"], readback_token=result["readback_token"],
        result_sha256=controls.work.digest(controls.work.encoded(result)))


@pytest.mark.parametrize("scenario", ["success", "save-failure", "stale"])
async def test_one_tool_preserves_unknown_preferences_and_publishes_exact_generation(selected, monkeypatch, scenario):
    from litetui.settings_service import SettingsService
    app, evidence = selected
    assert evidence["capture"]["tool_selection_version"] == evidence["activation"]["tool_selection_version"] == 1
    listing, review = await reviewed(app, monkeypatch)
    row = next(row for row in listing["tools"] if row["name"] == "selection_probe")
    assert row["enabled"] and row["gated"] and row["authority"] == "read_only"
    assert row["description"] == "Exact description 雪"
    assert review["tool"] == row
    assert review["after"] == {**review["before"], "tools_disabled": ["missing-plugin", "missing-plugin", "selection_probe"]}
    assert review["first_system_before"] == review["first_system_after"]
    if scenario == "save-failure":
        monkeypatch.setattr(SettingsService, "save_patch", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("Controlled save failure")))
    if scenario == "stale":
        app.settings.tools_disabled.append("changed-after-review")
    result = await published_rpc(app, monkeypatch, {"id": "apply-selection", "type": controls.PREFIX + "apply", "review": review})
    expected = {"success": "applied", "save-failure": "session_only", "stale": "refused"}[scenario]
    assert result["status"] == expected, result
    assert controls.mutation_hold(app)
    if scenario == "stale":
        app.settings.tools_disabled.pop()
    readback = await published_rpc(app, monkeypatch, read_request(app, review, result))
    assert readback["verified"] and not controls.mutation_hold(app)
    if scenario != "stale":
        assert readback["current"]["tools_disabled"] == ["missing-plugin", "missing-plugin", "selection_probe"]
        assert result["saved_after"]["tools_disabled"] == (["missing-plugin", "missing-plugin"] if scenario == "save-failure" else readback["current"]["tools_disabled"])
    proof = (await controls.shutdown(app))["runtime_controls_shutdown"]
    assert proof["published_generation"] == (scenario != "stale")
    export = os.environ.get("LITETUI_TOOL_SELECTION_FIXTURES")
    if export:
        path = Path(export)
        cases = {} if not path.exists() else json.loads(path.read_text(encoding="utf-8"))
        cases[scenario] = {**evidence, "listing": listing, "review": review, "result": result, "readback": readback, "shutdown": proof}
        path.write_text(json.dumps(cases, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


async def test_new_authorizations_wait_for_exact_tool_readback_and_recapture_disabled(selected, monkeypatch):
    app, _ = selected
    async with controls.authorization(app) as old:
        assert old.disabled == frozenset({"missing-plugin"})
    _, review = await reviewed(app, monkeypatch)
    result = await published_rpc(app, monkeypatch, {"id": "apply-selection", "type": controls.PREFIX + "apply", "review": review})
    async def capture():
        async with controls.authorization(app) as ticket:
            return ticket.disabled, ticket.generation
    waiter = asyncio.create_task(capture())
    await asyncio.sleep(0)
    assert not waiter.done() and waiter in controls._state(app).waiters
    def before_emit(_):
        assert not waiter.done() and controls.mutation_hold(app)
    await published_rpc(app, monkeypatch, read_request(app, review, result), before_emit=before_emit)
    assert await asyncio.wait_for(waiter, 2) == (frozenset({"missing-plugin", "selection_probe"}), 1)
    assert old.disabled == frozenset({"missing-plugin"}) and old.generation == 0


async def test_per_tool_review_rejects_dynamic_unknown_stale_registry_and_unreviewed_set(selected, monkeypatch):
    app, _ = selected
    listing = await published_rpc(app, monkeypatch, cmd(app, "tool_list"))
    await rejected(app, monkeypatch, cmd(app, "validate", snapshot=listing["snapshot"]["snapshot"],
        generation=0, change={"field": "tool_selection", "value": {"name": "mcp__unknown__tool", "enabled": True}}), "statically registered")
    for operation in ("set_settings", "gui.settings.apply"):
        with pytest.raises(ValueError, match="reviewed runtime"):
            controls.operation_allowed(app, {"type": operation, "changes": {"tools_disabled": []}})
    listing, review = await reviewed(app, monkeypatch)
    app.plugins.tools[-1].spec["function"]["description"] = "Changed metadata"
    result = await published_rpc(app, monkeypatch, {"id": "apply-stale", "type": controls.PREFIX + "apply", "review": review})
    assert result["status"] == "refused" and not result["effect_attempted"]


async def test_legacy_runtime_policy_stays_strict_without_optin(app, monkeypatch):
    app._launch_options.work_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app.settings.mcp_enabled = False
    app._cli_tool_profile = app._active_tool_profile = "strict"
    capture = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_capture", runtime_controls_version=1))
    assert "tool_selection_version" not in capture and "tools_disabled" not in capture["runtime_policy"]["facts"]
    await rejected(app, monkeypatch, command(app, type="gui.work.native_activate",
        instance=capture["instance"], activation_token=capture["activation_token"], runtime_controls_version=1, tool_selection_version=1), "selection negotiation")
    await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate",
        instance=capture["instance"], activation_token=capture["activation_token"], runtime_controls_version=1))
    await rejected(app, monkeypatch, cmd(app, "tool_list"), "not negotiated")


async def test_unpublished_tool_preference_drift_cannot_create_authorization(selected):
    app, _ = selected
    app.settings.tools_disabled.append("unpublished")
    with pytest.raises(controls.AuthorizationClosed, match="without published review"):
        async with controls.authorization(app):
            raise AssertionError("unpublished policy admitted a tool")


async def test_enable_removes_only_exact_target_and_preserves_every_other_entry(selected, monkeypatch):
    from litetui.tool_list import disabled_after
    app, _ = selected
    original = ["unknown", "selection_probe", "unknown", "selection_probe", "another"]
    assert disabled_after(original, "selection_probe", True) == ["unknown", "unknown", "another"]
    assert disabled_after(original, "selection_probe", False) == original
    _, review = await reviewed(app, monkeypatch)
    result = await published_rpc(app, monkeypatch, {"id": "disable-first", "type": controls.PREFIX + "apply", "review": review})
    await published_rpc(app, monkeypatch, read_request(app, review, result))
    _, review = await reviewed(app, monkeypatch, enabled=True)
    assert review["after"]["tools_disabled"] == ["missing-plugin", "missing-plugin"]
    result = await published_rpc(app, monkeypatch, {"id": "enable-again", "type": controls.PREFIX + "apply", "review": review})
    readback = await published_rpc(app, monkeypatch, read_request(app, review, result))
    assert readback["verified"] and readback["current"]["tools_disabled"] == ["missing-plugin", "missing-plugin"]
    assert controls._state(app).generation == 2


@pytest.mark.parametrize("selected", ["codex", "claude"], indirect=True)
async def test_native_provider_inventory_and_host_preference_boundaries(selected, monkeypatch):
    app, _ = selected
    listing = await published_rpc(app, monkeypatch, cmd(app, "tool_list"))
    provider = listing["snapshot"]["facts"]["provider"]
    if provider == "codex":
        assert "provider-owned" in listing["editable_reason"]
        await rejected(app, monkeypatch, cmd(app, "validate", snapshot=listing["snapshot"]["snapshot"], generation=0,
            change={"field": "tool_selection", "value": {"name": "selection_probe", "enabled": False}}), "provider-owned")
        assert app.settings.tools_disabled == ["missing-plugin", "missing-plugin"]
    else:
        assert provider == "claude" and listing["editable_reason"] is None
        _, review = await reviewed(app, monkeypatch)
        assert "inventory remains fixed" in review["consequence"] and "explicit Claude New" in review["consequence"]
