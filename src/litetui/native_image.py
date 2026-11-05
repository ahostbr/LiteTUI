"""A single bounded image prepared for one explicitly submitted native composer prompt."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import stat
from pathlib import Path

from litetui import (
    appsvc,
    background_input,
    history_projection,
    native_context,
    native_history,
    native_runtime_controls,
)
from litetui.agent_store import _unlinked

OPERATION = "gui.images.native_prepare"
MAX_FILE = 8 * 1024 * 1024
MAX_PIXELS = 16_000_000


def delivery(app):
    if getattr(app.backend, "owns_native_turns", False):
        if not app.tools_enabled:
            raise ValueError("Claude opens a conversation-owned image file with tools. Enable tools first.")
        return "file"
    if getattr(app, "model_type", None) == "llm" and app.backend.name == "lmstudio":
        raise ValueError("The loaded LM Studio model is text-only. Select a vision model.")
    return "inline"


def read_image(app, raw, *, formats=None):
    if not isinstance(raw, str) or not raw or len(raw) > 2048 or any(ord(c) < 32 for c in raw):
        raise ValueError("Enter a local absolute image file path (up to 2048 characters).")
    path = Path(raw)
    if not path.is_absolute() or path.is_reserved() or (os.name == "nt" and (path.drive.startswith("\\\\") or ":" in str(path)[2:])):
        raise ValueError("Enter a local absolute image file path.")
    try:
        path = _unlinked(path).resolve(strict=True)
    except OSError as exc:
        raise ValueError("Image file does not exist or cannot be opened.") from exc
    if os.name == "nt" and path.drive.startswith("\\\\"):
        raise ValueError("The resolved image must be on a local drive.")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("Choose a regular image file.")
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE:
            raise ValueError("Choose an image file no larger than 8 MiB.")
        data = stream.read(MAX_FILE + 1)
    if not 0 < len(data) <= MAX_FILE:
        raise ValueError("Image file changed or exceeds 8 MiB.")
    return decode_image(Path(raw).name, data, formats or ["PNG", "JPEG", "WEBP"])


def decode_image(name, data, formats):
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data), formats=formats) as image:
            width, height = image.size
            if width * height > MAX_PIXELS:
                raise ValueError("Image exceeds 16 million pixels.")
            if getattr(image, "is_animated", False):
                raise ValueError("Choose a still image.")
            fmt = image.format
            encoded = appsvc.encode_image(image)
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("Could not read a still PNG, JPEG, or WebP image.") from exc
    if not encoded:
        raise ValueError("Could not decode this image.")
    png = base64.b64decode(encoded, validate=True)
    return ({"name": name, "format": fmt, "width": width, "height": height,
             "sent_width": int.from_bytes(png[16:20], "big"), "sent_height": int.from_bytes(png[20:24], "big"),
             "png_bytes": len(png), "png_sha256": hashlib.sha256(png).hexdigest()}, encoded)


def check_inline_budget(app, encoded, prompt):
    from litetui.conversation import ConversationRepository

    content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded}},
               {"type": "text", "text": prompt}]
    row = {"role": "user", "content": content}
    # The image is never a native frame. Its base64 counts in the semantic
    # context and in the actual JSONL record that a subsequent native Resume reads.
    history_projection.encoded([*app.conversation, row], history_projection.MAX_CONTEXT)
    path = app.store.convo_path
    size = path.stat().st_size if path is not None and path.is_file() else 0
    record = {"type": "msg", "message": row, "ts": 1.7976931348623157e308}
    if size + len(ConversationRepository.record_bytes(record, separator=True)) > native_history.MAX_TRANSCRIPT:
        raise ValueError("The image would exceed the 8 MiB saved transcript limit. Choose a smaller image.")
    if reason := app._oversize_refusal(content):
        raise ValueError(reason)


def prepare(app, cmd):
    bridge, binding = native_context.read_binding(app, cmd, OPERATION, "path", "clear")
    if not native_runtime_controls.enabled(app) or bridge.closing:
        raise ValueError("Image attachments require an active owned runtime.")
    current = getattr(bridge, "composer_image", None)
    if cmd["path"] is None:
        if current is None or current[0]["token"] != cmd["clear"]:
            candidate = getattr(bridge, "clipboard_candidate", None)
            if candidate is None or candidate[0]["token"] != cmd["clear"]:
                raise ValueError("This attachment no longer belongs to the current composer.")
            bridge.clipboard_candidate = None
        else:
            bridge.composer_image = None
            bridge.clipboard_candidate = None
        return {"version": 1, "kind": "image_cleared", **binding, "token": cmd["clear"]}
    if cmd["clear"] is not None:
        raise ValueError("Choose one image action.")
    mode = delivery(app)
    details, encoded = read_image(app, cmd["path"])
    if mode == "inline":
        check_inline_budget(app, encoded, "Describe this image.")
    bridge.check()
    review = {"version": 1, "kind": "image_review", **binding, "token": secrets.token_hex(32),
              "delivery": mode, **details}
    bridge.clipboard_candidate = None
    bridge.composer_image = (review, encoded)
    return review


def clipboard(app, cmd):
    from litetui import native_clipboard

    bridge, binding = native_context.read_binding(app, cmd, "gui.images.native_clipboard", "previous", "select", "path")
    if not native_runtime_controls.enabled(app) or bridge.closing:
        raise ValueError("Image paste requires an active owned runtime.")
    prior = getattr(bridge, "composer_image", None)
    if cmd["previous"] != (prior[0]["token"] if prior else None):
        raise ValueError("The previous attachment changed. Reopen the composer picker.")
    if cmd["select"] is not None:
        candidate = getattr(bridge, "clipboard_candidate", None)
        if (cmd["path"] is not None or candidate is None or cmd["select"] != candidate[0]["token"]
                or delivery(app) != candidate[0]["delivery"]):
            raise ValueError("This reviewed clipboard image changed. Read the clipboard again.")
        bridge.composer_image = candidate
        bridge.clipboard_candidate = None
        return candidate[0]
    mode = delivery(app)
    details, encoded = native_clipboard.read(app) if cmd["path"] is None else read_image(app, cmd["path"])
    if mode == "inline":
        check_inline_budget(app, encoded, "Describe this image.")
    bridge.check()
    review = {"version": 1, "kind": "image_review", **binding, "token": secrets.token_hex(32),
              "delivery": mode, **details}
    bridge.clipboard_candidate = (review, encoded)
    return review


def retained(app, received):
    """Prove the exact submitted review is still owned after a pre-admission refusal."""
    bridge = background_input.bridge(app)
    entry = getattr(bridge, "composer_image", None)
    if (not entry or bridge.closing or not native_runtime_controls.enabled(app)
            or background_input.encoded(received) != background_input.encoded(entry[0])
            or bridge.instance != entry[0]["work_instance"]
            or any(entry[0][key] != value for key, value in bridge.identity.items())):
        return None
    try:
        bridge.check()
    except (OSError, ValueError):
        return None
    return entry[0]


def consume(app, cmd):
    bridge = background_input.bridge(app)
    entry = getattr(bridge, "composer_image", None)
    if (retained(app, cmd["native_image"]) is None or delivery(app) != entry[0]["delivery"]
            or getattr(app, "pending_image", None)):
        raise ValueError("The selected image or conversation changed. Select the image again.")
    bridge.check()
    if entry[0]["delivery"] == "inline":
        check_inline_budget(app, entry[1], cmd["message"])
    bridge.composer_image = None
    bridge.clipboard_candidate = None
    return entry[1]
