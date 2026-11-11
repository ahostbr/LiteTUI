"""Reviewed current-model inference preferences in the owned conversation."""

from __future__ import annotations

import asyncio
import copy
import secrets
from dataclasses import dataclass

from litetui import (
    backend_catalog,
    backend_transition,
    llm_backend,
    native_context,
    native_settings,
    settings_runtime,
)
from litetui import (
    native_model_preference_catalog as vocabulary,
)
from litetui.plugins import model_switch
from litetui.settings_scope import SETTING_SPECS
from litetui.turn_engine import TurnEngine

PREFIX = "gui.models.native_preferences_"
FIELDS = vocabulary.FIELDS
BACKENDS = vocabulary.BACKENDS


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
    owners: tuple = ()
    snapshot: dict | None = None
    info: object = None
    apply_published: bool = False
    publication: object = None
    phase: str | None = None
    task: object = None
    command: bytes | None = None
    result: object = None
    raw: bytes | None = None


def _encoded(value):
    return backend_catalog._encoded(value, 1024 * 1024)


def _checked(field, value):
    return vocabulary.checked(field, value)


def _map(value):
    if not isinstance(value, dict) or any(not isinstance(k, str) for k in value):
        raise ValueError("Model inference preferences must be an object")
    _encoded(value)
    return value


def _choice(field, mode, value):
    if (
        mode in ("inherit", "server")
        and value is None
        and (mode != "server" or field in vocabulary.INFERENCE)
    ):
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
    extra = request.get("extra_body", {})
    return {
        key: (
            request.get("response_format")
            if key == "json_schema"
            else extra.get("chat_template_kwargs", {}).get("enable_thinking")
            if key == "enable_thinking"
            else request.get(key, extra.get(key))
        )
        for key in vocabulary.INFERENCE
    }


def _next_request(app):
    actual = app._effective_request_overrides()
    inherited_settings = copy.deepcopy(app.settings)
    all_rows = inherited_settings.model_infer_overrides
    inherited_row = dict(all_rows.get(app.model_id, {}))
    for key in vocabulary.INFERENCE:
        inherited_row.pop(key, None)
    all_rows[app.model_id] = inherited_row
    inherited = llm_backend._merged_overrides(inherited_settings, app.model_id)
    projected = dict(actual)
    for key in vocabulary.INFERENCE:
        if (
            key == "reasoning_effort"
            and getattr(app, "_cli_effective_thinking", None) is not None
            or key == "max_tokens"
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
    for name, timing in (
        ("model_infer_overrides", "next_turn"),
        ("llama_load_settings", "reload"),
    ):
        spec = SETTING_SPECS[name]
        if spec.scope.value != "conversation" or spec.apply_timing != timing:
            raise ValueError("Model preference persistence scope changed")
    supported = app.backend.name in BACKENDS
    if supported and getattr(app.backend, "_settings", None) is not app.settings:
        raise ValueError("Backend preferences differ from the current runtime settings")
    catalog = vocabulary.catalog(app)
    key = binding["model"]
    documents = {
        "inference": {
            "saved": _map(before.saved.model_infer_overrides),
            "active": _map(app.settings.model_infer_overrides),
        },
        "load": {
            "saved": _map(before.saved.llama_load_settings),
            "active": _map(app.settings.llama_load_settings),
        },
    }
    raw = {
        group: {
            state: copy.deepcopy(_map(rows.get(key, {})))
            for state, rows in values.items()
        }
        for group, values in documents.items()
    }
    current_request, inherited_request = (
        _next_request(app)
        if supported
        else (
            {key: None for key in vocabulary.INFERENCE},
            {key: None for key in vocabulary.INFERENCE},
        )
    )
    fields = {}
    for field in FIELDS:
        meta = catalog[field]
        group = meta["group"]
        saved_row, active_row = raw[group]["saved"], raw[group]["active"]
        stored, active = (
            _checked(field, saved_row.get(field)),
            _checked(field, active_row.get(field)),
        )
        overridden = (
            field == "max_tokens"
            and getattr(getattr(app, "_launch_options", None), "max_tokens", None)
            is not None
            or field == "reasoning_effort"
            and getattr(app, "_cli_effective_thinking", None) is not None
        )
        default = None
        if group == "inference" and supported:
            values = dict(app._effective_request_overrides())
            values.pop(field, None)
            if field == "reasoning_effort":
                values[field] = "default"
            default = _request_values(app, values)[field]
        fields[field] = {
            "present": field in saved_row,
            "saved": stored,
            "active_present": field in active_row,
            "active": active,
            "next_request": current_request[field] if group == "inference" else active,
            "inherited": inherited_request[field] if group == "inference" else None,
            "editable": meta["editable"]
            and not overridden
            and (
                group == "load"
                or documents[group]["saved"] == documents[group]["active"]
            ),
            "overridden": overridden,
            "server_default": default,
        }
    load = raw["load"]["active"]
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
        "catalog": catalog,
        "raw": raw,
    }
    result["revision"] = backend_catalog.fingerprint(
        app,
        context,
        {"preferences": result, "revisions": revisions, "documents": documents},
        include_pending=False,
    )
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


def _owners(app):
    return (app.backend, app.settings, app._agent_session, app.conversation)


def _same(app, review):
    if any(a is not b for a, b in zip(_owners(app), review.owners, strict=True)):
        raise ValueError("Model preferences lost their exact runtime owner")


def _fatal(app):
    app._backend_transition_error = backend_transition.CLOSE_REQUIRED
    app._owned_launch_error = backend_transition.CLOSE_REQUIRED
    app._cli_launch_error = backend_transition.CLOSE_REQUIRED


def _release(app):
    app._native_model_preferences_hold = False


def _result(app, cmd, r, binding, kind, **extra):
    result = {**_receipt(binding, r, kind), **extra}
    r.phase = "readback" if kind == "model_preferences_saved" else "apply"
    r.task, r.command, r.result, r.raw = (
        asyncio.current_task(),
        _encoded(cmd),
        result,
        _encoded(result),
    )
    app._native_model_preferences_review = r
    return result


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
    if isinstance(r, Review) and r.applied is not None and op != "readback":
        raise ValueError(
            "Model preference review was already consumed; await its readback"
        )
    if op in ("apply", "readback"):
        if (
            not isinstance(r, Review)
            or r.token != cmd["token"]
            or r.instance != initial_bridge.instance
        ):
            raise ValueError("Review the current model preferences again")
        _same(app, r)
        if op == "readback" and (not r.apply_published or r.phase != "apply"):
            raise ValueError("Await the exact published preference application")
    app._native_model_preferences_review = None
    try:
        bridge, binding, info, state = await _inspect(app, cmd, op, args[op])
        snap = state[0]
        if op == "snapshot":
            return snap
        if op == "readback":
            if (
                r.applied != cmd["apply_id"]
                or r.applied is None
                or r.after != snap["revision"]
                or r.snapshot != snap
            ):
                raise ValueError(
                    "Model preferences changed after saving. Refresh to inspect the outcome."
                )
            return _result(
                app, cmd, r, binding, "model_preferences_saved", preferences=snap
            )
        _quiet(app)
        if op == "review":
            field, value, mode = cmd["field"], cmd["value"], cmd["mode"]
            _choice(field, mode, value)
            if field not in FIELDS:
                raise ValueError("Choose a supported model preference")
            row, meta = snap["fields"][field], snap["catalog"][field]
            if mode == "value":
                vocabulary.checked(field, value, meta)
            if cmd["revision"] != snap["revision"] or not row["editable"]:
                raise ValueError("Model preferences changed. Refresh and review again.")
            if (
                mode == "inherit"
                and not row["present"]
                or mode != "inherit"
                and row["present"]
                and row["saved"] == value
            ):
                raise ValueError("Choose a different model preference")
            candidate = dict(snap["raw"][meta["group"]]["saved"])
            if mode == "inherit":
                candidate.pop(field, None)
            else:
                candidate[field] = value
            if meta["group"] == "load":
                model_switch.validate_load_cfg(candidate)
                if app.backend.name == "llamacpp":
                    selected = app.model_rows.get(app.model_id)
                    llm_backend.resolve_speculation(
                        app.model_id,
                        getattr(selected, "path", None),
                        candidate,
                        app.settings,
                    )
            r = Review(
                secrets.token_hex(32),
                bridge.instance,
                snap["revision"],
                field,
                value,
                mode,
                owners=_owners(app),
                info=info,
            )
            app._native_model_preferences_review = r
            return _receipt(binding, r, "model_preferences_review")
        if r.revision != snap["revision"]:
            raise ValueError("Model preferences changed after review")
        group = snap["catalog"][r.field]["group"]
        setting = (
            "model_infer_overrides" if group == "inference" else "llama_load_settings"
        )
        candidate = copy.deepcopy(getattr(state[3].saved, setting))
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
            _same(owner, r)
            _quiet(owner)
            current, context, service, before, path = _snapshot(owner, binding, info)
            if (
                current["revision"] != r.revision
                or not current["fields"][r.field]["editable"]
            ):
                raise ValueError("Model preferences changed before publication")
            if group == "load":
                model_switch.validate_load_cfg(values)
                if owner.backend.name == "llamacpp":
                    selected = owner.model_rows.get(owner.model_id)
                    llm_backend.resolve_speculation(
                        owner.model_id,
                        getattr(selected, "path", None),
                        values,
                        owner.settings,
                    )
            return (
                {
                    "field": setting,
                    "value": candidate,
                    "scope": "conversation",
                    "destination": str(path),
                    "effective_before": copy.deepcopy(getattr(owner.settings, setting)),
                },
                context,
                service,
                before,
            )

        def outcome(_owner, _cmd, status, **detail):
            if status == "saved":
                return None
            runtime = detail.get("runtime", ())
            if (
                group == "load"
                and status == "partial"
                and len(runtime) == 1
                and runtime[0].status == "pending"
                and runtime[0].action == "reload"
            ):
                return None
            if detail.get("mutated") or detail.get("close_required"):
                return "Model preference publication is uncertain. Close and reopen to inspect it."
            return "Model preference was refused. Refresh to inspect it."

        app._native_model_preferences_hold = True
        error = native_settings.publish_reviewed(
            app, cmd, reader, outcome, lambda _k, value: _map(value), strict=True
        )
        if error:
            if not getattr(app, "_backend_transition_error", None):
                _release(app)
            raise ValueError(error)
        current = _snapshot(app, binding, info)[0]
        expected = (
            snap["fields"][r.field]["inherited"]
            if r.mode == "inherit"
            else snap["fields"][r.field]["server_default"]
            if r.mode == "server"
            else vocabulary.wire_value(r.field, r.value, snap["catalog"][r.field])
        )
        if (
            current["raw"][group]["saved"] != values
            or current["revision"] == r.revision
            or group == "inference"
            and (
                getattr(app.settings, setting) != candidate
                or current["fields"][r.field]["next_request"] != expected
            )
            or group == "load"
            and current["raw"][group]["active"] != snap["raw"][group]["active"]
        ):
            raise ValueError("Model preferences failed independent effective readback")
        r.after, r.applied, r.snapshot = current["revision"], cmd["id"], current
        return _result(app, cmd, r, binding, "model_preferences_applied")
    except BaseException:
        if getattr(app, "_native_model_preferences_hold", False):
            _fatal(app)
        raise


@dataclass
class Publication:
    app: object
    review: Review
    task: object
    result: object
    raw: bytes
    phase: str
    used: bool = False


def readback_allowed(app, cmd):
    r = getattr(app, "_native_model_preferences_review", None)
    if (
        not isinstance(r, Review)
        or cmd.get("type") != PREFIX + "readback"
        or not r.apply_published
        or r.phase != "apply"
        or cmd.get("token") != r.token
        or cmd.get("apply_id") != r.applied
    ):
        return False
    native_context.read_binding(app, cmd, PREFIX + "readback", "token", "apply_id")
    _same(app, r)
    return True


def prepare_publication(app, cmd, result):
    if cmd.get("type") not in (PREFIX + "apply", PREFIX + "readback"):
        return None
    r = getattr(app, "_native_model_preferences_review", None)
    phase = "readback" if cmd["type"].endswith("readback") else "apply"
    try:
        if (
            not isinstance(r, Review)
            or r.publication is not None
            or r.task is not asyncio.current_task()
            or r.phase != phase
            or r.command != _encoded(cmd)
            or r.result is not result
            or r.raw != _encoded(result)
            or phase == "apply"
            and r.apply_published
        ):
            raise ValueError("Model preference publication lost its exact owner")
        _same(app, r)
        _, binding = native_context.read_binding(
            app,
            cmd,
            cmd["type"],
            *(("token",) if phase == "apply" else ("token", "apply_id")),
        )
        if _snapshot(app, binding, r.info)[0] != r.snapshot:
            raise ValueError("Model preferences changed before publication")
        r.publication = Publication(
            app, r, asyncio.current_task(), result, _encoded(result), phase
        )
        return r.publication
    except Exception:
        _fatal(app)
        raise


def finish_publication(app, ticket):
    r = ticket.review
    if (
        ticket.app is not app
        or ticket.used
        or ticket.task is not asyncio.current_task()
        or r.publication is not ticket
        or _encoded(ticket.result) != ticket.raw
        or getattr(app, "_native_model_preferences_review", None) is not r
    ):
        _fatal(app)
        raise ValueError("Model preference publication lost its one-use owner")
    ticket.used, r.publication = True, None
    if ticket.phase == "apply":
        r.apply_published = True
    else:
        app._native_model_preferences_review = None
        if not getattr(app, "_gui_quitting", False):
            _release(app)
