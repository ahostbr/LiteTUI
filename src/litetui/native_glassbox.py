"""Review the registered local Glassbox effect and independently sample its outcome."""

from __future__ import annotations

import secrets
import stat
import sys
from dataclasses import dataclass

from litetui import gui_rpc
from litetui import native_runtime_controls as controls
from litetui.agent_preparation import await_preparation
from litetui.agent_store import _unlinked
from litetui.native_context import read_binding
from litetui.plugins import glassbox_plugin as glassbox

PREFIX = "gui.glassbox.native_"


@dataclass
class Review:
    token: str
    action: str
    before: dict
    stamp: tuple
    after: dict | None = None
    apply_id: str | None = None
    status: str | None = None


def _path(app):
    root = _unlinked(app._agent_session.store.data_root)
    path = _unlinked(glassbox._RECORDER.path)
    if path != root / "artifacts" / "glassbox.jsonl":
        raise ValueError(
            "Glassbox recording path differs from the owned artifacts directory"
        )
    try:
        info = path.lstat()
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("Glassbox recording target must be a single regular file")
    text = str(path)
    if not path.is_absolute() or len(text.encode("utf-8")) > 4096 or "\x00" in text:
        raise ValueError("Glassbox recording path is unavailable")
    return text


def _sample(app):
    entry = app.plugins.commands.get("glassbox")
    server = glassbox.SERVER
    retained = getattr(app, "_glassbox_owned_server", None)
    if (
        entry is None
        or entry.owner != glassbox.PLUGIN_ID
        or entry.handler is not glassbox._cmd_glassbox
        or glassbox._RECORDER_OWNER is not app
        or not isinstance(server, glassbox.Server)
        or server._httpd is not None
        and server.owner is not app
        or retained is not None
        and retained._httpd is not None
        and retained is not server
    ):
        raise ValueError(
            "Glassbox requires this app's registered builtin and retained listener"
        )
    port, revision = glassbox.DEFAULT_PORT, glassbox._CONTROL_REVISION
    if (
        type(port) is not int
        or not 1 <= port <= 65535
        or type(revision) is not int
        or not 0 <= revision <= 2**53 - 1
        or type(glassbox._ENABLED) is not bool
    ):
        raise ValueError("Glassbox cached control state is unavailable")
    sample = {
        "recording": glassbox._ENABLED,
        **server.snapshot(),
        "revision": revision,
        "record_path": _path(app),
        "listen_port": port,
    }
    stamp = (
        entry,
        entry.handler,
        server,
        glassbox.HUB,
        glassbox._RECORDER,
        server._httpd,
        server._thread,
    )
    return sample, stamp


def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    fields = {
        "snapshot": (),
        "review": ("action",),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    bridge, binding = read_binding(app, cmd, PREFIX + op, *fields[op])
    before, stamp = _sample(app)
    common = {"version": 1, **binding}
    review = getattr(bridge, "glassbox_review", None)
    if op in {"review", "apply"}:
        bridge.glassbox_review = None
        gui_rpc._idle(app)
        if not controls.enabled(app):
            raise ValueError("Glassbox controls require negotiated runtime ownership")
        controls._guard(app, controls._state(app))
        if controls.mutation_hold(app):
            raise ValueError("Runtime controls are still settling")
    if op == "snapshot":
        return {**common, "kind": "glassbox_snapshot", **before}
    if op == "review":
        if before["revision"] >= 2**53 - 1:
            raise ValueError("Glassbox control revision is exhausted")
        action = cmd["action"]
        if type(action) is not str or action not in {"start", "stop"}:
            raise ValueError("Choose an explicit Glassbox Start or Stop")
        if action == "start" and before["state"] == "stopping":
            raise ValueError(
                "Stop and join the retained listener before starting again"
            )
        review = Review(secrets.token_hex(32), action, before, stamp)
        bridge.glassbox_review = review
        return {
            **common,
            "kind": "glassbox_review",
            **before,
            "action": action,
            "token": review.token,
        }
    if not isinstance(review, Review) or cmd["token"] != review.token:
        raise ValueError("Review this exact Glassbox owner and state again")
    if op == "apply":
        if (
            review.apply_id is not None
            or before != review.before
            or any(
                current is not captured
                for current, captured in zip(stamp, review.stamp, strict=True)
            )
        ):
            raise ValueError(
                "The reviewed Glassbox owner, listener or recording state changed"
            )
        # The registered source handler owns both effects and their normal announcement.
        review.apply_id = cmd["id"]
        bridge.glassbox_review = review
        handler_failed = False
        try:
            app.plugins.commands["glassbox"].handler(app, "glassbox", review.action)
        except Exception as exc:  # noqa: BLE001 - retain exact effects after a partial handler failure
            from litetui import runtime_log

            runtime_log.record_error(
                "glassbox_control_failed", detail=type(exc).__name__
            )
            handler_failed = True
        after, _ = _sample(app)
        review.after = after
        expected = (
            (
                after["recording"]
                and after["state"] == "running"
                and after["port"] == after["listen_port"]
            )
            if review.action == "start"
            else (not after["recording"] and after["state"] == "stopped")
        )
        if handler_failed:
            review.status = "failed"
        elif expected:
            review.status = "applied"
        elif review.action == "start":
            review.status = "partial"
        else:
            review.status = "retained"
        bridge.glassbox_review = review
        return {
            **common,
            "kind": "glassbox_applied",
            **after,
            "action": review.action,
            "token": review.token,
            "apply_id": review.apply_id,
            "status": review.status,
        }
    if (
        review.apply_id is None
        or cmd["apply_id"] != review.apply_id
        or before != review.after
        or any(
            current is not captured
            for current, captured in zip(stamp[:5], review.stamp[:5], strict=True)
        )
    ):
        raise ValueError(
            "Glassbox independent readback differs from the recorded outcome"
        )
    return {
        **common,
        "kind": "glassbox_verified",
        **before,
        "action": review.action,
        "token": review.token,
        "apply_id": review.apply_id,
        "status": review.status,
    }


async def settle_before_shutdown(app):
    # Cleanup still belongs to the retained app if its command was unloaded later.
    module = sys.modules.get("litetui.plugins.glassbox_plugin")
    if module is not None:
        await await_preparation(lambda: module.stop_owned(app))
