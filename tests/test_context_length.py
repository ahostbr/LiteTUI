"""The context the app believes must be the context the model has.

Ryan: "when it loads a model the context set in settings isnt being respected
keeps default to 8k".

Three faults, stacked, and the middle one is why fixing the obvious one alone
would not have worked:

  1. NOTHING APPLIED THE SETTING ON A MODEL SWITCH. `_apply_context_length()`
     had exactly one caller — a /settings save where the value CHANGED. Boot
     deliberately does not call it (that path used to load a 27B on every app
     start and every test). So picking a model just set `model_id` and let LM
     Studio JIT-load it at the server default. That is the 8k.

  2. A NOT-LOADED MODEL REPORTED ITS CEILING AS ITS WINDOW. The reader took
     `loaded_context_length or max_context_length`, so a merely-installed model
     answered 262,144 — a number the session does not have. A switch-time apply
     that trusted it would conclude the window already exceeded 120,000 and
     load nothing, so fault 1 could not be fixed without this.

  3. THE NO-OP GUARD COULD NEVER FIRE. It compared the READOUT to the REQUEST,
     and LM Studio clamps: ask 120,000, get 120,064.

And the number from fault 2 fed `_maybe_autocompact`, which DIVIDES by it: 80%
of 262,144 is 209,715 tokens, unreachable inside an 8k window. Auto-compact
would never fire and the model would blow its real context instead.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

import app as app_mod
from settings import Settings

app_mod.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-ctx-"))

LOADED, CEILING = 120064, 262144


class _Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd) if isinstance(cmd, (list, tuple)) else [str(cmd)])

        class _P:
            returncode = 0
            stdout = stderr = ""

        return _P()

    @property
    def load_calls(self) -> list[list[str]]:
        return [c for c in self.calls if len(c) >= 2 and c[0] == "lms" and c[1] == "load"]


@pytest.fixture
def recorder(monkeypatch):
    r = _Recorder()
    monkeypatch.setattr(app_mod.ttyguard, "run", r)
    return r


def _app(ctx=120000):
    a = app_mod.LiteTUI()
    a.settings = Settings(default_model="qwen/qwen3.8-27b", default_context_length=ctx)
    a.model_id = "qwen/qwen3.8-27b"
    a._system = lambda *_a, **_k: None
    a._fetch_ctx_window = lambda: None
    return a


# ── fault 2: a ceiling is not a window ────────────────────────────────────
def test_a_not_loaded_model_is_reported_as_not_loaded(monkeypatch):
    payload = {"data": [{"id": "m", "max_context_length": CEILING, "type": "llm"}]}
    monkeypatch.setattr(app_mod.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(payload))
    got = app_mod.LiteTUI._read_model_info("m")
    assert got == (CEILING, "llm", False), got
    assert got[2] is False, "a ceiling must never be reported as a loaded window"


def test_a_loaded_model_reports_its_loaded_window(monkeypatch):
    payload = {"data": [{"id": "m", "loaded_context_length": LOADED,
                         "max_context_length": CEILING, "type": "llm"}]}
    monkeypatch.setattr(app_mod.urllib.request, "urlopen",
                        lambda *a, **k: _FakeResp(payload))
    assert app_mod.LiteTUI._read_model_info("m") == (LOADED, "llm", True)


class _FakeResp:
    def __init__(self, payload):
        import json as _j
        self._b = _j.dumps(payload).encode()

    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self): return self._b


# ── auto-compact must not divide by a ceiling ─────────────────────────────
def test_autocompact_refuses_when_the_window_is_only_a_ceiling():
    a = _app()
    fired = []
    a._handle_command = lambda c: fired.append(c)
    a.ctx_max, a.ctx_used, a.ctx_loaded = CEILING, 250_000, False
    a._maybe_autocompact()
    assert fired == [], "compacted against a window the session does not have"


def test_control_autocompact_DOES_fire_on_a_real_loaded_window():
    """Proves the refusal above is about `ctx_loaded`, not about the numbers.

    Without this, the test above would pass just as well if auto-compact had
    been broken outright — which is the failure mode the refusal risks
    introducing: turning a wrong number into no number.
    """
    a = _app()
    fired = []
    a._handle_command = lambda c: fired.append(c)
    a.ctx_max, a.ctx_used, a.ctx_loaded = LOADED, int(LOADED * 0.95), True
    a._maybe_autocompact()
    assert fired == ["/compact"], fired


def test_a_stale_window_is_re_read_once_the_model_is_resident():
    """Refusing to guess must not become refusing to ever fire.

    At boot the model is often not loaded, so the threshold correctly declines.
    The first message then makes LM Studio load it — and if nothing asks again,
    ctx_loaded stays False for the whole session and auto-compact never runs.
    """
    a = _app()
    asked = []
    a._fetch_ctx_window = lambda: asked.append(True)

    a.ctx_loaded = False
    a._resync_ctx_if_stale()
    assert asked, "a stale window was never re-read"

    asked.clear()
    a.ctx_loaded = True
    a._resync_ctx_if_stale()
    assert asked == [], "a known-good window must not be re-read every turn"


# ── fault 3: the clamp ────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_an_already_satisfied_window_is_not_reloaded(recorder, monkeypatch):
    """120,064 satisfies a request for 120,000. Reloading evicts 17GB to
    change nothing — and the OLD guard (readout == request) could never see
    this case, because LM Studio clamps."""
    monkeypatch.setattr(app_mod.LiteTUI, "_read_model_info",
                        staticmethod(lambda mid: (LOADED, "llm", True)))
    a = _app(ctx=120000)
    async with a.run_test() as pilot:
        a._apply_context_length()
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert recorder.load_calls == [], recorder.load_calls


@pytest.mark.asyncio
async def test_a_not_loaded_model_IS_loaded_at_the_configured_window(recorder, monkeypatch):
    """The actual bug: nothing applied the setting, so LM Studio JIT-loaded at
    its own default."""
    monkeypatch.setattr(app_mod.LiteTUI, "_read_model_info",
                        staticmethod(lambda mid: (CEILING, "llm", False)))
    a = _app(ctx=120000)
    async with a.run_test() as pilot:
        a._apply_context_length()
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert len(recorder.load_calls) == 1, recorder.calls
    argv = recorder.load_calls[0]
    assert "--context-length" in argv and "120000" in argv, argv


@pytest.mark.asyncio
async def test_force_lowers_a_window_that_already_exceeds_the_request(recorder, monkeypatch):
    """A /settings change is an instruction, not a suggestion.

    "at least as much is fine" is right for a model switch and WRONG here:
    lowering the window to free VRAM is a legitimate thing to ask for, and a
    >= guard would silently ignore it.
    """
    monkeypatch.setattr(app_mod.LiteTUI, "_read_model_info",
                        staticmethod(lambda mid: (LOADED, "llm", True)))
    a = _app(ctx=32000)
    async with a.run_test() as pilot:
        a._apply_context_length(force=True)
        for _ in range(8):
            await pilot.pause()
            await asyncio.sleep(0.05)
    assert len(recorder.load_calls) == 1, recorder.calls
    assert "32000" in recorder.load_calls[0]


# ── fault 1: the switch applies it ────────────────────────────────────────
def test_every_explicit_model_switch_applies_the_setting():
    """Source-level, deliberately: the three switch paths (/model <n>,
    /model <name>, and the picker callback) are three separate code paths and
    the bug was one of omission. A behavioural test on one of them would leave
    the other two free to regress.

    Boot is asserted NOT to apply, by the same reading — that rule is what
    stopped the app loading a 27B on every start.
    """
    src = Path(app_mod.__file__).read_text(encoding="utf-8")
    switch = src.count('self._system(f"Switched to: {self.model_id}")')
    assert switch == 3, f"expected 3 switch sites, found {switch}"

    # Each one is followed by an apply.
    for chunk in src.split('self._system(f"Switched to: {self.model_id}")')[1:]:
        head = chunk[:400]
        assert "_apply_context_length()" in head, (
            "a model switch that does not apply the configured context length "
            "leaves LM Studio to JIT-load at its own default"
        )

    connect = src.split("Connected — model:", 1)[0][-1200:]
    assert "_apply_context_length()" not in connect.replace(
        "# _apply_context_length()", ""), "connect must never load a model"
