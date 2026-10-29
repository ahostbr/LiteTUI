use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use litetui_terminal::{
    management::{Management, Step},
    restored_queue::{Intent, Snapshot, Update, View},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

fn data() -> Value {
    json!({"session_id":"session","backend":"codex","model":"model","policy":"hold","held":2,"snapshot":"a".repeat(64),
    "entries":[
      {"id":"one","state":"queued","revision":2,"content":"SAVED CONTENT\nliteral \\n and \u{1b}[31m red",
       "source":"queued","tool_profile":"strict","thread_id":"thread","turn_id":"turn","can_cancel":true,"can_continue":true},
      {"id":"two","state":"uncertain","revision":4,"content":"POSSIBLY SENT",
       "source":"queued","tool_profile":"strict","thread_id":"thread","turn_id":"turn","can_cancel":false}
    ]})
}
fn connected() -> Management {
    let mut m = Management::default();
    let hello = m
        .begin(
            &json!({"management_protocols":[1],"capabilities":["hello","state"],
      "conversation_id":"session","backend":"codex","model":"model","tool_profile":"strict"}),
        )
        .unwrap();
    let Step::Write(state) = reply(
        &mut m,
        &hello,
        json!({"protocol_version":1,"data_version":1,
      "capabilities":["hello","state"],"operations":["gui.hello","gui.state","gui.restored_input.list","gui.restored_input.cancel",
        "gui.models.list","gui.models.select","gui.restored_input.continue","gui.restored_input.settled"]}),
    ) else {
        panic!()
    };
    reply(
        &mut m,
        &state,
        json!({"session_id":"session","busy":false,"mode":"normal","tool_profile":"strict",
      "model":{"backend":"codex","model":"model"},"ownership":{"data_version":1,"writable":true},
      "restored_input":{"policy":"hold","held":2},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],
      "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]}),
    );
    m
}
fn reply(m: &mut Management, command: &Value, result: Value) -> Step {
    m.accept(json!({"type":"response","id":command["id"],"ok":true,"result":result}))
        .unwrap()
}
fn open(m: &mut Management) -> Snapshot {
    let command = m.request_queue(Intent::Open).unwrap();
    assert_eq!(command["session_id"], "session");
    let Step::Queue(Update::Snapshot(snapshot)) = reply(m, &command, data()) else {
        panic!()
    };
    snapshot
}
fn cancel(m: &mut Management) -> Value {
    let snapshot = open(m);
    m.request_queue(Intent::Cancel {
        revision: snapshot.revision,
        entry: "one".into(),
    })
    .unwrap()
}
#[test]
fn cancellation_requires_correlated_fresh_readback_before_next_prompt() {
    let mut m = connected();
    let command = cancel(&mut m);
    assert_eq!(command["type"], "gui.restored_input.cancel");
    assert_eq!(command["snapshot"], "a".repeat(64));
    assert!(!m.ready());
    assert!(m.prompt("early").is_err());
    assert!(m.request_queue(Intent::Open).is_err());
    let Step::Write(read) = reply(&mut m, &command, json!({"cancelled":"one"})) else {
        panic!()
    };
    assert_eq!(read["type"], "gui.restored_input.list");
    assert_ne!(read["id"], command["id"]);
    assert!(!m.ready());
    let mut updated = data();
    updated["entries"].as_array_mut().unwrap().remove(0);
    updated["held"] = json!(1);
    let Step::Queue(Update::Snapshot(snapshot)) = reply(&mut m, &read, updated) else {
        panic!()
    };
    assert_eq!(snapshot.entries.len(), 1);
    assert!(snapshot.message.unwrap().contains("Cancellation saved"));
    assert!(m.ready());
    m.prompt("next-explicit").unwrap();
}
#[test]
fn negative_ack_reads_actual_saved_queue_without_resending() {
    let mut m = connected();
    let command = cancel(&mut m);
    let Step::Write(read) = m
        .accept(json!({"type":"response","id":command["id"],"ok":false,"error":"changed"}))
        .unwrap()
    else {
        panic!()
    };
    let Step::Queue(Update::Snapshot(snapshot)) = reply(&mut m, &read, data()) else {
        panic!()
    };
    assert_eq!(snapshot.entries.len(), 2);
    assert!(snapshot.message.unwrap().contains("Request failed"));
    assert!(m.ready());
}
#[test]
fn failed_or_inconsistent_readback_makes_connection_uncertain() {
    for fault in ["missing", "still-held", "foreign", "model", "policy"] {
        let mut m = connected();
        let command = cancel(&mut m);
        let Step::Write(read) = reply(&mut m, &command, json!({})) else {
            panic!()
        };
        let mut value = data();
        match fault {
            "foreign" => value["session_id"] = json!("other"),
            "model" => value["model"] = json!("other"),
            "policy" => value["policy"] = json!("automatic"),
            _ => {}
        }
        let frame = if fault == "missing" {
            json!({"type":"response","id":read["id"],"ok":false,"error":"disk failed"})
        } else {
            json!({"type":"response","id":read["id"],"ok":true,"result":value})
        };
        assert!(m.accept(frame).is_err(), "{fault}");
        assert!(!m.ready());
        assert!(m.request_queue(Intent::Open).is_err());
    }
}
#[test]
fn stale_unoffered_and_turn_invalidated_selections_are_rejected() {
    let mut m = connected();
    let snapshot = open(&mut m);
    for (revision, entry) in [
        (snapshot.revision + 1, "one"),
        (snapshot.revision, "two"),
        (snapshot.revision, "invented"),
    ] {
        assert!(
            m.request_queue(Intent::Cancel {
                revision,
                entry: entry.into()
            })
            .is_err()
        );
    }
    m.prompt("new-turn").unwrap();
    assert!(
        m.request_queue(Intent::Cancel {
            revision: snapshot.revision,
            entry: "one".into()
        })
        .is_err()
    );
}
#[test]
fn model_control_invalidates_queue_selection() {
    let mut m = connected();
    let snapshot = open(&mut m);
    let cmd = m
        .request_control(litetui_terminal::model_controls::Intent::Open(
            litetui_terminal::model_controls::Kind::Model,
        ))
        .unwrap();
    reply(
        &mut m,
        &cmd,
        json!({"backend":"codex","model":"model","models":[{"slug":"model","name":"Model","current":true,"loaded":null}]}),
    );
    assert!(
        m.request_queue(Intent::Cancel {
            revision: snapshot.revision,
            entry: "one".into()
        })
        .is_err()
    );
}
#[test]
fn parser_rejects_ambiguous_and_oversized_evidence() {
    for fault in [
        "duplicate",
        "count",
        "state",
        "revision",
        "policy",
        "token",
        "budget",
    ] {
        let mut value = data();
        match fault {
            "duplicate" => value["entries"][1]["id"] = json!("one"),
            "count" => value["held"] = json!(1),
            "state" => value["entries"][0]["state"] = json!("invented"),
            "revision" => value["entries"][0]["revision"] = json!(true),
            "policy" => value["entries"][1]["can_cancel"] = json!(true),
            "token" => value["snapshot"] = json!("A".repeat(64)),
            "budget" => value["entries"][0]["content"] = json!("x".repeat(65537)),
            _ => unreachable!(),
        }
        assert!(Snapshot::parse(&value, 1).is_err(), "{fault}");
    }
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn view() -> View {
    let mut view = View::default();
    view.open();
    assert_eq!(view.take_intent(), Some(Intent::Open));
    view.observe(Update::Snapshot(Snapshot::parse(&data(), 7).unwrap()));
    view
}
fn draw(view: &mut View, w: u16, h: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
#[test]
fn confirmation_requires_arm_visible_prompt_and_fresh_enter() {
    let mut view = view();
    view.key(key(KeyCode::Delete));
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
    draw(&mut view, 100, 30);
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
    view.key(key(KeyCode::Delete));
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
    assert!(draw(&mut view, 100, 30).contains("Cancel this saved entry?"));
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    view.key(repeat);
    assert!(view.take_intent().is_none());
    view.key(key(KeyCode::Enter));
    assert_eq!(
        view.take_intent(),
        Some(Intent::Cancel {
            revision: 7,
            entry: "one".into()
        })
    );
    view.close();
    assert!(view.owns_input()); // A sent request still owns its readback.
    view.observe(Update::Rejected("refused".into()));
    assert!(!view.owns_input());
}
#[test]
fn resize_pointer_focus_refresh_and_escape_disarm_cancellation() {
    for action in [
        "resize", "hidden", "mouse", "focus", "scroll", "refresh", "escape",
    ] {
        let mut view = view();
        draw(&mut view, 100, 30);
        view.key(key(KeyCode::Delete));
        draw(&mut view, 100, 30);
        match action {
            "resize" => {
                draw(&mut view, 80, 24);
            }
            "hidden" => {
                draw(&mut view, 35, 13);
                draw(&mut view, 100, 30);
            }
            "mouse" => view.mouse(MouseEvent {
                kind: MouseEventKind::Moved,
                column: 0,
                row: 0,
                modifiers: KeyModifiers::NONE,
            }),
            "focus" => view.key(key(KeyCode::Down)),
            "scroll" => view.key(key(KeyCode::PageDown)),
            "refresh" => view.observe(Update::Snapshot(Snapshot::parse(&data(), 8).unwrap())),
            "escape" => view.key(key(KeyCode::Esc)),
            _ => unreachable!(),
        }
        draw(&mut view, 100, 30);
        view.key(key(KeyCode::Enter));
        assert!(view.take_intent().is_none(), "{action}");
    }
}
#[test]
fn unsafe_entry_can_be_inspected_but_never_armed() {
    let mut view = view();
    draw(&mut view, 100, 30);
    view.key(key(KeyCode::Down));
    view.key(key(KeyCode::Delete));
    let text = draw(&mut view, 100, 30);
    assert!(text.contains("POSSIBLY SENT"));
    assert!(text.contains("Cancellation unavailable"));
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
}
#[test]
fn full_payload_is_scrollable_and_terminal_escapes_are_literal() {
    let mut v = view();
    let mut value = data();
    value["entries"][0]["content"] = json!(format!(
        "{}END-OF-PAYLOAD",
        "line \u{1b}[31m literal \\n 漢字\n".repeat(60)
    ));
    v.observe(Update::Snapshot(Snapshot::parse(&value, 1).unwrap()));
    let first = draw(&mut v, 80, 24);
    assert!(!first.contains('\u{1b}'));
    assert!(first.contains("literal \\n"));
    for _ in 0..100 {
        v.key(key(KeyCode::PageDown));
    }
    assert!(draw(&mut v, 80, 24).contains("END-OF-PAYLOAD"));
    assert!(v.take_intent().is_none());
}

#[test]
fn failed_inspection_never_reports_empty_and_full_error_is_inspectable() {
    let mut v = view();
    v.observe(Update::Rejected(format!(
        "{}\nERROR-END",
        "long reason ".repeat(100)
    )));
    let first = draw(&mut v, 80, 24);
    assert!(first.contains("Held input unavailable."));
    assert!(!first.contains("No held inputs."));
    for _ in 0..100 {
        v.key(key(KeyCode::PageDown));
    }
    assert!(draw(&mut v, 80, 24).contains("ERROR-END"));
    v.key(key(KeyCode::Delete));
    draw(&mut v, 80, 24);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
}

#[test]
fn continuation_requires_source_offer_and_fresh_visible_confirmation() {
    let mut v = view();
    draw(&mut v, 100, 30);
    v.key(key(KeyCode::Char('c')));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    assert!(draw(&mut v, 100, 30).contains("Run this saved input"));
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    v.key(repeat);
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Enter));
    let intent = v.take_intent().unwrap();
    assert_eq!(
        intent,
        Intent::Continue {
            revision: 7,
            entry: "one".into()
        }
    );
    assert_eq!(
        v.continuation_text(&intent).unwrap(),
        data()["entries"][0]["content"].as_str().unwrap()
    );
    assert!(v.owns_input());
    for fault in ["legacy", "unsafe", "later", "backend", "invalid"] {
        let mut value = data();
        match fault {
            "legacy" => {
                value["entries"][0]
                    .as_object_mut()
                    .unwrap()
                    .remove("can_continue");
            }
            "unsafe" => {
                value["entries"][0]["state"] = json!("sending");
                value["entries"][0]["can_cancel"] = json!(false);
            }
            "later" => {
                value["entries"][1]["can_continue"] = json!(true);
            }
            "backend" => value["backend"] = json!("claude"),
            "invalid" => value["entries"][0]["can_continue"] = json!(1),
            _ => unreachable!(),
        }
        if fault == "legacy" {
            let mut v = view();
            v.observe(Update::Snapshot(Snapshot::parse(&value, 9).unwrap()));
            draw(&mut v, 100, 30);
            v.key(key(KeyCode::Char('c')));
            draw(&mut v, 100, 30);
            v.key(key(KeyCode::Enter));
            assert!(v.take_intent().is_none());
        } else {
            assert!(Snapshot::parse(&value, 9).is_err(), "{fault}");
        }
    }
}

#[test]
fn continue_and_cancel_confirmations_cannot_be_swapped_without_redrawing() {
    let mut v = view();
    draw(&mut v, 100, 30);
    v.key(key(KeyCode::Delete));
    draw(&mut v, 100, 30);
    v.key(key(KeyCode::Char('c')));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 100, 30);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Continue { .. })));
}

fn continuing(m: &mut Management) -> Value {
    let snapshot = open(m);
    assert!(
        m.prepare_continuation(snapshot.revision + 1, "one")
            .is_err()
    );
    assert!(m.prepare_continuation(snapshot.revision, "two").is_err());
    let prepared = m.prepare_continuation(snapshot.revision, "one").unwrap();
    assert_eq!(prepared.prompt, data()["entries"][0]["content"]);
    let command = m
        .begin_continuation(prepared, "continue-operation")
        .unwrap();
    assert_eq!(command["type"], "gui.restored_input.continue");
    assert_eq!(command["snapshot"], "a".repeat(64));
    assert!(command.get("message").is_none() && command.get("tool_profile").is_none());
    assert!(!m.ready() && m.needs_continuation_readback());
    assert!(m.prompt("early").is_err());
    command
}
fn settled_value(refused: bool) -> Value {
    let mut result = data();
    result["snapshot"] = json!("b".repeat(64));
    if !refused {
        result["entries"].as_array_mut().unwrap().remove(0);
        result["held"] = json!(1);
    }
    result["operation_id"] = json!("continue-operation");
    result["entry_id"] = json!("one");
    result["continuation_outcome"] = json!(if refused { "refused" } else { "accepted" });
    result
}
#[test]
fn continuation_ack_and_terminal_require_separate_correlated_queue_readback() {
    for refused in [false, true] {
        let mut m = connected();
        let command = continuing(&mut m);
        assert!(matches!(
            reply(
                &mut m,
                &command,
                json!({"turn":if refused {"refused"} else {"accepted"}})
            ),
            Step::Event(_)
        ));
        assert!(!m.ready()); // The execution reducer owns the admission receipt.
        let read = m.read_continuation(refused).unwrap();
        assert_eq!(read["type"], "gui.restored_input.settled");
        assert_eq!(read["operation_id"], command["id"]);
        assert_eq!(read["entry_id"], "one");
        assert_ne!(read["id"], command["id"]);
        assert!(m.prompt("still-early").is_err());
        let Step::Queue(Update::Snapshot(snapshot)) = reply(&mut m, &read, settled_value(refused))
        else {
            panic!()
        };
        assert_eq!(snapshot.entries.iter().any(|e| e.id == "one"), refused);
        assert!(snapshot.message.unwrap().contains("saved queue read back"));
        assert!(m.ready());
        m.prompt("explicit-next").unwrap();
    }
}
#[test]
fn inconsistent_continuation_readback_never_reenables_submission() {
    for fault in [
        "operation",
        "entry",
        "outcome",
        "membership",
        "failed",
        "session",
    ] {
        let mut m = connected();
        continuing(&mut m);
        let read = m.read_continuation(false).unwrap();
        let mut value = settled_value(false);
        match fault {
            "operation" => value["operation_id"] = json!("foreign"),
            "entry" => value["entry_id"] = json!("foreign"),
            "outcome" => value["continuation_outcome"] = json!("refused"),
            "membership" => {
                value = settled_value(true);
                value["continuation_outcome"] = json!("accepted");
            }
            "session" => value["session_id"] = json!("foreign"),
            _ => {}
        }
        let frame = json!({"type":"response","id":read["id"],"ok":fault!="failed","result":value,"error":"unavailable"});
        assert!(m.accept(frame).is_err(), "{fault}");
        assert!(!m.ready());
        assert!(m.prompt("unsafe").is_err());
    }
}
