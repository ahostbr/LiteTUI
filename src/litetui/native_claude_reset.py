"""Reviewed Claude-session reset under the unchanged owned LiteTUI conversation."""
from __future__ import annotations

import asyncio
import copy
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING

from litetui import background_input as work
from litetui import (
    claude_turn,
    gui_rpc,
    native_context,
    native_history,
    restored_claude,
)
from litetui import native_runtime_controls as controls
from litetui.claude_backend import settle_close
from litetui.claude_persistence import path_for
from litetui.shutdown_wait import join_task

if TYPE_CHECKING:
    from litetui.claude_session import ClaudeSession

PREFIX = "gui.claude.native_reset_"
OPERATIONS = tuple(PREFIX + name for name in ("review", "apply", "readback"))


@dataclass
class Attempt:
    binding: dict
    owner: tuple
    ledger: object
    before: dict
    raw: bytes | None
    transcript: bytes | None
    history: bytes
    pending: bytes
    context: bytes
    session: ClaudeSession | None
    session_owner: object
    session_facts: tuple
    segment: str | None
    token: str
    target: str
    review: dict
    task: asyncio.Task | None = None
    applied: str | None = None
    after: dict | None = None
    closing: bool = False
    error: str | None = None
    command: bytes | None = None
    result: dict | None = None
    result_raw: bytes | None = None
    reader: asyncio.Task | None = None
    publication: object = None


def _owner(app):
    from litetui.plugins.convo import _cmd_claude
    entry = app.plugins.commands.get("/claude")
    if (app.backend.name != "claude" or not getattr(app.backend, "owns_native_turns", False)
            or entry is None or entry.owner != "convo" or entry.handler is not _cmd_claude):
        raise ValueError("Claude reset requires the exact registered builtin Claude command")
    return app._agent_session, app.store, app.backend, app.settings, app.conversation, entry


def _ledger(app):
    disk = restored_claude._read(app.store)
    live = getattr(app, "_claude_ledger", None)
    if live is not None and (getattr(app, "_claude_ledger_path", None) != app.convo_dir
                            or live.document != disk.document):
        raise ValueError("Claude ledger changed outside its captured owner")
    return disk.document, work._read(path_for(app.convo_dir), 8 * 1024 * 1024)


def _session_facts(session):
    if session is None:
        return ()
    return (session.session_id, getattr(session.lifecycle, "generation", None),
            getattr(session.lifecycle, "active_turn", None), str(getattr(getattr(session, "options", None), "cwd", "")))


def _same(app, attempt, *, before=False, closing=False):
    bridge = work.bridge(app)
    if (bridge is None or bridge.instance != attempt.binding["work_instance"]
            or any(a is not b for a, b in zip(_owner(app), attempt.owner, strict=True))
            or work.identity(app) != {k: attempt.binding[k] for k in work.identity(app)}
            or work.encoded(app.conversation) != attempt.history
            or work.encoded(app._pending_input) != attempt.pending
            or controls._context(app) != attempt.context
            or getattr(app, "_claude_ledger", None) is not attempt.ledger):
        raise ValueError("Claude reset lost its exact conversation, held inputs or authority")
    bridge.check(allow_closing=closing)
    app._validate_owned_identity()
    if attempt.ledger is not None and (getattr(app, "_claude_ledger_path", None) != app.convo_dir
                                       or attempt.ledger.dir != app.convo_dir
                                       or attempt.ledger.file != path_for(app.convo_dir)):
        raise ValueError("Claude reset ledger owner changed its conversation path")
    if before:
        document, raw = _ledger(app)
        if (document != attempt.before or raw != attempt.raw
                or work._read(app.convo_path, native_history.MAX_TRANSCRIPT) != attempt.transcript
                or app.backend.session is not attempt.session
                or app.backend.segment_id != attempt.segment
                or getattr(attempt.session, "_owner", None) is not attempt.session_owner
                or _session_facts(attempt.session) != attempt.session_facts):
            raise ValueError("Claude session or saved history changed since review")


def _quiet(app):
    gui_rpc._idle(app)
    if why := controls.mutation_hold(app):
        raise ValueError(why)
    state = controls._state(app)
    if state is None:
        raise ValueError("Claude reset requires published native runtime controls")
    controls._guard(app, state)
    if any(not worker.is_finished for worker, _origin in work.bridge(app).executions.values()):
        raise ValueError("Wait for the exact prior turn to settle before resetting Claude")
    if any(producer.phase != "retired" for producer in work.bridge(app).producers.values()):
        raise ValueError("Wait for owned background work before resetting Claude")
    if any(row["kind"] != "management" for row in gui_rpc.active_work(app)):
        raise ValueError("Wait for owned work before resetting Claude")
    if getattr(app, "_claude_compact_resume", None) is not None:
        raise ValueError("Resolve the pending Claude continuation before resetting its session")


def blocking(app):
    attempt = getattr(app, "_native_claude_reset", None)
    return isinstance(attempt, Attempt) and attempt.applied is not None


def readback_allowed(app, cmd):
    attempt = getattr(app, "_native_claude_reset", None)
    return (isinstance(attempt, Attempt) and cmd.get("type") == PREFIX + "readback"
            and cmd.get("token") == attempt.token and cmd.get("apply_id") == attempt.applied)


def _reply(attempt, kind):
    return {"version": 1, **attempt.binding, "kind": kind, "token": attempt.token,
            "review": copy.deepcopy(attempt.review), "apply_id": attempt.applied,
            "ledger_after": None if attempt.after is None else work.digest(work.encoded(attempt.after)),
            "cleanup_verified": attempt.task is not None and attempt.task.done() and not attempt.error}


def _closed(attempt):
    session = attempt.session
    if session is not None:
        owner = getattr(session, "_owner", None)
        if owner is not attempt.session_owner or owner is not None and not owner.done():
            raise RuntimeError("The exact Claude SDK owner is still running")
        if session.lifecycle.cleanup_errors:
            raise RuntimeError("Claude SDK cleanup failed; the original session remains retained")


async def _apply(app, attempt):
    try:
        failures = await settle_close(app)
        if failures or getattr(app, "_claude_closing", None):
            raise RuntimeError("Previous Claude cleanup did not settle: " + "; ".join(failures))
        _same(app, attempt, before=True, closing=attempt.closing)
        await app.backend.close()
        _closed(attempt)
        _same(app, attempt, closing=attempt.closing)
        if attempt.closing or getattr(app, "_gui_quitting", False) or work.bridge(app).closing:
            raise RuntimeError("Claude reset stopped by Quit before selecting a new segment")
        document, raw = _ledger(app)
        if document != attempt.before or raw != attempt.raw:
            raise ValueError("Claude ledger changed during SDK retirement")
        ledger = claude_turn.ledger_for(app)
        attempt.ledger = ledger
        segment = ledger.select_segment(claude_turn.launch_workspace(app), new=True, segment_id=attempt.target)
        expected = copy.deepcopy(attempt.before)
        expected["selected_segment"] = attempt.target
        expected["segments"][attempt.target] = segment
        if (segment["session_id"] is not None or segment["entries"] or ledger.document != expected
                or work.strict_json(work._read(ledger.file, 8 * 1024 * 1024)) != expected):
            raise ValueError("New Claude segment or retained deliveries failed independent readback")
        attempt.after = expected
        _verify(app, attempt)
    except BaseException as exc:
        attempt.error = str(exc) or type(exc).__name__
        raise


def _verify(app, attempt):
    _same(app, attempt)
    _closed(attempt)
    if (attempt.after is None or app.backend.session is not None or app.backend.segment_id is not None
            or getattr(app, "_claude_closing", None)
            or work.strict_json(work._read(path_for(app.convo_dir), 8 * 1024 * 1024)) != attempt.after
            or app._claude_ledger.document != attempt.after
            or app._claude_ledger.selected["id"] != attempt.target):
        raise ValueError("Claude reset lacks the exact saved segment and retired runtime")
    if attempt.transcript is not None:
        if work._read(app.convo_path, native_history.MAX_TRANSCRIPT) != attempt.transcript:
            raise ValueError("Claude reset changed saved conversation history")
    else:
        saved = native_history.capture_file(app.convo_path)
        if saved.meta.get("id") != app.convo_id or work.encoded(saved.messages) != attempt.history:
            raise ValueError("Claude reset materialized another conversation or history")


async def dispatch(app, cmd):
    action = cmd["type"].removeprefix(PREFIX)
    fields = {"review": (), "apply": ("token",), "readback": ("token", "apply_id")}
    _bridge, binding = native_context.read_binding(app, cmd, PREFIX + action, *fields[action])
    if action == "review":
        _quiet(app)
        before, raw = _ledger(app)
        session = app.backend.session
        token, target = secrets.token_hex(32), secrets.token_hex(16)
        selected = before["segments"].get(before["selected_segment"] or "")
        if session is not None and (selected is None or app.backend.segment_id != selected["id"]
                                    or session.session_id != selected["session_id"]):
            raise ValueError("Claude SDK session differs from the selected saved segment")
        review = {"segment": None if selected is None else selected["id"],
                  "native_session": None if selected is None else selected["session_id"],
                  "target": target, "workspace": claude_turn.launch_workspace(app),
                  "held_inputs": sum(entry["state"] != "terminal" for segment in before["segments"].values() for entry in segment["entries"]),
                  "segments": len(before["segments"]), "materialize": bool(app.store.pending),
                  "history_sha256": work.digest(work.encoded(app.conversation)),
                  "ledger_before": work.digest(work.encoded(before))}
        if len(work.encoded(review)) > 8192:
            raise ValueError("Claude reset review exceeds its display budget")
        attempt: Attempt | None = Attempt(binding, _owner(app), getattr(app, "_claude_ledger", None), before, raw,
                          work._read(app.convo_path, native_history.MAX_TRANSCRIPT),
                          work.encoded(app.conversation), work.encoded(app._pending_input), controls._context(app),
                          session, getattr(session, "_owner", None), _session_facts(session), app.backend.segment_id, token, target, review)
        app._native_claude_reset = attempt
        return _reply(attempt, "claude_reset_review")
    attempt = getattr(app, "_native_claude_reset", None)
    if not isinstance(attempt, Attempt) or cmd["token"] != attempt.token or binding != attempt.binding:
        raise ValueError("Review the current Claude session again")
    if action == "apply":
        _quiet(app)
        _same(app, attempt, before=True)
        if attempt.applied is not None:
            raise ValueError("Claude reset was already attempted; it cannot be replayed")
        attempt.applied = cmd["id"]
        attempt.task = asyncio.create_task(_apply(app, attempt), name="native-claude-reset")
        await join_task(attempt.task)
        _verify(app, attempt)
        return _reply(attempt, "claude_reset_applied")
    if attempt.applied is None or cmd["apply_id"] != attempt.applied or attempt.error:
        raise ValueError("Claude reset did not settle; close this runtime to inspect it")
    _verify(app, attempt)
    attempt.reader, attempt.command = asyncio.current_task(), work.encoded(cmd)
    attempt.result = _reply(attempt, "claude_reset_saved")
    attempt.result_raw = work.encoded(attempt.result)
    return attempt.result


async def retire(app):
    attempt = getattr(app, "_native_claude_reset", None)
    if not isinstance(attempt, Attempt):
        return
    attempt.closing = True
    if attempt.task is not None:
        try:
            await join_task(attempt.task)
        except Exception as exc:  # noqa: BLE001 - retain the failure and still prove exact SDK cleanup
            attempt.error = str(exc) or type(exc).__name__
        if attempt.session is not None:
            await attempt.session.close()
        _closed(attempt)
    app._native_claude_reset = None


@dataclass
class Publication:
    attempt: Attempt
    used: bool = False


def prepare_publication(app, cmd, result):
    if cmd.get("type") != PREFIX + "readback":
        return None
    attempt = getattr(app, "_native_claude_reset", None)
    if (not isinstance(attempt, Attempt) or attempt.reader is not asyncio.current_task()
            or attempt.command != work.encoded(cmd) or attempt.result is not result
            or attempt.result_raw != work.encoded(result) or attempt.publication is not None):
        raise ValueError("Claude reset lost its exact readback response owner")
    _verify(app, attempt)
    ticket = Publication(attempt)
    attempt.publication = ticket
    return ticket


def finish_publication(app, ticket):
    attempt = getattr(app, "_native_claude_reset", None)
    if (not isinstance(ticket, Publication) or ticket.used or ticket.attempt is not attempt
            or attempt.publication is not ticket or attempt.reader is not asyncio.current_task()
            or work.encoded(attempt.result) != attempt.result_raw):
        raise ValueError("Claude reset publication is foreign, changed or consumed")
    _verify(app, attempt)
    ticket.used = True
    app._native_claude_reset = None
    controls.signal_stop(app)
