"""The `studio` tool: local generation — images, audio, 3D — from the chat.

Three LOCAL apps, three transports, one tool:

  image  -> LiteImage's REST API   (env LITEIMAGE_API_URL, default :7426)
  sound  -> LiteSound's REST API   (env LITESOUND_API_URL, default :7427)
  model  -> the `lst run model` CLI (LiteModeler has no HTTP server)

image and sound talk to PORTS, so whichever copy is running answers — an end
user's installed app and the dev checkout are indistinguishable here, which
is the point. model delegates to litesuite-tools, which owns the only copy
of the find-the-CLI logic (env -> dev checkout -> installed app resources,
Electron-as-node when there is no system Node). Duplicating that chain here
would give it two owners, and the second copy always drifts.

Everything degrades to an honest sentence: app not running, lst not
installed, job still cooking. The model acts on those; a traceback it
cannot.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from litetui import seat_guard
from litetui import ttyguard
from litetui import tool_schemas

IMAGE_URL = os.environ.get("LITEIMAGE_API_URL", "http://127.0.0.1:7426").rstrip("/")
SOUND_URL = os.environ.get("LITESOUND_API_URL", "http://127.0.0.1:7427").rstrip("/")

#: Longest we wait per request. Image generation is synchronous at the API,
#: so it gets real time; sound returns a job id fast and is polled.
IMAGE_GEN_TIMEOUT = 300
QUICK_TIMEOUT = 10

_SOUND_MODES = ("music", "song", "sfx", "ambient")

STUDIO_TOOL_SPEC = tool_schemas.load("studio")


def _http(method: str, url: str, body: dict | None = None, timeout: int = QUICK_TIMEOUT) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8", errors="replace")
    try:
        return json.loads(text)
    except ValueError:
        return {"raw": text[:2000]}


def _fmt(result: dict) -> str:
    out = json.dumps(result, indent=2, ensure_ascii=False)
    if len(out) > 4000:
        out = out[:4000] + "\n… (truncated)"
    return out


def _not_running(app: str, url: str, err: Exception) -> str:
    return (
        f"[{app} not available] no answer at {url} ({err.__class__.__name__}). "
        f"The {app} desktop app must be RUNNING for this — ask the human to "
        f"open it, then retry."
    )


# -- image ---------------------------------------------------------------

#: tool arg name -> LiteImage API field. Omitted args fall back to the API's
#: own defaults — EXCEPT guidance_scale, which is pinned in generate below.
_IMAGE_PASSTHROUGH = {
    "width": "width",
    "height": "height",
    "steps": "steps",
    "guidance": "guidance_scale",
    "negative_prompt": "negative_prompt",
    "seed": "seed",
    "sampler": "sampling_method",
    "scheduler": "scheduler",
    "batch": "batch_count",
    "lora_dir": "lora_dir",
    "init_image_path": "init_image_path",
    "strength": "strength",
    "clip_skip": "clip_skip",
    "output_path": "output_path",
}


def _image(action: str, args: dict) -> str:
    try:
        if action == "status":
            return _fmt(_http("GET", f"{IMAGE_URL}/status", timeout=3))
        if action == "models":
            return _fmt(_http("GET", f"{IMAGE_URL}/models", timeout=30))
        if action == "config":
            return _fmt(_http("GET", f"{IMAGE_URL}/config", timeout=10))
        if action == "cancel":
            return _fmt(_http("POST", f"{IMAGE_URL}/cancel", body={}))
        if action == "load_model":
            file_path = (args.get("file_path") or "").strip()
            if not file_path:
                return ("Error: 'file_path' is required for image load_model. "
                        "Use action=models to list available checkpoints.")
            # Loading a big GGUF takes real time — same budget as generation.
            return _fmt(_http("POST", f"{IMAGE_URL}/load_model",
                              body={"file_path": file_path}, timeout=300))
        if action == "unload_model":
            return _fmt(_http("POST", f"{IMAGE_URL}/unload_model", body={}))
        if action == "generate":
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                return "Error: 'prompt' is required for image generate."
            body = {"prompt": prompt}
            for arg_key, api_key in _IMAGE_PASSTHROUGH.items():
                if args.get(arg_key) is not None:
                    body[api_key] = args[arg_key]
            # The API's own guidance default (7.5) does NOT match LiteImage's
            # UI default (3.5), and for flux1-dev the overdriven CFG produced
            # blurry/overprocessed results while the UI was fine with 3.5
            # (Ryan, 2026-08-29: "the issue is the settings you're using").
            # Pin the tool default to the UI value; an explicit `guidance` arg
            # overrides it for other model families.
            if "guidance_scale" not in body:
                body["guidance_scale"] = 3.5
            if args.get("save_to_gallery") is True:
                body["save_to_gallery"] = True
            return _fmt(_http("POST", f"{IMAGE_URL}/generate", body,
                              timeout=IMAGE_GEN_TIMEOUT))
        return (f"Error: unknown image action {action!r} "
                "(status, generate, models, load_model, unload_model, config, cancel)")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return _not_running("LiteImage", IMAGE_URL, e)


# -- sound ---------------------------------------------------------------

def _sound(action: str, args: dict) -> str:
    try:
        if action == "status":
            return _fmt(_http("GET", f"{SOUND_URL}/status", timeout=3))
        if action in ("jobs", "gallery", "backends"):
            return _fmt(_http("GET", f"{SOUND_URL}/{action}"))
        if action == "job":
            job_id = args.get("job_id")
            if not job_id:
                return "Error: 'job_id' is required for job."
            return _fmt(_http("GET", f"{SOUND_URL}/job/{job_id}"))
        if action == "cancel":
            job_id = args.get("job_id")
            if not job_id:
                return "Error: 'job_id' is required for cancel."
            return _fmt(_http("POST", f"{SOUND_URL}/job/{job_id}/cancel", body={}))
        if action == "generate":
            mode = (args.get("mode") or "").strip().lower()
            if mode not in _SOUND_MODES:
                return (f"Error: sound generate needs mode = "
                        f"{' | '.join(_SOUND_MODES)} (got {mode or 'nothing'!r})")
            if not (args.get("prompt") or args.get("lyrics")):
                return "Error: sound generate needs a 'prompt' and/or 'lyrics'."
            body = {}
            for key in ("prompt", "lyrics", "duration"):
                if args.get(key) is not None:
                    body[key] = args[key]
            result = _http("POST", f"{SOUND_URL}/generate/{mode}", body, timeout=30)
            return _fmt(result) + (
                "\n(async: poll with app=sound action=job job_id=<id> until done)"
                if isinstance(result, dict) and result.get("job_id") else ""
            )
        return (f"Error: unknown sound action {action!r} "
                f"(status, generate, job, jobs, cancel, gallery, backends)")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return _not_running("LiteSound", SOUND_URL, e)


_DONE_STATES = {"done", "completed", "complete", "succeeded", "failed",
                "error", "cancelled", "canceled"}


def _sound_wait(job_id: str, budget_s: float) -> dict:
    """Poll a sound job to completion. Used under suspension: the model that
    would normally poll is unloaded, so the TOOL owns the wait."""
    deadline = time.time() + budget_s
    last: dict = {"job_id": job_id, "state": "unknown"}
    while time.time() < deadline:
        try:
            last = _http("GET", f"{SOUND_URL}/job/{job_id}")
        except (urllib.error.URLError, OSError, TimeoutError):
            pass    # transient during heavy generation; keep waiting
        state = str(last.get("state", last.get("status", ""))).lower()
        if state in _DONE_STATES or last.get("file") or last.get("path"):
            return last
        time.sleep(3)
    last["_timeout"] = f"still running after {int(budget_s)}s"
    return last


# -- model ---------------------------------------------------------------

def _model(action: str, args: dict) -> str:
    lst = shutil.which("lst")
    if not lst:
        return (
            "[lst CLI not found] LiteModeler is driven through litesuite-tools "
            "(`lst run model ...`), which owns the find-the-CLI logic for dev "
            "checkouts AND installed copies. Install LiteSuite's tools, or ask "
            "the human to."
        )
    cmd = [lst, "run", "model", f"action={action}"]
    if args.get("prompt"):
        cmd.append(f"prompt={args['prompt']}")
    try:
        # Through the ttyguard envelope, like every child this app spawns —
        # a raw subprocess can steal or wreck the terminal this TUI lives in.
        # (The envelope sweep in test_ttyguard.py caught exactly this line
        # as a raw call. The gate works; obey it.)
        proc = ttyguard.run(cmd, timeout=600)
    except subprocess.TimeoutExpired:
        return "[model action timed out after 600s]"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out.strip() or f"(exit {proc.returncode}, no output)"
    return out[:4000]


# -- entry ---------------------------------------------------------------

#: Actions that light up the GPU. Everything else (status, job, gallery,
#: models, config...) is a lookup and never touches the seat. load_model
#: joins generate: loading a big GGUF needs the VRAM the seat occupies, so it
#: suspends first like any generation.
_GPU_ACTIONS = {"generate", "load_model"}


def _generate_suspended(app: str, action: str, args: dict, seat_model: str,
                        backend=None) -> str:
    """Suspend the agent's own model, generate, resume. The order of the
    finally matters more than anything else in this file: whatever the
    generation did — succeed, fail, raise — the seat comes back.

    The backend handle routes the verbs to the ENGINE that owns the seat —
    lms verbs against a llama-served model manage nothing (Sentinel's
    integration finding, 2026-08-21). An attached llama server refuses the
    suspend by name; the refusal is surfaced and generation proceeds
    unsuspended, which is exactly the pre-seat-guard behavior for a seat we
    cannot manage."""
    rec = seat_guard.record(seat_model, backend)
    if rec is None:
        # Seat not resident (already unloaded, or unmanageable on this
        # backend): nothing to suspend, just generate.
        return _dispatch(app, action, args)

    ok, why = seat_guard.safe_to_suspend(rec)
    if not ok:
        return (f"[not suspending the seat] {why} — another request may be "
                f"mid-stream on this model. Retry when it is idle.")

    err = seat_guard.suspend(rec, backend)
    if err:
        return f"[seat suspend failed] {err} — generation not started."

    note = (f"\n[seat {rec['identifier']} was suspended for this generation "
            f"and restored at ctx {rec.get('context')} — your first reply "
            f"re-reads the conversation, expect a slow first token]")
    try:
        if app == "sound":
            # Submit, then wait INSIDE the tool: the poller is unloaded.
            out = _sound(action, args)
            job_id = None
            try:
                job_id = json.loads(out.split("\n(async")[0]).get("job_id")
            except ValueError:
                pass
            if job_id:
                budget = float(args.get("duration") or 60) * 3 + 180
                final = _sound_wait(str(job_id), budget)
                out = _fmt(final)
        else:
            out = _dispatch(app, action, args)
    finally:
        resume_err = seat_guard.resume(rec, backend)
    if resume_err:
        return out + f"\n\n[SEAT RESUME PROBLEM] {resume_err}"
    return out + note


def _dispatch(app: str, action: str, args: dict) -> str:
    if app == "image":
        return _image(action, args)
    if app == "sound":
        return _sound(action, args)
    if app == "model":
        return _model(action, args)
    return f"Error: unknown app {app!r} — image, sound, or model."


def run(args: dict, seat_model: str | None = None, backend=None) -> str:
    app = (args.get("app") or "").strip().lower()
    action = (args.get("action") or "").strip().lower()
    if seat_model and app in ("image", "sound", "model") and action in _GPU_ACTIONS:
        return _generate_suspended(app, action, args, seat_model, backend)
    return _dispatch(app, action, args)
