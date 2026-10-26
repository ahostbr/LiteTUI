"""The `studio` tool: local generation — images, video, face swap, the H3 Director,
audio, 3D — from the chat.

Four LOCAL apps, three transports, one tool:

  image     -> LiteImage's REST API   (env LITEIMAGE_API_URL, default :7426)
  director  -> the same API's /director routes (LiteImage's H3 Director)
  sound     -> LiteSound's REST API   (env LITESOUND_API_URL, default :7427)
  model     -> the `lst run model` CLI (LiteModeler has no HTTP server)

image and sound talk to PORTS, so whichever copy is running answers — an end
user's installed app and the dev checkout are indistinguishable here, which
is the point. model delegates to litesuite-tools, which owns the only copy
of the find-the-CLI logic (env -> dev checkout -> installed app resources,
Electron-as-node when there is no system Node). Duplicating that chain here
would give it two owners, and the second copy always drifts.

THE SEAT (T0702). Every action that puts work on the GPU suspends the agent's
own model first (`seat_guard`, routed to the engine that serves it), runs the
job TO COMPLETION — an async job is waited on inside the call, because the
model that would poll it is the thing that was unloaded — asks the app to give
its own GPU memory back, and only then reloads the seat. Ryan 2026-10-26: the
studio tool must "correctly unload the local model from vram during
generation", and after the job the seat gets its VRAM back before LiteImage's
resident processes can keep it.

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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from litetui import runtime_log
from litetui import seat_guard
from litetui import ttyguard
from litetui import tool_schemas

IMAGE_URL = os.environ.get("LITEIMAGE_API_URL", "http://127.0.0.1:7426").rstrip("/")
SOUND_URL = os.environ.get("LITESOUND_API_URL", "http://127.0.0.1:7427").rstrip("/")

#: Longest we wait on one synchronous generation request (generate, inpaint,
#: ControlNet, upscale, face swap). LiteImage's own queue gives up at 5 minutes;
#: inpaint, ControlNet and upscale have no limit of their own.
IMAGE_GEN_TIMEOUT = 900
QUICK_TIMEOUT = 10
#: An async job the tool owns (its seat is suspended) is polled this often. Under
#: the ~5 s the face-swap sidecar waits before exiting once idle, so a finished
#: video swap is read before its status route disappears with the process.
POLL_S = 2.0
#: Default longest wait for one async job while the seat is suspended. A job
#: still running after it is CANCELLED before the seat comes back: reloading the
#: seat beside a live GPU job is the collision this tool exists to prevent.
#: `wait_s` raises it per call (a long H3 render).
JOB_WAIT_S = 4 * 3600

_SOUND_MODES = ("music", "song", "sfx", "ambient", "speech")

STUDIO_TOOL_SPEC = tool_schemas.load("studio")


def _http(method: str, url: str, body: dict | None = None, timeout: int = QUICK_TIMEOUT) -> dict:
    """One JSON request. An HTTP error with a body is an ANSWER, not an outage.

    🔴 `HTTPError` IS A `URLError`. Every 4xx/5xx the apps return — a missing
    field, the licence, an engine that is not provisioned, a failed generation —
    used to fall into the callers' `URLError` arm and be reported as "the app
    must be RUNNING", which sent the model to tell the human to open an app that
    was open. The body is returned instead, with the status beside it.
    """
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            text = resp.read().decode("utf-8", errors="replace")
            status = None
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", errors="replace") if e.fp else ""
        status = e.code
    try:
        out = json.loads(text)
    except ValueError:
        out = {"raw": text[:2000]}
    if not isinstance(out, dict):
        out = {"value": out}
    if status is not None:
        out["http_status"] = status
    return out


#: Response fields that carry whole files as base64. Shown to the model as a
#: size, never inline: one image is ~1 MB of text the model cannot look at, and
#: it pushed the paths it CAN use out of the truncated reply.
_BULKY = ("imageBase64", "result_b64", "png")


def _slim(value):
    if isinstance(value, dict):
        return {k: (f"<{len(v)} chars of base64 omitted>" if k in _BULKY and isinstance(v, str) else _slim(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [_slim(v) for v in value]
    return value


def _fmt(result: dict) -> str:
    out = json.dumps(_slim(result), indent=2, ensure_ascii=False)
    if len(out) > 4000:
        out = out[:4000] + "\n… (truncated)"
    return out


def _not_running(app: str, url: str, err: Exception) -> str:
    # T137: no URL or class name in the line a human reads — which address was
    # tried and what raised go to the error sink.
    runtime_log.record_error(
        "studio_app_not_running",
        detail=f"{app} at {url}: {type(err).__name__}: {err}",
        exc=err,
    )
    return (
        f"[{app} not available] The {app} desktop app must be RUNNING for this — "
        f"ask the human to open it, then retry."
    )


def _params(args: dict) -> dict:
    """The raw request fields the caller passed in `params` (they win over shorthand args)."""
    p = args.get("params")
    return dict(p) if isinstance(p, dict) else {}


# -- async jobs ----------------------------------------------------------

@dataclass(frozen=True)
class _Job:
    """How one kind of async job is submitted, read and stopped."""
    submit: str                              # POST path
    status: str                              # GET path, with {id}
    cancel: str                              # POST path, with {id}
    job_id: Callable[[dict], object]         # from the submit reply
    state: Callable[[dict], object]          # from a status reply
    done: frozenset

    def poll(self, base: str, job_id: str) -> dict:
        return _http("GET", base + self.status.format(id=urllib.parse.quote(str(job_id), safe="")))


def _top(key):
    return lambda r: r.get(key)


def _inner(outer, key):
    return lambda r: (r.get(outer) or {}).get(key) if isinstance(r.get(outer), dict) else None


_TRACKER_DONE = frozenset({"complete", "error", "cancelled"})

#: LiteImage's async routes (api-server.ts, ultratex-routes.ts). Their state field
#: and terminal words differ per route; each is read from its own emitter.
_IMAGE_JOBS = {
    "video": _Job("/video/generate", "/video/status/{id}", "/video/cancel/{id}",
                  _top("job_id"), _top("status"), _TRACKER_DONE),
    "pipeline": _Job("/pipeline", "/pipeline/status/{id}", "/pipeline/cancel/{id}",
                     _top("job_id"), _top("status"), _TRACKER_DONE),
    "pipeline_batch": _Job("/pipeline/batch", "/pipeline/status/{id}", "/pipeline/cancel/{id}",
                           _top("job_id"), _top("status"), _TRACKER_DONE),
    # The sidecar's own words (video_pipeline.py): done, not complete.
    "faceswap_video": _Job("/faceswap/video", "/faceswap/video/{id}", "/faceswap/video/{id}/cancel",
                           _top("job_id"), _top("status"), frozenset({"done", "error", "cancelled"})),
    "video_upscale": _Job("/video/upscale", "/video/upscale/{id}", "/video/upscale/{id}/cancel",
                          _inner("job", "id"), _inner("job", "status"), _TRACKER_DONE),
    # UltraTex spells its ends differently (ultratex.ts): completed, failed, canceled.
    "ultratex": _Job("/ultratex/jobs", "/ultratex/jobs/{id}", "/ultratex/jobs/{id}/cancel",
                     _inner("job", "id"), _inner("job", "status"),
                     frozenset({"completed", "failed", "canceled"})),
}

#: A Director render (director-routes.ts): the job is `value`, its state is `stage`.
_DIRECTOR_JOB = _Job("/director/request", "/director/jobs/{id}", "/director/jobs/{id}/cancel",
                     _inner("value", "id"), _inner("value", "stage"),
                     frozenset({"complete", "error", "cancelled", "interrupted"}))

#: LiteSound (job-tracker.ts): submit path depends on the mode, so it is built at the call.
_SOUND_JOB = _Job("", "/job/{id}", "/job/{id}/cancel", _top("job_id"), _top("status"), _TRACKER_DONE)


def _wait(job: _Job, base: str, job_id: str, budget_s: float, cancel_body: dict | None = None) -> dict:
    """Poll a job to an end state. Used under suspension: the model that would
    normally poll is unloaded, so the TOOL owns the wait. Past the budget the
    job is cancelled — the seat must not come back beside a live GPU job."""
    deadline = time.time() + budget_s
    last: dict = {"job_id": job_id, "status": "unknown"}
    refused = unreachable = 0
    while True:
        try:
            got = job.poll(base, job_id)
        except (urllib.error.URLError, OSError, TimeoutError):
            got = None
        if got is None:
            unreachable += 1             # one slow answer is normal; a minute of none is a closed app
            if unreachable >= max(1, int(60 / POLL_S)):
                last["_note"] = "the app stopped answering while the job ran; its last known state is above"
                return last
        elif "http_status" in got:
            refused += 1                 # the status route is gone (a sidecar that exited)
            if refused >= 5:
                last = {**last, "_status_route": got,
                        "_note": "the job's status route stopped answering; its last known state is above"}
                return last
        else:
            last, refused, unreachable = got, 0, 0
            if str(job.state(got) or "").lower() in job.done:
                return last
        if time.time() >= deadline:
            break
        time.sleep(POLL_S)
    try:
        cancelled = _http("POST", base + job.cancel.format(id=urllib.parse.quote(str(job_id), safe="")),
                          cancel_body or {})
    except (urllib.error.URLError, OSError, TimeoutError):
        cancelled = {"ok": False}
    last["_timeout"] = (f"still running after {int(budget_s)}s — CANCELLED so your model could be "
                        f"reloaded (cancel answered {cancelled}). Pass a larger wait_s for a long job.")
    return last


def _submit(job: _Job, base: str, path: str, body: dict, timeout: int, wait: bool, budget: float,
            app: str, poll_hint: str, cancel_body: dict | None = None) -> str:
    """POST an async job, then either wait for it (the seat is suspended) or hand back
    its id with how to poll it."""
    result = _http("POST", base + path, body, timeout=timeout)
    job_id = job.job_id(result)
    if not job_id:
        return _fmt(result)
    if not wait:
        return _fmt(result) + f"\n(async: poll with {poll_hint} job_id={job_id} until it reports done)"
    return _fmt(_wait(job, base, str(job_id), budget, cancel_body))


def _budget(args: dict) -> float:
    try:
        return max(1.0, float(args.get("wait_s") or JOB_WAIT_S))
    except (TypeError, ValueError):
        return float(JOB_WAIT_S)


# -- image ---------------------------------------------------------------

#: tool arg name -> LiteImage API field. Omitted args fall back to the API's
#: own defaults — EXCEPT guidance_scale, which is pinned in generate below.
#: Anything else the API takes rides in `params` under its API name.
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

#: Synchronous GPU routes: action -> (path, timeout).
_IMAGE_SYNC = {
    "upscale": ("/upscale", IMAGE_GEN_TIMEOUT),
    "faceswap": ("/faceswap", IMAGE_GEN_TIMEOUT),
    # A whole gallery, one swap per image.
    "faceswap_scrape": ("/faceswap/scrape", 3 * 3600),
}

_IMAGE_LOOKUPS = {
    "status": ("/status", 3), "models": ("/models", 30), "config": ("/config", 10),
    "loras": ("/loras", 30), "video_readiness": ("/video/readiness", 10),
    "ultratex_status": ("/ultratex/status", 30),
}


def _image_body(args: dict) -> dict:
    body = {}
    if (args.get("prompt") or "").strip():
        body["prompt"] = args["prompt"].strip()
    for arg_key, api_key in _IMAGE_PASSTHROUGH.items():
        if args.get(arg_key) is not None:
            body[api_key] = args[arg_key]
    if args.get("save_to_gallery") is True:
        body["save_to_gallery"] = True
    body.update(_params(args))
    return body


def _image(action: str, args: dict, wait: bool = False) -> str:
    try:
        if action in _IMAGE_LOOKUPS:
            path, timeout = _IMAGE_LOOKUPS[action]
            return _fmt(_http("GET", f"{IMAGE_URL}{path}", timeout=timeout))
        if action == "cancel":
            kind = (args.get("kind") or "").strip().lower()
            if kind:
                job = _IMAGE_JOBS.get(kind)
                if job is None or not args.get("job_id"):
                    return f"Error: image cancel of a job needs kind = {' | '.join(_IMAGE_JOBS)} and job_id."
                return _fmt(_http("POST", IMAGE_URL + job.cancel.format(id=args["job_id"]), {}))
            return _fmt(_http("POST", f"{IMAGE_URL}/cancel", body={}))
        if action == "job":
            job = _IMAGE_JOBS.get((args.get("kind") or "").strip().lower())
            if job is None or not args.get("job_id"):
                return f"Error: image job needs kind = {' | '.join(_IMAGE_JOBS)} and job_id."
            return _fmt(job.poll(IMAGE_URL, str(args["job_id"])))
        if action == "load_model":
            file_path = (args.get("file_path") or _params(args).get("file_path") or "").strip()
            if not file_path:
                return ("Error: 'file_path' is required for image load_model. "
                        "Use action=models to list available checkpoints.")
            # Loading a big GGUF takes real time — same budget as generation.
            return _fmt(_http("POST", f"{IMAGE_URL}/load_model",
                              body={"file_path": file_path}, timeout=300))
        if action == "unload_model":
            return _fmt(_http("POST", f"{IMAGE_URL}/unload_model", body={}))
        if action == "scrape":
            # Lists a gallery's images; no GPU (the swap is faceswap_scrape).
            return _fmt(_http("POST", f"{IMAGE_URL}/scraper/scrape", _params(args), timeout=300))
        if action == "generate":
            body = _image_body(args)
            if not body.get("prompt"):
                return "Error: 'prompt' is required for image generate."
            # The API's own guidance default (7.5) does NOT match LiteImage's
            # UI default (3.5), and for flux1-dev the overdriven CFG produced
            # blurry/overprocessed results while the UI was fine with 3.5
            # (the user, 2026-08-29: "the issue is the settings you're using").
            # Pin the tool default to the UI value; an explicit `guidance` arg
            # overrides it for other model families.
            if "guidance_scale" not in body:
                body["guidance_scale"] = 3.5
            return _fmt(_http("POST", f"{IMAGE_URL}/generate", body, timeout=IMAGE_GEN_TIMEOUT))
        if action in _IMAGE_SYNC:
            path, timeout = _IMAGE_SYNC[action]
            return _fmt(_http("POST", IMAGE_URL + path, _image_body(args), timeout=timeout))
        if action in _IMAGE_JOBS:
            job = _IMAGE_JOBS[action]
            body = _image_body(args)
            if action == "video" and not body.get("prompt"):
                return "Error: 'prompt' is required for image video."
            return _submit(job, IMAGE_URL, job.submit, body, 60, wait, _budget(args), "image",
                           f"app=image action=job kind={action}")
        return (f"Error: unknown image action {action!r} (status, models, loras, config, "
                f"video_readiness, ultratex_status, generate, upscale, faceswap, faceswap_scrape, "
                f"scrape, {', '.join(_IMAGE_JOBS)}, job, cancel, load_model, unload_model)")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return _not_running("LiteImage", IMAGE_URL, e)


# -- director (LiteImage's H3 Director) -----------------------------------

_DIRECTOR_ENGINE_OPS = ("validate", "install", "cancel", "activate", "model")


def _director(action: str, args: dict, wait: bool = False) -> str:
    params = _params(args)
    base = IMAGE_URL
    try:
        if action == "describe":
            full = "full" if params.get("project") == "full" else "summary"
            return _fmt(_http("GET", f"{base}/director/describe?project={full}", timeout=30))
        if action == "request":
            if not params.get("action"):
                return ("Error: director request needs params = a Director request object with its "
                        "own action (list-projects, get-project, create-project, edit-project, compile, "
                        "readiness, list-checkpoints, …).")
            if params["action"] in ("generate", "retry-job"):
                action = "generate" if params["action"] == "generate" else "retry"
            else:
                return _fmt(_http("POST", f"{base}/director/request", params, timeout=120))
        if action == "ui":
            if params:
                return _fmt(_http("POST", f"{base}/director/ui", params, timeout=60))
            return _fmt(_http("GET", f"{base}/director/ui", timeout=30))
        if action == "screenshot":
            crop = "0" if params.get("crop") is False else "1"
            return _fmt(_http("GET", f"{base}/director/screenshot?crop={crop}", timeout=60))
        if action == "engine":
            op = str(params.pop("op", "") or "").strip()
            if not op:
                return _fmt(_http("GET", f"{base}/director/engine", timeout=30))
            if op not in _DIRECTOR_ENGINE_OPS:
                return f"Error: director engine op must be one of {', '.join(_DIRECTOR_ENGINE_OPS)}."
            return _fmt(_http("POST", f"{base}/director/engine/{op}", params, timeout=120))
        if action == "jobs":
            return _fmt(_http("GET", f"{base}/director/jobs", timeout=30))
        if action == "job":
            if not args.get("job_id"):
                return "Error: 'job_id' is required for director job."
            return _fmt(_DIRECTOR_JOB.poll(base, str(args["job_id"])))
        if action == "cancel":
            if not args.get("job_id"):
                return "Error: 'job_id' is required for director cancel."
            return _fmt(_http("POST", base + _DIRECTOR_JOB.cancel.format(id=args["job_id"]), params))
        if action in ("generate", "retry"):
            request = {**params, "action": "generate" if action == "generate" else "retry-job"}
            if action == "generate" and not request.get("projectId"):
                return ("Error: director generate needs params.projectId and params.expectedRevision "
                        "(describe or request list-projects gives both); intent = preview | balanced | final.")
            if action == "retry" and not request.get("jobId"):
                return "Error: director retry needs params.jobId."
            cancel_body = {"projectId": request["projectId"]} if request.get("projectId") else {}
            return _submit(_DIRECTOR_JOB, base, "/director/request", request, 120, wait, _budget(args),
                           "director", "app=director action=job", cancel_body)
        return ("Error: unknown director action "
                f"{action!r} (describe, request, ui, screenshot, engine, generate, retry, job, jobs, cancel)")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return _not_running("LiteImage", base, e)


# -- sound ---------------------------------------------------------------

def _sound(action: str, args: dict, wait: bool = False) -> str:
    try:
        if action == "status":
            return _fmt(_http("GET", f"{SOUND_URL}/status", timeout=3))
        if action in ("jobs", "gallery", "backends", "config"):
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
        if action == "backend_load":
            body = _params(args)
            if not body.get("backend"):
                return "Error: sound backend_load needs params.backend (see action=backends)."
            return _fmt(_http("POST", f"{SOUND_URL}/backend/load", body, timeout=900))
        if action == "backend_unload":
            return _fmt(_http("POST", f"{SOUND_URL}/backend/unload", body={}, timeout=120))
        if action == "generate":
            mode = (args.get("mode") or "").strip().lower()
            if mode not in _SOUND_MODES:
                return (f"Error: sound generate needs mode = "
                        f"{' | '.join(_SOUND_MODES)} (got {mode or 'nothing'!r})")
            body = {}
            for key in ("prompt", "lyrics", "duration"):
                if args.get(key) is not None:
                    body[key] = args[key]
            body.update(_params(args))
            if not (body.get("prompt") or body.get("lyrics")):
                return "Error: sound generate needs a 'prompt' and/or 'lyrics'."
            return _submit(_SOUND_JOB, SOUND_URL, f"/generate/{mode}", body, 30, wait,
                           _budget(args), "sound", "app=sound action=job")
        return (f"Error: unknown sound action {action!r} "
                f"(status, generate, job, jobs, cancel, gallery, backends, config, "
                f"backend_load, backend_unload)")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        return _not_running("LiteSound", SOUND_URL, e)


# -- model ---------------------------------------------------------------

#: Arguments that steer this tool, never forwarded to `lst run model`.
_TOOL_ONLY = {"app", "action", "params", "wait_s", "background"}
#: litesuite-tools' own timeouts for these verbs (model.py), plus a minute.
_MODEL_TIMEOUTS = {"generate": 960, "batch": 3660, "rig": 960}


def _model(action: str, args: dict, wait: bool = False) -> str:
    lst = shutil.which("lst")
    if not lst:
        return (
            "[lst CLI not found] LiteModeler is driven through litesuite-tools "
            "(`lst run model ...`), which owns the find-the-CLI logic for dev "
            "checkouts AND installed copies. Install LiteSuite's tools, or ask "
            "the human to."
        )
    # Public studio shorthand differs from the litesuite-tools action name.
    cli_action = "list_families" if action == "families" else action
    cmd = [lst, "run", "model", f"action={cli_action}"]
    # Every parameter the model tool takes (out, image, prompt, family, seed, steps,
    # input, format, …) is forwarded as key=value: generate REQUIRES `out`, and a
    # prompt-only forward made every generate fail on it.
    fields = {k: v for k, v in args.items() if k not in _TOOL_ONLY and v is not None}
    fields.update(_params(args))
    for key, value in fields.items():
        if isinstance(value, bool):
            value = "true" if value else "false"
        cmd.append(f"{key}={value}")
    timeout = _MODEL_TIMEOUTS.get(cli_action, 600)
    try:
        # Through the ttyguard envelope, like every child this app spawns —
        # a raw subprocess can steal or wreck the terminal this TUI lives in.
        # (The envelope sweep in test_ttyguard.py caught exactly this line
        # as a raw call. The gate works; obey it.)
        proc = ttyguard.run(cmd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"[model action timed out after {timeout}s]"
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    out = out.strip() or f"(exit {proc.returncode}, no output)"
    return out[:4000]


# -- entry ---------------------------------------------------------------

#: Actions that light up the GPU, per app. Everything else (status, job,
#: gallery, models, config, project edits…) is a lookup or an edit and never
#: touches the seat. load_model joins them: loading a big GGUF needs the VRAM
#: the seat occupies.
_GPU_ACTIONS = {
    "image": {"generate", "upscale", "load_model", "faceswap", "faceswap_scrape", *_IMAGE_JOBS},
    "director": {"generate", "retry"},
    "sound": {"generate", "backend_load"},
    "model": {"generate", "batch", "rig"},
}

_APPS = {"image": _image, "director": _director, "sound": _sound, "model": _model}


def _is_gpu(app: str, action: str, args: dict) -> bool:
    if action in _GPU_ACTIONS.get(app, ()):
        return True
    # A Director request whose own action renders.
    return app == "director" and action == "request" and \
        _params(args).get("action") in ("generate", "retry-job")


def _release_app_gpu(app: str) -> str:
    """Ask the app that just ran a GPU job to give its GPU memory back before the
    seat reloads (Ryan 2026-10-26, T0702: release, then reload the seat). Returns a
    note for the reply, "" when there is nothing to say.

    LiteImage keeps a resident sd-server ("keep model loaded"), the face-swap
    sidecar and the LTX sidecar after a job; `/gpu/release` stops them and keeps the
    chosen model selected. LiteModeler's prompt mode drives LiteImage, so it is
    released too. LiteSound loads its backend on each generate, so `/backend/unload`
    costs the next generate nothing it would not do anyway."""
    if app in ("image", "director", "model"):
        url, what = f"{IMAGE_URL}/gpu/release", "LiteImage"
    elif app == "sound":
        url, what = f"{SOUND_URL}/backend/unload", "LiteSound"
    else:
        return ""
    try:
        got = _http("POST", url, {}, timeout=120)
    except (urllib.error.URLError, OSError, TimeoutError):
        return ""                         # not running: it holds nothing
    if got.get("http_status") == 404:
        return (f"\n[{what} has no GPU release route in this build — anything it keeps "
                "resident stays in VRAM while your model reloads]")
    if got.get("http_status") or got.get("ok") is False:
        return f"\n[{what} did not release its GPU memory: {_fmt(got)}]"
    return ""


def _generate_suspended(app: str, action: str, args: dict, seat_model: str,
                        backend=None) -> str:
    """Suspend the agent's own model, generate, release the app's GPU memory,
    resume. The order of the finally matters more than anything else in this
    file: whatever the generation did — succeed, fail, raise — the seat comes
    back.

    The backend handle routes the verbs to the ENGINE that owns the seat —
    lms verbs against a llama-served model manage nothing (Sentinel's
    integration finding, 2026-08-21). A seat the engine cannot free is refused
    by name and the job is NOT started (T0702)."""
    rec = seat_guard.record(seat_model, backend)
    if rec is None:
        # Seat not resident (already unloaded, or a remote backend): nothing to
        # suspend, just generate.
        return _APPS[app](action, args)

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
    out, released = "", ""
    try:
        out = _APPS[app](action, args, wait=True)
    finally:
        try:
            released = _release_app_gpu(app)
        finally:
            resume_err = seat_guard.resume(rec, backend)
    if resume_err:
        return out + released + f"\n\n[SEAT RESUME PROBLEM] {resume_err}"
    return out + released + note


def run(args: dict, seat_model: str | None = None, backend=None) -> str:
    app = (args.get("app") or "").strip().lower()
    action = (args.get("action") or "").strip().lower()
    if app not in _APPS:
        return f"Error: unknown app {app!r} — image, director, sound, or model."
    if seat_model and _is_gpu(app, action, args):
        return _generate_suspended(app, action, args, seat_model, backend)
    return _APPS[app](action, args)
