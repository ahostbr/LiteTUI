"""Versioned, structured management operations for desktop clients.

This module has no stdout or Textual imports. rpc.py owns framing, and the app
retains its existing setters, authority checks and durable state ownership.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import json
import math
import os
import re
import threading
import time
import uuid
from contextvars import ContextVar
from dataclasses import asdict, fields, replace
from datetime import datetime
from pathlib import Path
from typing import Literal, get_args, get_origin, get_type_hints

from litetui import llm_backend, paths, tasks
from litetui import settings as settings_mod
from litetui.shared_state import DATA_VERSION, Lease, OwnershipError, check_data_version

PROTOCOL_VERSION = 1
OPERATION_CONTEXT = ContextVar("gui_management_operation", default=None)


def current_operation(app):
    return OPERATION_CONTEXT.get() or getattr(app, "_gui_operation_id", None)


OPERATIONS = (
    "hello", "state", "prompt.submit", "settings.get", "settings.validate", "settings.apply",
    "conversations.list", "conversations.read", "conversations.open", "conversations.create",
    "conversations.rename", "conversations.edit", "conversations.retry", "conversations.compact",
    "models.list", "models.discover", "models.reconnect", "models.select", "models.confirm", "extensions.list",
    "hooks.get", "hooks.validate", "hooks.apply", "hooks.repair", "hooks.test",
    "mcp.list", "mcp.connect", "mcp.disconnect", "mcp.reconnect", "mcp.reload", "mcp.add", "mcp.remove",
    "skills.list", "skills.read", "skills.use", "tools.list",
    "memory.list", "memory.read", "memory.write",
    "jobs.list", "jobs.create", "jobs.update", "jobs.delete", "jobs.pause", "jobs.resume", "jobs.run",
    "tasks.list", "tasks.kill", "tasks.tail", "goals.get", "goals.create", "goals.pause", "goals.resume", "goals.clear",
    "calendar.list", "diagnostics.get", "host_tools.register", "host_tools.unregister", "host_tools.result",
    "images.attach", "images.clear", "screen.mark", "subagents.list", "subagents.kill", "subagents.tail",
    "models.load", "models.unload", "models.config", "models.configure",
    "system.get", "system.set", "tools.execute", "glassbox.get", "glassbox.start", "glassbox.stop",
    "work.get", "work.cancel",
)
# These settings are consumed during construction/connection. A saved choice
# does not become an effective connection merely because a setter returned.
RECONNECT = frozenset({"lm_host", "ninfer_host", "llama_host", "llama_executable", "backend", "default_model", "mcp_enabled", "skills_enabled", "mcp_disabled_servers"})
RESTART = frozenset({"plugins_disabled", "skill_roots"})


def active_work(app):
    """Work owned by THIS runtime; perpetual pollers are not active jobs."""
    result = []
    if app._chat_running():
        result.append({"id": "conversation", "kind": "conversation", "cancellable": True})
    for task in app.bg_tasks.values():
        if task.state == "running" and task.owner_pid == os.getpid():
            result.append({"id": task.id, "kind": task.tool,
                           "cancellable": task.proc is not None or tasks.pending_cancellable(task)})
    tracked = {"modelctl", "ctxload", "tasks", "cancel", "mark", "hooks", "init"}
    for worker in getattr(app, "workers", ()):
        if worker.group in tracked and not worker.is_finished:
            result.append({"id": f"worker:{worker.group}:{worker.name}", "kind": worker.group, "cancellable": False})
    if getattr(app, "_gui_management_busy", False):
        result.append({"id": "management", "kind": "management", "cancellable": False})
    for request_id in getattr(app, "_gui_host_pending", {}):
        result.append({"id": request_id, "kind": "host-tool", "cancellable": True})
    return result


def _idle(app):
    if getattr(app, "_chat_running", lambda: False)():
        raise ValueError("An operation is active; cancel or wait for it before changing this resource")
    try:
        caller = asyncio.current_task()
    except RuntimeError:
        caller = None
    if getattr(app, "_gui_management_busy", False) and getattr(app, "_gui_management_owner", None) is not caller:
        raise ValueError("A management operation is active; wait for it before changing this resource")


def _session_path(session_id):
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("Invalid session_id")
    return paths.CONVO_DIR / session_id / "convo.jsonl"


def _read_session(session_id):
    from litetui.conversation import ConversationRepository
    path = _session_path(session_id)
    meta, messages = ConversationRepository.read(path)
    return {"session_id": session_id, "meta": meta, "messages": messages}


def _settings(app):
    requested = asdict(app.settings)
    effective = dict(getattr(app, "_gui_effective_settings", requested))
    deferred = {}
    metadata = []
    for field in fields(settings_mod.Settings):
        name = field.name
        apply = "restart" if name in RESTART else "reconnect" if name in RECONNECT else "immediate"
        if apply == "immediate":
            effective[name] = requested[name]
        if effective.get(name) != requested[name]:
            deferred[name] = apply
        metadata.append({"name": name, "label": name.replace("_", " ").capitalize(),
                         "type": str(field.type), "apply": apply,
                         "source": settings_mod.source_of(name) or "settings",
                         "read_only": bool(settings_mod.source_of(name))})
    return {"metadata": metadata, "requested": requested, "effective": effective, "deferred": deferred}


def _matches(value, kind):
    origin, args = get_origin(kind), get_args(kind)
    if origin is Literal:
        return value in args
    if args and origin not in (list, dict):
        return any(_matches(value, arg) for arg in args)
    if kind is type(None):
        return value is None
    if origin is list:
        return isinstance(value, list) and all(_matches(item, args[0]) for item in value)
    if origin is dict or kind is dict:
        return isinstance(value, dict)
    if kind is float:
        return type(value) in (float, int) and math.isfinite(value)
    return type(value) is kind


def _validate(app, patch):
    if not isinstance(patch, dict):
        raise TypeError("patch must be an object")
    types = get_type_hints(settings_mod.Settings)
    for name, value in patch.items():
        if name not in types:
            raise ValueError(f"unknown setting: {name}")
        if settings_mod.source_of(name):
            raise ValueError(f"{name} is controlled by {settings_mod.source_of(name)}")
        if not _matches(value, types[name]):
            raise ValueError(f"{name}: expected {types[name]}")
        if name in {"tool_iterations", "max_tokens_tools", "max_tokens_chat", "llama_models_max"} and value < 1:
            raise ValueError(f"{name} must be positive")
        if name == "tool_policy_profile":
            from litetui.tool_policy import selectable_profile_names
            if value not in selectable_profile_names():
                raise ValueError(f"{name}: unsupported profile")
        if name == "backend" and value not in llm_backend.BACKEND_NAMES:
            raise ValueError("backend: unsupported backend")
        if name in ("lm_host", "llama_host") and not value.startswith(("http://", "https://")):
            raise ValueError(f"{name}: expected an http(s) server URL")
        if name == "ninfer_host" and value and not value.startswith(("http://", "https://")):
            raise ValueError("ninfer_host: expected an http(s) server URL, or blank to discover")
    candidate = copy.deepcopy(app.settings)
    for name, value in patch.items():
        setattr(candidate, name, value)
    if patch.get("llama_executable"):
        from litetui.llm_backend import llama_executable
        llama_executable(candidate)
    return candidate


def _validate_model_fields(values, rows):
    for name, _label, kind, *extra in rows:
        value = values.get(name)
        if value is None:
            continue
        valid = ((kind == "int" and type(value) is int)
                 or (kind == "float" and type(value) in (int, float) and math.isfinite(value))
                 or (kind == "text" and isinstance(value, str))
                 or (kind == "tri" and type(value) is bool)
                 or (kind == "select" and value in extra[0]))
        if not valid:
            raise ValueError(f"{name}: invalid {kind} value")


def _conversations(app, action, cmd):
    from litetui.conversation import ConversationRepository
    if action == "list":
        return [{"session_id": meta.get("id") or path.parent.name,
                 "title": ConversationRepository.label(meta, msgs), "meta": meta,
                 "message_count": len(msgs), "updated_at": path.stat().st_mtime}
                for path, meta, msgs in ConversationRepository.list_all()]
    session_id = cmd.get("session_id") or getattr(app, "convo_id", "")
    if action == "read":
        if session_id == getattr(app, "convo_id", "") and not _session_path(session_id).exists():
            result = {"session_id": session_id, "meta": {"id": session_id}, "messages": copy.deepcopy(app.conversation)}
        else:
            result = _read_session(session_id)
        result["read_only"] = False
        if session_id != getattr(app, "convo_id", ""):
            try:
                with Lease(_session_path(session_id).parent / ".session.lease"):
                    pass
            except OwnershipError:
                result["read_only"] = True
                result["ownership_reason"] = "This conversation is active in another process"
        return result
    _idle(app)
    if action == "create":
        app._handle_command("/new")
        app._materialise_convo()
        return _read_session(app.convo_id)
    if action == "open":
        path = _session_path(session_id)
        if not path.is_file():
            raise ValueError("Conversation does not exist")
        app._resume(path)
        if app.convo_id != session_id:
            raise ValueError("Conversation could not be resumed")
        return _read_session(session_id)
    if session_id != app.convo_id:
        raise ValueError("Open this conversation before editing it")
    app.store.acquire()
    if action == "rename":
        title = str(cmd.get("title", "")).strip()
        if not title or len(title) > 200:
            raise ValueError("title must contain 1–200 characters")
        app.store.write_record({"type": "rename", "name": title, "ts": time.time()})
    elif action in ("edit", "retry"):
        index = cmd.get("index")
        if type(index) is not int or not 0 <= index < len(app.conversation):
            raise ValueError("index must name a persisted user message")
        prior = app.conversation[index]
        if prior.get("role") != "user":
            raise ValueError("Only a user message can be edited or retried")
        message = cmd.get("message", prior.get("content", ""))
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be nonempty text")
        app.conversation = app.conversation[:index]
        app.store.record_snapshot(app.conversation, "GUI retry from user message")
        app._submit_text(message, alt_chord=False, source="rpc")
        return {"session_id": session_id, "accepted": True}
    elif action == "compact":
        app._handle_command("/compact")
        return {"session_id": session_id, "accepted": True}
    else:
        raise ValueError(f"Unsupported conversation action: {action}")
    if app.store.persist_error:
        raise OSError(app.store.persist_error)
    return _read_session(session_id)


def _jobs(app, action, cmd):
    from litetui import scheduler
    if action in ("pause", "resume"):
        app._gui_schedules_paused = action == "pause"
        if action == "resume":
            app._gui_quitting = False
        return {"paused": app._gui_schedules_paused}
    scheduler.refresh(app.jobs, paths.data_root())
    if action == "list":
        return [asdict(job) for job in app.jobs]
    _idle(app)
    if action == "run":
        job = next((j for j in app.jobs if j.id == cmd.get("job_id")), None)
        if job is None:
            raise ValueError("Unknown job_id")
        delivered = app._fire_job(job, manual=True)
        if not delivered:
            raise ValueError("Job was not delivered: scheduler is paused, owned elsewhere, or its durable stamp failed")
        return {"delivered": True, "job_id": job.id, "run_count": job.run_count}
    candidate = list(app.jobs)
    if action == "create":
        payload = dict(cmd.get("job", {}))
        if payload.get("kind") == "loop":
            job = scheduler.Job.loop(prompt=payload["prompt"], interval_minutes=payload["interval_minutes"], owner_convo_id=app.convo_id)
        else:
            job = scheduler.Job(**payload)
            job.cron()
        if not job.prompt.strip():
            raise ValueError("A scheduled prompt cannot be empty")
        if any(j.id == job.id for j in candidate):
            raise ValueError("Duplicate job ID")
        candidate.append(job)
    else:
        job = next((j for j in candidate if j.id == cmd.get("job_id")), None)
        if job is None:
            raise ValueError("Unknown job_id")
        if action == "delete":
            candidate.remove(job)
        elif action == "update":
            patch = cmd.get("patch", {})
            if not isinstance(patch, dict) or set(patch) - {"prompt", "schedule", "label", "enabled", "new_conversation"}:
                raise ValueError("Invalid editable job fields")
            updated = replace(job, **patch)
            if updated.kind != "loop":
                updated.cron()
            candidate[candidate.index(job)] = updated
        else:
            raise ValueError("Unknown jobs action")
    scheduler.save(candidate, paths.data_root())
    app.jobs[:] = candidate
    return [asdict(job) for job in app.jobs]


def _memory(app, action, cmd):
    directory = _session_path(cmd.get("session_id") or app.convo_id).parent
    if action == "list":
        return [{"name": str(p.relative_to(directory)).replace("\\", "/"), "bytes": p.stat().st_size}
                for p in directory.rglob("*.md")]
    name = cmd.get("name")
    if not isinstance(name, str) or (name not in ("memory.md", "soul.md", "handoff.md") and not re.fullmatch(r"memories/[A-Za-z0-9_. -]+\.md", name)):
        raise ValueError("name must identify a conversation memory, soul, or handoff file")
    path = (directory / name).resolve()
    if not path.is_relative_to(directory.resolve()):
        raise ValueError("Memory path escapes the conversation")
    if action == "read":
        return {"name": name, "text": path.read_text(encoding="utf-8") if path.exists() else ""}
    _idle(app)
    if directory != app.convo_dir:
        raise ValueError("Open this conversation before changing its memory")
    app.store.acquire()
    text = cmd.get("text")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return {"name": name, "text": text}


def dispatch(app, cmd):
    """Execute nonblocking operations on the existing app loop."""
    operation = str(cmd.get("type", ""))[4:]
    if operation not in OPERATIONS:
        raise ValueError(f"Unsupported GUI operation: {operation}")
    if operation == "hello":
        if type(cmd.get("protocol_version")) is not int or cmd["protocol_version"] != PROTOCOL_VERSION:
            raise ValueError(f"Unsupported GUI protocol version; expected {PROTOCOL_VERSION}")
        check_data_version(paths.data_root())
        app._gui_rpc_enabled = True
        if not hasattr(app, "_gui_effective_settings"):
            app._gui_effective_settings = asdict(app.settings)
        return {"protocol_version": PROTOCOL_VERSION, "data_version": DATA_VERSION,
                "capabilities": list(OPERATIONS), "operations": [f"gui.{name}" for name in OPERATIONS]}
    if operation == "state":
        return {"session_id": app.convo_id, "busy": app._chat_running() or bool(getattr(app, "_gui_management_busy", False)), "mode": "plan" if app._plan_mode else "normal",
                "tool_profile": app.chosen_tool_profile, "model": app._rpc_model_state(),
                "settings": _settings(app), "conversations": _conversations(app, "list", {}),
                "jobs": _jobs(app, "list", {}), "tasks": [t.to_row() for t in app.bg_tasks.values()],
                "ownership": {"writable": app.store.owned, "data_version": DATA_VERSION},
                "schedules_paused": bool(getattr(app, "_gui_schedules_paused", False)),
                "active_work": active_work(app), "usage": getattr(app, "_gui_usage", None),
                "pending_approvals": [getattr(app, "_gui_approval_details", {}).get(key, {"id": key}) for key, future in getattr(app, "_approval_waiters", {}).items() if not future.done()],
                "pending_questions": [{"type": "user_input_requested", "id": key, "questions": [s.to_dict() for s in value[2]]} for key, value in getattr(app, "_rpc_pending_asks", {}).items() if not value[0].is_set()],
                "pending_model_loads": [{"request_id": key, **value["details"]} for key, value in getattr(app, "_gui_model_pending", {}).items()]}
    domain, action = operation.split(".", 1)
    if domain == "prompt":
        message, behavior = cmd.get("message"), cmd.get("behavior", "normal")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be nonempty text")
        if behavior not in ("normal", "queue", "interrupt"):
            raise ValueError("behavior must be normal, queue or interrupt")
        if getattr(app, "_gui_management_busy", False) or getattr(app, "_gui_quitting", False):
            raise ValueError("A management or shutdown operation is active")
        # Commands can change sessions and cannot be queued as ordinary text.
        if message.lstrip().startswith("/"):
            raise ValueError("Use the structured management operation for commands")
        app.store.acquire()
        alt_chord = False if behavior == "normal" else app.settings.enter_interrupts != (behavior == "interrupt")
        app._gui_next_operation_id = cmd.get("id")
        try:
            app._submit_text(message, alt_chord=alt_chord, source="rpc")
        finally:
            app._gui_next_operation_id = None
        return {"accepted": True, "behavior": behavior}
    if domain == "work":
        failures = []
        if action == "cancel":
            app._gui_schedules_paused = True
            app._gui_quitting = True
            from litetui import ask_user_question, goal_loop
            ask_user_question.cancel_pending_asks(app)
            from litetui.tool_approval import DENIED
            for future in getattr(app, "_approval_waiters", {}).values():
                if not future.done():
                    future.set_result(DENIED)
            for pending in getattr(app, "_gui_model_pending", {}).values():
                if not pending["future"].done():
                    pending["future"].set_result(False)
            goal = goal_loop.load_goal(app.convo_dir)
            if goal and goal.status == "active":
                goal.status = "paused"
                goal_loop.save_goal(app.convo_dir, goal)
            app._pending_input.clear()
            app.stop_turn_over_rpc()
            app._stop_requested = True
            for task in app.bg_tasks.values():
                if task.state == "running" and task.owner_pid == os.getpid():
                    reason = app._kill_background(task.id)
                    if reason:
                        failures.append({"task_id": task.id, "reason": reason})
        return {"active_work": active_work(app), "cancellation_errors": failures}
    if domain == "settings":
        if action == "get":
            return _settings(app)
        candidate = _validate(app, cmd.get("patch"))
        if action == "validate":
            return {"valid": True, "requested": asdict(candidate)}
        _idle(app)
        if not hasattr(app, "_gui_effective_settings"):
            app._gui_effective_settings = asdict(app.settings)
        app._on_settings_saved(candidate)
        if getattr(app, "_settings_persist_error", None):
            raise OSError("Settings applied for this session but could not be saved; check runtime diagnostics")
        if "tool_policy_profile" in cmd["patch"]:
            app.set_tool_profile(candidate.tool_policy_profile, announce=False)
        return _settings(app)
    if domain == "conversations":
        return _conversations(app, action, cmd)
    if domain == "models":
        if action == "list":
            return app._rpc_model_state()
        if action == "confirm":
            if type(cmd.get("allow")) is not bool:
                raise ValueError("allow must be a boolean")
            pending = getattr(app, "_gui_model_pending", {}).get(cmd.get("request_id"))
            if pending is None or pending["future"].done():
                raise ValueError("Unknown or expired model-load request_id")
            pending["future"].set_result(cmd["allow"])
            return {"resolved": cmd["request_id"], "allow": cmd["allow"]}
        if action == "select":
            _idle(app)
            from litetui.plugins.model_switch import switch_model
            if not switch_model(app, str(cmd.get("slug", ""))):
                raise ValueError("Model is not available")
            return app._rpc_model_state()
        from litetui.llm_backend import LLAMA_EXE, llama_executable, scan_models
        binary = llama_executable(app.settings)
        return {"executable": str(binary), "default_executable": str(LLAMA_EXE),
                "installed": binary.is_file(), "backend": app.settings.backend,
                "models": [asdict(row) for row in scan_models(app.settings)]}
    if domain == "system":
        if action == "set":
            _idle(app)
            text = cmd.get("text")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("System prompt must be nonempty text")
            app.store.acquire()
            from litetui.plugins.convo import _cmd_system
            _cmd_system(app, "/system", text)
        return {"text": next((m.get("content", "") for m in app.conversation if m.get("role") == "system"), "")}
    if domain in ("extensions", "tools", "skills"):
        if domain == "tools":
            return app.plugins.tool_specs()
        skills = [{"name": s.name, "description": getattr(s, "description", ""), "path": str(getattr(s, "path", ""))} for s in app.skills]
        if domain == "extensions":
            return {"skills": skills, "mcp": app.mcp.describe(),
                    "plugins": [{"id": name, "status": status, "enabled": status == "active"} for name, status in app.plugins.status.items()],
                    "tools": app.plugins.tool_specs()}
        if action == "list":
            return skills
        skill = next((s for s in app.skills if s.name == cmd.get("name")), None)
        if skill is None:
            raise ValueError("Unknown skill name")
        if action == "read":
            return {"name": skill.name, "text": Path(skill.path).read_text(encoding="utf-8")}
        app._handle_command(f"/skills {skill.name}")
        return {"accepted": True, "name": skill.name}
    if domain == "hooks":
        from litetui import lifecycle_hooks as hooks
        config = app.hook_config
        if action == "get":
            state = config.snapshot()
            scopes, errors, raw, encoded = {}, {}, {}, {}
            for scope in ("global", "project"):
                try:
                    scopes[scope] = [h.document() for h in config.read(scope)]
                except hooks.HookError as exc:
                    scopes[scope], errors[scope] = [], str(exc)
                    try:
                        content = config.path(scope).read_bytes()
                    except OSError:
                        raw[scope], encoded[scope] = None, None
                    else:
                        raw[scope] = content.decode("utf-8-sig", errors="replace")
                        encoded[scope] = base64.b64encode(content).decode("ascii")
            return {**scopes, "scope_errors": errors, "raw_documents": raw, "raw_bytes": encoded,
                    "effective": [h.document() for h in state.hooks], "disabled": state.disabled, "error": state.error,
                    "events": sorted(hooks.EVENTS), "results": [{"scope": key[0], "id": key[1], **asdict(value)} for key, value in app.hook_results.items()]}
        scope = cmd.get("scope", "global")
        config.path(scope)
        if action == "repair":
            if not isinstance(cmd.get("text"), str) or not isinstance(cmd.get("expected_bytes"), str):
                raise ValueError("repair requires text and the raw_bytes token from hooks.get")
            rows = config.repair(scope, cmd["text"], base64.b64decode(cmd["expected_bytes"], validate=True))
            return {"hooks": [h.document() for h in rows], "repaired": scope}
        edited = config.parse_document(cmd.get("document"), scope)
        if action == "validate":
            return {"valid": True, "hooks": [h.document() for h in edited]}
        baseline = config.parse_document(cmd.get("baseline"), scope)
        return {"hooks": [h.document() for h in config.save(scope, edited, baseline)]}
    if domain == "memory":
        return _memory(app, action, cmd)
    if domain == "jobs":
        return _jobs(app, action, cmd)
    if domain in ("tasks", "subagents"):
        if action == "list":
            return [t.to_row() for t in app.bg_tasks.values() if domain != "subagents" or t.tool == "subagent"]
        task = app.bg_tasks.get(cmd.get("task_id"))
        if task is None:
            raise ValueError("Unknown task_id")
        if action == "kill":
            reason = app._kill_background(task.id)
            if reason:
                raise ValueError(reason)
            return {"killed": task.id}
        from litetui import tasks
        return {"id": task.id, "tail": tasks.tail_text(task, paths.data_root())}
    if domain == "goals":
        from litetui import goal_loop
        if action != "get":
            _idle(app)
            app.store.acquire()
            arg = str(cmd.get("objective", "")).strip() if action == "create" else action
            if not arg:
                raise ValueError("objective must be nonempty")
            goal_loop.goal_command(app, arg)
        goal = goal_loop.load_goal(app.convo_dir)
        return asdict(goal) if goal else None
    if domain == "calendar":
        start = datetime.fromisoformat(cmd.get("after") or datetime.now().isoformat())  # noqa: DTZ005 - scheduler uses local wall time
        return [{**asdict(j), "next_at": j.next_after(start).isoformat() if j.next_after(start) else None} for j in app.jobs]
    if domain == "diagnostics":
        from litetui.version import __version__
        return {"runtime_version": __version__, "protocol_version": PROTOCOL_VERSION,
                "data_root": str(paths.data_root()), "session_id": app.convo_id,
                "persistence_error": app.store.persist_error, "settings_persistence_error": getattr(app, "_settings_persist_error", None),
                "model": app._rpc_model_state()}
    if domain == "host_tools":
        return _host_tools(app, action, cmd)
    if domain == "glassbox":
        from litetui.plugins import glassbox_plugin as glassbox
        if action != "get":
            glassbox._cmd_glassbox(app, "/glassbox", action)
        return {"running": glassbox.SERVER.running, "recording": glassbox._ENABLED,
                "url": f"http://127.0.0.1:{glassbox.SERVER.port}/events" if glassbox.SERVER.running else None,
                "recording_path": str(glassbox._RECORDER.path)}
    if domain == "images":
        if action == "clear":
            app.pending_image = None
            return {"attached": False}
        from litetui import appsvc
        path = Path(cmd.get("path", "")).expanduser().resolve()
        if not path.is_file():
            raise ValueError("Image file does not exist")
        content = appsvc.load_image_file(app, path)
        if not content:
            raise ValueError("This image could not be decoded")
        app.pending_image = content
        return {"attached": True, "name": path.name}
    if domain == "screen":
        from litetui.plugins.mark_plugin import MARK_SCRIPT, start_mark
        if not MARK_SCRIPT.exists():
            raise ValueError("Screen marking is unavailable: the bundled marker overlay is missing")
        start_mark(app)
        return {"accepted": True}
    raise ValueError(f"Operation requires asynchronous dispatch: {operation}")


async def async_dispatch(app, cmd):
    """Keep management work visible while awaiting a backend or approval."""
    operation = cmd["type"]
    blocking = operation in {"gui.models.load", "gui.models.unload", "gui.models.configure", "gui.models.reconnect", "gui.tools.execute", "gui.hooks.test"} or operation.startswith("gui.mcp.") and not operation.endswith(".list")
    if blocking:
        if getattr(app, "_gui_management_busy", False):
            raise ValueError("Another management operation is active; wait or cancel it")
        app._gui_management_busy = True
        app._gui_management_owner = asyncio.current_task()
    token = OPERATION_CONTEXT.set(None if operation == "gui.prompt.submit" else cmd.get("id"))
    try:
        return await _async_dispatch(app, cmd)
    finally:
        OPERATION_CONTEXT.reset(token)
        if blocking:
            app._gui_management_busy = False
            app._gui_management_owner = None


async def _async_dispatch(app, cmd):
    operation = cmd["type"]
    if operation == "gui.conversations.open":
        result = dispatch(app, cmd)
        conversation = app.conversation
        backend = app.backend
        session_id = app.convo_id
        worker = getattr(app, "_native_history_worker", None)
        if worker is not None:
            await worker.wait()
            if (app.conversation is not conversation or app.backend is not backend
                    or app.convo_id != session_id):
                raise ValueError("Conversation changed while native history was loading")
            return dispatch(app, {"type": "gui.conversations.read", "session_id": session_id})
        return result
    if operation == "gui.models.reconnect":
        _idle(app)
        from litetui import llm_backend
        app.backend = llm_backend.make_backend(app.settings)
        worker = app.connect()
        await worker.wait()
        if not getattr(app, "_gui_connection_success", False):
            raise ValueError("Connection failed; requested connection settings remain deferred")
        if not hasattr(app, "_gui_effective_settings"):
            app._gui_effective_settings = asdict(app.settings)
        for name in ("lm_host", "ninfer_host", "llama_host", "llama_executable", "backend", "default_model"):
            app._gui_effective_settings[name] = getattr(app.settings, name)
        return {"connected": True, "model": app._rpc_model_state(), "settings": _settings(app)}
    if operation in ("gui.models.load", "gui.models.unload", "gui.models.config", "gui.models.configure"):
        from litetui.plugins import model_switch
        action = operation.rsplit(".", 1)[1]
        key = cmd.get("slug") or app.model_id
        if not isinstance(key, str) or not key:
            raise ValueError("slug must name a model")
        if action == "config":
            info = await app.backend.model_info(key)
            return {"slug": key, "info": info, "backend": app.backend.name,
                    "load": app.settings.llama_load_settings.get(key, {}),
                    "inference": app.settings.model_infer_overrides.get(key, {}),
                    "presets": app.settings.llama_presets,
                    "load_fields": model_switch._LOAD_FIELDS, "inference_fields": model_switch._INFER_FIELDS}
        _idle(app)
        if action == "configure":
            load, inference = cmd.get("load", {}), cmd.get("inference", {})
            if not isinstance(load, dict) or not isinstance(inference, dict):
                raise ValueError("load and inference must be objects")
            if type(cmd.get("apply_load", False)) is not bool:
                raise ValueError("apply_load must be a boolean")
            _validate_model_fields(load, model_switch._LOAD_FIELDS)
            _validate_model_fields(inference, [*model_switch._INFER_FIELDS, ("enable_thinking", "", "tri", None)])
            model_switch.validate_load_cfg(load)
            for field, value in inference.items():
                if field == "json_schema":
                    json.loads(value)
                elif field in {"reasoning_effort", "enable_thinking", "stop", "max_tokens"}:
                    continue
                else:
                    _validate(app, {field: value})
            prior = app.settings.llama_load_settings.get(key, {})
            candidate = copy.deepcopy(app.settings)
            candidate.llama_load_settings[key] = dict(load)
            candidate.model_infer_overrides[key] = dict(inference)
            preset = cmd.get("preset_name")
            if preset:
                candidate.llama_presets[str(preset)] = {"load": dict(load), "inference": dict(inference)}
            settings_mod.save(candidate)
            app.settings = candidate
            if cmd.get("apply_load", False) and prior != load:
                await app.backend.apply_load_settings(key, load)
            return {"slug": key, "saved": True, "load_applied": bool(cmd.get("apply_load", False)),
                    "load": load, "inference": inference}
        await getattr(app.backend, action)(key)
        app.connect()
        return {"slug": key, "action": action, "completed": True}
    if operation == "gui.tools.execute":
        _idle(app)
        name, args = cmd.get("name"), cmd.get("args", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            raise ValueError("name and args are required")
        if app.plugins.dispatch_for(name) is None:
            raise ValueError("Unknown tool name")
        prior = app._active_tool_profile
        app._active_tool_profile = app.chosen_tool_profile
        try:
            text, ok = await app._execute_tool(name, dict(args))
            return {"name": name, "ok": ok, "result": text}
        finally:
            app._active_tool_profile = prior
    if operation.startswith("gui.mcp."):
        action = operation.rsplit(".", 1)[1]
        if action == "list":
            return app.mcp.describe()
        _idle(app)
        if action == "reload":
            app.mcp.reload_configs()
        else:
            name = cmd.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError("name is required")
            if action == "add":
                result = await asyncio.to_thread(app.mcp.add, name, cmd.get("config"))
            elif action in ("connect", "disconnect", "reconnect", "remove"):
                result = await asyncio.to_thread(getattr(app.mcp, action), name)
            else:
                raise ValueError("Unknown MCP operation")
            if isinstance(result, str) and result:
                raise ValueError(result)
        app.rebuild_mcp_dispatch()
        return app.mcp.describe()
    if operation == "gui.hooks.test":
        from litetui import hook_host
        from litetui import lifecycle_hooks as hooks
        hook = hooks.Hook.parse(cmd.get("hook"), cmd.get("scope", "global"))
        sample = cmd.get("sample", {})
        if sample.get("event") not in hooks.EVENTS:
            raise ValueError("sample requires a supported event")
        document = hooks.event_document(sample["event"], app._hook_workspace, sample.get("data", {}), source=sample.get("source", "typed"))
        return asdict(await hook_host.invoke(app, hook, document, app.chosen_tool_profile, testing=True))
    return dispatch(app, cmd)


def _host_tools(app, action, cmd):
    from litetui.tool_policy import MCP_UNKNOWN_POLICY
    if not hasattr(app, "_gui_host_pending"):
        app._gui_host_pending = {}
        app._gui_host_owners = set()
    if action == "result":
        request_id = cmd.get("request_id")
        pending = app._gui_host_pending.get(request_id)
        if pending is None:
            raise ValueError("Unknown or expired host tool request_id")
        pending["result"] = str(cmd.get("result", "")) if not cmd.get("error") else f"Error: {cmd['error']}"
        pending["event"].set()
        return {"resolved": request_id}
    plugin_id = cmd.get("plugin_id")
    if not isinstance(plugin_id, str) or not re.fullmatch(r"[a-zA-Z0-9_.-]+", plugin_id):
        raise ValueError("Invalid plugin_id")
    owner = f"gui:{plugin_id}"
    if action == "unregister":
        _idle(app)  # a runner already resolved before its approval must retain its provider
        if any(p["owner"] == owner for p in app._gui_host_pending.values()):
            raise ValueError("Plugin has active work; finish or cancel before disabling")
        app.plugins.unload(owner)
        app._gui_host_owners.discard(owner)
        return {"unregistered": plugin_id}
    if owner in app._gui_host_owners:
        raise ValueError("Plugin tools are already registered")
    specs = cmd.get("tools")
    if not isinstance(specs, list) or not specs:
        raise ValueError("tools must be a nonempty list of function tool specs")
    names = []
    for spec in specs:
        name = spec.get("function", {}).get("name") if isinstance(spec, dict) else None
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("Invalid function tool name")
        if name in names or app.plugins.dispatch_for(name) is not None:
            raise ValueError(f"Tool collision: {name}")
        names.append(name)
    def runner(name):
        def execute(args):
            request_id = uuid.uuid4().hex
            pending = {"event": threading.Event(), "owner": owner}
            app._gui_host_pending[request_id] = pending
            app.call_from_thread(app._rpc_emit, {"type": "host_tool_requested", "request_id": request_id,
                "plugin_id": plugin_id, "name": name, "arguments": args,
                "session_id": app.convo_id, "operation_id": current_operation(app)})
            try:
                deadline = time.monotonic() + 300
                while not pending["event"].wait(0.1):
                    if getattr(app, "_stop_requested", False):
                        return "Error: host tool cancelled"
                    if time.monotonic() > deadline:
                        return "Error: host tool timed out"
                return pending["result"]
            finally:
                app._gui_host_pending.pop(request_id, None)
        return execute
    for spec, name in zip(specs, names, strict=True):
        app.plugins.add_tool(owner, spec, runner(name), policy=MCP_UNKNOWN_POLICY)
    app._gui_host_owners.add(owner)
    return {"registered": plugin_id, "tools": names}
