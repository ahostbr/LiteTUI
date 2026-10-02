"""T1049-B: a locked seat's CONFIRM goes to the agent that SPAWNED it, by inbox.

Ryan (a-29047520): "must be handled by their leaders, their clawed leaders".
Ryan (04169351): "whatever agent spawned the light qi instance should be babysitting it".
Ryan (6e280dd4): a typed CONFIRM in an agent-launched seat -> "The launching agent";
an agent-launched LiteGUI's CONFIRM -> "No, launching agent".

Initial ancestry is recorded ONLY from the marker envelope
(LITETUI_SPAWN_IDENTITY=1 + LITEHARNESS_SPAWNED_BY), never from an inherited env
(Dijkstra M1). Each new request resolves the registered seat's current spawned_by;
its pending reply authority is frozen to that destination, so re-registration
changes future requests only. Missing/invalid presence never uses cached ancestry.
⚠️ FORGEABLE, stated (B4): local presence is only as trustworthy as its writer;
`send --from` is not
validated and every local agent can read ~/.liteharness, so the from + nonce check
below stops accidents (a stray or late reply), not a malicious local agent.

Only an APPROVE continues. A DENY, no answer within `relay_approval_timeout_s`, and
a spawner the registry does not know all STOP the turn, and every outcome is logged
to runtime.jsonl as "approval_relay" (Marquee S1(b)), a cancelled wait included.
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid

from textual.containers import Horizontal
from textual.app import ScreenStackError
from textual.css.query import NoMatches
from textual.widgets import Button, Static

from litetui import runtime_log

#: Set ONLY by a launcher whose rpc host relays approvals (agent_supervisor).
#: Process-only: popped at startup, like LITETUI_OWNER. As forgeable as the marker.
APPROVAL_HOST_ENV = "LITETUI_APPROVAL_HOST"
SPAWNED_BY_ENV = "LITEHARNESS_SPAWNED_BY"

_ANSWER = re.compile(r"\s*(APPROVE|DENY)\s+(appr-[0-9a-f]{12})\b")
_INPUT_LIMIT = 2048


class HumanApproval(Static):
    """Local seat control for one pending relay request; never an inbox answer."""

    DEFAULT_CSS = """
    HumanApproval { height: auto; border: round $warning; padding: 0 1; }
    HumanApproval Horizontal { height: auto; }
    HumanApproval Button { margin-right: 1; }
    """

    def __init__(self, ident: str, name: str) -> None:
        super().__init__(classes="human-approval")
        self.ident = ident
        self.tool_name = name

    def compose(self):
        yield Static(f"{self.tool_name} ({self.ident}) — human override")
        with Horizontal():
            yield Button("Deny", variant="error", classes="human-approval-deny")
            yield Button("Approve", variant="warning", classes="human-approval-allow")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.has_class("human-approval-deny"):
            allow = False
        elif event.button.has_class("human-approval-allow"):
            allow = True
        else:
            return
        event.stop()
        if take_human_answer(self.app, self.ident, allow):
            self.remove()


def take_human_answer(app, ident: str, allow: bool) -> bool:
    """Only this local UI call resolves the exact pending request, once."""
    entry = _pending(app).get(ident)
    if entry is None or entry[0].done() or not isinstance(allow, bool):
        return False
    entry[0].set_result(allow)
    return True


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


def current_spawner(app) -> str | None:
    """Only current own presence can grant inbox reply authority."""
    seat = getattr(app, "seat", None)
    return seat.current_spawner() if seat is not None else None


async def ask_spawner(app, name: str, args, decision, source) -> str:
    """approved | denied | timeout | absent. Logged either way."""
    spawner = current_spawner(app)
    timeout = timeout_s(app)
    ident = "appr-" + uuid.uuid4().hex[:12]
    future = asyncio.get_running_loop().create_future()
    pending = _pending(app)
    pending[ident] = (future, spawner)
    # Dijkstra K1(c): logged in the finally, so a wait cancelled by Esc, stop() or
    # the Claude bridge's deadline still leaves its line ("cancelled").
    status = "cancelled"
    control = None
    wait_token = app._begin_wait((spawner or 'unavailable')[:8], "approval")
    try:
        seat = getattr(app, "seat", None)
        # Unregistered means no inbox poller, so no answer could ever arrive. The
        # registry refusing the id (`send` exit != 0) is the "absent" signal.
        sent = (spawner is not None and seat is not None and getattr(seat, "registered", False)
                and await asyncio.to_thread(seat.send, spawner,
                                            _message(app, ident, name, args, decision, source, timeout)))
        if not sent:
            status = "absent"
        else:
            app._system(f"asked {(spawner or 'unavailable')[:8]} (the spawning agent) to approve {name} "
                        f"({ident}); waiting up to {timeout:.0f} s")
            try:
                log = app.query_one("#chat-log")
            except (AttributeError, NoMatches, ScreenStackError):
                pass  # Headless/unmounted tests still use the spawner inbox.
            else:
                control = HumanApproval(ident, name)
                await log.mount(control)
            try:
                status = "approved" if await asyncio.wait_for(future, timeout) else "denied"
            except TimeoutError:
                status = "timeout"
    finally:
        app._end_wait(wait_token)
        pending.pop(ident, None)
        if control is not None and control.is_mounted:
            await control.remove()
        record(app, status, name, source, ident)
    if status == "approved":
        app._system(f"approval received for {name} ({ident})")
    return status


def take_answer(app, msg: dict) -> bool:
    """Consume `msg` only if it answers a PENDING request, from the agent that
    request went to. Anything else stays ordinary mail, so nothing is eaten."""
    # Dijkstra SHOULD: LiteSuite's orchestrator writes payload.text, not body.
    text = msg.get("body") or (msg.get("payload") or {}).get("text") or ""
    match = _ANSWER.match(str(text))
    if match is None:
        return False
    entry = _pending(app).get(match.group(2))
    if entry is None or msg.get("from") != entry[1] or entry[0].done():
        return False
    entry[0].set_result(match.group(1) == "APPROVE")
    return True


def stop_line(app, name: str, status: str) -> str:
    # The launch id (or a fresh presence read) may differ from the destination
    # of the request that just finished. Do not attribute its outcome to either.
    return {
        "denied": f"[stopped — the request's spawning agent denied {name}]",
        "timeout": (f"[stopped — the request's spawning agent did not answer the approval "
                    f"for {name} within {timeout_s(app):.0f}s; refused and logged]"),
        "absent": (f"[stopped — the current spawning agent is not reachable (invalid presence, not registered, "
                   f"or the harness is off); {name} was not run; refused and logged]"),
        "no_spawner": (f"[stopped — an agent launched this LiteTUI without naming itself "
                       f"(LITETUI_SPAWN_IDENTITY=1 + {SPAWNED_BY_ENV}), so nobody can approve "
                       f"{name}; refused and logged (T1049)]"),
    }[status]
