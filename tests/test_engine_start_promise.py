"""T865 — `/engine start` promised a start it had not decided to make.

🔴 WHAT SENTINEL SAW, DRIVING THE exe-missing ARM (2026-09-17, T863):

    Starting ninfer-serve — weights take about ten seconds…
    ninfer-serve not installed at C:\\… — install it from LiteSuite's Model Hub,
    or set ninfer_executable.

Both sentences are correct in isolation. The end state is correct. Nothing
raised, nothing was left running, and 2,900-odd tests were green. The only thing
wrong is THE ORDER A PERSON READS THEM IN — and there is no assertion over a
final value that can see that, which is why this needed the third instrument:

    READING finds what matches your criterion.
    RUNNING finds what throws.
    DRIVING finds what a person is TOLD.

`start()` refuses for FIVE reasons before it spawns — an engine already
registered and answering, one registered and still loading, a full card, no
exe, no artifact — so the promise was wrong on all five, not just the one that
was driven.

FIX: `start(..., notice=...)`, called ONCE immediately before the spawn. The
line moves from the caller (which knows only that the user asked) into the
launcher (which knows the start is happening).

⚠️ THE ARM THAT MATTERS SECOND IS THE CONTROL. Deleting the message entirely
also removes the contradiction, and would pass an arm that only checks the
refusal path — while making a ten-second silent freeze. Both arms, always.
"""

from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest

from litetui import ninfer_engine as eng
from litetui.llm_backend import BackendError
from litetui.plugins import model_switch

PROMISE = "Starting ninfer-serve"


# ── the command surface ─────────────────────────────────────────────────────

class _App:
    """Only what `_cmd_engine` touches."""

    def __init__(self, backend):
        self.backend = backend
        self.said: list[str] = []
        self.connected = 0
        self.worker: object | None = None

    def system_message(self, msg, *a, **k):
        self.said.append(str(msg))

    def call_from_thread(self, fn, *a, **k):
        # The real one hops to the UI thread; here the ORDER is the subject and
        # it is preserved either way.
        return fn(*a, **k)

    def _chat_running(self) -> bool:
        return False

    def run_worker(self, coro, **kw):
        self.worker = coro

    def connect(self):
        self.connected += 1


class _Backend:
    """A ninfer backend whose start either refuses or proceeds."""

    name = "ninfer"

    def __init__(self, *, refuse: str | None = None):
        self.refuse = refuse
        self.notice = None

    async def start_engine(self, *, notice=None):
        self.notice = notice
        if self.refuse:
            raise BackendError(self.refuse)
        if notice is not None:      # what the launcher does, at the spawn
            notice()
        return "started ninfer-serve pid 777 at http://127.0.0.1:49260 (m)"


def _drive(app) -> None:
    model_switch._cmd_engine(app, "engine", "start")
    assert app.worker is not None, "the command did not start its worker"
    asyncio.run(app.worker)


# ── the command: promise only what you have decided to do ───────────────────

def test_a_refused_start_never_promises_one():
    """🔴 THE DEFECT. The refusal must not be preceded by its own contradiction."""
    app = _App(_Backend(refuse="ninfer-serve not installed at C:/nope.exe — "
                               "install it from LiteSuite's Model Hub, or set "
                               "ninfer_executable."))
    _drive(app)
    assert not any(PROMISE in s for s in app.said), (
        "the user was told a start was underway by a path that then refused: "
        f"{app.said}")
    assert any("not installed" in s for s in app.said), app.said
    assert app.connected == 0, "a refused start must not reconnect"


def test_a_start_that_happens_still_says_it_is_starting():
    """🔴 THE CONTROL, AND IT IS NOT OPTIONAL. Deleting the line also removes the
    contradiction — and leaves a ten-second silence where weights are loading.
    The promise must survive, in order, on the path that earns it."""
    app = _App(_Backend())
    _drive(app)
    promises = [i for i, s in enumerate(app.said) if PROMISE in s]
    started = [i for i, s in enumerate(app.said) if "started ninfer-serve" in s]
    assert len(promises) == 1, app.said
    assert started and promises[0] < started[0], app.said
    assert app.connected == 1, app.said


def test_the_promise_is_the_launchers_call_not_the_commands():
    """The command hands a callback down rather than printing on its own clock —
    the structural half of the fix, and what stops the line drifting back up."""
    app = _App(_Backend(refuse="nope"))
    _drive(app)
    assert callable(app.backend.notice), "no notice was handed to the launcher"


# ── the launcher: the callback fires at the spawn, and only there ───────────

class _Proc:
    pid = 777

    def poll(self):
        return None


def _artifact(tmp_path: Path) -> Path:
    body = json.dumps({"model_id": "m", "components": {"text": {}}}).encode()
    head = bytearray(eng.NINFER_V3_HEADER_BYTES)
    head[:8] = eng.NINFER_V3_MAGIC
    struct.pack_into("<Q", head, 8, len(body))
    p = tmp_path / "m.ninfer"
    p.write_bytes(bytes(head) + body)
    return p


class _Settings:
    backend = "ninfer"
    ninfer_host = ""
    ninfer_executable = ""
    ninfer_artifact = ""
    ninfer_max_context = 32768


def _startable(tmp_path, monkeypatch, order: list):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 30000)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    monkeypatch.setattr(eng, "free_port", lambda host="127.0.0.1": 49260)
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    exe = tmp_path / "ninfer" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    s = _Settings()
    s.ninfer_executable = str(exe)
    s.ninfer_artifact = str(_artifact(tmp_path))

    def spawn(cmd, **kw):
        order.append("spawn")
        kw["stdout"].write("listening on http://127.0.0.1:49260\n")
        kw["stdout"].flush()
        return _Proc()

    return s, spawn


def test_the_notice_fires_immediately_before_the_spawn(tmp_path, monkeypatch):
    order: list[str] = []
    s, spawn = _startable(tmp_path, monkeypatch, order)
    owned = eng.start(s, healthy=lambda h: False, spawn=spawn,
                      notice=lambda: order.append("notice"))
    assert order == ["notice", "spawn"], order
    eng.stop(owned)


def test_a_refusal_never_reaches_the_notice(tmp_path, monkeypatch):
    """The same mutation that matters for VRAM: a refusal spawns nothing — and
    now says nothing either."""
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(eng, "engine_process_alive", lambda: True)
    eng.register_host("http://127.0.0.1:7")
    order: list[str] = []
    with pytest.raises(BackendError):
        eng.start(_Settings(), healthy=lambda h: True,
                  spawn=lambda *a, **k: order.append("spawn"),
                  notice=lambda: order.append("notice"))
    assert order == [], order


def test_the_launcher_still_starts_without_a_notice(tmp_path, monkeypatch):
    """`notice` is optional: every existing caller passes nothing."""
    order: list[str] = []
    s, spawn = _startable(tmp_path, monkeypatch, order)
    owned = eng.start(s, healthy=lambda h: False, spawn=spawn)
    assert order == ["spawn"], order
    eng.stop(owned)
