"""Reviewed host-history edit/retry: durable publication before one ordinary turn.

The source owns selection, admission, the append-only snapshot, and the exact
execution worker. A displayed history fragment is never execution authority.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from dataclasses import asdict, replace

from litetui import (
    backend_catalog,
    claude_rewind,
    codex_rewind,
    conversation_transition,
    hook_host,
    native_history,
    seat_authority,
)
from litetui import history_projection as projection
from litetui.conversation import ConversationRepository

VERSION = 1
MAX_TEXT = 65536
MAX_SEGMENTS = 100
MAX_CALLBACKS = 512
MAX_WORKERS = 512
PREFIX = "gui.conversations.native_rewrite_"
COMMON = ("version", "session_id", "agent_id", "backend", "model")
SELECTION = ("history_snapshot", "context_sha256", "message_index", "message_sha256")
REVIEW_FIELDS = (*COMMON, *SELECTION, "snapshot", "review", "action_id", "action", "text", "text_sha256",
                 "prepared_context_sha256", "prepared_messages", "prefix_context_sha256", "prefix_messages",
                 "discarded_messages")
CLOSE_REQUIRED = "Conversation rewrite is uncertain; close this runtime and reopen explicitly."


from litetui.conversation_action import (
    _SCHEDULE,
    Attempt,
    Execution,
    OwnedWork,
    QuestionOrigin,
    Review,
    ThreadEvent,
    ToolCall,
    _attempt,
    _authorized,
    _continuation,
    _drain,
    _echo,
    _files,
    _finish_exit,
    _history_command,
    _identity,
    _join_tools,
    _join_workers,
    _objects,
    _operation,
    _owned_runner,
    _prefix,
    _protected,
    _receipts,
    _refusal_checkpoint,
    _required_attempt,
    _saved_receipts,
    _scope,
    _settle_shutdown,
    _stable,
    _task,
    _track_owned,
    _work_budget,
    advance_receipts,
    after_refresh,
    capture_question,
    capture_tool,
    captured_work,
    card_summary,
    event_origin,
    forget_question,
    hook_cleanup_scope,
    inline_runner,
    operation_allowed,
    question_abort_origins,
    question_registry_guard,
    register_attempt,
    request_shutdown,
    require_open,
    segment,
    settle_before_shutdown,
    stop,
    stopped,
    stream_origin,
    stream_runner,
    submission_refusal,
    tool_after_allow_prompt,
    tool_refusal,
    track_descendant,
    track_stream,
    track_tool,
)

__all__ = ['_SCHEDULE', 'Attempt', 'Execution', 'OwnedWork', 'QuestionOrigin', 'Review', 'ThreadEvent', 'ToolCall', '_attempt', '_authorized', '_continuation', '_drain', '_echo', '_files', '_finish_exit', '_history_command', '_identity', '_join_tools', '_join_workers', '_objects', '_operation', '_owned_runner', '_prefix', '_protected', '_receipts', '_refusal_checkpoint', '_required_attempt', '_saved_receipts', '_scope', '_settle_shutdown', '_stable', '_task', '_track_owned', '_work_budget', 'advance_receipts', 'after_refresh', 'capture_question', 'capture_tool', 'captured_work', 'card_summary', 'event_origin', 'forget_question', 'hook_cleanup_scope', 'inline_runner', 'operation_allowed', 'question_abort_origins', 'question_registry_guard', 'register_attempt', 'request_shutdown', 'require_open', 'segment', 'settle_before_shutdown', 'stop', 'stopped', 'stream_origin', 'stream_runner', 'submission_refusal', 'tool_after_allow_prompt', 'tool_refusal', 'track_descendant', 'track_stream', 'track_tool']


def _text(app, value):
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > MAX_TEXT or "\0" in value:
        raise ValueError("Edit/Retry requires nonempty text of at most 65536 UTF-8 bytes without NUL")
    if value.lstrip().startswith("/"):
        raise ValueError("Edit/Retry cannot dispatch slash commands")
    if app._looks_like_image_path(value):
        raise ValueError("Edit/Retry does not load image paths; use a new explicit attachment")
    if getattr(app, "pending_image", None) is not None:
        raise ValueError("Resolve the pending attachment before Edit/Retry")
    if why := app._oversize_refusal(value):
        raise ValueError(why)
    return value


def _eligibility(app, message, prefix):
    try:
        native = hasattr(app.backend, "app_server")
        if native:
            if app.backend.name != "codex":
                raise ValueError("This provider does not support native Codex rewind")
            thread, *_ = codex_rewind.row_link(message)
            if thread != codex_rewind.Binding.capture(app).thread_id:
                raise ValueError("The saved row belongs to another native thread")
        elif getattr(app.backend, "owns_native_turns", False):
            if app.backend.name != "claude" or not message.get("claude_delivery"):
                raise ValueError("This saved user row lacks a verifiable native Claude delivery")
            claude_rewind.Binding.capture(app, rewrite=True)
        if app.store.pending or not app.store.owned:
            raise ValueError("Edit/Retry requires an already materialized owned conversation")
        allowed = {"role", "content", "provider_metadata", "codex_delivery"} if native else {"role", "content"}
        if getattr(app.backend, "owns_native_turns", False):
            allowed.add("claude_delivery")
        if not set(message) <= allowed:
            raise ValueError("Edit/Retry currently supports plain user text without delivery or provider metadata")
        if reason := conversation_transition._eligibility(app):
            raise ValueError(reason)
        if getattr(app, "pending_image", None) is not None:
            raise ValueError("Resolve the pending attachment before Edit/Retry")
        _prefix(app, prefix)
        app._validate_owned_execution()
    except (ValueError, OSError) as exc:
        return str(exc)
    return None


def _snapshot(app, cmd):
    _operation(cmd)
    if type(cmd.get("version")) is not int or cmd["version"] != VERSION:
        raise ValueError("Conversation rewrite requires version 1")
    # A dispatched snapshot negotiates shutdown even if selection later fails.
    app._conversation_rewrite_protocol = True
    require_open(app)
    if any(cmd.get(key) != value for key, value in _identity(app).items()):
        raise ValueError("Rewrite belongs to another active owned identity")
    descriptor, captured, _index = native_history._collect(app, _history_command(app))
    if _saved_receipts(captured) != _receipts(app):
        raise ValueError("Saved store receipts disagree with the current owned context")
    if (cmd.get("history_snapshot") != descriptor["snapshot"]
            or cmd.get("context_sha256") != descriptor["context_sha256"]):
        raise ValueError("Current history changed; select the user message again")
    messages = captured.messages
    index = cmd.get("message_index")
    if type(index) is not int or not 0 <= index < len(messages):
        raise ValueError("Rewrite message index does not name current history")
    message = messages[index]
    if message.get("role") != "user" or not isinstance(message.get("content"), str):
        raise ValueError("Edit/Retry requires a plain-text user message")
    original = message["content"]
    if len(original.encode("utf-8")) > MAX_TEXT:
        raise ValueError("Original user text exceeds the 65536-byte editor budget")
    if cmd.get("message_sha256") != projection.digest(projection.encoded(message)):
        raise ValueError("Selected user message hash changed")
    hooks, registry = conversation_transition._hooks(app), conversation_transition._registry(app)
    reason = "Current transcript is read-only" if not captured.writable else _eligibility(app, message, messages[:index])
    retry_reason = reason
    if retry_reason is None:
        try:
            _text(app, original)
        except ValueError as exc:
            retry_reason = str(exc)
    value = {"kind": "conversation_rewrite_snapshot", **_identity(app),
             **{key: cmd[key] for key in SELECTION}, "original_text": original,
             "original_bytes": len(original.encode("utf-8")), "prefix_messages": index,
             "prefix_context_sha256": projection.digest(projection.encoded(messages[:index])),
             "discarded_messages": len(messages) - index - 1,
             "actions": {"edit": {"eligible": reason is None, "reason": reason},
                         "retry": {"eligible": retry_reason is None, "reason": retry_reason}}}
    context = backend_catalog._context(app, app.convo_id)
    value["snapshot"] = backend_catalog.fingerprint(app, context, {
        "rewrite": value, "hooks": asdict(hooks), "registry": registry,
        "objects": [id(value) for value in _objects(app)]})
    review = Review(b"", _objects(app), _protected(app), _files(app), hooks, registry,
                    captured.messages_bytes, captured.raw or b"", app.conversation,
                    seat_authority.turn_profile(app, "rpc", seat_authority.seat_profile(app)),
                    getattr(app, "_active_tool_profile", None), _receipts(app))
    _stable(app, review, original=True)
    return value, review


def snapshot(app, cmd):
    return native_history._wire(cmd, _snapshot(app, cmd)[0])


async def validate_codex(app, cmd):
    current, capture = _snapshot(app, cmd)
    chosen = current["actions"].get(cmd.get("action"), {})
    if current["snapshot"] != cmd.get("snapshot") or not chosen.get("eligible"):
        raise ValueError(chosen.get("reason") or "Codex rewrite selection changed")
    rewind = await codex_rewind.Rewind.review(app, json.loads(capture.history), current["message_index"])
    _stable(app, capture, original=True)
    return validate(app, cmd, rewind=rewind)


def validate(app, cmd, *, rewind=None):
    current, captured = _snapshot(app, cmd)
    if cmd.get("snapshot") != current["snapshot"]:
        raise ValueError("Rewrite selection is stale; inspect again")
    action = cmd.get("action")
    if action not in {"edit", "retry"} or not current["actions"][action]["eligible"]:
        raise ValueError(current["actions"].get(action, {}).get("reason") or "Unsupported rewrite action")
    if action == "retry" and "text" in cmd:
        raise ValueError("Retry cannot override the original user text")
    text = _text(app, cmd.get("text") if action == "edit" else current["original_text"])
    saved = json.loads(captured.history)
    fork = claude_rewind.Fork.review(app, saved, current["message_index"]) if getattr(app.backend, "owns_native_turns", False) else None
    candidate = fork.candidate(text) if fork else {"role": "user", "content": text}
    messages = saved[:current["message_index"]] + [candidate]
    value = {"kind": "conversation_rewrite_review", **{key: current[key] for key in (*COMMON, *SELECTION, "snapshot",
        "prefix_messages", "prefix_context_sha256", "discarded_messages")},
        "review": secrets.token_hex(32), "action_id": str(uuid.uuid4()), "action": action, "text": text,
        "text_sha256": projection.digest(text.encode("utf-8")),
        "prepared_context_sha256": projection.digest(projection.encoded(messages)), "prepared_messages": len(messages),
        "effects": {"prompt_hooks": bool(conversation_transition._matching(captured.hooks, "prompt_before", "rpc")),
                    "provider_execution": True, "provider_rewind": False}}
    if hasattr(app.backend, "app_server"):
        if rewind is None:
            raise ValueError("Codex rewrites require native history inspection")
        value["effects"]["provider_rewind"] = True
        value["native_rewind"] = rewind.plan
        review = codex_rewind.Review(**{**vars(captured), "wire": projection.encoded(value)}, rewind=rewind)
    elif fork is not None:
        value["effects"]["provider_fork"] = True
        value["native_fork"] = fork.plan
        review = claude_rewind.Review(**{**vars(captured), "wire": projection.encoded(value)}, fork=fork)
    else:
        review = replace(captured, wire=projection.encoded(value))
    _snapshot_record(app, review, messages)
    app._conversation_rewrite_review = review
    return native_history._wire(cmd, value)


def _snapshot_record(app, review, messages):
    receipts = json.loads(review.receipts)
    record = ConversationRepository.snapshot_record(messages, "Reviewed Edit/Retry", **receipts)
    payload = ConversationRepository.record_bytes(record, separator=bool(review.raw and not review.raw.endswith(b"\n")))
    if len(review.raw) + len(payload) > native_history.MAX_TRANSCRIPT:
        raise ValueError("Replacement snapshot would exceed the 8 MiB transcript budget")
    return record


def _receipt(app, cmd, attempt, turn, reason=None):
    wire = json.loads(attempt.review.wire)
    return native_history._wire(cmd, {"kind": "conversation_rewrite_apply",
        **{key: wire[key] for key in (*COMMON, *SELECTION, "snapshot", "review", "action_id", "action",
                                     "prepared_context_sha256", "prepared_messages", "text_sha256")},
        "operation_id": attempt.operation, "turn": turn, "mutated": attempt.mutated,
        "close_required": turn == "failed", "reason": reason,
        "persistence": {"snapshot_written": attempt.written, "readback_verified": attempt.verified},
        "refusal_history": json.loads(attempt.refusal_history) if turn == "refused" and attempt.refusal_history else None})


async def apply(app, cmd):
    operation = _operation(cmd)
    review = getattr(app, "_conversation_rewrite_review", None)
    if not isinstance(review, Review):
        raise ValueError("No unconsumed Edit/Retry review; inspect again")  # noqa: TRY004 - protocol refusal
    app._conversation_rewrite_review = None
    wire = json.loads(review.wire)
    fields = (*REVIEW_FIELDS, "effects", "native_rewind") if isinstance(review, codex_rewind.Review) else REVIEW_FIELDS
    if isinstance(review, claude_rewind.Review):
        fields = (*REVIEW_FIELDS, "effects", "native_fork")
    if not _echo(cmd, wire, fields):
        raise ValueError("Apply must exactly echo its reviewed Edit/Retry action")
    attempt = Attempt(review, operation, _task())
    register_attempt(app, attempt)
    reasons: list[str] = []
    old_hook_snapshot = getattr(app, "_conversation_transition_hook_snapshot", None)
    old_hook_outcomes = getattr(app, "_conversation_transition_hook_outcomes", None)
    app._conversation_transition_hook_snapshot = review.hooks
    app._conversation_transition_hook_outcomes = []
    app._conversation_transition_cleanup_error = None
    try:
        with _scope(app, attempt, "admission"):
            def before_accept():
                _stable(app, review, original=True)
                _text(app, wire["text"])
                if reason := conversation_transition._eligibility(app):
                    raise ValueError(reason)

            before_accept()
            reasons.clear()

            def persist(message):
                _stable(app, review, original=True, admitted=True)
                expected = review.fork.candidate(wire["text"]) if isinstance(review, claude_rewind.Review) else {"role": "user", "content": wire["text"]}
                if message != expected:
                    raise ValueError("Admission changed the reviewed user message")
                messages = json.loads(review.history)[:wire["message_index"]] + [message]
                if projection.digest(projection.encoded(messages)) != wire["prepared_context_sha256"]:
                    raise ValueError("Prepared rewrite context changed")
                record = _snapshot_record(app, review, messages)
                # Any attempted append can have partial durable effects. Never
                # restore the old list as a claim that the write was rolled back.
                attempt.mutated = True
                app.store.write_record(record)
                if app.store.persist_error:
                    raise OSError(app.store.persist_error)
                captured = native_history.capture_file(app.convo_path)
                if (captured.meta.get("id") != app.convo_id or not captured.writable
                        or captured.messages_bytes != projection.encoded(messages)
                        or _saved_receipts(captured) != review.receipts):
                    raise OSError("Replacement snapshot failed complete durable readback")
                attempt.written = True
                app.conversation = messages
                attempt.verified = True

            item = {"content": wire["text"], "text": wire["text"], "source": "rpc",
                    "operation_id": operation, "tool_profile": review.profile}
            async def prepare_provider():
                if isinstance(review, claude_rewind.Review):
                    await review.fork.prepare(app, attempt, item, before_accept)
                else:
                    await review.rewind.revert(app, attempt)
                    before_accept()

            accepted = await hook_host.admit_prompt_origin(app, item, on_refused=reasons.append,
                before_accept=before_accept, persist_user=persist, allow_prompt=False,
                **({"before_persist": prepare_provider} if isinstance(review, (codex_rewind.Review, claude_rewind.Review)) else {}))
            if getattr(app, "_conversation_transition_cleanup_error", None):
                raise RuntimeError("Prompt hook cleanup is unproven")
            if accepted is None:
                if attempt.mutated:
                    raise RuntimeError("Provider history preparation ran but the replacement was refused. Reopen to inspect native history.")
                attempt.reason = reasons[-1] if reasons else "Prompt was not admitted"
                _refusal_checkpoint(app, attempt)
                return _receipt(app, cmd, attempt, "refused", attempt.reason)
            _stable(app, review, admitted=True)
            if not attempt.verified:
                raise OSError("Admission did not verify replacement persistence")
            token = _SCHEDULE.set((app, attempt, _task()))
            try:
                worker = app._stream(origin=accepted)
            finally:
                _SCHEDULE.reset(token)
            if attempt.execution is None or attempt.execution.worker is not worker:
                raise RuntimeError("Canonical rewrite execution did not register its worker")
            attempt.outcome = "accepted"
            attempt.drain = asyncio.create_task(_drain(app, attempt))
            return _receipt(app, cmd, attempt, "accepted")
    except BaseException as exc:  # noqa: BLE001 - partial persistence is never rollback
        reason = (str(exc) or type(exc).__name__).encode("utf-8")[:4096].decode("utf-8", "ignore")
        if attempt.mutated and isinstance(review, claude_rewind.Review):
            reason = f"Claude Edit/Retry stopped while {review.fork.stage}: {reason}".encode("utf-8")[:4096].decode("utf-8", "ignore")
        failed = attempt.mutated or bool(getattr(app, "_conversation_transition_cleanup_error", None))
        if not failed:
            try:
                with _scope(app, attempt, "admission"):
                    _refusal_checkpoint(app, attempt)
            except Exception:  # noqa: BLE001 - refusal needs coherent positive current proof
                failed = True
        attempt.outcome = "failed" if failed else "refused"
        attempt.reason = reason
        if failed:
            app._conversation_rewrite_error = CLOSE_REQUIRED
        return _receipt(app, cmd, attempt, attempt.outcome, reason)
    finally:
        app._conversation_transition_hook_snapshot = old_hook_snapshot
        app._conversation_transition_hook_outcomes = old_hook_outcomes


async def settled(app, cmd):
    _operation(cmd)
    attempt = _attempt(app)
    if attempt is None or attempt.kind != "rewrite" or not attempt.pending:
        raise ValueError("No pending Edit/Retry settlement")
    wire = json.loads(attempt.review.wire)
    if (not _echo(cmd, wire, COMMON) or cmd.get("action_id") != wire["action_id"]
            or cmd.get("operation_id") != attempt.operation or attempt.outcome == "failed"):
        raise ValueError("Unknown or failed Edit/Retry settlement")
    if attempt.drain is not None:
        await asyncio.shield(attempt.drain)
    await _join_workers(attempt)
    with _scope(app, attempt, "settled"):
        capture = attempt.refusal if attempt.outcome == "refused" else attempt.review
        if capture is None:
            raise ValueError("Refused rewrite lacks its captured current-history proof")
        _stable(app, capture, original=attempt.outcome == "refused", admitted=attempt.outcome == "accepted")
        descriptor, captured, _index = native_history._collect(app, _history_command(app))
        if _saved_receipts(captured) != _receipts(app):
            raise ValueError("Settled rewrite store receipts disagree with durable history")
        if attempt.outcome == "refused" and projection.encoded(descriptor) != attempt.refusal_history:
            raise ValueError("Refused rewrite history changed again before settlement")
        result = {"kind": "conversation_rewrite_settled", **{key: wire[key] for key in (
            *COMMON, *SELECTION, "snapshot", "review", "action_id", "action")},
            "operation_id": attempt.operation, "turn": attempt.outcome, "worker_settled": True,
            "mutated": attempt.mutated, "close_required": False, "history": descriptor,
            "execution": {"segments": len(attempt.segments or ()),
                          "last_execution_id": attempt.segments[-1] if attempt.segments else None}}
        if isinstance(attempt.review, codex_rewind.Review):
            rewind = attempt.review.rewind
            if attempt.outcome == "accepted":
                if len(attempt.segments or ()) != 1:
                    raise ValueError("Verified Codex rewrites require exactly one replacement execution")
                try:
                    result["native_rewind"] = await rewind.settlement(app, captured.messages, wire["message_index"], wire["text"])
                    _stable(app, attempt.review, admitted=True)
                    if native_history.capture_file(app.convo_path).raw != captured.raw:
                        raise ValueError("Saved history changed during Codex replacement verification")
                except (Exception, asyncio.CancelledError):
                    attempt.outcome = "failed"
                    app._conversation_rewrite_error = CLOSE_REQUIRED
                    raise
            else:
                result["native_rewind"] = None
        if isinstance(attempt.review, claude_rewind.Review):
            result["native_fork"] = None
            if attempt.outcome == "accepted":
                try:
                    if len(attempt.segments or ()) != 1:
                        raise ValueError("Claude Edit/Retry requires one native replacement execution")
                    result["native_fork"] = attempt.review.fork.settled(app,attempt,captured.messages,wire["message_index"])
                except Exception:
                    attempt.outcome = "failed"
                    app._conversation_rewrite_error = CLOSE_REQUIRED
                    raise
        native_history._wire(cmd, result)
        attempt.pending = False
        return result
