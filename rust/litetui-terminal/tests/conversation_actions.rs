use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use litetui_terminal::{
    conversation_actions::{
        self as a, Action, Identity, Intent, Receipt, Review, Snapshot, Update, View,
    },
    conversation_browser as browser,
    management::{Management, Step},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "00000000-0000-4000-8000-000000000001";
const TARGET: &str = "00000000-0000-4000-8000-000000000002";
const AGENT: &str = "11111111-1111-4111-8111-111111111111";
fn identity(session: &str) -> Identity {
    Identity {
        session: session.into(),
        agent: AGENT.into(),
        backend: "custom".into(),
        model: "model-A".into(),
    }
}
fn data(session: &str, pending: bool, title: &str) -> Value {
    let mut v = identity(session).command(a::SNAPSHOT);
    v["kind"] = json!("conversation_transition_snapshot");
    v["snapshot"] = json!("a".repeat(64));
    v["current"] = json!({"title":title,"pending":pending,"materialized":!pending});
    v["effects"] = json!({"materialize":true,"lifecycle_hooks":true,"provider_cleanup":false});
    v["actions"] =
        json!({"new":{"eligible":true,"reason":null},"rename":{"eligible":true,"reason":null}});
    v
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&data(SESSION, false, "Original title"), &identity(SESSION)).unwrap()
}
fn review_data(action: Action, pending: bool) -> Value {
    let mut v = identity(SESSION).command(a::VALIDATE);
    v["kind"] = json!("conversation_transition_review");
    v["snapshot"] = json!("a".repeat(64));
    v["review"] = json!("b".repeat(64));
    v["action_id"] = json!("22222222-2222-4222-8222-222222222222");
    v["action"] = json!(action.as_str());
    v["target_session_id"] = json!(if action == Action::New {
        TARGET
    } else {
        SESSION
    });
    v["title"] = if action == Action::Rename {
        json!("Canonical title")
    } else {
        Value::Null
    };
    v["effects"] = json!({"materialize":action==Action::New||pending,"lifecycle_hooks":action==Action::New||pending,"provider_cleanup":false});
    v
}
fn review(action: Action) -> Review {
    Review::parse(&review_data(action, false), snapshot(), action).unwrap()
}
fn receipt(action: Action) -> Value {
    let r = review(action);
    let mut v = r.request();
    v["kind"] = json!("conversation_transition_apply");
    v["from_session_id"] = json!(SESSION);
    v["session_id"] = json!(r.target);
    v["status"] = json!(if action == Action::New {
        "created"
    } else {
        "renamed"
    });
    v["mutated"] = json!(true);
    v["close_required"] = json!(false);
    v["error"] = Value::Null;
    v["persistence"] = json!({"transcript":true,"settings":true});
    v["lifecycle"] =
        json!({"settled":true,"hooks_completed":true,"provider_cleanup_completed":true});
    v
}
fn key(c: KeyCode) -> KeyEvent {
    KeyEvent::new(c, KeyModifiers::NONE)
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
fn view(action: Action) -> View {
    let mut v = View::default();
    v.open(action);
    assert_eq!(v.take_intent(), Some(Intent::Read { action }));
    v.observe(Update::Snapshot(Box::new(snapshot())));
    v
}
fn last_page(v: &mut View) {
    for _ in 0..30 {
        draw(v, 80, 28);
        v.key(key(KeyCode::PageDown));
    }
    draw(v, 80, 28);
}
#[test]
fn source_identity_and_eligibility_are_strict_and_bounded() {
    for (k, x) in [
        ("session_id", json!(TARGET)),
        ("agent_id", json!(SESSION)),
        ("model", json!("other")),
        ("snapshot", json!("A".repeat(64))),
        ("extra", json!("x".repeat(65536))),
    ] {
        let mut v = data(SESSION, false, "Old");
        v[k] = x;
        assert!(Snapshot::parse(&v, &identity(SESSION)).is_err(), "{k}");
    }
    let mut v = data(SESSION, true, "Pending");
    v["current"]["materialized"] = json!(true);
    assert!(Snapshot::parse(&v, &identity(SESSION)).is_err());
    v = data(SESSION, false, "Old");
    v["actions"]["new"] = json!({"eligible":false,"reason":"Resolve held input"});
    let s = Snapshot::parse(&v, &identity(SESSION)).unwrap();
    assert!(s.request(Action::New, "").is_err());
    assert!(s.request(Action::Rename, "exact\n raw title  ").is_ok());
}
#[test]
fn review_uses_source_title_and_never_changes_target_or_materialization() {
    let r = review(Action::Rename);
    assert_eq!(r.title.as_deref(), Some("Canonical title"));
    for key in ["target_session_id", "snapshot", "action", "agent_id"] {
        let mut v = review_data(Action::Rename, false);
        v[key] = json!("wrong");
        assert!(Review::parse(&v, snapshot(), Action::Rename).is_err());
    }
    let mut v = review_data(Action::Rename, false);
    v["effects"]["materialize"] = json!(true);
    assert!(Review::parse(&v, snapshot(), Action::Rename).is_err());
    let mut v = review_data(Action::New, false);
    v["target_session_id"] = json!(SESSION);
    assert!(Review::parse(&v, snapshot(), Action::New).is_err());
}
#[test]
fn receipt_requires_single_review_identity_and_settled_durable_outcome() {
    for action in [Action::Rename, Action::New] {
        Receipt::parse(&receipt(action), review(action)).unwrap();
        for (key, value) in [
            ("review", json!("c".repeat(64))),
            ("close_required", json!(true)),
            ("mutated", json!(false)),
            ("status", json!("failed")),
        ] {
            let mut v = receipt(action);
            v[key] = value;
            assert!(Receipt::parse(&v, review(action)).is_err());
        }
        for key in ["settled", "hooks_completed", "provider_cleanup_completed"] {
            let mut v = receipt(action);
            v["lifecycle"][key] = json!(false);
            assert!(Receipt::parse(&v, review(action)).is_err());
        }
    }
    let mut v = receipt(Action::New);
    v["session_id"] = json!(SESSION);
    v["status"] = json!("refused");
    v["mutated"] = json!(false);
    v["persistence"] = json!({"transcript":false,"settings":false});
    v["error"] = json!("Stale review");
    assert!(Receipt::parse(&v, review(Action::New)).unwrap().refused);
}
#[test]
fn local_title_is_lossless_bounded_and_shift_printable_works() {
    let mut v = view(Action::Rename);
    draw(&mut v, 80, 28);
    for _ in 0..100 {
        v.key(key(KeyCode::Backspace));
    }
    v.paste("  Alpha\nβ\tTail  ");
    v.key(KeyEvent::new(KeyCode::Char('A'), KeyModifiers::SHIFT));
    v.key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::CONTROL));
    assert_eq!(v.draft(), "  Alpha\nβ\tTail  A");
    draw(&mut v, 80, 28);
    v.paste(&"x".repeat(4097));
    assert_eq!(v.draft(), "  Alpha\nβ\tTail  A");
    v.key(key(KeyCode::Enter));
    assert!(
        matches!(v.take_intent(),Some(Intent::Validate{title,..})if title=="  Alpha\nβ\tTail  A")
    );
}
#[test]
fn late_validation_after_escape_never_applies_and_refresh_keeps_draft() {
    let mut v = view(Action::Rename);
    draw(&mut v, 80, 28);
    v.paste(" edited");
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Validate { .. })));
    v.key(key(KeyCode::Esc));
    v.observe(Update::Review(Box::new(review(Action::Rename))));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(!v.owns_input());
    assert!(v.take_intent().is_none());
    let mut v = view(Action::Rename);
    draw(&mut v, 80, 28);
    v.paste(" retained");
    v.observe(Update::Rejected("stale".into()));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::F(5)));
    v.take_intent();
    v.observe(Update::Snapshot(Box::new(snapshot())));
    assert_eq!(v.draft(), "Original title retained");
}
#[test]
fn complete_paged_review_disarms_for_resize_pointer_focus_and_held_enter() {
    for invalidation in 0..4 {
        let mut v = view(Action::New);
        let mut r = review(Action::New);
        r.before.title = "Long title ".repeat(200);
        v.observe(Update::Review(Box::new(r)));
        last_page(&mut v);
        match invalidation {
            0 => {
                draw(&mut v, 79, 28);
            }
            1 => v.hide(),
            2 => v.mouse(MouseEvent {
                kind: MouseEventKind::Moved,
                column: 3,
                row: 3,
                modifiers: KeyModifiers::NONE,
            }),
            _ => {
                let mut k = key(KeyCode::Enter);
                k.kind = KeyEventKind::Repeat;
                v.key(k);
            }
        }
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none());
        last_page(&mut v);
        v.key(key(KeyCode::Enter));
        assert!(matches!(v.take_intent(), Some(Intent::Apply { .. })));
        v.close();
        assert!(v.owns_input(), "sent mutation retains ownership");
        v.complete(Action::New, TARGET).unwrap();
        assert!(!v.owns_input());
    }
}
#[test]
fn compact_views_and_all_themes_keep_cancel_visible_and_escape_untrusted_review() {
    for theme in litetui_terminal::theme::builtin_themes() {
        for (w, h) in [(36, 14), (42, 18)] {
            let mut v = view(Action::Rename);
            let mut r = review(Action::Rename);
            r.title = Some("safe\u{202e}title".into());
            v.observe(Update::Review(Box::new(r)));
            let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
            t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
            let text: String = t
                .backend()
                .buffer()
                .content
                .iter()
                .map(|c| c.symbol())
                .collect();
            assert!(text.contains("Esc"));
            assert!(!text.contains('\u{202e}'));
        }
    }
}
fn state(session: &str) -> Value {
    json!({"session_id":session,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":"custom","model":"model-A"},"ownership":{"writable":true,"data_version":1,"agent_id":AGENT},"schedules_paused":true,"restored_input":{"policy":"hold","held":0},"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],"tool_policy":{"verified":"in driver"}})
}
fn history(session: &str, new: bool) -> Value {
    json!({"session_id":session,"read_only":false,"meta":{"id":session,"name":"Canonical title"},"messages":if new{json!([{"role":"system","content":"Fresh base"}])}else{json!([{"role":"user","content":"Old message","provider_metadata":{"thread":"original-ref"}}])}})
}
fn queue(session: &str) -> Value {
    json!({"session_id":session,"backend":"custom","model":"model-A","policy":"hold","snapshot":"d".repeat(64),"held":0,"entries":[]})
}
fn page(session: &str, title: &str) -> Value {
    json!({"session_id":session,"agent_id":AGENT,"snapshot":"e".repeat(64),"offset":0,"total":1,"entries":[{"session_id":session,"agent_id":AGENT,"agent_name":"Owned","title":title,"message_count":1,"updated_at":1234.0,"current":true,"can_open":false,"reason":"Already open"}],"previous_cursor":null,"next_cursor":null})
}
fn reply(m: &mut Management, c: &Value, r: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":r}))
}
fn write(s: Step) -> Value {
    let Step::Write(c) = s else { panic!("{s:?}") };
    c
}
fn request(m: &mut Management, i: Intent) -> Value {
    m.request_browser(browser::Intent::Action(i)).unwrap()
}
fn connected() -> Management {
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,"backend":"custom","model":"model-A","tool_profile":"strict"})).unwrap();
    let c=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",a::SNAPSHOT,a::VALIDATE,a::APPLY,"gui.conversations.read",browser::PAGE,"gui.restored_input.list"]})).unwrap());
    reply(&mut m, &c, state(SESSION)).unwrap();
    m
}
fn inspect(m: &mut Management, a: Action) {
    let c = request(m, Intent::Read { action: a });
    assert!(matches!(
        reply(m, &c, data(SESSION, false, "Original title")).unwrap(),
        Step::ConversationAction {
            update: Update::Snapshot(_),
            ..
        }
    ));
}
fn reviewed(m: &mut Management, a: Action) -> Value {
    inspect(m, a);
    let c = request(
        m,
        Intent::Validate {
            snapshot: "a".repeat(64),
            action: a,
            title: " raw title ".into(),
        },
    );
    let c = write(reply(m, &c, state(SESSION)).unwrap());
    assert_eq!(c["max_response_bytes"], 1048576);
    let c = write(reply(m, &c, history(SESSION, false)).unwrap());
    let c = write(reply(m, &c, queue(SESSION)).unwrap());
    assert!(matches!(
        reply(m, &c, review_data(a, false)).unwrap(),
        Step::ConversationAction {
            update: Update::Review(_),
            ..
        }
    ));
    request(
        m,
        Intent::Apply {
            review: "b".repeat(64),
        },
    )
}
fn after_state(m: &mut Management, a: Action, c: &Value) -> Value {
    let target = if a == Action::New { TARGET } else { SESSION };
    let c = write(reply(m, c, receipt(a)).unwrap());
    assert_eq!(c["type"], a::SNAPSHOT);
    let c = write(reply(m, &c, data(target, false, "Canonical title")).unwrap());
    assert_eq!(c["type"], "gui.state");
    c
}
#[test]
fn publication_requires_all_independent_readbacks_and_review_cannot_replay() {
    for action in [Action::Rename, Action::New] {
        let mut m = connected();
        let c = reviewed(&mut m, action);
        assert_eq!(c["type"], a::APPLY);
        assert!(!m.ready());
        let target = if action == Action::New {
            TARGET
        } else {
            SESSION
        };
        let c = after_state(&mut m, action, &c);
        let c = write(reply(&mut m, &c, state(target)).unwrap());
        assert_eq!(c["max_response_bytes"], 1048576);
        let c = write(reply(&mut m, &c, history(target, action == Action::New)).unwrap());
        let c = write(reply(&mut m, &c, queue(target)).unwrap());
        assert!(!m.ready());
        let Step::ConversationChanged(changed) =
            reply(&mut m, &c, page(target, "Canonical title")).unwrap()
        else {
            panic!()
        };
        assert_eq!(changed.snapshot.session, target);
        assert!(m.ready());
        assert!(
            m.request_browser(browser::Intent::Action(Intent::Apply {
                review: "b".repeat(64)
            }))
            .is_err()
        );
    }
}
#[test]
fn preflight_refusal_keeps_session_but_generic_apply_failure_closes() {
    let mut m = connected();
    inspect(&mut m, Action::New);
    let c = request(
        &mut m,
        Intent::Validate {
            snapshot: "a".repeat(64),
            action: Action::New,
            title: String::new(),
        },
    );
    let mut s = state(SESSION);
    s["restored_input"]["held"] = json!(1);
    assert!(matches!(
        reply(&mut m, &c, s).unwrap(),
        Step::ConversationAction {
            update: Update::Rejected(_),
            ..
        }
    ));
    assert!(m.ready());
    let c = reviewed(&mut m, Action::New);
    assert!(
        m.accept(json!({"type":"response","id":c["id"],"ok":false,"error":"unknown apply result"}))
            .is_err()
    );
}
#[test]
fn contradictory_title_policy_history_or_catalog_never_publishes() {
    for fault in ["title", "policy", "history", "catalog"] {
        let mut m = connected();
        let c = reviewed(&mut m, Action::Rename);
        let c = write(reply(&mut m, &c, receipt(Action::Rename)).unwrap());
        let mut d = data(SESSION, false, "Canonical title");
        if fault == "title" {
            d["current"]["title"] = json!("wrong");
            assert!(reply(&mut m, &c, d).is_err());
            continue;
        }
        let c = write(reply(&mut m, &c, d).unwrap());
        let mut s = state(SESSION);
        if fault == "policy" {
            s["tool_policy"] = json!({"different":true});
            assert!(reply(&mut m, &c, s).is_err());
            continue;
        }
        let c = write(reply(&mut m, &c, s).unwrap());
        let mut h = history(SESSION, false);
        if fault == "history" {
            h["messages"][0]["provider_metadata"]["thread"] = json!("changed");
            assert!(reply(&mut m, &c, h).is_err());
            continue;
        }
        let c = write(reply(&mut m, &c, h).unwrap());
        let c = write(reply(&mut m, &c, queue(SESSION)).unwrap());
        assert!(reply(&mut m, &c, page(SESSION, "wrong")).is_err());
    }
}
#[test]
fn new_rejects_old_provider_reference_and_shutdown_registers_no_followup() {
    let mut m = connected();
    let c = reviewed(&mut m, Action::New);
    let c = after_state(&mut m, Action::New, &c);
    let c = write(reply(&mut m, &c, state(TARGET)).unwrap());
    assert!(reply(&mut m, &c, history(TARGET, false)).is_err());
    let mut m = connected();
    let c = reviewed(&mut m, Action::New);
    m.begin_close();
    assert!(matches!(
        reply(&mut m, &c, receipt(Action::New)).unwrap(),
        Step::ConversationAction {
            update: Update::Rejected(_),
            ..
        }
    ));
    assert!(!m.ready());
}
#[test]
fn f8_actions_remain_accessible_when_pending_current_is_not_in_saved_catalog() {
    let mut v = browser::View::default();
    v.open();
    v.take_intent();
    let mut p = page(SESSION, "Original");
    p["entries"] = json!([]);
    p["total"] = json!(0);
    v.observe(browser::Update::Page(
        browser::Page::parse(&p, 1, SESSION).unwrap(),
    ))
    .unwrap();
    let mut t = Terminal::new(TestBackend::new(80, 28)).unwrap();
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    v.key(key(KeyCode::Char('e')));
    assert_eq!(
        v.take_intent(),
        Some(browser::Intent::Action(Intent::Read {
            action: Action::Rename
        }))
    );
}

#[test]
fn every_conversation_phase_drains_without_followup_or_publication_on_quit() {
    for stop in 0..=10 {
        let mut m = connected();
        let mut c = request(
            &mut m,
            Intent::Read {
                action: Action::New,
            },
        );
        for stage in 0..=stop {
            if stage == stop {
                m.begin_close();
            }
            let result = match stage {
                0 => data(SESSION, false, "Original title"),
                1 => state(SESSION),
                2 => history(SESSION, false),
                3 => queue(SESSION),
                4 => review_data(Action::New, false),
                5 => receipt(Action::New),
                6 => data(TARGET, false, "Canonical title"),
                7 => state(TARGET),
                8 => history(TARGET, true),
                9 => queue(TARGET),
                10 => page(TARGET, "Canonical title"),
                _ => unreachable!(),
            };
            let step = reply(&mut m, &c, result).unwrap();
            if stage == stop {
                assert!(
                    matches!(
                        step,
                        Step::ConversationAction {
                            update: Update::Rejected(_),
                            ..
                        }
                    ),
                    "stage {stage}: {step:?}"
                );
                assert!(!m.ready());
                break;
            }
            c = match stage {
                0 => request(
                    &mut m,
                    Intent::Validate {
                        snapshot: "a".repeat(64),
                        action: Action::New,
                        title: String::new(),
                    },
                ),
                4 => request(
                    &mut m,
                    Intent::Apply {
                        review: "b".repeat(64),
                    },
                ),
                _ => write(step),
            };
        }
    }
}
#[test]
fn conversation_shutdown_proves_retirement_but_never_effects() {
    let command = review(Action::New).request();
    let proof = json!({"version":1,"worker_settled":true,"lifecycle_settled":true,"native_cleanup_settled":true,"effects_verified":false,"pending_at_close":true,"action_id":command["action_id"],"action":"new","from_session_id":SESSION,"target_session_id":TARGET,"agent_id":AGENT,"backend":"custom","model":"model-A","current_session_id":TARGET});
    a::verify_shutdown(&json!({"conversation_shutdown":proof}), Some(&command)).unwrap();
    for key in [
        "worker_settled",
        "lifecycle_settled",
        "native_cleanup_settled",
        "effects_verified",
        "action_id",
        "from_session_id",
        "target_session_id",
        "agent_id",
        "backend",
        "model",
        "current_session_id",
    ] {
        let mut p = proof.clone();
        p[key] = json!("contradiction");
        assert!(
            a::verify_shutdown(&json!({"conversation_shutdown":p}), Some(&command)).is_err(),
            "{key}"
        );
    }
    assert!(a::verify_shutdown(&json!({}), Some(&command)).is_err());
    assert!(a::verify_shutdown(&json!({"conversation_shutdown":proof}), None).is_err());
    let mut completed = proof.clone();
    completed["pending_at_close"] = json!(false);
    completed["current_session_id"] = json!("11111111-1111-4111-8111-111111111111");
    a::verify_shutdown(&json!({"conversation_shutdown":completed}), Some(&command)).unwrap();
    let mut p = proof;
    for key in [
        "action_id",
        "action",
        "from_session_id",
        "target_session_id",
        "agent_id",
        "backend",
        "model",
    ] {
        p[key] = Value::Null;
    }
    assert!(a::verify_shutdown(&json!({"conversation_shutdown":p}), Some(&command)).is_err());
    p["pending_at_close"] = json!(false);
    a::verify_shutdown(&json!({"conversation_shutdown":p}), Some(&command)).unwrap();
}
#[test]
fn post_action_mode_drift_is_not_published() {
    let mut m = connected();
    let c = reviewed(&mut m, Action::Rename);
    let c = after_state(&mut m, Action::Rename, &c);
    let mut s = state(SESSION);
    s["mode"] = json!("plan");
    assert!(reply(&mut m, &c, s).is_err());
}

fn live_state(session: &str) -> litetui_terminal::management::Snapshot {
    litetui_terminal::management::Snapshot {
        session: session.into(),
        model: "model-A".into(),
        backend: "custom".into(),
        profile: "strict".into(),
        mode: "normal".into(),
        writable: true,
        held_restored_input: Some(0),
        schedules_paused: true,
        conversations: 1,
        jobs: 0,
        tasks: 0,
        presentation: Default::default(),
        title: None,
    }
}
#[test]
fn verified_rename_preserves_history_usage_and_draft_while_new_clears_old_facts() {
    use litetui_terminal::{demo::Demo, live::Observation, runtime_state::Update as Facts};
    for action in [Action::Rename, Action::New] {
        let target = if action == Action::New {
            TARGET
        } else {
            SESSION
        };
        let mut app = Demo::live("custom".into(), "model-A".into());
        app.observe_live(Observation::Ready).unwrap();
        app.observe_live(Observation::State(Facts::from_management(live_state(
            SESSION,
        ))))
        .unwrap();
        app.observe_live(Observation::State(
            Facts::from_event(
                &json!({"type":"usage","usage":{"context_tokens":123,"max_context_tokens":999}}),
            )
            .unwrap()
            .unwrap(),
        ))
        .unwrap();
        app.observe_live(Observation::State(
            Facts::from_event(&json!({"type":"turn_end","tps":12.5}))
                .unwrap()
                .unwrap(),
        ))
        .unwrap();
        app.live.as_mut().unwrap().history = Some(
            litetui_terminal::backend_controls::history(&history(SESSION, false), SESSION).unwrap(),
        );
        app.composer.insert("UNSENT HUMAN DRAFT");
        let old: Vec<_> = app
            .live
            .as_ref()
            .unwrap()
            .runtime_state
            .footer_fields()
            .into_iter()
            .map(|v| (v.key, v.text))
            .collect();
        app.open_browser();
        app.live.as_mut().unwrap().browser.take_intent();
        let v = &mut app.live.as_mut().unwrap().browser.actions;
        v.open(action);
        v.take_intent();
        v.observe(Update::Snapshot(Box::new(snapshot())));
        v.observe(Update::Review(Box::new(review(action))));
        last_page(v);
        v.key(key(KeyCode::Enter));
        v.take_intent();
        let changed = a::Changed {
            action,
            snapshot: live_state(target),
            history: litetui_terminal::backend_controls::history(
                &history(target, action == Action::New),
                target,
            )
            .unwrap(),
            queue: litetui_terminal::restored_queue::Snapshot::parse(&queue(target), 1).unwrap(),
            policy: json!({}),
            current: Snapshot::parse(&data(target, false, "Canonical title"), &identity(target))
                .unwrap(),
            page: browser::Page::parse(&page(target, "Canonical title"), 1, target).unwrap(),
        };
        app.observe_live(Observation::ConversationChanged(Box::new(changed)))
            .unwrap();
        assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
        let chat = app.live.as_mut().unwrap();
        assert!(chat.take_submission().is_none());
        assert_eq!(chat.history.as_ref().unwrap().session, target);
        if action == Action::Rename {
            assert_eq!(
                chat.runtime_state
                    .footer_fields()
                    .into_iter()
                    .map(|v| (v.key, v.text))
                    .collect::<Vec<_>>(),
                old
            );
        } else {
            assert_eq!(chat.runtime_state.startup().unwrap().session, TARGET);
            use litetui_terminal::footer::FieldKey as K;
            assert!(
                !chat
                    .runtime_state
                    .footer_fields()
                    .iter()
                    .any(|v| matches!(v.key, K::Percent | K::Throughput))
            );
            assert!(chat.rows.is_empty());
        }
    }
}

#[test]
fn closing_consumes_failed_apply_without_accepting_its_effects_or_foreign_identity() {
    for failure in ["typed", "rpc", "foreign"] {
        let mut m = connected();
        let c = reviewed(&mut m, Action::New);
        m.begin_close();
        let result = if failure == "rpc" {
            m.accept(json!({"type":"response","id":c["id"],"ok":false,"error":"cancelled before receipt"}))
        } else {
            let mut v = receipt(Action::New);
            v["status"] = json!("failed");
            v["close_required"] = json!(true);
            v["session_id"] = json!(SESSION);
            v["lifecycle"]["settled"] = json!(false);
            v["error"] = json!("Cancelled; effects unverified");
            if failure == "foreign" {
                v["action_id"] = json!(SESSION);
            }
            reply(&mut m, &c, v)
        };
        if failure == "foreign" {
            assert!(result.is_err());
        } else {
            assert!(matches!(
                result.unwrap(),
                Step::ConversationAction {
                    update: Update::Rejected(_),
                    ..
                }
            ));
        }
    }
}

#[test]
fn newly_pending_source_work_refuses_review_without_losing_connection() {
    for field in [
        "active_work",
        "pending_approvals",
        "pending_questions",
        "pending_model_loads",
    ] {
        let mut m = connected();
        inspect(&mut m, Action::New);
        let c = request(
            &mut m,
            Intent::Validate {
                snapshot: "a".repeat(64),
                action: Action::New,
                title: String::new(),
            },
        );
        let mut v = state(SESSION);
        v[field] = json!([{}]);
        assert!(matches!(
            reply(&mut m, &c, v).unwrap(),
            Step::ConversationAction {
                update: Update::Rejected(_),
                ..
            }
        ));
        assert!(m.ready());
    }
}

/// Ryan 2026-11-12 on /new: "all it should do is auto start a new convo thats it". Python's /new
/// shows no dialog. The reviewed steps still run (read, validate, apply), unattended.
#[test]
fn new_runs_its_reviewed_steps_without_a_dialog() {
    let mut v = View::default();
    v.open_immediate(Action::New);
    assert_eq!(
        v.take_intent(),
        Some(Intent::Read {
            action: Action::New
        })
    );
    v.observe(Update::Snapshot(Box::new(snapshot())));
    assert!(matches!(
        v.take_intent(),
        Some(Intent::Validate {
            action: Action::New,
            ..
        })
    ));
    // Nothing is drawn while it runs.
    assert!(!draw(&mut v, 80, 28).contains("Enter to review"));
    v.observe(Update::Review(Box::new(review(Action::New))));
    let token = review(Action::New).token;
    assert_eq!(v.take_intent(), Some(Intent::Apply { review: token }));
    assert!(v.owns_input(), "input waits for the outcome");
    v.complete(Action::New, &review(Action::New).target)
        .unwrap();
    assert!(!v.owns_input());
    // Ineligible: no dialog either, just the reason.
    let mut held = data(SESSION, false, "Old");
    held["actions"]["new"] = json!({"eligible":false,"reason":"Resolve held input"});
    let mut v = View::default();
    v.open_immediate(Action::New);
    v.take_intent();
    v.observe(Update::Snapshot(Box::new(
        Snapshot::parse(&held, &identity(SESSION)).unwrap(),
    )));
    assert_eq!(v.take_intent(), None);
    assert!(!v.owns_input());
    assert_eq!(v.notice, "Resolve held input");
    // A refused validation closes with the source's words.
    let mut v = View::default();
    v.open_immediate(Action::New);
    v.take_intent();
    v.observe(Update::Snapshot(Box::new(snapshot())));
    v.take_intent();
    v.observe(Update::Rejected("Conversation changed".into()));
    assert!(!v.owns_input());
    assert_eq!(v.notice, "Conversation changed");
}
