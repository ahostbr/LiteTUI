"""Exact owned catalogue lookup: bounded reads, no adoption or execution."""
from __future__ import annotations

import copy
import json
import os
import stat
import uuid

import pytest
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as _controlled_app
from test_rust_conversation_page import seed

from litetui import conversation_page, gui_rpc, hook_host
from litetui.agent_ownership import AgentSession
from litetui.conversation import ConversationRepository

switch_app = _controlled_app
pytestmark = pytest.mark.asyncio


@pytest.fixture
def app(switch_app, monkeypatch):
    for name in ("_resume", "connect", "_refresh_native_history", "_materialise_convo", "_submit_text"):
        monkeypatch.setattr(switch_app, name, forbidden)
    monkeypatch.setattr(hook_host, "enter_conversation", forbidden)
    monkeypatch.setattr(hook_host, "leave_conversation", forbidden)
    return switch_app


def locate(app, target, **extra):
    return gui_rpc.dispatch(app, {"type": "gui.conversations.page", "session_id": app.convo_id,
                                 "locate_session_id": target, **extra})


def state(app):
    return (disk(app), app.convo_id, app.convo_path, id(app.store), id(app.backend), id(app.client),
            id(app._agent_session), id(app.conversation), copy.deepcopy(app.conversation),
            copy.deepcopy(app._pending_input), copy.deepcopy(vars(app.settings)), gui_rpc._tool_policy(app))


def catalog(app, count=125):
    paths = [app.convo_path] + [seed(app, title=f"Saved row {number}") for number in range(1, count)]
    for index, path in enumerate(paths):
        os.utime(path, (2000 - index, 2000 - index))
    return paths


@pytest.mark.parametrize("index", [49, 50, 99, 100, 124])
async def test_locate_returns_exact_aligned_page_and_reads_only_its_rows(app, monkeypatch, index):
    paths = catalog(app)
    reads = []
    original = ConversationRepository.read

    def read(path, *, max_bytes=None):
        assert max_bytes is not None and max_bytes <= conversation_page.MAX_TRANSCRIPT
        reads.append(path)
        return original(path, max_bytes=max_bytes)

    monkeypatch.setattr(ConversationRepository, "read", read)
    before = state(app)
    target = paths[index].parent.name
    result = locate(app, target)
    offset = index // 50 * 50
    expected = paths[offset:offset + 50]
    assert result["located_session_id"] == target
    assert result["session_id"] == app.convo_id and result["total"] == len(paths)
    assert result["agent_id"] == app._agent_session.authority.agent_id
    assert result["offset"] == offset
    assert [row["session_id"] for row in result["entries"]] == [path.parent.name for path in expected]
    assert reads == expected
    assert (result["previous_cursor"] is not None) is (offset > 0)
    assert (result["next_cursor"] is not None) is (offset + len(expected) < len(paths))
    assert state(app) == before


async def test_locate_explicitly_refreshes_same_uuid_after_insert_and_target_edit(app):
    paths = catalog(app, 102)
    target = paths[49]
    first = locate(app, target.parent.name)
    assert first["offset"] == 0
    added = seed(app, title="new first row")
    os.utime(added, (3000, 3000))
    second = locate(app, target.parent.name)
    assert second["offset"] == 50 and second["snapshot"] != first["snapshot"]
    with target.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"type": "rename", "name": "renamed exact target"}) + "\n")
    os.utime(target, (4000, 4000))
    third = locate(app, target.parent.name)
    assert third["offset"] == 0 and third["snapshot"] != second["snapshot"]
    assert third["entries"][0]["session_id"] == target.parent.name
    assert third["entries"][0]["title"] == "renamed exact target"
    assert first["located_session_id"] == second["located_session_id"] == third["located_session_id"]


@pytest.mark.parametrize("target", [None, True, 42, [], {}, "", "../outside",
                                  "1234", "AAAAAAAA-AAAA-4AAA-8AAA-AAAAAAAAAAAA",
                                  "aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa"])
async def test_malformed_locator_refuses_before_catalog_or_transcript_read(app, monkeypatch, target):
    monkeypatch.setattr(conversation_page, "_catalog", forbidden)
    before = state(app)
    with pytest.raises(ValueError, match="identity|UUID"):
        locate(app, target)
    assert state(app) == before


@pytest.mark.parametrize("cursor", [None, "", "stale-cursor"])
async def test_locator_and_any_cursor_field_refuse_before_read(app, monkeypatch, cursor):
    monkeypatch.setattr(conversation_page, "_catalog", forbidden)
    before = state(app)
    with pytest.raises(ValueError, match="cursor"):
        locate(app, app.convo_id, cursor=cursor)
    assert state(app) == before


@pytest.mark.parametrize("kind", ["missing", "deleted", "foreign"])
async def test_absent_or_foreign_target_never_falls_back_or_replays_neighbors(app, monkeypatch, kind):
    target = str(uuid.uuid4())
    if kind == "deleted":
        path = seed(app)
        target = path.parent.name
        path.unlink()
    elif kind == "foreign":
        owner = AgentSession.create_fresh(
            app._agent_session.store, name="ForeignInspectOwner", agent_id=str(uuid.uuid4()),
            backend="custom", model="old-model", thinking_level="default")
        try:
            target = seed(app, session=owner, title="FOREIGN SECRET").parent.name
        finally:
            owner.release()
    monkeypatch.setattr(ConversationRepository, "read", forbidden)
    before = state(app)
    with pytest.raises(ValueError, match="owned catalog"):
        locate(app, target)
    assert state(app) == before


@pytest.mark.parametrize("kind", ["readonly", "incompatible", "current", "pending-current"])
async def test_locate_does_not_require_open_eligibility_or_materialize_current(app, kind):
    target = app.convo_path if kind == "current" else seed(app)
    mode = target.stat().st_mode
    if kind == "readonly":
        target.chmod(stat.S_IREAD)
    elif kind == "incompatible":
        (target.parent / "settings.json").write_text(
            json.dumps({"execution": {"tools_enabled": True}}), encoding="utf-8")
    elif kind == "pending-current":
        app._stage_new_convo(str(uuid.uuid4()))
        app.conversation = [{"role": "system", "content": "Pending active context"}]
        assert not app.convo_path.exists()
    try:
        before = state(app)
        result = locate(app, target.parent.name)
        row = next(row for row in result["entries"] if row["session_id"] == target.parent.name)
        assert result["located_session_id"] == target.parent.name
        if kind != "pending-current":
            assert not row["can_open"]
        assert state(app) == before
        if kind == "pending-current":
            assert app.store.pending and not app.convo_path.exists()
    finally:
        if kind == "readonly":
            target.chmod(mode)


@pytest.mark.parametrize("fault", ["neighbor-size", "neighbor-identity", "aggregate", "catalog"])
async def test_locator_preserves_page_budgets_and_does_not_skip_bad_neighbors(app, monkeypatch, fault):
    target, neighbor = seed(app), seed(app)
    if fault == "neighbor-size":
        with neighbor.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "rename", "name": "x" * 2000}) + "\n")
        monkeypatch.setattr(conversation_page, "MAX_TRANSCRIPT", target.stat().st_size + 100)
    elif fault == "neighbor-identity":
        with neighbor.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "meta", "id": str(uuid.uuid4())}) + "\n")
    elif fault == "aggregate":
        monkeypatch.setattr(conversation_page, "MAX_PAGE_READ", target.stat().st_size)
    else:
        monkeypatch.setattr(conversation_page, "MAX_ENTRIES", 1)
    before = state(app)
    with pytest.raises(ValueError):
        locate(app, target.parent.name)
    assert state(app) == before


async def test_located_wire_budget_counts_echo_wrapper_and_request_id(app, monkeypatch):
    target = seed(app).parent.name
    result = locate(app, target)
    request_id = "r" * 1000
    frame = {"type": "response", "ok": True, "id": request_id, "result": result}
    size = len(json.dumps(frame, default=str).encode("utf-8"))
    assert len(json.dumps(result).encode("utf-8")) < size - 1
    monkeypatch.setattr(conversation_page, "MAX_RESPONSE", size)
    assert locate(app, target, id=request_id) == result
    monkeypatch.setattr(conversation_page, "MAX_RESPONSE", size - 1)
    with pytest.raises(ValueError, match="response"):
        locate(app, target, id=request_id)


@pytest.mark.parametrize("change", ["target", "owner", "policy"])
async def test_locate_rechecks_catalog_and_authority_before_publication(app, monkeypatch, change):
    target = seed(app)
    owner = app._agent_session
    original = ConversationRepository.read
    changed = False

    def read(path, *, max_bytes=None):
        nonlocal changed
        result = original(path, max_bytes=max_bytes)
        if not changed:
            changed = True
            if change == "target":
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "rename", "name": "concurrent edit"}) + "\n")
            elif change == "owner":
                app._agent_session = None
            else:
                app.settings.tools_enabled = not app.settings.tools_enabled
        return result

    monkeypatch.setattr(ConversationRepository, "read", read)
    try:
        with pytest.raises(ValueError, match="changed|owned session"):
            locate(app, target.parent.name)
    finally:
        app._agent_session = owner


async def test_omitted_locator_preserves_response_and_cursor_semantics(app):
    catalog(app, 53)
    command = {"type": "gui.conversations.page", "session_id": app.convo_id}
    first = gui_rpc.dispatch(app, command)
    assert "located_session_id" not in first
    assert gui_rpc.dispatch(app, {**command, "cursor": None}) == first
    second = gui_rpc.dispatch(app, {**command, "cursor": first["next_cursor"]})
    assert second["offset"] == 50 and "located_session_id" not in second
    assert gui_rpc.dispatch(app, {**command, "cursor": second["previous_cursor"]}) == first
