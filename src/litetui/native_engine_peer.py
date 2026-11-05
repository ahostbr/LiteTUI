"""Narrow, one-start consent to the local-seat warning after a proved refusal."""

from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path

import psutil

from litetui import harness
from litetui.background_input import _read, encoded, strict_json

SCOPE = "one_start_with_local_peers"
OPERATION = "gui.engine.native_peer_validate"


def _process_birth(pid):
    # A liveness boolean cannot distinguish a restarted process that reused its PID.
    try:
        return int(psutil.Process(pid).create_time() * 1000000)
    except (psutil.Error, OSError) as exc:
        raise ValueError("Cannot establish the current local process identity") from exc


def _label(value, maximum):
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > maximum
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    ):
        raise ValueError(
            "Local seat identity is incomplete or outside its review bounds"
        )
    return value


def capture(app, current, refusal, plan, *, consuming=False):
    generation = current.generation - int(consuming)
    if (
        not refusal
        or refusal["generation"] != generation
        or refusal["selection_id"] != plan.summary["selection_id"]
    ):
        raise ValueError("The prior refused Start no longer matches this launch plan")
    authority = app._agent_session.authority
    own_pid = os.getpid()
    own = {
        "agent_id": authority.agent_id,
        "name": _label(authority.name, 256),
        "backend": current.backend.name,
        "pid": own_pid,
        "born_us": _process_birth(own_pid),
    }
    seats = []
    pids = {own_pid}
    ids = {own["agent_id"]}
    for count, path in enumerate(Path(harness.AGENTS_DIR).glob("*.json"), 1):
        if count > 512:
            raise ValueError("The local registry exceeds the confirmation review limit")
        raw = _read(path, 65536)
        if raw is None:
            raise ValueError("A local seat registration changed during review")
        row = strict_json(raw)
        aid = harness._local_sibling_id(row, path.stem, own["agent_id"])
        if not aid or not harness.router_record.pid_is_live(row["session_pid"]):
            continue
        pid = row["session_pid"]
        if (
            pid in pids
            or aid in ids
            or len(seats) >= 8
            or row.get("agent_id") != path.stem
            or aid != path.stem
        ):
            raise ValueError(
                "Local seat registrations are ambiguous or exceed the review limit"
            )
        name = _label(row.get("name") or aid[:8], 256)
        seats.append(
            {
                "agent_id": _label(aid, 128),
                "name": name,
                "backend": _label(row.get("backend") or "unknown", 64),
                "pid": pid,
                "born_us": _process_birth(pid),
            }
        )
        pids.add(pid)
        ids.add(aid)
    seats.sort(key=lambda row: row["agent_id"])
    if not any(row["name"] == refusal["sibling"] for row in seats):
        raise ValueError("The local seat that blocked Start changed; inspect again")
    proof = {
        "refused_action_id": refusal["action_id"],
        "generation": generation,
        "selection_id": plan.summary["selection_id"],
        "current": own,
        "peers": seats,
        "spawn_new_engine": True,
        "other_engine_ownership": "unverified",
        "resource_checks": "required",
    }
    proof["sha256"] = hashlib.sha256(encoded(proof)).hexdigest()
    return proof


def authorize(app, current, sibling, model):
    attempt = getattr(current, "peer_attempt", None)
    if (
        not attempt
        or attempt["used"]
        or current.action_task is not asyncio.current_task()
        or current.desired_model != model
        or app.backend is not current.backend
    ):
        return False
    if not any(row["name"] == sibling for row in attempt["proof"]["peers"]):
        return False
    try:
        app._validate_owned_identity()
        if _resample(app, current, attempt) != attempt["proof"]:
            return False
    except (OSError, ValueError, RuntimeError):
        return False
    attempt["used"] = True
    return True


def _resample(app, current, attempt):
    # The action consumed one generation before entering the canonical VRAM guard.
    return capture(app, current, attempt["refusal"], attempt["plan"], consuming=True)


def verify_admitted(app, current):
    attempt = getattr(current, "peer_attempt", None)
    if attempt and (
        not attempt["used"] or _resample(app, current, attempt) != attempt["proof"]
    ):
        raise ValueError(
            "Local seat or process identity changed after explicit confirmation"
        )
