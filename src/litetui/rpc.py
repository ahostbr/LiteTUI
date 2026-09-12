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


def _target_id(cmd: dict[str, Any], key: str) -> tuple[str, str]:
    """The id of the thing this command RESOLVES, and which key carried it.

    🔴 TWO MEANINGS SHARED ONE KEY, AND THAT IS WHAT MADE T558 A ONE-LINE
    BREAK OF TWO FEATURES. `id` is the CORRELATION id -- "reply to me on this" --
    read at the top of `_dispatch` and echoed by `_respond`. For `answer` and
    `approve` the same key was ALSO read as the request being resolved. A host
    that set the envelope id last (LiteTuiAdapter.sendCommand, before
    a7b904826) therefore retargeted every Answer and every Allow at its own
    `cmd_N`, and this child refused them correctly -- "no ask is waiting on id
    'cmd_3'" -- while nothing anywhere said the two ids were the same field.
        A CORRELATION ID IS UNIQUE PER COMMAND; AN ASK ID IS UNIQUE PER
        QUESTION. One key cannot be both without making one of them wrong.

    ⬜ THE DEDICATED KEY WINS, AND `id` STILL WORKS. A host that has not been
    updated sends only `id` and keeps working -- that compatibility is the whole
    reason this is two landings and not one, because the child must accept the
    new key BEFORE any host sends it. When both are present and DISAGREE the
    dedicated key is the answer: that disagreement is precisely the T558
    clobber, and it is now harmless rather than fatal.

    Returns `(id, key_it_came_from)` so the caller can say which one it read --
    a host whose envelope is clobbering the target can see that in the reply
    instead of deducing it from a refusal about an id it never chose.
    """
    dedicated = str(cmd.get(key) or "")
    if dedicated:
        return dedicated, key
    return str(cmd.get("id") or ""), "id"


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
        # T558-B: `tool_profile` on the prompt was DEAD - the host had been
        # sending it on every turn since the adapter was written and nothing
        # here ever read it, so authority was whatever the spawn flag said and
        # a mid-session change did nothing until restart. Applied through the
        # SAME setter shift+tab uses, so there is one path and not two.
        profile = cmd.get("tool_profile")
        if isinstance(profile, str) and profile:
            if not app.set_tool_profile(profile, announce=False):
                # Refused rather than ignored: a caller that named a profile
                # this build does not have is asking for authority it will not
                # get, and a silent turn under the OLD authority is the wrong
                # kind of surprise.
                _respond(cmd_id, ok=False, error=f"unknown tool profile {profile!r}")
                return
        app._submit_text(str(message), alt_chord=False, source="rpc")
        _respond(cmd_id, ok=True, result={"turn": "accepted"})
    elif cmd_type == "set":
        # T558-B. Authority and plan mode, mid-session, through the SAME setters
        # Ctrl+P and shift+tab use. Both fields optional; an empty `set` is a
        # no-op rather than an error, so a host can send whichever it knows.
        changed: dict[str, Any] = {}
        profile = cmd.get("profile")
        if isinstance(profile, str) and profile:
            if not app.set_tool_profile(profile):
                _respond(cmd_id, ok=False, error=f"unknown tool profile {profile!r}")
                return
            changed["profile"] = profile
        mode = cmd.get("mode")
        if isinstance(mode, str) and mode:
            if mode not in ("plan", "normal", "default"):
                _respond(cmd_id, ok=False, error=f"unknown mode {mode!r} (plan|normal)")
                return
            app.set_plan_mode(mode == "plan")
            changed["mode"] = mode
        # The ACK carries what is now in force, not what was asked for. A host
        # that sent nothing recognisable gets an empty dict and can tell.
        _respond(cmd_id, ok=True, result={
            "changed": changed,
            "tool_policy_profile": str(getattr(app, "_active_tool_profile", None)),
            "plan_mode": bool(getattr(app, "_plan_mode", False)),
        })
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

        ask_id, id_key = _target_id(cmd, "ask_id")
        if not ask_id:
            _respond(
                cmd_id,
                ok=False,
                error="answer needs `ask_id` (or `id`) naming the ask it answers",
            )
            return
        ok = auq_mod.resolve_over_rpc(
            app,
            ask_id,
            str(cmd.get("action") or "submit"),
            cmd.get("answers"),
        )
        if ok:
            _respond(cmd_id, ok=True, result={"answered": ask_id, "id_key": id_key})
        else:
            # NAMES THE KEY IT LOOKED IN. The T558 refusal said "no ask is
            # waiting on id 'cmd_3'" and was true, correct and useless: the
            # host never chose cmd_3 as a target, so the message described a
            # value it could not connect to anything it had done.
            _respond(
                cmd_id,
                ok=False,
                error=f"no ask is waiting on {id_key}={ask_id!r} "
                      "(already answered, or never asked)",
            )
    elif cmd_type == "approve":
        # The other half of `tool_approval_requested` (T577). The turn is
        # parked on an asyncio future waiting for exactly this, and this
        # dispatch already runs on the app loop (`call_from_thread` in
        # `_reader_loop`), so the future is resolved directly.
        #
        # UNKNOWN ID IS AN ERROR, not a no-op -- same reason as `answer`
        # above. A silent success here means a host that believes it allowed
        # a tool call which was in fact already denied by timeout.
        from litetui import tool_approval as approval_mod

        approval_id, id_key = _target_id(cmd, "approval_id")
        if not approval_id:
            _respond(cmd_id, ok=False,
                     error="approve needs `approval_id` (or `id`) naming the request "
                           "it answers")
            return
        if "allow" not in cmd:
            # NOT defaulted. A missing `allow` could only be guessed, and
            # both guesses are wrong: defaulting to deny ends someone's turn
            # on a malformed message, defaulting to allow runs a tool nobody
            # approved.
            _respond(cmd_id, ok=False,
                     error="approve needs `allow`: true or false")
            return
        ok = approval_mod.resolve_over_rpc(
            app, approval_id, bool(cmd.get("allow")),
            bool(cmd.get("remember", False)),
        )
        if ok:
            _respond(cmd_id, ok=True,
                     result={"approved": approval_id, "allow": bool(cmd.get("allow")),
                             "id_key": id_key})
        else:
            _respond(
                cmd_id, ok=False,
                error=f"no approval is waiting on {id_key}={approval_id!r} "
                      "(already answered, or timed out)",
            )
    elif cmd_type == "abort":
        # T632. TWO THINGS HAD TO HAPPEN HERE AND NEITHER DID.
        #
        # The asks go FIRST. A tool call parked in `_run_over_rpc` blocks a
        # worker thread on a `threading.Event`, and stopping the turn does not
        # touch it — so releasing it before asking for the stop is what lets the
        # turn actually reach its end instead of stopping on paper.
        from litetui import ask_user_question as auq_mod

        cancelled = auq_mod.cancel_pending_asks(app)
        for ask_id in cancelled:
            # The host resolves its own card locally on interrupt, so this is
            # not what clears the UI. It is the child SAYING what it did, which
            # is the only record that the question died rather than was answered.
            app._rpc_emit({
                "type": "user_input_resolved",
                "id": ask_id,
                "cancelled": True,
                "reason": "abort",
            })
        stopped = app.stop_turn_over_rpc()
        # `stopped` is now MEASURED, not asserted. It used to be the literal
        # `True` on every abort, including the ones that did nothing — a reply
        # that agreed with the host while the turn ran on.
        _respond(cmd_id, ok=True, result={
            "stopped": stopped,
            "cancelled_asks": cancelled,
        })
    elif cmd_type == "list_models":
        _respond(cmd_id, ok=True, result=app._rpc_model_state()["models"])
    elif cmd_type == "set_model":
        from litetui.plugins.model_switch import switch_model

        slug = str(cmd.get("slug") or "")
        if switch_model(app, slug):
            _respond(cmd_id, ok=True, result=app._rpc_model_state())
        else:
            _respond(cmd_id, ok=False, error=f"model not available: {slug}")
    elif cmd_type == "get_settings":
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
            from litetui.plugins.model_switch import switch_model

            switch_model(app, str(patch["model"]))
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
        # 🔴 `app.jobs`, NOT A FRESH `scheduler.load` (T689). This handler used
        # to read its own copy off disk, which made it a SECOND holder of the
        # same list inside one process: an rpc create was invisible to the
        # calendar UI, and the UI's next save — still holding the pre-create
        # list — wrote it back out without the new job. The delta in
        # `row_store` reconciles two PROCESSES; it cannot reconcile two
        # holders inside one, and it should not have to. `app.jobs` is the
        # shared mutable list every other writer already goes through.
        if verb == "list":
            _respond(cmd_id, ok=True, result=[j.__dict__ for j in app.jobs])
        elif verb == "create":
            job = scheduler.Job(**{k: v for k, v in cmd.items() if k not in ("type", "id")})
            app.jobs.append(job)
            scheduler.save(app.jobs, paths.data_root())
            _respond(cmd_id, ok=True, result=job.__dict__)
        elif verb == "delete":
            job_id = cmd.get("job_id", "")
            for job in [j for j in app.jobs if getattr(j, "id", None) == job_id]:
                app.jobs.remove(job)
            scheduler.save(app.jobs, paths.data_root())
            _respond(cmd_id, ok=True, result={"deleted": job_id})
        else:
            _respond(cmd_id, ok=False, error=f"unknown jobs verb: {verb}")
    except Exception as e:
        _respond(cmd_id, ok=False, error=str(e))


def _handle_tasks(app: LiteTUI, cmd_type: str, cmd: dict[str, Any], cmd_id: Any) -> None:
    """Route tasks.list/kill/tail to T499 background tasks.

    🔴 `app.bg_tasks` HOLDS `tasks.Task` DATACLASSES, NOT DICTS (T571). This
    handler was written against a mapping — `{"id": k, **v}` and
    `v["status"] = "killed"` — so every call raised, the `except` below turned
    the raise into a polite rpc error, and `tasks.list` answered
    `"'Task' object is not a mapping"` for the life of the verb. Nothing
    crashed, nothing was logged, and a caller reads that failure as "no tasks"
    or "not supported yet". The catch-all is what made it survive: it is kept,
    because an unexpected raise here must not take the reader thread down, but
    it is no longer the only thing standing between a shape error and a caller.
    """
    verb = cmd_type.split(".", 1)[1] if "." in cmd_type else ""
    try:
        if verb == "list":
            # `to_row` is the serialiser the STORE already uses: same field set
            # on the wire and on disk, and it is what excludes the live `proc`
            # (a Popen holding a _thread.lock — deep-copying one was a crash).
            _respond(cmd_id, ok=True, result=[t.to_row() for t in app.bg_tasks.values()])
        elif verb == "kill":
            task_id = cmd.get("task_id", "")
            # THE APP'S OWN BODY, the one `/tasks kill` calls: it checks the
            # state and takes the process TREE down. Re-deciding here would be a
            # kill that reports success and leaves the child running.
            reason = app._kill_background(task_id)
            if reason is None:
                _respond(cmd_id, ok=True, result={"killed": task_id})
            else:
                _respond(cmd_id, ok=False, error=reason)
        elif verb == "tail":
            # Promised by this docstring since it was written, and absent: the
            # verb answered "unknown tasks verb". `tail_text` is the same body
            # `/tasks tail` uses, including its refusal to read a RUNNING task's
            # log — which does not exist until `finish` writes it.
            task_id = cmd.get("task_id", "")
            task = app.bg_tasks.get(task_id)
            if task is None:
                _respond(cmd_id, ok=False, error=f"no such task: {task_id}")
            else:
                from litetui import paths
                from litetui import tasks as tasks_mod

                _respond(cmd_id, ok=True, result={
                    "id": task_id, "tail": tasks_mod.tail_text(task, paths.data_root()),
                })
        else:
            _respond(cmd_id, ok=False, error=f"unknown tasks verb: {verb}")
    except Exception as e:
        _respond(cmd_id, ok=False, error=str(e))
