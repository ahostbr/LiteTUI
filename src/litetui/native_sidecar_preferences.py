"""Reviewed opt-in preference for the optional native desktop."""
from __future__ import annotations

import copy
import secrets

from litetui import (
    backend_catalog,
    backend_transition,
    native_context,
    native_settings,
    settings,
    settings_runtime,
)

PREFIX = "gui.sidecar_preferences.native_"
FIELDS = ("sidecar_enabled",)


def _snapshot(app, binding):
    service = settings_runtime.service_for(app)
    before = service.snapshot(app.convo_id, read_only=True)
    paths, _, revisions = native_settings.owned_documents(service, app.convo_id, app._agent_session)
    if revisions != before.revisions:
        raise ValueError("Sidecar opt-in changed during inspection.")
    saved, effective = before.saved.sidecar_enabled, app.settings.sidecar_enabled
    if type(saved) is not bool or type(effective) is not bool:
        raise ValueError("Sidecar opt-in must be a Boolean.")
    result = {"kind": "sidecar_preferences", "version": 1, **binding,
              "fields": {"sidecar_enabled": {"saved": saved, "effective": effective,
                       "editable": saved == effective and not settings.source_of("sidecar_enabled")}},
              "destination": str(paths["global"]), "revisions": revisions}
    context = backend_catalog._context(app, app.convo_id)
    result["revision"] = backend_catalog.fingerprint(app, context, result)
    return result, context, service, before


def dispatch(app, cmd):
    from litetui import native_sidecar
    owner = native_sidecar.manager(app)
    if owner is None or native_sidecar.DELEGATE.get() != (owner, native_sidecar.task()):
        raise ValueError("Sidecar opt-in requires its retained terminal proposal.")
    owner.current()
    op = cmd["type"].removeprefix(PREFIX)
    arguments = {"preferences": (), "review": ("revision", "field", "value"),
                 "apply": ("token",), "readback": ("token", "apply_id")}
    if op not in arguments:
        raise ValueError("Unknown Sidecar preference operation.")
    _, binding = native_context.read_binding(app, cmd, PREFIX + op, *arguments[op])
    if op == "preferences":
        return _snapshot(app, binding)[0]
    if op == "review":
        native_sidecar.require_admission(app, owner)
        backend_transition._quiet(app, require_unowned_server=False)
        snapshot = _snapshot(app, binding)[0]
        if (cmd["field"] != "sidecar_enabled" or type(cmd["value"]) is not bool or
                cmd["revision"] != snapshot["revision"] or not snapshot["fields"]["sidecar_enabled"]["editable"]
                or cmd["value"] == snapshot["fields"]["sidecar_enabled"]["saved"]):
            raise ValueError("Review a different available Sidecar opt-in.")
        review: dict | None = {"kind": "sidecar_preference_review", "version": 1, **binding, "token": secrets.token_hex(32),
                  "field": cmd["field"], "value": cmd["value"], "before": snapshot, "apply_id": None}
        app._native_sidecar_preference = review
        return copy.deepcopy(review)
    review = getattr(app, "_native_sidecar_preference", None)
    if not isinstance(review, dict) or review["token"] != cmd["token"]:
        raise ValueError("Sidecar preference needs its exact review.")
    if op == "apply":
        native_sidecar.require_admission(app, owner)
        if review["apply_id"] is not None:
            raise ValueError("Sidecar preference was consumed.")
        review["apply_id"] = cmd["id"]
        def reader(owner, _cmd):
            native_sidecar.require_admission(owner, native_sidecar.manager(owner))
            backend_transition._quiet(owner, require_unowned_server=False)
            current, context, service, before = _snapshot(owner, binding)
            if current != review["before"]:
                raise ValueError("Sidecar preference changed after review.")
            return {"field": "sidecar_enabled", "value": review["value"], "scope": "device",
                    "destination": current["destination"], "effective_before": current["fields"]["sidecar_enabled"]["effective"]}, context, service, before
        def receipt(_app, _cmd, status, **evidence):
            if status != "saved":
                raise ValueError("Sidecar preference was not independently saved: " + str(evidence.get("error")))
        native_settings.publish_reviewed(app, cmd, reader, receipt, lambda _key, value: value, strict=True)
        return {**copy.deepcopy(review), "kind": "sidecar_preference_applied"}
    if cmd["apply_id"] != review["apply_id"] or review["apply_id"] is None:
        raise ValueError("Sidecar preference readback identifies another save.")
    after = _snapshot(app, binding)[0]
    field = after["fields"]["sidecar_enabled"]
    if field["saved"] != review["value"] or field["effective"] != review["value"]:
        raise ValueError("Sidecar opt-in saved/effective readback differs.")
    return {**copy.deepcopy(review), "kind": "sidecar_preference_saved", "preferences": after}
