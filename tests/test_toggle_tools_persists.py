"""Ctrl+T persists, so the three surfaces onto the tools switch cannot disagree.

Ryan's ruling, 2026-08-24. He was shown the disagreement and offered keeping
Ctrl+T as a deliberate temporary override; he chose "make all three persist".

THE BUG. `action_toggle_tools` wrote only the RUNTIME flag `app.tools_enabled`,
never `settings.tools_enabled`. Boot reads the persisted one. So:

  * open Settings after Ctrl+T -> the switch shows the SAVED value, not the
    live one: a control lying about the state it governs;
  * save Settings after Ctrl+T -> your toggle is silently REVERTED, by a screen
    you opened to change something else.

Nothing documented the split. This is the same class as the profile-list drift
and the two refusal tables: TWO COPIES THAT MUST AGREE, WITH NOTHING ENFORCING
IT — except here the two copies are one field and one attribute, which is why
it hid so long.
"""
from __future__ import annotations

import pytest

from litetui import app as app_mod
from litetui.app import LiteTUI


def _app(monkeypatch):
    app = LiteTUI()
    app._connect = lambda: None
    saved = []
    monkeypatch.setattr(
        "litetui.app.settings_mod.save", lambda s, root=None: saved.append(s.tools_enabled)
    )
    return app, saved


@pytest.mark.asyncio
async def test_the_toggle_writes_BOTH_the_runtime_flag_and_the_setting(monkeypatch):
    app, saved = _app(monkeypatch)
    async with app.run_test(size=(100, 35)):
        app.tools_enabled = True
        app.settings.tools_enabled = True

        app.action_toggle_tools()
        assert app.tools_enabled is False, "the runtime flag did not move"
        assert app.settings.tools_enabled is False, "the SETTING did not move"

        app.action_toggle_tools()
        assert app.tools_enabled is True
        assert app.settings.tools_enabled is True

    assert saved == [False, True], f"settings were not persisted per toggle: {saved}"


@pytest.mark.asyncio
async def test_the_two_never_disagree_after_a_toggle(monkeypatch):
    """The property, stated directly rather than via the two writes.

    This is what a reader actually cares about, and it stays true even if the
    implementation later routes through some other setter.
    """
    app, _ = _app(monkeypatch)
    async with app.run_test(size=(100, 35)):
        for _ in range(4):
            app.action_toggle_tools()
            assert app.tools_enabled == app.settings.tools_enabled, (
                "the runtime flag and the persisted setting disagree — this is "
                "exactly the state where the settings screen starts lying"
            )


@pytest.mark.asyncio
async def test_a_failed_save_still_toggles_and_says_so(monkeypatch):
    """Honest degradation. A toggle that lasts one session beats a crash on
    Ctrl+T, but a SILENT half-success is the disagreement this change removes."""
    app = LiteTUI()
    app._connect = lambda: None
    said = []
    def _boom(s, root=None):
        raise OSError("read-only settings.json")
    monkeypatch.setattr("litetui.app.settings_mod.save", _boom)
    async with app.run_test(size=(100, 35)):
        app._system = lambda msg, *a, **k: said.append(str(msg))
        before = app.tools_enabled
        app.action_toggle_tools()

    assert app.tools_enabled is (not before), "the toggle itself must still work"
    assert any("could not save" in s for s in said), f"failure was swallowed: {said}"


@pytest.mark.asyncio
async def test_the_toggle_still_rebuilds_the_system_prompt(monkeypatch):
    """CONTROL. Persistence was ADDED to this verb; it must not have displaced
    what the verb already did. The prompt swap is the load-bearing half — the
    tools section becomes TOOLS_DISABLED_PROMPT — and a test that only checked
    the new behaviour would not notice it going missing."""
    app, _ = _app(monkeypatch)
    async with app.run_test(size=(100, 35)):
        app.conversation = [{"role": "system", "content": "seed"}]
        app.tools_enabled = True
        app.action_toggle_tools()
        rebuilt = app.conversation[0]["content"]

    assert rebuilt != "seed", "the system prompt was not rebuilt"
    assert "TOOLS ARE ADVERTISED BUT DISABLED" in rebuilt, (
        "the disabled prompt section did not replace tools.md"
    )
