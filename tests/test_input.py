"""Paste, copy and branding — driven through Textual's real pilot.

compose() cannot run outside a live App, so these go through run_test(): a real
DOM, real bindings, real focus. The clipboard reader is the only thing stubbed,
because it talks to the OS.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

import pytest

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _script_guard  # tests/ is sys.path[0] when a file is run as a script

# 🔴 THIS FILE BOOTS A REAL `LiteTUI()`, WHICH LOADS THE REPO ROOT'S settings.json
# — the developer's own gitignored config. `conftest.py`'s autouse guard cannot
# reach a script-style file, so without this the modal/sidebar assertions below
# read whatever `dialog_style` the human happens to be running. Measured
# 2026-08-28: they failed 19/22 on Ryan's box (sidebar) and passed 22/22 against
# the same tree with the value set to modal.
# The env pin is the OTHER half and it is not optional: an empty sandbox boots
# the first-boot engine picker, and app.py:3080 makes paste a no-op under any
# modal — guard alone 15/22, guard + pin 22/22. See tests/_script_guard.py.
_script_guard.pin_first_boot_env()
_script_guard.redirect_live_settings()

# Deliberately a SECOND import block: the env pin and the settings redirect must
# run BETWEEN these imports, not before or after them.
from litetui import app as m
from litetui import paths

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-input-"))

ok = []
#: The labels that FAILED. `ok` is bools, which is all an exit status needed; a
#: pytest arm has to be able to say WHICH check failed, and a bool cannot.
#: Recorded alongside rather than by changing `ok`, so `sum(ok)` / `all(ok)` /
#: `len(ok)` keep meaning exactly what they meant (T700).
failures: list[str] = []


def chk(label, cond):
    ok.append(bool(cond))
    if not cond:
        failures.append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def make_app(clip=None):
    a = m.LiteTUI()
    a.available_models = ["a-model", "b-model"]
    a.model_id = "b-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._clipboard_text = lambda: clip
    return a


async def main():
    print("=== branding ===")
    chk("TITLE is LiteTUI", m.LiteTUI.TITLE == "LiteTUI")
    chk("old class name is gone", not hasattr(m, "LMStudioChat"))
    src = open(Path(__file__).resolve().parent.parent / "src" / "litetui" / "app.py", encoding="utf-8").read()
    chk("no 'LM Studio Chat' string left in source", "LM Studio Chat" not in src)

    print("\n=== Ctrl+V pastes clipboard TEXT into the input ===")
    a = make_app(clip="hello world")
    async with a.run_test() as pilot:
        inp = a.query_one("#message-input", m.Input)
        inp.value = ""
        await pilot.press("ctrl+v")
        await pilot.pause()
        chk("clipboard text landed in the input", inp.value == "hello world")
        chk("cursor sits after the pasted text", inp.cursor_position == len("hello world"))

    print("\n=== paste SPLICES at the cursor, not append ===")
    a = make_app(clip="MID")
    async with a.run_test() as pilot:
        inp = a.query_one("#message-input", m.Input)
        inp.value = "ab"
        inp.cursor_position = 1
        await pilot.press("ctrl+v")
        await pilot.pause()
        chk("spliced at the cursor", inp.value == "aMIDb")

    print("\n=== a MULTI-LINE paste keeps every line (the submit-on-newline trap) ===")
    a = make_app(clip="line one\r\nline two\r\n\r\nline three")
    async with a.run_test() as pilot:
        inp = a.query_one("#message-input", m.Input)
        inp.value = ""
        await pilot.press("ctrl+v")
        await pilot.pause()
        chk("no newline survives into a single-line Input", "\n" not in inp.value)
        for frag in ("line one", "line two", "line three"):
            chk(f"kept: {frag}", frag in inp.value)
        chk("blank line did not become a double space", "  " not in inp.value)

    print("\n=== empty / imageless clipboard is a WARNING, not a crash ===")
    a = make_app(clip=None)
    async with a.run_test() as pilot:
        inp = a.query_one("#message-input", m.Input)
        inp.value = "keep me"
        await pilot.press("ctrl+v")
        await pilot.pause()
        chk("input untouched", inp.value == "keep me")

    print("\n=== bracketed paste (terminals that DO send it) ===")
    a = make_app(clip=None)
    async with a.run_test() as pilot:
        inp = a.query_one("#message-input", m.Input)
        inp.value = ""
        a.on_paste(m.events.Paste("bracketed text"))
        await pilot.pause()
        chk("bracketed paste inserted", inp.value == "bracketed text")

    print("\n=== paste does NOT hijack a modal ===")
    a = make_app(clip="should not appear")
    async with a.run_test() as pilot:
        a._handle_command("/model")
        await pilot.pause()
        chk("a modal is open", isinstance(a.screen, m.ModalScreen))
        a.action_paste_text()
        await pilot.pause()
        chk("modal still open after Ctrl+V", isinstance(a.screen, m.ModalScreen))
        await pilot.press("escape")
        await pilot.pause()
        chk("input was not written to", a.query_one("#message-input", m.Input).value == "")

    print("\n=== copy: the selection machinery is reachable by a key ===")
    keys = {b.key for b in m.LiteTUI.BINDINGS}
    chk("ctrl+shift+c is bound", "ctrl+shift+c" in keys)
    chk("ctrl+v is bound", "ctrl+v" in keys)
    chk("ctrl+c NOT stolen (Textual uses it for quit)", "ctrl+c" not in keys)
    from textual.screen import Screen
    chk("Screen.action_copy_text exists to bind to", hasattr(Screen, "action_copy_text"))
    chk("selection is enabled app-wide", m.LiteTUI.ALLOW_SELECT is True)

    print("\n=== no regression: Ctrl+O image path still bound ===")
    chk("ctrl+o still bound", "ctrl+o" in keys)



# ── the same checks, as a pytest arm (T700) ─────────────────────────────
#
# 🔴 THIS FILE IS NAMED `test_*` AND NOTHING HAS EVER RUN IT. A module-level
# exit raises SystemExit during collection, which pytest reports as
# INTERNALERROR and which abandons the WHOLE invocation — not just this file.
# Ten files in this directory were in that state (T699 fixed two, T700 the
# rest); each abort hid the others, which is why the class kept looking small.
#
# ⚠️ THE EVENT LOOP MOVES TOO. `asyncio.run(main())` ran at module level, so
# collection spun a loop and then aborted. Awaiting `main` inside the arm lets
# pytest-asyncio own the loop instead of a second one being started underneath
# it.


@pytest.mark.asyncio
async def test_every_check_in_this_file_passed() -> None:
    await main()
    assert ok, "no check ran"
    assert failures == [], failures


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
