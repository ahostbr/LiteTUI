"""Bounded canonical capture/transcription, launched under an owning private job."""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

MAX_TEXT = 16_384
MAX_RECORDING = 60


def run(directory):
    from litetui import stt_backend

    parameters = json.loads((directory / "parameters.json").read_text(encoding="utf-8"))
    proc = None
    try:
        if stt_backend.missing():
            raise ValueError("missing dependencies")
        proc = stt_backend.record_start(parameters["microphone"] or None, directory=directory,
                                        duration_s=MAX_RECORDING)
        if proc is None:
            raise ValueError("no recording device")
        (directory / "recording").touch()
        until = time.monotonic() + MAX_RECORDING
        while proc.poll() is None and time.monotonic() < until and not (directory / "finish").exists():
            time.sleep(0.025)
        wav = stt_backend.record_stop(proc)
        proc = None
        (directory / "transcribing").touch()
        text = stt_backend.transcribe(wav, parameters["model"], local_only=True) if wav else ""
        text = " ".join(text.split())
        if len(text.encode("utf-8")) > MAX_TEXT or "\0" in text:
            raise ValueError("transcript exceeds composer limit")
        result = {"status": "ready" if text else "empty", "text": text}
    except (OSError, ValueError, RuntimeError, ImportError, subprocess.SubprocessError):
        result = {"status": "failed", "text": ""}
    finally:
        if proc is not None:
            stt_backend.record_stop(proc)
    (directory / "result.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
