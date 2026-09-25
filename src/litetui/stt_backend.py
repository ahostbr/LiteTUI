"""Voice-in (dictation): record the mic, transcribe locally, no cloud.

Ryan 2026-09-18: "the lightest possible local stt ... whisper small" -> settled
on faster-whisper base.en (CTranslate2, CPU, ~140 MB, downloaded on opt-in).
Replaces the VRAM-evicting Qwen2-Audio-7B `listen` path for user dictation.

Capture reuses listen_tool's ffmpeg dshow helpers (already proven on this box).
Recording is a TOGGLE, not a fixed duration: record_start launches ffmpeg and
record_stop sends 'q' to its stdin so the WAV trailer is written cleanly (a
hard kill truncates the header). Transcription runs off the UI thread.

INTERPRETER: detection, model download and transcription all go through the
SAME global-first resolver as speech-out (optional_python — system Python
first, the locked project venv last). The app's venv does not carry
faster-whisper, so an in-process import would always fail; instead one
short-lived child under the resolved interpreter does the load+transcribe.
The model download (first WhisperModel load) happens inside that same child,
and the HF cache is per-user, so the Settings download and first live use
share it. Never inject another interpreter's site-packages into this process.
"""
from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from uuid import uuid4

from litetui import optional_python, paths
from litetui.listen_tool import (
    _check_not_silent,
    _ffmpeg,
    _first_dshow_device,
    _run,
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
_STT_TIMEOUT = 300  # CPU whisper is ~10-20x faster than realtime; safety cap


def missing() -> str:
    """What voice-in can't run with right now: '' = ready, else the missing
    part(s) ('faster-whisper', 'ffmpeg', or 'faster-whisper + ffmpeg').
    faster-whisper is probed through the global-first resolver — the locked
    app venv does not carry it, the system Python does."""
    out = []
    if _interpreter() is None:
        out.append("faster-whisper")
    if _ffmpeg() is None:
        out.append("ffmpeg")
    return " + ".join(out)


def available() -> bool:
    """True when faster-whisper (in a resolvable interpreter) AND ffmpeg are
    both present."""
    return missing() == ""


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
    wav = paths.data_root() / "recordings" / f"stt-{uuid4().hex}.wav"
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [ff, "-y", "-f", "dshow", "-i", f"audio={dev}",
           "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)]
    try:
        from litetui import ttyguard
        proc = ttyguard.popen(cmd, stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc._litetui_wav = wav
        return proc
    except Exception:
        return None


def record_stop(proc) -> str | None:
    """Stop recording gracefully ('q' -> ffmpeg writes the WAV trailer) and
    return the WAV path, or None if nothing usable was captured. A hard kill is
    the fallback; a WAV under a header's worth of bytes is treated as empty."""
    if proc is None:
        return None
    try:
        proc.stdin.write("q")  # ttyguard.popen opens the pipes in text mode
        proc.stdin.flush()
    except Exception:
        pass
    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            return None  # still writing/unknown: never transcribe an active file
    wav = getattr(proc, "_litetui_wav", None)
    if wav is not None and Path(wav).is_file() and Path(wav).stat().st_size > 1000:
        return str(wav)
    return None


#: The child does load+transcribe and prints the raw text. Args (not format
#: strings) carry the paths, so nothing in the WAV path can break the code.
_STT_CHILD = """\
import sys
from faster_whisper import WhisperModel
wav, size = sys.argv[1], sys.argv[2]
model = WhisperModel(size, device="cpu", compute_type="int8")
segments, _info = model.transcribe(wav, language="en")
print(" ".join(seg.text.strip() for seg in segments).strip())
"""


def _interpreter() -> str | None:
    """The global-first interpreter that can import faster_whisper — the same
    resolver speech-out uses (voice_backend.speak). None when no candidate
    Python has it."""
    return optional_python.resolve("faster_whisper")


def _run_worker(exe: str, wav: str, model_size: str) -> str:
    """Return the transcript; raise on launch, timeout or child failure.

    The UI reports failures separately from a successful but empty transcript.
    First use downloads the model inside the child (shared HF cache).
    """
    from litetui import ttyguard
    script = None
    try:
        # A WAV path with spaces or unicode exceeds nothing, but python -c
        # quoting does not deserve the risk either: write the child to a file
        # (same pattern as voice_backend.speak).
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8",
                                         suffix=".py", prefix="litetui-stt-",
                                         delete=False) as output:
            script = output.name
            output.write(_STT_CHILD)
        r = ttyguard.run([exe, "-E", "-B", script, wav, model_size],
                         timeout=_STT_TIMEOUT)
        if r.returncode != 0:
            detail = (r.stderr or "").strip()[-2000:]
            raise RuntimeError(f"transcriber exited {r.returncode}: {detail or 'no error output'}")
        return (r.stdout or "").strip()
    finally:
        if script is not None:
            Path(script).unlink(missing_ok=True)


def transcribe(wav: str, model_size: str = DEFAULT_MODEL) -> str:
    """Transcribe a WAV with faster-whisper. Blocking — call it in a worker
    thread. Returns "" for no speech; raises on transcription failure."""
    # Silence gate BEFORE whisper: base whisper hallucinates "You" / "Thank you"
    # on a silent or ultra-short clip (measured 2026-09-18 — a quiet take
    # printed "You"). listen_tool's volumedetect rejects it, so silence reads
    # as "no speech" instead of phantom text (and flags a mis-picked mic).
    try:
        if _check_not_silent(Path(wav)):
            return ""
    except Exception:
        pass
    exe = _interpreter()
    if exe is None:
        raise RuntimeError("faster-whisper interpreter is unavailable")
    text = _run_worker(exe, wav, model_size)
    # Belt for borderline audio that passes the volume gate: whisper's
    # stock hallucinations for "no real content" are a tiny closed set.
    if text.lower().strip(" .!?,") in _HALLUCINATIONS:
        return ""
    return text


if __name__ == "__main__":  # ponytail self-check — no mic/model needed
    assert isinstance(list_mics(), list)
    assert record_stop(None) is None
    print("missing:", missing() or "(nothing — ready)", "| mics:", list_mics())
