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

#[test]
fn approval_stop_preserves_partial_content_and_reason_across_followup_and_shutdown() {
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("first");
    app.submit();
    app.live.as_mut().unwrap().take_submission();
    app.observe_live(Observation::Text("partial answer".into()))
        .unwrap();
    app.observe_live(Observation::ApprovalStopped(
        "Denied \u{1b}[2J\u{202e}write".into(),
    ))
    .unwrap();
    let chat = app.live.as_ref().unwrap();
    assert!(chat.ready && !chat.busy && !chat.closed);
    assert_eq!(chat.rows[0].state, ReplyState::ApprovalStopped);
    assert_eq!(chat.rows[0].cards[0].answer, "partial answer");
    assert_eq!(
        chat.rows[0].stop_reason.as_deref(),
        Some("Denied \u{1b}[2J\u{202e}write")
    );
    let mut terminal = Terminal::new(TestBackend::new(110, 50)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cells = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    assert!(cells.contains("Stopped · approval"));
    assert!(cells.contains(r"Denied \u{1b}[2J\u{202e}write"));
    assert!(!cells.contains("Completed · stop"));
    app.insert_text("second");
    app.submit();
    app.live.as_mut().unwrap().take_submission();
    app.observe_live(Observation::Text("next answer".into()))
        .unwrap();
    app.observe_live(Observation::Complete).unwrap();
    app.finish_live(None);
    let chat = app.live.as_ref().unwrap();
    assert_eq!(chat.rows[0].state, ReplyState::ApprovalStopped);
    assert_eq!(chat.rows[1].state, ReplyState::Complete);
    assert_eq!(chat.rows[1].stop_reason, None);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cells = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    assert!(cells.contains("Stopped · approval") && cells.contains("Completed · stop"));
    assert!(cells.contains(r"Denied \u{1b}[2J\u{202e}write"));
}

#[test]
fn approval_stop_obeys_pending_row_approval_and_display_limits() {
    use litetui_terminal::approvals::Ledger;
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    assert!(
        chat.observe(Observation::ApprovalStopped("no row".into()))
            .is_err()
    );
    chat.submit("question").unwrap();
    assert!(
        chat.observe(Observation::ApprovalStopped("\n".repeat(1024)))
            .is_err()
    );
    assert!(chat.busy);
    assert_eq!(chat.rows[0].state, ReplyState::Sending);
    assert!(chat.rows[0].stop_reason.is_none());
    let mut ledger = Ledger::default();
    let request = ledger
        .request(&json!({
            "type":"tool_approval_requested","id":"open","tool":"write","input":{},
            "profile":"strict","why":"test","timeout_s":300
        }))
        .unwrap();
    chat.observe(Observation::Approval(request)).unwrap();
    assert!(
        chat.observe(Observation::ApprovalStopped("not closed".into()))
            .is_err()
    );
    assert!(chat.busy);
    chat.observe(Observation::Approval(
        ledger
            .close(&json!({
                "type":"tool_approval_closed","id":"open","outcome":"denied","reason":"answered"
            }))
            .unwrap(),
    ))
    .unwrap();
    chat.observe(Observation::ApprovalStopped("refused".into()))
        .unwrap();
    assert!(
        chat.observe(Observation::ApprovalStopped("duplicate".into()))
            .is_err()
    );
}

#[test]
fn stopped_reasons_are_retained_with_independent_row_budgets() {
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("first").unwrap();
    chat.observe(Observation::ApprovalStopped("x".repeat(16000)))
        .unwrap();
    chat.submit("second").unwrap();
    chat.observe(Observation::ApprovalStopped("x".repeat(16000)))
        .unwrap();
    chat.submit("third").unwrap();
    assert_eq!(chat.rows.len(), 3);
    assert_eq!(
        chat.rows[0].stop_reason.as_deref(),
        Some("x".repeat(16000).as_str())
    );
    assert_eq!(
        chat.rows[1].stop_reason.as_deref(),
        Some("x".repeat(16000).as_str())
    );
    assert!(chat.rows[0].render_units() >= 16000);
    assert!(chat.rows[1].render_units() >= 16000);
}

#[test]
fn native_continuation_preserves_draft_and_blocks_until_saved_queue_readback() {
    use litetui_terminal::restored_queue::{Intent, Snapshot, Update};
    for refused in [false, true] {
        let mut app = Demo::live("codex".into(), "model".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("UNSENT HUMAN DRAFT");
        let value = json!({"session_id":"session","backend":"codex","model":"model","policy":"hold",
            "held":1,"snapshot":"a".repeat(64),"entries":[
                {"id":"saved","state":"queued","revision":1,"content":"/literal recovered input","source":"queued",
                "tool_profile":"strict","thread_id":"thread","turn_id":"turn","can_cancel":true,"can_continue":true}]});
        app.observe_live(Observation::Queue(Update::Snapshot(
            Snapshot::parse(&value, 1).unwrap(),
        )))
        .unwrap();
        app.live
            .as_mut()
            .unwrap()
            .prepare_continuation(&Intent::Continue {
                revision: 1,
                entry: "saved".into(),
            })
            .unwrap();
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
        app.observe_live(Observation::Submission {
            id: "continued".into(),
        })
        .unwrap();
        if refused {
            app.observe_live(Observation::Refused("below floor".into()))
                .unwrap();
            assert_eq!(
                app.live.as_ref().unwrap().rows[0].state,
                ReplyState::Refused
            );
            let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
            terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
            let text: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Not started"));
        } else {
            app.observe_live(Observation::Text("continued reply".into()))
                .unwrap();
            app.observe_live(Observation::Complete).unwrap();
        }
        assert!(app.live.as_ref().unwrap().queue_pending());
        assert!(
            app.live
                .as_ref()
                .unwrap()
                .notice
                .contains("Checking saved input")
        );
        app.submit();
        assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        assert!(app.live.as_mut().unwrap().submit("too early").is_err());
        app.observe_live(Observation::Queue(Update::Snapshot(
            Snapshot::parse(&value, 2).unwrap(),
        )))
        .unwrap();
        assert!(!app.live.as_ref().unwrap().queue_pending());
        app.submit();
        assert_eq!(
            app.live.as_mut().unwrap().take_submission().as_deref(),
            Some("UNSENT HUMAN DRAFT")
        );
    }
}
#[test]
fn only_local_continuation_rejection_can_settle_before_submission() {
    use litetui_terminal::restored_queue::{Intent, Snapshot, Update};
    let mut chat = Chat::new("codex".into(), "model".into());
    chat.observe(Observation::Ready).unwrap();
    let snapshot = Snapshot::parse(&json!({"session_id":"session","backend":"codex","model":"model","policy":"hold",
        "held":1,"snapshot":"a".repeat(64),"entries":[
            {"id":"saved","state":"queued","revision":1,"content":"saved","source":"queued",
            "tool_profile":"strict","thread_id":"thread","turn_id":"turn","can_cancel":true,"can_continue":true}]}), 1).unwrap();
    chat.observe(Observation::Queue(Update::Snapshot(snapshot)))
        .unwrap();
    chat.prepare_continuation(&Intent::Continue {
        revision: 1,
        entry: "saved".into(),
    })
    .unwrap();
    chat.observe(Observation::Queue(Update::Rejected(
        "unrelated read rejected".into(),
    )))
    .unwrap();
    assert!(chat.busy && chat.queue_pending());
    chat.observe(Observation::Queue(Update::ContinuationRejected(
        "stale selection".into(),
    )))
    .unwrap();
    assert!(!chat.busy && !chat.queue_pending());
    assert_eq!(chat.rows[0].state, ReplyState::Refused);
    assert!(chat.submit("next").is_ok());
}

#[test]
fn reconciled_input_is_a_saved_observation_without_execution_or_draft_submission() {
    use litetui_terminal::restored_queue::{Snapshot, Update};
    let mut app = Demo::live("codex".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("HUMAN DRAFT");
    let value = json!({"session_id":"session","backend":"codex","model":"model","policy":"hold",
        "held":1,"snapshot":"a".repeat(64),"entries":[{"id":"saved","state":"uncertain","revision":2,
        "content":"Previously sent input","source":"queued","tool_profile":"strict",
        "thread_id":"thread","turn_id":"turn","can_cancel":false,"can_reconcile":true}]});
    let mut snapshot = Snapshot::parse(&value, 1).unwrap();
    snapshot.recovered = Some(Box::new(snapshot.entries.remove(0)));
    app.observe_live(Observation::Queue(Update::Snapshot(snapshot.clone())))
        .unwrap();
    let chat = app.live.as_mut().unwrap();
    assert!(!chat.busy && !chat.queue_pending());
    assert!(chat.take_submission().is_none());
    assert_eq!(chat.rows[0].state, ReplyState::Reconciled);
    assert!(chat.rows[0].cards.is_empty() && chat.rows[0].tools.is_empty());
    assert_eq!(app.composer.text(), "HUMAN DRAFT");
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
    let text: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect();
    assert!(text.contains("Previously accepted") && text.contains("not resent"));
    assert!(!text.contains("Completed · stop") && !text.contains("Assistant ·"));
    assert!(
        app.observe_live(Observation::Queue(Update::Snapshot(snapshot)))
            .is_err()
    );
    app.submit();
    assert_eq!(
        app.live.as_mut().unwrap().take_submission().as_deref(),
        Some("HUMAN DRAFT")
    );
}

#[test]
fn source_refusal_and_failure_render_without_completion_or_automatic_retry() {
    for failed in [false, true] {
        let mut app = Demo::live("claude".into(), "fixture".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("original request");
        app.submit();
        assert_eq!(
            app.live.as_mut().unwrap().take_submission().as_deref(),
            Some("original request")
        );
        app.insert_text("UNSENT DRAFT");
        if failed {
            app.observe_live(Observation::Text("PARTIAL ANSWER".into()))
                .unwrap();
            app.observe_live(Observation::Failed(
                "Lost provider connection \u{1b}[31m".into(),
            ))
            .unwrap();
        } else {
            app.observe_live(Observation::Information(
                "Claude cache warning: effort changes".into(),
            ))
            .unwrap();
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
            app.observe_live(Observation::Refused("Nothing was sent \u{1b}[31m".into()))
                .unwrap();
        }
        let chat = app.live.as_mut().unwrap();
        assert_eq!(
            chat.rows[0].state,
            if failed {
                ReplyState::Failed
            } else {
                ReplyState::Refused
            }
        );
        assert!(chat.take_submission().is_none());
        assert!(
            !chat.notice.contains("completed") && !chat.notice.contains("Checking saved input")
        );
        let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
        terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
        let text: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(text.contains(if failed { "Failed" } else { "Not started" }));
        assert!(text.contains("\\u{1b}[31m"));
        if failed {
            assert!(text.contains("PARTIAL ANSWER"));
        }
        assert!(!text.contains("Completed · stop"));
        assert_eq!(app.composer.text(), "UNSENT DRAFT");
        app.submit();
        assert_eq!(
            app.live.as_mut().unwrap().take_submission().as_deref(),
            Some("UNSENT DRAFT")
        );
        assert_eq!(app.live.as_ref().unwrap().rows.len(), 2);
    }
}
