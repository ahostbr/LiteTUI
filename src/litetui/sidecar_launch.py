"""Optional native preview lifecycle; no settings or job authority is transferred.

This launcher is intentionally not yet wired to commands. The Rust preview has
no parent IPC: opening it cannot satisfy /settings, /calendar, or /job parity.
"""
from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

VIEWS = frozenset({"settings", "calendar", "job"})


class SidecarWindow:
    def __init__(self, executable: Path, *, spawn: Callable = subprocess.Popen,
                 warn: Callable[[str], None] | None = None):
        self.executable = Path(executable)
        self.spawn = spawn
        self.warn = warn or (lambda _message: None)
        self.process = None

    def open(self, view: str) -> bool:
        if view not in VIEWS:
            raise ValueError(f"Unknown sidecar view: {view}")
        if not self.executable.is_file():
            self.warn(f"Sidecar is not installed: {self.executable}; use Textual instead.")
            return False
        if self.process is not None and self.process.poll() is None:
            # Re-routing a live child needs authenticated IPC. Do not report
            # success or spawn another window before that transport exists.
            self.warn("Sidecar view switching is not connected; use Textual instead.")
            return False
        try:
            self.process = self.spawn([str(self.executable), "--view", view], close_fds=True)
        except OSError as exc:
            self.warn(f"Sidecar failed to launch: {exc}; use Textual instead.")
            self.process = None
            return False
        return True

    def close(self) -> bool:
        if self.process is None:
            return False
        process = self.process
        self.process = None
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        return True


def try_open(view: str, *, enabled: bool, executable: Path,
             warn: Callable[[str], None] | None = None,
             spawn: Callable = subprocess.Popen) -> bool:
    """Single preview launch probe. Callers must retain a SidecarWindow for ownership."""
    if not enabled:
        return False
    return SidecarWindow(executable, warn=warn, spawn=spawn).open(view)
