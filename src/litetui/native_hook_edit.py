"""Create, edit, delete or repair one scoped lifecycle hook through the canonical validator and writer.

The source owns every decision. The frontend sends a bounded form; ``Hook.parse`` and
``HookConfig.parse_document`` (the ``hooks.validate`` path) validate it, ``HookConfig.atomic_write``
publishes it under the canonical scope locks, and nothing here ever runs a hook. Environment values
and working directories never cross the wire: an edit preserves them from the stored hook.
"""
from __future__ import annotations

import json
import secrets
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import gui_rpc, lifecycle_hooks, native_context, native_hook_toggle
from litetui.agent_store import _unlinked

PREFIX = "gui.hooks.native_edit_"
REPAIR = "gui.hooks.native_repair_"
REPAIR_LIMIT = 65536
SCOPES = native_hook_toggle.SCOPES
ACTIONS = ("create", "update", "delete")
FIELDS = ("id", "events", "executable", "argv", "enabled", "mode", "timeout", "sources", "tools")
FILES = {"global": "global hooks.json", "project": "project .litetui/hooks.json"}
LIMITS = {"hooks": native_hook_toggle.MAX_HOOKS, "executable": 1024, "argv": 64, "arg": 1024,
          "tools": 32, "tool": 128, "choices": 16}


def refuse(reason="The hook exceeds the native editing limits; edit the file directly."):
    return ValueError(reason)


def wire_hook(hook):
    """The editable fields only. Environment values and the working directory stay in the source."""
    data = hook.document()
    row = {name: data[name] for name in FIELDS}
    row["env_count"] = len(hook.env)
    row["custom_cwd"] = hook.cwd is not None
    return row


def bounded_text(value, cap):
    return type(value) is str and len(value.encode()) <= cap


def check_form(form):
    """Bounds only. Meaning (events, gate mode, timeout, duplicates) belongs to the canonical validator."""
    if type(form) is not dict or set(form) != set(FIELDS):
        raise refuse("Hook form fields are malformed.")
    lists = {"events": LIMITS["choices"], "sources": LIMITS["choices"], "argv": LIMITS["argv"], "tools": LIMITS["tools"]}
    caps = {"events": 64, "sources": 64, "argv": LIMITS["arg"], "tools": LIMITS["tool"]}
    for name, count in lists.items():
        value = form[name]
        if type(value) is not list or len(value) > count or not all(bounded_text(v, caps[name]) for v in value):
            raise refuse(f"Hook {name} exceed the native editing limits.")
    if not (bounded_text(form["id"], 96) and native_hook_toggle.SAFE.fullmatch(form["id"])):
        raise refuse("Hook IDs use letters, digits, underscore, dot and hyphen (1 to 96 characters).")
    if not bounded_text(form["executable"], LIMITS["executable"]):
        raise refuse("Hook executable exceeds the native editing limits.")


def check_rows(rows):
    if len(rows) > LIMITS["hooks"]:
        raise refuse("A scope holds at most 64 hooks.")
    for hook in rows:
        data = hook.document()
        try:
            check_form({name: data[name] for name in FIELDS})
        except ValueError as exc:
            raise refuse() from exc
    return rows


def effective(app, state, identity):
    hook = next((h for h in state.hooks if h.id == identity), None)
    if hook is None:
        return None
    return {"scope": hook.scope, "enabled": hook.enabled,
            "command_id": hook.approval_name(app._hook_workspace).rsplit(":", 1)[-1]}


def canonical_bytes(rows):
    """The exact bytes the canonical writer publishes, produced by that writer in a private directory."""
    with tempfile.TemporaryDirectory(prefix="hook-edit-") as directory:
        target = Path(directory) / "hooks.json"
        lifecycle_hooks.HookConfig.atomic_write(target, rows)
        return target.read_bytes()


@dataclass
class Review:
    instance: str
    config: object
    before: native_hook_toggle.Capture
    scope: str
    hook_id: str
    rows: tuple
    after: bytes
    wire: dict
    apply_id: str | None = None


def guards(app, bridge, review):
    bridge.check()
    app._validate_owned_identity()
    gui_rpc._idle(app)
    if (bridge.closing or app.hook_config is not review.config
            or native_hook_toggle.capture(app).revision != review.before.revision):
        raise ValueError("Hook scope, source bytes or effective configuration changed; refresh before applying.")


def publish(app, bridge, pending):
    path = pending.before.paths[SCOPES.index(pending.scope)]
    guards(app, bridge, pending)
    # Both scopes decide the effective hook: hold both canonical sibling locks, in a fixed order.
    with ExitStack() as locks:
        for scope_path in pending.before.paths:
            lock_path = _unlinked(scope_path.with_suffix(scope_path.suffix + ".lock"))
            if lock_path.exists() and (not lock_path.is_file() or lock_path.stat().st_nlink != 1):
                raise ValueError("Linked or special hook locks cannot be used.")
            try:
                locks.enter_context(lifecycle_hooks._file_lock(scope_path, wait=False))
            except OSError as exc:
                raise ValueError("A hook scope is locked or unavailable; refresh before trying again.") from exc
        guards(app, bridge, pending)  # revision compare-and-swap, now that the writer owns the locks
        lifecycle_hooks.HookConfig.atomic_write(path, pending.rows)


def scope_bytes(app, scope):
    """The scope document as stored, through the same bounded, unlinked read the editor uses."""
    path = _unlinked(app.hook_config.path(scope)).absolute()
    status, raw = native_context._regular_file(path)
    if status == "oversize":
        raise ValueError("Hook document exceeds the 256 KiB inspection limit.")
    return raw


def readable(raw, scope):
    try:
        native_hook_toggle.document(raw, scope)
    except ValueError:
        return False
    return True


def diagnose(raw, scope):
    """Why the canonical parser refuses this document; the person's own file, so its field names may show."""
    try:
        lifecycle_hooks.HookConfig.parse_document(json.loads(raw.decode("utf-8-sig")), scope)
    except ValueError as exc:  # HookError, JSONDecodeError and UnicodeDecodeError are all ValueErrors
        return str(exc)[:300]
    return ("Native editing does not support this document: an unusual ID, more than 64 hooks, "
            "or duplicate JSON keys.")


def snapshot(app, scope, base):
    catalog = {"events": list(lifecycle_hooks.EVENTS),
               "gate_events": [e for e in lifecycle_hooks.EVENTS if e in lifecycle_hooks.GATES],
               "sources": list(lifecycle_hooks.SOURCES)}
    raw = scope_bytes(app, scope)
    if raw is not None and not readable(raw, scope):
        if len(raw) > REPAIR_LIMIT:
            raise ValueError("The unreadable hook document exceeds the 64 KiB native repair limit; repair the file directly.")
        return {**base, "kind": "hook_edit_snapshot", "state": "invalid", "revision": native_hook_toggle.digest(raw),
                "hooks_off": False, "file": FILES[scope], "rows": [], **catalog,
                "error": diagnose(raw, scope), "text": raw.decode("utf-8-sig", errors="replace")}
    current = native_hook_toggle.capture(app)
    rows = [wire_hook(h) for h in check_rows(current.rows[SCOPES.index(scope)])]
    reply = {**base, "kind": "hook_edit_snapshot", "state": "ok", "revision": current.revision,
             "hooks_off": current.effective.disabled, "file": FILES[scope], "rows": rows, **catalog,
             "error": None, "text": None}
    if len(work.encoded(reply)) > work.LIMITS["reply_bytes"] - 512:
        raise ValueError("Hook list exceeds the native inspection limit.")
    return reply


def review(app, bridge, scope, cmd, base):
    action, hook_id, form = cmd["action"], cmd["hook_id"], cmd["hook"]
    if action not in ACTIONS:
        raise ValueError("Choose create, edit or delete.")
    idx = SCOPES.index(scope)
    current = native_hook_toggle.capture(app)
    if cmd["revision"] != current.revision:
        raise ValueError("Hook configuration changed since selection; refresh the list.")
    stored = check_rows(current.rows[idx])
    if action == "delete":
        old = next((h for h in stored if h.id == hook_id), None)
        if old is None or form is not None:
            raise ValueError("Select one present hook to delete.")
        identity, new = hook_id, None
        rows = tuple(h for h in stored if h.id != hook_id)
    else:
        check_form(form)
        identity = form["id"]
        old = next((h for h in stored if h.id == identity), None)
        if action == "create" and (hook_id is not None or old is not None):
            raise ValueError("A hook with that ID already exists in this scope.")
        if action == "update" and (old is None or hook_id != identity):
            raise ValueError("The selected hook is no longer present, or its ID changed; create a new hook instead.")
        document = dict(form)
        if old is not None:  # private values stay in the source
            document.update(env=dict(old.env), cwd=old.cwd)
        new = lifecycle_hooks.Hook.parse(document, scope)  # canonical field validator
        rows = tuple(new if h.id == identity else h for h in stored) if old is not None else (*stored, new)
    # The canonical document validator (the hooks.validate path): version, shape and duplicate IDs.
    lifecycle_hooks.HookConfig.parse_document({"version": 1, "hooks": [h.document() for h in rows]}, scope)
    check_rows(rows)
    after = canonical_bytes(rows)
    if len(after) > native_context.MAX_DOCUMENT:
        raise ValueError("Edited hook document exceeds the 256 KiB limit.")
    expected = native_hook_toggle.capture(app, (scope, after))
    if (expected.paths != current.paths or expected.raw[1 - idx] != current.raw[1 - idx]
            or expected.effective.disabled != current.effective.disabled):
        raise ValueError("Hook scope changed while preparing review.")
    wire = {**base, "kind": "hook_edit_review", "revision": current.revision, "token": secrets.token_hex(32),
            "action": action, "hook_id": identity, "file": FILES[scope], "created_file": current.raw[idx] is None,
            "hooks_off": current.effective.disabled,
            "before": wire_hook(old) if old else None, "after": wire_hook(new) if new else None,
            "before_effective": effective(app, current.effective, identity),
            "after_effective": effective(app, expected.effective, identity),
            "others": len(rows) - (new is not None),
            "before_sha256": native_hook_toggle.digest(current.raw[idx]),
            "after_sha256": native_hook_toggle.digest(after), "after_revision": expected.revision}
    bridge.hook_edit = Review(bridge.instance, app.hook_config, current, scope, identity, rows, after, wire)
    return wire


def readback(app, pending):
    idx = SCOPES.index(pending.scope)
    current = native_hook_toggle.capture(app)

    def read_scope(name):
        status, raw = native_context._regular_file(current.paths[SCOPES.index(name)])
        if status == "oversize" or raw != current.raw[SCOPES.index(name)]:
            raise ValueError("Hook scope changed during effective readback.")
        return native_hook_toggle.document(raw, name)

    # Independently resolve what the host would select from fresh bounded scope reads.
    resolved = app.hook_config.snapshot(read_scope=read_scope)
    if resolved.error or resolved != current.effective:
        raise ValueError("Effective hook changed during independent readback.")
    saved = next((h for h in current.rows[idx] if h.id == pending.hook_id), None)
    actual = {"saved": wire_hook(saved) if saved else None,
              "effective": effective(app, resolved, pending.hook_id)}
    wire = pending.wire
    if (current.paths != pending.before.paths or current.raw[idx] != pending.after
            or current.raw[1 - idx] != pending.before.raw[1 - idx] or current.revision != wire["after_revision"]
            or actual["saved"] != wire["after"] or actual["effective"] != wire["after_effective"]):
        raise ValueError("Hook configuration changed after publication; saved and effective outcome is unverified.")
    return {"revision": current.revision, "sha256": native_hook_toggle.digest(current.raw[idx]),
            "hooks_off": resolved.disabled, **actual}


@dataclass
class Repair:
    instance: str
    config: object
    scope: str
    paths: tuple
    raw: tuple
    text: str
    rows: tuple
    after: bytes
    wire: dict
    apply_id: str | None = None


def sealed(app, bridge, pending):
    """Both scope files are still exactly the bytes reviewed, and nothing else has taken over."""
    bridge.check()
    app._validate_owned_identity()
    gui_rpc._idle(app)
    paths = tuple(_unlinked(app.hook_config.path(name)).absolute() for name in SCOPES)
    raw = tuple(scope_bytes(app, name) for name in SCOPES)
    if (bridge.closing or app.hook_config is not pending.config or paths != pending.paths
            or paths != lifecycle_hooks.config_paths(app._hook_workspace) or raw != pending.raw):
        raise ValueError("Hook scope or source bytes changed; reload before repairing.")


def repair_review(app, bridge, scope, cmd, base):
    text, revision = cmd["text"], cmd["revision"]
    if type(text) is not str or "\0" in text or len(text.encode()) > REPAIR_LIMIT:
        raise ValueError("The corrected hook document exceeds the native repair limit.")
    idx = SCOPES.index(scope)
    paths = tuple(_unlinked(app.hook_config.path(name)).absolute() for name in SCOPES)
    if paths != lifecycle_hooks.config_paths(app._hook_workspace) or paths[0] == paths[1]:
        raise ValueError("Hook scope paths changed or overlap.")
    raw = tuple(scope_bytes(app, name) for name in SCOPES)
    if raw[idx] is None or readable(raw[idx], scope):
        raise ValueError("There is no unreadable hook document in this scope; use hook editing instead.")
    if native_hook_toggle.digest(raw[idx]) != revision:
        raise ValueError("The unreadable document changed since it was shown; reload it.")
    try:
        document = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"The corrected text is not valid JSON: {exc}") from exc
    rows = check_rows(lifecycle_hooks.HookConfig.parse_document(document, scope))  # what repair() itself runs
    after = canonical_bytes(rows)
    expected = native_hook_toggle.capture(app, (scope, after))
    if expected.paths != paths or expected.raw[1 - idx] != raw[1 - idx]:
        raise ValueError("Hook scope changed while preparing review.")
    wire = {**base, "kind": "hook_repair_review", "revision": revision, "token": secrets.token_hex(32),
            "file": FILES[scope], "before_sha256": revision, "before_bytes": len(raw[idx]),
            "after_text": after.decode("utf-8").replace("\r\n", "\n"), "rows": [wire_hook(h) for h in rows],
            "after_sha256": native_hook_toggle.digest(after), "after_revision": expected.revision,
            "hooks_off": expected.effective.disabled}
    bridge.hook_edit = Repair(bridge.instance, app.hook_config, scope, paths, raw, text, rows, after, wire)
    return wire


def repair_readback(app, pending):
    idx = SCOPES.index(pending.scope)
    current = native_hook_toggle.capture(app)

    def read_scope(name):
        status, raw = native_context._regular_file(current.paths[SCOPES.index(name)])
        if status == "oversize" or raw != current.raw[SCOPES.index(name)]:
            raise ValueError("Hook scope changed during effective readback.")
        return native_hook_toggle.document(raw, name)

    resolved = app.hook_config.snapshot(read_scope=read_scope)
    if resolved.error or resolved != current.effective:
        raise ValueError("Effective hook changed during independent readback.")
    saved = [wire_hook(h) for h in current.rows[idx]]
    if (current.paths != pending.paths or current.raw[idx] != pending.after
            or current.raw[1 - idx] != pending.raw[1 - idx] or current.revision != pending.wire["after_revision"]
            or saved != pending.wire["rows"] or resolved.disabled != pending.wire["hooks_off"]):
        raise ValueError("Hook configuration changed after repair; saved and effective outcome is unverified.")
    return {"revision": current.revision, "sha256": native_hook_toggle.digest(current.raw[idx]),
            "hooks_off": resolved.disabled, "rows": saved}


def repair(app, cmd, bridge, scope, verb, base):
    if verb == "review":
        bridge.hook_edit = None
        return repair_review(app, bridge, scope, cmd, base)
    pending = getattr(bridge, "hook_edit", None)
    if (not isinstance(pending, Repair) or pending.instance != bridge.instance or pending.scope != scope
            or cmd["token"] != pending.wire["token"] or pending.config is not app.hook_config):
        raise ValueError("Hook repair review belongs to another scope, source instance or selection.")
    if verb == "apply":
        if pending.apply_id is None:
            sealed(app, bridge, pending)
            try:  # the canonical repair: compare-and-swap on the exact unreadable bytes, under the scope lock
                app.hook_config.repair(scope, pending.text, pending.raw[SCOPES.index(scope)])
            except (lifecycle_hooks.HookError, OSError) as exc:
                raise ValueError(f"Hook repair was refused: {exc}") from exc
            pending.apply_id = cmd["id"]
        elif pending.apply_id != cmd["id"]:
            raise ValueError("This hook repair was already applied.")
        return {**base, "kind": "hook_repair_apply", "token": pending.wire["token"], "apply_id": pending.apply_id}
    if pending.apply_id is None or cmd["apply_id"] != pending.apply_id:
        raise ValueError("Hook repair readback belongs to another application.")
    return {**base, "kind": "hook_repair_saved", "token": pending.wire["token"], "apply_id": pending.apply_id,
            **repair_readback(app, pending)}


def dispatch(app, cmd):
    op = cmd["type"]
    mending = op.startswith(REPAIR)
    verb = op.removeprefix(REPAIR if mending else PREFIX)
    fields = {"snapshot": ("scope",), "review": ("scope", "revision", "action", "hook_id", "hook"),
              "apply": ("scope", "token"), "readback": ("scope", "token", "apply_id")}
    if mending:
        fields["review"] = ("scope", "revision", "text")
    bridge, binding = native_context.read_binding(app, cmd, op, *fields[verb])
    scope = cmd["scope"]
    if type(scope) is not str or scope not in SCOPES:
        raise ValueError("Choose a supported hook scope.")
    gui_rpc._idle(app)
    base = {"version": 1, **binding, "scope": scope}
    if mending:
        return repair(app, cmd, bridge, scope, verb, base)
    if verb == "snapshot":
        bridge.hook_edit = None
        return snapshot(app, scope, base)
    if verb == "review":
        bridge.hook_edit = None
        if type(cmd["hook_id"]) not in (str, type(None)):
            raise ValueError("Malformed hook selection.")
        return review(app, bridge, scope, cmd, base)
    pending = getattr(bridge, "hook_edit", None)
    if (not isinstance(pending, Review) or pending.instance != bridge.instance or pending.scope != scope
            or cmd["token"] != pending.wire["token"] or pending.config is not app.hook_config):
        raise ValueError("Hook review belongs to another scope, source instance or selection.")
    if verb == "apply":
        if pending.apply_id is None:
            publish(app, bridge, pending)
            pending.apply_id = cmd["id"]
        elif pending.apply_id != cmd["id"]:
            raise ValueError("This hook change was already applied.")
        return {**base, "kind": "hook_edit_apply", "token": pending.wire["token"], "apply_id": pending.apply_id}
    if pending.apply_id is None or cmd["apply_id"] != pending.apply_id:
        raise ValueError("Hook readback belongs to another application.")
    return {**base, "kind": "hook_edit_saved", "token": pending.wire["token"], "apply_id": pending.apply_id,
            **readback(app, pending)}
