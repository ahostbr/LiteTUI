"""Theme fixture export reads public theme definitions, never live settings."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import export_rust_preview_themes as export

from litetui.themes import ALL_THEMES, THEME_TOKENS, theme_from_tokens


def test_ash_resolves_the_actual_component_tokens():
    colors = export.resolved_colors(ALL_THEMES["ash"])
    assert colors["surface"] == (21, 21, 20)
    assert colors["tool_background"] == (0, 0, 0)
    assert colors["thinking_border"] == (186, 182, 172)
    assert colors["tool_text"] == (232, 163, 61)


def test_catalog_includes_both_families_but_no_light_or_live_custom_themes():
    names = set(export.public_catalog())
    assert {"ash", "matrix", "lite-suite", "dracula", "nord", "textual-dark"} <= names
    assert not names.intersection(export.themes.LIGHT_BUILTINS)
    assert all(theme.dark for theme in export.public_catalog().values())
    assert "my-test-theme-1" not in names and "probe" not in names


def test_optional_custom_overrides_survive_the_resolved_theme_boundary():
    tokens = {name: "#202020" for name in THEME_TOKENS}
    tokens.update({"thinking-text": "#a1b2c3", "thinking-box": "#123456", "tool-text": "#abcdef",
                   "footer-foreground": "#8899aa", "footer-background": "#101112"})
    colors = export.resolved_colors(theme_from_tokens("fixture-custom", tokens))
    assert colors["thinking_text"] == (161, 178, 195)
    assert colors["thinking_border"] == (18, 52, 86)
    assert colors["tool_text"] == (171, 205, 239)
    assert colors["footer_foreground"] == (136, 153, 170)
    assert colors["footer_background"] == (16, 17, 18)


def test_old_custom_themes_without_optional_overrides_keep_derived_defaults():
    tokens = {name: "#202020" for name in THEME_TOKENS}
    colors = export.resolved_colors(theme_from_tokens("old-custom", tokens))
    assert colors["thinking_border"] == (32, 32, 32)
    assert colors["tool_text"] == (232, 163, 61)


def test_export_is_deterministic():
    assert export.render_catalog() == export.render_catalog()
