"""Rotating metadata-only runtime diagnostics.

This is deliberately not a conversation transcript. The sanitizer accepts a
small typed vocabulary and rejects bodies, nested objects, unknown keys, and
unbounded strings before the rotating sink sees them.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import uuid
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


class MetadataRejected(ValueError):
    """An event could carry content rather than bounded diagnostic metadata."""


_ALLOWED_KEYS = frozenset(
    {
        "event",
        "site",
        "component",
        "operation",
        "name",
        "id",
        "channel",
        "count",
        "duration_ms",
        "exit_code",
        "error_type",
        "ok",
        "intensity",
        "status",
        "model",
        "method",
        "server",
        "plugin",
        "bytes",
        "turns",
        "messages",
    }
)
_PROHIBITED_KEYS = frozenset(
    {
        "prompt",
        "body",
        "content",
        "text",
        "message",
        "arguments",
        "args",
        "result",
        "output",
        "conversation",
        "label",
        "command",
    }
)
_TOKEN = re.compile(r"^[A-Za-z0-9_.:/@+-]+$")
_MAX_STRING = 128


def sanitize_event(event: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(event, Mapping):
        raise MetadataRejected("runtime event must be a mapping")
    keys = set(event)
    prohibited = sorted(keys & _PROHIBITED_KEYS)
    if prohibited:
        raise MetadataRejected(f"prohibited metadata key(s): {', '.join(prohibited)}")
    unknown = sorted(keys - _ALLOWED_KEYS)
    if unknown:
        raise MetadataRejected(f"unknown metadata key(s): {', '.join(unknown)}")
    if "event" not in event:
        raise MetadataRejected("runtime event needs an event name")

    clean: dict[str, object] = {}
    for key, value in event.items():
        if isinstance(value, str):
            if not value or len(value) > _MAX_STRING:
                raise MetadataRejected(f"metadata string {key!r} is empty or too long")
            if not _TOKEN.fullmatch(value):
                raise MetadataRejected(f"metadata string {key!r} is not a bounded token")
            clean[key] = value
        elif isinstance(value, (bool, int)):
            clean[key] = value
        elif isinstance(value, float):
            if not math.isfinite(value):
                raise MetadataRejected(f"metadata number {key!r} is not finite")
            clean[key] = value
        elif value is None:
            clean[key] = None
        else:
            # Lists and nested dictionaries are rejected even when their key is
            # known; they are the easiest way to smuggle bodies through metadata.
            raise MetadataRejected(f"metadata value {key!r} must be scalar")
    return clean


class RuntimeRecorder:
    """One size-bounded JSONL sink with deterministic backup rotation."""

    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 2 * 1024 * 1024,
        backup_count: int = 3,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger(f"litetui.runtime.{uuid.uuid4().hex}")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        self._handler = RotatingFileHandler(
            self.path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._logger.addHandler(self._handler)

    def write(self, event: Mapping[str, object]) -> bool:
        try:
            clean = sanitize_event(event)
            row: dict[str, Any] = {"ts": time.time(), **clean}
            self._logger.info(
                json.dumps(row, ensure_ascii=False, separators=(",", ":"))
            )
            return True
        except (MetadataRejected, OSError, ValueError):
            # Diagnostics are best-effort and may never take down the chat.
            return False

    def write_signal(self, signal: Mapping[str, object]) -> bool:
        """Adapt the existing glass-box observer shape without its free label."""
        event: dict[str, object] = {
            "event": "signal",
            "channel": str(signal.get("channel") or "unknown"),
        }
        intensity = signal.get("intensity")
        if isinstance(intensity, (int, float)) and not isinstance(intensity, bool):
            event["intensity"] = float(intensity)
        return self.write(event)

    def close(self) -> None:
        self._logger.removeHandler(self._handler)
        self._handler.close()


def default_log_path(root: Path) -> Path:
    return Path(root) / ".logs" / "runtime.jsonl"


_ACTIVE: RuntimeRecorder | None = None


def install(
    path: Path,
    *,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> RuntimeRecorder:
    """Install the process-wide sink once; producers all call :func:`record`."""
    global _ACTIVE
    if _ACTIVE is not None:
        _ACTIVE.close()
    _ACTIVE = RuntimeRecorder(
        path,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    return _ACTIVE


def record(event: str, **metadata: object) -> bool:
    """The one producer seam. No installed sink is a cheap, safe no-op."""
    if _ACTIVE is None:
        return False
    return _ACTIVE.write({"event": event, **metadata})


def record_signal(signal: Mapping[str, object]) -> bool:
    if _ACTIVE is None:
        return False
    return _ACTIVE.write_signal(signal)
