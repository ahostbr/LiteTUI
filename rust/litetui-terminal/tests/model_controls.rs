use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::{
    management::{Management, Step},
    model_controls::{Choices, Intent, Kind, Update, View},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

fn ready() -> Value {
    json!({"management_protocols":[1],"capabilities":["hello","state"],
    "conversation_id":"session","backend":"claude","model":"model-A","tool_profile":"strict"})
}
fn connected() -> Management {
    let mut m = Management::default();
    let hello = m.begin(&ready()).unwrap();
    let Step::Write(read)=m.accept(json!({"type":"response","id":hello["id"],"ok":true,"result":{
        "protocol_version":1,"data_version":1,"capabilities":["hello","state"],
        "operations":["gui.hello","gui.state","gui.models.list","gui.models.select","gui.models.capabilities","gui.thinking.set"]
    }})).unwrap() else{panic!()};
    m.accept(json!({"type":"response","id":read["id"],"ok":true,"result":{
        "session_id":"session","busy":false,"mode":"normal","tool_profile":"strict",
        "model":{"model":"model-A","backend":"claude"},"ownership":{"data_version":1,"writable":false},
        "schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],
        "pending_approvals":[],"pending_questions":[],"pending_model_loads":[]
    }})).unwrap();
    m
}
fn models(current: &str) -> Value {
    json!({"backend":"claude","model":current,"models":[
        {"slug":"model-A","name":"Model A","current":current=="model-A","loaded":null},
        {"slug":"model-B","name":"Model B","current":current=="model-B","loaded":false}
    ]})
}
fn thinking(level: Value) -> Value {
    json!({"backend":"claude","model":"model-A","thinking":{
        "level":level,"levels":["default","low","high"],"source":"backend metadata","note":"Model-dependent effect."
    }})
}
fn reply(m: &mut Management, cmd: &Value, result: Value) -> Step {
    m.accept(json!({"type":"response","id":cmd["id"],"ok":true,"result":result}))
        .unwrap()
}
fn open(m: &mut Management, kind: Kind) -> Choices {
    let cmd = m.request_control(Intent::Open(kind)).unwrap();
    let result = if kind == Kind::Model {
        models("model-A")
    } else {
        thinking(Value::Null)
    };
    let Step::Control(Update::Choices(menu)) = reply(m, &cmd, result) else {
        panic!()
    };
    menu
}
fn model_event(m: &mut Management, id: &Value, target: &str) {
    let event = json!({"type":"model_state","backend":"claude","model":target});
    assert!(matches!(m.accept(event.clone()).unwrap(), Step::Ignore));
    assert!(matches!(
        m.accept(
            json!({"type":"gui.event","event_id":"12345678-1234-4123-8123-123456789abc",
        "session_id":"session","operation_id":id,"event":event})
        )
        .unwrap(),
        Step::Event(_)
    ));
}
#[test]
fn model_event_and_ack_do_not_release_control_until_readback() {
    let mut m = connected();
    let menu = open(&mut m, Kind::Model);
    let cmd = m
        .request_control(Intent::Select {
            revision: menu.revision,
            value: "model-B".into(),
            context: None,
        })
        .unwrap();
    assert_eq!(cmd["type"], "gui.models.select");
    model_event(&mut m, &cmd["id"], "model-B");
    assert_eq!(m.model(), Some("model-A"));
    assert!(!m.ready());
    assert!(m.prompt("early").is_err());
    let Step::Write(read) = reply(&mut m, &cmd, models("model-B")) else {
        panic!()
    };
    assert_ne!(read["id"], cmd["id"]);
    assert!(!m.ready());
    let Step::Control(Update::Choices(updated)) = reply(&mut m, &read, models("model-B")) else {
        panic!()
    };
    assert_eq!(updated.current, "model-B");
    assert_eq!(m.model(), Some("model-B"));
    assert!(m.ready());
    m.prompt("next").unwrap();
}
#[test]
fn negative_apply_reads_actual_state_without_resending_or_rolling_back() {
    for actual in ["model-A", "model-B"] {
        let mut m = connected();
        let menu = open(&mut m, Kind::Model);
        let cmd = m
            .request_control(Intent::Select {
                revision: menu.revision,
                value: "model-B".into(),
                context: None,
            })
            .unwrap();
        let Step::Write(read) = m
            .accept(json!({"type":"response","id":cmd["id"],"ok":false,
            "error":"controlled failure after possible publication"}))
            .unwrap()
        else {
            panic!()
        };
        assert_eq!(read["type"], "gui.models.list");
        let Step::Control(Update::Choices(updated)) = reply(&mut m, &read, models(actual)) else {
            panic!()
        };
        assert!(updated.message.unwrap().contains("Request failed"));
        assert_eq!(m.model(), Some(actual));
        assert!(m.ready());
    }
}
#[test]
fn stale_unoffered_and_concurrent_choices_never_allocate_commands() {
    let mut m = connected();
    let menu = open(&mut m, Kind::Model);
    assert!(
        m.request_control(Intent::Select {
            revision: menu.revision + 1,
            value: "model-B".into(),
            context: None,
        })
        .is_err()
    );
    assert!(
        m.request_control(Intent::Select {
            revision: menu.revision,
            value: "invented".into(),
            context: None,
        })
        .is_err()
    );
    let cmd = m
        .request_control(Intent::Select {
            revision: menu.revision,
            value: "model-B".into(),
            context: None,
        })
        .unwrap();
    assert!(m.request_control(Intent::Open(Kind::Thinking)).is_err());
    let Step::Write(read) = reply(&mut m, &cmd, models("model-B")) else {
        panic!()
    };
    reply(&mut m, &read, models("model-B"));
    assert!(
        m.request_control(Intent::Select {
            revision: menu.revision,
            value: "model-A".into(),
            context: None,
        })
        .is_err()
    );
}
#[test]
fn thinking_default_is_explicit_and_effective_level_requires_readback() {
    let mut m = connected();
    let menu = open(&mut m, Kind::Thinking);
    assert_eq!(menu.current, "default");
    let cmd = m
        .request_control(Intent::Select {
            revision: menu.revision,
            value: "high".into(),
            context: None,
        })
        .unwrap();
    assert_eq!(cmd["level"], "high");
    let Step::Write(read) = reply(&mut m, &cmd, json!({"level":"high"})) else {
        panic!()
    };
    assert_eq!(read["model"], "model-A");
    let Step::Control(Update::Choices(menu)) = reply(&mut m, &read, thinking(json!("high"))) else {
        panic!()
    };
    assert_eq!(menu.current, "high");
}
#[test]
fn failed_readback_or_success_with_wrong_effect_blocks_the_connection() {
    for rejected in [false, true] {
        let mut m = connected();
        let menu = open(&mut m, Kind::Thinking);
        let cmd = m
            .request_control(Intent::Select {
                revision: menu.revision,
                value: "high".into(),
                context: None,
            })
            .unwrap();
        let Step::Write(read) = reply(&mut m, &cmd, json!({"level":"high"})) else {
            panic!()
        };
        let response = if rejected {
            json!({"type":"response","id":read["id"],"ok":false,"error":"unavailable"})
        } else {
            json!({"type":"response","id":read["id"],"ok":true,"result":thinking(json!("low"))})
        };
        assert!(m.accept(response).is_err());
        assert!(!m.ready());
    }
}
#[test]
fn read_only_error_is_recoverable_but_model_events_need_owned_operation() {
    let mut m = connected();
    let cmd = m.request_control(Intent::Open(Kind::Model)).unwrap();
    assert!(matches!(
        m.accept(
            json!({"type":"response","id":cmd["id"],"ok":false,"error":"catalogue unavailable"})
        )
        .unwrap(),
        Step::Control(Update::Rejected(_))
    ));
    assert!(m.ready());
    let menu = open(&mut m, Kind::Model);
    m.request_control(Intent::Select {
        revision: menu.revision,
        value: "model-B".into(),
        context: None,
    })
    .unwrap();
    let event = json!({"type":"model_state","backend":"claude","model":"model-B"});
    m.accept(event.clone()).unwrap();
    assert!(
        m.accept(
            json!({"type":"gui.event","event_id":"12345678-1234-4123-8123-123456789abc",
        "session_id":"session","operation_id":"not-the-command","event":event})
        )
        .is_err()
    );
}
#[test]
fn catalogue_and_thinking_schema_limits_are_explicit() {
    let mut value = models("model-A");
    value["models"][0]["current"] = json!(false);
    assert!(Choices::parse(Kind::Model, &value, 1, "session").is_err());
    let mut value = models("model-A");
    value["models"][0]["loaded"] = json!("yes");
    assert!(Choices::parse(Kind::Model, &value, 1, "session").is_err());
    let mut value = models("model-A");
    value["models"] = json!(vec![value["models"][0].clone(); 513]);
    assert!(Choices::parse(Kind::Model, &value, 1, "session").is_err());
    for value in [json!(true), json!(4), json!("x".repeat(65))] {
        assert!(Choices::parse(Kind::Thinking, &thinking(value), 1, "session").is_err());
    }
    let mut value = thinking(Value::Null);
    value["thinking"].as_object_mut().unwrap().remove("level");
    assert!(Choices::parse(Kind::Thinking, &value, 1, "session").is_err());
    let mut value = thinking(Value::Null);
    value["thinking"]["levels"] = json!(["default", "default"]);
    assert!(Choices::parse(Kind::Thinking, &value, 1, "session").is_err());
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn view() -> View {
    let mut v = View::default();
    v.open(Kind::Model);
    v.take_intent();
    v.observe(Update::Choices(
        Choices::parse(Kind::Model, &models("model-A"), 1, "session").unwrap(),
    ));
    v
}
fn draw(v: &mut View, w: u16, h: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
#[test]
fn selector_requires_visible_fresh_intent_and_never_mutates_on_focus() {
    let mut v = view();
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    assert!(draw(&mut v, 80, 24).contains("Select a model"));
    v.key(key(KeyCode::Down));
    assert!(v.take_intent().is_none());
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    v.key(repeat);
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(),Some(Intent::Select{value,..})if value=="model-B"));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.close();
    assert!(v.waiting); // Closing cannot invent an operation outcome.
    v.observe(Update::Rejected("source refused".into()));
    assert!(!v.waiting && !v.open);
}
#[test]
fn resize_and_pointer_changes_invalidate_hidden_confirmation() {
    let mut v = view();
    draw(&mut v, 80, 24);
    // Python's picker at 80x24: the box's first row line is row 8, from column 4.
    v.mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 8,
        modifiers: KeyModifiers::NONE,
    });
    draw(&mut v, 12, 6);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 80, 24);
    v.mouse(MouseEvent {
        kind: MouseEventKind::Up(MouseButton::Left),
        column: 10,
        row: 8,
        modifiers: KeyModifiers::NONE,
    });
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(),Some(Intent::Select{value,..})if value=="model-A"));
}
#[test]
fn unsent_intent_can_be_closed_without_io_and_tab_only_reads() {
    let mut v = view();
    draw(&mut v, 80, 24);
    v.key(key(KeyCode::Enter));
    v.close();
    assert!(!v.waiting);
    assert!(v.take_intent().is_none());
    let mut v = view();
    draw(&mut v, 80, 24);
    v.key(key(KeyCode::Tab));
    assert_eq!(v.take_intent(), Some(Intent::Open(Kind::Thinking)));
}
#[test]
fn model_change_clears_old_context_rate_and_thinking() {
    use litetui_terminal::{
        footer::FieldKey as K,
        runtime_state::{State, Update as S},
    };
    let mut state = State::default();
    state.apply(S::from_event(&json!({"type":"ready","model":"model-A","thinking_level":"high","context_length":200000})).unwrap().unwrap());
    state.apply(
        S::from_event(
            &json!({"type":"usage","usage":{"context_tokens":1000,"max_context_tokens":4000}}),
        )
        .unwrap()
        .unwrap(),
    );
    let menu = Choices::parse(Kind::Model, &models("model-B"), 1, "session").unwrap();
    state.apply(S::from_choices(&menu));
    let fields = state.footer_fields();
    assert!(
        fields
            .iter()
            .any(|f| f.key == K::Model && f.text == "model-B")
    );
    assert!(
        !fields
            .iter()
            .any(|f| matches!(f.key, K::Percent | K::Throughput | K::Thinking))
    );
    assert!(
        fields
            .iter()
            .any(|f| f.key == K::Context && f.text == "ctx —")
    );
}

#[test]
fn selector_uses_theme_background_and_escapes_metadata_across_compact_sizes() {
    for theme in litetui_terminal::theme::builtin_themes() {
        for (w, h) in [(36, 14), (52, 18), (100, 28)] {
            let mut value = models("model-A");
            value["models"][0]["name"] = json!("لا 雪 \u{1b}[31m \u{202e} name");
            let mut v = View::default();
            v.open(Kind::Model);
            v.take_intent();
            v.observe(Update::Choices(
                Choices::parse(Kind::Model, &value, 1, "session").unwrap(),
            ));
            let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
            terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
            let symbols: String = terminal
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(!symbols.contains('\u{1b}') && !symbols.contains('\u{202e}'));
            // Python's modal covers the screen in the theme background; the box is $surface.
            let buffer = terminal.backend().buffer();
            assert_eq!(buffer[(0, 0)].bg, theme.background);
            assert!(buffer.content.iter().any(|c| c.bg == theme.surface));
            v.key(key(KeyCode::Enter));
            assert!(matches!(v.take_intent(),Some(Intent::Select{value,..})if value=="model-A"));
        }
    }
}

#[test]
fn horizontal_inspection_reaches_every_source_field_without_losing_graphemes() {
    let mut v = view();
    let menu = v.menu.as_mut().unwrap();
    menu.current = format!("{}CURRENT_TAIL", "c".repeat(1000));
    menu.rows[0].label = format!("{}ROW_TAIL", "雪👩‍💻".repeat(500));
    menu.note = format!("{}NOTE_TAIL", "\u{1b}".repeat(1024));
    v.notice = format!("{}ERROR_TAIL", "e".repeat(4096));
    assert!(draw(&mut v, 80, 24).contains("Select a model"));
    let mut found = [false; 4];
    for _ in 0..750 {
        let text = draw(&mut v, 80, 24);
        for (i, tail) in ["CURRENT_TAIL", "ROW_TAIL", "NOTE_TAIL", "ERROR_TAIL"]
            .iter()
            .enumerate()
        {
            found[i] |= text.contains(tail);
        }
        assert!(!text.contains('\u{1b}'));
        v.key(key(KeyCode::Right));
    }
    // Python's picker shows the title, the rows (wrapped, not scrolled sideways), the hint:
    // no current line, no note, no status line. A 2000-cell row wraps past the 19 visible lines.
    assert_eq!(found, [false, false, false, false]);
    // Inspection never changes or applies the selected source value.
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Select { value, .. }) if value == "model-A"));
}
