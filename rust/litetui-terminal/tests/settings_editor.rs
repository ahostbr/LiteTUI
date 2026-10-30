use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use litetui_terminal::{
    management::{Management, Step},
    settings_editor::{self as edit, Intent, Outcome, Review, Snapshot, Update, View},
    settings_view,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
fn data() -> Value {
    json!({"version":1,"kind":"output_budget_snapshot","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":"a".repeat(64),"revisions":{"global":"a".repeat(64),"agent":"b".repeat(64),"conversation":"absent"},"fields":{
"max_tokens_chat":{"scope":"conversation","apply_timing":"next_turn","saved":4096,"effective":4096,"editable":true,"reason":null,"minimum":1,"maximum":2147483647},
"max_tokens_tools":{"scope":"conversation","apply_timing":"next_turn","saved":8192,"effective":8192,"editable":true,"reason":null,"minimum":1,"maximum":2147483647}}})
}
fn snapshot(v: &Value) -> Snapshot {
    Snapshot::parse(v, SESSION, AGENT, "custom", "test").unwrap()
}
fn review_data(s: &Snapshot, field: &str, value: u64) -> Value {
    let b = s.budget(field).unwrap();
    json!({"version":1,"kind":"output_budget_review","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":s.model,"snapshot":s.token,"revisions":s.revisions,"field":field,"value":value,"scope":"conversation","apply_timing":"next_turn","saved_before":b.saved,"effective_before":b.effective,"review":"c".repeat(64)})
}
fn review() -> Review {
    let s = snapshot(&data());
    Review::parse(
        &review_data(&s, "max_tokens_chat", 1234),
        &s,
        "max_tokens_chat",
        1234,
    )
    .unwrap()
}
fn receipt(partial: bool) -> Value {
    let r = review();
    json!({"version":1,"kind":"output_budget_save","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":r.snapshot.token,"review":r.token,"field":r.field,"value":r.value,"status":if partial{"partial"}else{"saved"},"mutated":true,"close_required":false,"persistence":[{"destination":format!("C:/owned/conversations/{SESSION}/settings.json"),"scope":"conversation","saved":true,"error":null,"revision":"d".repeat(64),"fields":[r.field]}],"runtime":[{"field":r.field,"scope":"conversation","status":if partial{"failed"}else{"applied"},"action":if partial{"retry"}else{"none"},"requested":1234,"effective":if partial{4096}else{1234},"reason":if partial{json!("controlled runtime failure")}else{Value::Null}}],"saved":1234,"effective":if partial{4096}else{1234},"revisions":{"global":"a".repeat(64),"agent":"b".repeat(64),"conversation":"d".repeat(64)},"error":null})
}
fn after(partial: bool) -> Value {
    let mut v = data();
    v["snapshot"] = json!("e".repeat(64));
    v["revisions"]["conversation"] = json!("d".repeat(64));
    v["fields"]["max_tokens_chat"]["saved"] = json!(1234);
    v["fields"]["max_tokens_chat"]["effective"] = json!(if partial { 4096 } else { 1234 });
    v
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
    v.open("max_tokens_chat");
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Snapshot(snapshot(&data())));
    v
}
fn draft(v: &mut View) {
    draw(v, 80, 28);
    v.key(key(KeyCode::Home));
    for _ in 0..4 {
        v.key(key(KeyCode::Delete));
    }
    v.paste("1234");
}
#[test]
fn snapshot_rejects_wrong_identity_bounds_scope_and_eligibility() {
    for fault in [
        "session",
        "agent",
        "backend",
        "model",
        "version",
        "token",
        "revision",
        "scope",
        "timing",
        "field",
        "extra",
        "min",
        "max",
        "zero",
        "bool",
        "negative",
        "over",
        "authority",
        "reason",
        "bytes",
    ] {
        let mut v = data();
        match fault {
            "session" => v["session_id"] = json!(AGENT),
            "agent" => v["agent_id"] = json!(SESSION),
            "backend" => v["backend"] = json!("claude"),
            "model" => v["model"] = json!("other"),
            "version" => v["version"] = json!(2),
            "token" => v["snapshot"] = json!("X".repeat(64)),
            "revision" => v["revisions"]["foreign"] = json!("absent"),
            "scope" => v["fields"]["max_tokens_chat"]["scope"] = json!("global"),
            "timing" => v["fields"]["max_tokens_chat"]["apply_timing"] = json!("reload"),
            "field" => {
                v["fields"]
                    .as_object_mut()
                    .unwrap()
                    .remove("max_tokens_tools");
            }
            "extra" => v["fields"]["temperature"] = json!({}),
            "min" => v["fields"]["max_tokens_chat"]["minimum"] = json!(0),
            "max" => v["fields"]["max_tokens_chat"]["maximum"] = json!(1),
            "zero" => v["fields"]["max_tokens_chat"]["saved"] = json!(0),
            "bool" => v["fields"]["max_tokens_chat"]["effective"] = json!(true),
            "negative" => v["fields"]["max_tokens_chat"]["effective"] = json!(-1),
            "over" => v["fields"]["max_tokens_chat"]["effective"] = json!(2147483648u64),
            "authority" => v["fields"]["max_tokens_chat"]["editable"] = json!(false),
            "reason" => v["fields"]["max_tokens_chat"]["reason"] = json!("locked"),
            "bytes" => v["extra"] = json!("x".repeat(256 * 1024)),
            _ => unreachable!(),
        }
        assert!(
            Snapshot::parse(&v, SESSION, AGENT, "custom", "test").is_err(),
            "{fault}"
        );
    }
}
#[test]
fn exact_review_and_receipt_are_checked_before_independent_readback() {
    let s = snapshot(&data());
    let r = review();
    for fault in [
        "field",
        "value",
        "snapshot",
        "saved_before",
        "effective_before",
        "scope",
        "revisions",
        "review",
    ] {
        let mut v = review_data(&s, "max_tokens_chat", 1234);
        match fault {
            "value" | "saved_before" | "effective_before" => v[fault] = json!(999),
            "revisions" => v[fault] = json!({}),
            _ => v[fault] = json!("wrong"),
        };
        assert!(
            Review::parse(&v, &s, "max_tokens_chat", 1234).is_err(),
            "{fault}"
        );
    }
    for partial in [false, true] {
        let o = Outcome::parse(&receipt(partial), &r).unwrap().unwrap();
        o.verify(&snapshot(&after(partial)), &r).unwrap();
        let mut wrong = after(partial);
        wrong["fields"]["max_tokens_tools"]["effective"] = json!(1000);
        assert!(o.verify(&snapshot(&wrong), &r).is_err());
    }
    for fault in [
        "mutated",
        "close_required",
        "status",
        "saved",
        "effective",
        "runtime",
        "destination",
        "revision",
        "field",
    ] {
        let mut v = receipt(false);
        match fault {
            "mutated" => v[fault] = json!(false),
            "close_required" => v[fault] = json!(true),
            "status" => v[fault] = json!("failed"),
            "saved" | "effective" => v[fault] = json!(999),
            "runtime" => v[fault][0]["status"] = json!("pending"),
            "destination" => v["persistence"][0][fault] = json!("global"),
            "revision" => v["persistence"][0][fault] = json!("a".repeat(64)),
            "field" => v[fault] = json!("max_tokens_tools"),
            _ => unreachable!(),
        };
        assert!(Outcome::parse(&v, &r).is_err(), "{fault}");
    }
}
#[test]
fn editor_requires_source_review_then_fresh_visible_confirmation() {
    let mut v = view();
    draft(&mut v);
    assert_eq!(v.draft(), "1234");
    v.key(key(KeyCode::Enter));
    assert!(matches!(
        v.take_intent(),
        Some(Intent::Validate { value: 1234, .. })
    ));
    v.observe(Update::Review(review()));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    let visible = draw(&mut v, 80, 28);
    assert!(visible.contains("Saved: 4096 → 1234"));
    assert!(visible.contains("Scope: conversation"));
    assert!(visible.contains("Applies: next_turn"));
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    v.key(repeat);
    assert!(v.take_intent().is_none());
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Save { .. })));
}
#[test]
fn refusal_retains_draft_and_requires_explicit_refresh_and_new_review() {
    let mut v = view();
    draft(&mut v);
    v.observe(Update::Rejected("stale".into()));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    assert_eq!(v.draft(), "1234");
    v.key(key(KeyCode::Char('r')));
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Snapshot(snapshot(&data())));
    assert_eq!(v.draft(), "1234");
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(matches!(
        v.take_intent(),
        Some(Intent::Validate { value: 1234, .. })
    ));
}
#[test]
fn readonly_and_invalid_numeric_input_never_validate() {
    let mut d = data();
    d["fields"]["max_tokens_chat"]["editable"] = json!(false);
    d["fields"]["max_tokens_chat"]["reason"] = json!("Launch --max-tokens owns this value");
    let mut v = view();
    v.observe(Update::Snapshot(snapshot(&d)));
    assert!(draw(&mut v, 80, 28).contains("--max-tokens"));
    v.paste("12");
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    assert_eq!(v.draft(), "4096");
    for value in ["0", "2147483648"] {
        let mut v = view();
        draw(&mut v, 80, 28);
        v.key(key(KeyCode::Home));
        for _ in 0..4 {
            v.key(key(KeyCode::Delete));
        }
        v.paste(value);
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none());
    }
    let mut v = view();
    draw(&mut v, 80, 28);
    v.paste("\n12");
    v.paste("１２");
    assert_eq!(v.draft(), "4096");
}
#[test]
fn resize_focus_mouse_and_confirmation_pages_require_redraw() {
    let mut s = snapshot(&data());
    s.model = "model-".repeat(170);
    let r = Review::parse(
        &review_data(&s, "max_tokens_chat", 1234),
        &s,
        "max_tokens_chat",
        1234,
    )
    .unwrap();
    let mut v = view();
    v.observe(Update::Review(r));
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    for _ in 0..50 {
        v.key(key(KeyCode::PageDown));
    }
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    for _ in 0..50 {
        draw(&mut v, 42, 18);
        v.key(key(KeyCode::PageDown));
    }
    draw(&mut v, 42, 18);
    v.hide();
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    assert!(draw(&mut v, 36, 14).contains("42×18"));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.observe(Update::Review(review()));
    draw(&mut v, 80, 28);
    v.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: 5,
        row: 5,
        modifiers: KeyModifiers::NONE,
    });
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Save { .. })));
}
#[test]
fn saved_and_known_partial_show_durable_and_effective_values_separately() {
    for partial in [false, true] {
        let mut v = view();
        v.observe(Update::Saved {
            snapshot: snapshot(&after(partial)),
            outcome: Outcome::parse(&receipt(partial), &review())
                .unwrap()
                .unwrap()
                .into(),
        });
        let screen = draw(&mut v, 96, 30);
        assert!(screen.contains("Saved 1234"));
        assert!(screen.contains(if partial {
            "Effective 4096"
        } else {
            "Effective 1234"
        }));
        if partial {
            assert!(screen.contains("Runtime failed"));
        }
    }
}
fn state() -> Value {
    let mut v = json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":"custom","model":"test"},"ownership":{"data_version":1,"writable":true,"agent_id":AGENT},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],"tool_policy":{"checked":"in driver"},"settings":{"effective":{"max_tokens_chat":1234,"max_tokens_tools":8192}}});
    v["settings"]["requested"] = v["settings"]["effective"].clone();
    v["settings"]["deferred"] = json!({});
    v["settings"]["metadata"]=json!(edit::FIELDS.map(|name|json!({"name":name,"label":name,"type":"int","source":"settings","read_only":false,"secret":false,"apply":"next_turn"})));
    v
}
fn reply(m: &mut Management, c: &Value, result: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":result}))
}
fn write(s: Step) -> Value {
    let Step::Write(c) = s else { panic!("{s:?}") };
    c
}
fn connected() -> Management {
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,"backend":"custom","model":"test","tool_profile":"strict"})).unwrap();
    let c=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",edit::READ,edit::VALIDATE,edit::SAVE]})).unwrap());
    reply(&mut m, &c, state()).unwrap();
    m
}
fn request(m: &mut Management, i: Intent) -> Value {
    m.request_settings(settings_view::Intent::Editor(i))
        .unwrap()
}
fn prepare(m: &mut Management) -> Value {
    let c = request(m, Intent::Read);
    reply(m, &c, data()).unwrap();
    let c = request(
        m,
        Intent::Validate {
            snapshot: "a".repeat(64),
            field: "max_tokens_chat".into(),
            value: 1234,
        },
    );
    assert_eq!(c["type"], edit::VALIDATE);
    reply(
        m,
        &c,
        review_data(&snapshot(&data()), "max_tokens_chat", 1234),
    )
    .unwrap();
    request(
        m,
        Intent::Save {
            review: "c".repeat(64),
        },
    )
}
#[test]
fn reducer_save_waits_for_independent_budget_and_policy_reads() {
    for partial in [false, true] {
        let mut m = connected();
        let c = prepare(&mut m);
        assert_eq!(c["type"], edit::SAVE);
        assert!(!m.ready());
        let read = write(reply(&mut m, &c, receipt(partial)).unwrap());
        assert_eq!(read["type"], edit::READ);
        let read = write(reply(&mut m, &read, after(partial)).unwrap());
        assert_eq!(read["type"], "gui.state");
        assert_eq!(read["include_conversations"], false);
        let mut st = state();
        st["settings"]["effective"]["max_tokens_chat"] = json!(if partial { 4096 } else { 1234 });
        st["settings"]["requested"] = st["settings"]["effective"].clone();
        assert!(matches!(
            reply(&mut m, &read, st).unwrap(),
            Step::Settings {
                update: settings_view::Update::Edited {
                    update: Update::Saved { .. },
                    ..
                },
                policy: Some(_)
            }
        ));
        assert!(m.ready());
        assert!(
            m.request_settings(settings_view::Intent::Editor(Intent::Save {
                review: "c".repeat(64)
            }))
            .is_err()
        );
    }
}
#[test]
fn reducer_refuses_stale_before_save_and_preserves_original_connection() {
    let mut m = connected();
    let c = prepare(&mut m);
    let mut r = receipt(false);
    r["status"] = json!("refused");
    r["mutated"] = json!(false);
    r["error"] = json!("stale");
    r["persistence"] = json!([]);
    r["runtime"] = json!([]);
    r["saved"] = Value::Null;
    r["effective"] = Value::Null;
    r["revisions"] = Value::Null;
    assert!(matches!(
        reply(&mut m, &c, r).unwrap(),
        Step::Settings {
            update: settings_view::Update::Editor(Update::Rejected(_)),
            policy: None
        }
    ));
    assert!(m.ready());
    m.prompt("fresh-explicit-prompt").unwrap();
}
#[test]
fn reducer_foreign_or_contradictory_readback_fails_closed() {
    for fault in [
        "agent",
        "saved",
        "revision",
        "peer",
        "state-model",
        "state-value",
        "hold",
        "busy",
    ] {
        let mut m = connected();
        let c = prepare(&mut m);
        let c = write(reply(&mut m, &c, receipt(false)).unwrap());
        let mut v = after(false);
        match fault {
            "agent" => v["agent_id"] = json!(SESSION),
            "saved" => v["fields"]["max_tokens_chat"]["saved"] = json!(5678),
            "revision" => v["revisions"]["conversation"] = json!("a".repeat(64)),
            "peer" => v["fields"]["max_tokens_tools"]["effective"] = json!(100),
            _ => {}
        }
        let next = reply(&mut m, &c, v);
        if ["agent", "saved", "revision", "peer"].contains(&fault) {
            assert!(next.is_err(), "{fault}");
            continue;
        }
        let c = write(next.unwrap());
        let mut v = state();
        match fault {
            "state-model" => v["model"]["model"] = json!("other"),
            "state-value" => v["settings"]["effective"]["max_tokens_chat"] = json!(999),
            "hold" => v["restored_input"]["policy"] = json!("automatic"),
            "busy" => v["busy"] = json!(true),
            _ => {}
        }
        assert!(reply(&mut m, &c, v).is_err(), "{fault}");
        assert!(!m.ready());
    }
}

#[test]
fn minimum_geometry_cursor_and_all_themes_are_readable() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut v = view();
        let mut t = Terminal::new(TestBackend::new(42, 18)).unwrap();
        t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        use ratatui::backend::Backend;
        let cursor = t.backend_mut().get_cursor_position().unwrap();
        let row: String = (0..42)
            .map(|x| t.backend().buffer()[(x, cursor.y)].symbol())
            .collect();
        assert!(row.contains("New value: 4096"), "{row}");
        assert_eq!(cursor.x, 17);
        assert_eq!(t.backend().buffer()[(2, 2)].bg, theme.background);
        v.key(key(KeyCode::Home));
        for _ in 0..4 {
            v.key(key(KeyCode::Delete));
        }
        v.key(key(KeyCode::Char('r')));
        v.take_intent();
        v.observe(Update::Snapshot(snapshot(&data())));
        assert_eq!(
            v.draft(),
            "",
            "Explicit refresh preserves a cleared local edit"
        );
    }
}
#[test]
fn saved_update_replaces_parent_inspector_and_preserves_selection() {
    for partial in [false, true] {
        let mut v = settings_view::View::default();
        let mut st = state();
        st["settings"]["requested"]["max_tokens_chat"] = json!(4096);
        st["settings"]["effective"]["max_tokens_chat"] = json!(4096);
        v.observe(settings_view::Update::Snapshot(
            settings_view::Snapshot::parse(&st, SESSION).unwrap(),
        ));
        v.editor.open("max_tokens_chat");
        let mut st = state();
        if partial {
            st["settings"]["requested"]["max_tokens_chat"] = json!(4096);
            st["settings"]["effective"]["max_tokens_chat"] = json!(4096);
        }
        v.observe(settings_view::Update::Edited {
            update: Update::Saved {
                snapshot: snapshot(&after(partial)),
                outcome: Outcome::parse(&receipt(partial), &review())
                    .unwrap()
                    .unwrap()
                    .into(),
            },
            inspection: settings_view::Snapshot::parse(&st, SESSION).unwrap(),
        });
        v.key(key(KeyCode::Esc));
        assert_eq!(
            v.snapshot.as_ref().unwrap().rows[0].effective,
            if partial { "4096" } else { "1234" }
        );
    }
}
#[test]
fn bidi_model_is_escaped_in_final_confirmation() {
    let mut s = snapshot(&data());
    s.model = "left\u{202e}right".into();
    let r = Review::parse(
        &review_data(&s, "max_tokens_chat", 1234),
        &s,
        "max_tokens_chat",
        1234,
    )
    .unwrap();
    let mut v = view();
    v.observe(Update::Review(r));
    let screen = draw(&mut v, 80, 28);
    assert!(screen.contains("left\\u{202e}right"));
    assert!(!screen.contains('\u{202e}'));
}

#[test]
fn independent_readback_cannot_change_edit_authority_or_other_scope_revisions() {
    let review = review();
    for scope in ["global", "agent"] {
        let mut receipt = receipt(false);
        receipt["revisions"][scope] = json!("f".repeat(64));
        let outcome = Outcome::parse(&receipt, &review).unwrap().unwrap();
        let mut data = after(false);
        data["revisions"][scope] = json!("f".repeat(64));
        assert!(outcome.verify(&snapshot(&data), &review).is_err());
    }
    let outcome = Outcome::parse(&receipt(false), &review).unwrap().unwrap();
    let mut data = after(false);
    data["fields"]["max_tokens_chat"]["editable"] = json!(false);
    data["fields"]["max_tokens_chat"]["reason"] = json!("A new request override owns this budget");
    assert!(outcome.verify(&snapshot(&data), &review).is_err());
}
