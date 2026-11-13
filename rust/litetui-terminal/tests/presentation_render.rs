//! Each device presentation preference changes the frame the compiled Ratatui renderer paints.
//! Preferences arrive the way production delivers them: a verified F10 snapshot update.
use litetui_terminal::{
    approvals::Ledger,
    demo::Demo,
    live::Observation,
    presentation::{FIELDS, FOOTER_ORDER_DEFAULT},
    presentation_preferences::{Reply, Request, Update},
    runtime_state::Update as State,
    settings_view::Update as SettingsUpdate,
    ui,
    work_protocol::Identity,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

fn rows(app: &mut Demo, width: u16, height: u16) -> Vec<String> {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(usize::from(width))
        .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect()
}
fn frame(app: &mut Demo, width: u16, height: u16) -> String {
    rows(app, width, height).join("\n")
}
/// Column of the first cell of `needle`, scanning rows top to bottom.
fn find(rows: &[String], needle: &str) -> Option<(usize, usize)> {
    rows.iter().enumerate().find_map(|(y, row)| {
        row.find(needle)
            .map(|byte| (row[..byte].chars().count(), y))
    })
}

fn identity() -> Identity {
    Identity {
        session: "s".into(),
        agent: "a".into(),
        backend: "claude".into(),
        model: "zz-model".into(),
    }
}
fn default_value(key: &str) -> Value {
    match key {
        "error_message_style" => json!("plain"),
        "dialog_style" => json!("modal"),
        "dialog_side" => json!("right"),
        "footer_order" => json!(FOOTER_ORDER_DEFAULT),
        "show_stop_time" | "footer_task_manager" | "footer_show_key_hints" => json!(false),
        _ => json!(true),
    }
}
/// A source snapshot as the runtime reports it after the user saved `changes`.
fn preferences(changes: Value) -> SettingsUpdate {
    let id = identity();
    let mut root = id.request();
    for (key, value) in [
        ("kind", json!("presentation_preferences")),
        ("scope", json!("device")),
        ("work_instance", json!("instance")),
        ("revision", json!("c".repeat(64))),
    ] {
        root[key] = value;
    }
    for key in FIELDS {
        let value = changes
            .get(key)
            .cloned()
            .unwrap_or_else(|| default_value(key));
        root["fields"][key] = json!({
            "effective": value, "saved": value,
            "editable": key != "footer_task_manager", "overridden": false,
        });
    }
    match Request::List
        .response(&root, &id, "instance", "list")
        .unwrap()
    {
        Reply::Update(update @ Update::List(_)) => {
            SettingsUpdate::Presentation(Ok(Box::new(update)))
        }
        _ => panic!("list reply"),
    }
}
fn apply(app: &mut Demo, changes: Value) {
    app.observe_live(Observation::Settings(preferences(changes)))
        .unwrap();
}

fn live() -> Demo {
    let mut app = Demo::live("claude".into(), "zz-model".into());
    app.observe_live(Observation::Ready).unwrap();
    app
}
fn runtime_facts(app: &mut Demo, tps: Option<f64>) {
    let ready = json!({"type":"ready","model":"zz-model","conversation_id":"conv1234abcd",
        "thinking_level":"high","context_length":8000});
    let usage = json!({"type":"usage","usage":{"context_tokens":1000,"max_context_tokens":8000}});
    for event in [ready, usage] {
        app.observe_live(Observation::State(
            State::from_event(&event).unwrap().unwrap(),
        ))
        .unwrap();
    }
    if let Some(tps) = tps {
        let end = json!({"type":"turn_end","stopReason":"stop","tps":tps});
        app.observe_live(Observation::State(
            State::from_event(&end).unwrap().unwrap(),
        ))
        .unwrap();
    }
}

#[test]
fn show_thinking_hides_the_reasoning_block_but_not_the_answer() {
    let mut app = live();
    app.live.as_mut().unwrap().submit("question").unwrap();
    app.observe_live(Observation::Reasoning("weighing options carefully".into()))
        .unwrap();
    app.observe_live(Observation::Text("the final answer".into()))
        .unwrap();
    let shown = frame(&mut app, 110, 50);
    assert!(
        shown.contains("Thinking") && shown.contains("weighing options carefully"),
        "{shown}"
    );
    assert!(shown.contains("the final answer"));
    apply(&mut app, json!({"show_thinking": false}));
    let hidden = frame(&mut app, 110, 50);
    assert!(
        !hidden.contains("Thinking") && !hidden.contains("weighing options"),
        "{hidden}"
    );
    assert!(hidden.contains("the final answer"));
    apply(&mut app, json!({}));
    assert!(frame(&mut app, 110, 50).contains("weighing options carefully"));
}

fn finished(app: &mut Demo, tps: Option<f64>, how: Observation) {
    app.live.as_mut().unwrap().submit("question").unwrap();
    app.observe_live(Observation::Text("done answer".into()))
        .unwrap();
    runtime_facts(app, tps);
    app.observe_live(how).unwrap();
}

#[test]
fn stop_line_follows_its_switches_for_completed_and_stopped_turns() {
    let mut app = live();
    finished(&mut app, Some(38.24), Observation::Complete);
    let shown = frame(&mut app, 110, 50);
    // Python's card ends with its stop line only, no state words.
    assert!(!shown.contains("Completed · stop"), "{shown}");
    let line = shown
        .lines()
        .find(|line| line.contains("Cooked for"))
        .unwrap_or_else(|| panic!("{shown}"));
    assert!(
        line.contains("s · 38.2 tok/s") && !line.contains("done "),
        "{line}"
    );
    apply(&mut app, json!({"show_stop_time": true}));
    let timed = frame(&mut app, 110, 50);
    if cfg!(windows) {
        let line = timed.lines().find(|l| l.contains("Cooked for")).unwrap();
        assert!(line.contains(" · done ") && (line.contains(" AM") || line.contains(" PM")));
    }
    apply(
        &mut app,
        json!({"show_stop_line": false, "show_stop_time": true}),
    );
    let hidden = frame(&mut app, 110, 50);
    assert!(!hidden.contains("Cooked for") && !hidden.contains("Completed · stop"));

    let mut stopped = live();
    finished(&mut stopped, None, Observation::Cancelled("by user".into()));
    let shown = frame(&mut stopped, 110, 50);
    assert!(
        shown.contains("stopped after") && !shown.contains("Cooked for"),
        "{shown}"
    );
}

#[test]
fn footer_order_and_every_switch_change_the_painted_footer() {
    let mut app = live();
    runtime_facts(&mut app, Some(12.5));
    let all = rows(&mut app, 160, 40);
    let footer = all[all.len() - 2].clone();
    let fields = [
        ("pct", "12%", "footer_show_context_pct"),
        ("ctx", "ctx 1,000 / 8,000", "footer_show_context"),
        ("convo", "conv1234", "footer_show_convo"),
        ("think", "think:high", "footer_show_thinking"),
        ("tps", "12.5 tok/s", "footer_show_tps"),
    ];
    let at = |footer: &str, needle: &str| footer.find(needle);
    for (_, text, _) in fields {
        assert!(at(&footer, text).is_some(), "{text} missing: {footer}");
    }
    assert!(at(&footer, "12%") < at(&footer, "ctx 1,000"));
    assert!(at(&footer, "ctx 1,000") < at(&footer, "conv1234"));
    assert!(at(&footer, "conv1234") < at(&footer, "zz-model"));
    assert!(at(&footer, "zz-model") < at(&footer, "think:high"));
    assert!(at(&footer, "think:high") < at(&footer, "12.5 tok/s"));

    for (_, text, switch) in fields {
        apply(&mut app, json!({ switch: false }));
        let all = rows(&mut app, 160, 40);
        let hidden = &all[all.len() - 2];
        assert!(at(hidden, text).is_none(), "{switch} left {text}: {hidden}");
        for (_, other, _) in fields.iter().filter(|(_, other, _)| *other != text) {
            assert!(
                at(hidden, other).is_some(),
                "{switch} hid {other}: {hidden}"
            );
        }
    }

    let mut order: Vec<Value> = FOOTER_ORDER_DEFAULT.iter().map(|id| json!(id)).collect();
    let tps = order.remove(7);
    order.insert(0, tps);
    let convo = order.remove(4);
    order.push(convo);
    apply(&mut app, json!({ "footer_order": order }));
    let all = rows(&mut app, 160, 40);
    let moved = &all[all.len() - 2];
    assert!(at(moved, "12.5 tok/s") < at(moved, "12%"), "{moved}");
    assert!(at(moved, "12%") < at(moved, "ctx 1,000"));
    assert!(at(moved, "ctx 1,000") < at(moved, "zz-model"));
    assert!(at(moved, "think:high") < at(moved, "conv1234"), "{moved}");
}

#[test]
fn autoscroll_off_leaves_an_unlocked_reader_where_they_are() {
    for autoscroll in [true, false] {
        let mut app = live();
        app.live.as_mut().unwrap().submit("question").unwrap();
        app.observe_live(Observation::Text("line\n".repeat(200)))
            .unwrap();
        apply(&mut app, json!({ "autoscroll": autoscroll }));
        rows(&mut app, 100, 30);
        assert!(app.max_scroll > 50);
        app.scroll = app.max_scroll;
        app.observe_live(Observation::Text("more\n".repeat(20)))
            .unwrap();
        if autoscroll {
            assert_eq!(app.scroll, usize::MAX, "a locked reader follows the output");
        } else {
            assert_eq!(
                app.scroll, app.max_scroll,
                "an unlocked reader is not moved"
            );
        }
    }
}

#[test]
fn sidebar_dialogs_dock_to_the_chosen_edge_and_the_chat_reflows_beside_them() {
    let ask = |app: &mut Demo| {
        app.live
            .as_mut()
            .unwrap()
            .submit("keep this visible")
            .unwrap();
        let event = json!({"type":"tool_approval_requested","id":"dock","tool":"write",
            "input":{"path":"not-executed.txt"},"profile":"strict","why":"workspace write","timeout_s":300.0});
        app.observe_live(Observation::Approval(
            Ledger::default().request(&event).unwrap(),
        ))
        .unwrap();
    };
    let (width, height) = (140u16, 40u16);
    let mut modal = live();
    ask(&mut modal);
    let modal_rows = rows(&mut modal, width, height);
    let (modal_x, _) = find(&modal_rows, "Tool approval").unwrap();
    assert!(
        modal_x > 15 && modal_x < 80,
        "a modal is centered, not docked: {modal_x}"
    );

    let mut right = live();
    ask(&mut right);
    apply(&mut right, json!({"dialog_style": "sidebar"}));
    let right_rows = rows(&mut right, width, height);
    let (dock_x, _) = find(&right_rows, "Tool approval").unwrap();
    assert!(
        dock_x >= usize::from(width) * 2 / 3 - 4,
        "right dock at {dock_x}"
    );
    let (chat_x, _) = find(&right_rows, "keep this visible").unwrap();
    assert!(chat_x < dock_x, "the chat stays readable beside the dock");

    let mut left = live();
    ask(&mut left);
    apply(
        &mut left,
        json!({"dialog_style": "sidebar", "dialog_side": "left"}),
    );
    let left_rows = rows(&mut left, width, height);
    let (dock_x, _) = find(&left_rows, "Tool approval").unwrap();
    assert!(dock_x < 4, "left dock at {dock_x}");
    let (chat_x, _) = find(&left_rows, "keep this visible").unwrap();
    assert!(
        chat_x > 30,
        "the chat reflows to the right of the dock: {chat_x}"
    );

    // A side that changes only matters for sidebar dialogs.
    let mut modal_left = live();
    ask(&mut modal_left);
    apply(&mut modal_left, json!({"dialog_side": "left"}));
    assert_eq!(
        find(&rows(&mut modal_left, width, height), "Tool approval"),
        find(&modal_rows, "Tool approval")
    );
    // Too narrow for a dock, the dialog falls back to the modal placement.
    let mut narrow = live();
    ask(&mut narrow);
    apply(&mut narrow, json!({"dialog_style": "sidebar"}));
    assert!(frame(&mut narrow, 70, 30).contains("Tool approval"));
}
