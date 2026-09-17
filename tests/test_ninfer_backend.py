"""NInfer as a LiteTUI backend.

🔴 RYAN, 2026-09-17: *"LITETUI WAS ALWAYS THE END GOAL FOR NINFER and agents got
hung up on litesuites integration"*.

Every arm here states what it would catch. The three that matter most are the
ones a status-code handler, a hardcoded port, or a silent no-op would each pass.
"""

from __future__ import annotations

import asyncio
import json

import io
import pytest
from pathlib import Path

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
    assert "/engine start" in message          # Ryan a-35456da0: LiteTUI may start it


def test_an_explicit_ninfer_host_wins_over_discovery(tmp_path, monkeypatch):
    """T806 delta: a hand-started engine has nothing to register itself in, so
    settings.ninfer_host (LITETUI_NINFER_HOST) names it and discovery is not
    consulted. Env is redirected so the arm never reads the real machine."""
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path))  # no config -> discovery = None
    seen: list[str] = []
    monkeypatch.setattr(NInferBackend, "_health", staticmethod(lambda host, timeout=2.0: seen.append(host) or True))
    settings = _Settings()
    settings.ninfer_host = "http://127.0.0.1:49260/"
    backend = NInferBackend(settings)
    assert run(backend.ensure_running()) == "ok"
    assert seen == ["http://127.0.0.1:49260"]          # trailing slash stripped, discovery skipped
    assert backend.base_url() == "http://127.0.0.1:49260/v1"


def test_the_backend_is_attached_unless_it_started_the_engine():
    """Attached by default; owned only after /engine start (Ryan a-35456da0:
    "LiteTUI may start it"). The rule that survives: shutdown() stops ONLY an
    engine this backend started — an attached one is left alone.
    """
    from litetui import ninfer_engine

    backend = NInferBackend(_Settings())
    assert backend.attached is True
    backend.shutdown()                      # attached: nothing to stop, no error

    class _Proc:
        pid = 4242
        def poll(self):
            return None
    owned = ninfer_engine.OwnedEngine(proc=_Proc(), host="http://127.0.0.1:1", log_path=Path("x.log"),
                                      log_file=io.StringIO(), model_id="m")
    backend._owned = owned
    assert backend.attached is False
    stopped = []
    monkey = ninfer_engine.stop
    ninfer_engine.stop = lambda o: stopped.append(o.host)
    try:
        backend.shutdown()
    finally:
        ninfer_engine.stop = monkey
    assert stopped == ["http://127.0.0.1:1"] and backend.attached is True


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


# ── thinking (the feature Ryan named) ────────────────────────────────────────


def test_the_thinking_levels_come_from_the_engines_contract():
    """🔴 FOUR, NOT LiteTUI'S FIVE — read from `serving.md`, not from our own
    vocabulary. A recognised effort-capable template exposes low/medium/xhigh,
    and `none` disables thinking. `high` would 400 on the artifact we ship."""
    backend = NInferBackend(_Settings())
    assert backend.reasoning_levels("qwen3.8-27b") == ["none", "low", "medium", "xhigh"]
    assert "high" not in backend.reasoning_levels(None)
    assert "max" not in backend.reasoning_levels(None)


def test_thinking_capabilities_asks_the_backend_and_adds_no_ninfer_arm():
    """🔴 THE WHOLE INTEGRATION, AND IT IS A METHOD RATHER THAN A BRANCH.

    RYAN, 12:1x: *"were writing the same code to do the same thing in a slightly
    different way over and over for each backend."* `thinking_capabilities`
    checks `backend.reasoning_levels` FIRST and only falls through to name
    branches when a backend does not answer. This asserts the source string —
    "backend model metadata" — so an edit that added an `elif name == "ninfer"`
    beside the other two would still pass a levels-only assertion and fail here.
    """
    from litetui import thinking_capabilities as tc

    class App:
        backend = NInferBackend(_Settings())
        model_id = "qwen3.8-27b"
        settings = _Settings()

    caps = tc.thinking_capabilities(App())
    assert caps["source"] == "backend model metadata"
    # "none" is rendered as "off" by the shared resolver — not by us.
    assert caps["levels"] == ["default", "off", "low", "medium", "xhigh"]


def test_thinking_capabilities_has_no_ninfer_name_branch():
    """⬜ THE STRUCTURAL HALF OF THE ARM ABOVE. Source text, because a name
    branch that shadowed the seam would produce identical levels and the
    behavioural arm could not tell."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "src" / "litetui" / "thinking_capabilities.py"
    ).read_text(encoding="utf-8")
    assert "ninfer" not in src.lower()


def test_the_levels_are_sent_verbatim_and_off_becomes_none():
    """🔴 VERIFIED, NOT ASSUMED. `turn_engine._resolve_reasoning_effort` is the
    one place a level becomes a wire value, and ninfer takes the non-lmstudio
    arm: verbatim, with `off` -> `"none"`, which is the engine's own spelling
    for thinking disabled."""
    from litetui.turn_engine import _resolve_reasoning_effort

    assert _resolve_reasoning_effort("off", "ninfer") == "none"
    for level in ("low", "medium", "xhigh"):
        assert _resolve_reasoning_effort(level, "ninfer") == level
    assert _resolve_reasoning_effort(None, "ninfer") is None


def test_an_unsupported_level_is_named_rather_than_read_as_a_broken_engine():
    """🔴 THE ONLY WAY THE LEVEL SET EVER GETS NARROWED.

    Nothing advertises which efforts a loaded template exposes, so an effort it
    lacks comes back as a 400 BEFORE prompt preparation. Without its own
    sentence that reads as the engine failing, when it means "this model has no
    Extra High".
    """
    code, action = classify_ninfer_error('{"code":"reasoning_effort_not_supported"}')
    assert action == "thinking-level"
    assert "/thinking" in ninfer_error_sentence('{"code":"' + code + '"}', "raw")


def test_the_recovery_breadcrumb_does_not_hand_out_an_lms_command():
    """🔴 THE FALLBACK WAS A DECISION ABOUT US, AND IT WAS WRONG.

    `seat_guard._write_breadcrumb` writes the note a human finds when a resume
    failed and the agent's brain is gone. Its own docstring says the instruction
    "must match the ENGINE the seat lives on" — then it branched on `llamacpp`
    and sent everything else to LM Studio's CLI. An NInfer seat got `lms load`
    for a program that has never heard of a `.ninfer` artifact.
    """
    backend = NInferBackend(_Settings())
    hint = backend.reload_hint({"identifier": "qwen3.8-27b"})
    assert "lms" not in hint
    assert "Model Hub" in hint


def test_seat_guard_asks_the_backend_before_its_own_branches():
    """⬜ DUCK-TYPED, SO THE OTHER TWO BACKENDS ARE UNTOUCHED. A backend that
    does not answer keeps exactly the behaviour it had — which is why this reads
    the source rather than only the NInfer path: the ORDER is the fix."""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "src" / "litetui" / "seat_guard.py").read_text(
        encoding="utf-8"
    )
    asked = src.index('getattr(backend, "reload_hint", None)')
    branched = src.index('getattr(backend, "name", "") == "llamacpp"')
    assert asked < branched, "the name branch still wins over the backend's own answer"


def test_the_model_screen_labels_the_engine_it_is_actually_on():
    """🔴 A TWO-WAY LABEL IN A FOUR-BACKEND APP CALLED EVERYTHING ELSE "LM
    Studio". On NInfer the Model screen said LM Studio at the top while refusing
    every LM Studio verb underneath."""
    from litetui.llm_backend import BACKEND_NAMES, backend_label

    # The label comes from the ONE registry (llm_backend.BACKENDS), the same
    # table /backend, the Settings select and gui_rpc read — no fourth copy.
    assert "ninfer" in BACKEND_NAMES
    assert "NInfer" in backend_label("ninfer")
    assert backend_label("lmstudio") != backend_label("ninfer")
    assert backend_label("something-new") == "something-new"   # unknown degrades to its own name


def test_applying_load_settings_is_no_longer_a_silent_no_op():
    """🔴 TWO NAME BRANCHES MEANT EVERY OTHER BACKEND FELL OFF THE END.

    The settings were SAVED to disk and never sent anywhere, with no message
    either way — Ryan's *"all 3 buttons are unclickable"* complaint one app
    along, in its quieter form. The new arm asks the backend and shows what it
    says; NInfer's refusal is already a sentence naming where the control lives.

    Source text, because the failure is a MISSING branch: no behavioural arm can
    observe a path that does not execute.
    """
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "src" / "litetui" / "plugins" / "model_switch.py"
    ).read_text(encoding="utf-8")
    assert "elif load_cfg != prior_load:" in src, "backends without a name branch still no-op"
    # And the refusal it will surface is a real sentence, not an empty string.
    backend = NInferBackend(_Settings())
    with pytest.raises(BackendError) as excinfo:
        run(backend.apply_load_settings("qwen3.8-27b", {"ctx": 8192}))
    assert "restart the engine" in str(excinfo.value)


def test_base_url_never_raises_so_the_app_can_boot(tmp_path, monkeypatch):
    """🔴 THE APP COULD NOT BOOT ON THIS BACKEND AND NO UNIT ARM COULD SEE IT.

    `LiteTUI.__init__` builds its `AsyncOpenAI` client from `backend.base_url()`
    at CONSTRUCTION (app.py:1381), long before anything calls `ensure_running`.
    The first version raised there when no engine was registered, so selecting
    this backend with the engine down produced a TRACEBACK before the TUI
    existed — not a message.

        EVERY ARM CONSTRUCTED THE BACKEND DIRECTLY AND NONE BOOTED THE APP. The
        defect lived in the one line between the two, and driving a real turn is
        what found it.

    The other two backends cannot hit this: their host is a SETTING, present
    whether or not anything is listening. Ours is discovered, so absence is a
    state they never have.
    """
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path))
    backend = NInferBackend(_Settings())
    url = backend.base_url()  # must not raise
    assert url.endswith("/v1")
    # ⬜ A DEAD PORT, NOT A PLAUSIBLE ONE: port 0 cannot be connected to, so a
    # request cannot silently reach whatever else is listening locally.
    assert ":0/" in url

    # …and `host()` still refuses loudly, because its callers need a real one.
    with pytest.raises(BackendError):
        backend.host()


def test_the_host_is_resolved_lazily_so_a_later_start_is_picked_up(tmp_path, monkeypatch):
    """🔴 THE ENGINE OUTLIVES NEITHER SIDE'S ORDER. The app is constructed
    before `ensure_running`, and the engine may be started after LiteTUI is
    already open. Caching the answer at `__init__` would pin "no engine" for the
    life of the process — which is what a constructor-time read does."""
    monkeypatch.setenv("LITESUITE_LLM_DIR", str(tmp_path))
    backend = NInferBackend(_Settings())
    assert ":0/" in backend.base_url()

    (tmp_path / "config.json").write_text(
        json.dumps({"extraEndpoints": [{"baseUrl": "http://127.0.0.1:63177", "kind": "ninfer"}]}),
        encoding="utf-8",
    )
    assert backend.base_url() == "http://127.0.0.1:63177/v1"


# ── /v1/models: the shape the app unpacks ────────────────────────────────────


_MODELS_BODY = {"data": [{"id": "qwen3.8-27b", "max_model_len": 32768}]}


def _served(monkeypatch, body=_MODELS_BODY):
    b = NInferBackend(_Settings())
    b._host = "http://127.0.0.1:63177"
    monkeypatch.setattr(NInferBackend, "_get_json", lambda self, url, timeout=10.0: body)
    return b


def test_model_info_is_the_three_tuple_the_app_unpacks(monkeypatch):
    """🔴 THE ARM THAT WOULD HAVE CAUGHT THE APP DYING ON EVERY NINFER BOOT.

    `model_info` returned a four-key DICT. `app.py:4205` is
    `self.ctx_max, self.model_type, self.ctx_loaded = got`, so the `ctx` worker
    raised `ValueError: too many values to unpack (expected 3)` and killed the
    app moments after `connect()` had listed the model correctly — which
    surfaced only as `gui.state` reporting `"models": []`.

    The assertion is the UNPACK itself, in the app's own spelling, because that
    is the thing that broke: a dict of exactly three keys would satisfy a
    `len() == 3` check and still crash here differently.
    """
    b = _served(monkeypatch)
    got = run(b.model_info("qwen3.8-27b"))
    window, model_type, loaded = got            # app.py:4205, verbatim
    assert (window, loaded) == (32768, True)
    # No llm/vlm discriminator on this wire — None is what the engine said.
    assert model_type is None
    # app.py:4148 and :3418 index it; prove those readings too.
    assert got[2] is True and got[0] == 32768


def test_model_info_is_none_for_a_model_this_engine_does_not_serve(monkeypatch):
    """⬜ None, not an exception — the contract every other backend keeps.

    `app.py:4201` wraps the CALL in try/except but unpacks OUTSIDE it, so a
    raise here is survivable and a wrong SHAPE is not. Both siblings
    (`llm_backend.py:1582`, `:1151`) return None; so does this.
    """
    b = _served(monkeypatch)
    assert run(b.model_info("something-else")) is None


def test_list_models_reports_the_one_artifact_as_loaded(monkeypatch):
    """⬜ One artifact per process, resident from startup — `loaded` is a fact
    here, not a guess, and it is what `connect()` prefers when it picks."""
    b = _served(monkeypatch)
    rows = run(b.list_models())
    assert [(r.key, r.loaded, r.source) for r in rows] == [("qwen3.8-27b", True, "server")]


def test_list_models_refuses_an_engine_that_serves_nothing(monkeypatch):
    """⬜ An up-but-empty engine is a contradiction, not an empty catalogue:
    returning `[]` would put the app on the "no chat model available" path and
    blame the model folder for a serving fault."""
    b = _served(monkeypatch, {"data": []})
    with pytest.raises(BackendError):
        run(b.list_models())


# ── the surface the app calls without a guard ────────────────────────────────


#: Attributes reached on `backend.` that this backend is NOT required to have,
#: each with the reason it is exempt. An exemption list that grows is the
#: signal to stop exempting and start implementing.
_NOT_OURS = {
    # Codex-only: every reach is behind `name == "codex"` (app.py:1644) or the
    # native-loop branch (app.py:6490, model_transport.py:634).
    "app_server",
    # `@property.setter` — a decorator artifact in the source, not a call.
    "setter",
    # llama.cpp's owned-server recovery, and app.py:3480 wraps it in try/except
    # precisely because not every backend has one.
    "recover_owner_exit",
}


def test_every_unguarded_backend_call_is_implemented():
    """🔴 THE ARM FOR THE WHOLE CLASS, NOT FOR ONE MISSING METHOD.

    `app.py:5901` is `self.backend.request_overrides(self.model_id)` with NO
    `hasattr` guard, so omitting it did not degrade — it raised
    `AttributeError: 'NInferBackend' object has no attribute
    'request_overrides'` from inside the chat worker and killed the FIRST turn
    ever driven through this backend. 57 arms were green at that moment,
    because every one of them called a method that existed.

        A MISSING METHOD IS NOT A MISSING FEATURE. It is a crash, and the
        surface is DERIVABLE — so derive it, rather than waiting for each one
        to fire in turn. The same sweep found `seat_snapshot`, `seat_suspend`
        and `seat_resume` missing on the image/audio VRAM path
        (`studio_tool.py:262`, `listen_tool.py:415`).

    ⚠️ THIS ARM READS SOURCE, SO IT IS SCOPED TO WHAT IT READS — AND ITS SCOPE
    HAS LET ONE THROUGH ALREADY. Two known blind spots, both measured, not
    imagined:

      1. AN ALIAS. `b = self.backend; b.foo()` does not match the pattern.
      2. 🔴 `getattr`. `model_residency.py:63` is
         `getattr(backend, "loaded_models", None)`, so the missing
         `loaded_models` did NOT show up here — this arm stayed green while
         Ryan's `/settings` raised. Verified by deleting the method: 61 passed.

    Blind spot 2 cannot simply be pattern-matched away, because `getattr` with
    a default is ALSO how a genuinely optional capability is read (`app_server`,
    `reload_hint`), and flagging those would make this arm cry wolf until
    somebody widened the exemption list instead of the implementation.

        SO THE STRUCTURAL PROTECTION FOR A `getattr` SEAM IS NOT THIS ARM — IT
        IS MAKING THE FALLBACK SURVIVABLE. `model_residency` now guards its
        `asyncio.run` fallback (`tests/test_residency_loop_guard.py`), so a
        backend missing that method degrades to the snapshot instead of
        crashing the dialog. A specific arm below pins the method itself.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src" / "litetui"
    called: dict[str, str] = {}
    # 🔴 THIS LIST IS THE ARM'S SCOPE, AND IT IS WHERE ONE GOT THROUGH.
    # `loaded_models` was missing and this arm passed, because
    # `model_residency.py` and `plugins/settings_ui.py` were not read -- Ryan
    # found it by typing `/settings`. Widened; a file added to the turn or
    # dialog path must be added here too, or the arm silently narrows.
    for rel in ("app.py", "turn_engine.py", "gui_rpc.py", "seat_guard.py",
                "thinking_capabilities.py", "plugins/model_switch.py",
                "model_residency.py", "plugins/settings_ui.py",
                "listen_tool.py", "studio_tool.py"):
        path = root / rel
        if not path.exists():
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in re.finditer(r"\bbackend\.([A-Za-z_]\w*)", line):
                called.setdefault(m.group(1), f"{rel}:{n}")

    missing = {a: where for a, where in called.items()
               if a not in _NOT_OURS and not hasattr(NInferBackend, a)}
    assert not missing, (
        "NInferBackend is missing attributes the app calls unguarded: "
        + ", ".join(f"{a} ({where})" for a, where in sorted(missing.items()))
    )


def test_request_overrides_is_the_siblings_merge_not_a_third_spelling(monkeypatch):
    """⬜ Global sampling defaults + this model's Inference-tab overrides, and
    an override of None REMOVES the global rather than sending null — the rule
    `_merged_overrides` already encodes for the other two backends."""
    from litetui.settings import Settings

    st = Settings()
    st.model_infer_overrides = {"qwen3.8-27b": {"top_k": 7, "temperature": None}}
    b = NInferBackend(st)
    got = b.request_overrides("qwen3.8-27b")
    assert got.get("top_k") == 7
    assert "temperature" not in got


def test_the_seat_cannot_be_suspended_and_says_why(monkeypatch):
    """🔴 REFUSE BY NAME; NEVER REPORT VRAM FREED THAT IS NOT.

    `seat_guard.suspend` hands this sentence to `studio_tool`/`listen_tool`,
    which then decide whether to run anyway. Returning None would claim the
    card was freed and let an image job start against a full one.
    """
    b = _served(monkeypatch)
    err = b.seat_suspend({"identifier": "qwen3.8-27b"})
    assert err and "life of the process" in err


def test_seat_snapshot_is_resident_or_absent(monkeypatch):
    """⬜ One artifact per process: "is it loaded" and "is this the model this
    engine serves" are the same question, so there is no cold row to report."""
    b = _served(monkeypatch)
    rec = b.seat_snapshot("qwen3.8-27b")
    assert rec == {"identifier": "qwen3.8-27b", "context": 32768,
                   "parallel": None, "status": "idle", "queued": 0}
    assert b.seat_snapshot("something-else") is None
    # Resume is a question, not an assumption — it asks the engine.
    assert b.seat_resume({"identifier": "qwen3.8-27b"}) is None
    assert "no longer serving" in (b.seat_resume({"identifier": "gone"}) or "")


def test_loaded_models_exists_because_the_residency_read_is_a_getattr():
    """🔴 NAMED EXPLICITLY, BECAUSE THE DERIVED ARM ABOVE CANNOT SEE IT.

    `model_residency.resident_models` reads this with
    `getattr(backend, "loaded_models", None)` (`model_residency.py:63`) and
    falls back to `asyncio.run(list_models())` — which raises inside Textual's
    loop. Ryan typed `/settings` on this backend and got that traceback instead
    of the dialog.

    ⬜ SYNC ON PURPOSE. The caller is a command handler; an async answer is the
    one thing it cannot use.
    """
    import inspect

    from litetui import model_residency

    assert hasattr(NInferBackend, "loaded_models")
    assert not inspect.iscoroutinefunction(NInferBackend.loaded_models)
    # The spelling the caller actually uses, so a rename on either side is red.
    assert getattr(NInferBackend, "loaded_models", None) is not None
    assert "loaded_models" in inspect.getsource(model_residency.resident_models)


def test_loaded_models_is_the_served_row_and_survives_a_dead_engine(monkeypatch):
    """⬜ One artifact per process, so the served id IS the resident set; and a
    backend that cannot reach its engine reports NOTHING rather than raising
    into a dialog that is already being built."""
    assert _served(monkeypatch).loaded_models() == ["qwen3.8-27b"]

    dead = NInferBackend(_Settings())
    dead._host = "http://127.0.0.1:1"

    def refuse(self, url, timeout=10.0):
        raise BackendError("engine is down")

    monkeypatch.setattr(NInferBackend, "_get_json", refuse)
    assert dead.loaded_models() == []
