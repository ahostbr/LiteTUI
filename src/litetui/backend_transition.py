"""Owned backend transitions with explicit publication and fatal ambiguity."""
from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import asdict
from pathlib import Path

from litetui import backend_catalog, llm_backend
from litetui.agent_store import _unlinked, valid_id

VERSION = 1
_LOCAL = frozenset({"custom", "lmstudio", "llamacpp", "ninfer", "strata", "xolotl"})
GUARDED_FIELDS = frozenset({"version", "session_id", "agent_id", "snapshot", "model"})
CLOSE_REQUIRED = "Backend transition is uncertain; close this runtime and reopen explicitly."


def require_open(app):
    from litetui import native_engine

    native_engine.require_open(app)
    from litetui import conversation_transition
    conversation_transition.require_open(app)
    from litetui import conversation_rewrite
    conversation_rewrite.require_open(app)
    reason = getattr(app, "_backend_transition_error", None)
    if reason:
        raise ValueError(reason)


def submission_refusal(app, *, initializing=False):
    from litetui import conversation_transition
    if reason := conversation_transition.submission_refusal(app):
        return reason
    from litetui import conversation_rewrite
    if reason := conversation_rewrite.submission_refusal(app):
        return reason
    reason = getattr(app, "_backend_transition_error", None)
    if reason:
        return reason
    if getattr(app, "_backend_transition_inflight", False):
        return "A backend transition is active; wait before sending or changing the conversation."
    from litetui import native_engine
    if initializing and native_engine.state(app) is None:
        return None
    return native_engine.submission_refusal(app)


def _quiet(app, *, require_unowned_server=True):
    from textual.worker import NoActiveWorker, get_current_worker

    from litetui.gui_rpc import _idle, active_work
    from litetui.plugin_reload_activity import produce_activity
    from litetui.plugin_reload_children import children_pending
    from litetui.plugin_reload_state import blocking_reasons

    _idle(app)
    backend_catalog._context(app, app.convo_id)
    _journal_idle(app)
    if getattr(app, "_hold_restored_input", False) is not True:
        raise ValueError("Backend switching requires restored-input hold policy")
    if (getattr(app, "_pending_input", ()) or getattr(app, "_restored_input_error", False)
            or getattr(app, "_codex_recovery_pending", False)):
        raise ValueError("Resolve queued or uncertain input before switching backend")
    if any(row["kind"] != "management" for row in active_work(app)):
        raise ValueError("Owned work is active; wait before switching backend")
    try:
        current = get_current_worker()
    except NoActiveWorker:
        current = None
    from litetui import background_input
    ignored = (() if current is None else (current,)) + background_input.transition_waiters(app)
    report = produce_activity(
        app, ignore_workers=ignored,
        children_pending=lambda: children_pending(Path.home() / ".litetui-agents", app.convo_id),
    )
    reasons = blocking_reasons(report.snapshot)
    if reasons:
        raise ValueError("Backend switch requires idle owned work: " + "; ".join(reasons))
    if (getattr(app, "_gui_host_pending", {}) or getattr(app, "_gui_model_pending", {})
            or any(not future.done() for future in getattr(app, "_approval_waiters", {}).values())
            or any(not item[0].is_set() for item in getattr(app, "_rpc_pending_asks", {}).values())):
        raise ValueError("Resolve pending tool, model, approval or question work before switching backend")
    server = getattr(app.backend, "app_server", None)
    if server is not None:
        activity = server.runtime_activity
        if activity.uncertain or activity.commands or activity.requests or (
                getattr(server, "process", None) is not None and not activity.connected):
            raise ValueError("Native backend work is pending or unknown")
    if require_unowned_server and (getattr(app, "_custom_server_process", None) is not None
            or getattr(app.backend, "_owned", None) is not None):
        raise ValueError("Stop the outgoing owned local server explicitly before switching backend")
    session = getattr(app.backend, "session", None)
    lifecycle = getattr(session, "lifecycle", None)
    if lifecycle is not None and getattr(lifecycle, "cleanup_errors", False):
        raise ValueError("Native backend cleanup is unresolved")



def _journal_idle(app):
    """Read hidden pending delivery evidence; never restore, reconcile or send it."""
    from litetui import restored_claude, restored_input
    from litetui.conversation import ConversationRepository

    histories = [app.conversation]
    path = _unlinked(app.store.convo_path)
    if path.exists():
        meta, messages = ConversationRepository.read(path, max_bytes=8 * 1024 * 1024)
        if meta.get("id") != app.convo_id:
            raise ValueError("Current transcript identity changed")
        histories.append(messages)
    for messages in histories:
        delivered = {message.get("codex_delivery", {}).get("id") for message in messages}
        for ident, (entry, _entries, _index) in restored_input._records(messages).items():
            if ident not in delivered and entry.get("state") != "denied":
                raise ValueError("Resolve saved Codex input before switching backend")
    ledger = restored_claude._read(app.store)
    if any(ledger.pending(ident) for ident in ledger.document["segments"]):
        raise ValueError("Resolve saved Claude input before switching backend")


def _receipt(app, cmd, status, *, mutated=False, close_required=False, error=None):
    return {
        "version": VERSION, "kind": "backend_transition", "status": status,
        "snapshot": cmd.get("snapshot"), "mutated": mutated, "close_required": close_required,
        "session_id": getattr(app, "convo_id", None), "agent_id": cmd.get("agent_id"),
        "backend": getattr(getattr(app, "backend", None), "name", None),
        "model": getattr(app, "model_id", None), "error": error,
    }


async def transition(app, cmd):
    """Refusal is recoverable only before candidate I/O; later failures close."""
    candidate = None
    context = None
    mutated = False
    try:
        require_open(app)
        if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
            raise ValueError("Guarded backend transition requires version 1")
        _quiet(app)
        from litetui import native_mcp_lifecycle
        native_mcp_lifecycle.require_transition(app)
        catalog = backend_catalog.catalog(app, {"session_id": cmd.get("session_id")})
        if valid_id(cmd.get("agent_id")) != catalog["agent_id"]:
            raise ValueError("Backend selection belongs to another agent")
        token = cmd.get("snapshot")
        if (not isinstance(token, str) or not re.fullmatch("[0-9a-f]{64}", token)
                or token != catalog["snapshot"]):
            raise ValueError("Backend selection is stale; refresh before switching")
        name = cmd.get("name")
        if name not in {row["name"] for row in catalog["entries"]}:
            raise ValueError("Backend selection is not visible in the source catalog")
        # Ryan 2026-11-12: "/backend switches backend only /model displays models that backend
        # provides". The model is chosen afterwards by /model (gui.models.select).
        if "model" not in cmd or cmd["model"] is not None:
            raise ValueError("/backend switches the backend only; choose the model with /model after it")
        if "context" in cmd:
            raise ValueError("A backend-only switch chooses no context")
        context = backend_catalog._context(app, catalog["session_id"])
        if getattr(app, "_startup_history_path", None) is not None:
            raise ValueError("Current history is still connecting")
    except Exception as exc:  # noqa: BLE001 - all preflight failures have a recoverable wire result
        return _receipt(app, cmd, "refused", error=str(exc)[:2048])
    return await _switch_only(app, cmd, name, token, context)


async def _switch_only(app, cmd, name, token, context):
    """/backend as Python does it: switch and save the backend at once, the model unchosen.

    Ryan 2026-11-12: "/backend switches backend only /model displays models that backend
    provides". App.apply_backend_change is Python's own sequence (save, close the old native
    session, the level last used there, "Backend → NAME; reconnecting…", connect). A backend that
    does not answer leaves the seat on it, disconnected, as in Python. Local backends connect
    only: nothing is started or loaded.
    """
    from litetui import background_input, gpu_gate
    from litetui.gui_rpc import _tool_policy
    from litetui.launch_options import LaunchOptions

    if name == app.backend.name:
        return _receipt(app, cmd, "refused", error=f"Already on {name}")
    if name == "ninfer" and not gpu_gate.is_rtx_5090():
        return _receipt(app, cmd, "refused", error=gpu_gate.why_not())
    if reason := submission_refusal(app):
        return _receipt(app, cmd, "refused", error=reason)
    old_launch = getattr(app, "_launch_options", None)
    launch = LaunchOptions(server_mode="connect" if name in _LOCAL else "auto",
                           timeout=getattr(old_launch, "timeout", 600),
                           max_tokens=getattr(old_launch, "max_tokens", None)
                           if name not in {"codex", "claude"} else None,
                           history_projection_version=getattr(old_launch, "history_projection_version", None),
                           work_lifecycle_version=getattr(old_launch, "work_lifecycle_version", None),
                           mcp_lifecycle_version=getattr(old_launch, "mcp_lifecycle_version", None),
                           thinking_explicit=getattr(old_launch, "thinking_explicit", None))
    conversation, history = app.conversation, copy.deepcopy(app.conversation)
    policy = _tool_policy(app)
    old_client = app.client
    mutated = False
    app._backend_transition_inflight = True
    try:
        work_proof = await background_input.before_identity_transition(app)
        _quiet(app)
        if (app.conversation is not conversation or app.conversation != history
                or backend_catalog.catalog(app, {"session_id": context.session_id})["snapshot"] != token):
            raise ValueError("Backend selection changed before publication")
        for mapping in ("_invocation_saved_values", "_launch_overrides"):
            values = getattr(app, mapping, {})
            values.pop("backend", None)
            values.pop("default_model", None)
        app._cli_initial_backend = None
        app._launch_backend, app._launch_options, app._launch_prepared = name, launch, False
        mutated = True
        # apply_backend_change refuses while a transition is in flight; this one is it.
        app._backend_transition_inflight = False
        connection = app.apply_backend_change(name)
        app._backend_transition_inflight = True
        if app.backend.name != name or app.settings.backend != name:
            raise ValueError("Backend switch did not take")
        if (context.session.authority.backend, context.session.authority.model) != (name, None):
            raise ValueError("Published backend switch failed authority readback")
        await connection.wait()
        if app.backend.name != name or app.model_id:
            raise ValueError("Backend switch identity changed while connecting")
        if app.client is not old_client and not old_client.is_closed():
            await old_client.close()
        if app.conversation is not conversation or app.conversation != history or _tool_policy(app) != policy:
            raise ValueError("History or tool policy changed during backend switch")
        if getattr(app, "_pending_input", ()):
            raise ValueError("Input arrived during backend switch")
        if not hasattr(app, "_gui_effective_settings"):
            app._gui_effective_settings = asdict(app.settings)
        for key in ("backend", "default_model"):
            app._gui_effective_settings[key] = getattr(app.settings, key)
        receipt = _receipt(app, cmd, "switched", mutated=True)
        receipt["model"] = None
        receipt["connected"] = bool(getattr(app, "_gui_connection_success", False))
        # App._offer_unchosen_model: Python opens /model once a session when the backend lists
        # models and none is chosen (connect skips that for engine backends and under RPC; the
        # native frontend opens it on this answer).
        offer = bool(app.available_models and name not in llm_backend.ENGINE_BACKENDS
                     and not getattr(app, "_unchosen_picker_offered", False))
        if offer:
            app._unchosen_picker_offered = True
        receipt["offer_model_picker"] = offer
        receipt.update(background_input.after_identity_transition(app, work_proof))
        return receipt
    except BaseException as exc:
        if not mutated and not isinstance(exc, asyncio.CancelledError):
            return _receipt(app, cmd, "refused", error=(str(exc) or type(exc).__name__)[:2048])
        app._backend_transition_error = CLOSE_REQUIRED
        app._owned_launch_error = CLOSE_REQUIRED
        app._cli_launch_error = CLOSE_REQUIRED
        if isinstance(exc, asyncio.CancelledError):
            raise
        return _receipt(app, cmd, "failed", mutated=True, close_required=True,
                        error=(str(exc) or type(exc).__name__)[:2048])
    finally:
        app._backend_transition_inflight = False
