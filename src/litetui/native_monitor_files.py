"""Captured monitor configuration and bounded, reviewed output destinations."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from litetui.agent_store import _unlinked
from litetui.file_observations import stable_read
from litetui.monitor import targets
from litetui.plugins import monitor_plugin as plugin

MAX_CONFIG = 65536
MAX_LOG = 4 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_TOTAL = 64 * 1024 * 1024
MAX_TARGETS = 16


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def parse_arguments(arguments):
    if (type(arguments) is not str or len(arguments.encode()) > 8192
            or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in arguments)):
        raise ValueError("Invalid monitor arguments")
    try:
        parts = shlex.split(arguments, posix=False)
    except ValueError as error:
        raise ValueError("Quote the complete monitor config path") from error
    action = parts.pop(0).lower() if parts and not parts[0].startswith("--") else "sweep"
    if action == "run":
        action = "sweep"
    if action not in {"targets", "init", "demo", "sweep"}:
        raise ValueError("Use /monitor targets, init, demo or sweep, optionally --config PATH")
    path = plugin.DEFAULT_CONFIG
    if parts:
        if len(parts) != 2 or parts[0] != "--config":
            raise ValueError("Monitor accepts only one --config PATH option")
        path = parts[1]
        if len(path) >= 2 and path[0] == path[-1] and path[0] in "\"'":
            path = path[1:-1]
        if not path:
            raise ValueError("Monitor config path cannot be empty")
    if action == "demo" and parts:
        raise ValueError("Monitor demo uses its fixed sample targets; it takes no --config")
    return action, path


def _stamp(info, *, birthtime=False):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_nlink, info.st_size,
            info.st_mtime_ns, info.st_birthtime_ns if birthtime else info.st_ctime_ns)


def read(path, limit):
    path = _unlinked(Path(path).absolute())
    try:
        before = path.stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
        raise ValueError("Monitor files must be bounded, unlinked regular files")
    with path.open("rb") as stream:
        opened = os.fstat(stream.fileno())
        data = stream.read(limit + 1)
        finished = os.fstat(stream.fileno())
    after = _unlinked(path).stat()
    if len(data) > limit or not stable_read(before, opened, finished, after, stamp=_stamp):
        raise ValueError("Monitor file changed during inspection")
    return data


def file_row(path, data):
    if len(str(path).encode()) > 4096 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in str(path)):
        raise ValueError("Monitor output path exceeds its display budget")
    return {"path": str(path), "sha256": digest(data) if data is not None else None,
            "bytes": len(data) if data is not None else 0}


def display_url(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if ":" in host:
        host = "[" + host + "]"
    if parsed.port is not None:
        host += ":" + str(parsed.port)
    return urlunsplit((parsed.scheme, host, parsed.path, "[redacted]" if parsed.query else "", ""))


def validate_targets(rows):
    if len(rows) > MAX_TARGETS:
        raise ValueError("Monitor native sweeps support at most 16 targets")
    names = set()
    for row in rows:
        if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", row.name)
                or row.name.casefold() in names or row.name.rstrip(". ") != row.name
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", row.name)):
            raise ValueError("Monitor target names must be unique safe artifact names")
        names.add(row.name.casefold())
        if (len(row.url.encode()) > 2048 or any(ord(char) < 32 or 127 <= ord(char) < 160 for char in row.url)
                or type(row.timeout_ms) is not int
                or not 1 <= row.timeout_ms <= 60000 or type(row.screenshot) is not bool):
            raise ValueError("Monitor target URL or timeout exceeds the native limit")
        if not urlsplit(row.url).hostname:
            raise ValueError("Monitor requires an absolute HTTP(S) target")
        display_url(row.url)
        for value in (row.expect_text, row.text_selector):
            if value is not None and (type(value) is not str or len(value.encode()) > 4096):
                raise ValueError("Monitor target text exceeds the native limit")
    return rows


def snapshot(app, arguments):
    action, selected = parse_arguments(arguments)
    root = _unlinked(Path(app._hook_workspace).absolute())
    if not root.is_dir():
        raise ValueError("Monitor requires the current owned workspace directory")
    config = _unlinked((root / selected).absolute())
    if len(str(config).encode()) > 4096:
        raise ValueError("Monitor configuration path exceeds its native budget")
    raw = None if action == "demo" else read(config, MAX_CONFIG)
    try:
        rows = (plugin.demo_targets() if action == "demo" else
                targets.parse_targets(raw.decode("utf-8"), config.suffix) if raw is not None else [])
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise ValueError("Monitor config must contain a bounded targets list") from error
    validate_targets(rows)
    output_paths = ([config] if action == "init" else [] if action == "targets" else [root / plugin.DEFAULT_LOG])
    if action == "sweep":
        output_paths += [root / plugin.DEFAULT_STATE]
        output_paths += [root / plugin.DEFAULT_SHOTS / (row.name + ".png") for row in rows if row.screenshot]
    captured = {}
    for path in output_paths:
        path = _unlinked(path.absolute())
        limit = MAX_IMAGE if path.suffix.lower() == ".png" else MAX_LOG
        captured[path] = read(path, limit)
    if sum(len(data or b"") for data in captured.values()) > MAX_TOTAL:
        raise ValueError("Monitor output preimages exceed the capture budget")
    if len({str(path).casefold() for path in output_paths}) != len(output_paths):
        raise ValueError("Monitor output destinations overlap")
    if action != "init" and any(str(path).casefold() == str(config).casefold() for path in output_paths):
        raise ValueError("Monitor output would overwrite the selected configuration")
    if action == "init" and config.suffix.lower() == ".toml":
        raise ValueError("Monitor init writes JSON; choose a JSON config path")
    allowed = action == "targets" or action == "demo" or action == "init" and raw is None or action == "sweep" and bool(rows)
    reason = "" if allowed else "Config already exists; init never overwrites it." if action == "init" else "Create a config with targets before sweeping."
    sidecars = []
    for path in captured:
        lock_path = _unlinked(Path(str(path) + ".lock"))
        lock_data = read(lock_path, 1)
        if lock_data not in (None, b"", b"\0"):
            raise ValueError("Monitor output lock is not a coordination marker")
        sidecars.append(str(lock_path))
    seed = (json.dumps(targets.default_config(), indent=2) + "\n") if action == "init" else None
    value = {"action": action, "arguments": arguments, "workspace": str(root), "sidecars": sidecars, "seed": seed,
             "config": file_row(config, raw), "allowed": allowed, "reason": reason,
             "targets": [{"name": row.name, "url": display_url(row.url), "url_sha256": digest(row.url.encode()),
                          "screenshot": row.screenshot, "timeout_ms": row.timeout_ms} for row in rows],
             "outputs": [file_row(path, data) for path, data in captured.items()]}
    value["revision"] = digest(encoded(value))
    return value, rows, captured


def parameters(snapshot, rows, captured):
    return {"action": snapshot["action"], "targets": [asdict(row) for row in rows],
            "outputs": [str(path) for path in captured]}
