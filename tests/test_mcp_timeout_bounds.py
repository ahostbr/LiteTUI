"""An MCP timeout must bound a server that is ALIVE and SILENT.

🔴 THE BUG THIS PINS. `MCPServer._read_until()` checked its deadline and then
called `self.proc.stdout.readline()` — a blocking read on the calling thread. A
server that stays alive and never emits a newline holds that call forever: the
deadline is never re-examined, `proc.poll()` is never re-examined, and the
declared timeout expires only in the docstring. Affects BOTH `start()`
(initialize / tools/list) and every `call()`.

BoldChip's stalled-pipe probe was still blocked after 0.5s against a declared
0.1s timeout.

⚠️ AND THE MODULE DOCSTRING ASSERTED THE OPPOSITE — "Every wait is bounded — a
hung server must degrade to one failed tool call, never a frozen UI." That
sentence described the intent and was read as a description of the code. Second
instance of the same shape in this repo today; the first was
`_sync_seat_identity`'s claim that the next heartbeat would re-register.

⏱️ WHY THESE TESTS DRIVE THE READ ON A WORKER THREAD. With the bug present the
call never returns, so a direct call would hang the whole suite rather than fail
it. Every assertion below is therefore "did it finish inside a budget", which is
the property actually under test — and it fails FAST when the bound is missing.
"""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest

import mcp_client
from mcp_client import MCPError, MCPServer

# Generous next to the 0.1s timeouts under test: the assertion is "bounded at
# all", not "bounded to the millisecond". A machine under load must not turn a
# real pass into a flake.
BUDGET = 5.0


class _Pipe:
    """A stdout that yields the lines it is given, then stalls ALIVE.

    Stalling rather than returning "" is the whole point: an empty read is EOF,
    which the old code already handled. The unhandled case is a live server
    holding the pipe open and saying nothing.
    """

    def __init__(self, lines: list[str] | None = None):
        self._lines = list(lines or [])
        self._stalled = threading.Event()
        self.reads_after_stall = 0

    def readline(self) -> str:
        if self._lines:
            return self._lines.pop(0)
        self._stalled.set()
        self.reads_after_stall += 1
        # Alive and silent. Bounded so a leaked reader thread cannot outlive
        # the test session by much.
        time.sleep(30)
        return ""

    def wait_until_stalled(self, timeout: float = 2.0) -> bool:
        return self._stalled.wait(timeout)

    def close(self) -> None:
        pass


class _Proc:
    """The subset of subprocess.Popen that MCPServer actually touches."""

    def __init__(self, lines: list[str] | None = None, exit_code: int | None = None):
        self.stdout = _Pipe(lines)
        self.stdin = io.StringIO()
        self.returncode = exit_code
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated += 1
        self.returncode = -15

    def kill(self):
        self.killed += 1
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


def _server(lines=None, exit_code=None) -> MCPServer:
    srv = MCPServer("probe", {}, Path("."), io.StringIO())
    srv.proc = _Proc(lines, exit_code)
    srv._start_reader()
    return srv


def _frame(rid: int, result=None) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": rid, "result": result or {"ok": True}}) + "\n"


def _in_budget(fn, budget: float = BUDGET):
    """Run fn on a worker thread; return (finished, value_or_exc, elapsed)."""
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


# ── the bound ───────────────────────────────────────────────────────────────

def test_a_stalled_but_live_server_times_out_instead_of_blocking_forever():
    """The defect, stated as an assertion. Nothing is on the pipe and the
    process never exits, so only a real bound can end this call."""
    srv = _server()
    finished, box, elapsed = _in_budget(lambda: srv._read_until(1, timeout=0.1))

    assert finished, (
        f"_read_until never returned within {BUDGET}s against a declared 0.1s "
        "timeout — the read is still blocking the caller"
    )
    assert isinstance(box.get("exc"), MCPError), f"expected MCPError, got {box!r}"
    assert elapsed < BUDGET


def test_the_bound_holds_for_INITIALIZE_too_not_only_tool_calls():
    """`start()` waits on the same primitive, so a server that stalls during
    the handshake would hang the app at boot rather than at first use."""
    srv = _server()
    finished, box, _ = _in_budget(
        lambda: srv._request("initialize", {}, timeout=0.1)
    )
    assert finished, "the handshake path is still unbounded"
    assert isinstance(box.get("exc"), MCPError)


def test_a_timeout_POISONS_the_connection_so_the_next_call_cannot_inherit_it():
    """A server that missed its deadline may still deliver that frame later,
    which would pair with the NEXT request and succeed with the wrong answer —
    worse than a timeout, because it looks like a result."""
    srv = _server()
    finished, _, _ = _in_budget(lambda: srv._read_until(1, timeout=0.1))
    assert finished

    assert srv.proc.terminated or srv.proc.killed, (
        "the stalled process was left running to poison the next call"
    )


# ── controls: the bound must not break the working path ─────────────────────

def test_CONTROL_a_responsive_server_still_returns_its_frame():
    """A test that only proves things time out would pass with a client that
    can no longer talk to anything."""
    srv = _server([_frame(1, {"tools": []})])
    finished, box, _ = _in_budget(lambda: srv._read_until(1, timeout=5.0))
    assert finished
    assert box.get("exc") is None, f"a good server was refused: {box.get('exc')!r}"
    assert box["value"]["result"] == {"tools": []}


def test_CONTROL_a_notification_before_the_answer_is_skipped_not_mistaken_for_it():
    """Servers interleave their own notifications with responses. Matching on
    id is what stops a response pairing with the wrong request."""
    note = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}) + "\n"
    srv = _server([note, _frame(7, {"v": 7})])
    finished, box, _ = _in_budget(lambda: srv._read_until(7, timeout=5.0))
    assert finished and box.get("exc") is None
    assert box["value"]["result"] == {"v": 7}


def test_CONTROL_a_banner_on_stdout_is_logged_not_treated_as_a_protocol_error():
    """Servers that print a banner to stdout are common; their noise must not
    become a failure."""
    srv = _server(["starting up, please wait\n", _frame(3)])
    finished, box, _ = _in_budget(lambda: srv._read_until(3, timeout=5.0))
    assert finished and box.get("exc") is None
    assert box["value"]["id"] == 3


def test_an_out_of_order_response_is_HELD_not_dropped():
    """The reader now drains the pipe continuously, so a frame for another id
    can arrive while this one is being awaited. Dropping it would strand the
    request that owns it — a hang with no timeout attached to it."""
    srv = _server([_frame(2, {"second": True}), _frame(1, {"first": True})])

    finished, box, _ = _in_budget(lambda: srv._read_until(1, timeout=5.0))
    assert finished and box.get("exc") is None
    assert box["value"]["result"] == {"first": True}

    finished2, box2, _ = _in_budget(lambda: srv._read_until(2, timeout=5.0))
    assert finished2 and box2.get("exc") is None, "the earlier frame was thrown away"
    assert box2["value"]["result"] == {"second": True}


def test_a_server_that_EXITED_is_reported_as_an_exit_not_as_a_timeout():
    """The two failures need different fixes, so they must not read alike."""
    srv = MCPServer("probe", {}, Path("."), io.StringIO())
    srv.proc = _Proc(lines=[""], exit_code=3)   # EOF, already dead
    srv._start_reader()

    finished, box, _ = _in_budget(lambda: srv._read_until(1, timeout=5.0))
    assert finished
    exc = box.get("exc")
    assert isinstance(exc, MCPError)
    assert "timed out" not in str(exc).lower(), f"an exit was reported as a timeout: {exc}"


# ── the claim in the module docstring is now true ───────────────────────────

def test_the_read_no_longer_blocks_on_the_CALLERS_thread():
    """Structural guard. The defect was `stdout.readline()` executing on the
    thread that owns the deadline; a dedicated reader is what moves it off.
    Asserted on the AST rather than on text, because a docstring quoting the
    old call would match a string search — that trap fired once already today
    in the sibling seat test.
    """
    import ast

    src = Path(mcp_client.__file__).read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.FunctionDef) and n.name == "_read_until"
    )
    for node in ast.walk(fn):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr != "readline", (
                "_read_until still performs the blocking read on the caller's thread"
            )
