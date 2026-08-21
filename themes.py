"""LiteTUI's own themes — the LiteSuite palette, ported.

THE SOURCE OF TRUTH IS LITESUITE. Every preset here is a port of
`LiteSuite/apps/web/src/litesuite/lib/themes.ts` (12 presets, including
Matrix and Lite Suite), mapped token-for-token onto Textual's Theme fields so
the desktop app and this TUI answer to one palette. Do not invent colors
here — edit the LiteSuite file and re-port, or the two selectors drift.

The mapping, chosen once and used for every preset:

    LiteSuite token          Textual Theme field
    accent               ->  primary        (the identity color)
    accentBright         ->  secondary
    void                 ->  background     (darkest surface)
    panel                ->  surface
    shelf                ->  panel          (Textual's "panel" is a level up)
    bone                 ->  foreground     (brightest text)
    ok / danger / warning->  success / error / warning
    info                 ->  accent

Textual derives the -darken/-lighten/-muted ladder from these, which is what
LiteTUI's CSS ($primary-darken-3, $surface-darken-1, ...) consumes.

Registered at app init; the active one is chosen from the command palette
(ctrl+p) or /settings, and PERSISTED via settings.theme_name — before this,
the app silently reset to textual-dark every boot because nothing stored the
choice.
"""

from __future__ import annotations

from textual.theme import Theme

#: name -> Theme. Insertion order is display order in the settings dropdown.
LITETUI_THEMES: dict[str, Theme] = {}


def _port(
    name: str,
    *,
    accent: str,
    accent_bright: str,
    void: str,
    panel: str,
    shelf: str,
    bone: str,
    ok: str,
    danger: str,
    warning: str,
    info: str,
) -> None:
    LITETUI_THEMES[name] = Theme(
        name=name,
        primary=accent,
        secondary=accent_bright,
        background=void,
        surface=panel,
        panel=shelf,
        foreground=bone,
        success=ok,
        error=danger,
        warning=warning,
        accent=info,
        dark=True,
    )


# ── the LiteSuite presets, extracted verbatim from themes.ts ─────────────────
_port("oscura-midnight", accent="#c9a24d", accent_bright="#dbb55e",
      void="#0a0a0b", panel="#131314", shelf="#1b1b1d", bone="#e8e4dc",
      ok="#4a8c5e", danger="#c0453a", warning="#f59e0b", info="#7ca8cf")

_port("dusk", accent="#e07850", accent_bright="#f09070",
      void="#0f0a0a", panel="#1a1214", shelf="#241a1d", bone="#f0e0d8",
      ok="#5a9960", danger="#d04540", warning="#fbbf24", info="#8090c0")

_port("lime", accent="#84cc16", accent_bright="#a3e635",
      void="#080a08", panel="#101410", shelf="#181e18", bone="#e0ead8",
      ok="#3ab060", danger="#f43f5e", warning="#facc15", info="#818cf8")

_port("ocean", accent="#38bdf8", accent_bright="#60a8f0",
      void="#0c1222", panel="#131c31", shelf="#1e293b", bone="#e2e8f0",
      ok="#2dd4bf", danger="#f87171", warning="#fcd34d", info="#818cf8")

_port("retro", accent="#fbbf24", accent_bright="#f0b860",
      void="#18120b", panel="#231a0f", shelf="#2d2215", bone="#fef3c7",
      ok="#84cc16", danger="#dc2626", warning="#e07850", info="#5eaaa8")

_port("neo", accent="#e879f9", accent_bright="#d880d8",
      void="#0d0d0d", panel="#151515", shelf="#1f1f1f", bone="#f0f0f0",
      ok="#4ade80", danger="#fb7185", warning="#fcd34d", info="#22d3ee")

_port("forest", accent="#22c55e", accent_bright="#4ade80",
      void="#060a06", panel="#0e1610", shelf="#162018", bone="#e6f0e6",
      ok="#34d399", danger="#ef4444", warning="#fbbf24", info="#38bdf8")

# The one Ryan asked for by name. Phosphor on black; even info is full
# green, because in the Matrix there is no other color.
_port("matrix", accent="#00FF41", accent_bright="#55ff55",
      void="#0D0208", panel="#0D0208", shelf="#003B00", bone="#00FF41",
      ok="#00FF41", danger="#FF0000", warning="#FFD700", info="#00FF41")

# The house theme: LiteSuite's gold-on-graphite identity.
_port("lite-suite", accent="#c9a24d", accent_bright="#dbb55e",
      void="#0a0a0b", panel="#131314", shelf="#1b1b1d", bone="#e8e4dc",
      ok="#4a8c5e", danger="#c0453a", warning="#f59e0b", info="#7ca8cf")

_port("abyss", accent="#d4a039", accent_bright="#e8b44d",
      void="#080a0e", panel="#0e1118", shelf="#141822", bone="#dcd8d0",
      ok="#8fd88f", danger="#dc4444", warning="#f59e0b", info="#6098cc")

_port("cockpit", accent="#c9a24d", accent_bright="#dbb55e",
      void="#08080b", panel="#13131a", shelf="#16161e", bone="#f2efea",
      ok="#8fc49a", danger="#d87373", warning="#d9b365", info="#6fc3d4")

# `ok` is DELIBERATELY not green (upstream comment, kept): the design's
# premise is that a healthy fleet is colourless — a green success token would
# put colour on the most common state and invert the whole rule.
_port("amber-ledger", accent="#e8b33f", accent_bright="#f2c356",
      void="#0d0b08", panel="#110e0a", shelf="#171208", bone="#efe6d6",
      ok="#8b8065", danger="#c5453b", warning="#d9713c", info="#a99c84")


# ── stripped from the picker (Ryan, 2026-08-21: "strip all the light mode
# ones out") — unregistered at app init and excluded from settings choices.
# textual-ansi rides along: terminal-relative colors, neither dark nor ours.
LIGHT_BUILTINS = (
    "textual-light", "catppuccin-latte", "solarized-light",
    "rose-pine-dawn", "atom-one-light", "textual-ansi",
)

# ── the SHADES family — "it should be 50 shades of gray lmfao" ───────────────
#
# LiteTUI-NATIVE, not ported: no upstream source, so the parity gate does not
# cover them — the gate that does is test_themes' saturation bound, which
# fails any shade whose core colors drift colorful. The design rule is
# amber-ledger's, generalized: HEALTHY IS COLORLESS. success is a gray,
# warning a pale sand-gray, and only error keeps enough desaturated brick to
# be findable — nothing neon, no orange, no green.

SHADE_THEMES: dict[str, Theme] = {}


def _shade(
    name: str,
    *,
    bg: str,
    surface: str,
    panel: str,
    fg: str,
    primary: str,
    secondary: str,
    error: str = "#9c5a52",     # desaturated brick — findable, never neon
    warning: str = "#a89a80",   # pale sand-gray
) -> None:
    SHADE_THEMES[name] = Theme(
        name=name,
        primary=primary,
        secondary=secondary,
        background=bg,
        surface=surface,
        panel=panel,
        foreground=fg,
        success=secondary,      # healthy is colorless
        warning=warning,
        error=error,
        accent=primary,
        dark=True,
    )


# Ten temperatures of gray, darkest ladder to lightest accent.
_shade("obsidian", bg="#050506", surface="#0b0b0d", panel="#111114",
       fg="#c8c8ce", primary="#8e8e99", secondary="#5f5f68")
_shade("graphite", bg="#0c0c0d", surface="#131315", panel="#1a1a1d",
       fg="#d6d6da", primary="#b0b0b8", secondary="#6e6e76")
_shade("onyx", bg="#0a0a09", surface="#121211", panel="#191917",
       fg="#dcdad4", primary="#a6a29a", secondary="#6b6862")
_shade("charcoal", bg="#101112", surface="#17181a", panel="#1f2023",
       fg="#cfd2d6", primary="#9aa0a8", secondary="#5e646c")
_shade("gunmetal", bg="#0b0d10", surface="#111419", panel="#171b21",
       fg="#c2c9d2", primary="#8b939b", secondary="#565e66")
_shade("slate", bg="#0e1013", surface="#151821", panel="#1c2028",
       fg="#c8ccd6", primary="#9da3ac", secondary="#5e646e")
_shade("smoke", bg="#111010", surface="#181716", panel="#201e1d",
       fg="#d4d0cc", primary="#a8a099", secondary="#68625c")
_shade("ash", bg="#0d0d0c", surface="#151514", panel="#1d1d1b",
       fg="#d0cec8", primary="#bab6ac", secondary="#74716a")
_shade("pewter", bg="#0f1011", surface="#161819", panel="#1e2022",
       fg="#d8dbdd", primary="#c0c6ca", secondary="#78807e")
_shade("iron", bg="#0a0b0c", surface="#101214", panel="#16181b",
       fg="#c4c6c9", primary="#82868c", secondary="#4e5257")

#: Everything LiteTUI registers: the LiteSuite ports plus the native shades.
ALL_THEMES: dict[str, Theme] = {**LITETUI_THEMES, **SHADE_THEMES}
