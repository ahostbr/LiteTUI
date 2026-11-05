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

from litetui import gui_rpc, mcp_client, native_context
from litetui.agent_store import _unlinked
from litetui.plugins.mcp_manage import _mutation_blocked_reason
from litetui.shared_state import coordinated_write

PREFIX = "gui.mcp.native_config_"
FILES = mcp_client.MCP_CONFIG_NAMES
MAX_ROWS = 64
SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,96}")


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


def _receipt(binding, review, kind):
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
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    op = cmd["type"].removeprefix(PREFIX)
    bridge, binding = native_context.read_binding(app, cmd, PREFIX + op, *fields[op])
    manager, root = _root(app)
    if op in {"list", "review"}:
        app._native_mcp_config_review = None
        before = _files(root)
        snapshot = _snapshot(binding, before)
        if op == "list":
            return snapshot
        _idle(app, manager)
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
        return _receipt(binding, review, "mcp_config_review")

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
        return _receipt(binding, review, "mcp_config_applied")
    if (
        review.apply_id is None
        or cmd["apply_id"] != review.apply_id
        or _files(root) != review.after
    ):
        raise ValueError(
            "Shared MCP declarations changed after apply. Refresh to inspect them."
        )
    result = _receipt(binding, review, "mcp_config_saved")
    result["config"] = _snapshot(binding, _files(root))
    if result["config"]["revision"] != result["after"]:
        raise ValueError("Shared MCP declarations changed during readback")
    app._native_mcp_config_review = None
    return result
