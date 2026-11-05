"""Reuse the Context fixture's real owned conversation and record where its transcript lives."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

spec = importlib.util.spec_from_file_location("_system_prompt_base", Path(__file__).with_name("context_runtime.py"))
assert spec and spec.loader
base = importlib.util.module_from_spec(spec)
spec.loader.exec_module(base)
common = base.common
original_seed = common.seed


def seed(app):
    original_seed(app)
    assert app.conversation[0]["role"] == "system" and not app.store.pending
    (base.root / "system-seed.json").write_text(json.dumps({
        "transcript": str(app.convo_path),
        "system": app.conversation[0]["content"],
        "backend": app.backend.name,
    }, ensure_ascii=False), encoding="utf-8")


common.seed = seed
if __name__ == "__main__":
    common.history.main()
