import copy
import json

import pytest

from litetui.conversation_export import export, markdown


def meta(result="result", thread="thread"):
    return {"provider": "codex", "app_server_thread_id": thread,
            "display_trace": {"version": 1, "items": [
                {"id": "tool", "turnId": "turn", "kind": "commandExecution", "name": "command",
                 "state": "interrupted", "result": result, "durationMs": 0},
                {"id": "answer", "turnId": "turn", "kind": "agentMessage", "result": "answer"},
            ]}}


def test_export_deduplicates_mirrored_metadata_and_uses_latest_results():
    messages = [{"role": "user", "content": "question", "provider_metadata": meta("old")},
                {"role": "assistant", "content": "answer", "provider_metadata": meta("recovered")}]
    before = copy.deepcopy(messages)
    text = markdown(messages)
    assert text.count("recovered") == 1 and "old" not in text
    assert text.count("answer") == 1
    assert "interrupted · 0s" in text
    assert text.index("question") < text.index("recovered") < text.index("answer")
    assert messages == before


def test_export_does_not_join_ids_across_threads_or_include_image_bytes():
    text = markdown([{"role": "user", "content": [{"type": "image_url", "image_url": {
        "url": "data:image/png;base64,PRIVATE_BYTES"}}], "provider_metadata": meta(thread="one")},
                     {"role": "assistant", "provider_metadata": meta(thread="two")}])
    assert text.count("## Tool activity") == 2
    assert "Image attachment" in text and "PRIVATE_BYTES" not in text


def test_export_fences_untrusted_markdown_and_reports_unknown_duration():
    metadata = meta("```\n# injected")
    metadata["display_trace"]["items"][0].pop("durationMs")
    text = markdown([{"role": "assistant", "provider_metadata": metadata}])
    assert "unknown duration" in text
    assert "````\n```\n# injected\n````" in text


def test_file_export_replays_edits_and_never_overwrites(tmp_path):
    source, destination = tmp_path / "convo.jsonl", tmp_path / "export.md"
    rows = [{"type": "msg", "message": {"role": "user", "content": "old"}},
            {"type": "edit", "index": 0, "message": {"role": "user", "content": "new"}}]
    source.write_text("\n".join(map(json.dumps, rows)), encoding="utf-8")
    original = source.read_bytes()
    export(source, destination)
    assert "new" in destination.read_text(encoding="utf-8")
    assert source.read_bytes() == original
    with pytest.raises(FileExistsError):
        export(source, destination)


def test_cli_export_returns_before_constructing_app(monkeypatch, tmp_path):
    from litetui import app, cli

    def forbidden(*args, **kwargs):
        pytest.fail("export constructed the app")

    monkeypatch.setattr(app, "LiteTUI", forbidden)
    source, destination = tmp_path / "convo.jsonl", tmp_path / "out.md"
    source.write_text(json.dumps({"type": "msg", "message": {"role": "user", "content": "hello"}}), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["litetui", "--export-conversation", str(source),
                                     "--export-output", str(destination)])
    cli.main()
    assert "hello" in destination.read_text(encoding="utf-8")


@pytest.mark.parametrize("state", ["pending", "cancel", "unanswered", "answered"])
def test_questions_use_latest_revision_and_keep_delivery_distinct(state):
    pending = {"version": 1, "id": "question", "threadId": "thread", "state": "pending",
               "questions": [{"title": "Choose a color", "options": ["Blue", "Green"]}]}
    current = {**pending, "state": state, "revision": 2}
    owner = {"async_questions": [current]}
    if state == "answered":
        current["deliveryId"] = "delivery"
        owner["steering"] = [{"id": "delivery", "state": "queued", "item": {"content": "My answer"}}]
    messages = [{"role": "user", "provider_metadata": owner},
                {"role": "assistant", "provider_metadata": {"async_questions": [pending]}}]
    before = copy.deepcopy(messages)
    text = markdown(messages)
    assert text.count("## Codex question") == 1
    assert f"State: {state}" in text
    assert "Option: Blue" in text
    assert ("My answer" in text) == (state == "answered")
    if state == "answered":
        assert "Delivery: queued" in text
        messages.append({"role": "user", "content": "My answer", "codex_delivery": {"id": "delivery"}})
        assert markdown(messages).count("My answer") == 1
        messages.pop()
    assert messages == before
