"""MCPServer stdio transport: bounded _send with a RETAINED writer. No real
process — a fake Popen whose stdin.write blocks under our control, and whose
terminate/kill do NOT unblock the writer (simulating a descendant that inherited
the read end), so the retain-until-observed-terminal contract is exercised.

Run only this file:
    python -m pytest tests/test_mcp_transport.py
"""
from __future__ import annotations

import io
import threading

import pytest

import litetui.mcp_client as mc


class _Stdin:
    def __init__(self, gate: threading.Event, *, raise_on_write=None):
        self._gate = gate
        self._raise = raise_on_write
        self.written: list[str] = []
        self.closed = False

    def write(self, line):
        if self._raise is not None:
            raise self._raise
        self._gate.wait()          # block until released (never, for the timeout test)
        self.written.append(line)

    def flush(self):
        pass

    def close(self):
        self.closed = True         # _send must NEVER call this while a writer is live


class _Proc:
    def __init__(self, gate, *, raise_on_write=None):
        self.stdin = _Stdin(gate, raise_on_write=raise_on_write)
        self.returncode = 0
        self.terminated = False
        self.killed = False

    def poll(self):
        return None                # running

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True     # deliberately does NOT unblock the writer

    def kill(self):
        self.killed = True


def _server(tmp_path, proc) -> mc.MCPServer:
    srv = mc.MCPServer("x", {}, tmp_path, io.StringIO())
    srv.proc = proc
    return srv


def test_send_writes_when_the_pipe_accepts_it(tmp_path):
    gate = threading.Event(); gate.set()
    proc = _Proc(gate)
    srv = _server(tmp_path, proc)
    srv._send({"jsonrpc": "2.0", "id": 1})
    assert proc.stdin.written and proc.stdin.written[0].endswith("\n")
    assert srv._writer is None                       # terminal writer cleared, not retained


def test_blocked_write_bounds_caller_kills_child_and_retains_writer(tmp_path):
    gate = threading.Event()                         # never set -> the write blocks
    proc = _Proc(gate)
    srv = _server(tmp_path, proc)
    with pytest.raises(mc.MCPError) as ei:
        srv._send({"jsonrpc": "2.0", "id": 1}, timeout=0.05)
    assert "retained" in str(ei.value).lower()       # caller bounded
    assert srv.error and "timed out" in srv.error
    assert proc.terminated                           # killed the CAPTURED child's read end
    assert proc.stdin.closed is False                # NEVER closed under the live writer
    assert srv._writer is not None and srv._writer.is_alive()   # retained, still blocked

    # A second send is refused while the writer is retained (no daemon pile-up).
    with pytest.raises(mc.MCPError) as ei2:
        srv._send({"jsonrpc": "2.0", "id": 2})
    assert "still blocked" in str(ei2.value).lower()

    gate.set()                                       # the descendant finally released it
    srv._writer.join(2)
    assert not srv._writer.is_alive()
    assert srv._writer_in_flight() is False           # observed terminal -> retention reaped


def test_timeout_kills_the_captured_proc_not_a_replacement(tmp_path):
    gate = threading.Event()
    original = _Proc(gate)
    srv = _server(tmp_path, original)

    replacement = _Proc(threading.Event())
    # Swap the manager's proc to a fresh one WHILE the write is blocked: only a
    # thread can do that mid-_send, so approximate it by starting _send in a
    # thread, swapping, then asserting the ORIGINAL (captured) proc was killed.
    done = threading.Event()

    def _do_send():
        try:
            srv._send({"jsonrpc": "2.0", "id": 1}, timeout=0.05)
        except mc.MCPError:
            pass
        finally:
            done.set()

    th = threading.Thread(target=_do_send, daemon=True)
    th.start()
    srv.proc = replacement                            # reconnect replaced the proc
    done.wait(2)
    assert original.terminated                        # the captured proc was killed
    assert replacement.terminated is False            # the replacement was NOT
    gate.set()


def test_broken_pipe_is_a_bounded_error(tmp_path):
    gate = threading.Event(); gate.set()
    proc = _Proc(gate, raise_on_write=BrokenPipeError("gone"))
    srv = _server(tmp_path, proc)
    with pytest.raises(mc.MCPError) as ei:
        srv._send({"jsonrpc": "2.0", "id": 1})
    assert "send failed" in str(ei.value).lower()
    assert "BrokenPipeError" in str(ei.value) and "gone" not in str(ei.value)   # type only
    assert srv._writer is None
