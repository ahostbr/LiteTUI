"""Reviewed current-conversation mutations with durable and lifecycle readback."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import secrets
import uuid
from dataclasses import asdict, dataclass
from types import SimpleNamespace

from litetui import backend_catalog, backend_transition, hook_host, lifecycle_hooks
from litetui.agent_store import _unlinked, valid_id
from litetui.conversation import ConversationRepository
from litetui.plugins.convo import normalize_title

VERSION = 1
MAX_RESPONSE = 65536
CLOSE_REQUIRED = "Conversation publication is uncertain; close this runtime and reopen explicitly."
IDENTITY = ("session_id", "agent_id", "backend", "model")
ECHO = (*IDENTITY, "version", "snapshot", "review", "action_id", "action", "target_session_id", "title")


@dataclass(frozen=True)
class Review:
    wire: bytes
    context: object
    hooks: lifecycle_hooks.Snapshot
    registry: tuple
    protected: bytes
    history: bytes
    transcript: str | None
    settings: str | None
    hook_source: str


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def submission_refusal(app):
    if getattr(app, "_conversation_transition_shutdown_requested", False):
        return "This conversation runtime is shutting down."
    if getattr(app, "_conversation_transition_error", None):
        return CLOSE_REQUIRED
    if getattr(app, "_conversation_transition_task", None) is not None:
        return "A conversation transition is active; wait before sending or changing this session."
    return None


def require_open(app):
    why = submission_refusal(app)
    owner = getattr(app, "_conversation_transition_task", None)
    from litetui.native_history import connection_reader
    reader = connection_reader(app) if owner is not None else False
    if why and (getattr(app, "_conversation_transition_error", None) or owner is not _task() and not reader):
        raise ValueError(why)


def require_execution(app):
    if why := submission_refusal(app):
        raise ValueError(why)


def _identity(app):
    session = getattr(app, "_agent_session", None)
    return {"session_id": app.convo_id,
            "agent_id": session.authority.agent_id if session is not None else None,
            "backend": app.backend.name, "model": app.model_id}


def _encoded(value, limit=8 * 1024 * 1024):
    return backend_catalog._encoded(value, limit)


def _bounded(value):
    if len(json.dumps(value).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("Conversation response exceeds its byte budget")
    return value


def _registry(app):
    registry = app.plugins
    commands = tuple((key, id(value), id(value.handler)) for key, value in registry.commands.items())
    sections = tuple((id(row), row.owner, row.order, id(row.render), id(row.enabled))
                     for row in registry.prompt_sections)
    return id(registry), commands, sections


def _hooks(app):
    """Use canonical merge semantics on bounded, stable captured documents."""
    if os.environ.get("LITETUI_HOOKS", "").lower() == "off":
        return lifecycle_hooks.Snapshot(disabled=True)
    config = getattr(app, "hook_config", None)
    if config is None:
        return lifecycle_hooks.Snapshot()
    documents = {}
    for scope in ("global", "project"):
        path = _unlinked(config.path(scope))
        before = backend_catalog._disk_hash(path, 65536)
        if before is None:
            documents[scope] = ()
            continue
        with path.open("rb") as source:
            data = source.read(65537)
        if len(data) > 65536 or hashlib.sha256(data).hexdigest() != before:
            raise ValueError("Lifecycle hook configuration changed while reading")
        if backend_catalog._disk_hash(path, 65536) != before:
            raise ValueError("Lifecycle hook configuration changed while reading")
        documents[scope] = lifecycle_hooks.HookConfig.parse_document(json.loads(data.decode("utf-8-sig")), scope)
    return lifecycle_hooks.HookConfig.snapshot(SimpleNamespace(read=documents.__getitem__))


def _protected(app):
    from litetui.gui_rpc import _tool_policy

    return _encoded({"settings": asdict(app.settings), "authority": asdict(app._agent_session.authority),
                     "policy": _tool_policy(app), "thinking": getattr(app, "_thinking_level", None),
                     "launch": asdict(app._launch_options) if getattr(app, "_launch_options", None) else None,
                     "invocation": getattr(app, "_invocation_saved_values", {}),
                     "overrides": getattr(app, "_launch_overrides", {}),
                     "hold": getattr(app, "_hold_restored_input", False),
                     "hook_source": hook_host.context(app)["source"],
                     "hook_workspace": str(getattr(app, "_hook_workspace", ""))})


def _eligibility(app):
    from litetui import native_engine

    if native_engine.state(app) is not None:
        return "Reopen explicitly before changing a native-engine conversation"
    try:
        backend_transition._quiet(app, require_unowned_server=False)
        if getattr(app, "_hook_lifecycle", ()) or getattr(app, "_hook_lock", None) and app._hook_lock.locked():
            raise ValueError("Conversation lifecycle hooks are still active")
        if getattr(app, "_claude_closing", None):
            raise ValueError("Outgoing native cleanup is still tracked; wait before changing conversation")
        if getattr(app, "_startup_history_path", None) is not None:
            raise ValueError("Current history is still connecting")
    except (ValueError, OSError) as exc:
        return str(exc)
    return None


def _snapshot(app, cmd):
    require_execution(app)
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Conversation transitions require version 1")
    context = backend_catalog._context(app, cmd.get("session_id"))
    identity = _identity(app)
    if any(cmd.get(key) != value for key, value in identity.items()):
        raise ValueError("Conversation transition belongs to another owned identity")
    before = backend_catalog.fingerprint(app, context, {"kind": "conversation_transition"})
    hooks, registry = _hooks(app), _registry(app)
    if hooks.error:
        raise ValueError(hooks.error)
    pending = bool(app.store.pending)
    if pending:
        meta, messages = {}, app.conversation
    else:
        meta, messages = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
        if meta.get("id") != app.convo_id or messages != app.conversation:
            raise ValueError("Current saved transcript disagrees with the owned live history")
    reason = _eligibility(app)
    effects = {"lifecycle_hooks": bool(hooks.hooks and not hooks.disabled),
               "provider_cleanup": bool(getattr(app.backend, "owns_native_turns", False)),
               "materialize": pending}
    result = {"version": VERSION, "kind": "conversation_transition_snapshot", **identity,
              "current": {"title": ConversationRepository.label(meta, messages), "pending": pending,
                          "materialized": not pending},
              "actions": {name: {"eligible": reason is None, "reason": reason} for name in ("rename", "new")},
              "effects": effects}
    facts = {"result": result, "registry": registry, "hooks": asdict(hooks),
             "hook_context": hook_host.context(app),
             "hook_conversation": getattr(app, "_hook_conversation_id", None),
             "hook_workspace": str(getattr(app, "_hook_workspace", ""))}
    token = backend_catalog.fingerprint(app, context, facts)
    if (_hooks(app) != hooks or _registry(app) != registry or backend_catalog._context(app, app.convo_id) != context
            or backend_catalog.fingerprint(app, context, {"kind": "conversation_transition"}) != before):
        raise ValueError("Conversation changed while reviewing; refresh")
    result["snapshot"] = token
    return _bounded(result), context, hooks, registry


def snapshot(app, cmd):
    result = _snapshot(app, cmd)[0]
    app._conversation_transition_protocol = True
    return result


def _matching(hooks, event, source):
    return () if hooks.disabled else hooks.matching(event, source)


def validate_title_input(raw: object) -> str:
    """Share native draft bounds without normalizing or authorizing a title."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 4096 or "\0" in raw:
        raise ValueError("Title must be text within 4096 UTF-8 bytes without NUL")
    return raw


def validate(app, cmd):
    current, context, hooks, registry = _snapshot(app, cmd)
    if cmd.get("snapshot") != current["snapshot"]:
        raise ValueError("Conversation review is stale; refresh")
    action = cmd.get("action")
    if action not in ("rename", "new"):
        raise ValueError("Only current-session Rename and New are supported")
    if not current["actions"][action]["eligible"]:
        raise ValueError(current["actions"][action]["reason"])
    title = None
    if action == "rename":
        raw = validate_title_input(cmd.get("title"))
        title = normalize_title(raw)
        if not title:
            raise ValueError("Title must contain non-whitespace text")
    elif cmd.get("title") is not None:
        raise ValueError("New does not accept a title")
    target = str(uuid.uuid4()) if action == "new" else app.convo_id
    if action == "new" and _unlinked(context.session.conversation_directory(target)).exists():
        raise ValueError("Reserved conversation identity already exists; review again")
    source = hook_host.context(app)["source"]
    materialize = action == "new" or app.store.pending
    events = (["conversation_leave"] if action == "new" and getattr(app, "_hook_conversation_id", None) else [])
    if materialize:
        events.append("conversation_start")
    result = {"version": VERSION, "kind": "conversation_transition_review", **_identity(app),
              "snapshot": current["snapshot"], "review": secrets.token_hex(32), "action_id": str(uuid.uuid4()),
              "action": action, "target_session_id": target, "title": title,
              "effects": {"materialize": bool(materialize),
                          "provider_cleanup": action == "new" and bool(getattr(app.backend, "owns_native_turns", False)),
                          "lifecycle_hooks": any(_matching(hooks, event, source) for event in events)}}
    _bounded(result)
    app._conversation_transition_review = Review(_encoded(result), context, hooks, registry, _protected(app),
        _encoded(app.conversation), backend_catalog._disk_hash(app.convo_path, 8 * 1024 * 1024),
        backend_catalog._disk_hash(app.convo_dir / "settings.json", 1024 * 1024), source)
    return copy.deepcopy(result)


def _receipt(app, cmd, status, *, mutated=False, close_required=False, error=None,
             transcript=False, settings=False, settled=False, hooks=False, cleanup=False):
    identity = _identity(app)
    message = (str(error) or type(error).__name__).encode("utf-8")[:4096].decode("utf-8", "ignore") if error is not None else None
    return _bounded({"version": VERSION, "kind": "conversation_transition_apply", **identity,
        "from_session_id": cmd.get("session_id"),
        **{key: cmd.get(key) for key in ("snapshot", "review", "action_id", "action", "target_session_id", "title")},
        "status": status, "mutated": mutated, "close_required": close_required, "error": message,
        "persistence": {"transcript": transcript, "settings": settings},
        "lifecycle": {"settled": settled, "hooks_completed": hooks, "provider_cleanup_completed": cleanup}})


def _unchanged(app, review, *, target=None):
    context = review.context
    if getattr(app, "_conversation_transition_shutdown_requested", False):
        raise ValueError("Conversation shutdown was requested")
    if (app._agent_session is not context.session or app.store is not context.store or app.backend is not context.backend
            or app.convo_id != (target or context.session_id) or _protected(app) != review.protected
            or _registry(app) != review.registry or _hooks(app) != review.hooks
            or getattr(app, "_pending_input", ()) or getattr(app, "_restored_input_error", False)):
        raise ValueError("Conversation identity, policy, input or lifecycle configuration changed during publication")


async def _joined(awaitable, *, cancel=False):
    """Cancellation retains the real lifecycle owner until its work settles."""
    worker = asyncio.create_task(awaitable)
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        if cancel:
            worker.cancel()
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
        try:
            worker.result()
        except BaseException:  # noqa: BLE001, S110 - preserve original cancellation after settlement
            pass
        raise


async def _settle_hooks(app, review, event, session_id, offset):
    await _joined(hook_host.drain_lifecycle(app), cancel=True)
    expected = _matching(review.hooks, event, review.hook_source)
    records = app._conversation_transition_hook_outcomes[offset:]
    actual = [(row["scope"], row["id"]) for row in records
              if row["event"] == event and row["conversation_id"] == session_id and row["allowed"]]
    if actual != [(hook.scope, hook.id) for hook in expected] or len(records) != len(expected):
        raise ValueError("Conversation lifecycle hook outcome failed, changed or did not settle")
    if getattr(app, "_conversation_transition_cleanup_error", None):
        raise ValueError(app._conversation_transition_cleanup_error)
    if app._hook_lifecycle or app._hook_lock.locked():
        raise ValueError("Conversation lifecycle work did not settle")


def _verify_persistence(app, review, action, title):
    if app.store.pending or not app.store.owned or app.store.persist_error:
        raise ValueError(app.store.persist_error or "Conversation materialization did not acquire durable ownership")
    app.store._validate_owned_transcript()
    meta, messages = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
    if meta.get("id") != app.convo_id or messages != app.conversation:
        raise ValueError("Saved conversation identity or history disagrees with runtime")
    if action == "rename" and (meta.get("name") != title or _encoded(messages) != review.history):
        raise ValueError("Rename did not preserve history and the reviewed title")
    if action == "new" and any(message.get("role") != "system" for message in messages):
        raise ValueError("New conversation acquired unexpected user or provider history")
    path = app.convo_dir / "settings.json"
    digest = backend_catalog._disk_hash(path, 1024 * 1024)
    if digest is None:
        raise ValueError("Conversation settings did not persist")
    if action == "rename" and review.settings is not None and digest != review.settings:
        raise ValueError("Rename unexpectedly changed conversation settings")
    if action == "new" or review.settings is None:
        with path.open("rb") as source:
            payload = json.loads(source.read(1024 * 1024 + 1))
        if any(payload.get(key) != value for key, value in asdict(app._convo_settings).items()):
            raise ValueError("Saved conversation settings disagree with canonical birth settings")


async def _close_native(app):
    from litetui.claude_backend import settle_close

    failures = await settle_close(app)
    if failures:
        app._conversation_transition_cleanup_error = "Outgoing native cleanup failed: " + "; ".join(failures)
        raise ValueError(app._conversation_transition_cleanup_error)
    app._conversation_transition_cleanup_error = None


async def apply(app, cmd):
    """Consume one exact review; any ambiguous effect closes the source gate."""
    review = getattr(app, "_conversation_transition_review", None)
    if not isinstance(review, Review):
        return _receipt(app, cmd, "refused", error="No unconsumed conversation review; review again")
    app._conversation_transition_review = None
    wire = json.loads(review.wire)
    try:
        if any(cmd.get(key) != wire.get(key) for key in ECHO):
            raise ValueError("Apply requires the exact reviewed conversation action")
        current = snapshot(app, cmd)
        if current["snapshot"] != wire["snapshot"] or not current["actions"][wire["action"]]["eligible"]:
            raise ValueError("Conversation action is stale or no longer eligible; review again")
        target, action = wire["target_session_id"], wire["action"]
        valid_id(target)
        if action == "new" and _unlinked(review.context.session.conversation_directory(target)).exists():
            raise ValueError("Reserved conversation already exists; review again")
        _unchanged(app, review)
    except Exception as exc:  # noqa: BLE001 - known pre-effect refusals remain usable
        return _receipt(app, cmd, "refused", error=exc)
    app._conversation_transition_shutdown_identity = {
        "action_id": wire["action_id"], "action": wire["action"],
        "from_session_id": wire["session_id"], "target_session_id": wire["target_session_id"],
        **{key: wire[key] for key in ("agent_id", "backend", "model")}}
    app._conversation_transition_task = asyncio.current_task()
    app._conversation_transition_pending = True
    app._conversation_transition_hook_outcomes = []
    app._conversation_transition_hook_snapshot = review.hooks
    mutated = False
    cleanup = False
    hooks_done = False
    work_proof = None
    try:
        if action == "new":
            from litetui import background_input
            work_proof = await background_input.before_identity_transition(app)
            from litetui.claude_backend import close_native

            mutated = True  # cleanup and lifecycle hooks may have external effects
            bridge = getattr(app.backend, "_claude_tools", None)
            if getattr(app.backend, "owns_native_turns", False):
                app._conversation_transition_cleanup_error = "Outgoing native cleanup has not been verified"
                if bridge:
                    bridge.stop()
                close_native(app, app.backend)
                await _joined(_close_native(app))
            cleanup = True
            _unchanged(app, review)
            if getattr(app, "_hook_conversation_id", None):
                hook_host.leave_conversation(app)
                await _settle_hooks(app, review, "conversation_leave", review.context.session_id, 0)
                _unchanged(app, review)
            if backend_catalog._disk_hash(review.context.store.convo_path, 8 * 1024 * 1024) != review.transcript:
                raise ValueError("Outgoing durable history changed before New")
            app._stage_new_convo(target)
            app.conversation = []
            app._load_system_prompt()
        else:
            cleanup = True
        offset = len(app._conversation_transition_hook_outcomes)
        was_pending = app.store.pending
        mutated = True
        app._materialise_convo()
        if was_pending:
            await _settle_hooks(app, review, "conversation_start", target, offset)
            if getattr(app, "_hook_conversation_id", None) != target:
                raise ValueError("New conversation lifecycle identity did not settle")
        hooks_done = True
        _unchanged(app, review, target=target)
        if action == "rename":
            app.store.write_record({"type": "rename", "name": wire["title"]})
        else:
            if app._bind_task_store() is not True:
                raise ValueError("New conversation task store did not bind cleanly")
        _verify_persistence(app, review, action, wire["title"])
        _unchanged(app, review, target=target)
        if action == "new":
            old_path = review.context.session.conversation_directory(review.context.session_id) / "convo.jsonl"
            if backend_catalog._disk_hash(old_path, 8 * 1024 * 1024) != review.transcript:
                raise ValueError("New conversation changed outgoing durable history")
        app._refresh_ctx_label()
        receipt = _receipt(app, cmd, "renamed" if action == "rename" else "created", mutated=True,
                           transcript=True, settings=True, settled=True, hooks=True, cleanup=True)
        if action == "new":
            receipt.update(background_input.after_identity_transition(app, work_proof))
        return _bounded(receipt)
    except BaseException as exc:  # noqa: BLE001 - cancellation after effects is not rollback
        app._conversation_transition_error = CLOSE_REQUIRED
        return _receipt(app, cmd, "failed", mutated=mutated, close_required=True,
                        error=exc or "Conversation action cancelled", hooks=hooks_done, cleanup=cleanup)
    finally:
        app._conversation_transition_pending = False
        app._conversation_transition_task = None
        app._conversation_transition_hook_outcomes = None
        app._conversation_transition_hook_snapshot = None
