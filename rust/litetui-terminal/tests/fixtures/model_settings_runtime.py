"""Defaults/preset UI, actual requests and reopen; all external boundaries controlled."""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

path = Path(__file__).with_name("model_preferences_effort_runtime.py")
spec = importlib.util.spec_from_file_location("_settings_effort_boundaries", path)
assert spec and spec.loader
effort = importlib.util.module_from_spec(spec)
spec.loader.exec_module(effort)
original_seed = effort.seed


def seed(app):
    from litetui import settings_runtime
    original_seed(app)
    if "--convo" not in sys.argv:
        candidate = copy.deepcopy(app.settings)
        candidate.llama_presets = {
            "Boost": {"inference": {"temperature": 0.9, "reasoning_effort": "high"},
                      "load": {"ctx": 4096}},
        }
        settings_runtime.persist_or_raise(app, candidate)
        app.settings = candidate
        app.backend.set_settings(candidate)
        from litetui import effort_projection
        effort_projection.install(app)


if __name__ == "__main__":
    effort.full.seed = seed
    effort.full.listing = effort.listing
    effort.full.main()
