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
import sys
import ttyguard
import uuid
from datetime import datetime, timezone
from pathlib import Path
import tool_schemas

INBOX_ROOT = Path.home() / ".liteharness" / "inbox"
NEW, CUR, DONE = INBOX_ROOT / "new", INBOX_ROOT / "cur", INBOX_ROOT / "done"

DEFAULT_TIER = "worker"
DEFAULT_CLI = "litetui"
POLL_SECONDS = 5.0

#: Refresh presence every N polls (5s each -> ~60s). `discover` reads
#: `last_seen`, which registration writes ONCE, so a seat with no beat decays to
#: [ghost] while its process is plainly alive -- measured at 10 minutes.
HEARTBEAT_EVERY = 12

#: Set to a non-empty value to make registration a no-op.
#:
#: 🔴 THE SUITE USED TO EVICT THE RUNNING APP FROM THE FLEET. Tests construct
#: LiteTUI, LiteTUI registers, and registration passes --takeover, which is
#: documented to refuse a live holder and measurably does not (see register()).
#: So every `python tests/run_all.py` took the name "LiteTUI" from Ryan's
#: running instance and moved its registry row to ~/.liteharness/.ghost_evicted_*.
#: Measured 2026-08-20: the live app was pid 474900 and its record was in the
#: graveyard, while the roster's only LiteTUI row named a dead test process.
#:
#: Same family as the `lms load` on connect and the .convos pollution: the app's
#: own startup path reaching LIVE SHARED STATE from a test. The registry is not
#: this repo's to write during a test run.
NO_HARNESS_ENV = "LITETUI_NO_HARNESS"


def harness_disabled() -> bool:
    return bool(os.environ.get(NO_HARNESS_ENV, "").strip())


def new_agent_id() -> str:
    return str(uuid.uuid4())


def agent_id_for_convo(convo_id: str) -> str:
    """The seat id for a conversation — the SAME id every time it is resumed.

    🔴 WHY THIS EXISTS. The seat id was a fresh uuid4 per PROCESS, so every
    resume of the same conversation joined the fleet as a stranger and left the
    previous id behind as a ghost. Measured in one evening: one conversation,
    three ids (ed8ee93e -> 8113984f -> e8a69016), two of them still
    heartbeating on the roster while pointing at nothing. A dispatch addressed
    to the id you last saw lands in a dead mailbox and `send` still exits 0 —
    misdelivery here is SILENT.

    The conversation id is already stable across resume (_resume restores it
    from the meta record), so it is the natural identity. Deriving instead of
    storing means the id needs no lookup and no fallback branch: given a convo
    id you can compute the seat id without opening a file, which is exactly
    what you want while debugging a roster that disagrees with reality.

    Empty convo id -> a random one, preserving the old behaviour for the window
    before a conversation exists. That case must stay RANDOM rather than
    resolving to one shared constant, or every seat with no convo would
    collide on the same id.
    """
    cid = (convo_id or "").strip()
    if not cid:
        return new_agent_id()
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "litetui:seat:" + cid))


def _resolved_name(stdout: str) -> str | None:
    """The name the registry ACTUALLY assigned, from register's own output.

    `self.name` is what we ASKED for. The registry may hand back something else
    -- a live holder of that name means takeover refuses and a generated name is
    issued instead. Anything that displays `seat.name` without this (the footer
    does) would show a name the fleet does not know the agent by, which is worse
    than showing nothing: it disagrees with `discover` while looking authoritative.

    Parsed from `Registered agent <id>: cli=..., model=..., tier=..., name=<n>`.
    Returns None if the line is not in that shape, so a format change degrades to
    "keep the requested name" rather than to an exception on the startup path.
    """
    for line in (stdout or "").splitlines():
        if "name=" not in line:
            continue
        tail = line.rsplit("name=", 1)[1].strip()
        # Trailing `, team=...` / ` @pane...` segments are appended after name.
        for stop in (",", " "):
            if stop in tail:
                tail = tail.split(stop, 1)[0]
        if tail:
            return tail
    return None


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
    def _presence_argv(self) -> list[str]:
        """Everything both register() and heartbeat() send.

        ONE list, because two copies of one argv will drift — and the drift is
        invisible: a heartbeat that omitted --session-pid would refresh the
        timestamp while quietly clearing the field that decides ghost-vs-live.
        """
        return [sys.executable, "-m", "liteharness.cli", "register",
                "--agent-id", self.agent_id,
                "--cli", self.cli,
                "--model", self.model,
                "--tier", self.tier,
                "--name", self.name,
                # OUR OWN pid, so the fleet can tell this seat from a corpse.
                # Both mechanisms that read presence.session_pid treat a falsy
                # one as not-live: the takeover guard (so a running seat's name
                # was stealable) and the janitor's dead-owner purge (so dead
                # rows piled up -- four ghosts on the roster). Added to the CLI
                # as an opt-in flag; requires liteharness with --session-pid.
                "--session-pid", str(os.getpid())]

    def heartbeat(self) -> bool:
        """Refresh presence so the roster keeps showing this seat.

        🔴 WITHOUT THIS THE SEAT REGISTERS ONCE AND ROTS. `last_seen` is written
        at registration and never again, so `discover` demoted a LIVE app --
        correct pid, alive process -- to `[ghost] ... 10m ago`. Everything else
        about the seat can be right and the fleet still loses it.

        There is no heartbeat verb: `register` is documented as "Update agent
        presence info", and re-registering was MEASURED to refresh last_seen and
        restore [active].

        ⚠️ NO --takeover. register() claims a name; this only says "still here".
        A heartbeat that claimed the name every minute would make two instances
        fight for it forever, and would re-arm the eviction this seat was just
        the victim of.

        Failure is silent by design: a heartbeat is not news, and a poll-loop
        that reported every miss would paint the transcript. `registered` is
        left alone -- a missed beat is not a deregistration.
        """
        if not self.registered or harness_disabled():
            return False
        try:
            r = ttyguard.run(self._presence_argv(), timeout=30)
            if r.returncode == 0:
                self.name = _resolved_name(r.stdout) or self.name
                return True
        except Exception:
            pass
        return False

    def register(self) -> bool:
        """Announce this seat. Never fatal — LiteTUI runs fine unharnessed.

        Runs as a subprocess with output CAPTURED, not inherited: a child that
        writes to this console corrupts the TUI, and liteharness prints a banner
        on register.

        A no-op under LITETUI_NO_HARNESS, and it says so in `error` rather than
        reporting a quiet success: the footer then reads "unregistered", which
        is TRUE. A guard that fakes registration would hide the very state it
        was added to produce.
        """
        if harness_disabled():
            self.error = f"disabled by {NO_HARNESS_ENV}"
            return False
        try:
            r = ttyguard.run(
                self._presence_argv() + [
                 # RECLAIM OUR OWN NAME FROM OUR OWN CORPSE.
                 #
                 # The agent id is minted per PROCESS and persisted nowhere, so
                 # every launch is a new agent to the fleet -- that part is
                 # correct and must stay (the conversation id cannot be reused
                 # for it: convo_id changes WITHIN a process on /new and resume,
                 # so identity would shift mid-session and two windows resuming
                 # the same conversation would be two consumers on one mailbox).
                 #
                 # But without --takeover the NAME cannot carry across either:
                 # the previous process still holds "LiteTUI" in the registry, so
                 # the name is refused and a random one is generated instead.
                 # Measured on the live roster 2026-08-19 -- SIX rows for one
                 # seat: LiteTUI, BlackGrid, HazeCrypt, PrimeWard, HotPack,
                 # CyanWedge. Anyone who wrote down a name had a stale pointer
                 # one restart later.
                 #
                 # --takeover is DOCUMENTED to evict only a ghost and to refuse
                 # a genuinely live holder. ⚠ THAT GUARD DOES NOT PROTECT THIS
                 # SEAT, and I measured it rather than assuming: two live probes,
                 # and the second took the name from the first.
                 #
                 # _agent_record_live reads presence.session_pid and treats a
                 # falsy one as NOT live. ✅ FIXED 2026-08-20: the CLI grew an
                 # opt-in --session-pid, _presence_argv() passes ours, and a
                 # restored seat was measured reading [active] rather than
                 # [ghost]. (This block used to say the field was unreachable
                 # from `liteharness.cli register` and that the seat "always
                 # reads as a ghost" -- both were true when written and are not
                 # now.)
                 #
                 # Two windows at once still trade the NAME; mail is addressed by
                 # agent_id, so nothing is misdelivered.
                 #
                 # ⚠️ A LIVE PID IS NOT ENOUGH ON ITS OWN. `last_seen` is written
                 # once at registration, so a seat with a correct live pid still
                 # decays to [ghost] -- measured at 10 minutes. heartbeat() is
                 # what keeps it on the roster.
                 "--takeover"],
                timeout=30,
            )
            self.registered = r.returncode == 0
            if self.registered:
                self.name = _resolved_name(r.stdout) or self.name
            else:
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
            ttyguard.run(
                [sys.executable, "-m", "liteharness.cli", "deregister",
                 "--agent-id", self.agent_id],
                timeout=15,
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

    def send(self, to: str, body: str) -> bool:
        """Reply into the fleet. Uses --body-file: an inline double-quoted
        message runs backticks as shell commands and still reports success."""
        try:
            tmp = INBOX_ROOT.parent / f".litetui_send_{uuid.uuid4().hex}.txt"
            tmp.write_text(body, encoding="utf-8")
            try:
                # NO --priority flag: this CLI has no such option, and unknown tokens
                # fall through into the message body — combined with --body-file that
                # is "both given" -> exit 1. Every send would fail for it.
                r = ttyguard.run(
                    [sys.executable, "-m", "liteharness.cli", "send", to,
                     "--body-file", str(tmp), "--from", self.agent_id],
                    timeout=30,
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

HARNESS_TOOL_SPEC = tool_schemas.load("harness")


def discover() -> str:
    """Who is online, as the CLI reports it.

    Output is captured, never inherited — a child writing to this console would
    paint over the TUI.
    """
    try:
        r = ttyguard.run(
            [sys.executable, "-m", "liteharness.cli", "discover"],
            timeout=30,
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
