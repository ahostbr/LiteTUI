"""Official launch composition has a bounded second host-message slot."""
import json
from pathlib import Path

import pytest
from test_owner_resume import build as _build
from test_owner_resume import folder

from litetui import appsvc
from litetui.app import LiteTUI
from litetui.claude_turn import ledger_for, system_prompt_for
from litetui.conversation import ConversationRepository

build = _build
LAUNCH = "Use short answers"


async def apply_launch(app, text, *, accepted=True):
    async def registered():
        return True  # mock registration transport; real owned execution guard still runs

    from litetui.llm_backend import ModelRow
    app._register_owned_startup = registered
    model = app._cli_initial_model or app.model_id
    app.available_models = [model]
    app.model_rows = {model: ModelRow(model, None, "fixture", loaded=True)}
    app._connect_settled = True
    app._cli_launch_error = None
    app._cli_thinking_level = None
    app._cli_system_prompt = text
    await LiteTUI._apply_cli_args.__wrapped__(app)
    assert bool(app._cli_launch_error) is (not accepted)


async def legacy_fixture(tmp_path, monkeypatch, build, owner, backend, suffix, launch=LAUNCH, *, legacy=True):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    if suffix in ("index", "both"):
        # File text may quote full marker-like examples; the suffix is opaque.
        (repo / "AGENT_INDEX.md").write_text("# Widgets\nQuoted example: " + appsvc.STORE_HEADER, encoding="utf-8")
    memory = folder(tmp_path, "widgets") if owner else None
    first = build(memory, backend)
    async with first.run_test() as pilot:
        await pilot.pause()
        await apply_launch(first, launch)
        assert first.conversation[0] == {"role": "system", "content": launch}
        assert first.conversation[1]["role"] == "system"
        first._materialise_convo()
        if suffix == "index":
            root = memory or first._agent_session.memory_root
            names = ("SOUL.md", "NOW.md", "DOMAIN.md") if owner else ("memory.md", "soul.md", "handoff.md")
            for name in names:
                (root / name).write_text("", encoding="utf-8")
        if suffix != "none":
            first._request_messages()
        if backend == "claude":
            system_prompt_for(first, ledger_for(first).select_segment(str(Path.cwd())))
        first._append({"role": "user", "content": "saved first input"})
        path = first.convo_path
        saved = [dict(message) for message in first.conversation]
    first.store.release()
    first._agent_session.release()
    if not legacy:
        return memory, path, saved
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record.pop("store_identity", None)
        record.pop("store_injected", None)
    path.write_bytes(("\n".join(json.dumps(record) for record in records) + "\n").encode())
    native = path.parent / "claude_ledger.json"
    if native.exists():
        data = json.loads(native.read_text(encoding="utf-8"))
        for segment in data["segments"].values():
            segment.pop("store_identity", None)
            segment.pop("store_injected", None)
        native.write_bytes(json.dumps(data).encode())
    return memory, path, saved


@pytest.mark.asyncio
@pytest.mark.parametrize("owner", [False, True], ids=["home", "owner"])
@pytest.mark.parametrize("backend,suffix", [("lmstudio", "none"), ("lmstudio", "index"),
                                             ("lmstudio", "store"), ("lmstudio", "both"),
                                             ("claude", "none")])
async def test_same_legacy_launch_shape_resumes_without_rewriting_systems(tmp_path, monkeypatch, build, owner, backend, suffix):
    memory, path, saved = await legacy_fixture(tmp_path, monkeypatch, build, owner, backend, suffix)
    resumed = build(memory, backend)
    resumed._cli_system_prompt = LAUNCH
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        assert resumed._request_messages()[:2] == saved[:2]
        assert ConversationRepository.read(path)[1][:2] == saved[:2]
        if backend == "claude":
            ledger = ledger_for(resumed)
            before = ledger.file.read_bytes()
            assert system_prompt_for(resumed, ledger.selected) == ledger.selected["system_prompt"]
            assert ledger.file.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "Different role", LAUNCH + " plus more"], ids=["missing", "changed", "longer"])
@pytest.mark.parametrize("backend,suffix", [("lmstudio", "both"), ("claude", "none")])
async def test_legacy_launch_must_match_supplied_text_exactly(tmp_path, monkeypatch, build, replacement, backend, suffix):
    memory, path, _ = await legacy_fixture(tmp_path, monkeypatch, build, False, backend, suffix)
    resumed = build(memory, backend)
    resumed._cli_system_prompt = replacement
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    before = path.read_bytes()
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert not resumed._resume(path)
        assert len(said) == 1 and "Store mismatch" in said[0] and "omit --convo" in said[0]
    assert path.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [appsvc.STORE_HEADER, appsvc.INDEX_HEADER])
async def test_legacy_launch_marker_is_ambiguous_even_with_same_text(tmp_path, monkeypatch, build, marker):
    text = LAUNCH + "\n" + marker
    memory, path, _ = await legacy_fixture(tmp_path, monkeypatch, build, False, "lmstudio", "none", text)
    resumed = build(memory)
    resumed._cli_system_prompt = text
    said = []
    resumed._system = lambda message, *a, **k: said.append(str(message))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert not resumed._resume(path)
        assert len(said) == 1 and "Store mismatch" in said[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["gap", "missing-host", "incomplete-host", "bad-opening"])
async def test_legacy_launch_never_searches_past_the_two_fixed_slots(tmp_path, monkeypatch, build, damage):
    memory, path, saved = await legacy_fixture(tmp_path, monkeypatch, build, False, "lmstudio", "store")
    messages = [dict(message) for message in saved]
    if damage == "gap":
        messages[0]["content"] = LAUNCH + "intervening text" + messages[0]["content"][len(LAUNCH):]
    elif damage == "bad-opening":
        messages[0]["content"] = LAUNCH + "\n\n" + appsvc.STORE_HEADER + "\n\nnot the generated opening"
    else:
        messages.append(dict(messages[1]))  # a later valid host slot must never rescue it
        messages[1] = {"role": "user" if damage == "missing-host" else "system", "content": "incomplete host"}
    path.write_bytes((json.dumps({"type": "snapshot", "messages": messages}) + "\n").encode())
    resumed = build(memory)
    resumed._cli_system_prompt = LAUNCH
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert not resumed._resume(path)
        assert len(said) == 1 and "Store mismatch" in said[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True], ids=["recorded", "legacy"])
@pytest.mark.parametrize("backend,suffix", [("lmstudio", "none"), ("lmstudio", "index"),
                                             ("lmstudio", "store"), ("lmstudio", "both"),
                                             ("claude", "none")])
async def test_cli_resume_twice_keeps_message_count_and_receipt(tmp_path, monkeypatch, build, legacy, backend, suffix):
    memory, path, saved = await legacy_fixture(tmp_path, monkeypatch, build, True, backend, suffix, legacy=legacy)
    resumed = build(memory, backend)
    resumed._cli_system_prompt = LAUNCH
    async with resumed.run_test() as pilot:
        await pilot.pause()
        resumed._cli_convo_id = path.parent.name
        for _ in range(2):
            assert resumed._resume(path, startup=True)
            before = [dict(message) for message in resumed.conversation]
            receipt = resumed._store_injected
            raw = path.read_bytes()
            await apply_launch(resumed, LAUNCH)
            assert resumed.conversation == before, "CLI resume stacked another launch message"
            assert resumed._store_injected is receipt
            assert path.read_bytes() == raw
            assert resumed.conversation[:2] == saved[:2]


@pytest.mark.asyncio
@pytest.mark.parametrize("replacement", [None, "Changed launch", LAUNCH + " extra"])
async def test_cli_recorded_resume_refuses_different_launch_without_inserting(tmp_path, monkeypatch, build, replacement):
    memory, path, _ = await legacy_fixture(tmp_path, monkeypatch, build, False, "lmstudio", "both", legacy=False)
    resumed = build(memory)
    resumed._cli_system_prompt = LAUNCH
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        resumed._cli_convo_id = path.parent.name
        before = [dict(message) for message in resumed.conversation]
        receipt = resumed._store_injected
        raw = path.read_bytes()
        said.clear()
        await apply_launch(resumed, replacement, accepted=False)
        assert resumed.conversation == before and resumed._store_injected is receipt
        assert path.read_bytes() == raw
        assert len(said) == 1 and "Launch text mismatch" in said[0] and "omit --convo" in said[0]
