"""Reviewed manual host-history compaction, using the canonical maintenance worker."""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any

from litetui import backend_catalog, conversation_transition, native_history
from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui.conversation import ConversationRepository

VERSION = 1
PREFIX = "gui.conversations.native_compact_"
COMMON = action.COMMON
SELECTION = ("history_snapshot", "context_sha256")
FIELDS = (*COMMON, *SELECTION, "snapshot", "review", "action_id", "action", "root_execution_id",
          "plan", "config", "hint", "hint_sha256", "request_prompt_sha256", "effects")
IDENTITY = (*COMMON, *SELECTION, "snapshot", "review", "action_id", "action", "root_execution_id")


@dataclass(frozen=True)
class CompactOutcome:
    outcome: str
    reason_code: str
    reason: str | None = None
    publication_attempted: bool = False
    publication_verified: bool = False


@dataclass
class Publication:
    owner: Any
    expected: bytes
    attempted: bool = False
    verified: bool = False


def command_refusal(app, text):
    if (native_history.enabled(app) and isinstance(text, str) and text.split()
            and text.split()[0].lower() == "/compact"):
        return "Compact requires the reviewed native compaction action; open its review first."
    return None


def _config(app):
    settings = app.settings
    value = {"keep_recent": settings.compact_keep_recent, "max_tokens": settings.compact_max_tokens,
             "max_tool_iters": settings.compact_max_tool_iters, "thinking_level": settings.compact_thinking_level,
             "tools_enabled": app.tools_enabled, "wake_after_compact": settings.wake_after_compact,
             "clear_screen_after_compact": settings.clear_screen_after_compact}
    for key, low, high in (("keep_recent", 0, 1000000), ("max_tokens", 256, 2147483647), ("max_tool_iters", 1, 512)):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise ValueError("Unsupported compaction configuration: " + key)
    if value["thinking_level"] not in {"default", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        raise ValueError("Unsupported compaction thinking level")
    if any(type(value[key]) is not bool for key in ("tools_enabled", "wake_after_compact", "clear_screen_after_compact")):
        raise ValueError("Compaction effects must be booleans")
    return value


def _prompt():
    from litetui.app import COMPACT_PROMPT
    return COMPACT_PROMPT


def _plan(app, messages, config):
    system = int(bool(messages and messages[0].get("role") == "system"))
    body = messages[system:]
    tail = app._safe_tail(body, config["keep_recent"])
    head = body[:len(body) - len(tail)] if tail else body
    return {"messages": len(messages), "system_messages": system, "head_messages": len(head),
            "tail_messages": len(tail), "head_context_sha256": projection.digest(projection.encoded(head)),
            "tail_context_sha256": projection.digest(projection.encoded(tail)),
            "prompt_sha256": projection.digest(_prompt().encode("utf-8")),
            "config_sha256": projection.digest(projection.encoded(config))}


def _eligibility(app):
    if hasattr(app.backend, "app_server") or getattr(app.backend, "owns_native_turns", False):
        return "This provider owns native context; host-history compaction is unavailable"
    if getattr(app, "_owner_memory_dir", None) is not None:
        return "Reviewed manual compaction does not yet support owner-mode handoff"
    if app.store.pending or not app.store.owned:
        return "Compaction requires an already materialized owned conversation"
    if getattr(app, "pending_image", None) is not None:
        return "Resolve the pending attachment before compaction"
    return conversation_transition._eligibility(app)


def _snapshot(app, cmd):
    action._operation(cmd)
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Reviewed compaction requires version 1")
    app._conversation_compact_protocol = True
    action.require_open(app)
    if not action._echo(cmd, action._identity(app), COMMON):
        raise ValueError("Compaction belongs to another owned identity")
    descriptor, captured, _index = native_history._collect(app, action._history_command(app))
    if action._saved_receipts(captured) != action._receipts(app):
        raise ValueError("Compaction store receipts disagree with durable history")
    if cmd.get("history_snapshot") != descriptor["snapshot"] or cmd.get("context_sha256") != descriptor["context_sha256"]:
        raise ValueError("Compaction history changed; inspect again")
    action._prefix(app, captured.messages)
    config = _config(app)
    hooks, registry = conversation_transition._hooks(app), conversation_transition._registry(app)
    reason = "Current transcript is read-only" if not captured.writable else _eligibility(app)
    value = {"kind": "conversation_compact_snapshot", **action._identity(app),
             **{key: cmd[key] for key in SELECTION}, "eligible": reason is None, "reason": reason,
             "plan": _plan(app, captured.messages, config), "config": config}
    value["snapshot"] = backend_catalog.fingerprint(app, backend_catalog._context(app, app.convo_id), {
        "compact": value, "hooks": asdict(hooks), "registry": registry,
        "objects": [id(item) for item in action._objects(app)]})
    profile = getattr(app, "_active_tool_profile", None)
    review = action.Review(b"", action._objects(app), action._protected(app), action._files(app), hooks, registry,
                           captured.messages_bytes, captured.raw or b"", app.conversation,
                           profile, profile, action._receipts(app))
    action._stable(app, review, original=True)
    return value, review


def snapshot(app, cmd):
    return native_history._wire(cmd, _snapshot(app, cmd)[0])


def validate(app, cmd):
    current, captured = _snapshot(app, cmd)
    if cmd.get("snapshot") != current["snapshot"] or not current["eligible"]:
        raise ValueError(current["reason"] or "Compaction review is stale")
    hint = cmd.get("hint")
    if not isinstance(hint, str) or len(hint.encode("utf-8")) > action.MAX_TEXT or "\0" in hint:
        raise ValueError("Compaction hint must be at most 65536 UTF-8 bytes without NUL")
    config = current["config"]
    value = {"kind": "conversation_compact_review", **{key: current[key] for key in
             (*COMMON, *SELECTION, "snapshot", "plan", "config")},
             "review": secrets.token_hex(32), "action_id": str(uuid.uuid4()), "action": "compact",
             "root_execution_id": str(uuid.uuid4()), "hint": hint,
             "hint_sha256": projection.digest(hint.encode("utf-8")),
             "request_prompt_sha256": projection.digest((_prompt() + ("\n\n" + hint if hint else "")).encode("utf-8")),
             "effects": {"provider_execution": True, "provider_rewind": False,
                         **{key: config[key] for key in ("tools_enabled", "wake_after_compact", "clear_screen_after_compact")}}}
    app._conversation_compact_review = replace(captured, wire=projection.encoded(value))
    return native_history._wire(cmd, value)


def _receipt(app, cmd, attempt):
    wire = json.loads(attempt.review.wire)
    return native_history._wire(cmd, {"kind": "conversation_compact_apply", **{key: wire[key] for key in IDENTITY},
        "operation_id": attempt.operation, "status": attempt.outcome, "reason": attempt.reason,
        "close_required": attempt.outcome == "failed",
        "refusal_history": json.loads(attempt.refusal_history) if attempt.outcome == "refused" and attempt.refusal_history else None})


async def apply(app, cmd):
    operation = action._operation(cmd)
    review = getattr(app, "_conversation_compact_review", None)
    if not isinstance(review, action.Review):
        raise ValueError("No unconsumed compaction review")  # noqa: TRY004 - typed protocol refusal
    app._conversation_compact_review = None
    wire = json.loads(review.wire)
    if not action._echo(cmd, wire, FIELDS):
        raise ValueError("Apply must exactly echo its reviewed compaction")
    attempt = action.Attempt(review, operation, action._task(), kind="compact")
    action.register_attempt(app, attempt)
    try:
        with action._scope(app, attempt, "admission"):
            action._stable(app, review, original=True)
            if reason := _eligibility(app):
                raise ValueError(reason)
            if _config(app) != wire["config"] or _plan(app, app.conversation, wire["config"]) != wire["plan"]:
                raise ValueError("Compaction plan or configuration changed")
            # Manual maintenance cannot consume a stale automatic/handoff flag.
            if getattr(app, "_interrupted_compact_resume", None) is not None or getattr(app, "_compact_is_auto", False):
                raise ValueError("Another compaction continuation is pending")
            token = action._SCHEDULE.set((app, attempt, action._task()))
            try:
                worker = app._compact(wire["hint"])
            finally:
                action._SCHEDULE.reset(token)
            if attempt.execution is None or attempt.execution.worker is not worker:
                raise RuntimeError("Canonical compaction root did not register")
            attempt.outcome = "accepted"
            attempt.drain = asyncio.create_task(action._drain(app, attempt))
    except BaseException as exc:  # noqa: BLE001 - scheduled or uncertain effects close admission
        attempt.reason = (str(exc) or type(exc).__name__).encode("utf-8")[:4096].decode("utf-8", "ignore")
        failed = attempt.execution is not None or attempt.mutated
        if not failed:
            try:
                with action._scope(app, attempt, "admission"):
                    action._refusal_checkpoint(app, attempt)
            except Exception:  # noqa: BLE001 - no refusal without positive current proof
                failed = True
        attempt.outcome = "failed" if failed else "refused"
        if failed:
            app._conversation_rewrite_error = action.CLOSE_REQUIRED
    return _receipt(app, cmd, attempt)


def begin_publication(app, rebuilt):
    """Bind the canonical candidate before either truncate or system-edit write."""
    if not action._authorized(app):
        return None
    attempt = action._required_attempt(app)
    action._stable(app, attempt.review, admitted=True)
    raw = projection.encoded(rebuilt)
    if len(raw) > native_history.MAX_TRANSCRIPT:
        raise ValueError("Compacted context exceeds the 8 MiB source budget")
    captured = native_history.capture_file(app.convo_path)
    if (not captured.writable or captured.meta.get("id") != app.convo_id
            or captured.messages_bytes != projection.encoded(app.conversation)
            or action._saved_receipts(captured) != action._receipts(app)):
        raise ValueError("Compaction input is not coherent with durable history")
    publication = Publication(action._task(), raw)
    attempt.publication = publication
    return publication


def write_publication_record(app, record):
    """Use the actual canonical record bytes for a bounded captured append."""
    attempt = action._attempt(app)
    if attempt is None or not action._authorized(app) or not isinstance(attempt.publication, Publication):
        return False
    publication = attempt.publication
    if publication.owner is not action._task() or publication.verified:
        return False
    captured = native_history.capture_file(app.convo_path)
    if not captured.writable or captured.meta.get("id") != app.convo_id:
        raise ValueError("Compaction transcript changed its writable owned identity")
    raw = captured.raw or b""
    payload = ConversationRepository.record_bytes(record, separator=bool(raw and not raw.endswith(b"\n")))
    if len(raw) + len(payload) > native_history.MAX_TRANSCRIPT:
        raise ValueError("Compaction append exceeds the 8 MiB transcript budget")
    publication.attempted = True
    attempt.mutated = True
    app.store.write_record(record)
    if app.store.persist_error:
        raise OSError(app.store.persist_error)
    return True


def verify_publication(app, publication):
    if publication is None:
        return
    attempt = action._required_attempt(app)
    if (not action._authorized(app) or attempt.publication is not publication
            or publication.owner is not action._task() or not publication.attempted or publication.verified):
        raise ValueError("Compaction publication changed its exact owner")
    captured = native_history.capture_file(app.convo_path)
    if (app.store.persist_error or not captured.writable or captured.meta.get("id") != app.convo_id
            or captured.messages_bytes != publication.expected or projection.encoded(app.conversation) != publication.expected
            or action._saved_receipts(captured) != action._receipts(app)):
        raise OSError("Compaction publication failed exact durable/live/receipt readback")
    publication.verified = True


def capture_root_result(app, attempt: action.Attempt, result):
    if (attempt is not action._attempt(app) or not action._authorized(app) or attempt.root_outcome is not None
            or not isinstance(result, CompactOutcome)):
        raise ValueError("Compaction root lacks a unique typed invocation outcome")
    allowed = {("compacted", "compacted"), ("no_change", "too_short"), ("no_change", "already_recent"),
               ("cancelled", "cancelled"), ("failed", "no_summary"), ("failed", "provider_failed")}
    if (result.outcome, result.reason_code) not in allowed:
        raise ValueError("Compaction root returned an unknown outcome")
    publication = attempt.publication
    attempted = bool(isinstance(publication, Publication) and publication.attempted)
    verified = bool(isinstance(publication, Publication) and publication.verified)
    if (result.publication_attempted != attempted or result.publication_verified != verified
            or (result.outcome == "compacted") != (attempted and verified) or attempted and not verified):
        raise ValueError("Compaction root cannot prove its publication outcome")
    action._stable(app, attempt.review, admitted=True)
    with action.readback(app):
        descriptor, captured, _index = native_history._collect(app, action._history_command(app))
    if action._saved_receipts(captured) != action._receipts(app):
        raise ValueError("Compaction root receipts disagree with durable history")
    attempt.root_outcome = result
    attempt.root_history = projection.encoded(descriptor)


async def settled(app, cmd):
    action._operation(cmd)
    attempt = action._attempt(app)
    if attempt is None or attempt.kind != "compact" or not attempt.pending:
        raise ValueError("No pending compaction settlement")
    wire = json.loads(attempt.review.wire)
    if (not action._echo(cmd, wire, COMMON) or cmd.get("action_id") != wire["action_id"]
            or cmd.get("operation_id") != attempt.operation or attempt.outcome == "failed"):
        raise ValueError("Unknown or failed compaction settlement")
    if attempt.drain is not None:
        await asyncio.shield(attempt.drain)
    await action._join_workers(attempt)
    with action._scope(app, attempt, "settled"):
        refused = attempt.outcome == "refused"
        capture = attempt.refusal if refused else attempt.review
        if capture is None:
            raise ValueError("Compaction refusal lacks a coherent current checkpoint")
        action._stable(app, capture, original=refused, admitted=not refused)
        descriptor, captured, _index = native_history._collect(app, action._history_command(app))
        if action._saved_receipts(captured) != action._receipts(app):
            raise ValueError("Compaction settlement store receipts disagree")
        if refused:
            if projection.encoded(descriptor) != attempt.refusal_history:
                raise ValueError("Refused compaction history changed again")
            outcome = CompactOutcome("no_change", "admission_refused", attempt.reason)
            root_history = None
        else:
            if not attempt.root_started or attempt.root_history is None or not isinstance(attempt.root_outcome, CompactOutcome):
                raise ValueError("Compaction root has no completed invocation witness")
            outcome = attempt.root_outcome
            root_history = json.loads(attempt.root_history)
        result = {"kind": "conversation_compact_settled", **{key: wire[key] for key in IDENTITY},
                  "operation_id": attempt.operation, "status": attempt.outcome, "worker_settled": True,
                  "close_required": False, "outcome": outcome.outcome, "reason_code": outcome.reason_code,
                  "reason": outcome.reason, "history_changed": descriptor["context_sha256"] != wire["context_sha256"],
                  "history": descriptor, "root_history": root_history,
                  "witness": {"root_started": attempt.root_started, "input_context_sha256": wire["context_sha256"],
                              "root_context_sha256": (root_history or descriptor)["context_sha256"],
                              "publication_attempted": outcome.publication_attempted,
                              "publication_verified": outcome.publication_verified},
                  "execution": {"segments": len(attempt.segments or ()),
                                "last_execution_id": attempt.segments[-1] if attempt.segments else None}}
        native_history._wire(cmd, result)
        attempt.pending = False
        return result
