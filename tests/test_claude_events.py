"""Behavioural contract for claude_events normalization.

Two boundaries are exercised deliberately:

* the **wire dicts** in `fixtures/claude_sdk_turn.json`, which is what the CLI
  actually emits and what the module must survive unparsed, and
* the **real SDK dataclasses**, built by the pinned SDK's own parser, so the
  duck-typed object path is checked against the shipped types rather than
  against a fake that agrees with my reading of them.

The object arm skips when the optional `claude` extra is absent. It is the only
arm that needs it; everything else runs on fixtures alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from litetui.claude_events import (
    DIAGNOSTIC_LIMIT,
    ClaudeEvent,
    ClaudeEventStream,
    ClaudeUsage,
)

FIXTURE = Path(__file__).parent / "fixtures" / "claude_sdk_turn.json"


def frames():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def drive(*messages):
    """Push every message through one stream, return (stream, events)."""
    stream = ClaudeEventStream()
    events = []
    for message in messages:
        events.extend(stream.push(message))
    return stream, events


def kinds(events):
    return [event.kind for event in events]


def only(events, kind):
    return [event for event in events if event.kind == kind]


def delta(text, message_id=None):
    return {
        "type": "stream_event",
        "uuid": "u",
        "session_id": "sess-abc",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


def start(message_id="msg_01", usage=None):
    return {
        "type": "stream_event",
        "uuid": "u",
        "session_id": "sess-abc",
        "event": {
            "type": "message_start",
            "message": {"id": message_id, "model": "claude-opus-5", "usage": usage},
        },
    }


def snapshot(text, message_id="msg_01", blocks=None, **message):
    content = blocks if blocks is not None else [{"type": "text", "text": text}]
    return {
        "type": "assistant",
        "uuid": "u-snap",
        "session_id": "sess-abc",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "content": content,
            **message,
        },
    }


# -- the realistic turn ------------------------------------------------


def test_fixture_turn_projects_the_expected_shape():
    stream, events = drive(*frames())
    assert stream.session_id == "sess-abc"
    assert kinds(events) == [
        "session",
        "usage",
        "text_delta",
        "text_delta",
        "tool_use",
        "usage",
        "message",
        "tool_use",
        "tool_result",
        "usage",
        "message",
        "usage",
        "result",
    ]
    session = only(events, "session")[0]
    assert session.model == "claude-opus-5"
    assert session.data["cwd"] == "C:/Projects/LiteTUI"
    assert only(events, "result")[0].is_error is False


def test_streamed_text_is_not_rendered_twice():
    _, events = drive(*frames())
    message = only(events, "message")[0]
    assert message.reconcile == "append"
    assert message.text == ""
    assert message.data["full_text"] == "Reading the file."


def test_tool_card_opens_before_the_input_is_final():
    _, events = drive(*frames())
    opened, final = only(events, "tool_use")[:2]
    assert (opened.complete, opened.tool_id, opened.tool_name) == (False, "toolu_01", "Read")
    assert opened.tool_input == {}
    assert final.complete is True
    assert final.tool_input == {"file_path": "C:/Projects/LiteTUI/README.md"}


def test_tool_result_is_joined_to_its_tool_by_id():
    _, events = drive(*frames())
    result = only(events, "tool_result")[0]
    assert (result.tool_id, result.tool_name) == ("toolu_01", "Read")
    assert result.tool_result == "# LiteTUI\n"
    assert result.is_error is False


# -- snapshot / delta reconciliation -----------------------------------


def test_snapshot_appends_only_the_deltas_that_went_missing():
    # The middle of a stream is lost — a dropped frame, a reader restart. The
    # snapshot is the only chance to repair it, so it must hand back the tail
    # rather than being suppressed by a "was streamed" flag.
    _, events = drive(start(), delta("Reading "), snapshot("Reading the file."))
    message = only(events, "message")[0]
    assert message.reconcile == "append"
    assert message.text == "the file."


def test_divergent_snapshot_replaces_rather_than_splicing():
    _, events = drive(start(), delta("Rea"), delta("XXX"), snapshot("Reading the file."))
    message = only(events, "message")[0]
    assert message.reconcile == "replace"
    assert message.text == "Reading the file."


def test_complete_message_fallback_when_nothing_streamed():
    _, events = drive(snapshot("No deltas at all."))
    message = only(events, "message")[0]
    assert message.reconcile == "replace"
    assert message.text == "No deltas at all."


def test_snapshot_for_a_different_message_id_is_not_reconciled_against_it():
    # Identity, not position: a second message must not inherit the first
    # message's streamed prefix just because it arrived next.
    _, events = drive(start("msg_01"), delta("first"), snapshot("second", message_id="msg_02"))
    message = only(events, "message")[0]
    assert (message.message_id, message.reconcile, message.text) == ("msg_02", "replace", "second")


def test_thinking_reconciles_independently_of_text():
    thinking_delta = {
        "type": "stream_event",
        "uuid": "u",
        "session_id": "s",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "Let me "},
        },
    }
    _, events = drive(
        start(),
        thinking_delta,
        snapshot(
            "",
            blocks=[
                {"type": "thinking", "thinking": "Let me check.", "signature": "sig"},
                {"type": "text", "text": "Checked."},
            ],
        ),
    )
    thinking = only(events, "thinking")[0]
    assert (thinking.reconcile, thinking.text) == ("append", "check.")
    assert only(events, "message")[0].text == "Checked."


def test_blocks_are_preserved_verbatim_and_ordered_for_the_fallback():
    blocks = [
        {"type": "text", "text": "one"},
        {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        {"type": "text", "text": "two"},
    ]
    _, events = drive(snapshot("", blocks=blocks))
    message = only(events, "message")[0]
    assert message.data["blocks"] == blocks
    assert message.text == "onetwo"


# -- usage: nothing invented -------------------------------------------


def test_occupancy_comes_from_a_request_never_from_the_turn_aggregate():
    _, events = drive(*frames())
    request, terminal = only(events, "usage")[0], only(events, "usage")[-1]
    assert request.usage.source == "message"
    # 1200 input + 8000 cache read + 300 cache write.
    assert request.usage.context_tokens == 9500
    assert terminal.usage.source == "result"
    # The result aggregates the turn's requests; calling that "occupancy"
    # would report a context larger than any that ever existed.
    assert terminal.usage.context_tokens is None
    assert terminal.usage.max_context_tokens == 200000


def test_cost_and_model_usage_are_carried_separately_with_no_derived_delta():
    _, events = drive(*frames())
    usage = only(events, "usage")[-1].usage
    assert usage.cost_usd == 0.0412
    assert usage.model_usage["claude-opus-5"]["costUSD"] == 0.0412
    assert usage.raw["input_tokens"] == 2600
    # No turn delta is published: whether result usage is per-turn or
    # session-cumulative is unsettled for this pair, so it is not guessed.
    assert not hasattr(usage, "turn_tokens")
    assert not hasattr(usage, "delta")


def test_an_omitted_counter_stays_unknown():
    _, events = drive(start(usage={"input_tokens": 10}))
    usage = only(events, "usage")[0].usage
    assert usage.input_tokens == 10
    assert usage.output_tokens is None
    assert usage.cache_read_tokens is None
    assert usage.context_tokens == 10


def test_camel_case_usage_spelling_is_read_too():
    _, events = drive(start(usage={"inputTokens": 7, "cacheReadInputTokens": 3}))
    usage = only(events, "usage")[0].usage
    assert (usage.input_tokens, usage.cache_read_tokens, usage.context_tokens) == (7, 3, 10)


def test_negative_and_non_integer_counters_are_rejected_as_unknown():
    _, events = drive(start(usage={"input_tokens": -5, "output_tokens": "many"}))
    usage = only(events, "usage")[0].usage
    assert (usage.input_tokens, usage.output_tokens, usage.context_tokens) == (None, None, None)


# -- terminal state -----------------------------------------------------


def test_success_subtype_with_is_error_true_is_reported_as_an_error():
    # The CLI emits subtype "success" alongside is_error=True; reading the
    # subtype alone calls a failed turn a good one.
    _, events = drive(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "s",
            "is_error": True,
            "api_error_status": 529,
            "terminal_reason": "completed",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "num_turns": 1,
        }
    )
    result = only(events, "result")[0]
    assert result.is_error is True
    assert result.data["subtype"] == "success"
    assert result.data["api_error_status"] == 529


def test_terminal_reason_survives_an_interrupt():
    _, events = drive(
        {
            "type": "result",
            "subtype": "success",
            "session_id": "s",
            "is_error": False,
            "terminal_reason": "aborted_streaming",
            "duration_ms": 1,
            "duration_api_ms": 1,
            "num_turns": 1,
        }
    )
    assert only(events, "result")[0].data["terminal_reason"] == "aborted_streaming"


def test_turn_state_is_dropped_at_the_terminal_event():
    stream, _ = drive(*frames())
    assert stream._streamed == {}
    assert stream._tools == {}
    assert stream._active is None


def test_conversation_reset_drops_the_running_totals():
    stream = ClaudeEventStream()
    for frame in frames():
        stream.push(frame)
    assert stream.usage is not None
    events = stream.push(
        {
            "type": "conversation_reset",
            "new_conversation_id": "conv-2",
            "uuid": "u",
            "session_id": "sess-abc",
        }
    )
    assert kinds(events) == ["reset"]
    assert events[0].data["new_conversation_id"] == "conv-2"
    assert stream.usage is None


# -- native lifecycle ---------------------------------------------------


def test_compaction_is_observed_and_carries_its_payload():
    _, events = drive(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "session_id": "s",
            "uuid": "u",
            "compact_metadata": {"trigger": "auto", "pre_tokens": 150000},
        }
    )
    assert kinds(events) == ["compaction"]
    assert events[0].data["compact_metadata"]["trigger"] == "auto"


@pytest.mark.parametrize(
    "subtype,status",
    [
        ("task_started", None),
        ("task_progress", None),
        ("task_notification", "completed"),
        ("task_updated", "killed"),
    ],
)
def test_background_task_lifecycle_projects_a_task_event(subtype, status):
    frame = {
        "type": "system",
        "subtype": subtype,
        "session_id": "s",
        "uuid": "u",
        "task_id": "task-1",
        "tool_use_id": "toolu_9",
    }
    if subtype == "task_notification":
        frame["status"] = status
    if subtype == "task_updated":
        frame["patch"] = {"status": status}
    _, events = drive(frame)
    assert kinds(events) == ["task"]
    assert events[0].data["phase"] == subtype
    assert events[0].data["task_id"] == "task-1"
    assert events[0].data["status"] == status
    assert events[0].tool_id == "toolu_9"


def test_nested_agent_work_keeps_its_attribution():
    _, events = drive(
        {
            "type": "assistant",
            "uuid": "u",
            "session_id": "s",
            "parent_tool_use_id": "toolu_parent",
            "message": {
                "id": "msg_child",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": "sub-agent говорит"}],
            },
        }
    )
    assert all(event.parent_tool_use_id == "toolu_parent" for event in events)


def test_rate_limit_transition_is_surfaced():
    _, events = drive(
        {
            "type": "rate_limit_event",
            "uuid": "u",
            "session_id": "s",
            "rate_limit_info": {"status": "allowed_warning", "utilization": 0.82},
        }
    )
    assert kinds(events) == ["rate_limit"]
    assert events[0].data["utilization"] == 0.82


def test_server_side_tool_blocks_are_flagged_as_such():
    _, events = drive(
        snapshot(
            "",
            blocks=[
                {"type": "server_tool_use", "id": "srv_1", "name": "web_search", "input": {"q": "x"}},
                {"type": "advisor_tool_result", "tool_use_id": "srv_1", "content": {"type": "ok"}},
            ],
        )
    )
    use, result = only(events, "tool_use")[0], only(events, "tool_result")[0]
    assert use.data["server"] is True
    assert result.data["server"] is True
    assert result.tool_name == "web_search"


# -- a tool result is untrusted terminal input --------------------------


def test_tool_result_text_is_escape_stripped_and_secret_redacted():
    # Native results bypass app._execute_tool, so the dispatch-loop sanitizing
    # at app.py:7573 never sees them. This is the one place they all cross.
    _, events = drive(
        {
            "type": "user",
            "uuid": "u",
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": "\x1b[<35;95;34MOPENAI_API_KEY=sk-live-should-not-survive\n",
                    }
                ]
            },
        }
    )
    text = only(events, "tool_result")[0].tool_result
    assert "\x1b" not in text
    assert "sk-live-should-not-survive" not in text
    assert "OPENAI_API_KEY" in text


def test_structured_tool_result_parts_are_sanitized_in_place():
    _, events = drive(
        {
            "type": "user",
            "uuid": "u",
            "session_id": "s",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text", "text": "AWS_SECRET_ACCESS_KEY=abcd1234"},
                            {"type": "image", "source": {"data": "iVBOR"}},
                        ],
                    }
                ]
            },
        }
    )
    parts = only(events, "tool_result")[0].tool_result
    assert "abcd1234" not in parts[0]["text"]
    assert parts[1] == {"type": "image", "source": {"data": "iVBOR"}}


# -- nothing kills the reader ------------------------------------------


def test_unknown_frame_type_becomes_a_diagnostic_not_a_raise():
    _, events = drive({"type": "quantum_telemetry", "session_id": "s", "payload": 1})
    assert kinds(events) == ["diagnostic"]
    assert events[0].data["frame_type"] == "quantum_telemetry"
    assert "quantum_telemetry" in events[0].detail


def test_unknown_stream_event_becomes_a_diagnostic():
    _, events = drive(
        {
            "type": "stream_event",
            "uuid": "u",
            "session_id": "s",
            "event": {"type": "telepathy_delta", "delta": {}},
        }
    )
    assert kinds(events) == ["diagnostic"]


def test_benign_stream_events_are_silent():
    quiet = [
        {"type": "stream_event", "uuid": "u", "session_id": "s", "event": {"type": name}}
        for name in ("ping", "message_stop", "content_block_stop", "message_delta")
    ]
    _, events = drive(*quiet)
    assert events == []


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"type": "assistant"},
        {"type": "assistant", "message": "not a mapping"},
        {"type": "user", "message": {"content": [None, 7]}},
        {"type": "stream_event", "event": None},
        {"type": "result"},
        {"type": "system"},
        None,
        42,
        "a bare string",
        object(),
    ],
)
def test_malformed_frames_never_raise(bad):
    stream = ClaudeEventStream()
    events = stream.push(bad)
    assert all(isinstance(event, ClaudeEvent) for event in events)


def test_a_diagnostic_describes_the_frame_without_reproducing_it():
    _, events = drive(
        {
            "type": "unheard_of",
            "session_id": "s",
            "ANTHROPIC_API_KEY": "sk-ant-should-not-appear",
            "blob": "x" * 5000,
        }
    )
    diagnostic = events[0]
    assert len(diagnostic.detail) <= DIAGNOSTIC_LIMIT + 1
    assert "sk-ant-should-not-appear" not in diagnostic.detail
    assert "x" * 100 not in diagnostic.detail
    # Key NAMES describe the shape; the values are what could carry a secret.
    assert "ANTHROPIC_API_KEY" in diagnostic.data["keys"]
    assert "sk-ant-should-not-appear" not in repr(diagnostic.data)


def test_a_non_mapping_content_block_is_diagnosed_without_losing_its_siblings():
    _, events = drive(
        snapshot(
            "",
            blocks=[
                {"type": "text", "text": "kept"},
                "not a block",
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
            ],
        )
    )
    assert only(events, "message")[0].text == "kept"
    assert kinds(events).count("diagnostic") == 1
    assert only(events, "tool_use")[0].tool_id == "t1"


def test_reader_failure_normalizes_into_an_error_event():
    stream = ClaudeEventStream(session_id="sess-abc")
    event = stream.failure(ConnectionResetError("pipe closed: token=sk-ant-live"))
    assert event.kind == "error"
    assert event.is_error is True
    assert event.session_id == "sess-abc"
    assert event.data["scope"] == "reader"
    assert "ConnectionResetError" in event.detail
    assert "sk-ant-live" not in event.detail


def test_an_assistant_error_is_surfaced_alongside_its_message():
    _, events = drive(
        {
            "type": "assistant",
            "uuid": "u",
            "session_id": "s",
            "error": "rate_limit",
            "message": {"id": "msg_e", "model": "claude-opus-5", "content": []},
        }
    )
    error = only(events, "error")[0]
    assert (error.is_error, error.detail, error.data["scope"]) == (True, "rate_limit", "assistant")


def test_reset_turn_clears_reconciliation_state():
    stream = ClaudeEventStream()
    stream.push(start())
    stream.push(delta("half "))
    stream.reset_turn()
    events = stream.push(snapshot("half written"))
    message = only(events, "message")[0]
    assert (message.reconcile, message.text) == ("replace", "half written")


# -- the duck-typed object path ----------------------------------------


def test_simple_namespace_objects_take_the_object_path():
    # The fake SDK boundary: attribute dispatch must not depend on the real
    # classes being importable.
    message = NS(
        content=[NS(text="hello"), NS(id="t1", name="Read", input={"p": 1})],
        model="claude-opus-5",
        message_id="msg_ns",
        uuid="u-ns",
        session_id="sess-ns",
        parent_tool_use_id=None,
        usage={"input_tokens": 5},
        stop_reason="end_turn",
        error=None,
    )
    _, events = drive(message)
    assert kinds(events) == ["usage", "message", "tool_use"]
    assert only(events, "message")[0].text == "hello"
    assert only(events, "tool_use")[0].tool_name == "Read"


def test_real_sdk_objects_project_the_same_events_as_the_wire_dicts():
    """The object path, checked against the shipped 0.2.159 dataclasses.

    Skips without the optional `claude` extra. Everything above runs on
    fixtures alone, so the suite stays meaningful without the SDK installed.
    """
    pytest.importorskip("claude_agent_sdk")
    from claude_agent_sdk._internal.message_parser import parse_message

    wire = frames()
    parsed = [parse_message(frame) for frame in wire]
    assert all(message is not None for message in parsed)

    _, from_dicts = drive(*wire)
    _, from_objects = drive(*parsed)

    assert kinds(from_objects) == kinds(from_dicts)
    for left, right in zip(from_objects, from_dicts, strict=True):
        assert (left.text, left.reconcile) == (right.text, right.reconcile)
        assert (left.tool_id, left.tool_name, left.tool_input) == (
            right.tool_id,
            right.tool_name,
            right.tool_input,
        )
        assert left.message_id == right.message_id
        assert left.is_error == right.is_error
        assert left.usage == right.usage


def test_usage_is_comparable_by_value():
    # The session layer diffs observations; a frozen dataclass makes that a
    # comparison rather than a field-by-field audit.
    assert ClaudeUsage(source="result", input_tokens=1) == ClaudeUsage(
        source="result", input_tokens=1
    )
    assert ClaudeUsage(source="result") != ClaudeUsage(source="message")


def test_a_snapshot_without_a_text_block_never_wipes_streamed_text():
    """Per-block snapshots (live, CLI 2.1.281): a thinking- or tool-only snapshot says
    nothing about the text, so it must not replace what streamed with ""; and deltas after
    it keep their message id until the stream's message_stop."""
    _, events = drive(
        start(),
        delta("Hello"),
        snapshot("", blocks=[{"type": "thinking", "thinking": "hm", "signature": "s"}]),
        delta(" world"),
        snapshot("Hello world"),
        {"type": "stream_event", "uuid": "u", "session_id": "sess-abc", "event": {"type": "message_stop"}},
    )
    assert [e.message_id for e in only(events, "text_delta")] == ["msg_01", "msg_01"]
    first, last = only(events, "message")
    assert (first.reconcile, first.text) == ("append", "")
    assert (last.reconcile, last.text) == ("append", "")
