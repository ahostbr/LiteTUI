"""Shared retirement and exact release publication for owned Voice helpers."""

from __future__ import annotations

import asyncio
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from litetui import background_input as work
from litetui import native_runtime_controls as controls
from litetui import ttyguard
from litetui.native_helper_cleanup import remove_directory

DIRECTORY_RELEASE_WAIT_S = 10.0
MAX_BYTES = 65536


@dataclass
class Owner:
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
            raise ValueError("Voice operation belongs to another owned runtime")
        bridge = work.bridge(app)
        if (
            bridge is None
            or not bridge.active
            or bridge.instance != self.binding["work_instance"]
        ):
            raise ValueError("Voice operation belongs to another Work instance")
        bridge.check()
        app._validate_owned_execution()


async def retire(
    app, *, closing=False, attribute, wait_seconds=DIRECTORY_RELEASE_WAIT_S
):
    catalog = getattr(app, attribute, None)
    if catalog is None:
        return
    catalog.cancel.set()
    if catalog.task is not None:
        from litetui.shutdown_wait import join_task

        await join_task(catalog.task)
        if not catalog.task.done():
            raise RuntimeError("Voice operation helper has not retired")
    if catalog.unsettled:
        raise RuntimeError("Voice operation descendant retirement is unproven")
    if catalog.temporary is not None:
        await remove_directory(catalog.temporary, wait_seconds=wait_seconds)
        catalog.temporary = None
    if closing:
        bridge = work.bridge(app)
        if bridge is None or not bridge.closing:
            raise RuntimeError(
                "Voice operation shutdown requires its closing Work owner"
            )
        setattr(app, attribute, None)


@dataclass
class Publication:
    app: object
    catalog: Owner
    task: asyncio.Task | None
    command: bytes
    result: dict
    raw: bytes
    attribute: str
    used: bool = False


def prepare_publication(app, cmd, result, *, attribute, operation):
    if cmd.get("type") != operation or cmd.get("action") != "close":
        return None
    catalog = getattr(app, attribute, None)
    if (
        catalog is None
        or catalog.release_command != work.encoded(cmd)
        or catalog.release_task is not work._task()
        or catalog.release_result is not result
        or catalog.release_raw != work.encoded(result)
        or catalog.publication is not None
    ):
        raise ValueError("Voice operation close publication is foreign or changed")
    catalog.check(app, catalog.binding)
    ticket = Publication(
        app,
        catalog,
        work._task(),
        catalog.release_command,
        result,
        catalog.release_raw,
        attribute,
    )
    catalog.publication = ticket
    return ticket


def finish_publication(app, ticket):
    attribute = ticket.attribute if isinstance(ticket, Publication) else ""
    if (
        not isinstance(ticket, Publication)
        or ticket.app is not app
        or ticket.used
        or ticket.task is not work._task()
        or getattr(app, attribute, None) is not ticket.catalog
        or ticket.catalog.publication is not ticket
        or work.encoded(ticket.result) != ticket.raw
        or ticket.catalog.release_command != ticket.command
        or ticket.catalog.release_result is not ticket.result
    ):
        raise ValueError("Voice operation close lost its exact publication owner")
    ticket.catalog.check(app, ticket.catalog.binding)
    ticket.used = True
    ticket.catalog.publication = None
    setattr(app, attribute, None)
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


async def run(owner, command, *, timeout, result_file="result.json"):
    worker = asyncio.create_task(
        asyncio.to_thread(
            ttyguard.run_owned_writer,
            command,
            cwd=owner.directory,
            env=dict(os.environ),
            timeout=timeout,
            cancel=owner.cancel,
        )
    )
    try:
        try:
            code = await asyncio.shield(worker)
        except asyncio.CancelledError:
            owner.cancel.set()
            from litetui.shutdown_wait import join_task

            await join_task(worker)
            raise InterruptedError("Voice operation cancelled")
        if code != 0:
            raise ValueError("Voice helper failed")
        raw = work._read(owner.directory / result_file, MAX_BYTES)
        if raw is None:
            raise ValueError("Voice helper returned no bounded result")
        return work.strict_json(raw)
    except ttyguard.OwnedTreeUnsettled:
        owner.unsettled = True
        raise
