"""What a turn DECIDES, tested with no app, no mount, and no server.

That is the whole point of the extraction. Every assertion below used to be
reachable only by driving `_stream` or `_compact` -- 375- and 285-line methods
that mount widgets and write to a Textual reactive on nearly every token. The
request shape and the compaction threshold were real logic hiding inside a
render loop, so they were never tested directly. They are now.

🔴 THESE ARE CHARACTERISATION TESTS. This was a behaviour-preserving refactor,
so every expectation below is what the INLINE code produced at b604bc2, not an
improvement on it. If one of these ever has to change to make a feature work,
that is a behaviour change and it needs saying out loud.
"""
from __future__ import annotations

import types

import pytest

from litetui.turn_engine import TurnEngine


# ── compaction policy ───────────────────────────────────────────────────

def _due(**over):
    args = dict(enabled=True, at_percent=80, ctx_max=1000, ctx_used=900,
                ctx_loaded=True, failed_at=None)
    args.update(over)
    return TurnEngine.autocompact_due(**args)


def test_a_window_over_the_threshold_is_due_and_reports_the_percent():
    """THE POSITIVE ARM. Returning the percent rather than True is what lets
    the caller say '87%' in the message it shows the user."""
    assert _due(ctx_used=900, ctx_max=1000, at_percent=80) == 90


def test_a_window_under_the_threshold_is_not_due():
    assert _due(ctx_used=700) is None


def test_the_threshold_is_inclusive_at_exactly_the_percent():
    """Boundary. `pct < at_percent` returns None, so equality IS due --
    pinned because flipping it to `<=` is a one-character change that would
    silently delay every compaction by a percent."""
    assert _due(ctx_used=800, ctx_max=1000, at_percent=80) == 80


def test_disabled_never_fires():
    assert _due(enabled=False) is None


@pytest.mark.parametrize("field", ["ctx_max", "ctx_used"])
def test_an_unknown_window_refuses_rather_than_guessing(field):
    """Never guess a threshold. Zero and None both mean 'not known yet'."""
    assert _due(**{field: 0}) is None
    assert _due(**{field: None}) is None


def test_an_UNLOADED_model_refuses_because_ctx_max_is_a_CEILING():
    """🔴 THE ONE THAT MATTERS MOST, and the least obvious.

    While the model is not loaded, ctx_max is the model's CEILING rather than
    its live window. 80% of 262,144 is 209,715 tokens, which an 8k window can
    never reach -- so without this arm auto-compaction would silently NEVER
    fire and the model would blow its real context instead. The bug this
    prevents is invisible: nothing errors, the feature just quietly does not
    exist.
    """
    assert _due(ctx_max=262_144, ctx_used=250_000, ctx_loaded=False) is None
    # ...and the same numbers DO fire once it is loaded. Without this second
    # line the test would still pass if the function always returned None.
    assert _due(ctx_max=262_144, ctx_used=250_000, ctx_loaded=True) == 95


def test_an_unmoved_window_after_a_FAILED_compaction_does_not_retry():
    """The retry loop watched in the wild: 'Compacting 126 messages / Compact
    failed / Compacting 126 messages', no backoff, each attempt a full
    request. Identical inputs fail identically, so wait for a real change."""
    assert _due(ctx_used=900, failed_at=900) is None


def test_but_it_DOES_retry_once_the_window_actually_moves():
    """NEGATIVE ARM for the line above. Without it, a version that latched
    permanently after one failure would pass -- and auto-compaction would be
    dead for the rest of the session."""
    assert _due(ctx_used=901, failed_at=900) == 90


# ── the chat request ────────────────────────────────────────────────────

def _chat(**over):
    args = dict(model_id="m", messages=[{"role": "user", "content": "hi"}],
                tools_enabled=False, max_tokens_tools=111, max_tokens_chat=222,
                request_overrides={}, thinking_level=None, tools=None)
    args.update(over)
    return TurnEngine.chat_request(**args)


def test_a_chat_request_streams_and_asks_for_usage():
    """stream_options.include_usage is what delivers the token counts the
    context footer and the ETA estimator are built on. Dropping it does not
    break the turn -- it silently breaks both of those instead."""
    k = _chat()
    assert k["stream"] is True
    assert k["stream_options"] == {"include_usage": True}
    assert k["messages"] == [{"role": "user", "content": "hi"}]


def test_a_missing_model_id_falls_back_rather_than_sending_None():
    assert _chat(model_id=None)["model"] == "local-model"
    assert _chat(model_id="")["model"] == "local-model"


def test_the_token_budget_follows_the_TOOLS_toggle():
    """Two different budgets, chosen by the toggle -- a tool turn needs room
    for results the chat turn never carries."""
    assert _chat(tools_enabled=False)["max_tokens"] == 222
    assert _chat(tools_enabled=True, tools=[])["max_tokens"] == 111


def test_tools_are_sent_only_when_enabled():
    assert "tools" not in _chat(tools_enabled=False)
    assert _chat(tools_enabled=True, tools=[{"x": 1}])["tools"] == [{"x": 1}]


def test_thinking_off_is_sent_as_none_not_as_off():
    """The server's vocabulary, not ours. 'off' is this app's word; LM Studio
    accepts 'none'. Sending 'off' is a 400."""
    assert _chat(thinking_level="off")["extra_body"]["reasoning_effort"] == "none"
    assert _chat(thinking_level="high")["extra_body"]["reasoning_effort"] == "high"


def test_no_thinking_level_sends_no_extra_body_at_all():
    """An absent knob must leave the server's own default in charge. Sending
    extra_body={} is not the same as not sending it."""
    assert "extra_body" not in _chat(thinking_level=None)


def test_sampling_overrides_are_split_between_native_and_extra_body():
    """The OpenAI client's create() has typed params and no **kwargs, so
    top_k as a TOP-LEVEL key is a TypeError rather than a passthrough. It has
    to ride extra_body; temperature must not."""
    k = _chat(request_overrides={"temperature": 0.5, "top_k": 40})
    assert k["temperature"] == 0.5, "a native param was not passed natively"
    assert k.get("extra_body", {}).get("top_k") == 40, "top_k must ride extra_body"
    assert "top_k" not in k, "top_k at the top level is a TypeError, not a param"


# ── the compaction request ──────────────────────────────────────────────

def _compact(**over):
    args = dict(model_id="m", messages=[{"role": "user", "content": "sum"}],
                max_tokens=333, thinking_level="low", tools_enabled=False,
                tools=None)
    args.update(over)
    return TurnEngine.compact_request(**args)


def test_a_compaction_streams_so_the_card_can_show_the_summary_being_born():
    assert _compact()["stream"] is True


def test_a_compaction_sends_NO_stream_options_unlike_a_chat_turn():
    """🔴 THE TWO REQUESTS ARE NOT THE SAME REQUEST, and this is the
    difference most likely to be 'tidied' away by someone merging the two
    builders into one. Pinned so that merge has to be deliberate."""
    assert "stream_options" not in _compact()
    assert "stream_options" in _chat()


def test_a_compaction_sends_no_sampling_overrides():
    """A compaction is not a creative turn; it takes the server's defaults
    for SAMPLING. Chat/model overrides must not change compaction settings."""
    k = _compact(request_overrides={"temperature": 0.9, "top_p": 0.5})
    assert "temperature" not in k and "top_p" not in k


def test_the_compaction_thinking_level_is_its_own_setting():
    assert _compact(thinking_level="off")["extra_body"]["reasoning_effort"] == "none"
    assert _compact(thinking_level="xhigh")["extra_body"]["reasoning_effort"] == "xhigh"


# ── per-model thinking level (/modelcfg Inference tab) ───────────────


def test_a_per_model_thinking_level_wins_over_the_global_one():
    """The /modelcfg row arrives merged into request_overrides; it must beat
    the global level for this model, not sit beside it."""
    k = _chat(thinking_level="xhigh", request_overrides={"reasoning_effort": "low"})
    assert k["extra_body"]["reasoning_effort"] == "low"


def test_a_per_model_off_is_sent_as_none_like_the_global_one():
    """Same wire vocabulary either way: 'off' is this app's word, LM Studio
    accepts 'none'. A per-model off must not leak the raw word."""
    k = _chat(thinking_level="medium", request_overrides={"reasoning_effort": "off"})
    assert k["extra_body"]["reasoning_effort"] == "none"


def test_a_blank_per_model_inherits_the_global_level():
    """Blank is stored as ABSENT (the screen pops it), so no key in the
    overrides dict IS the blank — and the global level must come through."""
    k = _chat(thinking_level="xhigh", request_overrides={"temperature": 0.5})
    assert k["extra_body"]["reasoning_effort"] == "xhigh"


@pytest.mark.parametrize("backend", ["ninfer", "llamacpp", "codex"])
@pytest.mark.parametrize("compact_level,model_level,expected", [
    ("low", "xhigh", "low"),
    ("off", "xhigh", "none"),
    ("xhigh", "off", "xhigh"),
    (None, "xhigh", None),
])
def test_compaction_setting_wins_over_model_thinking(backend, compact_level, model_level, expected):
    """Compaction owns its reasoning budget independently of normal chat."""
    overrides = {"reasoning_effort": model_level}
    k = _compact(thinking_level=compact_level, backend_name=backend,
                 request_overrides=overrides)
    assert k.get("extra_body", {}).get("reasoning_effort") == expected
    assert overrides == {"reasoning_effort": model_level}


def test_compaction_uses_its_setting_before_lmstudio_capability_mapping():
    overrides = {"reasoning_effort": "off"}
    k = _compact(backend_name="lmstudio", request_overrides=overrides)
    assert "reasoning_effort" not in k.get("extra_body", {})
    k = _compact(backend_name="lmstudio", request_overrides=overrides,
                 graded_thinking_models=["m"])
    assert k["extra_body"]["reasoning_effort"] == "low"


def test_compaction_with_no_level_sends_no_extra_body_either():
    """Symmetric with the chat arm (test_no_thinking_level_sends_no_extra_body_at_all):
    an absent level must leave the server's own default in charge, not send a null.
    The compact arm used to always write it -- this pins the guard."""
    assert "extra_body" not in _compact(thinking_level=None)


def test_tools_ride_along_so_step_1_of_the_compact_prompt_can_happen():
    """Without them the instruction to persist durable state before the
    history is destroyed is theatre -- the model is told to save and given
    nothing to save with."""
    assert "tools" not in _compact(tools_enabled=False)
    assert _compact(tools_enabled=True, tools=[{"t": 1}])["tools"] == [{"t": 1}]


# ── streamed tool-call assembly ─────────────────────────────────────────

def _delta(index=0, id=None, name=None, arguments=None):
    fn = types.SimpleNamespace(name=name, arguments=arguments)
    return types.SimpleNamespace(index=index, id=id, function=fn)


def test_a_name_split_across_chunks_is_CONCATENATED_not_overwritten():
    """THE REASON THIS IS += AND NOT =. A tool name arrives in pieces; the
    last assignment would leave 'sh' instead of 'bash' and dispatch would
    fail on a tool that does not exist."""
    acc: dict = {}
    TurnEngine.accumulate_tool_call(acc, _delta(name="ba"))
    TurnEngine.accumulate_tool_call(acc, _delta(name="sh"))
    assert acc[0]["name"] == "bash"


def test_arguments_arrive_a_fragment_at_a_time_and_must_survive_reassembly():
    """JSON streamed character-wise is only valid once whole. Overwriting
    would produce a fragment that json.loads rejects."""
    acc: dict = {}
    for piece in ('{"cm', 'd": "', 'ls"}'):
        TurnEngine.accumulate_tool_call(acc, _delta(arguments=piece))
    assert acc[0]["arguments"] == '{"cmd": "ls"}'


def test_the_id_is_kept_from_whichever_chunk_carries_it():
    """The id arrives once, often on a chunk with no name and no arguments,
    and the tool RESULT has to be paired back to it. Losing it breaks the
    pairing, which the model sees as an unanswered call."""
    acc: dict = {}
    TurnEngine.accumulate_tool_call(acc, _delta(id="call_9"))
    TurnEngine.accumulate_tool_call(acc, _delta(name="bash"))
    assert acc[0] == {"id": "call_9", "name": "bash", "arguments": ""}


def test_a_chunk_with_no_id_does_not_ERASE_the_id_already_held():
    """NEGATIVE ARM. `slot['id'] = tc.id` unguarded would null it on the very
    next fragment -- and every later fragment is exactly that shape."""
    acc: dict = {}
    TurnEngine.accumulate_tool_call(acc, _delta(id="call_9"))
    TurnEngine.accumulate_tool_call(acc, _delta(id=None, arguments="{}"))
    assert acc[0]["id"] == "call_9"


def test_parallel_tool_calls_are_kept_apart_by_index():
    """Models emit several calls in one turn, interleaved across chunks. The
    index is the only thing separating them."""
    acc: dict = {}
    TurnEngine.accumulate_tool_call(acc, _delta(index=0, name="bash"))
    TurnEngine.accumulate_tool_call(acc, _delta(index=1, name="read"))
    TurnEngine.accumulate_tool_call(acc, _delta(index=0, arguments="{}"))
    assert acc[0]["name"] == "bash" and acc[1]["name"] == "read"
    assert acc[1]["arguments"] == ""


def test_the_flags_report_what_THIS_chunk_carried_not_what_is_accumulated():
    """The caller mounts a widget on the chunk that first names a slot, so
    the flag has to describe the chunk. Reporting the accumulated state would
    fire on every subsequent fragment too."""
    acc: dict = {}
    idx, named, argued = TurnEngine.accumulate_tool_call(acc, _delta(name="bash"))
    assert (idx, named, argued) == (0, True, False)
    idx, named, argued = TurnEngine.accumulate_tool_call(acc, _delta(arguments="{}"))
    assert (idx, named, argued) == (0, False, True), (
        "a chunk carrying only arguments must not claim it named the tool"
    )


def test_a_delta_with_no_function_at_all_is_survivable():
    """Some chunks carry only the index and the id. Reaching through a None
    function is an AttributeError mid-stream, which kills the turn."""
    acc: dict = {}
    d = types.SimpleNamespace(index=0, id="call_1", function=None)
    idx, named, argued = TurnEngine.accumulate_tool_call(acc, d)
    assert (named, argued) == (False, False)
    assert acc[0]["id"] == "call_1"


def test_ninfer_asks_for_prompt_progress_others_do_not():
    """return_progress rides extra_body ONLY for the ninfer backend, so the
    engine streams real prefill progress. A blanket send would set a field
    LM Studio / llama.cpp reject; not sending it for ninfer means the app is
    back to the previous-turn projection this whole change replaces."""
    assert _chat(backend_name="ninfer")["extra_body"]["return_progress"] is True
    assert "extra_body" not in _chat(backend_name="lmstudio")
    assert "extra_body" not in _chat(backend_name="")
