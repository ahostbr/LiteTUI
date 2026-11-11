"""Device voice preferences under an active owned Work instance. No audio or discovery."""

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

PREFIX = "gui.voice.native_"
FIELDS = (
    "stt_model",
    "stt_mic",
    "stt_hotkey",
    "tts_enabled",
    "tts_litesuite_first",
    "tts_engine",
    "tts_voice",
    "tts_edge_voice",
    "tts_timeout",
)
MAX_TEXT = 512
MAX_HOTKEY = 64
CHOICES = {
    "stt_model": ("tiny.en", "base.en", "small.en"),
    "tts_engine": ("pyttsx3", "edge"),
    "tts_litesuite_first": (False, True),
    "tts_enabled": (False, True),
}


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
    if name in ("tts_enabled", "tts_litesuite_first"):
        if type(value) is bool:
            return value
    elif name == "tts_timeout":
        if type(value) is int and 1 <= value <= 2147483647:
            return value
    elif (
        isinstance(value, str)
        and len(value.encode("utf-8")) <= 512
        and all(ch.isprintable() for ch in value)
    ):
        return value
    raise ValueError(
        "A configured voice preference exceeds the supported display format"
    )


def _hotkey(value):
    # Match presentation.Hotkey's pure parser. Empty restores the default chord.
    spelling = value.strip().translate(
        str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")
    )
    if not spelling:
        return True
    parts = spelling.split("+")
    if spelling.endswith("++"):
        parts, key = parts[:-2], "+"
    else:
        key = parts.pop()
    if any(part not in ("ctrl", "control", "shift", "alt", "meta") for part in parts):
        return False
    named = {
        "space",
        "enter",
        "tab",
        "escape",
        "backspace",
        "delete",
        "insert",
        "home",
        "end",
        "pageup",
        "pagedown",
        "up",
        "down",
        "left",
        "right",
    }
    if key in named or len(key) == 1 and key.isprintable():
        return True
    return (
        len(key) > 1
        and key[0] == "f"
        and key[1:].isascii()
        and key[1:].isdigit()
        and 1 <= int(key[1:]) <= 24
    )


def _editor(field):
    return {
        "kind": "choice"
        if field in CHOICES
        else "integer"
        if field == "tts_timeout"
        else "hotkey"
        if field == "stt_hotkey"
        else "text",
        "choices": list(CHOICES.get(field, ())),
        "max_bytes": MAX_HOTKEY if field == "stt_hotkey" else MAX_TEXT,
        "minimum": 1 if field == "tts_timeout" else None,
        "maximum": 2147483647 if field == "tts_timeout" else None,
    }


def _checked(field, value):
    if field not in FIELDS:
        raise ValueError("Select a supported voice preference")
    if field in CHOICES and not any(
        type(value) is type(item) and value == item for item in CHOICES[field]
    ):
        raise ValueError("Select a supported voice preference")
    _value(field, value)
    if field == "stt_hotkey" and (
        len(value.encode("utf-8")) > MAX_HOTKEY or not _hotkey(value)
    ):
        raise ValueError("Enter a supported shortcut, such as ctrl+space, alt+r or f9")
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
            "Voice preferences changed while inspecting their device destination"
        )
    values = {}
    for key in FIELDS:
        spec = SETTING_SPECS[key]
        if spec.scope.value != "device" or spec.apply_timing != "immediate":
            raise ValueError("Voice preference ownership changed")
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
            "editor": _editor(key),
            "editable": not overridden
            and saved == effective
            and (key not in CHOICES or effective in CHOICES[key]),
        }
    result = {
        "kind": "voice_preferences",
        "version": 1,
        "scope": "device",
        **binding,
        "listen_registered": app.plugins.dispatch_for("listen") is not None,
        "fields": values,
    }
    result["revision"] = hashlib.sha256(
        backend_catalog._encoded([result, revisions], 32768)
    ).hexdigest()
    return result, context, service, before, paths["global"]


def _quiet(app):
    backend_transition._quiet(app, require_unowned_server=False)
    if getattr(app, "_mic_proc", None) is not None:
        raise ValueError("Finish current dictation before changing voice preferences")


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
        app._native_voice_review = None
        return _snapshot(app, binding)[0]
    if op == "review":
        app._native_voice_review = None
        _quiet(app)
        key, value = cmd["field"], cmd["value"]
        if not isinstance(key, str):
            raise ValueError("Unknown voice preference")
        _checked(key, value)
        current = _snapshot(app, binding)[0]
        row = current["fields"][key]
        if (
            cmd["revision"] != current["revision"]
            or not row["editable"]
            or row["effective"] == value
        ):
            raise ValueError("Refresh voice preferences and select an editable change")
        review = Review(
            secrets.token_hex(32), bridge.instance, current["revision"], key, value
        )
        app._native_voice_review = review
        return _receipt(binding, review, "voice_review")

    review = getattr(app, "_native_voice_review", None)
    if (
        not isinstance(review, Review)
        or review.token != cmd["token"]
        or review.instance != bridge.instance
    ):
        raise ValueError("Review the current voice preference again")
    if op == "apply":
        if review.apply_id is not None:
            raise ValueError("Voice review was already consumed")
        app._native_voice_review = None

        def reader(owner, _cmd):
            bridge.check()
            _quiet(owner)
            current, context, service, before, destination = _snapshot(owner, binding)
            field = current["fields"][review.field]
            if current["revision"] != review.revision or not field["editable"]:
                raise ValueError("Voice preferences changed after review")
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
                return "Voice settings publication is uncertain. Close and reopen to inspect it."
            return "Voice preference was refused. Refresh to inspect it."

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
                "Voice preference did not match its durable and effective readback"
            )
        review.after, review.apply_id = after["revision"], cmd["id"]
        app._native_voice_review = review
        return _receipt(binding, review, "voice_applied")
    if review.apply_id is None or review.apply_id != cmd["apply_id"]:
        raise ValueError("Voice preference readback belongs to another application")
    app._native_voice_review = None
    current = _snapshot(app, binding)[0]
    if review.after != current["revision"]:
        raise ValueError(
            "Voice preferences changed after saving. Refresh to inspect them."
        )
    return {**_receipt(binding, review, "voice_saved"), "preferences": current}
