"""A conversation is created on the first user message, never at boot.

Reported: "even if im loading the app todo a /resume it will create a convo
before i can do /resume" — so the list you opened /resume to read filled with
the debris of opening it.

These assert the ABSENCE of files, which is the whole feature, plus the two
things absence must not cost: the system prompt still reaching disk, and a
resumed conversation being appended to rather than replaced.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

import app as app_mod


def _fresh_store() -> Path:
    d = Path(tempfile.mkdtemp(prefix="convos-lazy-"))
    app_mod.CONVO_DIR = d
    return d


def _count(d: Path) -> int:
    return len([p for p in d.iterdir() if p.is_dir()]) if d.exists() else 0


def _app():
    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    return a


@pytest.mark.asyncio
async def test_booting_creates_nothing():
    d = _fresh_store()
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        assert _count(d) == 0, "booting the app wrote a conversation to disk"
        # …but it is STAGED: the footer and /clear need an id to name.
        assert a.convo_id
        assert a.convo_path is not None
        assert a._convo_pending is True


@pytest.mark.asyncio
async def test_commands_before_the_first_message_create_nothing():
    """The reported case: launch, look around, resume. Nothing should be born."""
    d = _fresh_store()
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        a._handle_command("/help")
        await pilot.pause()
        a.screen.dismiss(None)
        await pilot.pause()
        a._handle_command("/clear")
        await pilot.pause()
        a._handle_command("/clear-screen")
        await pilot.pause()
        assert _count(d) == 0, "a command created a conversation"


@pytest.mark.asyncio
async def test_the_first_user_message_creates_exactly_one():
    d = _fresh_store()
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        a._materialise_convo()
        assert _count(d) == 1
        # Idempotent — a second call must not mint another.
        a._materialise_convo()
        assert _count(d) == 1


@pytest.mark.asyncio
async def test_the_system_prompt_survives_deferral():
    """Deferring the write must not lose what was already in memory.

    The system prompt is appended at boot, long before the directory exists.
    _materialise_convo snapshots self.conversation, and that is what carries it.
    Without this the file would open with no system message and a resumed
    conversation would silently lose its instructions.
    """
    d = _fresh_store()
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        assert any(m.get("role") == "system" for m in a.conversation), "fixture is wrong"
        a._materialise_convo()
        recs = [json.loads(l) for l in a.convo_path.read_text(encoding="utf-8").splitlines()]
        kinds = [r.get("type") for r in recs]
        assert "meta" in kinds
        assert "snapshot" in kinds, "nothing carried the pre-creation state to disk"
        snap = next(r for r in recs if r.get("type") == "snapshot")
        roles = [m.get("role") for m in snap["messages"]]
        assert "system" in roles, "the system prompt was lost by deferring the write"


@pytest.mark.asyncio
async def test_resume_adopts_without_minting_a_throwaway():
    """The exact reported scenario, end to end."""
    d = _fresh_store()

    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        a._materialise_convo()
        a._append({"role": "user", "content": "the real one"})
        await pilot.pause()
    assert _count(d) == 1

    b = _app()
    async with b.run_test() as pilot:
        await pilot.pause()
        assert _count(d) == 1, "booting to resume minted a throwaway conversation"
        rows = b._list_convos()
        b._resume(rows[0][0])
        await pilot.pause()
        assert _count(d) == 1
        assert b._convo_pending is False, "a resumed conversation must not be 'pending'"

        # And a later message APPENDS to the resumed store rather than creating.
        b._append({"role": "user", "content": "continuing"})
        await pilot.pause()
        assert _count(d) == 1
        assert "continuing" in b.convo_path.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_reading_the_store_never_creates_it():
    """Reads must not have side effects — that is how this bug worked."""
    d = _fresh_store()
    a = _app()
    async with a.run_test() as pilot:
        await pilot.pause()
        a._read_store_file("memory.md", 1000)
        a._read_store_file("soul.md", 1000)
        assert _count(d) == 0, "reading the store created it"
