"""Paste, copy and branding — driven through Textual's real pilot.

compose() cannot run outside a live App, so these go through run_test(): a real
DOM, real bindings, real focus. The clipboard reader is the only thing stubbed,
because it talks to the OS.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app as m

m.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-input-"))

ok = []


def chk(label, cond):
    ok.append(bool(cond))
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
    src = open(Path(__file__).resolve().parent.parent / "app.py", encoding="utf-8").read()
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


asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
