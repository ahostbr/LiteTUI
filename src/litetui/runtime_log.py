"""Rotating runtime diagnostics — two sinks, one owner.

The metadata sink (runtime.jsonl) is deliberately not a conversation transcript.
Its sanitizer accepts a small typed vocabulary and rejects bodies, nested
objects, unknown keys, and unbounded strings before the rotating handler sees
them. That strictness is also why raw exception text can NEVER reach it — a
string with spaces fails the token regex and the whole event drops silently —
so T137 added a second sink beside it: runtime-errors.log takes exactly what
the metadata sink refuses (raw detail, tracebacks), for the debug surface only.
Chat never reads either file; rule c of the error-copy sweep keeps raw detail
out of the user-facing line and into here instead.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
import traceback
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
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

#: Hard cap on one detail blob in the error sink. A spawned server's whole
#: console tail is legitimate; a multi-megabyte body is not. Rotation bounds
#: the file, this bounds the single entry.
_MAX_DETAIL = 16000


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
    """Two size-bounded sinks with deterministic backup rotation.

    The metadata sink (JSONL, sanitized) and the error sink (plain text, raw).
    Both are best-effort: a full disk or a locked file degrades diagnostics to
    nothing — it never takes down the chat.
    """

    def __init__(
        self,
        path: Path,
        *,
        errors_path: Path | None = None,
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
        # The error sink gets its OWN logger: one handler per file. Sharing the
        # metadata logger would fan every raw traceback out into runtime.jsonl
        # and every JSON line into the text log — two feeds, one owner each.
        self.errors_path = (
            Path(errors_path) if errors_path else self.path.parent / "runtime-errors.log"
        )
        self._error_logger = logging.getLogger(f"litetui.runtime.errors.{uuid.uuid4().hex}")
        self._error_logger.setLevel(logging.INFO)
        self._error_logger.propagate = False
        self._error_handler = RotatingFileHandler(
            self.errors_path,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
            delay=True,
        )
        self._error_handler.setFormatter(logging.Formatter("%(message)s"))
        self._error_logger.addHandler(self._error_handler)

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

    def write_error(
        self,
        event: str,
        *,
        detail: str = "",
        exc: BaseException | None = None,
        **metadata: object,
    ) -> bool:
        """The raw sink (T137). Takes what :meth:`write` refuses; never raises.

        `detail` is the unbounded text — exception strings, spawned-server log
        tails; `exc` contributes its full traceback when given; `metadata` is
        best-effort JSON on one line and needs no token discipline here, because
        this file is for a human with a debugger, not for the sanitizer.
        """
        try:
            stamp = (
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
            lines = [stamp, f"event={event}"]
            if metadata:
                try:
                    lines.append(
                        "meta="
                        + json.dumps(metadata, ensure_ascii=False, default=str)
                    )
                except (TypeError, ValueError):
                    lines.append(f"meta={metadata!r}")
            text = str(detail)
            if len(text) > _MAX_DETAIL:
                text = text[:_MAX_DETAIL] + f"\n... [truncated at {_MAX_DETAIL} chars]"
            for ln in text.splitlines() or [""]:
                lines.append("  " + ln)
            if exc is not None:
                tb = "".join(
                    traceback.format_exception(type(exc), exc, exc.__traceback__)
                ).rstrip("\n")
                for ln in tb.splitlines():
                    lines.append("  " + ln)
            self._error_logger.info("\n".join(lines) + "\n\n")
            return True
        except (OSError, ValueError):
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
        self._error_logger.removeHandler(self._error_handler)
        self._error_handler.close()


def default_log_path(root: Path) -> Path:
    return Path(root) / ".logs" / "runtime.jsonl"


def default_errors_path(root: Path) -> Path:
    """Beside the metadata sink — one .logs/ to find, two files inside."""
    return Path(root) / ".logs" / "runtime-errors.log"


_ACTIVE: RuntimeRecorder | None = None


def install(
    path: Path,
    *,
    errors_path: Path | None = None,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
) -> RuntimeRecorder:
    """Install the process-wide sinks once; producers call :func:`record` and
    :func:`record_error`. The error sink defaults to beside the metadata one."""
    global _ACTIVE
    if _ACTIVE is not None:
        _ACTIVE.close()
    _ACTIVE = RuntimeRecorder(
        path,
        errors_path=errors_path,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )
    return _ACTIVE


def record(event: str, **metadata: object) -> bool:
    """The metadata producer seam. No installed sink is a cheap, safe no-op."""
    if _ACTIVE is None:
        return False
    return _ACTIVE.write({"event": event, **metadata})


def record_error(
    event: str,
    *,
    detail: str = "",
    exc: BaseException | None = None,
    **metadata: object,
) -> bool:
    """The raw producer seam (T137). Where :func:`record` refuses text, this keeps it.

    Rule c of the error-copy sweep: the user-facing line says what seems wrong
    and what to do; THIS is where the exception's own words go — never chat.
    No installed sink is a cheap, safe no-op, exactly like :func:`record`.
    """
    if _ACTIVE is None:
        return False
    return _ACTIVE.write_error(event, detail=detail, exc=exc, **metadata)


def record_signal(signal: Mapping[str, object]) -> bool:
    if _ACTIVE is None:
        return False
    return _ACTIVE.write_signal(signal)
