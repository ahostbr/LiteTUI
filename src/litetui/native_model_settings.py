"""Defaults and preset plans for the existing model-preference transaction."""

from __future__ import annotations

import copy

from litetui import backend_catalog, llm_backend, native_settings
from litetui import native_model_preference_catalog as vocabulary
from litetui import settings as settings_mod
from litetui.plugins import model_switch
from litetui.settings_scope import SETTING_SPECS

DEFAULTS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
    "seed",
    "stop",
)
MAPS = ("model_infer_overrides", "llama_load_settings", "llama_presets")
REQUEST_FIELDS = vocabulary.INFERENCE + ("seed",)


def section(cmd):
    value = cmd.get("section", "model")
    if value not in ("model", "defaults", "presets"):
        raise ValueError("Choose a supported model settings section")
    return value


def name(value):
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value.encode("utf8")) > 256
    ):
        raise ValueError(
            "Preset names must be nonempty, trimmed, and at most 256 UTF-8 bytes"
        )
    return value


def _same(a, b):
    return native_settings._same(a, b)


def request(app, candidate):
    from litetui import native_model_preferences as models

    values = llm_backend._merged_overrides(candidate, app.model_id)
    row = candidate.model_infer_overrides.get(app.model_id, {})
    if "reasoning_effort" in row and row["reasoning_effort"] is None:
        values["reasoning_effort"] = "default"
    actual = app._effective_request_overrides()
    if getattr(getattr(app, "_launch_options", None), "max_tokens", None) is not None:
        values["max_tokens"] = actual["max_tokens"]
    if getattr(app, "_cli_effective_thinking", None) is not None:
        values.pop("reasoning_effort", None)
        if "reasoning_effort" in actual:
            values["reasoning_effort"] = actual["reasoning_effort"]
    return models._request_values(app, values, REQUEST_FIELDS)


def extend_snapshot(app, result, context, service, before, paths, selected):
    """Keep the existing model rows, ownership fingerprint, and request consumer."""
    from litetui import native_model_preferences as models

    result["section"] = selected
    result["request"] = (
        models._request_values(app, app._effective_request_overrides(), REQUEST_FIELDS)
        if result["supported"]
        else {key: None for key in REQUEST_FIELDS}
    )
    result["destinations"] = {
        "conversation": str(paths["conversation"]),
        "defaults": str(paths["global"]),
    }
    keys = DEFAULTS if selected == "defaults" else MAPS
    result["settings"] = {
        state: {key: copy.deepcopy(getattr(values, key)) for key in keys}
        for state, values in (("saved", before.saved), ("active", app.settings))
    }
    if selected == "presets":
        result["presets"] = copy.deepcopy(before.saved.llama_presets)
        models._map(result["presets"])
        for preset, value in result["presets"].items():
            name(preset)
            _preset(value)
        spec = SETTING_SPECS["llama_presets"]
        if spec.scope.value != "defaults" or spec.apply_timing != "next_turn":
            raise ValueError("Preset persistence scope changed")
        return
    current_request = (
        models._request_values(app, app._effective_request_overrides(), REQUEST_FIELDS)
        if result["supported"]
        else {}
    )
    overrides = copy.deepcopy(app.settings.model_infer_overrides.get(app.model_id, {}))
    result["model_overrides"] = overrides
    fields, catalog = {}, {}
    for key in DEFAULTS:
        spec = SETTING_SPECS[key]
        if spec.scope.value != "conversation" or spec.apply_timing != "next_turn":
            raise ValueError("Sampling default persistence scope changed")
        saved, active = getattr(before.saved, key), getattr(app.settings, key)
        vocabulary.checked(key, saved)
        vocabulary.checked(key, active)
        meta = copy.deepcopy(result["catalog"][key])
        env = settings_mod.source_of(key)
        reason = (
            f"Environment {env} owns this default"
            if env
            else "This provider owns its inference configuration."
            if not result["supported"]
            else "Refresh/reopen to reconcile saved and active defaults."
            if not _same(saved, active)
            else None
        )
        meta.update(
            group="inference",
            editable=reason is None,
            reason=reason,
            help="Conversation default; a selected-model override can mask the next request."
            + (
                " Enter a JSON array of exact strings; [] disables defaults."
                if key == "stop"
                else " Clear leaves the server default."
            ),
        )
        catalog[key] = meta
        fields[key] = {
            "present": True,
            "saved": copy.deepcopy(saved),
            "active_present": True,
            "active": copy.deepcopy(active),
            "next_request": current_request.get(key),
            "inherited": None,
            "editable": reason is None,
            "overridden": key in overrides,
            "server_default": None,
        }
    result["fields"], result["catalog"] = fields, catalog
    result["raw"] = {
        "inference": copy.deepcopy(result["settings"]),
        "load": {"saved": {}, "active": {}},
    }
    result["deferred_load_fields"] = 0


def _preset(value):
    if not isinstance(value, dict) or set(value) - {"load", "inference"}:
        raise ValueError("Preset must contain inference and/or load dictionaries")
    for group in ("load", "inference"):
        row = value.get(group, {})
        if not isinstance(row, dict) or any(not isinstance(k, str) for k in row):
            raise ValueError("Preset fields must be an object")
    backend_catalog._encoded(value, 128 * 1024)
    return value


def plan(app, snap, before, field, mode, value):
    """Pure exact plan; the frozen encoded result is re-derived before publication."""
    from litetui.gui_rpc import _validate

    selected = snap["section"]
    if not snap["supported"]:
        raise ValueError("This provider owns its inference configuration")
    candidate = copy.deepcopy(app.settings)
    affected = []
    if selected == "defaults":
        if field not in DEFAULTS or not snap["fields"][field]["editable"]:
            raise ValueError("Choose an editable conversation default")
        if mode not in ("value", "server") or (mode == "server") != (value is None):
            raise ValueError("Choose a value or the server default")
        actual = [] if field == "stop" and mode == "server" else value
        vocabulary.checked(field, actual)
        _validate(app, {field: actual})
        if _same(snap["fields"][field]["saved"], actual):
            raise ValueError("Choose a different conversation default")
        changes = {field: actual}
        scope = "conversation"
        affected.append(
            {
                "group": "default",
                "field": field,
                "before": snap["fields"][field]["saved"],
                "after": actual,
            }
        )
        setattr(candidate, field, copy.deepcopy(actual))
    else:
        name(field)
        if value is not None or mode not in ("save", "apply"):
            raise ValueError("Choose preset Save or Apply")
        if not _same(
            before.saved.model_infer_overrides, app.settings.model_infer_overrides
        ):
            raise ValueError("Saved inference preferences differ from the active model")
        if mode == "save":
            if not _same(before.saved.llama_presets, app.settings.llama_presets):
                raise ValueError(
                    "Saved presets differ from active settings; reopen before saving"
                )
            preset = {
                group: copy.deepcopy(snap["raw"][group]["saved"])
                for group in ("inference", "load")
            }
            _preset(preset)
            presets = copy.deepcopy(before.saved.llama_presets)
            if field in presets and _same(presets[field], preset):
                raise ValueError("Preset already contains these saved settings")
            affected.append(
                {
                    "group": "preset",
                    "field": field,
                    "before": presets.get(field),
                    "after": preset,
                }
            )
            presets[field] = preset
            changes, scope = {"llama_presets": presets}, "defaults"
        else:
            if field not in snap["presets"]:
                raise ValueError("Choose an existing preset")
            preset = _preset(snap["presets"][field])
            changes, scope = {}, "conversation"
            for group, setting in (
                ("inference", "model_infer_overrides"),
                ("load", "llama_load_settings"),
            ):
                rows = copy.deepcopy(getattr(before.saved, setting))
                old = rows.get(app.model_id, {})
                row = {**old, **copy.deepcopy(preset.get(group, {}))}
                for key, new in row.items():
                    if key in old and _same(old[key], new):
                        continue
                    if (
                        key in vocabulary.FIELDS
                        and snap["catalog"][key]["group"] == group
                    ):
                        meta = snap["catalog"][key]
                        if not snap["fields"][key]["editable"]:
                            raise ValueError(
                                f"{key}: selected model or invocation owns this field"
                            )
                        vocabulary.checked(key, new, meta)
                    elif group == "inference" and key == "seed":
                        vocabulary.checked(key, new)
                    affected.append(
                        {
                            "group": group,
                            "field": key,
                            "before": old.get(key),
                            "after": new,
                        }
                    )
                if group == "load":
                    model_switch.validate_load_cfg(row)
                    if app.backend.name == "llamacpp":
                        selected_model = app.model_rows.get(app.model_id)
                        llm_backend.resolve_speculation(
                            app.model_id,
                            getattr(selected_model, "path", None),
                            row,
                            app.settings,
                        )
                if row:
                    rows[app.model_id] = row
                changes[setting] = rows
                setattr(candidate, setting, copy.deepcopy(rows))
            if not affected:
                raise ValueError("Preset already matches this model's saved settings")
    transaction = {
        "scope": scope,
        "destination": snap["destinations"][scope],
        "changes": changes,
        "effective_before": {
            key: copy.deepcopy(getattr(app.settings, key)) for key in changes
        },
        "affected": sorted(affected, key=lambda item: (item["group"], item["field"])),
        "next_request": request(app, candidate),
        "pending_load": not _same(
            changes.get("llama_load_settings", app.settings.llama_load_settings),
            app.settings.llama_load_settings,
        ),
    }
    backend_catalog._encoded(transaction, 1024 * 1024)
    return transaction


def apply(app, cmd, review, binding, info, bridge, state):
    import json

    from litetui import native_model_preferences as models

    expected = json.loads(review.plan)

    def reader(owner, _cmd):
        bridge.check()
        models._same(owner, review)
        models._quiet(owner)
        current, context, service, before, _ = models._snapshot(
            owner, binding, info, review.section
        )
        if current["revision"] != review.revision:
            raise ValueError("Model settings changed before publication")
        actual = plan(owner, current, before, review.field, review.mode, review.value)
        if not _same(actual, expected):
            raise ValueError("Model settings plan changed before publication")
        return actual, context, service, before

    def outcome(_owner, _cmd, status, **detail):
        runtime = detail.get("runtime", ())
        if (
            status in ("saved", "partial")
            and runtime
            and all(
                row.status == "applied"
                or row.field == "llama_load_settings"
                and row.status == "pending"
                and row.action == "reload"
                for row in runtime
            )
        ):
            return None
        if detail.get("mutated") or detail.get("close_required"):
            return "Model settings publication is uncertain. Close and reopen to inspect it."
        return "Model settings were refused. Refresh to inspect them."

    app._native_model_preferences_hold = True
    error = native_settings.publish_reviewed_many(
        app, cmd, reader, outcome, lambda _key, value: value, strict=True
    )
    if error:
        if not getattr(app, "_backend_transition_error", None):
            models._release(app)
        raise ValueError(error)
    current = models._snapshot(app, binding, info, review.section)[0]
    if current["revision"] == review.revision:
        raise ValueError("Model settings did not publish a new revision")
    for key, value in expected["changes"].items():
        if not _same(current["settings"]["saved"][key], value):
            raise ValueError("Model settings failed independent durable readback")
        active = (
            expected["effective_before"][key] if key == "llama_load_settings" else value
        )
        if not _same(current["settings"]["active"][key], active):
            raise ValueError("Model settings failed independent active readback")
    actual = models._request_values(
        app, app._effective_request_overrides(), REQUEST_FIELDS
    )
    if not _same(actual, expected["next_request"]):
        raise ValueError("Model settings failed independent request readback")
    return current
