"""Reviewed current-model inference preferences in the owned conversation."""

from __future__ import annotations

import copy
import hashlib
import secrets
from dataclasses import dataclass

from litetui import (
    backend_catalog,
    backend_transition,
    gui_rpc,
    llm_backend,
    native_context,
    native_settings,
    settings_runtime,
)
from litetui.plugins import model_switch
from litetui.settings_scope import SETTING_SPECS
from litetui.turn_engine import TurnEngine

PREFIX = "gui.models.native_preferences_"
FIELDS = ("temperature", "top_p", "max_tokens")
BACKENDS = frozenset(("lmstudio", "llamacpp", "custom", "ninfer", "strata"))


@dataclass
class Review:
    token: str
    instance: str
    revision: str
    field: str
    value: object
    mode: str
    applied: str | None = None
    after: str | None = None


def _encoded(value):
    return backend_catalog._encoded(value, 65536)


def _checked(field, value):
    if field not in FIELDS:
        raise ValueError("Choose a supported inference preference")
    gui_rpc._validate_model_fields({field: value}, model_switch._INFER_FIELDS)
    if value is None:
        return None
    if field == "max_tokens" and (
        type(value) is not int or not 1 <= value <= 2147483647
    ):
        raise ValueError("Response limit must be a positive integer")
    return value


def _map(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Model inference preferences must be an object")
    _encoded(value)
    return value


def _choice(field, mode, value):
    if mode == "inherit" and value is None:
        return
    if mode == "value" and value is not None:
        _checked(field, value)
        return
    raise ValueError("Choose an explicit value or inherit this conversation's default")


def _request_values(app, values):
    # Use the exact normal request constructor, without messages or provider I/O.
    request = TurnEngine.chat_request(
        model_id=app.model_id,
        messages=[],
        tools_enabled=app.tools_enabled,
        max_tokens_tools=app.settings.max_tokens_tools,
        max_tokens_chat=app.settings.max_tokens_chat,
        request_overrides=values,
        thinking_level=app.thinking_level,
        backend_name=app.backend.name,
        graded_thinking_models=app.settings.lmstudio_graded_thinking_models,
    )
    return {key: _checked(key, request.get(key)) for key in FIELDS}


def _next_request(app):
    actual = app._effective_request_overrides()
    inherited_settings = copy.deepcopy(app.settings)
    all_rows = inherited_settings.model_infer_overrides
    inherited_row = dict(all_rows.get(app.model_id, {}))
    for key in FIELDS:
        inherited_row.pop(key, None)
    all_rows[app.model_id] = inherited_row
    inherited = llm_backend._merged_overrides(inherited_settings, app.model_id)
    projected = dict(actual)
    for key in FIELDS:
        if (
            key == "max_tokens"
            and getattr(getattr(app, "_launch_options", None), "max_tokens", None)
            is not None
        ):
            continue
        if key in inherited:
            projected[key] = inherited[key]
        else:
            projected.pop(key, None)
    return _request_values(app, actual), _request_values(app, projected)


def _snapshot(app, binding, info):
    context = backend_catalog._context(app, binding["session_id"])
    service = settings_runtime.service_for(app)
    before = service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024)
    paths, _, revisions = native_settings.owned_documents(
        service, context.session_id, app._agent_session
    )
    if revisions != before.revisions:
        raise ValueError("Model preferences changed while reading owned settings")
    spec = SETTING_SPECS["model_infer_overrides"]
    if spec.scope.value != "conversation" or spec.apply_timing != "next_turn":
        raise ValueError("Model preference persistence scope changed")
    supported = app.backend.name in BACKENDS
    if supported and getattr(app.backend, "_settings", None) is not app.settings:
        raise ValueError("Backend preferences differ from the current runtime settings")
    saved = _map(before.saved.model_infer_overrides)
    effective = _map(app.settings.model_infer_overrides)
    key = binding["model"]
    saved_row, effective_row = _map(saved.get(key, {})), _map(effective.get(key, {}))
    current_request, inherited_request = (
        _next_request(app)
        if supported
        else ({key: None for key in FIELDS}, {key: None for key in FIELDS})
    )
    fields = {}
    for field in FIELDS:
        stored = _checked(field, saved_row.get(field))
        active = _checked(field, effective_row.get(field))
        overridden = (
            field == "max_tokens"
            and getattr(getattr(app, "_launch_options", None), "max_tokens", None)
            is not None
        )
        fields[field] = {
            "present": field in saved_row,
            "saved": stored,
            "active_present": field in effective_row,
            "active": active,
            "next_request": current_request[field],
            "inherited": inherited_request[field],
            "editable": supported and saved == effective and not overridden,
            "overridden": overridden,
        }
    load = app.settings.llama_load_settings.get(key, {})
    if not isinstance(load, dict):
        raise TypeError("Configured model load preferences must be an object")
    result = {
        "version": 1,
        "kind": "model_preferences",
        "scope": "conversation",
        **binding,
        "supported": supported,
        "reason": None
        if supported
        else "This provider owns its inference configuration.",
        "reported_loaded": info[2] if info else None,
        "context_length": info[0] if info else None,
        "context_source": "client_declared"
        if app.backend.name == "custom"
        else "backend_reported",
        "deferred_load_fields": len(load),
        "fields": fields,
    }
    result["revision"] = hashlib.sha256(
        _encoded([result, revisions, saved, effective, load])
    ).hexdigest()
    return result, context, service, before, paths["conversation"]


async def _inspect(app, cmd, op, fields):
    bridge, binding = native_context.read_binding(app, cmd, PREFIX + op, *fields)
    before = backend_catalog._context(app, binding["session_id"])
    info = None
    if binding["backend"] in BACKENDS:
        info = await before.backend.model_info(binding["model"])
        if info is not None and (
            not isinstance(info, (list, tuple))
            or len(info) != 3
            or info[0] is not None
            and (type(info[0]) is not int or not 1 <= info[0] <= 2147483647)
            or not isinstance(info[1], str)
            or type(info[2]) is not bool
        ):
            raise ValueError(
                "Model information is incomplete or exceeds the display budget"
            )
    bridge.check()
    native_context.read_binding(app, cmd, PREFIX + op, *fields)
    after = backend_catalog._context(app, binding["session_id"])
    if any(
        getattr(before, k) is not getattr(after, k)
        for k in ("session", "store", "backend", "settings")
    ):
        raise ValueError("Runtime ownership changed during model inspection")
    return bridge, binding, info, _snapshot(app, binding, info)


def _quiet(app):
    backend_transition._quiet(app, require_unowned_server=False)


def _receipt(binding, r, kind):
    return {
        "version": 1,
        "kind": kind,
        "scope": "conversation",
        **binding,
        "token": r.token,
        "field": r.field,
        "mode": r.mode,
        "value": r.value,
        "before": r.revision,
        "after": r.after,
        "apply_id": r.applied,
    }


async def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    args = {
        "snapshot": (),
        "review": ("revision", "field", "mode", "value"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    if op not in args:
        raise ValueError("Unknown model preference operation")
    initial_bridge, _ = native_context.read_binding(app, cmd, PREFIX + op, *args[op])
    r = getattr(app, "_native_model_preferences_review", None)
    if op in ("apply", "readback"):
        if (
            not isinstance(r, Review)
            or r.token != cmd["token"]
            or r.instance != initial_bridge.instance
        ):
            raise ValueError("Review the current model preferences again")
        if op == "apply" and r.applied is not None:
            raise ValueError("Model preference review was already consumed")
    app._native_model_preferences_review = None
    bridge, binding, info, state = await _inspect(app, cmd, op, args[op])
    snap = state[0]
    if op == "snapshot":
        return snap
    _quiet(app)
    if op == "review":
        field, value, mode = cmd["field"], cmd["value"], cmd["mode"]
        _choice(field, mode, value)
        if mode == "value" and field != "max_tokens":
            gui_rpc._validate(app, {field: value})
        if field not in FIELDS:
            raise ValueError("Choose a supported inference preference")
        row = snap["fields"][field]
        if cmd["revision"] != snap["revision"] or not row["editable"]:
            raise ValueError("Model preferences changed. Refresh and review again.")
        if (
            mode == "inherit"
            and not row["present"]
            or mode == "value"
            and row["present"]
            and row["saved"] == value
        ):
            raise ValueError("Choose a different model preference")
        r = Review(
            secrets.token_hex(32), bridge.instance, snap["revision"], field, value, mode
        )
        app._native_model_preferences_review = r
        return _receipt(binding, r, "model_preferences_review")
    if op == "readback":
        app._native_model_preferences_review = None
        if (
            r.applied != cmd["apply_id"]
            or r.applied is None
            or r.after != snap["revision"]
        ):
            raise ValueError(
                "Model preferences changed after saving. Refresh to inspect the outcome."
            )
        return {**_receipt(binding, r, "model_preferences_saved"), "preferences": snap}
    app._native_model_preferences_review = None
    if r.revision != snap["revision"]:
        raise ValueError("Model preferences changed after review")
    candidate = copy.deepcopy(app.settings.model_infer_overrides)
    values = dict(candidate.get(binding["model"], {}))
    if r.mode == "inherit":
        values.pop(r.field, None)
    else:
        values[r.field] = r.value
    if values:
        candidate[binding["model"]] = values
    else:
        candidate.pop(binding["model"], None)

    def reader(owner, _cmd):
        bridge.check()
        _quiet(owner)
        current, context, service, before, path = _snapshot(owner, binding, info)
        if (
            current["revision"] != r.revision
            or not current["fields"][r.field]["editable"]
        ):
            raise ValueError("Model preferences changed before publication")
        return (
            {
                "field": "model_infer_overrides",
                "value": candidate,
                "scope": "conversation",
                "destination": str(path),
                "effective_before": copy.deepcopy(owner.settings.model_infer_overrides),
            },
            context,
            service,
            before,
        )

    def outcome(_owner, _cmd, status, **detail):
        if status == "saved":
            return None
        if detail.get("mutated") or detail.get("close_required"):
            return "Model preference publication is uncertain. Close and reopen to inspect it."
        return "Model preference was refused. Refresh to inspect it."

    error = native_settings.publish_reviewed(
        app, cmd, reader, outcome, lambda _k, value: _map(value), strict=True
    )
    if error:
        raise ValueError(error)
    try:
        current = _snapshot(app, binding, info)[0]
        expected = (
            snap["fields"][r.field]["inherited"] if r.mode == "inherit" else r.value
        )
        if (
            app.settings.model_infer_overrides != candidate
            or current["revision"] == r.revision
            or current["fields"][r.field]["next_request"] != expected
        ):
            raise ValueError("Model preferences failed effective request readback")
    except Exception as exc:
        app._backend_transition_error = backend_transition.CLOSE_REQUIRED
        app._owned_launch_error = backend_transition.CLOSE_REQUIRED
        app._cli_launch_error = backend_transition.CLOSE_REQUIRED
        raise ValueError(
            "Model preferences were saved, but effective readback failed. Close and reopen to inspect them."
        ) from exc
    r.after, r.applied = current["revision"], cmd["id"]
    app._native_model_preferences_review = r
    return _receipt(binding, r, "model_preferences_applied")
