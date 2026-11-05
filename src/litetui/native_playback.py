"""Reviewed playback of one exact source-owned saved assistant response."""
from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import native_history as history
from litetui import native_runtime_controls as controls
from litetui import ttyguard, voice_backend

OPERATION = "gui.voice.native_playback"
SELECTION = ("snapshot", "block_index", "block_id", "message_index", "message_sha256")
MAX_TEXT = 16384
DIRECTORY_RELEASE_WAIT_S = 10.0


@dataclass
class Playback:
    binding: dict
    identity: tuple
    instance: str
    token: str
    text: str
    preferences: dict
    cancel: threading.Event
    temporary: object = None
    directory: Path | None = None
    task: asyncio.Task | None = None
    status: str = "review"
    unsettled: bool = False
    release_command: bytes | None = None
    release_task: asyncio.Task | None = None
    release_result: dict | None = None
    release_raw: bytes | None = None
    publication: Publication | None = None

    def check(self, app):
        bridge = work.bridge(app)
        if (self.identity != (app._agent_session, app.store, app.backend, app.model_id)
                or bridge is None or not bridge.active or bridge.instance != self.instance):
            raise ValueError("Playback belongs to a different owned Work instance")
        bridge.check()
        app._validate_owned_execution()


def _preferences(app):
    settings = app.settings
    if settings.tts_enabled is not True:
        raise ValueError("Enable response speech in Voice settings before playback")
    if settings.tts_engine not in voice_backend.ENGINES:
        raise ValueError("Choose a supported speech engine in Voice settings")
    if type(settings.tts_timeout) is not int or not 1 <= settings.tts_timeout <= 3600:
        raise ValueError("Native playback needs a configured timeout between 1 and 3600 seconds")
    preferred = settings.tts_litesuite_first
    if type(preferred) is not bool:
        raise ValueError("Invalid LiteSuite speech preference")
    voice = (settings.tts_edge_voice if settings.tts_engine == "edge" else settings.tts_voice) or ""
    if type(voice) is not str or len(voice.encode()) > 512 or not all(c.isprintable() for c in voice):
        raise ValueError("Configured speech voice exceeds its display limit")
    return {"engine": settings.tts_engine, "voice": voice, "timeout": settings.tts_timeout,
            "litesuite_first": preferred}


def _guard(app):
    if not controls.enabled(app):
        raise ValueError("Playback requires active runtime controls")
    state = controls._state(app)
    controls._guard(app, state)
    if state.attempt is not None and not state.attempt.verified:
        raise ValueError("A runtime control owns its exact result and readback publication")
    if getattr(app, "_native_model_lifecycle_hold", False) or getattr(app, "_backend_transition_error", None):
        raise ValueError("Resolve the owned model operation before response playback")
    if controls._owner(state)["state"] != "idle" or app._pending_input:
        raise ValueError("Finish the running reply and pending input before playback")
    if getattr(app, "_native_dictation", None) is not None:
        raise ValueError("Close dictation before response playback")


def _selected(app, cmd):
    descriptor, _capture, index = history._collect(app, cmd)
    if descriptor["snapshot"] != cmd["snapshot"] or not descriptor["materialized"]:
        raise ValueError("Saved response changed. Refresh history before speaking")
    number = cmd["block_index"]
    if type(number) is not int or not 0 <= number < len(index.view.blocks):
        raise ValueError("Invalid response block")
    block = index.view.blocks[number]
    if (block.kind != "assistant" or block.role != "assistant" or block.state != "complete"
            or block.block_id != cmd["block_id"] or type(cmd["message_index"]) is not int
            or block.message_index != cmd["message_index"] or block.message_sha256 != cmd["message_sha256"]):
        raise ValueError("Select a complete saved assistant answer")
    text = voice_backend.clean_for_speech(block.content, limit=len(block.content))
    if not text or len(text.encode()) > MAX_TEXT or not all(c.isprintable() for c in text):
        raise ValueError("This response has no bounded speakable text")
    return text


def _command(directory):
    return [sys.executable, "-B", "-m", "litetui.native_playback_worker", str(directory)]


def _status(playback):
    if playback.task is not None and not playback.task.done():
        raw = work._read(playback.directory / "route", 32)
        if raw == b"local":
            return "local"
        if raw == b"queued":
            return "queued"
        if raw == b"uncertain":
            return "uncertain"
        return "starting"
    return playback.status


def _result(playback, *, closed=False, outcome=None):
    return {"version": 1, "kind": "response_playback", **playback.binding,
            "playback": playback.token, "text": playback.text,
            "text_sha256": hashlib.sha256(playback.text.encode()).hexdigest(),
            "preferences": playback.preferences, "status": "closed" if closed else _status(playback),
            "outcome": outcome}


async def _run(playback):
    environment = dict(os.environ)
    environment.update({key: str(playback.directory) for key in ("TMP", "TEMP", "TMPDIR")})
    worker = asyncio.create_task(asyncio.to_thread(ttyguard.run_owned_writer, _command(playback.directory),
        cwd=playback.directory, env=environment, timeout=playback.preferences["timeout"] + 20, cancel=playback.cancel))
    try:
        try:
            code = await asyncio.shield(worker)
        except asyncio.CancelledError:
            playback.cancel.set()
            from litetui.shutdown_wait import join_task
            await join_task(worker)
            raise InterruptedError("Playback cancelled")
        if code != 0:
            raise ValueError("Playback helper failed")
        raw = work._read(playback.directory / "result.json", 1024)
        result = work.strict_json(raw)
        if set(result) != {"status"} or result["status"] not in {"queued", "uncertain", "unavailable", "local_finished", "failed"}:
            raise ValueError("Invalid playback result")
        playback.status = result["status"]
    except ttyguard.OwnedTreeUnsettled:
        playback.status, playback.unsettled = "failed", True
    except InterruptedError:
        playback.status = "stopped"
    except (OSError, ValueError, RuntimeError, TypeError, KeyError, subprocess.SubprocessError):
        # An interrupted acknowledgement cannot prove the remote request was absent.
        playback.status = "uncertain" if playback.preferences["litesuite_first"] else "failed"


async def _remove_directory(temporary):
    """Remove the private helper directory once its job has drained.

    The job can report no active process while Windows still pins the helper's working
    directory (measured up to ~450 ms on an idle machine, longer under load). That sharing
    violation is transient, so only it is retried, and only for DIRECTORY_RELEASE_WAIT_S;
    a lock that never lifts, or any other error, still fails the close or shutdown."""
    deadline = time.monotonic() + DIRECTORY_RELEASE_WAIT_S
    while True:
        try:
            temporary.cleanup()
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
        await asyncio.sleep(0.025)


async def retire(app, *, closing=False):
    playback = getattr(app, "_native_playback", None)
    if playback is None:
        return
    playback.cancel.set()
    if playback.task is not None:
        from litetui.shutdown_wait import join_task
        await join_task(playback.task)
        if not playback.task.done():
            raise RuntimeError("Playback helper has not retired")
    if playback.unsettled:
        raise RuntimeError("Local playback descendant retirement is unproven")
    if playback.temporary is not None:
        await _remove_directory(playback.temporary)
        playback.temporary = None
    if closing:
        bridge = work.bridge(app)
        if bridge is None or not bridge.closing:
            raise RuntimeError("Playback shutdown release requires a closing Work owner")
        app._native_playback = None


@dataclass
class Publication:
    app: object
    playback: Playback
    task: asyncio.Task | None
    command: bytes
    result: dict
    raw: bytes
    used: bool = False


def prepare_publication(app, cmd, result):
    if cmd.get("type") != OPERATION or cmd.get("action") != "close":
        return None
    playback = getattr(app, "_native_playback", None)
    if (playback is None or playback.release_command != work.encoded(cmd)
            or playback.release_task is not work._task() or playback.release_result is not result
            or playback.release_raw != work.encoded(result) or playback.publication is not None):
        raise ValueError("Playback release publication is foreign, changed or consumed")
    playback.check(app)
    ticket = Publication(app, playback, work._task(), playback.release_command, result, playback.release_raw)
    playback.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (not isinstance(ticket, Publication) or ticket.app is not app or ticket.used
            or ticket.task is not work._task() or getattr(app, "_native_playback", None) is not ticket.playback
            or ticket.playback.publication is not ticket or work.encoded(ticket.result) != ticket.raw
            or ticket.playback.release_command != ticket.command or ticket.playback.release_result is not ticket.result):
        raise ValueError("Playback release lost its exact one-use publication owner")
    ticket.playback.check(app)
    ticket.used = True
    ticket.playback.publication = None
    app._native_playback = None
    controls.signal_stop(app)
    bridge = work.bridge(app)
    wake = getattr(app, "_flush_pending_input", None)
    if wake is not None and getattr(app, "_pending_input", ()) and bridge is not None and not bridge.closing:
        app.call_later(wake)


def blocking(app):
    return getattr(app, "_native_playback", None) is not None


async def dispatch(app, cmd):
    keys = {"type", "id", "version", *history.COMMON, *SELECTION, "action", "playback", "text_sha256"}
    if set(cmd) != keys or cmd["type"] != OPERATION:
        raise ValueError("Unexpected playback request fields")
    history._common(app, cmd)
    action = cmd["action"]
    if action not in ("review", "start", "status", "close"):
        raise ValueError("Unknown playback action")
    binding = {key: cmd[key] for key in (*history.COMMON, *SELECTION)}
    identity = (app._agent_session, app.store, app.backend, app.model_id)
    playback = getattr(app, "_native_playback", None)
    if action == "review":
        _guard(app)
        if playback is not None or cmd["playback"] is not None or cmd["text_sha256"] is not None:
            raise ValueError("Close the previous playback first")
        text, preferences = _selected(app, cmd), _preferences(app)
        playback = Playback(binding, identity, work.bridge(app).instance, work.ident(), text, preferences, threading.Event())
        app._native_playback = playback
        return _result(playback)
    if (playback is None or playback.binding != binding or playback.identity != identity
            or playback.token != cmd["playback"]
            or hashlib.sha256(playback.text.encode()).hexdigest() != cmd["text_sha256"]):
        raise ValueError("Playback is stale or belongs to another selected response")
    playback.check(app)
    if playback.release_command is not None:
        raise ValueError("Playback release awaits its exact result publication")
    if action == "start":
        _guard(app)
        if playback.task is not None or playback.status != "review":
            raise ValueError("This playback was already attempted")
        if _selected(app, cmd) != playback.text or _preferences(app) != playback.preferences:
            raise ValueError("Response or voice settings changed after review")
        directory = app._agent_session.store.data_root / "recordings"
        directory.mkdir(parents=True, exist_ok=True)
        playback.temporary = tempfile.TemporaryDirectory(prefix="speech-", dir=directory)
        playback.directory = Path(playback.temporary.name)
        (playback.directory / "parameters.json").write_bytes(work.encoded({**playback.preferences, "text": playback.text}))
        playback.status = "starting"
        playback.task = asyncio.create_task(_run(playback))
    elif action == "close":
        playback.release_command, playback.release_task = work.encoded(cmd), work._task()
        outcome = _status(playback)
        if outcome == "starting":
            outcome = "uncertain" if playback.preferences["litesuite_first"] else "stopped"
        elif outcome == "local":
            outcome = "stopped"
        await retire(app)
        result = _result(playback, closed=True, outcome=outcome)
        playback.release_result, playback.release_raw = result, work.encoded(result)
        return result
    return _result(playback)
