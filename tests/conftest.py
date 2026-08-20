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

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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
