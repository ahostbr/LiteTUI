"""Toggle one existing scoped hook without exporting commands or normalizing unrelated document bytes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import gui_rpc, lifecycle_hooks, native_context
from litetui.agent_store import _unlinked

SCOPES = ("global", "project")
PREFIX = "gui.hooks.native_"
MAX_HOOKS = 64
SAFE = re.compile(r"[A-Za-z0-9_.-]{1,96}")
DECODER = json.JSONDecoder()


def digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def document(raw, scope):
    if raw is None:
        return ()
    try:
        obj = work.strict_json(raw.decode("utf-8-sig"))
        if type(obj.get("version")) is not int:
            raise ValueError
        rows = lifecycle_hooks.HookConfig.parse_document(obj, scope)
        if len(rows) > MAX_HOOKS or any(not SAFE.fullmatch(h.id) for h in rows):
            raise ValueError
        return rows
    except (AttributeError, TypeError, ValueError, RecursionError) as exc:
        # Parser diagnostics can include user-supplied field names and raw snippets.
        raise ValueError(f"Unsupported or malformed {scope} hook document; no edit is available.") from exc


@dataclass
class Capture:
    paths: tuple
    raw: tuple
    rows: tuple
    effective: lifecycle_hooks.Snapshot
    revision: str


def capture(app, replacement=None):
    cfg = app.hook_config
    if getattr(app, "_conversation_transition_hook_snapshot", None) is not None:
        raise ValueError("Finish the conversation transition before changing configured hooks.")
    if not isinstance(cfg, lifecycle_hooks.HookConfig):
        raise TypeError("The canonical lifecycle hook configuration is unavailable.")
    paths = tuple(_unlinked(cfg.path(scope)).absolute() for scope in SCOPES)
    if paths != lifecycle_hooks.config_paths(app._hook_workspace) or paths[0] == paths[1]:
        raise ValueError("Hook scope paths changed or overlap.")
    raw = []
    for scope, path in zip(SCOPES, paths, strict=True):
        status, data = native_context._regular_file(path)
        if status == "oversize":
            raise ValueError("Hook document exceeds the 256 KiB inspection limit.")
        raw.append(replacement[1] if replacement is not None and replacement[0] == scope else data)
    rows = tuple(document(data, scope) for data, scope in zip(raw, SCOPES, strict=True))
    state = cfg.snapshot(read_scope=lambda scope: rows[SCOPES.index(scope)])
    if state.error:
        raise ValueError("Effective hook configuration could not be resolved.")
    binding = {"paths": [str(p) for p in paths], "files": [digest(data) for data in raw], "off": state.disabled}
    return Capture(paths, tuple(raw), rows, state, digest(work.encoded(binding)))


def details(app, hook, state):
    executable = hook.executable.replace("\\", "/").rsplit("/", 1)[-1]
    effective = next((h for h in state.hooks if h.id == hook.id), None)
    if state.disabled:
        effect = "off"
    elif effective is None:
        raise ValueError("Effective hook row is unavailable.")
    elif effective.scope != hook.scope:
        effect = "shadowed"
    else:
        effect = "enabled" if effective.enabled else "disabled"
    return {"id": hook.id, "scope": hook.scope, "enabled": hook.enabled, "effect": effect, "mode": hook.mode,
            "effective_scope": effective.scope if effective else None, "effective_enabled": effective.enabled if effective else None,
            "effective_command": effective.approval_name(app._hook_workspace).rsplit(":", 1)[-1] if effective else None,
            "executable": executable if SAFE.fullmatch(executable) else "(name withheld)",
            "command_id": hook.approval_name(app._hook_workspace).rsplit(":", 1)[-1],
            "events": list(dict.fromkeys(hook.events)), "sources": list(dict.fromkeys(hook.sources)),
            "argument_count": len(hook.argv), "env_count": len(hook.env), "custom_cwd": hook.cwd is not None,
            "filter_count": len(hook.tools), "timeout": hook.timeout}


def ws(text, i):
    while i < len(text) and text[i] in " \r\t\n":
        i += 1
    return i


def members(text, start):
    """Yield already validated object members and original value spans; strings may contain JSON punctuation."""
    cursor = ws(text, start + 1)
    while text[cursor] != "}":
        key, end = DECODER.raw_decode(text, cursor)
        begin = ws(text, ws(text, end) + 1)
        _, end = DECODER.raw_decode(text, begin)
        yield key, begin, end
        cursor = ws(text, end)
        if text[cursor] == ",":
            cursor = ws(text, cursor + 1)


def toggle_bytes(raw, selected, enabled):
    # Retain a BOM, escaped keys, omitted defaults, whitespace, unrelated secrets and trailing newline exactly.
    text = raw.decode("utf-8")
    start = ws(text, 1 if text.startswith("\ufeff") else 0)
    array = next(begin for key, begin, _ in members(text, start) if key == "hooks")
    cursor = ws(text, array + 1)
    for _ in range(selected):
        _, end = DECODER.raw_decode(text, cursor)
        cursor = ws(text, ws(text, end) + 1)
    value = "true" if enabled else "false"
    for key, begin, end in members(text, cursor):
        if key == "enabled":
            return (text[:begin] + value + text[end:]).encode("utf-8")
    return (text[:cursor + 1] + '"enabled":' + value + "," + text[cursor + 1:]).encode("utf-8")


@dataclass
class Review:
    instance: str
    config: object
    before: Capture
    scope: str
    hook_id: str
    after: bytes
    wire: dict
    apply_id: str | None = None


def row(state, scope, identity):
    for i, hook in enumerate(state.rows[SCOPES.index(scope)]):
        if hook.id == identity:
            return i, hook
    raise ValueError("The selected hook is no longer present.")


def _guards(app, bridge, review):
    bridge.check()
    app._validate_owned_identity()
    gui_rpc._idle(app)
    if bridge.closing or app.hook_config is not review.config or capture(app).revision != review.before.revision:
        raise ValueError("Hook scope, source bytes or effective configuration changed; refresh before applying.")


def publish(app, bridge, pending):
    scope = SCOPES.index(pending.scope)
    path = pending.before.paths[scope]
    _guards(app, bridge, pending)
    # Both scopes determine the reviewed effective hook. Acquire the canonical
    # sibling locks in a fixed order so other HookConfig writers cannot change
    # either document between final validation and publication.
    with ExitStack() as locks:
        for scope_path in pending.before.paths:
            lock_path = _unlinked(scope_path.with_suffix(scope_path.suffix + ".lock"))
            if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
                raise ValueError("Linked or special hook locks cannot be used.")
            try:
                locks.enter_context(lifecycle_hooks._file_lock(scope_path, wait=False))
            except OSError as exc:
                raise ValueError("A hook scope is locked or unavailable; refresh before trying again.") from exc
        _guards(app, bridge, pending)
        fd, temporary = tempfile.mkstemp(prefix="hooks-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(pending.after)
                stream.flush()
                os.fsync(stream.fileno())
            if native_context._regular_file(Path(temporary)) != ("content", pending.after):
                raise ValueError("Staged hook bytes changed before publication.")
            _guards(app, bridge, pending)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)


def dispatch(app, cmd):
    op = cmd["type"]
    verb = op.removeprefix(PREFIX)
    fields = {"list": (), "review": ("revision", "hook_id"), "apply": ("token",), "readback": ("token", "apply_id")}
    bridge, binding = native_context.read_binding(app, cmd, op, "scope", *fields[verb])
    scope = cmd["scope"]
    if type(scope) is not str or scope not in SCOPES:
        raise ValueError("Choose a supported hook scope.")
    gui_rpc._idle(app)
    idx = SCOPES.index(scope)
    base = {"version": 1, **binding, "scope": scope}
    if verb in ("list", "review"):
        bridge.hook_test = None
        bridge.hook_toggle = None
        current = capture(app)
        if verb == "list":
            reply = {**base, "kind": "hook_list", "revision": current.revision,
                     "rows": [details(app, hook, current.effective) for hook in current.rows[idx]]}
            if len(work.encoded(reply)) > work.LIMITS["reply_bytes"] - 512:
                raise ValueError("Hook list exceeds the native inspection limit.")
            return reply
        if cmd["revision"] != current.revision:
            raise ValueError("Hook configuration changed since selection; refresh the list.")
        index, hook = row(current, scope, cmd["hook_id"])
        after = toggle_bytes(current.raw[idx], index, not hook.enabled)
        if len(after) > native_context.MAX_DOCUMENT:
            raise ValueError("Edited hook document exceeds the 256 KiB limit.")
        expected = capture(app, (scope, after))
        if expected.paths != current.paths or expected.raw[1 - idx] != current.raw[1 - idx] or expected.effective.disabled != current.effective.disabled:
            raise ValueError("Hook scope changed while preparing review.")
        after_hook = row(expected, scope, hook.id)[1]
        wire = {**base, "kind": "hook_review", "revision": current.revision, "token": secrets.token_hex(32),
                "before": details(app, hook, current.effective), "after": details(app, after_hook, expected.effective),
                "before_sha256": digest(current.raw[idx]), "after_sha256": digest(after), "after_revision": expected.revision}
        bridge.hook_toggle = Review(bridge.instance, app.hook_config, current, scope, hook.id, after, wire)
        return wire
    pending = getattr(bridge, "hook_toggle", None)
    if (pending is None or pending.instance != bridge.instance or pending.scope != scope
            or cmd["token"] != pending.wire["token"] or pending.config is not app.hook_config):
        raise ValueError("Hook review belongs to another scope, source instance or selection.")
    if verb == "apply":
        if pending.apply_id is None:
            publish(app, bridge, pending)
            pending.apply_id = cmd["id"]
        elif pending.apply_id != cmd["id"]:
            raise ValueError("This hook toggle was already applied.")
        return {**base, "kind": "hook_apply", "token": pending.wire["token"], "apply_id": pending.apply_id}
    if pending.apply_id is None or cmd["apply_id"] != pending.apply_id:
        raise ValueError("Hook readback belongs to another application.")
    current = capture(app)
    _, hook = row(current, scope, pending.hook_id)
    # Independently resolve the hook the host would select from fresh bounded scope reads.
    def effective_scope(name):
        status, raw = native_context._regular_file(current.paths[SCOPES.index(name)])
        if status == "oversize" or raw != current.raw[SCOPES.index(name)]:
            raise ValueError("Hook scope changed during effective readback.")
        return document(raw, name)

    effective = app.hook_config.snapshot(read_scope=effective_scope)
    if effective.error or effective != current.effective:
        raise ValueError("Effective hook changed during independent readback.")
    actual = details(app, hook, effective)
    if (current.paths != pending.before.paths or current.raw[idx] != pending.after
            or current.raw[1 - idx] != pending.before.raw[1 - idx] or actual != pending.wire["after"]
            or current.revision != pending.wire["after_revision"]):
        raise ValueError("Hook configuration changed after publication; saved and effective outcome is unverified.")
    return {**base, "kind": "hook_saved", "token": pending.wire["token"], "apply_id": pending.apply_id,
            "revision": current.revision, "sha256": digest(current.raw[idx]), "actual": actual}
