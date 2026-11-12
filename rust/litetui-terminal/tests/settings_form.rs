//! /settings drawn from Python's own settings dialog (native_settings_form.py).
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::settings_form::{Form, Intent, Update, View};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

const S: &str = "11111111-1111-4111-8111-111111111111";

fn form_json(kind: &str) -> Value {
    json!({
        "version": 1, "kind": kind, "session_id": S, "agent_id": "agent", "backend": "custom", "model": "m",
        "form": "a".repeat(64), "title": "Settings",
        "subtitle": "Esc cancels · Ctrl+S saves · ←/→ or click to change tab",
        "search_placeholder": "Find a setting, help text, or section…",
        "search_status": "",
        "tabs": [
            {"id": "tab-model", "label": "Model", "scroll": true, "items": [
                {"kind": "section", "id": "model-connection", "label": "▾ Connection & default model  ·  5 controls · conversation", "open": true, "section": null, "search": "model connection & default model default model lm studio host", "fields": {"default_model": "default_model default model", "pin_default_model": "pin_default_model re-apply on every connect", "lm_host": "lm_host lm studio host"}},
                {"kind": "row", "indent": false, "section": "model-connection", "items": [
                    {"kind": "label", "text": "Default model", "inline": false, "section": "model-connection"},
                    {"kind": "select", "id": "f-default_model", "value": "", "options": [["(whatever LM Studio has loaded)", ""], ["m", "m"]], "disabled": false, "section": "model-connection"},
                    {"kind": "text", "text": "Selected on connect when the server is serving it.", "id": null, "classes": ["set-help"], "section": "model-connection"}
                ]},
                {"kind": "row", "indent": false, "section": "model-connection", "items": [
                    {"kind": "line", "section": "model-connection", "items": [
                        {"kind": "switch", "id": "f-pin_default_model", "value": false, "disabled": false, "section": "model-connection"},
                        {"kind": "label", "text": "Re-apply on every connect", "inline": true, "section": "model-connection"}
                    ]},
                    {"kind": "text", "text": "Off = only pick it when nothing is loaded.", "id": null, "classes": ["set-help"], "section": "model-connection"}
                ]},
                {"kind": "row", "indent": false, "section": "model-connection", "items": [
                    {"kind": "label", "text": "LM Studio host", "inline": false, "section": "model-connection"},
                    {"kind": "input", "id": "f-lm_host", "value": "http://localhost:1234", "placeholder": "http://localhost:1234", "password": false, "disabled": false, "section": "model-connection"}
                ]},
                {"kind": "row", "indent": false, "section": "model-connection", "items": [
                    {"kind": "label", "text": "Anthropic API key (paid)", "inline": false, "section": "model-connection"},
                    {"kind": "input", "id": "f-anthropic_api_key", "set": true, "placeholder": "unset", "password": true, "disabled": false, "section": "model-connection"}
                ]},
                {"kind": "section", "id": "model-engine", "label": "▸ Engine  ·  4 controls · device", "open": false, "section": null, "search": "model engine tools", "fields": {"tools_enabled": "tools_enabled tools enabled"}},
                {"kind": "row", "indent": false, "section": "model-engine", "items": [
                    {"kind": "line", "section": "model-engine", "items": [
                        {"kind": "switch", "id": "f-tools_enabled", "value": false, "disabled": true, "reason": "Changed with its reviewed control (Ctrl+T, Shift+Tab, /tools, /mcp), not here.", "section": "model-engine"},
                        {"kind": "label", "text": "Tools enabled", "inline": true, "section": "model-engine"}
                    ]}
                ]}
            ]},
            {"id": "tab-strata", "label": "Strata", "scroll": true, "items": [
                {"kind": "text", "text": "Strata settings", "id": null, "classes": [], "section": null}
            ]}
        ],
        "buttons": [
            {"id": "set-save", "label": "Save", "disabled": false},
            {"id": "set-cancel", "label": "Cancel", "disabled": false},
            {"id": "set-defaults", "label": "Restore defaults", "disabled": false}
        ]
    })
}
fn form() -> Form {
    Form::parse(
        &form_json("settings_form"),
        "settings_form",
        S,
        "agent",
        "custom",
        "m",
    )
    .unwrap()
}
fn opened() -> View {
    let mut view = View::default();
    view.open();
    assert_eq!(view.take_intent(), Some(Intent::Read));
    view.observe(Update::Form(Box::new(form())));
    view
}
fn key(view: &mut View, code: KeyCode) {
    view.key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn screen(view: &mut View, width: u16, height: u16) -> Vec<String> {
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
    let buffer = terminal.backend().buffer();
    (0..height)
        .map(|y| (0..width).map(|x| buffer[(x, y)].symbol()).collect())
        .collect()
}
fn at(row: &str, column: usize) -> String {
    row.chars().skip(column).collect()
}

#[test]
fn parse_refuses_secrets_foreign_identity_and_bad_tokens() {
    form();
    let mut leaked = form_json("settings_form");
    leaked["tabs"][0]["items"][4]["items"][1]["value"] = json!("sk-secret");
    assert!(Form::parse(&leaked, "settings_form", S, "agent", "custom", "m").is_err());
    for (key, bad) in [
        ("session_id", json!("other")),
        ("agent_id", json!("x")),
        ("model", json!("n")),
        ("form", json!("short")),
        ("version", json!(2)),
    ] {
        let mut v = form_json("settings_form");
        v[key] = bad;
        assert!(
            Form::parse(&v, "settings_form", S, "agent", "custom", "m").is_err(),
            "{key}"
        );
    }
    let mut duplicate = form_json("settings_form");
    duplicate["tabs"][1]["items"][0] = json!({"kind": "input", "id": "f-lm_host", "value": "", "placeholder": "", "password": false, "disabled": false});
    assert!(Form::parse(&duplicate, "settings_form", S, "agent", "custom", "m").is_err());
}

#[test]
fn draws_pythons_box_tabs_section_rows_and_buttons_where_python_does() {
    let mut view = opened();
    let rows = screen(&mut view, 140, 45);
    // #set-box: 92 wide, 88% of 45 rows (39), centered: rows 3..41, columns 24..115.
    assert!(at(&rows[3], 24).starts_with("█▀▀▀"), "{}", rows[3]);
    assert!(at(&rows[41], 24).starts_with("█▄▄▄"));
    assert_eq!(rows[3].chars().nth(115), Some('█'));
    assert!(at(&rows[5], 27).starts_with("Settings"));
    assert!(at(&rows[6], 27).starts_with("Esc cancels · Ctrl+S saves"));
    assert!(at(&rows[8], 27).starts_with("▊▔▔"));
    assert!(at(&rows[9], 27).starts_with("▊  Find a setting"));
    // Tabs on row 12 (" Model " from column 27), Textual's underline on row 13.
    assert!(
        at(&rows[12], 27).starts_with(" Model  Strata "),
        "{}",
        rows[12]
    );
    assert!(at(&rows[13], 27).starts_with("╸━━━━━╺━"), "{}", rows[13]);
    // The first section header: blank margin row 14, tall header rows 15-17, label centered.
    assert!(at(&rows[15], 27).starts_with("▔▔▔"));
    assert!(rows[16].contains("▾ Connection & default model"));
    assert!(at(&rows[18], 28).starts_with("Default model"));
    assert!(at(&rows[19], 27).starts_with("▊▔▔"));
    assert!(at(&rows[20], 27).starts_with("▊  (whatever LM Studio has loaded)"));
    assert!(rows[20].contains("▼"));
    assert!(at(&rows[22], 28).starts_with("Selected on connect"));
    // Switch line: the 10-cell switch, its label on the switch's first row.
    assert!(
        at(&rows[24], 27).starts_with("▊▔▔▔▔▔▔▔▔▎ Re-apply on every connect"),
        "{}",
        rows[24]
    );
    // Below the fold at 45 rows: on a taller screen the secret shows only that it is set, and
    // the folded Engine section shows its header but not its rows.
    let tall = screen(&mut view, 140, 80);
    assert!(tall.iter().any(|r| r.contains("••••••••")));
    assert!(tall.iter().any(|r| r.contains("▸ Engine")));
    assert!(!tall.iter().any(|r| r.contains("Tools enabled")));
    // Buttons: content bottom rows 37-39, from column 29 (margin-left 2).
    assert!(at(&rows[37], 29).starts_with("▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔▔"));
    assert!(
        rows[38].contains("Save")
            && rows[38].contains("Cancel")
            && rows[38].contains("Restore defaults")
    );
}

#[test]
fn switch_select_and_input_edits_save_only_what_changed() {
    let mut view = opened();
    // Python's focus order: search box, Clear, tab bar, then the rows (section header first).
    for _ in 0..3 {
        key(&mut view, KeyCode::Tab);
    }
    key(&mut view, KeyCode::Tab); // select
    key(&mut view, KeyCode::Enter); // open choices
    key(&mut view, KeyCode::Down);
    key(&mut view, KeyCode::Enter); // choose "m"
    key(&mut view, KeyCode::Tab); // switch
    key(&mut view, KeyCode::Char(' '));
    key(&mut view, KeyCode::Tab); // lm_host input, caret at end
    for c in ":9".chars() {
        key(&mut view, KeyCode::Char(c));
    }
    key(&mut view, KeyCode::Backspace);
    key(&mut view, KeyCode::Char('5'));
    // Toggling back and forth leaves no edit behind.
    view.key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
    let Some(Intent::Save { form, values }) = view.take_intent() else {
        panic!("no save");
    };
    assert_eq!(form, "a".repeat(64));
    assert_eq!(
        Value::Object(values),
        json!({"f-default_model": "m", "f-pin_default_model": true, "f-lm_host": "http://localhost:1234:5"})
    );
    assert!(view.waiting);
}

#[test]
fn locked_reviewed_fields_and_folded_sections() {
    let mut view = opened();
    // Open the folded Engine section from its header, then try the reviewed switch.
    for _ in 0..8 {
        key(&mut view, KeyCode::Tab);
    }
    key(&mut view, KeyCode::Enter);
    // Its row is below the fold; focusing it scrolls it into view.
    key(&mut view, KeyCode::Tab);
    let rows = screen(&mut view, 140, 45);
    assert!(rows.iter().any(|r| r.contains("Tools enabled")), "{rows:?}");
    key(&mut view, KeyCode::Enter);
    let rows = screen(&mut view, 140, 45);
    assert!(
        rows.iter().any(|r| r.contains("reviewed control")),
        "{rows:?}"
    );
    view.key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
    // Nothing edited: Save closes, like Python's.
    assert_eq!(view.take_intent(), None);
    assert!(!view.owns_input());
}

#[test]
fn esc_with_edits_asks_save_discard_or_keep_editing() {
    let mut view = opened();
    for _ in 0..5 {
        key(&mut view, KeyCode::Tab);
    }
    key(&mut view, KeyCode::Enter); // toggle the switch
    key(&mut view, KeyCode::Esc);
    let rows = screen(&mut view, 140, 45);
    assert!(rows.iter().any(|r| r.contains("Unsaved settings changes")));
    assert!(
        rows.iter()
            .any(|r| r.contains("Keep editing") && r.contains("Discard"))
    );
    key(&mut view, KeyCode::Right); // Discard
    key(&mut view, KeyCode::Enter);
    assert!(!view.owns_input());
    assert_eq!(view.take_intent(), None);
}

#[test]
fn save_results_follow_pythons_dialog() {
    let saved = |status: &str, message: &str| {
        let mut v = form_json("settings_form_save");
        v["status"] = json!(status);
        v["message"] = json!(message);
        Update::saved(&v, S, "agent", "custom", "m").unwrap()
    };
    // Complete: the dialog closes.
    let mut view = opened();
    for _ in 0..5 {
        key(&mut view, KeyCode::Tab);
    }
    key(&mut view, KeyCode::Enter);
    view.key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
    view.take_intent();
    view.observe(saved("saved", "Settings saved."));
    assert!(!view.owns_input());
    // Refused: the draft stays and Python's message shows.
    let mut view = opened();
    for _ in 0..5 {
        key(&mut view, KeyCode::Tab);
    }
    key(&mut view, KeyCode::Enter);
    view.key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
    view.take_intent();
    view.observe(saved(
        "refused",
        "Cannot save — tool_iterations: 'x' is not a valid number",
    ));
    assert!(view.owns_input() && view.dirty());
    let rows = screen(&mut view, 140, 45);
    assert!(
        rows.iter()
            .any(|r| r.contains("Cannot save — tool_iterations"))
    );
    // Partial: saved, still open with the message, draft cleared.
    view.observe(saved(
        "partial",
        "Runtime changes are pending: lm_host apply on the next reconnect",
    ));
    assert!(view.owns_input() && !view.dirty());
    // Bad status is refused by the parser.
    let mut v = form_json("settings_form_save");
    v["status"] = json!("maybe");
    v["message"] = json!("");
    assert!(Update::saved(&v, S, "agent", "custom", "m").is_err());
}

#[test]
fn left_right_on_the_tab_bar_and_clicks_change_tab() {
    let mut view = opened();
    // Tab, Tab: from the search box past Clear to the tab bar (as the Python capture steps).
    key(&mut view, KeyCode::Tab);
    key(&mut view, KeyCode::Tab);
    key(&mut view, KeyCode::Right);
    let rows = screen(&mut view, 140, 45);
    assert!(rows.iter().any(|r| r.contains("Strata settings")));
    key(&mut view, KeyCode::Left);
    let rows = screen(&mut view, 140, 45);
    assert!(rows.iter().any(|r| r.contains("Default model")));
    // Click " Strata " on the tab row.
    view.mouse(crossterm::event::MouseEvent {
        kind: crossterm::event::MouseEventKind::Down(crossterm::event::MouseButton::Left),
        column: 36,
        row: 12,
        modifiers: KeyModifiers::NONE,
    });
    let rows = screen(&mut view, 140, 45);
    assert!(rows.iter().any(|r| r.contains("Strata settings")));
}

#[test]
fn search_is_pythons_section_and_field_match() {
    let mut view = opened();
    let rows = screen(&mut view, 140, 45);
    // Python's status is empty until the first search.
    assert!(
        !rows[8..11].join("").contains("Search labels"),
        "{}",
        rows[9]
    );
    // The search box has focus first; a pasted query runs Python's match once.
    assert!(view.accepts_paste());
    view.paste("lm studio host");
    let rows = screen(&mut view, 140, 45);
    assert!(rows[9].contains("lm studio host"));
    assert!(
        rows[9].contains("1 section · 1 direct field match"),
        "{}",
        rows[9]
    );
    // Only the matching field's row of the matching section shows; the engine section hides.
    assert!(rows.iter().any(|r| r.contains("LM Studio host")));
    assert!(!rows.iter().any(|r| r.contains("Default model")));
    assert!(!rows.iter().any(|r| r.contains("Engine")));
    // Python focuses the first matching control: typing now edits LM Studio host.
    key(&mut view, KeyCode::Char('x'));
    view.key(KeyEvent::new(KeyCode::Char('s'), KeyModifiers::CONTROL));
    let Some(Intent::Save { values, .. }) = view.take_intent() else {
        panic!("no save");
    };
    assert_eq!(
        Value::Object(values),
        json!({"f-lm_host": "http://localhost:1234x"})
    );
}

#[test]
fn clear_empties_the_search_and_restores_sections() {
    let mut view = opened();
    view.paste("nothing-matches-this");
    let rows = screen(&mut view, 140, 45);
    assert!(rows[9].contains("No settings match"));
    key(&mut view, KeyCode::Tab); // Clear
    key(&mut view, KeyCode::Enter);
    let rows = screen(&mut view, 140, 45);
    assert!(
        rows[8..11]
            .join("\n")
            .contains("Search labels, help, scopes, and"),
        "{rows:?}"
    );
    assert!(rows.iter().any(|r| r.contains("Default model")));
}

#[test]
fn keys_pressed_while_the_form_loads_apply_once_it_arrives() {
    let mut view = View::default();
    view.open();
    view.take_intent();
    // Tab, Tab, Right before the form exists: as in Textual, they act once it is there.
    key(&mut view, KeyCode::Tab);
    key(&mut view, KeyCode::Tab);
    key(&mut view, KeyCode::Right);
    view.observe(Update::Form(Box::new(form())));
    let rows = screen(&mut view, 140, 45);
    assert!(
        rows.iter().any(|r| r.contains("Strata settings")),
        "{rows:?}"
    );
}

#[test]
fn restore_defaults_asks_like_python_then_sends_the_restore() {
    let mut view = opened();
    // Shift+Tab from the search box wraps to the last bottom button, "Restore defaults".
    view.key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT));
    key(&mut view, KeyCode::Enter);
    let rows = screen(&mut view, 140, 45);
    assert!(
        rows.iter()
            .any(|r| r.contains("Restore all settings to defaults?")),
        "{rows:?}"
    );
    assert!(rows.iter().any(|r| r.contains("Keep editing")));
    key(&mut view, KeyCode::Right); // Keep editing
    key(&mut view, KeyCode::Enter);
    assert_eq!(view.take_intent(), None);
    key(&mut view, KeyCode::Enter); // ask again
    key(&mut view, KeyCode::Enter); // Restore defaults
    assert_eq!(
        view.take_intent(),
        Some(Intent::Restore {
            form: "a".repeat(64)
        })
    );
    assert!(view.waiting);
}
