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
import shutil
import sys
from litetui import ttyguard
import uuid
from datetime import datetime, timezone
from pathlib import Path
from litetui import tool_schemas

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
#: 🔴 SET THIS IN TESTS. Without it, constructing LiteTUI REGISTERS, and
#: registration passes --takeover, which does not refuse a live holder -- so a
#: test run EVICTS the developer's running app from the fleet registry and
#: leaves a dead test process as the only LiteTUI row.
#: The registry is live shared state and is not this repo's to write from a test.
#: Why: Docs/adr/0002-tests-must-not-write-the-live-registry.md
NO_HARNESS_ENV = "LITETUI_NO_HARNESS"


def harness_disabled() -> bool:
    return bool(os.environ.get(NO_HARNESS_ENV, "").strip())


class _Refused:
    """What the CLI door hands back when the harness is switched off.

    A result object rather than a raised exception: every caller in this module
    already branches on `returncode`, so a refusal looks exactly like a failed
    call and no caller has to learn a new control flow to stay correct.
    """

    returncode = 1
    stdout = ""
    stderr = f"refused: {NO_HARNESS_ENV} is set"


class _Unavailable:
    """What the CLI door hands back when the liteharness launcher is missing.

    A result object rather than a raised exception, for the same reason as
    `_Refused`: every caller already branches on `returncode`. The stderr names
    BOTH places that were searched, because "liteharness not installed" sent the
    last reader looking for a missing package when the package was installed all
    along — just not where this process could reach it.
    """

    returncode = 127  # shell convention for command-not-found
    stdout = ""
    stderr = (
        "liteharness launcher not found: no 'liteharness' on PATH and no "
        f"Scripts/liteharness.exe (or bin/liteharness) under {sys.base_prefix}"
    )


def _liteharness_exe() -> str | None:
    """Absolute path to the liteharness CONSOLE SCRIPT, or None.

    🔴 WHY NOT `[sys.executable, "-m", "liteharness.cli"]`, WHICH THIS REPLACED.
    `sys.executable` is the LOCKED PROJECT VENV (run.bat: `uv run --locked`),
    and `liteharness` is deliberately NOT in it — run.bat's header records why
    the two environments must not couple, and pyproject pins this venv for the
    app and its test gate, not for the fleet CLI. So every call in this module
    ran `<.venv python> -m liteharness.cli` against an interpreter that answers
    `No module named 'liteharness'`. Measured 2026-08-30 in this very venv.

    The consequence was not "the harness CLI is unavailable", which would be
    fine — LiteTUI runs unharnessed by design. It was that registration failed
    at startup, silently and exactly once, so the seat never joined the fleet:
    its inbox `new/` never drained and the footer reported a seat that did not
    exist. THIS MODULE'S OWN DOCSTRING ALREADY KNEW: `_cli` below notes that
    discover was inert "because the locked venv has no `liteharness` installed"
    and files that under lucky accidents propping up a guard. The same fact was
    the live defect one paragraph away, read as a test-isolation detail.

    Resolution order, and why two of them:
      1. `shutil.which` — the normal answer, and the one that keeps working if
         liteharness is ever installed somewhere else on PATH.
      2. `sys.base_prefix`/Scripts — because a venv puts ITS OWN Scripts first
         on PATH, so `which` can be shadowed by exactly the environment that
         caused this bug. base_prefix points at the interpreter the venv was
         built FROM, which is where a `pip install --user`-style console script
         actually lands.

    None rather than a guess: a wrong path would fail inside the subprocess with
    a WinError the caller reports as an unrelated crash.
    """
    found = shutil.which("liteharness")
    if found:
        return found
    base = Path(sys.base_prefix)
    for candidate in (base / "Scripts" / "liteharness.exe", base / "bin" / "liteharness"):
        if candidate.exists():
            return str(candidate)
    return None


def _cli(args: list[str], *, timeout: int):
    """THE ONE DOOR to the liteharness CLI. Every subprocess in this module
    goes through here, so the disabled-path check lives in ONE place.

    `args` starts at the VERB (`["register", "--agent-id", ...]`); the door
    prepends the launcher. Callers do not name an interpreter — that is the
    whole point, and it is why the resolution above cannot be bypassed by a
    call site written later.

    WHY A DOOR AND NOT FIVE GUARDS. `harness_disabled()` used to gate exactly
    two of this module's five live-state surfaces -- register and heartbeat,
    both registry WRITES. deregister, send and discover reached the live fleet
    with the guard armed. Measured 2026-08-23: with LITETUI_NO_HARNESS=1 set,
    `discover()` returned the live roster, six agents with real ids.

    They were inert in the suite for THREE UNRELATED ACCIDENTAL REASONS, none
    of which was the guard: poll because the inbox worker returned before its
    loop, send because `registered` was False, and discover because the locked
    venv has no `liteharness` installed. A contract held by three coincidences
    reads exactly like a contract held by one guard, and each coincidence could
    evaporate on its own -- adding `liteharness` to the dev group, which is the
    obvious thing to do the first time a test needs it, re-arms discover with no
    code change and nothing to fail.

    A new CLI call added later gets the guard by construction, because there is
    nowhere else to make one from.
    """
    if harness_disabled():
        return _Refused()
    exe = _liteharness_exe()
    if exe is None:
        return _Unavailable()
    return ttyguard.run([exe, *args], timeout=timeout)


def new_agent_id() -> str:
    return str(uuid.uuid4())


def process_agent_id() -> str:
    """One stable id per process — uuid5 from hostname + pid.

    T507-T5: the seat id was random (uuid4) at construction, then rebound to
    uuid5(convo_id) per new conversation. Each rebind left the previous id as a
    ghost. Measured: LiteTUI/BurntPath/BrightDuct = 3 ghosts of pid 133252.
    Fix: one id per process, every conversation reuses it.
    """
    import platform
    key = f"litetui:process:{platform.node()}:{os.getpid()}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, key))


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
        return ["register",
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
            r = _cli(self._presence_argv(), timeout=30)
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
            r = _cli(
                self._presence_argv() + [
                 # RECLAIM OUR OWN NAME FROM OUR OWN CORPSE.
                 #
                 # 🔴 The agent id is DERIVED from the conversation (agent_id_for_convo,
                 # uuid5), never minted per process. Ryan rejected per-process ids on
                 # 2026-08-21 after they put a dispatched task in a DEAD MAILBOX while
                 # `send` exited 0. Do NOT reintroduce them -- and do not re-derive them
                 # from first principles either, which is what happens when this note is
                 # simply deleted.
                 #
                 # ⚠️ --takeover does NOT protect this seat. It is documented to refuse a
                 # live holder and measurably does not, so a second process TAKES THE NAME.
                 # Address mail by agent_id, never by name.
                 #
                 # ⚠️ A live session_pid is NOT enough: last_seen is written once, so a seat
                 # decays to [ghost] at ~10 minutes. heartbeat() is what keeps it on the
                 # roster.
                 #
                 # Why: Docs/adr/0003-seat-identity-is-derived-from-the-conversation.md
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
            _cli(
                ["deregister", "--agent-id", self.agent_id],
                timeout=15,
            )
        except Exception:
            pass

    def rebind(self, new_agent_id: str) -> bool:
        """Move this seat to a new identity in ONE transition.

        🔴 WHAT THIS REPLACES WAS A SILENT DEREGISTRATION. app.py's
        `_sync_seat_identity()` used to deregister the old row, assign the new
        id, and set `registered = False`, on the theory — stated in its own
        docstring, three lines above the assignment that defeated it — that the
        next heartbeat would register the new identity. `heartbeat()` opens with
        `if not self.registered`, so it returned immediately, and `register()`
        is reached from exactly ONE place: startup. Nothing re-armed the seat.
        After /new or /resume the app was absent from `discover`, its id named
        no registry row, and the footer read "unregistered". Probed 2026-08-23:
        registered False, heartbeat False, transport calls 0.

        ⭐ The lesson is not "that comment was wrong". It is that the comment
        and the assignment that contradicts it were BOTH read many times, by
        the person who wrote them, and the sentence won. Follow the control
        flow in this file; do not trust its prose.

        ⚠️ A SEAT THAT NEVER HELD A REGISTRATION IS NOT RE-REGISTERED HERE.
        Boot reaches this seam before startup registration runs, and the model
        id is not known until `_connect` settles — registering early would
        claim the row as model "unknown". Adopting the id and returning False
        is the honest report of "nothing was rebound", not a failure.

        Failure is LOUD, unlike `heartbeat()`. A missed beat is not news; a
        seat that has silently stopped existing is exactly the news that went
        unreported for the entire life of this bug. `error` carries the reason
        and the caller surfaces it.
        """
        if new_agent_id == self.agent_id:
            return self.registered

        was_registered = self.registered
        if was_registered:
            # Retire the OLD row while `agent_id` still NAMES it. It carries
            # this process's pid, so every liveness check that separates ghost
            # from live reads it as alive and keeps offering it as a delivery
            # target. Order is load-bearing: swapping the id first would leave
            # the stale row on the roster forever.
            #
            # Swallowed, and only here: a roster that keeps a stale row beats a
            # seat that never comes back. Letting this abort the rebind would
            # trade the ghost for the exact invisibility this method exists to
            # end — a strictly worse failure than the one being avoided.
            try:
                self.deregister()
            except Exception:
                pass
            self.registered = False

        self.agent_id = new_agent_id
        self.error = None
        if not was_registered:
            return False
        return self.register()

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

        GATED SEPARATELY FROM _cli BECAUSE IT NEVER GOES THROUGH IT. This is
        the only live-state surface here that is pure filesystem, and it is the
        destructive one: it MOVES files out of the shared maildir, which is how
        six agents coordinate, and the move is irreversible from the sender's
        side. A disabled harness must not consume mail it will never deliver.
        """
        if harness_disabled():
            return []
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
        message runs backticks as shell commands and still reports success.

        Guarded HERE as well as at the door, because the tmp file is written
        into the live ~/.liteharness/ directory BEFORE the CLI is called -- a
        refusal at the door alone would still create and unlink a file in a
        directory a disabled harness has no business touching.
        """
        if harness_disabled():
            return False
        try:
            tmp = INBOX_ROOT.parent / f".litetui_send_{uuid.uuid4().hex}.txt"
            tmp.write_text(body, encoding="utf-8")
            try:
                # NO --priority flag: this CLI has no such option, and unknown tokens
                # fall through into the message body — combined with --body-file that
                # is "both given" -> exit 1. Every send would fail for it.
                r = _cli(
                    ["send", to,
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


AGENTS_DIR = Path.home() / ".liteharness" / "agents"


def resolve_agent(token: str) -> tuple[str | None, str]:
    """Resolve a name or id prefix to a full agent id.

    Returns (full_id, error_message). On success error_message is empty.
    On failure full_id is None and the error says why.
    """
    if not AGENTS_DIR.is_dir():
        return None, "agent registry not found"
    agents: list[tuple[str, str]] = []
    for f in AGENTS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            aid = data.get("agent_id") or f.stem
            name = data.get("name") or ""
            agents.append((aid, name))
        except Exception:
            continue
    if not agents:
        return None, "no agents registered"
    # exact id
    for aid, name in agents:
        if aid == token:
            return aid, ""
    # exact name (case-insensitive)
    by_name = [(aid, n) for aid, n in agents if n.lower() == token.lower()]
    if len(by_name) == 1:
        return by_name[0][0], ""
    if len(by_name) > 1:
        ids = ", ".join(a for a, _ in by_name)
        return None, f"ambiguous name {token!r} matches {len(by_name)} agents: {ids}. Use discover to get the full id."
    # unique id prefix
    by_prefix = [(aid, n) for aid, n in agents if aid.startswith(token)]
    if len(by_prefix) == 1:
        return by_prefix[0][0], ""
    if len(by_prefix) > 1:
        candidates = ", ".join(f"{a[:8]} ({n})" for a, n in by_prefix)
        return None, f"ambiguous prefix {token!r} matches {len(by_prefix)} agents: {candidates}. Use discover to get the full id."
    return None, f"no agent matches {token!r}. Use discover to list online agents."


def discover() -> str:
    """Who is online, as the CLI reports it.

    Output is captured, never inherited — a child writing to this console would
    paint over the TUI.
    """
    try:
        r = _cli(["discover"], timeout=30)
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
        to_raw = str(args.get("to") or "").strip()
        body = str(args.get("body") or "")
        if not to_raw:
            return "[error] send: `to` is required — get an agent id from discover"
        if not body.strip():
            return "[error] send: `body` is required"
        to, err = resolve_agent(to_raw)
        if to is None:
            return f"[error] send: {err}"
        if to == seat.agent_id:
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
