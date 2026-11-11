"""Explicit device-name discovery under one joined, publication-held Work owner."""

from __future__ import annotations

import asyncio
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from litetui import background_input as work
from litetui import native_dictation, native_voice_preferences, ttyguard
from litetui import native_voice_jobs as jobs
from litetui.native_context import read_binding
from litetui.native_voice_catalog_worker import names

OPERATION = "gui.voice.native_catalog"
TIMEOUT = 20
DIRECTORY_RELEASE_WAIT_S = 10.0


Catalog = jobs.Owner
Publication = jobs.Publication

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
    try:
        result = await jobs.run(catalog, _command(catalog.directory), timeout=TIMEOUT)
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
    return await jobs.retire(app, closing=closing, attribute="_native_voice_catalog", wait_seconds=DIRECTORY_RELEASE_WAIT_S)


def prepare_publication(app, cmd, result):
    return jobs.prepare_publication(app, cmd, result, attribute="_native_voice_catalog", operation=OPERATION)


finish_publication = jobs.finish_publication

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
