"""The store snapshot is derived from message 0, so it survives anything that rewrites it.

Ctrl+T, plan mode and /new each replace message 0 with the base prompt. A flag
that remembered "already injected" outlived that content and kept the store out
until the process restarted. The marker in message 0 is the only state: absent
means inject a fresh snapshot from disk, present means leave it alone.
"""
from __future__ import annotations

import pytest
from test_compaction_ui import _Chunk, _run, _scripted_create, _seed, _settle, off_local_lm_studio

from litetui import app as app_mod
from litetui import appsvc, settings
from litetui.agent_launch_context import ordinary
from litetui.plugins import convo
from litetui.settings import Settings

OLD = "SOUL-LINE-ALPHA-1001"
NEW = "SOUL-LINE-BETA-2002"


@pytest.fixture
def make_app(tmp_path):
    made = []

    def build(soul: str = OLD) -> app_mod.LiteTUI:
        session = ordinary(tmp_path, settings.load())
        app = app_mod.LiteTUI(agent_session=session)
        made.append(app)
        app._connect = lambda: None
        app._fetch_ctx_window = lambda: None
        app._apply_context_length = lambda: None
        app._materialise_convo()
        for name in ("memory.md", "soul.md", "handoff.md"):
            (session.memory_root / name).write_text("", encoding="utf-8")
        (session.memory_root / "soul.md").write_text(soul + "\n", encoding="utf-8")
        return app

    yield build
    for app in reversed(made):
        try:
            app.store.release()
        finally:
            app._agent_session.release()


def _soul(app: app_mod.LiteTUI, text: str) -> None:
    (app._agent_session.memory_root / "soul.md").write_text(text + "\n", encoding="utf-8")


def _records(app: app_mod.LiteTUI) -> int:
    return len((app.convo_dir / "convo.jsonl").read_text(encoding="utf-8").splitlines())


def _system_text(app: app_mod.LiteTUI) -> str:
    return app._request_messages()[0]["content"]


# (a) the two in-place rewrites of message 0

def test_one_header_constant_serves_both_modules():
    assert app_mod.STORE_HEADER is appsvc.STORE_HEADER


@pytest.mark.asyncio
async def test_tools_toggle_brings_the_store_back_once(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)):
        assert _system_text(app).count(appsvc.STORE_HEADER) == 1
        _soul(app, NEW)
        app.action_toggle_tools()
        assert appsvc.STORE_HEADER not in app.conversation[0]["content"]
        text = _system_text(app)
        assert text.count(appsvc.STORE_HEADER) == 1
        assert NEW in text and OLD not in text


@pytest.mark.asyncio
async def test_plan_mode_brings_the_store_back_once(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)):
        assert _system_text(app).count(appsvc.STORE_HEADER) == 1
        _soul(app, NEW)
        assert app.set_plan_mode(True, announce=False)
        text = _system_text(app)
        assert text.count(appsvc.STORE_HEADER) == 1
        assert NEW in text and OLD not in text


# (b) /new

@pytest.mark.asyncio
async def test_new_conversation_gets_a_fresh_snapshot(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)) as pilot:
        assert _system_text(app).count(appsvc.STORE_HEADER) == 1
        _soul(app, NEW)
        convo._cmd_new(app, "new", "")
        await pilot.pause()
        assert appsvc.STORE_HEADER not in app.conversation[0]["content"]
        text = _system_text(app)
        assert text.count(appsvc.STORE_HEADER) == 1
        assert NEW in text and OLD not in text


# (c) resume keeps what it was given

def test_resumed_snapshot_is_not_reinjected(make_app):
    first = make_app()
    saved = _system_text(first)
    resumed = make_app(soul=NEW)
    resumed.conversation[0] = {"role": "system", "content": saved}
    before = _records(resumed)
    out = _system_text(resumed)
    assert out == saved and out.count(appsvc.STORE_HEADER) == 1
    assert OLD in out and NEW not in out
    assert _records(resumed) == before, "a resumed snapshot must not write an edit record"


# (d) local compaction rebuild

def test_compaction_does_not_carry_the_stale_snapshot_forward(make_app, monkeypatch):
    # Owned-seat authority and startup registration are not under test here;
    # the fixture app is rebound to a custom backend the fixture seat does not own.
    async def ready(self, *, timeout=None):
        return None

    monkeypatch.setattr(app_mod.LiteTUI, "_validate_owned_execution", lambda self: None)
    monkeypatch.setattr(app_mod.LiteTUI, "_ensure_chat_ready", ready)

    async def body():
        create, _ = _scripted_create([[_Chunk(content="a summary")]])
        app = make_app()
        app.settings = Settings(clear_screen_after_compact=False, compact_keep_recent=2,
                                wake_after_compact=False)
        off_local_lm_studio(app)
        async with app.run_test(size=(120, 40)) as pilot:
            _seed(app)
            assert OLD in _system_text(app)
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot)
            assert app.conversation[1]["content"].startswith("[Summary of earlier conversation")
            assert appsvc.STORE_HEADER not in app.conversation[0]["content"]
            _soul(app, NEW)
            text = _system_text(app)
            assert text.count(appsvc.STORE_HEADER) == 1
            assert NEW in text and OLD not in text
    _run(body())


# (e) one edit record per injection, never per turn

@pytest.mark.asyncio
async def test_reinjection_writes_one_record_not_one_per_turn(make_app):
    app = make_app()
    async with app.run_test(size=(120, 40)):
        _system_text(app)
        app.action_toggle_tools()
        after_toggle = _records(app)
        for _ in range(10):
            _system_text(app)
        assert _records(app) == after_toggle + 1


# (f) strip_store_block

def _store_text(app) -> str:
    return appsvc.store_block(app)


def test_strip_leaves_content_without_the_header_unchanged():
    from litetui.appsvc import strip_store_block
    plain = "base prompt\n\nsecond paragraph"
    assert strip_store_block(plain) == plain
    assert strip_store_block("") == ""


def test_strip_removes_the_block_and_is_idempotent(make_app):
    from litetui.appsvc import strip_store_block
    app = make_app()
    base = "base prompt"
    full = base + _store_text(app)
    assert appsvc.STORE_HEADER in full
    once = strip_store_block(full)
    assert once == base
    assert strip_store_block(once) == once


def test_strip_keeps_an_index_block_that_precedes_the_store(make_app):
    from litetui.appsvc import strip_store_block
    app = make_app()
    index = f"\n\n{appsvc.INDEX_HEADER}\n\nINDEX-BODY\n"
    full = "base prompt" + index + _store_text(app)
    out = strip_store_block(full)
    assert out == "base prompt" + index
    assert appsvc.STORE_HEADER not in out
