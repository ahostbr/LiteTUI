"""JSONL-over-stdio RPC bridge for headless LiteTUI (T507-T2).

stdin: one JSON object per line — commands.
stdout: one JSON object per line — events and responses.
stderr: everything else (Textual's own output, debug logs).

The reader runs in a daemon thread and posts commands onto the app loop.
The emitter is called from the app loop; it serialises and flushes stdout.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from litetui.app import LiteTUI


_real_stdout = os.fdopen(os.dup(1), "w", encoding="utf-8", closefd=True)
_real_stdin = sys.__stdin__

def rpc_emit(data: dict[str, Any]) -> None:
    """Write one JSON line to fd 1 (the real stdout, immune to Textual's replacement)."""
    try:
        _real_stdout.write(json.dumps(data, default=str) + "\n")
        _real_stdout.flush()
    except (BrokenPipeError, OSError):
        pass


def start_rpc_reader(app: LiteTUI) -> None:
    """Spawn the stdin reader thread. Called once from on_mount."""
    t = threading.Thread(target=_reader_loop, args=(app,), daemon=True, name="rpc-stdin")
    t.start()


def _reader_loop(app: LiteTUI) -> None:
    """Read JSON lines from stdin, dispatch to app loop."""
    for raw in (_real_stdin or sys.stdin):
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
    elif cmd_type == "answer":
        # The other half of `user_input_requested` (T558-A). The tool call is
        # parked on a worker thread waiting for exactly this.
        #
        # AN UNKNOWN ID IS AN ERROR, NOT A NO-OP. The two ways to get here with a
        # stale id are a late answer to a call that already moved on and a typo
        # in the id, and both leave the host believing it answered a question
        # that is still waiting. Saying so is what lets the host tell them apart
        # from a delivery that worked.
        from litetui import ask_user_question as auq_mod

        ask_id = str(cmd.get("id") or "")
        if not ask_id:
            _respond(cmd_id, ok=False, error="answer needs the `id` of the ask it answers")
            return
        ok = auq_mod.resolve_over_rpc(
            app,
            ask_id,
            str(cmd.get("action") or "submit"),
            cmd.get("answers"),
        )
        if ok:
            _respond(cmd_id, ok=True, result={"answered": ask_id})
        else:
            _respond(
                cmd_id,
                ok=False,
                error=f"no ask is waiting on id {ask_id!r} (already answered, or never asked)",
            )
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
    elif cmd_type == "set_settings":
        patch = cmd.get("patch", {})
        if "thinking_level" in patch:
            app.thinking_level = patch["thinking_level"] if patch["thinking_level"] != "off" else None
        if "model" in patch:
            if patch["model"] in app.available_models:
                app.model_id = patch["model"]
        _respond(cmd_id, ok=True, result={
            "thinking_level": app.thinking_level,
            "model": app.model_id,
        })
    elif cmd_type == "list_commands":
        cmds = []
        seen = set()
        for token, entry in app.plugins.commands.items():
            if token in seen:
                continue
            seen.add(token)
            # headless_ok: commands that push a screen cannot run headless
            handler_src = getattr(entry.handler, "__qualname__", "") + str(getattr(entry.handler, "__code__", ""))
            headless_ok = "push_screen" not in handler_src and "present_dialog" not in handler_src
            cmds.append({"command": token, "help": entry.help, "headless_ok": headless_ok})
        _respond(cmd_id, ok=True, result=cmds)
    elif cmd_type == "run_command":
        line = cmd.get("line", "").strip()
        if not line:
            _respond(cmd_id, ok=False, error="empty command")
            return
        # Check headless_ok
        parts = line.split(maxsplit=1)
        name = parts[0].lower()
        entry = app.plugins.commands.get(name)
        if entry is not None:
            handler_src = getattr(entry.handler, "__qualname__", "") + str(getattr(entry.handler, "__code__", ""))
            if "push_screen" in handler_src or "present_dialog" in handler_src:
                _respond(cmd_id, ok=False, error="needs terminal", needs="terminal")
                return
        try:
            app._handle_command(line)
            _respond(cmd_id, ok=True, result={"ran": line})
        except Exception as e:
            _respond(cmd_id, ok=False, error=str(e))
    elif cmd_type.startswith("jobs."):
        _handle_jobs(app, cmd_type, cmd, cmd_id)
    elif cmd_type.startswith("tasks."):
        _handle_tasks(app, cmd_type, cmd, cmd_id)
    elif cmd_type == "list_skills":
        skills = [{"name": s.name, "description": getattr(s, "description", "")} for s in app.skills]
        _respond(cmd_id, ok=True, result=skills)
    elif cmd_type == "use_skill":
        name = cmd.get("name", "")
        try:
            app._handle_command(f"/skills {name}")
            _respond(cmd_id, ok=True, result={"ok": True})
        except Exception as e:
            _respond(cmd_id, ok=False, error=str(e))
    else:
        _respond(cmd_id, ok=False, error=f"unknown command: {cmd_type}")


def _respond(cmd_id: Any, *, ok: bool, result: Any = None, error: str | None = None, needs: str | None = None) -> None:
    resp: dict[str, Any] = {"type": "response", "ok": ok}
    if cmd_id is not None:
        resp["id"] = cmd_id
    if ok:
        resp["result"] = result
    else:
        resp["error"] = error
    if needs:
        resp["needs"] = needs
    rpc_emit(resp)


def _handle_jobs(app: LiteTUI, cmd_type: str, cmd: dict[str, Any], cmd_id: Any) -> None:
    """Route jobs.list/create/update/delete to the scheduler."""
    try:
        from litetui import scheduler
        from litetui import paths
        verb = cmd_type.split(".", 1)[1] if "." in cmd_type else ""
        if verb == "list":
            jobs = scheduler.load(paths.ROOT)
            _respond(cmd_id, ok=True, result=[j.__dict__ for j in jobs])
        elif verb == "create":
            jobs = scheduler.load(paths.ROOT)
            job = scheduler.Job(**{k: v for k, v in cmd.items() if k not in ("type", "id")})
            jobs.append(job)
            scheduler.save(paths.ROOT, jobs)
            _respond(cmd_id, ok=True, result=job.__dict__)
        elif verb == "delete":
            job_id = cmd.get("job_id", "")
            jobs = scheduler.load(paths.ROOT)
            jobs = [j for j in jobs if getattr(j, "id", None) != job_id]
            scheduler.save(paths.ROOT, jobs)
            _respond(cmd_id, ok=True, result={"deleted": job_id})
        else:
            _respond(cmd_id, ok=False, error=f"unknown jobs verb: {verb}")
    except Exception as e:
        _respond(cmd_id, ok=False, error=str(e))


def _handle_tasks(app: LiteTUI, cmd_type: str, cmd: dict[str, Any], cmd_id: Any) -> None:
    """Route tasks.list/kill/tail to T499 background tasks."""
    verb = cmd_type.split(".", 1)[1] if "." in cmd_type else ""
    try:
        if verb == "list":
            rows = [{"id": k, **v} for k, v in app.bg_tasks.items()]
            _respond(cmd_id, ok=True, result=rows)
        elif verb == "kill":
            task_id = cmd.get("task_id", "")
            if task_id in app.bg_tasks:
                app.bg_tasks[task_id]["status"] = "killed"
                _respond(cmd_id, ok=True, result={"killed": task_id})
            else:
                _respond(cmd_id, ok=False, error=f"no such task: {task_id}")
        else:
            _respond(cmd_id, ok=False, error=f"unknown tasks verb: {verb}")
    except Exception as e:
        _respond(cmd_id, ok=False, error=str(e))
