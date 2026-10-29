use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::{
    demo::Demo,
    live::{Observation, TerminalOutcome, Turn},
    questions::{Action, Answer, Decision, Event, Ledger, View},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::collections::BTreeSet;

fn request(id: &str) -> Value {
    let mut value: Value =
        serde_json::from_str(include_str!("fixtures/question_request.json")).unwrap();
    value["id"] = json!(id);
    value
}
fn decision(id: &str, action: Action) -> Decision {
    Decision {
        id: id.into(),
        action,
        answers: vec![
            Answer {
                selected: BTreeSet::from([0, 1]),
                note: "both".into(),
            },
            Answer {
                selected: BTreeSet::new(),
                note: "fixture-secret".into(),
            },
        ],
    }
}
fn ack(command: &Value) -> Value {
    json!({"type":"response","id":command["id"],"ok":true,"result":{"answered":command["ask_id"],"id_key":"ask_id"}})
}
fn cancelled(id: &str) -> Value {
    json!({"type":"user_input_resolved","id":id,"cancelled":true,"reason":"abort"})
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn started() -> Turn {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"fixture"}),"claude","fixture").unwrap();
    let command = turn.submit("question").unwrap();
    turn.accept(
        &json!({"type":"response","id":command["id"],"ok":true,"result":{"turn":"accepted"}}),
        "claude",
        "fixture",
    )
    .unwrap();
    turn.accept(
        &json!({"type":"turn_start","model":"fixture"}),
        "claude",
        "fixture",
    )
    .unwrap();
    turn
}
#[test]
fn source_fixture_supports_multiselect_and_secret_free_text() {
    let mut ledger = Ledger::default();
    let Event::Requested(request) = ledger.request(&request("a")).unwrap() else {
        panic!()
    };
    assert!(request.questions[0].multi_select);
    assert!(request.questions[1].secret && request.questions[1].options.is_empty());
    let command = ledger
        .decide(decision("a", Action::Submit))
        .unwrap()
        .unwrap();
    assert_ne!(command["id"], command["ask_id"]);
    assert_eq!(command["answers"][0]["selected"], json!([0, 1]));
    assert_eq!(command["answers"][1]["note"], "fixture-secret");
    assert!(!ledger.settled() && ledger.accounted_for());
    assert!(matches!(
        ledger.response(&ack(&command)).unwrap(),
        Some(Event::Acknowledged { accepted: true, .. })
    ));
    assert!(ledger.settled());
    assert!(ledger.response(&ack(&command)).is_err());
    assert!(!format!("{:?}", decision("a", Action::Submit)).contains("fixture-secret"));
}
#[test]
fn cancel_never_transmits_partial_notes_and_chat_allows_incomplete_answers() {
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    let command = ledger
        .decide(decision("a", Action::Cancel))
        .unwrap()
        .unwrap();
    assert_eq!(command["action"], "cancel");
    assert_eq!(command["answers"], json!([]));
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    let mut partial = decision("a", Action::Submit);
    partial.answers[1].note.clear();
    assert!(ledger.decide(partial.clone()).is_err());
    partial.action = Action::Chat;
    let command = ledger.decide(partial).unwrap().unwrap();
    assert_eq!(command["action"], "chat");
}
#[test]
fn malformed_requests_and_unbounded_display_are_rejected() {
    for (field, value) in [
        ("multiSelect", json!(1)),
        ("isSecret", json!("yes")),
        ("selected", json!([90])),
        ("answered", json!(true)),
        ("note", json!(true)),
        ("question", json!("\n".repeat(1200))),
        ("question", json!("x".repeat(16385))),
    ] {
        let mut raw = request("a");
        raw["questions"][0][field] = value;
        assert!(Ledger::default().request(&raw).is_err(), "{field}");
    }
    for id in ["", "x y", "x\u{202e}"] {
        assert!(Ledger::default().request(&request(id)).is_err());
    }
    for questions in [
        json!([]),
        json!(vec![request("x")["questions"][0].clone(); 9]),
    ] {
        let mut raw = request("a");
        raw["questions"] = questions;
        assert!(Ledger::default().request(&raw).is_err());
    }
}
#[test]
fn invalid_answers_and_reused_ids_never_gain_authority() {
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    assert!(ledger.request(&request("a")).is_err());
    for kind in 0..3 {
        let mut answer = decision("a", Action::Submit);
        match kind {
            0 => {
                answer.answers[0].selected.insert(99);
            }
            1 => answer.answers[1].note = "x".repeat(1025),
            _ => {
                answer.answers.pop();
            }
        }
        assert!(ledger.decide(answer).is_err());
    }
    let command = ledger
        .decide(decision("a", Action::Submit))
        .unwrap()
        .unwrap();
    assert!(ledger.decide(decision("a", Action::Cancel)).is_err());
    let mut response = ack(&command);
    response["result"]["answered"] = json!("wrong");
    assert!(ledger.response(&response).is_err());
    assert!(!ledger.settled());
    ledger.response(&ack(&command)).unwrap();
    assert!(ledger.request(&request("a")).is_err());
}
#[test]
fn source_cancellation_drops_unsent_intent_and_does_not_retarget_next_question() {
    let mut ledger = Ledger::default();
    let mut view = View::default();
    view.observe(ledger.request(&request("a")).unwrap())
        .unwrap();
    view.observe(ledger.request(&request("b")).unwrap())
        .unwrap();
    view.key(key(KeyCode::Esc));
    assert!(view.current().unwrap().waiting);
    view.observe(ledger.resolved(&cancelled("a")).unwrap())
        .unwrap();
    assert!(view.take_decision().is_none());
    assert_eq!(view.current().unwrap().request.id, "b");
    assert!(!view.current().unwrap().waiting);
    assert!(
        ledger
            .decide(decision("a", Action::Submit))
            .unwrap()
            .is_none()
    );
    assert!(ledger.resolved(&cancelled("a")).is_err());
}
#[test]
fn cancellation_before_late_ack_remains_correlated() {
    for accepted in [true, false] {
        let mut ledger = Ledger::default();
        ledger.request(&request("a")).unwrap();
        let command = ledger
            .decide(decision("a", Action::Submit))
            .unwrap()
            .unwrap();
        ledger.resolved(&cancelled("a")).unwrap();
        assert!(!ledger.settled());
        let response = if accepted {
            ack(&command)
        } else {
            json!({"type":"response","id":command["id"],"ok":false,"error":"already closed"})
        };
        ledger.response(&response).unwrap();
        assert!(ledger.settled());
    }
    let mut ledger = Ledger::default();
    ledger.request(&request("a")).unwrap();
    let command = ledger
        .decide(decision("a", Action::Submit))
        .unwrap()
        .unwrap();
    assert!(
        ledger
            .response(&json!({"type":"response","id":command["id"],"ok":false,"error":"refused"}))
            .is_err()
    );
}
#[test]
fn terminal_before_answer_ack_closes_form_before_settling_turn() {
    for approval_stop in [false, true] {
        let mut turn = started();
        let mut app = Demo::live("claude".into(), "fixture".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("prompt");
        app.submit();
        app.live.as_mut().unwrap().take_submission();
        app.observe_live(turn.accept(&request("a"), "claude", "fixture").unwrap())
            .unwrap();
        let command = turn.answer(decision("a", Action::Submit)).unwrap().unwrap();
        let text = json!({"type":"text_delta","text":"answer"});
        app.observe_live(turn.accept(&text, "claude", "fixture").unwrap())
            .unwrap();
        let terminal = if approval_stop {
            json!({"type":"turn_end","stopReason":"approval","error":"stopped"})
        } else {
            json!({"type":"turn_end","stopReason":"stop"})
        };
        app.observe_live(turn.accept(&terminal, "claude", "fixture").unwrap())
            .unwrap();
        assert!(turn.submit("early").is_err());
        assert!(app.has_live_question());
        let observation = turn.accept(&ack(&command), "claude", "fixture").unwrap();
        assert!(
            matches!(&observation,Observation::Question{settled:Some(outcome),..}
            if *outcome == if approval_stop {TerminalOutcome::ApprovalStopped("stopped".into())} else {TerminalOutcome::Complete})
        );
        app.observe_live(observation).unwrap();
        assert!(!app.has_live_question() && !app.live.as_ref().unwrap().busy);
        turn.submit("follow-up").unwrap();
    }
}
#[test]
fn unanswered_question_blocks_terminal_and_deadline_is_not_answered_by_default() {
    let mut turn = started();
    turn.accept(&request("a"), "claude", "fixture").unwrap();
    turn.accept(
        &json!({"type":"text_delta","text":"partial"}),
        "claude",
        "fixture",
    )
    .unwrap();
    assert!(
        turn.accept(
            &json!({"type":"turn_end","stopReason":"stop"}),
            "claude",
            "fixture"
        )
        .is_err()
    );
}
#[test]
fn form_navigation_toggles_clear_and_partial_chat_are_explicit() {
    let mut ledger = Ledger::default();
    let mut view = View::default();
    view.observe(ledger.request(&request("a")).unwrap())
        .unwrap();
    assert!(view.take_decision().is_none());
    view.key(key(KeyCode::Tab));
    view.key(key(KeyCode::Enter));
    view.key(key(KeyCode::Tab));
    view.key(key(KeyCode::Char(' ')));
    assert_eq!(view.current().unwrap().answers[0], BTreeSet::from([0, 1]));
    view.current_mut().unwrap().focus = 4;
    view.activate(); // Clear.
    assert!(view.current().unwrap().answers[0].is_empty());
    view.current_mut().unwrap().focus = 6;
    view.activate(); // Chat.
    assert_eq!(view.take_decision().unwrap().action, Action::Chat);
    view.key(key(KeyCode::Esc));
    assert!(view.take_decision().is_none());
}
#[test]
fn single_choice_replaces_selection_and_submit_requires_every_question() {
    let mut raw = request("a");
    raw["questions"][0]["multiSelect"] = json!(false);
    let mut ledger = Ledger::default();
    let mut view = View::default();
    view.observe(ledger.request(&raw).unwrap()).unwrap();
    view.current_mut().unwrap().focus = 1;
    view.activate();
    view.current_mut().unwrap().focus = 2;
    view.activate();
    assert_eq!(view.current().unwrap().answers[0], BTreeSet::from([1]));
    view.current_mut().unwrap().jump(1);
    view.current_mut().unwrap().focus = 3;
    view.activate(); // Submit.
    assert!(view.take_decision().is_none());
    assert!(view.current().unwrap().notice.contains("Choose an option"));
    view.current_mut().unwrap().focus = 1;
    for ch in "fixture-secret".chars() {
        view.key(key(KeyCode::Char(ch)));
    }
    view.current_mut().unwrap().focus = 3;
    view.activate();
    let answer = view.take_decision().unwrap();
    assert_eq!(answer.answers[1].note, "fixture-secret");
    assert_eq!(answer.action, Action::Submit);
}
#[test]
fn rendering_masks_secrets_and_mouse_cannot_submit_on_press_or_stale_release() {
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("prompt");
    app.submit();
    let mut ledger = Ledger::default();
    app.observe_live(Observation::Question {
        event: ledger.request(&request("a")).unwrap(),
        settled: None,
    })
    .unwrap();
    {
        let p = app.live.as_mut().unwrap().questions.current_mut().unwrap();
        p.jump(1);
        p.focus = 1;
        p.paste("fixture-secret", false);
    }
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
    let cells = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    assert!(cells.contains("Secret note (hidden)") && cells.contains("********"));
    assert!(!cells.contains("fixture-secret"));
    let chat = app.live.as_mut().unwrap();
    let hit = chat
        .questions
        .current()
        .unwrap()
        .hits
        .iter()
        .find(|h| h.target == litetui_terminal::questions::HitTarget::Control(4))
        .unwrap()
        .rect;
    chat.questions.mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: hit.x,
        row: hit.y,
        modifiers: KeyModifiers::NONE,
    });
    assert!(chat.questions.take_decision().is_none());
    chat.questions.mouse(MouseEvent {
        kind: MouseEventKind::Up(MouseButton::Left),
        column: hit.x,
        row: hit.y,
        modifiers: KeyModifiers::NONE,
    });
    assert_eq!(chat.questions.take_decision().unwrap().action, Action::Chat);
}
#[test]
fn repeat_and_release_cannot_answer_or_mutate_a_question() {
    let mut ledger = Ledger::default();
    let mut view = View::default();
    view.observe(ledger.request(&request("a")).unwrap())
        .unwrap();
    for kind in [KeyEventKind::Repeat, KeyEventKind::Release] {
        let mut event = key(KeyCode::Esc);
        event.kind = kind;
        view.key(event);
        assert!(view.take_decision().is_none());
    }
}

#[test]
fn changed_geometry_question_or_keyboard_cancels_a_mouse_press() {
    use litetui_terminal::questions::{Hit, HitTarget};
    use ratatui::layout::Rect;
    for change in 0..4 {
        let mut ledger = Ledger::default();
        let mut view = View::default();
        view.observe(ledger.request(&request("a")).unwrap())
            .unwrap();
        let hit = Hit {
            rect: Rect::new(2, 3, 10, 1),
            target: HitTarget::Control(6),
        };
        view.current_mut().unwrap().set_hits(vec![hit.clone()]);
        view.mouse(MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: 2,
            row: 3,
            modifiers: KeyModifiers::NONE,
        });
        match change {
            0 => {
                view.current_mut().unwrap().set_hits(vec![Hit {
                    rect: Rect::new(1, 3, 11, 1),
                    ..hit.clone()
                }]);
            }
            1 => {
                view.current_mut().unwrap().jump(1);
                view.current_mut().unwrap().set_hits(vec![hit]);
            }
            2 => {
                view.key(key(KeyCode::Tab));
            }
            _ => {
                view.current_mut().unwrap().invalidate_hits();
                view.current_mut().unwrap().set_hits(vec![hit]);
            }
        }
        view.mouse(MouseEvent {
            kind: MouseEventKind::Up(MouseButton::Left),
            column: 2,
            row: 3,
            modifiers: KeyModifiers::NONE,
        });
        assert!(view.take_decision().is_none());
    }
}
#[test]
fn compact_form_keeps_last_question_tab_visible_and_clickable() {
    let mut raw = request("a");
    let sample = raw["questions"][0].clone();
    raw["questions"] = json!(vec![sample; 8]);
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("prompt");
    app.submit();
    let event = Ledger::default().request(&raw).unwrap();
    app.observe_live(Observation::Question {
        event,
        settled: None,
    })
    .unwrap();
    app.live
        .as_mut()
        .unwrap()
        .questions
        .current_mut()
        .unwrap()
        .jump(7);
    let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
    terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .questions
            .current()
            .unwrap()
            .hits
            .iter()
            .any(|hit| hit.target == litetui_terminal::questions::HitTarget::Question(7))
    );
}

#[test]
fn long_note_keeps_the_editing_caret_visible_at_compact_width() {
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("prompt");
    app.submit();
    let event = Ledger::default().request(&request("a")).unwrap();
    app.observe_live(Observation::Question {
        event,
        settled: None,
    })
    .unwrap();
    let pending = app.live.as_mut().unwrap().questions.current_mut().unwrap();
    pending.focus = 3;
    pending.paste(&format!("{}NOTE-END", "body ".repeat(100)), false);
    let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
    terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
    let cells = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    assert!(cells.contains("NOTE-END"));
}
