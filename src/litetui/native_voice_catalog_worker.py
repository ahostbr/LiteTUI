"""Device-name discovery only, run inside the parent's bounded owned job."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

MAX_ROWS = 64
MAX_TEXT = 512
MAX_BYTES = 64 * 1024


def names(items):
    if type(items) is not list or len(items) > MAX_ROWS:
        raise ValueError("Voice catalog exceeds its row limit")
    if any(
        type(item) is not str
        or not item
        or len(item.encode("utf8")) > MAX_TEXT
        or not all(c.isprintable() for c in item)
        for item in items
    ):
        raise ValueError("Voice catalog contains an invalid name")
    return tuple(dict.fromkeys(items))


def bounded_probe(command, timeout):
    """Bound ffmpeg stderr while its inherited parent Job owns all descendants."""
    from litetui.ttyguard import NO_WINDOW

    cached = bytearray()
    overflow = False
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        creationflags=NO_WINDOW,
    ) as child:

        def read():
            nonlocal overflow
            while chunk := child.stderr.read(4096):
                remaining = MAX_BYTES - len(cached)
                cached.extend(chunk[:remaining])
                overflow |= len(chunk) > remaining

        reader = threading.Thread(
            target=read, name="voice-catalog-stderr", daemon=False
        )
        reader.start()
        try:
            child.wait(timeout=timeout)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=2)
            reader.join(timeout=2)
            if reader.is_alive():
                # Non-daemon reader retains this helper until its owning Job
                # cancels every inherited pipe owner on the parent deadline.
                raise OSError("Voice catalog output reader has not retired")
        if overflow:
            raise ValueError("Voice catalog probe output exceeds its limit")
    return SimpleNamespace(stderr=cached.decode("utf8", errors="replace"))


def run(directory):
    parameters = json.loads((directory / "parameters.json").read_text(encoding="utf8"))
    if parameters == {"field": "stt_mic"}:
        from litetui import stt_backend

        previous = stt_backend._run
        try:
            stt_backend._run = bounded_probe
            items = stt_backend.list_mics()
        finally:
            stt_backend._run = previous
    elif parameters == {"field": "tts_voice"}:
        from litetui.voice_backend import list_sapi_voices

        items = list_sapi_voices()
    else:
        raise ValueError("Unknown voice catalog")
    raw = json.dumps({"items": names(items)}, ensure_ascii=False).encode("utf8")
    if len(raw) > MAX_BYTES:
        raise ValueError("Voice catalog exceeds its byte limit")
    (directory / "result.json").write_bytes(raw)


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
