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
import urllib.error
import urllib.request

import ttyguard

IMAGE_URL = os.environ.get("LITEIMAGE_API_URL", "http://127.0.0.1:7426").rstrip("/")
SOUND_URL = os.environ.get("LITESOUND_API_URL", "http://127.0.0.1:7427").rstrip("/")

#: Longest we wait per request. Image generation is synchronous at the API,
#: so it gets real time; sound returns a job id fast and is polled.
IMAGE_GEN_TIMEOUT = 300
QUICK_TIMEOUT = 10

_SOUND_MODES = ("music", "song", "sfx", "ambient")

STUDIO_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "studio",
        "description": (
            "Generate media with the LOCAL Lite studio apps — no API keys, no "
            "cloud, runs on this machine's GPU.\n"
            "app=image (LiteImage): actions status | generate (prompt, "
            "width/height/steps optional) | models | cancel. Generation is "
            "synchronous — the reply carries the saved file path.\n"
            "app=sound (LiteSound): actions status | generate (mode = music|"
            "song|sfx|ambient; prompt and/or lyrics; duration seconds) | "
            "job (job_id) | jobs | cancel (job_id) | gallery | backends. "
            "Generation is ASYNC: generate returns a job_id — poll with "
            "action=job until it reports done, then the file path is in the "
            "result. Do not spam polls; every few seconds is plenty.\n"
            "app=model (LiteModeler, prompt-to-3D): actions status | config | "
            "generate (prompt) | families — delegated to the `lst` CLI.\n"
            "image and sound need their app RUNNING (status says). If one is "
            "not running, tell the human to open it — do not retry blind."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "app": {"type": "string", "enum": ["image", "sound", "model"]},
                "action": {"type": "string"},
                "prompt": {"type": "string"},
                "mode": {"type": "string", "description": "sound: music|song|sfx|ambient"},
                "lyrics": {"type": "string", "description": "sound: song lyrics"},
                "duration": {"type": "number", "description": "sound: seconds"},
                "job_id": {"type": "string", "description": "sound: job to poll/cancel"},
                "width": {"type": "number"},
                "height": {"type": "number"},
                "steps": {"type": "number"},
            },
            "required": ["app", "action"],
        },
    },
}


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

def _image(action: str, args: dict) -> str:
    try:
        if action == "status":
            return _fmt(_http("GET", f"{IMAGE_URL}/status", timeout=3))
        if action == "models":
            return _fmt(_http("GET", f"{IMAGE_URL}/models", timeout=30))
        if action == "cancel":
            return _fmt(_http("POST", f"{IMAGE_URL}/cancel", body={}))
        if action == "generate":
            prompt = (args.get("prompt") or "").strip()
            if not prompt:
                return "Error: 'prompt' is required for image generate."
            body = {"prompt": prompt}
            for key in ("width", "height", "steps"):
                if args.get(key) is not None:
                    body[key] = args[key]
            return _fmt(_http("POST", f"{IMAGE_URL}/generate", body,
                              timeout=IMAGE_GEN_TIMEOUT))
        return f"Error: unknown image action {action!r} (status, generate, models, cancel)"
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

def run(args: dict) -> str:
    app = (args.get("app") or "").strip().lower()
    action = (args.get("action") or "").strip().lower()
    if app == "image":
        return _image(action, args)
    if app == "sound":
        return _sound(action, args)
    if app == "model":
        return _model(action, args)
    return f"Error: unknown app {app!r} — image, sound, or model."
