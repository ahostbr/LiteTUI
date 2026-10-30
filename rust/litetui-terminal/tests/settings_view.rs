use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::settings_view::{Intent, Snapshot, Update, View};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

fn state() -> Value {
    json!({"session_id":"owned-session","settings":{
        "metadata":[
            {"name":"max_tokens_chat","label":"Output tokens","type":"int","apply":"immediate","source":"settings","read_only":false,"secret":false},
            {"name":"lm_host","label":"Server address","type":"str","apply":"reconnect","source":"CLI","read_only":true,"secret":false},
            {"name":"skill_roots","label":"Skill roots","type":"list[str]","apply":"restart","source":"settings","read_only":false,"secret":false}
        ],
        "requested":{"max_tokens_chat":2048,"lm_host":"http://requested","skill_roots":["new"]},
        "effective":{"max_tokens_chat":2048,"lm_host":"http://effective","skill_roots":["old"]},
        "deferred":{"lm_host":"reconnect","skill_roots":"restart"}
    }})
}
fn snapshot(value: &Value) -> Snapshot {
    Snapshot::parse(value, "owned-session").unwrap()
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn view(value: &Value) -> View {
    let mut view = View::default();
    view.open();
    assert_eq!(view.take_intent(), Some(Intent::Read));
    view.observe(Update::Snapshot(snapshot(value)));
    view
}
fn draw(view: &mut View, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    terminal
        .draw(|frame| view.draw(frame, frame.area(), &theme))
        .unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
#[test]
fn requested_effective_deferred_and_invocation_authority_are_distinct() {
    let snapshot = snapshot(&state());
    assert_eq!(snapshot.rows.len(), 3);
    assert!(!snapshot.rows[0].deferred);
    assert_eq!(snapshot.rows[0].requested, "2048");
    assert!(snapshot.rows[1].read_only && snapshot.rows[1].deferred);
    assert_eq!(snapshot.rows[1].source, "CLI");
    assert_eq!(snapshot.rows[1].requested, "\"http://requested\"");
    assert_eq!(snapshot.rows[1].effective, "\"http://effective\"");
    assert_eq!(snapshot.rows[2].apply, "restart");
}
#[test]
fn rejects_foreign_missing_duplicate_and_contradictory_contracts() {
    for fault in [
        "foreign",
        "duplicate",
        "missing-request",
        "missing-effect",
        "missing-metadata",
        "extra-request",
        "extra-effective",
        "extra-deferred",
        "wrong-deferred",
        "unchanged-deferred",
        "immediate-deferred",
        "mode",
        "authority",
        "metadata-empty",
        "name",
        "type",
        "source",
    ] {
        let mut state = state();
        match fault {
            "foreign" => state["session_id"] = json!("foreign"),
            "duplicate" => {
                let row = state["settings"]["metadata"][0].clone();
                state["settings"]["metadata"]
                    .as_array_mut()
                    .unwrap()
                    .push(row);
            }
            "missing-request" => {
                state["settings"]["requested"]
                    .as_object_mut()
                    .unwrap()
                    .remove("max_tokens_chat");
            }
            "missing-effect" => {
                state["settings"]["effective"]
                    .as_object_mut()
                    .unwrap()
                    .remove("max_tokens_chat");
            }
            "missing-metadata" => {
                state["settings"]["metadata"].as_array_mut().unwrap().pop();
            }
            "extra-request" => state["settings"]["requested"]["hidden"] = json!(true),
            "extra-effective" => state["settings"]["effective"]["hidden"] = json!(true),
            "extra-deferred" => state["settings"]["deferred"]["hidden"] = json!("restart"),
            "wrong-deferred" => state["settings"]["deferred"]["lm_host"] = json!("restart"),
            "unchanged-deferred" => {
                state["settings"]["deferred"]["max_tokens_chat"] = json!("reconnect")
            }
            "immediate-deferred" => state["settings"]["metadata"][1]["apply"] = json!("immediate"),
            "mode" => state["settings"]["metadata"][1]["apply"] = json!("maybe"),
            "authority" => state["settings"]["metadata"][1]["read_only"] = json!(1),
            "metadata-empty" => state["settings"]["metadata"] = json!([]),
            "name" => state["settings"]["metadata"][1]["name"] = json!("lm\nhost"),
            "type" => state["settings"]["metadata"][1]["type"] = json!(null),
            "source" => state["settings"]["metadata"][1]["source"] = json!(""),
            _ => unreachable!(),
        }
        assert!(Snapshot::parse(&state, "owned-session").is_err(), "{fault}");
    }
}
#[test]
fn budgets_apply_to_values_row_count_and_total_bytes() {
    let mut value = state();
    let big = json!("x".repeat(16 * 1024));
    value["settings"]["requested"]["max_tokens_chat"] = big.clone();
    value["settings"]["effective"]["max_tokens_chat"] = big;
    assert!(
        Snapshot::parse(&value, "owned-session")
            .unwrap_err()
            .to_string()
            .contains("value")
    );
    let mut value = state();
    let row = value["settings"]["metadata"][0].clone();
    value["settings"]["metadata"] = json!(vec![row; 513]);
    assert!(Snapshot::parse(&value, "owned-session").is_err());
    let mut value = state();
    value["settings"]["extra"] = json!("x".repeat(256 * 1024));
    assert!(
        Snapshot::parse(&value, "owned-session")
            .unwrap_err()
            .to_string()
            .contains("byte budget")
    );
}
#[test]
fn readonly_keys_never_create_a_mutation_and_navigation_shows_pending_values() {
    let mut view = view(&state());
    assert!(draw(&mut view, 100, 30).contains("Output tokens"));
    view.key(key(KeyCode::Down));
    let screen = draw(&mut view, 100, 30);
    assert!(
        screen.contains("http://requested")
            && screen.contains("http://effective")
            && screen.contains("PENDING")
    );
    for code in [
        KeyCode::Enter,
        KeyCode::Char('s'),
        KeyCode::Delete,
        KeyCode::Backspace,
    ] {
        view.key(key(code));
        assert!(view.take_intent().is_none());
    }
    view.key(key(KeyCode::Char('r')));
    assert!(view.waiting);
    assert_eq!(view.take_intent(), Some(Intent::Read));
    view.observe(Update::Snapshot(snapshot(&state())));
    assert!(draw(&mut view, 100, 30).contains("http://requested"));
}
#[test]
fn escape_closes_even_hidden_but_consumed_reads_still_settle() {
    let mut view = View::default();
    view.open();
    draw(&mut view, 20, 8);
    view.key(key(KeyCode::Esc));
    assert!(!view.owns_input() && view.take_intent().is_none());
    view.open();
    assert_eq!(view.take_intent(), Some(Intent::Read));
    view.key(key(KeyCode::Esc));
    assert!(!view.open && view.waiting);
    view.observe(Update::Snapshot(snapshot(&state())));
    assert!(!view.owns_input());
}
#[test]
fn hidden_modified_and_release_navigation_is_ignored() {
    let mut view = view(&state());
    view.key(key(KeyCode::End));
    assert!(draw(&mut view, 100, 30).contains("1 / 3"));
    view.key(KeyEvent::new_with_kind(
        KeyCode::End,
        KeyModifiers::NONE,
        KeyEventKind::Release,
    ));
    view.key(KeyEvent::new(KeyCode::End, KeyModifiers::CONTROL));
    assert!(draw(&mut view, 100, 30).contains("1 / 3"));
    view.key(key(KeyCode::End));
    assert!(draw(&mut view, 100, 30).contains("3 / 3"));
    view.hide();
    view.key(key(KeyCode::Home));
    assert!(draw(&mut view, 100, 30).contains("3 / 3"));
}
#[test]
fn long_values_are_scrollable_and_terminal_controls_are_escaped() {
    let mut value = state();
    value["settings"]["metadata"][0]["label"] = json!("雪 لا \u{1b}[31m\u{202e} label");
    let text = format!("{} Ω", "prefix ".repeat(150));
    value["settings"]["requested"]["max_tokens_chat"] = json!(text);
    value["settings"]["effective"]["max_tokens_chat"] =
        value["settings"]["requested"]["max_tokens_chat"].clone();
    let mut view = view(&value);
    let first = draw(&mut view, 60, 22);
    assert!(!first.contains('\u{1b}') && !first.contains('\u{202e}') && !first.contains("Ω"));
    for _ in 0..100 {
        view.key(key(KeyCode::PageDown));
    }
    let last = draw(&mut view, 60, 22);
    assert!(last.contains("Ω"), "{last}");
    for _ in 0..100 {
        view.key(key(KeyCode::PageUp));
    }
    assert!(!draw(&mut view, 60, 22).contains("Ω"));
}
#[test]
fn pointer_selection_requires_a_pair_on_unchanged_geometry() {
    let mut view = view(&state());
    // 100x30 -> centered 98x28 inner(2,2); header y2, row0 y3, row1 y4.
    draw(&mut view, 100, 30);
    let mouse = |kind, row| MouseEvent {
        kind,
        column: 4,
        row,
        modifiers: KeyModifiers::NONE,
    };
    view.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 4));
    assert!(draw(&mut view, 100, 30).contains("1 / 3"));
    view.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 4));
    draw(&mut view, 90, 28);
    view.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 4));
    assert!(draw(&mut view, 100, 30).contains("1 / 3"));
    view.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 4));
    view.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 4));
    assert!(draw(&mut view, 100, 30).contains("2 / 3"));
}
#[test]
fn minimum_view_and_all_themes_remain_readable_and_closable() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut view = view(&state());
        let mut terminal = Terminal::new(TestBackend::new(42, 18)).unwrap();
        terminal
            .draw(|frame| view.draw(frame, frame.area(), &theme))
            .unwrap();
        assert_eq!(
            terminal
                .backend()
                .buffer()
                .content
                .iter()
                .find(|c| c.symbol() == ">")
                .unwrap()
                .bg,
            theme.background
        );
        view.key(key(KeyCode::Esc));
        assert!(!view.owns_input());
    }
}

#[test]
fn secrets_are_never_retained_or_drawn_in_the_inspector() {
    let mut state = state();
    state["settings"]["metadata"][0]["secret"] = json!(true);
    state["settings"]["requested"]["max_tokens_chat"] = json!("test-secret-not-a-real-key");
    state["settings"]["effective"]["max_tokens_chat"] = json!("test-secret-not-a-real-key");
    let parsed = snapshot(&state);
    assert!(parsed.rows[0].secret);
    assert_eq!(parsed.rows[0].requested, "Set (hidden)");
    assert!(!format!("{parsed:?}").contains("test-secret-not-a-real-key"));
    assert!(!draw(&mut view(&state), 100, 30).contains("test-secret-not-a-real-key"));
    state["settings"]["requested"]["max_tokens_chat"] = json!("");
    state["settings"]["effective"]["max_tokens_chat"] = json!("");
    assert_eq!(snapshot(&state).rows[0].effective, "Not set");
    state["settings"]["metadata"][0]
        .as_object_mut()
        .unwrap()
        .remove("secret");
    assert!(Snapshot::parse(&state, "owned-session").is_err());
}

#[test]
fn global_minimum_shows_resize_notice_instead_of_invisible_input_owner() {
    for (width, height) in [(36, 14), (41, 17), (42, 17), (41, 18)] {
        let mut view = view(&state());
        let screen = draw(&mut view, width, height);
        assert!(screen.contains("Settings need at least 42×18."));
        assert!(screen.contains("Esc to close"));
        assert!(view.owns_input());
        view.key(key(KeyCode::Esc));
        assert!(!view.owns_input());
    }
}

#[test]
fn canonical_next_turn_and_reload_modes_keep_distinct_effective_values() {
    let mut state = state();
    state["settings"]["metadata"][0]["apply"] = json!("next_turn");
    state["settings"]["metadata"][1]["apply"] = json!("reload");
    state["settings"]["deferred"]["lm_host"] = json!("reload");
    let snapshot = snapshot(&state);
    assert_eq!(snapshot.rows[0].apply, "next_turn");
    assert!(!snapshot.rows[0].deferred);
    assert_eq!(snapshot.rows[1].apply, "reload");
    assert!(snapshot.rows[1].deferred);
    state["settings"]["effective"]["max_tokens_chat"] = json!(1);
    state["settings"]["deferred"]["max_tokens_chat"] = json!("next_turn");
    assert!(Snapshot::parse(&state, "owned-session").is_err());
}
