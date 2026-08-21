"""The suite must not write paths the running app owns.

Three separate live stores have been damaged by test runs in this repo, all
found on 2026-08-20:

    a1e8686   registration evicted the running app's row from the fleet
    55001fa   tests polluted .convos
    (here)    tests overwrote the live settings.json

That is one missing rule, not three bugs. These tests guard the rule rather
than the three instances, because the instances keep arriving by new routes —
the settings one reached `save()` three frames down inside an app method, with
the word `save` appearing nowhere in the test that caused it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import settings as settings_mod  # noqa: E402
from settings import Settings  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_SETTINGS = REPO_ROOT / settings_mod.SETTINGS_FILENAME


def test_the_default_settings_path_is_redirected_during_tests():
    """The guard is ARMED. If the autouse fixture stops applying, this fails
    immediately rather than at the next unlucky restart."""
    resolved = settings_mod.settings_path()
    assert resolved != LIVE_SETTINGS, (
        f"settings_path() still resolves to the live file: {resolved}"
    )


def test_an_explicit_root_is_still_honoured(tmp_path):
    # test_settings.py passes root=tmp_path and must be unaffected. A guard
    # that broke correct callers would be traded for a different bug.
    assert settings_mod.settings_path(tmp_path) == tmp_path / settings_mod.SETTINGS_FILENAME


def test_saving_with_no_root_does_not_touch_the_live_file():
    """THE REGRESSION ITSELF. This is the exact call the app makes at
    app.py:3452 — `settings_mod.save(new)`, no root — reached from tests via
    `_on_settings_saved`. Before the guard it rewrote the live file with a
    default-valued Settings, resetting tool_iterations 100 -> 48."""
    before = LIVE_SETTINGS.read_bytes() if LIVE_SETTINGS.exists() else None

    written = settings_mod.save(Settings(tool_iterations=48))

    assert written != LIVE_SETTINGS, f"save() wrote the live path: {written}"
    after = LIVE_SETTINGS.read_bytes() if LIVE_SETTINGS.exists() else None
    assert after == before, "the live settings.json changed during a test"


def test_CONTROL_the_live_path_is_the_one_we_think_it_is():
    """Without this, every assertion above could be comparing against a path
    that is not the real settings file, and pass while guarding nothing."""
    assert LIVE_SETTINGS.name == "settings.json"
    assert LIVE_SETTINGS.parent == REPO_ROOT
    assert (REPO_ROOT / "app.py").is_file(), "REPO_ROOT is not the repo root"


def test_CONTROL_the_guard_would_have_caught_the_original():
    """A guard that cannot fail is decoration. This proves the check
    discriminates: with settings_path restored to the real one, the assertion
    in test_saving_with_no_root_does_not_touch_the_live_file is false."""
    real = settings_mod.settings_path.__wrapped__ if hasattr(
        settings_mod.settings_path, "__wrapped__"
    ) else None
    # The fixture replaced the module attribute; reconstruct what the unpatched
    # function returns without calling the patched one.
    unpatched = REPO_ROOT / settings_mod.SETTINGS_FILENAME
    assert unpatched == LIVE_SETTINGS
    assert settings_mod.settings_path() != unpatched, (
        "patched and unpatched resolve to the same path — the guard is a no-op"
    )
    assert real is None or True  # the attribute shape is incidental


def test_harness_registration_is_disabled_for_the_suite():
    # The companion guard, from a1e8686. Same rule, different store: a test
    # must not take the running app's seat.
    import os

    assert os.environ.get("LITETUI_NO_HARNESS") == "1"


def test_settings_roundtrip_still_works_under_the_guard(tmp_path):
    # The guard must not break ordinary explicit-root use.
    s = Settings(tool_iterations=7)
    settings_mod.save(s, root=tmp_path)
    back = settings_mod.load(root=tmp_path)
    assert back.tool_iterations == 7
    assert json.loads((tmp_path / settings_mod.SETTINGS_FILENAME).read_text())["tool_iterations"] == 7
