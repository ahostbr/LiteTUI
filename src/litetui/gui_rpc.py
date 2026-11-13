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

from litetui import restored_input, settings_runtime
from litetui import llm_backend, paths, seat_authority, tasks
from litetui import settings as settings_mod
from litetui.shared_state import DATA_VERSION, check_data_version

PROTOCOL_VERSION = 1
OPERATION_CONTEXT = ContextVar("gui_management_operation", default=None)


def current_operation(app):
    return OPERATION_CONTEXT.get() or getattr(app, "_gui_operation_id", None)


OPERATIONS = (
    "checkpoint.prepare", "checkpoint.status", "checkpoint.flush", "checkpoint.release",
    "claude.native_reset_review", "claude.native_reset_apply", "claude.native_reset_readback",
    "sidecar.native_configure",
    "sidecar.native_snapshot", "sidecar.native_review", "sidecar.native_apply", "sidecar.native_readback",
    "sidecar.native_step_review", "sidecar.native_step_apply", "sidecar.native_step_readback", "sidecar.native_cancel",
    "monitor.native_snapshot", "monitor.native_review", "monitor.native_apply", "monitor.native_readback", "monitor.native_close",
    "glassbox.native_snapshot", "glassbox.native_review", "glassbox.native_apply", "glassbox.native_readback",
    "hello", "state", "screen.native_mark", "voice.native_dictation", "voice.native_playback", "voice.native_catalog", "voice.native_action", "backends.list", "prompt.submit", "prompt.native_queue_review", "prompt.native_queue_apply", "prompt.native_send_review", "prompt.native_send_apply", "settings.get", "settings.validate", "settings.apply",
    "settings.native_form", "settings.native_form_save", "settings.native_form_restore",
    "system_lines",
    "prompt.native_pause_review", "prompt.native_pause_apply", "prompt.native_pause_readback",
    "conversations.list", "conversations.page", "conversations.preview", "conversations.read", "conversations.open", "conversations.create",
    "conversations.rename", "conversations.edit", "conversations.retry", "conversations.compact",
    "conversations.native_snapshot", "conversations.native_validate", "conversations.native_apply",
    "conversations.native_rewrite_snapshot", "conversations.native_rewrite_validate",
    "conversations.native_rewrite_apply", "conversations.native_rewrite_settled",
    "conversations.native_compact_snapshot", "conversations.native_compact_validate",
    "conversations.native_compact_apply", "conversations.native_compact_settled",
    "history.native_snapshot", "history.native_page", "history.native_image", "conversations.native_open",
    "models.list", "models.discover", "models.reconnect", "models.select", "models.confirm", "extensions.list",
    "hooks.native_list", "hooks.native_review", "hooks.native_apply", "hooks.native_readback",
    "hooks.native_test_review", "hooks.native_test_run",
    "hooks.native_edit_snapshot", "hooks.native_edit_review", "hooks.native_edit_apply", "hooks.native_edit_readback",
    "hooks.native_repair_review", "hooks.native_repair_apply", "hooks.native_repair_readback",
    "hooks.get", "hooks.validate", "hooks.apply", "hooks.repair", "hooks.test",
    "mcp.list", "mcp.connect", "mcp.disconnect", "mcp.reconnect", "mcp.reload", "mcp.add", "mcp.remove",
    "skills.list", "skills.read", "skills.use", "tools.list",
    "memory.list", "memory.read", "memory.write",
    "jobs.list", "jobs.create", "jobs.update", "jobs.delete", "jobs.pause", "jobs.resume", "jobs.run",
    "tasks.list", "tasks.kill", "tasks.tail", "goals.get", "goals.create", "goals.pause", "goals.resume", "goals.clear",
    "calendar.list", "diagnostics.get", "host_tools.register", "host_tools.unregister", "host_tools.result",
    "images.attach", "images.clear", "images.native_prepare", "images.native_clipboard", "images.native_preview", "screen.mark", "subagents.list", "subagents.kill", "subagents.tail",
    "models.load", "models.unload", "models.config", "models.configure",
    "models.native_lifecycle_snapshot", "models.native_lifecycle_review", "models.native_lifecycle_apply", "models.native_lifecycle_readback",
    "models.native_preferences_snapshot", "models.native_preferences_review",
    "models.native_preferences_apply", "models.native_preferences_readback",
    "plugins.native_list", "plugins.native_review", "plugins.native_apply", "plugins.native_readback",
    "context.native_page", "inspection.native_status", "inspection.native_status_with_glassbox",
    "voice.native_preferences", "voice.native_review", "voice.native_apply", "voice.native_readback",
    "presentation.native_preferences", "presentation.native_review", "presentation.native_apply", "presentation.native_readback",
    "connection.native_preferences", "connection.native_review", "connection.native_apply", "connection.native_readback",
    "agent.native_preferences", "agent.native_review", "agent.native_apply", "agent.native_readback",
    "mcp.native_lifecycle_snapshot", "mcp.native_lifecycle_review", "mcp.native_lifecycle_apply", "mcp.native_lifecycle_readback",
    "mcp.native_config_list", "mcp.native_config_review", "mcp.native_config_add_review", "mcp.native_config_remove_review",
    "mcp.native_config_apply", "mcp.native_config_readback",
    "context.native_topic_page", "context.native_topic_edit", "context.native_topic_review",
    "context.native_topic_apply", "context.native_topic_readback",
    "context.native_edit", "context.native_review", "context.native_apply", "context.native_readback",
    "context.native_system_edit", "context.native_system_review", "context.native_system_apply", "context.native_system_readback",
    "system.get", "system.set", "tools.execute", "glassbox.get", "glassbox.start", "glassbox.stop",
    "work.get", "work.cancel",
    "runtime.native_snapshot", "runtime.native_validate", "runtime.native_apply", "runtime.native_readback", "runtime.native_tool_list",
    "work.native_capture", "work.native_activate", "work.native_settled",
    "work.native_execution_validate", "work.native_execution_apply", "work.native_execution_readback", "work.native_execution_stop",
    "work.native_schedule_manual_review",
    "work.native_schedule_accept", "work.native_schedule_decline", "work.native_schedule_readback",
    "work.native_deferred_validate", "work.native_deferred_discard", "work.native_deferred_readback",
    "work.native_snapshot", "work.native_page", "work.native_tail", "work.native_calendar",
    "work.native_pause_validate", "work.native_pause_apply", "work.native_pause_readback",
    "work.native_stop_validate", "work.native_stop_apply", "work.native_stop_settled",
    "work.native_control_validate", "work.native_control_apply", "work.native_control_readback",
    "thinking.set", "context.set", "backend.set", "backends.native_models", "models.capabilities",
    "engine.start", "engine.stop", "engine.status",
    "engine.native_inspect", "engine.native_validate", "engine.native_peer_validate", "engine.native_reconnect_validate", "engine.native_action", "engine.native_readback",
    "engine.native_config_snapshot", "engine.native_config_review", "engine.native_config_save", "engine.native_config_readback",
    "settings.native_snapshot", "settings.native_validate", "settings.native_save",
    "settings.native_compact_snapshot", "settings.native_compact_validate", "settings.native_compact_save",
    "themes.native_snapshot", "themes.native_validate", "themes.native_save",
    "commands.list", "commands.resolve", "skills.native_prepare",
    "restored_input.list", "restored_input.cancel", "restored_input.continue", "restored_input.settled", "restored_input.reconcile", "restored_input.resolve",
)


def _model_picker_state(app):
    """`gui.models.list`: the model state plus what Python's /model picker shows and asks.

    `labels` is plugins/model_switch._row_label for each row; `context` is what
    context_choice.ask needs for this backend (None for hosted providers, which are not asked).
    Additive keys only: `model_state` events keep the plain `_rpc_model_state` shape.
    """
    from litetui import context_choice
    state = app._rpc_model_state()
    backend = getattr(app, "backend", None)
    name = getattr(backend, "name", None) or ""
    status = "ready" if getattr(backend, "remote", False) else "loaded"
    labels = {}
    for slug in getattr(app, "available_models", []):
        row = getattr(app, "model_rows", {}).get(slug)
        tag = f"  · {row.source}" + (f"  · {status}" if row.loaded else "") if row is not None else ""
        labels[slug] = ("▸ " if slug == getattr(app, "model_id", "") else "  ") + slug + tag
    state["labels"] = labels
    key = context_choice.main_key(name)
    state["context"] = None if key is None else {
        "setting": key,
        "applies_on_engine_start": name in context_choice.APPLIES_ON_ENGINE_START,
        "suggestions": {s: context_choice.suggestion(app.settings, name, s) for s in labels},
        "remembered": {s: context_choice.remembered(app.settings, name, s) for s in labels},
    }
    return state


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
    from litetui.backend_transition import require_open

    require_open(app)
    from litetui import conversation_action
    if conversation_action.readback_allowed(app):
        return
    if getattr(app, "_chat_running", lambda: False)():
        raise ValueError("An operation is active; cancel or wait for it before changing this resource")
    try:
        caller = asyncio.current_task()
    except RuntimeError:
        caller = None
    if getattr(app, "_gui_management_busy", False) and getattr(app, "_gui_management_owner", None) is not caller:
        raise ValueError("A management operation is active; wait for it before changing this resource")


def _session_path(session_id, app=None):
    session = getattr(app, "_agent_session", None)
    if session is not None:
        from litetui.storage_catalog import owned_transcript
        return owned_transcript(session, session_id)
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("Invalid session_id")
    return paths.CONVO_DIR / session_id / "convo.jsonl"


def _read_session(session_id, app=None):
    from litetui.conversation import ConversationRepository
    path = _session_path(session_id, app)
    meta, messages = ConversationRepository.read(path)
    return {"session_id": session_id, "meta": meta, "messages": messages}


def _settings(app):
    requested = asdict(app.settings)
    effective = dict(getattr(app, "_gui_effective_settings", requested))
    deferred = {}
    metadata = []
    from litetui import gpu_gate
    from litetui.settings_scope import SETTING_SPECS
    from litetui.sidecar_settings import SECRET_FIELDS

    hide_ninfer = not gpu_gate.is_rtx_5090()   # T893
    for field in fields(settings_mod.Settings):
        name = field.name
        if hide_ninfer and name.startswith("ninfer_"):
            requested.pop(name, None)
            effective.pop(name, None)
            continue
        apply = SETTING_SPECS[name].apply_timing
        if apply in {"immediate", "next_turn"}:
            effective[name] = requested[name]
        if effective.get(name) != requested[name]:
            deferred[name] = apply
        metadata.append({"name": name, "label": name.replace("_", " ").capitalize(),
                         "type": str(field.type), "apply": apply,
                         "source": settings_mod.source_of(name) or "settings",
                         "read_only": bool(settings_mod.source_of(name)), "secret": name in SECRET_FIELDS})
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
        if name == "strata_host" and value and not value.startswith(("http://", "https://")):
            raise ValueError("strata_host: expected an http(s) server URL, or blank for the install's port")
        if name == "xolotl_host" and value and not value.startswith(("http://", "https://")):
            raise ValueError("xolotl_host: expected an http(s) server URL, or blank for the config's port")
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


def _tool_policy(app):
    """Current effective facts, matching ready rather than saved preferences."""
    return {
        "tools_enabled": bool(getattr(app, "tools_enabled", False)),
        "mcp_enabled": bool(app.settings.mcp_enabled),
        "tool_profile": str(getattr(app, "_active_tool_profile", None)),
        "tool_profile_requested": getattr(app, "_cli_tool_profile", None) or None,
        "approval_route": seat_authority.confirm_route(app), "cwd": os.getcwd(),
    }


def _conversations(app, action, cmd):
    from litetui import conversation_rewrite
    from litetui.conversation import ConversationRepository
    conversation_rewrite.operation_allowed(app, "gui.conversations." + action)
    if action in ("page", "preview"):
        from litetui import conversation_page

        return conversation_page.page(app, cmd) if action == "page" else conversation_page.preview(app, cmd)
    if action == "list":
        session = getattr(app, '_agent_session', None)
        if session is not None:
            from litetui.storage_catalog import conversations
            rows = conversations(session.store.data_root, agent_id=session.authority.agent_id)
            result = []
            for location in rows:
                meta, msgs = ConversationRepository.read(location.transcript)
                result.append({"session_id": location.conversation_id,
                    "agent_id": location.agent_id, "agent_name": location.agent_name,
                    "title": ConversationRepository.label(meta, msgs), "meta": meta,
                    "message_count": len(msgs), "updated_at": location.transcript.stat().st_mtime})
            return result
        return [{"session_id": meta.get("id") or path.parent.name,
                 "title": ConversationRepository.label(meta, msgs), "meta": meta,
                 "message_count": len(msgs), "updated_at": path.stat().st_mtime}
                for path, meta, msgs in ConversationRepository.list_all()]
    session_id = cmd.get("session_id") or getattr(app, "convo_id", "")
    if action == "read":
        budget = cmd.get("max_response_bytes")
        if "max_response_bytes" in cmd and (type(budget) is not int or not 1 <= budget <= 1048576):
            raise ValueError("max_response_bytes must be an integer from 1 through 1048576")
        if session_id == getattr(app, "convo_id", "") and not _session_path(session_id, app).exists():
            result = {"session_id": session_id, "meta": {"id": session_id}, "messages": copy.deepcopy(app.conversation)}
        else:
            result = _read_session(session_id, app)
        result["read_only"] = getattr(app, '_agent_session', None) is None
        if result["read_only"]:
            result["ownership_reason"] = "Legacy conversation archive is read-only; no owned agent session"
        if budget is not None:
            # Match rpc._respond/rpc_emit framing and default ASCII escaping.
            response = {"type": "response", "ok": True}
            if cmd.get("id") is not None:
                response["id"] = cmd["id"]
            response["result"] = result
            if len(json.dumps(response, default=str).encode("utf-8")) > budget:
                raise ValueError("Conversation read exceeds max_response_bytes; current session is unchanged")
        return result
    if getattr(app, '_agent_session', None) is None:
        raise ValueError("Mutable conversation RPC requires an owned agent session")
    _idle(app)
    if action == "create":
        app._handle_command("/new")
        app._materialise_convo()
        return _read_session(app.convo_id, app)
    if action == "open":
        from litetui import background_input
        value = background_input.bridge(app)
        if value is not None and value.active and (
                getattr(app, "_work_open_owner", None) != (value, asyncio.current_task())
                or not value.closing or getattr(value, "transition_proof", None) is None):
            raise ValueError("Captured conversation opening requires an awaited identity transition")
        if "from_session_id" in cmd or "snapshot" in cmd:
            from litetui import conversation_page

            conversation_page.validate_open(app, cmd)
        path = _session_path(session_id, app)
        if not path.is_file():
            raise ValueError("Conversation does not exist")
        resumed = app._resume(path)
        if not resumed or app.convo_id != session_id:
            raise ValueError("Conversation could not be resumed")
        return _read_session(session_id, app)
    if session_id != app.convo_id:
        raise ValueError("Open this conversation before editing it")
    app.store.acquire()
    if action == "rename":
        title = str(cmd.get("title", "")).strip()
        if not title or len(title) > 200:
            raise ValueError("title must contain 1â€“200 characters")
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
        app._snapshot("GUI retry from user message")
        app._submit_text(message, alt_chord=False, source="rpc")
        return {"session_id": session_id, "accepted": True}
    elif action == "compact":
        from litetui import restart_checkpoint
        if restart_checkpoint.is_held(app):
            from litetui.plugin_reload_activity import idle_infra_phase
            with idle_infra_phase(app):
                restart_checkpoint.authorize_compaction(app, cmd.get("checkpoint_id"), cmd.get("handoff_sha256"))
        app._handle_command("/compact")
        return {"session_id": session_id, "accepted": True}
    else:
        raise ValueError(f"Unsupported conversation action: {action}")
    if app.store.persist_error:
        raise OSError(app.store.persist_error)
    return _read_session(session_id, app)


def _job_run_candidate(app, cmd):
    from litetui import scheduler
    job = next((j for j in app.jobs if j.id == cmd.get("job_id")), None)
    if job is None:
        raise ValueError("Unknown job_id")
    why = seat_authority.withheld(app, scheduler.level_of(job.tool_profile))
    if why:
        raise ValueError(f"Job {job.id} not run: {why}")
    return job


async def _run_captured_job(app, cmd):
    from textual.worker import Worker

    from litetui import scheduler
    from litetui.shutdown_wait import join_task

    scheduler.refresh(app.jobs, paths.data_root())
    _idle(app)
    job = _job_run_candidate(app, cmd)
    worker = app._fire_job(job, manual=True)
    if not isinstance(worker, Worker):
        raise TypeError("Captured scheduler Run requires its exact source worker")
    # The accepted source invocation retains ownership through caller cancellation.
    # WorkerFailed/WorkerCancelled propagate; truthiness is never delivery evidence.
    result = await join_task(asyncio.create_task(worker.wait()))
    if not isinstance(result, dict) or type(result.get("delivered")) is not bool or result.get("job_id") != job.id:
        raise ValueError("Captured scheduler Run did not return its structured receipt")
    return copy.deepcopy(result)


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
        from litetui import background_input
        if background_input.enabled(app):
            raise ValueError("Captured scheduler Run requires asynchronous dispatch")
        job = _job_run_candidate(app, cmd)
        delivered = app._fire_job(job, manual=True)
        if not delivered:
            raise ValueError("Job was not delivered: scheduler is paused, owned elsewhere, or its durable stamp failed")
        return {"delivered": True, "job_id": job.id, "run_count": job.run_count}
    candidate = list(app.jobs)
    if action == "create":
        payload = dict(cmd.get("job", {}))
        # T1082: a loop created here (LiteGUI's loop form, a human in a live instance)
        # records the level of the turn it was created in and takes none; a cron job
        # records its creator's pick through schedule_level (none = this seat's level).
        if payload.get("kind") == "loop":
            if "tool_profile" in payload:
                raise ValueError(seat_authority.LOOP_REFUSAL)
            job = scheduler.Job.loop(prompt=payload["prompt"], interval_minutes=payload["interval_minutes"],
                                     owner_convo_id=app.convo_id, tool_profile=seat_authority.loop_level(app))
        else:
            payload["tool_profile"] = seat_authority.schedule_level(app, payload.get("tool_profile"))
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


def _owned_memory_path(session, name):
    from litetui.agent_store import MEMORY_FILES, StoreError, _unlinked, valid_name
    if not isinstance(name, str):
        raise ValueError("name must identify an agent memory, soul, or handoff file")
    if name not in MEMORY_FILES:
        parts = name.split('/')
        if len(parts) < 2 or parts[0] != 'memories' or not parts[-1].endswith('.md'):
            raise ValueError("name must identify an agent memory, soul, or handoff file")
        for part in parts[1:]:
            valid_name(part)
        if any(part in ('.', '..') for part in parts):
            raise StoreError('Memory path escapes the selected agent')
    # Check original path and ancestors, never resolve away link evidence.
    return _unlinked(session.memory_root / name)


def _owned_memory(app, session, action, cmd, *, before_publish=None, create_only=False):
    from litetui.agent_store import MEMORY_FILES, _unlinked
    _session_path(cmd.get("session_id") or app.convo_id, app)
    directory = session.memory_root
    if action == "list":
        candidates = [_unlinked(directory / name) for name in sorted(MEMORY_FILES)]
        memories = _unlinked(directory / 'memories')
        if memories.exists():
            # Never traverse reparse points. This also checks empty directories,
            # which a file-only glob would otherwise silently skip.
            for root, dirs, files in os.walk(memories, followlinks=False):
                for name in dirs:
                    _unlinked(Path(root) / name)
                for name in files:
                    path = _unlinked(Path(root) / name)
                    if name.endswith('.md'):
                        candidates.append(_owned_memory_path(session, path.relative_to(directory).as_posix()))
        return [{"name": path.relative_to(directory).as_posix(), "bytes": path.stat().st_size}
                for path in candidates if path.is_file()]
    name = cmd.get("name")
    path = _owned_memory_path(session, name)
    if action == "read":
        return {"name": name, "text": path.read_text(encoding="utf-8") if path.exists() else ""}
    if action != "write":
        raise ValueError("Unsupported memory action")
    _idle(app)
    text = cmd.get("text")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    session.authority
    path = _owned_memory_path(session, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _unlinked(path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp"))
    handle = temporary.open('x', encoding='utf-8', newline='' if before_publish else None)
    try:
        with handle:
            handle.write(text)
        session.authority
        _owned_memory_path(session, name)
        if before_publish:
            before_publish(temporary)
        if create_only:
            # Publish a missing document without displacing a concurrently created file.
            os.link(_unlinked(temporary), path)
        else:
            _unlinked(temporary).replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"name": name, "text": text}


def _memory(app, action, cmd):
    session = getattr(app, '_agent_session', None)
    if session is not None:
        return _owned_memory(app, session, action, cmd)
    requested = cmd.get("session_id") or app.convo_id
    selected = _session_path(requested, app).parent
    directory = session.memory_root if session is not None else selected
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
    if session is None and directory != app.convo_dir:
        raise ValueError("Open this conversation before changing its memory")
    if session is not None:
        session.authority
        from litetui.agent_store import _unlinked
        _unlinked(path)
    else:
        app.store.acquire()
    text = cmd.get("text")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
    return {"name": name, "text": text}


# During a restart hold, reads and completion of already-issued callbacks remain
# available. New mutations are refused BEFORE their first side effect, including
# async management operations that do not pass through dispatch().
_RESTART_SAFE = frozenset({
    "hello", "state", "checkpoint.prepare", "checkpoint.status", "checkpoint.flush", "checkpoint.release",
    "settings.get", "settings.validate", "conversations.list", "conversations.read", "conversations.compact",
    "models.list", "models.capabilities", "models.config", "models.confirm", "extensions.list",
    "hooks.get", "hooks.validate", "mcp.list", "skills.list", "skills.read", "tools.list",
    "memory.list", "memory.read", "jobs.list", "tasks.list", "tasks.tail", "goals.get", "calendar.list",
    "diagnostics.get", "host_tools.result", "subagents.list", "subagents.tail", "system.get",
    "glassbox.get", "work.get", "engine.status",
})


def _restart_gate(app, operation):
    from litetui import restart_checkpoint
    if operation not in _RESTART_SAFE and restart_checkpoint.is_held(app):
        raise ValueError("Restart checkpoint hold blocks this mutation")


def dispatch(app, cmd):
    """Execute nonblocking operations on the existing app loop."""
    from litetui import native_engine
    native_engine.operation_allowed(app, str(cmd.get("type", "")))
    from litetui import native_schedule, native_work_execution
    native_work_execution.operation_allowed(app, str(cmd.get("type", "")))
    native_schedule.operation_allowed(app, str(cmd.get("type", "")))
    from litetui import conversation_transition
    conversation_transition.require_execution(app)
    operation = str(cmd.get("type", ""))[4:]
    _restart_gate(app, operation)
    if operation not in OPERATIONS:
        raise ValueError(f"Unsupported GUI operation: {operation}")
    if operation in {"mcp.native_config_list", "mcp.native_config_review", "mcp.native_config_add_review",
                     "mcp.native_config_remove_review", "mcp.native_config_apply", "mcp.native_config_readback"}:
        from litetui import native_mcp_config
        return native_mcp_config.dispatch(app, cmd)
    if operation in {"connection.native_preferences", "connection.native_review", "connection.native_apply", "connection.native_readback"}:
        from litetui import native_connection_preferences
        return native_connection_preferences.dispatch(app, cmd)
    if operation in {"voice.native_preferences", "voice.native_review", "voice.native_apply", "voice.native_readback"}:
        from litetui import native_voice_preferences
        return native_voice_preferences.dispatch(app, cmd)
    if operation in {"presentation.native_preferences", "presentation.native_review", "presentation.native_apply", "presentation.native_readback"}:
        from litetui import native_presentation_preferences
        return native_presentation_preferences.dispatch(app, cmd)
    if operation in {"agent.native_preferences", "agent.native_review", "agent.native_apply", "agent.native_readback"}:
        from litetui import native_agent_preferences
        return native_agent_preferences.dispatch(app, cmd)
    if operation in {"inspection.native_status", "inspection.native_status_with_glassbox"}:
        from litetui import native_inspection
        return native_inspection.status(app, cmd)
    if operation in {"context.native_topic_edit", "context.native_topic_review", "context.native_topic_apply",
                     "context.native_topic_readback", "context.native_edit", "context.native_review", "context.native_apply", "context.native_readback"}:
        from litetui import native_context_edit
        return native_context_edit.dispatch(app, cmd)
    if operation in {"context.native_system_edit", "context.native_system_review",
                     "context.native_system_apply", "context.native_system_readback"}:
        from litetui import native_system_prompt
        return native_system_prompt.dispatch(app, cmd)
    if operation in {"plugins.native_list", "plugins.native_review", "plugins.native_apply", "plugins.native_readback"}:
        from litetui import native_plugin_reload
        return native_plugin_reload.dispatch(app, cmd)
    if operation in {"context.native_page", "context.native_topic_page"}:
        from litetui import native_context
        return native_context.page(app, cmd)
    if operation in {"work.native_execution_validate", "work.native_execution_readback"}:
        return getattr(native_work_execution, operation.removeprefix("work.native_execution_"))(app, cmd)
    if operation.startswith("work.native_execution_"):
        raise ValueError("Explicit execution requires asynchronous dispatch")
    if operation == "history.native_image":
        from litetui import native_history
        return native_history.image(app, cmd)
    if operation in {"history.native_snapshot", "history.native_page"}:
        from litetui import native_history
        return native_history.snapshot(app, cmd) if operation.endswith("snapshot") else native_history.page(app, cmd)
    if operation in {"work.native_control_validate", "work.native_control_apply", "work.native_control_readback"}:
        from litetui import native_work_controls
        return getattr(native_work_controls, operation.removeprefix("work.native_control_"))(app, cmd)
    if operation.startswith("work.native_deferred_"):
        from litetui import native_deferred
        return getattr(native_deferred, operation.removeprefix("work.native_deferred_"))(app, cmd)
    if operation == "work.native_schedule_manual_review":
        from litetui import native_manual_schedule
        return native_manual_schedule.review(app, cmd)
    if operation in {"work.native_schedule_decline", "work.native_schedule_readback"}:
        return getattr(native_schedule, operation.removeprefix("work.native_schedule_"))(app, cmd)
    if operation == "work.native_schedule_accept":
        raise ValueError("Scheduled acceptance requires asynchronous dispatch")
    if operation in {"work.native_capture", "work.native_activate"}:
        from litetui import background_input
        return background_input.capture(app, cmd) if operation.endswith("capture") else background_input.activate(app, cmd)
    if operation == "work.native_settled":
        raise ValueError("Work settlement requires asynchronous dispatch")
    if operation.startswith("work.native_"):
        from litetui import native_work
        action = operation.removeprefix("work.native_")
        if action == "stop_apply":
            raise ValueError("Task Stop requires asynchronous dispatch")
        if action not in native_work.OPERATIONS:
            raise ValueError("Unknown native Work inspection operation")
        return getattr(native_work, action)(app, cmd)
    if operation == "themes.native_snapshot":
        from litetui import native_themes
        return native_themes.snapshot(app, cmd)
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
        include_conversations = cmd.get("include_conversations", True)
        if type(include_conversations) is not bool:
            raise ValueError("include_conversations must be a boolean")
        return {"session_id": app.convo_id, "busy": app._chat_running() or bool(getattr(app, "_gui_management_busy", False)), "mode": "plan" if app._plan_mode else "normal",
                # T1049 (Dijkstra F1): the LOCKED seat profile. LiteGUI echoes this on
                # every prompt (App.tsx:144); the raw choice made a locked seat refuse every Send.
                "tool_profile": seat_authority.seat_profile(app), "model": app._rpc_model_state(),
                "settings": _settings(app),
                "conversations": _conversations(app, "list", {}) if include_conversations else [],
                "jobs": _jobs(app, "list", {}), "tasks": [t.to_row() for t in app.bg_tasks.values()],
                "ownership": {"writable": app.store.owned, "data_version": DATA_VERSION,
                              "agent_id": app._agent_session.authority.agent_id
                              if getattr(app, "_agent_session", None) is not None else None},
                "tool_policy": _tool_policy(app),
                "restored_input": restored_input.status(app),
                "schedules_paused": bool(getattr(app, "_gui_schedules_paused", False)),
                "active_work": active_work(app), "usage": getattr(app, "_gui_usage", None),
                "pending_approvals": [getattr(app, "_gui_approval_details", {}).get(key, {"id": key}) for key, future in getattr(app, "_approval_waiters", {}).items() if not future.done()],
                "pending_questions": [{"type": "user_input_requested", "id": key, "questions": [s.to_dict() for s in value[2]]} for key, value in getattr(app, "_rpc_pending_asks", {}).items() if not value[0].is_set()],
                "pending_model_loads": [{"request_id": key, **value["details"]} for key, value in getattr(app, "_gui_model_pending", {}).items()]}
    if operation == "backends.list":
        from litetui.backend_catalog import catalog

        return catalog(app, cmd)
    if operation == "thinking.set":
        _idle(app)
        from litetui.thinking_capabilities import set_thinking, thinking_capabilities
        set_thinking(app, cmd.get("level"))
        return {"level": app.thinking_level, "capabilities": thinking_capabilities(app)}
    if operation == "models.capabilities":
        from litetui.thinking_capabilities import thinking_capabilities
        backend = cmd.get("backend") or app.backend.name
        model = cmd.get("model") or app.model_id
        if backend == "claude" and backend != app.backend.name:
            from litetui.model_capabilities import offline_capabilities
            return offline_capabilities(backend, model)
        if backend != app.backend.name:
            raise ValueError("Requested backend catalogue is not connected; capability unavailable")
        if model == app.model_id:
            thinking = thinking_capabilities(app)
            thinking["level"] = app.thinking_level
            # /think's own picker rows and highlighted row (plugins/misc), so a native
            # frontend shows what Python's picker shows.
            from litetui.plugins.misc import UNSET, _thinking_rows
            thinking["picker"] = [[level, label] for level, label in _thinking_rows(app)]
            thinking["picker_current"] = app.thinking_level or UNSET
        else:
            reader = getattr(app.backend, "reasoning_levels", None)
            if not callable(reader) or model not in getattr(app.backend, "models", {}):
                raise ValueError("Requested model capability is unavailable in the connected catalogue")
            levels = reader(model)
            thinking = {"levels": ["default", *("off" if x == "none" else x for x in levels)],
                        "source": "connected backend model metadata"}
        return {"backend": backend, "model": model, "thinking": thinking}
    if operation in {"glassbox.native_snapshot", "glassbox.native_review", "glassbox.native_apply", "glassbox.native_readback"}:
        from litetui import native_glassbox
        return native_glassbox.dispatch(app, cmd)
    domain, action = operation.split(".", 1)
    from litetui import restart_checkpoint
    if domain == "checkpoint":
        from litetui.plugin_reload_activity import idle_infra_phase
        # Exclude this exact control worker, not a group or unrelated task.
        with idle_infra_phase(app):
            if action == "prepare":
                return restart_checkpoint.prepare(app, cmd.get("checkpoint_id"), disposition=cmd.get("disposition"))
            if action == "status":
                return restart_checkpoint.status(app)
            if action == "flush":
                return restart_checkpoint.flush(app, cmd.get("checkpoint_id"))
            if action == "release":
                return restart_checkpoint.release(app, cmd.get("checkpoint_id"), reconciled=cmd.get("reconciled"))
    if domain == "prompt" and restart_checkpoint.is_held(app):
        raise ValueError("Restart checkpoint hold blocks new input")
    if domain == "restored_input":
        _idle(app)
        if action == "list":
            return restored_input.inspect(app, cmd.get("session_id"))
        if action == "cancel":
            return restored_input.cancel(app, cmd)
        if action == "resolve":
            return restored_input.resolve(app, cmd)
        raise ValueError("Continuation requires asynchronous dispatch")
    if operation in {"prompt.native_pause_review", "prompt.native_pause_apply", "prompt.native_pause_readback"}:
        from litetui import native_chat_pause
        return native_chat_pause.dispatch(app, cmd)
    if operation in {"prompt.native_queue_review", "prompt.native_queue_apply", "prompt.native_send_review", "prompt.native_send_apply"}:
        from litetui import native_midturn
        return native_midturn.dispatch(app, cmd)
    if domain == "prompt":
        message, behavior = cmd.get("message"), cmd.get("behavior", "normal")
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be nonempty text")
        if behavior not in ("normal", "queue", "interrupt"):
            raise ValueError("behavior must be normal, queue or interrupt")
        from litetui import background_input
        if background_input.enabled(app) and behavior != "normal":
            raise ValueError("Explicit queue or interrupt requires captured user-input lifecycle support")
        if reason := background_input.prompt_refusal(app):
            raise ValueError(reason)
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
            return _model_picker_state(app)
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
    if operation == "hooks.native_test_review":
        from litetui import native_hook_test
        return native_hook_test.review(app, cmd)
    if operation in {"hooks.native_list", "hooks.native_review", "hooks.native_apply", "hooks.native_readback"}:
        from litetui import native_hook_toggle
        return native_hook_toggle.dispatch(app, cmd)
    if operation in {"hooks.native_edit_snapshot", "hooks.native_edit_review", "hooks.native_edit_apply", "hooks.native_edit_readback",
                     "hooks.native_repair_review", "hooks.native_repair_apply", "hooks.native_repair_readback"}:
        from litetui import native_hook_edit
        return native_hook_edit.dispatch(app, cmd)
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
            # T1043: the goal's origin; with gui.hello this reads "gui".
            app._command_source = "rpc"
            try:
                goal_loop.goal_command(app, arg)
            finally:
                app._command_source = None
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
    if operation == "images.native_preview":
        from litetui import native_image
        return native_image.preview(app, cmd)
    if operation == "images.native_clipboard":
        from litetui import native_image
        return native_image.clipboard(app, cmd)
    if operation == "images.native_prepare":
        from litetui import native_image
        return native_image.prepare(app, cmd)
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
    from litetui import native_claude_reset
    if operation in native_claude_reset.OPERATIONS:
        return await native_claude_reset.dispatch(app, cmd)
    if native_claude_reset.blocking(app):
        raise ValueError("Claude reset owns its exact readback publication")
    from litetui import native_mark
    if operation == native_mark.OPERATION:
        return await native_mark.dispatch(app, cmd)
    if native_mark.blocking(app):
        raise ValueError("Close the Mark review before another management action")
    from litetui import native_sidecar, native_sidecar_preferences
    if operation.startswith(native_sidecar_preferences.PREFIX):
        return native_sidecar_preferences.dispatch(app, cmd)
    if operation.startswith(native_sidecar.PREFIX):
        return await native_sidecar.dispatch(app, cmd)
    native_sidecar.allowed(app, cmd)
    from litetui import (
        native_dictation,
        native_playback,
        native_voice_actions,
        native_voice_catalog,
    )
    if operation == native_voice_actions.OPERATION:
        return await native_voice_actions.dispatch(app, cmd)
    if native_voice_actions.blocking(app):
        raise ValueError("Close the reviewed Voice action before another management action")
    if operation == native_voice_catalog.OPERATION:
        return await native_voice_catalog.dispatch(app, cmd)
    if native_voice_catalog.blocking(app):
        raise ValueError("Close voice discovery before another management action")
    if operation == native_playback.OPERATION:
        if native_dictation.blocking(app):
            raise ValueError("Finish or cancel dictation before playback")
        return await native_playback.dispatch(app, cmd)
    if native_playback.blocking(app):
        raise ValueError("Close response playback before another management action")
    if operation == native_dictation.OPERATION:
        return await native_dictation.dispatch(app, cmd)
    if native_dictation.blocking(app):
        raise ValueError("Finish or cancel dictation before another management action")
    from litetui import native_runtime_controls, native_work_execution
    if operation in native_runtime_controls.OPERATIONS:
        token = OPERATION_CONTEXT.set(cmd.get("id"))
        try:
            handler = getattr(native_runtime_controls, operation.removeprefix(native_runtime_controls.PREFIX))
            return await handler(app, cmd) if operation.endswith("_apply") else handler(app, cmd)
        finally:
            OPERATION_CONTEXT.reset(token)
    native_runtime_controls.operation_allowed(app, cmd)
    if operation in native_work_execution.OPERATIONS:
        token = OPERATION_CONTEXT.set(cmd.get("id"))
        try:
            handler = getattr(native_work_execution, operation.removeprefix(native_work_execution.PREFIX))
            return await handler(app, cmd) if operation.endswith(("_apply", "_stop")) else handler(app, cmd)
        finally:
            OPERATION_CONTEXT.reset(token)
    native_work_execution.operation_allowed(app, operation)
    # A known scheduled decision/readback grants no execution by itself. Route
    # it before generic shutdown refusal so a closing pre-stamp owner can return
    # its correlated safe refusal. accept still revalidates execution and every
    # canonical effect under its exact worker and scheduler lease.
    if operation in {"gui.work.native_schedule_accept", "gui.work.native_schedule_decline", "gui.work.native_schedule_readback"}:
        from litetui import native_schedule
        token = OPERATION_CONTEXT.set(cmd.get("id"))
        try:
            if operation.endswith("_accept"):
                return await native_schedule.accept(app, cmd)
            return getattr(native_schedule, operation.removeprefix("gui.work.native_schedule_"))(app, cmd)
        finally:
            OPERATION_CONTEXT.reset(token)
    from litetui import native_engine
    native_engine.operation_allowed(app, operation)
    from litetui import native_schedule
    native_schedule.operation_allowed(app, operation)
    from litetui import conversation_transition
    conversation_transition.require_execution(app)
    if operation == "gui.work.native_settled":
        # This exact recorded-admission join grants no execution. The source
        # validates its owned bridge and worker without blocking action retirement.
        from litetui import background_input
        token = OPERATION_CONTEXT.set(cmd.get("id"))
        try:
            return await background_input.settled(app, cmd)
        finally:
            OPERATION_CONTEXT.reset(token)
    from litetui import conversation_rewrite
    conversation_rewrite.operation_allowed(app, operation)
    _restart_gate(app, operation[4:])
    blocking = operation in {"gui.conversations.native_compact_apply", "gui.conversations.native_compact_settled", "gui.conversations.native_rewrite_apply", "gui.conversations.native_rewrite_settled", "gui.restored_input.reconcile", "gui.restored_input.continue", "gui.restored_input.settled", "gui.conversations.open", "gui.models.load", "gui.models.unload", "gui.models.configure", "gui.models.reconnect", "gui.tools.execute", "gui.hooks.test", "gui.hooks.native_test_run", "gui.context.set", "gui.backend.set", "gui.backends.native_models", "gui.engine.start", "gui.engine.stop", "gui.engine.native_action", "gui.settings.native_save", "gui.settings.native_compact_save", "gui.settings.native_form_save", "gui.settings.native_form_restore", "gui.themes.native_save", "gui.conversations.native_apply", "gui.conversations.native_open"} or operation in {"gui.mcp.connect", "gui.mcp.disconnect", "gui.mcp.reconnect", "gui.mcp.reload", "gui.mcp.add", "gui.mcp.remove"}
    if operation.startswith(("gui.models.native_preferences_", "gui.models.native_lifecycle_", "gui.mcp.native_lifecycle_")):
        blocking = True
    if operation == "gui.models.select":
        from litetui import background_input
        blocking = background_input.enabled(app)
    if blocking:
        if getattr(app, "_gui_management_busy", False):
            raise ValueError("Another management operation is active; wait or cancel it")
        app._gui_management_busy = True
        app._gui_management_owner = asyncio.current_task()
    token = OPERATION_CONTEXT.set(None if operation == "gui.prompt.submit" else cmd.get("id"))
    try:
        if operation in native_engine.RECOVERY:
            with native_engine.recovery(app):
                return await _async_dispatch(app, cmd)
        if operation in native_engine.READ_ONLY and not operation.startswith("gui.engine.native_"):
            # These operations return synchronously; no await shares the exemption.
            with native_engine.inspection(app):
                return dispatch(app, cmd)
        return await _async_dispatch(app, cmd)
    finally:
        OPERATION_CONTEXT.reset(token)
        if blocking:
            app._gui_management_busy = False
            app._gui_management_owner = None


async def _async_dispatch(app, cmd):
    operation = cmd["type"]
    from litetui import native_engine
    native_engine.operation_allowed(app, operation)
    from litetui import native_schedule
    native_schedule.operation_allowed(app, operation)
    from litetui import conversation_transition
    conversation_transition.require_execution(app)
    from litetui import native_settings_form
    if operation in native_settings_form.OPERATIONS:
        return await native_settings_form.dispatch(app, cmd)
    from litetui import native_system_lines
    if operation == native_system_lines.OPERATION:
        return native_system_lines.read(app, cmd)
    if operation.startswith("gui.mcp.native_lifecycle_"):
        from litetui import native_mcp_lifecycle
        return await native_mcp_lifecycle.dispatch(app, cmd)
    if operation.startswith("gui.models.native_lifecycle_"):
        from litetui import native_model_lifecycle
        return await native_model_lifecycle.dispatch(app, cmd)
    if operation.startswith("gui.models.native_preferences_"):
        from litetui import native_model_preferences
        return await native_model_preferences.dispatch(app, cmd)
    if operation == "gui.work.native_stop_apply":
        from litetui import native_work
        return await native_work.stop_apply(app, cmd)
    if operation == "gui.jobs.run":
        from litetui import background_input
        if background_input.enabled(app):
            return await _run_captured_job(app, cmd)
    if operation.startswith("gui.conversations.native_compact_"):
        from litetui import conversation_compact
        action = operation.removeprefix("gui.conversations.native_compact_")
        if action in {"snapshot", "validate"}:
            return getattr(conversation_compact, action)(app, cmd)
        if action in {"apply", "settled"}:
            return await getattr(conversation_compact, action)(app, cmd)
        raise ValueError("Unknown reviewed compaction operation")
    if operation.startswith("gui.conversations.native_rewrite_"):
        from litetui import conversation_rewrite
        action = operation.removeprefix("gui.conversations.native_rewrite_")
        if action == "validate" and hasattr(app.backend, "app_server"):
            return await conversation_rewrite.validate_codex(app, cmd)
        if action in {"snapshot", "validate"}:
            return getattr(conversation_rewrite, action)(app, cmd)
        if action in {"apply", "settled"}:
            return await getattr(conversation_rewrite, action)(app, cmd)
        raise ValueError("Unknown Edit/Retry operation")
    if operation == "gui.models.select":
        from litetui.plugins.model_switch import switch_model_captured
        _idle(app)
        target = str(cmd.get("slug", ""))
        tokens = cmd.get("context")
        changed = False
        if tokens is not None:
            # plugins/model_switch._choose_model: remember the size for this model, make it the
            # backend's main context setting, then switch; a re-pick with a new size applies it.
            from litetui import context_choice
            name = getattr(app.backend, "name", "")
            key = context_choice.main_key(name)
            if (type(tokens) is not int or key is None
                    or not context_choice.CTX_FLOOR <= tokens <= context_choice.CTX_CEILING):
                raise ValueError("Context length must be a token count this backend reads")
            if target not in app.available_models:
                raise ValueError("Model is not available")
            chosen = context_choice.settings_after(app.settings, name, target, tokens)
            changed = getattr(app.settings, key, None) != tokens
            settings_runtime.persist_or_raise(app, chosen, baseline=app.settings)
            app.settings = chosen
            set_settings = getattr(app.backend, "set_settings", None)
            if set_settings is not None:
                set_settings(chosen)
        unchanged_model = target == app.model_id
        result = await switch_model_captured(app, target)
        if tokens is not None and changed and unchanged_model:
            app.apply_context_length(force=True)  # a worker, as _choose_model starts it
        return result
    if operation == "gui.conversations.native_open":
        from litetui import native_history
        return await native_history.open_conversation(app, cmd)
    if operation.startswith("gui.conversations.native_"):
        from litetui import conversation_transition

        action = operation.rsplit("_", 1)[1]
        if action == "apply":
            return await conversation_transition.apply(app, cmd)
        if action in {"snapshot", "validate"}:
            return getattr(conversation_transition, action)(app, cmd)
        raise ValueError("Unknown native conversation operation")
    if operation.startswith("gui.engine.native_config_"):
        from litetui import native_engine_config
        return native_engine_config.dispatch(app, cmd)
    if operation.startswith("gui.engine.native_"):
        from litetui import native_engine
        name = operation.rsplit("_", 1)[1]
        if name in {"inspect", "validate"}:
            return getattr(native_engine, name)(app, cmd)
        if name in {"action", "readback"}:
            return await getattr(native_engine, name)(app, cmd)
        raise ValueError("Unknown native engine operation")
    if operation in {"gui.commands.list", "gui.commands.resolve"}:
        from litetui import command_surface

        return command_surface.catalog(app, cmd) if operation == "gui.commands.list" else command_surface.resolve(app, cmd)
    if operation == "gui.skills.native_prepare":
        from litetui import native_skills

        return native_skills.prepare(app, cmd)
    if operation in {"gui.themes.native_snapshot", "gui.themes.native_validate", "gui.themes.native_save"}:
        from litetui import native_themes
        action = operation.rsplit("_", 1)[1]
        return getattr(native_themes, action)(app, cmd)
    if operation in {"gui.settings.native_compact_snapshot", "gui.settings.native_compact_validate", "gui.settings.native_compact_save"}:
        from litetui import native_compaction_settings

        action = operation.rsplit("_", 1)[-1]
        return getattr(native_compaction_settings, action)(app, cmd)
    if operation in {"gui.settings.native_snapshot", "gui.settings.native_validate", "gui.settings.native_save"}:
        from litetui import native_settings

        action = operation.rsplit("_", 1)[1]
        return getattr(native_settings, action)(app, cmd)
    if operation == "gui.restored_input.reconcile":
        _idle(app)
        return await restored_input.reconcile(app, cmd)
    if operation == "gui.restored_input.continue":
        _idle(app)
        return await restored_input.continue_input(app, cmd)
    if operation == "gui.restored_input.settled":
        return await restored_input.settled(app, cmd)
    if operation == "gui.backends.native_models":
        from litetui.backend_transition import candidate_models
        return await candidate_models(app, cmd)
    if operation == "gui.backend.set":
        from litetui.backend_transition import GUARDED_FIELDS, transition

        if GUARDED_FIELDS.intersection(cmd):
            return await transition(app, cmd)
        _idle(app)
        candidate = _validate(app, {"backend": cmd.get("name")})
        settings_runtime.persist_or_raise(app, candidate)
        app.settings = candidate
        return await _async_dispatch(app, {"type": "gui.models.reconnect"})
    if operation == "gui.context.set":
        _idle(app)
        value = cmd.get("context")
        if type(value) is not int or value < 1:
            raise ValueError("context must be a positive integer")
        if getattr(app.backend, "remote", False):
            raise ValueError("The remote backend owns its context window")
        if not app.model_id:
            raise ValueError("Select a model before setting context")
        # Same backend.load owner as apply_context_length; do not swallow load failure.
        await app.backend.load(app.model_id, ctx=value)
        app._fetch_ctx_window()
        return {"requested": value, "model": app._rpc_model_state(), "completed": True}
    if operation.startswith("gui.engine."):
        _idle(app)
        backend = app.backend
        from litetui import llm_backend
        if backend.name not in llm_backend.ENGINE_BACKENDS:
            from litetui import gpu_gate
            # T893: NInfer is never named on a box that is not an RTX 5090 (as /engine's own refusal).
            engines = "NInfer, Strata or Xolotl" if gpu_gate.is_rtx_5090() else "Strata or Xolotl"
            raise ValueError(f"engine operations require the {engines} backend")
        action = operation.rsplit(".", 1)[1]
        if action == "status":
            return {"status": backend.engine_status()}
        if action == "start":
            result = await backend.start_engine()
            return {"result": result, "status": backend.engine_status()}
        if action == "stop":
            from litetui import agent_preparation
            if not backend.begin_stop():
                raise ValueError(f"{getattr(backend, 'label', 'NInfer')} engine start/stop is already active")
            try:
                result = await agent_preparation.await_preparation(backend.stop_engine)
                return {"result": result, "status": backend.engine_status()}
            finally:
                backend.end_stop()
        raise ValueError("Unsupported engine operation")
    if operation == "gui.conversations.open":
        from litetui import backend_transition, background_input, conversation_page
        proof = None
        value = background_input.bridge(app)
        if background_input.enabled(app):
            backend_transition._quiet(app, require_unowned_server=False)
            if "from_session_id" in cmd or "snapshot" in cmd:
                conversation_page.validate_open(app, cmd)
            target = cmd.get("session_id") or app.convo_id
            if not _session_path(target, app).is_file():
                raise ValueError("Conversation does not exist")
            _read_session(target, app)
            proof = await background_input.before_identity_transition(app)
            app._work_open_owner = (value, asyncio.current_task())
        try:
            dispatch(app, cmd)
            conversation = app.conversation
            backend = app.backend
            session_id = app.convo_id
            model = app.model_id
            connection = getattr(app, "_resume_connect_worker", None)
            if connection is None:
                reason = getattr(app, "_resume_backend_error", None)
                raise ValueError(reason or "Conversation connection did not start")

            def unchanged():
                if (app.conversation is not conversation or app.backend is not backend
                        or app.convo_id != session_id or app.model_id != model
                        or getattr(app, "_resume_connect_worker", None) is not connection):
                    raise ValueError("Conversation changed while it was opening")

            # _resume starts connect; connect creates the native-history worker.
            # Looking for history first acknowledges success before either can fail.
            await connection.wait()
            unchanged()
            reason = (getattr(app, "_resume_backend_error", None)
                      or getattr(app, "_resume_connection_error", None))
            if (reason or not getattr(app, "_connect_settled", False)
                    or not getattr(app, "_gui_connection_success", False)):
                raise ValueError(reason or "Conversation connection did not complete")
            worker = getattr(app, "_native_history_worker", None)
            if worker is not None:
                await worker.wait()
                unchanged()
                if getattr(app, "_native_history_worker", None) is not worker:
                    raise ValueError("Conversation changed while native history was loading")
            result = dispatch(app, {"type": "gui.conversations.read", "session_id": session_id})
            result["connected"] = True
            result.update(background_input.after_identity_transition(app, proof))
            return result
        except BaseException:
            if proof is not None:
                app._backend_transition_error = backend_transition.CLOSE_REQUIRED
            raise
        finally:
            app._work_open_owner = None
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
        for name in ("lm_host", "ninfer_host", "strata_host", "xolotl_host", "llama_host", "llama_executable", "backend", "default_model"):
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
            owners = (app.settings, app.backend, app.conversation, getattr(app, "_agent_session", None))
            authority = owners[3].authority if owners[3] is not None else None
            settings_runtime.persist_or_raise(app, candidate)
            if owners[3] is not None:
                from litetui import native_settings
                try:
                    if any(left is not right for left, right in zip(
                            owners, (app.settings, app.backend, app.conversation, app._agent_session), strict=True)):
                        raise ValueError("Model configuration lost its execution owner")
                    service = settings_runtime.service_for(app)
                    saved = service.snapshot(app.convo_id, read_only=True, max_bytes=1024 * 1024)
                    if (app._agent_session.authority != authority
                            or native_settings.owned_documents(service, app.convo_id, app._agent_session)[2] != saved.revisions
                            or any(not native_settings._same(getattr(saved.saved, key), getattr(candidate, key))
                                   for key in ("model_infer_overrides", "llama_load_settings", "llama_presets"))):
                        raise ValueError("Model configuration failed durable owner readback")
                except Exception:
                    app._owned_launch_error = "Model configuration is uncertain; close and reopen."
                    raise
            app.settings = candidate
            # `candidate` is a fresh object; re-point the backend or these
            # saved per-model settings stay on the stale object it captured.
            if hasattr(app.backend, "set_settings"):
                app.backend.set_settings(candidate)
            if owners[3] is not None:
                from litetui import effort_projection
                try:
                    effort_projection.install(app)
                except Exception:
                    app._owned_launch_error = "Model configuration is uncertain; close and reopen."
                    raise
            if cmd.get("apply_load", False) and prior != load:
                await app.backend.apply_load_settings(key, load)
            return {"slug": key, "saved": True, "load_applied": bool(cmd.get("apply_load", False)),
                    "load": load, "inference": inference}
        ctx = cmd.get("ctx")
        if ctx is not None and (action != "load" or type(ctx) is not int or ctx < 1):
            raise ValueError("ctx is supported only for load and must be a positive integer")
        await getattr(app.backend, action)(key, **({"ctx": ctx} if ctx is not None else {}))
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
        app._active_tool_profile = seat_authority.seat_profile(app)
        try:
            text, ok = await app._execute_tool(name, dict(args))
            return {"name": name, "ok": ok, "result": text}
        finally:
            app._active_tool_profile = prior
    if operation in {"gui.mcp.list", "gui.mcp.connect", "gui.mcp.disconnect", "gui.mcp.reconnect",
                     "gui.mcp.reload", "gui.mcp.add", "gui.mcp.remove"}:
        from litetui.agent_preparation import await_preparation
        manager = app.mcp
        action = operation.rsplit(".", 1)[1]
        if action == "list":
            return manager.describe()
        _idle(app)
        try:
            if action == "reload":
                manager.reload_configs()
            else:
                name = cmd.get("name")
                if not isinstance(name, str) or not name:
                    raise ValueError("name is required")
                if action == "add":
                    result = await await_preparation(lambda: manager.add(name, cmd.get("config")))
                elif action in ("connect", "disconnect", "reconnect", "remove"):
                    result = await await_preparation(lambda: getattr(manager, action)(name))
                else:
                    raise ValueError("Unknown MCP operation")
                if isinstance(result, str) and result:
                    raise ValueError(result)
        finally:
            if app.mcp is manager:
                app.rebuild_mcp_dispatch()
        if app.mcp is not manager:
            raise ValueError("MCP manager changed; refresh to inspect the captured operation outcome")
        if action == "disconnect" and name in getattr(manager, "_stop_failed", ()):
            raise ValueError("MCP stop is unconfirmed; the owned server is retained for explicit cleanup retry")
        return manager.describe()
    if operation.startswith("gui.monitor.native_"):
        from litetui import native_monitor
        return await native_monitor.dispatch(app, cmd)
    if operation == "gui.hooks.native_test_run":
        from litetui import native_hook_test
        return await native_hook_test.run(app, cmd)
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
