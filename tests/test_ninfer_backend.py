"""NInfer as a LiteTUI backend.

🔴 RYAN, 2026-09-17: *"LITETUI WAS ALWAYS THE END GOAL FOR NINFER and agents got
hung up on litesuites integration"*.

Every arm here states what it would catch. The three that matter most are the
ones a status-code handler, a hardcoded port, or a silent no-op would each pass.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from litetui.llm_backend import BackendError
from litetui.ninfer_backend import (
    NInferBackend,
    classify_ninfer_error,
    discover_ninfer_host,
    format_timings,
    ninfer_error_sentence,
)


class _Settings:
    lm_host = "http://127.0.0.1:1234"


def run(coro):
    """Same idiom as tests/test_llama_chat_ready.py — no plugin dependency."""
    return asyncio.run(coro)


# ── discovery ────────────────────────────────────────────────────────────────


def _write_config(tmp_path, endpoints) -> object:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "extraEndpoints": endpoints}), encoding="utf-8")
    return path


def test_finds_the_engine_litesuite_registered(tmp_path):
    """🔴 THE PORT IS ALLOCATED, SO IT IS READ AND NEVER GUESSED.

    `ninfer-serve` is started on whatever port was free, so there is no default
    to probe. A hardcoded 7480 would be right for one session and wrong for the
    next — wrong in the direction that looks like the engine is down.
    """
    cfg = _write_config(tmp_path, [{"baseUrl": "http://127.0.0.1:49962", "kind": "ninfer"}])
    assert discover_ninfer_host(cfg) == "http://127.0.0.1:49962"


def test_ignores_endpoints_that_are_not_ninfer(tmp_path):
    """⬜ CONTROL: the same list carries llama.cpp and LM Studio entries.

    Without the `kind` check this would attach to whatever was listed first and
    then fail in a way that reads as an NInfer bug.
    """
    cfg = _write_config(
        tmp_path,
        [
            {"baseUrl": "http://127.0.0.1:8088", "kind": "llama-server"},
            {"baseUrl": "http://127.0.0.1:49962", "kind": "ninfer"},
        ],
    )
    assert discover_ninfer_host(cfg) == "http://127.0.0.1:49962"


@pytest.mark.parametrize(
    "body",
    [
        '{"version":1}',                                  # no extraEndpoints
        '{"version":1,"extraEndpoints":[]}',              # registered nothing
        '{"version":1,"extraEndpoints":"nope"}',          # wrong type
        "not json at all",
    ],
)
def test_absence_is_an_answer_not_an_error(tmp_path, body):
    """⬜ NO ENGINE IS A STATE, NOT A FAULT.

    LiteSuite REMOVES the entry when the engine stops, so every shape here means
    "none is running" — which is exactly what a user who has not started one
    should be told. Raising would turn a normal state into a crash on boot.
    """
    path = tmp_path / "config.json"
    path.write_text(body, encoding="utf-8")
    assert discover_ninfer_host(path) is None


def test_a_missing_config_file_is_none(tmp_path):
    assert discover_ninfer_host(tmp_path / "absent.json") is None


# ── the error model ──────────────────────────────────────────────────────────


def test_service_unavailable_is_a_relaunch_not_a_retry():
    """🔴 THE ROW THE WHOLE TABLE EXISTS FOR.

    A status-code handler sees 503 and retries. The engine is DONE — the process
    has to come back from its launch config — so retrying produces a tight loop
    against a corpse. This is the lifecycle difference from llama.cpp and it is
    not guessable from the HTTP status.
    """
    code, action = classify_ninfer_error('{"code":"service_unavailable"}')
    assert code == "service_unavailable"
    assert action == "relaunch"


def test_queue_timeout_is_the_one_that_retries():
    """⬜ CONTROL: 503 has TWO meanings on this wire.

    `request_queue_timeout` really is a retry. If both mapped the same way the
    arm above would pass on a table that had collapsed them.
    """
    assert classify_ninfer_error('{"code":"request_queue_timeout"}')[1] == "retry"


@pytest.mark.parametrize(
    ("code", "action"),
    [
        ("context_length_exceeded", "trim"),
        ("server_overloaded", "backoff"),
    ],
)
def test_the_documented_codes_each_have_their_own_action(code, action):
    assert classify_ninfer_error({"code": code})[1] == action


def test_reads_the_code_nested_under_error_too():
    """⚠️ THE CODE SITS IN TWO SHAPES ON THIS WIRE — top level for the OpenAI
    dialect and under `error` for others. Reading one would make the table look
    right while firing on half the failures."""
    assert classify_ninfer_error('{"error":{"code":"server_overloaded"}}')[1] == "backoff"


@pytest.mark.parametrize("body", [None, "", "<html>", '{"message":"no code here"}', "[]"])
def test_an_unrecognised_body_is_not_forced_into_the_table(body):
    """⬜ (None, None) RATHER THAN A DEFAULT ACTION. Guessing "retry" for an
    error nobody classified is how a client loops on something it does not
    understand."""
    assert classify_ninfer_error(body) == (None, None)


def test_an_unknown_error_keeps_the_engines_own_words():
    """🔴 THE FALLBACK IS THE ENGINE'S SENTENCE, NOT OURS. A wrapper that
    replaced an unrecognised error with a generic line would delete the only
    description of a failure nobody has seen before."""
    assert ninfer_error_sentence('{"message":"top_k must be in [0,20]"}', "raw text") == "raw text"


def test_a_known_error_becomes_something_a_person_can_act_on():
    sentence = ninfer_error_sentence('{"code":"context_length_exceeded"}', "raw")
    assert "/compact" in sentence


# ── the timings line ─────────────────────────────────────────────────────────


def test_timings_become_the_status_line_ryan_asked_for():
    """🔴 *"66toks is less than lmstudio etc"* / *"whole point is a toks
    improvement"*. The engine reports the rate PER REQUEST, so the TUI can show
    what the turn actually ran at instead of a number measured once."""
    line = format_timings({"prompt_per_second": 812.4, "predicted_per_second": 151.2})
    assert line is not None
    assert "151.2 tok/s" in line
    assert "812 tok/s" in line


@pytest.mark.parametrize(
    "timings",
    [None, {}, "nope", {"predicted_per_second": 0}, {"predicted_per_second": "fast"}],
)
def test_no_timings_is_no_line_rather_than_zero(timings):
    """⬜ A STATUS LINE READING "0.0 tok/s" IS A MEASUREMENT. A missing one is
    the absence of a build that reports it, and the two must not look alike."""
    assert format_timings(timings) is None


# ── the control plane this engine does not have ──────────────────────────────


def test_load_refuses_by_name_rather_than_doing_nothing():
    """🔴 A SILENT NO-OP WOULD LET THE MODEL SCREEN LIE.

    Every knob — context ceiling, KV dtype, speculative backend, vision,
    concurrency — is a STARTUP flag and `ninfer-serve` cannot widen any of them
    afterwards. A control that accepted the change and did nothing is the same
    defect as a disabled button with no reason: the user believes it worked.
    """
    backend = NInferBackend(_Settings())
    with pytest.raises(BackendError) as excinfo:
        run(backend.load("qwen3.8-27b"))
    assert "Model Hub" in str(excinfo.value)


def test_apply_load_settings_refuses_too():
    backend = NInferBackend(_Settings())
    with pytest.raises(BackendError) as excinfo:
        run(backend.apply_load_settings("qwen3.8-27b", {"contextSize": 8192}))
    assert "fixed at startup" in str(excinfo.value)


def test_unload_refuses_rather_than_taking_the_engine_down():
    backend = NInferBackend(_Settings())
    with pytest.raises(BackendError):
        run(backend.unload("qwen3.8-27b"))


def test_shutdown_does_not_stop_an_engine_we_do_not_own():
    """🔴 THE ARM FOR RYAN'S STANDING RULE (a-5fd08920).

    LiteSuite owns the process, the approval and the VRAM. A `shutdown` that
    killed it would take down ~20 GiB that someone else authorised, from a TUI
    closing a tab. It clears our own handle and nothing else.
    """
    backend = NInferBackend(_Settings())
    backend._host = "http://127.0.0.1:49962"
    backend.shutdown()
    assert backend._host is None


def test_no_registered_engine_says_where_to_start_one(tmp_path, monkeypatch):
    """⬜ THE REFUSAL NAMES THE PLACE. "not running" is true and useless; the
    user has to be told the engine lives in LiteSuite and that LiteTUI will not
    start one.

    🔴 AND THE ENV IS REDIRECTED, BECAUSE THE FIRST VERSION READ RYAN'S REAL
    MACHINE. Without this the arm asks the box whether an engine happens to be
    running: it FAILED on the first run for the best possible reason — one was,
    registered at a live allocated port. A suite whose green is a property of
    one machine's state is the trap this repo has already recorded once.
    """
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path))
    backend = NInferBackend(_Settings())
    with pytest.raises(BackendError) as excinfo:
        run(backend.ensure_running())
    message = str(excinfo.value)
    assert "Model Hub" in message
    assert "never starts one" in message


def test_the_backend_is_always_attached():
    """🔴 THERE IS NO STATE IN WHICH THIS IS FALSE, and that is the design.

    The llama.cpp backend needs ownership rules because it spawns; a backend
    that cannot spawn cannot orphan VRAM. If this ever returns False, something
    has taught this class to start a process.
    """
    assert NInferBackend(_Settings()).attached is True


# ── the factory ──────────────────────────────────────────────────────────────


def test_the_factory_builds_it():
    """🔴 A BACKEND NOTHING CAN CONSTRUCT IS A FILE, NOT A FEATURE.

    `make_backend` is THE seam — app.py calls it at boot and again on
    `/backend`, and gui_rpc.py calls it too. A class that is never reachable
    from it would pass every arm above and be unusable from the app.
    """
    from litetui import llm_backend

    class S:
        backend = "ninfer"
        lm_host = "http://127.0.0.1:1234"

    made = llm_backend.make_backend(S())
    assert made.name == "ninfer"
    assert isinstance(made, NInferBackend)


def test_an_unknown_backend_still_names_the_valid_ones():
    """⬜ AND THE REFUSAL LISTS IT. The error text is the only place a user
    learns what they may type; adding a backend without adding it here makes
    the new one undiscoverable."""
    from litetui import llm_backend

    class S:
        backend = "not-a-backend"
        lm_host = "http://127.0.0.1:1234"

    with pytest.raises(BackendError) as excinfo:
        llm_backend.make_backend(S())
    assert "ninfer" in str(excinfo.value)


# ── the reuse seams (T806 delta) ─────────────────────────────────────────────


def test_the_engines_own_rate_is_one_function_not_two_parsers():
    """⬜ THE STATUS LINE AND THE tok/s FIELD READ THE SAME NUMBER.

    `format_timings` renders what `decode_rate_from_timings` returns. Two
    parsers over one object is the drift class this repo already records under
    "two counts of one thing that must agree".
    """
    from litetui.ninfer_backend import decode_rate_from_timings

    timings = {"predicted_per_second": 151.2, "prompt_per_second": 812.4}
    assert decode_rate_from_timings(timings) == pytest.approx(151.2)
    assert "151.2 tok/s" in (format_timings(timings) or "")


@pytest.mark.parametrize(
    "timings",
    [None, {}, "nope", {"predicted_per_second": 0}, {"predicted_per_second": True}],
)
def test_no_reported_rate_leaves_the_clients_estimate_standing(timings):
    """🔴 None, NOT ZERO — because the caller treats None as "I have nothing"
    and publishes its own arithmetic instead. A 0.0 would REPLACE a working
    estimate with a wrong one.

    `True` is in the list because `isinstance(True, int)` is True in Python: a
    bool would sail through a numeric check and publish a rate of 1.0.
    """
    from litetui.ninfer_backend import decode_rate_from_timings

    assert decode_rate_from_timings(timings) is None


def test_the_rate_publisher_prefers_the_engines_figure():
    """🔴 THE SEAM WAS ALREADY THERE AND ITS DOCSTRING SAID SO.

    `TpsState.final` divides the server's token count by the CLIENT's wall
    clock, so queueing and admission are charged to the model — a busy engine
    reads slower than it ran. Its own words are "settle to the exact figure the
    server reports"; the engine's decode-loop figure is a more exact one.
    """
    import time

    from litetui.turnstats import TpsState

    state = TpsState()
    state.t0 = time.monotonic() - 10.0  # ten seconds of wall clock
    # 100 tokens / 10 s = 10 tok/s by the clock; the engine says it ran at 151.2.
    assert state.final(100) == pytest.approx(10.0, rel=0.05)
    assert state.final(100, reported_rate=151.2) == pytest.approx(151.2)


def test_a_reported_rate_does_not_skip_the_existing_guards():
    """⬜ A TURN THAT NEVER STARTED, OR PRODUCED NOTHING, IS STILL NOTHING TO
    PUBLISH. The new argument is a better answer to the same question, not a
    way around the two conditions that were already right."""
    from litetui.turnstats import TpsState

    fresh = TpsState()  # t0 is None: no turn in flight
    assert fresh.final(100, reported_rate=151.2) is None

    import time

    started = TpsState()
    started.t0 = time.monotonic() - 1.0
    assert started.final(0, reported_rate=151.2) is None


def test_every_list_of_backend_names_agrees():
    """🔴 FOUR PLACES NAME THE BACKENDS AND NOTHING BOUND THEM.

    `_make_backend`'s branches, `/backend`'s accepted words, its picker rows and
    `gui_rpc`'s validator are four hand-written lists of one set. A backend the
    factory builds and the rpc refuses is selectable from the TUI and not from
    the GUI — the kind of split nobody notices until a user does, and exactly
    what `stt-provider-tables-agree` was written for one repo over.

    Source text is the instrument because none of the four is an exhaustive
    type: a green typecheck here means "nothing was type-linked".
    """
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "litetui"
    picker = (root / "plugins" / "model_switch.py").read_text(encoding="utf-8")
    rpc = (root / "gui_rpc.py").read_text(encoding="utf-8")
    factory = (root / "llm_backend.py").read_text(encoding="utf-8")

    assert '"ninfer"' in factory, "the factory cannot build it"
    assert '("ninfer", ' in picker, "the picker does not offer it"
    assert '"ninfer"' in rpc, "the rpc validator refuses what the factory builds"


def test_the_picker_reports_whether_an_engine_is_RUNNING(tmp_path, monkeypatch):
    """🔴 NOT "installed" — THE OTHER THREE ROWS' WORD IS THE WRONG ONE HERE.

    LiteTUI attaches and never starts NInfer, so an installed-but-stopped engine
    is not selectable in any useful sense. A row saying "installed" would send
    the user to a backend that refuses every turn.
    """
    from litetui.plugins.model_switch import _ninfer_mark

    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path))
    assert "not running" in _ninfer_mark()

    (tmp_path / "config.json").write_text(
        json.dumps({"extraEndpoints": [{"baseUrl": "http://127.0.0.1:49962", "kind": "ninfer"}]}),
        encoding="utf-8",
    )
    assert "49962" in _ninfer_mark()
