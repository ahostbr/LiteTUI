"""Speak the agent's replies aloud — local-first, two engines.

Ryan 2026-09-18: "ship the lightest possible local stt/tts backend ... do both
pytts and edge support". This is the TTS-out half.

    pyttsx3  -> Windows SAPI5 DIRECTLY. No MCI, no playsound, no network, no
                download — the SAPI voices (David/Zira) are already installed.
                The default, because it always works offline. Robotic, but real.
    edge     -> Microsoft's cloud neural voices (en-GB-SoniaNeural &c): saved to
                mp3 and played with playsound==1.2.2. Needs network + the two
                optional deps. The "sounds better" opt-in.

WHY A DETACHED SUBPROCESS PER UTTERANCE, not an in-process call: the whole
reason OpenBolt's edge+playsound attempt kept throwing MCI error 263 is that
playsound/winmm wants its own process and message context; called from the
app's thread it fails silently. pyttsx3's runAndWait() would likewise block the
Textual event loop. So every utterance is a fire-and-forget child that owns its
own audio handle and dies when the clip ends — Popen returns immediately.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys

#: The engines the Voice settings tab offers. `pyttsx3` first = the default.
ENGINES = ("pyttsx3", "edge")
DEFAULT_EDGE_VOICE = "en-GB-SoniaNeural"

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _speaker() -> tuple[str, int]:
    """(interpreter, creationflags) for the speaking child. Prefer pythonw.exe:
    measured 2026-09-18 — a `python.exe` child was SILENT (with or without a
    console) while `pythonw.exe` (GUI subsystem) kept an audio session and was
    the only one audible. It also has no console, so no flag and no flash.
    Fall back to python.exe + CREATE_NO_WINDOW when pythonw is missing."""
    exe = sys.executable or "python"
    d, name = os.path.split(exe)
    if name.lower() in ("python.exe", "python"):
        cand = os.path.join(d, "pythonw.exe")
        if os.path.exists(cand):
            return cand, 0
    return exe, _NO_WINDOW


def _has(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def available_engines() -> list[str]:
    """The engines this install can actually run right now. pyttsx3 needs its
    package; edge needs BOTH edge_tts and playsound. The tab greys out what is
    missing rather than offering an engine that will fail silently in a child."""
    out: list[str] = []
    if _has("pyttsx3"):
        out.append("pyttsx3")
    if _has("edge_tts") and _has("playsound"):
        out.append("edge")
    return out


def list_sapi_voices() -> list[str]:
    """SAPI voice NAMES for the pyttsx3 picker (e.g. 'Microsoft Zira Desktop').
    Empty when pyttsx3 is absent — the caller shows 'system default'."""
    try:
        import pyttsx3

        eng = pyttsx3.init()
        names = [v.name for v in eng.getProperty("voices")]
        try:
            eng.stop()
        except Exception:
            pass
        return names
    except Exception:
        return []


def clean_for_speech(text: str, *, limit: int = 600) -> str:
    """Strip what should never be read aloud — fenced code, inline backticks,
    and long paths — then cap length. A spoken summary, not a transcript."""
    import re

    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code fences
    text = re.sub(r"`[^`]*`", " ", text)                # inline code
    text = re.sub(r"[A-Za-z]:\\[^\s]+|/[^\s]+/[^\s]+", " ", text)  # paths
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# The children. repr() of the args is injected verbatim, so a quote or newline
# in the text can never break out — repr escapes it.
_SAPI_CHILD = """\
import pyttsx3
e = pyttsx3.init()
v = {voice!r}
if v:
    for voice in e.getProperty('voices'):
        if voice.name == v:
            e.setProperty('voice', voice.id); break
e.say({text!r})
e.runAndWait()
"""

_EDGE_CHILD = """\
import asyncio, tempfile, time, pathlib, edge_tts
from playsound import playsound
async def go():
    o = pathlib.Path(tempfile.gettempdir()) / f'lt_tts_{{int(time.time()*1000)}}.mp3'
    await edge_tts.Communicate({text!r}, voice={voice!r}, rate='+0%').save(str(o))
    try:
        playsound(str(o), block=True)
    finally:
        o.unlink(missing_ok=True)
asyncio.run(go())
"""


def speak(text: str, *, engine: str = "pyttsx3", voice: str | None = None) -> bool:
    """Fire-and-forget one utterance in a detached child. Returns True if a
    child was launched, False if the text was empty or the engine unavailable.
    NEVER raises — a failed speak must not break a turn."""
    text = clean_for_speech(text)
    if not text:
        return False
    if engine == "edge":
        if not (_has("edge_tts") and _has("playsound")):
            return False
        child = _EDGE_CHILD.format(text=text, voice=voice or DEFAULT_EDGE_VOICE)
    else:
        if not _has("pyttsx3"):
            return False
        child = _SAPI_CHILD.format(text=text, voice=voice or "")
    py, flags = _speaker()
    try:
        subprocess.Popen([py, "-c", child], creationflags=flags)
        return True
    except Exception:
        return False


if __name__ == "__main__":  # ponytail self-check: no framework, one assert path
    assert "code" not in clean_for_speech("say this ```code``` and `x` done")
    assert clean_for_speech("a" * 999) == "a" * 600
    assert clean_for_speech("   ") == ""
    print("available engines:", available_engines())
    print("sapi voices:", list_sapi_voices())
    speak("Voice backend self test.", engine="pyttsx3")
    print("ok")
