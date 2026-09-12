"""Thinking text, thinking box and tool text are theme tokens.

Ryan, of the /settings theme creator: "thinking text and tool text and thinking
box color arent there".

They were not missing from the FORM -- the form loops THEME_TOKENS and always
did. They were never theme values at all: the tool card's colour was a hardcoded
hex in the render path, and the thinking frame was derived from $warning, so
neither could be edited without moving everything else warning-coloured.

Textual's Theme is a fixed dataclass, so the three ride in its `variables` dict
and reach the CSS as $thinking-text / $thinking-box / $tool-text.

Two hazards are guarded here, because both would be silent and both would be
worse than the missing feature:

  1. UNDEFINED VARIABLE = NO STYLESHEET. Textual does not skip a declaration it
     cannot resolve, it fails the sheet. The themes we do not author carry none
     of these variables -- and textual-dark is this app's hard fallback whenever
     a saved theme name will not resolve. Adding a themeable colour could
     therefore have bricked the app for anyone on a built-in.

  2. NEW REQUIRED TOKEN = EVERY SAVED THEME BREAKS. Custom themes persist as
     {token: hex} and are rebuilt from that dict at every startup. A token that
     raises when absent would reject themes the user saved before it existed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import Input, Label, TabbedContent

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import settings as settings_mod
from litetui import themes as themes_mod
from litetui.settings_screen import SettingsScreen


def _settings_body(app_or_screen):
    """The settings BODY — where `_collect`, `on_click` and the controls live.

    They moved out of `SettingsScreen` so a `SidePanel` can mount the same
    widget (T232). `query_one` still reaches descendants from the screen; it is
    the DIRECT method access that had to follow the logic.
    """
    from litetui.settings_screen import SettingsBody
    node = getattr(app_or_screen, "screen", app_or_screen)
    return node.query_one(SettingsBody)



def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = []
    return a


CORE_ONLY = {
    "primary": "#00ff41", "secondary": "#55ff55", "accent": "#00ff41",
    "success": "#00ff41", "warning": "#ffcc00", "error": "#ff5555",
    "background": "#000000", "surface": "#0a0a0a", "panel": "#111111",
    "foreground": "#e0e0e0",
}


def test_the_creator_offers_the_three_new_rows() -> None:
    """The form loops THEME_FORM_TOKENS, so this is what the user sees."""
    for tok in ("thinking-text", "thinking-box", "tool-text"):
        assert tok in themes_mod.THEME_FORM_TOKENS, f"{tok} has no row in the creator"
    # ...and the core ten are still all there.
    for tok in themes_mod.THEME_TOKENS:
        assert tok in themes_mod.THEME_FORM_TOKENS


def test_a_theme_saved_before_these_tokens_existed_still_builds() -> None:
    """Backwards compatibility, asserted on the exact shape that is persisted."""
    theme = themes_mod.theme_from_tokens("saved-earlier", CORE_ONLY)
    assert theme.name == "saved-earlier"
    for tok in themes_mod.THEME_EXTRA_TOKENS:
        assert theme.variables.get(tok), f"{tok} was not defaulted for an old theme"


def test_a_supplied_extra_wins_over_the_default() -> None:
    tokens = {**CORE_ONLY, "tool-text": "#123456"}
    theme = themes_mod.theme_from_tokens("custom", tokens)
    assert theme.variables["tool-text"] == "#123456"


def test_a_malformed_extra_falls_back_instead_of_rejecting_the_theme() -> None:
    """A bad extra must not cost the user their ten good colours."""
    tokens = {**CORE_ONLY, "thinking-box": "not-a-color"}
    theme = themes_mod.theme_from_tokens("custom", tokens)
    assert theme.variables["thinking-box"].startswith("#")


@pytest.mark.asyncio
async def test_a_builtin_theme_still_resolves_every_variable() -> None:
    """textual-dark carries none of these, and it is the hard fallback."""
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        a.theme = "textual-dark"
        await pilot.pause()
        variables = a.get_css_variables()
        for tok in themes_mod.THEME_EXTRA_TOKENS:
            assert variables.get(tok), (
                f"${tok} is undefined under textual-dark -- an undefined variable "
                "fails the whole stylesheet, not just one rule"
            )

        # And the widgets that consume them still render.
        block = m.ThinkingBlock()
        a.query_one("#chat-log").mount(block)
        await pilot.pause()
        assert block.size.height > 0


@pytest.mark.asyncio
async def test_tool_cards_take_their_colour_from_the_theme() -> None:
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        tool = m.ToolMessage("bash")
        a.query_one("#chat-log").mount(tool)
        await pilot.pause()
        colour = tool._tool_name_color()
        assert colour.startswith("#"), f"tool colour is not a hex: {colour!r}"

    # The pure function stays callable with no app and no theme -- that is why
    # it takes the colour as an argument rather than reaching for one.
    parts = m.tool_display_parts(tool)
    assert parts and m.TOOL_NAME_DEFAULT in parts[0][1]


@pytest.mark.asyncio
async def test_the_creator_SAVES_the_extra_rows() -> None:
    """Rendering a row is not the same as reading it back.

    The form loops THEME_FORM_TOKENS and the collector looped THEME_TOKENS, so
    the three extra fields were drawn, accepted typing and opened the colour
    picker -- and were dropped on save. The theme persisted without them and the
    colours silently stayed default, which is indistinguishable from "the theme
    doesn't work".

    This asserts the round trip, not the presence of a widget: a test that only
    queried #ct-thinking-text would have passed against the broken build.
    """
    from textual.app import App, ComposeResult
    from textual.widgets import Input

    from litetui import settings as settings_mod
    from litetui.settings_screen import SettingsScreen

    class Host(App):
        def compose(self) -> ComposeResult:
            return []

        def on_mount(self) -> None:
            self.push_screen(
                SettingsScreen(settings_mod.Settings(), models=["m1"], mcp_servers=[]),
                lambda r: None,
            )

    app = Host()
    async with app.run_test() as pilot:
        await pilot.pause()
        screen = app.screen

        # Every rendered row must exist...
        for tok in themes_mod.THEME_FORM_TOKENS:
            screen.query_one(f"#ct-{tok}", Input)

        screen.query_one("#ct-name", Input).value = "round-trip"
        for tok in themes_mod.THEME_TOKENS:
            screen.query_one(f"#ct-{tok}", Input).value = "#101010"
        wanted = {
            "thinking-text": "#00ff41",
            "thinking-box": "#123456",
            "tool-text": "#abcdef",
            "footer-foreground": "#33ccff",
            "footer-background": "#142536",
        }
        for tok, value in wanted.items():
            screen.query_one(f"#ct-{tok}", Input).value = value

        out = _settings_body(screen)._collect()
        saved = out.custom_themes["round-trip"]
        for tok, value in wanted.items():
            assert saved.get(tok) == value, (
                f"{tok} was typed into the creator and did not survive the save "
                f"(got {saved.get(tok)!r})"
            )


@pytest.mark.asyncio
async def test_footer_colours_round_trip_and_preserve_warnings(tmp_path) -> None:
    """Editor -> save -> CSS AND Rich, at the narrow width Ryan actually uses."""
    legacy = themes_mod.theme_from_tokens("legacy", CORE_ONLY)
    assert not set(themes_mod.THEME_FOOTER_TOKENS) & legacy.variables.keys()
    invalid = themes_mod.theme_from_tokens(
        "invalid", {**CORE_ONLY, "footer-foreground": "bad", "footer-background": "bad"}
    )
    assert not set(themes_mod.THEME_FOOTER_TOKENS) & invalid.variables.keys()

    a = make_app()
    a.seat = SimpleNamespace(registered=True, name="OpenBolt")
    a.ctx_used, a.ctx_max, a.ctx_loaded = 27000, 100000, True
    a.convo_id = "4f3a1c9d-2b7e-4a11-9c30-8e5f6d1b2a44"
    a.thinking_level = "high"

    def footer_colour(field):
        text = a.query_one(".ctx-label").content
        return text.get_style_at_offset(a.console, text.plain.index(field)).color.triplet.hex

    async with a.run_test(size=(76, 30)) as pilot:
        await pilot.pause()
        a.push_screen(SettingsScreen(a.settings, models=["a-model"], mcp_servers=[]),
                      a._on_settings_saved)
        await pilot.pause()
        body = _settings_body(a)
        body.query_one(TabbedContent).active = "tab-themes"
        await pilot.pause()
        assert body.query_one("#ct-footer-background", Input).value.startswith("#")
        labels = [label.content for label in body.query(Label)]
        assert "Context footer text" in labels and "Context footer background" in labels
        body.query_one("#ct-name", Input).value = "footer-test"
        body.query_one("#ct-footer-foreground", Input).value = "#33ccff"
        body.query_one("#ct-footer-background", Input).value = "#142536"
        body.action_save()
        await pilot.pause()

        assert a.theme == "footer-test"
        loaded = settings_mod.load(root=tmp_path)
        assert loaded.custom_themes["footer-test"]["footer-foreground"] == "#33ccff"
        assert loaded.custom_themes["footer-test"]["footer-background"] == "#142536"
        for selector in (".ctx-label", ".palette-button"):
            assert a.query_one(selector).styles.background.hex == "#142536"
        assert footer_colour("OpenBolt") == "#33ccff"
        assert footer_colour("27%") == "#33ccff"
        assert footer_colour("autonomous on") == "#7aa2f7"
        assert a.query_one(".ctx-label").content.cell_len <= 63

        # The setting is a neutral text colour, never a way to hide warnings.
        a.ctx_used = 92000
        a._plan_mode = True
        a._refresh_ctx_label()
        await pilot.pause()
        assert footer_colour("OpenBolt") == "#33ccff"
        assert footer_colour("92%") == "#e5534b"
        assert footer_colour("plan:on") == "#bb9af7"
        a._active_tool_profile = m.tool_policy.INTERACTIVE
        a.ctx_used = 75000
        a._refresh_ctx_label()
        await pilot.pause()
        assert footer_colour("interactive on") == "#7d8799"
        assert footer_colour("75%") == "#e8a33d"

        # Re-editing the ACTIVE name must invalidate CSS as well as Rich text.
        a.push_screen(SettingsScreen(a.settings, models=["a-model"], mcp_servers=[]),
                      a._on_settings_saved)
        await pilot.pause()
        body = _settings_body(a)
        assert body.query_one("#ct-footer-foreground", Input).value == "#33ccff"
        assert body.query_one("#ct-footer-background", Input).value == "#142536"
        body.query_one("#ct-name", Input).value = "footer-test"
        body.query_one("#ct-footer-foreground", Input).value = "#aacc55"
        body.query_one("#ct-footer-background", Input).value = "#263748"
        body.action_save()
        await pilot.pause()
        assert footer_colour("OpenBolt") == "#aacc55"
        assert a.query_one(".ctx-label").styles.background.hex == "#263748"

        # Switching back to an old preset restores its original spans/strip.
        a.theme = "ash"
        await pilot.pause()
        assert footer_colour("OpenBolt") == "#7d8799"
        assert a.query_one(".ctx-label").styles.background.hex != "#263748"
