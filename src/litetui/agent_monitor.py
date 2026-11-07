"""The agent-callable `monitor` tool: watch a command or a file, get WOKEN by it.

Input -> watch -> events, with each event arriving as INPUT NOBODY TYPED. A
monitor is the same class of thing a background shell result is (tasks.py): its
output reaches the seat through `LiteTUI._deliver_inbox`, the funnel mail, cron
and finished background tasks already use. That funnel holds an event while a
turn is running and starts a turn with it when the seat is idle, so this module
adds no delivery channel and no second consumer of anything: it reads only the
command or the file it was pointed at.

Two sources, one pipeline:

    producer thread --lines--> queue --> pump thread --batched events--> deliver()

  COMMAND   `ttyguard.popen` (no console window, stdin DEVNULL, job-object
            kill), stdout and stderr merged. Every line is also appended to a
            log file, so an event cut for length can name where the rest is.
  FILE      polled at POLL_S. Positioned at the end of the file AT ARM TIME (so a
            line appended right after the call is never missed), and at 0 for a
            file that does not exist yet. A shrunk file or a new file identity
            (rotation, atomic replace) restarts at 0. A same-size mtime change
            is reported as one `modified` line. A trailing line with no newline
            is flushed after PARTIAL_FLUSH_S of quiet.

The pump batches lines that arrive within BATCH_S of the first into one event,
enforces the lifetime (`timeout_ms`) and the flood guard, and delivers.

This module is pure: no Textual, no app. The plugin supplies `deliver` (which
crosses onto the UI loop), `convo_id` and `running`, so every rule here is
testable with a plain callback.
"""

from __future__ import annotations

import collections
import itertools
import os
import queue
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from litetui import paths, tool_policy, ttyguard

#: Lines within this long of the first line of a batch become ONE event.
BATCH_S = 0.2
#: File mode stat interval.
POLL_S = 0.25
#: A trailing line without a newline is an event once the file is quiet this long.
PARTIAL_FLUSH_S = 1.0

DEFAULT_TIMEOUT_MS = 300_000
MIN_TIMEOUT_MS = 1_000
MAX_TIMEOUT_MS = 3_600_000

#: More than FLOOD_MAX_EVENTS events inside FLOOD_WINDOW_S stops the monitor.
FLOOD_WINDOW_S = 10.0
FLOOD_MAX_EVENTS = 30
MAX_ACTIVE = 8
#: Ended monitors kept so `list` can still say how they ended.
KEEP_ENDED = 20

EVENT_MAX_LINES = 40
EVENT_MAX_CHARS = 4000
LINE_MAX_CHARS = 1000
#: Largest slice of a watched file read per poll.
READ_CHUNK = 1 << 20

LOG_DIR = ("output", "monitors")

RUNNING = "running"
#: Terminal states, in the words `list` prints.
ENDED = "ended"        # the command exited
STOPPED = "stopped"    # the seat asked
TIMEOUT = "timeout"    # timeout_ms elapsed
FLOOD = "flood"        # the flood guard
LOST = "lost"          # the conversation moved on, the app went away, or delivery failed

#: States whose last word is delivered to the seat as a notice.
_NOTIFIED = frozenset({ENDED, TIMEOUT, FLOOD})

_EOF = object()


def classify_monitor(args: Mapping[str, object], workspace: Path):
    """What a call to `monitor` can do. Watching a file or listing is a read;
    a COMMAND runs a process, so it carries `process_execution` and whatever
    `classify_shell` finds in it. The deny floor reads the same `command`
    argument for every policy, so it applies here without being named."""
    action = str(args.get("action") or "start").lower()
    if action != "start" or not args.get("command"):
        return (tool_policy.READ_ONLY,)
    return (tool_policy.PROCESS_EXECUTION, *tool_policy.classify_shell(args, workspace))


POLICY = tool_policy.ToolPolicy(
    frozenset({tool_policy.READ_ONLY}),
    "Watch a command or a file and wake on its output",
    classify_args=classify_monitor,
)


def _cut_line(line: str) -> str:
    if len(line) <= LINE_MAX_CHARS:
        return line
    return f"{line[:LINE_MAX_CHARS]} [... {len(line) - LINE_MAX_CHARS} chars cut]"


class Monitor:
    """One watch. Created and started by `MonitorService`; every field below
    `state` is written by this monitor's own threads."""

    def __init__(self, mid: str, mode: str, target: str, label: str, timeout_s: float,
                 convo_id: str, deliver: Callable[[str], None], alive: Callable[[], bool],
                 log_path: Path | None = None) -> None:
        self.id = mid
        self.mode = mode
        self.target = target
        self.label = label
        self.convo_id = convo_id
        self.timeout_s = timeout_s
        self.log_path = log_path
        self.started = time.time()
        self.ended: float | None = None
        self.state = RUNNING
        self.events = 0
        self.exit_code: int | None = None
        self.proc: object | None = None
        self._deliver = deliver
        self._alive = alive
        self._deadline = time.monotonic() + timeout_s
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._reason_lock = threading.Lock()
        self._reason: str | None = None
        self._kill_lock = threading.Lock()
        self._killed = False
        # file mode
        self._offset = 0
        self._ident: tuple | None = None
        self._mtime: int | None = None
        self._partial = b""
        self._partial_since: float | None = None
        self._threads: list[threading.Thread] = []

    # ── lifecycle ──────────────────────────────────────────────────────────

    def begin_command(self, argv, *, shell: bool, cwd: str, env: dict) -> None:
        # The log exists before its path is advertised, so a read of it never
        # lands before the first line.
        if self.log_path is not None:
            try:
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self.log_path.touch()
            except OSError:
                self.log_path = None
        env = {"PYTHONUNBUFFERED": "1", **env}  # a piped python child would otherwise hold its events in a block buffer
        self.proc = ttyguard.popen(
            argv, shell=shell, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=cwd, env=env, kill_on_close=True,
        )
        self._spawn(self._read_command)

    def begin_file(self) -> str:
        """Position at the end of the file NOW, then watch. Returns what was found."""
        try:
            st = os.stat(self.target)
        except OSError:
            self._offset, self._ident = 0, None
            found = "not created yet; everything written to it is an event"
        else:
            self._offset, self._ident, self._mtime = st.st_size, (st.st_dev, st.st_ino), st.st_mtime_ns
            found = f"exists, {st.st_size} bytes; only what is appended from now on is an event"
        self._spawn(self._watch_file)
        return found

    def _spawn(self, producer) -> None:
        for name, fn in ((producer.__name__, producer), ("_pump", self._pump)):
            t = threading.Thread(target=fn, name=f"monitor-{self.id}-{name}", daemon=True)
            self._threads.append(t)
            t.start()

    def stop(self, reason: str = STOPPED) -> None:
        """End the watch. Idempotent; the first reason wins."""
        self._set_reason(reason)
        self._stop.set()
        self._kill()

    def _set_reason(self, reason: str) -> None:
        with self._reason_lock:
            if self._reason is None:
                self._reason = reason

    def _kill(self) -> None:
        """Once. `stop` (tool thread) and `_finish` (pump thread) both arrive here,
        and a job handle closed twice could close a recycled one."""
        with self._kill_lock:
            proc = self.proc
            if self._killed or proc is None:
                return
            self._killed = True
            if proc.poll() is None:
                ttyguard.kill_tree(proc.pid, proc)

    @property
    def seconds(self) -> float:
        return max(0.0, (self.ended or time.time()) - self.started)

    # ── producers ──────────────────────────────────────────────────────────

    def _read_command(self) -> None:
        log = None
        try:
            if self.log_path is not None:
                try:
                    log = self.log_path.open("a", encoding="utf-8", errors="replace", newline="\n")
                except OSError:
                    log = None
            for raw in self.proc.stdout:
                line = raw.rstrip("\r\n")
                if log is not None:
                    log.write(line + "\n")
                    log.flush()
                if line.strip():
                    self._q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            if log is not None:
                log.close()
            try:
                self.exit_code = self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001 - the exit code is garnish; the EOF is not
                pass
            self._q.put(_EOF)

    def _watch_file(self) -> None:
        while not self._stop.wait(POLL_S):
            try:
                self._poll_file()
            except OSError:
                continue  # a file mid-rotation or briefly locked: look again

    def _poll_file(self) -> None:
        try:
            st = os.stat(self.target)
        except OSError:
            # Gone (rotated away, deleted): whatever appears next is a new file.
            self._ident, self._offset, self._partial, self._partial_since = None, 0, b"", None
            return
        ident = (st.st_dev, st.st_ino)
        if self._ident is None or ident != self._ident:
            self._offset, self._partial, self._partial_since = 0, b"", None
        elif st.st_size < self._offset:
            self._offset, self._partial, self._partial_since = 0, b"", None
        self._ident = ident
        grew = st.st_size > self._offset
        if grew:
            with open(self.target, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read(min(st.st_size - self._offset, READ_CHUNK))
            self._offset += len(data)
            *whole, self._partial = (self._partial + data).split(b"\n")
            for raw in whole:
                self._put_file_line(raw)
        elif self._mtime is not None and st.st_mtime_ns != self._mtime and self._offset:
            self._q.put(f"[modified: {self.target} rewritten in place, {st.st_size} bytes]")
        self._mtime = st.st_mtime_ns
        now = time.monotonic()
        if not self._partial:
            self._partial_since = None
        elif grew or self._partial_since is None:
            self._partial_since = now
        elif now - self._partial_since >= PARTIAL_FLUSH_S:
            raw, self._partial, self._partial_since = self._partial, b"", None
            self._put_file_line(raw)

    def _put_file_line(self, raw: bytes) -> None:
        line = raw.decode("utf-8", errors="replace").rstrip("\r")
        if line.strip():
            self._q.put(line)

    # ── the pump ───────────────────────────────────────────────────────────

    def _pump(self) -> None:
        pending: list[str] = []
        window: collections.deque = collections.deque()
        first = 0.0
        eof = False
        try:
            while not self._stop.is_set():
                if time.monotonic() >= self._deadline:
                    self._set_reason(TIMEOUT)
                    break
                if not self._alive():
                    self._set_reason(LOST)
                    break
                try:
                    item = self._q.get(timeout=0.05)
                except queue.Empty:
                    item = None
                if item is _EOF:
                    eof = True
                elif item is not None:
                    if not pending:
                        first = time.monotonic()
                    pending.append(item)
                if pending and (eof or time.monotonic() - first >= BATCH_S):
                    if not self._emit(pending, window):
                        pending = []
                        break
                    pending = []
                if eof:
                    self._set_reason(ENDED)
                    break
        except Exception:  # noqa: BLE001 - a pump that dies silently is a watch that lies
            self._set_reason(LOST)
        finally:
            self._finish(pending, window)

    def _emit(self, lines: list[str], window: collections.deque) -> bool:
        """Deliver one batch. False means stop: flood, or delivery failed."""
        now = time.monotonic()
        while window and now - window[0] > FLOOD_WINDOW_S:
            window.popleft()
        if len(window) >= FLOOD_MAX_EVENTS:
            self._set_reason(FLOOD)
            return False
        window.append(now)
        self.events += 1
        try:
            self._deliver(self._format_event(lines))
        except Exception:  # noqa: BLE001 - the seat cannot be reached: nothing left to wake
            self._set_reason(LOST)
            return False
        return True

    def _format_event(self, lines: list[str]) -> str:
        kept: list[str] = []
        chars = 0
        for i, raw in enumerate(lines):
            line = _cut_line(raw)
            if i >= EVENT_MAX_LINES or chars + len(line) > EVENT_MAX_CHARS:
                where = (f"the log has all of it: {self.log_path.as_posix()}" if self.log_path
                         else f"read the rest from {self.target}")
                kept.append(f"[... {len(lines) - i} more line(s) cut — {where} ...]")
                break
            kept.append(line)
            chars += len(line) + 1
        n = len(lines)
        head = (f"[monitor {self.id} · {self.mode} · {self.label or self.target} · "
                f"event {self.events} · {n} line{'s' if n != 1 else ''}]")
        return head + "\n" + "\n".join(kept)

    def _finish(self, pending: list[str], window: collections.deque) -> None:
        reason = self._reason or STOPPED
        # Lines already read when the end came are still the seat's to see.
        if pending and reason in (ENDED, TIMEOUT):
            try:
                self.events += 1
                self._deliver(self._format_event(pending))
            except Exception:  # noqa: BLE001
                reason = LOST
        self._stop.set()
        self._kill()
        self.state = reason
        self.ended = time.time()
        if reason in _NOTIFIED:
            try:
                self._deliver(self._notice(reason))
            except Exception:  # noqa: BLE001 - last word undeliverable; the state is recorded
                pass

    def _notice(self, reason: str) -> str:
        who = f"[monitor {self.id} · {self.label or self.target}"
        if reason == TIMEOUT:
            return (f"{who} timed out after {self.timeout_s:g} s · {self.events} event(s) delivered] "
                    f"Re-arm with monitor start if you still need it.")
        if reason == FLOOD:
            return (f"{who} stopped by the flood guard: more than {FLOOD_MAX_EVENTS} events in "
                    f"{FLOOD_WINDOW_S:g} s · {self.events} event(s) delivered] Filter the source "
                    f"(for a command, grep --line-buffered) and re-arm.")
        code = f" with exit code {self.exit_code}" if self.exit_code is not None else ""
        return f"{who} ended: the command exited{code} · {self.events} event(s) delivered]"


class MonitorService:
    """The tool's body: arguments in, text out, and the set of live monitors."""

    def __init__(self, *, deliver: Callable[[str], None], convo_id: Callable[[], str],
                 running: Callable[[], bool] = lambda: True, agent_id: str | None = None,
                 argv: Callable[[str], tuple] | None = None, data_root: Callable[[], Path] | None = None):
        self._deliver = deliver
        self._convo_id = convo_id
        self._running = running
        self._agent_id = agent_id
        self._argv = argv or _default_argv
        self._data_root = data_root or paths.data_root
        self._ids = itertools.count(1)
        self._monitors: dict[str, Monitor] = {}
        self._lock = threading.Lock()

    def run(self, args: Mapping[str, object]) -> str:
        action = str(args.get("action") or "start").strip().lower()
        if action == "start":
            return self._start(args)
        if action == "stop":
            return self._stop(str(args.get("id") or "").strip())
        if action == "list":
            return self._list()
        return f"[error] monitor: unknown action {action!r} (start, stop, list)"

    def stop_all(self) -> None:
        for mon in list(self._monitors.values()):
            if mon.state == RUNNING:
                mon.stop(LOST)

    # ── start ──────────────────────────────────────────────────────────────

    def _start(self, args: Mapping[str, object]) -> str:
        command = str(args.get("command") or "").strip()
        raw_path = str(args.get("path") or "").strip()
        if bool(command) == bool(raw_path):
            return "[error] monitor start needs exactly one of `command` or `path`"
        try:
            timeout_ms = int(args.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        except (TypeError, ValueError):
            return "[error] monitor: timeout_ms must be an integer number of milliseconds"
        timeout_ms = max(MIN_TIMEOUT_MS, min(MAX_TIMEOUT_MS, timeout_ms))
        label = str(args.get("label") or "").strip()[:60]
        convo = self._convo_id() or ""
        with self._lock:
            if sum(1 for m in self._monitors.values() if m.state == RUNNING) >= MAX_ACTIVE:
                return f"[error] monitor: {MAX_ACTIVE} monitors are already running; stop one first"
            mid = f"m-{next(self._ids)}"
            self._prune()
            alive = self._alive_for(convo)
            if command:
                log = Path(self._data_root()).joinpath(*LOG_DIR) / f"{mid}-{int(time.time())}.log"
                mon = Monitor(mid, "command", command, label, timeout_ms / 1000, convo,
                              self._deliver, alive, log_path=log)
                argv, shell = self._argv(command)
                try:
                    mon.begin_command(argv, shell=shell, cwd=str(Path.cwd()),
                                      env=_process_env(self._agent_id))
                except OSError as e:
                    return f"[error] monitor: {type(e).__name__}: {e}"
                self._monitors[mid] = mon
                where = (f"output log: {mon.log_path.as_posix()}" if mon.log_path
                         else "no output log (it could not be created)")
                found = "every line it prints (stderr included) is an event"
            else:
                path = _resolve(raw_path)
                if path.is_dir():
                    return f"[error] monitor: {path.as_posix()} is a directory; name a file"
                mon = Monitor(mid, "file", path.as_posix(), label, timeout_ms / 1000, convo,
                              self._deliver, alive)
                found = mon.begin_file()
                self._monitors[mid] = mon
                where = f"watching {path.as_posix()}"
        return (f"[monitor {mid} started · {mon.mode} · {label or mon.target} · "
                f"timeout {timeout_ms / 1000:g} s]\n{where}; {found}.\n"
                f"Events arrive as messages tagged [inbox from monitor] and wake you when idle. "
                f"Continue other work or end the turn and wait; `monitor stop id={mid}` ends it.")

    def _alive_for(self, convo: str) -> Callable[[], bool]:
        def alive() -> bool:
            if not self._running():
                return False
            return not convo or self._convo_id() == convo
        return alive

    def _prune(self) -> None:
        ended = sorted((m for m in self._monitors.values() if m.state != RUNNING),
                       key=lambda m: m.ended or 0.0)
        for mon in ended[: max(0, len(ended) - KEEP_ENDED)]:
            self._monitors.pop(mon.id, None)

    # ── stop / list ────────────────────────────────────────────────────────

    def _mine(self) -> list[Monitor]:
        convo = self._convo_id() or ""
        return [m for m in self._monitors.values() if not m.convo_id or m.convo_id == convo]

    def _stop(self, mid: str) -> str:
        if not mid:
            return "[error] monitor stop needs `id`"
        mon = next((m for m in self._mine() if m.id == mid), None)
        if mon is None:
            return f"[error] no monitor {mid} in this conversation (monitor list shows them)"
        if mon.state != RUNNING:
            return f"{mid} already {mon.state} after {mon.seconds:.0f} s · {mon.events} event(s) delivered"
        mon.stop(STOPPED)
        return f"{mid} stopped after {time.time() - mon.started:.0f} s · {mon.events} event(s) delivered"

    def _list(self) -> str:
        rows = sorted(self._mine(), key=lambda m: m.started)
        if not rows:
            return "no monitors"
        return "\n".join(
            f"{m.id}  {m.state:<7} {m.seconds:6.0f} s  {m.events:>3} ev  {m.mode:<7} "
            f"{m.label or m.target}" + (f"  (log {m.log_path.as_posix()})" if m.log_path else "")
            for m in rows)


def _default_argv(command: str) -> tuple:
    """The same shell the bash tool runs: a real bash when the box has one."""
    from litetui.plugins import core_tools
    exe = core_tools.bash_exe()
    return ([exe, "-c", command], False) if exe is not None else (command, True)


def _process_env(agent_id: str | None) -> dict:
    from litetui import harness
    return harness.tool_process_env(agent_id)


def _resolve(raw: str) -> Path:
    from litetui.plugins.core_tools import _resolve_path
    return _resolve_path(raw)
