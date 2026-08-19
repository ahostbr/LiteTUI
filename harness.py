"""LiteHarness seat — register LiteTUI as an agent and receive its own inbox.

LiteTUI ships inside LiteSuite and installs through the same wizard, so it gets
a seat in the fleet like any other agent: a registration other agents can
discover, and a monitor that WAKES it when mail arrives.

🔴 WHY THIS DOES NOT CALL `liteharness.hooks.watch` OR `check_inbox`.
Both are CONSUMERS: they move files out of `inbox/new/` for whichever agent id
they resolve. Two consumers on one mailbox is the exact defect the
`ls-liteharness` fix retracted — a background watcher drained the shared inbox
and mail vanished for three hours. `watch_inbox` also writes to STDOUT, which in
a Textual app paints straight over the screen.

So this module reads the maildir directly and obeys one rule that makes a second
consumer safe:

    ONLY EVER TOUCH A FILE WHOSE `to` IS THIS AGENT.

Anything addressed elsewhere is left in `new/` exactly as found — unread,
unmoved, unclaimed. A poller that filtered on nothing would be indistinguishable
from the bug it is avoiding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

INBOX_ROOT = Path.home() / ".liteharness" / "inbox"
NEW, CUR, DONE = INBOX_ROOT / "new", INBOX_ROOT / "cur", INBOX_ROOT / "done"

DEFAULT_TIER = "worker"
DEFAULT_CLI = "litetui"
POLL_SECONDS = 5.0


def new_agent_id() -> str:
    return str(uuid.uuid4())


class Seat:
    """This LiteTUI session's identity in the harness."""

    def __init__(self, agent_id: str, name: str, model: str,
                 tier: str = DEFAULT_TIER, cli: str = DEFAULT_CLI):
        self.agent_id = agent_id
        self.name = name
        self.model = model or "unknown"
        self.tier = tier
        self.cli = cli
        self.registered = False
        self.error: str | None = None

    # ── registration ────────────────────────────────────────────────────────
    def register(self) -> bool:
        """Announce this seat. Never fatal — LiteTUI runs fine unharnessed.

        Runs as a subprocess with output CAPTURED, not inherited: a child that
        writes to this console corrupts the TUI, and liteharness prints a banner
        on register.
        """
        try:
            r = subprocess.run(
                [sys.executable, "-m", "liteharness.cli", "register",
                 "--agent-id", self.agent_id,
                 "--cli", self.cli,
                 "--model", self.model,
                 "--tier", self.tier,
                 "--name", self.name],
                capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.registered = r.returncode == 0
            if not self.registered:
                self.error = (r.stderr or r.stdout or "").strip()[:200] or f"exit {r.returncode}"
            return self.registered
        except FileNotFoundError:
            self.error = "liteharness not installed"
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        return False

    def deregister(self) -> None:
        if not self.registered:
            return
        try:
            subprocess.run(
                [sys.executable, "-m", "liteharness.cli", "deregister",
                 "--agent-id", self.agent_id],
                capture_output=True, text=True, timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except Exception:
            pass

    # ── inbox ───────────────────────────────────────────────────────────────
    def _addressed_to_me(self, msg: dict) -> bool:
        to = msg.get("to")
        if to is None or to == "broadcast" or to == "*":
            return True
        return to == self.agent_id

    def poll(self) -> list[dict]:
        """Claim and return this seat's messages. Others' mail is untouched.

        Order: read -> verify addressee -> move -> return. Moving BEFORE
        confirming the addressee is how a poller steals mail, and the move is
        irreversible from the sender's point of view.
        """
        if not NEW.is_dir():
            return []
        out: list[dict] = []
        try:
            entries = sorted(NEW.glob("*.json"))
        except OSError:
            return []
        for f in entries:
            try:
                msg = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                # Not ours to judge, and possibly mid-write by the sender.
                continue
            if not isinstance(msg, dict) or not self._addressed_to_me(msg):
                continue
            if msg.get("from") == self.agent_id:
                continue  # our own send, echoed back
            if _expired(msg):
                _move(f, DONE)
                continue
            if _move(f, DONE) is not None:
                out.append(msg)
        return out

    def send(self, to: str, body: str, priority: str = "normal") -> bool:
        """Reply into the fleet. Uses --body-file: an inline double-quoted
        message runs backticks as shell commands and still reports success."""
        try:
            tmp = INBOX_ROOT.parent / f".litetui_send_{uuid.uuid4().hex}.txt"
            tmp.write_text(body, encoding="utf-8")
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "liteharness.cli", "send", to,
                     "--body-file", str(tmp), "--from", self.agent_id,
                     "--priority", priority],
                    capture_output=True, text=True, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                return r.returncode == 0
            finally:
                try:
                    tmp.unlink()
                except OSError:
                    pass
        except Exception:
            return False


def _expired(msg: dict) -> bool:
    ttl = msg.get("ttl_minutes")
    ts = msg.get("timestamp")
    if not ttl or not ts:
        return False
    try:
        sent = datetime.fromisoformat(str(ts))
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - sent).total_seconds() / 60.0
        return age_min > float(ttl)
    except Exception:
        return False


def _move(src: Path, dest_dir: Path) -> Path | None:
    """Atomically claim a file. None when someone else got there first."""
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / src.name
        os.replace(src, dest)
        return dest
    except OSError:
        return None


def format_message(msg: dict) -> str:
    """Render an inbox message for the chat log."""
    frm = str(msg.get("from") or "?")[:8]
    pri = msg.get("priority") or "normal"
    body = (msg.get("body") or "").strip()
    return f"[inbox from {frm} · {pri}]\n{body}"


# ── Agent-facing tool ────────────────────────────────────────────────────────
#
# The seat registered and received mail from the very first version, and the
# agent had no way to ANSWER any of it — reachable, but mute. These are the
# fleet primitives it actually needs, exposed as ONE tool with an action rather
# than four separate ones, so the model picks a verb instead of inventing CLI
# syntax.
#
# `send` routes through Seat.send, which writes the body to a FILE and passes
# --body-file. That is not style: an inline double-quoted message runs backticks
# as shell commands AND still reports success, so a model that puts a code fence
# in a message would silently execute it.

HARNESS_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "harness",
        "description": (
            "Talk to the LiteHarness agent fleet. Actions: "
            "whoami — your agent id, name, tier and whether registration succeeded; "
            "discover — which agents are online right now; "
            "send — message another agent (requires `to` and `body`); "
            "check — poll your inbox immediately instead of waiting for the monitor."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["whoami", "discover", "send", "check"],
                    "description": "Which fleet operation to perform",
                },
                "to": {
                    "type": "string",
                    "description": "Target agent id, from discover (send only)",
                },
                "body": {
                    "type": "string",
                    "description": "Message text to deliver (send only)",
                },
            },
            "required": ["action"],
        },
    },
}


def discover() -> str:
    """Who is online, as the CLI reports it.

    Output is captured, never inherited — a child writing to this console would
    paint over the TUI.
    """
    try:
        r = subprocess.run(
            [sys.executable, "-m", "liteharness.cli", "discover"],
            capture_output=True, text=True, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or r.stderr or "").strip()
        return out or "(discover returned nothing)"
    except FileNotFoundError:
        return "[error] liteharness is not installed"
    except Exception as e:
        return f"[error] discover: {type(e).__name__}: {e}"


def run(seat, args: dict) -> str:
    """Dispatch the `harness` tool. Never raises — every path returns text."""
    # str() first: a model can emit a number or null here, and .strip() on a
    # non-str raises AttributeError from inside a tool that promises never to.
    action = str(args.get("action") or "").strip().lower()

    if action == "whoami":
        lines = [
            f"agent_id   : {seat.agent_id}",
            f"name       : {seat.name}",
            f"tier       : {seat.tier}",
            f"cli        : {seat.cli}",
            f"model      : {seat.model}",
            f"registered : {seat.registered}",
        ]
        if seat.error:
            lines.append(f"error      : {seat.error}")
        return "\n".join(lines)

    if action == "discover":
        return discover()

    if action == "check":
        msgs = seat.poll()
        if not msgs:
            return "(no new messages)"
        return "\n\n".join(format_message(m) for m in msgs)

    if action == "send":
        to = str(args.get("to") or "").strip()
        body = str(args.get("body") or "")
        if not to:
            return "[error] send: `to` is required — get an agent id from discover"
        if not body.strip():
            return "[error] send: `body` is required"
        if to == seat.agent_id:
            # watch_inbox drops from == to by design, so this could never arrive.
            return (
                "[error] send: that is your OWN id. The watcher drops self-addressed "
                "mail, so it would be silently discarded rather than delivered."
            )
        if not seat.registered:
            return "[error] send: this seat is not registered, so it has no return address"
        return f"sent to {to[:8]}" if seat.send(to, body) else f"[error] send to {to[:8]} failed"

    return (
        f"[error] unknown action {action!r} — valid actions are "
        "whoami, discover, send, check"
    )
