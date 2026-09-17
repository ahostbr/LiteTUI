"""Suspend the agent's own model around GPU generation, then put it back.

The situation this solves is self-referential: the agent IS a ~29 GB
resident in LM Studio, and the generation it just ordered needs that VRAM.
Loading beside it near-OOMed this workstation once (2026-08-21) — weights
arithmetic said "fits", the KV + compute buffers said otherwise.

Why suspending mid-turn is SAFE: an LM Studio model is only busy while a
completion request is in flight. Tool execution happens BETWEEN completions
— the turn is parked waiting for the tool result — so the tool may unload
the very model that called it, do the generation, reload, and the next
completion resumes from the transcript LiteTUI holds. The conversation
lives in the app, not the model.

What must survive is the LOAD CONFIG: identifier, context length, parallel.
`lms ps --json` reports all three, plus the safety gate: `status` and
`queued` — never unload a model that is serving or has queued requests,
because parallel=4 means OTHER seats may be mid-stream on it.

What cannot survive is the KV cache: the first completion after a resume
re-ingests the whole prompt once. Slow first token, then normal. That is
the price of the VRAM, and it is honest to say so rather than pretend.

Deliberately NOT preserved: TTL. Reloading with a TTL would arm an
auto-unload on the human's seat that they never chose.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time

from litetui import paths, ttyguard

#: Where the recovery breadcrumb lands if a resume fails: the exact reload
#: command, on disk, findable by a human whose agent has gone quiet.
BREADCRUMB = paths.data_root() / "suspended_seat.json"

RESUME_RETRIES = 3
LOAD_TIMEOUT = 180


def _lms() -> str | None:
    return shutil.which("lms")


def _ps() -> list[dict]:
    lms = _lms()
    if not lms:
        return []
    try:
        proc = ttyguard.run([lms, "ps", "--json"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    try:
        rows = json.loads(proc.stdout or "[]")
    except ValueError:
        return []
    return rows if isinstance(rows, list) else []


def record(model_id: str, backend=None) -> dict | None:
    """The seat's live load config, or None when it is not loaded at all.

    With a backend handle the snapshot comes from THAT engine — the llama
    router's /models, or LM Studio's `lms ps`. The legacy no-backend path
    stays: the shipped call sites all pass one now, but the module must keep
    working standalone (its tests, and any older caller)."""
    if backend is not None:
        return backend.seat_snapshot(model_id)
    for row in _ps():
        if row.get("identifier") == model_id:
            return {
                "identifier": row.get("identifier"),
                "context": row.get("contextLength"),
                "parallel": row.get("parallel"),
                "status": row.get("status"),
                "queued": row.get("queued", 0),
            }
    return None


def safe_to_suspend(rec: dict) -> tuple[bool, str]:
    """Only an idle seat with an empty queue may be taken down.

    parallel > 1 means this model may be serving seats that are not us;
    unloading mid-stream kills THEIR turn, not ours.
    """
    if rec.get("status") != "idle":
        return False, f"model is {rec.get('status')!r}, not idle"
    if rec.get("queued"):
        return False, f"{rec['queued']} request(s) queued on the model"
    return True, ""


def reload_command(rec: dict) -> list[str]:
    """The exact argv that restores the seat as recorded."""
    cmd = ["lms", "load", str(rec["identifier"]), "--gpu", "max", "-y"]
    if rec.get("context"):
        cmd[3:3] = ["-c", str(rec["context"])]
    if rec.get("parallel"):
        cmd += ["--parallel", str(rec["parallel"])]
    return cmd


def suspend(rec: dict, backend=None) -> str | None:
    """Unload the seat. None on success, else an error sentence.

    An attached llama server refuses here by NAME (LiteSuite owns it) —
    the caller surfaces the refusal and generates without suspending only
    if it dares; that decision is not this module's."""
    if backend is not None:
        err = backend.seat_suspend(rec)
        if err:
            return err
        if backend.seat_snapshot(rec["identifier"]) is not None:
            return "unload reported success but the model is still loaded"
        _write_breadcrumb(rec, backend)
        return None
    lms = _lms()
    if not lms:
        return "lms CLI not found — cannot manage the seat"
    try:
        ttyguard.run([lms, "unload", str(rec["identifier"])], timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"unload failed: {e.__class__.__name__}"
    if record(rec["identifier"]) is not None:
        return "unload reported success but the model is still loaded"
    _write_breadcrumb(rec, None)
    return None


def _write_breadcrumb(rec: dict, backend) -> None:
    """The recovery note a human finds when a resume failed and the agent's
    own brain is missing. The reload instruction must match the ENGINE the
    seat lives on — an `lms load` line for a llama-served seat restores
    nothing."""
    # 🔴 ASK THE BACKEND FIRST (T806). This branched on `llamacpp` and sent
    # everything else to LM Studio's CLI — so an NInfer seat was handed an
    # `lms load` line for a program that has never heard of a `.ninfer`
    # artifact. A FALLBACK IS A DECISION ABOUT EVERY BACKEND THAT DOES NOT HAVE
    # A BRANCH, INCLUDING THE ONES THAT DO NOT EXIST YET.
    #
    # ⬜ Duck-typed: a backend that does not answer keeps the behaviour it had.
    own_hint = getattr(backend, "reload_hint", None) if backend is not None else None
    if callable(own_hint):
        reload_hint = own_hint(rec)
    elif backend is not None and getattr(backend, "name", "") == "llamacpp":
        reload_hint = (f"/load {rec['identifier']} in LiteTUI "
                       f"(or POST /models/load to {backend.host()})")
    else:
        reload_hint = " ".join(reload_command(rec))
    BREADCRUMB.write_text(json.dumps({
        "recorded": rec,
        "reload": reload_hint,
        "note": "seat suspended for a studio generation; resume should have "
                "cleared this file — if it is still here, run the reload "
                "command above.",
    }, indent=2), encoding="utf-8")


def resume(rec: dict, backend=None) -> str | None:
    """Reload the seat exactly as recorded. None on success.

    Retries, then leaves the breadcrumb in place: a failed resume means the
    agent's own brain is missing — the error text is for the TRANSCRIPT and
    the human, because the model that would normally read it is the thing
    that is gone.
    """
    if backend is not None:
        last = ""
        for attempt in range(1, RESUME_RETRIES + 1):
            last = backend.seat_resume(rec) or ""
            if not last:
                BREADCRUMB.unlink(missing_ok=True)
                return None
            if "instead of" in last:
                # Loaded, but at the WRONG context — say so; a silently
                # shrunken window fails much later. Not retryable.
                BREADCRUMB.unlink(missing_ok=True)
                return last + " — check the model's load settings"
            time.sleep(2 * attempt)
        return (
            f"SEAT RESUME FAILED after {RESUME_RETRIES} attempts ({last}). "
            f"The agent's model is NOT loaded. Restore it with /load "
            f"{rec['identifier']} (also recorded in {BREADCRUMB})"
        )
    lms = _lms()
    if not lms:
        return "lms CLI not found"
    cmd = [lms] + reload_command(rec)[1:]
    last = ""
    for attempt in range(1, RESUME_RETRIES + 1):
        try:
            ttyguard.run(cmd, timeout=LOAD_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            last = e.__class__.__name__
            time.sleep(2 * attempt)
            continue
        back = record(rec["identifier"])
        if back is not None:
            if rec.get("context") and back.get("context") != rec.get("context"):
                # Loaded, but at the WRONG context — the JIT-default trap.
                # Say so; a silently shrunken window fails much later.
                BREADCRUMB.unlink(missing_ok=True)
                return (f"seat reloaded but at context {back.get('context')} "
                        f"instead of {rec['context']} — check LM Studio")
            BREADCRUMB.unlink(missing_ok=True)
            return None
        last = "load reported success but the model is not listed"
        time.sleep(2 * attempt)
    return (
        f"SEAT RESUME FAILED after {RESUME_RETRIES} attempts ({last}). "
        f"The agent's model is NOT loaded. Restore it with:\n  "
        + " ".join(reload_command(rec))
        + f"\n(also recorded in {BREADCRUMB})"
    )
