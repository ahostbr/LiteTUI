"""StrataBackend (T0374) — the same shape as NInferBackend, against a local fake server.
Nothing here starts an engine: the only server is a thread in this process."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from litetui import llm_backend, strata_engine
from litetui.launch_options import LaunchOptions
from litetui.llm_backend import BackendError
from litetui.ninfer_backend import NInferBackend
from litetui.settings import Settings
from litetui.strata_backend import STRATA_REASONING_LEVELS, StrataBackend
from litetui.turn_engine import _resolve_reasoning_effort


@pytest.fixture(autouse=True)
def _no_real_litesuite_config(monkeypatch, tmp_path):
    """`strata_root` falls back to LiteSuite's config.json; never read the user's real one."""
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))


@pytest.fixture
def server():
    """A Strata-shaped server: /health and /v1/models, with a switchable `loaded`, and the
    load switch `serve/server.py` answers — POST /v1/unload and /v1/load (200 with the new
    state, 409 while a request runs, 404 on a build without them)."""
    state = {"loaded": True, "images": False, "busy": 0, "switch": True, "unload_status": None,
             "load_fails": None, "posts": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _reply(self, code, body):
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            state["posts"].append((self.path, self.headers.get("Content-Type")))
            if self.path not in ("/v1/unload", "/v1/load") or not state["switch"]:
                return self._reply(404, {"error": {"message": "not found"}})
            if state["busy"]:
                state["busy"] -= 1
                return self._reply(409, {"error": {"type": "model_busy",
                                                   "message": "a request is running or queued"}})
            if self.path == "/v1/unload":
                status = state["unload_status"] or ("unloaded" if state["loaded"] else "not loaded")
                if status in ("unloaded", "not loaded"):
                    state["loaded"] = False
                return self._reply(200, {"status": status})
            if state["load_fails"]:
                return self._reply(503, {"error": {"type": "server_error", "message": state["load_fails"]}})
            state["loaded"] = True
            return self._reply(200, {"status": "loaded"})

        def do_GET(self):
            status = {"value": "loaded" if state["loaded"] else "unloaded"}
            body = {
                "/health": {"status": "ok", "loaded": state["loaded"], "service": "strata"},
                "/v1/models": {"object": "list", "data": [{
                    "id": "qwen3.8-flash-next-iq3_xxs", "status": status, "meta": {"n_ctx": 150000},
                    "architecture": {"input_modalities": ["text", "image"] if state["images"] else ["text"]}}]},
            }.get(self.path)
            payload = json.dumps(body or {}).encode()
            self.send_response(200 if body else 404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["host"] = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield state
    httpd.shutdown()
    httpd.server_close()


def _backend(host: str) -> StrataBackend:
    return StrataBackend(Settings(backend="strata", strata_host=host))


def test_strata_is_a_listed_backend_the_factory_builds():
    assert "strata" in llm_backend.BACKEND_NAMES and "strata" in llm_backend.ENGINE_BACKENDS
    assert "Strata" in llm_backend.backend_label("strata")
    backend = llm_backend.make_backend(Settings(backend="strata"))
    assert isinstance(backend, StrataBackend) and backend.name == "strata" and backend.label == "Strata"


def test_it_answers_everything_the_ninfer_backend_answers():
    """The app calls backend methods without a guard, so a missing one is a crash (the
    lesson recorded in ninfer_backend.py). NInfer's public surface is the contract; the
    two names below are NInfer-only and every caller of them uses getattr."""
    public = lambda cls: {a for a in dir(cls) if not a.startswith("_")}  # noqa: E731
    assert public(NInferBackend) - public(StrataBackend) == {"engine_concurrency", "DEAD_HOST"}


def test_a_loaded_server_is_ready_and_reports_its_window(server):
    backend = _backend(server["host"])
    assert asyncio.run(backend.ensure_running()) == "ok"
    assert backend.base_url() == server["host"] + "/v1"
    rows = asyncio.run(backend.list_models())
    assert [(r.key, r.loaded) for r in rows] == [("qwen3.8-flash-next-iq3_xxs", True)]
    assert asyncio.run(backend.model_info(rows[0].key)) == (150000, "llm", True)
    assert asyncio.run(backend.model_info("something-else")) is None
    assert backend.loaded_models() == [rows[0].key]
    asyncio.run(backend.ensure_chat_ready(rows[0].key))
    server["images"] = True
    assert asyncio.run(backend.model_info(rows[0].key))[1] == "vlm"


def test_an_unloaded_server_is_not_ready_because_a_turn_would_reload_it(server):
    server["loaded"] = False
    backend = _backend(server["host"])
    with pytest.raises(BackendError, match="unloaded"):
        asyncio.run(backend.ensure_running())
    with pytest.raises(BackendError, match="unloaded"):
        asyncio.run(backend.ensure_chat_ready("qwen3.8-flash-next-iq3_xxs"))
    assert backend.loaded_models() == [] and backend.seat_snapshot("qwen3.8-flash-next-iq3_xxs") is None
    assert "UNLOADED" in backend.engine_status()


def test_no_server_names_the_two_ways_forward_and_never_raises_from_base_url():
    backend = _backend("http://127.0.0.1:9")            # discard port: nothing answers
    assert backend.base_url() == "http://127.0.0.1:9/v1"
    with pytest.raises(BackendError, match="/engine start") as exc:
        asyncio.run(backend.ensure_running())
    assert strata_engine.STRATA_REPO_URL in str(exc.value)
    assert backend.loaded_models() == [] and backend.stop_engine() == "no engine is running."
    assert "no Strata server" in backend.engine_status()


def test_a_server_it_did_not_start_is_never_stopped(server):
    backend = _backend(server["host"])
    assert backend.attached and "not started by this LiteTUI" in backend.stop_engine()
    assert backend.shutdown().owned is False


def test_the_control_plane_refuses_by_name(server):
    backend = _backend(server["host"])
    for call in (backend.load("x"), backend.unload("x"), backend.apply_load_settings("x", {})):
        with pytest.raises(BackendError, match="fixed when the server starts"):
            asyncio.run(call)


MODEL = "qwen3.8-flash-next-iq3_xxs"


def test_a_studio_generation_frees_the_seat_through_the_servers_own_switch(tmp_path, monkeypatch, server):
    """T0702. Ryan 2026-10-26: the studio tool must "correctly unload the local model from vram
    during generation". The seat used to refuse ("suspend unsupported"), so a generation never
    started on a Strata seat. Strata's server unloads between requests and keeps answering
    (`POST /v1/unload`), and loads again on `POST /v1/load` — the whole cycle runs on the server
    and works for a server LiteTUI did not start: nothing is killed or restarted."""
    from litetui import seat_guard

    monkeypatch.setattr(seat_guard, "BREADCRUMB", tmp_path / "crumb.json")
    backend = _backend(server["host"])
    assert backend.attached
    rec = seat_guard.record(MODEL, backend)
    assert rec["context"] == 150000

    assert seat_guard.suspend(rec, backend) is None
    assert server["loaded"] is False, "the engine must actually have unloaded"
    assert backend.seat_snapshot(MODEL) is None
    assert "/v1/load" in seat_guard.BREADCRUMB.read_text(encoding="utf-8"), \
        "the recovery line must name what reloads THIS engine"

    assert seat_guard.resume(rec, backend) is None
    assert server["loaded"] is True and not seat_guard.BREADCRUMB.exists()
    assert [p for p, _ in server["posts"]] == ["/v1/unload", "/v1/load"]
    assert {ct for _, ct in server["posts"]} == {"application/json"}, \
        "the server refuses a load switch sent as anything but JSON"


def test_a_busy_server_is_waited_on_then_named(monkeypatch, server):
    monkeypatch.setattr(StrataBackend, "SEAT_BUSY_WAIT_S", 5.0)
    monkeypatch.setattr("litetui.strata_backend.time.sleep", lambda s: None)
    backend = _backend(server["host"])
    server["busy"] = 2                     # a turn finishing: two refusals, then free
    assert backend.seat_suspend({"identifier": MODEL}) is None and server["loaded"] is False

    server["loaded"], server["busy"] = True, 10 ** 6
    monkeypatch.setattr(StrataBackend, "SEAT_BUSY_WAIT_S", 0.0)
    err = backend.seat_suspend({"identifier": MODEL})
    assert err and "still running" in err and server["loaded"] is True


@pytest.mark.parametrize("change,words", [
    ({"switch": False}, "no /v1/unload"),
    ({"unload_status": "unsupported"}, "cannot unload"),
])
def test_a_server_that_cannot_unload_is_refused_by_name(server, change, words):
    server.update(change)
    err = _backend(server["host"]).seat_suspend({"identifier": MODEL})
    assert err and words in err and server["loaded"] is True


def test_resume_says_why_the_model_did_not_come_back(server):
    backend = _backend(server["host"])
    assert backend.seat_suspend({"identifier": MODEL}) is None
    server["load_fails"] = "the GPU is in use by another program: 900 MiB of VRAM free"
    err = backend.seat_resume({"identifier": MODEL, "context": 150000})
    assert err and "in use by another program" in err
    server["load_fails"] = None
    assert backend.seat_resume({"identifier": MODEL, "context": 150000}) is None
    assert backend.seat_resume({"identifier": MODEL}) is None, "already serving: nothing to do"


def test_no_server_cannot_be_suspended_or_resumed():
    backend = _backend("http://127.0.0.1:9")
    assert "no Strata server" in backend.seat_suspend({"identifier": MODEL})
    assert "no Strata server" in backend.seat_resume({"identifier": MODEL})


def test_start_goes_through_the_shared_lifecycle_and_the_strata_launcher(monkeypatch, server):
    backend = _backend(server["host"])
    owned = strata_engine.OwnedEngine(proc=type("P", (), {"pid": 7, "poll": lambda self: None})(),
                                      host=server["host"], log_path="log", log_file=None,
                                      model_id="flash", registered=False)
    monkeypatch.setattr(strata_engine, "start", lambda settings, healthy, notice=None: owned)
    assert asyncio.run(backend.start_engine()) == f"started Strata pid 7 at {server['host']} (flash)"
    assert not backend.attached and "LiteTUI-owned" in backend.engine_status()
    with pytest.raises(BackendError, match="already tracked"):
        asyncio.run(backend.start_engine())


def test_thinking_levels_are_the_servers_and_stale_ones_fold_onto_them():
    assert StrataBackend(Settings()).reasoning_levels("any") == list(STRATA_REASONING_LEVELS)
    wire = {level: _resolve_reasoning_effort(level, "strata") for level in
            ("off", "minimal", "low", "medium", "high", "xhigh", "max")}
    assert wire == {"off": "none", "minimal": "low", "low": "low", "medium": "medium",
                    "high": "high", "xhigh": "high", "max": "high"}
    assert set(wire.values()) <= set(STRATA_REASONING_LEVELS)


def test_cli_launch_options_map_onto_the_strata_settings():
    start = LaunchOptions(base_url="http://127.0.0.1:8085", server_mode="start",
                          context_length=131072, model_path="C:/strata/strata-iq3_xxs.json")
    assert start.overrides(Settings(), "strata", None) == {
        "strata_host": "http://127.0.0.1:8085", "strata_max_context": 131072,
        "strata_config": "C:/strata/strata-iq3_xxs.json"}
    with pytest.raises(ValueError, match="fixed at startup"):
        LaunchOptions(context_length=131072).overrides(Settings(), "strata", None)
    with pytest.raises(ValueError, match="no server executable"):
        LaunchOptions(server_executable="x.exe", server_mode="start").overrides(Settings(), "strata", None)
