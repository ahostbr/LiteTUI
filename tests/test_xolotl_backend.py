"""XolotlBackend (T0633) — StrataBackend with Xolotl's launcher, settings and name, against a
local fake server shaped like Xolotl's (Strata's serve/server.py behind serve_ds41.py).
Nothing here starts an engine: the only server is a thread in this process."""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from litetui import llm_backend, xolotl_engine
from litetui.launch_options import LaunchOptions
from litetui.llm_backend import BackendError
from litetui.settings import Settings
from litetui.strata_backend import StrataBackend
from litetui.turn_engine import _resolve_reasoning_effort
from litetui.xolotl_backend import XOLOTL_REASONING_LEVELS, XolotlBackend

MODEL = "deepseek-v4.1-flash-xolotl"


@pytest.fixture(autouse=True)
def _no_real_litesuite_config(monkeypatch, tmp_path):
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path / "llm"))


@pytest.fixture
def server():
    state = {"loaded": True}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            status = {"value": "loaded" if state["loaded"] else "unloaded"}
            body = {
                "/health": {"status": "ok", "loaded": state["loaded"], "service": "strata"},
                "/v1/models": {"object": "list", "data": [{
                    "id": MODEL, "status": status, "meta": {"n_ctx": 262144},
                    "architecture": {"input_modalities": ["text", "image"]}}]},
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


def _backend(host: str) -> XolotlBackend:
    return XolotlBackend(Settings(backend="xolotl", xolotl_host=host))


def test_xolotl_is_a_listed_engine_backend_the_factory_builds():
    assert "xolotl" in llm_backend.BACKEND_NAMES and "xolotl" in llm_backend.ENGINE_BACKENDS
    assert "DeepSeek" in llm_backend.backend_label("xolotl")
    assert "xolotl" in dict(llm_backend.visible_backends())
    backend = llm_backend.make_backend(Settings(backend="xolotl"))
    assert isinstance(backend, XolotlBackend) and backend.name == "xolotl" and backend.label == "Xolotl"
    assert backend._engine is xolotl_engine


def test_it_answers_everything_strata_answers():
    public = lambda cls: {a for a in dir(cls) if not a.startswith("_")}  # noqa: E731
    assert public(StrataBackend) <= public(XolotlBackend)


def test_a_loaded_server_is_ready_and_its_messages_name_xolotl(server):
    backend = _backend(server["host"])
    assert asyncio.run(backend.ensure_running()) == "ok"
    assert backend.base_url() == server["host"] + "/v1"
    rows = asyncio.run(backend.list_models())
    assert [(r.key, r.loaded) for r in rows] == [(MODEL, True)]
    assert asyncio.run(backend.model_info(MODEL)) == (262144, "vlm", True)
    asyncio.run(backend.ensure_chat_ready(MODEL))
    with pytest.raises(BackendError, match="this Xolotl server serves"):
        asyncio.run(backend.ensure_chat_ready("qwen"))
    assert "attached Xolotl server" in backend.engine_status()


def test_an_unloaded_server_is_not_ready(server):
    server["loaded"] = False
    backend = _backend(server["host"])
    with pytest.raises(BackendError, match="Xolotl server .* unloaded"):
        asyncio.run(backend.ensure_running())
    assert backend.loaded_models() == [] and "UNLOADED" in backend.engine_status()


def test_no_server_names_xolotls_settings_and_repo():
    backend = _backend("http://127.0.0.1:9")
    with pytest.raises(BackendError, match="set xolotl_host") as exc:
        asyncio.run(backend.ensure_running())
    assert xolotl_engine.XOLOTL_REPO_URL in str(exc.value)
    assert "no Xolotl server" in backend.engine_status() and backend.stop_engine() == "no engine is running."


def test_the_control_plane_refuses_with_xolotls_context_setting(server):
    backend = _backend(server["host"])
    with pytest.raises(BackendError, match="xolotl_max_context"):
        asyncio.run(backend.load("x"))
    assert "a Xolotl server holds its model" in backend.seat_suspend({})


def test_start_goes_through_the_shared_lifecycle_and_the_xolotl_launcher(monkeypatch, server):
    backend = _backend(server["host"])
    owned = xolotl_engine.OwnedEngine(proc=type("P", (), {"pid": 9, "poll": lambda self: None})(),
                                      host=server["host"], log_path="log", log_file=None,
                                      model_id=MODEL, registered=False)
    monkeypatch.setattr(xolotl_engine, "start", lambda settings, healthy, notice=None: owned)
    assert asyncio.run(backend.start_engine()) == f"started Xolotl pid 9 at {server['host']} ({MODEL})"
    assert "LiteTUI-owned Xolotl server" in backend.engine_status()


def test_thinking_levels_are_serve_ds41s_and_ultra_folds_to_max():
    assert XolotlBackend(Settings()).reasoning_levels("any") == list(XOLOTL_REASONING_LEVELS)
    wire = {level: _resolve_reasoning_effort(level, "xolotl") for level in
            ("off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")}
    assert wire == {"off": "none", "minimal": "minimal", "low": "low", "medium": "medium",
                    "high": "high", "xhigh": "xhigh", "max": "max", "ultra": "max"}


def test_cli_launch_options_map_onto_the_xolotl_settings():
    start = LaunchOptions(base_url="http://127.0.0.1:8150", server_mode="start",
                          context_length=262144, model_path="F:/REPO_CLONES/Xolotl/strata-ds41-1017-v3.json")
    assert start.overrides(Settings(), "xolotl", None) == {
        "xolotl_host": "http://127.0.0.1:8150", "xolotl_max_context": 262144,
        "xolotl_config": "F:/REPO_CLONES/Xolotl/strata-ds41-1017-v3.json"}
    with pytest.raises(ValueError, match="Xolotl context is fixed at startup"):
        LaunchOptions(context_length=131072).overrides(Settings(), "xolotl", None)
    with pytest.raises(ValueError, match="Xolotl has no server executable"):
        LaunchOptions(server_executable="x.exe", server_mode="start").overrides(Settings(), "xolotl", None)
