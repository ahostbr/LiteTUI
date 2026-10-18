"""A launch cannot silently change the store of a saved conversation."""
from pathlib import Path

import pytest

from litetui import app as app_mod
from litetui import owner_mode
from litetui.agent_launch_context import ordinary
from litetui.claude_turn import ledger_for, system_prompt_for
from litetui.settings import Settings


@pytest.fixture
def build(tmp_path):
    apps = []

    def make(memory, backend="lmstudio"):
        session = ordinary(tmp_path, Settings(seat_name="Alpha", backend=backend,
                                              default_model="fixture-model"))
        app = app_mod.LiteTUI(agent_session=session, owner_memory_dir=memory)
        apps.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._cli_convo_id = None
        (session.memory_root / "soul.md").write_text("HOME-BODY", encoding="utf-8")
        return app

    yield make
    for app in reversed(apps):
        app.store.release()
        app._agent_session.release()


def folder(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    for file in owner_mode.OWNER_FILES:
        (root / file).write_text(f"OWNER-BODY-{name}-{file}", encoding="utf-8")
    return root


def save(app, backend):
    app._materialise_convo()
    app._request_messages()
    if backend == "claude":
        system_prompt_for(app, ledger_for(app).select_segment(str(Path.cwd())))
    app._append({"role": "user", "content": "saved turn"})
    path = app.convo_path
    app.store.release()
    app._agent_session.release()
    return path


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
@pytest.mark.parametrize("change", ["ordinary-to-owner", "owner-to-ordinary", "other-folder"])
@pytest.mark.parametrize("via_cli", [False, True])
async def test_mismatched_resume_refuses_before_adoption(tmp_path, build, backend, change, via_cli):
    original = None if change == "ordinary-to-owner" else folder(tmp_path, "widgets")
    target = None if change == "owner-to-ordinary" else folder(tmp_path, "gadgets")
    path = save(build(original, backend), backend)
    transcript = path.read_bytes()
    ledger_path = path.parent / "claude_ledger.json"
    ledger_bytes = ledger_path.read_bytes() if ledger_path.exists() else None
    resumed = build(target, backend)
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        before = resumed.convo_path
        resumed._cli_convo_id = path.parent.name
        accepted = resumed._resume_cli_conversation() if via_cli else resumed._resume(path)
        assert not accepted, "store mismatch was silently resumed"
        if via_cli:
            assert "Store mismatch" in resumed._cli_launch_error
        assert resumed.convo_path == before
        assert len(said) == 1 and "store mismatch" in said[0].lower()
        assert "--convo" in said[0] and path.parent.name in said[0]
        if original is None:
            assert "without --owner and --memory" in said[0]
        else:
            assert "--owner Alpha" in said[0] and "--memory" in said[0]
            assert str(original) in said[0]
    assert path.read_bytes() == transcript
    if ledger_bytes is not None:
        assert ledger_path.read_bytes() == ledger_bytes


@pytest.mark.parametrize("change", ["ordinary-to-owner", "owner-to-ordinary", "other-folder"])
def test_recorded_claude_prompt_is_refused_not_rewritten(tmp_path, build, change):
    original = None if change == "ordinary-to-owner" else folder(tmp_path, "widgets")
    target = None if change == "owner-to-ordinary" else folder(tmp_path, "gadgets")
    app = build(original)
    app._materialise_convo()
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(Path.cwd()))
    prompt = system_prompt_for(app, segment)
    before = ledger.file.read_bytes()
    app._owner_memory_dir = target
    with pytest.raises(owner_mode.OwnerModeError, match="[Ss]tore mismatch"):
        system_prompt_for(app, segment)
    assert ledger.segment(segment["id"])["system_prompt"] == prompt
    assert ledger.file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
async def test_same_owner_resume_preserves_both_snapshots(tmp_path, build, backend):
    memory = folder(tmp_path, "widgets")
    first = build(memory, backend)
    path = save(first, backend)
    saved = first.conversation[0]["content"]
    native = ledger_for(first).selected if backend == "claude" else None
    (memory / "NOW.md").write_text("CHANGED-NOW", encoding="utf-8")
    resumed = build(memory, backend)
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        assert resumed._request_messages()[0]["content"] == saved
        if native:
            assert system_prompt_for(resumed, ledger_for(resumed).selected) == native["system_prompt"]


@pytest.mark.asyncio
async def test_saved_native_prompt_is_checked_even_when_message_zero_matches(tmp_path, build):
    from litetui.textfmt import memory_prompt

    memory = folder(tmp_path, "widgets")
    first = build(None)
    first._materialise_convo()
    first._request_messages()
    ledger = ledger_for(first)
    segment = ledger.select_segment(str(Path.cwd()))
    ledger.fix_system_prompt(segment["id"], memory_prompt(first.convo_id, memory, owner=True),
                             store_identity={"version": 1, "mode": "owner", "folder": str(memory)})
    path = save(first, "lmstudio")
    before = ledger.file.read_bytes()
    resumed = build(None)
    said = []
    resumed._system = said.append
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert not resumed._resume(path)
        assert len(said) == 1 and "Store mismatch" in said[0]
        assert "--memory" in said[0]
    assert ledger.file.read_bytes() == before


@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner"])
def test_new_prompts_record_store_identity_as_data(tmp_path, build, owner):
    from litetui.conversation import ConversationRepository

    memory = folder(tmp_path, "widgets") if owner else None
    app = build(memory)
    expected = {"version": 1, "mode": "owner" if owner else "home",
                "folder": str(memory or app._agent_session.memory_root)}
    app._materialise_convo()
    meta, _ = ConversationRepository.read(app.convo_path)
    assert meta.get("store_identity") == expected, "transcript has no recorded store identity"
    import json
    records = [json.loads(line) for line in app.convo_path.read_text(encoding="utf-8").splitlines()]
    snapshot = next(record for record in records if record["type"] == "snapshot")
    assert snapshot["store_identity"] == expected
    assert snapshot["messages"][0] == app.conversation[0]
    assert "store_identity" not in records[0], "provenance must accompany prompt, not a separate meta write"
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(Path.cwd()))
    system_prompt_for(app, segment)
    assert ledger.selected.get("store_identity") == expected


def _index_example(tmp_path, monkeypatch, owner):
    from litetui.appsvc import STORE_HEADER

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    heading = "memory.md" if owner else "NOW.md"
    text = f"# Widgets index\n\n```markdown\n{STORE_HEADER}\n### {heading} (current contents)\n```\n"
    (repo / "AGENT_INDEX.md").write_bytes(text.encode("utf-8"))
    monkeypatch.chdir(repo)
    return text.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner"])
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
async def test_index_example_cannot_change_same_store_identity(tmp_path, monkeypatch, build, owner, backend):
    example = _index_example(tmp_path, monkeypatch, owner)
    memory = folder(tmp_path, "widgets") if owner else None
    first = build(memory, backend)
    path = save(first, backend)
    saved = first.conversation[0]["content"]
    assert example in saved
    resumed = build(memory, backend)
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        assert resumed._request_messages()[0]["content"] == saved
        if backend == "claude":
            ledger = ledger_for(resumed)
            before = ledger.file.read_bytes()
            prompt = ledger.selected["system_prompt"]
            assert example in prompt
            assert system_prompt_for(resumed, ledger.selected) == prompt
            assert ledger.file.read_bytes() == before


@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner"])
def test_index_example_cannot_change_recorded_native_prompt(tmp_path, monkeypatch, build, owner):
    example = _index_example(tmp_path, monkeypatch, owner)
    app = build(folder(tmp_path, "widgets") if owner else None)
    app._materialise_convo()
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(Path.cwd()))
    prompt = system_prompt_for(app, segment)
    before = ledger.file.read_bytes()
    assert example in prompt
    assert system_prompt_for(app, segment) == prompt
    assert ledger.file.read_bytes() == before


def _write_heading_example(app, memory, fenced):
    heading = "### memory.md (current contents)" if memory else "### NOW.md (current contents)"
    example = f"```markdown\n{heading}\n```" if fenced else heading
    path = memory / "DOMAIN.md" if memory else app._agent_session.memory_root / "soul.md"
    path.write_text(f"Documentation example:\n\n{example}\n", encoding="utf-8")
    return example


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner"])
@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "unfenced"])
async def test_same_store_resume_treats_heading_examples_as_file_contents(tmp_path, build, owner, fenced):
    memory = folder(tmp_path, "widgets") if owner else None
    first = build(memory)
    example = _write_heading_example(first, memory, fenced)
    path = save(first, "lmstudio")
    saved = first.conversation[0]["content"]
    assert example in saved
    resumed = build(memory)
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        assert resumed._request_messages()[0]["content"] == saved


@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner"])
@pytest.mark.parametrize("fenced", [True, False], ids=["fenced", "unfenced"])
def test_recorded_claude_prompt_treats_heading_examples_as_file_contents(tmp_path, build, owner, fenced):
    memory = folder(tmp_path, "widgets") if owner else None
    app = build(memory)
    example = _write_heading_example(app, memory, fenced)
    app._materialise_convo()
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(Path.cwd()))
    prompt = system_prompt_for(app, segment)
    before = ledger.file.read_bytes()
    assert example in prompt
    assert system_prompt_for(app, segment) == prompt
    assert ledger.file.read_bytes() == before


@pytest.mark.parametrize("malformed", [None, {"version": 2, "mode": "home", "folder": "/widgets"},
                                       {"version": 1, "mode": "owner", "folder": "relative"}])
def test_invalid_recorded_identity_refuses_without_text_fallback(build, malformed):
    from litetui.store_identity import require_store_match

    app = build(None)
    with pytest.raises(owner_mode.OwnerModeError, match="recorded store provenance.*omit --convo"):
        require_store_match(app, malformed, prompt=app._system_prompt_text(), convo_id=app.convo_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True], ids=["ordinary", "owner"])
@pytest.mark.parametrize("backend", ["lmstudio", "claude"])
async def test_legacy_host_slot_preserves_same_store_with_arbitrary_file_examples(
        tmp_path, monkeypatch, build, owner, backend):
    import json

    _index_example(tmp_path, monkeypatch, owner)
    memory = folder(tmp_path, "widgets") if owner else None
    first = build(memory, backend)
    _write_heading_example(first, memory, True)
    path = save(first, backend)
    # Construct an old-format fixture. Product code must never backfill it.
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record.pop("store_identity", None)
    path.write_bytes(("\n".join(json.dumps(r) for r in records) + "\n").encode())
    native_path = path.parent / "claude_ledger.json"
    if backend == "claude":
        raw = json.loads(native_path.read_text(encoding="utf-8"))
        for segment in raw["segments"].values():
            segment.pop("store_identity", None)
        native_path.write_bytes(json.dumps(raw).encode())
    before = path.read_bytes()
    native_before = native_path.read_bytes() if native_path.exists() else None
    resumed = build(memory, backend)
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        if backend == "claude":
            ledger = ledger_for(resumed)
            assert system_prompt_for(resumed, ledger.selected) == ledger.selected["system_prompt"]
    assert path.read_bytes() == before
    if native_before is not None:
        assert native_path.read_bytes() == native_before


@pytest.mark.parametrize("native", [False, True])
def test_unknown_legacy_prefix_refuses_without_searching_later_file_bodies(build, native):
    from litetui.store_identity import require_store_match

    app = build(None)
    prompt = "custom prefix\n" + app._system_prompt_text()
    with pytest.raises(owner_mode.OwnerModeError, match="legacy prompt has no verifiable host store slot.*omit --convo"):
        require_store_match(app, prompt=prompt, convo_id=app.convo_id, native=native)


@pytest.mark.parametrize("native", [False, True])
@pytest.mark.parametrize("change", ["ordinary-to-owner", "owner-to-ordinary", "other-folder"])
def test_legacy_known_slot_still_refuses_real_store_mismatch(tmp_path, build, native, change):
    from litetui.store_identity import require_store_match

    original = None if change == "ordinary-to-owner" else folder(tmp_path, "widgets")
    target = None if change == "owner-to-ordinary" else folder(tmp_path, "gadgets")
    app = build(original)
    prompt = app._system_prompt_text()
    app._owner_memory_dir = target
    with pytest.raises(owner_mode.OwnerModeError, match="saved conversation and launch use different memory stores"):
        require_store_match(app, prompt=prompt, convo_id=app.convo_id, native=native)


def test_legacy_native_requires_same_launch_prefix(build):
    from litetui.store_identity import require_store_match

    app = build(None)
    app._cli_system_prompt = "Launch role and method"
    prompt = app._cli_system_prompt + "\n\n" + app._system_prompt_text()
    assert require_store_match(app, prompt=prompt, convo_id=app.convo_id, native=True)["mode"] == "home"
    app._cli_system_prompt = "Different launch role"
    with pytest.raises(owner_mode.OwnerModeError, match="no verifiable host store slot"):
        require_store_match(app, prompt=prompt, convo_id=app.convo_id, native=True)
