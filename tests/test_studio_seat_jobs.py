"""The studio tool around the seat (T0702): every GPU action suspends the agent's own model,
runs the job to its end — async jobs waited on INSIDE the call — has the app give its GPU
memory back, and only then reloads the seat.

Ryan 2026-10-26: "we need to make sure litetuis studio tool, can run all liteimage and lite*
app generation while correctly unload the local model from vram during generation. for
liteimage this includes all generation and features threw the generation panel, faceswap
panel and H3 director." And, same day, for what happens after the job: release, then reload
the seat.

The apps are a real HTTP server in this process shaped like LiteImage / LiteSound (routes and
reply shapes read from their api-server.ts, director-routes.ts, job trackers). The transport is
part of what is tested — an app's error reply used to be reported as "the app is not running".
One event log is shared by the fake seat and the fake apps, so ORDER is asserted, not just
presence.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from litetui import seat_guard, studio_tool


class Apps:
    """LiteImage + LiteSound on one port. `routes[(method, path)]` -> (code, body) or a callable."""

    def __init__(self, log):
        self.log, self.routes = log, {}
        apps = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self, method):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"null") if n else None
                path = self.path.split("?", 1)[0]
                apps.log.append((method, path, body))
                answer = apps.routes.get((method, path), (404, {"ok": False, "error": f"Unknown route: {path}"}))
                code, reply = answer(body) if callable(answer) else answer
                payload = json.dumps(reply).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def sequence(self, *answers):
        """A route that answers each call with the next reply, then repeats the last."""
        left = list(answers)
        return lambda body: left.pop(0) if len(left) > 1 else left[0]


class Seat:
    """A backend whose seat can be freed; logs into the apps' event log."""
    name = "fake"

    def __init__(self, log, refuse=None):
        self.log, self.refuse, self.loaded = log, refuse, True

    def seat_snapshot(self, key):
        return {"identifier": key, "context": 32768, "parallel": 1, "status": "idle", "queued": 0} \
            if self.loaded else None

    def seat_suspend(self, rec):
        if self.refuse:
            return self.refuse
        self.loaded = False
        self.log.append(("SEAT", "suspend", None))
        return None

    def seat_resume(self, rec):
        self.loaded = True
        self.log.append(("SEAT", "resume", None))
        return None

    def reload_hint(self, rec):
        return "reload the fake seat"


@pytest.fixture
def env(tmp_path, monkeypatch):
    log = []
    apps = Apps(log)
    monkeypatch.setattr(studio_tool, "IMAGE_URL", apps.url)
    monkeypatch.setattr(studio_tool, "SOUND_URL", apps.url)
    monkeypatch.setattr(studio_tool, "POLL_S", 0.0)
    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "crumb.json")
    apps.routes[("POST", "/gpu/release")] = (200, {"ok": True, "released": ["sd-server"]})
    apps.routes[("POST", "/backend/unload")] = (200, {"ok": True})
    yield SimpleNamespace(log=log, apps=apps, seat=Seat(log))
    apps.httpd.shutdown()
    apps.httpd.server_close()


def _steps(log):
    """The event log as (who, what) — requests reduced to their path."""
    return [(m, p) for m, p, _ in log]


def test_an_app_error_is_its_answer_not_an_outage(env):
    """🔴 HTTPError is a URLError: LiteImage's 403 licence reply was reported as "LiteImage
    must be RUNNING", sending the model to ask the human to open an app that was open."""
    env.apps.routes[("POST", "/generate")] = (403, {"ok": False, "error": "Sign in with Google or activate a license"})
    out = studio_tool.run({"app": "image", "action": "generate", "prompt": "a fox"})
    assert "Sign in with Google" in out and "403" in out
    assert "not available" not in out


def test_base64_is_omitted_and_the_paths_survive(env):
    env.apps.routes[("POST", "/generate")] = (200, {"ok": True, "imagePath": "C:/out/fox.png",
                                                     "imageBase64": "A" * 50000})
    out = studio_tool.run({"app": "image", "action": "generate", "prompt": "a fox"})
    assert "C:/out/fox.png" in out and "AAAA" not in out and "base64 omitted" in out


def test_an_async_video_is_waited_on_with_the_seat_out_then_the_gpu_is_released(env):
    env.apps.routes[("POST", "/video/generate")] = (200, {"ok": True, "job_id": "v1"})
    env.apps.routes[("GET", "/video/status/v1")] = env.apps.sequence(
        (200, {"ok": True, "status": "running", "progress": 10}),
        (200, {"ok": True, "status": "running", "progress": 60}),
        (200, {"ok": True, "status": "complete", "result": {"mp4Path": "C:/out/clip.mp4"}}))
    out = studio_tool.run({"app": "image", "action": "video", "prompt": "rain on a window"},
                          seat_model="seat", backend=env.seat)
    assert "C:/out/clip.mp4" in out and "was suspended for this generation" in out
    steps = _steps(env.log)
    assert steps[0] == ("SEAT", "suspend") and steps[1] == ("POST", "/video/generate")
    assert steps[-2:] == [("POST", "/gpu/release"), ("SEAT", "resume")], \
        "the app gives its GPU back BEFORE the seat reloads"
    assert steps.count(("GET", "/video/status/v1")) == 3


def test_without_a_local_seat_an_async_job_hands_back_its_id(env):
    env.apps.routes[("POST", "/video/generate")] = (200, {"ok": True, "job_id": "v1"})
    out = studio_tool.run({"app": "image", "action": "video", "prompt": "rain"})
    assert "v1" in out and "action=job kind=video" in out
    assert ("POST", "/gpu/release") not in _steps(env.log)


def test_a_job_past_its_wait_is_cancelled_before_the_seat_comes_back(env):
    env.apps.routes[("POST", "/faceswap/video")] = (200, {"ok": True, "job_id": "f1", "output_path": "C:/o.mp4"})
    env.apps.routes[("GET", "/faceswap/video/f1")] = (200, {"ok": True, "status": "running"})
    env.apps.routes[("POST", "/faceswap/video/f1/cancel")] = (200, {"ok": True, "cancelled": True})
    out = studio_tool.run({"app": "image", "action": "faceswap_video", "wait_s": 0.01,
                           "params": {"video_path": "C:/in.mp4", "target_path": "C:/face.png"}},
                          seat_model="seat", backend=env.seat)
    assert "CANCELLED" in out
    steps = _steps(env.log)
    assert steps.index(("POST", "/faceswap/video/f1/cancel")) < steps.index(("SEAT", "resume"))


def test_an_app_that_closes_mid_job_ends_the_wait_and_the_seat_comes_back(env):
    env.apps.routes[("POST", "/video/generate")] = (200, {"ok": True, "job_id": "v9"})
    env.apps.routes[("GET", "/video/status/v9")] = (200, {"ok": True, "status": "running", "progress": 5})
    polls = []
    real_poll = studio_tool._Job.poll

    def poll(self, base, job_id):
        polls.append(job_id)
        if len(polls) > 2:               # LiteImage was closed after two answers
            raise studio_tool.urllib.error.URLError(ConnectionRefusedError("closed"))
        return real_poll(self, base, job_id)

    env.apps.routes[("POST", "/video/cancel/v9")] = (200, {"ok": True})
    studio_tool._Job.poll = poll
    try:
        out = studio_tool.run({"app": "image", "action": "video", "prompt": "rain"},
                              seat_model="seat", backend=env.seat)
    finally:
        studio_tool._Job.poll = real_poll
    assert "stopped answering" in out and _steps(env.log)[-1] == ("SEAT", "resume")


def test_a_seat_that_cannot_be_freed_starts_nothing(env):
    seat = Seat(env.log, refuse="the Strata server at http://127.0.0.1:8080 has no /v1/unload")
    out = studio_tool.run({"app": "image", "action": "generate", "prompt": "x"},
                          seat_model="seat", backend=seat)
    assert "generation not started" in out and "no /v1/unload" in out
    assert env.log == [], "the app must not have been asked for anything"


def test_an_h3_director_render_runs_to_its_end_with_the_seat_out(env):
    env.apps.routes[("POST", "/director/request")] = (200, {"ok": True, "value": {"id": "d1", "stage": "queued"}})
    env.apps.routes[("GET", "/director/jobs/d1")] = env.apps.sequence(
        (200, {"ok": True, "value": {"id": "d1", "stage": "base", "progress": 0.4}}),
        (200, {"ok": True, "value": {"id": "d1", "stage": "complete",
                                     "artifacts": {"mp4Path": "C:/director/render.mp4"}}}))
    out = studio_tool.run({"app": "director", "action": "generate",
                           "params": {"projectId": "p1", "expectedRevision": 4, "intent": "final"}},
                          seat_model="seat", backend=env.seat)
    assert "C:/director/render.mp4" in out
    sent = next(b for m, p, b in env.log if p == "/director/request")
    assert sent == {"projectId": "p1", "expectedRevision": 4, "intent": "final", "action": "generate"}
    assert _steps(env.log)[-2:] == [("POST", "/gpu/release"), ("SEAT", "resume")]


def test_a_director_request_that_renders_suspends_too_and_one_that_edits_does_not(env):
    env.apps.routes[("POST", "/director/request")] = (200, {"ok": True, "value": {"projects": []}})
    studio_tool.run({"app": "director", "action": "request", "params": {"action": "list-projects"}},
                    seat_model="seat", backend=env.seat)
    assert ("SEAT", "suspend") not in _steps(env.log)
    env.apps.routes[("POST", "/director/request")] = (200, {"ok": True, "value": {"id": "d2", "stage": "queued"}})
    env.apps.routes[("GET", "/director/jobs/d2")] = (200, {"ok": True, "value": {"id": "d2", "stage": "complete"}})
    studio_tool.run({"app": "director", "action": "request",
                     "params": {"action": "retry-job", "jobId": "d0"}},
                    seat_model="seat", backend=env.seat)
    assert ("SEAT", "suspend") in _steps(env.log)


def test_sound_waits_for_its_job_and_releases_its_backend(env):
    env.apps.routes[("POST", "/generate/speech")] = (200, {"ok": True, "job_id": "s1"})
    env.apps.routes[("GET", "/job/s1")] = env.apps.sequence(
        (200, {"ok": True, "status": "running"}),
        (200, {"ok": True, "status": "complete", "outputPath": "C:/out/line.wav"}))
    out = studio_tool.run({"app": "sound", "action": "generate", "mode": "speech", "prompt": "hello"},
                          seat_model="seat", backend=env.seat)
    assert "C:/out/line.wav" in out
    assert _steps(env.log)[-2:] == [("POST", "/backend/unload"), ("SEAT", "resume")]


def test_an_older_liteimage_without_a_release_route_is_named(env):
    del env.apps.routes[("POST", "/gpu/release")]
    env.apps.routes[("POST", "/faceswap")] = (200, {"ok": True, "outputPath": "C:/out/swapped.png"})
    out = studio_tool.run({"app": "image", "action": "faceswap",
                           "params": {"source_image": "C:/in.png", "target_image": "C:/face.png"}},
                          seat_model="seat", backend=env.seat)
    assert "C:/out/swapped.png" in out and "no GPU release route" in out
    assert _steps(env.log)[-1] == ("SEAT", "resume")


def test_an_image_upscales_to_an_exact_size_with_dlss5_through_the_canvas_path(env):
    """Ryan 2026-10-26: "i want an agent to be able to directly upscale an image to X size and also
    use dlss5 if i want". The Canvas's own handler does it (LiteImage generate:upscale): ESRGAN
    passes to reach the size, one Lanczos resize, DLSS 5 last, the saved model and runtime."""
    env.apps.routes[("POST", "/ipc/generate:upscale")] = (202, {"ok": True, "job": {"id": "up1", "status": "running"}})
    env.apps.routes[("GET", "/ipc/jobs/up1")] = env.apps.sequence(
        (200, {"ok": True, "job": {"id": "up1", "status": "running"}}),
        (200, {"ok": True, "job": {"id": "up1", "status": "complete", "result": {
            "result": {"id": "x"}, "followUps": [{"id": "x-sized"}, {"id": "x-dlss5"}],
            "final": {"imagePath": "C:/out/x_dlss5.png"}}}}))
    out = studio_tool.run({"app": "image", "action": "upscale", "params": {
        "image_path": "C:/in.png", "width": 3840, "dlss5": True, "output_path": "C:/final/big.png"}},
        seat_model="seat", backend=env.seat)
    assert "C:/out/x_dlss5.png" in out
    sent = next(b for m, p, b in env.log if p == "/ipc/generate:upscale")
    assert sent == {"args": [{"imagePath": "C:/in.png", "width": 3840, "outputPath": "C:/final/big.png", "dlss5": {}}]}
    steps = _steps(env.log)
    assert steps.index(("SEAT", "suspend")) < steps.index(("POST", "/ipc/generate:upscale"))
    assert steps[-2:] == [("POST", "/gpu/release"), ("SEAT", "resume")]


def test_a_video_upscale_gets_the_panels_defaults_for_what_it_requires(env):
    env.apps.routes[("POST", "/video/upscale")] = (202, {"ok": True, "job": {"id": "vu1", "status": "queued"}})
    env.apps.routes[("GET", "/video/upscale/vu1")] = (200, {"ok": True, "job": {"id": "vu1", "status": "complete",
                                                                             "output": "C:/out/4k.mp4"}})
    out = studio_tool.run({"app": "image", "action": "video_upscale", "params": {
        "source": "C:/in.mp4", "output": "C:/out/4k.mp4", "width": 3840, "height": 2160}},
        seat_model="seat", backend=env.seat)
    assert "C:/out/4k.mp4" in out
    sent = next(b for m, p, b in env.log if p == "/video/upscale")
    assert sent == {"source": "C:/in.mp4", "output": "C:/out/4k.mp4", "width": 3840, "height": 2160,
                    "aspect": "source", "fit": "fit", "crop": None, "provider": "dlss5"}
    assert "needs params.source" in studio_tool.run({"app": "image", "action": "video_upscale",
                                                     "params": {"source": "C:/in.mp4"}})


def _bridge(env, channel, gpu, job_states):
    env.apps.routes[("GET", "/ipc/channels")] = (200, {"ok": True, "channels": [
        {"channel": channel, "gpu": gpu, "available": True}]})
    env.apps.routes[("POST", f"/ipc/{channel}")] = (202, {"ok": True, "job": {"id": "j1", "status": "running"}})
    env.apps.routes[("GET", "/ipc/jobs/j1")] = env.apps.sequence(*[
        (200, {"ok": True, "job": {"id": "j1", "status": s, "result": {"imagePaths": ["C:/out/fixed-face.png"]}}})
        for s in job_states])


def test_a_panel_feature_without_a_rest_route_runs_through_the_bridge_with_the_seat_out(env):
    """Face fix, identity, H3 references... reach LiteImage as the panel's own handler (/ipc)."""
    _bridge(env, "generate:start", True, ["running", "complete"])
    request = {"prompt": "portrait", "faceFix": True, "identity": {"kind": "ipadapter", "images": ["C:/me.png"]}}
    out = studio_tool.run({"app": "image", "action": "ipc",
                           "params": {"channel": "generate:start", "args": [request]}},
                          seat_model="seat", backend=env.seat)
    assert "fixed-face.png" in out
    sent = next(b for m, p, b in env.log if p == "/ipc/generate:start")
    assert sent == {"args": [request]}
    steps = _steps(env.log)
    assert steps.index(("SEAT", "suspend")) < steps.index(("POST", "/ipc/generate:start"))
    assert steps[-2:] == [("POST", "/gpu/release"), ("SEAT", "resume")]


def test_a_bridged_lookup_never_suspends_and_is_waited_on(env):
    _bridge(env, "faceswap:saved-faces-list", False, ["complete"])
    out = studio_tool.run({"app": "image", "action": "ipc",
                           "params": {"channel": "faceswap:saved-faces-list", "args": []}},
                          seat_model="seat", backend=env.seat)
    assert "complete" in out
    assert ("SEAT", "suspend") not in _steps(env.log)


def test_model_generate_forwards_every_field_lst_needs(monkeypatch, env):
    """`lst run model action=generate` REQUIRES `out` and image-or-prompt; the tool used to
    forward the prompt alone, so every generate failed on the missing `out`."""
    calls = []
    monkeypatch.setattr(studio_tool.shutil, "which", lambda _n: "lst.exe")
    monkeypatch.setattr(studio_tool.ttyguard, "run", lambda cmd, timeout: calls.append((cmd, timeout)) or
                        SimpleNamespace(stdout="wrote C:/out/cat.glb", stderr="", returncode=0))
    out = studio_tool.run({"app": "model", "action": "generate", "prompt": "a cat", "out": "C:/out/cat.glb",
                           "family": "trellis2", "params": {"seed": 7, "no_texture": True}},
                          seat_model="seat", backend=env.seat)
    assert "cat.glb" in out
    cmd, timeout = calls[0]
    assert cmd[:4] == ["lst.exe", "run", "model", "action=generate"]
    assert set(cmd[4:]) == {"prompt=a cat", "out=C:/out/cat.glb", "family=trellis2", "seed=7", "no_texture=true"}
    assert timeout > 900, "litesuite-tools itself waits 900 s for a generate"
    assert _steps(env.log)[-2:] == [("POST", "/gpu/release"), ("SEAT", "resume")], \
        "prompt mode drives LiteImage, so LiteImage is released too"
