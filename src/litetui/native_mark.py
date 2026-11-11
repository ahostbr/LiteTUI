"""Reviewed screen marking; pixels and notes enter only the unsent composer."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import gui_rpc, native_image, ttyguard
from litetui import native_runtime_controls as controls
from litetui import native_voice_jobs as jobs
from litetui.agent_store import _unlinked
from litetui.native_context import read_binding
from litetui.plugins import mark_plugin as plugin

OPERATION = "gui.screen.native_mark"
ATTRIBUTE = "_native_mark"
MAX_DRAFT = 16384
MAX_NOTE = 4096
SECONDS = 180


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stamp(info):
    # Windows path stat infers executable bits from the suffix; fstat does not.
    return info.st_dev, info.st_ino, info.st_nlink, stat.S_IFMT(info.st_mode), info.st_size, info.st_mtime_ns


def read(path, cap, *, system_executable=False):
    """Read an unlinked regular file without accepting a replaced or growing input."""
    path = _unlinked(Path(path))
    before = path.lstat()
    # Windows protects its PowerShell executable using system hardlinks. Captured
    # images and bundled scripts still require a single link. All reads reject reparse paths.
    if (not stat.S_ISREG(before.st_mode) or not system_executable and before.st_nlink != 1
            or not 0 < before.st_size <= cap):
        raise ValueError("Mark requires a bounded unlinked regular file")
    with path.open("rb") as stream:
        if _stamp(os.fstat(stream.fileno())) != _stamp(before):
            raise ValueError("Mark input changed before reading")
        data = stream.read(cap + 1)
        finished = os.fstat(stream.fileno())
    if (_stamp(before) != _stamp(finished)
            or _stamp(before) != _stamp(_unlinked(path).lstat())
            or len(data) != before.st_size):
        raise ValueError("Mark input changed while reading")
    return data


def _file(path, cap, *, system_executable=False):
    path = Path(path).absolute()
    drive = path.drive.removeprefix("\\\\?\\")
    if (not path.is_absolute()
            or os.name == "nt" and re.fullmatch("[A-Za-z]:", drive) is None):
        raise ValueError("Mark requires local bundled files")
    return {"path": str(path), "sha256": hashlib.sha256(read(path, cap, system_executable=system_executable)).hexdigest()}


def _launcher():
    return shutil.which("powershell.exe")


def _plan(app):
    launcher = _launcher()
    if launcher is None:
        raise ValueError("The bundled screen marker requires Windows PowerShell")
    script = _file(plugin.MARK_SCRIPT, 262144)
    ring = plugin.MARK_SCRIPT.with_name("ring.png")
    return {"script": script, "launcher": _file(launcher, 32 * 1024 * 1024, system_executable=True),
            "ring": _file(ring, native_image.MAX_FILE) if ring.exists() else None,
            "delivery": native_image.delivery(app), "seconds": SECONDS}


def _source(app):
    entry = app.plugins.commands.get("/mark")
    if entry is None or entry.owner != "mark" or entry.handler is not plugin._cmd_mark:
        raise ValueError("Mark requires the exact registered builtin command")
    return (entry, entry.handler, work.encoded(controls._facts(app)),
            getattr(app, "model_type", None))


def _prior(bridge):
    entry = getattr(bridge, "composer_image", None)
    return entry[0] if entry else None


def _ready(app):
    gui_rpc._idle(app)
    if not controls.enabled(app):
        raise ValueError("Mark requires an active owned runtime")
    state = controls._state(app)
    controls._guard(app, state)
    if app._pending_input or controls._owner(state)["state"] != "idle":
        raise ValueError("Finish running and queued work before marking the screen")


@dataclass
class Capture(jobs.Owner):
    draft: str = ""
    previous: object = None
    source: tuple = ()
    plan: object = None
    result: object = None
    candidate: object = None
    applied: bool = False


def _check_review(app, capture):
    capture.check(app, capture.binding)
    current = _source(app)
    if (current[0] is not capture.source[0] or current[1] is not capture.source[1]
            or current[2:] != capture.source[2:] or capture.plan != _plan(app)):
        raise ValueError("The reviewed marker, launcher or image preferences changed")
    if _prior(work.bridge(app)) != capture.previous:
        raise ValueError("The composer attachment changed after the Mark review")


def _command(capture):
    return [capture.plan["launcher"]["path"], "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", capture.plan["script"]["path"], "-Interactive", "-HandoffFile",
            str(capture.directory / "mark.json"), "-Color", "cyan"]


def _text(value, limit):
    if (type(value) is not str or len(value.encode("utf-8")) > limit
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} and c not in "\n\t" for c in value)):
        raise ValueError("Mark text contains unsupported controls or exceeds its limit")
    return value


def append(draft, message):
    text = draft + (" " if draft else "") + message
    if len(text) > MAX_DRAFT:
        raise ValueError("The complete marked composer text exceeds 16384 characters")
    return text


def _captured(capture, value):
    from PIL import Image

    if type(value) is not dict:
        raise ValueError("Invalid screen marker handoff")
    if set(value) == {"cancelled"} and value["cancelled"] is True:
        capture.status = "cancelled"
        return
    if set(value) != {"x", "y", "mon", "mon_x", "mon_y", "png", "text"}:
        raise ValueError("Invalid screen marker handoff fields")
    path = capture.directory / "mark.png"
    if type(value["png"]) is not str or value["png"] != str(path):
        raise ValueError("Marker image escaped its exact private handoff")
    note = _text(value["text"].replace("\r\n", "\n") if type(value["text"]) is str else value["text"], MAX_NOTE)
    for key in ("x", "y", "mon", "mon_x", "mon_y"):
        if type(value[key]) is not int:
            raise ValueError("Marker coordinates must be integers")
    if (not -(2**31) <= value["x"] < 2**31 or not -(2**31) <= value["y"] < 2**31
            or not 0 <= value["mon"] < 256):
        raise ValueError("Marker coordinates exceed their bounds")
    details, encoded = native_image.decode_image("mark.png", read(path, native_image.MAX_FILE), ["PNG"])
    if not (0 <= value["mon_x"] < details["width"] and 0 <= value["mon_y"] < details["height"]):
        raise ValueError("Marker coordinates are outside the original captured monitor")
    sx = value["mon_x"] * details["sent_width"] // details["width"]
    sy = value["mon_y"] * details["sent_height"] // details["height"]
    message = (
        f"[mark] I marked the screen at ({value['x']},{value['y']}) — monitor {value['mon']}, "
        f"monitor-local ({value['mon_x']},{value['mon_y']}) in the original "
        f"{details['width']}×{details['height']} monitor image. "
        f"The attached image shows that whole monitor with the ring at approximately ({sx},{sy}) "
        f"in its delivered {details['sent_width']}×{details['sent_height']} pixels. "
        "Respond to what I am pointing at."
    )
    if note:
        message += " My note: " + note.replace("\n", " ").replace("\t", " ")
    review = {"version": 1, "kind": "image_review", **capture.binding,
              "token": os.urandom(32).hex(), "delivery": capture.plan["delivery"], **details}
    with Image.open(io.BytesIO(base64.b64decode(encoded, validate=True)), formats=["PNG"]) as image:
        preview = {"version": 1, "kind": "image_preview", **capture.binding,
                   "token": review["token"], "png_sha256": review["png_sha256"],
                   **native_image.thumbnail(image)}
    capture.candidate = (review, encoded)
    capture.result = {"coordinates": {key: value[key] for key in ("x", "y", "mon", "mon_x", "mon_y")},
                      "note": note, "message": message, "image": review, "preview": preview}
    capture.status = "ready"


async def _run(capture):
    try:
        result = await jobs.run(capture, _command(capture), timeout=SECONDS, result_file="mark.json")
        if capture.cancel.is_set():
            raise InterruptedError("Mark cancelled")
        _captured(capture, result)
    except ttyguard.OwnedTreeUnsettled:
        capture.status, capture.unsettled = "failed", True
    except InterruptedError:
        capture.status = "cancelled"
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError):
        capture.status = "failed"


def _result(capture, status=None):
    return {"version": 1, "kind": "mark", **capture.binding,
            "capture": capture.token, "draft_sha256": digest(capture.draft),
            "previous": capture.previous, "plan": capture.plan, "status": status or capture.status,
            "result": capture.result,
            "result_sha256": work.digest(work.encoded(capture.result)) if capture.result else None,
            "applied": capture.applied}


def blocking(app):
    return getattr(app, ATTRIBUTE, None) is not None


async def retire(app, *, closing=False):
    await jobs.retire(app, closing=closing, attribute=ATTRIBUTE)


def prepare_publication(app, cmd, result):
    return jobs.prepare_publication(app, cmd, result, attribute=ATTRIBUTE, operation=OPERATION)


async def dispatch(app, cmd):
    bridge, binding = read_binding(app, cmd, OPERATION, "action", "capture", "draft",
                                  "draft_sha256", "previous", "result_sha256")
    action = cmd["action"]
    if action not in {"review", "start", "status", "apply", "close"}:
        raise ValueError("Unknown native Mark action")
    if type(cmd["draft_sha256"]) is not str or re.fullmatch("[0-9a-f]{64}", cmd["draft_sha256"]) is None:
        raise ValueError("Mark requires the exact composer draft")
    capture = getattr(app, ATTRIBUTE, None)
    if action == "review":
        if reason := controls.mutation_hold(app):
            raise ValueError(reason)
        _ready(app)
        draft = _text(cmd["draft"], MAX_DRAFT * 4)
        if len(draft) > MAX_DRAFT or digest(draft) != cmd["draft_sha256"]:
            raise ValueError("Mark draft differs from the complete composer")
        if (capture is not None or cmd["capture"] is not None or cmd["result_sha256"] is not None
                or cmd["previous"] != _prior(bridge)):
            raise ValueError("Finish the previous Mark or refresh the composer attachment")
        source, plan = _source(app), _plan(app)
        capture = Capture(binding, (app._agent_session, app.store, app.backend, app.model_id),
                          work.ident(), "", "mark", threading.Event(), status="review",
                          draft=draft, previous=cmd["previous"], source=source, plan=plan)
        setattr(app, ATTRIBUTE, capture)
        return _result(capture)
    if (capture is None or cmd["capture"] != capture.token or cmd["draft"] is not None
            or cmd["draft_sha256"] != digest(capture.draft) or cmd["previous"] != capture.previous
            or capture.release_command is not None):
        raise ValueError("Mark review is stale or awaits its exact Close publication")
    capture.check(app, binding)
    if action != "apply" and cmd["result_sha256"] is not None:
        raise ValueError("Unexpected Mark result identity")
    if action == "close":
        capture.release_command, capture.release_task = work.encoded(cmd), work._task()
        await retire(app)
        if capture.applied and getattr(bridge, "composer_image", None) != capture.candidate:
            raise ValueError("Applied Mark image changed before the exact Close readback")
        result = _result(capture, "closed")
        capture.release_result, capture.release_raw = result, work.encoded(result)
        return result
    if action == "start":
        if capture.status != "review" or capture.task is not None:
            raise ValueError("This Mark overlay has already started")
        _ready(app)
        _check_review(app, capture)
        directory = _unlinked(app._agent_session.store.data_root / "recordings")
        directory.mkdir(parents=True, exist_ok=True)
        capture.temporary = tempfile.TemporaryDirectory(prefix="mark-", dir=directory)
        capture.directory = Path(capture.temporary.name)
        capture.status = "running"
        capture.task = asyncio.create_task(_run(capture))
    elif action == "apply":
        if (capture.status != "ready" or capture.task is None or not capture.task.done()
                or capture.applied or cmd["result_sha256"] != work.digest(work.encoded(capture.result))):
            raise ValueError("Review this completed Mark result before applying it")
        _ready(app)
        _check_review(app, capture)
        message = append(capture.draft, capture.result["message"])
        # Count the whole eventual composer, image bytes, saved transcript and context.
        native_image.check_inline_budget(app, capture.candidate[1], message)
        bridge.check()
        bridge.composer_image = capture.candidate
        capture.applied, capture.status = True, "applied"
    return _result(capture)
