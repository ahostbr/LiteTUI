"""Bounded stdio writes + zombie/reader-safe shutdown for MCPServer.

🔴 THE TWO BUGS THIS PINS (the HELD 09c8b40/210bdf4 design, integrated with the
five review corrections).

1. `_send` wrote+flushed on the CALLER's thread. A child that stops reading and
   lets its stdin pipe buffer fill blocks that write FOREVER, and `call()` holds
   `self._lock` across the request -- so the serialized tool call hangs the
   whole UI. The write now runs on a joinable thread so the caller is bounded;
   the blocked writer is RETAINED (never joined, its stdin never closed under
   it -- the blocked write holds the BufferedWriter lock) until observed
   terminal.

2. `stop()` on the exception path only `kill()`ed -- no second `wait()`, so a
   killed child was left a zombie -- and it never joined the reader thread, so
   a stopped server leaked a live reader.

⏱️ WHY THE ASSERTIONS DRIVE THE WORK ON A THREAD. With the send bug present the
call never returns, so a direct call would hang the suite. Every assertion is
"did it finish inside a budget" -- the property actually under test. BUDGET is
generous next to the small timeouts so a loaded machine can't turn a real pass
into a flake (bounded at all, not to the millisecond).

No real process is ever spawned: the child is a fake proc whose stdin/stdout are
thread-controlled fakes.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

from litetui import mcp_client
from litetui.mcp_client import MCPServer, MCPError, SEND_TIMEOUT

# Generous next to the sub-second timeouts under test.
BUDGET = 5.0


class _Pipe:
    """A stdout yielding the given lines then returning the given sleep-then-EOF.

    A short pre-EOF sleep lets a test PROVE `stop` joins the reader: a reader
    that takes ~0.4s to reach EOF is only observably dead once `stop` has
    waited for it.
    """

    def __init__(self, lines=None, pre_eof_sleep: float = 0.0):
        self._lines = list(lines or [])
        self._pre_eof_sleep = pre_eof_sleep

    def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._pre_eof_sleep:
            time.sleep(self._pre_eof_sleep)
        return ""


class _BlockingStdin:
    """A stdin whose write() blocks until the child reads (which, in the
    timeout test, never happens) -- the filled-pipe case."""

    def __init__(self):
        self._release = threading.Event()
        self.blocked = threading.Event()
        self.closed = False
        self.written = 0

    def write(self, data):
        self.written += 1
        self.blocked.set()
        self._release.wait()          # block until released (or the writer is abandoned)
        return len(data)

    def flush(self):
        pass

    def close(self):
        self.closed = True
        self._release.set()          # a close lets any blocked writer finish

    def release(self):
        self._release.set()


class _RaisingStdin:
    """A stdin whose write() immediately raises (broken pipe)."""

    def __init__(self, exc=BrokenPipeError):
        self._exc = exc

    def write(self, data):
        raise self._exc("child closed the pipe")

    def flush(self):
        pass

    def close(self):
        pass


class _Proc:
    """The subset of subprocess.Popen MCPServer touches, with call counters."""

    def __init__(self, stdin=None, lines=None, exit_code=None,
                 terminate_raises=False, pre_eof_sleep=0.0):
        self.stdin = stdin
        self.stdout = _Pipe(lines, pre_eof_sleep)
        self.returncode = exit_code
        self.terminated = 0
        self.killed = 0
        self.waits = []
        self._terminate_raises = terminate_raises

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        if self._terminate_raises:
            raise OSError("terminate failed")
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        self.waits.append(timeout)
        return self.returncode


def _server(proc: _Proc) -> MCPServer:
    srv = MCPServer("probe", {}, Path("."), io.StringIO())
    srv.proc = proc
    srv._start_reader()
    return srv


def _in_budget(fn, budget: float = BUDGET):
    """Run fn on a daemon worker thread; return (finished, box, elapsed)."""
    box: dict = {}

    def go():
        t0 = time.monotonic()
        try:
            box["value"] = fn()
        except BaseException as e:  # noqa: BLE001 — the exception IS the result here
            box["exc"] = e
        box["elapsed"] = time.monotonic() - t0

    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(budget)
    return (not th.is_alive()), box, box.get("elapsed")


# ── the bounded write ────────────────────────────────────────────────────────

def test_send_is_bounded_when_the_child_stops_reading():
    """The defect, stated as an assertion: a filled stdin pipe must not hang the
    caller. The honest caller bound is the write-wait PLUS the kill that follows
    it (a truthfully bounded total), not the write-wait alone."""
    srv = _server(_Proc(stdin=_BlockingStdin()))
    timeout = 0.5
    finished, box, elapsed = _in_budget(lambda: srv._send({"id": 1}, timeout=timeout))
    assert finished, f"_send blocked the caller past {BUDGET}s (unbounded write)"
    assert isinstance(box.get("exc"), MCPError), f"expected MCPError, got {box!r}"
    # Truthful budget: bounded by the write-wait + the follow-up kill, not unbounded.
    assert elapsed < timeout + mcp_client._KILL_TOTAL + 1.0
    assert "retained" in str(box["exc"])


def test_CONTROL_a_write_through_that_is_read_completes_and_keeps_the_server_usable():
    srv = _server(_Proc(stdin=io.StringIO()))
    finished, box, _ = _in_budget(lambda: srv._send({"id": 1}, timeout=BUDGET))
    assert finished and box.get("exc") is None, f"write-through refused: {box.get('exc')!r}"
    # Not retained: a second write still goes through.
    finished2, box2, _ = _in_budget(lambda: srv._send({"id": 2}, timeout=BUDGET))
    assert finished2 and box2.get("exc") is None


def test_a_second_send_is_refused_while_a_writer_is_retained():
    """Uncancellable daemon writers must not pile up: once one is retained, the
    next send is refused until it is observed terminal."""
    srv = _server(_Proc(stdin=_BlockingStdin()))
    finished, box, _ = _in_budget(lambda: srv._send({"id": 1}, timeout=0.4))
    assert finished and isinstance(box.get("exc"), MCPError)
    # A second send now sees the retained writer and is refused (not piled on).
    finished2, box2, _ = _in_budget(lambda: srv._send({"id": 2}, timeout=0.4))
    assert finished2 and isinstance(box2.get("exc"), MCPError)
    assert "retained" in str(box2["exc"])


def test_a_blocked_send_kills_the_captured_child_not_a_replacement():
    """A reconnect can replace self.proc mid-write; the timeout teardown must act
    on the CAPTURED child, never a replacement it has not written to."""
    a = _BlockingStdin()
    proc_a = _Proc(stdin=a)
    srv = _server(proc_a)
    proc_b = _Proc(stdin=io.StringIO())
    box: dict = {}

    def send():
        try:                       # capture, don't leak, the expected MCPError
            srv._send({"id": 1}, timeout=0.6)
        except BaseException as e:  # noqa: BLE001
            box["exc"] = e

    th = threading.Thread(target=send, daemon=True)
    th.start()
    assert a.blocked.wait(2.0), "the write never reached the blocking stdin"
    srv.proc = proc_b                 # a reconnect swapped in a new child
    th.join(BUDGET)
    assert isinstance(box.get("exc"), MCPError), f"expected the retained timeout, got {box!r}"
    assert proc_a.terminated or proc_a.killed, "the captured child was not torn down"
    assert proc_b.terminated == 0 and proc_b.killed == 0, "a replacement child was killed"


def test_a_broken_pipe_is_a_bounded_type_only_error():
    srv = _server(_Proc(stdin=_RaisingStdin()))
    finished, box, elapsed = _in_budget(lambda: srv._send({"id": 1}, timeout=0.5))
    assert finished, "a broken pipe must not hang the caller"
    assert isinstance(box.get("exc"), MCPError)
    assert "BrokenPipeError" in str(box["exc"])


def test_the_retained_failure_is_recorded_on_the_server_for_the_manager():
    """Correction 2 (retained/typed failure integration): a timed-out send is not
    silently dropped -- the server records it so the manager's describe()
    reports the server as failed rather than a bare 'connected'."""
    srv = _server(_Proc(stdin=_BlockingStdin()))
    finished, box, _ = _in_budget(lambda: srv._send({"id": 1}, timeout=0.4))
    assert finished and isinstance(box.get("exc"), MCPError)
    assert srv.error, "the send timeout was not recorded for the manager"
    assert "timed out" in srv.error


# ── the reaping, reader-joining shutdown ─────────────────────────────────────

def test_stop_reaps_the_killed_child_on_the_kill_fallback():
    """Correction: the kill fallback must wait() AGAIN to reap the child, or the
    process is left a zombie."""
    srv = _server(_Proc(terminate_raises=True))
    srv.stop()
    assert srv.proc.killed == 1, "the kill fallback was not taken"
    assert srv.proc.waits, "the killed child was not reaped (no wait after kill)"


def test_stop_reaps_on_the_clean_terminate_path_too():
    srv = _server(_Proc())
    srv.stop()
    assert srv.proc.terminated == 1
    assert srv.proc.waits, "the terminated child was not reaped"


def test_stop_joins_the_reader_thread():
    """A stopped server must not leak a live reader: stop() waits for the reader
    to hit EOF and exit. A reader that takes ~0.4s to reach EOF is only
    observably dead once stop() has joined it."""
    srv = _server(_Proc(pre_eof_sleep=0.4))
    assert srv._reader is not None and srv._reader.is_alive()
    srv.stop()
    assert not srv._reader.is_alive(), "stop() returned while the reader still ran"


def test_stop_does_not_close_stdin_while_a_writer_is_retained():
    """Closing stdin under a retained writer would deadlock on the BufferedWriter
    lock; the kill closes the child's read end instead."""
    blocking = _BlockingStdin()
    srv = _server(_Proc(stdin=blocking))
    finished, box, _ = _in_budget(lambda: srv._send({"id": 1}, timeout=0.4))
    assert finished and isinstance(box.get("exc"), MCPError)
    srv.stop()
    assert not blocking.closed, "stdin was closed under a retained (blocked) writer"


# ── the five review corrections, made explicit ───────────────────────────────

def test_restart_is_refused_while_a_writer_is_retained():
    """Correction 3 (restart refusal): a restart would replace self.proc under a
    still-blocked writer, so start() must refuse until the writer is terminal."""
    srv = _server(_Proc(stdin=_BlockingStdin()))
    finished, box, _ = _in_budget(lambda: srv._send({"id": 1}, timeout=0.4))
    assert finished and isinstance(box.get("exc"), MCPError)

    def restart():
        srv.start()

    finished2, box2, _ = _in_budget(restart)
    assert finished2 and isinstance(box2.get("exc"), MCPError)
    assert "retained" in str(box2["exc"]).lower() or "restart" in str(box2["exc"]).lower()


def test_stop_called_from_the_reader_thread_does_not_join_itself():
    """Correction 4 (self-reader join guard): if stop() is ever reached from the
    reader thread, joining self would stall for the whole join budget. The guard
    skips it, so stop returns fast."""
    srv = _server(_Proc())

    def stop_from_reader():
        srv._reader = threading.current_thread()   # simulate being on the reader
        srv.stop()

    finished, box, elapsed = _in_budget(stop_from_reader)
    assert finished and box.get("exc") is None
    assert elapsed < 1.5, f"stop() self-joined the reader (elapsed {elapsed:.2f}s)"


def test_send_caller_bound_is_the_honest_total():
    """Correction 5 (truthful timeout budgets): the caller's worst case is the
    write-wait PLUS the kill, and that total is a named constant -- not implied
    to be the write-wait alone. Assert the constant is real and the measured
    elapsed sits under it."""
    assert mcp_client._KILL_TOTAL >= mcp_client._TERMINATE_WAIT + mcp_client._KILL_WAIT - 1e-9
    srv = _server(_Proc(stdin=_BlockingStdin()))
    timeout = 0.5
    finished, box, elapsed = _in_budget(lambda: srv._send({"id": 1}, timeout=timeout))
    assert finished
    assert elapsed < timeout + mcp_client._KILL_TOTAL + 1.0
