"""Explicitly hold or resume future host-managed model rounds in this runtime."""
from __future__ import annotations

import secrets
from dataclasses import dataclass

from litetui import native_runtime_controls as controls
from litetui.native_context import read_binding
from litetui.plugins import misc

PREFIX = "gui.prompt.native_pause_"
MAX_REVISION = 2**53 - 1


@dataclass
class Review:
    token: str
    origin: dict | None
    before: bool
    revision: int
    applied_by: str | None = None


def _sample(app, state):
    owner = controls._owner(state)
    stage = owner["state"]
    origin = owner["origin"]
    if stage == "active" and app.paused:
        worker, _ = state.bridge.executions[origin["execution_id"]]
        if getattr(app, "_chat_pause_waiter", None) is not None and app._chat_pause_waiter is worker._task:
            stage = "held"
    revision = getattr(app, "_chat_pause_revision", 0)
    if type(app.paused) is not bool or type(revision) is not int or not 0 <= revision <= MAX_REVISION:
        raise ValueError("Chat pause state is unavailable")
    return {"paused": app.paused, "revision": revision, "stage": stage, "origin": origin}


def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    fields = {"review": ("origin",), "apply": ("token",), "readback": ("token", "apply_id")}
    bridge, binding = read_binding(app, cmd, PREFIX + op, *fields[op])
    entry = app.plugins.commands.get("/pause")
    if (not controls.enabled(app) or bridge.provider != "host" or entry is None
            or entry.owner != "misc" or entry.handler is not misc._cmd_pause):
        raise ValueError("Chat pause requires the registered /pause command and a host-managed backend")
    state = controls._state(app)
    review = getattr(bridge, "round_pause", None)
    if op != "readback":
        bridge.round_pause = None
        controls._guard(app, state)
        if controls.mutation_hold(app):
            raise ValueError("Runtime controls are still settling")
    sample = _sample(app, state)
    common = {"version": 1, **binding, **sample}
    if op == "review":
        if (sample["origin"] != cmd["origin"] or sample["stage"] == "terminal"
                or sample["origin"] is not None and getattr(app, "_stop_requested", False)
                or sample["revision"] >= MAX_REVISION):
            raise ValueError("The foreground round changed; review pause again")
        review = Review(secrets.token_hex(32), sample["origin"], sample["paused"], sample["revision"])
        bridge.round_pause = review
        return {**common, "kind": "chat_pause_review", "token": review.token}
    if not isinstance(review, Review) or review.token != cmd["token"]:
        raise ValueError("Review this exact chat pause state again")
    if op == "apply":
        if (review.applied_by is not None or sample["origin"] != review.origin or sample["paused"] != review.before
                or sample["revision"] != review.revision or sample["stage"] == "terminal"
                or sample["origin"] is not None and getattr(app, "_stop_requested", False)):
            raise ValueError("The reviewed chat pause state changed")
        if not app.set_paused(not review.before, announce=False):
            raise ValueError("The reviewed chat pause choice was not applied")
        review.applied_by = cmd["id"]
        bridge.round_pause = review
        sample = _sample(app, state)
        common.update(sample)
    elif cmd["apply_id"] != review.applied_by or review.applied_by is None:
        raise ValueError("Chat pause readback requires its exact apply")
    if sample["paused"] is review.before or sample["revision"] != review.revision + 1:
        raise ValueError("Chat pause could not be independently verified")
    return {**common, "kind": "chat_pause_applied" if op == "apply" else "chat_pause_verified",
            "token": review.token, "apply_id": review.applied_by}
