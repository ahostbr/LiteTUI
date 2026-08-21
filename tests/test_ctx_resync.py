"""A window read while the model was unloaded must not stick for the session.

WHAT RYAN SAW. The footer read `ctx 56,373 / 262,144 max` while LM Studio was
serving that model at 120,064. 262,144 is the model's CEILING, and the "max"
marker is the app correctly saying so — but the reading was STALE: the API
reported the model loaded at 120,064 the whole time.

WHY IT STUCK. `_resync_ctx_if_stale` was wired into ONE of the agent loop's
exits — the plain-answer branch. A turn that ends by being STOPPED, or by
hitting the tool-iteration cap, skipped it entirely. So a reading taken while
the model was unloaded survived every subsequent turn.

🔴 AND THE CONSEQUENCE IS NOT COSMETIC. `_maybe_autocompact` refuses to divide
by a ceiling (deliberately — 80% of 262,144 is unreachable inside a 120k
window). With ctx_loaded stuck False, AUTO-COMPACT NEVER FIRES AT ALL. Refusing
to guess had become refusing to ever act, which is the exact failure the
refusal was written to avoid.

⏱️ THE TIMING TRAP, recorded because it cost a wrong conclusion. The re-read is
a Textual worker that shells out to an HTTP call and takes ~2 SECONDS. A first
attempt to verify this waited ~1 second, saw the old values, and concluded the
mechanism was broken. It was not — the instrument was too impatient. Anything
here that polls must allow seconds, and must FAIL LOUDLY on timeout rather than
report the pre-fetch state as a result.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import app as app_mod
from settings import Settings

app_mod.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-ctxresync-"))

LOADED, CEILING = 120064, 262144


def _app():
    a = app_mod.LiteTUI()
    a.settings = Settings(default_model="m", default_context_length=120000)
    a.model_id = "m"
    a._connect = lambda: None
    a._system = lambda *x, **k: None
    return a


async def _settle(pilot, predicate, seconds: float = 8.0):
    """Wait for a condition, generously, and say so if it never happens.

    Returning quietly on timeout would let a broken mechanism read as a pass —
    the exact way the first investigation of this bug went wrong.
    """
    deadline = seconds / 0.05
    for _ in range(int(deadline)):
        await pilot.pause()
        await asyncio.sleep(0.05)
        if predicate():
            return True
    return False


@pytest.mark.asyncio
async def test_a_stale_ceiling_is_replaced_once_the_model_is_loaded(monkeypatch):
    monkeypatch.setattr(
        app_mod.LiteTUI, "_read_model_info", staticmethod(lambda mid: (LOADED, "llm", True))
    )
    a = _app()
    async with a.run_test() as pilot:
        a.ctx_max, a.ctx_loaded = CEILING, False  # the state on Ryan's screen
        a._resync_ctx_if_stale()
        ok = await _settle(pilot, lambda: a.ctx_loaded)
        assert ok, f"never re-read: ctx_max={a.ctx_max} ctx_loaded={a.ctx_loaded}"
    assert a.ctx_max == LOADED


@pytest.mark.asyncio
async def test_a_known_good_window_is_not_re_read_every_turn(monkeypatch):
    """The re-read costs an HTTP round trip; it must be conditional."""
    calls = []
    monkeypatch.setattr(
        app_mod.LiteTUI,
        "_read_model_info",
        staticmethod(lambda mid: (calls.append(mid), (LOADED, "llm", True))[1]),
    )
    a = _app()
    async with a.run_test() as pilot:
        a.ctx_max, a.ctx_loaded = LOADED, True
        a._resync_ctx_if_stale()
        await _settle(pilot, lambda: False, seconds=1.0)
    assert calls == [], "re-read a window already known to be the loaded one"


def test_the_resync_runs_at_turn_START_not_only_at_one_exit():
    """Source-level, deliberately: the defect was an OMISSION on some of the
    loop's exits, and the loop has at least three (plain answer, stopped, and
    the tool-iteration cap). Turn START has no branches at all — every turn
    passes through it — so that is where the guarantee lives.

    A behavioural test on one exit would leave the other two free to regress,
    which is precisely what happened.
    """
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    stream = src.split("async def _stream", 1)[1]
    head = stream.split("for _iteration in range", 1)[0]
    assert "_resync_ctx_if_stale()" in head, (
        "the turn-start resync is gone; a window read while the model was "
        "unloaded will stick for the whole session and auto-compact will never fire"
    )


def test_autocompact_still_refuses_a_ceiling():
    """The refusal and the resync are a PAIR. Without the resync the refusal
    becomes permanent silence; without the refusal the threshold is computed
    against a number the session does not have. Neither is safe alone."""
    a = _app()
    fired = []
    a._handle_command = lambda c: fired.append(c)
    a.ctx_max, a.ctx_used, a.ctx_loaded = CEILING, 250_000, False
    a._maybe_autocompact()
    assert fired == []

    a.ctx_max, a.ctx_used, a.ctx_loaded = LOADED, int(LOADED * 0.95), True
    a._maybe_autocompact()
    assert fired == ["/compact"], "the pair must still fire on a real window"
