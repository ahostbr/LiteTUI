"""Make the repo root importable from tests/.

The tests moved out of the repo root into tests/, and `import app` resolved only
by accident: `python -m pytest` from the root puts the CWD on sys.path. Run
pytest from anywhere else, or as a bare `pytest`, and every module here fails to
import.

This makes it explicit rather than dependent on how you happened to invoke it.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
