"""The text of an inbox message, from either producer, on the real functions.

Two producers share the maildir. The Python CLI and MCP tool write a top-level `body`; the desktop
app's inbox nests the text as `payload.text` and writes no `body`. `format_message` read only
`body`, so the second kind rendered as a header line with nothing under it.
"""
from litetui import harness as harness_mod

# The shape the desktop app's relay writes (measured on real maildir files): no body, no priority.
APP_SHAPED = {
    "id": "00000000-0000-4000-8000-000000000001-1787449957924",
    "from": "orchestrator-chat",
    "to": "00000000-0000-4000-8000-000000000002",
    "type": "seat-message",
    "payload": {"text": "I'm coming.", "reply_to": "orchestrator-chat"},
    "ts": 1787449957924,
}
CLI_SHAPED = {"from": "1a2b3c4d-0000", "priority": "urgent", "type": "notification", "body": "ping"}


def test_an_app_shaped_message_renders_its_payload_text():
    out = harness_mod.format_message(APP_SHAPED)
    assert out == "[inbox from orchestr · normal]\nI'm coming."


def test_the_cli_shape_still_renders_its_body_and_priority():
    out = harness_mod.format_message(CLI_SHAPED)
    assert out == "[inbox from 1a2b3c4d · urgent]\nping"


def test_body_wins_when_both_exist():
    msg = {**APP_SHAPED, "body": "top-level words"}
    assert harness_mod.message_text(msg) == "top-level words"


def test_a_blank_body_falls_through_to_the_payload():
    msg = {**APP_SHAPED, "body": "   "}
    assert harness_mod.message_text(msg) == "I'm coming."


def test_payload_body_is_read_after_payload_text():
    assert harness_mod.message_text({"payload": {"body": "nested body"}}) == "nested body"
    assert harness_mod.message_text({"payload": {"text": "t", "body": "b"}}) == "t"


def test_missing_or_malformed_text_is_empty_and_never_raises():
    for msg in ({}, {"payload": None}, {"payload": "plain"}, {"payload": []},
                {"payload": {"text": 5}}, {"payload": {"text": None}}, {"body": 7},
                {"body": None, "payload": {"text": ["x"]}}):
        assert harness_mod.message_text(msg) == "", msg
        assert harness_mod.format_message(msg).endswith("\n"), msg


def test_surrounding_whitespace_is_stripped_from_either_shape():
    assert harness_mod.message_text({"body": "  a  "}) == "a"
    assert harness_mod.message_text({"payload": {"text": "\n b \n"}}) == "b"
