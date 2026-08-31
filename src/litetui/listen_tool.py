"""The `listen` tool: audio perception via Qwen2-Audio-7B, local only.

Ears for the agent. The model (mradermacher/Qwen2-Audio-7B-Instruct GGUF +
mmproj) lives in LM Studio's models dir but is served by a STANDALONE
llama-server on port 8090 — not by LM Studio itself, because LM Studio
0.4.21 hard-rejects audio content parts at the schema level (measured
2026-08-30: HTTP 400 "content objects must have a 'type' field that is
either 'text' or 'image_url'"). llama.cpp's OpenAI endpoint accepts them;
that is the path Qwen2-Audio's model card documents.

Full seat swap per listen — the studio-tool pattern, because the agent's own
model and Qwen2-Audio cannot share VRAM (measured: they evict each other).
The order of the finally matters more than anything else in this file:
whatever the listen did — succeed, fail, raise — the audio server goes and
the seat comes back.

Reliability facts measured 2026-08-30 that shape the contract:
  * speech transcription is excellent (edge-tts clip came back word-for-word)
  * exact numbers are FABRICATED (a pure 440Hz sine was reported as "60 bpm",
    then "120 bpm" on re-run) — the model is an understanding model, not a
    spectrum analyzer; the schema says so, and the agent should use ffmpeg
    for precise frequency work instead of asking
  * silence is HALLUCINATED (room tone at -74.7 dB peak came back as "an
    ambient pop track in Bb minor at 103.36 bpm") — the model never says "I
    hear nothing", so a volumedetect guard bails BEFORE any seat swap when
    the peak is below SILENCE_DB

Everything degrades to an honest sentence: missing binary, silent input,
seat busy, server died with its log tail. The model acts on those; a
traceback it cannot.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

from litetui import paths
from litetui import seat_guard
from litetui import tool_schemas
from litetui import ttyguard

LISTEN_TOOL_SPEC = tool_schemas.load("listen")

#: Qwen2-Audio GGUFs — LM Studio downloaded them; llama-server reads the same
#: files. The mmproj IS the audio encoder (1.2 GB f16).
MODEL_DIR = Path(os.environ.get(
    "LMSTUDIO_MODELS", r"C:\Users\Ryan\.lmstudio\models")) \
    / "mradermacher" / "Qwen2-Audio-7B-Instruct-GGUF"
GGUF = MODEL_DIR / "Qwen2-Audio-7B-Instruct.Q4_K_M.gguf"
MMPROJ = MODEL_DIR / "Qwen2-Audio-7B-Instruct.mmproj-f16.gguf"

AUDIO_PORT = 8090                        # spare port; LM Studio is 1234
API = f"http://127.0.0.1:{AUDIO_PORT}/v1/chat/completions"
HEALTH = f"http://127.0.0.1:{AUDIO_PORT}/health"

#: Working files (normalized wav, server log) — the designated junk dir.
WORKDIR = Path(paths.ROOT) / "temp-working-dir"
SERVER_LOG = WORKDIR / "llama_server.log"

AUDIO_CTX = 8192                          # Qwen2-Audio maxContextLength (measured)
START_TIMEOUT = 240   # seconds to wait for llama-server to come up
INFER_TIMEOUT = 300   # seconds for one completion (short audio, big margin)

#: Below this peak level the wav is digital silence / room tone and Qwen2-
#: Audio will hallucinate a full description of it (measured: -74.7 dB room
#: tone came back as "ambient pop track in Bb minor at 103.36 bpm"). Bail
#: before touching the seat instead of letting that reach the agent.
SILENCE_DB = -60.0

DEFAULT_QUESTION = ("Describe exactly what you hear in this audio. Be specific "
                    "about sounds, speech content, and anything notable.")


# -- helpers ---------------------------------------------------------------

def _run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    """Short-lived child under the ttyguard envelope.

    The first build (2026-08-30) died in a reader thread: text=True decodes
    with cp1252 and `lms load` emits a 0x8f byte mid-progress-bar. UTF-8 +
    replace is honest about what we cannot decode instead of raising, and
    the defaults of ttyguard.popen are exactly that (encoding="utf-8",
    errors="replace"), so the envelope adds DEVNULL stdin, no console window
    and the terminal repair on top of the fix. Timeout keeps the semantics
    of run(): kill the child, then re-raise."""
    proc = ttyguard.popen(cmd, stdin=subprocess.DEVNULL)
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def _llama_server() -> str | None:
    """The llama-server binary: PATH first (post winget-update), then the
    winget package dir by prefix so a version bump does not break us."""
    p = shutil.which("llama-server")
    if p:
        return p
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        for d in sorted(Path(local).glob(
                "Microsoft/WinGet/Packages/ggml.llamacpp_*")):
            exe = d / "llama-server.exe"
            if exe.exists():
                return str(exe)
    return None


def _ffmpeg() -> str | None:
    return shutil.which("ffmpeg")


def _log_tail(n: int = 8) -> str:
    try:
        lines = SERVER_LOG.read_text(encoding="utf-8", errors="replace") \
                             .strip().splitlines()[-n:]
        return " | ".join(l for l in lines if l.strip())
    except OSError:
        return "(no server log)"


# -- audio acquisition ------------------------------------------------------

def _first_dshow_device() -> str | None:
    """First Direct Show audio input via `ffmpeg -f dshow -list_devices`.

    Plain `-i dummy` without -list_devices is rejected by this ffmpeg build
    ("Malformed dshow input string", measured 2026-08-30) — the list form
    prints lines like: "Microphone (ZTD39 Device)" (audio)."""
    ff = _ffmpeg()
    if not ff:
        return None
    try:
        proc = _run([ff, "-hide_banner", "-f", "dshow",
                     "-list_devices", "1", "-i", "dummy"], 15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r'"([^"]+)"\s*\(audio\)', proc.stderr or "")
    return m.group(1) if m else None


def _get_wav(args: dict) -> tuple[Path | None, str]:
    """Normalize the chosen source to a 16 kHz mono WAV (Qwen2-Audio's
    processor rate). Returns (path, "") or (None, error sentence)."""
    ff = _ffmpeg()
    if not ff:
        return None, ("[listen] ffmpeg not found on PATH — cannot normalize "
                      "audio. Install it and retry.")

    WORKDIR.mkdir(parents=True, exist_ok=True)
    out = WORKDIR / "listen_input.wav"

    if args.get("path"):
        src = Path(str(args["path"]))
        if not src.exists():
            return None, f"[listen] file not found: {src}"
        cmd = [ff, "-y", "-i", str(src), "-ar", "16000", "-ac", "1",
               "-c:a", "pcm_s16le", str(out)]
    elif args.get("seconds"):
        device = (args.get("device") or "").strip() or _first_dshow_device()
        if not device:
            return None, ("[listen] no microphone found — pass `device` with a "
                          "Direct Show name (`ffmpeg -f dshow -list_devices 1 "
                          "-i dummy` lists them).")
        cmd = [ff, "-y", "-f", "dshow", "-i", f"audio={device}",
               "-t", str(args["seconds"]), "-ar", "16000", "-ac", "1",
               "-c:a", "pcm_s16le", str(out)]
    else:
        return None, "[listen] give `path` (action=file) or `seconds` (action=record)."

    try:
        proc = _run(cmd, 300)
    except subprocess.TimeoutExpired:
        return None, "[listen] ffmpeg timed out after 300s."
    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 1000:
        tail = (proc.stderr or "").strip().splitlines()[-2:]
        return None, f"[listen] capture failed ({proc.returncode}): {' | '.join(tail)}"

    secs = out.stat().st_size / (16000 * 2)
    print(f"[listen] wav ready: {out} (~{secs:.1f}s, 16 kHz mono)")
    return out, ""


def _check_not_silent(wav: Path) -> str | None:
    """volumedetect on the normalized wav; an error sentence when it is silence.

    Runs BEFORE any seat swap — a silent input costs nothing to reject and
    would otherwise cost a full suspend/start/infer/resume cycle plus a
    hallucinated answer (the model never says "I hear nothing")."""
    ff = _ffmpeg()
    if not ff:
        return None    # cannot measure — let the model have its say
    try:
        proc = _run([ff, "-hide_banner", "-i", str(wav), "-af", "volumedetect",
                     "-f", "null", "-"], 60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"max_volume:\s*(-?\d+\.?\d*)\s*dB", proc.stderr or "")
    if not m:
        return None
    peak = float(m.group(1))
    print(f"[listen] peak level {peak:.1f} dB")
    if peak < SILENCE_DB:
        return (f"[listen] input is digital silence (peak {peak:.1f} dB < "
                f"{SILENCE_DB} dB) — nothing to hear. No seat swap performed.")
    return None


# -- the audio server -------------------------------------------------------

def _start_server() -> tuple[subprocess.Popen | None, str]:
    """Launch llama-server with the Qwen2-Audio GGUF + mmproj on AUDIO_PORT."""
    exe = _llama_server()
    if not exe:
        return None, ("[listen] llama-server not found — `winget install "
                      "ggml.llamacpp` (or put it on PATH).")
    for p in (GGUF, MMPROJ):
        if not p.exists():
            return None, f"[listen] missing model file: {p}"

    cmd = [exe, "-m", str(GGUF), "--mmproj", str(MMPROJ),
           "-c", str(AUDIO_CTX), "--host", "127.0.0.1",
           "--port", str(AUDIO_PORT)]
    print(f"[listen] starting llama-server on port {AUDIO_PORT} ...")
    WORKDIR.mkdir(parents=True, exist_ok=True)
    logf = open(SERVER_LOG, "w", encoding="utf-8")
    t0 = time.time()
    try:
        # Envelope: DEVNULL stdin (a server reading the inherited TUI stdin is
        # exactly the incident class ttyguard exists to kill), no console window.
        # logf is an opened file, so popen's text-mode encoding does not apply.
        proc = ttyguard.popen(cmd, stdin=subprocess.DEVNULL,
                              stdout=logf, stderr=subprocess.STDOUT)
    except OSError as e:
        logf.close()
        return None, f"[listen] could not start llama-server: {e}"

    deadline = t0 + START_TIMEOUT
    while time.time() < deadline:
        if proc.poll() is not None:
            logf.close()
            return None, (f"[listen] llama-server exited early "
                          f"(code {proc.returncode}): {_log_tail()}")
        try:
            with urllib.request.urlopen(HEALTH, timeout=3) as resp:
                if resp.status == 200:
                    print(f"[listen] audio server up in {time.time()-t0:.0f}s")
                    return proc, ""
        except (urllib.error.URLError, OSError):
            pass    # not listening yet; keep waiting
        time.sleep(2)

    try:
        proc.terminate()
    except OSError:
        pass
    logf.close()
    return None, (f"[listen] llama-server did not become healthy within "
                  f"{START_TIMEOUT}s: {_log_tail()}")


def _stop_server(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass
    print("[listen] audio server stopped")


# -- the question -----------------------------------------------------------

def _ask(wav: Path, question: str) -> tuple[str | None, str]:
    b64 = base64.b64encode(wav.read_bytes()).decode("ascii")
    body = {
        "model": "qwen2-audio",   # llama-server ignores the name; one model
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "input_audio",
                 "input_audio": {"data": b64, "format": "wav"}},
            ],
        }],
        "temperature": 0.2,
    }
    req = urllib.request.Request(
        API, data=json.dumps(body).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=INFER_TIMEOUT) as resp:
            text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        # The body is the whole game — it names what the server rejected.
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:2000]
        except OSError:
            pass
        return None, (f"[listen] HTTP {e.code} from llama-server: "
                      f"{detail or '(empty body)'}\n[server log tail] {_log_tail()}")
    except (urllib.error.URLError, OSError) as e:
        return None, (f"[listen] no answer from the audio server "
                      f"({e.__class__.__name__}): {_log_tail()}")
    try:
        data = json.loads(text)
        answer = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, f"[listen] unexpected API response shape: {text[:500]}"
    print(f"[listen] answered in {time.time()-t0:.0f}s")
    return answer, ""


# -- status -----------------------------------------------------------------

def _status(seat_model: str | None) -> str:
    """Readiness report — cheap, never touches the seat."""
    lines = ["[listen status]"]
    exe = _llama_server()
    lines.append(f"  llama-server : {'found ' + exe if exe else 'NOT FOUND (winget install ggml.llamacpp)'}")
    for label, p in (("gguf", GGUF), ("mmproj", MMPROJ)):
        size = f"{p.stat().st_size/1e6:.0f} MB" if p.exists() else "MISSING"
        lines.append(f"  {label:<12}: {size}")
    ff = _ffmpeg()
    lines.append(f"  ffmpeg     : {'found' if ff else 'NOT FOUND'}")
    if seat_model:
        rec = seat_guard.record(seat_model)
        if rec is None:
            lines.append(f"  your seat    : {seat_model} not loaded in LM Studio")
        else:
            ok, why = seat_guard.safe_to_suspend(rec)
            state = "idle — a listen can proceed" if ok else f"busy ({why})"
            lines.append(f"  your seat    : {rec.get('status')} at ctx "
                         f"{rec.get('context')} — {state}")
    return "\n".join(lines)


# -- entry ------------------------------------------------------------------

def run(args: dict, seat_model: str | None = None, backend=None) -> str:
    action = (args.get("action") or "").strip().lower()
    if action == "status":
        return _status(seat_model)
    if action not in ("file", "record"):
        return "[listen] unknown action — use status, file, or record."

    wav, err = _get_wav(args)
    if err:
        return err
    assert wav is not None

    silent = _check_not_silent(wav)
    if silent:
        return silent

    # Gate + suspend the agent's own seat. The VRAM conflict is with whatever
    # engine serves that seat — the backend handle routes the verbs there,
    # exactly like studio_tool does (lms verbs against a llama-served model
    # manage nothing).
    if not seat_model:
        return ("[listen] cannot determine my own model identifier — refusing "
                "to swap seats blind.")
    rec = seat_guard.record(seat_model, backend)
    if rec is None:
        return (f"[listen] seat {seat_model!r} not found in the engine's "
                f"model list — refusing to touch anything. Is it loaded?")
    ok, why = seat_guard.safe_to_suspend(rec)
    if not ok:
        return (f"[listen] not suspending the seat: {why}. Retry when it is "
                f"idle.")

    err = seat_guard.suspend(rec, backend)
    if err:
        return f"[listen] seat suspend failed: {err} — no listen started."
    print(f"[listen] seat {seat_model} suspended (ctx {rec.get('context')})")

    server: subprocess.Popen | None = None
    try:
        server, serr = _start_server()
        if serr:
            return serr
        answer, aerr = _ask(wav, (args.get("question") or "").strip()
                            or DEFAULT_QUESTION)
    finally:
        # Whatever happened above — heard it, API error, timeout — the audio
        # server goes and the agent's brain comes back.
        _stop_server(server)
        resume_err = seat_guard.resume(rec, backend)

    if aerr:
        return aerr + (f"\n[listen] SEAT RESUME PROBLEM: {resume_err}"
                       if resume_err else "")
    if resume_err:
        return (f"=== WHAT I HEARD ===\n{answer.strip()}\n\n"
                f"[listen] SEAT RESUME PROBLEM: {resume_err}")

    note = ("\n[seat was suspended for this listen and restored — your first "
            "reply re-reads the conversation, expect a slow first token]")
    return f"=== WHAT I HEARD ===\n{answer.strip()}{note}"
