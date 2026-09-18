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
    a = m.LiteTUI()
    a.available_models = ["a-model", "b-model"]
    a.model_id = "b-model"
    a._connect = lambda: None          # never touch the network in a test
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None
    return a


async def main():
    print("=== /settings opens the real screen ===")
    # `/settings` asks only for a read-only residency snapshot. Patch the
    # command's lookup seam for this block, including standalone-script mode
    # where no pytest monkeypatch fixture exists, and restore it even if a UI
    # assertion fails so later tests keep the real function.
    from litetui.plugins import settings_ui

    original_resident_models = settings_ui.model_residency.resident_models
    settings_ui.model_residency.resident_models = lambda app: ({"b-model"}, False)
    try:
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
    finally:
        settings_ui.model_residency.resident_models = original_resident_models
    chk(
        "settings residency probe restored after the screen closes",
        settings_ui.model_residency.resident_models is original_resident_models,
    )

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


@pytest.mark.asyncio
async def test_saving_a_backend_setting_keeps_the_backend_on_the_new_object() -> None:
    """🔴 THE STALE-SETTINGS-REFERENCE DEFECT, PINNED.

    Ryan set `ninfer_max_context` in /settings, saved it, and the engine still
    spawned with the old value — "I can't set the context level." Root cause:
    `_collect` builds a NEW Settings object (`replace()`) and `_on_settings_saved`
    rebinds `app.settings` to it, but the backend had captured the OLD object in
    `__init__` and `ninfer_engine.start` reads `self._settings`. The save handler
    now re-points the backend; this proves it, through the real handler, so a
    regression that drops the re-point fails here and not in production.
    """
    from dataclasses import replace

    from litetui import llm_backend

    a = m.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None

    # The save handler renders through the live screen (_update_header), so it
    # must run inside the pilot, exactly like the other arms in this file.
    async with a.run_test():
        # Put a NInfer backend in place, holding the live settings object.
        base = a.settings
        base.backend = "ninfer"
        a.backend = llm_backend.make_backend(base)
        assert a.backend.name == "ninfer"
        assert a.backend._settings is a.settings, "precondition: backend holds the live object"
        assert a.backend._settings.ninfer_max_context == 32768, "precondition: default context"

        # User changes ninfer_max_context and saves — through the REAL handler.
        new = replace(base, ninfer_max_context=100000)
        assert new is not base, "precondition: the save path replaces the object"
        a._on_settings_saved(new)

        # The defect was: the backend stayed on `base` (32768). It must now
        # track the saved object so the engine spawn reads 100000.
        assert a.backend._settings is a.settings, "backend must follow the saved settings object"
        assert a.backend._settings.ninfer_max_context == 100000


@pytest.fixture
def _no_settings_write(monkeypatch):
    """`/backend`'s path PERSISTS the choice, which is right in production and
    wrong in a suite: it rewrites the checkout's real settings.json. Caught by
    conftest's `_guard_checkout_root_writes` on the first run of the arms
    below. The arms assert the LIVE object, never the file, so the write is
    stubbed rather than redirected."""
    from litetui import settings as settings_mod

    written: list = []
    monkeypatch.setattr(settings_mod, "save", lambda s, *a, **k: written.append(s))
    return written


@pytest.mark.asyncio
async def test_saving_a_changed_backend_actually_switches_the_engine(_no_settings_write) -> None:
    """🔴 THE SAME DEFECT, ONE LAYER OUT — and the one Ryan hit hardest.

    2026-09-18: *"i set the fucking backend to ninfer and loaded in vram and
    its still talking to fucking lmstudio"*. Saving wrote `backend` to
    settings.json and nothing else: `self.backend` is built by `make_backend`
    at boot and was rebuilt ONLY by the `/backend` command, and `backend` was
    not in the handler's `deferred` list either — so the control reported
    success, changed nothing, and did not even say a /reconnect was needed.

    The arm asserts the OBJECT changed, not just the string, because the
    string was always right. That is what made it invisible.
    """
    from dataclasses import replace

    from litetui import llm_backend

    a = m.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None

    async with a.run_test():
        base = a.settings
        base.backend = "lmstudio"
        a.backend = llm_backend.make_backend(base)
        assert a.backend.name == "lmstudio", "precondition: running on LM Studio"

        reconnected: list[str] = []
        a.connect = lambda: reconnected.append(a.backend.name)

        a._on_settings_saved(replace(base, backend="ninfer"))

        assert a.backend.name == "ninfer", (
            "the saved backend must reach the LIVE engine, not just settings.json")
        assert a.settings.backend == "ninfer"
        assert reconnected == ["ninfer"], (
            "a swapped backend must reconnect: `client` is built from "
            "backend.base_url(), so a new object on the old endpoint is the "
            "same bug one layer down")


@pytest.mark.asyncio
async def test_saving_with_the_backend_unchanged_does_not_reconnect(_no_settings_write) -> None:
    """An unrelated save must not tear down a working connection."""
    from dataclasses import replace

    from litetui import llm_backend

    a = m.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None

    async with a.run_test():
        base = a.settings
        base.backend = "lmstudio"
        a.backend = llm_backend.make_backend(base)

        reconnected: list[str] = []
        a.connect = lambda: reconnected.append(a.backend.name)

        a._on_settings_saved(replace(base, tools_enabled=not base.tools_enabled))

        assert reconnected == [], "only a CHANGED backend reconnects"
        assert a.backend.name == "lmstudio"


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
