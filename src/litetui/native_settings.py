"""Guarded owned output-budget review and save; never starts or loads engines."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, is_dataclass

from litetui import (
    backend_catalog,
    backend_transition,
    codex_settings,
    settings_runtime,
)
from litetui.settings_scope import SETTING_SPECS
from litetui.settings_service import SettingChange

VERSION = 1
FIELDS = ("max_tokens_chat", "max_tokens_tools")
MAXIMUM = 2147483647
CLOSE_REQUIRED = "Settings publication is uncertain; close this runtime and reopen explicitly."


def _version(cmd):
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Native settings require version 1")


def _integer(value):
    if type(value) is not int or not 1 <= value <= MAXIMUM:
        raise ValueError(f"Output budget must be an integer from 1 through {MAXIMUM}")
    return value


def _bounded(value):
    backend_catalog._encoded(value, 64 * 1024)
    return value


def _identity(app):
    session = getattr(app, "_agent_session", None)
    return {"session_id": getattr(app, "convo_id", None),
            "agent_id": session.authority.agent_id if session is not None else None,
            "backend": getattr(getattr(app, "backend", None), "name", None),
            "model": getattr(app, "model_id", None)}


def _snapshot(app, cmd):
    _version(cmd)
    backend_transition._quiet(app, require_unowned_server=False)
    context = backend_catalog._context(app, cmd.get("session_id"))
    # Validate owned paths and byte limits before the canonical snapshot reader.
    before_read = backend_catalog.fingerprint(app, context, {"kind": "output_budget_preflight"})
    service = settings_runtime.service_for(app)
    saved = service.snapshot(context.session_id, read_only=True)
    controls = {key: codex_settings.control(app.backend, key) for key in FIELDS}
    request = {}
    if any(control is None or control.editable for control in controls.values()):
        request = app.backend.request_overrides(app.model_id)
        if not isinstance(request, dict):
            raise ValueError("Backend request controls are unavailable")
    fields = {}
    from litetui import settings

    for key in FIELDS:
        spec = SETTING_SPECS[key]
        if spec.scope.value != "conversation" or spec.apply_timing != "next_turn":
            raise ValueError("Output budget source scope or timing changed")
        control = controls[key]
        reason = control.help if control is not None and not control.editable else None
        if getattr(getattr(app, "_launch_options", None), "max_tokens", None) is not None:
            reason = "The --max-tokens launch option owns request output budgets"
        elif "max_tokens" in request:
            reason = "The selected model's request override owns its output budget"
        elif settings.source_of(key):
            reason = f"Environment {settings.source_of(key)} owns this output budget"
        fields[key] = {"scope": spec.scope.value, "apply_timing": spec.apply_timing,
                       "saved": _integer(getattr(saved.saved, key)),
                       "effective": _integer(getattr(app.settings, key)),
                       "editable": reason is None, "reason": reason,
                       "minimum": 1, "maximum": MAXIMUM}
    result = {"version": VERSION, "kind": "output_budget_snapshot", **_identity(app),
              "revisions": saved.revisions, "fields": fields}
    token = backend_catalog.fingerprint(app, context, result)
    if service.snapshot(context.session_id, read_only=True).revisions != saved.revisions:
        raise ValueError("Settings changed while reading; refresh before editing")
    if backend_catalog._context(app, context.session_id) != context or backend_catalog.fingerprint(app, context, result) != token:
        raise ValueError("Settings authority changed while reading; refresh before editing")
    if backend_catalog.fingerprint(app, context, {"kind": "output_budget_preflight"}) != before_read:
        raise ValueError("Settings changed while reading; refresh before editing")
    result["snapshot"] = token
    return _bounded(result), context, service, saved


def snapshot(app, cmd):
    return _snapshot(app, cmd)[0]


def _review(app, cmd):
    current, context, service, saved = _snapshot(app, cmd)
    if cmd.get("agent_id") != current["agent_id"]:
        raise ValueError("Settings review belongs to another agent")
    if cmd.get("snapshot") != current["snapshot"] or cmd.get("revisions") != current["revisions"]:
        raise ValueError("Settings review is stale; refresh before editing")
    key = cmd.get("field")
    if not isinstance(key, str) or key not in FIELDS:
        raise ValueError("Only conversation output budgets are editable here")
    field = current["fields"][key]
    if not field["editable"]:
        raise ValueError(field["reason"])
    value = _integer(cmd.get("value"))
    from litetui.gui_rpc import _validate

    # Reuse the same source type/positive/environment validation as Settings.
    _validate(app, {key: value})
    review = {"version": VERSION, "kind": "output_budget_review", **_identity(app),
              "snapshot": current["snapshot"], "revisions": current["revisions"],
              "field": key, "value": value, "scope": field["scope"],
              "apply_timing": field["apply_timing"], "saved_before": field["saved"],
              "effective_before": field["effective"]}
    review["review"] = hashlib.sha256(backend_catalog._encoded(review, 64 * 1024)).hexdigest()
    return _bounded(review), context, service, saved


def validate(app, cmd):
    """Pure review: no settings files, runtime values or launch overrides change."""
    return _review(app, cmd)[0]


def _protected(app, field):
    from litetui import store_identity
    from litetui.gui_rpc import _tool_policy

    values = asdict(app.settings)
    values.pop(field)
    launch = getattr(app, "_launch_options", None)
    return backend_catalog._encoded({
        "settings": values, "authority": asdict(app._agent_session.authority),
        "policy": _tool_policy(app), "history": app.conversation,
        "pending": getattr(app, "_pending_input", []),
        "hold": getattr(app, "_hold_restored_input", False),
        "identity": store_identity.capture(app),
        "launch": asdict(launch) if is_dataclass(launch) and not isinstance(launch, type) else None,
        "thinking": getattr(app, "_thinking_level", None),
        "cli_thinking": getattr(app, "_cli_effective_thinking", None),
    }, 8 * 1024 * 1024)


def _receipt(app, cmd, status, *, mutated=False, close_required=False, error=None,
             persistence=(), runtime=(), saved=None, effective=None, revisions=None):
    try:
        identity = _identity(app)
    except (ValueError, OSError):
        identity = {"session_id": getattr(app, "convo_id", None), "agent_id": cmd.get("agent_id"),
                    "backend": getattr(getattr(app, "backend", None), "name", None),
                    "model": getattr(app, "model_id", None)}
    return _bounded({"version": VERSION, "kind": "output_budget_save", **identity,
                     "snapshot": cmd.get("snapshot"), "review": cmd.get("review"),
                     "field": cmd.get("field"), "value": cmd.get("value"), "status": status,
                     "mutated": mutated, "close_required": close_required, "error": error,
                     "persistence": [asdict(item) for item in persistence],
                     "runtime": [asdict(item) for item in runtime], "saved": saved,
                     "effective": effective, "revisions": revisions})


def save(app, cmd):
    """One explicit publication attempt; no automatic retry or rollback."""
    try:
        review, context, service, before = _review(app, cmd)
        if cmd.get("review") != review["review"]:
            raise ValueError("Save requires the exact validated review")
        key, value = review["field"], review["value"]
        protected = _protected(app, key)
        requested = copy.deepcopy(app.settings)
        setattr(requested, key, value)
        backend, settings, conversation = app.backend, app.settings, app.conversation
        # Last source read immediately before the synchronous publication boundary.
        if _review(app, cmd)[0] != review:
            raise ValueError("Settings changed before publication")
    except Exception as exc:  # noqa: BLE001 - malformed/stale requests are recoverable
        return _receipt(app, cmd, "refused", error=str(exc)[:2048])

    result = None
    combined = None
    try:
        result = service.save_patch(context.session_id, [SettingChange(key, value, "conversation")], before.revisions)
        after = service.snapshot(context.session_id, read_only=True)
        if (app.backend is not backend or app.settings is not settings or app.conversation is not conversation
                or _protected(app, key) != protected):
            raise ValueError("Settings authority changed during publication")
        if not result.fully_saved:
            if after.revisions != before.revisions or getattr(app.settings, key) != review["effective_before"]:
                raise ValueError("Settings publication failed after an observable change")
            return _receipt(app, cmd, "refused", persistence=result.persistence,
                            saved=getattr(after.saved, key), effective=getattr(app.settings, key),
                            revisions=after.revisions, error="; ".join(item.error or "Save refused" for item in result.persistence if not item.saved)[:2048])
        expected = asdict(before.saved)
        expected[key] = value
        if (asdict(after.saved) != expected or after.revisions['global'] != before.revisions['global']
                or after.revisions['agent'] != before.revisions['agent']):
            raise ValueError("Saved output budget failed independent durable readback")
        combined = settings_runtime.apply_saved_result(app, requested, result)
        if (app.backend is not backend or app.settings is not settings or app.conversation is not conversation
                or _protected(app, key) != protected):
            raise ValueError("Settings application changed protected execution state")
        effective = _integer(getattr(app.settings, key))
        if len(combined.runtime) != 1 or combined.runtime[0].field != key or combined.runtime[0].effective != effective:
            raise ValueError("Runtime output budget outcome disagrees with readback")
        status = "saved" if combined.runtime[0].status == "applied" and effective == value else "partial"
        return _receipt(app, cmd, status, mutated=True, persistence=combined.persistence,
                        runtime=combined.runtime, saved=getattr(after.saved, key), effective=effective,
                        revisions=after.revisions)
    except BaseException as exc:
        # A raised write/apply can have completed before failing; never guess rollback.
        app._backend_transition_error = CLOSE_REQUIRED
        app._owned_launch_error = CLOSE_REQUIRED
        app._cli_launch_error = CLOSE_REQUIRED
        if not isinstance(exc, Exception):
            raise
        return _receipt(app, cmd, "failed", mutated=True, close_required=True,
                        error=(str(exc) or type(exc).__name__)[:2048],
                        persistence=result.persistence if result else (),
                        runtime=combined.runtime if combined else ())
