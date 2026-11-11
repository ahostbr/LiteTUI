"""One reviewed monitor attempt, contained worker and publication-owned release."""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from litetui import background_input as work
from litetui import gui_rpc, ttyguard
from litetui import native_monitor_files as files
from litetui import native_runtime_controls as controls
from litetui.agent_store import _unlinked
from litetui.native_context import read_binding
from litetui.native_helper_cleanup import remove_directory
from litetui.plugins import monitor_plugin as plugin
from litetui.shared_state import coordinated_write

PREFIX = "gui.monitor.native_"
OPERATIONS = tuple(PREFIX + operation for operation in ("snapshot", "review", "apply", "readback", "close"))


@dataclass
class Attempt:
    binding: dict
    owner: tuple
    snapshot: dict
    targets: list
    captured: dict
    token: str
    apply_id: str | None = None
    temporary: object = None
    directory: Path | None = None
    task: asyncio.Task | None = None
    cancel: threading.Event = field(default_factory=threading.Event)
    unsettled: bool = False
    status: str = "review"
    rows: list = field(default_factory=list)
    after: list = field(default_factory=list)
    written: bool = False
    release_command: bytes | None = None
    release_task: asyncio.Task | None = None
    release_result: dict | None = None
    release_raw: bytes | None = None
    publication: object = None


def _owner(app):
    entry = app.plugins.commands.get("/monitor")
    if entry is None or entry.owner != "monitor" or entry.handler is not plugin._handle:
        raise ValueError("Monitor requires the exact registered builtin command")
    return (app._agent_session, app.store, app.backend, entry, entry.handler, app.model_id,
            str(Path(app._hook_workspace).absolute()), work.encoded(controls._facts(app)))


def _check(app, attempt):
    bridge = work.bridge(app)
    current = _owner(app)
    if (bridge is None or not bridge.active or bridge.instance != attempt.binding["work_instance"]
            or any(left is not right for left, right in zip(current[:5], attempt.owner[:5], strict=True))
            or current[5:] != attempt.owner[5:]):
        raise ValueError("Monitor belongs to a different registered runtime owner")
    bridge.check()
    app._validate_owned_execution()


def _guard(app):
    gui_rpc._idle(app)
    if not controls.enabled(app):
        raise ValueError("Monitor requires negotiated native runtime ownership")
    controls._guard(app, controls._state(app))
    if reason := controls.mutation_hold(app):
        raise ValueError(reason)
    if app._pending_input or controls._owner(controls._state(app))["state"] != "idle":
        raise ValueError("Finish queued and running work before starting a monitor operation")


def _command(directory):
    return [sys.executable, "-B", "-m", "litetui.native_monitor_worker", str(directory)]


def _rows(value, attempt):
    if type(value) is not dict or set(value) != {"rows"} or type(value["rows"]) is not list:
        raise ValueError("Invalid monitor helper result")
    rows = value["rows"]
    expected = attempt.snapshot["targets"] if attempt.snapshot["action"] != "init" else []
    if len(rows) != len(expected):
        raise ValueError("Monitor helper changed the reviewed target inventory")
    for row, target in zip(rows, expected, strict=True):
        if (type(row) is not dict or set(row) != {"name", "url", "ok", "status", "text_chars", "expect_met", "regression", "error"}
                or row["name"] != target["name"] or row["url"] != target["url"]
                or type(row["ok"]) is not bool or type(row["expect_met"]) is not bool
                or row["status"] not in {"ok", "error"} or row["ok"] != (row["status"] == "ok")
                or type(row["text_chars"]) is not int or not 0 <= row["text_chars"] <= 100_000_000
                or row["regression"] not in {"none", "baseline", "stable", "changed"}
                or row["error"] not in {"", "Probe failed; see the selected browser target."}):
            raise ValueError("Invalid bounded monitor result row")
    return rows


def _promote(attempt):
    candidates = {}
    for index, (path, before) in enumerate(attempt.captured.items()):
        staged = files.read(attempt.directory / f"output-{index}", files.MAX_IMAGE if path.suffix.lower() == ".png" else files.MAX_LOG)
        candidates[path] = staged if staged is not None else before
    if sum(len(data or b"") for data in candidates.values()) > files.MAX_TOTAL:
        raise ValueError("Monitor results exceed the total artifact budget")
    if attempt.snapshot["action"] == "init" and list(candidates.values()) != [attempt.snapshot["seed"].encode("utf-8")]:
        raise ValueError("Monitor init changed the exact reviewed seed")
    if attempt.snapshot["action"] in {"demo", "sweep"}:
        log = next(iter(attempt.captured))
        if candidates[log] is None or not candidates[log].startswith(attempt.captured[log] or b""):
            raise ValueError("Monitor log did not preserve its captured prefix")
    with ExitStack() as locks:
        for path in sorted(candidates, key=str):
            _unlinked(path.parent).mkdir(parents=True, exist_ok=True)
            _unlinked(Path(str(path) + ".lock"))
            locks.enter_context(coordinated_write(path, timeout=1))
        for path, before in attempt.captured.items():
            if files.read(path, files.MAX_IMAGE if path.suffix.lower() == ".png" else files.MAX_LOG) != before:
                raise ValueError("A reviewed monitor output changed; no result can overwrite it")
        for path, candidate in candidates.items():
            if attempt.cancel.is_set():
                raise InterruptedError("Monitor cancelled before output publication")
            if candidate == attempt.captured[path]:
                continue
            if candidate is None:
                raise ValueError("Monitor cannot remove a reviewed output")
            if attempt.snapshot["action"] == "init":
                with _unlinked(path).open("xb") as stream:
                    attempt.written = True
                    stream.write(candidate)
            else:
                descriptor, name = tempfile.mkstemp(prefix=".monitor-", dir=path.parent)
                temporary = Path(name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(candidate)
                        stream.flush()
                    if files.read(path, files.MAX_IMAGE if path.suffix.lower() == ".png" else files.MAX_LOG) != attempt.captured[path]:
                        raise ValueError("Monitor output changed before publication")
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            attempt.written = True
    return [files.file_row(path, data) for path, data in candidates.items()]


async def _run(app, attempt):
    environment = dict(os.environ)
    environment.update({key: str(attempt.directory) for key in ("TMP", "TEMP", "TMPDIR")})
    worker = asyncio.create_task(asyncio.to_thread(ttyguard.run_owned_writer, _command(attempt.directory),
        cwd=attempt.directory, env=environment, timeout=max(30, len(attempt.targets) * 150), cancel=attempt.cancel))
    try:
        try:
            code = await asyncio.shield(worker)
        except asyncio.CancelledError:
            attempt.cancel.set()
            from litetui.shutdown_wait import join_task
            await join_task(worker)
            raise InterruptedError("Monitor cancelled")
        if code:
            raise ValueError("Monitor helper failed")
        if attempt.cancel.is_set():
            raise InterruptedError("Monitor cancelled")
        attempt.rows = _rows(work.strict_json(files.read(attempt.directory / "result.json", 65536)), attempt)
        _check(app, attempt)
        current, _, _ = files.snapshot(app, attempt.snapshot["arguments"])
        if current != attempt.snapshot:
            raise ValueError("Reviewed monitor configuration or output changed")
        promotion = asyncio.create_task(asyncio.to_thread(_promote, attempt))
        try:
            attempt.after = await asyncio.shield(promotion)
        except asyncio.CancelledError:
            attempt.cancel.set()
            from litetui.shutdown_wait import join_task
            await join_task(promotion)
            raise InterruptedError("Monitor cancelled during output publication")
        attempt.status = "completed"
    except ttyguard.OwnedTreeUnsettled:
        attempt.status, attempt.unsettled = "uncertain", True
    except InterruptedError:
        attempt.status = "uncertain" if attempt.written else "cancelled"
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError):
        attempt.status = "uncertain" if attempt.written else "failed"


def _result(attempt, kind):
    artifacts = []
    if attempt.task is not None and attempt.task.done():
        artifacts = [files.file_row(path, files.read(path, files.MAX_IMAGE if path.suffix.lower() == ".png" else files.MAX_LOG))
                     for path in attempt.captured]
        if attempt.status == "completed" and artifacts != attempt.after:
            raise ValueError("Monitor outputs changed after publication; no completed readback is available")
    return {"version": 1, **attempt.binding, "kind": kind, "token": attempt.token,
            "apply_id": attempt.apply_id, "snapshot": attempt.snapshot,
            "status": attempt.status, "rows": attempt.rows, "artifacts": artifacts,
            "cleanup_verified": attempt.task is not None and attempt.task.done() and not attempt.unsettled}


async def retire(app, *, closing=False):
    attempt = getattr(app, "_native_monitor", None)
    if attempt is None:
        return
    attempt.cancel.set()
    if attempt.task is not None:
        from litetui.shutdown_wait import join_task
        await join_task(attempt.task)
    if attempt.unsettled or attempt.task is not None and not attempt.task.done():
        raise RuntimeError("Monitor helper retirement remains unproven")
    if attempt.temporary is not None:
        await remove_directory(attempt.temporary, wait_seconds=10)
        attempt.temporary = None
    if closing:
        bridge = work.bridge(app)
        if bridge is None or not bridge.closing:
            raise RuntimeError("Monitor shutdown requires its closing Work owner")
        app._native_monitor = None


def blocking(app):
    return getattr(app, "_native_monitor", None) is not None


def readback_allowed(app, cmd):
    attempt = getattr(app, "_native_monitor", None)
    return (attempt is not None and cmd.get("type") in {PREFIX + "readback", PREFIX + "close"}
            and cmd.get("token") == attempt.token and cmd.get("apply_id") == attempt.apply_id)


@dataclass
class Publication:
    app: object
    attempt: Attempt
    task: asyncio.Task | None
    command: bytes
    result: dict
    raw: bytes
    used: bool = False


def prepare_publication(app, cmd, result):
    if cmd.get("type") != PREFIX + "close":
        return None
    attempt = getattr(app, "_native_monitor", None)
    if (attempt is None or attempt.release_command != work.encoded(cmd)
            or attempt.release_task is not work._task() or attempt.release_result is not result
            or attempt.release_raw != work.encoded(result) or attempt.publication is not None):
        raise ValueError("Monitor release lost its exact response owner")
    _check(app, attempt)
    ticket = Publication(app, attempt, work._task(), attempt.release_command, result, attempt.release_raw)
    attempt.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (not isinstance(ticket, Publication) or ticket.app is not app or ticket.used
            or ticket.task is not work._task() or getattr(app, "_native_monitor", None) is not ticket.attempt
            or ticket.attempt.publication is not ticket or work.encoded(ticket.result) != ticket.raw
            or ticket.attempt.release_command != ticket.command or ticket.attempt.release_result is not ticket.result):
        raise ValueError("Monitor release is foreign, changed or consumed")
    _check(app, ticket.attempt)
    ticket.used = True
    ticket.attempt.publication = None
    app._native_monitor = None
    controls.signal_stop(app)
    if app._pending_input and not work.bridge(app).closing:
        app.call_later(app._flush_pending_input)


async def dispatch(app, cmd):
    action = cmd["type"].removeprefix(PREFIX)
    fields = {"snapshot": ("arguments",), "review": ("arguments", "revision"),
              "apply": ("token",), "readback": ("token", "apply_id"), "close": ("token", "apply_id")}
    bridge, binding = read_binding(app, cmd, PREFIX + action, *fields[action])
    owner = _owner(app)
    if action in {"snapshot", "review"}:
        if blocking(app):
            raise ValueError("Close the current monitor result before another inspection")
        snapshot, targets, captured = files.snapshot(app, cmd["arguments"])
        if action == "snapshot":
            return {"version": 1, **binding, "kind": "monitor_snapshot", "snapshot": snapshot}
        _guard(app)
        if snapshot["revision"] != cmd["revision"] or not snapshot["allowed"] or snapshot["action"] == "targets":
            raise ValueError("Refresh and review an eligible monitor operation")
        attempt = Attempt(binding, owner, snapshot, targets, captured, secrets.token_hex(32))
        bridge.monitor_review = attempt
        return {"version": 1, **binding, "kind": "monitor_review", "snapshot": snapshot, "token": attempt.token}
    attempt = getattr(bridge, "monitor_review", None)
    if not isinstance(attempt, Attempt) or attempt.binding != binding or attempt.token != cmd["token"]:
        raise ValueError("Review this exact monitor operation first")
    _check(app, attempt)
    if action == "apply":
        _guard(app)
        if attempt.apply_id is not None or files.snapshot(app, attempt.snapshot["arguments"])[0] != attempt.snapshot:
            raise ValueError("Monitor review changed or was already attempted")
        attempt.apply_id, attempt.status = cmd["id"], "running"
        app._native_monitor = attempt
        try:
            directory = app._agent_session.store.data_root / "monitor-runs"
            directory.mkdir(parents=True, exist_ok=True)
            attempt.temporary = tempfile.TemporaryDirectory(prefix="native-", dir=directory)
            attempt.directory = Path(attempt.temporary.name)
            for index, data in enumerate(attempt.captured.values()):
                if data is not None:
                    (attempt.directory / f"output-{index}").write_bytes(data)
            (attempt.directory / "parameters.json").write_bytes(files.encoded(files.parameters(attempt.snapshot, attempt.targets, attempt.captured)))
            attempt.task = asyncio.create_task(_run(app, attempt))
        except (OSError, ValueError, TypeError):
            attempt.status = "failed"
            attempt.task = asyncio.create_task(asyncio.sleep(0))
            await attempt.task
        return _result(attempt, "monitor_applied")
    if attempt.apply_id != cmd["apply_id"] or getattr(app, "_native_monitor", None) is not attempt:
        raise ValueError("Monitor readback belongs to a different applied attempt")
    if attempt.release_command is not None:
        raise ValueError("Monitor close awaits its exact release publication")
    if action == "close":
        attempt.release_command, attempt.release_task = work.encoded(cmd), work._task()
        await retire(app)
        result = _result(attempt, "monitor_closed")
        attempt.release_result, attempt.release_raw = result, work.encoded(result)
        return result
    return _result(attempt, "monitor_readback")
