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
        self.entered = threading.Event()

    def write(self, line):
        if self._raise is not None:
            raise self._raise
        self.entered.set()
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
    assert original.stdin.entered.wait(2)
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


# ── stop(): reap the killed child (second wait) + join the reader ─────────────
def test_stop_reaps_a_killed_child_and_joins_the_reader(tmp_path):
    gate = threading.Event(); gate.set()
    waits = []

    class HardProc(_Proc):
        def terminate(self):                       # force the kill path
            raise OSError("terminate refused")
        def wait(self, timeout=None):
            waits.append(timeout)
            return 0

    proc = HardProc(gate)
    srv = _server(tmp_path, proc)
    reader = threading.Thread(target=lambda: None, daemon=True)   # already-finishing reader
    reader.start()
    srv._reader = reader
    srv.stop()
    assert proc.killed and waits                    # killed AND reaped (second wait after kill)
    assert not reader.is_alive()                    # reader joined


def test_stop_does_not_close_stdin_while_a_writer_is_retained(tmp_path):
    gate = threading.Event()                        # writer blocks -> retained
    proc = _Proc(gate)
    srv = _server(tmp_path, proc)
    with pytest.raises(mc.MCPError):
        srv._send({"jsonrpc": "2.0", "id": 1}, timeout=0.05)
    assert srv._writer.is_alive()                   # retained
    proc.stdin.closed = False
    # stop() is UNCERTAIN while the writer is live: it must RAISE (not silently
    # succeed) and must NOT close stdin under the live writer.
    with pytest.raises(mc.MCPError) as ei:
        srv.stop()
    assert "writer still alive" in str(ei.value).lower()
    assert proc.stdin.closed is False               # NEVER closed under the live writer
    # item 9: once the writer is terminal, a stop() RETRY confirms and clears.
    gate.set(); srv._writer.join(2)
    srv.stop()
    assert proc.stdin.closed is True                # safe to close once the writer is gone
    assert srv._quarantined is None


# ── persistent quarantine + start() boundary guard ───────────────────────────
def test_quarantine_persists_until_a_confirmed_stop(tmp_path):
    gate = threading.Event()                        # writer blocks -> retained
    proc = _Proc(gate)
    srv = _server(tmp_path, proc)
    with pytest.raises(mc.MCPError):
        srv._send({"jsonrpc": "2.0", "id": 1}, timeout=0.05)
    assert srv._quarantined is not None

    # Release + reap the writer: the writer retention clears...
    gate.set(); srv._writer.join(2)
    assert srv._writer_in_flight() is False

    # ...but the QUARANTINE is persistent: a fresh send is STILL refused until a
    # confirmed stop() clears it (item 2) — it is not auto-recovered by the
    # writer dying.
    with pytest.raises(mc.MCPError) as ei:
        srv._send({"jsonrpc": "2.0", "id": 2})
    assert "quarantined" in str(ei.value).lower()
    # and start() is refused for the same reason (item 8).
    with pytest.raises(mc.MCPError) as ei2:
        srv.start()
    assert "cannot start" in str(ei2.value).lower()

    # A confirmed stop() is the recovery path; it clears the quarantine.
    srv.stop()
    assert srv._quarantined is None
    assert srv.proc is None


def test_start_refuses_a_live_writer_and_an_unreaped_proc(tmp_path):
    # a live in-flight writer -> refuse (item 8)
    hold = threading.Event()
    w = threading.Thread(target=hold.wait, daemon=True); w.start()
    srv = mc.MCPServer("x", {}, tmp_path, io.StringIO())
    srv._writer = w
    with pytest.raises(mc.MCPError) as ei:
        srv.start()
    assert "in flight" in str(ei.value).lower()
    hold.set(); w.join(1)

    # a previous proc that has not exited -> refuse (item 8)
    srv2 = mc.MCPServer("y", {}, tmp_path, io.StringIO())
    srv2.proc = _Proc(threading.Event())            # poll() -> None (running)
    with pytest.raises(mc.MCPError) as ei2:
        srv2.start()
    assert "not reaped" in str(ei2.value).lower()


def test_stop_raises_uncertain_when_the_child_is_not_reaped(tmp_path):
    class NeverDies(_Proc):
        def terminate(self): raise OSError("terminate refused")
        def kill(self):      raise OSError("kill refused")
        def wait(self, timeout=None):
            raise TimeoutError("still alive")        # never reaped
        def poll(self):
            return None                              # still running
    proc = NeverDies(threading.Event())
    srv = _server(tmp_path, proc)
    with pytest.raises(mc.MCPError) as ei:
        srv.stop()
    assert "child not reaped" in str(ei.value).lower()
    # nothing was cleared: the handle is retained for a later verify + retry.
    assert srv.proc is proc
    assert srv._quarantined is not None


def test_stop_all_retains_uncertain_servers(tmp_path):
    m = mc.MCPManager(tmp_path)

    class OKServer:
        name = "ok"
        def stop(self):
            return None
    class BadServer:
        name = "bad"
        def stop(self):
            raise RuntimeError("child not reaped")
    ok, bad = OKServer(), BadServer()
    with m._servers_lock:
        m.servers = {"ok": ok, "bad": bad}
    m.stop_all()
    # cleanly stopped removed; uncertain RETAINED + quarantined, truthful failure
    assert "ok" not in m.servers
    assert m.servers.get("bad") is bad
    assert "bad" in m._stop_failed and "ok" not in m._stop_failed
    assert "bad" in m.failures
    assert m._closing is True


def test_concurrent_send_cannot_replace_a_writer_before_start(tmp_path, monkeypatch):
    gate = threading.Event()
    proc = _Proc(gate)
    srv = _server(tmp_path, proc)
    registered = threading.Event()
    release_start = threading.Event()
    second_entered = threading.Event()
    errors = []
    original_start = threading.Thread.start

    def paused_start(thread):
        if thread.name == "mcp-writer-x":
            registered.set()
            assert release_start.wait(2)
        return original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", paused_start)

    def send(second=False):
        if second:
            second_entered.set()
        try:
            srv._send({"id": 2 if second else 1}, timeout=0.2)
        except mc.MCPError as exc:
            errors.append(str(exc))

    first = threading.Thread(target=send)
    second = threading.Thread(target=lambda: send(True))
    first.start()
    assert registered.wait(2)
    writer = srv._writer
    second.start()
    assert second_entered.wait(2)
    try:
        assert srv._send_lock.acquire(blocking=False) is False
    finally:
        release_start.set()
    first.join(2)
    second.join(2)
    assert not first.is_alive() and not second.is_alive()
    assert srv._writer is writer
    assert len(errors) == 2
    gate.set()
    writer.join(2)
    srv.stop()


def test_stop_refuses_new_send_during_child_teardown(tmp_path):
    gate = threading.Event()
    gate.set()
    proc = _Proc(gate)
    srv = _server(tmp_path, proc)
    original_terminate = proc.terminate

    def terminate():
        with pytest.raises(mc.MCPError, match="quarantined"):
            srv._send({"id": 1})
        original_terminate()

    proc.terminate = terminate
    srv.stop()
    assert not proc.stdin.written
    assert srv._quarantined is None
