"""The ported palette, and the rules it still answers to — upstream is NOT one.

⚠️ THIS FILE USED TO HOLD A CROSS-REPO DRIFT GATE and no longer does.
`test_the_port_matches_the_source_token_for_token` re-extracted LiteSuite's
themes.ts and failed on the first mismatched hex. It was DELETED 2026-08-28 by
Ryan's ruling *let them diverge* — the palettes are independent now, LiteSuite
`cb071ea4` moved matrix upstream, and this port deliberately does not follow.
See themes.py's own header for the contract that replaced it.

⇒ WHAT REMAINS HERE IS EVERY RULE THAT IS OURS ALONE: the presets exist, they
are dark, none shadows a Textual builtin, the shade ladder is gray and muted,
and `amber-ledger`'s success is NOT green. Those never depended on upstream,
which is why the ruling did not touch them. **A test removed here would now
lose a rule outright — there is no second gate behind these.**
"""
from types import SimpleNamespace

from textual.theme import BUILTIN_THEMES

from litetui import themes as themes_mod
from litetui.app import LiteTUI

EXPECTED = [
    "oscura-midnight", "dusk", "lime", "ocean", "retro", "neo", "forest",
    "matrix", "lite-suite", "abyss", "cockpit", "amber-ledger",
]


def test_all_twelve_presets_are_ported():
    assert list(themes_mod.LITETUI_THEMES) == EXPECTED


def test_the_two_ryan_asked_for_by_name():
    m = themes_mod.LITETUI_THEMES["matrix"]
    assert m.primary == "#00FF41" and m.background == "#0D0208"
    ls = themes_mod.LITETUI_THEMES["lite-suite"]
    assert ls.primary == "#c9a24d" and ls.background == "#0a0a0b"


def test_every_port_is_dark():
    """Ryan's ask was dark-based themes; a light one here is a port error."""
    assert all(t.dark for t in themes_mod.LITETUI_THEMES.values())


def test_no_port_shadows_a_builtin_name():
    """register_theme on a builtin name would silently replace it."""
    assert not set(themes_mod.LITETUI_THEMES) & set(BUILTIN_THEMES)


def test_amber_ledger_ok_is_deliberately_not_green():
    """Upstream design rule: a healthy fleet is colourless. The obvious
    'fix' — making success green — inverts the theme's whole premise, and
    upstream carries a comment begging people not to. This is that comment,
    with teeth."""
    assert themes_mod.LITETUI_THEMES["amber-ledger"].success == "#8b8065"


# --- persistence -------------------------------------------------------------
def test_watch_theme_persists_a_change(monkeypatch):
    from litetui import settings as settings_mod
    saved = []
    monkeypatch.setattr(settings_mod, "save", lambda st: saved.append(st.theme_name))
    ns = SimpleNamespace(settings=SimpleNamespace(theme_name="textual-dark"))
    LiteTUI.watch_theme(ns, "matrix")
    assert saved == ["matrix"]
    assert ns.settings.theme_name == "matrix"


def test_watch_theme_skips_a_no_op_write(monkeypatch):
    """Boot applies the saved theme, which fires the watcher with the name
    already in settings — writing there would rewrite the file every boot."""
    from litetui import settings as settings_mod
    saved = []
    monkeypatch.setattr(settings_mod, "save", lambda st: saved.append(1))
    ns = SimpleNamespace(settings=SimpleNamespace(theme_name="matrix"))
    LiteTUI.watch_theme(ns, "matrix")
    assert saved == []


def test_watch_theme_before_settings_exist_is_inert():
    ns = SimpleNamespace(settings=None)
    LiteTUI.watch_theme(ns, "nord")  # must not raise


# --- the SHADES family: "50 shades of gray", enforced not promised ----------
def _spread(hexcolor):
    """max(r,g,b) - min(r,g,b): 0 = pure gray, 255 = fully saturated."""
    h = hexcolor.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return max(r, g, b) - min(r, g, b)


def test_ten_shades_exist_and_are_dark():
    assert len(themes_mod.SHADE_THEMES) == 10
    assert all(t.dark for t in themes_mod.SHADE_THEMES.values())


def test_every_shade_is_actually_gray():
    """THE MEASURABLE VERSION OF THE BRIEF. Core colors (surfaces, text,
    primary, secondary, success) must be near-monochrome — channel spread
    under 16 of 255. This morning a hand-typed port fabricated saturated
    colors nobody noticed by eye; this bound is what stops that happening
    to the family whose entire identity is having none."""
    for name, t in themes_mod.SHADE_THEMES.items():
        for field in ("background", "surface", "panel", "foreground",
                      "primary", "secondary", "success"):
            spread = _spread(getattr(t, field))
            assert spread <= 16, f"{name}.{field} spread={spread} — not gray"


def test_shade_semantics_are_muted_not_neon():
    """Ryan's complaint was orange and green everywhere. warning stays a
    sand-gray (spread <= 48); error keeps just enough brick to be findable
    (spread <= 96) — desaturated, never neon. A neon #FF0000 spreads 255."""
    for name, t in themes_mod.SHADE_THEMES.items():
        assert _spread(t.warning) <= 48, f"{name}.warning too colorful"
        assert 16 <= _spread(t.error) <= 96, f"{name}.error: invisible or neon"


def test_healthy_is_colorless_in_every_shade():
    """The amber-ledger rule, generalized to the whole family: success is a
    GRAY, so the most common state carries no color."""
    for name, t in themes_mod.SHADE_THEMES.items():
        assert _spread(t.success) <= 16, f"{name}.success has color"


# --- the strip ---------------------------------------------------------------
def test_light_builtins_are_stripped_from_choices():
    from litetui.settings_screen import _theme_choices
    names = [n for n, _ in _theme_choices()]
    for light in themes_mod.LIGHT_BUILTINS:
        assert light not in names, f"{light} still offered"
    # negative arm: the strip must not have taken dark builtins with it
    assert "textual-dark" in names and "nord" in names


def test_all_themes_is_ports_plus_shades_no_overlap():
    assert not set(themes_mod.LITETUI_THEMES) & set(themes_mod.SHADE_THEMES)
    assert themes_mod.ALL_THEMES == {**themes_mod.LITETUI_THEMES,
                                     **themes_mod.SHADE_THEMES}


# --- the custom theme creator ------------------------------------------------
def test_theme_from_tokens_builds_a_dark_theme():
    toks = {k: "#123456" for k in themes_mod.THEME_TOKENS}
    t = themes_mod.theme_from_tokens("mine", toks)
    assert t.dark and t.name == "mine" and t.primary == "#123456"


def test_theme_from_tokens_names_the_bad_field():
    """The error string is the UI: the settings screen shows it verbatim."""
    toks = {k: "#123456" for k in themes_mod.THEME_TOKENS}
    toks["warning"] = "orange"
    try:
        themes_mod.theme_from_tokens("mine", toks)
        assert False, "accepted a word as a color"
    except ValueError as e:
        assert "warning" in str(e) and "orange" in str(e)


def test_theme_from_tokens_rejects_short_hex_and_empty_name():
    toks = {k: "#123456" for k in themes_mod.THEME_TOKENS}
    toks["panel"] = "#123"
    try:
        themes_mod.theme_from_tokens("mine", toks)
        assert False
    except ValueError as e:
        assert "panel" in str(e)
    try:
        themes_mod.theme_from_tokens("   ", {k: "#123456" for k in themes_mod.THEME_TOKENS})
        assert False
    except ValueError as e:
        assert "name" in str(e)


def test_choices_include_customs_without_duplicating_registry_names():
    from litetui.settings_screen import _theme_choices
    names = [n for n, _ in _theme_choices({"my-noir": {}, "matrix": {}})]
    assert "my-noir" in names
    assert names.count("matrix") == 1   # a custom shadowing a port lists once


def test_register_custom_themes_skips_corrupt_entries_loudly():
    """A typo in hand-edited settings.json costs ONE theme, not the boot —
    and it is announced, never silent."""
    reg, notes = [], []
    ns = SimpleNamespace(
        settings=SimpleNamespace(custom_themes={
            "good": {k: "#101010" for k in themes_mod.THEME_TOKENS},
            "bad": {"primary": "not-a-color"},
        }),
        register_theme=lambda t: reg.append(t.name),
        notify=lambda msg, severity=None: notes.append(msg),
    )
    LiteTUI._register_custom_themes(ns)
    assert reg == ["good"]
    assert notes and "bad" in notes[0]
