"""ocr.py — text out of an image using Windows' own OCR engine (Windows.Media.Ocr).

No tesseract, no model downloads: the OS ships the recognizer.

    python ocr.py path/to/img.jpg [--max-chars 2000]

Lines come back in reading order (top-to-bottom, then left-to-right),
joined with " | ", so you can guess the layout from a flat string.
"""
from __future__ import annotations

import asyncio
import sys

from winrt.windows.graphics.imaging import BitmapDecoder
from winrt.windows.media.ocr import OcrEngine
from winrt.windows.storage import FileAccessMode, StorageFile


async def recognize(path: str) -> list[tuple[int, int, str]]:
    file = await StorageFile.get_file_from_path_async(path)
    stream = await file.OpenAsync(FileAccessMode.Read)

    if path.lower().endswith((".png", ".webp")):
        decoder_id = BitmapDecoder.PngDecoderId
    else:
        decoder_id = BitmapDecoder.JpegDecoderId

    decoder = await BitmapDecoder.CreateAsync(decoder_id, stream)
    bitmap = await decoder.DecodePixelDataAsync()

    engine = OcrEngine.TryCreateFromUserProfileLanguage()
    if engine is None:
        raise RuntimeError("no OCR engine for the user profile language")
    result = await engine.RecognizeAsync(bitmap)

    # (y, x, text) per line — caller sorts for reading order
    return [(int(l.BoundingRect.Y), int(l.BoundingRect.X), l.Text) for l in result.Lines]


def main() -> None:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a.split("=")[0]: a.split("=", 1)[1] if "=" in a else None for a in sys.argv[1:] if a.startswith("--")}
    if len(args) < 1:
        print(__doc__)
        sys.exit(1)

    max_chars = int(flags.get("--max-chars") or 2000)
    lines = asyncio.run(recognize(args[0]))
    text = " | ".join(t for _, _, t in sorted(lines))
    print(text[:max_chars])


if __name__ == "__main__":
    main()

