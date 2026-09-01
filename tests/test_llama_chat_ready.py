"""D2/D11 — chatting an unloaded or loading model must not surface a raw 400.

What the user did: typed a message. What the user saw:
``Error: Error code: 400 - {'error': {'code': 400, 'message': 'model is not
loaded', 'type': 'invalid_request_error'}}`` — because app.py hands whatever
``client.chat.completions.create`` raises straight to the message widget.

The router's own answers, measured on the installed b9360 binary and kept in
``tests/fixtures/llama_router_chat_errors_b9360.json``:

  model unloaded, in the preset  400  "model is not loaded"
  model absent from the preset   400  "model 'x' not found"
  no model field at all          400  "model name is missing from the request"
  model still LOADING            503  "Loading model"          <- not a 400

So there are two different situations behind one status, and one of them
(``loading``) resolves by itself if you wait. The seam owns that distinction:
``ensure_chat_ready`` waits out a load that is already in flight and says
"/load" in plain words when nothing is coming.

🔴 The 400 from ``/models/unload`` after a router restart (llm_backend's
``_apply_sync`` docstring, D3) is a DIFFERENT 400 on a DIFFERENT route. The
guard at the bottom of this file exists to catch a fix that swallows it.
"""
from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from litetui import llm_backend
from litetui import paths
from litetui.llm_backend import BackendError, LlamaCppBackend, LMStudioBackend
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
ROUTER_ERRORS = json.loads((FIXTURES / "llama_router_chat_errors_b9360.json").read_text())
SINGLE_MODELS = json.loads((FIXTURES / "llama_single_model_models_b9360.json").read_text())
SINGLE_PROPS = json.loads((FIXTURES / "llama_single_model_props_b9360.json").read_text())


class RouterStub:
    """Router /models with the spike's state machine: a model in `loading`
    flips to `loaded` after `loading_polls` reads."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        self.loading_polls = 0
        self.requests: list[tuple[str, str]] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                stub.requests.append(("GET", self.path))
                if self.path == "/health":
                    self._send({"status": "ok"})
                elif self.path == "/props":
                    self._send({"role": "router", "build_info": "b9360-stub"})
                elif self.path == "/models":
                    data = []
                    for key, m in stub.models.items():
                        if m["state"] == "loading":
                            m["polls"] += 1
                            if m["polls"] > stub.loading_polls:
                                m["state"] = "loaded"
                        data.append({"id": key,
                                     "status": {"value": m["state"], "args": []},
                                     "architecture": {"input_modalities": ["text"]}})
                    self._send({"data": data})
                else:
                    self._send({"error": "nope"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path))
                key = body.get("model")
                if self.path == "/models/load" and key in stub.models:
                    stub.models[key]["state"] = "loading"
                    stub.models[key]["polls"] = 0
                    self._send({"success": True})
                elif self.path == "/models/unload" and key in stub.models:
                    # The strictness that catches a fix which swallows the
                    # D3 400 (unload of a model this router never loaded).
                    if stub.models[key]["state"] == "unloaded":
                        self._send({"error": f"model {key!r} is not loaded"}, 400)
                    else:
                        stub.models[key]["state"] = "unloaded"
                        self._send({"success": True})
                else:
                    self._send({"error": f"unknown model {key!r}"}, 400)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def add(self, key: str, *, state="unloaded"):
        self.models[key] = {"state": state, "polls": 0}

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


class SingleStub:
    """A single-model server: one resident model, no load/unload routes."""

    def __init__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health":
                    self._send({"status": "ok"})
                elif self.path == "/models":
                    self._send(SINGLE_MODELS)
                elif self.path == "/props":
                    self._send(SINGLE_PROPS)
                else:
                    self._send({"error": "nope"}, 404)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


class LMStub:
    """LM Studio's native /api/v0/models. `loaded_context_length` present =
    the model is resident; absent = downloaded but cold."""

    def __init__(self, models):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                body = json.dumps({"data": models}).encode()
                code = 200 if self.path == "/api/v0/models" else 404
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture()
def router():
    s = RouterStub()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _sandboxed(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(paths, "LLAMA_DIR", tmp_path / ".llama")


def _run(coro):
    return asyncio.run(coro)


def _backend(host: str) -> LlamaCppBackend:
    s = Settings()
    s.llama_host = host
    s.llama_attach_hosts = []
    s.llama_scan_litesuite = False
    s.llama_scan_lmstudio = False
    s.llama_scan_hf_cache = False
    return LlamaCppBackend(s)


# ── provenance ───────────────────────────────────────────────────────────────

def test_the_raw_errors_this_replaces_are_the_ones_we_measured():
    """The plain-words mapping exists because THIS is what the server says.
    Pinning the captured bodies keeps that link load-bearing: if the engine
    stops saying it, this fails and the mapping gets revisited instead of
    silently drifting into a translation of an error nobody sends any more."""
    code, body = ROUTER_ERRORS["chat_unloaded"]
    assert code == 400 and "model is not loaded" in str(body)
    code, body = ROUTER_ERRORS["chat_unknown"]
    assert code == 400 and "not found" in str(body)
    code, body = ROUTER_ERRORS["chat_while_loading"]
    assert code == 503, (
        "a load in flight is a DIFFERENT status from the two 400s — that is "
        "why it is waited out rather than refused")


# ── the raw 400, in plain words ──────────────────────────────────────────────

def test_an_unloaded_model_is_told_to_load_first(router):
    """The defect: 400 'model is not loaded' reached the message widget."""
    router.add("m1", state="unloaded")
    b = _backend(router.host)
    _run(b.ensure_running())
    with pytest.raises(BackendError) as exc:
        _run(b.ensure_chat_ready("m1"))
    msg = str(exc.value)
    assert "m1" in msg
    assert "/load" in msg, f"tell the user the command that fixes it — got {msg!r}"
    assert "400" not in msg, "the raw status code is not plain words"


def test_a_load_already_in_flight_is_waited_out(router):
    """`loading` is the case that resolves by itself. Refusing it would send
    the user to /load a model that is already loading."""
    router.add("m1", state="loading")
    router.loading_polls = 3
    b = _backend(router.host)
    _run(b.ensure_running())
    _run(b.ensure_chat_ready("m1"))          # must not raise
    assert router.models["m1"]["state"] == "loaded"


def test_a_loaded_model_passes_without_loading_anything(router):
    """The no-implicit-load law: a readiness CHECK never loads weights."""
    router.add("m1", state="loaded")
    b = _backend(router.host)
    _run(b.ensure_running())
    _run(b.ensure_chat_ready("m1"))
    assert not [r for r in router.requests if r[0] == "POST"], \
        "a readiness check must not POST anything, least of all /models/load"


def test_a_model_the_server_does_not_have_is_named(router):
    router.add("m1", state="loaded")
    b = _backend(router.host)
    _run(b.ensure_running())
    with pytest.raises(BackendError) as exc:
        _run(b.ensure_chat_ready("ghost"))
    msg = str(exc.value)
    # T137: the model name stays (it is the action); the host URL no longer
    # rides in the user-facing line — it goes to the error sink.
    assert "ghost" in msg
    assert router.host not in msg
    assert "not loaded" not in msg, \
        "'absent' and 'cold' are different problems with different fixes"


def test_no_model_selected_is_its_own_message(router):
    b = _backend(router.host)
    _run(b.ensure_running())
    with pytest.raises(BackendError) as exc:
        _run(b.ensure_chat_ready(None))
    assert "/model" in str(exc.value)


def test_a_single_model_server_is_always_ready():
    """It serves one model and ignores the request's `model` field entirely
    (measured: a chat naming a nonexistent model still answered 200). There
    is nothing to load and nothing to refuse."""
    solo = SingleStub()
    try:
        b = _backend(solo.host)
        _run(b.ensure_running())
        _run(b.ensure_chat_ready(SINGLE_MODELS["data"][0]["id"]))   # no raise
    finally:
        solo.close()


# ── LM Studio: do not break what already works ───────────────────────────────

def test_lmstudio_cold_model_is_left_to_its_own_jit_load():
    """LM Studio JIT-loads on first request. Refusing a cold model here would
    break chats that work today — the fix must not be a new blocker."""
    lm = LMStub([{"id": "cold-one", "type": "llm", "max_context_length": 4096}])
    try:
        s = Settings()
        s.lm_host = lm.host
        _run(LMStudioBackend(s).ensure_chat_ready("cold-one"))      # no raise
    finally:
        lm.close()


def test_lmstudio_names_a_model_it_has_never_downloaded():
    lm = LMStub([{"id": "cold-one", "type": "llm", "max_context_length": 4096}])
    try:
        s = Settings()
        s.lm_host = lm.host
        with pytest.raises(BackendError) as exc:
            _run(LMStudioBackend(s).ensure_chat_ready("never-heard-of-it"))
        assert "never-heard-of-it" in str(exc.value)
    finally:
        lm.close()


# ── the 400 that must SURVIVE ────────────────────────────────────────────────

def test_the_unload_400_is_still_not_swallowed(router):
    """GUARD (green before the fix, must stay green after). D3: unloading a
    model the CURRENT router never loaded answers 400, and that 400 is real
    information — the apply-ordering regression is only visible because it
    surfaces. A readiness fix that catches 400s broadly would hide it."""
    router.add("m1", state="unloaded")
    b = _backend(router.host)
    _run(b.ensure_running())
    with pytest.raises(BackendError) as exc:
        _run(b.unload("m1"))
    assert "m1" in str(exc.value)
