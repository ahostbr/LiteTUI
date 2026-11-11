"""Explicit device-name discovery under one joined, publication-held Work owner."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import native_dictation, native_voice_preferences, ttyguard
from litetui import native_runtime_controls as controls
from litetui.native_context import read_binding
from litetui.native_helper_cleanup import remove_directory
from litetui.native_voice_catalog_worker import MAX_BYTES, names

OPERATION = "gui.voice.native_catalog"
TIMEOUT = 20
DIRECTORY_RELEASE_WAIT_S = 10.0


@dataclass
class Catalog:
    binding: dict
    identity: tuple
    token: str
    revision: str
    field: str
    cancel: threading.Event
    temporary: object = None
    directory: Path | None = None
    task: asyncio.Task | None = None
    status: str = "starting"
    items: tuple = ()
    unsettled: bool = False
    release_command: bytes | None = None
    release_task: asyncio.Task | None = None
    release_result: dict | None = None
    release_raw: bytes | None = None
    publication: object = None

    def check(self, app, binding):
        if self.binding != binding or self.identity != (
            app._agent_session,
            app.store,
            app.backend,
            app.model_id,
        ):
            raise ValueError("Voice catalog belongs to another owned runtime")
        bridge = work.bridge(app)
        if (
            bridge is None
            or not bridge.active
            or bridge.instance != self.binding["work_instance"]
        ):
            raise ValueError("Voice catalog belongs to another Work instance")
        bridge.check()
        app._validate_owned_execution()


def blocking(app):
    return getattr(app, "_native_voice_catalog", None) is not None


def _result(catalog, status=None):
    return {
        "version": 1,
        "kind": "voice_catalog",
        "scope": "device",
        **catalog.binding,
        "catalog": catalog.token,
        "before": catalog.revision,
        "field": catalog.field,
        "status": status or catalog.status,
        "items": list(catalog.items),
    }


def _command(directory):
    return [
        sys.executable,
        "-B",
        "-m",
        "litetui.native_voice_catalog_worker",
        str(directory),
    ]


async def _run(catalog):
    worker = asyncio.create_task(
        asyncio.to_thread(
            ttyguard.run_owned_writer,
            _command(catalog.directory),
            cwd=catalog.directory,
            env=dict(os.environ),
            timeout=TIMEOUT,
            cancel=catalog.cancel,
        )
    )
    try:
        try:
            code = await asyncio.shield(worker)
        except asyncio.CancelledError:
            catalog.cancel.set()
            from litetui.shutdown_wait import join_task

            await join_task(worker)
            raise InterruptedError("Voice discovery cancelled")
        if code != 0:
            raise ValueError("Voice discovery helper failed")
        raw = work._read(catalog.directory / "result.json", MAX_BYTES)
        if raw is None:
            raise ValueError("Voice discovery returned no result")
        result = work.strict_json(raw)
        if set(result) != {"items"}:
            raise ValueError("Invalid voice catalog result")
        selected = names(result["items"])
        if list(selected) != result["items"]:
            raise ValueError("Voice helper returned duplicate names")
        catalog.items, catalog.status = selected, "ready"
    except ttyguard.OwnedTreeUnsettled:
        catalog.status, catalog.unsettled = "failed", True
    except InterruptedError:
        catalog.status = "cancelled"
    except (
        OSError,
        ValueError,
        RuntimeError,
        TypeError,
        KeyError,
        subprocess.SubprocessError,
    ):
        catalog.status = "failed"


async def retire(app, *, closing=False):
    catalog = getattr(app, "_native_voice_catalog", None)
    if catalog is None:
        return
    catalog.cancel.set()
    if catalog.task is not None:
        from litetui.shutdown_wait import join_task

        await join_task(catalog.task)
        if not catalog.task.done():
            raise RuntimeError("Voice catalog helper has not retired")
    if catalog.unsettled:
        raise RuntimeError("Voice catalog descendant retirement is unproven")
    if catalog.temporary is not None:
        await remove_directory(catalog.temporary, wait_seconds=DIRECTORY_RELEASE_WAIT_S)
        catalog.temporary = None
    if closing:
        bridge = work.bridge(app)
        if bridge is None or not bridge.closing:
            raise RuntimeError("Voice catalog shutdown requires its closing Work owner")
        app._native_voice_catalog = None


@dataclass
class Publication:
    app: object
    catalog: Catalog
    task: asyncio.Task | None
    command: bytes
    result: dict
    raw: bytes
    used: bool = False


def prepare_publication(app, cmd, result):
    if cmd.get("type") != OPERATION or cmd.get("action") != "close":
        return None
    catalog = getattr(app, "_native_voice_catalog", None)
    if (
        catalog is None
        or catalog.release_command != work.encoded(cmd)
        or catalog.release_task is not work._task()
        or catalog.release_result is not result
        or catalog.release_raw != work.encoded(result)
        or catalog.publication is not None
    ):
        raise ValueError("Voice catalog close publication is foreign or changed")
    catalog.check(app, catalog.binding)
    ticket = Publication(
        app, catalog, work._task(), catalog.release_command, result, catalog.release_raw
    )
    catalog.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (
        not isinstance(ticket, Publication)
        or ticket.app is not app
        or ticket.used
        or ticket.task is not work._task()
        or getattr(app, "_native_voice_catalog", None) is not ticket.catalog
        or ticket.catalog.publication is not ticket
        or work.encoded(ticket.result) != ticket.raw
        or ticket.catalog.release_command != ticket.command
        or ticket.catalog.release_result is not ticket.result
    ):
        raise ValueError("Voice catalog close lost its exact publication owner")
    ticket.catalog.check(app, ticket.catalog.binding)
    ticket.used = True
    ticket.catalog.publication = None
    app._native_voice_catalog = None
    controls.signal_stop(app)
    bridge = work.bridge(app)
    wake = getattr(app, "_flush_pending_input", None)
    if (
        wake is not None
        and getattr(app, "_pending_input", ())
        and bridge is not None
        and not bridge.closing
    ):
        app.call_later(wake)


async def dispatch(app, cmd):
    _, binding = read_binding(
        app, cmd, OPERATION, "action", "catalog", "field", "revision"
    )
    action, field = cmd["action"], cmd["field"]
    if action not in ("start", "status", "close") or field not in (
        "stt_mic",
        "tts_voice",
    ):
        raise ValueError("Choose a supported voice catalog operation")
    catalog = getattr(app, "_native_voice_catalog", None)
    if action == "start":
        native_dictation._guard(app)
        current = native_voice_preferences._snapshot(app, binding)[0]
        if (
            catalog is not None
            or cmd["catalog"] is not None
            or current["revision"] != cmd["revision"]
            or not current["fields"][field]["editable"]
        ):
            raise ValueError("Refresh editable voice preferences before discovery")
        catalog = Catalog(
            binding,
            (app._agent_session, app.store, app.backend, app.model_id),
            work.ident(),
            current["revision"],
            field,
            threading.Event(),
        )
        app._native_voice_catalog = catalog
        directory = app._agent_session.store.data_root / "recordings"
        directory.mkdir(parents=True, exist_ok=True)
        catalog.temporary = tempfile.TemporaryDirectory(
            prefix="voice-catalog-", dir=directory
        )
        catalog.directory = Path(catalog.temporary.name)
        (catalog.directory / "parameters.json").write_bytes(
            work.encoded({"field": field})
        )
        catalog.task = asyncio.create_task(_run(catalog))
        return _result(catalog)
    if (
        catalog is None
        or cmd["catalog"] != catalog.token
        or cmd["revision"] != catalog.revision
        or field != catalog.field
        or catalog.release_command is not None
    ):
        raise ValueError("Voice catalog is stale or awaits its close publication")
    catalog.check(app, binding)
    if action == "close":
        catalog.release_command, catalog.release_task = work.encoded(cmd), work._task()
        await retire(app)
        result = _result(catalog, "closed")
        catalog.release_result, catalog.release_raw = result, work.encoded(result)
        return result
    return _result(catalog)
