"""Opt-in bounded function-event METADATA, never argument content or HTTP logs."""
from __future__ import annotations

import json
import os
import re
import stat

from litetui import runtime_log
from litetui.agent_store import _unlinked

CAPTURE_ENV = "LITETUI_CAPTURE_TOOL_EVENTS"
CAPTURE_FILE = "tool-call-stream.jsonl"
MAX_BYTES = 1024 * 1024
_TOKEN = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


class ToolStreamCapture:
    def __init__(self, session, conversation_id):
        self.session = session
        self.conversation_id = conversation_id
        self.sequence = 0
        self.calls = {}
        self.stopped = False

    @classmethod
    def for_app(cls, app):
        if os.environ.get(CAPTURE_ENV) != "1":
            return None
        session = getattr(app, "_agent_session", None)
        conversation_id = getattr(app, "convo_id", None)
        if session is None or not conversation_id:
            runtime_log.record("tool_stream_capture_refused", status="unowned")
            return None
        return cls(session, conversation_id)

    def record(self, event):
        if self.stopped:
            return
        kind = event.get("type")
        if kind in ("response.output_item.added", "response.output_item.done"):
            item = event.get("item", {})
            if item.get("type") != "function_call":
                return
            self.calls[event.get("output_index", 0)] = item.get("call_id"), item.get("name")
            self._append(kind, item.get("call_id"), item.get("name"), item.get("arguments", ""))
        elif kind in ("response.function_call_arguments.delta", "response.function_call_arguments.done"):
            call_id, name = self.calls.get(event.get("output_index", 0), (None, None))
            field = "delta" if kind.endswith(".delta") else "arguments"
            self._append(kind, call_id, name, event.get(field, ""))
        elif kind == "response.completed":
            for item in event.get("response", {}).get("output", []):
                if item.get("type") == "function_call":
                    self._append(kind, item.get("call_id"), item.get("name"), item.get("arguments", ""))

    def _append(self, kind, call_id, name, arguments):
        if self.stopped:
            return
        # Bounded identifiers only. The payload is reduced to a length BEFORE
        # serialization: neither secrets nor argument fragments reach disk.
        self.sequence += 1
        row = {"type": kind, "sequence": self.sequence,
               "call_id": self._token(call_id), "name": self._token(name),
               "argument_chars": len(arguments) if isinstance(arguments, str) else None}
        try:
            data = (json.dumps(row) + "\n").encode("utf-8")
            directory = self.session.conversation_directory(self.conversation_id)
            target = _unlinked(directory / CAPTURE_FILE)
            # No guessed roots, folder creation, rotations or deletion. The
            # lease and non-linked path are revalidated on EVERY append.
            if target.exists():
                info = target.stat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Capture target must be a private regular file")
            with target.open("ab") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError("Capture target must be a private regular file")
                if info.st_size + len(data) > MAX_BYTES:
                    self.stopped = True
                    runtime_log.record("tool_stream_capture_stopped", status="limit")
                    return
                handle.write(data)
        except (OSError, ValueError):
            self.stopped = True
            runtime_log.record("tool_stream_capture_stopped", status="storage")

    @staticmethod
    def _token(value):
        return value if isinstance(value, str) and _TOKEN.fullmatch(value) else None
