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

#[test]
fn footer_and_ready_notice_use_reported_tool_policy() {
    let mut app = Demo::live("claude".into(), "reported-model".into());
    app.observe_live(Observation::State(update(json!({
        "type":"ready","model":"reported-model","tools_enabled":true,"mcp_enabled":false,
        "tool_profile":"strict","approval_route":"host",
        "authority_glyphs":{"strict":"||","interactive":"||","autonomous":">>"},
        "seat_name":"RustLive","context_loaded":true,"context_length":8192,"version":"0.29.1",
    }))))
    .unwrap();
    app.observe_live(Observation::Ready).unwrap();
    // Python reports the policy in the footer (below), not in a ready notice.
    assert!(!app.notice.contains("Tools on"), "{}", app.notice);
    // Python's footer (textfmt.profile_text): "<glyph> <profile> on", no tools on/off.
    let state = &app.live.as_ref().unwrap().runtime_state;
    assert_eq!(field(state, K::Authority).as_deref(), Some("|| strict on"));
    assert_eq!(field(state, K::Seat).as_deref(), Some("RustLive"));
    assert_eq!(field(state, K::Context).as_deref(), Some("ctx — / 8,192"));
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|f| ui::draw(f, &mut app)).unwrap();
    let cells = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect::<String>();
    assert!(cells.contains("|| strict on") && cells.contains("RustLive"));
    assert!(
        cells.contains("meters:off") && cells.contains("v0.29.1"),
        "{cells}"
    );
    // Python's composer: tall border, its placeholder, and the mic / pause chips.
    assert!(
        cells.contains("▊Message... (Ctrl+V paste | Ctrl+O image | /help)"),
        "{cells}"
    );
    assert!(cells.contains(" ○ mic  ⏸ pause  ▁▎"), "{cells}");
    app.finish_live(None);
    assert!(app.footer_fields.is_empty());
}

fn screen(app: &mut Demo, width: u16, height: u16) -> Vec<String> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| {
            (0..width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect()
}

const SPLASH: &str =
    "\n  ██     ██████\n  ██       ██  \n  ██████ ██████\n      a tagline · /help\n";

#[test]
fn start_screen_is_pythons_title_banner_and_connected_line() {
    let mut app = Demo::live("custom".into(), "perf-fixture".into());
    app.observe_live(Observation::State(update(json!({
        "type":"ready","model":"perf-fixture",
        "title":"LiteTUI — Custom server · perf-fixture · no tools · think:default",
        "banner":SPLASH,
    }))))
    .unwrap();
    app.observe_live(Observation::Ready).unwrap();
    let rows = screen(&mut app, 100, 30);
    // Row 0: " ⭘" at column 1, the title centered between the 8-cell icon and 10-cell clock.
    assert!(rows[0].starts_with(" ⭘      "), "{}", rows[0]);
    let title = "LiteTUI — Custom server · perf-fixture · no tools · think:default";
    let room = 100 - 18;
    let left = 8 + (room - title.chars().count()) / 2;
    assert_eq!(
        rows[0].chars().skip(left).collect::<String>().trim_end(),
        title
    );
    // Row 1 is blank (Python's now row), then the splash, centered, then the system line.
    assert_eq!(rows[1].trim(), "");
    let at = |needle: &str| rows.iter().position(|r| r.contains(needle)).expect(needle);
    let art = at("██     ██████");
    assert_eq!(rows[art - 1].trim(), "");
    assert_eq!(rows[art + 2].trim(), "██████ ██████");
    let tagline = at("a tagline · /help");
    assert_eq!(tagline, art + 4);
    let connected = at("Connected — model: perf-fixture");
    assert_eq!(connected, tagline + 2);
    let row = &rows[connected];
    let text = "Connected — model: perf-fixture";
    assert_eq!(
        row.find(text).map(|b| row[..b].chars().count()),
        Some(1 + (98 - 31) / 2)
    );
    // No Rust-only status or ready notice rows remain.
    assert!(
        rows.iter()
            .all(|r| !r.contains("Ctrl+Q quits") && !r.contains("scroll 0/0"))
    );
    // A later model_state carries Python's new title.
    app.observe_live(Observation::State(update(json!({
        "type":"model_state","backend":"custom","model":"perf-fixture","title":"LiteTUI — retitled",
    }))))
    .unwrap();
    assert!(screen(&mut app, 100, 30)[0].contains("LiteTUI — retitled"));
}

#[test]
fn long_title_is_cut_with_an_ellipsis_and_narrow_splash_falls_back() {
    let mut app = Demo::live("custom".into(), "m".into());
    let long = format!("LiteTUI — {}", "x".repeat(200));
    app.observe_live(Observation::State(update(json!({
        "type":"ready","model":"m","title":long,"banner":format!("\n  {}\n  ██\n      tag\n", "█".repeat(48)),
    }))))
    .unwrap();
    app.observe_live(Observation::Ready).unwrap();
    let rows = screen(&mut app, 60, 20);
    let bar: Vec<char> = rows[0].chars().collect();
    // Title cells 8..50 are filled and end with "…"; the clock space stays blank.
    assert_eq!(bar[49], '…', "{}", rows[0]);
    assert!(bar[50..].iter().all(|c| *c == ' '));
    let narrow = screen(&mut app, 40, 20);
    // 48-cell art does not fit 40 columns: Python's compact wordmark replaces it.
    assert!(rows.iter().any(|r| r.contains(&"█".repeat(48))));
    assert!(
        narrow.iter().any(|r| r.contains("█   ███ ███ ███")),
        "{narrow:?}"
    );
}

#[test]
fn malformed_title_or_banner_is_refused() {
    for (key, limit) in [("title", 4096), ("banner", 8192)] {
        for bad in [json!(7), json!("x".repeat(limit + 1))] {
            let mut event = json!({"type":"ready","model":"m"});
            event[key] = bad.clone();
            assert!(Update::from_event(&event).is_err(), "{key}");
        }
    }
    assert!(Update::from_event(&json!({"type":"model_state","title":3})).is_err());
    assert!(
        Update::from_event(&json!({"type":"model_state","backend":"b","model":"m"}))
            .unwrap()
            .is_none()
    );
}
