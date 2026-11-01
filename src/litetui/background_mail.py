"""Durable inbox intake before mailbox claim, using the canonical prompt path.

The mailbox is external to conversation storage. A source-owned producer records
its exact immutable input before a successful move can release that input to the
prompt hooks. Failed moves and interrupted intake remain held, never replayed.
"""
from __future__ import annotations

import asyncio
import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from litetui import background_input as work

RAW_MAIL_BYTES = 131072


def item(app: Any, message: dict) -> dict:
    from litetui import approval_delivery, harness, seat_authority, tool_policy
    text = harness.format_message(message)
    profile = seat_authority.seat_profile(app)
    content = text
    if tool_policy.PROFILES.get(profile) and tool_policy.PROFILES[profile].confirm:
        rule = tool_policy.inbox_turn_rule(seat_authority.confirm_route(app), getattr(app, "_spawner_id", None))
        content = f"{text}\n\n{rule}"
    value = {"content": content, "text": text, "tool_profile": profile, "source": "harness"}
    ident = approval_delivery.request_id(message)
    if ident:
        value["approval_request_id"] = ident
    return value


def deliver(app: Any, payload: dict) -> None:
    from litetui import approval_delivery, hook_host
    entry = work.entry_for(app, payload)
    if entry is None:
        entry = work.capture_inbox(app, payload)
    if entry is not None and entry.state != "queued":
        return
    ident = payload.get("approval_request_id")
    if ident:
        approval_delivery.stage(ident, "received")
    text = payload.get("text", payload["content"])
    if app._chat_running():
        payload["bubble"] = app._user_bubble(text, False, queued=True)
        approval_delivery.enqueue(app._pending_input, payload, ident)
        return
    app._user_bubble(text, False)
    if entry is not None:
        app._pending_input.append(payload)
    hook_host.start_prompt(app, payload)


@dataclass(eq=False)
class Intake:
    record: Any
    payload: dict
    message: dict
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    claimed: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())
    entry: Any = None
    worker: Any = None
    error: BaseException | None = None


async def prepare(app: Any, message: dict, object_id: str) -> Intake:
    from litetui import tasks
    work.require_recapture_complete(app)
    value = work.bridge(app)
    if value is None or not value.active:
        raise ValueError("Inbox capture is not active")
    # Direct receipt callers also receive the complete-message budget before
    # hashing or retaining unknown metadata. Maildir reads enforce it earlier.
    if len(work.encoded(message)) > RAW_MAIL_BYTES:
        raise ValueError("Inbox metadata exceeds captured input capacity; mail left unclaimed")
    payload = item(app, message)
    immutable = {key: payload[key] for key in ("content", "text", "tool_profile", "source")}
    if len(work.encoded(immutable)) > work.LIMITS["payload_bytes"]:
        raise ValueError("Inbox message exceeds captured input capacity; mail left unclaimed")
    producers = list(value.producers.values())
    producers.extend(entry.producer for entry in value.inputs.values() if entry.recovered)
    if any(p.ref["kind"] == "inbox" and p.ref["object_id"] == object_id for p in producers):
        raise ValueError("Inbox message already has retained intake evidence; automatic replay refused")
    reservation = work.reserve(app)
    try:
        app._materialise_convo()
        record = work.register_producer(app, "inbox", object_id, owner_instance=tasks._INSTANCE_ID,
                                        reservation=reservation)
    finally:
        work.release(app, reservation)
    intake = Intake(record, payload, copy.deepcopy(message))

    async def run() -> None:
        try:
            intake.entry = work.enqueue(app, record, payload)
            intake.ready.set()
            accepted = await intake.claimed
            if not accepted or intake.entry.state != "queued":
                return
            from litetui import approval_relay, tool_approval
            if approval_relay.take_answer(app, intake.message) or tool_approval.take_spawner_answer(app, intake.message):
                value.input_event(intake.entry, "cancelled", "Mail resolved an existing approval; no prompt was admitted")
                return
            deliver(app, payload)
        except BaseException as error:
            intake.error = error
            raise
        finally:
            if intake.entry is not None and intake.entry.state == "queued" and payload not in app._pending_input:
                value.input_event(intake.entry, "held", "Inbox intake ended before prompt admission; original input retained")
            intake.ready.set()

    runner = work.producer_runner(app, record, run())
    try:
        intake.worker = app.run_worker(runner, group="work-mail", exclusive=False, exit_on_error=False)
        work.bind_producer_worker(app, record, intake.worker)
        await asyncio.wait_for(intake.ready.wait(), 5)
        if intake.error is not None:
            raise ValueError("Inbox input could not be durably retained before claim") from intake.error
        if intake.entry is None:
            raise ValueError("Inbox producer ended without durable input evidence")
    except BaseException:
        if intake.worker is not None:
            intake.worker.cancel()
        else:
            runner.close()
        raise
    return intake


def finish(intake: Intake, claimed: bool) -> bool:
    if not intake.claimed.done():
        intake.claimed.set_result(claimed)
    return True  # Delivery belongs to the exact registered producer, not poll's caller.


def route(app: Any, message: dict) -> bool:
    work.require_recapture_complete(app)
    if not work.enabled(app) or work.capturing_completion(app):
        return False

    async def receive() -> None:
        try:
            intake = await prepare(app, message, work.ident())
            finish(intake, True)
        except (ValueError, OSError) as error:
            app._system(f"Inbox delivery held: {error}")

    app.run_worker(receive(), group="work-mail-intake", exclusive=False, exit_on_error=False)
    return True


async def poll(app: Any) -> list[dict]:
    if work.activation_pending(app):
        return []
    from litetui import approval_relay, tool_approval

    async def before(path: Path, message: dict) -> Intake | None:
        # Approval controls retain their canonical resolver and do not consume
        # prompt capacity or wait behind the producer they must unblock.
        if approval_relay.pending_answer(app, message) is not None or tool_approval.pending_spawner_answer(app, message) is not None:
            return None
        object_id = work.digest(work.encoded({"mail": path.name, "message": message}))
        return await prepare(app, message, object_id)

    def deliver_control(message: dict) -> None:
        if not (approval_relay.take_answer(app, message) or tool_approval.take_spawner_answer(app, message)):
            app._system("Inbox approval answer no longer matches a pending request; retained as a control receipt without starting a prompt.")

    call = None

    def before_claim(path: Path, message: dict) -> Any:
        if call is not None and call.cancelled.is_set():
            return False
        try:
            intake = app.call_from_thread(before, path, message)
        except (ValueError, OSError) as error:
            app.call_from_thread(app._system, f"Inbox left unclaimed: {error}")
            return False
        if intake is None:
            # This exact message was a live approval control at claim time.
            # If its permission expires while the file moves, retain it as a
            # control receipt; it must never turn into an unreserved prompt.
            def resolved(claimed: bool) -> bool:
                if claimed:
                    app.call_from_thread(deliver_control, message)
                return True
            return resolved
        return lambda claimed: app.call_from_thread(finish, intake, claimed)

    # Register the real synchronous callback with the shared lifetime owner.
    # track_tool joins it on cancellation and then re-raises cancellation, so
    # cancelling the monitor cannot abandon a late external mailbox claim.
    try:
        callback, call = work.capture_observer(app, app.seat.poll)
    except ValueError:
        return []  # Recapture/closing never falls back to unowned mailbox I/O.
    try:
        return await work.track_tool(call, asyncio.to_thread(
            callback, prepare=before_claim, max_bytes=RAW_MAIL_BYTES))
    finally:
        await work.finish_tool(call)
        value = work.bridge(app)
        if value is not None and not value.closing and call is not None and call.retired.is_set() and call in value.calls:
            value.calls.remove(call)
