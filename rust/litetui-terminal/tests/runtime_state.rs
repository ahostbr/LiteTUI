use litetui_terminal::{
    demo::Demo,
    footer::FieldKey as K,
    live::Observation,
    runtime_state::{State, Update},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn update(event: Value) -> Update {
    Update::from_event(&event).unwrap().unwrap()
}
fn ready() -> Update {
    update(
        json!({"type":"ready","model":"reported-model","conversation_id":"abcdefgh-more","thinking_level":"low","context_length":200000}),
    )
}
fn field(state: &State, key: K) -> Option<String> {
    state
        .footer_fields()
        .into_iter()
        .find(|field| field.key == key)
        .map(|field| field.text)
}
#[test]
fn ceiling_is_not_a_confirmed_window_and_missing_maximum_clears_old_value() {
    let mut state = State::default();
    assert!(state.footer_fields().is_empty());
    state.apply(ready());
    assert_eq!(
        field(&state, K::Context).as_deref(),
        Some("ctx — / 200,000 max")
    );
    assert_eq!(field(&state, K::Percent), None);
    state.apply(update(
        json!({"type":"usage","usage":{"context_tokens":1000,"max_context_tokens":8000}}),
    ));
    assert_eq!(
        field(&state, K::Context).as_deref(),
        Some("ctx 1,000 / 8,000")
    );
    assert_eq!(field(&state, K::Percent).as_deref(), Some("12%"));
    state.apply(update(json!({"type":"usage","usage":{"context_tokens":0}})));
    assert_eq!(field(&state, K::Context).as_deref(), Some("ctx 0 / ?"));
    assert_eq!(field(&state, K::Percent), None);
}
#[test]
fn missing_optional_facts_remain_unknown_not_requested_defaults() {
    let mut state = State::default();
    state.apply(update(json!({"type":"ready","model":"reported-model"})));
    assert_eq!(
        state
            .footer_fields()
            .iter()
            .map(|f| f.key)
            .collect::<Vec<_>>(),
        vec![K::Context, K::Model]
    );
    for key in ["conversation_id", "thinking_level", "context_length"] {
        let event = json!({"type":"ready","model":"m",key:Value::Null});
        assert!(Update::from_event(&event).is_ok());
    }
    state.apply(ready());
    assert_eq!(field(&state, K::Conversation).as_deref(), Some("abcdefgh"));
    assert_eq!(field(&state, K::Thinking).as_deref(), Some("think:low"));
    assert_eq!(field(&state, K::Model).as_deref(), Some("reported-model"));
}
#[test]
fn counts_reject_wrong_types_and_rates_are_finite_nonnegative() {
    for bad in [
        json!(true),
        json!(-1),
        json!(0.5),
        json!("4"),
        json!(1e30),
        json!([]),
        json!({}),
    ] {
        for key in ["context_tokens", "max_context_tokens"] {
            let mut usage = json!({"context_tokens":1});
            usage[key] = bad.clone();
            assert!(
                Update::from_event(&json!({"type":"usage","usage":usage})).is_err(),
                "{key}={bad}"
            );
        }
        assert!(
            Update::from_event(&json!({"type":"ready","model":"m","context_length":bad})).is_err()
        );
    }
    for usage in [
        Value::Null,
        json!([]),
        json!({}),
        json!({"context_tokens":null}),
        json!({"context_tokens":1,"max_context_tokens":0}),
    ] {
        assert!(Update::from_event(&json!({"type":"usage","usage":usage})).is_err());
    }
    assert!(
        Update::from_event(&json!({"type":"usage","usage":{"context_tokens":u64::MAX}})).is_ok()
    );
    for rate in [json!(true), json!("3"), json!(-0.1), json!([])] {
        assert!(Update::from_event(&json!({"type":"turn_end","tps":rate})).is_err());
    }
}
#[test]
fn rate_is_settled_only_then_cleared_on_next_turn_without_inventing_output_count() {
    let mut state = State::default();
    state.apply(ready());
    state.apply(update(json!({"type":"turn_end","tps":12.34})));
    assert_eq!(field(&state, K::Throughput).as_deref(), Some("12.3 tok/s"));
    state.apply(update(json!({"type":"turn_start"})));
    assert_eq!(field(&state, K::Throughput).as_deref(), Some("12.3 tok/s"));
    state.apply(update(json!({"type":"turn_start", "provider":"claude"})));
    assert_eq!(field(&state, K::Throughput), None);
    state.apply(update(json!({"type":"turn_end","tps":0})));
    assert_eq!(field(&state, K::Throughput).as_deref(), Some("0.0 tok/s"));
    state.apply(update(json!({"type":"turn_end","tps":null})));
    assert_eq!(field(&state, K::Throughput), None);
    state.apply(update(
        json!({"type":"usage","usage":{"context_tokens":42,"output_tokens":9876}}),
    ));
    assert_eq!(field(&state, K::Throughput), None);
}
#[test]
fn metadata_is_bounded_and_terminal_controls_are_not_rendered_raw() {
    for (key, limit) in [
        ("model", 1024),
        ("conversation_id", 128),
        ("thinking_level", 64),
    ] {
        for bad in [json!(true), json!(42), json!("x".repeat(limit + 1))] {
            let mut event = json!({"type":"ready","model":"m"});
            event[key] = bad;
            assert!(Update::from_event(&event).is_err());
        }
    }
    let mut state = State::default();
    state.apply(update(json!({"type":"ready","model":"bad\u{001b}[2J","thinking_level":"low\u{0007}\n\t","conversation_id":"\u{001b}12345678"})));
    assert!(
        state
            .footer_fields()
            .iter()
            .all(|f| !f.text.chars().any(|c| c.is_control()))
    );
}
#[test]
fn real_state_reaches_footer_renderer_and_closure_removes_controls() {
    let mut app = Demo::live("claude".into(), "requested-not-reported".into());
    app.observe_live(Observation::State(ready())).unwrap();
    app.observe_live(Observation::Ready).unwrap();
    app.observe_live(Observation::State(update(
        json!({"type":"usage","usage":{"context_tokens":4000,"max_context_tokens":8000}}),
    )))
    .unwrap();
    let mut terminal = Terminal::new(TestBackend::new(180, 35)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let text = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>();
    for expected in [
        "50%",
        "ctx 4,000 / 8,000",
        "abcdefgh",
        "reported-model",
        "think:low",
    ] {
        assert!(text.contains(expected), "{expected}");
    }
    for key in [K::Seat, K::Authority, K::Plan] {
        assert!(app.footer_fields.iter().all(|f| f.key != key));
    }
    app.finish_live(Some("closed for test".into()));
    assert!(app.footer_fields.is_empty());
    assert!(app.live.as_ref().unwrap().closed);
    assert!(app.observe_live(Observation::State(ready())).is_err());
    assert!(app.footer_fields.is_empty());
    assert_eq!(app.notice, "closed for test");
}
