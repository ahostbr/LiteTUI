"""Reviewed fallback speech, optional Edge installation and STT cache actions."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import native_dictation, native_voice_preferences
from litetui import native_voice_actions_worker as worker
from litetui import native_voice_jobs as jobs
from litetui.native_context import read_binding

OPERATION = "gui.voice.native_action"
ATTRIBUTE = "_native_voice_action"


@dataclass
class Action(jobs.Owner):
    preferences: dict | None = None
    selection: dict | None = None
    plan: dict | None = None
    result: dict | None = None
    phase: str = "review"
    applied: bool = False


def blocking(app):
    return getattr(app, ATTRIBUTE, None) is not None


def _preferences(snapshot, kind, selection=None):
    values = {key: row["effective"] for key, row in snapshot["fields"].items()}
    selection = {} if selection is None else selection
    allowed = (
        {"tts_engine", "tts_voice", "tts_edge_voice", "tts_timeout"}
        if kind == "fallback"
        else {"stt_model"}
        if kind == "stt"
        else set()
    )
    if type(selection) is not dict or set(selection) - allowed:
        raise ValueError("Unsupported Voice action draft")
    for key, value in selection.items():
        row = snapshot["fields"][key]
        if (
            not row["editable"]
            or native_voice_preferences._value(key, value) != value
            or key in native_voice_preferences.CHOICES
            and value not in native_voice_preferences.CHOICES[key]
        ):
            raise ValueError("Voice action draft is overridden or invalid")
        values[key] = value
    engine = values["tts_engine"]
    result = {
        "engine": engine,
        "voice": values["tts_edge_voice"] if engine == "edge" else values["tts_voice"],
        "timeout": values["tts_timeout"],
        "model": values["stt_model"],
    }
    if kind == "fallback" and (
        engine not in ("pyttsx3", "edge") or not 1 <= result["timeout"] <= 3600
    ):
        raise ValueError(
            "Choose an installed fallback engine and timeout from 1 to 3600 seconds"
        )
    if (
        kind == "stt"
        and result["model"] not in native_voice_preferences.CHOICES["stt_model"]
    ):
        raise ValueError("Choose a supported dictation model before downloading")
    return result


def _hash(owner):
    return work.digest(work.encoded(owner.plan)) if owner.plan is not None else None


def _result(owner, status=None):
    return {
        "version": 1,
        "kind": "voice_action",
        "scope": "device",
        **owner.binding,
        "run": owner.token,
        "before": owner.revision,
        "task": owner.field,
        "status": status or owner.status,
        "plan": owner.plan,
        "plan_sha256": _hash(owner),
        "result": owner.result,
        "applied": owner.applied,
    }


def _command(directory):
    return [
        sys.executable,
        "-B",
        "-m",
        "litetui.native_voice_actions_worker",
        str(directory),
    ]


async def _run(owner):
    try:
        timeout = (
            90
            if owner.phase == "review"
            else owner.preferences["timeout"] + 60
            if owner.field == "fallback"
            else 300
            if owner.field == "edge"
            else 1900
        )
        raw = await jobs.run(owner, _command(owner.directory), timeout=timeout)
        if owner.phase == "review":
            if set(raw) != {"plan"}:
                raise ValueError("Invalid Voice review result")
            owner.plan = worker.validate(raw["plan"], owner.field, owner.preferences)
            owner.status = "review"
        else:
            expected = {
                "kind": {
                    "fallback": "fallback_finished",
                    "edge": "edge_ready",
                    "stt": "stt_cache_ready",
                }[owner.field],
                "interpreter": owner.plan["interpreter"],
                "cache": owner.plan["cache"],
                "model": owner.plan["model"],
                "dependencies_verified": True,
            }
            if (
                set(raw) != {"result"}
                or raw["result"] != expected
                or type(raw["result"]["dependencies_verified"]) is not bool
            ):
                raise ValueError("Voice result lacks independent exact verification")
            owner.result, owner.status = expected, "verified"
    except InterruptedError:
        owner.status = "cancelled"
    except (
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ):
        owner.status = "failed"


def _launch(owner):
    (owner.directory / "result.json").unlink(missing_ok=True)
    worker.write(
        owner.directory / "parameters.json",
        {
            "action": owner.field,
            "phase": owner.phase,
            "preferences": owner.preferences,
            "plan": owner.plan,
        },
    )
    owner.task = asyncio.create_task(_run(owner))


async def retire(app, *, closing=False):
    return await jobs.retire(app, closing=closing, attribute=ATTRIBUTE)


def prepare_publication(app, cmd, result):
    return jobs.prepare_publication(
        app, cmd, result, attribute=ATTRIBUTE, operation=OPERATION
    )


async def dispatch(app, cmd):
    _, binding = read_binding(
        app,
        cmd,
        OPERATION,
        "action",
        "run",
        "task",
        "revision",
        "plan_sha256",
        "values",
    )
    action, kind = cmd["action"], cmd["task"]
    if (
        action not in ("review", "apply", "status", "close")
        or kind not in worker.PACKAGES
    ):
        raise ValueError("Choose a supported Voice action")
    owner = getattr(app, ATTRIBUTE, None)
    if action == "review":
        native_dictation._guard(app)
        snapshot = native_voice_preferences._snapshot(app, binding)[0]
        if (
            owner is not None
            or cmd["run"] is not None
            or cmd["plan_sha256"] is not None
            or cmd["revision"] != snapshot["revision"]
        ):
            raise ValueError("Refresh Voice preferences before preparing this action")
        preferences = _preferences(snapshot, kind, cmd["values"])
        owner = Action(
            binding,
            (app._agent_session, app.store, app.backend, app.model_id),
            work.ident(),
            snapshot["revision"],
            kind,
            threading.Event(),
            preferences=preferences,
            selection=dict(cmd["values"]),
            status="preparing",
        )
        setattr(app, ATTRIBUTE, owner)
        directory = app._agent_session.store.data_root / "recordings"
        directory.mkdir(parents=True, exist_ok=True)
        owner.temporary = tempfile.TemporaryDirectory(
            prefix="voice-action-", dir=directory
        )
        owner.directory = Path(owner.temporary.name)
        _launch(owner)
        return _result(owner)
    if (
        owner is None
        or cmd["run"] != owner.token
        or kind != owner.field
        or cmd["revision"] != owner.revision
        or owner.release_command is not None
        or cmd["values"] != owner.selection
    ):
        raise ValueError("Voice action is stale or awaits its exact Close publication")
    owner.check(app, binding)
    if cmd["plan_sha256"] is not None and cmd["plan_sha256"] != _hash(owner):
        raise ValueError("Voice action plan has changed")
    if action == "apply":
        if (
            owner.status != "review"
            or owner.applied
            or owner.task is None
            or not owner.task.done()
            or cmd["plan_sha256"] != _hash(owner)
        ):
            raise ValueError("Review this exact Voice action before Apply")
        snapshot = native_voice_preferences._snapshot(app, binding)[0]
        if (
            snapshot["revision"] != owner.revision
            or _preferences(snapshot, kind, owner.selection) != owner.preferences
        ):
            raise ValueError("Voice preferences changed after review")
        owner.applied, owner.phase, owner.status = True, "apply", "running"
        _launch(owner)
        return _result(owner)
    if action == "close":
        owner.release_command, owner.release_task = work.encoded(cmd), work._task()
        await retire(app)
        result = _result(owner, "closed")
        owner.release_result, owner.release_raw = result, work.encoded(result)
        return result
    return _result(owner)
