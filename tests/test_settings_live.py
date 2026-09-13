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

import pytest

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _script_guard  # tests/ is sys.path[0] when a file is run as a script

# 🔴 THE HIGHEST-RISK FILE IN THE SCRIPT-STYLE SET, AND IT WAS THE LEAST GUARDED.
# "live" in this file's name means a real running APP, not the live settings
# FILE — but it drives `/settings` through that app, which is the exact route
# `conftest.py`'s docstring records as having reset Ryan's `tool_iterations`
# 100 -> 48 (`_on_settings_saved` -> `app.py:3452` -> `settings_mod.save(new)`
# with no root=). conftest's autouse fixture closed that for the pytest half and
# cannot reach a script-style file. See tests/_script_guard.py.
_script_guard.pin_first_boot_env()
_script_guard.redirect_live_settings()

# Deliberately a SECOND import block: the env pin and the settings redirect must
# run BETWEEN these imports, not before or after them.
from litetui import app as m
from litetui import model_residency
from litetui import paths
from litetui import settings as settings_mod

paths.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-settings-"))
ok = []
#: The labels that FAILED. `ok` is bools, which is all the script needed for an
#: exit status; a pytest arm has to be able to say WHICH check failed, and a
#: bool cannot. Recorded alongside rather than by changing `ok`, so `sum(ok)` /
#: `all(ok)` / `len(ok)` below keep meaning exactly what they meant (T699).
failures: list[str] = []


def chk(label, cond):
    ok.append(bool(cond))
    if not cond:
        failures.append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def make_app():
    # `/settings` asks only for a read-only residency snapshot. Keep this wiring
    # test deterministic in both pytest and standalone-script mode: there is no
    # conftest fixture in the latter, and a test must never dial Ryan's live
    # backend merely to mount a screen.
    model_residency.resident_models = lambda app: ({"b-model"}, False)
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
        from litetui.settings_screen import SettingsScreen

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


# ── the same checks, as a pytest arm (T699) ─────────────────────────────
#
# 🔴 SAME DEFECT AS `test_footer.py`: a module-level `asyncio.run(main())` whose
# `main` ended in `sys.exit`, so collection aborted the entire run. The tally
# and the exit moved out of `main` and behind `__main__`; `main` itself is
# unchanged and still does every check.
#
# ⬜ AND IT IS FASTER IN THE SUITE THAN IT IS AS A SCRIPT. The child process a
# subprocess runner would spawn pays the ~4s MCP refusal that conftest's
# `_never_dial_out_from_a_constructor` exists to avoid; in-process it does not.


@pytest.mark.asyncio
async def test_the_live_settings_wiring_holds() -> None:
    await main()
    assert ok, "no check ran"
    assert failures == [], failures


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
