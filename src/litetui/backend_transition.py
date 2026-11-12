"""Owned backend/model transitions with explicit publication and fatal ambiguity."""
from __future__ import annotations

import asyncio
import copy
import re
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from litetui import backend_catalog, llm_backend, settings_runtime
from litetui.agent_store import _unlinked, valid_id
from litetui.launch_options import LaunchOptions
from litetui.settings_service import SettingChange

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


async def _close_backend(app, backend):
    from litetui.claude_backend import close_native, settle_close

    if backend.name == "lmstudio":
        backend.shutdown()
        session = getattr(backend, "_sdk_session", None)
        if session is not None and not await asyncio.to_thread(session.wait_closed, timeout=30):
            raise ValueError("Outgoing LM Studio SDK cleanup did not settle")
    elif getattr(backend, "owns_native_turns", False):
        close_native(app, backend)
        failures = await settle_close(app)
        if failures:
            raise ValueError("Outgoing native cleanup failed: " + "; ".join(failures))
    elif hasattr(backend, "app_server"):
        server = backend.app_server
        backend.shutdown()
        # Process exit is not reader settlement: its EOF/finally drains pending
        # requests, questions and runtime activity before ownership can move on.
        await asyncio.wait_for(server.close(), 30)
        process = getattr(server, "process", None)
        if process is not None and process.returncode is None:
            raise ValueError("Outgoing native process did not settle")


def _check_thinking(backend, settings, model, level):
    from litetui.thinking_capabilities import thinking_capabilities

    view = SimpleNamespace(backend=backend, settings=settings, model_id=model,
                           _model_thinking_levels=None)
    if level not in thinking_capabilities(view)["levels"]:
        raise ValueError("Current thinking level is unsupported by the selected model; choose it explicitly before switching")


def _same_selection(app, context, candidate, model):
    if (app._agent_session is not context.session or app.store is not context.store
            or app.convo_id != context.session_id or app.backend is not candidate
            or app.model_id != model or app.settings.backend != candidate.name):
        raise ValueError("Backend transition identity changed")
    authority = context.session.authority
    if (authority.backend, authority.model) != (candidate.name, model):
        raise ValueError("Backend transition authority disagrees with selection")


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
        model = backend_catalog._text(cmd.get("model"), 4096)
        if not model.strip() or any(ord(char) < 32 or ord(char) == 127 for char in model):
            raise ValueError("Select a nonempty explicit model without control characters")
        from litetui import context_choice
        tokens = cmd.get("context")
        if tokens is not None:
            if (type(tokens) is not int or context_choice.main_key(name) is None
                    or not context_choice.CTX_FLOOR <= tokens <= context_choice.CTX_CEILING):
                raise ValueError("Context length must be a token count this backend reads")
        context = backend_catalog._context(app, catalog["session_id"])
        if getattr(app, "_startup_history_path", None) is not None:
            raise ValueError("Current history is still connecting")
        service = settings_runtime.service_for(app)
        saved = service.snapshot(context.session_id, read_only=True)
        settings = copy.deepcopy(app.settings)
        settings.backend, settings.default_model = name, model
        old_launch = getattr(app, "_launch_options", None)
        launch = LaunchOptions(server_mode="connect" if name in _LOCAL else "auto",
                               timeout=getattr(old_launch, "timeout", 600),
                               max_tokens=getattr(old_launch, "max_tokens", None)
                               if name not in {"codex", "claude"} else None,
                               history_projection_version=getattr(old_launch, "history_projection_version", None),
                               work_lifecycle_version=getattr(old_launch, "work_lifecycle_version", None),
                               mcp_lifecycle_version=getattr(old_launch, "mcp_lifecycle_version", None),
                               thinking_explicit=getattr(old_launch, "thinking_explicit", None))
        candidate = llm_backend.make_backend(settings)
        if name == "llamacpp":
            candidate.require_existing_server()
        if candidate.name != name:
            raise ValueError("Backend factory substituted the requested backend")
        history = copy.deepcopy(app.conversation)
        conversation = app.conversation
        old_backend, old_client = app.backend, app.client
        from litetui.gui_rpc import _tool_policy
        policy = _tool_policy(app)
    except Exception as exc:  # noqa: BLE001 - all preflight failures have a recoverable wire result
        return _receipt(app, cmd, "refused", error=str(exc)[:2048])

    app._backend_transition_inflight = True
    # Until the candidate has been checked, nothing of the running session has changed: a
    # failure there (connect, model list, an unsupported thinking level, its timeout) is a
    # refusal in Python's words and the session goes on (Ryan's LM Studio switch, 2026-11-12).
    checked = False
    try:
        from litetui.launch_options import prepare

        view = SimpleNamespace(backend=candidate, _launch_options=launch,
                               _launch_backend=name, _launch_prepared=False, _system=app._system)
        async with asyncio.timeout(min(launch.timeout, 60)):
            await prepare(view)
            rows = await candidate.list_models()
            selected = [row for row in rows if row.key == model and
                        not any(skip in row.key.lower() for skip in ("embed", "embedding"))]
            # Python's /model lists every model the backend serves, loaded or not; picking one
            # selects it (no fallback) and switch_model's apply_context_length loads it.
            if len(selected) != 1:
                raise ValueError("Requested model is unavailable; no fallback was used")
            from litetui import thinking_capabilities
            # The level last used with this backend comes back with it, else the current one stays.
            level = thinking_capabilities.level_after_switch(app.settings, name, context.authority.thinking_level)
            _check_thinking(candidate, settings, model, level)
        checked = True
        _quiet(app)
        if (app.conversation is not conversation or app.conversation != history
                or backend_catalog.catalog(app, {"session_id": context.session_id})["snapshot"] != token):
            raise ValueError("Backend selection changed before publication")
        from litetui import background_input
        work_proof = await background_input.before_identity_transition(app)
        changes = [SettingChange("backend", name, "conversation"),
                   SettingChange("default_model", model, "conversation")]
        # A publication attempt may have committed before its readback fails.
        mutated = True
        result = service.save_patch(context.session_id, changes, saved.revisions)
        failures = [outcome.error for outcome in result.persistence if not outcome.saved]
        if failures:
            raise OSError("; ".join(failures))
        authority = context.session.authority
        if (authority.backend, authority.model, authority.thinking_level) != (
                name, model, context.authority.thinking_level):
            raise ValueError("Published backend selection failed authority readback")
        if level != authority.thinking_level:
            context.session.update_execution(backend=name, model=model, thinking_level=level)
            authority = context.session.authority
            if (authority.backend, authority.model, authority.thinking_level) != (name, model, level):
                raise ValueError("Restored thinking level failed authority readback")
        mutated = True
        from litetui import convo_settings

        app.settings = settings
        app._convo_settings = convo_settings.load(app.convo_dir)
        for mapping in ("_invocation_saved_values", "_launch_overrides"):
            values = getattr(app, mapping, {})
            values.pop("backend", None)
            values.pop("default_model", None)
        app._cli_initial_backend = None
        app._cli_initial_model = None
        app._user_model_choice = model
        app._launch_backend, app._launch_options, app._launch_prepared = name, launch, False
        app._model_thinking_levels = None
        # Only a real invocation flag pins effort across an explicit backend switch.
        app._thinking_level = None if authority.thinking_level == "default" else authority.thinking_level
        app.backend = candidate
        app.model_id = model
        app.available_models = []
        app.model_rows = {}
        app._native_history_worker = None
        await _close_backend(app, old_backend)
        connection = app.connect()
        await connection.wait()
        _same_selection(app, context, candidate, model)
        if not getattr(app, "_connect_settled", False) or not getattr(app, "_gui_connection_success", False):
            raise ValueError("Requested backend connection did not complete")
        worker = getattr(app, "_native_history_worker", None)
        if worker is not None:
            await worker.wait()
            _same_selection(app, context, candidate, model)
            if getattr(app, "_native_history_worker", None) is not worker:
                raise ValueError("Native history worker changed during transition")
        row = app.model_rows.get(model)
        if row is None or row.key != model or app.available_models.count(model) != 1:
            raise ValueError("Requested model is not listed after connection; no fallback was used")
        if app.client is not old_client:
            await old_client.close()
        _check_thinking(candidate, app.settings, model, authority.thinking_level)
        app._validate_owned_execution()
        if app.conversation is not conversation or app.conversation != history or _tool_policy(app) != policy:
            raise ValueError("History or tool policy changed during backend transition")
        if getattr(app, "_pending_input", ()):
            raise ValueError("Input arrived during backend transition")
        if not hasattr(app, "_gui_effective_settings"):
            app._gui_effective_settings = asdict(app.settings)
        for key in ("backend", "default_model"):
            app._gui_effective_settings[key] = getattr(app.settings, key)
        if tokens is not None:
            # plugins/model_switch._choose_model: remember it for this model and make it the backend's
            # main context setting. Saving loads nothing; the setting applies when the model loads.
            chosen = context_choice.settings_after(app.settings, name, model, tokens)
            settings_runtime.persist_or_raise(app, chosen, baseline=app.settings)
            app.settings = chosen
            set_settings = getattr(app.backend, "set_settings", None)
            if set_settings is not None:
                set_settings(chosen)
        if not row.loaded:
            # The user picked it: plugins/model_switch.switch_model's tail. Loads at the backend's
            # context setting (the answer above, if any) only when the model is not already serving
            # that much; a backend error is reported in its own words. A worker, as in Python.
            app.apply_context_length()
        receipt = _receipt(app, cmd, "connected", mutated=True)
        receipt.update(background_input.after_identity_transition(app, work_proof))
        return receipt
    except BaseException as exc:
        if not checked and not mutated and not isinstance(exc, asyncio.CancelledError):
            error = str(exc) or type(exc).__name__
            if isinstance(exc, TimeoutError):
                error = f"{name} did not answer in time; nothing changed"
            try:
                await _close_backend(app, candidate)
            except Exception as cleanup:  # noqa: BLE001 - the session is untouched; report both
                error += "; cleanup: " + (str(cleanup) or type(cleanup).__name__)
            return _receipt(app, cmd, "refused", error=error[:2048])
        # Publication can fail after the atomic replace but before its readback.
        try:
            mutated = mutated or context.session.authority != context.authority
        except (ValueError, OSError):
            mutated = True
        app._backend_transition_error = CLOSE_REQUIRED
        app._owned_launch_error = CLOSE_REQUIRED
        app._cli_launch_error = CLOSE_REQUIRED
        error = str(exc) or type(exc).__name__
        try:
            if candidate is not app.backend:
                await _close_backend(app, candidate)
            if app.client is not old_client and not old_client.is_closed():
                await old_client.close()
        except BaseException as cleanup:  # noqa: BLE001 - preserve the original fatal error during cleanup
            error += "; cleanup: " + (str(cleanup) or type(cleanup).__name__)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return _receipt(app, cmd, "failed", mutated=mutated, close_required=True, error=error[:2048])
    finally:
        app._backend_transition_inflight = False


MODEL_ROWS = 512


def _model_label(row, remote: bool) -> str:
    """Python's /model row (plugins/model_switch._row_label) for a row that is not yet current."""
    status = "ready" if remote else "loaded"
    return "  " + row.key + f"  · {row.source}" + (f"  · {status}" if row.loaded else "")


async def candidate_models(app, cmd):
    """List the models another backend serves, the way /model lists them after /backend.

    Python LiteTUI switches the backend, then connect() lists the new backend's models and, because a
    backend switch leaves the seat's model unchosen, opens the "Select a model" picker. The native
    frontend asks for that list first so the pick and the switch publish together. Connect-only for
    local backends: nothing is started or loaded. The candidate is closed before returning.
    """
    from litetui import context_choice
    require_open(app)
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Backend model listing requires version 1")
    _quiet(app)
    catalog = backend_catalog.catalog(app, {"session_id": cmd.get("session_id")})
    if valid_id(cmd.get("agent_id")) != catalog["agent_id"]:
        raise ValueError("Backend selection belongs to another agent")
    if cmd.get("snapshot") != catalog["snapshot"]:
        raise ValueError("Backend selection is stale; refresh before switching")
    name = cmd.get("name")
    if name not in {row["name"] for row in catalog["entries"]}:
        raise ValueError("Backend selection is not visible in the source catalog")
    if name == catalog["selected_backend"]:
        raise ValueError(f"Already on {name}")  # plugins/model_switch._switch_backend says the same
    settings = copy.deepcopy(app.settings)
    settings.backend = name
    old_launch = getattr(app, "_launch_options", None)
    launch = LaunchOptions(server_mode="connect" if name in _LOCAL else "auto",
                           timeout=getattr(old_launch, "timeout", 600))
    candidate = llm_backend.make_backend(settings)
    if candidate.name != name:
        raise ValueError("Backend factory substituted the requested backend")
    try:
        if name == "llamacpp":
            candidate.require_existing_server()
        view = SimpleNamespace(backend=candidate, _launch_options=launch,
                               _launch_backend=name, _launch_prepared=False, _system=app._system)
        from litetui.launch_options import prepare
        async with asyncio.timeout(min(launch.timeout, 60)):
            await prepare(view)
            rows = await candidate.list_models()
    finally:
        await _close_backend(app, candidate)
    rows = [row for row in rows if not any(skip in row.key.lower() for skip in ("embed", "embedding"))]
    if len(rows) > MODEL_ROWS:
        raise ValueError("Backend model list exceeds the picker budget")
    remote = bool(getattr(candidate, "remote", False))
    key = context_choice.main_key(name)
    return {
        "version": VERSION, "kind": "backend_models", "session_id": catalog["session_id"],
        "agent_id": catalog["agent_id"], "snapshot": catalog["snapshot"], "name": name,
        "models": [{"key": row.key, "label": _model_label(row, remote), "loaded": bool(row.loaded)}
                   for row in rows],
        "context": None if key is None else {
            "setting": key,
            "applies_on_engine_start": name in context_choice.APPLIES_ON_ENGINE_START,
            "suggestions": {row.key: context_choice.suggestion(settings, name, row.key) for row in rows},
            "remembered": {row.key: context_choice.remembered(settings, name, row.key) for row in rows},
        },
    }
