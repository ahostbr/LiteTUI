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

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m
import themes as themes_mod


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._jobs = []
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
    from settings_screen import SettingsScreen
    import settings as settings_mod

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
        }
        for tok, value in wanted.items():
            screen.query_one(f"#ct-{tok}", Input).value = value

        out = screen._collect()
        saved = out.custom_themes["round-trip"]
        for tok, value in wanted.items():
            assert saved.get(tok) == value, (
                f"{tok} was typed into the creator and did not survive the save "
                f"(got {saved.get(tok)!r})"
            )
