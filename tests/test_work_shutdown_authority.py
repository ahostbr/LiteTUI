"""Closing Work owns retirement even after request settings become unreadable."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_retired_schedule_authority import changed_owner
from test_rust_background_input import app as _app
from test_rust_background_input import armed as _armed
from test_rust_background_input import command
from test_rust_background_input import switch_app as _switch_app
from test_rust_compaction_settings import reviewed, save, target

from litetui import background_input as work
from litetui import conversation_action, settings_service
from litetui.agent_ownership import OwnershipError
from litetui.agent_store import StoreError
from litetui.llm_backend import BackendError

app, armed, switch_app = _app, _armed, _switch_app
assert Path(work.__file__).resolve() == Path(__file__).resolve().parents[1] / "src/litetui/background_input.py"

pytestmark = pytest.mark.asyncio


async def tear_preference(app, monkeypatch, field):
    value = False if field == "clear_screen_after_compact" else 768
    path = target(app, field)
    original = path.read_bytes()
    review = await reviewed(app, field, value)
    original_write = settings_service._write

    def torn_write(destination, data, **kwargs):
        if destination == path:
            path.write_bytes(b'{"incomplete":')
            raise OSError("Controlled torn preference publication")
        return original_write(destination, data, **kwargs)

    with monkeypatch.context() as changed:
        changed.setattr(settings_service, "_write", torn_write)
        receipt = await save(app, review)
    assert receipt["status"] == "failed" and receipt["close_required"], receipt
    assert path.read_bytes() == b'{"incomplete":'
    return path, original


@pytest.mark.parametrize("field", ["clear_screen_after_compact", "compact_max_tokens"])
@pytest.mark.parametrize("route", ["work", "quit"])
async def test_torn_preference_joins_exact_work_owner_without_reopening_execution(armed, monkeypatch, field, route):
    app = armed
    value = work.bridge(app)
    projection = app._owned_effort_projection
    authority = app._agent_session.authority
    transcript = app.convo_path.read_bytes()
    path, original = await tear_preference(app, monkeypatch, field)
    try:
        before = value.raw, value.revision
        for validate in (app._validate_owned_identity, value.check, value.persist):
            with pytest.raises(json.JSONDecodeError):
                validate()
        with pytest.raises((ValueError, json.JSONDecodeError)):
            work.capture(app, command(app))
        with pytest.raises(ValueError, match="closing task"):
            value._finish_shutdown(value.close_event)
        assert (value.raw, value.revision) == before
        if route == "quit":
            replies, exits = [], []
            monkeypatch.setattr(app, "exit", lambda: exits.append(True))
            assert conversation_action.request_shutdown(app, lambda **reply: replies.append(reply))
            await asyncio.wait_for(app._conversation_rewrite_exit_task, 3)
            assert len(replies) == 1 and replies[0]["ok"], replies
            assert exits == [True]
            proof = replies[0]["result"]["work_shutdown"]
        else:
            proof = await asyncio.wait_for(work.settle_before_shutdown(app), 3)
        assert proof["worker_settled"] and proof["process_settled"] and proof["callback_settled"]
        assert proof["admission_closed"] and not proof["effects_verified"]
        assert proof["ledger_sha256"] == work.digest(value.path.read_bytes())
        assert value.path.read_bytes() == value.raw
        assert path.read_bytes() == b'{"incomplete":'
        assert app._owned_effort_projection is projection
        assert app._agent_session.authority == authority
        assert app.convo_path.read_bytes() == transcript
        assert not app._rewrite_test_requests
        with pytest.raises(json.JSONDecodeError):
            app._validate_owned_identity()
        with pytest.raises((ValueError, json.JSONDecodeError)):
            app._validate_owned_execution()
        with pytest.raises(ValueError, match="closing task"):
            value._finish_shutdown(value.close_event)
    finally:
        path.write_bytes(original)


@pytest.mark.parametrize("kind", [
    "session", "backend", "model", "base", "settings-base", "invocation",
    "missing-invocation", "invalid-invocation", "changed-invocation",
    "store-object", "store-session", "store-path", "lease", "disk-authority",
])
async def test_shutdown_still_requires_exact_retained_authority(armed, monkeypatch, kind):
    value = work.bridge(armed)
    before = value.raw, value.revision
    with changed_owner(armed, monkeypatch, kind), pytest.raises((ValueError, BackendError, OwnershipError)):
        await asyncio.wait_for(work.settle_before_shutdown(armed), 3)
    assert (value.raw, value.revision) == before
    assert not armed._rewrite_test_requests


@pytest.mark.parametrize("kind", ["foreign-ledger", "changed-path", "released-store", "lease-path"])
async def test_shutdown_cannot_publish_over_unowned_storage(armed, monkeypatch, kind):
    value = work.bridge(armed)
    before = value.raw, value.revision
    original = value.path.read_bytes() if value.path.exists() else None
    path = value.path
    with monkeypatch.context() as changed:
        if kind == "foreign-ledger":
            path.write_bytes(b'{"foreign":true}')
        elif kind == "changed-path":
            changed.setattr(value, "path", path.with_name("foreign.json"))
        elif kind == "lease-path":
            changed.setattr(armed.store._lease, "path", path.with_name("foreign.lease"))
        else:
            changed.setattr(armed.store, "_lease", None)
        try:
            with pytest.raises((ValueError, StoreError)):
                await asyncio.wait_for(work.settle_before_shutdown(armed), 3)
            assert (value.raw, value.revision) == before
            if kind == "foreign-ledger":
                assert path.read_bytes() == b'{"foreign":true}'
            elif kind == "changed-path":
                assert not value.path.exists()
        finally:
            if kind == "foreign-ledger":
                if original is None:
                    path.unlink()
                else:
                    path.write_bytes(original)


async def test_quiet_transition_still_requires_request_projection(armed, monkeypatch):
    value = work.bridge(armed)
    path, original = await tear_preference(armed, monkeypatch, "clear_screen_after_compact")
    try:
        with pytest.raises(json.JSONDecodeError):
            await work.before_identity_transition(armed)
        assert not value.closing and value.shutdown_task is None
    finally:
        path.write_bytes(original)
