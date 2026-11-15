"""T1318: the Strata/Xolotl thinking budget, sent per request as `reasoning_budget_tokens` (serve/server.py
`reasoning_budget`: the request's key, else the config's; 0 = none). The setting `strata_reasoning_budget_tokens` is the TOP
budget N; the think level scales it (xhigh/max/ultra and an omitted level N, high 3N/4, medium N/2, low and minimal
N/4, off none), read BEFORE Strata's fold of xhigh to high. The answer room is always reserved on top:
max_tokens = budget + the max_tokens the request would have sent (T1312's rule for NInfer, which keeps its one budget)."""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from litetui.settings import Settings
from litetui.turn_engine import TurnEngine

SRC = Path(__file__).resolve().parents[1] / "src" / "litetui"
N = 16384
KEY = "reasoning_budget_tokens"


def _chat(**over):
    args = dict(model_id="m", messages=[{"role": "user", "content": "hi"}], tools_enabled=True,
                max_tokens_tools=16384, max_tokens_chat=222, request_overrides={}, thinking_level=None, tools=[])
    args.update(over)
    return TurnEngine.chat_request(**args)


def _compact(**over):
    args = dict(model_id="m", messages=[{"role": "user", "content": "sum"}], max_tokens=333, thinking_level="low",
                tools_enabled=False, tools=None)
    args.update(over)
    return TurnEngine.compact_request(**args)


#: (think level, reasoning_budget_tokens, max_tokens) for N = 16384 and max_tokens_tools = 16384.
TABLE = [
    ("xhigh", 16384, 32768), ("max", 16384, 32768), ("ultra", 16384, 32768), (None, 16384, 32768),
    ("high", 12288, 28672), ("medium", 8192, 24576), ("low", 4096, 20480), ("minimal", 4096, 20480),
]


@pytest.mark.parametrize("backend", ["strata", "xolotl"])
@pytest.mark.parametrize("level, budget, max_tokens", TABLE)
def test_the_budget_follows_the_think_level_and_the_answer_room_is_always_added(backend, level, budget, max_tokens):
    k = _chat(backend_name=backend, thinking_budget=N, thinking_level=level)
    assert k["extra_body"][KEY] == budget
    assert k["max_tokens"] == max_tokens == 16384 + budget
    assert "thinking_budget" not in k["extra_body"]          # NInfer's key, not Strata's


def test_xhigh_and_high_get_different_budgets_though_strata_folds_both_onto_high():
    xhigh = _chat(backend_name="strata", thinking_budget=N, thinking_level="xhigh")
    high = _chat(backend_name="strata", thinking_budget=N, thinking_level="high")
    assert xhigh["extra_body"]["reasoning_effort"] == high["extra_body"]["reasoning_effort"] == "high"   # the fold
    assert (xhigh["extra_body"][KEY], high["extra_body"][KEY]) == (16384, 12288)
    assert (xhigh["max_tokens"], high["max_tokens"]) == (32768, 28672)


@pytest.mark.parametrize("backend", ["strata", "xolotl"])
@pytest.mark.parametrize("off", [
    dict(thinking_level="off"),
    dict(request_overrides={"enable_thinking": False}),
    dict(request_overrides={"reasoning_effort": "none"}),
])
def test_thinking_off_gets_neither_the_key_nor_the_room(backend, off):
    k = _chat(backend_name=backend, thinking_budget=N, **off)
    assert KEY not in (k.get("extra_body") or {})
    assert k["max_tokens"] == 16384


@pytest.mark.parametrize("build", [_chat, _compact])
@pytest.mark.parametrize("budget", [None, 0, -5])
def test_no_budget_means_no_key_and_no_extra_room(build, budget):
    base = build(backend_name="strata")
    got = build(backend_name="strata", thinking_budget=budget)
    assert KEY not in (got.get("extra_body") or {})
    assert got["max_tokens"] == base["max_tokens"]


def test_a_per_model_max_tokens_override_and_the_chat_value_are_the_bases_that_are_raised():
    assert _chat(backend_name="strata", thinking_budget=1000, thinking_level="xhigh",
                 request_overrides={"max_tokens": 4000})["max_tokens"] == 5000
    assert _chat(backend_name="strata", thinking_budget=2048, thinking_level="xhigh",
                 tools_enabled=False)["max_tokens"] == 222 + 2048


@pytest.mark.parametrize("backend", ["strata", "xolotl"])
@pytest.mark.parametrize("level, budget, max_tokens", [
    ("xhigh", 16384, 333 + 16384), ("high", 12288, 333 + 12288), ("medium", 8192, 333 + 8192),
    ("low", 4096, 333 + 4096), (None, 16384, 333 + 16384)])
def test_a_compaction_follows_its_own_level_and_gets_the_room(backend, level, budget, max_tokens):
    k = _compact(backend_name=backend, thinking_budget=N, thinking_level=level)
    assert k["extra_body"][KEY] == budget and k["max_tokens"] == max_tokens


def test_a_compaction_with_thinking_off_gets_neither_and_a_pinned_one_follows_the_chat_effort():
    off = _compact(backend_name="strata", thinking_budget=N, thinking_level="off")
    assert KEY not in (off.get("extra_body") or {}) and off["max_tokens"] == 333
    pinned_off = _compact(backend_name="strata", thinking_budget=N, thinking_level="xhigh", pinned_effort="none")
    assert KEY not in (pinned_off.get("extra_body") or {}) and pinned_off["max_tokens"] == 333
    # Re-pinned to the chat turn's WIRE effort (Strata folded xhigh to high): that is all the compaction can see.
    pinned = _compact(backend_name="strata", thinking_budget=N, thinking_level="off", pinned_effort="high")
    assert pinned["extra_body"][KEY] == 12288 and pinned["max_tokens"] == 333 + 12288
    default = _compact(backend_name="strata", thinking_budget=N, thinking_level="off", pinned_effort=None)
    assert default["extra_body"][KEY] == 16384


@pytest.mark.parametrize("build", [_chat, _compact])
@pytest.mark.parametrize("backend", ["lmstudio", "llamacpp", "codex", ""])
def test_another_backend_gets_neither_the_key_nor_the_room(build, backend):
    k = build(backend_name=backend, thinking_budget=2048)
    assert KEY not in (k.get("extra_body") or {}) and "thinking_budget" not in (k.get("extra_body") or {})
    assert k["max_tokens"] == (16384 if build is _chat else 333)


def test_ninfer_keeps_its_one_budget_whatever_the_level():
    for level in ("low", "medium", "high", None):
        k = _chat(backend_name="ninfer", thinking_budget=2048, thinking_level=level, tools_enabled=False)
        assert k["extra_body"]["thinking_budget"] == 2048 and k["max_tokens"] == 222 + 2048
        assert KEY not in k["extra_body"]


def test_each_backend_reads_its_own_setting():
    s = Settings(ninfer_thinking_budget=111, strata_reasoning_budget_tokens=222)
    assert TurnEngine.thinking_budget_for(s, "ninfer") == 111
    assert TurnEngine.thinking_budget_for(s, "strata") == TurnEngine.thinking_budget_for(s, "xolotl") == 222
    assert TurnEngine.thinking_budget_for(s, "lmstudio") is None
    for unset in (None, 0, -1):
        assert TurnEngine.thinking_budget_for(Settings(strata_reasoning_budget_tokens=unset), "strata") is None


def test_every_turn_engine_call_in_app_asks_for_the_backends_budget():
    """The wiring: a call site that still reads ninfer_thinking_budget sends Strata no budget."""
    text = (SRC / "app.py").read_text(encoding="utf-8")
    calls = len(re.findall(r"TurnEngine\.(?:chat_request|compact_request)\(", text))
    wired = len(re.findall(r"thinking_budget=TurnEngine\.thinking_budget_for\(self\.settings, self\.backend\.name\)", text))
    assert calls == 3 and wired == calls
    assert "thinking_budget=self.settings.ninfer_thinking_budget" not in text


# ── the room a server names is a TOTAL: it still wins over the raised value ──────────────

@pytest.mark.asyncio
async def test_a_servers_named_room_still_caps_a_strata_compaction_raised_by_the_budget(monkeypatch):
    """The T1312 replay (tests/test_compact_reactive_retry.py's recorded strata refusals) with the budget on the
    Strata arm. The clamp runs after the raise in app.py for every backend, so nothing is raised twice."""
    from test_compact_reactive_retry import (RECORDED_PROMPT_TOKENS, WINDOW, _conversation_like_the_recording,
                                             _replay_app, _run)
    app, notes, log, served = _replay_app(monkeypatch, _conversation_like_the_recording(), "strata")
    build = TurnEngine.compact_request
    # The test app's backend cannot be renamed (the owned-agent authority check refuses it), so the request
    # builder is told it is Strata with a 1000-token budget at xhigh (the full N); the app's own clamp is under test.
    monkeypatch.setattr(TurnEngine, "compact_request", staticmethod(lambda **kw: build(
        **{**kw, "backend_name": "strata", "thinking_budget": 1000, "thinking_level": "xhigh",
           "pinned_effort": TurnEngine.UNPINNED})))
    async with app.run_test() as pilot:
        await _run(app, pilot)
    assert served["rounds"] == 9, notes
    first = 24000 + 1000
    assert log["refused"][0] == (1, first)
    expected, cap = [first, WINDOW - RECORDED_PROMPT_TOKENS[0]], WINDOW - RECORDED_PROMPT_TOKENS[0]
    for prompt in RECORDED_PROMPT_TOKENS[1:]:
        if prompt + cap > WINDOW:
            expected.append(cap)
            cap = WINDOW - prompt
        expected.append(cap)
    assert log["compaction"] == expected, (log["compaction"], expected)
