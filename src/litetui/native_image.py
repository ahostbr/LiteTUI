"""A single bounded image prepared for one explicitly submitted native composer prompt."""
from __future__ import annotations

import base64
import hashlib
import io
import os
import secrets
import stat
import tempfile
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


def check_inline_budget(app, encoded, prompt, reserved=0):
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
    if size + reserved + len(ConversationRepository.record_bytes(record, separator=True)) > native_history.MAX_TRANSCRIPT:
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


def preview(app, cmd):
    from PIL import Image

    bridge, binding = native_context.read_binding(app, cmd, "gui.images.native_preview", "token", "png_sha256")
    if not native_runtime_controls.enabled(app) or bridge.closing:
        raise ValueError("Image preview requires an active owned composer")
    for slot in ("clipboard_candidate", "composer_image"):
        entry = getattr(bridge, slot, None)
        if entry and entry[0]["token"] == cmd["token"]:
            break
    else:
        raise ValueError("This image is no longer selected")
    review, encoded = entry
    if (any(review[key] != value for key, value in binding.items())
            or cmd["png_sha256"] != review["png_sha256"]):
        raise ValueError("The reviewed image changed")
    png = base64.b64decode(encoded, validate=True)
    if len(png) != review["png_bytes"] or hashlib.sha256(png).hexdigest() != review["png_sha256"]:
        raise ValueError("Retained image bytes differ from the review")
    with Image.open(io.BytesIO(png), formats=["PNG"]) as source:
        if source.size != (review["sent_width"], review["sent_height"]):
            raise ValueError("Retained image dimensions differ from the review")
        pixels = thumbnail(source)
    bridge.check()
    return {"kind": "image_preview", "version": 1, **binding, "token": review["token"],
            "png_sha256": review["png_sha256"], **pixels}


def thumbnail(source):
    """Produce the same bounded raster for an open, validated still image."""
    from PIL import Image

    source.thumbnail((64, 40), Image.Resampling.LANCZOS)
    return {"width": source.width, "height": source.height,
            "rgba": base64.b64encode(source.convert("RGBA").tobytes()).decode("ascii")}


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


# --- A reviewed image attached to a mid-turn queued message --------------------------------------
# The Work ledger rewrites its whole document on every state change and caps a payload at 64 KiB, so
# queued pixels never enter it. The ledger keeps the image's identity (name, PNG byte count, SHA-256);
# the bytes live in one conversation-owned content-addressed file that is re-verified on every read.

def queued_descriptor(review):
    return {"name": review["name"], "png_bytes": review["png_bytes"], "png_sha256": review["png_sha256"]}


def validate_descriptor(image):
    if (type(image) is not dict or set(image) != {"name", "png_bytes", "png_sha256"}
            or type(image["name"]) is not str or not 0 < len(image["name"]) <= 1024
            or any(ord(c) < 32 for c in image["name"])
            or type(image["png_bytes"]) is not int or not 0 < image["png_bytes"] <= 16 * 1024 * 1024
            or type(image["png_sha256"]) is not str or len(image["png_sha256"]) != 64
            or any(c not in "0123456789abcdef" for c in image["png_sha256"])):
        raise ValueError("Malformed queued image identity")
    return image


def queued_path(app, image):
    return Path(app.convo_dir) / "images" / f"queued-{validate_descriptor(image)['png_sha256']}.png"


def _exact(png, image):
    if len(png) != image["png_bytes"] or hashlib.sha256(png).hexdigest() != image["png_sha256"]:
        raise ValueError("Queued image bytes differ from the reviewed identity")
    return png


def store_queued(app, review, encoded):
    """Write the exact retained PNG once under its own digest, then read it back."""
    image = validate_descriptor(queued_descriptor(review))
    png = _exact(base64.b64decode(encoded, validate=True), image)
    path = queued_path(app, image)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        descriptor, name = tempfile.mkstemp(prefix=".queued-image-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(png)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            Path(name).unlink(missing_ok=True)
    read_queued(app, image)
    return str(path)


def read_queued(app, image):
    path = queued_path(app, image)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != image["png_bytes"]:
            raise ValueError("Queued image file is missing or changed")
        png = _exact(path.read_bytes(), image)
    except OSError as exc:
        raise ValueError("Queued image file is missing or unreadable") from exc
    return base64.b64encode(png).decode("ascii")


def provider_payload(app, payload):
    """The payload as its provider owner stores and sends it, with the image bytes re-verified."""
    if "image" not in payload:
        return payload
    image = validate_descriptor(payload["image"])
    text = payload["content"]
    if getattr(app.backend, "owns_native_turns", False):
        from litetui.claude_turn import inline_images
        read_queued(app, image)
        content, _ = inline_images(app, [{"type": "image_url", "image_url": {"url": "data:image/png;base64,"}},
                                         {"type": "text", "text": text}], str(queued_path(app, image)))
    else:
        content = [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + read_queued(app, image)}},
                   {"type": "text", "text": text}]
    return {key: value for key, value in {**payload, "content": content}.items() if key != "image"}
