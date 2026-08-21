"""Drive /settings and /clear-screen through a REAL running app.

The unit tests cover the store and the screen in isolation. Neither proves the
app WIRES them: a command that never reaches the dispatcher, or a screen that
throws on mount inside the real DOM, would leave every one of those tests green.

Same reasoning as test_modals: compose() cannot run outside a live App, so
stubbing it proves nothing.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import paths
import settings as settings_mod

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-settings-"))
ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model", "b-model"]
    a.model_id = "b-model"
    a._connect = lambda: None          # never touch the network in a test
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    return a


async def main():
    print("=== /settings opens the real screen ===")
    a = make_app()
    async with a.run_test() as pilot:
        a._handle_command("/settings")
        await pilot.pause()
        from settings_screen import SettingsScreen

        chk("a SettingsScreen is on top", isinstance(a.screen, SettingsScreen))
        # The knobs behind the two caps that started this must be reachable.
        for fid in ("f-tool_iterations", "f-compact_max_tokens", "f-autocompact_at_percent"):
            try:
                a.screen.query_one(f"#{fid}")
                found = True
            except Exception:
                found = False
            chk(f"{fid} is present and mounted", found)
        a.screen.action_cancel()
        await pilot.pause()
        chk("cancel closes it", not isinstance(a.screen, SettingsScreen))

    print("\n=== /clear-screen clears the DISPLAY, not the conversation ===")
    a = make_app()
    async with a.run_test() as pilot:
        # _append records to the conversation and mounts NOTHING. Render real
        # widgets, or the fixture proves nothing about the display.
        a._append({"role": "user", "content": "keep me"})
        a._user_bubble("keep me", False)
        a._append({"role": "assistant", "content": "and me"})
        a._system("a rendered line")
        await pilot.pause()

        before_msgs = len(a.conversation)
        before = list(a.query_one("#chat-log").children)
        chk("several widgets were rendered before clearing", len(before) >= 2)
        chk("conversation has messages", before_msgs >= 2)

        a._handle_command("/clear-screen")
        await pilot.pause()

        # IDENTITY, not count. The clear posts a one-widget note, so a count can
        # land on the same number it started from and report a working feature
        # as broken — which is exactly what the first version of this did.
        after = list(a.query_one("#chat-log").children)
        survivors = [w for w in before if w in after]
        chk("🔴 every previously rendered widget is gone", survivors == [])
        chk("a note replaced them (cleared, not blanked)", len(after) >= 1)
        chk("🔴 the CONVERSATION is untouched — this is the whole point",
            len(a.conversation) == before_msgs)

    print("\n=== /clear (the OTHER one) really does reset the conversation ===")
    a = make_app()
    async with a.run_test() as pilot:
        a._append({"role": "user", "content": "gone"})
        await pilot.pause()
        had = len(a.conversation)
        a._handle_command("/clear")
        await pilot.pause()
        # The discriminating pair: if /clear-screen and /clear did the same
        # thing, the test above would pass for the wrong reason.
        chk("/clear DOES drop the conversation (so the two differ)",
            len(a.conversation) != had or had == 0)

    print("\n=== the app boots with settings applied, not defaults-in-code ===")
    a = make_app()
    async with a.run_test() as pilot:
        await pilot.pause()
        chk("settings object exists on the app", isinstance(a.settings, settings_mod.Settings))
        chk("thinking_level came from settings, not a hardcoded None",
            a.thinking_level == a.settings.thinking_level)
        chk("tools_enabled came from settings",
            a.tools_enabled == a.settings.tools_enabled)

    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)


asyncio.run(main())
