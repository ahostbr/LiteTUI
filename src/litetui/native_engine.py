"""One-shot, reviewed controls for retained NInfer/Strata engine ownership.

Metadata is not readiness or calibrated resource admission. Canonical launchers,
resource gates and terminal shutdown proofs remain the only lifecycle authority.
"""
from __future__ import annotations

import asyncio
import copy
import json
import secrets
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field

from litetui import backend_catalog, backend_transition, engine_review_plan
from litetui.agent_preparation import await_preparation
from litetui.engine_lifecycle import EngineUnavailable

ENGINES = frozenset({"ninfer", "strata"})
MAX_RESPONSE = 1024 * 1024
LEGACY_MUTATIONS = frozenset({
    "gui.engine.start", "gui.engine.stop", "gui.models.reconnect", "gui.models.select",
    "gui.models.load", "gui.models.unload", "gui.models.configure", "gui.context.set",
    "gui.backend.set", "gui.conversations.open", "gui.conversations.create",
    "gui.settings.apply", "set_model", "set_settings", "use_skill", "run_command",
})
RECOVERY = frozenset({"gui.restored_input.cancel", "gui.restored_input.resolve", "gui.restored_input.reconcile"})
READ_ONLY = frozenset({
    "gui.hello", "gui.state", "gui.conversations.read", "gui.conversations.list",
    "gui.conversations.page", "gui.conversations.preview", "gui.restored_input.list",
    "gui.work.get", "gui.work.cancel", "gui.diagnostics.get", "gui.themes.native_snapshot",
    "gui.history.native_snapshot", "gui.history.native_page",
    "gui.work.native_snapshot", "gui.work.native_page", "gui.work.native_tail", "gui.work.native_calendar",
    "gui.work.native_pause_validate", "gui.work.native_pause_apply", "gui.work.native_pause_readback",
    "gui.engine.native_inspect", "gui.engine.native_validate", "gui.engine.native_peer_validate", "gui.engine.native_reconnect_validate",
    "gui.engine.native_action", "gui.engine.native_readback",
    "gui.engine.native_config_snapshot", "gui.engine.native_config_review", "gui.engine.native_config_save", "gui.engine.native_config_readback",
})


@dataclass
class State:
    backend: object
    session_id: str = ""
    desired_model: str = ""
    session: object = None
    agent_id: str = ""
    instance: str = field(default_factory=lambda: secrets.token_hex(32))
    generation: int = 0
    admission: str = "management_only"
    effective_model: str | None = None
    pending: bool = False
    action_task: object = None
    shutdown_requested: bool = False
    cancel_start: threading.Event = field(default_factory=threading.Event)
    peer_refusal: dict | None = None
    peer_attempt: dict | None = None
    fatal: str | None = None
    reviews: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)
    handle: object = None
    handle_token: str | None = None
    connect_worker: object = None
    connect_task: object = None
    recovery_task: object = None
    inspection_task: object = None
    inspection_depth: int = 0


def _task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def state(app):
    return getattr(app, "_native_engine_state", None)


def requested(app):
    return getattr(getattr(app, "_launch_options", None), "engine_management", False) is True


def submission_refusal(app):
    current = state(app)
    if current is None:
        return "Engine management startup has not established prompt readiness" if requested(app) else None
    session = getattr(app, "_agent_session", None)
    try:
        authority = session.authority if session is not None else None
        matches = (authority is not None and session is current.session and authority.agent_id == current.agent_id
                   and app.backend is current.backend and getattr(app, "convo_id", None) == current.session_id
                   and getattr(app, "model_id", None) == current.desired_model and authority is not None
                   and (authority.backend, authority.model) == (current.backend.name, current.desired_model))
    except (ValueError, OSError):
        matches = False
    if not matches:
        return "Engine capability identity changed; close this runtime and reopen explicitly"
    if current.shutdown_requested:
        return "Engine management is shutting down; no new action or prompt is allowed"
    if current.fatal:
        return current.fatal
    if current.pending:
        return "An engine lifecycle action is active; wait for its settled readback"
    if current.admission != "prompt_ready":
        return "The engine is not prompt-ready; review its engine controls before sending"
    return None


def require_open(app):
    current = state(app)
    if current is None:
        return  # ordinary initialization still validates its owned launch separately
    # Synchronous metadata has no execution permission and never crosses an await.
    if current.inspection_depth and current.inspection_task is _task():
        return
    if current.recovery_task is _task() and current.recovery_task is not None:
        return
    if current.connect_task is _task() and current.connect_task is not None:
        return
    if current.connect_worker is not None:
        from textual.worker import NoActiveWorker, get_current_worker
        try:
            if get_current_worker() is current.connect_worker:
                return
        except NoActiveWorker:
            pass
    why = submission_refusal(app)
    if why:
        raise ValueError(why)


def require_execution(app):
    why = submission_refusal(app)
    if why:
        raise ValueError(why)


def operation_allowed(app, operation):
    if state(app) is not None and operation in LEGACY_MUTATIONS:
        raise ValueError("This engine capability requires reviewed native controls; reopen explicitly to change backend or session")
    if operation not in READ_ONLY and operation not in RECOVERY:
        require_execution(app)


def command_refusal(app, command):
    if state(app) is not None and command.strip().lower() not in ("/quit", "/exit", "/q"):
        return "Use the reviewed native controls for this engine session; legacy command handlers are unavailable"
    return None


@contextmanager
def inspection(app):
    current = state(app)
    if current is None:
        yield
        return
    prior = current.inspection_task
    current.inspection_task = _task()
    current.inspection_depth += 1
    try:
        yield
    finally:
        current.inspection_depth -= 1
        current.inspection_task = prior


@contextmanager
def recovery(app):
    current = state(app)
    if current is None:
        yield
        return
    _common(app, {"version": 1, "session_id": current.session_id, "agent_id": current.agent_id,
                  "backend": current.backend.name, "desired_model": current.desired_model,
                  "management_instance": current.instance})
    if current.pending:
        raise ValueError("Wait for engine lifecycle settlement before resolving held input")
    prior = current.recovery_task
    current.recovery_task = _task()
    try:
        yield
    finally:
        current.recovery_task = prior


def note_connection_error(app, error):
    app._native_engine_management_reason = None
    if requested(app) and isinstance(error, EngineUnavailable) and app.backend.name in ENGINES:
        session = getattr(app, "_agent_session", None)
        if session is not None and session.authority.backend == app.backend.name and session.authority.model:
            app._native_engine_management_reason = error.reason
            app._model_id = session.authority.model


def startup_management(app):
    """Only the explicit typed unavailable condition can continue startup review."""
    return (requested(app) and app.backend.name in ENGINES
            and getattr(app, "_native_engine_management_reason", None) in ("engine_unavailable", "engine_unloaded")
            and getattr(app, "_agent_session", None) is not None
            and not getattr(app, "_owned_launch_error", None))


def ready_fields(app):
    if (app.backend.name not in ENGINES or getattr(app, "_agent_session", None) is None
            or getattr(app, "_cli_launch_error", None)):
        return {}
    authority = app._agent_session.authority
    if not authority.model or app.model_id != authority.model or authority.backend != app.backend.name:
        return {}
    managed = startup_management(app)
    if not managed and not getattr(app, "_gui_connection_success", False):
        return {}
    current = State(app.backend, session_id=app.convo_id, desired_model=authority.model, session=app._agent_session, agent_id=authority.agent_id, admission="management_only" if managed else "prompt_ready",
                    effective_model=None if managed else authority.model)
    app._native_engine_state = current
    result = {"admission": current.admission, "management_instance": current.instance,
              "engine_generation": current.generation, "desired_model": authority.model,
              "effective_model": current.effective_model}
    if managed:
        result.update(launch_status="management", management_reason=app._native_engine_management_reason)
    return result


def _error(value):
    return str(value).encode("utf-8", "replace")[:4096].decode("utf-8", "ignore")


def _bounded(value):
    if len(json.dumps(value, allow_nan=False).encode("utf-8")) > MAX_RESPONSE:
        raise ValueError("Engine response exceeds the transport budget")
    return value


def _common(app, cmd):
    current = state(app)
    if current is None or app.backend is not current.backend:
        raise ValueError("This startup has no native engine management capability")
    if type(cmd.get("version")) is not int or cmd["version"] != 1:
        raise ValueError("Native engine controls require version 1")
    with inspection(app):
        context = backend_catalog._context(app, cmd.get("session_id"))
    model = backend_catalog._text(context.authority.model, 1024)
    if current.session is not context.session or current.agent_id != context.authority.agent_id or current.session_id != context.session_id or current.desired_model != model:
        raise ValueError("Engine management capability belongs to the startup selection; reopen explicitly")
    expected = {"version": 1, "session_id": context.session_id, "agent_id": context.authority.agent_id,
                "backend": context.selected, "desired_model": model, "management_instance": current.instance}
    if any(cmd.get(key) != value for key, value in expected.items()):
        raise ValueError("Engine request identity changed; reopen its review")
    if current.shutdown_requested:
        raise ValueError("Engine management is shutting down")
    if current.fatal:
        raise ValueError(current.fatal)
    return current, context, expected


def _ownership(current):
    owned = current.backend._owned
    if current.handle is not owned:
        current.handle = owned
        current.handle_token = secrets.token_hex(32) if owned is not None else None
    return {"kind": "retained_handle" if owned is not None else "unowned",
            "handle_token": current.handle_token,
            "host": str(owned.host) if owned is not None else None,
            "pid": getattr(owned.proc, "pid", None) if owned is not None else None}


def _quiet_reason(app):
    try:
        with inspection(app):
            backend_transition._quiet(app, require_unowned_server=False)
    except (ValueError, OSError) as exc:
        return _error(exc)
    return None


def _binding(app, context):
    return backend_catalog.fingerprint(app, context, {"kind": "native_engine_authority"})


def _inspect(app, cmd):
    from litetui.gui_rpc import _tool_policy

    current, context, common = _common(app, cmd)
    before = _binding(app, context)
    ownership = _ownership(current)
    backend = current.backend
    lifecycle = {"starting": bool(backend._starting), "stopping": bool(backend._stopping),
                 "worker_settled": not current.pending and not backend._starting and not backend._stopping}
    reason = _quiet_reason(app)
    if not lifecycle["worker_settled"]:
        reason = "An engine lifecycle worker has not settled"
    plan = None
    plan_error = None
    try:
        plan = engine_review_plan.inspect(app.settings, common["backend"], common["desired_model"])
    except (ValueError, OSError, RuntimeError) as exc:
        plan_error = _error(exc)
    admission_session = getattr(backend, "_admission_session", None)
    resource_reason = None
    if admission_session is not None and (admission_session.unsettled_loads or admission_session.leases or admission_session.active_loads):
        resource_reason = "Resource admission retains a lease or unresolved reservation; capacity has not been released"
    attached_reason = "This session is already connected to an unowned engine; stop it where it was started" if current.admission == "prompt_ready" and current.handle is None else None
    start_reason = reason or resource_reason or attached_reason or ("Stop the retained engine handle before starting another" if current.handle is not None else None) or plan_error
    stop_reason = reason or ("This runtime does not retain an owned engine handle" if current.handle is None else None)
    result = {"kind": "engine_snapshot", **common, "generation": current.generation,
              "admission": current.admission, "effective_model": current.effective_model,
              "tool_policy": _tool_policy(app), "ownership": ownership, "lifecycle": lifecycle,
              "launch": plan.summary if plan is not None else None,
              "actions": {"start": {"eligible": start_reason is None, "reason": start_reason},
                          "stop": {"eligible": stop_reason is None, "reason": stop_reason}},
              "readiness_checked": False, "resources_checked": False}
    if (requested(app) and current.admission == "management_only" and current.handle is None
            and reason is None and resource_reason is None and lifecycle["worker_settled"]):
        from litetui import native_engine_endpoint
        try:
            result["reconnect"]=native_engine_endpoint.selection(app)
        except (ValueError, RuntimeError, OSError):
            pass  # No currently selected attachable endpoint.
    if (current.peer_refusal and current.peer_refusal["verified"] and not start_reason
            and plan.summary["selection_id"] == current.peer_refusal["selection_id"]):
        from litetui import native_engine_peer
        try:
            result["local_peer_start"] = native_engine_peer.capture(app, current, current.peer_refusal, plan)
        except (OSError, RuntimeError, ValueError):
            # A changed/inaccessible registration cannot authorize a bypass of the warning.
            pass
    result["snapshot"] = backend_catalog.fingerprint(app, context, result)
    if before != _binding(app, context):
        raise ValueError("Engine authority changed during inspection")
    return _bounded(result), plan, context


def inspect(app, cmd):
    return _inspect(app, cmd)[0]


def validate(app, cmd):
    snap, plan, context = _inspect(app, cmd)
    current = state(app)
    action = cmd.get("action")
    from litetui import native_engine_endpoint, native_engine_peer
    peer = None
    reconnect = None
    if cmd.get("type") == native_engine_endpoint.OPERATION:
        reconnect = snap.get("reconnect")
        if action != "reconnect" or reconnect is None or cmd.get("consent_scope") != "one_reconnect":
            raise ValueError("Choose the current configured endpoint for connect-only verification")
    elif cmd.get("type") == native_engine_peer.OPERATION:
        peer = snap.get("local_peer_start")
        if (action != "start" or peer is None or cmd.get("peer_sha256") != peer["sha256"]
                or cmd.get("consent_scope") != native_engine_peer.SCOPE):
            raise ValueError("Second-instance consent requires the exact local seat review")
    elif action not in ("start", "stop") or cmd.get("consent_scope") != "one_" + action:
        raise ValueError("Choose one explicit engine start or stop")
    if type(cmd.get("generation")) is not int or cmd["generation"] != snap["generation"] or cmd.get("snapshot") != snap["snapshot"]:
        raise ValueError("Engine review is stale; inspect again")
    if reconnect is None and not snap["actions"][action]["eligible"]:
        raise ValueError(snap["actions"][action]["reason"])
    selection = reconnect["selection_id"] if reconnect else snap["launch"]["selection_id"] if action == "start" else None
    handle = snap["ownership"]["handle_token"] if action == "stop" else None
    if cmd.get("selection_id") != selection or cmd.get("handle_token") != handle:
        raise ValueError("Engine selection or retained handle changed")
    fields = ("version", "session_id", "agent_id", "backend", "desired_model", "management_instance",
              "snapshot", "generation", "action", "selection_id", "handle_token", "consent_scope")
    request = {key: cmd.get(key) for key in fields}
    result = {"kind": "engine_review", **request, "review": secrets.token_hex(32),
              "action_id": str(uuid.uuid4()),
              "summary": reconnect or (snap["launch"] if action == "start" else snap["ownership"]),
              "timeout_ms": 1200000 if action == "start" else 120000}
    if peer is not None:
        result["local_peer_start"] = peer
        result["peer_sha256"] = peer["sha256"]
    current.reviews.clear()  # only the latest explicit review can be consumed
    current.reviews[result["review"]] = (copy.deepcopy(result), plan, context, _binding(app, context))
    return _bounded(result)


def _shutdown(value):
    if value is None:
        return None
    return {key: getattr(value, key) for key in ("owned", "attempted", "main_exited", "tree", "pid", "retained", "error")}


async def _connect(app, current, desired, *, endpoint=None):
    old_client = app.client
    current.connect_task = _task()
    try:
        worker = app.connect()
        current.connect_worker = worker
        await worker.wait()
        if (not getattr(app, "_connect_settled", False) or not getattr(app, "_gui_connection_success", False)
                or app.backend is not current.backend or app.model_id != desired):
            raise ValueError("Engine connection did not establish the reviewed model")
        history_worker = getattr(app, "_native_history_worker", None)
        if history_worker is not None:
            await history_worker.wait()
        rows = await current.backend.list_models()
        if len(rows) != 1 or rows[0].key != desired or not rows[0].loaded:
            raise ValueError("The engine did not confirm exactly the reviewed loaded model")
        await current.backend.ensure_chat_ready(desired)
        if not await asyncio.to_thread(current.backend._health, endpoint or current.backend._owned.host):
            raise ValueError("The owned engine stopped being ready during connection")
    finally:
        current.connect_worker = None
        current.connect_task = None
        if old_client is not app.client:
            await asyncio.wait_for(old_client.close(), 30)


async def action(app, cmd):
    current, context, common = _common(app, cmd)
    record = current.reviews.get(cmd.get("review"))
    if record is None:
        raise ValueError("Engine action has no unconsumed source review")
    review, plan, reviewed_context, binding = record
    keys = ("version", "session_id", "agent_id", "backend", "desired_model", "management_instance",
            "snapshot", "generation", "action", "selection_id", "handle_token", "consent_scope", "review", "action_id")
    if cmd.get("peer_sha256") != review.get("peer_sha256") or any(cmd.get(key) != review.get(key) for key in keys):
        raise ValueError("Engine action differs from the explicit review")
    if current.pending or current.generation != review["generation"]:
        raise ValueError("Engine action generation was already consumed")
    current.reviews.clear()
    consumed = current.generation
    refused = current.peer_refusal
    current.peer_refusal = None
    current.generation += 1
    result = {"kind": "engine_action", **common, "action_id": review["action_id"], "review": review["review"],
              "action": review["action"], "snapshot": review["snapshot"], "consumed_generation": consumed,
              "generation": current.generation, "status": "refused", "admission": current.admission,
              "effective_model": current.effective_model, "handle_token": _ownership(current)["handle_token"],
              "worker_settled": True, "cancel_requested": False, "spawn_attempted": False,
              "mutated": False, "close_required": False, "error_code": None, "error": None, "shutdown": None}
    if review["action"]=="reconnect":
        result["reconnect"]=review["summary"]
    peer=review.get("local_peer_start")
    if peer is not None:
        result["peer_sha256"]=peer["sha256"]
        result["peer_accepted"]=False
    history = copy.deepcopy(app.conversation)
    from litetui.gui_rpc import _tool_policy
    policy = _tool_policy(app)
    def verify():
        _now, fresh_context, _identity = _common(app, cmd)
        if fresh_context != context:
            raise ValueError("Owned engine context changed during review")
        if (app.backend is not current.backend or context.session is not reviewed_context.session
                or _binding(app, context) != binding):
            raise ValueError("Reviewed engine authority changed before action")
        if review["action"]=="reconnect":
            from litetui import native_engine_endpoint
            if native_engine_endpoint.selection(app)!=review["summary"]:
                raise ValueError("Reviewed engine endpoint changed")
        if plan is not None and review["action"] == "start":
            plan.verify()
    try:
        verify()
        reason = _quiet_reason(app)
        if reason:
            raise ValueError(reason)
        if _ownership(current)["handle_token"] != review["handle_token"]:
            raise ValueError("Reviewed engine ownership changed")
    except (ValueError, OSError, RuntimeError) as exc:
        result.update(error_code="stale_review", error=_error(exc))
        current.receipts[result["action_id"]] = copy.deepcopy(result)
        return _bounded(result)
    current.pending = True
    current.action_task = _task()
    current.cancel_start = threading.Event()
    current.admission = "management_only"
    current.effective_model = None
    app._native_engine_resource_action = True
    app._native_engine_resource_refusal = None
    app._native_engine_resource_sibling = None
    if peer is not None:
        current.peer_attempt={"proof":peer,"refusal":refused,"plan":plan,"used":False}
    try:
        if review["action"]=="reconnect":
            from litetui import native_engine_endpoint
            target=review["summary"]
            with native_engine_endpoint.pinned(target["endpoint"]):
                await native_engine_endpoint.join_probe(native_engine_endpoint.check_served(current.backend,target))
                verify()  # Remain in the exact GUI management owner after the protected read.
                result["mutated"]=True  # The canonical client rebind can now have started.
                await native_engine_endpoint.join_probe(_connect(app,current,common["desired_model"],endpoint=target["endpoint"]))
                verify()
                native_engine_endpoint.verify_connection(app,target)
            if app.conversation!=history or _tool_policy(app)!=policy:
                raise ValueError("Connect-only action changed the reviewed conversation or policy")
            current.admission="prompt_ready"
            current.effective_model=common["desired_model"]
            result["status"]="connected"
        elif review["action"] == "start":
            def notice():
                result["spawn_attempted"] = True
            def admitted_start():
                verify()
                if peer is not None:
                    from litetui import native_engine_peer
                    native_engine_peer.verify_admitted(app, current)
                # From this point the canonical launcher may append logs or
                # materialize a reviewed config before any spawn is attempted.
                result["mutated"] = True
            await current.backend.start_engine(notice=notice, reviewed_plan=plan, reviewed_check=admitted_start, cancelled=current.cancel_start.is_set)
            verify()
            await _connect(app, current, common["desired_model"])
            verify()
            if app.conversation != history or _tool_policy(app) != policy:
                raise ValueError("Engine connection changed reviewed history or tool policy")
            current.admission = "prompt_ready"
            current.effective_model = common["desired_model"]
            result["status"] = "started"
        else:
            if not current.backend.begin_stop():
                raise ValueError("The engine lifecycle could not claim this stop")
            try:
                def stop():
                    proof = current.backend.shutdown_owned()
                    result["shutdown"] = _shutdown(proof)
                    return proof
                result["mutated"] = True
                proof = await await_preparation(stop)
            finally:
                current.backend.end_stop()
            result["status"] = "retained" if proof.retained else "stopped"
            app._gui_connection_success = False
    except asyncio.CancelledError:
        result.update(status="cancelled", cancel_requested=True, error_code="cancelled",
                      error="Engine action cancellation joined its lifecycle worker; inspect retained ownership")
    except Exception as exc:  # noqa: BLE001 - retain lifecycle facts before reporting an action failure
        code = getattr(app, "_native_engine_resource_refusal", None)
        result.update(status="retained" if current.backend._owned is not None else "refused",
                      error_code=code or "lifecycle_refused", error=_error(exc))
        if result["spawn_attempted"] and current.backend._owned is None and result["shutdown"] is None:
            # Canonical EngineStartFailed carries a terminal cleanup proof.
            proof = getattr(exc, "shutdown", None)
            if proof is None and not hasattr(exc, "retained_owned"):
                result.update(status="failed", close_required=True)
    finally:
        app._native_engine_resource_action = False
        if current.peer_attempt is not None:
            result["peer_accepted"]=current.peer_attempt["used"]
            current.peer_attempt=None
        if (result["error_code"]=="resource_confirmation_required" and not result["mutated"]
                and not result["spawn_attempted"] and current.backend._owned is None
                and isinstance(app._native_engine_resource_sibling,str)):
            current.peer_refusal={"action_id":review["action_id"],"generation":current.generation,
                                  "selection_id":review["selection_id"],"sibling":app._native_engine_resource_sibling,"verified":False}
        current.pending = False
        owned = _ownership(current)
        settled = not current.backend._starting and not current.backend._stopping
        if result["status"] not in ("started","connected"):
            current.admission = "retained_recovery" if current.handle is not None else "management_only"
            current.effective_model = None
        if not settled:
            result.update(status="failed", close_required=True, error_code="unsettled_worker")
        if result["close_required"]:
            current.fatal = "Engine lifecycle outcome is uncertain; close this runtime and reopen explicitly"
        result.update(admission=current.admission, effective_model=current.effective_model,
                      handle_token=owned["handle_token"], worker_settled=settled)
        current.receipts.clear()
        current.receipts[result["action_id"]] = copy.deepcopy(result)
    return _bounded(result)


async def readback(app, cmd):
    current, context, common = _common(app, cmd)
    receipt = current.receipts.get(cmd.get("action_id"))
    if receipt is None or cmd.get("review") != receipt["review"] or type(cmd.get("generation")) is not int or cmd["generation"] != current.generation or receipt["generation"] != current.generation:
        raise ValueError("Engine readback does not match the last consumed action")
    before = _binding(app, context)
    settled = not current.pending and not current.backend._starting and not current.backend._stopping
    connected = current.admission == "prompt_ready" and settled
    try:
        if connected and receipt["action"]=="reconnect":
            from litetui import native_engine_endpoint
            target=receipt["reconnect"]
            with native_engine_endpoint.pinned(target["endpoint"]):
                native_engine_endpoint.verify_connection(app,target)
                await native_engine_endpoint.check_served(current.backend,target)
                native_engine_endpoint.verify_connection(app,target)
        elif connected:
            owned = current.backend._owned
            host = owned.host if owned is not None else current.backend.host()
            if not await asyncio.to_thread(current.backend._health, host):
                raise ValueError("Owned engine health changed before independent readback")
            rows = await current.backend.list_models()
            if len(rows) != 1 or rows[0].key != common["desired_model"] or not rows[0].loaded:
                raise ValueError("Owned engine loaded model changed before independent readback")
        _now, after, _common_after = _common(app, cmd)
        if after != context or before != _binding(app, after):
            raise ValueError("Engine authority changed during independent readback")
    except BaseException:
        current.admission = "retained_recovery" if current.backend._owned is not None else "management_only"
        current.effective_model = None
        current.fatal = "Engine readback did not settle; close this runtime and reopen explicitly"
        app._gui_connection_success = False
        raise
    result = {"kind": "engine_readback", **common, "action_id": receipt["action_id"], "review": receipt["review"],
              "generation": current.generation, "admission": current.admission,
              "effective_model": current.effective_model, "handle_token": _ownership(current)["handle_token"],
              "worker_settled": settled, "connected": connected,
              "loaded_model": common["desired_model"] if connected else None, "shutdown": receipt["shutdown"]}
    if "reconnect" in receipt:
        result["reconnect"]=receipt["reconnect"]
    if "peer_sha256" in receipt:
        result.update(peer_sha256=receipt["peer_sha256"],peer_accepted=receipt["peer_accepted"])
    if current.peer_refusal and current.peer_refusal["action_id"] == receipt["action_id"]:
        current.peer_refusal["verified"]=True
    return _bounded(result)
