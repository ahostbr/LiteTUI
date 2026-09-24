"""`--convo <id>` must resume that conversation, not silently start a new one.

The flag was parsed, documented and threaded to `app._cli_convo_id`
(cli.py:62 -> app.py __init__) and then read by nothing. A launch that asked
for a specific conversation got a fresh one and no line saying otherwise, which
is the single outcome the flag exists to prevent.

Three layers, because the defect could live in any of them: that
`_apply_cli_args` reaches the wiring, that the wiring resumes the right
conversation, and that a flag naming nothing REFUSES instead of quietly
dropping the launch prompt into the fresh chat it was meant to replace.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from litetui import app as app_mod
from litetui import paths
from litetui.app import LiteTUI

_apply = LiteTUI._apply_cli_args.__wrapped__


@pytest.fixture(autouse=True)
def _isolated_convos(tmp_path, monkeypatch):
    """Per-test conversation store.

    Not a module-level `paths.CONVO_DIR = mkdtemp()`: that mutates global
    config for every test that runs after this file and leaks the directory.
    ConversationRepository.list_all reads paths.CONVO_DIR at call time
    (conversation.py:179), so a monkeypatched attribute is enough.
    """
    store = tmp_path / "convos"
    store.mkdir()
    monkeypatch.setattr(paths, "CONVO_DIR", store)
    return store


# -- the wiring is reached, and its refusal is honoured --------------------


class _FakeApp(SimpleNamespace):
    """Only the fields _apply_cli_args touches before it returns."""

    def __init__(self, convo_id, *, resolves=True, first_prompt=None):
        super().__init__(
            _cli_convo_id=convo_id,
            _cli_initial_model=None,
            _cli_system_prompt=None,
            _cli_thinking_level=None,
            _first_prompt=first_prompt,
            _launch_options=None,
            _cli_launch_error=None,
            available_models=[],
            _connect_settled=True,
            conversation=[],
            convo_id="fresh-conversation",
            commands=[],
            submitted=[],
            _said=[],
            _resolves=resolves,
        )

    _resume_cli_convo = LiteTUI._resume_cli_convo

    def _handle_command(self, cmd):
        self.commands.append(cmd)
        if self._resolves:  # what a successful /resume does: it moves us
            self.convo_id = cmd.split(maxsplit=1)[1]

    def _system(self, msg):
        self._said.append(msg)

    async def _ensure_chat_ready(self, timeout=None):
        return True

    def _submit_text(self, text, alt_chord=False):
        self.submitted.append(text)


def _run(app):
    asyncio.run(_apply(app))


def test_the_flag_is_dispatched_through_the_resume_path():
    app = _FakeApp("abc123")
    _run(app)
    assert app.commands == ["/resume abc123"]
    assert app._cli_launch_error is None


def test_no_flag_dispatches_nothing():
    app = _FakeApp(None)
    _run(app)
    assert app.commands == []


def test_a_blank_flag_never_opens_the_picker():
    # `/resume` with no argument opens the conversation picker. A launch flag
    # that degraded into a modal on a headless start would be worse than the
    # dead flag it replaced.
    app = _FakeApp("   ")
    _run(app)
    assert app.commands == []


def test_an_unresolved_id_blocks_the_launch_prompt():
    # The defect this exists for: the resume reported no match and the prompt
    # then went into the fresh chat anyway — a silent fallback onto exactly
    # the conversation the user ruled out.
    app = _FakeApp("no-such-convo", resolves=False, first_prompt="do the thing")
    _run(app)
    assert app.submitted == [], "the prompt must not land in the fresh chat"
    assert app._cli_launch_error
    assert any("blocked" in s for s in app._said)


def test_a_resolved_id_still_lets_the_prompt_through():
    app = _FakeApp("abc123", first_prompt="do the thing")
    _run(app)
    assert app.submitted == ["do the thing"]


# -- the wiring resumes the right conversation -----------------------------
#
# Mounted for real: _resume renders into the chat log, so an unmounted app
# raises ScreenStackError before reaching anything worth asserting.


def _mounted():
    app = app_mod.LiteTUI()
    app.connect = lambda: None
    app._connect = lambda: None
    return app


def _save(app, text):
    """Start a conversation, put a message in it, return its uuid."""
    app._new_convo()
    app._materialise_convo()
    # _append is the single choke point: live conversation AND disk.
    app._append({"role": "user", "content": text})
    return app.convo_id


@pytest.mark.asyncio
async def test_the_named_conversation_is_the_one_resumed():
    app = _mounted()
    async with app.run_test():
        wanted = _save(app, "resume me")
        _save(app, "not me")
        app._new_convo()
        started_in = app.convo_id
        app._cli_convo_id = wanted
        assert app._resume_cli_convo() is True
        assert app.convo_id == wanted
        assert app.convo_id != started_in
        assert any(m.get("content") == "resume me" for m in app.conversation)
        assert not any(m.get("content") == "not me" for m in app.conversation)


@pytest.mark.asyncio
async def test_a_full_id_resolves_even_though_resume_accepts_prefixes():
    app = _mounted()
    async with app.run_test():
        wanted = _save(app, "prefix me")
        app._new_convo()
        app._cli_convo_id = wanted
        assert app._resume_cli_convo() is True
        assert app.convo_id == wanted


@pytest.mark.asyncio
async def test_an_unambiguous_prefix_is_accepted():
    app = _mounted()
    async with app.run_test():
        wanted = _save(app, "prefix me")
        app._new_convo()
        app._cli_convo_id = wanted[:8]
        assert app._resume_cli_convo() is True
        assert app.convo_id == wanted


@pytest.mark.asyncio
async def test_an_ambiguous_prefix_is_refused_rather_than_resolved(monkeypatch):
    # /resume picks the FIRST row a prefix matches, which is fine for a human
    # reading the list. A launch flag doing it would choose between
    # conversations with nobody watching, so it refuses instead.
    app = _mounted()
    async with app.run_test():
        _save(app, "one")
        app._new_convo()
        here = app.convo_id
        said = []
        # Two names, one sink: _cmd_resume prints through system_message while
        # _resume_cli_convo uses _system, and _system is a class-level alias so
        # an instance patch of one does not cover the other.
        app.system_message = said.append
        app._system = said.append
        rows = [(paths.CONVO_DIR / uid / "convo.jsonl", {}, [])
                for uid in ("shared-aaaa", "shared-bbbb")]
        monkeypatch.setattr(
            "litetui.conversation.ConversationRepository.list_all",
            staticmethod(lambda: rows))
        app._cli_convo_id = "shared-"
        assert app._resume_cli_convo() is False
        assert app.convo_id == here, "an ambiguous flag must resume nothing"
        assert any("more than one conversation" in s for s in said)


@pytest.mark.asyncio
async def test_an_unknown_id_reports_and_creates_no_conversation():
    app = _mounted()
    async with app.run_test():
        _save(app, "existing")
        app._new_convo()
        app._materialise_convo()
        before = {p.name for p in paths.CONVO_DIR.iterdir() if p.is_dir()}
        started_in = app.convo_id
        said = []
        # _cmd_resume prints through system_message; _system is only a
        # class-level alias for it, so patching the alias misses this.
        app.system_message = said.append
        app._cli_convo_id = "0000000-not-a-real-conversation"
        assert app._resume_cli_convo() is False
        after = {p.name for p in paths.CONVO_DIR.iterdir() if p.is_dir()}
        assert after == before, "a failed resume must not leave a conversation behind"
        assert app.convo_id == started_in, "and must not move off the current one"
        assert any("0000000-not-a-real-conversation" in s for s in said)
