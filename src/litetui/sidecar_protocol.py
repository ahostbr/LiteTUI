"""Versioned line-frame envelope for the optional local Rust child.

This is a wire *contract*, not a transport or child-process manager. The parent
must keep its launch capability private and bind it to the spawned process.
"""
from __future__ import annotations

import json

VERSION = 1
MAX_FRAME_BYTES = 1_048_576
_KEYS = frozenset({"version", "id", "token", "command", "payload"})
_REQUIRED = frozenset({"version", "id", "token", "command"})


def decode(raw: bytes, expected_token: str) -> dict:
    """Return a validated frame, or raise ValueError before dispatching it."""
    if not expected_token:
        raise ValueError("Missing sidecar capability")
    if len(raw) > MAX_FRAME_BYTES:
        raise ValueError("Sidecar frame too large")
    try:
        frame = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Malformed sidecar frame") from exc
    if not isinstance(frame, dict) or not _REQUIRED <= frame.keys() or frame.keys() - _KEYS:
        raise ValueError("Invalid sidecar frame fields")
    if type(frame["version"]) is not int or frame["version"] != VERSION:
        raise ValueError("Unsupported sidecar protocol version")
    if type(frame["id"]) is not int or frame["id"] < 0:
        raise ValueError("Invalid sidecar request id")
    if not isinstance(frame["token"], str) or frame["token"] != expected_token:
        raise ValueError("Invalid sidecar capability")
    if not isinstance(frame["command"], str) or not frame["command"]:
        raise ValueError("Invalid sidecar command")
    frame.setdefault("payload", None)
    return frame


def encode(request_id: int, token: str, command: str, payload: object = None) -> bytes:
    """Serialize a parent request, applying the same validation as inbound frames."""
    frame = {"version": VERSION, "id": request_id, "token": token,
             "command": command, "payload": payload}
    try:
        raw = json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("Unserializable sidecar payload") from exc
    decode(raw, token)
    return raw
