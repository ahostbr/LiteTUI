"""Parent-owned optional sidecar preview lifecycle.

No settings or scheduler authority crosses this connection yet. The child is
only a presentation preview; never replace a Textual editor with it.
"""
from __future__ import annotations

import os
import queue
import secrets
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from litetui import sidecar_protocol

VIEWS = frozenset({"timeline", "settings", "calendar", "job"})


class SidecarWindow:
    def __init__(self, executable: Path, *, spawn: Callable = subprocess.Popen,
                 warn: Callable[[str], None] | None = None, timeout: float = 3):
        self.executable = Path(executable)
        self.spawn = spawn
        self.warn = warn or (lambda _message: None)
        self.timeout = timeout
        self.process = None
        self.token = ""
        self._next_id = 1
        self._exchange_lock = threading.Lock()

    def _exchange(self, command: str, payload: object) -> dict:
        if not self._exchange_lock.acquire(blocking=False):
            raise RuntimeError("Sidecar pipe is busy")
        try:
            return self._exchange_locked(command, payload)
        finally:
            self._exchange_lock.release()

    def _start_reader(self, process) -> None:
        assert process.stdout is not None
        stdout = process.stdout
        self._pending: dict[int, queue.Queue] = {}
        self._reader_error: Exception | None = None

        def read_replies() -> None:
            try:
                while self.process is process:
                    raw = stdout.readline(sidecar_protocol.MAX_FRAME_BYTES + 2)
                    if not raw:
                        raise RuntimeError("Sidecar disconnected")
                    if len(raw) > sidecar_protocol.MAX_FRAME_BYTES + 1 or not raw.endswith(b"\n"):
                        raise ValueError("Invalid sidecar reply length")
                    frame = sidecar_protocol.decode(raw[:-1], self.token)
                    if frame["command"] != "reply" or not isinstance(frame["payload"], dict):
                        raise ValueError("Unexpected sidecar reply")
                    pending = self._pending.get(frame["id"])
                    if pending is None:
                        raise ValueError("Unmatched sidecar reply")
                    pending.put_nowait(frame["payload"])
            except (OSError, RuntimeError, ValueError) as exc:
                self._reader_error = exc
            finally:
                error = self._reader_error or RuntimeError("Sidecar disconnected")
                for pending in tuple(self._pending.values()):
                    try:
                        pending.put_nowait(error)
                    except queue.Full:
                        pass

        self._reader = threading.Thread(target=read_replies, name="sidecar-replies", daemon=True)
        self._reader.start()

    def _exchange_locked(self, command: str, payload: object) -> dict:
        process = self.process
        if self._reader_error is not None:
            raise RuntimeError("Sidecar disconnected") from self._reader_error
        if process is None or process.poll() is not None or process.stdin is None or process.stdout is None:
            raise RuntimeError("Sidecar disconnected")
        request_id = self._next_id
        self._next_id += 1
        raw = sidecar_protocol.encode(request_id, self.token, command, payload)
        response: queue.Queue[dict | Exception] = queue.Queue(maxsize=1)
        self._pending[request_id] = response
        try:
            process.stdin.write(raw + b"\n")
            process.stdin.flush()
            try:
                value = response.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise TimeoutError("Sidecar did not respond") from exc
            if isinstance(value, Exception):
                raise value
            return value
        finally:
            self._pending.pop(request_id, None)

    def open(self, view: str, *, enabled: bool = True) -> bool:
        if view not in VIEWS:
            raise ValueError(f"Unknown sidecar view: {view}")
        if not enabled:
            return False
        if not self.executable.is_file():
            self.warn(f"Sidecar is not installed: {self.executable}; use Textual instead.")
            return False
        if self.process is not None and self.process.poll() is None:
            try:
                result = self._exchange("open", {"view": view})
                if result.get("opened") == view:
                    return True
                raise ValueError("Sidecar did not open the requested view")
            except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
                self.warn(f"Sidecar view switch failed: {exc}; use Textual instead.")
                self.close()
                return False
        self.token = secrets.token_hex(32)
        self._next_id = 1
        try:
            env = {**os.environ, "LITETUI_SIDECAR_TOKEN": self.token}
            self.process = self.spawn([str(self.executable), "--view", view, "--parent-pipe"],
                                      stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.DEVNULL, close_fds=True, env=env)
            self._start_reader(self.process)
            result = self._exchange("hello", {})
            if result.get("version") != sidecar_protocol.VERSION or result.get("ready") is not True:
                raise ValueError("Incompatible sidecar handshake")
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            self.warn(f"Sidecar handshake/launch failed: {exc}; use Textual instead.")
            self.close()
            return False
        return True

    def open_settings_snapshot(self, snapshot: dict) -> bool:
        """Send parent-owned, read-only settings; never treat a preview as an editor."""
        if not self.open("settings"):
            return False
        try:
            result = self._exchange("settings_snapshot", snapshot)
            if result.get("settings_snapshot") is True:
                return True
            raise ValueError("Sidecar rejected settings snapshot")
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            self.warn(f"Sidecar snapshot failed: {exc}; use Textual instead.")
            self.close()
            return False

    def open_jobs_snapshot(self, view: str, snapshot: dict) -> bool:
        """Show parent-owned jobs without scheduling or editing in the child."""
        if view not in {"calendar", "job", "timeline"}:
            raise ValueError(f"Unknown jobs view: {view}")
        if not self.open(view):
            return False
        try:
            result = self._exchange("jobs_snapshot", snapshot)
            if result.get("jobs_snapshot") is True:
                return True
            raise ValueError("Sidecar rejected jobs snapshot")
        except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
            self.warn(f"Sidecar jobs snapshot failed: {exc}; use Textual instead.")
            self.close()
            return False

    def close(self) -> bool:
        if self.process is None:
            return False
        process = self.process
        self.process = None
        self.token = ""
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=self.timeout)
        finally:
            for pipe in (process.stdin, process.stdout):
                if pipe is not None:
                    pipe.close()
        return True
