"""Canonical reconnect through the registered source reader and owned lifecycle."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_model_lifecycle_boundaries", path)
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
common.history.serve = common.serve
if __name__ == "__main__":
    common.history.main()
