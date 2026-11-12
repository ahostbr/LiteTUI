use crossterm::event::{KeyCode, KeyEvent, KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use litetui_terminal::{
    backend_controls::{Catalog, Intent, ModelList, Update, View},
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
fn listing(models: Value, context: Value) -> Value {
    json!({"version":1,"kind":"backend_models","session_id":SESSION,"agent_id":AGENT,"snapshot":"a".repeat(64),"name":"lmstudio","models":models,"context":context})
}
fn models_view(context: Value) -> View {
    let mut v = view();
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Down));
    v.key(key(KeyCode::Enter));
    assert_eq!(
        v.take_intent(),
        Some(Intent::Models {
            revision: 7,
            name: "lmstudio".into()
        })
    );
    let list = ModelList::parse(
        &listing(
            json!([{"key":"a-model","label":"  a-model  · lmstudio  · loaded","loaded":true},{"key":"b-model","label":"  b-model  · lmstudio","loaded":false}]),
            context,
        ),
        &catalog(),
        "lmstudio",
    )
    .unwrap();
    v.observe(Update::Models(list)).unwrap();
    v
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
fn engine_picker_reads_like_python_with_readiness_marks() {
    let mut d = data("custom", "old");
    d["marks"] = json!({"lmstudio":"installed","custom":"set URL in /settings","unknown":"x"});
    let mut v = View::default();
    v.open();
    v.take_intent();
    v.observe(Update::Catalog(
        Catalog::parse(&d, 7, SESSION, AGENT).unwrap(),
    ))
    .unwrap();
    let text = draw(&mut v, 80, 28);
    assert!(
        text.contains("Select the engine for this conversation"),
        "{text}"
    );
    assert!(
        text.contains("▸ Custom server  · set URL in /settings"),
        "{text}"
    );
    assert!(text.contains("LM Studio  · installed"), "{text}");
    assert!(
        text.contains("↑↓ move · Enter or click to select · Esc to cancel"),
        "{text}"
    );
    assert!(!text.contains("Model:"), "{text}");
}
#[test]
fn picking_a_loaded_model_on_a_hosted_backend_switches_without_a_context_question() {
    let mut v = models_view(Value::Null);
    let text = draw(&mut v, 80, 28);
    assert!(text.contains("Select a model") && text.contains("a-model  · lmstudio  · loaded"));
    v.key(key(KeyCode::Enter));
    assert_eq!(
        v.take_intent(),
        Some(Intent::Select {
            revision: 7,
            name: "lmstudio".into(),
            model: "a-model".into(),
            context: None,
        })
    );
    assert!(v.waiting);
    assert!(
        v.observe(Update::Changed {
            backend: "foreign".into(),
            model: "a-model".into()
        })
        .is_err()
    );
    v.observe(Update::Changed {
        backend: "lmstudio".into(),
        model: "a-model".into(),
    })
    .unwrap();
    assert!(!v.owns_input());
}
#[test]
fn local_backend_asks_context_and_esc_keeps_current() {
    let facts = json!({"setting":"default_context_length","applies_on_engine_start":false,"suggestions":{"a-model":null,"b-model":8192},"remembered":{"a-model":null,"b-model":8192}});
    let mut v = models_view(facts.clone());
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Down));
    v.key(key(KeyCode::Enter));
    let text = draw(&mut v, 80, 28);
    assert!(
        text.contains("Context length for b-model (tokens)"),
        "{text}"
    );
    assert!(text.contains("The model is loaded at this size. Last used for this model: 8,192."));
    assert_eq!(v.model_text(), "8192");
    v.key(key(KeyCode::Esc));
    assert_eq!(
        v.take_intent(),
        Some(Intent::Select {
            revision: 7,
            name: "lmstudio".into(),
            model: "b-model".into(),
            context: None,
        })
    );
    let mut v = models_view(facts);
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert_eq!(v.model_text(), "");
    v.paste("131,072");
    v.key(key(KeyCode::Enter));
    assert!(matches!(
        v.take_intent(),
        Some(Intent::Select {
            context: Some(131072),
            ..
        })
    ));
}
#[test]
fn context_answer_follows_python_parse_rules() {
    use litetui_terminal::backend_controls::parse_context;
    assert_eq!(parse_context(" 100_000 "), Ok(100000));
    assert_eq!(parse_context("131,072"), Ok(131072));
    assert!(
        parse_context("100k")
            .unwrap_err()
            .contains("Type a number of tokens")
    );
    assert!(
        parse_context("511")
            .unwrap_err()
            .contains("512 to 1,048,576")
    );
    assert!(parse_context(&(1u64 << 21).to_string()).is_err());
}
#[test]
fn current_engine_says_already_on_and_mouse_click_selects_like_python() {
    let mut v = view();
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    assert_eq!(v.notice, "Already on custom");
    let mut v = view();
    let text = draw(&mut v, 80, 28);
    let row = text.lines().position(|l| l.contains("LM Studio")).unwrap() as u16;
    let m = |kind| MouseEvent {
        kind,
        column: 20,
        row,
        modifiers: KeyModifiers::NONE,
    };
    v.mouse(m(MouseEventKind::Up(MouseButton::Left)));
    assert!(
        v.take_intent().is_none(),
        "an unpaired release selects nothing"
    );
    v.mouse(m(MouseEventKind::Down(MouseButton::Left)));
    v.mouse(m(MouseEventKind::Up(MouseButton::Left)));
    assert!(matches!(v.take_intent(), Some(Intent::Models { name, .. }) if name == "lmstudio"));
}
#[test]
fn foreign_or_malformed_model_lists_are_refused() {
    for fault in [
        "kind",
        "snapshot",
        "name",
        "duplicate",
        "control",
        "suggestion",
    ] {
        let mut d = listing(
            json!([{"key":"a","label":"a","loaded":true}]),
            json!({"setting":"default_context_length","applies_on_engine_start":false,"suggestions":{"a":4096},"remembered":{"a":null}}),
        );
        match fault {
            "kind" => d["kind"] = json!("support_metadata"),
            "snapshot" => d["snapshot"] = json!("b".repeat(64)),
            "name" => d["name"] = json!("codex"),
            "duplicate" => {
                d["models"] = json!([{"key":"a","label":"a","loaded":true},{"key":"a","label":"a","loaded":true}])
            }
            "control" => d["models"][0]["key"] = json!("a\n"),
            "suggestion" => d["context"]["suggestions"]["a"] = json!(-1),
            _ => unreachable!(),
        }
        assert!(
            ModelList::parse(&d, &catalog(), "lmstudio").is_err(),
            "{fault}"
        );
    }
}
#[test]
fn minimum_geometry_and_all_themes_render() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut v = view();
        let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
        terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    }
    let mut v = view();
    draw(&mut v, 35, 9);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Esc));
    assert!(!v.waiting);
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
            context: None,
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
            context: None,
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
            model: "x".repeat(1025),
            context: None,
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
            context: None,
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
