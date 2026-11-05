"""Inspect shared declarations and edit one disabled flag without opening MCP transports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from litetui import gui_rpc, mcp_client, native_context
from litetui.agent_store import _unlinked
from litetui.plugins.mcp_manage import _mutation_blocked_reason
from litetui.shared_state import coordinated_write

PREFIX = "gui.mcp.native_config_"
FILES = mcp_client.MCP_CONFIG_NAMES
WRITE_FILE = mcp_client.WRITE_CONFIG_NAME  # the only file the source writer edits
MAX_ROWS = 64
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,96}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,95}")
HEADER_NAME = re.compile(r"[A-Za-z0-9-]{1,96}")
MAX_COMMAND, MAX_URL, MAX_ARGS, MAX_ARG, MAX_PAIRS, MAX_VALUE = 1024, 2048, 64, 1024, 32, 4096


@dataclass
class Review:
    manager: mcp_client.MCPManager
    root: Path
    instance: str
    token: str
    name: str
    previous: bool
    disabled: bool
    before: tuple
    after: tuple
    apply_id: str | None = None
    change: str = "toggle"
    transport: str = ""
    entry: dict | None = None


def _pairs(values):
    result = {}
    for key, value in values:
        if key in result:
            raise ValueError("MCP declaration contains duplicate fields")
        result[key] = value
    return result


def _root(app):
    manager = getattr(app, "mcp", None)
    if not isinstance(manager, mcp_client.MCPManager):
        raise ValueError("The owned MCP manager is unavailable")  # noqa: TRY004 - RPC refusal
    root = _unlinked(app._agent_session.store.data_root)
    if manager.root != root or not root.is_dir():
        raise ValueError("MCP declarations do not belong to the owned data root")
    return manager, root


def _files(root):
    documents = []
    for name in FILES:
        status, data = native_context._regular_file(root / name)
        if status == "oversize":
            raise ValueError("MCP declaration file exceeds 256 KiB")
        documents.append(data)
    return tuple(documents)


def _revision(files):
    digest = hashlib.sha256()
    for data in files:
        digest.update(
            b"-" if data is None else b"+" + len(data).to_bytes(4, "big") + data
        )
    return digest.hexdigest()


def _invalid_constant(_value):
    raise ValueError("MCP declaration contains a non-JSON numeric constant")


def _doc(raw):
    if raw is None:
        return {}, {}, False
    try:
        doc = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_invalid_constant,
        )
    except (ValueError, RecursionError):
        raise ValueError(
            "MCP declaration is not a complete unambiguous UTF-8 JSON document"
        ) from None
    if not isinstance(doc, dict):
        raise ValueError("MCP declaration requires a JSON object")  # noqa: TRY004 - RPC refusal
    for key in ("mcpServers", "servers"):
        if key in doc and not isinstance(doc[key], dict):
            raise ValueError("MCP server declarations require a JSON object")
    block = doc.get("mcpServers") or doc.get("servers") or {}
    return doc, block, "mcpServers" in doc and block is doc["mcpServers"]


def _rows(files):
    rows, earlier, hidden = [], set(), 0
    for index, data in enumerate(files):
        _, block, canonical = _doc(data)
        for name, config in block.items():
            if not SAFE_NAME.fullmatch(name):
                hidden += 1
                continue
            if len(rows) >= MAX_ROWS:
                raise ValueError(
                    "Shared MCP declarations exceed the 64-row inspection limit"
                )
            valid = (
                isinstance(config, dict) and mcp_client.validate_entry(config) is None
            )
            entry = config if isinstance(config, dict) else {}
            transport = (
                "stdio"
                if isinstance(entry.get("command"), str) and entry.get("command")
                else (
                    "http"
                    if isinstance(entry.get("url"), str) and entry.get("url")
                    else "unknown"
                )
            )
            disabled = entry.get("disabled", False)
            disabled = disabled if type(disabled) is bool else None
            rows.append(
                {
                    "name": name,
                    "file": FILES[index],
                    "transport": transport,
                    "disabled": disabled,
                    "has_args": bool(entry.get("args")),
                    "has_env": bool(entry.get("env")),
                    "has_headers": bool(entry.get("headers")),
                    "shadowed": name in earlier,
                    "removable": index == 1 and canonical and name not in earlier,
                    "editable": index == 1
                    and canonical
                    and name not in earlier
                    and valid
                    and transport != "unknown"
                    and disabled is not None,
                }
            )
        earlier.update(block)
    return rows, hidden


def _text(value, limit, label):
    if (
        type(value) is not str
        or not value
        or len(value) > limit
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError(
            f"{label} must be 1-{limit} printable characters without surrounding spaces"
        )
    return value


def _pairs_of(value, pattern, label):
    if type(value) is not dict or len(value) > MAX_PAIRS:
        raise ValueError(f"{label} must be an object of at most {MAX_PAIRS} entries")
    for key, item in value.items():
        if not pattern.fullmatch(key):
            raise ValueError(f"{label} contains an invalid name")
        if (
            type(item) is not str
            or not item
            or len(item) > MAX_VALUE
            or any(ord(char) < 32 or ord(char) == 127 for char in item)
        ):
            raise ValueError(f"{label} values must be 1-{MAX_VALUE} printable characters")
    return dict(value)


def _spec(spec):
    """Strictly parse the reviewed declaration form into (name, entry) with the source's rules."""
    if type(spec) is not dict or spec.get("transport") not in {"stdio", "http"}:
        raise ValueError("MCP declaration requires a stdio or http transport")
    stdio = spec["transport"] == "stdio"
    keys = (
        {"transport", "name", "command", "args", "env"}
        if stdio
        else {"transport", "name", "url", "headers"}
    )
    if set(spec) != keys:
        raise ValueError("MCP declaration contains unexpected fields")
    name = spec["name"]
    if type(name) is not str or not SAFE_NAME.fullmatch(name):
        raise ValueError("A server name needs 1-96 letters, digits, underscore, dot or hyphen")
    if stdio:
        args = spec["args"]
        if type(args) is not list or len(args) > MAX_ARGS:
            raise ValueError(f"Arguments must be a list of at most {MAX_ARGS} items")
        entry = {
            "command": _text(spec["command"], MAX_COMMAND, "Command"),
            "args": [_text(arg, MAX_ARG, "Argument") for arg in args],
        }
        env = _pairs_of(spec["env"], ENV_NAME, "Environment")
        if env:
            entry["env"] = env
    else:
        url = _text(spec["url"], MAX_URL, "URL")
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname)
            parsed.port  # noqa: B018 - raises ValueError for an invalid port
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("A URL must be an absolute http:// or https:// address")
        entry = {"type": "http", "url": url}
        headers = _pairs_of(spec["headers"], HEADER_NAME, "Headers")
        if headers:
            entry["headers"] = headers
    reason = mcp_client.validate_entry(entry)
    if reason:
        raise ValueError(f"Cannot add {name!r}: {reason}")
    return name, entry


def _names(values):
    if not isinstance(values, dict):
        return [], 0
    safe = [key for key in values if SAFE_NAME.fullmatch(key)][:MAX_PAIRS]
    return safe, len(values) - len(safe)


def _redact(config):
    """Names and a target only: values, arguments, paths and query strings never leave the source."""
    from litetui.native_mcp_lifecycle import _redacted

    entry = config if isinstance(config, dict) else {}
    transport, target = "unknown", ""
    strings = {key: entry[key] for key in ("command", "url") if isinstance(entry.get(key), str)}
    if strings.get("command") or strings.get("url"):
        transport, target = _redacted(strings)
    args = entry.get("args")
    env, hidden_env = _names(entry.get("env"))
    headers, hidden_headers = _names(entry.get("headers"))
    return {
        "transport": transport,
        "target": target,
        "args": len(args) if isinstance(args, list) else 0,
        "env": env,
        "headers": headers,
        "omitted": hidden_env + hidden_headers,
    }


def _sized(doc):
    data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    if len(data) > native_context.MAX_DOCUMENT:
        raise ValueError("Edited MCP declaration exceeds 256 KiB")
    return data


def _review_add(manager, root, bridge, row_count, cmd, before):
    name, entry = _spec(cmd["spec"])
    if name in _doc(before[0])[1]:
        raise ValueError(
            f"{FILES[0]} already declares {name!r} and wins on precedence; "
            f"a write to {WRITE_FILE} would never be read"
        )
    doc = _doc(before[1])[0]
    if "servers" in doc:
        raise ValueError(
            f"{WRITE_FILE} uses a legacy `servers` block; /mcp writes only `mcpServers`"
        )
    block = doc.setdefault("mcpServers", {})
    if name in block:
        raise ValueError(f"{name!r} is already declared in {WRITE_FILE}")
    if row_count >= MAX_ROWS:
        raise ValueError("Shared MCP declarations are at the 64-row inspection limit")
    block[name] = entry
    redacted = _redact(entry)
    return Review(
        manager, root, bridge.instance, secrets.token_hex(32), name, False, False,
        before, (before[0], _sized(doc)), change="add", transport=redacted["transport"],
        entry=redacted,
    )


def _review_remove(manager, root, bridge, rows, cmd, before):
    name = cmd["name"]
    if type(name) is not str or not SAFE_NAME.fullmatch(name):
        raise ValueError("Choose a visible MCP declaration to remove")
    mine = next((r for r in rows if r["file"] == WRITE_FILE and r["name"] == name), None)
    primary = any(r["file"] == FILES[0] and r["name"] == name for r in rows)
    if mine is None or mine["shadowed"] or not mine["removable"]:
        if primary:
            raise ValueError(f"{name!r} is declared in {FILES[0]}, which /mcp does not write")
        raise ValueError(f"no server named {name!r} in {WRITE_FILE}")
    doc, block, _ = _doc(before[1])
    redacted = _redact(block.pop(name))
    return Review(
        manager, root, bridge.instance, secrets.token_hex(32), name, False, False,
        before, (before[0], _sized(doc)), change="remove", transport=mine["transport"],
        entry=redacted,
    )


_KINDS = {
    "toggle": ("mcp_config_review", "mcp_config_applied", "mcp_config_saved"),
    "add": ("mcp_config_add_review", "mcp_config_add_applied", "mcp_config_add_saved"),
    "remove": ("mcp_config_remove_review", "mcp_config_remove_applied", "mcp_config_remove_saved"),
}


def _idle(app, manager):
    gui_rpc._idle(app)
    from textual.worker import NoActiveWorker, get_current_worker

    try:
        worker = get_current_worker()
    except NoActiveWorker:
        worker = None
    own = (
        (worker,)
        if (
            worker is not None
            and worker.group == "gui-management"
            and worker._task is asyncio.current_task()
        )
        else ()
    )
    if app.settings.mcp_enabled is not False or _mutation_blocked_reason(
        app, ignore_workers=own
    ):
        raise ValueError(
            "MCP configuration changes require an idle supported session with MCP off"
        )
    with manager._servers_lock:
        if manager.servers or manager._stop_failed or manager._closing:
            raise ValueError(
                "MCP configuration changes require all server processes to be stopped"
            )


def _snapshot(binding, files):
    rows, hidden = _rows(files)
    return {
        "version": 1,
        **binding,
        "scope": "shared",
        "kind": "mcp_config",
        "revision": _revision(files),
        "rows": rows,
        "hidden": hidden,
    }


def _receipt(binding, review, phase):
    kind = _KINDS[review.change][phase]
    if review.change != "toggle":
        return {
            "version": 1,
            **binding,
            "scope": "shared",
            "kind": kind,
            "token": review.token,
            "name": review.name,
            "file": WRITE_FILE,
            "transport": review.transport,
            "entry": review.entry,
            "before": _revision(review.before),
            "after": _revision(review.after),
            "apply_id": review.apply_id,
        }
    return {
        "version": 1,
        **binding,
        "scope": "shared",
        "kind": kind,
        "token": review.token,
        "name": review.name,
        "previous": review.previous,
        "disabled": review.disabled,
        "before": _revision(review.before),
        "after": _revision(review.after),
        "apply_id": review.apply_id,
    }


def dispatch(app, cmd):
    try:
        return _dispatch(app, cmd)
    except (OSError, UnicodeError, RecursionError):
        raise ValueError(
            "Shared MCP declaration could not be safely read or published. Refresh to inspect it."
        ) from None


def _dispatch(app, cmd):
    fields = {
        "list": (),
        "review": ("revision", "name", "disabled"),
        "add_review": ("revision", "spec"),
        "remove_review": ("revision", "name"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    op = cmd["type"].removeprefix(PREFIX)
    bridge, binding = native_context.read_binding(app, cmd, PREFIX + op, *fields[op])
    manager, root = _root(app)
    if op in {"list", "review", "add_review", "remove_review"}:
        app._native_mcp_config_review = None
        before = _files(root)
        snapshot = _snapshot(binding, before)
        if op == "list":
            return snapshot
        _idle(app, manager)
        if op != "review":
            if snapshot["revision"] != cmd["revision"]:
                raise ValueError("Shared MCP declarations changed. Refresh and review again")
            if op == "add_review":
                review = _review_add(manager, root, bridge, len(snapshot["rows"]), cmd, before)
            else:
                review = _review_remove(manager, root, bridge, snapshot["rows"], cmd, before)
            app._native_mcp_config_review = review
            return _receipt(binding, review, 0)
        target = next(
            (
                row
                for row in snapshot["rows"]
                if row["file"] == FILES[1] and row["name"] == cmd["name"]
            ),
            None,
        )
        if (
            snapshot["revision"] != cmd["revision"]
            or target is None
            or not target["editable"]
            or type(cmd["disabled"]) is not bool
            or cmd["disabled"] == target["disabled"]
        ):
            raise ValueError(
                "Refresh and choose an editable, unshadowed .mcp.json declaration"
            )
        doc, block, _ = _doc(before[1])
        block[target["name"]]["disabled"] = cmd["disabled"]
        data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
        if len(data) > native_context.MAX_DOCUMENT:
            raise ValueError("Edited MCP declaration exceeds 256 KiB")
        review = Review(
            manager,
            root,
            bridge.instance,
            secrets.token_hex(32),
            target["name"],
            target["disabled"],
            cmd["disabled"],
            before,
            (before[0], data),
        )
        app._native_mcp_config_review = review
        return _receipt(binding, review, 0)

    review = getattr(app, "_native_mcp_config_review", None)
    if (
        not isinstance(review, Review)
        or review.token != cmd["token"]
        or review.manager is not manager
        or review.root != root
        or review.instance != bridge.instance
    ):
        raise ValueError("MCP configuration review is stale")
    if op == "apply":
        if review.apply_id is not None:
            raise ValueError("MCP configuration review was already consumed")
        app._native_mcp_config_review = None
        _idle(app, manager)
        if not manager._try_claim():
            raise ValueError("MCP maintenance is in progress")
        path = root / FILES[1]
        try:
            lock_path = _unlinked(path.with_name(path.name + ".lock"))
            if lock_path.exists():
                info = lock_path.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("MCP declaration lock is not an owned regular file")
            with manager._op_lock, coordinated_write(path, timeout=0):

                def guard():
                    bridge.check()
                    app._validate_owned_identity()
                    if _root(app) != (manager, root) or _files(root) != review.before:
                        raise ValueError("Shared MCP declarations changed since review")
                    _idle(app, manager)

                guard()
                mcp_client.save_reviewed_declaration(path, review.after[1], guard)
                if _files(root) != review.after:
                    raise ValueError(
                        "MCP configuration publication could not be verified. Refresh to inspect it."
                    )
        finally:
            manager._release_claim()
        review.apply_id = cmd["id"]
        app._native_mcp_config_review = review
        return _receipt(binding, review, 1)
    if (
        review.apply_id is None
        or cmd["apply_id"] != review.apply_id
        or _files(root) != review.after
    ):
        raise ValueError(
            "Shared MCP declarations changed after apply. Refresh to inspect them."
        )
    result = _receipt(binding, review, 2)
    result["config"] = _snapshot(binding, _files(root))
    if result["config"]["revision"] != result["after"]:
        raise ValueError("Shared MCP declarations changed during readback")
    if review.change != "toggle":
        present = any(
            row["file"] == WRITE_FILE and row["name"] == review.name
            for row in result["config"]["rows"]
        )
        if present != (review.change == "add"):
            raise ValueError("Shared MCP declarations changed during readback")
    app._native_mcp_config_review = None
    return result
