"""Actual source control negotiation, canonical setters and publication receipts."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest
import pytest_asyncio
from test_rust_background_input import app as owned_app
from test_rust_background_input import command
from test_rust_background_input import switch_app as owned_switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import native_runtime_controls as controls
from litetui import paths

app, switch_app = owned_app, owned_switch_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def armed(app, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "PLAN_PROMPT_FILE", tmp_path / "PLAN.md")
    app._launch_options.work_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app.settings.mcp_enabled = False
    app._cli_tool_profile = app._active_tool_profile = "strict"
    capture = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_capture",
        execution_version=1, runtime_controls_version=1))
    activation = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate",
        instance=capture["instance"], activation_token=capture["activation_token"],
        scheduled_transition_version=1, execution_version=1, runtime_controls_version=1))
    return app, {"capture": capture, "activation": activation}


def cmd(app, operation, **fields):
    return {"id": "runtime-" + operation, "type": controls.PREFIX + operation,
            **controls._base(controls._state(app)), **fields}


async def snapshot_review(app, monkeypatch, field, value):
    snapshot = await published_rpc(app, monkeypatch, cmd(app, "snapshot"))
    review = await published_rpc(app, monkeypatch, cmd(app, "validate", snapshot=snapshot["snapshot"],
        generation=snapshot["generation"], change={"field": field, "value": value}))
    return snapshot, review


def export_case(case):
    target = os.environ.get("LITETUI_RUNTIME_CONTROL_FIXTURES")
    if not target:
        return
    path = Path(target)
    cases = {} if not path.exists() else {case["name"]: case for case in work.strict_json(path.read_bytes())["cases"]}
    cases[case["name"]] = case
    path.write_bytes(work.encoded({"version": 1, "cases": list(cases.values())}))


@pytest.mark.parametrize(("scenario", "field"), [
    ("tools", "tools_enabled"), ("plan", "plan_mode"), ("profile", "tool_profile"),
    ("stale-plan", "plan_mode"), ("failed-save", "tools_enabled"), ("saved-before-error", "tools_enabled")])
async def test_control_publication_and_saved_evidence(armed, monkeypatch, scenario, field):
    app, case = armed
    selected = "strict" if field == "tool_profile" else not getattr(app, {"tools_enabled": "tools_enabled", "plan_mode": "_plan_mode"}[field])
    snapshot, review = await snapshot_review(app, monkeypatch, field, selected)
    assert snapshot["generation"] == 0
    if scenario == "stale-plan":
        paths.PLAN_PROMPT_FILE.write_text("Changed after review", encoding="utf-8")
    if scenario in ("failed-save", "saved-before-error"):
        from litetui.settings_service import SettingsService
        save = SettingsService.save_patch
        def failure(self, *args, **kwargs):
            if scenario == "saved-before-error":
                save(self, *args, **kwargs)
            raise OSError("Controlled write failure")
        monkeypatch.setattr(SettingsService, "save_patch", failure)

    result = await published_rpc(app, monkeypatch, {"type": controls.PREFIX + "apply", "id": "apply", "review": review})
    status = {"stale-plan": "refused", "failed-save": "session_only"}.get(scenario, "applied")
    assert result["status"] == status, result["reason"]
    assert controls.mutation_hold(app)
    readback = await published_rpc(app, monkeypatch, cmd(app, "readback",
        action_id=review["action_id"], review_token=review["review_token"], apply_operation_id=result["apply_operation_id"],
        readback_token=result["readback_token"], result_sha256=work.digest(work.encoded(result))))
    assert readback["verified"] and not controls.mutation_hold(app)
    if scenario == "profile":
        assert app._cli_tool_profile is None
    if status in ("applied", "session_only"):
        assert readback["current"] == review["after"]
        assert readback["first_system"] == review["first_system_after"]
    shutdown = await controls.shutdown(app)
    assert shutdown["runtime_controls_historical_shutdown"] is None
    proof = shutdown["runtime_controls_shutdown"]
    assert proof["published_generation"] == (status != "refused")
    assert proof["disposition"] == ("no_effect" if status == "refused" else "verified")
    export_case({"name": scenario, **case, "snapshot": snapshot, "review": review, "result": result,
                 "readback": readback, "shutdown": proof})


async def test_exact_prompt_policy_and_old_wire_rejected(armed):
    app, _ = armed
    policy = {"version": 1, "work_instance": work.bridge(app).instance, "generation": 0}
    request = {"type": "prompt", "message": "continue", "id": "prompt", "runtime_policy": policy}
    controls.operation_allowed(app, request)
    for bad in ({**request, "tool_profile": "strict"}, {**request, "runtime_policy": {**policy, "generation": True}},
                {**request, "runtime_policy": {**policy, "generation": 1}}, {**request, "message": "/tools"},
                {"type": "set", "key": "profile", "value": "strict"}):
        with pytest.raises(ValueError):
            controls.operation_allowed(app, bad)


async def test_authorization_waits_for_publication_and_recaptures_current_profile(armed, monkeypatch):
    app, _ = armed
    _, review = await snapshot_review(app, monkeypatch, "tool_profile", "interactive")
    result = await published_rpc(app, monkeypatch, {"id": "apply", "type": controls.PREFIX + "apply", "review": review})
    state = controls._state(app)
    async def authorize():
        async with controls.authorization(app, profile="strict") as ticket:
            return ticket.profile, ticket.generation, ticket.context
    waiter = asyncio.create_task(authorize())
    await asyncio.sleep(0)
    assert waiter in state.waiters and not state.tickets
    app._hook_source = "rpc"
    read = cmd(app, "readback", action_id=review["action_id"], review_token=review["review_token"],
        apply_operation_id=result["apply_operation_id"], readback_token=result["readback_token"],
        result_sha256=work.digest(work.encoded(result)))
    response = await published_rpc(app, monkeypatch, read, before_emit=lambda _: assert_waiting(waiter, state))
    assert response["verified"]
    profile, generation, context = await asyncio.wait_for(waiter, 2)
    assert (profile, generation, context["source"]) == ("interactive", 1, "rpc")
    assert not state.waiters and not state.tickets


def assert_waiting(waiter, state):
    assert waiter in state.waiters and not state.tickets and not waiter.done()


async def test_foreign_publication_task_and_changed_object_cannot_release(armed, monkeypatch):
    app, _ = armed
    _, review = await snapshot_review(app, monkeypatch, "plan_mode", True)
    apply = {"id": "apply", "type": controls.PREFIX + "apply", "review": review}
    result = await controls.apply(app, apply)
    async def foreign():
        return controls.prepare_publication(app, apply, result)
    with pytest.raises(ValueError, match="foreign"):
        await asyncio.create_task(foreign())
    with pytest.raises(ValueError, match="foreign"):
        controls.prepare_publication(app, apply, dict(result))
    ticket = controls.prepare_publication(app, apply, result)
    controls.finish_publication(app, ticket)
    with pytest.raises(ValueError):
        controls.finish_publication(app, ticket)
    assert controls.mutation_hold(app)


async def test_unreviewed_document_write_is_uncertain_even_if_selected_value_matches(armed, monkeypatch):
    from litetui.settings_service import SettingsService
    app, _ = armed
    _, review = await snapshot_review(app, monkeypatch, "tools_enabled", not app.tools_enabled)
    save = SettingsService.save_patch
    def write_extra(self, session_id, *args, **kwargs):
        result = save(self, session_id, *args, **kwargs)
        target = self._paths(session_id)["conversation"]
        document = work.strict_json(target.read_bytes())
        document["unexpected_extension"] = ["mutated"]
        target.write_bytes(work.encoded(document))
        return result
    monkeypatch.setattr(SettingsService, "save_patch", write_extra)
    result = await published_rpc(app, monkeypatch, {"id": "apply", "type": controls.PREFIX + "apply", "review": review})
    assert result["status"] == "uncertain" and result["close_required"]
    read = cmd(app, "readback", action_id=review["action_id"], review_token=review["review_token"],
        apply_operation_id=result["apply_operation_id"], readback_token=result["readback_token"],
        result_sha256=work.digest(work.encoded(result)))
    readback = await published_rpc(app, monkeypatch, read)
    assert not readback["verified"] and readback["release"] == "close"
    proof = (await controls.shutdown(app))["runtime_controls_shutdown"]
    assert proof["effect_attempted"] and proof["disposition"] == "uncertain" and proof["readback_published"]
