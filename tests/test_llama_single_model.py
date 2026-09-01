"""D12 — a single-model llama-server is not the shape this module assumed.

Router mode is what WE spawn. A single-model ``llama-server -m <gguf>`` is
what LiteSuite runs, and it is the default attach target
(``settings.llama_attach_hosts`` → :8088). Its control surface is a different
SHAPE, not a different encoding, and every fixture here is transcribed from
the real b9360 binary rather than imagined (capture script + raw JSON in
``tests/fixtures/llama_single_model_*.json``):

  * ``GET /models`` carries an Ollama-shaped ``models`` array ALONGSIDE the
    OpenAI ``data`` array. The ``data`` entries have ``id``/``meta`` and
    **no ``status``** and **no ``architecture``** — the two keys router-mode
    parsing reads. A resident, serving model therefore parses as NOT loaded.
  * ``id`` is the model ALIAS, which defaults to the GGUF's file name WITH
    the ``.gguf`` suffix — so it never equals discovery's ``path.stem``, and
    the same model lists twice: once from the server, once from disk.
  * ``GET /props`` is the authority: ``model_alias``, ``model_path``,
    ``modalities.vision``, ``default_generation_settings.n_ctx`` (the LIVE
    window, not the ceiling). Router ``/props`` answers ``"role": "router"``;
    single-model ``/props`` has no ``role`` at all. That is the discriminator.
  * ``POST /models/load`` and ``POST /models/unload`` are **404 File Not
    Found**. The routes do not exist. Refusing to manage such a server is
    not caution, it is the only thing that can work.
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
from litetui.llm_backend import BackendError, LlamaCppBackend
from litetui.settings import Settings

FIXTURES = Path(__file__).parent / "fixtures"
MODELS_BODY = json.loads((FIXTURES / "llama_single_model_models_b9360.json").read_text())
PROPS_BODY = json.loads((FIXTURES / "llama_single_model_props_b9360.json").read_text())
ROUTER_PROPS = json.loads((FIXTURES / "llama_router_props_b9360.json").read_text())

#: What the real server calls it: the file name, suffix and all.
SERVED_ID = MODELS_BODY["data"][0]["id"]


class SingleModelStub:
    """Replays the captured b9360 single-model responses verbatim.

    /models/load and /models/unload answer 404 exactly as the real binary
    does — a test whose stub invents those routes would prove nothing about
    the server we are actually talking to.
    """

    def __init__(self, *, props=PROPS_BODY):
        self.requests: list[tuple[str, str]] = []
        self.props = props
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
                elif self.path == "/models":
                    self._send(MODELS_BODY)
                elif self.path == "/props":
                    self._send(stub.props)
                else:
                    self._send({"error": {"message": "File Not Found",
                                          "code": 404}}, 404)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(n)
                stub.requests.append(("POST", self.path))
                self._send({"error": {"message": "File Not Found",
                                      "type": "not_found_error", "code": 404}}, 404)

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
def single():
    s = SingleModelStub()
    yield s
    s.close()


@pytest.fixture(autouse=True)
def _sandboxed(monkeypatch, tmp_path):
    monkeypatch.setattr(llm_backend.time, "sleep", lambda s: None)
    monkeypatch.setattr(paths, "LLAMA_DIR", tmp_path / ".llama")


def _run(coro):
    return asyncio.run(coro)


def _settings(host: str, *, attach: list[str] | None = None,
              models_dir: Path | None = None) -> Settings:
    s = Settings()
    s.llama_host = host
    s.llama_attach_hosts = attach if attach is not None else []
    s.llama_scan_litesuite = False
    s.llama_scan_lmstudio = False
    s.llama_scan_hf_cache = False
    s.llama_models_dirs = [str(models_dir)] if models_dir else []
    return s


# ── the shape itself ─────────────────────────────────────────────────────────

def test_single_model_server_is_recognised_and_router_is_not():
    """The discriminator, both directions. A test that only proved the
    single-model case would pass just as well if we called EVERY server
    single-model — so the same code path runs against router /props too."""
    solo = SingleModelStub()
    routerish = SingleModelStub(props=ROUTER_PROPS)
    try:
        b = LlamaCppBackend(_settings(solo.host))
        _run(b.ensure_running())
        assert b.single_model is True

        b2 = LlamaCppBackend(_settings(routerish.host))
        _run(b2.ensure_running())
        assert b2.single_model is False, '/props role="router" is a router'
    finally:
        solo.close()
        routerish.close()


def test_listing_marks_the_resident_model_loaded(single):
    """The defect in one line: the model IS being served, and the row said
    ``loaded=False`` because a single-model /models entry has no ``status``."""
    b = LlamaCppBackend(_settings(single.host))
    _run(b.ensure_running())
    rows = _run(b.list_models())
    assert len(rows) == 1, f"one server, one model — got {[r.key for r in rows]}"
    row = rows[0]
    assert row.key == SERVED_ID
    assert row.loaded is True, "a resident, serving model must not list as cold"
    assert row.path == PROPS_BODY["model_path"], "path comes from /props, not a guess"
    assert row.source == "server"


def test_disk_models_are_not_offered_by_a_single_model_server(single, tmp_path):
    """A single-model server can serve exactly one model, forever. Listing the
    other GGUFs on the box offers the user a choice that cannot be honoured:
    picking one changes nothing and the next reply comes from the resident
    model anyway."""
    models_dir = tmp_path / "disk"
    models_dir.mkdir()
    (models_dir / "some-other-model.gguf").write_bytes(b"\0" * (20 * 1024 * 1024))
    b = LlamaCppBackend(_settings(single.host, models_dir=models_dir))
    _run(b.ensure_running())
    keys = [r.key for r in _run(b.list_models())]
    assert keys == [SERVED_ID], "only the served model may be offered"


def test_model_info_reports_the_live_window(single):
    """meta/props carry n_ctx (the window the server was started with) and
    n_ctx_train (the ceiling). The ceiling-vs-window lesson holds here too."""
    b = LlamaCppBackend(_settings(single.host))
    _run(b.ensure_running())
    want = PROPS_BODY["default_generation_settings"]["n_ctx"]
    assert _run(b.model_info(SERVED_ID)) == (want, "llm", True)


def test_model_info_is_none_for_a_model_this_server_does_not_serve(single):
    b = LlamaCppBackend(_settings(single.host))
    _run(b.ensure_running())
    assert _run(b.model_info("something-else")) is None


# ── attach state ─────────────────────────────────────────────────────────────

def test_a_single_model_server_on_our_own_port_is_attached(single):
    """The attach-state half of D12. `_ensure_running_sync`'s own comment said
    "Treat as attached: never kill what we cannot prove we own" while the code
    set `_attached_host = None`, so `attached` came out False — and every
    management refusal keyed on `attached` therefore did not fire.

    We only ever spawn ROUTER mode. A single-model server answering our port
    is by construction somebody else's process."""
    b = LlamaCppBackend(_settings(single.host))
    _run(b.ensure_running())
    assert b.attached is True
    assert b.host() == single.host
    assert b.base_url() == f"{single.host}/v1"


def test_management_is_refused_and_never_reaches_the_404_routes(single):
    """/models/load and /models/unload are 404 on the real binary. The refusal
    must name the situation BEFORE the request goes out — a 404 turned into
    "load failed" tells the user the model is broken, not that this server
    cannot switch models at all."""
    b = LlamaCppBackend(_settings(single.host))
    _run(b.ensure_running())
    before = len([r for r in single.requests if r[0] == "POST"])
    for coro in (b.load(SERVED_ID), b.unload(SERVED_ID),
                 b.apply_load_settings(SERVED_ID, {"ctx": 4096})):
        with pytest.raises(BackendError) as exc:
            _run(coro)
        msg = str(exc.value)
        # T137: no URL in the user-facing line — the situation (one model, no
        # switch route) is what must be named; which host was tried rides in
        # the error sink.
        assert single.host not in msg
        assert "one model" in msg, f"say WHY it cannot be done — got {msg!r}"
    assert len([r for r in single.requests if r[0] == "POST"]) == before, \
        "no request may be sent to a route that does not exist"


def test_seat_guard_does_not_try_to_suspend_a_single_model_server(single):
    """A GUARD, not a red test: this already passed before the fix, but for
    the wrong reason (the row parsed as not-loaded). It must keep passing for
    the right one — the server is unmanageable, so there is no seat to take."""
    b = LlamaCppBackend(_settings(single.host))
    _run(b.ensure_running())
    assert b.seat_snapshot(SERVED_ID) is None, \
        "an unmanageable seat must read as not-suspendable"
