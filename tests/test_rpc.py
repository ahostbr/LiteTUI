"""T507-T6 — RPC behavioral tests.

Spawns `litetui --rpc` and drives it through JSONL stdin/stdout.
Live subprocess cases require explicit LITETUI_E2E=1; collection never probes.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from litetui import settings as settings_mod

# Never probe at collection. Live execution requires explicit opt-in.
_needs_backend = pytest.mark.skipif(
    os.environ.get("LITETUI_E2E") != "1", reason="requires LITETUI_E2E=1"
)


def _child_env(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    # Read-only selection from caller config; all writes use the isolated copy.
    settings = settings_mod.load()
    settings.mcp_enabled = False
    settings.plugins_disabled = ["scheduler", "skills", "harness", "glassbox"]
    settings_mod.save(settings, root=root)
    return dict(os.environ, LITETUI_DATA_ROOT=str(root), LITETUI_NO_HARNESS="1")


class RpcSession:
    """Manage a litetui --rpc subprocess."""

    def __init__(self, extra_args: list[str] | None = None, cwd: str | None = None):
        cmd = [sys.executable, "-m", "litetui.cli", "--rpc"]
        if extra_args:
            cmd.extend(extra_args)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=cwd or os.getcwd(),
            env=_child_env(Path(cwd or os.getcwd()) / "rpc-data"),
            text=True,
            bufsize=1,
        )
        self.events: list[dict] = []
        self._start_reader()

    def _start_reader(self):
        self._lines = queue.Queue()

        def read():
            try:
                for line in iter(self.proc.stdout.readline, ""):
                    self._lines.put(line)
            finally:
                self._lines.put(None)

        self._reader = threading.Thread(target=read, daemon=True)
        self._reader.start()

    def send(self, cmd: dict) -> None:
        assert self.proc.stdin
        self.proc.stdin.write(json.dumps(cmd) + "\n")
        self.proc.stdin.flush()

    def read_until(self, predicate, timeout: float = 120.0) -> dict | None:
        """Read events until predicate(event) is true or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                line = self._lines.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            self.events.append(evt)
            if predicate(evt):
                return evt
        return None

    def read_events(self, timeout: float = 5.0) -> list[dict]:
        before = len(self.events)
        self.read_until(lambda event: False, timeout)
        return self.events[before:]

    def wait_ready(self, timeout: float = 30.0) -> dict | None:
        return self.read_until(lambda e: e.get("type") == "ready", timeout=timeout)

    def close(self):
        try:
            self.send({"type": "shutdown", "id": "fin"})
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()


@pytest.fixture
def session(tmp_path):
    s = RpcSession(cwd=str(tmp_path))
    yield s
    s.close()


@_needs_backend
class TestRpcTurn:
    def test_ready_event(self, session: RpcSession):
        ready = session.wait_ready()
        assert ready is not None, "should receive ready event"
        assert ready["type"] == "ready"
        assert "version" in ready
        assert "model" in ready
        assert "cwd" in ready

    def test_prompt_turn(self, session: RpcSession):
        ready = session.wait_ready()
        assert ready is not None

        session.send({"type": "prompt", "id": "p1", "message": "reply exactly: rpc ok"})

        # Collect events until turn_end
        turn_end = session.read_until(
            lambda e: e.get("type") == "turn_end",
            timeout=120,
        )

        # Find the response for our prompt
        prompt_resp = next(
            (e for e in session.events if e.get("type") == "response" and e.get("id") == "p1"),
            None,
        )
        assert prompt_resp is not None, "prompt should get a response"
        assert prompt_resp["ok"] is True

        # Should have turn_start
        starts = [e for e in session.events if e.get("type") == "turn_start"]
        assert len(starts) >= 1, "should have turn_start"

        # Should have turn_end
        assert turn_end is not None, "should have turn_end"

        # Text deltas
        deltas = [e for e in session.events if e.get("type") == "text_delta"]
        text = "".join(d.get("text", "") for d in deltas)
        print(f"[T507] reply: {len(text)} chars, {len(deltas)} deltas")

    def test_list_models(self, session: RpcSession):
        ready = session.wait_ready()
        assert ready is not None

        session.send({"type": "list_models", "id": "lm1"})
        resp = session.read_until(
            lambda e: e.get("type") == "response" and e.get("id") == "lm1",
            timeout=10,
        )
        assert resp is not None
        assert resp["ok"] is True
        assert isinstance(resp["result"], list)

    def test_set_thinking(self, session: RpcSession):
        ready = session.wait_ready()
        assert ready is not None

        session.send({"type": "set_thinking", "id": "st1", "level": "low"})
        resp = session.read_until(
            lambda e: e.get("type") == "response" and e.get("id") == "st1",
            timeout=10,
        )
        assert resp is not None
        assert resp["ok"] is True

        session.send({"type": "get_settings", "id": "gs1"})
        resp = session.read_until(
            lambda e: e.get("type") == "response" and e.get("id") == "gs1",
            timeout=10,
        )
        assert resp is not None
        assert resp["result"]["thinking_level"] == "low"

    def test_unknown_command(self, session: RpcSession):
        ready = session.wait_ready()
        assert ready is not None

        session.send({"type": "nope", "id": "bad1"})
        resp = session.read_until(
            lambda e: e.get("type") == "response" and e.get("id") == "bad1",
            timeout=10,
        )
        assert resp is not None
        assert resp["ok"] is False
        assert "unknown" in resp.get("error", "").lower()


@_needs_backend
class TestStdoutPurity:
    """T519: fd 1 in --rpc mode carries ONLY valid JSONL — no Textual escape codes."""

    def test_every_stdout_line_is_json_no_esc(self, tmp_path):
        """Start --rpc, collect raw stdout bytes, assert no ESC and all lines parse as JSON."""
        proc = subprocess.Popen(
            [sys.executable, "-m", "litetui.cli", "--rpc"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=str(tmp_path),
            env=_child_env(tmp_path / "rpc-data"),
        )
        try:
            # Read raw stdout in a thread so we can timeout
            import threading
            raw_bytes = bytearray()
            def reader():
                assert proc.stdout
                while True:
                    chunk = proc.stdout.read(4096)
                    if not chunk:
                        break
                    raw_bytes.extend(chunk)
            t = threading.Thread(target=reader, daemon=True)
            t.start()
            t.join(timeout=20)

            # Shut down
            try:
                assert proc.stdin
                proc.stdin.write(b'{"type":"shutdown","id":"fin"}\n')
                proc.stdin.flush()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                proc.kill()
                proc.wait()

            raw = raw_bytes.decode("utf-8", errors="replace")
            lines = [l for l in raw.split("\n") if l.strip()]
            assert len(lines) >= 1, "should have at least the ready event"

            bad: list[str] = []
            for line in lines:
                if "\x1b" in line:
                    bad.append(f"ESC in: {line[:80]}")
                try:
                    json.loads(line)
                except json.JSONDecodeError:
                    bad.append(f"non-JSON: {line[:80]}")
            assert bad == [], f"stdout lines must be clean JSON: {bad}"

            # The ready event must be present
            events = [json.loads(l) for l in lines]
            ready = next((e for e in events if e.get("type") == "ready"), None)
            assert ready is not None, "ready event should be in stdout"
        finally:
            proc.kill()
            proc.wait()


@_needs_backend
class TestArgv:
    def test_prompt_argv(self, tmp_path):
        """GP3: --prompt submits the first turn."""
        s = RpcSession(
            extra_args=["--prompt", "reply exactly: argv ok"],
            cwd=str(tmp_path),
        )
        try:
            ready = s.wait_ready()
            assert ready is not None

            # The first turn should fire automatically
            turn_end = s.read_until(
                lambda e: e.get("type") == "turn_end",
                timeout=120,
            )
            assert turn_end is not None, "the argv prompt should produce a turn"
            deltas = [e for e in s.events if e.get("type") == "text_delta"]
            text = "".join(d.get("text", "") for d in deltas)
            print(f"[T507-argv] reply: {len(text)} chars")
        finally:
            s.close()
