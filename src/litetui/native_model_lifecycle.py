"""Reviewed model operations at the existing endpoint, with owned target adoption."""

from __future__ import annotations

import asyncio
import copy
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urlsplit

from litetui import (
    backend_catalog,
    backend_transition,
    convo_settings,
    native_context,
)
from litetui.native_engine_endpoint import join_probe
from litetui.shutdown_wait import join_task

PREFIX = "gui.models.native_lifecycle_"
ACTIONS = ("load", "unload", "reconnect")
CLOSE_REQUIRED = "Model operation is uncertain. Close this runtime and reopen to inspect it; nothing was replayed."


@dataclass
class Review:
    token: str
    snapshot: dict
    backend: object
    settings: object
    session: object
    conversation: object
    history: bytes
    stored: dict
    action: str
    bridge: object = None
    transition: dict | None = None
    adopted: str | None = None
    invocation: tuple | None = None
    ownership: tuple | None = None
    connection: object = None
    children: list = field(default_factory=list)
    applied: str | None = None
    after: str | None = None
    current: dict | None = None
    apply_task: object = None
    apply_command: bytes | None = None
    apply_result: object = None
    apply_raw: bytes | None = None
    apply_published: bool = False
    readback_task: object = None
    readback_command: bytes | None = None
    readback_result: object = None
    readback_raw: bytes | None = None
    publication: Publication | None = None


def _encoded(value):
    return backend_catalog._encoded(value, 1024 * 1024)


def _ownership(backend):
    return (
        getattr(backend, "_attached_host", None),
        getattr(backend, "_attached_owner", None),
        getattr(backend, "_connect_only", None),
    )


def _revision(app, context, result):
    return backend_catalog.fingerprint(
        app,
        context,
        {"state": result, "ownership": _ownership(context.backend)},
        include_pending=False,
    )


def _endpoint(backend):
    value = backend.base_url().rstrip("/")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or len(value) > 2048
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("The current endpoint cannot be safely reviewed")
    return value


def _connection(app, backend, endpoint):
    binding = getattr(app, "_client_binding", None)
    return bool(
        binding is not None
        and binding.client is app.client
        and binding.backend_type is type(backend)
        and binding.endpoint == endpoint
        and str(app.client.base_url).rstrip("/") == endpoint
        and getattr(app, "_connect_settled", False)
        and getattr(app, "_gui_connection_success", False)
    )


def _quiet(app):
    backend_transition._quiet(app, require_unowned_server=False)


def _fatal(app):
    app._backend_transition_error = CLOSE_REQUIRED
    app._owned_launch_error = CLOSE_REQUIRED
    app._cli_launch_error = CLOSE_REQUIRED
    from litetui import native_runtime_controls

    native_runtime_controls.signal_stop(app)


def _release(app):
    from litetui import native_runtime_controls

    app._backend_transition_inflight = False
    app._native_model_lifecycle_hold = False
    native_runtime_controls.signal_stop(app)
    if getattr(app, "_pending_input", ()) and not getattr(app, "_gui_quitting", False):
        app.call_later(app._flush_pending_input)


def _same(app, r):
    if (
        app.backend is not r.backend
        or app.settings is not r.settings
        or app._agent_session is not r.session
        or app.conversation is not r.conversation
        or _encoded(app.conversation) != r.history
        or _ownership(app.backend) != r.ownership
        or app.model_id != (r.adopted or r.snapshot["model"])
        or _endpoint(app.backend) != r.snapshot["endpoint"]
        or getattr(app.backend, "_owned", None) is not None
        or getattr(app, "_custom_server_process", None) is not None
    ):
        raise ValueError(
            "The reviewed runtime, model, endpoint or conversation changed"
        )
    app._validate_owned_identity()
    if r.action == "unload" and r.invocation != (
        getattr(app, "_cli_initial_model", None), getattr(app, "_user_model_choice", None)
    ):
        raise ValueError("Unload changed the selected invocation")


CHOICE_FIELDS = ("target", "context_mode", "context_value")


def _choice(app, cmd, r):
    source = r.snapshot if r is not None else cmd
    target = source.get("target", app.model_id)
    mode = source.get("context_mode", "inherit")
    value = source.get("context_value")
    if (type(target) is not str or not target or len(target.encode("utf-8")) > 1024
            or any(ord(c) < 32 for c in target)
            or mode not in ("inherit", "default", "value")
            or (mode == "value" and (type(value) is not int or not 1 <= value <= 2147483647))
            or (mode != "value" and value is not None)):
        raise ValueError("Invalid model target or one-use context choice")
    inherited = app.settings.default_context_length
    if inherited is not None and (type(inherited) is not int or not 1 <= inherited <= 2147483647):
        raise ValueError("Invalid inherited load context")
    want = inherited if mode == "inherit" else value
    return target, mode, value, inherited, want


def _binding(app, cmd, op, fields, r=None):
    if r is None or r.transition is None:
        return native_context.read_binding(app, cmd, PREFIX + op, *fields)
    # Only this retained operation may use its exact historical binding. There
    # is no general exemption for a retired Work epoch or an arbitrary read.
    from litetui import background_input as work
    binding = {k: r.snapshot[k] for k in
               ("session_id", "agent_id", "backend", "model", "work_instance")}
    work._keys(cmd, ("type", "id", "version", *binding, *fields))
    _same(app, r)
    if (op not in ("apply", "readback") or cmd["type"] != PREFIX + op
            or type(cmd["version"]) is not int or cmd["version"] != 1
            or any(cmd[k] != v for k, v in binding.items())
            or cmd.get("token") != r.token
            or op == "readback" and cmd.get("apply_id") != r.applied
            or work.bridge(app) is not None or not r.bridge.closing
            or r.bridge.owner[0] is not r.session or r.bridge.owner[1] is not app.store
            or r.bridge.instance != binding["work_instance"]
            or r.bridge.identity != {k: binding[k] for k in work.identity(app)}
            or r.bridge.transition_proof != r.transition["work_shutdown"]
            or r.bridge.shutdown_task is None or not r.bridge.shutdown_task.done()
            or getattr(app, "_native_work_recapture_required", None) != {
                "previous_instance": r.bridge.instance, "identity": work.identity(app)}):
        raise ValueError("Model readback lost its exact retired operation owner")
    work.text(cmd["id"])
    return r.bridge, binding


def track_connection_worker(app, worker):
    """Only the current canonical connect worker can register its context child."""
    r = getattr(app, "_native_model_lifecycle_connect", None)
    if not isinstance(r, Review):
        return False
    from textual.worker import get_current_worker

    if get_current_worker() is not r.connection:
        raise ValueError("Model connection child belongs to another worker")
    r.children.append(worker)
    return True


async def connection_read(app, awaitable, *, context=False):
    """Join a canonical connection's thread-backed reads even when Quit cancels its worker."""
    r = getattr(app, "_native_model_lifecycle_connect", None)
    if not isinstance(r, Review):
        return await awaitable
    from textual.worker import get_current_worker

    worker = get_current_worker()
    expected = r.children if context else (r.connection,)
    if worker not in expected:
        awaitable.close()
        raise ValueError("Model connection read belongs to another worker")
    return await join_probe(awaitable)


def _safe_start(app, backend):
    options = getattr(app, "_launch_options", None)
    if (
        not getattr(app, "_launch_prepared", False)
        or options is None
        or options.server_mode != "connect"
        or getattr(app, "_startup_history_path", None) is not None
    ):
        return "Reconnect requires an already prepared connect-only launch."
    if (
        getattr(backend, "_owned", None) is not None
        or getattr(app, "_custom_server_process", None) is not None
    ):
        return "This runtime owns a server process; use its explicit engine lifecycle controls."
    selected = getattr(app, "_user_model_choice", None) or getattr(
        app, "_cli_initial_model", None
    )
    if selected is not None and selected != app.model_id:
        return "Reconnect would restore another model; resolve the current invocation selection first."
    if (
        app.settings.pin_default_model
        and app.settings.default_model != app.model_id
        and not (
            getattr(app, "_user_model_choice", None)
            or getattr(app, "_cli_initial_model", None)
        )
    ):
        return "A pinned default would change the selected model; resolve it before reconnecting."
    if backend.name == "llamacpp" and (
        not backend._connect_only
        or backend.host().rstrip("/") != backend._host.rstrip("/")
    ):
        return "llama.cpp must be pinned to its current connect-only endpoint."
    return None


async def _inspect(app, cmd, op, fields, r=None):
    bridge, binding = _binding(app, cmd, op, fields, r)
    target, mode, value, inherited, want = _choice(app, cmd, r)
    context = backend_catalog._context(app, app.convo_id)
    guard = backend_catalog.fingerprint(app, context, {}, include_pending=False)
    backend = context.backend
    supported = backend.name in {"custom", "lmstudio", "llamacpp"}
    endpoint = _endpoint(backend) if supported else None
    reason = (
        _safe_start(app, backend)
        if supported
        else "This provider owns its model lifecycle; use its provider or F12 engine controls."
    )
    info, rows = None, []
    if supported:

        async def inspect():
            return await backend.list_models(), await backend.model_info(target)

        rows, info = await join_probe(inspect())
        if backend.name == "llamacpp":
            served = await join_probe(asyncio.to_thread(backend._server_models))
        else:
            served = None
    if r is None or r.transition is None:
        bridge.check()
    _binding(app, cmd, op, fields, r)
    after = backend_catalog._context(app, app.convo_id)
    if any(
        getattr(context, key) is not getattr(after, key)
        for key in ("session", "store", "backend", "settings")
    ):
        raise ValueError("Model inspection changed runtime ownership")
    if backend_catalog.fingerprint(app, after, {}, include_pending=False) != guard:
        raise ValueError("Runtime or saved settings changed during model inspection")
    if len(rows) > 4096:
        raise ValueError("Model listing exceeds the lifecycle inspection budget")
    if supported and getattr(backend, "_settings", None) is not app.settings:
        raise ValueError("Backend settings differ from the current runtime")
    if supported and _endpoint(backend) != endpoint:
        raise ValueError("Model inspection changed endpoint")
    names = [row.key for row in rows]
    if (len(set(names)) != len(names) or any(type(key) is not str or not key
            or len(key.encode("utf-8")) > 1024 or any(ord(c) < 32 for c in key) for key in names)):
        raise ValueError("Model listing contains duplicate or invalid targets")
    selected = [row for row in rows if row.key == target]
    advertised = len(selected) == 1 and (
        backend.name != "llamacpp" or target in served
    )
    loaded, capacity = None, None
    if info is not None:
        if (
            not isinstance(info, (list, tuple))
            or len(info) != 3
            or type(info[2]) is not bool
            or (
                info[0] is not None
                and (type(info[0]) is not int or not 1 <= info[0] <= 2147483647)
            )
        ):
            raise ValueError("Model metadata is incomplete")
        capacity = info[0]
        if backend.name != "custom":
            loaded = info[2]
            if advertised and selected[0].loaded is not loaded:
                raise ValueError("Model residency changed during inspection")
    load = app.settings.llama_load_settings.get(target, {})
    if not isinstance(load, dict) or len(load) > 128:
        raise ValueError("Model load preferences exceed the inspection budget")
    ctx = load.get("ctx")
    if ctx is not None and (type(ctx) is not int or not 1 <= ctx <= 2147483647):
        raise ValueError("Invalid configured context length")
    if not advertised and reason is None:
        reason = "The selected model is not uniquely advertised by this endpoint."
    control_reason = reason
    if backend.name == "custom":
        control_reason = "Custom servers have no standard model load or unload API."
    elif (
        backend.name == "llamacpp"
        and control_reason is None
        and (
            not backend.attached
            or backend._attached_owner is None
            or backend.single_model
        )
    ):
        control_reason = (
            "This server has no cooperating router owner; use the server that owns it."
        )
    actions = {}
    for action in ACTIONS:
        why = reason if action == "reconnect" else control_reason
        if action == "load" and why is None:
            if loaded is not False:
                why = "Load requires a model explicitly reported as not loaded."
            elif backend.name == "llamacpp" and want is not None:
                why = "This cooperating router owns its presets; a context override requires its owner to reload the preset."
            elif backend.name == "lmstudio" and any(
                k != "ctx" and v is not None for k, v in load.items()
            ):
                why = "LM Studio can apply only context length; review other load settings in its own UI."
        if action == "reconnect" and target != app.model_id:
            why = "Reconnect applies only to the selected model."
        if action == "unload" and why is None and loaded is not True:
            why = "Unload requires a model explicitly reported as loaded."
        actions[action] = {"eligible": why is None, "reason": why}
    result = {
        "version": 1,
        "kind": "model_lifecycle",
        **binding,
        "endpoint": endpoint,
        "lifetime": "external" if supported and reason is None else "unmanaged",
        "advertised": advertised,
        "reported_loaded": loaded,
        "context_length": capacity,
        "context_source": "client_declared"
        if backend.name == "custom"
        else "backend_reported",
        "target": target,
        "selected_model": app.model_id,
        "models": names,
        "context_mode": mode,
        "context_value": value,
        "inherited_context": inherited,
        "preset_context": ctx,
        "load_context": want,
        "other_load_fields": sum(k != "ctx" for k in load),
        "connected": _connection(app, backend, endpoint) if supported else False,
        "actions": actions,
    }
    result["revision"] = _revision(app, after, result)
    return bridge, result


def _receipt(r, kind):
    return {
        "version": 1,
        "kind": kind,
        **{
            k: r.snapshot[k]
            for k in ("session_id", "agent_id", "backend", "model", "work_instance")
        },
        "token": r.token,
        "action": r.action,
        "before": r.snapshot["revision"],
        "after": r.after,
        "apply_id": r.applied,
        "target": r.snapshot["target"],
        **(r.transition or {}),
    }


@dataclass
class RetainedClient:
    client: object
    task: asyncio.Task | None = None


def _retain_client(app, client):
    clients = getattr(app, "_native_model_lifecycle_clients", None)
    if clients is None:
        clients = app._native_model_lifecycle_clients = []
    retained = next((item for item in clients if item.client is client), None)
    if retained is None:
        retained = RetainedClient(client)
        clients.append(retained)
    return retained


async def _close_client(app, retained):
    # One owner per close attempt. Caller cancellation cannot abandon it; a
    # failed attempt leaves the exact client available for shutdown to retry.
    task = retained.task
    if (
        task is None
        or task.done()
        and (
            task.cancelled()
            or task.exception() is not None
            or not retained.client.is_closed()
        )
    ):
        task = retained.task = asyncio.create_task(retained.client.close())
    await join_task(task)
    if task.cancelled() or not retained.client.is_closed():
        raise RuntimeError("Replaced model client cleanup remains unproven")
    clients = app._native_model_lifecycle_clients
    clients[:] = [item for item in clients if item is not retained]


async def settle_before_shutdown(app):
    """Join the admitted action, then retry only its retained client cleanup."""
    app._gui_quitting = True
    action = getattr(app, "_native_model_lifecycle_action", None)
    if action is not None:
        if action is asyncio.current_task():
            raise RuntimeError("A model lifecycle action cannot join itself")
        if not action.done():
            action.cancel()
        try:
            await join_task(action)
        except Exception as exc:  # noqa: BLE001 - effects remain fatal; cleanup still owns its handles
            from litetui import runtime_log

            runtime_log.record_error(
                "model_lifecycle_failed_during_shutdown", detail=type(exc).__name__
            )
    for retained in tuple(getattr(app, "_native_model_lifecycle_clients", ())):
        await _close_client(app, retained)


async def _connect(app, r):
    old_client = app.client
    app._native_model_lifecycle_connect = r
    try:
        r.connection = app.connect()
        await r.connection.wait()
        for child in r.children:
            await child.wait()
        _same(app, r)
        if not _connection(app, app.backend, r.snapshot["endpoint"]):
            raise ValueError(
                "Canonical connect did not settle the reviewed client/backend binding"
            )
        if app.available_models.count(app.model_id) != 1:
            raise ValueError("Canonical connect did not retain the requested model")
    finally:
        # Retain before any await, including a context-child join that may fail.
        retained = (
            _retain_client(app, old_client) if old_client is not app.client else None
        )
        try:
            for child in r.children:
                if not child.is_finished:
                    await child.wait()
        finally:
            app._native_model_lifecycle_connect = None
            if retained is not None:
                await _close_client(app, retained)


async def dispatch(app, cmd):
    op = cmd["type"].removeprefix(PREFIX)
    fields = {
        "snapshot": (),
        "review": ("revision", "action"),
        "apply": ("token",),
        "readback": ("token", "apply_id"),
    }
    if op not in fields:
        raise ValueError("Unknown model lifecycle operation")
    r = getattr(app, "_native_model_lifecycle_review", None)
    if op in ("snapshot", "review") and any(k in cmd for k in CHOICE_FIELDS):
        fields[op] = (*fields[op], *CHOICE_FIELDS)
    bridge, _ = _binding(app, cmd, op, fields[op], r if op in ("apply", "readback") else None)
    if r is not None and r.applied is not None and op != "readback":
        raise ValueError(
            "Await the exact model operation readback before another action"
        )
    if op in ("apply", "readback") and (
        not isinstance(r, Review)
        or r.token != cmd["token"]
        or r.snapshot["work_instance"] != bridge.instance
    ):
        raise ValueError("Review the current model operation again")
    if op == "readback" and (not isinstance(r, Review) or not r.apply_published or r.readback_result is not None):
        raise ValueError(
            "Await the exact published model operation before reading it back"
        )
    app._native_model_lifecycle_review = None
    try:
        _, snap = await _inspect(app, cmd, op, fields[op], r if op in ("apply", "readback") else None)
        if op == "readback":
            assert isinstance(r, Review)  # The correlated phase guard above proved this.
            _same(app, r)
            if (
                r.applied != cmd["apply_id"]
                or r.applied is None
                or r.after != snap["revision"]
            ):
                raise ValueError("Model state changed after the operation")
            result = {**_receipt(r, "model_lifecycle_settled"), "state": snap}
            r.readback_task, r.readback_command = asyncio.current_task(), _encoded(cmd)
            r.readback_result, r.readback_raw = result, _encoded(result)
            app._native_model_lifecycle_review = r
            return result
        _quiet(app)
        if op == "snapshot":
            return snap
        if op == "review":
            action = cmd["action"]
            if action not in ACTIONS or snap["revision"] != cmd["revision"]:
                raise ValueError(
                    "Refresh the current model before reviewing an operation"
                )
            if not snap["actions"][action]["eligible"]:
                raise ValueError(snap["actions"][action]["reason"])
            r = Review(
                secrets.token_hex(32),
                snap,
                app.backend,
                app.settings,
                app._agent_session,
                app.conversation,
                _encoded(app.conversation),
                asdict(convo_settings.load(app.convo_dir)),
                action,
            )
            r.bridge = bridge
            r.invocation = (getattr(app, "_cli_initial_model", None), getattr(app, "_user_model_choice", None))
            r.ownership = _ownership(app.backend)
            app._native_model_lifecycle_review = r
            return _receipt(r, "model_lifecycle_review")
        assert isinstance(r, Review)  # Snapshot and Review returned; only Apply remains.
        _same(app, r)
        if r.snapshot != snap or not snap["actions"][r.action]["eligible"]:
            raise ValueError("Model state changed after review")
        app._backend_transition_inflight = True
        app._native_model_lifecycle_hold = True
        app._native_model_lifecycle_action = asyncio.current_task()
        effect_completed = False
        try:
            if app.backend.name == "llamacpp":
                # Revalidate the source-managed cooperating owner at the pinned endpoint.
                # Connect-only authority prevents discovery, restart or preset rebuild.
                await join_probe(app.backend.ensure_running())
                _same(app, r)
                _, refreshed = await _inspect(app, cmd, op, fields[op], r)
                if refreshed != snap:
                    raise ValueError(
                        "Router ownership or model state changed before the operation"
                    )
            if r.action != "reconnect":
                kwargs = (
                    {"ctx": snap["load_context"]}
                    if r.action == "load" and app.backend.name == "lmstudio"
                    else {}
                )
                await join_probe(getattr(app.backend, r.action)(snap["target"], **kwargs))
                effect_completed = True
                _same(app, r)
            if r.action == "load" and snap["target"] != app.model_id:
                from litetui import background_input as work
                from litetui.plugins.model_switch import adopt_loaded_model
                # The completed load owns admission. Remove only our own hold
                # until before_identity_transition synchronously begins close;
                # every other quiescence/ownership check remains in force.
                app._native_model_lifecycle_hold = False
                try:
                    proof = await work.before_identity_transition(app)
                finally:
                    app._native_model_lifecycle_hold = True
                if work.bridge(app) is not r.bridge or work.identity(app) != r.bridge.identity:
                    raise ValueError("Loaded target lost its original Work owner")
                app._work_model_owner = (r.bridge, work._task())
                try:
                    adopt_loaded_model(app, snap["target"], ctx=snap["load_context"], refresh=False)
                    r.adopted = snap["target"]
                    r.transition = work.after_identity_transition(app, proof)
                finally:
                    app._work_model_owner = None
            # Unloading another target preserves selection, invocation and the
            # selected client's context. Only the target's residency changes.
            if r.action != "unload" or snap["target"] == app.model_id:
                await join_probe(_connect(app, r))
            _, current = await _inspect(app, cmd, op, fields[op], r)
            _same(app, r)
            expected = copy.deepcopy(r.stored)
            if r.adopted is not None:
                expected["model"] = r.adopted
            if r.action == "load" and app.backend.name == "lmstudio":
                expected["lmstudio_load"] = (
                    {"ctx": snap["load_context"]} if snap["load_context"] else {}
                )
            if asdict(convo_settings.load(app.convo_dir)) != expected:
                raise ValueError(
                    "Canonical model load settings did not settle in this conversation"
                )
            if r.action != "reconnect" and current["reported_loaded"] is not (
                r.action == "load"
            ):
                raise ValueError(
                    "Backend did not independently confirm the model operation"
                )
            if (
                r.action == "load"
                and app.backend.name == "lmstudio"
                and snap["load_context"] is not None
                and current["context_length"] != snap["load_context"]
            ):
                raise ValueError(
                    "Loaded context differs from the reviewed load preference"
                )
            if not current["connected"]:
                raise ValueError("Canonical connection is not ready")
            r.applied, r.after, r.current = cmd["id"], current["revision"], current
            r.apply_task, r.apply_command = asyncio.current_task(), _encoded(cmd)
            result = _receipt(r, "model_lifecycle_applied")
            r.apply_result, r.apply_raw = result, _encoded(result)
            app._native_model_lifecycle_review = r
            return result
        except BaseException as exc:
            from litetui.llm_backend import VramRefused

            if isinstance(exc, VramRefused) and r.action == "load" and not effect_completed:
                # Only the canonical load guard proves no effect. A similarly
                # typed later failure cannot release already changed residency.
                _release(app)
                raise
            _fatal(app)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise ValueError(CLOSE_REQUIRED) from exc
        finally:
            app._native_model_lifecycle_action = None
    except BaseException:
        if op == "readback" and isinstance(r, Review) and r.applied is not None:
            _fatal(app)
        raise


@dataclass
class Publication:
    app: Any
    review: Review
    task: object
    result: object
    raw: bytes
    phase: str
    used: bool = False


def readback_allowed(app, cmd):
    r = getattr(app, "_native_model_lifecycle_review", None)
    if not isinstance(r, Review) or cmd.get("type") != PREFIX + "readback":
        return False
    if (
        not r.apply_published
        or r.readback_result is not None
        or cmd.get("token") != r.token
        or cmd.get("apply_id") != r.applied
    ):
        return False
    _binding(app, cmd, "readback", ("token", "apply_id"), r)
    _same(app, r)
    return True


def prepare_publication(app, cmd, result):
    op = cmd.get("type")
    if op not in {PREFIX + "apply", PREFIX + "readback"}:
        return None
    r = getattr(app, "_native_model_lifecycle_review", None)
    phase = "apply" if op.endswith("_apply") else "readback"
    try:
        if (
            not isinstance(r, Review)
            or r.publication is not None
            or r.current is None
            or asyncio.current_task() is not getattr(r, phase + "_task")
            or _encoded(cmd) != getattr(r, phase + "_command")
            or result is not getattr(r, phase + "_result")
            or _encoded(result) != getattr(r, phase + "_raw")
            or phase == "apply"
            and r.apply_published
        ):
            raise ValueError(
                "Model publication is foreign, changed or already consumed"
            )
        _binding(app, cmd, phase,
                 {"apply": ("token",), "readback": ("token", "apply_id")}[phase], r)
        _same(app, r)
        context = backend_catalog._context(app, app.convo_id)
        observed = {k: v for k, v in r.current.items() if k != "revision"}
        if (
            not _connection(app, app.backend, r.snapshot["endpoint"])
            or _revision(app, context, observed) != r.after
        ):
            raise ValueError("Model operation changed before its result publication")
        ticket = Publication(
            app, r, asyncio.current_task(), result, _encoded(result), phase
        )
        r.publication = ticket
        return ticket
    except Exception:
        _fatal(app)
        raise


def finish_publication(app: Any, ticket: Publication):
    r = ticket.review
    if (
        ticket.app is not app
        or ticket.used
        or ticket.task is not asyncio.current_task()
        or r.publication is not ticket
        or _encoded(ticket.result) != ticket.raw
        or getattr(app, "_native_model_lifecycle_review", None) is not r
    ):
        _fatal(app)
        raise ValueError("Model publication lost its exact one-use owner")
    ticket.used = True
    r.publication = None
    if ticket.phase == "apply":
        r.apply_published = True
        return
    app._native_model_lifecycle_review = None
    if getattr(app, "_gui_quitting", False):
        return
    _release(app)
