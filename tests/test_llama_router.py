"""LlamaCppBackend against a scripted router stub — the protocol facts from
the live spike, replayed deterministically: /models status shapes, async
load (success=STARTED, poll until loaded), attach-vs-spawn precedence, the
attached-server refusals, and model_info's ceiling-vs-window contract.
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
from litetui import runtime_log
from litetui.llm_backend import BackendError, LlamaCppBackend
from litetui.settings import Settings


class RouterStub:
    """A llama-server router with scripted state. status transitions are the
    spike's: unloaded → loading → loaded, with `loading` lasting a
    configurable number of /models polls."""

    def __init__(self):
        self.models: dict[str, dict] = {}
        self.loading_polls = 0
        self.requests: list[tuple[str, str, dict | None]] = []
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
                stub.requests.append(("GET", self.path, None))
                if self.path == "/health":
                    self._send({"status": "ok"})
                elif self.path == "/models":
                    data = []
                    for key, m in stub.models.items():
                        if m["state"] == "loading":
                            m["polls"] += 1
                            if m["polls"] > stub.loading_polls:
                                m["state"] = "loaded"
                        data.append({
                            "id": key,
                            "status": {"value": m["state"], "args": m["args"]},
                            "architecture": {"input_modalities": m.get("mods", ["text"])},
                        })
                    self._send({"data": data})
                else:
                    self._send({"error": "nope"}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                stub.requests.append(("POST", self.path, body))
                key = body.get("model")
                if self.path == "/models/load" and key in stub.models:
                    stub.models[key]["state"] = "loading"
                    stub.models[key]["polls"] = 0
                    self._send({"success": True})
                elif self.path == "/models/unload" and key in stub.models:
                    # The real router 400s an unload of a model it has not
                    # loaded (proven live, 2026-08-21) — the stub must be as
                    # strict or the apply-ordering regression can't reproduce.
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

    def add(self, key: str, *, state="unloaded", ctx=None, mods=("text",)):
        args = ["llama-server.exe", "--alias", key]
        if ctx:
            args += ["--ctx-size", str(ctx)]
        self.models[key] = {"state": state, "args": args, "polls": 0, "mods": list(mods)}

    def close(self):
        self.server.shutdown()
        self.thread.join(timeout=5)


@pytest.fixture()
def stub():
    s = RouterStub()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _fast_and_sandboxed(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(paths, "LLAMA_DIR", tmp_path / ".llama")
    monkeypatch.setattr(
        llm_backend, "installed_flags", lambda: frozenset(llm_backend.FLAG_FOR.values())
    )


def _backend(stub: RouterStub, tmp_path: Path, *, models_dir: Path | None = None) -> LlamaCppBackend:
    s = Settings()
    s.llama_host = stub.host
    s.llama_attach_hosts = []
    s.llama_scan_litesuite = False
    s.llama_scan_lmstudio = False
    s.llama_scan_hf_cache = False
    s.llama_models_dirs = [str(models_dir)] if models_dir else []
    return LlamaCppBackend(s)


def _run(coro):
    return asyncio.run(coro)


def test_healthy_own_host_needs_no_spawn(stub, tmp_path):
    b = _backend(stub, tmp_path)
    assert _run(b.ensure_running()) == "ok"
    assert not b.attached


def test_attach_beats_spawn(stub, tmp_path):
    s = Settings()
    s.llama_host = "http://127.0.0.1:1"        # nothing there, and port 1 fails fast
    s.llama_attach_hosts = [stub.host]
    b = LlamaCppBackend(s)
    assert _run(b.ensure_running()) == f"attached {stub.host}"
    assert b.attached
    assert b.base_url() == f"{stub.host}/v1"   # the chat client follows the attach


def test_missing_exe_error_names_the_path(stub, tmp_path, monkeypatch):
    monkeypatch.setattr(llm_backend, "LLAMA_EXE", Path("Z:/nope/llama-server.exe"))
    s = Settings()
    s.llama_host = "http://127.0.0.1:1"
    s.llama_attach_hosts = []
    b = LlamaCppBackend(s)
    with pytest.raises(BackendError) as exc:
        _run(b.ensure_running())
    assert "Z:/nope" in str(exc.value).replace("\\", "/")
    assert "Model Hub" in str(exc.value)


def test_load_polls_until_loaded(stub, tmp_path):
    stub.add("m1", ctx=4096)
    stub.loading_polls = 3
    b = _backend(stub, tmp_path)
    _run(b.load("m1"))
    assert stub.models["m1"]["state"] == "loaded"
    # success=true was NOT trusted as loaded: /models was polled after.
    gets = [p for (v, p, _) in stub.requests if v == "GET" and p == "/models"]
    assert len(gets) >= 3


def test_model_info_ceiling_vs_window(stub, tmp_path):
    stub.add("hot", state="loaded", ctx=8192)
    stub.add("cold", ctx=262144)
    stub.add("seer", state="loaded", ctx=4096, mods=("text", "image"))
    b = _backend(stub, tmp_path)
    assert _run(b.model_info("hot")) == (8192, "llm", True)
    window, _type, loaded = _run(b.model_info("cold"))
    assert loaded is False, "a merely-registered model must never claim to be serving"
    assert _run(b.model_info("seer"))[1] == "vlm"
    assert _run(b.model_info("ghost")) is None


def test_unload_round_trip(stub, tmp_path):
    stub.add("m1", state="loaded")
    b = _backend(stub, tmp_path)
    _run(b.unload("m1"))
    assert stub.models["m1"]["state"] == "unloaded"


def test_attached_refuses_management(stub, tmp_path):
    s = Settings()
    s.llama_host = "http://127.0.0.1:1"
    s.llama_attach_hosts = [stub.host]
    b = LlamaCppBackend(s)
    _run(b.ensure_running())
    stub.add("m1")
    for coro in (b.load("m1"), b.unload("m1"), b.apply_load_settings("m1", {"ctx": 1})):
        with pytest.raises(BackendError) as exc:
            _run(coro)
        # ⚠️ THE WORDING CHANGED, THE RULE DID NOT. This used to assert
        # "LiteSuite owns" — a guess baked into the message, right often
        # enough to survive and wrong for every hand-started llama-server.
        # The owner is now read from `router.json`, and this stub wrote no
        # record, so it is honestly "another app". A record-less router is
        # still off limits exactly as before: coexistence is granted by a
        # signed claim, never by the shape of the server.
        assert "another app owns" in str(exc.value)
        assert "LiteSuite" not in str(exc.value)
        # T137: no URL in the user-facing line — which host was tried rides in
        # the error sink.
        assert stub.host not in str(exc.value)


def test_load_error_body_is_surfaced(stub, tmp_path):
    b = _backend(stub, tmp_path)
    with pytest.raises(BackendError) as exc:
        _run(b.load("ghost"))
    assert "ghost" in str(exc.value)


def test_list_merges_server_and_disk(stub, tmp_path):
    stub.add("served-only", state="loaded")
    models_dir = tmp_path / "disk"
    models_dir.mkdir()
    (models_dir / "disk-only.gguf").write_bytes(b"\0" * (20 * 1024 * 1024))
    b = _backend(stub, tmp_path, models_dir=models_dir)
    rows = {r.key: r for r in _run(b.list_models())}
    assert rows["served-only"].loaded is True
    assert rows["disk-only"].loaded is False
    assert rows["disk-only"].source == "custom"


def test_apply_on_a_loaded_model_survives_the_router_restart(stub, tmp_path, monkeypatch):
    """Ryan's manual pass, first apply to a LOADED model: _regen_ini restarts
    the router, the fresh process lists everything unloaded, and the old
    unload-AFTER-regen order sent a 400 that aborted the apply before the
    reload. The unload must happen while the OLD router still knows the
    model."""
    stub.add("m1", state="loaded", ctx=4096)
    b = _backend(stub, tmp_path)

    def _restart_evicts_everything():
        for m in stub.models.values():
            m["state"] = "unloaded"
    monkeypatch.setattr(b, "_regen_ini", _restart_evicts_everything)

    _run(b.apply_load_settings("m1", {"ctx": 8192}))   # must NOT raise
    assert stub.models["m1"]["state"] == "loaded", "apply must end with the model reloaded"


def test_load_fails_fast_when_the_worker_dies_at_argv(stub, tmp_path, monkeypatch):
    """A worker that dies parsing its arguments leaves the ROUTER reporting
    "loading" forever — measured live as an idle GPU and a 300s wait. Our own
    log has the truth; the poll must read it and fail NAMING the argument."""
    stub.add("m1")
    stub.loading_polls = 10**9   # the router never flips to loaded
    log_dir = tmp_path / ".llama"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "litetui-llama-server.log").write_text(
        '[60984] error while handling argument "--draft-max": the argument '
        "has been removed. use --spec-draft-n-max\n",
        encoding="utf-8",
    )
    seen = []
    monkeypatch.setattr(runtime_log, "record_error", lambda event, **kw: seen.append(kw))
    b = _backend(stub, tmp_path)
    with pytest.raises(BackendError) as exc:
        _run(b.load("m1"))
    msg = str(exc.value)
    # T137: the raw log line (naming --draft-max) lands in the error sink, not
    # the user-facing line; the line names the model and points at OUR OWN log.
    assert "--draft-max" not in msg
    assert "m1" in msg
    assert "litetui-llama-server.log" in msg
    assert any("--draft-max" in d.get("detail", "") for d in seen), \
        "the bad argument must still be identifiable — now via the sink"


def test_seat_cycle_on_router(stub, tmp_path):
    stub.add("seat", state="loaded", ctx=4096)
    b = _backend(stub, tmp_path)
    rec = b.seat_snapshot("seat")
    assert rec == {"identifier": "seat", "context": 4096, "parallel": None,
                   "status": "idle", "queued": 0}
    assert b.seat_suspend(rec) is None
    assert stub.models["seat"]["state"] == "unloaded"
    assert b.seat_snapshot("seat") is None
    assert b.seat_resume(rec) is None
    assert stub.models["seat"]["state"] == "loaded"


def test_seat_refusal_when_attached(stub, tmp_path):
    s = Settings()
    s.llama_host = "http://127.0.0.1:1"
    s.llama_attach_hosts = [stub.host]
    b = LlamaCppBackend(s)
    _run(b.ensure_running())
    stub.add("seat", state="loaded")
    assert b.seat_snapshot("seat") is None, "an unmanageable seat must read as not-suspendable"
    err = b.seat_suspend({"identifier": "seat"})
    # T137 wording: the owner is read from router.json (this stub wrote none),
    # so it is honestly "another app" — never a baked-in LiteSuite guess.
    assert err and "another app owns" in err
    assert "LiteSuite" not in err
