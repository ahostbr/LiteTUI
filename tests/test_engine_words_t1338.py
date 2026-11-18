"""T1338 + T1339 — what the user reads while an engine starts.

A model started with --max-context 150000 --max-concurrency 2 needed 7.2 GiB more VRAM than the card
had. LiteTUI said only "exited before it was ready", then every send started it again; and the
failure text pasted the engine's own log (timestamps, a device-profile history with wattage and
dates) into the chat of a public app.

🔴 NO MODEL IS EVER LOADED HERE. The engine's output is a recorded log, replayed by a fake spawn;
the card is never touched.
"""
from __future__ import annotations

import asyncio
import re
import threading
import time
from types import SimpleNamespace

import pytest

from litetui import engine_autostart, engine_words
from litetui import ninfer_engine as eng
from litetui.llm_backend import BackendError
from litetui.ninfer_backend import NInferBackend
from litetui.plugins import model_switch

# A real ninfer-serve startup failure, recorded verbatim from its log.
REAL_FAILURE = """\
2026-11-17 20:01:15.323  INFO  build 2026-11-03.1+2026-11-03.1-15-g66931cd0-dirty
2026-11-17 20:01:15.351  INFO  starting engine
2026-11-17 20:01:15.632  INFO  engine | device profile nvidia-geforce-rtx-5090-sm120: 62 routed keys (built in: RTX 5090 at 575 W, 2026-09-25; unified routes and two-stage GDN at 450 W, 2026-09-27; Q4/Q5 MoE prefill tile at 450 W, 2026-10-15)
2026-11-17 20:01:16.214  INFO  loading weights | 20.8 GiB
2026-11-17 20:01:21.216  INFO  weights ready | 20.8 GiB | 5.0s | 4.16 GiB/s
2026-11-17 20:01:21.487  ERROR startup failed | finalizing target | 258 ms
2026-11-17 20:01:21.611  ERROR minimum Engine runtime reservation requires 7683126272 bytes in addition to 1073741824 bytes of automatic headroom, but only 6808113152 bytes are available after weights
"""
ARGS = ["--max-context", "150000", "--spec", "dflash", "--max-concurrency", "2"]

#: What must never reach the chat: a date, a clock time, a wattage, the device-profile history.
LEAKS = (re.compile(r"\b20\d\d-\d\d-\d\d\b"), re.compile(r"\b\d\d:\d\d:\d\d"), re.compile(r"\b\d{2,3} ?W\b"),
         re.compile(r"built in", re.I), re.compile(r"device profile", re.I), re.compile(r"\bINFO\b"),
         re.compile(r"\bERROR\b"), re.compile(r"\d{9,} bytes"))


def _assert_clean(text: str) -> None:
    for rx in LEAKS:
        assert not rx.search(text), (rx.pattern, text)


# ── the sentence ─────────────────────────────────────────────────────────────

def test_the_vram_failure_is_said_in_gib_with_the_settings_to_change():
    said, reason = engine_words.explain_failure(REAL_FAILURE, ARGS)
    assert said == ("Not enough VRAM: needs 7.2 GiB + 1 GiB headroom after the weights, 6.3 GiB free. "
                    "Lower the context (150000) or concurrency (2), or free GPU memory.")
    assert reason == engine_words.REASON_VRAM
    _assert_clean(said)


def test_a_vram_failure_without_the_flags_in_the_argv_still_says_what_to_lower():
    said, reason = engine_words.explain_failure(REAL_FAILURE, [])
    assert "Lower the context or concurrency" in said and reason == engine_words.REASON_VRAM


def test_another_engine_error_is_said_without_its_timestamp_or_timing_and_is_not_marked_repeatable():
    text = ("2026-11-17 21:00:00.100  INFO  starting engine\n"
            "2026-11-17 21:00:01.100  ERROR startup failed | loading weights | 12.5 ms\n"
            "2026-11-17 21:00:01.200  ERROR cannot open artifact: file is truncated\n")
    said, reason = engine_words.explain_failure(text, ARGS)
    assert said == "cannot open artifact: file is truncated" and reason is None
    _assert_clean(said)


def test_only_a_phase_line_is_said_as_a_phase_not_a_timing():
    said, _ = engine_words.explain_failure("2026-11-17 21:00:01.100  ERROR startup failed | finalizing target | 258 ms\n", [])
    assert said == "startup failed, finalizing target"


def test_no_error_line_at_all_is_still_a_sentence():
    said, reason = engine_words.explain_failure("2026-11-17 21:00:00.100  INFO  starting engine\n", [])
    assert said == "it stopped without saying why" and reason is None


@pytest.mark.parametrize("line, status", [
    ("loading weights | 20.8 GiB", "Loading weights 20.8 GiB"),
    ("loading weights 82.7% | 45.0 GiB/54.4 GiB | 1.2 GiB/s | ETA 2.0s", "Loading weights 82.7% (45.0 of 54.4 GiB)"),
    ("weights ready", "Weights loaded"),
    ("CUDA graphs ready", "GPU graphs ready"),
    ("engine ready", "Ready"),
])
def test_progress_lines_become_short_statuses(line, status):
    assert engine_words.plain_progress(line) == status


def test_clean_tail_drops_levels_and_info_lines():
    text = ("2026-11-17 21:00:00.100  INFO  device profile x: 62 routed keys (built in: RTX 5090 at 575 W, 2026-09-25)\n"
            "2026-11-17 21:00:01.200  ERROR model-00047 is missing\n")
    assert engine_words.clean_tail(text) == "model-00047 is missing"


# ── the start: what LiteTUI raises when the engine dies ──────────────────────

class _Settings:
    ninfer_host = ""
    ninfer_executable = ""
    ninfer_artifact = ""
    ninfer_max_context = 150000
    ninfer_max_concurrency = 2
    ninfer_kv_dtype = "fp8"
    ninfer_kv_capacity = "auto"
    ninfer_host_kv_mib = 0


class _Proc:
    pid = 4242

    def __init__(self):
        self.code = None

    def poll(self):
        return self.code


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv(eng.LITESUITE_LLM_DIR_ENV, str(tmp_path / "llm"))
    (tmp_path / "llm").mkdir()
    monkeypatch.setattr(eng.atexit, "register", lambda *a, **k: None)
    monkeypatch.setattr(eng, "gpu_free_mib", lambda: 30000)
    monkeypatch.setattr(eng, "engine_process_alive", lambda: False)
    exe = tmp_path / "bin" / "ninfer-serve.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"MZ")
    art = tmp_path / "models" / "swift15.ninfer"
    art.parent.mkdir()
    art.write_bytes(b"not a real artifact")
    s = _Settings()
    s.ninfer_executable, s.ninfer_artifact = str(exe), str(art)
    return SimpleNamespace(tmp=tmp_path, settings=s)


def _dying_spawn(output: str):
    """An 'engine' that prints `output` into the log and exits at once (the 20:01 failure)."""
    def spawn(cmd, **kw):
        out, proc = kw["stdout"], _Proc()
        out.write(output)
        out.flush()
        proc.code = 1
        return proc
    return spawn


def test_a_vram_short_start_raises_the_plain_sentence_and_marks_it_repeatable(box):
    with pytest.raises(eng.EngineStartFailed) as exc:
        eng.start(box.settings, healthy=lambda h: False, spawn=_dying_spawn(REAL_FAILURE))
    text = str(exc.value)
    assert text.startswith("Not enough VRAM: needs 7.2 GiB + 1 GiB headroom after the weights, 6.3 GiB free.")
    assert "(150000)" in text and "(2)" in text
    assert exc.value.reason == engine_words.REASON_VRAM
    _assert_clean(text)


def test_an_unexplained_exit_points_at_the_log_and_leaks_nothing(box):
    noisy = REAL_FAILURE.splitlines()[:5]            # INFO lines only, then the process dies
    with pytest.raises(eng.EngineStartFailed) as exc:
        eng.start(box.settings, healthy=lambda h: False, spawn=_dying_spawn("\n".join(noisy) + "\n"))
    text = str(exc.value)
    assert text.startswith("ninfer-serve could not start: it stopped without saying why. Engine log: ")
    assert exc.value.reason is None
    _assert_clean(text.split("Engine log:")[0])


def test_the_raw_engine_log_is_still_written_to_the_log_file(box):
    with pytest.raises(eng.EngineStartFailed):
        eng.start(box.settings, healthy=lambda h: False, spawn=_dying_spawn(REAL_FAILURE))
    logged = eng.log_path().read_text(encoding="utf-8")
    assert "device profile nvidia-geforce-rtx-5090-sm120" in logged and "7683126272 bytes" in logged


# ── a repeat of the same start is declined ───────────────────────────────────

class _App:
    """The slice of LiteTUI's app the send path and the start waiter touch."""

    def __init__(self, box, start):
        self.said, self.sent, self._pending_input = [], [], []
        self.backend = NInferBackend(box.settings)
        self.backend.name = "ninfer"
        self.settings = box.settings
        self.model_id = ""
        self._rpc = False
        self._spawned_marker = False
        self.input_box = SimpleNamespace(value="")
        self.starts = []
        self._start = start

    def system_message(self, text):
        self.said.append(text)

    notify = lambda self, *a, **k: None          # noqa: E731
    call_from_thread = lambda self, fn, *a: fn(*a)   # noqa: E731

    def query_one(self, *_a, **_k):
        return self.input_box


def _failing_backend(app, error):
    async def start_engine(notice=None, progress=None):
        app.starts.append(1)
        raise error
    app.backend.start_engine = start_engine


def _vram_error():
    return eng.EngineStartFailed("Not enough VRAM: needs 7.2 GiB + 1 GiB headroom after the weights, "
                                 "6.3 GiB free. Lower the context (150000) or concurrency (2), or free GPU memory.",
                                 reason=engine_words.REASON_VRAM)


def test_a_vram_failure_blocks_a_send_under_the_same_settings_and_gives_the_message_back(box):
    app = _App(box, None)
    _failing_backend(app, _vram_error())
    asyncio.run(model_switch.start_and_connect(app, app.backend))
    assert app.starts == [1] and app.said[-1].startswith("Not enough VRAM")

    words = engine_autostart.declined_words(app, "typed")
    assert words and "Not enough VRAM" in words and "/engine start" in words
    assert not engine_autostart.should_hold(app, "typed"), "a send would start the same failing engine again"
    engine_autostart.decline(app, {"text": "hello", "content": "hello"}, words)
    assert app.input_box.value == "hello" and app.starts == [1]


def test_changing_a_setting_unblocks_the_next_send(box):
    app = _App(box, None)
    _failing_backend(app, _vram_error())
    asyncio.run(model_switch.start_and_connect(app, app.backend))
    assert engine_autostart.declined_words(app, "typed")
    box.settings.ninfer_max_context = 32768
    assert engine_autostart.declined_words(app, "typed") is None
    assert engine_autostart.should_hold(app, "typed")


def test_an_explicit_engine_start_always_tries_again_and_a_success_clears_the_block(box):
    app = _App(box, None)
    _failing_backend(app, _vram_error())
    asyncio.run(model_switch.start_and_connect(app, app.backend))
    asyncio.run(model_switch.start_and_connect(app, app.backend))           # /engine start: not declined
    assert app.starts == [1, 1]

    async def ok(notice=None, progress=None):
        return "started"
    app.backend.start_engine = ok
    app.connect = lambda: None
    asyncio.run(model_switch.start_and_connect(app, app.backend))
    assert engine_autostart.declined_words(app, "typed") is None


def test_a_failure_that_may_not_repeat_never_blocks_a_send(box):
    app = _App(box, None)
    _failing_backend(app, eng.EngineStartFailed("ninfer-serve could not start: cannot open artifact. Engine log: x"))
    asyncio.run(model_switch.start_and_connect(app, app.backend))
    assert engine_autostart.declined_words(app, "typed") is None
    assert engine_autostart.should_hold(app, "typed")


def test_the_status_line_for_a_progress_step_is_a_status_not_a_log_line(box):
    app = _App(box, None)
    seen = []

    async def start_engine(notice=None, progress=None):
        progress("loading weights | 20.8 GiB")
        progress("weights ready")
        return "started"
    app.backend.start_engine = start_engine
    app.backend.label = "NInfer"
    app.system_message = lambda text: seen.append(text)
    app.connect = lambda: None
    asyncio.run(model_switch.start_and_connect(app, app.backend))
    assert seen[:2] == ["NInfer: Loading weights 20.8 GiB", "NInfer: Weights loaded"], seen
