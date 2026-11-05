"""All model-screen controls through a registered child; external llama boundaries are fixtures."""

from __future__ import annotations

import copy
import importlib.util
import os
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_full_model_preference_boundaries", path)
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
        app.model_id: {"future-infer": {"keep": True}},
        "unselected": {"top_k": 99},
    }
    candidate.llama_load_settings = {
        app.model_id: {"future-load": {"keep": True}},
        "unselected": {"ctx": 8192},
    }
    settings_runtime.persist_or_raise(app, candidate)
    app.settings = candidate
    app.backend.set_settings(candidate)


async def running(backend):
    backend._attached_host = "http://127.0.0.1:45120"
    backend._attached_owner = "fixture-owner"
    backend._shape = "router"
    return "controlled server"


async def listing(backend):
    from litetui.llm_backend import ModelRow

    return [
        ModelRow(
            "switch-old", "controlled.gguf", "fixture", True, nextn_predict_layers=2
        )
    ]


async def info(_backend, _model):
    return 8192, "llm", True


async def chat_ready(_backend, _model):
    return None


if __name__ == "__main__":
    import traceback

    from litetui import llm_backend

    denied = common.history.boundary.forbidden

    def forbidden(*args, **kwargs):
        (common.root / "preference-denied-stack.txt").write_text(
            "".join(traceback.format_stack()), encoding="utf8"
        )
        return denied(*args, **kwargs)

    common.history.boundary.forbidden = forbidden
    common.seed = seed
    common.history.serve = common.serve
    with ExitStack() as stack:
        for owner, name, value in (
            (llm_backend.LlamaCppBackend, "ensure_running", running),
            (llm_backend.LlamaCppBackend, "list_models", listing),
            (llm_backend.LlamaCppBackend, "model_info", info),
            (llm_backend.LlamaCppBackend, "ensure_chat_ready", chat_ready),
            (llm_backend.LlamaCppBackend, "load", common.history.boundary.forbidden),
            (llm_backend.LlamaCppBackend, "unload", common.history.boundary.forbidden),
            (
                llm_backend.LlamaCppBackend,
                "apply_load_settings",
                common.history.boundary.forbidden,
            ),
            (llm_backend, "_healthy", lambda *_args, **_kwargs: True),
            (
                llm_backend,
                "configured_flags",
                lambda _settings: frozenset(llm_backend.FLAG_FOR.values()) | {"jinja"},
            ),
            (
                llm_backend,
                "configured_spec_types",
                lambda _settings: frozenset(("none", "draft-simple", "draft-mtp")),
            ),
            (llm_backend, "gguf_metadata", lambda _path: ({}, 2)),
        ):
            stack.enter_context(patch.object(owner, name, value))
        common.history.main()
