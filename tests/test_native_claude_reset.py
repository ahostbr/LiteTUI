"""Exact reset review, saved state and retained cleanup; no provider or device access."""
from __future__ import annotations

import asyncio
import copy
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from test_rust_background_input import app as _app
from test_rust_background_input import command
from test_rust_background_input import switch_app as _switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import claude_turn, gui_rpc, paths, seat_authority
from litetui import native_claude_reset as reset
from litetui import native_runtime_controls as controls
from litetui.claude_backend import ClaudeBackend
from litetui.claude_persistence import path_for

app, switch_app = _app, _switch_app
pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def ready(app, monkeypatch, tmp_path, request):
    if getattr(request, "param", False):
        app._stage_new_convo(str(uuid4()))
        app.conversation = [{"role": "system", "content": "Exact pending system"}]
        app._hook_conversation_id = None
    app.backend = ClaudeBackend(app.settings)
    app._agent_session.update_execution(backend="claude", model=app.model_id, thinking_level="default")
    app._launch_options.work_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app.settings.mcp_enabled = False
    app._cli_tool_profile = app._active_tool_profile = "strict"
    monkeypatch.setattr(paths, "PLAN_PROMPT_FILE", tmp_path / "PLAN.md")
    monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    capture = await published_rpc(app, monkeypatch, command(app, type="gui.work.native_capture", execution_version=1, runtime_controls_version=1))
    await published_rpc(app, monkeypatch, command(app, type="gui.work.native_activate", instance=capture["instance"],
        activation_token=capture["activation_token"], execution_version=1, runtime_controls_version=1, scheduled_transition_version=1))
    yield app
    attempt = getattr(app, "_native_claude_reset", None)
    if attempt is not None and attempt.session is not None:
        attempt.session.release.set()
        attempt.session.lifecycle.cleanup_errors.clear()
    await reset.retire(app)
    if app.backend.session is not None:
        await app.backend.close()


def cmd(app, phase, **fields):
    return {"type": reset.PREFIX + phase, "id": "reset-" + phase, "version": 1,
            **work.identity(app), "work_instance": work.bridge(app).instance, **fields}


class Session:
    def __init__(self):
        self.release = asyncio.Event()
        self.release.set()
        self.closed = asyncio.Event()
        self._owner = asyncio.create_task(self.closed.wait())
        self.lifecycle = SimpleNamespace(cleanup_errors=[], generation=1, active_turn=None)
        self.calls = 0
    async def close(self):
        self.calls += 1
        await self.release.wait()
        self.closed.set()
        await self._owner


def establish(app, held=True):
    ledger = claude_turn.ledger_for(app)
    old = ledger.select_segment(claude_turn.launch_workspace(app))
    ledger.bind_session(old["id"], str(uuid4()))
    if held:
        entry = ledger.prepare(old["id"], "Retain this exact held input", "strict", "typed")
        app._pending_input.append({"content": entry["content"], "_claude_entry": entry,
                                   "_claude_segment": old["id"], "_claude_conversation": app.convo_id})
    session = Session()
    session.session_id = ledger.selected["session_id"]
    app.backend.session, app.backend.segment_id = session, old["id"]
    return ledger, session


@pytest.mark.parametrize("ready", [False, True], indirect=True)
async def test_pure_review_cancel_and_exact_reset_keep_conversation_and_publication(ready):
    app = ready
    if not app.store.pending:
        ledger, session = establish(app)
    else:
        ledger, session = None, None
    identity, instance = work.identity(app), work.bridge(app).instance
    history, pending, authority = copy.deepcopy(app.conversation), copy.deepcopy(app._pending_input), app._agent_session.authority
    before = work._read(path_for(app.convo_dir), 8 * 1024 * 1024)
    transcript = work._read(app.convo_path, 8 * 1024 * 1024)
    review = await gui_rpc.async_dispatch(app, cmd(app, "review"))
    assert work._read(path_for(app.convo_dir), 8 * 1024 * 1024) == before
    assert work._read(app.convo_path, 8 * 1024 * 1024) == transcript
    assert not reset.blocking(app) and (session is None or session.calls == 0)
    # Closing a pure review changes nothing and sends nothing.
    await reset.retire(app)
    assert work._read(path_for(app.convo_dir), 8 * 1024 * 1024) == before
    review = await gui_rpc.async_dispatch(app, cmd(app, "review"))
    old = copy.deepcopy(ledger.document) if ledger else {"segments": {}}
    applied = await gui_rpc.async_dispatch(app, cmd(app, "apply", token=review["token"]))
    assert applied["cleanup_verified"] and reset.blocking(app) and controls.mutation_hold(app)
    assert work.identity(app) == identity and work.bridge(app).instance == instance
    assert app.conversation == history and app._pending_input == pending and app._agent_session.authority is authority
    assert app.backend.session is None and (session is None or session._owner.done())
    after = app._claude_ledger.document
    assert after["selected_segment"] == review["review"]["target"]
    assert {k:v for k,v in after["segments"].items() if k != after["selected_segment"]} == old["segments"]
    request = cmd(app, "readback", token=review["token"], apply_id=applied["apply_id"])
    readback = await gui_rpc.async_dispatch(app, request)
    ticket = work.prepare_publication(app, request, readback)
    assert reset.blocking(app)
    work.finish_publication(app, ticket)
    assert not reset.blocking(app) and not app._rewrite_test_requests
    with pytest.raises(ValueError, match="consumed|foreign"):
        work.finish_publication(app, ticket)


@pytest.mark.parametrize("change", ["history", "ledger", "queue", "authority", "session", "policy", "binding", "token", "generation", "ledger-owner"])
async def test_stale_review_cannot_close_or_select(ready, change):
    app = ready
    ledger, session = establish(app)
    review = await reset.dispatch(app, cmd(app, "review"))
    request = cmd(app, "apply", token=review["token"])
    if change == "history": app.conversation.append({"role":"user","content":"changed"})
    elif change == "ledger": ledger.select_segment(claude_turn.launch_workspace(app), new=True)
    elif change == "ledger-owner":
        from litetui.claude_persistence import ClaudeLedger
        app._claude_ledger = ClaudeLedger(app.convo_dir)
    elif change == "queue": app._pending_input.append({"content":"changed"})
    elif change == "authority": app._agent_session.update_execution(backend="claude", model="different", thinking_level="default")
    elif change == "session": app.backend.segment_id = "f" * 32
    elif change == "policy": app.settings.tool_deny.append("changed")
    elif change == "generation": session.lifecycle.generation += 1
    elif change == "binding": request["work_instance"] = "foreign"
    else: request["token"] = "f" * 64
    with pytest.raises((ValueError, RuntimeError)):
        await reset.dispatch(app, request)
    assert session.calls == 0 and not reset.blocking(app)


async def test_slow_close_holds_admission_and_quit_joins_original_without_selecting(ready):
    app = ready
    ledger, session = establish(app)
    before = ledger.document
    review = await reset.dispatch(app, cmd(app, "review"))
    session.release.clear()
    applying = asyncio.create_task(reset.dispatch(app, cmd(app, "apply", token=review["token"])))
    while session.calls == 0: await asyncio.sleep(0)
    assert reset.blocking(app) and work.prompt_refusal(app)
    applying.cancel()
    cleanup = asyncio.create_task(reset.retire(app))
    await asyncio.sleep(0.01)
    assert not cleanup.done() and not applying.done() and app._native_claude_reset.session is session
    session.release.set()
    result = await asyncio.gather(applying, return_exceptions=True)
    await cleanup
    assert isinstance(result[0], RuntimeError) and ledger.document == before and session._owner.done()


async def test_failed_close_retains_original_sdk_handle_and_does_not_select(ready):
    app = ready
    ledger, session = establish(app)
    before = ledger.document
    review = await reset.dispatch(app, cmd(app, "review"))
    session.lifecycle.cleanup_errors.append("controlled SDK retirement failure")
    with pytest.raises(Exception, match="cleanup"):
        await reset.dispatch(app, cmd(app, "apply", token=review["token"]))
    assert app.backend.session is None and app._native_claude_reset.session is session
    assert ledger.document == before and reset.blocking(app)
    with pytest.raises(RuntimeError, match="cleanup"):
        await reset.retire(app)
    assert app._native_claude_reset.session is session
    session.lifecycle.cleanup_errors.clear()


@pytest.mark.parametrize("tamper", ["result", "ledger", "duplicate_apply", "wrong_apply_id"])
async def test_independent_readback_and_publication_refuse_changed_evidence(ready, tamper):
    app = ready
    ledger, _session = establish(app)
    review = await reset.dispatch(app, cmd(app, "review"))
    applied = await reset.dispatch(app, cmd(app, "apply", token=review["token"]))
    if tamper == "duplicate_apply":
        with pytest.raises(ValueError): await reset.dispatch(app, cmd(app, "apply", token=review["token"]))
    else:
        request = cmd(app, "readback", token=review["token"], apply_id="foreign" if tamper == "wrong_apply_id" else applied["apply_id"])
        if tamper == "ledger": ledger.select_segment(claude_turn.launch_workspace(app), new=True)
        if tamper in {"ledger", "wrong_apply_id"}:
            with pytest.raises(ValueError): await reset.dispatch(app, request)
        else:
            saved = await reset.dispatch(app, request)
            ticket = work.prepare_publication(app, request, saved)
            saved["ledger_after"] = "f" * 64
            with pytest.raises(ValueError): work.finish_publication(app, ticket)
    assert reset.blocking(app)


async def test_actual_rpc_readback_emission_is_the_admission_release(ready, monkeypatch):
    app = ready
    establish(app)
    review = await published_rpc(app, monkeypatch, cmd(app, "review"))
    applied = await published_rpc(app, monkeypatch, cmd(app, "apply", token=review["token"]))
    assert reset.blocking(app)
    saved = await published_rpc(app, monkeypatch, cmd(app, "readback", token=review["token"], apply_id=applied["apply_id"]),
                                before_emit=lambda _row: (reset.blocking(app) or pytest.fail("released before readback emission")))
    assert saved["cleanup_verified"] and not reset.blocking(app)


async def test_ledger_save_failure_retains_closed_owner_without_replay(ready, monkeypatch):
    from litetui.claude_persistence import ClaudeLedger
    app = ready
    ledger, session = establish(app)
    raw = ledger.file.read_bytes()
    review = await reset.dispatch(app, cmd(app, "review"))
    def fail(_ledger): raise OSError("Controlled ledger write failure")
    monkeypatch.setattr(ClaudeLedger, "_save", fail)
    with pytest.raises(OSError, match="write failure"):
        await reset.dispatch(app, cmd(app, "apply", token=review["token"]))
    assert ledger.file.read_bytes() == raw and session._owner.done()
    assert reset.blocking(app) and app._native_claude_reset.session is session
    assert not app._rewrite_test_requests


async def test_reset_refuses_mismatched_live_and_saved_claude_sessions(ready):
    app = ready
    _ledger, session = establish(app)
    session.session_id = str(uuid4())
    with pytest.raises(ValueError, match="differs"):
        await reset.dispatch(app, cmd(app, "review"))
    assert session.calls == 0
