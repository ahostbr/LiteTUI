"""Run model preference operations and a real turn builder behind fake SDK boundaries."""

from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_model_preference_boundaries", path)
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original = common.seed


def seed(app):
    from litetui import settings_runtime

    original(app)
    candidate = copy.deepcopy(app.settings)
    candidate.temperature = 0.7
    candidate.model_infer_overrides = {
        app.model_id: {"top_p": 0.9},
        "unselected-model": {"temperature": 0.61},
    }
    settings_runtime.persist_or_raise(app, candidate)
    app.settings = candidate
    app.backend.set_settings(candidate)


common.seed = seed
common.history.serve = common.serve
if __name__ == "__main__":
    common.history.main()
