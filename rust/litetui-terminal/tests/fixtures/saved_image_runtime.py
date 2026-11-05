"""Seed one owned transcript with two different embedded images and external decoys."""
from __future__ import annotations

import base64
import importlib.util
import io
import json
import os
from pathlib import Path

from PIL import Image

root = Path(os.environ["LITETUI_DATA_ROOT"])
spec = importlib.util.spec_from_file_location("_saved_image_fixture", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"]))
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed = common.seed


def seed(app):
    original_seed(app)
    parts = [{"type": "text", "text": "Two saved images"}]
    for color in ("#ff0000", "#00ff00"):
        with io.BytesIO() as out:
            Image.new("RGB", (120, 60), color).save(out, format="PNG")
            uri = "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()
        parts.append({"type": "image_url", "image_url": {"url": uri}})
        parts.append({"type": "text", "text": "Between image blocks"})
    parts.extend({"type": "image_url", "image_url": {"url": u}} for u in
                 ["https://example.invalid/SYNTHETIC_REMOTE", "file:///SYNTHETIC_FILE"])
    app.conversation.append({"role": "user", "content": parts})
    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
    assert not app.store.persist_error
    (root / "saved-image-input.json").write_text(json.dumps({"path": str(app.store.convo_path)}), encoding="utf8")


common.seed = seed
if __name__ == "__main__":
    common.history.main()
