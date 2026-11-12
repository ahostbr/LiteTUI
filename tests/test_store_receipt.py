"""Store injection is a recorded operation, never a search of file contents."""
import json

import pytest
from test_owner_resume import _index_example, folder, save
from test_owner_resume import build as _build

from litetui import appsvc
from litetui.claude_turn import ledger_for, system_prompt_for
from litetui.conversation import ConversationRepository

build = _build


@pytest.mark.parametrize("owner", [False, True])
def test_index_store_header_cannot_suppress_real_store(tmp_path, monkeypatch, build, owner):
    example = _index_example(tmp_path, monkeypatch, owner)
    memory = folder(tmp_path, "widgets") if owner else None
    app = build(memory)
    app._materialise_convo()
    prompt = app._request_messages()[0]["content"]
    assert example in prompt
    assert ("OWNER-BODY-widgets-SOUL.md" if owner else "HOME-BODY") in prompt
    assert prompt.count(appsvc.STORE_HEADER) == 2
    meta, _ = ConversationRepository.read(app.convo_path)
    assert meta["store_injected"] is True
    before = app.convo_path.read_bytes()
    assert app._request_messages()[0]["content"] == prompt
    assert app.convo_path.read_bytes() == before


def test_fresh_and_native_prompts_record_injection_receipt(build):
    app = build(None)
    app._materialise_convo()
    meta, _ = ConversationRepository.read(app.convo_path)
    assert meta["store_injected"] is False
    ledger = ledger_for(app)
    system_prompt_for(app, ledger.select_segment(str(app.convo_dir)))
    assert ledger.selected["store_injected"] is True
    assert not getattr(app, "_store_notice_text", None)


def remove_receipts(path):
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record.pop("store_injected", None)
        record.pop("store_identity", None)
    path.write_bytes(("\n".join(json.dumps(record) for record in records) + "\n").encode())
    native = path.parent / "claude_ledger.json"
    if native.exists():
        data = json.loads(native.read_text(encoding="utf-8"))
        for segment in data["segments"].values():
            segment.pop("store_injected", None)
            segment.pop("store_identity", None)
        native.write_bytes(json.dumps(data).encode())


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
async def test_legacy_notice_once_without_changing_prompt(tmp_path, build, owner, backend):
    memory = folder(tmp_path, "widgets") if owner else None
    first = build(memory, backend)
    path = save(first, backend)
    saved = first.conversation[0]["content"]
    remove_receipts(path)
    resumed = build(memory, backend)
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        notices = [text for text in said if text.startswith("Legacy store snapshot")]
        assert len(notices) == 1
        notice = notices[0]
        assert "may not have loaded" in notice and "/new" in notice
        root = memory or resumed._agent_session.memory_root
        files = ("SOUL.md", "NOW.md", "DOMAIN.md") if owner else ("memory.md", "soul.md", "handoff.md")
        assert all(str(root / name) in notice for name in files)
        for _ in range(2):
            messages = resumed._request_messages()
            assert messages[0]["content"] == saved
            assert sum(message.get("content") == notice for message in messages) == 1
        if backend == "claude":
            ledger = ledger_for(resumed)
            before = ledger.file.read_bytes()
            assert system_prompt_for(resumed, ledger.selected) == ledger.selected["system_prompt"]
            assert ledger.file.read_bytes() == before
        before = path.read_bytes()
        assert resumed._resume(path, startup=True)
        assert path.read_bytes() == before
        assert len([text for text in said if text.startswith("Legacy store snapshot")]) == 1
        assert ConversationRepository.read(path)[0]["store_notice"] == notice
        # Exercise the exact recovery action shown to both readers.
        from litetui.plugins import convo
        (root / files[0]).write_text("FRESH-AFTER-EXPLICIT-NEW", encoding="utf-8")
        convo._cmd_new(resumed, "new", "")
        await pilot.pause()
        assert resumed._store_notice_text is None
        assert "FRESH-AFTER-EXPLICIT-NEW" in resumed._request_messages()[0]["content"]
        if backend == "claude":
            ledger = ledger_for(resumed)
            fresh = system_prompt_for(resumed, ledger.select_segment(str(resumed.convo_dir)))
            assert "FRESH-AFTER-EXPLICIT-NEW" in fresh
            assert ledger.selected["store_injected"] is True
        assert len([text for text in said if text.startswith("Legacy store snapshot")]) == 1


@pytest.mark.asyncio
async def test_native_notice_failed_send_and_receipt_crash_retry_once(tmp_path):
    from litetui.claude_persistence import ClaudeLedger
    from litetui.claude_turn import _query_with_store_notice

    ledger = ClaudeLedger(tmp_path)
    calls = []

    class Session:
        fail = True

        async def query(self, entry_id, content):
            calls.append(content)
            if self.fail:
                raise OSError("send failed")

    session = Session()
    notice = "Legacy store snapshot notice"
    with pytest.raises(OSError, match="send failed"):
        await _query_with_store_notice(session, ledger, "first", "hello", notice)
    assert not ledger.store_notice_delivered
    session.fail = False
    real_save = ledger._save
    ledger._save = lambda: (_ for _ in ()).throw(OSError("crash before receipt"))
    with pytest.raises(OSError, match="crash before receipt"):
        await _query_with_store_notice(session, ledger, "second", "hello", notice)
    assert not ledger.store_notice_delivered
    ledger._save = real_save
    reopened = ClaudeLedger(tmp_path)
    await _query_with_store_notice(session, reopened, "third", "hello", notice)
    await _query_with_store_notice(session, reopened, "fourth", "hello", notice)
    assert calls == [notice + "\n\nhello"] * 3 + ["hello"]
    assert ClaudeLedger(tmp_path).store_notice_delivered


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
async def test_real_native_stream_sends_notice_once_or_never_for_modern(tmp_path, legacy):
    from test_claude_turn import result_event, turn_app

    from litetui.claude_turn import prepare_input, stream_turn

    app = turn_app(tmp_path, messages=["terminal"], events=[[result_event()]])
    notice = "Legacy store snapshot: read the named files; use /new to rebuild."
    if legacy:
        app._store_notice_text = notice
    session = app.backend.session
    sent = []

    async def query(entry_id, content):
        sent.append(content)

    session.query = query
    await stream_turn(app)
    app._claude_active_input = {"content": "next", **prepare_input(app, "next", "strict", "typed")}
    app._events = [[result_event()]]
    await stream_turn(app)
    assert sent == [(notice + "\n\nhello") if legacy else "hello", "next"]
    assert app._claude_ledger.store_notice_delivered is legacy
    entries = app._claude_ledger.selected["entries"]
    assert (entries[0].get("store_notice") == notice) is legacy
    assert "store_notice" not in entries[1]


def test_receipt_replay_resets_only_with_system_content(tmp_path):
    path = tmp_path / "convo.jsonl"
    system = {"role": "system", "content": "saved"}
    records = [
        {"type": "meta", "model": "fixture"},
        {"type": "snapshot", "messages": [system], "store_injected": False},
    ]

    def replay():
        path.write_bytes(("\n".join(json.dumps(record) for record in records) + "\n").encode())
        return ConversationRepository.read(path)

    assert replay()[0]["store_injected"] is False
    records.append({"type": "edit", "index": 0, "message": system, "store_injected": True})
    assert replay()[0]["store_injected"] is True
    records.append({"type": "msg", "message": {"role": "user", "content": "hello"}})
    records.append({"type": "edit", "index": 1, "message": {"role": "user", "content": "changed"}})
    assert replay()[0]["store_injected"] is True
    records.append({"type": "edit", "index": 0, "message": system})
    assert "store_injected" not in replay()[0]


@pytest.mark.asyncio
async def test_legacy_index_collision_without_loaded_memory_preserves_bytes(tmp_path, monkeypatch, build):
    _index_example(tmp_path, monkeypatch, False)
    first = build(None)
    first._materialise_convo()
    first._inject_index_once()
    first._append({"role": "user", "content": "saved without a loaded store"})
    saved = first.conversation[0]["content"]
    assert appsvc.STORE_HEADER in saved and "HOME-BODY" not in saved
    path = first.convo_path
    first.store.release()
    first._agent_session.release()
    remove_receipts(path)
    resumed = build(None)
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        outgoing = resumed._request_messages()
        assert outgoing[0]["content"] == saved
        notices = [text for text in said if text.startswith("Legacy store snapshot")]
        assert len(notices) == 1
        assert sum(message.get("content") == notices[0] for message in outgoing) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True], ids=["home", "owner"])
@pytest.mark.parametrize("insert", [False, True], ids=["replace", "insert"])
async def test_system_command_resets_receipt_and_replay_keeps_fresh_store(tmp_path, build, owner, insert):
    memory = folder(tmp_path, "widgets") if owner else None
    app = build(memory)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._materialise_convo()
        app._request_messages()
        assert app._store_injected is True
        provenance = ConversationRepository.read(app.convo_path)[0]["store_identity"]
        if insert:
            app.conversation = [{"role": "user", "content": "keep this prior turn"}]
            app._snapshot("fixture without a leading system message")
        root = memory or app._agent_session.memory_root
        (root / ("SOUL.md" if owner else "soul.md")).write_text("FRESH-SYSTEM-STORE", encoding="utf-8")
        app._handle_command("/system Be concise")
        assert app.conversation[0]["role"] == "system"
        assert "Be concise" in app.conversation[0]["content"]
        assert app.conversation[0]["content"].count("# BUILDER ROLE") == 1
        assert app._store_injected is False, "/system kept a receipt for removed content"
        meta, messages = ConversationRepository.read(app.convo_path)
        assert meta["store_injected"] is False and meta["store_identity"] == provenance
        assert messages == app.conversation
        assert app._resume(app.convo_path, startup=True)
        assert app._store_injected is False
        outgoing = app._request_messages()
        assert outgoing[0]["content"].startswith("# BUILDER ROLE")
        assert "Be concise" in outgoing[0]["content"]
        assert "FRESH-SYSTEM-STORE" in outgoing[0]["content"]
        assert outgoing[0]["content"].count(appsvc.STORE_HEADER) == 1
        assert app._store_injected is True
        meta, messages = ConversationRepository.read(app.convo_path)
        assert meta["store_injected"] is True and meta["store_identity"] == provenance
        assert messages == app.conversation, "persisted host bookkeeping is retained exactly"
        expected_public = [{"role": "system", "content": messages[0]["content"]}]
        if insert:
            expected_public.append({"role": "user", "content": "keep this prior turn"})
        assert outgoing == expected_public, "wire body/turns are identical without host bookkeeping"
        assert messages[0]["litetui_prompt_owner"] == 1 and "litetui_prompt_owner" not in outgoing[0]
        if insert:
            assert any(message.get("content") == "keep this prior turn" for message in messages)
        saved = outgoing[0]["content"]
        before = app.convo_path.read_bytes()
        assert app._resume(app.convo_path, startup=True)
        assert app._request_messages()[0]["content"] == saved
        assert app.convo_path.read_bytes() == before
