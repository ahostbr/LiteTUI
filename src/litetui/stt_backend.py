"""Voice-in (dictation): record the mic, transcribe locally, no cloud.

Ryan 2026-09-18: "the lightest possible local stt ... whisper small" -> settled
on faster-whisper base.en (CTranslate2, CPU, ~140 MB, downloaded on opt-in).
Replaces the VRAM-evicting Qwen2-Audio-7B `listen` path for user dictation.

Capture reuses listen_tool's ffmpeg dshow helpers (already proven on this box).
Recording is a TOGGLE, not a fixed duration: record_start launches ffmpeg and
record_stop sends 'q' to its stdin so the WAV trailer is written cleanly (a
hard kill truncates the header). Transcription runs off the UI thread; the
model is cached after first load.
"""
from __future__ import annotations

import importlib.util
import re
import subprocess

from litetui import paths
from litetui.listen_tool import (
    _check_not_silent, _ffmpeg, _first_dshow_device, _run,
)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DEFAULT_MODEL = "base.en"
#: Whisper's stock output on silent/contentless audio — dropped so an empty
#: take never injects phantom text into the input box.
_HALLUCINATIONS = frozenset({
    "you", "thank you", "thanks for watching", "thanks for watching!",
    "bye", "bye.", "so", ".", "",
})
_WAV = paths.data_root() / "stt-record.wav"
_model_cache: dict = {}


def available() -> bool:
    """True when faster-whisper is importable AND ffmpeg is on PATH — both are
    needed, so the UI can say which is missing instead of failing mid-record."""
    return importlib.util.find_spec("faster_whisper") is not None and _ffmpeg() is not None


def list_mics() -> list[str]:
    """Every Direct Show audio input name for the settings picker (listen_tool
    only exposes the FIRST). Empty when ffmpeg is absent or none are found."""
    ff = _ffmpeg()
    if not ff:
        return []
    try:
        proc = _run([ff, "-hide_banner", "-f", "dshow",
                     "-list_devices", "1", "-i", "dummy"], 15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return re.findall(r'"([^"]+)"\s*\(audio\)', proc.stderr or "")


def record_start(device: str | None = None):
    """Start recording to a 16 kHz mono WAV. Returns the ffmpeg Popen (its
    stdin is the stop channel) or None if there is no ffmpeg or no mic."""
    ff = _ffmpeg()
    if not ff:
        return None
    dev = (device or "").strip() or _first_dshow_device()
    if not dev:
        return None
    _WAV.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-y", "-f", "dshow", "-i", f"audio={dev}",
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(_WAV)]
    try:
        return subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                                creationflags=_NO_WINDOW)
    except Exception:
        return None


def record_stop(proc) -> "str | None":
    """Stop recording gracefully ('q' -> ffmpeg writes the WAV trailer) and
    return the WAV path, or None if nothing usable was captured. A hard kill is
    the fallback; a WAV under a header's worth of bytes is treated as empty."""
    if proc is None:
        return None
    try:
        proc.stdin.write(b"q")
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    if _WAV.exists() and _WAV.stat().st_size > 1000:
        return str(_WAV)
    return None


def transcribe(wav: str, model_size: str = DEFAULT_MODEL) -> str:
    """Transcribe a WAV with faster-whisper. Downloads the model on first use
    (the opt-in gate is the caller choosing to run this). Blocking — call it in
    a worker thread. Returns the text, or "" on any failure (never raises)."""
    # Silence gate BEFORE whisper: base whisper hallucinates "You" / "Thank you"
    # on a silent or ultra-short clip (measured 2026-09-18 — a quiet take
    # printed "You"). listen_tool's volumedetect rejects it, so silence reads
    # as "no speech" instead of phantom text (and flags a mis-picked mic).
    try:
        from pathlib import Path
        if _check_not_silent(Path(wav)):
            return ""
    except Exception:
        pass
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return ""
    try:
        model = _model_cache.get(model_size)
        if model is None:
            model = WhisperModel(model_size, device="cpu", compute_type="int8")
            _model_cache[model_size] = model
        segments, _info = model.transcribe(wav, language="en")
        text = " ".join(s.text.strip() for s in segments).strip()
        # Belt for borderline audio that passes the volume gate: whisper's
        # stock hallucinations for "no real content" are a tiny closed set.
        if text.lower().strip(" .!?,") in _HALLUCINATIONS:
            return ""
        return text
    except Exception:
        return ""


if __name__ == "__main__":  # ponytail self-check — no mic/model needed
    assert isinstance(list_mics(), list)
    assert record_stop(None) is None
    assert transcribe("nonexistent.wav") == ""
    print("available:", available(), "| mics:", list_mics())
