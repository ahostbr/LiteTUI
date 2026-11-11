"""Real unpinned CLI startup/reopen with controlled llama and SDK boundaries."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

path = Path(__file__).with_name("model_preferences_full_runtime.py")
spec = importlib.util.spec_from_file_location("_effort_boundaries", path)
assert spec and spec.loader
full = importlib.util.module_from_spec(spec)
spec.loader.exec_module(full)
original_seed = full.seed


def seed(app):
    assert app._cli_thinking_level is None and app._cli_effective_thinking is None
    assert app._agent_session.authority.thinking_level == "medium"
    if "--convo" not in sys.argv:
        original_seed(app)
    else:
        assert app.conversation and app.convo_path.is_file()
        app._validate_owned_identity()
    full.common.report["effort_launch_unpinned"] = True
    full.common.report["effort_reopened"] = "--convo" in sys.argv


async def listing(_backend):
    from litetui.llm_backend import ModelRow

    return [ModelRow(model, "controlled.gguf", "fixture", True, nextn_predict_layers=2)
            for model in ("switch-old", "switch-new")]


if __name__ == "__main__":
    full.seed = seed
    full.listing = listing
    full.main()
