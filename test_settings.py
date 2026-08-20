"""Settings store + /settings screen.

WHAT THESE ARE FOR
The two caps that prompted this work were invisible because they were literals
inside a request. The risk in replacing them with a settings object is a NEW
invisible failure: a screen that renders but whose controls do not read back,
or a saved file that silently loses a field. So these tests assert the
round-trip and the read-back, not "does it construct".
"""

from __future__ import annotations

import json
import os
from dataclasses import fields

import pytest

import settings as settings_mod
from settings import Settings, sampling_kwargs

# Booting LiteTUI creates a real .convos/<uuid>/ before anything is typed, so a
# test that instantiates the app pollutes the user's conversation list. Redirect
# it the way test_modals and test_store already do.
import tempfile
from pathlib import Path as _Path

import app as _app_mod

_app_mod.CONVO_DIR = _Path(tempfile.mkdtemp(prefix="convos-settings-unit-"))


# ── Store ────────────────────────────────────────────────────────────────────


def test_defaults_are_sane():
    s = Settings()
    assert s.autocompact_at_percent == 80
    assert s.autocompact_enabled is True
    assert s.clear_screen_after_compact is True
    assert s.tool_iterations == 48
    # The bug this whole change exists to fix: 2048 could not fit a summary
    # once reasoning was counted against it.
    assert s.compact_max_tokens > 2048


def test_roundtrip_preserves_every_field(tmp_path):
    s = Settings()
    s.temperature = 0.42
    s.seed = 7
    s.stop = ["<|end|>", "STOP"]
    s.default_model = "qwen/qwen3.8-27b"
    s.default_context_length = 131072
    s.mcp_disabled_servers = ["chrome"]
    s.autocompact_at_percent = 65
    settings_mod.save(s, root=tmp_path)

    back = settings_mod.load(root=tmp_path)
    for f in fields(Settings):
        assert getattr(back, f.name) == getattr(s, f.name), f.name


def test_unset_is_none_not_zero(tmp_path):
    """None must survive the round trip as None.

    temperature=0.0 and temperature=unset are different requests. If absence
    were stored as 0 the server's own default would be silently overridden.
    """
    s = Settings()
    assert s.temperature is None
    settings_mod.save(s, root=tmp_path)
    back = settings_mod.load(root=tmp_path)
    assert back.temperature is None
    assert back.seed is None
    assert sampling_kwargs(back) == {}


def test_corrupt_file_does_not_stop_startup(tmp_path):
    (tmp_path / settings_mod.SETTINGS_FILENAME).write_text("{not json", encoding="utf-8")
    s = settings_mod.load(root=tmp_path)
    assert s.tool_iterations == 48  # fell back to defaults rather than raising


def test_unknown_key_in_file_is_ignored(tmp_path):
    (tmp_path / settings_mod.SETTINGS_FILENAME).write_text(
        json.dumps({"tool_iterations": 99, "from_a_future_version": True}),
        encoding="utf-8",
    )
    s = settings_mod.load(root=tmp_path)
    assert s.tool_iterations == 99
    assert not hasattr(s, "from_a_future_version")


def test_bad_value_falls_back_without_killing_the_rest(tmp_path):
    (tmp_path / settings_mod.SETTINGS_FILENAME).write_text(
        json.dumps({"tool_iterations": "abc", "autocompact_at_percent": 55}),
        encoding="utf-8",
    )
    s = settings_mod.load(root=tmp_path)
    assert s.tool_iterations == 48       # reverted
    assert s.autocompact_at_percent == 55  # the good one still applied


# ── Env precedence ───────────────────────────────────────────────────────────


def test_env_beats_file(tmp_path, monkeypatch):
    """The pre-existing LM_TOOL_ITERS knob must keep winning."""
    settings_mod.save(Settings(tool_iterations=10), root=tmp_path)
    monkeypatch.setenv("LM_TOOL_ITERS", "77")
    s = settings_mod.load(root=tmp_path)
    assert s.tool_iterations == 77
    assert settings_mod.source_of("tool_iterations") == "LM_TOOL_ITERS"


def test_no_env_means_no_lock(tmp_path, monkeypatch):
    monkeypatch.delenv("LM_TOOL_ITERS", raising=False)
    assert settings_mod.source_of("tool_iterations") is None


def test_empty_env_var_does_not_override(tmp_path, monkeypatch):
    """An exported-but-empty var must not blank a real setting."""
    settings_mod.save(Settings(lm_host="http://box:9999"), root=tmp_path)
    monkeypatch.setenv("LITETUI_LM_HOST", "")
    s = settings_mod.load(root=tmp_path)
    assert s.lm_host == "http://box:9999"


# ── Screen ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_screen_mounts_and_reads_back_edits():
    """Mount for real and confirm a typed value reaches the returned Settings.

    A screen that renders but whose controls do not read back is exactly the
    failure this replaces — a control that looks like it works and does not.
    """
    from textual.app import App, ComposeResult
    from textual.widgets import Input, Switch
    from settings_screen import SettingsScreen

    captured: dict = {}

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(
                SettingsScreen(Settings(), models=["m1", "m2"], mcp_servers=["srv"]),
                lambda r: captured.setdefault("result", r),
            )

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        assert isinstance(screen, SettingsScreen)

        # Every non-env field must have a control; a missing one is a knob that
        # silently cannot be changed.
        for name in ("tool_iterations", "compact_max_tokens",
                     "autocompact_at_percent", "max_tokens_tools"):
            screen.query_one(f"#f-{name}", Input)
        for name in ("autocompact_enabled", "clear_screen_after_compact",
                     "tools_enabled", "skills_enabled", "mcp_enabled"):
            screen.query_one(f"#f-{name}", Switch)
        screen.query_one("#mcp-srv", Switch)

        screen.query_one("#f-tool_iterations", Input).value = "120"
        screen.query_one("#f-autocompact_at_percent", Input).value = "70"
        screen.query_one("#f-temperature", Input).value = "0.85"
        screen.query_one("#f-clear_screen_after_compact", Switch).value = False
        screen.query_one("#mcp-srv", Switch).value = False

        screen.action_save()
        await pilot.pause()

    result = captured.get("result")
    assert isinstance(result, Settings)
    assert result.tool_iterations == 120
    assert result.autocompact_at_percent == 70
    assert result.temperature == 0.85
    assert result.clear_screen_after_compact is False
    assert result.mcp_disabled_servers == ["srv"]


@pytest.mark.asyncio
async def test_screen_refuses_invalid_and_does_not_dismiss():
    from textual.app import App, ComposeResult
    from textual.widgets import Input
    from settings_screen import SettingsScreen

    captured: dict = {}

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(
                SettingsScreen(Settings()), lambda r: captured.setdefault("result", r)
            )

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        screen.query_one("#f-tool_iterations", Input).value = "not-a-number"
        screen.action_save()
        await pilot.pause()
        # Still open — a bad value must not be silently swallowed and dismissed.
        assert isinstance(app.screen, SettingsScreen)
        assert "result" not in captured

        # And an out-of-range percent is named, not clamped behind your back.
        screen.query_one("#f-tool_iterations", Input).value = "48"
        screen.query_one("#f-autocompact_at_percent", Input).value = "150"
        screen.action_save()
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        assert "result" not in captured


@pytest.mark.asyncio
async def test_cancel_returns_none():
    from textual.app import App, ComposeResult
    from settings_screen import SettingsScreen

    captured: dict = {}

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(
                SettingsScreen(Settings()), lambda r: captured.setdefault("result", r)
            )

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.screen.action_cancel()
        await pilot.pause()
    assert captured.get("result", "missing") is None


@pytest.mark.asyncio
async def test_env_locked_field_is_disabled(monkeypatch):
    """A field the env owns must render disabled, not editable-and-ignored."""
    from textual.app import App, ComposeResult
    from textual.widgets import Input
    from settings_screen import SettingsScreen

    monkeypatch.setenv("LM_TOOL_ITERS", "33")

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(settings_mod.load()))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        field = app.screen.query_one("#f-tool_iterations", Input)
        assert field.disabled is True

@pytest.mark.asyncio
async def test_every_field_is_reachable_without_opening_its_tab():
    """A field in a tab you never opened must still be collected.

    This is the assumption tabs rest on: TabbedContent mounts ALL panes and only
    HIDES the inactive ones. If it ever mounts lazily, _collect() would find no
    widget for those fields — and before the tab split it would have SILENTLY
    SKIPPED them, saving a partial settings object with no error.
    """
    from dataclasses import fields as dc_fields
    from textual.app import App, ComposeResult
    from settings_screen import SettingsScreen

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(Settings(), models=["m1"], mcp_servers=["srv"]))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        # Only the FIRST tab is active. Every other field lives in a hidden pane.
        unreachable = []
        for f in dc_fields(Settings):
            if f.name == "mcp_disabled_servers":
                continue  # rendered as per-server switches, not one control
            if settings_mod.source_of(f.name):
                continue  # env-locked fields are intentionally absent
            try:
                screen.query_one(f"#f-{f.name}")
            except Exception:
                unreachable.append(f.name)
        assert unreachable == [], (
            "these fields are not queryable while their tab is inactive: "
            + ", ".join(unreachable)
        )


@pytest.mark.asyncio
async def test_collect_refuses_a_partial_save_instead_of_skipping():
    """A missing control must raise, never be silently dropped.

    _collect() used to `continue` past a field whose widget it could not find.
    That turns a broken screen into a save that reports success and loses
    settings — indistinguishable, to the user, from the app ignoring them.
    """
    from textual.app import App, ComposeResult
    from settings_screen import SettingsScreen

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(Settings()))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen
        # Remove one control to simulate a pane that never mounted.
        screen.query_one("#f-tool_iterations").remove()
        await pilot.pause()
        with pytest.raises(ValueError, match="no control found for"):
            screen._collect()


@pytest.mark.asyncio
async def test_the_six_sections_are_tabs():
    from textual.app import App, ComposeResult
    from textual.widgets import TabPane
    from settings_screen import SettingsScreen

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(SettingsScreen(Settings()))

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        panes = app.screen.query(TabPane)
        assert len(panes) == 6, f"expected 6 section tabs, found {len(panes)}"

@pytest.mark.asyncio
async def test_the_panel_is_centred_not_docked_top_left():
    """Geometry, not stylesheet text.

    The panel shipped docked to the top-left while the app's CSS already
    contained `align: center middle` — SettingsScreen was simply absent from
    that rule's selector list. Asserting the CSS source would have passed
    throughout the bug.
    """
    import app as app_mod
    from settings_screen import SettingsScreen

    a = app_mod.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._apply_context_length = lambda: None

    async with a.run_test(size=(140, 50)) as pilot:
        a._handle_command("/settings")
        await pilot.pause()
        screen = a.screen
        assert isinstance(screen, SettingsScreen)
        box = screen.query_one("#set-box")
        await pilot.pause()

        sw, sh = screen.size.width, screen.size.height
        r = box.region

        left = r.x
        right = sw - (r.x + r.width)
        top = r.y
        bottom = sh - (r.y + r.height)

        # Centred means the slack is shared. Allow 1 cell for odd remainders.
        assert abs(left - right) <= 1, (
            f"not horizontally centred: {left} left vs {right} right "
            f"(box x={r.x} w={r.width}, screen w={sw})"
        )
        assert abs(top - bottom) <= 1, (
            f"not vertically centred: {top} top vs {bottom} bottom "
            f"(box y={r.y} h={r.height}, screen h={sh})"
        )
        # And it must not be flush against an edge, which is what "docked" was.
        assert left > 0 and top > 0, f"box is flush to an edge: x={r.x} y={r.y}"
