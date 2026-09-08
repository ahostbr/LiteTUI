"""JSONL-over-stdio RPC bridge for headless LiteTUI (T507-T2).

stdin: one JSON object per line — commands.
stdout: one JSON object per line — events and responses.
stderr: everything else (Textual's own output, debug logs).

The reader runs in a daemon thread and posts commands onto the app loop.
The emitter is called from the app loop; it serialises and flushes stdout.
"""

from __future__ import annotations

import json
import sys
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from litetui.app import LiteTUI


def rpc_emit(data: dict[str, Any]) -> None:
    """Write one JSON line to stdout. Called from the app loop only."""
    try:
        sys.stdout.write(json.dumps(data, default=str) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        pass


def start_rpc_reader(app: LiteTUI) -> None:
    """Spawn the stdin reader thread. Called once from on_mount."""
    t = threading.Thread(target=_reader_loop, args=(app,), daemon=True, name="rpc-stdin")
    t.start()


def _reader_loop(app: LiteTUI) -> None:
    """Read JSON lines from stdin, dispatch to app loop."""
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            cmd = json.loads(raw)
        except json.JSONDecodeError:
            rpc_emit({"type": "error", "error": "invalid JSON"})
            continue
        if not isinstance(cmd, dict) or "type" not in cmd:
            rpc_emit({"type": "error", "error": "missing 'type' field"})
            continue
        app.call_from_thread(_dispatch, app, cmd)
    # stdin closed → shutdown
    app.call_from_thread(app.exit)


def _dispatch(app: LiteTUI, cmd: dict[str, Any]) -> None:
    """Route a command on the app loop. T4 will expand this table."""
    cmd_type = cmd.get("type", "")
    cmd_id = cmd.get("id")

    if cmd_type == "shutdown":
        _respond(cmd_id, ok=True, result={"ok": True})
        app.exit()
    elif cmd_type == "prompt":
        message = cmd.get("message", "")
        if not message:
            _respond(cmd_id, ok=False, error="empty message")
            return
        app._submit_text(str(message), alt_chord=False)
        _respond(cmd_id, ok=True, result={"turn": "accepted"})
    elif cmd_type == "abort":
        app.action_stop_turn()
        _respond(cmd_id, ok=True, result={"stopped": True})
    elif cmd_type == "list_models":
        models = [
            {"slug": m, "loaded": getattr(app.model_rows.get(m), "loaded", None)}
            for m in app.available_models
        ]
        _respond(cmd_id, ok=True, result=models)
    elif cmd_type == "set_model":
        slug = cmd.get("slug", "")
        if slug in app.available_models:
            app.model_id = slug
            _respond(cmd_id, ok=True, result={"model": slug})
        else:
            _respond(cmd_id, ok=False, error=f"model not available: {slug}")
    elif cmd_type == "get_settings":
        from litetui import settings as settings_mod
        s = settings_mod.load()
        _respond(cmd_id, ok=True, result={
            "thinking_level": app.thinking_level,
            "tool_policy_profile": str(getattr(app, "_active_tool_profile", None)),
            "model": app.model_id,
        })
    elif cmd_type == "set_thinking":
        level = cmd.get("level")
        app.thinking_level = level if level != "off" else None
        _respond(cmd_id, ok=True, result={"level": app.thinking_level})
    else:
        _respond(cmd_id, ok=False, error=f"unknown command: {cmd_type}")


def _respond(cmd_id: Any, *, ok: bool, result: Any = None, error: str | None = None) -> None:
    resp: dict[str, Any] = {"type": "response", "ok": ok}
    if cmd_id is not None:
        resp["id"] = cmd_id
    if ok:
        resp["result"] = result
    else:
        resp["error"] = error
    rpc_emit(resp)
