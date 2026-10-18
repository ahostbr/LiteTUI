use litetui_terminal::{
    approvals::{Decision, Event, Ledger, View},
    live::{Observation, Turn},
};
use serde_json::{Value, json};

fn request(id: &str) -> Value {
    json!({"type":"tool_approval_requested", "id":id, "tool":"write", "input":{"path":"not-executed.txt"}, "profile":"strict", "why":"workspace write", "timeout_s":300.0})
}
fn closed(id: &str) -> Value {
    json!({"type":"tool_approval_closed", "id":id, "outcome":"denied", "reason":"answered"})
}
fn decision(id: &str, allow: bool) -> Decision {
    Decision {
        id: id.into(),
        allow,
    }
}
fn response(command: &Value, accepted: bool) -> Value {
    if accepted {
        json!({"type":"response", "id":command["id"], "ok":true,
        "result":{"approved":command["approval_id"], "allow":command["allow"], "id_key":"approval_id"}})
    } else {
        json!({"type":"response", "id":command["id"], "ok":false, "error":"already closed"})
    }
}
#[test]
fn command_and_request_ids_differ_ack_is_not_closure_and_never_remember() {
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    let command = ledger.decide(decision("a", false)).unwrap().unwrap();
    assert_ne!(command["id"], command["approval_id"]);
    assert_eq!(command["approval_id"], "a");
    assert_eq!(command["allow"], false);
    assert_eq!(command["remember"], false);
    assert!(matches!(
        ledger.response(&response(&command, true)).unwrap(),
        Some(Event::Acknowledged { accepted: true, .. })
    ));
    assert!(!ledger.settled());
    ledger.close(&closed("a")).unwrap();
    assert!(ledger.settled());
}
#[test]
fn closure_can_precede_ack_and_does_not_erase_command_correlation() {
    for accepted in [true, false] {
        let mut ledger = Ledger::default();
        ledger.request(&request("a")).unwrap();
        let command = ledger.decide(decision("a", true)).unwrap().unwrap();
        ledger.close(&closed("a")).unwrap();
        assert!(!ledger.settled());
        ledger.response(&response(&command, accepted)).unwrap();
        assert!(ledger.settled());
        assert!(ledger.response(&response(&command, accepted)).is_err());
    }
}
#[test]
fn closed_before_unsent_decision_drops_intent_without_answering_next_request() {
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    ledger.request(&request("b")).unwrap();
    ledger.close(&closed("a")).unwrap();
    assert!(ledger.decide(decision("a", true)).unwrap().is_none());
    assert_eq!(
        ledger.decide(decision("b", false)).unwrap().unwrap()["approval_id"],
        "b"
    );
    assert!(ledger.decide(decision("b", true)).is_err());
    assert!(ledger.decide(decision("unknown", true)).is_err());
}
#[test]
fn duplicate_unknown_closures_and_reused_ids_are_rejected() {
    let mut ledger = Ledger::default();
    assert!(ledger.close(&closed("a")).is_err());
    ledger.request(&request("a")).unwrap();
    assert!(ledger.request(&request("a")).is_err());
    ledger.close(&closed("a")).unwrap();
    assert!(ledger.close(&closed("a")).is_err());
    assert!(ledger.request(&request("a")).is_err());
}
#[test]
fn malformed_requests_never_create_authority_or_pending_state() {
    for (key, value) in [
        ("id", json!("")),
        ("id", json!("a\n")),
        ("tool", json!(false)),
        ("input", json!(null)),
        ("profile", json!(3)),
        ("why", json!("x".repeat(4097))),
        ("timeout_s", json!(0)),
        ("timeout_s", json!(-1)),
        ("timeout_s", json!(true)),
        ("input", json!({"path":"x".repeat(16384)})),
        ("why", json!("\u{061c}".repeat(3000))),
    ] {
        let mut event = request("a");
        event[key] = value;
        let mut ledger = Ledger::default();
        assert!(ledger.request(&event).is_err(), "{key}");
        assert!(ledger.settled());
    }
    let mut event = request("a");
    event.as_object_mut().unwrap().remove("timeout_s");
    assert!(Ledger::default().request(&event).is_err());
}
#[test]
fn unbounded_source_wait_is_explicit_not_a_default_or_local_timer() {
    let mut event = request("a");
    event["timeout_s"] = Value::Null;
    let Event::Requested(request) = Ledger::default().request(&event).unwrap() else {
        panic!()
    };
    assert_eq!(request.timeout_s, None);
}
#[test]
fn pending_and_lifetime_bounds_do_not_forget_closed_ids() {
    let mut ledger = Ledger::default();
    for index in 0..16 {
        ledger.request(&request(&index.to_string())).unwrap();
    }
    assert!(ledger.request(&request("overflow")).is_err());
    for index in 0..16 {
        ledger.close(&closed(&index.to_string())).unwrap();
    }
    for index in 16..100 {
        ledger.request(&request(&index.to_string())).unwrap();
        ledger.close(&closed(&index.to_string())).unwrap();
    }
    assert!(ledger.request(&request("over-lifetime")).is_err());
}
#[test]
fn malformed_response_does_not_count_as_decision_acceptance() {
    for (key, value) in [
        ("approved", json!("wrong")),
        ("allow", json!("true")),
        ("id_key", json!("id")),
    ] {
        let mut ledger = Ledger::default();
        ledger.request(&request("a")).unwrap();
        let command = ledger.decide(decision("a", true)).unwrap().unwrap();
        let mut event = response(&command, true);
        event["result"][key] = value;
        assert!(ledger.response(&event).is_err());
        assert!(!ledger.settled());
    }
}
#[test]
fn authority_outcome_and_wait_reason_are_preserved_separately() {
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    let mut event = closed("a");
    event["outcome"] = json!("approved");
    event["reason"] = json!("cancelled");
    assert_eq!(
        ledger.close(&event).unwrap(),
        Event::Closed {
            id: "a".into(),
            outcome: Some("approved".into()),
            reason: "cancelled".into()
        }
    );
}
#[test]
fn view_is_denial_first_one_decision_only_and_closes_only_on_source_event() {
    let mut ledger = Ledger::default();
    let mut view = View::default();
    view.observe(ledger.request(&request("a")).unwrap())
        .unwrap();
    view.observe(ledger.request(&request("b")).unwrap())
        .unwrap();
    assert!(!view.current().unwrap().allow);
    view.choose(false);
    view.choose(true);
    assert_eq!(view.take_decision(), Some(decision("a", false)));
    assert!(view.current().unwrap().waiting);
    view.observe(Event::Acknowledged {
        id: "a".into(),
        accepted: true,
    })
    .unwrap();
    assert_eq!(view.len(), 2);
    view.observe(ledger.close(&closed("a")).unwrap()).unwrap();
    assert_eq!(view.current().unwrap().request.id, "b");
    assert!(!view.current().unwrap().allow);
    view.choose(true);
    view.observe(ledger.close(&closed("b")).unwrap()).unwrap();
    assert!(view.is_empty() && view.take_decision().is_none());
}
fn started() -> Turn {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready", "protocol_version":1, "launch_status":"ready", "backend":"claude", "model":"fixture"}), "claude", "fixture").unwrap();
    let command = turn.submit("fixture").unwrap();
    turn.accept(
        &json!({"type":"response", "id":command["id"], "ok":true, "result":{"turn":"accepted"}}),
        "claude",
        "fixture",
    )
    .unwrap();
    turn.accept(
        &json!({"type":"turn_start", "model":"fixture"}),
        "claude",
        "fixture",
    )
    .unwrap();
    turn
}
#[test]
fn actual_turn_cannot_complete_with_open_approval_even_after_command_ack() {
    let mut turn = started();
    turn.accept(&request("a"), "claude", "fixture").unwrap();
    let command = turn.approve(decision("a", false)).unwrap().unwrap();
    turn.accept(&response(&command, true), "claude", "fixture")
        .unwrap();
    turn.accept(
        &json!({"type":"text_delta", "text":"answer"}),
        "claude",
        "fixture",
    )
    .unwrap();
    assert!(
        turn.accept(
            &json!({"type":"turn_end", "stopReason":"stop"}),
            "claude",
            "fixture"
        )
        .is_err()
    );
    turn.accept(&closed("a"), "claude", "fixture").unwrap();
    assert_eq!(
        turn.accept(
            &json!({"type":"turn_end", "stopReason":"stop"}),
            "claude",
            "fixture"
        )
        .unwrap(),
        Observation::Complete
    );
}

#[test]
fn live_approval_renders_source_fields_scrolls_full_request_and_never_mock_grants() {
    use litetui_terminal::{demo::Demo, ui};
    use ratatui::{Terminal, backend::TestBackend};
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("fixture");
    app.submit();
    let mut event = request("render-approval");
    event["input"] =
        json!({"command":"not executed", "tail":"LAST-ARGUMENT", "body": "data\n".repeat(400)});
    event["why"] = json!("Workspace write requires confirmation. \u{1b}[2J\u{202e}");
    app.observe_live(Observation::Approval(
        Ledger::default().request(&event).unwrap(),
    ))
    .unwrap();
    let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let text = |terminal: &Terminal<TestBackend>| {
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>()
    };
    let first = text(&terminal);
    for expected in [
        "Tool approval",
        "render-approval",
        "Profile: strict",
        "[> Deny <]",
        "Allow once",
        "300s",
        "turn deadline",
        "\\u{1b}",
    ] {
        assert!(first.contains(expected), "missing {expected}: {first}");
    }
    assert!(!first.contains('\u{1b}') && !first.contains('\u{202e}'));
    assert!(!first.contains("Deny mock"));
    assert!(!first.contains("LAST-ARGUMENT"));
    // Optional capture is actual Ratatui cells, not a separately recreated mock.
    if let Ok(path) = std::env::var("LITETUI_APPROVAL_RENDER") {
        let color = |c: ratatui::style::Color| match c {
            ratatui::style::Color::Rgb(r, g, b) => json!([r, g, b]),
            _ => panic!("unresolved terminal color {c:?}"),
        };
        let cells: Vec<_> = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| json!({"s":c.symbol(), "fg":color(c.fg), "bg":color(c.bg)}))
            .collect();
        std::fs::write(
            path,
            serde_json::to_vec(&json!({"width":100,"height":32,"cells":cells})).unwrap(),
        )
        .unwrap();
    }
    app.live
        .as_mut()
        .unwrap()
        .approvals
        .current_mut()
        .unwrap()
        .scroll = usize::MAX;
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert!(text(&terminal).contains("LAST-ARGUMENT"));
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .approvals
            .current()
            .unwrap()
            .scroll
            > 0
    );
    assert!(
        !app.live
            .as_ref()
            .unwrap()
            .approvals
            .current()
            .unwrap()
            .allow
    );
    app.finish_live(Some("disconnected; no answer replay".into()));
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert!(!text(&terminal).contains("Tool approval"));
}

#[test]
fn delayed_decision_response_finishes_the_single_already_received_terminal_event() {
    for accepted in [true, false] {
        let mut turn = started();
        turn.accept(&request("a"), "claude", "fixture").unwrap();
        let command = turn.approve(decision("a", false)).unwrap().unwrap();
        turn.accept(&closed("a"), "claude", "fixture").unwrap();
        turn.accept(
            &json!({"type":"text_delta", "text":"done"}),
            "claude",
            "fixture",
        )
        .unwrap();
        assert!(matches!(
            turn.accept(
                &json!({"type":"turn_end", "stopReason":"stop"}),
                "claude",
                "fixture"
            )
            .unwrap(),
            Observation::Information(_)
        ));
        assert!(turn.submit("not settled yet").is_err());
        assert!(
            turn.accept(
                &json!({"type":"text_delta", "text":"too late"}),
                "claude",
                "fixture"
            )
            .is_err()
        );
        assert_eq!(
            turn.accept(&response(&command, accepted), "claude", "fixture")
                .unwrap(),
            Observation::Complete
        );
        turn.submit("now settled").unwrap();
    }
}

#[test]
fn compact_nested_json_is_rejected_during_bounded_pretty_serialization() {
    let mut nested = json!(vec![0; 2000]);
    for _ in 0..60 {
        nested = json!([nested]);
    }
    let mut event = request("a");
    event["input"] = json!({"x":nested});
    assert!(serde_json::to_vec(&event).unwrap().len() < 16384);
    assert!(
        Ledger::default()
            .request(&event)
            .unwrap_err()
            .to_string()
            .contains("JSON display limit")
    );
}
#[test]
fn request_cannot_collide_with_its_independent_generated_command_id() {
    let id = "rust-approval-decision-0";
    let mut ledger = Ledger::default();
    ledger.request(&request(id)).unwrap();
    let command = ledger.decide(decision(id, true)).unwrap().unwrap();
    assert_ne!(command["id"], command["approval_id"]);
}
