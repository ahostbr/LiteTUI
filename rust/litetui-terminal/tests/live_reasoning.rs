use litetui_terminal::{
    demo::Demo,
    live::{Observation, Turn},
    live_chat::{Chat, ReplyState},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::json;
fn turn(backend: &str) -> Turn {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":backend,"model":"m"}),backend,"m").unwrap();
    let command = turn.submit("question").unwrap();
    turn.accept(
        &json!({"type":"response","id":command["id"],"ok":true,"result":{"turn":"accepted"}}),
        backend,
        "m",
    )
    .unwrap();
    turn.accept(&json!({"type":"turn_start","model":"m"}), backend, "m")
        .unwrap();
    turn
}
#[test]
fn generic_calls_and_late_results_keep_their_transcript_positions() {
    use litetui_terminal::{live_chat::Item, native_activity::ResultState};
    let mut turn = turn("custom");
    let mut chat = Chat::new("custom".into(), "m".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("question").unwrap();
    let id = "00112233-4455-4677-8899-aabbccddeeff";
    let request = json!({"type":"generic_tool_requested","observation_id":id,
        "round":0,"slot":0,"provider_call_id":"p","name":"read"});
    let result = json!({"type":"generic_tool_returned","observation_id":id,
        "result":"observed result","ok":true});
    assert!(turn.accept(&result, "custom", "m").is_err());
    for event in [
        json!({"type":"text_delta","text":"Before"}),
        request.clone(),
        json!({"type":"tool_call","name":"read","args":{}}),
        json!({"type":"text_delta","text":"After"}),
        result.clone(),
        json!({"type":"text_delta","text":" the tool."}),
    ] {
        chat.observe(turn.accept(&event, "custom", "m").unwrap())
            .unwrap();
    }
    assert!(turn.accept(&request, "custom", "m").is_err());
    assert!(turn.accept(&result, "custom", "m").is_err());
    let row = &chat.rows[0];
    assert_eq!(row.items, [Item::Card(0), Item::Tool(0), Item::Card(1)]);
    let timing = &row.tools[0].timing;
    let now = std::time::Instant::now();
    assert!(timing.elapsed_at(now).is_some());
    assert_eq!(
        timing.elapsed_at(now),
        timing.elapsed_at(now + std::time::Duration::from_secs(600))
    );
    assert_eq!(row.cards[0].answer, "Before");
    assert_eq!(row.cards[1].answer, "After the tool.");
    assert_eq!(
        row.tools[0].result,
        ResultState::Returned {
            text: "observed result".into(),
            is_error: Some(false)
        }
    );
}
#[test]
fn native_reasoning_requires_explicit_reconciliation_and_never_counts_as_answer() {
    let mut turn = turn("claude");
    assert_eq!(
        turn.accept(
            &json!({"type":"reasoning_delta","text":"old","reconcile":"append"}),
            "claude",
            "m"
        )
        .unwrap(),
        Observation::Reasoning("old".into())
    );
    assert_eq!(
        turn.accept(
            &json!({"type":"reasoning_delta","text":"correct","reconcile":"replace"}),
            "claude",
            "m"
        )
        .unwrap(),
        Observation::ReasoningSnapshot("correct".into())
    );
    for mode in [json!(null), json!(true), json!("unknown")] {
        assert!(
            turn.accept(
                &json!({"type":"reasoning_delta","text":"bad","reconcile":mode}),
                "claude",
                "m"
            )
            .is_err()
        );
    }
    assert!(
        turn.accept(
            &json!({"type":"reasoning_delta","text":"ambiguous"}),
            "claude",
            "m"
        )
        .is_err()
    );
    assert!(
        turn.accept(
            &json!({"type":"turn_end","stopReason":"stop","error":null}),
            "claude",
            "m"
        )
        .is_err()
    );
    assert!(
        Turn::default()
            .accept(
                &json!({"type":"reasoning_delta","text":"early","reconcile":"append"}),
                "claude",
                "m"
            )
            .is_err()
    );
}
#[test]
fn generic_append_only_reasoning_preserves_existing_wire_contract() {
    let mut turn = turn("codex");
    assert_eq!(
        turn.accept(
            &json!({"type":"reasoning_delta","text":"trace"}),
            "codex",
            "m"
        )
        .unwrap(),
        Observation::Reasoning("trace".into())
    );
    turn.accept(&json!({"type":"text_delta","text":"answer"}), "codex", "m")
        .unwrap();
    turn.accept(
        &json!({"type":"turn_end","stopReason":"stop","error":null}),
        "codex",
        "m",
    )
    .unwrap();
    assert!(
        turn.accept(
            &json!({"type":"reasoning_delta","text":"late"}),
            "codex",
            "m"
        )
        .is_err()
    );
}
#[test]
fn replacement_reasoning_reaches_the_existing_thinking_card_without_duplication() {
    let mut app = Demo::live("claude".into(), "m".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("question");
    app.submit();
    app.observe_live(Observation::Reasoning("discarded prefix".into()))
        .unwrap();
    app.observe_live(Observation::ReasoningSnapshot("correct trace".into()))
        .unwrap();
    app.observe_live(Observation::Reasoning(" tail\u{1b}".into()))
        .unwrap();
    assert!(
        app.live.as_ref().unwrap().rows[0].cards[0]
            .answer
            .is_empty()
    );
    app.observe_live(Observation::Text("actual answer".into()))
        .unwrap();
    app.observe_live(Observation::Complete).unwrap();
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    for expected in [
        "Thinking",
        "correct trace tail",
        "actual answer",
        "┏",
        "╍",
        "╏",
        "┗",
    ] {
        assert!(text.contains(expected), "{expected}");
    }
    assert!(text.contains("\\u{1b}"));
    assert!(!text.contains('\u{1b}'));
    assert!(!text.contains("discarded prefix"));
    assert!(!text.contains("mock-model"));
    assert_eq!(
        app.live.as_ref().unwrap().rows[0].state,
        ReplyState::Complete
    );
}
#[test]
fn replacement_traces_still_charge_the_shared_emitted_content_budget() {
    let mut turn = turn("claude");
    let event =
        json!({"type":"reasoning_delta", "text":"x".repeat(1024 * 1024), "reconcile":"replace"});
    for _ in 0..8 {
        turn.accept(&event, "claude", "m").unwrap();
    }
    assert!(
        turn.accept(&json!({"type":"text_delta", "text":"x"}), "claude", "m")
            .unwrap_err()
            .to_string()
            .contains("content budget")
    );
}
#[test]
fn reasoning_uses_shared_transcript_budget_and_replacement_releases_retained_units() {
    let mut chat = Chat::new("claude".into(), "m".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("q").unwrap();
    assert!(
        chat.observe(Observation::Reasoning("\n".repeat(65536)))
            .is_err()
    );
    assert!(chat.rows[0].cards[0].reasoning.is_empty());
    chat.observe(Observation::Reasoning("\n".repeat(900)))
        .unwrap();
    assert!(chat.observe(Observation::Text("\n".repeat(900))).is_err());
    chat.observe(Observation::ReasoningSnapshot(String::new()))
        .unwrap();
    chat.observe(Observation::Text("\n".repeat(900))).unwrap();
    chat.finish(Some("interrupted".into()));
    assert_eq!(chat.rows[0].state, ReplyState::Incomplete);
    assert!(chat.observe(Observation::Reasoning("late".into())).is_err());
}
