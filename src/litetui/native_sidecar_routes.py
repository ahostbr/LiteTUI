"""Translate desktop drafts into existing reviewed native owners.

The desktop never chooses a handler or supplies a native receipt. Each group
has its own canonical destination, revision, review, application and readback.
"""
from __future__ import annotations

import copy
import importlib

from litetui import native_settings, settings_runtime
from litetui.settings_scope import SETTING_SPECS

PREFERENCES = {
    "sidecar": "native_sidecar_preferences",
    "presentation": "native_presentation_preferences",
    "voice": "native_voice_preferences",
    "connection": "native_connection_preferences",
    "agent": "native_agent_preferences",
}
UNSUPPORTED = {
    "backend": "Use the terminal backend selector; changing execution authority also prepares its connection.",
    "default_model": "Use the terminal model selector; model selection changes owned execution authority.",
    "thinking_level": "Use the terminal Thinking control; base effort and per-model effort have distinct ownership.",
    "image_viewer_enabled": "The terminal has no external image-viewer preference control.",
    "mcp_enabled": "Use the terminal MCP lifecycle controls; transport ownership requires its own review.",
    "lmstudio_graded_thinking_models": "The terminal model metadata editor does not expose this legacy model-name list.",
    "codex_native_engine": "The native engine is selected at launch and cannot change inside this runtime.",
    "footer_task_manager": "The terminal has no machine-meter surface.",
}
MAX_GROUPS = 160


def module(route):
    name = PREFERENCES.get(route, {
        "defaults": "native_model_preferences",
        "budget": "native_settings",
        "compaction": "native_compaction_settings",
        "theme": "native_themes",
        "engine": "native_engine_config",
        "runtime": "native_runtime_controls",
        "job": "native_work_controls",
    }.get(route))
    if name is None:
        raise ValueError("Unknown source-owned Sidecar route")
    return importlib.import_module("litetui." + name)


def registry():
    from litetui import (
        native_compaction_settings,
        native_engine_config,
        native_model_settings,
    )

    result = {key: route for route in PREFERENCES for key in module(route).FIELDS}
    result.update(dict.fromkeys(native_settings.FIELDS, "budget"))
    result.update(dict.fromkeys(native_compaction_settings.FIELDS, "compaction"))
    result.update(dict.fromkeys(native_model_settings.DEFAULTS, "defaults"))
    result.update(dict.fromkeys(("theme_name", "custom_themes"), "theme"))
    result.update({key: "engine" for fields in native_engine_config.FIELDS.values() for key in fields})
    result.update(dict.fromkeys(("tools_enabled", "tool_policy_profile", "tools_disabled"), "runtime"))
    return {key: route for key, route in result.items() if key not in UNSUPPORTED}


def eligibility(app, public):
    """No discovery, network request or mutation while projecting editor eligibility."""
    from litetui import native_engine, native_runtime_controls, settings
    from litetui.sidecar_settings import SECRET_FIELDS

    routes = registry()
    for key, row in public["fields"].items():
        route = routes.get(key)
        reason = UNSUPPORTED.get(key)
        if key in SECRET_FIELDS:
            reason = "Secrets are set through the source credential editor; the native terminal never receives their values."
        elif route is None and reason is None:
            reason = "No reviewed native transaction currently owns this source setting."
        elif settings.source_of(key):
            reason = "An environment override owns this setting."
        elif key in (getattr(app, "_invocation_saved_values", {}) or {}):
            reason = "An explicit launch option owns this setting."
        elif row.get("control") and not row["control"]["editable"]:
            reason = row["control"]["help"]
        elif route == "engine" and (not native_engine.requested(app) or key not in module(route).FIELDS.get(app.backend.name, ())):
            reason = "This setting requires the matching explicitly managed native engine."
        elif route == "runtime" and not native_runtime_controls.enabled(app):
            reason = "Live runtime controls were not negotiated for this Work instance."
        row["native_route"] = route if reason is None else None
        row["native_reason"] = reason
        row["control"] = {"owner": "native terminal review", "editable": reason is None,
                          "help": reason or "Save proposes this change for explicit review in the terminal Sidecar panel."}
    public["native_review"] = True
    return public


def _same(a, b):
    return native_settings._same(a, b)


def groups(app, payload, public):
    from litetui import native_agent_preferences as agent
    from litetui.sidecar_settings import SECRET_FIELDS

    if (not isinstance(payload, dict) or set(payload) - {"changes", "expected_revisions", "conversation_id", "confirm_cache"}
            or "confirm_cache" in payload and type(payload["confirm_cache"]) is not bool
            or payload.get("conversation_id") != app.convo_id
            or not _same(payload.get("expected_revisions"), public["revisions"])):
        raise ValueError("Sidecar settings belong to another conversation or exact owned revisions; refresh.")
    changes = payload.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= 32:
        raise ValueError("Propose one through 32 source settings.")
    rows, seen = [], set()
    for change in changes:
        if not isinstance(change, dict) or set(change) != {"key", "value", "scope"}:
            raise ValueError("Each setting must name its exact key, value and scope.")
        key = change["key"]
        if not isinstance(key, str) or key in seen or key not in public["fields"] or key in SECRET_FIELDS:
            raise ValueError("Unknown, duplicated or secret setting proposal.")
        seen.add(key)
        field = public["fields"][key]
        if change["scope"] != field["scope"] or not field.get("native_route"):
            raise ValueError(field.get("native_reason") or "Setting scope changed.")
        row = {"route": field["native_route"], "field": key, "value": copy.deepcopy(change["value"]),
               "scope": field["scope"], "timing": field["apply_timing"]}
        if key == "custom_themes":
            value = row["value"]
            if not isinstance(value, dict) or set(value) != {"name", "tokens"}:
                raise ValueError("A palette proposal names one custom theme and its exact tokens.")
        elif _same(change["value"], field["saved"]):
            raise ValueError("A proposed setting already has that saved value; refresh the desktop draft.")
        rows.append(row)
    custom = next((row for row in rows if row["field"] == "custom_themes"), None)
    if custom:
        selected = next((row for row in rows if row["field"] == "theme_name"), None)
        if selected is None or selected["value"] != custom["value"]["name"]:
            raise ValueError("The native palette transaction saves and selects the named theme together.")
        rows.remove(selected)
        custom["fields"] = ["custom_themes", "theme_name"]
    expanded = []
    for row in rows:
        key = row["field"]
        if row["route"] == "agent" and agent.SPECS[key]["kind"] == "list":
            before = public["fields"][key]["saved"]
            after = agent._value(key, row["value"])
            # Canonical owner edits one entry. Exact list order is preserved by
            # removing the old suffix and then adding the proposed suffix.
            common = 0
            while common < min(len(before), len(after)) and before[common] == after[common]:
                common += 1
            current = list(before)
            for op, entries in (("remove", before[common:]), ("add", after[common:])):
                for entry in entries:
                    proposed = agent._candidate(key, op, entry, current)
                    expanded.append({**row, "op": op, "value": entry, "expected": proposed})
                    current = proposed
        elif row["route"] == "runtime" and key == "tools_disabled":
            from litetui.tool_list import disabled_after
            before = public["fields"][key]["saved"]
            after = row["value"]
            if not isinstance(after, list) or any(not isinstance(item, str) for item in after) or len(set(after)) != len(after):
                raise ValueError("Disabled tools must be an exact list of unique registered names.")
            current = before
            for enabled, entries in ((True, [k for k in before if k not in after]),
                                     (False, [k for k in after if k not in before])):
                for name in entries:
                    current = disabled_after(current, name, enabled)
                    expanded.append({**row, "value": {"name": name, "enabled": enabled}, "expected": current})
            if not _same(current, after):
                raise ValueError("Tool ordering differs from the canonical tool-selection writer.")
        else:
            expanded.append(row)
    if not expanded or len(expanded) > MAX_GROUPS:
        raise ValueError("The draft exceeds 160 independently reviewed native changes.")
    return expanded


def common(app, route):
    from litetui import background_input, native_engine
    binding = {**native_settings._identity(app), "version": 1}
    if route in PREFERENCES or route in ("defaults", "runtime"):
        bridge = background_input.bridge(app)
        if bridge is None or not bridge.active:
            raise ValueError("The current owned Work instance is unavailable.")
        bridge.check()
        binding["work_instance"] = bridge.instance
    if route == "runtime":
        binding["runtime_controls_version"] = 1
    if route == "engine":
        state = native_engine.state(app)
        return {key: value for key, value in {
            **binding, "desired_model": state.desired_model, "management_instance": state.instance
        }.items() if key != "model"}
    return binding


def prefix(route):
    return {
        "budget": "gui.settings.native_",
        "compaction": "gui.settings.native_compact_",
        "theme": "gui.themes.native_",
        "job": "gui.work.native_control_",
    }.get(route, getattr(module(route), "PREFIX", ""))


def snapshot_command(app, group):
    route = group["route"]
    name = "preferences" if route in PREFERENCES else "tool_list" if route == "runtime" and group["field"] == "tools_disabled" else "snapshot"
    result = {**common(app, route), "type": prefix(route) + name}
    if route == "defaults":
        result["section"] = "defaults"
    if route == "job":
        result["type"] = "gui.work.native_snapshot"
    return result


def review_command(app, group, snap):
    route, key, value = group["route"], group["field"], group["value"]
    command = {**common(app, route), "type": prefix(route) + "review"}
    if route in PREFERENCES or route == "defaults":
        command.update(revision=snap["revision"], field=key, value=value)
        if route == "agent":
            command["op"] = group.get("op", "set")
        elif route == "defaults":
            command.update(section="defaults", mode="server" if value is None else "value")
    elif route in ("budget", "compaction"):
        command.update(type=prefix(route) + "validate", snapshot=snap["snapshot"], revisions=snap["revisions"], field=key, value=value)
    elif route == "theme":
        command.update(type=prefix(route) + "validate", snapshot=snap["snapshot"], revisions=snap["revisions"])
        if key == "custom_themes":
            command.update(action="custom", **value)
        else:
            row = next((r for r in snap["rows"] if r["name"] == value and r["selectable"]), None)
            if row is None:
                raise ValueError("The proposed theme does not identify a selectable native definition.")
            command.update(action="select", row_id=row["row_id"])
    elif route == "engine":
        command.update(snapshot=snap["snapshot"], field=key, value=value)
    elif route == "runtime":
        if key == "tools_disabled":
            snap = snap["snapshot"]
        command.update(type=prefix(route) + "validate", snapshot=snap["snapshot"], generation=snap["generation"],
                       change={"field": {"tool_policy_profile": "tool_profile", "tools_disabled": "tool_selection"}.get(key, key), "value": value})
    elif route == "job":
        command.update(type=prefix(route) + "validate", instance=snap["instance"], snapshot=snap["snapshot"],
                       action="job_create", target_id=None, target_revision=None, draft=value)
    return command


def apply_command(app, group, review, draft):
    route = group["route"]
    command = {**common(app, route), "type": prefix(route) + "apply"}
    if route in PREFERENCES or route == "defaults":
        command["token"] = review["token"]
        if route == "defaults":
            command["section"] = "defaults"
    elif route == "budget":
        command = {**draft, "type": prefix(route) + "save", "review": review["review"]}
        command.pop("id", None)
    elif route in ("compaction", "theme", "job"):
        command = {**{k: copy.deepcopy(v) for k, v in review.items() if k != "kind"},
                   "type": prefix(route) + ("apply" if route == "job" else "save")}
    elif route == "engine":
        command.update(type=prefix(route) + "save", review=review["review"])
    elif route == "runtime":
        command = {"type": prefix(route) + "apply", "review": review}
    return command


def readback_command(app, group, review, applied):
    route = group["route"]
    command = {**common(app, route), "type": prefix(route) + "readback"}
    if route in PREFERENCES or route == "defaults":
        command.update(token=review["token"], apply_id=applied["apply_id"])
        if route == "defaults":
            command["section"] = "defaults"
    elif route in ("budget", "compaction", "theme"):
        command["type"] = prefix(route) + "snapshot"
    elif route == "engine":
        command.update(review=review["review"], save_id=applied["save_id"])
    elif route == "runtime":
        from litetui import background_input as work
        command.update(action_id=review["action_id"], review_token=review["review_token"],
                       apply_operation_id=applied["apply_operation_id"], readback_token=applied["readback_token"],
                       result_sha256=work.digest(work.encoded(applied)))
    elif route == "job":
        command.update(instance=applied["instance"], snapshot=applied["snapshot"],
                       **{k: applied[k] for k in ("review", "action_id", "receipt")})
    return command


def expected_values(app, group):
    key, value = group["field"], group.get("expected", group["value"])
    if group["route"] == "job":
        return {}
    if key == "stop" and value is None:
        value = []
    if key == "custom_themes":
        from litetui import theme_palette
        _theme, tokens = theme_palette.custom(value["name"], value["tokens"])
        saved = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True)
        result = copy.deepcopy(saved.saved.custom_themes)
        result[value["name"]] = tokens
        return {"custom_themes": result, "theme_name": value["name"]}
    return {key: copy.deepcopy(value)}


def verify_settings(app, expected, before, group):
    """Independent canonical disk and effective readback, never the child receipt."""
    service = settings_runtime.service_for(app)
    paths, documents, revisions = native_settings.owned_documents(service, app.convo_id, app._agent_session)
    saved = service.snapshot(app.convo_id, read_only=True, max_bytes=1024 * 1024)
    if revisions != saved.revisions:
        raise ValueError("Sidecar readback raced canonical settings.")
    facts = {}
    for key, value in expected.items():
        spec = SETTING_SPECS[key]
        slot = "conversation" if spec.scope.value == "conversation" else "global"
        raw = documents[slot]
        if slot == "conversation":
            raw = raw.get("execution", {})
        if key not in raw or not _same(raw[key], value) or not _same(getattr(saved.saved, key), value):
            raise ValueError("Sidecar field differs from its exact persisted destination.")
        effective = getattr(app.settings, key)
        delayed = spec.apply_timing in ("reload", "restart", "reconnect")
        target = before[key] if delayed else value
        if not _same(effective, target):
            raise ValueError("Sidecar effective field differs from the canonical apply timing.")
        facts[key] = {"saved": value, "effective": copy.deepcopy(effective), "destination": str(paths[slot]),
                      "scope": spec.scope.value, "timing": spec.apply_timing, "pending": delayed}
    return {"fields": facts, "revisions": revisions}
