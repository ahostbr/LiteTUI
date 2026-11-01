"""Pure bounded history projection: no widgets, tools, providers or filesystem."""
from __future__ import annotations

import copy
import hashlib
from typing import Any

import pytest

from litetui import history_projection as history


def fragments(view):
    return [row for _start, _end, rows in history.pages(view) for row in rows]


@pytest.mark.parametrize("body", ["x" * 40000, "\n" * 3000, "\x00" * 12000, "雪😀e\u0301" * 6000], ids=["long", "newlines", "controls", "unicode"])
def test_large_blocks_are_lossless_bounded_utf8_fragments(body):
    view = history.project([{"role": "assistant", "content": body}])
    rows = fragments(view)
    assert len(rows) > 1
    assert "".join(row["text"] for row in rows) == body
    position = 0
    for row in rows:
        part = row["part"]
        assert part["utf8_start"] == position
        position += len(row["text"].encode("utf-8"))
        assert part["utf8_end"] == position
        assert part["sha256"] == hashlib.sha256(row["text"].encode("utf-8")).hexdigest()
        assert part["utf8_total"] == len(body.encode("utf-8"))
        assert row["block_sha256"] == hashlib.sha256(body.encode("utf-8")).hexdigest()
    for _start, _end, page in history.pages(view):
        assert len(page) <= history.PAGE_FRAGMENTS
        assert sum(history.display_cost(row["text"]) + 320 + history.display_cost(row["role"]) + history.display_cost(row["label"]) + history.display_cost(row["state"] or "") for row in page) <= history.PAGE_UNITS


def test_more_than_one_thousand_messages_preserve_full_context():
    messages = [{"role": "system", "content": "hidden"}] + [{"role": "user", "content": str(i)} for i in range(1201)]
    view = history.project(messages)
    assert view.counts() == {"messages": 1202, "system": 1, "non_system": 1201, "blocks": 1201}
    assert [row["text"] for row in fragments(view)] == [str(i) for i in range(1201)]


@pytest.mark.parametrize("messages, expected", [([], True), ([{"role": "system", "content": "x"}], True),
    ([{"role": "system", "content": "x", "provider_metadata": {}}], False),
    ([{"role": "assistant", "content": ""}], False)])
def test_fresh_system_context_is_the_exact_native_predicate(messages, expected):
    assert history.project(messages).fresh_system_context is expected


def test_hidden_system_and_provider_fields_change_semantic_digest():
    first: list[dict[str, Any]] = [{"role": "system", "content": "one"}, {"role": "assistant", "content": "answer", "provider_metadata": {"native_ref": "a"}}]
    view = history.project(first)
    changed = copy.deepcopy(first)
    changed[0]["content"] = "two"
    assert history.project(changed).context_sha256 != view.context_sha256
    changed = copy.deepcopy(first)
    changed[1]["provider_metadata"]["native_ref"] = "b"
    assert history.project(changed).context_sha256 != view.context_sha256


def test_reasoning_and_recap_are_separate_inert_blocks():
    view = history.project([{"role": "assistant", "content": "Answer\n<recap>Summary</recap>", "reasoning_content": "Thought"}])
    assert [(row.kind, row.content) for row in view.blocks] == [("reasoning", "Thought"), ("assistant", "Answer"), ("summary", "Summary")]


def test_claude_card_lengths_preserve_answer_tool_interleave():
    message = {"role": "assistant", "content": "First\n\nLast", "claude_native": {
        "card_text_lengths": [5, 4], "activities": [
            {"id": "tool-one", "kind": "tool_use", "name": "Read", "input": {"path": "fixture"}, "card": 0},
            {"id": "tool-one", "kind": "tool_result", "result": "done", "is_error": False, "card": 0}]}}
    view = history.project([message])
    assert [row.kind for row in view.blocks] == ["assistant", "tool", "assistant"]
    assert view.blocks[0].content == "First" and view.blocks[2].content == "Last"
    assert "done" in view.blocks[1].content


def test_codex_trace_dedup_suppresses_duplicate_assistant_content():
    meta = {"app_server_thread_id": "thread", "display_trace": {"version": 1, "items": [
        {"id": "a", "turnId": "turn", "kind": "agentMessage", "result": "Native answer", "state": "completed"},
        {"id": "t", "turnId": "turn", "kind": "commandExecution", "name": "tool", "args": "x", "state": "running"}]}}
    messages = [{"role": "assistant", "content": "Duplicate", "provider_metadata": meta},
                {"role": "assistant", "content": "Duplicate", "provider_metadata": copy.deepcopy(meta)}]
    view = history.project(messages)
    assert [row.kind for row in view.blocks] == ["assistant", "tool"]
    assert view.blocks[0].content == "Native answer"
    assert view.blocks[1].state == "incomplete"
    assert "No completion" in view.blocks[1].content


def test_generic_tool_results_follow_canonical_omission_notice():
    view = history.project([{"role": "user", "content": "question"}, {"role": "tool", "content": "SECRET_RESULT"}, {"role": "assistant", "content": "answer"}])
    assert [row.kind for row in view.blocks] == ["user", "assistant", "notice"]
    assert "SECRET_RESULT" not in "".join(row.content for row in view.blocks)
    assert "1 tool result(s)" in view.blocks[-1].content


@pytest.mark.parametrize("block", [{"type": "image_url", "image_url": {"url": "data:image/png;base64,PRIVATE_PIXELS"}},
                                  {"type": "image", "source": {"data": "PRIVATE_PIXELS"}}, {"unexpected": "PRIVATE_PIXELS"}])
def test_attachment_and_unknown_blocks_never_dump_embedded_payload(block):
    view = history.project([{"role": "user", "content": [block]}])
    assert "PRIVATE_PIXELS" not in "".join(row.content for row in view.blocks)
    assert view.blocks[0].kind in ("attachment", "notice")


def test_context_and_projection_limits_are_separate(monkeypatch):
    monkeypatch.setattr(history, "MAX_PROJECTION", 100)
    with pytest.raises(ValueError, match="projection budget"):
        history.project([{"role": "user", "content": "small"}])


def test_empty_block_and_empty_history_have_complete_page_boundaries():
    assert list(history.pages(history.project([]))) == [((0, 0), (0, 0), [])]
    rows = fragments(history.project([{"role": "user", "content": ""}]))
    assert len(rows) == 1 and rows[0]["part"]["utf8_start"] == rows[0]["part"]["utf8_end"] == 0


@pytest.mark.parametrize("terminal_revision,expected", [(0, "Latest pending"), (1, None), (2, None)])
def test_saved_questions_use_canonical_revision_terminal_and_message_precedence(terminal_revision, expected):
    from types import SimpleNamespace

    from litetui.codex_async_questions import latest_question

    def message(title, revision, state):
        return {"role": "assistant", "content": "", "provider_metadata": {
            "app_server_thread_id": "thread", "async_questions": [{"version": 1,
                "id": "question", "threadId": "thread", "revision": revision,
                "state": state, "questions": [{"title": title}]}]}}

    messages = [message("Stale pending", 1, "pending"), message("Latest pending", 1, "pending"),
                message("Terminal title", terminal_revision, "answered")]
    chosen = latest_question(SimpleNamespace(conversation=messages), "question", "thread")[1]
    assert chosen["questions"][0]["title"] == (expected or "Terminal title")
    view = history.project(messages)
    notices = [row for row in view.blocks if row.label == "Saved question"]
    assert len(notices) == (1 if expected else 0)
    if notices:
        assert expected in notices[0].content and "read only" in notices[0].content
        assert notices[0].kind == "notice" and notices[0].state == "incomplete"
    assert all("Stale pending" not in row.content and "Terminal title" not in row.content for row in view.blocks)
