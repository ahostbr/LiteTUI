"""T1049-B: a locked seat's CONFIRM goes to the agent that SPAWNED it, by inbox.

Ryan (a-29047520): "must be handled by their leaders, their clawed leaders".
Ryan (04169351): "whatever agent spawned the light qi instance should be babysitting it".
Ryan (6e280dd4): a typed CONFIRM in an agent-launched seat -> "The launching agent";
an agent-launched LiteGUI's CONFIRM -> "No, launching agent".

The spawner is recorded at startup ONLY from the marker envelope
(LITETUI_SPAWN_IDENTITY=1 + LITEHARNESS_SPAWNED_BY), never from an inherited env
(Dijkstra M1). ⚠️ FORGEABLE, stated (B4): the spawner is whatever the LAUNCHER wrote,
so the envelope is only as trustworthy as that launcher; `send --from` is not
validated and every local agent can read ~/.liteharness, so the from + nonce check
below stops accidents (a stray or late reply), not a malicious local agent.

Only an APPROVE continues. A DENY, no answer within `relay_approval_timeout_s`, and
a spawner the registry does not know all STOP the turn, and every outcome is logged
to runtime.jsonl as "approval_relay" (Marquee S1(b)).
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from litetui import runtime_log

#: Set ONLY by a launcher whose rpc host relays approvals (agent_supervisor).
#: Process-only: popped at startup, like LITETUI_OWNER. As forgeable as the marker.
APPROVAL_HOST_ENV = "LITETUI_APPROVAL_HOST"
SPAWNED_BY_ENV = "LITEHARNESS_SPAWNED_BY"

_ANSWER = re.compile(r"\s*(APPROVE|DENY)\s+(appr-[0-9a-f]{12})\b")
_INPUT_LIMIT = 2048


def _pending(app) -> dict:
    pending = getattr(app, "_relay_pending", None)
    if pending is None:
        pending = app._relay_pending = {}
    return pending


def record(app, status: str, name: str, source: str | None, ident: str = "none") -> None:
    """One "approval_relay" line per outcome, approvals included."""
    runtime_log.record("approval_relay", site="app.authorize", component="relay",
                       operation=str(source or "unlabelled"), name=str(name),
                       id=ident, status=status)


def _message(app, ident: str, name: str, args, decision, source, timeout: float) -> str:
    seat = app.seat
    try:
        shown = json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        shown = repr(args)
    if len(shown) > _INPUT_LIMIT:
        shown = shown[:_INPUT_LIMIT] + " ...(truncated)"
    danger = getattr(decision, "danger", None) or ", ".join(sorted(decision.capabilities))
    return (f"[APPROVAL {ident}] {seat.name} ({seat.agent_id[:8]}) asks to run {name} "
            f"during a {source or 'unlabelled'} turn\n"
            f"Danger: {danger}; why: {decision.reason}\n"
            f"Input: {shown}\n"
            f"Answer by inbox with exactly one line: APPROVE {ident}  or  DENY {ident}\n"
            f"No answer within {timeout:.0f} s = the turn stops and this is logged.")


def timeout_s(app) -> float:
    return float(getattr(getattr(app, "settings", None), "relay_approval_timeout_s", 600) or 600)


async def ask_spawner(app, name: str, args, decision, source) -> str:
    """approved | denied | timeout | absent. Logged either way."""
    spawner = app._spawner_id
    timeout = timeout_s(app)
    ident = "appr-" + uuid.uuid4().hex[:12]
    future = asyncio.get_running_loop().create_future()
    pending = _pending(app)
    pending[ident] = (future, spawner)
    try:
        seat = getattr(app, "seat", None)
        # Unregistered means no inbox poller, so no answer could ever arrive. The
        # registry refusing the id (`send` exit != 0) is the "absent" signal.
        sent = (seat is not None and getattr(seat, "registered", False)
                and await asyncio.to_thread(seat.send, spawner,
                                            _message(app, ident, name, args, decision, source, timeout)))
        if not sent:
            status = "absent"
        else:
            app._system(f"asked {spawner[:8]} (the spawning agent) to approve {name} "
                        f"({ident}); waiting up to {timeout:.0f} s")
            try:
                status = "approved" if await asyncio.wait_for(future, timeout) else "denied"
            except TimeoutError:
                status = "timeout"
    finally:
        pending.pop(ident, None)
    record(app, status, name, source, ident)
    if status == "approved":
        app._system(f"{spawner[:8]} approved {name} ({ident})")
    return status


def take_answer(app, msg: dict) -> bool:
    """Consume `msg` only if it answers a PENDING request, from the agent that
    request went to. Anything else stays ordinary mail, so nothing is eaten."""
    match = _ANSWER.match(str(msg.get("body") or ""))
    if match is None:
        return False
    entry = _pending(app).get(match.group(2))
    if entry is None or msg.get("from") != entry[1] or entry[0].done():
        return False
    entry[0].set_result(match.group(1) == "APPROVE")
    return True


def stop_line(app, name: str, status: str) -> str:
    who = (getattr(app, "_spawner_id", None) or "")[:8]
    return {
        "denied": f"[stopped — {who} (the spawning agent) denied {name}]",
        "timeout": (f"[stopped — {who} (the spawning agent) did not answer the approval "
                    f"for {name} within {timeout_s(app):.0f}s; refused and logged]"),
        "absent": (f"[stopped — the spawning agent {who} is not reachable (not registered, "
                   f"or the harness is off); {name} was not run; refused and logged]"),
        "no_spawner": (f"[stopped — an agent launched this LiteTUI without naming itself "
                       f"(LITETUI_SPAWN_IDENTITY=1 + {SPAWNED_BY_ENV}), so nobody can approve "
                       f"{name}; refused and logged (T1049)]"),
    }[status]
