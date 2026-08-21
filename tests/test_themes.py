"""The LiteSuite palette, ported — and the port CANNOT drift from its source.

themes.py declares LiteSuite's themes.ts the source of truth. A declaration
is a wish; the cross-check below is the gate. When the LiteSuite checkout is
present it re-extracts every preset's tokens and compares them to the port —
the first hand-edited hex in themes.py fails here with the exact field named.
"""
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.theme import BUILTIN_THEMES

import themes as themes_mod
from app import LiteTUI

LITESUITE_THEMES_TS = Path("C:/Projects/LiteSuite/apps/web/src/litesuite/lib/themes.ts")

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


@pytest.mark.skipif(not LITESUITE_THEMES_TS.exists(),
                    reason="LiteSuite checkout not present on this machine")
def test_the_port_matches_the_source_token_for_token():
    """THE DRIFT GATE. Re-extracts themes.ts and compares every mapped field.
    themes.py says 'do not invent colors here' — this is what makes that
    sentence enforceable rather than aspirational."""
    src = LITESUITE_THEMES_TS.read_text(encoding="utf-8")
    body = src.split("export const THEMES", 1)[1]
    presets = {}
    for m in re.finditer(r'id:\s*"([^"]+)"', body):
        pid = m.group(1)
        chunk = body[m.start():]
        nxt = re.search(r'\n\s*id:\s*"', chunk[10:])
        if nxt:
            chunk = chunk[:nxt.start() + 10]
        presets[pid] = dict(re.findall(r'(\w+):\s*"([^"]+)"', chunk))

    mapping = {  # LiteSuite token -> Theme attribute
        "accent": "primary", "accentBright": "secondary", "void": "background",
        "panel": "surface", "shelf": "panel", "bone": "foreground",
        "ok": "success", "danger": "error", "warning": "warning", "info": "accent",
    }
    mismatches = []
    for pid, theme in themes_mod.LITETUI_THEMES.items():
        assert pid in presets, f"{pid} ported but absent upstream"
        for ls_tok, attr in mapping.items():
            want = presets[pid].get(ls_tok)
            got = getattr(theme, attr)
            if want is not None and got != want:
                mismatches.append(f"{pid}.{ls_tok}: port={got} source={want}")
    assert not mismatches, "PORT DRIFTED FROM themes.ts:\n  " + "\n  ".join(mismatches)


# --- persistence -------------------------------------------------------------
def test_watch_theme_persists_a_change(monkeypatch):
    import settings as settings_mod
    saved = []
    monkeypatch.setattr(settings_mod, "save", lambda st: saved.append(st.theme_name))
    ns = SimpleNamespace(settings=SimpleNamespace(theme_name="textual-dark"))
    LiteTUI.watch_theme(ns, "matrix")
    assert saved == ["matrix"]
    assert ns.settings.theme_name == "matrix"


def test_watch_theme_skips_a_no_op_write(monkeypatch):
    """Boot applies the saved theme, which fires the watcher with the name
    already in settings — writing there would rewrite the file every boot."""
    import settings as settings_mod
    saved = []
    monkeypatch.setattr(settings_mod, "save", lambda st: saved.append(1))
    ns = SimpleNamespace(settings=SimpleNamespace(theme_name="matrix"))
    LiteTUI.watch_theme(ns, "matrix")
    assert saved == []


def test_watch_theme_before_settings_exist_is_inert():
    ns = SimpleNamespace(settings=None)
    LiteTUI.watch_theme(ns, "nord")  # must not raise
