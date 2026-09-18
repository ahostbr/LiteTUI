"""T873 — /load and /modelcfg promised work that `_refuse_if_attached` refused.

The sibling of T865 (a689c2a), one command over. `_start_load` printed
"Loading <key>…" before awaiting `backend.load()`, and llamacpp's `_load_sync`
OPENS with `self._refuse_if_attached("load a model")`. On a server LiteTUI
adopted, or one started with a single `-m <gguf>`, the user read:

    Loading qwen…
    cannot load a model: that server is serving one model and cannot switch — …

🔴 THE ARMS HERE OBSERVE ORDER, NOT OUTCOME. Every final value is already
correct on the broken build: the refusal is raised, the message is printed, no
model is loaded. An assertion over "what did it end up saying" passes on both
builds. The only thing that differs is WHEN the promise was emitted relative to
the guard, so each arm records a SEQUENCE.

⬜ THE GUARD'S OWN COMMENT IS THE GENERALISABLE HALF. `_refuse_if_attached`
says: "Say WHY, and say it before the request goes out: /models/load is 404
here, and 'load refused — File Not Found' would blame the model for a property
of the server." The author reasoned explicitly about ordering — against the HTTP
REQUEST. Nobody was reasoning about the UI line one frame above it. Same file,
same concern, different axis.

⚠️ AND THE PLACEMENT IN `_apply_sync` IS LOAD-BEARING, WHICH IS WHAT
`test_an_adopted_router_never_hears_the_promise` exists to pin. Putting the
notice after `_refuse_if_attached` alone is the obvious fix and it is WRONG:
that guard RETURNS for an adopted router somebody signed for, and `_regen_ini`
refuses it two lines later. Only a notice below BOTH guards is honest.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from litetui import llm_backend, router_record
from litetui.plugins import model_switch
from litetui.settings import Settings

HOST = "http://127.0.0.1:7470"
PROMISE = "Loading"


# ── the command surface ─────────────────────────────────────────────────────

class _App:
    def __init__(self, backend):
        self.backend = backend
        self.settings = Settings()
        self.said: list[str] = []
        self.model_id = ""
        self.worker = None
        self.connected = 0

    def system_message(self, msg, *a, **k):
        self.said.append(str(msg))

    def call_from_thread(self, fn, *a, **k):
        return fn(*a, **k)

    def run_worker(self, coro, **kw):
        self.worker = coro

    def connect(self):
        self.connected += 1

    def fetch_context_window(self):
        pass


class _Backend:
    """A backend that refuses like llamacpp does — before any work."""

    name = "llamacpp"
    remote = False

    def __init__(self, *, refuse: str | None = None):
        self.refuse = refuse
        self.notice = None

    async def load(self, key, *, ctx=None, notice=None):
        self.notice = notice
        if self.refuse:                      # the guard, before the work
            raise llm_backend.BackendError(self.refuse)
        if notice is not None:
            notice()
        return None


def _drive_load(app, target="qwen"):
    model_switch._start_load(app, target)
    assert app.worker is not None
    asyncio.run(app.worker)


def test_a_refused_load_never_promises_one():
    """🔴 THE DEFECT. Red at 3f5ea7c, where the line is printed before the await."""
    app = _App(_Backend(refuse="cannot load a model: that server is serving one "
                               "model and cannot switch — …"))
    _drive_load(app)
    assert not any(PROMISE in s for s in app.said), (
        f"promised a load the backend then refused: {app.said}")
    assert any("serving one model" in s for s in app.said), app.said


def test_a_load_that_happens_still_says_it_is_loading():
    """🔴 THE CONTROL. Deleting the line also removes the contradiction and
    leaves a silent wait while weights load. The promise must survive here, and
    in order."""
    app = _App(_Backend())
    _drive_load(app)
    promises = [i for i, s in enumerate(app.said) if PROMISE in s]
    loaded = [i for i, s in enumerate(app.said) if s.startswith("Loaded:")]
    assert len(promises) == 1, app.said
    assert loaded and promises[0] < loaded[0], app.said


def test_the_command_hands_the_line_down_rather_than_printing_it():
    """The structural half: what stops the line drifting back above the await."""
    backend = _Backend(refuse="nope")
    app = _App(backend)
    _drive_load(app)
    assert callable(backend.notice), "no notice reached the backend"


# ── the launcher: llamacpp, where the guards actually live ──────────────────

@pytest.fixture(autouse=True)
def _record_in_a_tmp_home(tmp_path, monkeypatch):
    """Nothing here may read the developer's real router.json — it describes a
    router that is running on this machine."""
    path = tmp_path / "router.json"
    monkeypatch.setattr(router_record, "record_path", lambda: path)
    return path


def _llama(monkeypatch, *, shape=llm_backend._SHAPE_ROUTER):
    s = Settings()
    s.llama_host = HOST
    backend = llm_backend.LlamaCppBackend(s)
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: host.rstrip("/") == HOST)
    monkeypatch.setattr(backend, "_probe_shape", lambda: shape)
    return backend


def test_a_single_model_server_refuses_before_the_notice(monkeypatch):
    backend = _llama(monkeypatch, shape=llm_backend._SHAPE_SINGLE)
    backend._ensure_running_sync()
    order: list[str] = []
    with pytest.raises(llm_backend.BackendError):
        backend._load_sync("qwen", lambda: order.append("notice"))
    assert order == [], order


def test_the_notice_fires_immediately_before_the_load_request(monkeypatch):
    backend = _llama(monkeypatch)
    backend._attached_owner = None
    monkeypatch.setattr(backend, "attached", False)
    order: list[str] = []
    monkeypatch.setattr(backend, "_server_models",
                        lambda: {"qwen": {"status": {"value": "loaded"}}})
    monkeypatch.setattr(llm_backend, "_http_json",
                        lambda *a, **k: order.append("request") or {})
    backend._load_sync("qwen", lambda: order.append("notice"))
    assert order == ["notice", "request"], order


def test_an_adopted_router_never_hears_the_promise(_record_in_a_tmp_home, monkeypatch):
    """🔴 THE ARM THAT PINS THE PLACEMENT, and the obvious fix fails it.

    `_refuse_if_attached` RETURNS for a router somebody signed for — an adopted
    cooperating router is loadable on purpose — and `_regen_ini` then refuses
    the preset rewrite anyway. A notice placed after the FIRST guard would fire
    and be contradicted two lines later; only one below BOTH is honest.
    """
    router_record.write(pid=os.getpid(), port=7470, ini="theirs.ini",
                        owner="litesuite", path=_record_in_a_tmp_home)
    backend = _llama(monkeypatch)
    backend._ensure_running_sync()
    backend._refuse_if_attached("change load settings")   # must NOT raise

    order: list[str] = []
    with pytest.raises(llm_backend.BackendError) as excinfo:
        backend._apply_sync("qwen", {"ctx": 8192}, lambda: order.append("notice"))
    assert "preset" in str(excinfo.value)
    assert order == [], (
        "the promise was made between the two guards that can refuse it")


# ── LM Studio keeps its line: the backend that never had the defect ────────

def test_lmstudio_still_announces_because_the_caller_hands_one_notice(monkeypatch):
    """The caller passes ONE notice to whichever backend answers. LM Studio has
    no attached-server guard and was never wrong — but if it stopped firing the
    callback, its users would lose the line entirely and no arm above would
    notice."""
    s = Settings()
    backend = llm_backend.LMStudioBackend(s)
    order: list[str] = []

    class _LMS:
        def llm(self, key, config=None):
            order.append("llm")

    monkeypatch.setattr(backend, "_sdk", lambda: _LMS())
    monkeypatch.setattr(backend, "_record_load_settings", lambda *a, **k: None)
    asyncio.run(backend.load("qwen", notice=lambda: order.append("notice")))
    assert order == ["notice", "llm"], order
