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
    app._legacy_launch_candidates = (text,) if text else ()
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
        # Build the pre-T0338 fixture explicitly. The shipped launcher now emits
        # one canonical system, so asking it to manufacture the retired pair
        # would no longer test legacy provenance at all.
        first._cli_system_prompt = launch
        (first._agent_session.memory_root / "prompts" / "role.md").write_text(launch, encoding="utf-8")
        host = first.plugins.compose_prompt() + (
            "\n\nEnd each FINAL answer with <recap>two short lines: what you did and result, about 40 tokens</recap>. Do not put this tag in interim tool calls.")
        first.conversation[:] = [{"role": "system", "content": launch}, {"role": "system", "content": host}]
        first._store_injected = False
        first._materialise_convo()
        if suffix == "index":
            root = memory or first._agent_session.memory_root
            names = ("SOUL.md", "NOW.md", "DOMAIN.md") if owner else ("memory.md", "soul.md", "handoff.md")
            for name in names:
                (root / name).write_text("", encoding="utf-8")
        if suffix != "none":
            # These are the shipped original suffix injectors; request emission
            # now refuses a retired two-system pair until canonicalized.
            first._inject_index_once()
            first._inject_lore_once()
            first._inject_store_once()
        if backend == "claude":
            ledger = ledger_for(first)
            segment = ledger.select_segment(str(Path.cwd()))
            # Explicit historical native shape: raw launch then original host,
            # not the new canonical composer with a midturn stale snapshot.
            ledger.fix_system_prompt(segment["id"], launch + "\n\n" + host,
                                     store_identity=first._prompt_store_identity,
                                     store_injected=first._store_injected)
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
        with pytest.raises(ValueError, match="one initial string system"):
            resumed._request_messages()
        assert resumed.conversation[:2] == saved[:2]
        assert ConversationRepository.read(path)[1][:2] == saved[:2]
        if backend == "claude":
            ledger = ledger_for(resumed)
            before = ledger.file.read_bytes()
            # Store provenance remains readable, but selected-policy execution
            # cannot bless an unversioned native system from current files.
            from litetui.claude_turn import UncertainDelivery
            with pytest.raises(UncertainDelivery, match="/claude new"):
                system_prompt_for(resumed, ledger.selected)
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
@pytest.mark.parametrize("owner", [False, True], ids=["home", "owner"])
@pytest.mark.parametrize("marker", [appsvc.STORE_HEADER, appsvc.INDEX_HEADER])
async def test_legacy_launch_marker_with_same_text_resumes_unchanged(tmp_path, monkeypatch, build, owner, marker):
    text = LAUNCH + "\n" + marker
    memory, path, saved = await legacy_fixture(tmp_path, monkeypatch, build, owner, "lmstudio", "none", text)
    resumed = build(memory)
    resumed._cli_system_prompt = text
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=True)
        assert resumed.conversation[:2] == saved[:2]
        assert ConversationRepository.read(path)[1][:2] == saved[:2]


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
            assert resumed._store_injected is receipt
            assert [i for i, m in enumerate(resumed.conversation) if m["role"] == "system"] == [0]
            assert resumed.conversation[0]["content"].count("# BUILDER ROLE") == 1
            if before[0].get("litetui_prompt_version") == 2:
                assert resumed.conversation == before and path.read_bytes() == raw
            else:
                assert len(resumed.conversation) == len(before) - 1
                assert resumed.conversation[1:] == before[2:]


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
        assert len(said) == 1 and "Launch text mismatch" in said[0] and "new conversation" in said[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change,accepted", [("archive", True), ("trailing-space", True), ("empty", True),
                                          ("foreign", False), ("opening", False), ("body", False)])
async def test_real_fleet_extension_prefix(tmp_path, monkeypatch, build, change, accepted):
    # An anonymized real launch tail and fleet extension; private store bodies omitted.
    fixture = json.loads((Path(__file__).parent / "fixtures" / "legacy_fleet_launch.json").read_text())
    launch, content = fixture["launch"], fixture["message"]
    assert content.startswith(launch + "\nYou are registered")  # real one-newline suffix
    if change == "empty":
        content = content[:content.index("You are registered")] + "You are registered in the LiteHarness fleet as "
    elif change == "trailing-space":
        launch += "  \t\n"
    elif change == "foreign":
        content = content.replace("\n\nYou are registered", "\n\nForeign sentence.\n\nYou are registered", 1)
    elif change == "opening":
        content = content.replace("You are registered", "You are Registered", 1)
    elif change == "body":
        launch = launch.replace("When", "Then", 1)
    memory, path, saved = await legacy_fixture(tmp_path, monkeypatch, build, False, "lmstudio", "none", launch)
    saved[0] = {"role": "system", "content": content}
    path.write_bytes((json.dumps({"type": "snapshot", "messages": saved}) + "\n").encode())
    resumed = build(memory)
    resumed._cli_system_prompt = launch
    said = []
    resumed._system = lambda text, *a, **k: said.append(str(text))
    raw = path.read_bytes()
    async with resumed.run_test() as pilot:
        await pilot.pause()
        assert resumed._resume(path, startup=accepted) is accepted, "real fleet prefix classification differs"
        if accepted:
            expected = [dict(message) for message in saved[:2]]
            expected[0]["content"] = LiteTUI._FLEET_LINE_RE.sub(resumed._fleet_identity_sentence(), content, count=1)
            assert ConversationRepository.read(path)[1][:2] == expected
            raw = path.read_bytes()  # resume may refresh fleet identity and persist its one-time notice
            resumed._cli_convo_id = path.parent.name
            before = [dict(message) for message in resumed.conversation]
            await apply_launch(resumed, launch)
            assert [i for i, m in enumerate(resumed.conversation) if m["role"] == "system"] == [0]
            assert resumed.conversation[1:] == before[2:]
            raw = path.read_bytes()  # canonical launch migration is deliberately persisted
        else:
            assert len(said) == 1 and "Store mismatch" in said[0] and "omit --convo" in said[0]
        assert path.read_bytes() == raw


@pytest.mark.parametrize("current,expected", [
    ("", "extension"),
    ("launch \n", "launch\n\nextension"),
    ("\n\n" + appsvc.STORE_HEADER, "\n\nextension\n\n" + appsvc.STORE_HEADER),
    ("launch \n\n" + appsvc.STORE_HEADER, "launch\n\nextension\n\n" + appsvc.STORE_HEADER),
])
def test_shared_extension_join_preserves_existing_writer_output(current, expected):
    from types import SimpleNamespace

    app = SimpleNamespace(conversation=[{"role": "system", "content": current}], _convo_loading=True)
    LiteTUI._append_to_system(app, "extension")
    assert app.conversation == [{"role": "system", "content": expected}]
