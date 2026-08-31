"""The console-script fast-path: ``litetui --version`` must print and exit without
building the app (T127 release prep). Every package manager, wizard, and curious
user runs exactly that probe on a cold start; this is what keeps it cheap.

We run the REAL entry point in a subprocess — the same thing a stranger's shell
does after ``uv pip install litetui`` — rather than calling main() in-process,
so an accidental heavy import would show up as a slow/hung probe, not a pass.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))
from litetui.version import __version__  # noqa: E402


def _probe(*args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-m", "litetui.cli", *args],
        capture_output=True, text=True, cwd=ROOT, env=env, timeout=60,
    )


def test_version_flag_prints_the_version_and_exits():
    r = _probe("--version")
    assert r.returncode == 0, f"non-zero exit: {r.stderr}"
    assert r.stdout.strip() == f"litetui {__version__}", repr(r.stdout)


def test_short_V_flag_does_the_same():
    r = _probe("-V")
    assert r.returncode == 0, f"non-zero exit: {r.stderr}"
    assert r.stdout.strip() == f"litetui {__version__}", repr(r.stdout)
