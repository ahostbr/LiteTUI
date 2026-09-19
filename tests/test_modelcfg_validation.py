"""/modelcfg refuses to SAVE AND APPLY a value it cannot vouch for. T263.

🔴 WHAT HAPPENED, on a live walk of 632c28d (Sentinel, 22:54, router cuda:b9360).
`Ctrl+A` in a Textual `Input` is HOME, not select-all. So a typed "8192" landed
in FRONT of the existing "50000" and a typed "none" in front of the existing
projector path, and Ctrl+S wrote both into `.llama/litetui-models.ini` and
RELOADED THE MODEL ON THEM:

    ctx-size = 819250000
    mmproj   = noneC:/lmstudio/models/unsloth/Qwen3.5-0.8B-GGUF/mmproj-F32.gguf

The panel reported "Saved model config … load settings applied". Nothing
validated either field. A 819-million context is an OOM request that the router
happened to clamp; Ryan had already had one near-OOM that night from a 100k
context on a 27B. A projector path that does not exist is neither "none" nor a
file, so `write_preset_ini` emitted it verbatim.

⚠️ `_collect_group` DID NOT CATCH THIS AND WAS NEVER GOING TO. Its only check is
`int(raw)` — and "819250000" IS a valid int. Its error message,
"is not a valid number", is true of the cases it catches and says nothing about
the case that matters: a number that parses and is absurd.

⬜ WHAT THE BOUND IS, HONESTLY. `[512, CTX_TYPO_CEILING]` is a TYPO GUARD, not a
VRAM model. A per-model maximum is NOT available at the panel: `ModelRow` has no
such field, and the only real ceiling in the codebase (`max_context_length`) is
LM Studio's and lives behind a backend round-trip that has no business in a
keypress handler. So this rejects values that are orders of magnitude wrong —
819,250,000 is rejected by a factor of ~780 — and does NOT claim to know what
this model can actually hold. Saying "the max is 1048576" would be a number the
code cannot stand behind; saying "that is not a plausible context length" is one
it can.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from _settle import settle_until
from litetui import app as m
from litetui.plugins.model_switch import ModelConfigBody, ModelConfigScreen
from textual.widgets import Input, Static

#: Sentinel's two inputs, verbatim from the walk.
BAD_CTX = "819250000"
BAD_MMPROJ = ("noneC:/lmstudio/models/unsloth/"
              "Qwen3.5-0.8B-GGUF/mmproj-F32.gguf")


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def _panel(a, pilot):
    a.push_screen(ModelConfigScreen("a-model"))
    await settle_until(pilot, lambda: bool(a.screen.query(ModelConfigBody)))
    for _ in range(6):
        await pilot.pause()
    return a.screen.query_one(ModelConfigBody)


async def _apply_with(a, pilot, field: str, value: str):
    """Type `value` into `field`, press apply, and report what happened.

    Returns (panel_still_open, error_text, saved_cfg).
    """
    panel = await _panel(a, pilot)
    panel.query_one(field, Input).value = value
    await pilot.pause()
    panel.action_apply()
    for _ in range(6):
        await pilot.pause()

    still_open = bool(a.screen.query(ModelConfigBody))
    err = ""
    if still_open:
        found = a.screen.query("#mc-error")
        if found:
            err = str(found.first().renderable if hasattr(found.first(), "renderable")
                      else found.first().render())
    saved = (a.settings.llama_load_settings or {}).get("a-model", {})
    return still_open, err, saved


@pytest.mark.asyncio
async def test_an_absurd_context_is_refused_and_the_panel_stays_open():
    """🔴 THE EXACT VALUE FROM THE WALK. It parses as an int, so the only check
    that existed passed it straight through to the ini and to a model reload."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        still_open, err, saved = await _apply_with(a, pilot, "#ld-ctx", BAD_CTX)

        assert saved.get("ctx") != int(BAD_CTX), (
            f"{BAD_CTX} was SAVED — this is the value that reloaded a 27B on a "
            "819-million-token context request"
        )
        assert still_open, (
            "the panel closed on a refused save: the user loses the rest of the "
            "form and has no idea which field was wrong"
        )
        assert "ctx" in err.lower(), (
            f"the error line does not name the field that failed: {err!r}"
        )


@pytest.mark.asyncio
async def test_a_projector_that_is_neither_none_nor_a_file_is_refused():
    """The other half of the walk: "none" typed in FRONT of a path.

    ⚠️ IT MUST FAIL AS *BOTH* CHECKS, and the message has to say which. It is
    not the T245 sentinel (`is_no_projector` is an exact, stripped,
    case-insensitive match — see test_mmproj_autopair.py) and it is not a file
    that exists. A message naming only one of those sends the reader to check
    the wrong thing.
    """
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        still_open, err, saved = await _apply_with(a, pilot, "#ld-mmproj", BAD_MMPROJ)

        assert saved.get("mmproj") != BAD_MMPROJ, (
            "a projector path that does not exist was SAVED and would be "
            "emitted into the ini verbatim"
        )
        assert still_open, "the panel closed on a refused save"
        assert "mmproj" in err.lower(), (
            f"the error line does not name the field that failed: {err!r}"
        )


@pytest.mark.asyncio
async def test_a_GOOD_value_still_saves():
    """The polarity that keeps this from being "validation" that refuses
    everything. Both arms above are also satisfied by a panel that never saves."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        panel = await _panel(a, pilot)
        panel.query_one("#ld-ctx", Input).value = "8192"
        await pilot.pause()
        panel.action_apply()
        for _ in range(6):
            await pilot.pause()

        saved = (a.settings.llama_load_settings or {}).get("a-model", {})
        assert saved.get("ctx") == 8192, (
            f"a perfectly ordinary context length was refused: {saved!r}"
        )


@pytest.mark.asyncio
async def test_none_is_still_accepted_for_the_projector():
    """T245's sentinel must survive the new check — the whole point of `none` is
    that it is a legal, deliberate value, and a file-existence check that did not
    know about it would reject the one answer T245 exists to provide."""
    a = make_app()
    async with a.run_test(size=(120, 40)) as pilot:
        panel = await _panel(a, pilot)
        panel.query_one("#ld-mmproj", Input).value = "none"
        await pilot.pause()
        panel.action_apply()
        for _ in range(6):
            await pilot.pause()

        saved = (a.settings.llama_load_settings or {}).get("a-model", {})
        assert saved.get("mmproj") == "none", (
            f"the T245 sentinel was rejected by the T263 validator: {saved!r}"
        )
