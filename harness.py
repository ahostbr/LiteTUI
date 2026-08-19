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
