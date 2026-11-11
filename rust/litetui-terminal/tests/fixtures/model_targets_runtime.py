"""Canonical LM Studio load/unload with a private SDK and actual owned runtime."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

from litetui import llm_backend

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_target_boundaries", path)
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed = common.seed
original_serve = common.serve
resident = {"switch-old": 4096, "switch-new": None}
effects = []
root = Path(os.environ["LITETUI_DATA_ROOT"])


def save():
    (root / "model-target-effects.json").write_text(
        json.dumps(effects), encoding="utf-8"
    )


def rows(_backend):
    return [
        {
            "id": key,
            "type": "llm",
            "max_context_length": 8192,
            "loaded_context_length": value,
        }
        for key, value in resident.items()
    ]


class Sdk:
    def load(self, key, config=None):
        assert key in resident and (
            config is None or config.keys() == {"contextLength"}
        )
        effects.append(["load", key, config])
        resident[key] = config["contextLength"] if config else 4096
        save()

    def unload(self, key):
        assert key in resident
        effects.append(["unload", key])
        resident[key] = None
        save()


async def allow(key):
    assert key in resident
    return True


def seed(app):
    original_seed(app)
    app.backend.vram_gate = allow
    app.backend.resource_admission = None
    assert app.backend.name == "lmstudio"
    save()


async def serve(app):
    with (
        patch.object(llm_backend.LMStudioBackend, "_native_models", rows),
        patch.object(llm_backend.LMStudioBackend, "_sdk", staticmethod(Sdk)),
    ):
        await original_serve(app)


common.seed = seed
common.history.serve = serve
if __name__ == "__main__":
    common.history.main()
