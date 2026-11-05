"""Explicit, owned microphone capture with a separately confirmed composer result."""
from __future__ import annotations

import asyncio
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import native_runtime_controls as controls
from litetui import ttyguard
from litetui.native_context import read_binding
from litetui.native_dictation_worker import MAX_RECORDING, MAX_TEXT

OPERATION = "gui.voice.native_dictation"


@dataclass
class Capture:
    binding: dict
    identity: tuple
    token: str
    draft: str
    preferences: dict
    cancel: threading.Event
    temporary: object = None
    directory: Path | None = None
    task: asyncio.Task | None = None
    status: str = "review"
    transcript: str = ""
    unsettled: bool = False
    failure: str | None = None
    release_command: bytes | None = None
    release_task: asyncio.Task | None = None
    release_result: dict | None = None
    release_raw: bytes | None = None
    publication: Publication | None = None

    def check(self, app, binding):
        if self.binding != binding or self.identity != (app._agent_session, app.store, app.backend, app.model_id):
            raise ValueError("Dictation belongs to a different owned conversation")
        bridge = work.bridge(app)
        if bridge is None or not bridge.active or bridge.instance != self.binding["work_instance"]:
            raise ValueError("Dictation belongs to a different Work instance")
        bridge.check()
        app._validate_owned_execution()


def _preferences(app):
    settings = app.settings
    if settings.stt_model not in ("tiny.en", "base.en", "small.en"):
        raise ValueError("Select a supported installed dictation model in Voice settings")
    microphone = settings.stt_mic or ""
    hotkey = settings.stt_hotkey or "ctrl+space"
    for value in (microphone, hotkey):
        if type(value) is not str or len(value.encode()) > 512 or not all(c.isprintable() for c in value):
            raise ValueError("The configured dictation device exceeds its display limit")
    return {"model": settings.stt_model, "microphone": microphone, "hotkey": hotkey}


def _command(directory):
    return [sys.executable, "-B", "-m", "litetui.native_dictation_worker", str(directory)]


def _guard(app):
    if reason := controls.mutation_hold(app):
        raise ValueError(reason)
    if not controls.enabled(app):
        raise ValueError("Dictation requires the active runtime controls")
    state = controls._state(app)
    controls._guard(app, state)
    if controls._owner(state)["state"] != "idle" or app._pending_input:
        raise ValueError("Finish the running reply and pending input before dictating")


def _status(capture):
    if capture.task is not None and not capture.task.done():
        if (capture.directory / "transcribing").exists():
            return "transcribing"
        if (capture.directory / "recording").exists():
            return "recording"
        return "starting"
    return capture.status


def _result(capture, status=None):
    text = capture.transcript if (status or _status(capture)) in {"ready", "applied"} else ""
    return {"version": 1, "kind": "dictation", **capture.binding,
            "capture": capture.token, "draft_sha256": capture.draft,
            "preferences": capture.preferences, "seconds": MAX_RECORDING,
            "status": status or _status(capture), "text": text,
            "text_sha256": hashlib.sha256(text.encode()).hexdigest() if text else None}


async def _run(capture):
    worker = asyncio.create_task(asyncio.to_thread(ttyguard.run_owned_writer, _command(capture.directory),
                    cwd=capture.directory, env=dict(os.environ), timeout=MAX_RECORDING + 320, cancel=capture.cancel))
    try:
        try:
            code = await asyncio.shield(worker)
        except asyncio.CancelledError:
            capture.cancel.set()
            from litetui.shutdown_wait import join_task
            await join_task(worker)
            raise InterruptedError("Dictation worker cancelled")
        if code != 0:
            raise ValueError("Dictation helper failed")
        path = capture.directory / "result.json"
        raw = work._read(path, 64 * 1024)
        if raw is None:
            raise ValueError("Dictation helper did not produce a result")
        value = work.strict_json(raw)
        if set(value) != {"status", "text"} or value["status"] not in {"ready", "empty", "failed"}:
            raise ValueError("Invalid dictation result")
        text = value["text"]
        if type(text) is not str or len(text.encode()) > MAX_TEXT or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in text):
            raise ValueError("Invalid transcript")
        if (value["status"] == "ready") != bool(text.strip()) or value["status"] != "ready" and text:
            raise ValueError("Dictation text and state disagree")
        capture.status, capture.transcript = value["status"], text
    except ttyguard.OwnedTreeUnsettled:
        capture.status, capture.unsettled = "failed", True
    except InterruptedError:
        capture.status = "cancelled"
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError) as error:
        capture.status = "failed"
        capture.failure = str(error)


async def retire(app, *, closing=False):
    capture = getattr(app, "_native_dictation", None)
    if capture is None:
        return
    capture.cancel.set()
    if capture.task is not None:
        from litetui.shutdown_wait import join_task
        await join_task(capture.task)
        if not capture.task.done():
            raise RuntimeError("Dictation worker has not retired")
    if capture.unsettled:
        raise RuntimeError("Dictation descendant retirement is unproven")
    if capture.temporary is not None:
        capture.temporary.cleanup()
        capture.temporary = None
    if closing:
        bridge = work.bridge(app)
        if bridge is None or not bridge.closing:
            raise RuntimeError("Dictation shutdown release requires a closing Work owner")
        app._native_dictation = None


@dataclass
class Publication:
    app: object
    capture: Capture
    task: asyncio.Task | None
    command: bytes
    result: dict
    raw: bytes
    used: bool = False


def prepare_publication(app, cmd, result):
    if cmd.get("type") != OPERATION or cmd.get("action") not in ("apply", "cancel"):
        return None
    capture = getattr(app, "_native_dictation", None)
    if (capture is None or capture.release_command != work.encoded(cmd)
            or capture.release_task is not work._task() or capture.release_result is not result
            or capture.release_raw != work.encoded(result) or capture.publication is not None):
        raise ValueError("Dictation release publication is foreign, changed or consumed")
    capture.check(app, capture.binding)
    ticket = Publication(app, capture, work._task(), capture.release_command, result, capture.release_raw)
    capture.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (not isinstance(ticket, Publication) or ticket.app is not app or ticket.used
            or ticket.task is not work._task() or getattr(app, "_native_dictation", None) is not ticket.capture
            or ticket.capture.publication is not ticket or work.encoded(ticket.result) != ticket.raw
            or ticket.capture.release_command != ticket.command or ticket.capture.release_result is not ticket.result):
        raise ValueError("Dictation release lost its exact one-use publication owner")
    ticket.capture.check(app, ticket.capture.binding)
    ticket.used = True
    ticket.capture.publication = None
    app._native_dictation = None
    controls.signal_stop(app)
    bridge = work.bridge(app)
    wake = getattr(app, "_flush_pending_input", None)
    if wake is not None and getattr(app, "_pending_input", ()) and bridge is not None and not bridge.closing:
        app.call_later(wake)


async def release(app, capture, cmd, status):
    capture.release_command = work.encoded(cmd)
    capture.release_task = work._task()
    # Reserve before awaiting child cleanup, including cancellation from Review.
    capture.status = status
    await retire(app)
    result = _result(capture, status)
    capture.release_result, capture.release_raw = result, work.encoded(result)
    return result


def blocking(app):
    capture = getattr(app, "_native_dictation", None)
    return capture is not None and capture.status != "review"


async def dispatch(app, cmd):
    _bridge, binding = read_binding(app, cmd, OPERATION, "action", "capture", "draft_sha256", "text_sha256")
    action = cmd["action"]
    if action not in ("review", "start", "status", "finish", "cancel", "apply"):
        raise ValueError("Unknown dictation action")
    if not isinstance(cmd["draft_sha256"], str) or re.fullmatch("[0-9a-f]{64}", cmd["draft_sha256"]) is None:
        raise ValueError("Dictation requires the exact composer draft")
    capture = getattr(app, "_native_dictation", None)
    if action == "review":
        _guard(app)
        if cmd["capture"] is not None or cmd["text_sha256"] is not None or capture is not None:
            raise ValueError("Finish or discard the previous dictation")
        capture = Capture(binding, (app._agent_session, app.store, app.backend, app.model_id),
                          work.ident(), cmd["draft_sha256"], _preferences(app), threading.Event())
        app._native_dictation = capture
        return _result(capture)
    if capture is None or cmd["capture"] != capture.token or cmd["draft_sha256"] != capture.draft:
        raise ValueError("Dictation capture is stale or belongs to another draft")
    capture.check(app, binding)
    if capture.release_command is not None:
        raise ValueError("Dictation release awaits its exact result publication")
    if action != "apply" and cmd["text_sha256"] is not None:
        raise ValueError("Unexpected dictation transcript identity")
    if action == "start":
        if capture.status != "review" or capture.task is not None or _preferences(app) != capture.preferences:
            raise ValueError("Dictation was already started or its voice preferences changed")
        _guard(app)
        directory = app._agent_session.store.data_root / "recordings"
        directory.mkdir(parents=True, exist_ok=True)
        capture.temporary = tempfile.TemporaryDirectory(prefix="native-", dir=directory)
        capture.directory = Path(capture.temporary.name)
        (capture.directory / "parameters.json").write_bytes(work.encoded(capture.preferences))
        capture.status = "starting"
        capture.task = asyncio.create_task(_run(capture))
    elif action == "finish":
        if _status(capture) not in ("starting", "recording"):
            raise ValueError("Dictation is no longer recording")
        (capture.directory / "finish").touch()
    elif action == "cancel":
        return await release(app, capture, cmd, "cancelled")
    elif action == "apply":
        if _status(capture) != "ready" or cmd["text_sha256"] != hashlib.sha256(capture.transcript.encode()).hexdigest():
            raise ValueError("Review the completed dictation before applying it")
        return await release(app, capture, cmd, "applied")
    return _result(capture)
