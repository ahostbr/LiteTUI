"""Generated pixels only; exercise real decoding and native-provider content conversion."""
import base64
import hashlib
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from litetui import claude_turn, native_image
from litetui.codex_app_server import user_input


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "WEBP"])
def test_bounded_review_uses_canonical_png_and_provider_content(tmp_path, fmt):
    path = tmp_path / ("chosen." + fmt.lower())
    Image.new("RGB", (1600, 48), (15, 25, 45)).save(path, format=fmt)
    details, encoded = native_image.read_image(None, str(path))
    assert (details["format"], details["width"], details["height"]) == (fmt, 1600, 48)
    raw = base64.b64decode(encoded, validate=True)
    assert details["png_bytes"] == len(raw)
    assert details["png_sha256"] == hashlib.sha256(raw).hexdigest()
    with Image.open(io.BytesIO(raw)) as real:
        assert real.format == "PNG" and real.width == 1536
    url = "data:image/png;base64," + encoded
    parts = [{"type": "image_url", "image_url": {"url": url}}, {"type": "text", "text": "inspect"}]
    codex = user_input([{"role": "user", "content": parts}])
    assert codex[0] == {"type": "image", "url": url}
    saved = tmp_path / "conversation" / "images" / "submitted.png"
    saved.parent.mkdir(parents=True)
    saved.write_bytes(raw)
    with patch.object(claude_turn, "ledger_for") as ledger:
        text, paths = claude_turn.inline_images(object(), parts, str(saved))
    ledger.assert_called_once()
    assert paths == [str(saved)] and str(saved) in text and "inspect" in text
    assert encoded not in text


def test_invalid_local_file_and_backend_refusals(tmp_path):
    app = SimpleNamespace(backend=SimpleNamespace(name="lmstudio", owns_native_turns=False), model_type="llm")
    with pytest.raises(ValueError, match="text-only"):
        native_image.delivery(app)
    app.backend = SimpleNamespace(name="claude", owns_native_turns=True)
    app.tools_enabled = False
    with pytest.raises(ValueError, match="Enable tools"):
        native_image.delivery(app)
    app.tools_enabled = True
    assert native_image.delivery(app) == "file"
    for invalid in ["relative.png", str(tmp_path / "missing"), str(tmp_path)]:
        with pytest.raises(ValueError):
            native_image.read_image(app, invalid)
    broken = tmp_path / "corrupt.png"
    broken.write_bytes(b"not pixels")
    with pytest.raises(ValueError, match="still PNG"):
        native_image.read_image(app, str(broken))
    with broken.open("wb") as file:
        file.truncate(native_image.MAX_FILE + 1)
    with pytest.raises(ValueError, match="8 MiB"):
        native_image.read_image(app, str(broken))


def test_wrong_receipt_is_refused_without_releasing_other_image():
    receipt = {"width": 1, "delivery": "inline", "work_instance": "source"}
    bridge = SimpleNamespace(composer_image=(receipt, "pixels"), closing=False, identity={}, instance="source")
    with patch.object(native_image.background_input, "bridge", return_value=bridge), patch.object(
        native_image.native_runtime_controls, "enabled", return_value=True
    ), pytest.raises(ValueError, match="changed"):
        native_image.consume(object(), {"native_image": {**receipt, "width": True}})
    assert bridge.composer_image == (receipt, "pixels")



def test_inline_image_respects_remaining_real_native_history_budgets(tmp_path):
    from litetui import history_projection, native_history

    path = tmp_path / "convo.jsonl"
    app = SimpleNamespace(store=SimpleNamespace(convo_path=path), conversation=[], _oversize_refusal=lambda _: None)
    with path.open("wb") as f:
        f.truncate(native_history.MAX_TRANSCRIPT - 1)
    with pytest.raises(ValueError, match="saved transcript limit"):
        native_image.check_inline_budget(app, "aW1n", "hi")
    path.unlink()
    app.conversation = [{"role": "system", "content": "S" * history_projection.MAX_CONTEXT}]
    with pytest.raises(ValueError, match="semantic/projection byte budget"):
        native_image.check_inline_budget(app, "aW1n", "hi")
