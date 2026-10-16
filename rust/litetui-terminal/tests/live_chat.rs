use litetui_terminal::{
    demo::Demo,
    live::{Observation, Turn},
    live_chat::{Chat, ReplyState},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::json;

#[test]
fn composer_uses_live_rows_never_mock_turns_and_preserves_busy_draft() {
    let mut app = Demo::live("claude".into(), "claude-sonnet-5-5".into());
    app.insert_text("first");
    app.submit();
    assert_eq!(app.composer.text(), "first");
    app.observe_live(Observation::Ready).unwrap();
    app.submit();
    assert!(app.turns.is_empty());
    assert!(app.tools.is_empty());
    assert!(app.composer.text().is_empty());
    assert_eq!(
        app.live.as_mut().unwrap().take_submission().as_deref(),
        Some("first")
    );
    app.insert_text("second");
    app.submit();
    assert_eq!(app.composer.text(), "second");
    app.observe_live(Observation::Text("Hello".into())).unwrap();
    app.observe_live(Observation::Complete).unwrap();
    app.submit();
    assert_eq!(app.live.as_ref().unwrap().rows.len(), 2);
    assert_eq!(
        app.live.as_ref().unwrap().rows[0].state,
        ReplyState::Complete
    );
    app.reset();
    app.open_approval();
    app.next_tool();
    app.previous_tool();
    app.toggle_tool();
    app.tick(999);
    assert!(app.live.is_some());
    assert!(!app.approval_open);
    assert!(app.tools.is_empty());
}
#[test]
fn live_view_renders_real_rows_in_existing_geometry_without_fixtures() {
    let mut app = Demo::live("claude".into(), "claude-sonnet-5-5".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("real question");
    app.submit();
    app.observe_live(Observation::Text("actual reply".into()))
        .unwrap();
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    for expected in [
        "Rust LIVE",
        "real question",
        "actual reply",
        "Assistant",
        "Streaming",
        "╭",
        "╰",
    ] {
        assert!(text.contains(expected), "{expected}");
    }
    for forbidden in [
        "mock-model",
        "Preview reply:",
        "Show me the tool calls",
        "LOCAL PREVIEW",
        "Arguments",
    ] {
        assert!(!text.contains(forbidden), "{forbidden}");
    }
}
#[test]
fn snapshots_replace_without_duplication_and_failure_is_not_completion() {
    let mut chat = Chat::new("claude".into(), "model".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("question").unwrap();
    chat.observe(Observation::Text("old text".into())).unwrap();
    chat.observe(Observation::Snapshot("replacement".into()))
        .unwrap();
    assert_eq!(chat.rows[0].cards[0].answer, "replacement");
    chat.finish(Some("transport failed".into()));
    assert_eq!(chat.rows[0].state, ReplyState::Incomplete);
    assert!(chat.submit("do not send").is_err());
}
#[test]
fn slash_commands_and_closed_or_quitting_views_cannot_submit() {
    let mut chat = Chat::new("claude".into(), "model".into());
    chat.observe(Observation::Ready).unwrap();
    assert!(chat.submit("/settings").is_err());
    assert!(chat.rows.is_empty());
    chat.begin_quit();
    assert!(chat.submit("hi").is_err());
}
#[test]
fn sequential_commands_have_unique_ids_and_late_ack_cannot_match() {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"model"}),"claude","model").unwrap();
    let first = turn.submit("first").unwrap();
    assert!(turn.submit("concurrent").is_err());
    for event in [
        json!({"type":"response","id":first["id"],"ok":true,"result":{"turn":"accepted"}}),
        json!({"type":"turn_start","model":"model"}),
        json!({"type":"text_delta","text":"Hello"}),
        json!({"type":"turn_end","stopReason":"stop"}),
    ] {
        turn.accept(&event, "claude", "model").unwrap();
    }
    let second = turn.submit("second").unwrap();
    assert_ne!(first["id"], second["id"]);
    assert!(
        turn.accept(
            &json!({"type":"response","id":first["id"],"ok":true,"result":{"turn":"accepted"}}),
            "claude",
            "model"
        )
        .is_err()
    );
}
#[test]
fn an_empty_authoritative_snapshot_does_not_complete_a_vanished_answer() {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"model"}),"claude","model").unwrap();
    let command = turn.submit("hi").unwrap();
    for event in [
        json!({"type":"response","id":command["id"],"ok":true,"result":{"turn":"accepted"}}),
        json!({"type":"turn_start","model":"model"}),
        json!({"type":"text_delta","text":"old"}),
        json!({"type":"native_text_snapshot","text":""}),
    ] {
        turn.accept(&event, "claude", "model").unwrap();
    }
    assert!(
        turn.accept(
            &json!({"type":"turn_end","stopReason":"stop"}),
            "claude",
            "model"
        )
        .is_err()
    );
}

#[test]
fn live_submission_preserves_whitespace_after_the_composer_input_contract() {
    let mut app = Demo::live("claude".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("    indented text  ");
    app.submit();
    let chat = app.live.as_mut().unwrap();
    assert_eq!(
        chat.take_submission().as_deref(),
        Some("    indented text  ")
    );
    assert_eq!(chat.rows[0].prompt, "    indented text  ");
    // The transport/view adds no trimming even for a multiline caller; the
    // single-line composer has its separately tested paste normalization.
    let mut chat = Chat::new("claude".into(), "model".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("    indented text\n\n").unwrap();
    assert_eq!(
        chat.take_submission().as_deref(),
        Some("    indented text\n\n")
    );
}
#[test]
fn newline_dense_answers_are_bounded_before_layout_and_never_truncated() {
    let mut app = Demo::live("claude".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("question");
    app.submit();
    assert!(
        app.observe_live(Observation::Text("\n".repeat(65536)))
            .is_err()
    );
    assert!(
        app.live.as_ref().unwrap().rows[0].cards[0]
            .answer
            .is_empty()
    );
    app.observe_live(Observation::Text("\n".repeat(1000)))
        .unwrap();
    let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert!(app.max_scroll < 2048);
    assert_eq!(
        app.live.as_ref().unwrap().rows[0].cards[0].answer.len(),
        1000
    );
}
