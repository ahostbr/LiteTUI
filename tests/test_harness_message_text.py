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
    assert out == f"[inbox from orchestr · normal · id {APP_SHAPED['id']}]\nI'm coming."


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


# ── T0705: the delivered turn names the message ─────────────────────────────────────────────────
# A seat that must act on a message (`liteharness clerk forward <msg-id>`, `clerk digest`) was shown only
# "[inbox from f869ff86 · normal]" and had to scan the maildir with shell pipes to learn the id.
MSG_ID = "4d0f376f-2556-43d2-9e02-d09b839b0f86"
THREAD_ID = "224e71e0-9cd5-45a2-841b-647dd3c7cb56"
REAL_SHAPED = {"id": MSG_ID, "from": "f869ff86-1111-4222-8333-444444444444", "thread_id": THREAD_ID,
               "priority": "normal", "type": "notification", "body": "Clerk: noted."}


def test_the_header_carries_the_full_message_id_and_thread_id():
    out = harness_mod.format_message(REAL_SHAPED)
    assert out == f"[inbox from f869ff86 · normal · id {MSG_ID} · thread {THREAD_ID}]\nClerk: noted."
    assert out.splitlines()[0].count(MSG_ID) == 1, "the id is whole, not cut to the 8-char sender prefix"


def test_a_message_without_a_thread_names_only_its_id():
    out = harness_mod.format_message({k: v for k, v in REAL_SHAPED.items() if k != "thread_id"})
    assert out.splitlines()[0] == f"[inbox from f869ff86 · normal · id {MSG_ID}]"


def test_a_message_with_no_id_keeps_the_old_header():
    assert harness_mod.format_message(CLI_SHAPED) == "[inbox from 1a2b3c4d · urgent]\nping"


def test_an_id_that_would_break_the_header_line_is_left_out():
    # The approval relay's thread_id is a JSON object; a bracket or newline in an id would split the line.
    for bad in ('{"kind": "approval", "id": "appr-1"}', "a b", "x]\ny", "", 5, None, ["x"]):
        out = harness_mod.format_message({**REAL_SHAPED, "thread_id": bad, "id": bad})
        assert out == "[inbox from f869ff86 · normal]\nClerk: noted.", bad


def test_a_polled_message_reaches_the_seat_with_its_id(tmp_path, monkeypatch):
    """The real path: a file in new/ -> Seat.poll() -> format_message."""
    import json

    new = tmp_path / "new"
    new.mkdir()
    monkeypatch.setattr(harness_mod, "NEW", new)
    monkeypatch.setattr(harness_mod, "DONE", tmp_path / "done")
    # Redirected FIRST, then un-gated: poll() moves files, so it must never see the live maildir.
    assert harness_mod.NEW != harness_mod.INBOX_ROOT / "new"
    monkeypatch.delenv(harness_mod.NO_HARNESS_ENV, raising=False)
    seat = harness_mod.Seat("seat-a", "Seat A", "none")
    (new / "m.json").write_text(json.dumps({**REAL_SHAPED, "to": "seat-a"}), encoding="utf-8")
    delivered = seat.poll()
    assert [harness_mod.format_message(m).splitlines()[0] for m in delivered] == [
        f"[inbox from f869ff86 · normal · id {MSG_ID} · thread {THREAD_ID}]"]


def test_the_turn_the_model_receives_carries_the_message_id(monkeypatch):
    """The real delivery function: the first line of the user turn, as started for the model."""
    from types import SimpleNamespace

    from litetui import hook_host
    from litetui.app import LiteTUI
    from litetui.settings import Settings

    started = []
    monkeypatch.setattr(hook_host, "start_prompt", lambda app, item: started.append(item))
    app = SimpleNamespace(settings=Settings(), _pending_input=[], _chat_running=lambda: False,
                          _user_bubble=lambda *a, **k: None, _gui_quitting=False)
    LiteTUI._deliver_inbox(app, REAL_SHAPED)
    assert started, "the message must start a turn"
    first = started[0]["content"].splitlines()[0]
    assert f"id {MSG_ID}" in first and f"thread {THREAD_ID}" in first
