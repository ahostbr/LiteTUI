"""Use real source context-window admission to refuse a long caption before any provider request."""
from __future__ import annotations

import importlib.util
from pathlib import Path

path = Path(__file__).with_name("clipboard_runtime.py")
spec = importlib.util.spec_from_file_location("_clipboard_fixture", path)
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
original_seed = fixture.base.fixture.seed


def seed(app):
    original_seed(app)
    app.ctx_loaded = True
    app.ctx_max = app.settings.max_tokens_chat + 16


fixture.base.fixture.seed = seed
if __name__ == "__main__":
    fixture.base.fixture.history.main()
