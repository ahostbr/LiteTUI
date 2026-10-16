use litetui_terminal::{
    demo::Demo,
    live::{ContentKind as K, Observation, Turn},
    live_chat::{Chat, Item},
    native_activity::{Activity, ResultState},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn request(id: &str, card: usize) -> Value {
    json!({"type":"native_activity","provider":"claude","kind":"tool_use","id":id,"name":"Read","card_index":card,"input":{"path":"test"},"complete":true,"result":null,"is_error":null})
}
fn result(id: &str, card: usize, error: Value) -> Value {
    json!({"type":"native_activity","provider":"claude","kind":"tool_result","id":id,"name":null,"card_index":card,"input":null,"complete":true,"result":"reported result","is_error":error})
}
fn activity(event: Value) -> Observation {
    Observation::Activity(Activity::from_event(&event).unwrap())
}
fn content(card: usize, kind: K, text: &str) -> Observation {
    Observation::Content {
        card,
        kind,
        text: text.into(),
    }
}
fn chat() -> Chat {
    let mut chat = Chat::new("claude".into(), "m".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("q").unwrap();
    chat
}
#[test]
fn native_schema_preserves_unknown_outcomes_and_refuses_ambiguous_or_malformed_facts() {
    for error in [Value::Null, json!(false), json!(true)] {
        assert!(Activity::from_event(&result("t", 0, error)).is_ok());
    }
    for (key, bad) in [
        ("card_index", json!(-1)),
        ("card_index", json!(100)),
        ("card_index", json!(true)),
        ("card_index", json!(0.5)),
        ("provider", json!("other")),
        ("kind", json!("execute")),
        ("id", json!("")),
        ("id", json!("x".repeat(513))),
        ("complete", json!(1)),
        ("input", json!([])),
    ] {
        let mut event = request("t", 0);
        event[key] = bad;
        assert!(Activity::from_event(&event).is_err(), "{key}");
    }
    for key in ["card_index", "is_error", "complete", "result"] {
        let mut event = result("t", 0, json!(false));
        event.as_object_mut().unwrap().remove(key);
        assert!(Activity::from_event(&event).is_err(), "{key}");
    }
    let mut event = result("t", 0, json!("false"));
    assert!(Activity::from_event(&event).is_err());
    event["is_error"] = Value::Null;
    event["complete"] = json!(false);
    assert!(Activity::from_event(&event).is_err());
}
#[test]
fn first_seen_order_is_retained_and_late_snapshots_target_only_the_named_card() {
    let mut chat = chat();
    chat.observe(content(0, K::AnswerDelta, "before")).unwrap();
    chat.observe(activity(request("t", 0))).unwrap();
    chat.observe(content(1, K::AnswerDelta, "after")).unwrap();
    chat.observe(content(1, K::ReasoningDelta, "second trace"))
        .unwrap();
    chat.observe(content(0, K::AnswerSnapshot, "corrected before"))
        .unwrap();
    chat.observe(content(0, K::ReasoningSnapshot, "first trace"))
        .unwrap();
    chat.observe(activity(result("t", 0, json!(true)))).unwrap();
    let row = &chat.rows[0];
    assert_eq!(row.items, vec![Item::Card(0), Item::Tool(0), Item::Card(1)]);
    assert_eq!(row.cards[0].answer, "corrected before");
    assert_eq!(row.cards[1].answer, "after");
    assert_eq!(row.cards[0].reasoning, "first trace");
    assert_eq!(row.cards[1].reasoning, "second trace");
    assert_eq!(
        row.tools[0].result,
        ResultState::Returned {
            text: "reported result".into(),
            is_error: Some(true)
        }
    );
    assert!(chat.observe(Observation::Text("ambiguous".into())).is_err());
    assert_eq!(chat.rows[0].cards[0].answer, "corrected before");
}
#[test]
fn orphan_results_do_not_fabricate_requests_and_pending_tools_become_incomplete() {
    let mut chat = chat();
    chat.observe(activity(result("orphan", 0, Value::Null)))
        .unwrap();
    chat.observe(activity(request("pending", 0))).unwrap();
    chat.observe(content(0, K::AnswerDelta, "answer")).unwrap();
    chat.observe(Observation::Complete).unwrap();
    assert!(!chat.rows[0].tools[0].request_seen);
    assert_eq!(chat.rows[0].tools[1].result, ResultState::Incomplete);
    chat.submit("next").unwrap();
    chat.observe(activity(request("pending", 0))).unwrap();
    assert_eq!(chat.rows[1].tools.len(), 1, "provider IDs scoped per turn");
    chat.finish(Some("closed".into()));
    assert_eq!(chat.rows[1].tools[0].result, ResultState::Incomplete);
    assert!(
        chat.observe(activity(result("pending", 0, json!(false))))
            .is_err()
    );
}
#[test]
fn ownership_identity_and_capacity_fail_before_mutating_a_row() {
    let mut chat = chat();
    chat.observe(activity(request("t", 0))).unwrap();
    let mut changed = request("t", 0);
    changed["name"] = json!("Write");
    assert!(chat.observe(activity(changed)).is_err());
    assert!(chat.observe(content(2, K::AnswerDelta, "jump")).is_err());
    assert!(chat.observe(activity(request("new", 1))).is_err());
    let mut oversized = result("t", 0, json!(false));
    oversized["result"] = json!("\n".repeat(2000));
    assert!(chat.observe(activity(oversized)).is_err());
    assert_eq!(chat.rows[0].tools.len(), 1);
    assert_eq!(chat.rows[0].tools[0].result, ResultState::Pending);
    assert_eq!(chat.rows[0].cards.len(), 1);
    chat.observe(content(0, K::AnswerDelta, "kept")).unwrap();
    assert!(
        chat.observe(content(0, K::ReasoningDelta, &"\u{061c}".repeat(5000)))
            .is_err()
    );
}
#[test]
fn live_renderer_keeps_native_tools_between_cards_and_shows_reported_failure() {
    let mut app = Demo::live("claude".into(), "m".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("q");
    app.submit();
    for observation in [
        content(0, K::AnswerDelta, "BEFORE"),
        activity(request("t", 0)),
        content(1, K::AnswerDelta, "AFTER"),
        activity(result("t", 0, json!(true))),
    ] {
        app.observe_live(observation).unwrap();
    }
    let mut terminal = Terminal::new(TestBackend::new(140, 70)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    assert!(text.find("BEFORE").unwrap() < text.find("Claude · Read").unwrap());
    assert!(text.find("Claude · Read").unwrap() < text.find("AFTER").unwrap());
    for expected in [
        "reported failure",
        "Native tool t",
        "reported result",
        "not task completion",
    ] {
        assert!(text.contains(expected), "{expected}");
    }
    assert!(!text.contains("mock"));
}
#[test]
fn completion_uses_all_owned_answer_cards_and_tool_records_never_satisfy_it() {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"m"}),"claude","m").unwrap();
    let command = turn.submit("q").unwrap();
    turn.accept(
        &json!({"type":"response","id":command["id"],"ok":true,"result":{"turn":"accepted"}}),
        "claude",
        "m",
    )
    .unwrap();
    turn.accept(&json!({"type":"turn_start","model":"m"}), "claude", "m")
        .unwrap();
    turn.accept(&request("t", 0), "claude", "m").unwrap();
    assert!(
        turn.accept(
            &json!({"type":"turn_end","stopReason":"stop"}),
            "claude",
            "m"
        )
        .is_err()
    );
    assert!(
        turn.accept(
            &json!({"type":"text_delta","text":"unowned"}),
            "claude",
            "m"
        )
        .is_err()
    );
    for (card, text) in [(0, "before"), (1, "after")] {
        turn.accept(
            &json!({"type":"text_delta","card_index":card,"text":text}),
            "claude",
            "m",
        )
        .unwrap();
    }
    turn.accept(
        &json!({"type":"native_text_snapshot","card_index":0,"text":""}),
        "claude",
        "m",
    )
    .unwrap();
    assert_eq!(
        turn.accept(
            &json!({"type":"turn_end","stopReason":"stop"}),
            "claude",
            "m"
        )
        .unwrap(),
        Observation::Complete
    );
    assert!(
        turn.accept(&result("t", 0, json!(false)), "claude", "m")
            .is_err()
    );
}

#[test]
fn empty_card_announcement_precedes_tool_ownership_and_input_updates_are_authoritative() {
    let mut chat = chat();
    chat.observe(activity(request("first", 0))).unwrap();
    chat.observe(content(1, K::AnswerSnapshot, "")).unwrap();
    chat.observe(activity(request("recap-tool", 1))).unwrap();
    let mut partial = request("recap-tool", 1);
    partial["complete"] = json!(false);
    partial["input"] = json!({"path":"updated partial"});
    chat.observe(activity(partial)).unwrap();
    chat.observe(content(2, K::AnswerDelta, "Final")).unwrap();
    let row = &chat.rows[0];
    assert_eq!(
        row.items,
        vec![
            Item::Card(0),
            Item::Tool(0),
            Item::Card(1),
            Item::Tool(1),
            Item::Card(2)
        ]
    );
    assert!(row.cards[1].answer.is_empty());
    assert!(!row.tools[1].input_complete);
    assert!(
        row.tools[1]
            .input
            .as_ref()
            .unwrap()
            .contains("updated partial")
    );
}
