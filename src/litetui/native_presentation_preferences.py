"""Device presentation preferences (transcript, dialogs, footer) under an active owned Work instance."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

from litetui import (
    backend_catalog,
    backend_transition,
    native_context,
    native_settings,
    settings,
    settings_runtime,
)
from litetui.settings_scope import SETTING_SPECS

PREFIX = "gui.presentation.native_"
FIELDS = (
    "show_thinking",
    "show_stop_line",
    "show_stop_time",
    "autoscroll",
    "error_message_style",
    "dialog_style",
    "dialog_side",
    "footer_show_seat",
    "footer_show_thinking",
    "footer_show_bg",
    "footer_show_subagents",
    "footer_show_convo",
    "footer_show_context",
    "footer_show_context_pct",
    "footer_show_tps",
    "footer_show_cache",
    "footer_task_manager",
    "footer_show_key_hints",
    "footer_order",
)
BOOLEANS = tuple(name for name in FIELDS if name not in (
    "error_message_style", "dialog_style", "dialog_side", "footer_order"))
# The Rust frontend has no machine-meter surface, so its switch is shown but not
# offered for editing there (it remains a shared device setting for other hosts).
READ_ONLY = ("footer_task_manager",)
ENUMS = {
    "error_message_style": ("plain", "detail"),
    "dialog_style": ("modal", "sidebar"),
    "dialog_side": ("right", "left"),
}
CHOICES = {
    **{name: (False, True) for name in BOOLEANS if name not in READ_ONLY},
    **ENUMS,
}
ORDER = settings.FOOTER_ORDER_DEFAULT


@dataclass
class Review:
    token: str
    instance: str
    revision: str
    field: str
    value: object
    apply_id: str | None = None
    after: str | None = None


def _value(name, value):
    if name in BOOLEANS:
        if type(value) is bool:
            return value
    elif name in ENUMS:
        if type(value) is str and value in ENUMS[name]:
            return value
    elif (
        name == "footer_order"
        and type(value) is list
        and len(value) == len(ORDER)
        and all(type(item) is str for item in value)
        and sorted(value) == sorted(ORDER)
    ):
        return list(value)
    raise ValueError(
        "A configured presentation preference exceeds the supported display format"
    )


def _checked(field, value):
    if field == "footer_order":
        return _value(field, value)
    if field not in CHOICES or not any(
        type(value) is type(item) and value == item for item in CHOICES[field]
    ):
        raise ValueError("Select a supported presentation preference")
    return value


def _snapshot(app, binding):
    context = backend_catalog._context(app, binding["session_id"])
    service = settings_runtime.service_for(app)
    before = service.snapshot(context.session_id, read_only=True, max_bytes=1024 * 1024)
    paths, _, revisions = native_settings.owned_documents(
        service, context.session_id, app._agent_session
    )
    if (
        revisions != before.revisions
        or paths["global"] != app._agent_session.store.data_root / "settings.json"
    ):
        raise ValueError(
            "Presentation preferences changed while inspecting their device destination"
        )
    values = {}
    for key in FIELDS:
        spec = SETTING_SPECS[key]
        if spec.scope.value != "device" or spec.apply_timing != "immediate":
            raise ValueError("Presentation preference ownership changed")
        saved = _value(key, getattr(before.saved, key))
        effective = _value(key, getattr(app.settings, key))
        overridden = bool(
            settings.source_of(key)
            or key in (getattr(app, "_launch_overrides", None) or {})
        )
        values[key] = {
            "saved": saved,
            "effective": effective,
            "overridden": overridden,
            "editable": (key in CHOICES or key == "footer_order")
            and not overridden
            and saved == effective,
        }
    result = {
        "kind": "presentation_preferences",
        "version": 1,
        "scope": "device",
        **binding,
        "fields": values,
    }
    result["revision"] = hashlib.sha256(
        backend_catalog._encoded([result, revisions], 32768)
    ).hexdigest()
    return result, context, service, before, paths["global"]


def _quiet(app):
    backend_transition._quiet(app, require_unowned_server=False)


def _receipt(binding, review, kind):
    return {
        "kind": kind,
        "version": 1,
        "scope": "device",
        **binding,
        "token": review.token,
        "field": review.field,
        "value": review.value,
        "before": review.revision,
        "after": review.after,
        "apply_id": review.apply_id,
    }


def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    arguments = {
        "preferences": (),
        "review": ("revision", "field", "value"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    bridge, binding = native_context.read_binding(app, cmd, PREFIX + op, *arguments[op])
    if op == "preferences":
        app._native_presentation_review = None
        return _snapshot(app, binding)[0]
    if op == "review":
        app._native_presentation_review = None
        _quiet(app)
        key, value = cmd["field"], cmd["value"]
        if not isinstance(key, str) or key not in FIELDS:
            raise ValueError("Unknown presentation preference")
        _checked(key, value)
        current = _snapshot(app, binding)[0]
        row = current["fields"][key]
        if (
            cmd["revision"] != current["revision"]
            or not row["editable"]
            or row["effective"] == value
        ):
            raise ValueError(
                "Refresh presentation preferences and select an editable change"
            )
        review = Review(
            secrets.token_hex(32), bridge.instance, current["revision"], key, value
        )
        app._native_presentation_review = review
        return _receipt(binding, review, "presentation_review")

    review = getattr(app, "_native_presentation_review", None)
    if (
        not isinstance(review, Review)
        or review.token != cmd["token"]
        or review.instance != bridge.instance
    ):
        raise ValueError("Review the current presentation preference again")
    if op == "apply":
        if review.apply_id is not None:
            raise ValueError("Presentation review was already consumed")
        app._native_presentation_review = None

        def reader(owner, _cmd):
            bridge.check()
            _quiet(owner)
            current, context, service, before, destination = _snapshot(owner, binding)
            field = current["fields"][review.field]
            if current["revision"] != review.revision or not field["editable"]:
                raise ValueError("Presentation preferences changed after review")
            return (
                {
                    "field": review.field,
                    "value": review.value,
                    "scope": "device",
                    "destination": str(destination),
                    "effective_before": field["effective"],
                },
                context,
                service,
                before,
            )

        def result(_owner, _cmd, status, **details):
            if status == "saved":
                return None
            if details.get("close_required") or details.get("mutated"):
                return "Presentation settings publication is uncertain. Close and reopen to inspect it."
            return "Presentation preference was refused. Refresh to inspect it."

        error = native_settings.publish_reviewed(
            app, cmd, reader, result, _checked, strict=True
        )
        if error:
            raise ValueError(error)
        after = _snapshot(app, binding)[0]
        field = after["fields"][review.field]
        if (
            after["revision"] == review.revision
            or field["effective"] != review.value
            or field["saved"] != review.value
        ):
            raise ValueError(
                "Presentation preference did not match its durable and effective readback"
            )
        review.after, review.apply_id = after["revision"], cmd["id"]
        app._native_presentation_review = review
        return _receipt(binding, review, "presentation_applied")
    if review.apply_id is None or review.apply_id != cmd["apply_id"]:
        raise ValueError(
            "Presentation preference readback belongs to another application"
        )
    app._native_presentation_review = None
    current = _snapshot(app, binding)[0]
    if review.after != current["revision"]:
        raise ValueError(
            "Presentation preferences changed after saving. Refresh to inspect them."
        )
    return {**_receipt(binding, review, "presentation_saved"), "preferences": current}
