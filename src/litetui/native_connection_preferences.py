"""Reviewed connection and local-router preferences. Saving never connects.

Every field is registered in ``settings_scope.SETTING_SPECS``; this module only
reads that registry for scope and timing, then saves through the canonical
``native_settings.publish_reviewed`` path (``SettingsService`` scoped revision
checks, then ``settings_runtime.apply_saved_result``). That path leaves a
reconnect/restart/reload field *pending*: it never reconnects, loads a model,
probes a host or starts a process. API key VALUES are neither read nor shown;
only the environment variable NAME of the custom server is editable.

``default_model`` is deliberately absent: in an owned agent session it is the
agent's authority (``AgentSession.update_execution``), which the backend
switch (F9) and model selection (F3) own together with ``backend``.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass
from urllib.parse import urlsplit

from litetui import (
    backend_catalog,
    backend_transition,
    native_context,
    native_settings,
    settings,
    settings_runtime,
)
from litetui.settings_scope import SETTING_SPECS

PREFIX = "gui.connection.native_"
MAX_INT = 2147483647
MAX_TEXT = 2048
HIDDEN = "(unsupported value hidden)"


@dataclass(frozen=True)
class Spec:
    kind: str
    scope: str
    timing: str
    minimum: int | None = None
    maximum: int | None = None


# kind -> editor vocabulary. Scope and timing are asserted against the registry.
SPECS = {
    "lm_host": Spec("url", "conversation", "reconnect"),
    "pin_default_model": Spec("bool", "conversation", "reconnect"),
    "default_context_length": Spec("optional_int", "conversation", "reload", 1, MAX_INT),
    "custom_base_url": Spec("url_or_blank", "conversation", "reconnect"),
    "custom_api_key_env": Spec("env_name", "conversation", "reconnect"),
    "custom_context_length": Spec("int", "conversation", "reconnect", 0, MAX_INT),
    "claude_executable": Spec("path", "device", "reconnect"),
    "llama_host": Spec("url", "conversation", "reconnect"),
    "llama_executable": Spec("path", "conversation", "restart"),
    "llama_attach_hosts": Spec("url_list", "conversation", "restart"),
    "llama_models_max": Spec("int", "conversation", "restart", 1, MAX_INT),
    "lms_load_timeout_s": Spec("int", "conversation", "next_turn", 30, MAX_INT),
}
FIELDS = tuple(SPECS)
_ENV_UPPER = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_ENV_LOWER = re.compile(r"[a-z_][a-z0-9_]{0,63}")
_KEY_PREFIXES = ("sk_", "gsk_", "ghp_", "github_pat_", "hf_", "nvapi", "xox", "aiza", "glpat_")


@dataclass
class Review:
    token: str
    instance: str
    revision: str
    field: str
    value: object
    apply_id: str | None = None
    after: str | None = None


def _secret_names():
    from litetui.sidecar_settings import SECRET_FIELDS

    return sorted(SECRET_FIELDS)


def _printable(text):
    return all(ch.isprintable() for ch in text)


def _text(value, limit=MAX_TEXT):
    if (
        type(value) is not str
        or len(value.encode("utf-8")) > limit
        or not _printable(value)
        or value != value.strip()
    ):
        raise ValueError("Enter a single printable line without surrounding spaces")
    return value


def _url(value):
    _text(value)
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises for an invalid port
    except ValueError:
        raise ValueError("Enter an http(s) server URL") from None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Enter an http(s) server URL")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("A server URL must not contain embedded credentials")
    if parts.query or parts.fragment or "?" in value or "#" in value:
        raise ValueError("A server URL must not contain a query or fragment")
    return value


def _env_name(value):
    _text(value, 64)
    if value == "":
        return value
    if not (_ENV_UPPER.fullmatch(value) or _ENV_LOWER.fullmatch(value)) or value.lower().startswith(
        _KEY_PREFIXES
    ):
        raise ValueError(
            "Enter the environment variable NAME (for example MY_SERVER_KEY), never the key itself"
        )
    return value


def _int(value, spec, *, optional=False):
    if value is None and optional:
        return None
    if type(value) is not int or not spec.minimum <= value <= spec.maximum:
        raise ValueError(f"Enter an integer from {spec.minimum} through {spec.maximum}")
    return value


def _normalize(key, value):
    """The exact stored shape of a new value, with no I/O."""
    spec = SPECS[key]
    kind = spec.kind
    if kind == "bool":
        if type(value) is not bool:
            raise ValueError("Choose on or off")
        return value
    if kind in ("int", "optional_int"):
        return _int(value, spec, optional=kind == "optional_int")
    if kind == "url":
        return _url(value)
    if kind == "url_or_blank":
        return value if value == "" else _url(value)
    if kind == "url_list":
        if type(value) is not list or len(value) > 16:
            raise ValueError("Enter up to 16 http(s) server URLs")
        urls = [_url(item) for item in value]
        if len(set(urls)) != len(urls):
            raise ValueError("Server URLs must be distinct")
        return urls
    if kind == "env_name":
        return _env_name(value)
    return _text(value, 1024) if value != "" else value


def _shape(key, value):
    """Lenient type check for publish_reviewed's read of the previous effective value."""
    kind = SPECS[key].kind
    ok = (
        type(value) is bool
        if kind == "bool"
        else type(value) is int
        if kind == "int"
        else value is None or type(value) is int
        if kind == "optional_int"
        else type(value) is list and all(type(item) is str for item in value)
        if kind == "url_list"
        else type(value) is str
    )
    if not ok:
        raise ValueError("A connection preference has an unsupported stored shape")
    return value


def _redacted(value):
    """(display text, exact) for a URL: no userinfo, query or fragment is ever displayed."""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018
    except ValueError:
        return HIDDEN, False
    exact = True
    netloc = parts.netloc
    if "@" in netloc:
        netloc, exact = "***@" + netloc.rsplit("@", 1)[1], False
    if parts.query or parts.fragment or "?" in value or "#" in value:
        exact = False
    return parts._replace(netloc=netloc, query="", fragment="").geturl(), exact


def _display(key, raw):
    """A display-safe wire value and whether it is exactly the stored value."""
    spec = SPECS[key]
    kind = spec.kind

    def text(value, limit=MAX_TEXT):
        if (
            type(value) is not str
            or len(value.encode("utf-8")) > limit
            or not _printable(value)
        ):
            return HIDDEN, False
        return value, True

    if kind == "bool":
        return (raw, True) if type(raw) is bool else (None, False)
    if kind in ("int", "optional_int"):
        if raw is None and kind == "optional_int":
            return None, True
        ok = type(raw) is int and -MAX_INT <= raw <= MAX_INT
        return (raw, True) if ok else (None, False)
    if kind in ("url", "url_or_blank"):
        shown, exact = text(raw)
        if not exact or shown == "":
            return shown, exact
        shown, exact = _redacted(shown)
        return shown, exact
    if kind == "url_list":
        if type(raw) is not list or len(raw) > 16:
            return [], False
        rows = [_redacted(item) if text(item)[1] else (HIDDEN, False) for item in raw]
        return [row[0] for row in rows], all(row[1] for row in rows)
    return text(raw, 1024 if kind == "path" else 64)


def _effect(key):
    spec = SPECS[key]
    where = (
        "this conversation's settings"
        if spec.scope == "conversation"
        else "the shared device settings (every conversation)"
    )
    when = {
        "reconnect": "Takes effect at the next reconnect or relaunch.",
        "restart": "Takes effect the next time LiteTUI starts or restarts that local server.",
        "reload": "Takes effect the next time the model is loaded or settings are reloaded. "
        "The Python Settings dialog would reload the model now; this never does.",
        "next_turn": "Takes effect on the next turn.",
        "immediate": "Takes effect now in this session.",
    }[spec.timing]
    extra = {
        "claude_executable": " The executable is not run or version-checked until the next connection.",
        "custom_api_key_env": " Only the variable name is saved; the key is never read here.",
        "llama_executable": " The path is checked as a file but never run.",
    }.get(key, "")
    return (
        f"Saves to {where}. {when} Saving never reconnects, contacts a server, "
        f"loads a model or starts a process.{extra}"
    )


def _locked(app, key, saved_exact, effective_exact):
    env = settings.source_of(key)
    if env:
        return f"Environment variable {env} owns this value"
    if key in (getattr(app, "_launch_overrides", None) or {}) or key in (
        getattr(app, "_invocation_saved_values", None) or {}
    ):
        return "A launch option owns this value for this process"
    if not (saved_exact and effective_exact):
        return "The stored value cannot be shown safely; edit it outside the native TUI"
    return None


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
        raise ValueError("Connection preferences changed while inspecting their destination")
    fields = {}
    raw = {}
    for key in FIELDS:
        spec, registered = SPECS[key], SETTING_SPECS[key]
        if registered.scope.value != spec.scope or registered.apply_timing != spec.timing:
            raise ValueError("Connection preference ownership changed")
        saved, effective = getattr(before.saved, key), getattr(app.settings, key)
        raw[key] = [saved, effective]
        saved_shown, saved_exact = _display(key, saved)
        effective_shown, effective_exact = _display(key, effective)
        reason = _locked(app, key, saved_exact, effective_exact)
        fields[key] = {
            "scope": spec.scope,
            "timing": spec.timing,
            "kind": spec.kind,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "saved": saved_shown,
            "effective": effective_shown,
            "editable": reason is None
            and (spec.timing not in ("next_turn", "immediate") or saved == effective),
            "reason": reason
            or (
                None
                if spec.timing not in ("next_turn", "immediate") or saved == effective
                else "Saved and active values differ; refresh before editing"
            ),
            "effect": _effect(key),
        }
    result = {
        "kind": "connection_preferences",
        "version": 1,
        "scope": "mixed",
        **binding,
        "fields": fields,
        "key_settings": {"names": _secret_names(), "readable": False},
    }
    # Hidden raw values still invalidate a stale review, without being published.
    digest = hashlib.sha256(backend_catalog._encoded(raw, 1024 * 1024)).hexdigest()
    result["revision"] = hashlib.sha256(
        backend_catalog._encoded([result, revisions, digest], 65536)
    ).hexdigest()
    destinations = {"conversation": paths["conversation"], "device": paths["global"]}
    return result, context, service, before, destinations, raw


def _quiet(app):
    backend_transition._quiet(app, require_unowned_server=False)


def _receipt(binding, review, kind):
    spec = SPECS[review.field]
    return {
        "kind": kind,
        "version": 1,
        "scope": spec.scope,
        **binding,
        "token": review.token,
        "field": review.field,
        "value": review.value,
        "timing": spec.timing,
        "effect": _effect(review.field),
        "before": review.revision,
        "after": review.after,
        "apply_id": review.apply_id,
    }


def _same_value(left, right):
    return type(left) is type(right) and left == right


def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    arguments = {
        "preferences": (),
        "review": ("revision", "field", "value"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    if op not in arguments:
        raise ValueError("Unknown connection preference operation")
    bridge, binding = native_context.read_binding(app, cmd, PREFIX + op, *arguments[op])
    if op == "preferences":
        app._native_connection_review = None
        return _snapshot(app, binding)[0]
    if op == "review":
        app._native_connection_review = None
        _quiet(app)
        key, value = cmd["field"], cmd["value"]
        if not isinstance(key, str) or key not in SPECS:
            raise ValueError("Unknown connection preference")
        value = _normalize(key, value)
        current, _, _, before, _, _ = _snapshot(app, binding)
        row = current["fields"][key]
        if cmd["revision"] != current["revision"] or not row["editable"]:
            raise ValueError(row["reason"] or "Refresh connection preferences and select an editable change")
        if _same_value(getattr(before.saved, key), value):
            raise ValueError("The saved preference already has this value")
        from litetui.gui_rpc import _validate
        from litetui.llm_backend import BackendError

        # The registered source validator (type, environment ownership, executable file check).
        try:
            _validate(app, {key: value})
        except BackendError as exc:
            raise ValueError(str(exc)[:512]) from None
        if key == "custom_base_url" and value:
            from litetui.custom_backend import api_base

            api_base(value)
        review = Review(
            secrets.token_hex(32), bridge.instance, current["revision"], key, value
        )
        app._native_connection_review = review
        return _receipt(binding, review, "connection_review")

    review = getattr(app, "_native_connection_review", None)
    if (
        not isinstance(review, Review)
        or review.token != cmd["token"]
        or review.instance != bridge.instance
    ):
        raise ValueError("Review the current connection preference again")
    spec = SPECS[review.field]
    if op == "apply":
        if review.apply_id is not None:
            raise ValueError("Connection review was already consumed")
        app._native_connection_review = None
        effective_before = []

        def reader(owner, _cmd):
            bridge.check()
            _quiet(owner)
            current, context, service, saved, destinations, raw = _snapshot(owner, binding)
            if current["revision"] != review.revision or not current["fields"][review.field]["editable"]:
                raise ValueError("Connection preferences changed after review")
            effective_before[:] = [raw[review.field][1]]
            return (
                {
                    "field": review.field,
                    "value": review.value,
                    "scope": spec.scope,
                    "destination": str(destinations[spec.scope]),
                    "effective_before": raw[review.field][1],
                },
                context,
                service,
                saved,
            )

        def result(_owner, _cmd, status, **details):
            if status == "saved":
                return None
            runtime = details.get("runtime", ())
            if (
                status == "partial"
                and len(runtime) == 1
                and runtime[0].status == "pending"
                and runtime[0].action == spec.timing
            ):
                return None
            if details.get("close_required") or details.get("mutated"):
                return "Connection settings publication is uncertain. Close and reopen to inspect it."
            return "Connection preference was refused. Refresh to inspect it."

        before_rows = _snapshot(app, binding)[0]["fields"]
        error = native_settings.publish_reviewed(
            app, cmd, reader, result, lambda key, value: _shape(review.field, value), strict=True
        )
        if error:
            raise ValueError(error)
        after_state = _snapshot(app, binding)
        after, raw = after_state[0], after_state[5]
        saved, effective = raw[review.field]
        applied_now = spec.timing in ("immediate", "next_turn")
        expected_effective = review.value if applied_now else effective_before[0]
        if (
            after["revision"] == review.revision
            or not _same_value(saved, review.value)
            or not _same_value(effective, expected_effective)
            or any(
                after["fields"][name] != row
                for name, row in before_rows.items()
                if name != review.field
            )
        ):
            raise ValueError(
                "Connection preference did not match its durable and effective readback"
            )
        review.after, review.apply_id = after["revision"], cmd["id"]
        app._native_connection_review = review
        return _receipt(binding, review, "connection_applied")

    if review.apply_id is None or review.apply_id != cmd["apply_id"]:
        raise ValueError("Connection preference readback belongs to another application")
    app._native_connection_review = None
    current = _snapshot(app, binding)[0]
    if review.after != current["revision"]:
        raise ValueError(
            "Connection preferences changed after saving. Refresh to inspect them."
        )
    return {**_receipt(binding, review, "connection_saved"), "preferences": current}
