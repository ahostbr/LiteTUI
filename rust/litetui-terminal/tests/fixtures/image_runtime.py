"""Disposable image and capture of the real repository resume reader. Providers stay controlled."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import random
from pathlib import Path

from PIL import Image

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_image_boundaries", path)
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
seed_source = fixture.seed
serve_source = fixture.serve
root = Path(os.environ["LITETUI_DATA_ROOT"])


def seed(app):
    seed_source(app)
    # Deterministic gradient with fine-grained RGB detail, large enough to exercise real photo sizes.
    size = (1920, 1080)
    noise = Image.frombytes("RGB", size, random.Random(617).randbytes(size[0] * size[1] * 3))
    sky = Image.linear_gradient("L").resize(size).convert("RGB")
    Image.blend(sky, noise, 0.34).save(root / "selected image.png")


async def serve(app):
    from litetui import native_history
    from litetui.conversation import ConversationRepository
    try:
        await serve_source(app)
    finally:
        native_history.capture_file(app.store.convo_path)  # full native Resume capture/projection budgets
        _, rows = ConversationRepository.read(app.store.convo_path, max_bytes=native_history.MAX_TRANSCRIPT)
        user = [row["content"] for row in rows if row.get("role") == "user"][-1]
        if isinstance(user, list):
            raw = base64.b64decode(user[0]["image_url"]["url"].split(",", 1)[1], validate=True)
            digest = hashlib.sha256(raw).hexdigest()
            text = user[1]["text"]
        else:
            digest, text = None, user
        (root / "image-resume-reader.json").write_text(
            json.dumps({"digest": digest, "text": text, "pending_image": app.pending_image is not None}),
            encoding="utf-8",
        )


fixture.seed = seed
fixture.history.serve = serve

if __name__ == "__main__":
    fixture.history.main()
