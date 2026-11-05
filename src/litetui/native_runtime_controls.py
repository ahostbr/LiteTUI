"""Owned live runtime policy, exact publication and bounded authorization scopes."""
from __future__ import annotations

import asyncio
import copy
import secrets
import uuid
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from functools import wraps
from typing import Any

from litetui import background_input as work
from litetui import seat_authority, settings_runtime, tool_policy

VERSION = 1
PREFIX = "gui.runtime.native_"
OPERATIONS = tuple(PREFIX + name for name in ("snapshot", "validate", "apply", "readback"))
COMMON = ("version", "runtime_controls_version", "session_id", "agent_id", "backend", "model", "work_instance")
LIMITS = {"authorizations": 128, "waiters": 128}
MAX_BODY = 262144
MAX_GENERATION = 9007199254740991
CLOSE_REQUIRED = "Runtime control publication is uncertain; close without replay"
_SCOPE: ContextVar[Any] = ContextVar("runtime_control_authorization", default=None)
_LIFECYCLE_HOOK: ContextVar[Any] = ContextVar("reviewed_lifecycle_hook", default=None)
_APPLY: ContextVar[Any] = ContextVar("runtime_control_apply", default=None)


@dataclass(eq=False)
class Review:
    app: Any
    state: Any
    wire: bytes
    references: tuple
    context: bytes
    system_before: Any
    system_after: Any
    retired: bool = False
    consumed: bool = False


@dataclass(eq=False)
class Attempt:
    review: Review
    operation: str
    command: bytes
    task: Any
    effect_attempted: bool = False
    result: Any = None
    result_raw: bytes | None = None
    result_published: bool = False
    readback: Any = None
    readback_raw: bytes | None = None
    readback_command: bytes | None = None
    readback_operation: str | None = None
    readback_task: Any = None
    readback_published: bool = False
    verified: bool = False
    publication: Any = None
    persistence: Any = None
    documents_before: Any = None
    documents_after: Any = None
    historical: bool = False
    closed: bool = False


@dataclass(eq=False)
class Authorization:
    state: Any
    task: Any
    origin: Any
    generation: int
    profile: str
    tools_enabled: bool
    disabled: frozenset
    context: dict
    route: str
    children: set = field(default_factory=set)
    references: int = 1
    done: Any = field(default_factory=asyncio.Event)


@dataclass(eq=False)
class Publication:
    app: Any
    attempt: Attempt
    phase: str
    task: Any
    command: bytes
    result: dict
    raw: bytes
    used: bool = False


@dataclass(eq=False)
class State:
    app: Any
    bridge: work.Bridge
    generation: int
    stable: dict
    references: tuple
    snapshot: Any = None
    snapshot_context: bytes | None = None
    review: Review | None = None
    attempt: Attempt | None = None
    tickets: list = field(default_factory=list)
    waiters: dict = field(default_factory=dict)
    terminal: set = field(default_factory=set)
    changed: Any = field(default_factory=asyncio.Event)
    closing: bool = False
    error: str | None = None
    shutdown: Any = None


def _state(app):
    state = getattr(app, "_native_runtime_controls", None)
    return state if isinstance(state, State) else None


def enabled(app):
    state = _state(app)
    return (state is not None and work.bridge(app) is state.bridge and state.bridge.active
            and getattr(state.bridge, "runtime_controls_version", None) == VERSION)


def _base(state):
    return {"version": VERSION, "runtime_controls_version": VERSION,
            **state.bridge.identity, "work_instance": state.bridge.instance}


def _bounded(value):
    if len(work.encoded(value)) > MAX_BODY:
        raise ValueError("Runtime control exceeds its complete body budget")
    return value


def _generation(value):
    if type(value) is not int or not 0 <= value <= MAX_GENERATION:
        raise ValueError("Invalid runtime policy generation")
    return value


def _profile(value):
    if type(value) is not str or value not in tool_policy.PROFILES:
        raise ValueError("Unknown runtime tool profile")
    return value


def _facts(app):
    from litetui.gui_rpc import _tool_policy
    policy = _tool_policy(app)
    if type(policy["tools_enabled"]) is not bool or policy["mcp_enabled"] is not False or policy["approval_route"] != "host":
        raise ValueError("Runtime controls require the captured host approval/MCP policy")
    flag = getattr(app, "_cli_tool_profile", None)
    if flag is not None:
        _profile(flag)
    provider = work.provider(app)
    return {"tools_enabled": policy["tools_enabled"],
            "settings_profile": _profile(app.settings.tool_policy_profile),
            "chosen_profile": _profile(app.chosen_tool_profile),
            "seat_profile": _profile(seat_authority.seat_profile(app)),
            "effective_profile": _profile(app._active_tool_profile), "requested_cli_profile": flag,
            "plan_mode": bool(app._plan_mode), "mcp_enabled": False, "approval_route": "host",
            "cwd": work.text(policy["cwd"], 32768), "provider": provider,
            "inventory": {"host": "host_request", "claude": "claude_session", "codex": "codex_runtime"}[provider]}


def stable(facts):
    return {key: value for key, value in facts.items() if key != "effective_profile"}


def _references(app):
    return app._agent_session, app.store, app.backend


def _same(state, *, historical=False):
    app = state.app
    if app._agent_session is not state.references[0]:
        raise ValueError("Runtime control AgentSession changed")
    app._validate_owned_identity()
    if historical:
        return
    if (not enabled(app) or work.bridge(app) is not state.bridge or work.identity(app) != state.bridge.identity
            or any(a is not b for a, b in zip(_references(app), state.references, strict=True))):
        raise ValueError("Runtime control identity or owner changed")
    state.bridge.check(allow_closing=state.closing)


def _command(app, cmd, operation, fields=(), *, attempt=None):
    work._keys(cmd, ("type", "id", *COMMON, *fields))
    if cmd["type"] != PREFIX + operation:
        raise ValueError("Unknown runtime control operation")
    for key in ("version", "runtime_controls_version"):
        if type(cmd[key]) is not int or cmd[key] != VERSION:
            raise ValueError("Runtime controls require exact version 1")
    work.text(cmd["id"])
    state = _state(app) if attempt is None else attempt.review.state
    if state is None or any(cmd[key] != value for key, value in _base(state).items()):
        raise ValueError("Runtime control command belongs to another captured identity")
    _same(state, historical=attempt is not None and attempt.historical)
    _bounded(cmd)
    return state


def capture(app, value):
    historical = getattr(app, "_native_runtime_historical", None)
    if historical is not None and not historical.verified:
        # Inspection remains allowed; fresh activation cannot expire this owner.
        _same(historical.review.state, historical=True)
    facts = _facts(app)
    value.runtime_controls_capture = copy.deepcopy(facts)
    return {"runtime_controls_version": VERSION, "runtime_policy": {"generation": 0, "facts": facts},
            "runtime_controls_limits": dict(LIMITS)}


def activation(app, value):
    historical = getattr(app, "_native_runtime_historical", None)
    if historical is not None and not historical.verified:
        raise ValueError("A retired runtime control still owns its exact publication")
    facts = _facts(app)
    if facts != getattr(value, "runtime_controls_capture", None):
        raise ValueError("Runtime policy changed after Work capture")
    return {"runtime_controls_version": VERSION, "runtime_policy": {"generation": 0, "facts": facts}}


def activated(app, value):
    if getattr(value, "runtime_controls_version", None) != VERSION:
        return
    facts = activation(app, value)["runtime_policy"]["facts"]
    app._native_runtime_controls = State(app, value, 0, stable(facts), _references(app))
    app._native_runtime_predecessor = None
    app._native_runtime_retired_review = None
    # Keep a consumed historical refusal until the following close can attest it.


def _owner(state):
    live = [(worker, origin) for worker, origin in state.bridge.executions.values() if not worker.is_finished]
    if len(live) > 1:
        raise ValueError("Runtime controls require one exact foreground stream owner")
    if live:
        _worker, origin = live[0]
        return {"state": "terminal" if origin.execution_id in state.terminal else "active", "origin": origin.envelope_fields()}
    if getattr(state.app, "_chat_running", lambda: False)():
        raise ValueError("Active stream lacks its captured Work owner")
    return {"state": "idle", "origin": None}


def observe_event(app, event, origin):
    state = _state(app)
    if not enabled(app) or origin is None or event.get("type") != "turn_end":
        return
    state.terminal.intersection_update(key for key, (worker, _) in state.bridge.executions.items()
                                       if not worker.is_finished)
    binding = state.bridge.executions.get(origin.execution_id)
    if binding is not None and binding[1] == origin:
        state.terminal.add(origin.execution_id)


def _system(app):
    return (copy.deepcopy(app.conversation[0]) if app.conversation and app.conversation[0].get("role") == "system" else None)


def _system_evidence(row):
    if row is None:
        return None
    content = row.get("content")
    if not isinstance(content, str):
        raise ValueError("Current system message is not plain text")  # noqa: TRY004 - invalid wire evidence
    return {"row_sha256": work.digest(work.encoded(row)), "content_sha256": work.digest(content.encode("utf-8"))}


def _plan():
    from litetui import paths
    try:
        with paths.PLAN_PROMPT_FILE.open("rb") as source:
            raw = source.read(65537)
    except FileNotFoundError:
        return {"present": False, "sha256": None, "text": None}
    if len(raw) > 65536:
        raise ValueError("Plan prompt exceeds the review budget")
    return {"present": True, "sha256": work.digest(raw), "text": raw.decode("utf-8")}


def _saved(app):
    snapshot = settings_runtime.service_for(app).snapshot(app.convo_id, read_only=True, max_bytes=262144)
    work._keys(snapshot.revisions, ("global", "conversation", "agent"))
    return snapshot, {"tools_enabled": snapshot.saved.tools_enabled,
                      "tool_profile": _profile(snapshot.saved.tool_policy_profile)}


def _context(app):
    from litetui import conversation_transition, hook_host
    return work.encoded({"settings": asdict(app.settings), "authority": asdict(app._agent_session.authority),
                         "registry": conversation_transition._registry(app), "hooks": asdict(hook_host.snapshot(app)),
                         "workspace": str(getattr(app, "_hook_workspace", "")),
                         "invocation": getattr(app, "_invocation_saved_values", {}),
                         "plan": _plan(), "prompt": app._runtime_control_prompt()})


def mutation_hold(app):
    if getattr(app, "_native_claude_queue_publication", None) is not None:
        return "Claude queued input owns its exact confirmation publication"
    if getattr(app, "_native_model_lifecycle_hold", False):
        return "A reviewed model operation owns admission until its independent readback"
    state = _state(app)
    if state is not None:
        if state.error:
            return state.error
        if state.attempt is not None and not state.attempt.verified:
            return "A runtime control owns its exact result and readback publication"
    from litetui import native_dictation
    if native_dictation.blocking(app):
        return "Dictation owns capture and its exact release publication"
    from litetui import native_playback
    if native_playback.blocking(app):
        return "Response playback owns its exact release publication"
    return None


def _guard(app, state):
    from litetui import (
        conversation_action,
        conversation_transition,
        native_deferred,
        native_schedule,
        native_work_execution,
    )
    _same(state)
    conversation_action.require_open(app)
    conversation_transition.require_execution(app)
    if state.closing or state.error or getattr(app, "_gui_quitting", False):
        raise ValueError(state.error or "Runtime controls are closing")
    app._validate_owned_execution()
    if (native_work_execution.mutation_hold(app) or native_deferred.mutation_hold(app)
            or native_schedule.admission_refusal(app) or state.bridge.pending_readbacks):
        raise ValueError("Another owned Work mutation requires its exact readback")
    if any(entry.claim_task is not None or entry.admission_worker is not None and not entry.admission_worker.is_finished
           for entry in state.bridge.inputs.values()):
        raise ValueError("A Work claim or pre-admission hook is in flight")
    if (getattr(app, "_gui_management_busy", False)
            or getattr(app, "_gui_model_pending", {}) or getattr(app, "_gui_host_pending", {})
            or any(not future.done() for future in getattr(app, "_approval_waiters", {}).values())
            or any(not item[0].is_set() for item in getattr(app, "_rpc_pending_asks", {}).values())):
        raise ValueError("Resolve the existing management, approval or question owner")
    if getattr(app, "_hook_lifecycle", ()) or getattr(app, "_hook_lock", None) and app._hook_lock.locked():
        raise ValueError("An independent lifecycle hook is still pending")
    owner = _owner(state)
    if any(t.origin is None or t.origin.envelope_fields() != owner["origin"] for t in state.tickets):
        raise ValueError("An independent callback still owns its authorization")


def snapshot(app, cmd):
    state = _command(app, cmd, "snapshot")
    if why := mutation_hold(app):
        raise ValueError(why)
    facts, owner = _facts(app), _owner(state)
    if stable(facts) != state.stable:
        raise ValueError("Live policy changed outside its verified control generation")
    saved, values = _saved(app)
    context = _context(app)
    try:
        _guard(app, state)
        reason = None
    except (ValueError, OSError) as exc:
        reason = str(exc)[:2048]
    result = {**_base(state), "kind": "runtime_controls_snapshot", "generation": state.generation,
              "facts": facts, "owner": owner, "saved": values, "revisions": saved.revisions,
              "first_system": _system_evidence(_system(app)), "plan": _plan(),
              "profiles": [{"name": name, "label": name, "summary": profile.summary}
                           for name, profile in tool_policy.PROFILES.items()],
              "next_profile": tool_policy.cycle(facts["settings_profile"]),
              "editable": {key: reason for key in ("tools_enabled", "tool_profile", "plan_mode")}}
    result["snapshot"] = work.digest(work.encoded(result) + context + secrets.token_bytes(32))
    _bounded(result)
    state.snapshot, state.snapshot_context = copy.deepcopy(result), context
    return result


def _change(value):
    work._keys(value, ("field", "value"))
    key, selected = value["field"], value["value"]
    if key == "tool_profile":
        _profile(selected)
    elif key not in {"tools_enabled", "plan_mode"} or type(selected) is not bool:
        raise ValueError("Runtime controls require one exact Boolean or profile choice")
    return key, selected


def validate(app, cmd):
    state = _command(app, cmd, "validate", ("snapshot", "generation", "change"))
    _generation(cmd["generation"])
    key, selected = _change(cmd["change"])
    before = state.snapshot
    if (before is None or cmd["snapshot"] != before["snapshot"] or cmd["generation"] != state.generation
            or state.snapshot_context != _context(app)):
        raise ValueError("Runtime control snapshot changed; refresh before Review")
    _guard(app, state)
    if mutation_hold(app):
        raise ValueError(mutation_hold(app))
    saved, values = _saved(app)
    if (_facts(app) != before["facts"] or _owner(state) != before["owner"] or saved.revisions != before["revisions"]
            or values != before["saved"] or _system_evidence(_system(app)) != before["first_system"]):
        raise ValueError("Runtime policy, owner or saved evidence changed before Review")
    after = copy.deepcopy(before["facts"])
    if key == "tool_profile":
        for name in ("settings_profile", "chosen_profile", "seat_profile", "effective_profile"):
            after[name] = selected
        after["requested_cli_profile"] = None
    else:
        after[key] = selected
    first = _system(app)
    next_system = copy.deepcopy(first)
    if first is not None and key in {"tools_enabled", "plan_mode"} and before["facts"][key] != selected:
        next_system["content"] = app._runtime_control_prompt(**{key: selected})
    consequence = ("Apply one explicit conversation policy choice. New authorizations and admissions wait for published readback; "
                   "already captured calls retain their original authority. "
                   + ("Plan mode is runtime prompt guidance; it is not a tool permission profile. " if key == "plan_mode" else "The selected preference is saved for this conversation. "))
    if key == "plan_mode" and selected and not before["plan"]["present"]:
        consequence += "The plan prompt file is absent; the mode flag changes without a PLAN section. "
    if key == "tools_enabled" and before["facts"]["provider"] == "claude":
        consequence += "Claude's inventory remains fixed for its session; disabling denies calls, enabling requires explicit Claude New to advertise additional tools."
    result = {**_base(state), "kind": "runtime_controls_review", "action_id": str(uuid.uuid4()), "review_token": secrets.token_hex(32),
              "snapshot": before["snapshot"], "generation": state.generation, "change": copy.deepcopy(cmd["change"]),
              "before": copy.deepcopy(before["facts"]), "after": after, "owner_before": copy.deepcopy(before["owner"]),
              "saved_before": values, "revisions": copy.deepcopy(saved.revisions), "first_system_before": before["first_system"],
              "first_system_after": _system_evidence(next_system), "plan": copy.deepcopy(before["plan"]),
              "context_sha256": work.digest(state.snapshot_context), "consequence": consequence}
    _bounded(result)
    state.review = Review(app, state, work.encoded(result), (*_references(app), app.settings), state.snapshot_context, first, next_system)
    return result


def _retained(app, wire):
    raw = work.encoded(wire)
    state = _state(app)
    candidates = [None if state is None else state.review, getattr(app, "_native_runtime_retired_review", None)]
    for review in candidates:
        if isinstance(review, Review) and review.app is app and review.wire == raw and not review.consumed:
            _same(review.state, historical=review.retired)
            return review
    raise ValueError("Runtime Review is foreign, expired, changed or consumed")


def _find_attempt(app, cmd):
    state = _state(app)
    candidates = [None if state is None else state.attempt, getattr(app, "_native_runtime_historical", None)]
    for attempt in candidates:
        if attempt is not None:
            review = work.strict_json(attempt.review.wire)
            if cmd.get("action_id") == review["action_id"] and cmd.get("review_token") == review["review_token"]:
                return attempt
    raise ValueError("Runtime result has no retained invocation")


def reserve_command(app, cmd):
    """Register a validated command before a management worker can race Quit."""
    operation = cmd.get("type")
    if operation == PREFIX + "apply":
        work._keys(cmd, ("type", "id", "review"))
        work.text(cmd["id"])
        _bounded(cmd)
        review = _retained(app, cmd["review"])
        state = review.state
        if state.attempt is not None and not state.attempt.verified:
            raise ValueError("Another runtime control still owns its publication")
        review.consumed = True
        attempt = Attempt(review, cmd["id"], work.encoded(cmd), None, historical=review.retired)
        if review.retired:
            prior = getattr(app, "_native_runtime_historical", None)
            if prior is not None and not prior.verified:
                review.consumed = False
                raise ValueError("Historical runtime refusal slot is occupied")
            app._native_runtime_historical = attempt
        else:
            state.attempt = attempt
        return attempt
    if operation == PREFIX + "readback":
        attempt = _find_attempt(app, cmd)
        _command(app, cmd, "readback", ("action_id", "review_token", "apply_operation_id", "readback_token", "result_sha256"), attempt=attempt)
        if (attempt.result is None or not attempt.result_published or attempt.readback_command is not None
                or cmd["apply_operation_id"] != attempt.operation or cmd["readback_token"] != attempt.result["readback_token"]
                or cmd["result_sha256"] != work.digest(attempt.result_raw)):
            raise ValueError("Runtime readback changed its exact published result")
        attempt.readback_operation, attempt.readback_command = cmd["id"], work.encoded(cmd)
        return attempt
    return None


def owned_apply(app):
    attempt = _APPLY.get()
    if (not isinstance(attempt, Attempt) or attempt.review.app is not app or attempt.task is not work._task()
            or attempt.historical or attempt.review.state.attempt is not attempt):
        return None
    return attempt


def defer_remembered(app, key, selected):
    attempt = owned_apply(app)
    if attempt is None or key != "tool_policy_profile":
        return False
    change = work.strict_json(attempt.review.wire)["change"]
    if change != {"field": "tool_profile", "value": selected}:
        raise ValueError("Runtime setter changed its selected preference")
    return True


def persist_selection(app, candidate):
    """Canonical SettingsService patch under the exact selected owned revision."""
    attempt = owned_apply(app)
    if attempt is None:
        return None
    from litetui import convo_settings
    from litetui.settings_service import SettingChange
    review = work.strict_json(attempt.review.wire)
    key = {"tool_profile": "tool_policy_profile", "tools_enabled": "tools_enabled"}.get(review["change"]["field"])
    if key is None or candidate is not app.settings or getattr(candidate, key) != review["change"]["value"]:
        raise ValueError("Runtime persistence changed its selected field or Settings owner")
    service = settings_runtime.service_for(app)
    def before_write():
        _same(attempt.review.state)
        _, documents, revisions = _documents(app)
        if revisions != review["revisions"] or documents != attempt.documents_before:
            raise ValueError("Reviewed settings documents or ownership changed before the coordinated write")
    result = service.save_patch(app.convo_id,
        [SettingChange(key, review["change"]["value"], "conversation")], review["revisions"],
        max_bytes=262144, before_write=before_write)
    attempt.persistence = result
    app._settings_save_result = result
    if all(item.saved for item in result.persistence):
        app._convo_settings = convo_settings.load(app.convo_dir)
        for name in ("_saved_values", "_baseline"):
            values = copy.deepcopy(getattr(candidate, name, {}))
            values[key] = copy.deepcopy(review["change"]["value"])
            object.__setattr__(candidate, name, values)
        getattr(app, "_invocation_saved_values", {}).pop(key, None)
    return result


def _documents(app):
    from litetui.native_settings import owned_documents
    return owned_documents(settings_runtime.service_for(app), app.convo_id, app._agent_session)


def _preflight(attempt):
    app, state = attempt.review.app, attempt.review.state
    review = work.strict_json(attempt.review.wire)
    if attempt.historical:
        raise ValueError("This Review retired with its original identity; no effects are permitted")
    _guard(app, state)
    if state.generation != review["generation"] or state.generation >= MAX_GENERATION:
        raise ValueError("Runtime generation changed or cannot advance")
    if (any(a is not b for a, b in zip((*_references(app), app.settings), attempt.review.references, strict=True))
            or _context(app) != attempt.review.context or _facts(app) != review["before"]
            or _owner(state) != review["owner_before"] or _system(app) != attempt.review.system_before):
        raise ValueError("Runtime Review became stale before effects")
    saved, values = _saved(app)
    if saved.revisions != review["revisions"] or values != review["saved_before"]:
        raise ValueError("Runtime saved revisions changed before effects")
    _, documents, revisions = _documents(app)
    if revisions != review["revisions"]:
        raise ValueError("Runtime settings bytes changed before effects")
    attempt.documents_before = documents
    attempt.documents_after = copy.deepcopy(documents)
    key, selected = _change(review["change"])
    if key != "plan_mode":
        from litetui.settings_service import SettingChange, conversation_document
        field = "tool_policy_profile" if key == "tool_profile" else key
        attempt.documents_after["conversation"] = conversation_document(documents["conversation"], saved.saved,
            [SettingChange(field, selected, "conversation")])


def _verify_system(app, expected):
    if _system(app) != expected or getattr(app.store, "persist_error", None):
        raise ValueError("Canonical first-system-row publication is unverified")
    if not app.store.pending:
        from litetui.conversation import ConversationRepository
        meta, messages = ConversationRepository.read(app.convo_path, max_bytes=8 * 1024 * 1024)
        first = messages[0] if messages and messages[0].get("role") == "system" else None
        if meta.get("id") != app.convo_id or first != expected:
            raise ValueError("Owned stored system row differs from the canonical setter")


def _result(attempt, *, status, reason=None, facts=None, saved=None, revisions=None, system=None, persistence="not_attempted"):
    review = work.strict_json(attempt.review.wire)
    positive = status in {"applied", "session_only"}
    result = {**_base(attempt.review.state), "kind": "runtime_controls_result", "action_id": review["action_id"],
              "review_token": review["review_token"], "apply_operation_id": attempt.operation,
              "generation_before": review["generation"], "generation_after": review["generation"] + 1 if positive else None,
              "status": status, "effect_attempted": attempt.effect_attempted,
              "changed": positive and (facts != review["before"] or saved != review["saved_before"] or system != review["first_system_before"]),
              "runtime_after": facts, "owner_after": copy.deepcopy(review["owner_before"]) if positive else None,
              "persistence": persistence, "saved_after": saved, "revisions_after": revisions,
              "first_system_after": system, "reason": reason, "readback_token": secrets.token_hex(32),
              "close_required": status == "uncertain"}
    _bounded(result)
    attempt.result, attempt.result_raw = result, work.encoded(result)
    return result


async def apply(app, cmd):
    state = _state(app)
    candidates = [None if state is None else state.attempt, getattr(app, "_native_runtime_historical", None)]
    attempt = next((a for a in candidates if a is not None and a.command == work.encoded(cmd) and a.task is None), None)
    if attempt is None:
        attempt = reserve_command(app, cmd)
    if attempt is None or attempt.task is not None or attempt.result is not None:
        raise ValueError("Runtime Apply was already consumed")
    attempt.task = work._task()
    try:
        _preflight(attempt)
    except Exception as exc:  # noqa: BLE001 - still before the canonical setter
        return _result(attempt, status="refused", reason=str(exc)[:2048])
    review = work.strict_json(attempt.review.wire)
    key, selected = _change(review["change"])
    attempt.effect_attempted = True
    token = _APPLY.set(attempt)
    try:
        if key == "tools_enabled":
            app.set_tools_enabled(selected)
        elif key == "tool_profile":
            if not app.set_tool_profile(selected, source="native-review"):
                raise ValueError("Canonical profile setter refused its reviewed value")
        else:
            app.set_plan_mode(selected)
        _same(attempt.review.state)
        facts = _facts(app)
        if facts != review["after"] or _owner(attempt.review.state) != review["owner_before"]:
            raise ValueError("Canonical setter did not match its exact Review")
        _verify_system(app, attempt.review.system_after)
        saved, values = _saved(app)
        _, documents, revisions = _documents(app)
        if revisions != saved.revisions or any(revisions[name] != review["revisions"][name] for name in ("agent", "global")):
            raise ValueError("Unreviewed settings dependencies changed during publication")
        persistence, reason, status = "not_applicable", None, "applied"
        if key != "plan_mode":
            saved_key = "tool_profile" if key == "tool_profile" else "tools_enabled"
            unaffected = "tools_enabled" if key == "tool_profile" else "tool_profile"
            if (documents == attempt.documents_after and values[saved_key] == selected
                    and values[unaffected] == review["saved_before"][unaffected]):
                persistence = "saved"
            elif (documents == attempt.documents_before and saved.revisions == review["revisions"]
                  and values == review["saved_before"]):
                persistence, status = "failed", "session_only"
                reason = "The live control changed; its conversation preference could not be saved"
            else:
                raise ValueError("Runtime preference publication changed partially or ambiguously")
        elif values != review["saved_before"] or saved.revisions != review["revisions"]:
            raise ValueError("Plan mode unexpectedly changed saved preferences")
        return _result(attempt, status=status, reason=reason, facts=facts, saved=values,
                       revisions=saved.revisions, system=_system_evidence(_system(app)), persistence=persistence)
    except (Exception, asyncio.CancelledError) as exc:  # noqa: BLE001 - effects may already have occurred
        attempt.review.state.error = CLOSE_REQUIRED
        return _result(attempt, status="uncertain", reason=(str(exc) or type(exc).__name__)[:2048], persistence="uncertain")
    finally:
        _APPLY.reset(token)


def _readback_body(attempt):
    app, state, result = attempt.review.app, attempt.review.state, attempt.result
    review = work.strict_json(attempt.review.wire)
    facts = owner = values = revisions = first = effective = None
    verified, release, reason = False, "close", result["reason"]
    try:
        _same(state, historical=attempt.historical)
        if result["status"] == "uncertain":
            raise ValueError(result["reason"] or CLOSE_REQUIRED)
        if attempt.historical:
            if attempt.effect_attempted or result["status"] != "refused":
                raise ValueError("Retired Review acquired execution authority")
        else:
            facts, owner = _facts(app), _owner(state)
            saved, values = _saved(app)
            revisions, first = saved.revisions, _system_evidence(_system(app))
            if result["status"] == "refused":
                if stable(facts) != state.stable:
                    raise ValueError("Current authority changed outside its published generation")
            else:
                if stable(facts) != stable(review["after"]):
                    raise ValueError("Readback stable policy differs from the canonical setter")
                _verify_system(app, attempt.review.system_after)
                if values != result["saved_after"] or revisions != result["revisions_after"]:
                    raise ValueError("Saved preference changed after the setter")
                prior = result["owner_after"]
                if owner != prior:
                    origin = prior["origin"]
                    if (origin is None or origin["execution_id"] not in state.terminal
                            or owner["state"] != "idle" and owner["origin"] != origin):
                        raise ValueError("Readback changed its actual stream owner")
                # No canonical admitted-stream finish currently writes a restored
                # profile. Preserve the exact sample; never invent a restoration.
                if facts["effective_profile"] != result["runtime_after"]["effective_profile"]:
                    raise ValueError("Effective profile changed without an owned canonical restoration")
                effective = "unchanged"
        verified = True
        release = "unchanged" if result["status"] == "refused" else "install"
    except Exception as exc:  # noqa: BLE001 - unreadable or inconsistent evidence requires closure
        state.error = CLOSE_REQUIRED
        reason = str(exc)[:2048]
    return _bounded({**_base(state), "kind": "runtime_controls_readback", "action_id": review["action_id"],
        "review_token": review["review_token"], "apply_operation_id": attempt.operation,
        "readback_operation_id": attempt.readback_operation, "result_sha256": work.digest(attempt.result_raw),
        "generation_before": result["generation_before"], "generation_after": result["generation_after"],
        "status": result["status"], "verified": verified, "current": facts, "owner": owner,
        "effective_transition": effective if verified else None, "saved": values, "revisions": revisions,
        "first_system": first, "reason": reason, "release": release})


def readback(app, cmd):
    attempt = _find_attempt(app, cmd)
    if attempt.readback_command is None:
        reserve_command(app, cmd)
    if attempt.readback_command != work.encoded(cmd) or attempt.readback is not None:
        raise ValueError("Runtime readback was changed or reused")
    attempt.readback_task = work._task()
    result = _readback_body(attempt)
    attempt.readback, attempt.readback_raw = result, work.encoded(result)
    return result


def prepare_publication(app, cmd, result):
    if cmd.get("type") not in {PREFIX + "apply", PREFIX + "readback"}:
        return None
    if cmd["type"] == PREFIX + "apply":
        state = _state(app)
        candidates = [None if state is None else state.attempt, getattr(app, "_native_runtime_historical", None)]
        attempt = next((a for a in candidates if a is not None and a.command == work.encoded(cmd)), None)
        phase = "result"
    else:
        attempt, phase = _find_attempt(app, cmd), "readback"
    if attempt is None:
        raise ValueError("Runtime publication has no exact invocation")
    expected = attempt.result if phase == "result" else attempt.readback
    raw = attempt.result_raw if phase == "result" else attempt.readback_raw
    command = attempt.command if phase == "result" else attempt.readback_command
    published = attempt.result_published if phase == "result" else attempt.readback_published
    owner = attempt.task if phase == "result" else attempt.readback_task
    if (work._task() is not owner or result is not expected or work.encoded(result) != raw or work.encoded(cmd) != command
            or published or attempt.publication is not None):
        raise ValueError("Runtime publication is foreign, changed or consumed")
    _same(attempt.review.state, historical=attempt.historical)
    if phase == "readback":
        # The body may have waited behind an actual caller wrapper. Sample again
        # immediately before the synchronous write; only our exact object changes.
        refreshed = _readback_body(attempt)
        result.clear()
        result.update(refreshed)
        attempt.readback_raw = raw = work.encoded(result)
    ticket = Publication(app, attempt, phase, work._task(), command, result, raw)
    attempt.publication = ticket
    return ticket


def finish_publication(app, ticket):
    if (not isinstance(ticket, Publication) or ticket.app is not app or ticket.used or ticket.task is not work._task()
            or ticket.attempt.publication is not ticket or work.encoded(ticket.result) != ticket.raw):
        raise ValueError("Runtime publication lost its exact one-use owner")
    attempt, state = ticket.attempt, ticket.attempt.review.state
    _same(state, historical=attempt.historical)
    ticket.used = True
    attempt.publication = None
    if ticket.phase == "result":
        attempt.result_published = True
        return
    attempt.readback_published = True
    if not ticket.result["verified"] or ticket.result["release"] == "close":
        state.error = CLOSE_REQUIRED
        return
    attempt.verified = True
    if not attempt.historical and ticket.result["release"] == "install":
        state.generation = ticket.result["generation_after"]
        state.stable = stable(ticket.result["current"])
    state.changed.set()
    state.changed = asyncio.Event()
    wake = getattr(app, "_flush_pending_input", None)
    if wake is not None and getattr(app, "_pending_input", ()) and not state.closing:
        app.call_later(wake)


class AuthorizationClosed(ValueError):
    """This caller lost its exact execution while waiting for a published choice."""


def current_authorization(app):
    ticket = _SCOPE.get()
    task = work._task()
    if (isinstance(ticket, Authorization) and ticket.state.app is app
            and ticket in ticket.state.tickets and (ticket.task is task or task in ticket.children)):
        return ticket
    return None


def _retain_ticket(ticket):
    ticket.references += 1


def _release_ticket(ticket):
    ticket.references -= 1
    if not ticket.references:
        ticket.state.tickets.remove(ticket)
        ticket.done.set()


@contextmanager
def reviewed_lifecycle_hook(app, owner, review, event, session_id):
    """Run only this reviewed lifecycle event between the old and new Work epochs."""
    state = _state(app)
    if not enabled(app) or not state.closing:
        yield
        return
    identity = getattr(app, "_conversation_transition_shutdown_identity", {}) or {}
    destination = {"new": "conversation_start", "open": "conversation_resume"}.get(identity.get("action"))
    expected = identity.get("from_session_id") if event == "conversation_leave" else identity.get("target_session_id")
    if destination is None or event not in ("conversation_leave", destination) or expected != session_id:
        raise AuthorizationClosed("Lifecycle event does not match the reviewed conversation transition")
    if getattr(state.bridge, "transition_proof", None) is None or state.shutdown is None or state.error:
        raise AuthorizationClosed("Lifecycle hook lacks the settled Work epoch")

    def check_owner():
        if (owner is not app._conversation_transition_task or not app._conversation_transition_pending
                or app._conversation_transition_hook_snapshot is not review.hooks or _state(app) is not state
                or work.bridge(app) is not state.bridge or app._agent_session is not review.context.session
                or app.store is not review.context.store or app.backend is not review.context.backend
                or app.store.convo_id != session_id or not app.store.owned or app.store.pending
                or work.identity(app) != {**state.bridge.identity, "session_id": session_id}):
            raise AuthorizationClosed("Reviewed lifecycle hook lost its conversation owner")
        if (getattr(app, "_gui_quitting", False) or getattr(app, "_conversation_transition_shutdown_requested", False)
                or getattr(app, "_stop_requested", False)):
            raise AuthorizationClosed("Conversation stopped before lifecycle hook authorization")
        app._validate_owned_identity()

    check_owner()
    token = _LIFECYCLE_HOOK.set((app, asyncio.current_task(), check_owner))
    try:
        yield
    finally:
        _LIFECYCLE_HOOK.reset(token)


@asynccontextmanager
async def authorization(app, *, origin=None, profile=None, captured=None, lifecycle_hook=False):
    """Wait before reading authority. Only the exact original task or registered
    child can continue an already captured decision through a publication hold."""
    scope = _LIFECYCLE_HOOK.get() if lifecycle_hook else None
    if scope is not None:
        scoped_app, hook_task, check_owner = scope
        if scoped_app is not app or hook_task is not asyncio.current_task():
            raise AuthorizationClosed("Reviewed lifecycle hook belongs to another task")
        check_owner()
        # The old Work epoch stays closed. The reviewed hook still passes through
        # canonical shell policy and approval with its current conversation profile.
        yield None
        return
    if not enabled(app):
        yield None
        return
    inherited = current_authorization(app)
    if inherited is not None:
        yield inherited
        return
    from litetui import hook_host
    state = _state(app)
    task = asyncio.current_task()
    origin = origin or work.current_origin(app)
    if origin is not None:
        original = state.bridge.executions.get(origin.execution_id)
        if original is None or original[1] != origin:
            raise AuthorizationClosed("Authorization belongs to an expired stream")
    else:
        original = None
    parked = False
    try:
        while True:
            if getattr(app, "_native_model_lifecycle_hold", False) and getattr(app, "_backend_transition_error", None):
                raise AuthorizationClosed(app._backend_transition_error)
            if (state.closing or state.error or not enabled(app) or _state(app) is not state
                    or getattr(app, "_stop_requested", False) and (parked or origin is not None)):
                raise AuthorizationClosed("Runtime stopped before authorization")
            if original is not None and (state.bridge.executions.get(origin.execution_id) != original
                                         or original[0].is_finished):
                raise AuthorizationClosed("Authorization belongs to a finished execution")
            if not mutation_hold(app):
                break
            if not parked:
                if task in state.waiters or len(state.waiters) >= LIMITS["waiters"]:
                    raise AuthorizationClosed("Runtime authorization waiter limit reached")
                state.waiters[task] = origin
                parked = True
            await state.changed.wait()
    finally:
        if parked:
            state.waiters.pop(task, None)
    _same(state)
    if len(state.tickets) >= LIMITS["authorizations"]:
        raise AuthorizationClosed("Runtime authorization limit reached")
    # A parked request has not yet captured any authority. Values supplied before
    # that wait must never override the policy now in force.
    facts = _facts(app)
    ticket = Authorization(state, task, origin, state.generation,
        facts["effective_profile"] if parked or not profile else _profile(profile),
        facts["tools_enabled"], frozenset(app.settings.tools_disabled or ()),
        copy.deepcopy(captured if captured is not None and not parked else hook_host.context(app)),
        facts["approval_route"])
    state.tickets.append(ticket)
    token = _SCOPE.set(ticket)
    try:
        yield ticket
    finally:
        _SCOPE.reset(token)
        _release_ticket(ticket)


def tool_authorization(method):
    """Capture the one policy used by an entire host call, including its hooks."""
    @wraps(method)
    async def wrapped(app, name, *args, **kwargs):
        try:
            async with authorization(app, profile=kwargs.get("profile"),
                                     lifecycle_hook=kwargs.pop("_reviewed_lifecycle_hook", False)):
                return await method(app, name, *args, **kwargs)
        except AuthorizationClosed as exc:
            from litetui.textfmt import tool_denied
            return tool_denied("profile", name=name, reason=str(exc)), False
    return wrapped


class _AuthorizationContinuation:
    """One private handoff from a captured decision to its exact continuation."""

    def __init__(self, ticket, discard=None):
        self.ticket = ticket
        self.discard = discard
        _retain_ticket(ticket)

    def close(self):
        ticket, self.ticket = self.ticket, None
        if ticket is not None:
            try:
                if self.discard is not None:
                    self.discard()
            finally:
                self.discard = None
                _release_ticket(ticket)

    async def run(self, callback, *args, **kwargs):
        ticket, self.ticket = self.ticket, None
        if ticket is None:
            raise AuthorizationClosed("Authorization continuation already retired")
        self.discard = None
        task = asyncio.current_task()
        registered = task not in ticket.children
        if registered:
            ticket.children.add(task)
        token = _SCOPE.set(ticket)
        try:
            if _state(ticket.state.app) is not ticket.state or not enabled(ticket.state.app):
                raise AuthorizationClosed("Authorization belongs to a retired runtime")
            try:
                _same(ticket.state)
            except ValueError as exc:
                raise AuthorizationClosed("Authorization belongs to a changed session") from exc
            return await callback(*args, **kwargs)
        finally:
            _SCOPE.reset(token)
            if registered:
                ticket.children.discard(task)
            _release_ticket(ticket)


def retain_authorization(app):
    ticket = current_authorization(app)
    return _AuthorizationContinuation(ticket) if ticket is not None else None


def child_call(app, awaitable, *, work_call=None):
    """Retain the decision across a real child task, even before its first poll."""
    ticket = current_authorization(app)
    if ticket is None:
        return awaitable
    child = _AuthorizationContinuation(ticket, getattr(awaitable, "close", None))
    run = child.run(lambda: awaitable)
    if work_call is not None:
        # The Work producer installs its process slot and current task before
        # starting this coroutine. Its join path also owns pre-start cancellation.
        work_call.authorization_child = child
        return run
    try:
        task = asyncio.create_task(run)
    except BaseException:
        run.close()
        child.close()
        raise
    task.add_done_callback(lambda _task: child.close())
    return task


def signal_stop(app):
    state = _state(app)
    if state is not None:
        state.changed.set()
        state.changed = asyncio.Event()


def begin_close(app):
    state = _state(app)
    if state is not None:
        state.closing = True
        signal_stop(app)


def operation_allowed(app, cmd):
    if not enabled(app):
        return
    op = cmd.get("type")
    if op == "prompt":
        state = _state(app)
        policy = cmd.get("runtime_policy")
        if (cmd.keys() - {"native_image"} != {"type", "id", "message", "runtime_policy"} or not isinstance(cmd["message"], str)
                or not isinstance(cmd["id"], str) or not cmd["id"] or type(policy) is not dict
                or policy.keys() != {"version", "work_instance", "generation"}
                or type(policy["version"]) is not int or policy["version"] != VERSION
                or _generation(policy["generation"]) != state.generation
                or policy["work_instance"] != state.bridge.instance or stable(_facts(app)) != state.stable):
            raise ValueError("Prompt requires its published runtime policy generation")
        if str(cmd.get("message", "")).lstrip().startswith("/"):
            raise ValueError("Use the runtime command controls for slash commands")
    if op in {"set", "run_command", "gui.commands.execute", "gui.tools.execute", "gui.prompt.submit", "gui.system.set"}:
        raise ValueError("Use the reviewed runtime controls for this operation")
    fields = {"tools_enabled", "tool_policy_profile", "tool_profile", "profile", "plan_mode"}
    if op in {"set_settings", "gui.settings.apply", "gui.settings.native_validate", "gui.settings.native_save"}:
        def touches(value):
            if isinstance(value, list):
                return any(touches(item) for item in value)
            if isinstance(value, dict):
                return bool(fields.intersection(value)) or any(
                    (key in {"id", "key", "name", "field"} and isinstance(item, str) and item in fields)
                    or touches(item) for key, item in value.items())
            return False
        if touches(cmd):
            raise ValueError("Use the reviewed runtime control for this setting")
    if why := mutation_hold(app):
        from litetui import native_model_lifecycle
        if native_model_lifecycle.readback_allowed(app, cmd):
            return
        from litetui import native_schedule
        if op not in {*OPERATIONS, *native_schedule.INSPECTION, "shutdown", "abort",
                       "gui.work.native_settled", "approve", "answer"}:
            raise ValueError(why)


async def _join_attempt(attempt):
    if attempt is None:
        return
    worker = getattr(attempt, "worker", None)
    if worker is not None and not worker.is_finished:
        from textual.worker import WorkerCancelled, WorkerFailed
        try:
            await worker.wait()
        except (WorkerCancelled, WorkerFailed):
            pass
        if not worker.is_finished:
            raise RuntimeError("Runtime control worker did not finish")
    elif attempt.task is not None and attempt.task is not asyncio.current_task() and not attempt.task.done():
        await asyncio.shield(attempt.task)
    publication = attempt.publication
    if publication is not None:
        owner = publication.task
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            await asyncio.shield(owner)
        if owner is not None and owner is not asyncio.current_task() and not owner.done():
            raise RuntimeError("Runtime publication owner did not finish")
    attempt.closed = True


def _attempt_fields(attempt):
    if attempt is None:
        return dict.fromkeys(("action_id", "review_token", "apply_operation_id", "readback_operation_id",
                               "result_sha256", "readback_sha256"))
    review = work.strict_json(attempt.review.wire)
    return {"action_id": review["action_id"], "review_token": review["review_token"],
            "apply_operation_id": attempt.operation, "readback_operation_id": attempt.readback_operation,
            "result_sha256": work.digest(attempt.result_raw) if attempt.result_raw else None,
            "readback_sha256": work.digest(attempt.readback_raw) if attempt.readback_raw else None}


async def shutdown(app):
    state = _state(app)
    proof = getattr(app, "_native_runtime_predecessor", None)
    if state is not None and state.shutdown is None:
        begin_close(app)
        waiters = list(state.waiters)
        for ticket in tuple(state.tickets):
            await ticket.done.wait()
        for task in waiters:
            if task is not asyncio.current_task() and not task.done():
                await asyncio.gather(task, return_exceptions=True)
        if state.tickets or state.waiters:
            raise RuntimeError("Runtime authorizations did not retire")
        attempt = state.attempt
        await _join_attempt(attempt)
        disposition = "idle"
        if attempt is not None:
            if attempt.result is not None and attempt.result["status"] == "refused":
                disposition = "no_effect"
            elif attempt.verified:
                disposition = "verified"
            else:
                disposition = "uncertain" if attempt.effect_attempted else "no_effect"
        proof = {**_base(state), "kind": "runtime_controls_shutdown", "published_generation": state.generation,
                 **_attempt_fields(attempt), "pending_at_close": bool(attempt and not attempt.verified),
                 "readback_published": bool(attempt and attempt.readback_published),
                 "effect_attempted": bool(attempt and attempt.effect_attempted), "disposition": disposition,
                 "authorization_closed": True, "admission_closed": True,
                 "control_owner": "joined" if attempt else "absent",
                 "publication_owner": "joined" if attempt and (attempt.publication or attempt.result_published or attempt.readback_published) else "absent",
                 "authorization_owners_joined": True, "waiter_owners_joined": True,
                 "remaining_authorizations": 0, "remaining_waiters": 0, "effects_verified": False}
        state.shutdown = proof
    elif state is not None:
        proof = state.shutdown
    if proof is None:
        return {}
    old = getattr(app, "_native_runtime_historical", None)
    history = None
    if old is not None:
        await _join_attempt(old)
        if old.effect_attempted or not old.historical:
            raise ValueError("Historical runtime control acquired effects")
        history = {**_base(old.review.state), "kind": "runtime_controls_historical_shutdown",
                   **_attempt_fields(old), "readback_published": old.readback_published,
                   "disposition": "refused", "effect_attempted": False, "execution_authority": False,
                   "command_owner": "joined", "publication_owner": "joined" if old.publication or old.result_published else "absent",
                   "effects_verified": False}
    return {"runtime_controls_shutdown": copy.deepcopy(proof), "runtime_controls_historical_shutdown": history}


def retired(app, bridge):
    state = _state(app)
    if state is None:
        return {}
    if state.bridge is not bridge or not state.closing or state.shutdown is None:
        raise ValueError("Runtime identity changed without exact retirement")
    if state.review is not None and not state.review.consumed:
        state.review.retired = True
        app._native_runtime_retired_review = state.review
    app._native_runtime_predecessor = copy.deepcopy(state.shutdown)
    app._native_runtime_controls = None
    return copy.deepcopy(bridge.runtime_controls_retirement)
