"""T0583-D: the lore file rides into the system prompt of `.agents` seats.

Contract (twin of the liteharness SessionStart reader; change one, change the other):
  * ~/.liteharness/lore.md, or LITEHARNESS_LORE_FILE; missing/empty/blank -> nothing.
  * At most 4 `## ` sections (the preamble stays), then at most 2,400 chars cut on a
    line boundary; each cut adds one warning line naming the file.
  * Injected once into message 0 at conversation start, before the store, and it is
    still there after a local compaction; a Claude segment prompt carries it too,
    including the fresh segment a Claude compaction starts.
  * A seat with no owned agent home (a `.convos`-era archive) gets nothing, no error.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from test_compaction_ui import _Chunk, _run, _scripted_create, _seed, _settle
from test_store_reinject import _bypass_owned_seat, _compact_app

from litetui import app as app_mod
from litetui import appsvc, settings
from litetui.agent_launch_context import ordinary
from litetui.conversation import ConversationRepository
from litetui.lore import LORE_CAP, LORE_SECTIONS, lore_path, read_lore

STORY = "## THE ZERO\nA grep said 0 (LORE-MARKER-4410). Prove your search can hit.\n"


@pytest.fixture
def lore_file(tmp_path, monkeypatch):
    path = tmp_path / "lore.md"
    monkeypatch.setenv("LITEHARNESS_LORE_FILE", str(path))
    return path


@pytest.fixture
def make_app(tmp_path):
    made = []

    def build() -> app_mod.LiteTUI:
        session = ordinary(tmp_path, settings.load())
        app = app_mod.LiteTUI(agent_session=session)
        made.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._materialise_convo()
        for name in ("memory.md", "soul.md", "handoff.md"):
            (session.memory_root / name).write_text("", encoding="utf-8")
        (session.memory_root / "soul.md").write_text("SOUL-MARKER-1188\n", encoding="utf-8")
        return app

    yield build
    for app in reversed(made):
        try:
            app.store.release()
        finally:
            app._agent_session.release()


def _write(path, text: str) -> None:
    """Bytes, so Windows does not turn the fixture's newlines into CRLF."""
    path.write_bytes(text.encode("utf-8"))


def _system_text(app) -> str:
    return app._request_messages()[0]["content"]


# ── the reader ─────────────────────────────────────────────────────────


def test_default_path_is_under_the_home_and_the_env_var_overrides_it(monkeypatch, tmp_path):
    monkeypatch.delenv("LITEHARNESS_LORE_FILE", raising=False)
    assert lore_path().parts[-2:] == (".liteharness", "lore.md")
    monkeypatch.setenv("LITEHARNESS_LORE_FILE", str(tmp_path / "elsewhere.md"))
    assert lore_path() == tmp_path / "elsewhere.md"


@pytest.mark.parametrize("content", [None, "", "  \n\t\n"])
def test_missing_or_empty_or_blank_is_nothing(lore_file, content):
    if content is not None:
        _write(lore_file, content)
    assert read_lore() is None
    assert appsvc.lore_block(SimpleNamespace(_agent_session=object())) == ""


def test_a_file_within_both_limits_is_returned_whole(lore_file):
    _write(lore_file, "preamble\n\n" + STORY + "\n## SECOND\nbody\n")
    path, text, warnings = read_lore()
    assert path == lore_file and warnings == []
    assert text.startswith("preamble") and "## SECOND\nbody" in text


def test_only_the_first_four_sections_survive_and_the_preamble_stays(lore_file):
    body = "PREAMBLE\n\n" + "".join(f"## S{i}\ntext {i}\n### sub{i}\n" for i in range(1, 7))
    _write(lore_file, body)
    _, text, warnings = read_lore()
    assert LORE_SECTIONS == 4
    assert text.startswith("PREAMBLE") and "## S4\ntext 4\n### sub4" in text
    assert "S5" not in text and "S6" not in text, "'###' lines are not sections and 5, 6 are dropped"
    assert warnings == [f"[... lore {str(lore_file).replace(chr(92), '/')}: 6 sections, only the first 4 are injected]"]


def test_over_the_cap_is_cut_on_a_line_boundary_with_one_warning_line(lore_file):
    lines = [f"line {i:03d} " + "x" * 30 for i in range(100)]  # 100 lines of 39 chars + newline
    _write(lore_file, "## BIG\n" + "\n".join(lines) + "\n")
    full = len(("## BIG\n" + "\n".join(lines)))
    _, text, warnings = read_lore()
    assert len(text) <= LORE_CAP and text.endswith("x" * 30), "the last kept line is whole"
    assert text.splitlines()[-1] in lines
    assert warnings == [f"[... lore {str(lore_file).replace(chr(92), '/')} truncated at {LORE_CAP} of {full} chars]"]


def test_no_newline_in_the_slice_is_a_hard_cut(lore_file):
    _write(lore_file, "y" * (LORE_CAP + 50))
    _, text, warnings = read_lore()
    assert text == "y" * LORE_CAP and len(warnings) == 1


def test_both_limits_apply_sections_first_then_the_cap_each_on_its_own_line(lore_file):
    body = "".join(f"## S{i}\n" + "z" * 900 + "\n" for i in range(1, 7))
    _write(lore_file, body)
    _, text, warnings = read_lore()
    assert len(text) <= LORE_CAP
    assert "sections" in warnings[0] and "truncated" in warnings[1]
    assert f"of {4 * 907 - 1} chars" in warnings[1], "the size is the text left after the section cut"


# ── start of a conversation ────────────────────────────────────────────


def test_lore_lands_in_message_zero_once_before_the_store_and_is_persisted(lore_file, make_app):
    _write(lore_file, STORY)
    app = make_app()
    first = _system_text(app)
    assert first.count(appsvc.LORE_HEADER) == 1 and "LORE-MARKER-4410" in first
    assert first.index(appsvc.LORE_HEADER) < first.index(appsvc.STORE_HEADER)
    assert _system_text(app) == first
    replayed = ConversationRepository.read(app.convo_dir / "convo.jsonl")[1][0]["content"]
    assert replayed == first, "the edit record carries message 0, lore included"


def test_a_resumed_conversation_keeps_its_snapshot(lore_file, make_app):
    _write(lore_file, STORY)
    first = make_app()
    saved = _system_text(first)
    _write(lore_file, "## CHANGED\nLORE-MARKER-0000\n")
    resumed = make_app()
    resumed.conversation[0] = {"role": "system", "content": saved}
    resumed._store_injected = ConversationRepository.read(first.convo_path)[0]["store_injected"]
    out = _system_text(resumed)
    assert out == saved and "LORE-MARKER-0000" not in out


def test_no_lore_file_means_no_block_and_no_behaviour_change(lore_file, make_app):
    app = make_app()
    content = _system_text(app)
    assert appsvc.LORE_HEADER not in content and "SOUL-MARKER-1188" in content


def test_a_cut_is_named_in_the_prompt_and_the_user_is_told(lore_file, make_app):
    _write(lore_file, "".join(f"## S{i}\nbody\n" for i in range(1, 7)))
    app = make_app()
    notices = []
    app._system = notices.append
    content = _system_text(app)
    assert "only the first 4 are injected" in content and "S5" not in content
    assert any("only the first 4" in n for n in notices)


# ── .agents vs .convos ─────────────────────────────────────────────────


def test_an_agents_seat_gets_lore_and_a_convos_era_archive_gets_nothing_and_no_error(lore_file, make_app):
    _write(lore_file, STORY)
    agents_seat = make_app()
    assert "LORE-MARKER-4410" in appsvc.lore_block(agents_seat)
    convos_era = SimpleNamespace(_agent_session=None, convo_dir=lore_file.parent / ".convos" / "old")
    assert appsvc.lore_block(convos_era) == ""
    assert appsvc.lore_block(SimpleNamespace()) == "", "no session attribute at all is the same"


# ── compaction ─────────────────────────────────────────────────────────


def test_lore_is_still_there_once_after_a_local_compaction(lore_file, make_app, monkeypatch):
    _write(lore_file, STORY)
    _bypass_owned_seat(monkeypatch)

    async def body():
        create, _ = _scripted_create([[_Chunk(content="a summary")]])
        app = _compact_app(make_app)
        async with app.run_test(size=(120, 40)) as pilot:
            _seed(app)
            app.conversation[0] = {"role": "system", "content": "sys"}
            assert appsvc.LORE_HEADER in _system_text(app)
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot)
            assert app.conversation[1]["content"].startswith("[Summary of earlier conversation")
            assert appsvc.STORE_HEADER not in app.conversation[0]["content"], "the store was cut"
            assert appsvc.LORE_HEADER in app.conversation[0]["content"], "the lore was not"
            text = _system_text(app)
            assert text.count(appsvc.LORE_HEADER) == 1 and text.count(appsvc.STORE_HEADER) == 1
            assert text.index(appsvc.LORE_HEADER) < text.index(appsvc.STORE_HEADER)
    _run(body())


@pytest.mark.asyncio
async def test_a_rebuilt_message_zero_gets_the_lore_back(lore_file, make_app):
    _write(lore_file, STORY)
    app = make_app()
    async with app.run_test(size=(120, 40)):
        _system_text(app)
        app.action_toggle_tools()  # the host rewrites message 0 from its builder
        assert appsvc.LORE_HEADER not in app.conversation[0]["content"]
        assert _system_text(app).count(appsvc.LORE_HEADER) == 1


# ── the Claude backend path ────────────────────────────────────────────


def test_claude_segment_prompt_carries_lore_and_so_does_the_fresh_segment_after_compaction(
        lore_file, tmp_path, make_app):
    from litetui.claude_turn import ledger_for, system_prompt_for

    _write(lore_file, STORY)
    app = make_app()
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(tmp_path))
    prompt = system_prompt_for(app, segment)
    assert appsvc.LORE_HEADER in prompt and "LORE-MARKER-4410" in prompt
    _write(lore_file, "## CHANGED\nLORE-MARKER-0000\n")
    assert system_prompt_for(app, ledger.segment(segment["id"])) == prompt, "a resume keeps the recorded prefix"
    # claude_compact.compact() ends the session and starts exactly this: new=True with the summary as seed
    fresh = ledger.select_segment(str(tmp_path), new=True, seed="the summary")
    after = system_prompt_for(app, fresh)
    assert appsvc.LORE_HEADER in after and "LORE-MARKER-0000" in after, "rebuilt from the file as it is now"
    assert after.index(appsvc.LORE_HEADER) < after.index(appsvc.STORE_HEADER)
