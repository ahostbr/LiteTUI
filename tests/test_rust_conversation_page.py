"""Bounded native conversation discovery on actual owned stores; no providers."""
from __future__ import annotations

import base64
import json
import os
import stat
import uuid
from pathlib import Path

import pytest
import test_rust_conversation_open

from litetui import conversation_page, gui_rpc, store_identity
from litetui.agent_ownership import AgentSession
from litetui.conversation import ConversationRepository

saved_app = test_rust_conversation_open.saved_app
ConnectionGate = test_rust_conversation_open.ConnectionGate


def seed(app, *, ident=None, title=None, session=None):
    owner = session or app._agent_session
    repository = ConversationRepository(agent_session=owner)
    repository.stage(ident or str(uuid.uuid4()))
    repository.acquire()
    repository.pending = False
    repository.record_meta(app.model_id, owner.authority.name, owner.authority.agent_id)
    repository.record_snapshot(
        [{"role": "system", "content": "Fixture system"}, {"role": "user", "content": "Saved browser prompt"}],
        store_identity=store_identity.capture(app), store_injected=True)
    if title is not None:
        repository.write_record({"type": "rename", "name": title})
    path = repository.convo_path
    assert path is not None and path.is_file() and not repository.persist_error
    repository.release()
    return path


def page(app, cursor=None):
    return gui_rpc.dispatch(app, {"type": "gui.conversations.page", "session_id": app.convo_id,
                                  "cursor": cursor})


def opening(app, result, target):
    return {"type": "gui.conversations.open", "id": "open-owned", "session_id": target,
            "from_session_id": app.convo_id, "snapshot": result["snapshot"]}


@pytest.mark.asyncio
async def test_owned_page_uses_authoritative_titles_without_writes_or_foreign_rows(saved_app, monkeypatch):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, ident="11111111-2222-1333-8444-555555555555", title="Named saved conversation")
    foreign = AgentSession.create_fresh(
        app._agent_session.store, name="ForeignBrowserOwner", agent_id=str(uuid.uuid4()),
        backend="codex", model="fixture-model", thinking_level="low")
    try:
        foreign_path = seed(app, title="FOREIGN SECRET", session=foreign)
    finally:
        foreign.release()
    malformed = target.parent.parent / "not-a-canonical-uuid"
    malformed.mkdir()
    (malformed / "convo.jsonl").write_text("untrusted", encoding="utf-8")
    def tree():
        return {str(path): (path.stat().st_size, path.stat().st_mtime_ns)
                if path.name.endswith(".lease") else path.read_bytes()
                for path in app._agent_session.store.data_root.rglob("*") if path.is_file()}
    before = tree()
    result = page(app)
    assert result["session_id"] == app.convo_id
    assert result["agent_id"] == app._agent_session.authority.agent_id
    assert len(result["snapshot"]) == 64 and result["total"] == 2 and result["offset"] == 0
    assert result["previous_cursor"] is result["next_cursor"] is None
    current = next(row for row in result["entries"] if row["current"])
    assert current["session_id"] == app.convo_id
    assert not current["can_open"] and current["reason"] == "Already open"
    row = next(row for row in result["entries"] if not row["current"])
    assert row["session_id"] == target.parent.name and row["title"] == "Named saved conversation"
    assert row["message_count"] == 2 and row["updated_at"] >= 0
    assert row["can_open"] and row["reason"] == ""
    assert row["agent_id"] == result["agent_id"] and row["agent_name"] == app._agent_session.authority.name
    assert foreign_path.parent.name not in json.dumps(result)
    assert "FOREIGN SECRET" not in json.dumps(result)
    assert before == tree()


@pytest.mark.asyncio
async def test_pages_replay_only_fifty_transcripts_and_roundtrip_opaque_cursors(saved_app, monkeypatch):
    app = saved_app
    app._hold_restored_input = True
    for index in range(52):
        path = seed(app, title=f"Saved {index}")
        os.utime(path, (1000 + index, 1000 + index))
    original = ConversationRepository.read
    reads = []
    def read(path, *, max_bytes=None):
        assert max_bytes is not None and max_bytes <= conversation_page.MAX_TRANSCRIPT
        reads.append(path)
        return original(path, max_bytes=max_bytes)
    monkeypatch.setattr(ConversationRepository, "read", read)
    first = page(app)
    assert first["total"] == 53 and len(first["entries"]) == len(reads) == 50
    second = page(app, first["next_cursor"])
    assert second["offset"] == 50 and len(second["entries"]) == 3 and second["next_cursor"] is None
    assert second["snapshot"] == first["snapshot"]
    assert len(reads) == 53
    again = page(app, second["previous_cursor"])
    assert again == first
    assert len({row["session_id"] for row in first["entries"] + second["entries"]}) == 53


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["scope", "agent", "snapshot", "negative", "fraction", "beyond", "noncanonical", "oversize"])
async def test_forged_or_out_of_scope_cursor_is_rejected(saved_app, fault):
    app = saved_app
    result = page(app)
    token = conversation_page._cursor(app.convo_id, result["agent_id"], result["snapshot"], 0)
    value = json.loads(base64.urlsafe_b64decode(token))
    if fault == "scope":
        value["session_id"] = str(uuid.uuid4())
    elif fault == "agent":
        value["agent_id"] = str(uuid.uuid4())
    elif fault == "snapshot":
        value["snapshot"] = "a" * 64
    elif fault == "negative":
        value["offset"] = -50
    elif fault == "fraction":
        value["offset"] = 0.5
    elif fault == "beyond":
        value["offset"] = 50
    elif fault == "noncanonical":
        value["extra"] = True
    else:
        token = "x" * 1025
    if fault != "oversize":
        token = base64.urlsafe_b64encode(conversation_page._encoded(value)).decode("ascii")
    with pytest.raises(ValueError):
        page(app, token)


@pytest.mark.asyncio
async def test_cursor_rejects_catalog_change_and_refresh_starts_a_new_snapshot(saved_app):
    app = saved_app
    result = page(app)
    cursor = conversation_page._cursor(app.convo_id, result["agent_id"], result["snapshot"], 0)
    seed(app, title="added later")
    with pytest.raises(ValueError, match="stale"):
        page(app, cursor)
    refreshed = page(app)
    assert refreshed["snapshot"] != result["snapshot"] and refreshed["total"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["same-file", "another-file"])
async def test_catalog_change_during_page_read_is_not_published(saved_app, monkeypatch, changed):
    app = saved_app
    target = seed(app, title="target")
    original = ConversationRepository.read
    calls: list = []
    def read(path, *, max_bytes=None):
        result = original(path, max_bytes=max_bytes)
        if not calls:
            mutation = path if changed == "same-file" else (target if path != target else app.convo_path)
            with mutation.open("ab") as output:
                output.write(b'{"type":"rename","name":"changed"}\n')
        calls.append(path)
        return result
    monkeypatch.setattr(ConversationRepository, "read", read)
    with pytest.raises(ValueError, match="changed"):
        page(app)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["session", "owner", "busy", "loading", "persistence", "read-only", "path"])
async def test_catalog_requires_current_owned_idle_binding(saved_app, monkeypatch, fault):
    app = saved_app
    owner = app._agent_session
    command = {"type": "gui.conversations.page", "session_id": app.convo_id}
    if fault == "session":
        command["session_id"] = str(uuid.uuid4())
    elif fault == "owner":
        app._agent_session = None
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "loading":
        app.store.loading = True
    elif fault == "persistence":
        app.store.persist_error = "controlled storage failure"
    elif fault == "path":
        app.store.convo_path = app.convo_path.parent / "foreign.jsonl"
    else:
        app.store.release()
    try:
        with pytest.raises((ValueError, OSError)):
            gui_rpc.dispatch(app, command)
    finally:
        app._agent_session = owner


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["catalog", "transcript", "page-read", "response", "title", "identity"])
async def test_explicit_budgets_and_bad_transcripts_fail_without_truncation(saved_app, monkeypatch, fault):
    app = saved_app
    target = seed(app, title="target")
    if fault == "catalog":
        monkeypatch.setattr(conversation_page, "MAX_ENTRIES", 1)
    elif fault == "transcript":
        monkeypatch.setattr(conversation_page, "MAX_TRANSCRIPT", 10)
    elif fault == "page-read":
        monkeypatch.setattr(conversation_page, "MAX_PAGE_READ", max(target.stat().st_size, app.convo_path.stat().st_size))
    elif fault == "response":
        monkeypatch.setattr(conversation_page, "MAX_RESPONSE", 100)
    elif fault == "title":
        with target.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"type": "rename", "name": "x" * 4097}) + "\n")
    else:
        with target.open("a", encoding="utf-8") as output:
            output.write(json.dumps({"type": "meta", "id": str(uuid.uuid4())}) + "\n")
    with pytest.raises(ValueError):
        page(app)


@pytest.mark.asyncio
async def test_file_readonly_and_missing_hold_policy_are_visible_disabled_choices(saved_app):
    app = saved_app
    target = seed(app, title="read-only target")
    row = next(row for row in page(app)["entries"] if row["session_id"] == target.parent.name)
    assert not row["can_open"] and "holds" in row["reason"]
    app._hold_restored_input = True
    mode = target.stat().st_mode
    try:
        target.chmod(stat.S_IREAD)
        row = next(row for row in page(app)["entries"] if row["session_id"] == target.parent.name)
        assert not row["can_open"] and "read-only" in row["reason"]
    finally:
        target.chmod(mode)


@pytest.mark.asyncio
async def test_fresh_browser_selection_uses_existing_real_open_and_connection_barrier(saved_app):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="open destination")
    result = page(app)
    command = opening(app, result, target.parent.name)
    gate = ConnectionGate(app, native=True)
    app.connect = gate.connect
    gate.release.set()
    gate.history_release.set()
    opened = await gui_rpc.async_dispatch(app, command)
    assert opened["connected"] is True and opened["session_id"] == target.parent.name
    assert app.convo_path == target
    assert opened["messages"][-1]["content"] == "Reconciled saved answer"
    assert [message["content"] for message in opened["messages"] if message["role"] == "user"] == ["Saved browser prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["stale", "from", "target", "partial", "current", "holds", "changed-during-read"])
async def test_invalid_browser_selection_never_mutates_the_conversation(saved_app, monkeypatch, fault):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="open destination")
    result = page(app)
    command = opening(app, result, target.parent.name)
    if fault == "stale":
        seed(app, title="added after selection")
    elif fault == "from":
        command["from_session_id"] = str(uuid.uuid4())
    elif fault == "target":
        command["session_id"] = str(uuid.uuid4())
    elif fault == "partial":
        command.pop("from_session_id")
    elif fault == "current":
        command["session_id"] = app.convo_id
    elif fault == "holds":
        app._hold_restored_input = False
    else:
        original = ConversationRepository.read
        def read(path, *, max_bytes=None):
            value = original(path, max_bytes=max_bytes)
            with target.open("ab") as output:
                output.write(b'{"type":"rename","name":"changed"}\n')
            return value
        monkeypatch.setattr(ConversationRepository, "read", read)
    before = app.convo_id
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Stale/foreign browser selection reached resume")
    monkeypatch.setattr(app, "_resume", forbidden)
    with pytest.raises((ValueError, TypeError)):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_id == before and not app._gui_management_busy


@pytest.mark.asyncio
@pytest.mark.parametrize("include", [None, True, False])
async def test_state_can_skip_legacy_catalog_without_changing_legacy_default(saved_app, monkeypatch, include):
    app = saved_app
    calls: list = []
    def conversations(*_args):
        calls.append(True)
        return [{"legacy": "unchanged"}]
    monkeypatch.setattr(gui_rpc, "_conversations", conversations)
    monkeypatch.setattr(gui_rpc, "_settings", lambda _: {})
    monkeypatch.setattr(gui_rpc, "_jobs", lambda *_: [])
    command = {"type": "gui.state"}
    if include is not None:
        command["include_conversations"] = include
    result = gui_rpc.dispatch(app, command)
    assert result["conversations"] == ([] if include is False else [{"legacy": "unchanged"}])
    assert len(calls) == (0 if include is False else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, 0, 1, "false", [], {}])
async def test_state_rejects_nonboolean_catalog_flag(saved_app, invalid):
    with pytest.raises(ValueError, match="boolean"):
        gui_rpc.dispatch(saved_app, {"type": "gui.state", "include_conversations": invalid})


@pytest.mark.asyncio
async def test_bounded_reader_preserves_replay_and_stops_before_growth_overrun(saved_app, monkeypatch):
    app = saved_app
    target = seed(app, title="before")
    size = target.stat().st_size
    original = ConversationRepository.read(target)
    assert ConversationRepository.read(target, max_bytes=size) == original
    with pytest.raises(ValueError, match="byte limit"):
        ConversationRepository.read(target, max_bytes=size - 1)
    actual_open = Path.open
    def raced_open(path, *args, **kwargs):
        if path == target and args == ("rb",):
            with actual_open(target, "ab") as output:
                output.write(b"x" * 100)
        return actual_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", raced_open)
    with pytest.raises(ValueError, match="byte limit"):
        ConversationRepository.read(target, max_bytes=size + 8)


@pytest.mark.parametrize("cap", [True, False, 0, -1, 2.0, "10"])
def test_bounded_reader_rejects_invalid_caps_before_open(tmp_path, cap):
    with pytest.raises(ValueError, match="positive integer"):
        ConversationRepository.read(tmp_path / "absent.jsonl", max_bytes=cap)



@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("tools_enabled", True), ("mcp_enabled", True), ("tool_policy_profile", "autonomous"),
    ("tools_disabled", ["Read"]), ("mcp_disabled_servers", ["server"]), ("tool_deny", ["Write"]),
])
async def test_known_policy_changes_are_disabled_and_guarded_before_open(saved_app, monkeypatch, field, value):
    app = saved_app
    app._hold_restored_input = True
    app._cli_tool_profile = None
    app._convo_settings.tool_policy_profile = "strict"
    app.settings.tool_policy_profile = "strict"
    target = seed(app, title="incompatible")
    payload = {"execution": {field: value}}
    if field == "tool_policy_profile":
        payload[field] = value
    (target.parent / "settings.json").write_text(json.dumps(payload), encoding="utf-8")
    result = page(app)
    row = next(row for row in result["entries"] if row["session_id"] == target.parent.name)
    assert not row["can_open"] and "changes" in row["reason"]
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Incompatible saved policy reached resume")
    monkeypatch.setattr(app, "_resume", forbidden)
    with pytest.raises(ValueError, match="changes"):
        await gui_rpc.async_dispatch(app, opening(app, result, target.parent.name))


@pytest.mark.asyncio
async def test_owned_backend_and_explicit_launch_restrictions_override_historical_settings(saved_app):
    app = saved_app
    app._hold_restored_input = True
    app._cli_tool_profile = "strict"
    app._launch_overrides = {"tools_enabled": False}
    app._invocation_saved_values = {"tools_enabled": True}
    target = seed(app, title="historical backend")
    (target.parent / "settings.json").write_text(json.dumps({
        "backend": "claude", "model": "historical-model", "tool_policy_profile": "autonomous",
        "execution": {"backend": "claude", "default_model": "historical-model",
                      "tool_policy_profile": "autonomous", "tools_enabled": True},
    }), encoding="utf-8")
    row = next(row for row in page(app)["entries"] if row["session_id"] == target.parent.name)
    assert row["can_open"] and row["reason"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["invalid", "oversized", "launch-provenance"])
async def test_unusable_saved_settings_are_visible_without_implicit_defaults(saved_app, kind):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="settings problem")
    payload = "{invalid" if kind == "invalid" else "x" * (conversation_page.MAX_SETTINGS + 1)
    if kind == "launch-provenance":
        app._spawned_marker = False
        app._spawned_seat = False
        payload = json.dumps({"seat_spawned": True})
    (target.parent / "settings.json").write_text(payload, encoding="utf-8")
    row = next(row for row in page(app)["entries"] if row["session_id"] == target.parent.name)
    assert not row["can_open"] and row["reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["saved-settings", "runtime-policy"])
async def test_settings_and_effective_policy_changes_invalidate_page_tokens(saved_app, change):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app)
    result = page(app)
    cursor = conversation_page._cursor(app.convo_id, result["agent_id"], result["snapshot"], 0)
    if change == "saved-settings":
        (target.parent / "settings.json").write_text('{"execution":{"tools_enabled":true}}', encoding="utf-8")
    else:
        app.settings.tools_disabled = ["Read"]
    with pytest.raises(ValueError, match="stale"):
        page(app, cursor)


@pytest.mark.asyncio
async def test_state_reports_actual_policy_and_owned_agent_independently_of_saved_effective_cache(
        saved_app, monkeypatch):
    app = saved_app
    app._active_tool_profile = "strict"
    app._cli_tool_profile = "strict"
    app._rpc = True
    app._approval_host = True
    app._gui_effective_settings = {"tools_enabled": True, "mcp_enabled": True, "tool_policy_profile": "autonomous"}
    monkeypatch.setattr(gui_rpc, "_settings", lambda _: {})
    monkeypatch.setattr(gui_rpc, "_jobs", lambda *_: [])
    state = gui_rpc.dispatch(app, {"type": "gui.state", "include_conversations": False})
    assert state["ownership"]["agent_id"] == app._agent_session.authority.agent_id
    assert state["tool_policy"] == {
        "tools_enabled": False, "mcp_enabled": False, "tool_profile": "strict",
        "tool_profile_requested": "strict", "approval_route": "host", "cwd": os.getcwd(),
    }


@pytest.mark.asyncio
async def test_bounded_reader_matches_existing_edit_truncate_and_torn_record_replay(saved_app):
    target = seed(saved_app)
    repository = ConversationRepository(agent_session=saved_app._agent_session)
    repository.adopt(target, target.parent.name)
    try:
        repository.record_msg({"role": "assistant", "content": "old assistant"})
        repository.record_edit(1, {"role": "user", "content": "edited"})
        repository.write_record({"type": "truncate", "keep_system": True, "keep_from": 2,
                                 "prepend": [{"role": "user", "content": "replacement"}]})
        repository.write_record({"type": "rename", "name": "new title"})
    finally:
        repository.release()
    with target.open("ab") as output:
        output.write(b'{"type":"msg","message":"torn \\xf0\\x9f')
    expected = ConversationRepository.read(target)
    assert ConversationRepository.read(target, max_bytes=target.stat().st_size) == expected
    assert expected[0]["name"] == "new title"
    assert [message["content"] for message in expected[1]] == ["Fixture system", "replacement", "old assistant"]


@pytest.mark.asyncio
@pytest.mark.parametrize("system_count,visible_count,reason", [
    (0, 0, "Native browser history requires 1..1000 saved messages"),
    (1000, 1, "Native browser history requires 1..1000 saved messages"),
    (0, 101, "Native browser history supports at most 100 visible messages"),
    (900, 100, ""),
])
async def test_known_native_history_bounds_are_checked_before_open(
        saved_app, monkeypatch, system_count, visible_count, reason):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="bounded history")
    repository = ConversationRepository(agent_session=app._agent_session)
    repository.adopt(target, target.parent.name)
    try:
        repository.record_snapshot(
            [{"role": "system", "content": "system"} for _ in range(system_count)]
            + [{"role": "user", "content": "saved"} for _ in range(visible_count)])
    finally:
        repository.release()
    result = page(app)
    row = next(row for row in result["entries"] if row["session_id"] == target.parent.name)
    assert row["message_count"] == system_count + visible_count
    assert row["can_open"] is (not reason) and row["reason"] == reason
    if reason:
        def forbidden(*_args, **_kwargs):
            raise AssertionError("Unsupported saved history reached resume")
        monkeypatch.setattr(app, "_resume", forbidden)
        with pytest.raises(ValueError, match="Native browser history"):
            await gui_rpc.async_dispatch(app, opening(app, result, target.parent.name))


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["A" * 32444, "A" * 32445, "A" * 40000, "雪" * 5000],
                         ids=["exact-native-budget", "over-native-budget", "long-history", "unicode"])
async def test_preview_reads_selected_history_without_switching_or_connecting(saved_app, monkeypatch, content):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="preview destination")
    with target.open("ab") as output:
        output.write((json.dumps({"type": "snapshot", "messages": [{"role": "user", "content": content}]}) + "\n").encode())
    selected = page(app)
    command = opening(app, selected, target.parent.name) | {"type": "gui.conversations.preview"}
    current, store, saved = app.convo_id, app.store, target.read_bytes()
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Read-only preview reached conversation mutation or connection")
    monkeypatch.setattr(app, "_resume", forbidden)
    monkeypatch.setattr(app, "connect", forbidden)
    result = await gui_rpc.async_dispatch(app, command)
    assert result["session_id"] == target.parent.name and result["from_session_id"] == current
    assert result["snapshot"] == selected["snapshot"]
    assert result["agent_id"] == selected["agent_id"]
    assert result["read_only"] is False and "connected" not in result
    assert result["messages"] == [{"role": "user", "content": content}]
    assert app.convo_id == current and app.store is store and target.read_bytes() == saved
    assert not getattr(app, "_gui_management_busy", False)


@pytest.mark.asyncio
async def test_preview_rejects_changed_selection_without_opening(saved_app, monkeypatch):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="preview destination")
    command = opening(app, page(app), target.parent.name) | {"type": "gui.conversations.preview"}
    current = app.convo_id
    seed(app, title="new catalog entry")
    def forbidden(*_args, **_kwargs):
        raise AssertionError("Stale preview reached resume")
    monkeypatch.setattr(app, "_resume", forbidden)
    with pytest.raises(ValueError, match="stale"):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_id == current


@pytest.mark.asyncio
async def test_preview_bounds_wire_escaped_response_before_publishing(saved_app, monkeypatch):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="preview destination")
    with target.open("ab") as output:
        output.write((json.dumps({"type": "snapshot", "messages": [{"role": "user", "content": "雪" * 800}]}) + "\n").encode())
    command = opening(app, page(app), target.parent.name) | {"type": "gui.conversations.preview"}
    current = app.convo_id
    result = await gui_rpc.async_dispatch(app, command)
    size = len(json.dumps(result, allow_nan=False).encode("utf-8"))
    assert size > len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
    monkeypatch.setattr(conversation_page, "MAX_PREVIEW", size)
    assert await gui_rpc.async_dispatch(app, command) == result
    monkeypatch.setattr(conversation_page, "MAX_PREVIEW", size - 1)
    with pytest.raises(ValueError, match="preview.*response.*budget"):
        await gui_rpc.async_dispatch(app, command)
    assert app.convo_id == current


@pytest.mark.asyncio
async def test_preview_revalidates_catalog_after_its_bounded_read(saved_app, monkeypatch):
    app = saved_app
    app._hold_restored_input = True
    target = seed(app, title="preview destination")
    command = opening(app, page(app), target.parent.name) | {"type": "gui.conversations.preview"}
    current = app.convo_id
    original = ConversationRepository.read
    reads = 0
    def changing_read(path, *, max_bytes=None):
        nonlocal reads
        assert max_bytes == conversation_page.MAX_TRANSCRIPT
        value = original(path, max_bytes=max_bytes)
        reads += 1
        if reads == 2:
            with target.open("ab") as output:
                output.write(b'{"type":"rename","name":"changed during preview"}\n')
        return value
    monkeypatch.setattr(ConversationRepository, "read", changing_read)
    with pytest.raises(ValueError, match="catalog changed"):
        await gui_rpc.async_dispatch(app, command)
    assert reads == 2 and app.convo_id == current
