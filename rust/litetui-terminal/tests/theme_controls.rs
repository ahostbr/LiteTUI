use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::theme_controls::{
    self as themes, Change, Identity, Intent, Outcome, Palette, Review, Snapshot, Update, View,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
fn identity() -> Identity {
    Identity {
        session: SESSION.into(),
        agent: AGENT.into(),
        backend: "custom".into(),
        model: "test".into(),
    }
}
fn token_rows() -> Value {
    json!(themes::TOKENS.iter().enumerate().map(|(i,k)|json!({"key":k,"required":i<10,"raw":if i<13 {json!("#123456")}else{Value::Null},"resolved":if i<13{json!("#123456")}else{Value::Null},"source":if i<13{"explicit"}else{"omitted"}})).collect::<Vec<_>>())
}
fn palette() -> Value {
    let mut v = json!({});
    for (i, k) in themes::COLORS.iter().enumerate() {
        v[*k] = json!([i, 30, 200]);
    }
    v
}
fn row(id: char, name: &str) -> Value {
    json!({"row_id":id.to_string().repeat(64),"name":name,"definition":id.to_string().repeat(64),"source":"custom","selectable":true,"reason":null,"palette":palette(),"tokens":token_rows()})
}
fn data() -> Value {
    json!({"version":1,"kind":"theme_snapshot","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":"c".repeat(64),"scope":"device","apply_timing":"immediate","revisions":{"global":"d".repeat(64),"conversation":"absent","agent":"e".repeat(64)},"selection":{"saved_name":"Old","saved_definition":"a".repeat(64),"requested_name":"Old","runtime_name":"Old","effective_name":"Old","effective_row_id":"a".repeat(64),"effective_reason":null,"effective_definition":"a".repeat(64)},"eligible":true,"reason":null,"registered_definitions":{"Old":"a".repeat(64),"New":"b".repeat(64)},"rows":[row('a',"Old"),row('b',"New")],"invalid":[]})
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&data(), &identity()).unwrap()
}
fn change() -> Change {
    Change::Select {
        row: "b".repeat(64),
    }
}
fn review_data() -> Value {
    let s = data();
    let r = &s["rows"][1];
    json!({"version":1,"kind":"theme_review","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":s["snapshot"],"revisions":s["revisions"],"scope":"device","apply_timing":"immediate","review":"f".repeat(64),"action_id":ACTION,"action":"select","target":{"row_id":r["row_id"],"name":r["name"],"definition":r["definition"],"palette":r["palette"],"tokens":r["tokens"]},"overwrite":{"custom":false,"registered":false,"previous_definition":null},"selection_before":s["selection"],"fields":["theme_name"]})
}
fn review() -> Review {
    Review::parse(&review_data(), &snapshot(), &change()).unwrap()
}
fn after(partial: bool) -> Value {
    let mut v = data();
    v["snapshot"] = json!("1".repeat(64));
    v["revisions"]["global"] = json!("2".repeat(64));
    v["selection"]["saved_name"] = json!("New");
    v["selection"]["saved_definition"] = json!("b".repeat(64));
    if !partial {
        for k in ["requested_name", "runtime_name", "effective_name"] {
            v["selection"][k] = json!("New");
        }
        v["selection"]["effective_row_id"] = json!("b".repeat(64));
        v["selection"]["effective_definition"] = json!("b".repeat(64));
    }
    v
}
fn receipt(partial: bool) -> Value {
    let s = after(partial);
    json!({"version":1,"kind":"theme_save","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":"c".repeat(64),"review":"f".repeat(64),"action_id":ACTION,"action":"select","target_name":"New","target_definition":"b".repeat(64),"status":if partial{"partial"}else{"saved"},"mutated":true,"close_required":false,"error":null,"persistence":[{"destination":"C:/owned/settings.json","scope":"device","saved":true,"error":null,"revision":s["revisions"]["global"],"fields":["theme_name"]}],"runtime":{"status":if partial{"failed"}else{"applied"},"registered":true,"selected":!partial,"refresh_requested":!partial,"error":if partial{json!("Controlled selection failure")}else{Value::Null}},"selection":s["selection"],"revisions":s["revisions"]})
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn draw(v: &mut View, w: u16, h: u16) -> String {
    let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    t.backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn view() -> View {
    let mut v = View::default();
    v.open();
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Snapshot(Box::new(snapshot())));
    v
}
fn reviewed() -> View {
    let mut v = view();
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Down));
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Validate { .. })));
    v.observe(Update::Review(Box::new(review())));
    v
}
fn read_all(v: &mut View, w: u16, h: u16) {
    for _ in 0..100 {
        if draw(v, w, h).contains("Enter: save once") {
            return;
        }
        v.key(key(KeyCode::PageDown));
    }
    panic!("bounded review did not finish");
}
#[test]
fn source_order_effective_definition_and_all_palette_channels_are_retained() {
    let s = snapshot();
    assert_eq!(
        s.rows.iter().map(|r| r.name.as_str()).collect::<Vec<_>>(),
        ["Old", "New"]
    );
    assert_eq!(s.effective().unwrap().name, "Old");
    let t = s
        .effective()
        .unwrap()
        .palette
        .theme(&s.effective().unwrap().name);
    assert_eq!(t.background, ratatui::style::Color::Rgb(0, 30, 200));
    assert_eq!(t.footer_background, ratatui::style::Color::Rgb(13, 30, 200));
    let mut bright = palette();
    bright["background"] = json!([255, 255, 255]);
    assert!(
        Palette::parse(&bright).is_ok(),
        "source-defined dark flag does not impose a native luminance rule"
    );
}
#[test]
fn catalog_rejects_foreign_duplicate_malformed_or_oversized_authority() {
    for fault in [
        "identity",
        "version",
        "snapshot",
        "revision",
        "duplicate",
        "missing-color",
        "rgb",
        "float",
        "token-order",
        "token-required",
        "token-source",
        "token-raw",
        "effective",
        "eligibility",
        "row-budget",
        "bytes",
    ] {
        let mut v = data();
        match fault {
            "identity" => v["session_id"] = json!(AGENT),
            "version" => v["version"] = json!(2),
            "snapshot" => v["snapshot"] = json!("bad"),
            "revision" => v["revisions"]["global"] = json!(true),
            "duplicate" => v["rows"][1] = v["rows"][0].clone(),
            "missing-color" => {
                v["rows"][0]["palette"]
                    .as_object_mut()
                    .unwrap()
                    .remove("error");
            }
            "rgb" => v["rows"][0]["palette"]["error"] = json!([0, 256, 0]),
            "float" => v["rows"][0]["palette"]["error"] = json!([0, 1.0, 0]),
            "token-order" => v["rows"][0]["tokens"][0]["key"] = json!("error"),
            "token-required" => v["rows"][0]["tokens"][0]["required"] = json!(false),
            "token-source" => v["rows"][0]["tokens"][0]["source"] = json!("omitted"),
            "token-raw" => v["rows"][0]["tokens"][0]["raw"] = json!("x".repeat(257)),
            "effective" => v["selection"]["effective_definition"] = json!("f".repeat(64)),
            "eligibility" => v["eligible"] = json!(false),
            "row-budget" => v["rows"] = json!(vec![row('a', "Old"); 257]),
            "bytes" => v["extra"] = json!("x".repeat(themes::MAX_BYTES)),
            _ => unreachable!(),
        }
        assert!(Snapshot::parse(&v, &identity()).is_err(), "{fault}");
    }
}
#[test]
fn optional_invalid_raw_color_is_source_fallback_not_native_rejection() {
    let mut v = data();
    v["rows"][0]["tokens"][10]["raw"] = json!(" not a color ");
    v["rows"][0]["tokens"][10]["source"] = json!("derived");
    let s = Snapshot::parse(&v, &identity()).unwrap();
    assert_eq!(s.rows[0].tokens[10].raw.as_deref(), Some(" not a color "));
    let mut blocked = data();
    blocked["rows"][1]["selectable"] = json!(false);
    blocked["rows"][1]["reason"] = json!("Literal name resolves elsewhere");
    assert!(
        Snapshot::parse(&blocked, &identity())
            .unwrap()
            .request(&change())
            .is_err()
    );
}
#[test]
fn exact_review_and_saved_or_partial_independent_readback_are_required() {
    let r = review();
    let request = r.request();
    assert!(
        request.get("name").is_none() && request.get("tokens").is_none(),
        "save owns immutable source review"
    );
    for fault in [
        "action",
        "snapshot",
        "definition",
        "palette",
        "before",
        "fields",
        "overwrite",
    ] {
        let mut v = review_data();
        match fault {
            "action" => v["action"] = json!("custom"),
            "snapshot" => v["snapshot"] = json!("a".repeat(64)),
            "definition" => v["target"]["definition"] = json!("a".repeat(64)),
            "palette" => v["target"]["palette"]["error"] = json!([1, 2, 3]),
            "before" => v["selection_before"]["saved_name"] = json!("changed"),
            "fields" => v["fields"] = json!(["custom_themes", "theme_name"]),
            "overwrite" => v["overwrite"]["registered"] = json!(true),
            _ => unreachable!(),
        }
        assert!(
            Review::parse(&v, &snapshot(), &change()).is_err(),
            "{fault}"
        );
    }
    for partial in [false, true] {
        let o = Outcome::parse(&receipt(partial), &r).unwrap().unwrap();
        o.verify(&Snapshot::parse(&after(partial), &identity()).unwrap(), &r)
            .unwrap();
        for fault in ["revision", "selection", "palette"] {
            let mut v = after(partial);
            match fault {
                "revision" => v["revisions"]["agent"] = json!("a".repeat(64)),
                "selection" => v["selection"]["requested_name"] = json!("Other"),
                "palette" => {
                    if partial {
                        continue;
                    }
                    v["rows"][1]["palette"]["error"] = json!([1, 2, 3]);
                }
                _ => unreachable!(),
            }
            assert!(
                o.verify(&Snapshot::parse(&v, &identity()).unwrap(), &r)
                    .is_err(),
                "{partial}/{fault}"
            );
        }
    }
}
#[test]
fn no_op_save_is_valid_but_contradictory_success_or_unknown_mutation_is_not() {
    let r = review();
    let mut v = receipt(false);
    v["mutated"] = json!(false);
    assert!(Outcome::parse(&v, &r).is_err());
    for fault in [
        "action",
        "scope",
        "revision",
        "registered",
        "selected",
        "refresh",
        "close",
        "unknown",
    ] {
        let mut v = receipt(false);
        match fault {
            "action" => v["action_id"] = json!(SESSION),
            "scope" => v["persistence"][0]["scope"] = json!("conversation"),
            "revision" => v["revisions"]["conversation"] = json!("b".repeat(64)),
            "registered" => v["runtime"]["registered"] = json!(false),
            "selected" => v["runtime"]["selected"] = json!(false),
            "refresh" => v["runtime"]["refresh_requested"] = json!(false),
            "close" => v["close_required"] = json!(true),
            "unknown" => v["runtime"]["status"] = json!("unknown"),
            _ => unreachable!(),
        }
        assert!(Outcome::parse(&v, &r).is_err(), "{fault}");
    }
}
#[test]
fn full_review_requires_draw_every_page_and_fresh_enter() {
    let mut v = reviewed();
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    read_all(&mut v, 42, 18);
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    v.key(repeat);
    assert!(v.take_intent().is_none());
    read_all(&mut v, 42, 18);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Save { .. })));
}
#[test]
fn resize_focus_and_pointer_disarm_the_complete_review() {
    let mut v = reviewed();
    read_all(&mut v, 80, 28);
    draw(&mut v, 79, 27);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    read_all(&mut v, 79, 27);
    v.hide();
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    read_all(&mut v, 79, 27);
    v.mouse(MouseEvent {
        kind: MouseEventKind::Down(MouseButton::Left),
        column: 10,
        row: 10,
        modifiers: KeyModifiers::NONE,
    });
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
}
#[test]
fn cancelled_late_review_never_saves_and_dispatched_save_still_settles() {
    let mut v = view();
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Down));
    v.key(key(KeyCode::Enter));
    v.take_intent();
    v.key(key(KeyCode::Esc));
    v.observe(Update::Review(Box::new(review())));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(!v.owns_input());
    assert!(v.take_intent().is_none());
    let mut v = reviewed();
    read_all(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    v.take_intent();
    v.key(key(KeyCode::Esc));
    assert!(
        v.owns_input(),
        "dispatched save settlement is still pending"
    );
    v.observe(Update::Applied {
        snapshot: Box::new(Snapshot::parse(&after(false), &identity()).unwrap()),
        outcome: Box::new(Outcome::parse(&receipt(false), &review()).unwrap().unwrap()),
    });
    assert!(!v.is_open());
    assert!(!v.owns_input());
    assert_eq!(v.snapshot.unwrap().selection.effective, "New");
}
#[test]
fn custom_editor_preserves_exact_shift_paste_and_keeps_stale_draft() {
    let mut v = view();
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::F(2)));
    draw(&mut v, 42, 18);
    v.key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
    v.paste("雪 ");
    assert_eq!(v.draft_name(), "A雪 ");
    v.key(KeyEvent::new(KeyCode::Char('X'), KeyModifiers::CONTROL));
    draw(&mut v, 42, 18);
    assert_eq!(v.draft_name(), "A雪 ");
    v.key(key(KeyCode::Enter));
    assert!(matches!(
        v.take_intent(),
        Some(Intent::Validate {
            change: Change::Custom { .. },
            ..
        })
    ));
    v.observe(Update::Rejected("Stale revision".into()));
    assert_eq!(v.draft_name(), "A雪 ");
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::F(5)));
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Snapshot(Box::new(snapshot())));
    assert_eq!(v.draft_name(), "A雪 ");
}
#[test]
fn names_diagnostics_and_review_are_terminal_escaped() {
    let mut v = data();
    v["rows"][1]["name"] = json!("New\u{202e}X");
    v["registered_definitions"]
        .as_object_mut()
        .unwrap()
        .remove("New");
    v["registered_definitions"]["New\u{202e}X"] = json!("b".repeat(64));
    v["invalid"] = json!([{"name":"bad\u{1b}[2J","reason":"unsafe\u{2066}text"}]);
    let s = Snapshot::parse(&v, &identity()).unwrap();
    let mut ui = View::default();
    ui.open();
    ui.take_intent();
    ui.observe(Update::Snapshot(Box::new(s)));
    let screen = draw(&mut ui, 80, 28);
    assert!(!screen.contains('\u{202e}'));
    assert!(screen.contains("\\u{202e}") || screen.contains("\\u202e"));
    ui.key(key(KeyCode::F(7)));
    draw(&mut ui, 80, 28);
    ui.key(key(KeyCode::Enter));
    assert!(!draw(&mut ui, 80, 28).contains('\u{1b}'));
}
#[test]
fn compact_modal_remains_visible_and_editor_cursor_stays_inside_geometry() {
    let mut v = view();
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::F(2)));
    for (w, h) in [(36, 14), (41, 18), (42, 17)] {
        assert!(draw(&mut v, w, h).contains("Esc"));
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none());
    }
    let mut terminal = Terminal::new(TestBackend::new(42, 18)).unwrap();
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    v.paste(&"雪".repeat(80));
    terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    let cursor = ratatui::backend::Backend::get_cursor_position(terminal.backend_mut()).unwrap();
    assert!(cursor.x < 42 && cursor.y < 18);
}

fn state() -> Value {
    json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict",
        "model":{"backend":"custom","model":"test"},"ownership":{"writable":false,"data_version":1,"agent_id":AGENT},
        "schedules_paused":true,"restored_input":{"policy":"hold","held":0},"conversations":[],"jobs":[],"tasks":[],
        "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],
        "tool_policy":{"unchanged":"source policy"}})
}
fn response(
    m: &mut litetui_terminal::management::Management,
    command: &Value,
    result: Value,
) -> std::io::Result<litetui_terminal::management::Step> {
    m.accept(json!({"type":"response","id":command["id"],"ok":true,"result":result}))
}
fn write(step: litetui_terminal::management::Step) -> Value {
    if let litetui_terminal::management::Step::Write(v) = step {
        v
    } else {
        panic!("Expected source read/write")
    }
}
fn management() -> litetui_terminal::management::Management {
    use litetui_terminal::management::{Management, Step};
    let mut m = Management::default();
    let ready = json!({"type":"ready","management_protocols":[1],"capabilities":["hello","state"],
        "conversation_id":SESSION,"backend":"custom","model":"test","tool_profile":"strict"});
    assert!(matches!(
        m.accept(ready.clone()).unwrap(),
        Step::Bootstrap(_)
    ));
    let hello = m.begin(&ready).unwrap();
    let c = write(
        response(
            &mut m,
            &hello,
            json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],
        "operations":["gui.hello","gui.state",themes::SNAPSHOT,themes::VALIDATE,themes::SAVE]}),
        )
        .unwrap(),
    );
    let c = write(response(&mut m, &c, state()).unwrap());
    assert_eq!(c["type"], themes::SNAPSHOT);
    assert!(!m.ready(), "startup palette read owns operation barrier");
    let c = write(response(&mut m, &c, data()).unwrap());
    assert_eq!(c["type"], "gui.state");
    assert_eq!(c["include_conversations"], false);
    let Step::Theme {
        update: Update::Snapshot(_),
        policy: Some(_),
        startup: Some(_),
    } = response(&mut m, &c, state()).unwrap()
    else {
        panic!("Startup palette must be independently read before Ready")
    };
    assert!(m.ready());
    m
}
fn begin_save(m: &mut litetui_terminal::management::Management) -> Value {
    use litetui_terminal::management::Step;
    let c = m
        .request_theme(Intent::Validate {
            snapshot: snapshot().token,
            change: change(),
        })
        .unwrap();
    assert!(matches!(
        response(m, &c, review_data()).unwrap(),
        Step::Theme {
            update: Update::Review(_),
            ..
        }
    ));
    m.request_theme(Intent::Save {
        review: review().token,
        action_id: ACTION.into(),
    })
    .unwrap()
}
#[test]
fn source_startup_and_save_are_barriered_by_independent_palette_and_state_reads() {
    use litetui_terminal::management::Step;
    let mut m = management();
    let command = begin_save(&mut m);
    assert_eq!(command["type"], themes::SAVE);
    let c = write(response(&mut m, &command, receipt(false)).unwrap());
    assert_eq!(c["type"], themes::SNAPSHOT);
    assert!(!m.ready());
    let c = write(response(&mut m, &c, after(false)).unwrap());
    assert_eq!(c["type"], "gui.state");
    assert!(matches!(
        response(&mut m, &c, state()).unwrap(),
        Step::Theme {
            update: Update::Applied { .. },
            ..
        }
    ));
    assert!(m.ready());
    assert!(
        m.request_theme(Intent::Save {
            review: review().token,
            action_id: ACTION.into()
        })
        .is_err(),
        "review consumed once"
    );
}
#[test]
fn presentation_save_cannot_change_mode_holds_ownership_or_tool_policy() {
    for fault in ["mode", "held", "agent", "policy", "model", "busy"] {
        let mut m = management();
        let command = begin_save(&mut m);
        let c = write(response(&mut m, &command, receipt(false)).unwrap());
        let c = write(response(&mut m, &c, after(false)).unwrap());
        let mut v = state();
        match fault {
            "mode" => v["mode"] = json!("plan"),
            "held" => v["restored_input"]["held"] = json!(1),
            "agent" => v["ownership"]["agent_id"] = json!(SESSION),
            "policy" => v["tool_policy"] = json!({}),
            "model" => v["model"]["model"] = json!("other"),
            "busy" => v["busy"] = json!(true),
            _ => unreachable!(),
        }
        assert!(response(&mut m, &c, v).is_err(), "{fault}");
    }
}
#[test]
fn generic_save_error_is_not_a_safe_refusal_and_closing_registers_no_followups() {
    use litetui_terminal::management::Step;
    let mut m = management();
    let c = begin_save(&mut m);
    assert!(
        m.accept(
            json!({"type":"response","id":c["id"],"ok":false,"error":"unknown write outcome"})
        )
        .is_err()
    );
    let mut m = management();
    let c = begin_save(&mut m);
    m.begin_close();
    assert!(matches!(
        response(&mut m, &c, receipt(false)).unwrap(),
        Step::Ignore
    ));
}
#[test]
fn exact_failed_write_can_be_known_partial_without_claiming_runtime_application() {
    let mut v = receipt(true);
    v["persistence"][0]["saved"] = json!(false);
    v["persistence"][0]["error"] = json!("write returned error after exact durable bytes");
    v["runtime"] = json!({"status":"unchanged","registered":null,"selected":null,"refresh_requested":false,"error":null});
    v["error"] = json!("Persistence reported failure; independent saved readback differs");
    let o = Outcome::parse(&v, &review()).unwrap().unwrap();
    assert!(!o.persistence_saved);
    assert_eq!(o.runtime, "unchanged");
    o.verify(
        &Snapshot::parse(&after(true), &identity()).unwrap(),
        &review(),
    )
    .unwrap();
    v["runtime"]["selected"] = json!(true);
    assert!(Outcome::parse(&v, &review()).is_err());
}
#[test]
fn full_source_catalog_scrolls_without_truncation_and_mouse_selection_requires_a_pair() {
    let mut data = data();
    data["rows"] = json!(
        (0..256)
            .map(|i| {
                let mut r = row('a', &format!("Theme-{i:03}"));
                r["row_id"] = json!(format!("{i:064x}"));
                r["definition"] = json!(format!("{i:064x}"));
                r
            })
            .collect::<Vec<_>>()
    );
    data["registered_definitions"] = json!(
        (0..256)
            .map(|i| (format!("Theme-{i:03}"), format!("{i:064x}")))
            .collect::<std::collections::BTreeMap<_, _>>()
    );
    data["selection"]["effective_name"] = json!("Theme-000");
    data["selection"]["effective_row_id"] = json!("0".repeat(64));
    data["selection"]["effective_definition"] = json!("0".repeat(64));
    let mut v = View::default();
    v.open();
    v.take_intent();
    v.observe(Update::Snapshot(Box::new(
        Snapshot::parse(&data, &identity()).unwrap(),
    )));
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::End));
    assert!(draw(&mut v, 42, 18).contains("Theme-255"));
    let mut v = view();
    draw(&mut v, 42, 18);
    let mouse = |kind, column, row| MouseEvent {
        kind,
        column,
        row,
        modifiers: KeyModifiers::NONE,
    };
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 4, 5));
    v.key(key(KeyCode::Enter));
    assert!(
        matches!(v.take_intent(),Some(Intent::Validate{change:Change::Select{row},..}) if row=="a".repeat(64))
    );
    let mut v = view();
    draw(&mut v, 42, 18);
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 4, 5));
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 4, 5));
    v.key(key(KeyCode::Enter));
    assert!(
        matches!(v.take_intent(),Some(Intent::Validate{change:Change::Select{row},..}) if row=="b".repeat(64))
    );
}
#[test]
fn source_palette_reaches_live_renderer_without_touching_composer_or_history() {
    let mut app = litetui_terminal::demo::Demo::live("custom".into(), "test".into());
    app.insert_text("PRESERVE DRAFT");
    app.observe_live(litetui_terminal::live::Observation::Theme(
        Update::Snapshot(Box::new(snapshot())),
    ))
    .unwrap();
    assert_eq!(
        app.palette().background,
        ratatui::style::Color::Rgb(0, 30, 200)
    );
    assert_eq!(app.composer.text(), "PRESERVE DRAFT");
    assert!(app.live.as_ref().unwrap().rows.is_empty());
    app.live.as_mut().unwrap().ready = true;
    app.open_themes();
    assert!(app.has_live_themes());
    assert!(!app.themes_open, "offline picker remains separate");
}

#[test]
fn unsupported_current_palette_keeps_a_labelled_fallback_and_supported_recovery() {
    let mut d = data();
    d["selection"]["effective_row_id"] = Value::Null;
    d["selection"]["effective_reason"] = json!("Alpha is not representable");
    d["selection"]["effective_name"] = json!("Alpha current");
    d["selection"]["effective_definition"] = json!("3".repeat(64));
    d["registered_definitions"]["Alpha current"] = json!("3".repeat(64));
    let s = Snapshot::parse(&d, &identity()).unwrap();
    assert!(s.effective().is_none());
    assert!(s.request(&change()).is_ok());
    let mut app = litetui_terminal::demo::Demo::live("custom".into(), "test".into());
    app.observe_live(litetui_terminal::live::Observation::Theme(
        Update::Snapshot(Box::new(s)),
    ))
    .unwrap();
    assert!(app.live.as_ref().unwrap().effective_theme.is_none());
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .theme_status
            .contains("local fallback")
    );
    d["selection"]["effective_reason"] = Value::Null;
    assert!(Snapshot::parse(&d, &identity()).is_err());
}
#[test]
fn null_failed_destination_revision_is_canonical_but_partial_palette_drift_is_not() {
    let mut v = receipt(true);
    v["persistence"][0]["saved"] = json!(false);
    v["persistence"][0]["revision"] = Value::Null;
    v["persistence"][0]["error"] = json!("write error");
    v["error"] = json!("exact bytes changed despite error");
    v["runtime"] = json!({"status":"unchanged","registered":null,"selected":null,"refresh_requested":false,"error":null});
    let o = Outcome::parse(&v, &review()).unwrap().unwrap();
    let mut changed = after(true);
    changed["rows"][0]["palette"]["primary"] = json!([250, 1, 2]);
    assert!(
        o.verify(&Snapshot::parse(&changed, &identity()).unwrap(), &review())
            .is_err()
    );
    v["persistence"][0]["saved"] = json!(true);
    assert!(Outcome::parse(&v, &review()).is_err());
}
#[test]
fn already_effective_exact_theme_can_save_without_refresh_but_other_selection_cannot() {
    let mut before = data();
    before["selection"] = after(false)["selection"].clone();
    before["revisions"] = data()["revisions"].clone();
    let s = Snapshot::parse(&before, &identity()).unwrap();
    let mut rv = review_data();
    rv["selection_before"] = before["selection"].clone();
    let r = Review::parse(&rv, &s, &change()).unwrap();
    let mut v = receipt(false);
    v["runtime"]["refresh_requested"] = json!(false);
    assert!(Outcome::parse(&v, &r).unwrap().is_ok());
    assert!(Outcome::parse(&v, &review()).is_err());
}

#[test]
fn confirmation_focuses_on_changes_and_source_details_cannot_save() {
    let mut v = reviewed();
    let screen = draw(&mut v, 92, 34);
    assert!(screen.contains("Current: Old") && screen.contains("Target: New"));
    assert!(screen.contains("Palette preview") && screen.contains("apply immediately"));
    assert!(!screen.contains("Definition:") && !screen.contains("Global revision:"));
    assert!(!screen.contains("Source token values"));
    v.key(key(KeyCode::F(6)));
    let mut details = String::new();
    for _ in 0..10 {
        details.push_str(&draw(&mut v, 92, 34));
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none(), "details never arm save");
        // Enter disarms; draw before the next paging input.
        draw(&mut v, 92, 34);
        v.key(key(KeyCode::PageDown));
    }
    // Reopen details at the first page after the intentional Enter disarms above.
    v.key(key(KeyCode::Esc));
    draw(&mut v, 92, 34);
    v.key(key(KeyCode::F(6)));
    for _ in 0..10 {
        details.push_str(&draw(&mut v, 92, 34));
        v.key(key(KeyCode::PageDown));
    }
    assert!(details.contains("Definition:") && details.contains("Global revision:"));
    v.key(key(KeyCode::Esc));
    assert!(
        v.review.is_some(),
        "leaving details keeps the reviewed change"
    );
    read_all(&mut v, 92, 34);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Save { .. })));
}
#[test]
fn custom_repair_binds_an_unsupported_registered_definition_without_inventing_colors() {
    let mut before = data();
    before["registered_definitions"]["Alpha"] = json!("3".repeat(64));
    before["invalid"] = json!([{"name":"Alpha","reason":"Unsupported alpha"}]);
    let s = Snapshot::parse(&before, &identity()).unwrap();
    let change = Change::Custom {
        name: " Alpha ".into(),
        tokens: themes::TOKENS
            .iter()
            .map(|k| ((*k).into(), "#123456".into()))
            .collect(),
    };
    let mut raw = review_data();
    raw["action"] = json!("custom");
    raw["target"]["row_id"] = Value::Null;
    raw["target"]["name"] = json!("Alpha");
    raw["fields"] = json!(["custom_themes", "theme_name"]);
    raw["overwrite"] =
        json!({"custom":false,"registered":true,"previous_definition":"3".repeat(64)});
    let r = Review::parse(&raw, &s, &change).unwrap();
    let mut view = View::default();
    view.open();
    view.take_intent();
    view.observe(Update::Review(Box::new(r)));
    let mut screens = String::new();
    for _ in 0..20 {
        screens.push_str(&draw(&mut view, 60, 22));
        view.key(key(KeyCode::PageDown));
    }
    assert!(screens.contains("Overwrite this theme"));
    assert!(screens.contains("Entered name:  Alpha "));
    assert!(screens.contains("footer-background"));
    assert!(!screens.contains("Previous definition:"));
    raw["overwrite"]["previous_definition"] = json!("4".repeat(64));
    assert!(Review::parse(&raw, &s, &change).is_err());
}
#[test]
fn exactly_unchanged_saved_outcome_may_report_no_mutation() {
    let mut before = data();
    before["selection"] = after(false)["selection"].clone();
    let s = Snapshot::parse(&before, &identity()).unwrap();
    let mut rv = review_data();
    rv["selection_before"] = before["selection"].clone();
    let r = Review::parse(&rv, &s, &change()).unwrap();
    let mut result = receipt(false);
    result["mutated"] = json!(false);
    result["runtime"]["refresh_requested"] = json!(false);
    result["revisions"] = before["revisions"].clone();
    result["persistence"][0]["revision"] = before["revisions"]["global"].clone();
    let o = Outcome::parse(&result, &r).unwrap().unwrap();
    o.verify(&s, &r).unwrap();
    result["revisions"]["global"] = json!("1".repeat(64));
    assert!(Outcome::parse(&result, &r).is_err());
}
