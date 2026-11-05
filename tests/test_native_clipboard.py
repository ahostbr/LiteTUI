"""Exercise the clipboard allocation/ownership logic with private memory, never the OS clipboard."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from PIL import Image

from litetui import native_clipboard, native_image

spec = importlib.util.spec_from_file_location(
    "_private_clipboard_samples", Path(__file__).resolve().parents[1] / "rust/litetui-terminal/tests/fixtures/clipboard_samples.py",
)
assert spec and spec.loader
samples = importlib.util.module_from_spec(spec)
spec.loader.exec_module(samples)
PrivateClipboard, bitmap, file_list = samples.PrivateClipboard, samples.bitmap, samples.file_list


@pytest.mark.parametrize(("number", "kind"), [(999, "PNG"), (8, "DIB"), (17, "DIBV5")])
def test_private_clipboard_decodes_and_releases_one_version(monkeypatch, number, kind):
    api = PrivateClipboard({number: bitmap(kind, "navy")})
    monkeypatch.setattr(native_clipboard, "_win32", lambda: api)
    details, _ = native_clipboard.read(None)
    assert details["clipboard_sequence"] == 91 and details["clipboard_format"] == kind
    assert (details["sent_width"], details["sent_height"]) == (23, 15)
    assert api.events == ["open", ("get", number), "lock", "unlock", "close"]


def test_unsupported_busy_and_oversize_never_lock_or_leave_clipboard_open():
    api = PrivateClipboard({13: b"text"})
    with pytest.raises(ValueError, match="No supported image"):
        native_clipboard.capture(api)
    assert api.events == ["open", "close"]
    api = PrivateClipboard({999: b"x"})
    api.size = lambda _: native_image.MAX_FILE + 1
    with pytest.raises(ValueError, match="bounded allocation"):
        native_clipboard.capture(api)
    assert api.events == ["open", ("get", 999), "close"]
    api = PrivateClipboard({})
    api.opened = lambda _: False
    with pytest.raises(ValueError, match="busy"):
        native_clipboard.capture(api)
    assert not api.events


def test_file_drop_skips_nonimages_and_validates_names_and_bounds(tmp_path, monkeypatch):
    pic = tmp_path / "snow 雪.bmp"
    Image.new("RGB", (8, 9)).save(pic)
    api = PrivateClipboard({15: file_list([str(tmp_path / "a.txt"), str(tmp_path / "missing.png"), str(pic)])})
    monkeypatch.setattr(native_clipboard, "_win32", lambda: api)
    details, _ = native_clipboard.read(None)
    assert details["name"] == pic.name and details["format"] == "BMP" and details["clipboard_format"] == "FILES"
    for malformed in (bytes(10), file_list(["x"])[:-2], file_list(["x"] * 33)):
        with pytest.raises(ValueError):
            native_clipboard.file_names(malformed)
