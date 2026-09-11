"""A `--rpc` child never causes a model load. An interactive session still does.

T594. Ryan, 2026-09-10 22:0x, after six probe children of mine put a second 27B
into VRAM beside the one he was using: no model is loaded without checking first
and asking.

🔴 THE DISCRIMINATOR IS HEADLESS vs INTERACTIVE, NOT THE BACKEND. LM Studio
JIT-loads whatever a chat request names, and `_chat_ready_sync` chose that
deliberately — its docstring names D2/D11 and says refusing "would break chats
that work today". That is right for a person at a keyboard. It is wrong for a
child a consult panel spawned, where nobody is present to notice 18 GB vanish.
So the refusal is scoped to `--rpc`, and the last arm here is a CONTROL that the
interactive path still loads, so nobody "fixes" D2/D11 later by widening this.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as app_mod  # noqa: E402
from litetui import llm_backend as be  # noqa: E402


def _app(model_id, loaded, *, rpc=True):
    """A LiteTUI shell whose backend reports `loaded` as resident."""
    a = app_mod.LiteTUI.__new__(app_mod.LiteTUI)
    a._rpc = rpc
    a.model_id = model_id
    a.backend = SimpleNamespace(loaded_models=lambda: list(loaded))
    a.emitted: list[dict] = []
    a._rpc_emit = a.emitted.append
    return a


# ── the three branches ──────────────────────────────────────────────────────

def test_the_selected_model_is_resident_so_it_proceeds():
    a = _app("qwen/a", ["qwen/a"])
    assert a._headless_model_decision() == ("ok", "qwen/a", "")


def test_exactly_one_OTHER_is_resident_so_it_substitutes_and_says_so():
    a = _app("qwen/a", ["ektome-b"])
    action, model, why = a._headless_model_decision()
    assert (action, model) == ("substitute", "ektome-b")
    assert "not loaded" in why and "ektome-b" in why, why


def test_nothing_is_resident_so_it_REFUSES():
    a = _app("qwen/a", [])
    action, model, why = a._headless_model_decision()
    assert (action, model) == ("refuse", None)
    assert "(none)" in why and "does not load models" in why, why


def test_several_are_resident_and_none_is_the_one_asked_for_so_it_REFUSES():
    """Substituting would be picking one on the user's behalf — with two
    resident models there is no obvious answer, and guessing is how a consult
    panel silently reports the wrong model's opinion."""
    a = _app("qwen/a", ["b", "c"])
    action, _, why = a._headless_model_decision()
    assert action == "refuse"
    assert "b, c" in why, why


# ── the negative control: no request LEAVES for an unloaded id ──────────────

@pytest.mark.asyncio
async def test_NO_chat_request_leaves_an_rpc_child_for_an_unloaded_id():
    """🔴 THE ARM THAT MATTERS, and it needs its own positive half.

    "nothing was sent" is satisfied by a harness that could never send
    anything, so `ensure_chat_ready` below RECORDS its calls and the control
    underneath proves the same harness does let one through. Without that pair
    this asserts that my fake was never called.
    """
    reached: list[str | None] = []

    a = _app("qwen/a", [])
    a.backend.ensure_chat_ready = lambda key: reached.append(key)

    with pytest.raises(be.BackendError) as exc:
        await app_mod.LiteTUI._ensure_chat_ready(a)

    assert reached == [], "a request reached the backend for an unloaded model"
    assert "not loaded" in str(exc.value)
    assert [e.get("kind") for e in a.emitted] == ["model_not_loaded"], (
        f"the refusal must be announced on the wire, got {a.emitted}"
    )


@pytest.mark.asyncio
async def test_CONTROL_the_same_harness_DOES_pass_a_loaded_id_through():
    """The positive half. If this fails the arm above proves nothing."""
    reached: list[str | None] = []

    a = _app("qwen/a", ["qwen/a"])
    a.backend.ensure_chat_ready = lambda key: reached.append(key)

    await app_mod.LiteTUI._ensure_chat_ready(a)
    assert reached == ["qwen/a"], f"a resident model was not passed through: {reached}"


# ── the control that keeps D2/D11 alive ────────────────────────────────────

@pytest.mark.asyncio
async def test_CONTROL_an_INTERACTIVE_session_still_reaches_the_backend_cold():
    """⚠️ DO NOT "FIX" THIS BY WIDENING THE REFUSAL.

    An interactive LiteTUI on a cold model must still reach the backend, which
    is what lets LM Studio JIT-load it — the convenience `_chat_ready_sync`
    documents under D2/D11. Ryan's own seat is that person. The headless gate
    is scoped to `--rpc` precisely so this stays true.
    """
    reached: list[str | None] = []

    a = _app("qwen/a", [], rpc=False)      # nothing loaded, and NOT headless
    a.backend.ensure_chat_ready = lambda key: reached.append(key)

    await app_mod.LiteTUI._ensure_chat_ready(a)
    assert reached == ["qwen/a"], (
        "the interactive path stopped reaching the backend on a cold model — "
        "that breaks the JIT-load D2/D11 asked for"
    )
    assert a.emitted == [], "an interactive session must not emit rpc events"
