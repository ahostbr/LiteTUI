"""Reviewed manual host-history compaction, using the canonical maintenance worker."""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import asdict, dataclass, replace
from typing import Any

from litetui import (
    backend_catalog,
    claude_compaction_evidence,
    compaction_preferences,
    conversation_transition,
    native_history,
)
from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui.codex_compaction_evidence import Binding, VerifiedCompaction
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
    native: VerifiedCompaction | None = None
    claude: claude_compaction_evidence.SegmentReceipt | None = None


@dataclass(frozen=True)
class ClaudeReview(action.Review):
    binding: claude_compaction_evidence.Binding | None


@dataclass(frozen=True)
class CodexReview(action.Review):
    binding: Binding | None


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
    for source, key in (("compact_keep_recent", "keep_recent"), ("compact_max_tokens", "max_tokens"),
                        ("compact_max_tool_iters", "max_tool_iters"), ("compact_thinking_level", "thinking_level"),
                        ("wake_after_compact", "wake_after_compact"), ("clear_screen_after_compact", "clear_screen_after_compact")):
        compaction_preferences.checked(source, value[key])
    if type(value["tools_enabled"]) is not bool:
        raise ValueError("Compaction effects must be booleans")
    if app.backend.name == "codex" and hasattr(app.backend, "app_server"):
        value.update(tools_enabled=False, wake_after_compact=False, clear_screen_after_compact=False)
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
    if hasattr(app.backend, "app_server"):
        try:
            Binding.capture(app)
        except ValueError as exc:
            return str(exc)
    if getattr(app.backend, "owns_native_turns", False):
        try:
            claude_compaction_evidence.Binding.capture(app)
        except ValueError as exc:
            return str(exc)
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
    codex = app.backend.name == "codex" and hasattr(app.backend, "app_server")
    claude = app.backend.name == "claude" and getattr(app.backend, "owns_native_turns", False)
    binding = None
    if claude:
        try:
            binding = claude_compaction_evidence.Binding.capture(app)
        except ValueError:
            pass  # Show the specific eligibility reason below.
        plan = binding.plan(len(captured.messages)) if binding else {"kind": "claude", "messages": len(captured.messages),
                "segment": None, "native_session": None, "ledger_before": None, "prompt_sha256": None}
    elif codex:
        try:
            binding = Binding.capture(app)
        except ValueError:
            pass  # _eligibility supplies the reason for a read-only snapshot.
        plan = {"kind": "codex", "thread_id": binding.thread_id if binding else None,
                "messages": len(captured.messages)}
    else:
        plan = _plan(app, captured.messages, config)
    hooks, registry = conversation_transition._hooks(app), conversation_transition._registry(app)
    reason = "Current transcript is read-only" if not captured.writable else _eligibility(app)
    value = {"kind": "conversation_compact_snapshot", **action._identity(app),
             **{key: cmd[key] for key in SELECTION}, "eligible": reason is None, "reason": reason,
             "plan": plan, "config": config}
    value["snapshot"] = backend_catalog.fingerprint(app, backend_catalog._context(app, app.convo_id), {
        "compact": value, "hooks": asdict(hooks), "registry": registry,
        "objects": [id(item) for item in action._objects(app)],
        **({"codex": [id(binding.transport), id(binding.server), id(binding.process), id(binding.session),
                       binding.generation]} if codex and binding else {}),
        **({"claude": [id(binding.ledger), id(binding.session), id(binding.owner), binding.generation]}
           if claude and binding else {})})
    profile = getattr(app, "_active_tool_profile", None)
    review_type = ClaudeReview if claude else CodexReview if codex else action.Review
    review = review_type(b"", action._objects(app), action._protected(app), action._files(app), hooks, registry,
                           captured.messages_bytes, captured.raw or b"", app.conversation,
                           profile, profile, action._receipts(app), **({"binding": binding} if codex or claude else {}))
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
    native = isinstance(captured, CodexReview)
    if native and hint:
        raise ValueError("Codex native compaction does not accept guidance")
    if isinstance(captured, ClaudeReview):
        from litetui.claude_compact import request
        prompt = request(hint)
    else:
        prompt = _prompt() + ("\n\n" + hint if hint else "")
    config = current["config"]
    value = {"kind": "conversation_compact_review", **{key: current[key] for key in
             (*COMMON, *SELECTION, "snapshot", "plan", "config")},
             "review": secrets.token_hex(32), "action_id": str(uuid.uuid4()), "action": "compact",
             "root_execution_id": str(uuid.uuid4()), "hint": hint,
             "hint_sha256": projection.digest(hint.encode("utf-8")),
             "request_prompt_sha256": None if native else projection.digest(prompt.encode("utf-8")),
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
            if isinstance(review, ClaudeReview):
                if review.binding is None:
                    raise ValueError("Reviewed Claude session unavailable")
                review.binding.check(app)
                plan = review.binding.plan(len(app.conversation))
            elif isinstance(review, CodexReview):
                if review.binding is None:
                    raise ValueError("Reviewed Codex binding is unavailable")
                review.binding.check(app)
                plan = {"kind": "codex", "messages": len(app.conversation), "thread_id": review.binding.thread_id}
            else:
                plan = _plan(app, app.conversation, wire["config"])
            if _config(app) != wire["config"] or plan != wire["plan"]:
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


def is_reviewed_claude(app):
    return action._authorized(app) and isinstance(action._required_attempt(app).review, ClaudeReview)


async def reviewed_claude(app, hint):
    from litetui.claude_compact import compact

    attempt = action._required_attempt(app)
    binding = attempt.review.binding
    if (not is_reviewed_claude(app) or attempt.kind != "compact" or not attempt.root_started
            or attempt.mutated or binding is None):
        raise ValueError("Claude compaction lost the reviewed execution owner")
    binding.check(app)
    progress = claude_compaction_evidence.Progress(binding)
    attempt.mutated = True
    try:
        receipt = await compact(app, hint, reviewed=progress)
        if not isinstance(receipt, claude_compaction_evidence.SegmentReceipt):
            raise TypeError("The reviewed SDK result or publication is unverified")
        binding.verify_final(app, receipt)
    except Exception as exc:
        attempt.reason = f"Claude compaction stopped during {progress.stage}. Close and inspect the conversation."
        raise ValueError(attempt.reason) from exc
    return CompactOutcome("compacted", "compacted", publication_attempted=True, publication_verified=True, claude=receipt)


def is_reviewed_codex(app):
    return action._authorized(app) and isinstance(action._required_attempt(app).review, CodexReview)


async def reviewed_codex(app):
    """Only the captured maintenance worker may dispatch native compaction."""
    attempt = action._required_attempt(app)
    review = attempt.review
    if (not action._authorized(app) or not isinstance(review, CodexReview) or review.binding is None
            or attempt.kind != "compact" or attempt.mutated or not attempt.root_started):
        raise ValueError("Codex compaction lost the reviewed execution owner")
    review.binding.check(app)
    attempt.mutated = True  # Dispatch or cancellation can leave the remote result uncertain.
    receipt = await review.binding.transport.compact(expected_thread=review.binding.thread_id, binding=review.binding)
    review.binding.check(app)
    if not isinstance(receipt, VerifiedCompaction) or receipt.thread_id != review.binding.thread_id:
        raise ValueError("Codex compaction lacks verified native history")
    return CompactOutcome("compacted", "compacted", native=receipt)


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
    if isinstance(attempt.review, ClaudeReview) and raw != attempt.review.raw:
        raise ValueError("The reviewed Claude transcript changed before publication")
    payload = ConversationRepository.record_bytes(record, separator=bool(raw and not raw.endswith(b"\n")))
    if len(raw) + len(payload) > native_history.MAX_TRANSCRIPT:
        raise ValueError("Compaction append exceeds the 8 MiB transcript budget")
    publication.attempted = True
    attempt.mutated = True
    app.store.write_record(record)
    if app.store.persist_error:
        raise OSError(app.store.persist_error)
    if isinstance(attempt.review, ClaudeReview) and native_history.capture_file(app.convo_path).raw != raw + payload:
        raise OSError("Claude transcript did not append the exact reviewed truncation record")
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
    native = isinstance(attempt.review, CodexReview)
    if native:
        binding = attempt.review.binding
        if binding is None:
            raise ValueError("Reviewed Codex thread disappeared")
        binding.check(app)
        if (result.outcome == "compacted"
                and (not attempt.mutated or not isinstance(result.native, VerifiedCompaction)
                     or result.native.thread_id != binding.thread_id)
                or result.outcome != "compacted" and (attempt.mutated or result.outcome != "cancelled")):
            raise ValueError("Codex compaction lacks an exact native outcome")
        action._stable(app, attempt.review, original=True, admitted=True)
    elif result.native is not None:
        raise ValueError("Host compaction returned an unexpected provider witness")
    if isinstance(attempt.review, ClaudeReview):
        if result.outcome == "cancelled" and not attempt.mutated:
            attempt.review.binding.check(app)
        elif result.outcome == "compacted" and isinstance(result.claude, claude_compaction_evidence.SegmentReceipt):
            attempt.review.binding.verify_final(app, result.claude)
        else:
            raise ValueError("Claude compaction lacks SDK, segment and local publication proof")
    elif result.claude is not None:
        raise ValueError("Unexpected Claude segment witness")
    if (result.publication_attempted != attempted or result.publication_verified != verified
            or (result.outcome == "compacted" and not native) != (attempted and verified) or attempted and not verified):
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
        try:
            await asyncio.shield(attempt.drain)
        except Exception:
            if isinstance(attempt.review, ClaudeReview) and attempt.reason:
                raise ValueError(attempt.reason) from None
            raise
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
        if isinstance(attempt.review, CodexReview) and not refused:
            attempt.review.binding.check(app)
            action._stable(app, attempt.review, original=True, admitted=True)
            if (root_history != descriptor or descriptor["snapshot"] != wire["history_snapshot"]
                    or descriptor["context_sha256"] != wire["context_sha256"]):
                raise ValueError("Codex compaction changed the stored transcript")
        if isinstance(attempt.review, ClaudeReview) and not refused:
            if outcome.claude:
                attempt.review.binding.verify_final(app, outcome.claude)
                if root_history != descriptor:
                    raise ValueError("Published Claude transcript changed before settlement")
            else:
                attempt.review.binding.check(app)
                action._stable(app, attempt.review, original=True, admitted=True)
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
        if isinstance(attempt.review, ClaudeReview):
            result["witness"]["claude_compaction"] = asdict(outcome.claude) if outcome.claude else None
        if isinstance(attempt.review, CodexReview):
            result["witness"]["native_compaction"] = asdict(outcome.native) if outcome.native else None
        native_history._wire(cmd, result)
        attempt.pending = False
        return result
