"""Drive the modals through Textual's headless pilot — a real running app.

compose() cannot be called outside a live App (the container context managers
need one), so stubbing it proves nothing. run_test() gives an actual app with a
real DOM, so clicking and key presses are exercised rather than simulated.
"""
import asyncio, sys, tempfile, types
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
from plugins.help_plugin import HelpScreen
import paths

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-modal-"))
ok = []
def chk(label, cond):
    ok.append(bool(cond)); print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model", "b-model", "c-model"]
    a.model_id = "b-model"
    # never touch the network in a test
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    return a


async def main():
    print("=== /model opens a clickable picker ===")
    a = make_app()
    async with a.run_test() as pilot:
        a._handle_command("/model")
        await pilot.pause()
        scr = a.screen
        chk("a PickerScreen is on top", isinstance(scr, m.PickerScreen))
        ol = scr.query_one(m.OptionList)
        chk("one row per model", ol.option_count == 3)
        chk("current model is pre-highlighted", ol.highlighted == 1)
        labels = [str(ol.get_option_at_index(i).prompt) for i in range(ol.option_count)]
        chk("current row is marked with the arrow", labels[1].startswith("▸"))
        chk("others are not marked", not labels[0].startswith("▸"))

        # move down and select with the keyboard
        await pilot.press("down", "enter")
        await pilot.pause()
        chk("picker dismissed after select", not isinstance(a.screen, m.PickerScreen))
        chk("model switched to the highlighted row", a.model_id == "c-model")

    print("\n=== escape cancels without changing anything ===")
    b = make_app()
    async with b.run_test() as pilot:
        b._handle_command("/model")
        await pilot.pause()
        chk("picker open", isinstance(b.screen, m.PickerScreen))
        await pilot.press("escape")
        await pilot.pause()
        chk("dismissed", not isinstance(b.screen, m.PickerScreen))
        chk("model unchanged", b.model_id == "b-model")

    print("\n=== CLICKING a row selects it ===")
    c = make_app()
    async with c.run_test() as pilot:
        c._handle_command("/model")
        await pilot.pause()
        ol = c.screen.query_one(m.OptionList)
        await pilot.click(ol, offset=(3, 0))   # first visible row
        await pilot.pause()
        chk("click dismissed the picker", not isinstance(c.screen, m.PickerScreen))
        chk("click selected the clicked row", c.model_id == "a-model")

    print("\n=== /help is a scrollable modal with a Close button ===")
    d = make_app()
    async with d.run_test() as pilot:
        d._handle_command("/help")
        await pilot.pause()
        chk("HelpScreen on top", isinstance(d.screen, HelpScreen))
        body = d.screen.query_one("#help-body", m.Static)
        text = str(body.content)   # Textual 8: Static exposes .content
        chk("help text carried through", "/compact" in text and "Esc" in text)
        btn = d.screen.query_one("#help-close", m.Button)
        await pilot.click(btn)
        await pilot.pause()
        chk("Close button dismisses", not isinstance(d.screen, HelpScreen))

    print("\n=== Esc-stop dialog still works and is clickable ===")
    e = make_app()
    async with e.run_test() as pilot:
        e._chat_running = lambda: True
        e.action_stop_turn()
        await pilot.pause()
        chk("ConfirmStop on top", isinstance(e.screen, m.ConfirmStop))
        chk("has two buttons", len(e.screen.query(m.Button)) == 2)
        await pilot.click(e.screen.query_one("#no", m.Button))
        await pilot.pause()
        chk("No dismissed it", not isinstance(e.screen, m.ConfirmStop))
        chk("No did NOT request a stop", e._stop_requested is False)

        e.action_stop_turn()
        await pilot.pause()
        await pilot.click(e.screen.query_one("#yes", m.Button))
        await pilot.pause()
        chk("Yes requested the stop", e._stop_requested is True)

    print("\n=== typed forms still work (no regression) ===")
    f = make_app()
    async with f.run_test() as pilot:
        f._handle_command("/model 3")
        await pilot.pause()
        chk("/model <n> still switches without a modal", f.model_id == "c-model")
        chk("no modal was pushed", not isinstance(f.screen, m.PickerScreen))
        f._handle_command("/model a-model")
        await pilot.pause()
        chk("/model <name> still switches", f.model_id == "a-model")

    src = open(Path(__file__).resolve().parent.parent / "src" / "app.py", encoding="utf-8").read()
    chk("connection banner listing untouched", src.count("Use /model <number> to switch") == 1)

asyncio.run(main())
print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
