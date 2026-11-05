import json
from contextlib import ExitStack
from unittest.mock import patch

import pytest
from test_rust_backend_catalog import catalog_app as _catalog_app
from test_rust_backend_catalog import forbidden, tree
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import background_input as work
from litetui import (
    command_surface,
    gui_rpc,
    native_agent_preferences,
    native_runtime_controls,
    plugins,
    settings,
)
from litetui.settings_scope import SETTING_SPECS

app, switch_app, armed = owned_app, owned_switch_app, owned_context
command_app = _catalog_app
pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _no_ambient_environment_overrides(monkeypatch):
    for name in settings.ENV_OVERRIDES.values():
        monkeypatch.delenv(name, raising=False)


def call(owner, operation, **kw):
    return gui_rpc.dispatch(
        owner,
        {
            "type": native_agent_preferences.PREFIX + operation,
            "id": "agent-" + operation,
            "version": 1,
            **work.identity(owner),
            "work_instance": work.bridge(owner).instance,
            **kw,
        },
    )


def forbid_side_effects(stack):
    """Saving a preference never refreshes the skills index, loads a model or launches a seat."""
    from litetui import skills, small_task_dispatch, subagent_dispatch

    def forbidden(*_args, **_kwargs):
        pytest.fail("An agent preference save refreshed an index, loaded a model or launched a seat")

    for source, names in (
        (skills, ("discover_all", "write_cache")),
        (small_task_dispatch, ("dispatch",)),
        (subagent_dispatch, ("dispatch",)),
    ):
        for name in names:
            if hasattr(source, name):
                stack.enter_context(patch.object(source, name, forbidden))


def paths(owner):
    service = native_agent_preferences.settings_runtime.service_for(owner)
    return service._paths(owner.convo_id)


def conversation_execution(owner):
    return json.loads(paths(owner)["conversation"].read_bytes())["execution"]


def save(owner, field, value, op="set"):
    current = call(owner, "preferences")
    review = call(owner, "review", revision=current["revision"], field=field, op=op, value=value)
    applied = call(owner, "apply", token=review["token"])
    observed = call(owner, "readback", token=review["token"], apply_id=applied["apply_id"])
    return current, review, applied, observed


async def test_table_matches_the_canonical_registry():
    assert set(native_agent_preferences.FIELDS) == set(native_agent_preferences.SPECS)
    for key, spec in native_agent_preferences.SPECS.items():
        registry = SETTING_SPECS[key]
        assert (registry.scope.value, registry.apply_timing) == (spec["scope"], spec["timing"]), key
    assert len(native_agent_preferences.FIELDS) == 21


@pytest.mark.parametrize(
    "field,value,where",
    [
        ("tool_iterations", 17, "conversation"),
        ("tool_auto_background_s", 0, "conversation"),
        ("relay_approval_timeout_s", 90, "conversation"),
        ("tool_context_mode", "llm-tool-mask", "conversation"),
        ("tool_context_threshold_chars", 0, "conversation"),
        ("enter_interrupts", True, "global"),
        ("permission_judge", "count", "global"),
        ("allow_local_subagents", True, "global"),
        ("subagent_route", {"backend": "codex", "model": "gpt-5.1"}, "global"),
        ("subagent_route_override", {}, "conversation"),
        ("subagent_model", "small-model", "conversation"),
        ("tool_summary_model", "small-model", "conversation"),
        ("user_name", "Ryan", "global"),
        ("owner_domain_cap", 12000, "conversation"),
    ],
)
async def test_each_reviewed_field_saves_only_its_registry_destination(armed, field, value, where):
    owner = armed
    files = paths(owner)
    global_before = files["global"].read_bytes() if files["global"].exists() else b""
    with ExitStack() as guard:
        forbid_side_effects(guard)
        current = call(owner, "preferences")
        assert current["fields"][field]["editable"], current["fields"][field]
        review = call(owner, "review", revision=current["revision"], field=field, op="set", value=value)
        assert review["proposed"] == value
        assert review["effect"] == ("widens" if field == "allow_local_subagents" else "none")
        assert review["setting_scope"] == SETTING_SPECS[field].scope.value
        # Review is inert: no setting document or running value moved.
        assert (files["global"].read_bytes() if files["global"].exists() else b"") == global_before
        assert getattr(owner.settings, field) != value
        applied = call(owner, "apply", token=review["token"])
        with pytest.raises(ValueError):
            call(owner, "apply", token=review["token"])
        observed = call(owner, "readback", token=review["token"], apply_id=applied["apply_id"])
    row = observed["preferences"]["fields"][field]
    assert row["saved"] == row["effective"] == value == getattr(owner.settings, field)
    assert observed["after"] != current["revision"]
    document = json.loads(files["global"].read_bytes()) if where == "global" else conversation_execution(owner)
    assert document[field] == value
    other = json.loads(files["global"].read_bytes()) if where == "conversation" else conversation_execution(owner)
    assert other.get(field) != value or field not in SETTING_SPECS or where == "global" and field in other


async def test_clearing_an_optional_model_and_disabling_small_task_route_are_explicit(armed):
    owner = armed
    save(owner, "subagent_model", "tiny")
    cleared = save(owner, "subagent_model", None)[3]["preferences"]["fields"]["subagent_model"]
    assert cleared["saved"] is None and cleared["effective"] is None
    route = {"backend": "lmstudio", "model": "tiny-2b", "cognitive": "worker", "thinking_level": "low"}
    review = save(owner, "small_task_route", route)[1]
    assert review["effect"] == "widens" and "WIDENS" in review["consequence"]
    off = save(owner, "small_task_route", None)[1]
    assert off["effect"] == "narrows"


@pytest.mark.parametrize(
    "field,op,value,effect",
    [
        ("tool_always_allow", "add", "bash:process_execution", "widens"),
        ("tool_deny", "add", "bash:destructive_irreversible", "narrows"),
    ],
)
async def test_authority_lists_change_one_entry_through_a_review_that_states_the_effect(
        armed, field, op, value, effect, monkeypatch):
    owner = armed
    monkeypatch.setattr(native_runtime_controls, "enabled", lambda _app: True)
    current = call(owner, "preferences")
    review = call(owner, "review", revision=current["revision"], field=field, op=op, value=value)
    assert review["effect"] == effect
    assert effect.upper() in review["consequence"] and value in review["consequence"]
    assert review["proposed"] == [value]
    assert getattr(owner.settings, field) == []
    applied = call(owner, "apply", token=review["token"])
    # Admission belongs to the reviewed operation until its exact readback.
    assert native_agent_preferences.hold(owner)
    assert "tool-authority" in native_runtime_controls.mutation_hold(owner)
    for blocked in ("preferences",):
        with pytest.raises(ValueError, match="readback"):
            call(owner, blocked)
    allowed = {"type": native_agent_preferences.PREFIX + "readback", "token": review["token"], "apply_id": applied["apply_id"]}
    assert native_agent_preferences.readback_allowed(owner, {**allowed, **work.identity(owner), "id": "x", "version": 1,
                                                             "work_instance": work.bridge(owner).instance})
    assert not native_agent_preferences.readback_allowed(owner, {**allowed, "token": "0" * 64})
    # Runtime admission refuses every other operation, and admits only this exact readback.
    with pytest.raises(ValueError, match="tool-authority"):
        native_runtime_controls.operation_allowed(owner, {"type": "gui.models.select", "slug": "x"})
    native_runtime_controls.operation_allowed(owner, {**allowed, **work.identity(owner), "id": "x", "version": 1,
                                                       "work_instance": work.bridge(owner).instance})
    observed = call(owner, "readback", token=review["token"], apply_id=applied["apply_id"])
    assert not native_agent_preferences.hold(owner) and native_runtime_controls.mutation_hold(owner) is None
    assert observed["preferences"]["fields"][field]["saved"] == [value]
    assert conversation_execution(owner)[field] == [value]
    # The reverse operation states the reverse effect and removes exactly that entry.
    back = save(owner, field, value, op="remove")[1]
    assert back["effect"] == ("narrows" if effect == "widens" else "widens")
    assert getattr(owner.settings, field) == []


async def test_trusted_interpreter_requires_an_existing_unlinked_absolute_file(armed, tmp_path):
    owner = armed
    current = call(owner, "preferences")
    target = tmp_path / "python.exe"
    target.write_bytes(b"MZ")
    for bad in ("python.exe", str(tmp_path / "missing.exe"), str(tmp_path), "\\\\server\\share\\python.exe",
                str(tmp_path / ".." / "python.exe"), " " + str(target)):
        with pytest.raises(ValueError):
            call(owner, "review", revision=current["revision"], field="tool_trusted_interpreters", op="add", value=bad)
    review = save(owner, "tool_trusted_interpreters", str(target), op="add")[1]
    assert review["effect"] == "widens" and "exact file only" in review["consequence"]
    assert owner.settings.tool_trusted_interpreters == [str(target)]


@pytest.mark.parametrize(
    "field,op,value",
    [
        ("tool_iterations", "set", 0),
        ("tool_iterations", "set", True),
        ("tool_iterations", "set", 2147483648),
        ("tool_auto_background_s", "set", -1),
        ("relay_approval_timeout_s", "set", 0),
        ("owner_domain_cap", "set", 0),
        ("tool_context_mode", "set", "banana"),
        ("permission_judge", "set", "judge"),
        ("enter_interrupts", "set", 1),
        ("subagent_route", "set", {}),
        ("subagent_route", "set", {"backend": "nonesuch", "model": "m"}),
        ("subagent_route", "set", {"backend": "codex"}),
        ("subagent_route_override", "set", {"backend": "codex", "model": " padded "}),
        ("small_task_route", "set", {"backend": "codex", "model": "auto", "cognitive": "w", "thinking_level": "low"}),
        ("small_task_route", "set", {"backend": "codex", "model": "gpt", "cognitive": "w.md", "thinking_level": "low"}),
        ("subagent_model", "set", ""),
        ("seat_name", "set", "bad/name"),
        ("user_name", "set", " padded"),
        ("user_name", "set", "x" * 129),
        ("tool_always_allow", "set", ["bash:network"]),
        ("tool_always_allow", "add", "bash"),
        ("tool_always_allow", "add", "bash:unknown_capability"),
        ("tool_always_allow", "add", "bash:process_execution,network"),
        ("tool_always_allow", "add", "bash:destructive_irreversible"),
        ("tool_always_allow", "remove", "never:there"),
        ("skill_roots", "add", "relative/skills"),
        ("skill_roots", "add", "~/../skills"),
        ("nonesuch", "set", 1),
    ],
)
async def test_values_outside_the_source_bounds_are_refused_without_a_review(armed, field, op, value):
    current = call(armed, "preferences")
    with pytest.raises(ValueError):
        call(armed, "review", revision=current["revision"], field=field, op=op, value=value)
    assert call(armed, "preferences")["revision"] == current["revision"]


async def test_duplicate_and_no_op_changes_are_refused(armed):
    save(armed, "tool_deny", "powershell:network", op="add")
    current = call(armed, "preferences")
    for field, op, value in (
        ("tool_deny", "add", "powershell:network"),
        ("tool_iterations", "set", armed.settings.tool_iterations),
    ):
        with pytest.raises(ValueError):
            call(armed, "review", revision=current["revision"], field=field, op=op, value=value)


async def test_skills_save_is_pending_reconnect_and_never_refreshes_the_index(armed):
    owner = armed
    before = owner.settings.skills_enabled
    with ExitStack() as guard:
        forbid_side_effects(guard)
        _current, review, _applied, observed = save(owner, "skills_enabled", not before)
    assert review["apply_timing"] == "reconnect" and "Saved only" in review["consequence"]
    row = observed["preferences"]["fields"]["skills_enabled"]
    assert row["saved"] is (not before) and row["effective"] is before
    assert not row["editable"] and "reconnect" in row["reason"]
    assert owner.settings.skills_enabled is before
    assert conversation_execution(owner)["skills_enabled"] is (not before)


async def test_environment_launch_and_backend_ownership_refuse_edits(armed, monkeypatch):
    monkeypatch.setenv("LITETUI_SEAT_NAME", "Env Seat")
    current = call(armed, "preferences")
    row = current["fields"]["seat_name"]
    assert row["overridden"] and not row["editable"] and "LITETUI_SEAT_NAME" in row["reason"]
    with pytest.raises(ValueError):
        call(armed, "review", revision=current["revision"], field="seat_name", op="set", value="Another")
    armed._launch_overrides = {"tool_iterations": 5}
    assert call(armed, "preferences")["fields"]["tool_iterations"]["editable"] is False
    armed._launch_overrides = {}
    monkeypatch.setattr(armed.backend, "owns_native_turns", True, raising=False)
    reason = native_agent_preferences._reason(armed, "tool_iterations", 48, 48, False)
    assert reason and "Claude owns" in reason
    assert native_agent_preferences._reason(armed, "tool_deny", [], [], False) is None  # host authority


async def test_stale_unrelated_and_failed_persistence_never_look_applied(armed):
    owner = armed
    current = call(owner, "preferences")
    review = call(owner, "review", revision=current["revision"], field="tool_iterations", op="set", value=11)
    path = paths(owner)["global"]
    data = json.loads(path.read_bytes()) if path.exists() else {}
    data["retained_other_tool"] = {"value": 42}
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        call(owner, "apply", token=review["token"])
    assert owner.settings.tool_iterations != 11
    current = call(owner, "preferences")
    review = call(owner, "review", revision=current["revision"], field="tool_deny", op="add", value="bash:network")
    service = native_agent_preferences.settings_runtime.service_for(owner)
    actual = service.save_patch
    from litetui.settings_apply import PersistenceDestinationResult, SettingsSaveResult

    def refused(conversation_id, changes, revisions, **kwargs):
        destination = paths(owner)["conversation"]
        return SettingsSaveResult(persistence=(PersistenceDestinationResult(
            str(destination), "conversation", False, error="disk full", fields=("tool_deny",)),))

    with patch.object(service, "save_patch", refused), pytest.raises(ValueError, match="refused"):
        call(owner, "apply", token=review["token"])
    # A refusal before any change releases admission; nothing is claimed saved.
    assert owner.settings.tool_deny == [] and not native_agent_preferences.hold(owner)
    assert call(owner, "preferences")["fields"]["tool_deny"]["saved"] == []
    review = call(owner, "review", revision=call(owner, "preferences")["revision"],
                  field="tool_deny", op="add", value="bash:network")

    def wrote_then_reported_failure(*args, **kwargs):
        actual(*args, **kwargs)
        return refused(*args, **kwargs)

    # A write that changed disk while reporting failure is uncertain: admission stays held and close is required.
    with patch.object(service, "save_patch", wrote_then_reported_failure), pytest.raises(ValueError, match="uncertain"):
        call(owner, "apply", token=review["token"])
    assert native_agent_preferences.hold(owner) and owner._backend_transition_error
    assert owner.settings.tool_deny == []


async def test_readback_rejects_foreign_apply_and_post_save_drift(armed):
    owner = armed
    current = call(owner, "preferences")
    review = call(owner, "review", revision=current["revision"], field="tool_deny", op="add", value="bash:network")
    applied = call(owner, "apply", token=review["token"])
    with pytest.raises(ValueError, match="another application"):
        call(owner, "readback", token=review["token"], apply_id="replay")
    # The saved change still owns admission; only its exact readback releases it.
    assert native_agent_preferences.hold(owner)
    with pytest.raises(ValueError, match="again"):
        call(owner, "readback", token="0" * 64, apply_id=applied["apply_id"])
    path = paths(owner)["global"]
    saved = path.read_bytes()
    path.write_text(json.dumps({**json.loads(saved), "stt_mic": "another configured input"}), encoding="utf8")
    with pytest.raises(ValueError, match="changed after saving"):
        call(owner, "readback", token=review["token"], apply_id=applied["apply_id"])
    # Drift after an authority save is uncertain: the runtime must close, never admit work.
    assert native_agent_preferences.hold(owner) and owner._backend_transition_error


async def test_a_newer_review_supersedes_the_older_token(armed):
    current = call(armed, "preferences")
    review = call(armed, "review", revision=current["revision"], field="tool_iterations", op="set", value=9)
    # A newer review supersedes the older token entirely.
    call(armed, "review", revision=current["revision"], field="tool_iterations", op="set", value=10)
    with pytest.raises(ValueError, match="again"):
        call(armed, "apply", token=review["token"])


async def test_registered_subagent_commands_route_to_the_editor_and_never_apply(command_app):
    app = command_app
    registry = plugins.PluginRegistry()
    registry.add_command("subagent", ("/subagent-set",), forbidden, palette="Conversation subagent override", group="model")
    registry.add_command("subagent", ("/subagent-global",), forbidden, palette="Global subagent route", group="model")
    app.plugins = registry
    before = tree(app)
    catalog = command_surface.catalog(app, {"session_id": app.convo_id})
    request = {"session_id": app.convo_id, "version": 1, "agent_id": catalog["agent_id"], "snapshot": catalog["snapshot"]}
    for command, action in (("/subagent-set", "agent_prefs_override"), ("/subagent-global", "agent_prefs_global")):
        route = command_surface.resolve(app, {**request, "command": command})
        assert (route["native_action"], route["arguments"]) == (action, "")
        # Arguments would be an application request; the adapter takes none and the draft stays the user's.
        with pytest.raises(ValueError, match="does not accept command arguments"):
            command_surface.resolve(app, {**request, "command": command + " codex gpt-5.1"})
    assert tree(app) == before


@pytest.mark.parametrize("field", native_agent_preferences.AUTHORITY)
async def test_legacy_settings_paths_cannot_bypass_the_reviewed_authority_control(armed, monkeypatch, field):
    monkeypatch.setattr(native_runtime_controls, "enabled", lambda _app: True)
    for operation in ("gui.settings.apply", "set_settings", "gui.settings.native_save"):
        with pytest.raises(ValueError, match="reviewed runtime control"):
            native_runtime_controls.operation_allowed(armed, {"type": operation, "patch": {field: ["x:network"]}})
    # An unrelated preference is not caught by the same guard.
    native_runtime_controls.operation_allowed(armed, {"type": "gui.settings.apply", "patch": {"tool_iterations": 9}})
