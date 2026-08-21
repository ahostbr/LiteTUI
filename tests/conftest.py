"""Make the repo root importable from tests/.

The tests moved out of the repo root into tests/, and `import app` resolved only
by accident: `python -m pytest` from the root puts the CWD on sys.path. Run
pytest from anywhere else, or as a bare `pytest`, and every module here fails to
import.

This makes it explicit rather than dependent on how you happened to invoke it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"          # the modules moved; data stays at ROOT
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# 🔴 THE SUITE MUST NOT TOUCH THE LIVE FLEET REGISTRY.
#
# Constructing LiteTUI registers a harness seat, and registration passes
# --takeover, which evicts the running instance's row to
# ~/.liteharness/.ghost_evicted_*. Running the tests therefore stole the
# name "LiteTUI" from Ryan's live app and left the roster naming a test
# process that had already exited.
#
# Set BEFORE any test imports app/harness, and never overwritten if the
# caller already chose a value.
os.environ.setdefault("LITETUI_NO_HARNESS", "1")

# 🔴 THE SUITE MUST NOT WRITE THE LIVE settings.json EITHER.
#
# Measured 2026-08-20, with a backup and a restore: ONE test run reset Ryan's
# tool_iterations from 100 back to 48.
#
#   tests/test_no_implicit_model_load.py:124,144   a._on_settings_saved(Settings(...))
#   app.py:3452                                    settings_mod.save(new)   <- no root=
#   settings.settings_path()                       <repo>/settings.json     <- HIS file
#
# The tests build a fresh Settings(...) naming two fields; every OTHER field
# sits at its dataclass default, and settings.py:90 is `tool_iterations = 48`.
# That whole object lands on the live file.
#
# ⚠️ IT LOOKS INTERMITTENT, WHICH IS WHY IT SURVIVED. The running app holds the
# real values in memory and writes them back on its next save, healing the file
# — so the damage is only visible when the app RESTARTS between a test run and
# that next save. Exactly the sequence Ryan hit: set 100, test run wrote 48,
# restart loaded 48.
#
# 🔴 THE CALL IS INVISIBLE AT THE TEST SITE. Those tests never mention save();
# they call an app method that reaches it three frames down. Grepping the tests
# for `save(` finds nothing, which is why this is a choke-point guard and not a
# fix applied to two call sites — the next test to call an app method that
# persists would reintroduce it, and nobody would see it in review.
#
# Third instance of this class in this repo, all found the same day: the suite
# evicting the live fleet row (a1e8686), the suite polluting .convos (55001fa),
# and this. One missing rule, not three bugs: A TEST MUST NEVER WRITE A PATH THE
# RUNNING APP OWNS.
@pytest.fixture(autouse=True)
def _never_write_the_live_settings(tmp_path, monkeypatch):
    """Redirect the DEFAULT settings path to a per-test temp dir.

    An explicit `root=` is honoured untouched, so tests that already pass
    tmp_path (test_settings.py does, correctly) behave exactly as before. Only
    the root=None case — the live file — is redirected.
    """
    import settings as settings_mod

    real_settings_path = settings_mod.settings_path

    def redirected(root=None):
        if root is not None:
            return real_settings_path(root)
        return tmp_path / settings_mod.SETTINGS_FILENAME

    monkeypatch.setattr(settings_mod, "settings_path", redirected)

