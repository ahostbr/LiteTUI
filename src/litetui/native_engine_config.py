"""One reviewed preference for an explicitly managed, current local engine.

Where to connect (*_host) is per conversation; what starts the engine is device-wide
(develop 5f5ef63f, Ryan 2026-10-24), so each field carries its own scope and destination.
"""

from __future__ import annotations

import copy
import secrets

from litetui import (
    backend_catalog,
    gui_rpc,
    native_engine,
    native_settings,
    ninfer_engine,
    settings_runtime,
)
from litetui import settings as setting_defs
from litetui.background_input import _keys, text
from litetui.settings_scope import SETTING_SPECS

PREFIX = "gui.engine.native_config_"
FIELDS = {
    "ninfer": (
        "ninfer_host",
        "ninfer_executable",
        "ninfer_artifact",
        "ninfer_max_context",
        "ninfer_max_concurrency",
        "ninfer_kv_dtype",
        "ninfer_kv_capacity",
        "ninfer_host_kv_mib",
    ),
    "strata": ("strata_host", "strata_root", "strata_config", "strata_max_context"),
}
MAX_INT = 2**31 - 1


def _guard(app):
    current = native_engine.state(app)
    if (
        current.pending
        or current.backend._starting
        or current.backend._stopping
        or current.shutdown_requested
        or current.fatal
    ):
        raise ValueError("Wait for the current engine lifecycle operation")
    return (
        current.instance,
        current.generation,
        current.backend,
        current.backend._owned,
        current.admission,
        current.effective_model,
        current.desired_model,
    )


def _checked(field, value):
    if field in {
        "ninfer_max_context",
        "ninfer_max_concurrency",
        "ninfer_host_kv_mib",
        "strata_max_context",
    }:
        if value is None and field in {"ninfer_host_kv_mib", "strata_max_context"}:
            return value
        lo, hi = (
            ninfer_engine.NINFER_CONCURRENCY_RANGE
            if field == "ninfer_max_concurrency"
            else (1, MAX_INT)
        )
        if type(value) is not int or not lo <= value <= hi:
            raise ValueError(f"{field} requires an integer from {lo} to {hi}")
    else:
        if (
            type(value) is not str
            or len(value.encode("utf-8")) > 4096
            or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
        ):
            raise ValueError(
                f"{field} requires a single line of at most 4096 UTF-8 bytes"
            )
        if field == "ninfer_kv_dtype" and value not in ninfer_engine.NINFER_KV_DTYPES:
            raise ValueError("Choose a supported NInfer KV data type")
        if (
            field == "ninfer_kv_capacity"
            and value not in ("", "auto")
            and not (
                len(value) <= 10
                and value.isascii()
                and value.isdecimal()
                and 1 <= int(value) <= MAX_INT
            )
        ):
            raise ValueError(
                "KV capacity requires blank, auto or a positive token count"
            )
    return value


def _snapshot(app, cmd):
    current, context, common = native_engine._common(app, cmd)
    if not native_engine.requested(app):
        raise ValueError("Explicit engine management is required")
    _guard(app)
    if reason := native_engine._quiet_reason(app):
        raise ValueError(reason)
    service = settings_runtime.service_for(app)
    paths, _, revisions = native_settings.owned_documents(
        service, context.session_id, context.session
    )
    before = service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024)
    if before.revisions != revisions:
        raise ValueError("Engine preferences changed during inspection")
    fields = {}
    for field in FIELDS[context.selected]:
        spec = SETTING_SPECS[field]
        if (spec.scope.value, spec.apply_timing) not in (
            ("conversation", "reconnect"),
            ("device", "restart"),
        ):
            raise ValueError("Engine preference scope or timing changed")
        reason = None
        if setting_defs.source_of(field):
            reason = "An environment override owns this preference"
        elif field in getattr(app, "_invocation_saved_values", {}):
            reason = "An explicit launch option owns this preference"
        fields[field] = {
            "saved": getattr(before.saved, field),
            "effective": getattr(app.settings, field),
            "timing": spec.apply_timing,
            "scope": spec.scope.value,
            "locked": reason,
        }
        for value in (fields[field]["saved"], fields[field]["effective"]):
            if isinstance(value, str) and len(value.encode("utf-8")) > 4096:
                raise ValueError("Saved engine preference exceeds its display limit")
    result = {
        "kind": "engine_config_snapshot",
        **common,
        "scope": "mixed",
        "generation": current.generation,
        "admission": current.admission,
        "fields": fields,
        "revisions": revisions,
        "destination": {"conversation": str(paths["conversation"]), "device": str(paths["global"])},
    }
    result["snapshot"] = backend_catalog.fingerprint(app, context, result)
    return result, context, service, before


def _review(app, cmd, snapshot, field, value):
    snap, context, service, before = _snapshot(app, cmd)
    if (
        snapshot != snap["snapshot"]
        or type(field) is not str
        or field not in snap["fields"]
    ):
        raise ValueError(
            "Engine configuration changed; refresh and review the field again"
        )
    row = snap["fields"][field]
    if row["locked"]:
        raise ValueError(row["locked"])
    _checked(field, value)
    gui_rpc._validate(app, {field: value})
    if row["saved"] == value and type(row["saved"]) is type(value):
        raise ValueError("Saved engine preference already has this value")
    return (
        {
            "kind": "engine_config_review",
            **{k: v for k, v in snap.items() if k not in ("kind", "fields")},
            # One field, one canonical document: the publication core routes by this scope.
            "scope": row["scope"],
            "destination": snap["destination"][row["scope"]],
            "field": field,
            "value": value,
            "saved_before": row["saved"],
            "effective_before": row["effective"],
            "timing": row["timing"],
        },
        context,
        service,
        before,
    )


def dispatch(app, cmd):
    # Metadata access for this exact engine management operation never grants a turn.
    with native_engine.inspection(app):
        return _dispatch(app, cmd)


def _dispatch(app, cmd):
    action = cmd["type"].removeprefix(PREFIX)
    extra = {
        "snapshot": (),
        "review": ("snapshot", "field", "value"),
        "save": ("review",),
        "readback": ("review", "save_id"),
    }
    if action not in extra:
        raise ValueError("Unknown engine configuration operation")
    _keys(
        cmd,
        (
            "type",
            "id",
            "version",
            "session_id",
            "agent_id",
            "backend",
            "desired_model",
            "management_instance",
            *extra[action],
        ),
    )
    text(cmd["id"])
    current, _, common = native_engine._common(app, cmd)
    if not native_engine.requested(app):
        raise ValueError("Explicit engine management is required")
    if action == "snapshot":
        return _snapshot(app, cmd)[0]
    if action == "review":
        reviewed = _review(app, cmd, cmd["snapshot"], cmd["field"], cmd["value"])[0]
        reviewed["review"] = secrets.token_hex(32)
        current.config_review = reviewed
        return copy.deepcopy(reviewed)
    if action == "save":
        reviewed = getattr(current, "config_review", None)
        if reviewed is None or cmd["review"] != reviewed["review"]:
            raise ValueError("Review this engine preference again")
        current.config_review = None
        current.config_save = None

        def reader(app, cmd):
            review, context, service, before = _review(
                app, cmd, reviewed["snapshot"], reviewed["field"], reviewed["value"]
            )
            review["review"] = reviewed["review"]
            if review != reviewed:
                raise ValueError("Engine configuration changed since review")
            return review, context, service, before

        def receipt(_app, _cmd, status, **evidence):
            if status == "refused":
                return {
                    "kind": "engine_config_refused",
                    **common,
                    "review": reviewed["review"],
                    "save_id": cmd["id"],
                    "field": reviewed["field"],
                    "mutated": False,
                    "reason": evidence.get("error") or "Engine preference was refused",
                }
            if status == "failed" or evidence.get("close_required"):
                current.fatal = "Engine preference publication is uncertain; close and inspect the saved settings"
                return {"outcome": "failed", "reason": current.fatal}
            return {
                "kind": "engine_config_save",
                **common,
                "review": reviewed["review"],
                "save_id": cmd["id"],
                "field": reviewed["field"],
                "saved": evidence["saved"],
                "effective": evidence["effective"],
                "timing": reviewed["timing"],
                "revisions": evidence["revisions"],
                "generation": current.generation,
                "admission": current.admission,
            }

        saved = native_settings.publish_reviewed(
            app,
            cmd,
            reader,
            receipt,
            lambda _field, observed: observed,
            strict=True,
            guard=_guard,
        )
        if saved.get("kind") == "engine_config_refused":
            return saved
        if saved.get("outcome") == "failed":
            raise RuntimeError(saved["reason"])
        current.reviews.clear()  # A previous lifecycle review never silently inherits a preference edit.
        current.config_save = copy.deepcopy(saved)
        return saved
    saved = getattr(current, "config_save", None)
    if (
        saved is None
        or cmd["review"] != saved["review"]
        or cmd["save_id"] != saved["save_id"]
    ):
        raise ValueError(
            "Independent readback requires the exact engine preference save"
        )
    snap = _snapshot(app, cmd)[0]
    row = snap["fields"][saved["field"]]
    if any(saved[k] != snap[k] for k in ("revisions", "generation", "admission")) or (
        row["saved"],
        row["effective"],
        row["timing"],
    ) != (saved["saved"], saved["effective"], saved["timing"]):
        raise ValueError(
            "Engine preferences changed after save; refresh before editing"
        )
    return {**saved, "kind": "engine_config_readback"}
