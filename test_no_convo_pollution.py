"""No test may write into the user's real .convos/ store.

Booting LiteTUI creates `.convos/<uuid>/` with seed files before a single word
is typed. Ten empty conversations accumulated in one morning of test runs and
surfaced in /resume as "(no user message)" rows the user had to scroll past —
noise indistinguishable, at a glance, from real work.

Two guards, because either alone is weak:
  1. STATIC — any test that boots the app must redirect CONVO_DIR.
  2. BEHAVIOURAL — actually run the suite and assert the real store did not grow.
     The static check can be satisfied by a redirect that happens too late or
     targets the wrong module; only the count proves nothing was written.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

#: Files that construct the app or drive it through a pilot.
BOOTS = re.compile(r"LiteTUI\(\)|run_test\(")
#: A redirect of the module global, in any of the forms in use.
REDIRECTS = re.compile(r"\bCONVO_DIR\s*=")


def _test_files() -> list[Path]:
    return sorted(p for p in ROOT.glob("test_*.py") if p.name != Path(__file__).name)


def test_every_app_booting_test_redirects_the_store():
    offenders = []
    for f in _test_files():
        src = f.read_text(encoding="utf-8", errors="replace")
        if BOOTS.search(src) and not REDIRECTS.search(src):
            offenders.append(f.name)
    assert offenders == [], (
        "these tests boot LiteTUI without redirecting CONVO_DIR, so they leave "
        "empty conversations in the user's real store:\n  " + "\n  ".join(offenders)
    )


def test_the_static_check_can_actually_fail(tmp_path):
    """NEGATIVE CONTROL — the detector must flag a file that boots and does not redirect.

    Without this, a regex that silently stopped matching would report a clean
    suite forever, which is the failure mode it exists to prevent.
    """
    bad = "app = LiteTUI()\n"
    good = "import app as m\nm.CONVO_DIR = tmp\napp = LiteTUI()\n"
    assert BOOTS.search(bad) and not REDIRECTS.search(bad)
    assert BOOTS.search(good) and REDIRECTS.search(good)


@pytest.mark.parametrize("script", ["test_thinking.py", "test_modals.py"])
def test_running_a_booting_test_does_not_grow_the_real_store(script):
    """Behavioural half: run it for real and count the store before and after.

    This is what the static check cannot do. A redirect assigned after the app
    is constructed, or applied to a re-imported copy of the module, satisfies
    the regex and still writes to the real directory.
    """
    import app as app_mod

    # Read the REAL path from a fresh subprocess-free import, not from whatever
    # a sibling test may already have redirected in this process.
    real = Path(app_mod.__file__).resolve().parent / ".convos"

    def count() -> int:
        return len(list(real.iterdir())) if real.exists() else 0

    before = count()
    proc = subprocess.run(
        [sys.executable, str(ROOT / script)],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(ROOT),
    )
    after = count()
    assert after == before, (
        f"{script} added {after - before} conversation(s) to the real store "
        f"({real}). Redirect CONVO_DIR to a temp dir.\n"
        f"exit={proc.returncode}"
    )
