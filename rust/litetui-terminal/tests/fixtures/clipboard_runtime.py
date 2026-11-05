"""Private Windows-compatible clipboard allocations feed the unchanged owned-source clipboard reader."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

from litetui import native_clipboard


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


base = module("_controlled_image", Path(__file__).with_name("image_runtime.py"))
helpers = module("_private_clipboard", Path(__file__).with_name("clipboard_samples.py"))
root = Path(os.environ["LITETUI_DATA_ROOT"])
snapshots = []


def private_clipboard():
    index = len(snapshots)
    versions = [
        {8: helpers.bitmap("DIB", "navy")},
        {999: helpers.bitmap("PNG", "orange")},
        {15: helpers.file_list([str(root / "a.txt").removeprefix("\\\\?\\"), str(root / "selected image.png").removeprefix("\\\\?\\")])},
        {13: b"ordinary Unicode clipboard text"},
        {17: helpers.bitmap("DIBV5", "plum")},
    ]
    assert index < len(versions), "Unexpected additional clipboard read"
    snapshot = helpers.PrivateClipboard(versions[index], 201 + index)
    snapshots.append(snapshot)
    return snapshot


async def serve(app):
    original = native_clipboard._win32
    native_clipboard._win32 = private_clipboard
    try:
        await base.serve(app)
    finally:
        native_clipboard._win32 = original
        (root / "clipboard-ownership.json").write_text(json.dumps([clip.events for clip in snapshots]), encoding="utf-8")


base.fixture.history.serve = serve
if __name__ == "__main__":
    base.fixture.history.main()
