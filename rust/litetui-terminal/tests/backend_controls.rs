use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::{
    backend_controls::{Catalog, Intent, Update, View},
    management::{Management, Step},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "00000000-0000-4000-8000-000000000001";
const AGENT: &str = "11111111-1111-4111-8111-111111111111";
fn data(backend: &str, model: &str) -> Value {
    json!({"version":1,"kind":"support_metadata","readiness_checked":false,"session_id":SESSION,"agent_id":AGENT,"agent_name":"Owned agent","snapshot":"a".repeat(64),"selected_backend":backend,"configured_backend":backend,"selected_model":model,"codex_engine":{"requested_native":true,"effective":if backend=="codex"{json!("native")}else{Value::Null}},"entries":[{"name":"custom","label":"Custom server","selected":backend=="custom","owned_engine_controls":false},{"name":"lmstudio","label":"LM Studio","selected":backend=="lmstudio","owned_engine_controls":true},{"name":"codex","label":"Codex","selected":backend=="codex","owned_engine_controls":false}]})
}
fn catalog() -> Catalog {
    Catalog::parse(&data("custom", "old"), 7, SESSION, AGENT).unwrap()
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn view() -> View {
    let mut v = View::default();
    v.open();
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Catalog(catalog())).unwrap();
    v
}
fn draw(v: &mut View, w: u16, h: u16) -> String {
    let t = litetui_terminal::theme::builtin_themes().remove(0);
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    terminal.draw(|f| v.draw(f, f.area(), &t)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(w as usize)
        .map(|r| r.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn edit(v: &mut View, model: &str) {
    draw(v, 80, 28);
    v.key(key(KeyCode::Down));
    v.key(key(KeyCode::Enter));
    v.paste(model);
}
#[test]
fn metadata_is_owned_bounded_and_never_readiness() {
    let c = catalog();
    assert_eq!(c.configured, "custom");
    assert!(c.requested_native);
    assert!(c.effective_engine.is_none());
    assert_eq!(c.entries.len(), 3);
    assert!(c.selection(7, "lmstudio", "new").is_ok());
    for (r, b, m) in [
        (8, "lmstudio", "new"),
        (7, "foreign", "new"),
        (7, "custom", "old"),
        (7, "lmstudio", " "),
        (7, "lmstudio", " leading"),
        (7, "lmstudio", "new\n"),
    ] {
        assert!(c.selection(r, b, m).is_err());
    }
}
#[test]
fn malformed_or_foreign_catalog_cannot_be_presented() {
    for fault in [
        "version",
        "ready",
        "kind",
        "session",
        "agent",
        "duplicate",
        "selected",
        "missing-current",
        "missing-configured",
        "engine",
        "boolean",
        "count",
        "oversized",
        "snapshot",
        "model",
    ] {
        let mut d = data("custom", "old");
        match fault {
            "version" => d["version"] = json!(true),
            "ready" => d["readiness_checked"] = json!(true),
            "kind" => d["kind"] = json!("ready"),
            "session" => d["session_id"] = json!(AGENT),
            "agent" => d["agent_id"] = json!(SESSION),
            "duplicate" => {
                let r = d["entries"][0].clone();
                d["entries"].as_array_mut().unwrap().push(r);
            }
            "selected" => d["entries"][1]["selected"] = json!(true),
            "missing-current" => d["selected_backend"] = json!("unknown"),
            "missing-configured" => d["configured_backend"] = json!("unknown"),
            "engine" => d["codex_engine"]["effective"] = json!("native"),
            "boolean" => d["codex_engine"]["requested_native"] = json!(1),
            "count" => d["entries"] = json!(vec![d["entries"][0].clone(); 33]),
            "oversized" => d["extra"] = json!("x".repeat(65536)),
            "snapshot" => d["snapshot"] = json!("A".repeat(64)),
            "model" => d["selected_model"] = json!("x".repeat(1025)),
            _ => unreachable!(),
        };
        assert!(Catalog::parse(&d, 1, SESSION, AGENT).is_err(), "{fault}");
    }
}
#[test]
fn model_is_explicit_and_fresh_draw_is_required_to_apply() {
    let mut v = view();
    edit(&mut v, "new");
    v.key(key(KeyCode::Enter));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    let text = draw(&mut v, 80, 28);
    assert!(text.contains("Backend: lmstudio") && text.contains("Model: new"));
    v.key(KeyEvent::new_with_kind(
        KeyCode::Enter,
        KeyModifiers::NONE,
        KeyEventKind::Repeat,
    ));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Enter));
    assert_eq!(
        v.take_intent(),
        Some(Intent::Select {
            revision: 7,
            name: "lmstudio".into(),
            model: "new".into()
        })
    );
    assert!(v.waiting);
    v.close();
    assert!(v.owns_input());
    assert!(
        v.observe(Update::Changed {
            backend: "foreign".into(),
            model: "new".into()
        })
        .is_err()
    );
    v.observe(Update::Changed {
        backend: "lmstudio".into(),
        model: "new".into(),
    })
    .unwrap();
    assert!(!v.owns_input());
}
#[test]
fn resize_focus_selection_and_modifiers_disarm_confirmation() {
    for change in ["resize", "hide", "modifier", "mouse", "paste"] {
        let mut v = view();
        edit(&mut v, "new");
        v.key(key(KeyCode::Enter));
        draw(&mut v, 80, 28);
        match change {
            "resize" => {
                draw(&mut v, 36, 14);
            }
            "hide" => v.hide(),
            "modifier" => v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT)),
            "mouse" => v.mouse(MouseEvent {
                kind: MouseEventKind::Moved,
                column: 0,
                row: 0,
                modifiers: KeyModifiers::NONE,
            }),
            "paste" => v.paste("changed"),
            _ => unreachable!(),
        };
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none(), "{change}");
    }
}
#[test]
fn exact_model_paste_rejects_multiline_or_oversized_without_truncating() {
    let mut v = view();
    edit(&mut v, "new");
    for text in [
        "\nother".to_owned(),
        "\u{1b}escape".into(),
        "雪".repeat(400),
    ] {
        v.paste(&text);
        assert_eq!(v.model_text(), "new");
    }
    v.key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::CONTROL));
    v.paste("雪 model");
    assert_eq!(v.model_text(), "雪 model");
    v.key(key(KeyCode::Tab));
    v.key(key(KeyCode::Down));
    assert_eq!(v.model_text(), "");
}
#[test]
fn minimum_geometry_and_all_themes_render_without_default_model() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut v = view();
        let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
        terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        assert_eq!(v.model_text(), "");
    }
    let mut v = view();
    edit(&mut v, "new");
    draw(&mut v, 35, 13);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Esc));
    assert!(!v.waiting);
}
#[test]
fn long_confirmation_requires_review_pages_and_a_fresh_last_page() {
    let mut v = view();
    edit(&mut v, &"m".repeat(1024));
    draw(&mut v, 36, 14);
    v.key(key(KeyCode::Enter));
    draw(&mut v, 36, 14);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    for _ in 0..10 {
        v.key(key(KeyCode::PageDown));
        draw(&mut v, 36, 14);
    }
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(),Some(Intent::Select{model,..}) if model.len()==1024));
}
#[test]
fn paired_mouse_selection_cannot_apply() {
    let mut v = view();
    draw(&mut v, 80, 28);
    let m = |kind| MouseEvent {
        kind,
        column: 3,
        row: 7,
        modifiers: KeyModifiers::NONE,
    };
    v.mouse(m(MouseEventKind::Up(MouseButton::Left)));
    assert!(v.take_intent().is_none());
    v.mouse(m(MouseEventKind::Down(MouseButton::Left)));
    v.mouse(m(MouseEventKind::Up(MouseButton::Left)));
    assert!(v.take_intent().is_none());
}
fn state(backend: &str, model: &str) -> Value {
    json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":backend,"model":model},"ownership":{"data_version":1,"writable":true,"agent_id":AGENT},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],"tool_policy":{"checked":"later in driver"}})
}
fn history() -> Value {
    json!({"session_id":SESSION,"read_only":false,"meta":{"id":SESSION},"messages":[{"role":"user","content":"Saved prompt"},{"role":"assistant","content":"Saved answer"}]})
}
fn queue() -> Value {
    json!({"session_id":SESSION,"backend":"lmstudio","model":"new","policy":"hold","held":0,"snapshot":"c".repeat(64),"entries":[]})
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
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,"backend":"custom","model":"old","tool_profile":"strict"})).unwrap();
    let st=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state","gui.backends.list","gui.backend.set","gui.conversations.read","gui.restored_input.list"]})).unwrap());
    reply(&mut m, &st, state("custom", "old")).unwrap();
    m
}
fn prepare(m: &mut Management) -> Value {
    let read = m.request_backend(Intent::Read).unwrap();
    let Step::Backend(Update::Catalog(c)) = reply(m, &read, data("custom", "old")).unwrap() else {
        panic!()
    };
    let before = m
        .request_backend(Intent::Select {
            revision: c.revision,
            name: "lmstudio".into(),
            model: "new".into(),
        })
        .unwrap();
    assert_eq!(before["type"], "gui.conversations.read");
    assert_eq!(before["max_response_bytes"], 1048576);
    write(reply(m, &before, history()).unwrap())
}
fn emission(m: &mut Management, c: &Value, foreign: bool) -> std::io::Result<Step> {
    let event = json!({"type":"model_state","backend":"lmstudio","model":"new"});
    assert!(matches!(m.accept(event.clone()).unwrap(), Step::Ignore));
    m.accept(json!({"type":"gui.event","event_id":"12345678-1234-4123-8123-123456789abc","session_id":if foreign{AGENT}else{SESSION},"operation_id":c["id"],"execution_id":null,"hook_id":null,"source":"rpc","event":event}))
}
fn receipt(status: &str) -> Value {
    json!({"version":1,"kind":"backend_transition","status":status,"mutated":status=="connected","close_required":status=="failed","session_id":SESSION,"agent_id":AGENT,"snapshot":"a".repeat(64),"backend":if status=="connected"{"lmstudio"}else{"custom"},"model":if status=="connected"{"new"}else{"old"},"error":if status=="connected"{Value::Null}else{json!("source refusal")}})
}
#[test]
fn connected_receipt_waits_for_all_independent_readbacks_before_identity_change() {
    let mut m = connected();
    let set = prepare(&mut m);
    assert_eq!(set["type"], "gui.backend.set");
    assert_eq!(set["model"], "new");
    emission(&mut m, &set, false).unwrap();
    let st = write(reply(&mut m, &set, receipt("connected")).unwrap());
    assert_eq!(st["type"], "gui.state");
    assert_eq!(st["include_conversations"], false);
    assert!(!m.ready());
    assert_eq!(m.backend(), Some("custom"));
    let h = write(reply(&mut m, &st, state("lmstudio", "new")).unwrap());
    assert_eq!(h["max_response_bytes"], 1048576);
    let list = write(reply(&mut m, &h, history()).unwrap());
    let q = write(reply(&mut m, &list, data("lmstudio", "new")).unwrap());
    assert!(!m.ready());
    let Step::BackendChanged(changed) = reply(&mut m, &q, queue()).unwrap() else {
        panic!()
    };
    assert_eq!(changed.history.messages.len(), 2);
    assert!(m.ready());
    assert_eq!(m.backend(), Some("lmstudio"));
    assert_eq!(m.model(), Some("new"));
    m.prompt("fresh-prompt").unwrap();
}
#[test]
fn unmutated_refusal_is_retryable_but_failure_or_forged_receipt_closes() {
    let mut m = connected();
    let set = prepare(&mut m);
    assert!(matches!(
        reply(&mut m, &set, receipt("refused")).unwrap(),
        Step::Backend(Update::Rejected(_))
    ));
    assert!(m.ready());
    assert!(m.request_backend(Intent::Read).is_ok());
    for fault in [
        "failed", "mutated", "agent", "snapshot", "target", "no-event",
    ] {
        let mut m = connected();
        let set = prepare(&mut m);
        let mut r = receipt("connected");
        if fault != "no-event" {
            emission(&mut m, &set, false).unwrap();
        }
        match fault {
            "failed" => r = receipt("failed"),
            "mutated" => r["mutated"] = json!(false),
            "agent" => r["agent_id"] = json!(SESSION),
            "snapshot" => r["snapshot"] = json!("b".repeat(64)),
            "target" => r["model"] = json!("fallback"),
            _ => {}
        }
        assert!(reply(&mut m, &set, r).is_err(), "{fault}");
        assert!(!m.ready());
    }
}
#[test]
fn foreign_transition_event_and_readback_contradictions_fail_closed() {
    let mut m = connected();
    let set = prepare(&mut m);
    assert!(emission(&mut m, &set, true).is_err());
    for fault in ["session", "state", "held", "history", "catalog", "queue"] {
        let mut m = connected();
        let set = prepare(&mut m);
        emission(&mut m, &set, false).unwrap();
        let st = write(reply(&mut m, &set, receipt("connected")).unwrap());
        let mut v = state("lmstudio", "new");
        match fault {
            "session" => v["session_id"] = json!(AGENT),
            "state" => v["model"]["backend"] = json!("custom"),
            "held" => v["restored_input"]["held"] = json!(1),
            _ => {}
        }
        let r = reply(&mut m, &st, v);
        if ["session", "state", "held"].contains(&fault) {
            assert!(r.is_err());
            continue;
        }
        let h = write(r.unwrap());
        let mut v = history();
        if fault == "history" {
            v["messages"][1]["content"] = json!("changed");
        }
        let r = reply(&mut m, &h, v);
        if fault == "history" {
            assert!(r.is_err());
            continue;
        }
        let list = write(r.unwrap());
        let r = reply(
            &mut m,
            &list,
            data(
                if fault == "catalog" {
                    "custom"
                } else {
                    "lmstudio"
                },
                "new",
            ),
        );
        if fault == "catalog" {
            assert!(r.is_err());
            continue;
        }
        let q = write(r.unwrap());
        let mut v = queue();
        v["model"] = json!("foreign");
        assert!(reply(&mut m, &q, v).is_err());
    }
}
#[test]
fn unsupported_history_is_rejected_before_mutation_and_empty_owned_history_is_valid() {
    let mut m = connected();
    let read = m.request_backend(Intent::Read).unwrap();
    let Step::Backend(Update::Catalog(c)) = reply(&mut m, &read, data("custom", "old")).unwrap()
    else {
        panic!()
    };
    let before = m
        .request_backend(Intent::Select {
            revision: c.revision,
            name: "lmstudio".into(),
            model: "new".into(),
        })
        .unwrap();
    let mut h = history();
    h["messages"][0]["content"] = json!("x".repeat(40000));
    assert!(matches!(
        reply(&mut m, &before, h).unwrap(),
        Step::Backend(Update::Rejected(_))
    ));
    assert!(m.ready());
    let mut h = history();
    h["messages"] = json!([]);
    assert!(
        litetui_terminal::backend_controls::history(&h, SESSION)
            .unwrap()
            .messages
            .is_empty()
    );
    h["read_only"] = json!(true);
    assert!(litetui_terminal::backend_controls::history(&h, SESSION).is_err());
}

#[test]
fn confirmation_escapes_bidirectional_controls_without_changing_exact_model() {
    let mut v = view();
    edit(&mut v, "model\u{202e}txt");
    v.key(key(KeyCode::Enter));
    let text = draw(&mut v, 80, 28);
    assert!(!text.contains('\u{202e}'));
    assert!(text.contains("\\u{202e}"), "{text}");
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(),Some(Intent::Select{model,..}) if model=="model\u{202e}txt"));
}

#[test]
fn over_limit_explicit_model_fails_before_any_management_command() {
    let c = catalog();
    assert!(c.selection(7, "lmstudio", &"x".repeat(1024)).is_ok());
    assert!(c.selection(7, "lmstudio", &"x".repeat(1025)).is_err());
    let mut m = connected();
    let read = m.request_backend(Intent::Read).unwrap();
    let Step::Backend(Update::Catalog(c)) = reply(&mut m, &read, data("custom", "old")).unwrap()
    else {
        panic!()
    };
    assert!(
        m.request_backend(Intent::Select {
            revision: c.revision,
            name: "lmstudio".into(),
            model: "x".repeat(1025)
        })
        .is_err()
    );
    assert!(m.ready());
}
#[test]
fn settings_requires_fresh_unchanged_owned_state_and_masks_secrets() {
    use litetui_terminal::settings_view::{Intent, Update};
    let mut m = connected();
    let command = m.request_settings(Intent::Read).unwrap();
    assert_eq!(command["type"], "gui.state");
    assert_eq!(command["include_conversations"], false);
    assert!(!m.ready());
    let mut s = state("custom", "old");
    s["settings"] = json!({"metadata":[{"name":"token","label":"Token","type":"str","source":"settings","read_only":false,"secret":true,"apply":"reconnect"}],"requested":{"token":"SECRET-MUST-NOT-SURVIVE"},"effective":{"token":"SECRET-MUST-NOT-SURVIVE"},"deferred":{}});
    let result = reply(&mut m, &command, s).unwrap();
    let Step::Settings {
        update: Update::Snapshot(snapshot),
        policy: Some(_),
    } = result
    else {
        panic!("{result:?}")
    };
    assert!(!format!("{snapshot:?}").contains("SECRET-MUST-NOT-SURVIVE"));
    assert!(m.ready());
    for fault in ["session", "agent", "model", "backend", "profile", "busy"] {
        let mut m = connected();
        let c = m.request_settings(Intent::Read).unwrap();
        let mut s = state("custom", "old");
        match fault {
            "session" => s["session_id"] = json!(AGENT),
            "agent" => s["ownership"]["agent_id"] = json!(SESSION),
            "model" => s["model"]["model"] = json!("other"),
            "backend" => s["model"]["backend"] = json!("other"),
            "profile" => s["tool_profile"] = json!("autonomous"),
            "busy" => s["busy"] = json!(true),
            _ => {}
        }
        assert!(reply(&mut m, &c, s).is_err(), "{fault}");
        assert!(!m.ready());
    }
}

#[test]
fn confirmation_pages_cannot_be_skipped_before_they_are_drawn_and_editor_shows_caret() {
    let mut v = view();
    edit(&mut v, &"m".repeat(1024));
    draw(&mut v, 36, 14);
    v.key(key(KeyCode::Enter));
    for _ in 0..40 {
        v.key(key(KeyCode::PageDown));
    }
    draw(&mut v, 36, 14);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Esc));
    v.key(key(KeyCode::Home));
    let rendered = draw(&mut v, 36, 14);
    assert!(rendered.contains("Model: ▏mmm"), "{rendered}");
}
#[test]
fn settings_refuses_lost_hold_policy_but_allows_held_count() {
    for held in [0, 2] {
        let mut m = connected();
        let c = m
            .request_settings(litetui_terminal::settings_view::Intent::Read)
            .unwrap();
        let mut s = state("custom", "old");
        s["restored_input"]["held"] = json!(held);
        assert!(reply(&mut m, &c, s).is_ok());
    }
    let mut m = connected();
    let c = m
        .request_settings(litetui_terminal::settings_view::Intent::Read)
        .unwrap();
    let mut s = state("custom", "old");
    s["restored_input"]["policy"] = json!("automatic");
    assert!(reply(&mut m, &c, s).is_err());
}

#[test]
fn oversized_source_history_refusal_keeps_original_backend_ready_without_mutation() {
    let mut m = connected();
    let read = m.request_backend(Intent::Read).unwrap();
    let Step::Backend(Update::Catalog(c)) = reply(&mut m, &read, data("custom", "old")).unwrap()
    else {
        panic!()
    };
    let before = m
        .request_backend(Intent::Select {
            revision: c.revision,
            name: "lmstudio".into(),
            model: "new".into(),
        })
        .unwrap();
    assert_eq!(before["type"], "gui.conversations.read");
    assert_eq!(before["max_response_bytes"], 1048576);
    let step=m.accept(json!({"type":"response","id":before["id"],"ok":false,"error":"Conversation response exceeds requested byte limit"})).unwrap();
    assert!(matches!(step, Step::Backend(Update::Rejected(_))));
    assert!(m.ready());
    assert_eq!(m.backend(), Some("custom"));
    m.prompt("explicit-original-backend-prompt").unwrap();
}
