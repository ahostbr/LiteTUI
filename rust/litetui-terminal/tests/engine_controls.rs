use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use litetui_terminal::{
    engine_controls::{
        self as e, Action, Admission, Identity, Intent, Receipt, Review, Snapshot, Update, View,
    },
    management::{Management, Step},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "00000000-0000-4000-8000-000000000001";
const AGENT: &str = "11111111-1111-4111-8111-111111111111";
const ACTION_ID: &str = "22222222-2222-4222-8222-222222222222";
fn identity() -> Identity {
    Identity {
        session: SESSION.into(),
        agent: AGENT.into(),
        backend: "strata".into(),
        model: "engine-model".into(),
        instance: "1".repeat(64),
    }
}
fn data() -> Value {
    let mut v = identity().command("ignored");
    v.as_object_mut().unwrap().remove("type");
    for(k,value)in json!({"kind":"engine_snapshot","snapshot":"a".repeat(64),"generation":0,"admission":"management_only","effective_model":null,"tool_policy":{"verified":"driver"},"ownership":{"kind":"unowned","handle_token":null,"host":null,"pid":null},"lifecycle":{"starting":false,"stopping":false,"worker_settled":true},"launch":{"selection_id":"b".repeat(64),"model":"engine-model","executable":"C:/engine/python.exe","cwd":"C:/engine","artifact":"C:/engine/model","config":"C:/engine/config.json","derived_config":null,"context":8192,"concurrency":1,"load_shape":{"dtype":"bf16"},"endpoint_policy":"fixed_loopback","endpoint":"http://127.0.0.1:45120"},"actions":{"start":{"eligible":true,"reason":""},"stop":{"eligible":false,"reason":"No retained process handle"}},"readiness_checked":false,"resources_checked":false}).as_object().unwrap(){v[k]=value.clone();}
    v
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&data(), &identity()).unwrap()
}
fn review_data(s: &Snapshot, a: Action) -> Value {
    let mut v = s.request(a).unwrap();
    v.as_object_mut().unwrap().remove("type");
    v["kind"] = json!("engine_review");
    v["review"] = json!("c".repeat(64));
    v["action_id"] = json!(ACTION_ID);
    v["summary"] = if a == Action::Start {
        s.launch.clone()
    } else {
        s.ownership.clone()
    };
    v["timeout_ms"] = json!(1200000);
    v
}
fn review() -> Review {
    Review::parse(
        &review_data(&snapshot(), Action::Start),
        snapshot(),
        Action::Start,
    )
    .unwrap()
}
fn receipt_data() -> Value {
    let mut v = identity().command("ignored");
    v["kind"] = json!("engine_action");
    for(k,value)in json!({"action_id":ACTION_ID,"review":"c".repeat(64),"action":"start","snapshot":"a".repeat(64),"consumed_generation":0,"generation":1,"status":"started","admission":"prompt_ready","effective_model":"engine-model","handle_token":"d".repeat(64),"worker_settled":true,"cancel_requested":false,"spawn_attempted":true,"mutated":true,"close_required":false,"error_code":null,"error":null,"shutdown":null}).as_object().unwrap(){v[k]=value.clone();}
    v
}
fn readback_data() -> Value {
    let mut v = receipt_data();
    v["kind"] = json!("engine_readback");
    v["connected"] = json!(true);
    v["loaded_model"] = json!("engine-model");
    v
}
#[test]
fn metadata_is_owned_bounded_and_never_probe_readiness() {
    assert_eq!(snapshot().admission, Admission::ManagementOnly);
    for (key, value) in [
        ("agent_id", json!("foreign")),
        ("generation", json!(-1)),
        ("snapshot", json!("A".repeat(64))),
        ("readiness_checked", json!(true)),
        ("resources_checked", json!(true)),
        ("effective_model", json!("engine-model")),
    ] {
        let mut v = data();
        v[key] = value;
        assert!(Snapshot::parse(&v, &identity()).is_err(), "{key}");
    }
    let mut v = data();
    v["launch"]["load_shape"] = json!({"large":"x".repeat(16384)});
    assert!(Snapshot::parse(&v, &identity()).is_err());
    let mut v = data();
    v["ownership"]["kind"] = json!("retained_handle");
    assert!(Snapshot::parse(&v, &identity()).is_err());
    let mut v = data();
    v["actions"]["stop"]["eligible"] = json!(true);
    assert!(Snapshot::parse(&v, &identity()).is_err());
}
#[test]
fn invalid_config_does_not_remove_retained_stop_authority() {
    let mut v = data();
    v["launch"] = Value::Null;
    v["admission"] = json!("retained_recovery");
    v["ownership"] = json!({"kind":"retained_handle","handle_token":"d".repeat(64),"host":"http://127.0.0.1:45120","pid":12});
    v["actions"] = json!({"start":{"eligible":false,"reason":"Resolve retained handle"},"stop":{"eligible":true,"reason":""}});
    let s = Snapshot::parse(&v, &identity()).unwrap();
    assert!(s.request(Action::Start).is_err());
    assert_eq!(
        s.request(Action::Stop).unwrap()["handle_token"],
        "d".repeat(64)
    );
}
#[test]
fn review_cannot_change_the_source_plan_or_owned_handle() {
    for key in [
        "summary",
        "selection_id",
        "consent_scope",
        "action",
        "generation",
        "snapshot",
    ] {
        let mut v = review_data(&snapshot(), Action::Start);
        v[key] = json!("changed");
        assert!(
            Review::parse(&v, snapshot(), Action::Start).is_err(),
            "{key}"
        );
    }
    let mut v = review_data(&snapshot(), Action::Start);
    v["timeout_ms"] = json!(1200001);
    assert!(Review::parse(&v, snapshot(), Action::Start).is_err());
    let r = review();
    assert_eq!(r.request().unwrap()["action_id"], ACTION_ID);
    assert_eq!(r.request().unwrap()["consent_scope"], "one_start");
}
#[test]
fn receipt_requires_worker_settlement_and_independent_matching_readback() {
    let r = Receipt::parse(&receipt_data(), review()).unwrap();
    r.verify_readback(&readback_data()).unwrap();
    for (key, value) in [
        ("close_required", json!(true)),
        ("worker_settled", json!(false)),
        ("generation", json!(0)),
        ("consumed_generation", json!(1)),
        ("handle_token", Value::Null),
        ("spawn_attempted", json!(false)),
    ] {
        let mut v = receipt_data();
        v[key] = value;
        assert!(Receipt::parse(&v, review()).is_err(), "{key}");
    }
    for (key, value) in [
        ("connected", json!(false)),
        ("loaded_model", json!("wrong")),
        ("review", json!("e".repeat(64))),
        ("generation", json!(2)),
        ("worker_settled", json!(false)),
    ] {
        let mut v = readback_data();
        v[key] = value;
        assert!(r.verify_readback(&v).is_err(), "{key}");
    }
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn draw(v: &mut View, w: u16, h: u16) -> String {
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect::<String>()
}
fn view() -> View {
    let mut v = View::default();
    v.open();
    assert_eq!(v.take_intent(), Some(Intent::Read));
    v.observe(Update::Snapshot(Box::new(snapshot())));
    v
}
#[test]
fn visible_review_is_fresh_and_cancel_discards_late_validation() {
    let mut v = view();
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(matches!(
        v.take_intent(),
        Some(Intent::Validate {
            action: Action::Start,
            ..
        })
    ));
    v.key(key(KeyCode::Esc));
    v.observe(Update::Review(Box::new(review())));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(!v.owns_input());
    assert!(v.take_intent().is_none());
    assert!(!v.pending());
    let mut v = View::default();
    v.open();
    v.close();
    assert!(!v.pending());
    v.open();
    assert_eq!(v.take_intent(), Some(Intent::Read));
}
#[test]
fn full_review_paging_restarts_after_resize_or_focus_invalidation() {
    let mut s = snapshot();
    s.launch["load_shape"] = json!({"long":"details ".repeat(200)});
    let r = Review::parse(&review_data(&s, Action::Start), s, Action::Start).unwrap();
    let mut v = view();
    v.observe(Update::Review(Box::new(r)));
    for _ in 0..100 {
        draw(&mut v, 60, 20);
        v.key(key(KeyCode::PageDown));
    }
    draw(&mut v, 61, 20);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none(), "resizing restarts review");
    for _ in 0..100 {
        draw(&mut v, 61, 20);
        v.key(key(KeyCode::PageDown));
    }
    v.invalidate();
    draw(&mut v, 61, 20);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none(), "focus invalidates review");
    for _ in 0..100 {
        draw(&mut v, 61, 20);
        v.key(key(KeyCode::PageDown));
    }
    draw(&mut v, 61, 20);
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    v.key(repeat);
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Execute { .. })));
    v.close();
    assert!(v.pending(), "consumed action remains pending after hiding");
}
#[test]
fn compact_view_and_untrusted_confirmation_are_visible_and_escaped() {
    let mut s = snapshot();
    s.launch["artifact"] = json!("C:/model/\u{202e}name");
    let r = Review::parse(&review_data(&s, Action::Start), s, Action::Start).unwrap();
    let mut v = view();
    v.observe(Update::Review(Box::new(r)));
    assert!(draw(&mut v, 41, 17).contains("Resize to 42x18"));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    let rendered = draw(&mut v, 160, 50);
    assert!(!rendered.contains('\u{202e}'));
    assert!(rendered.contains("202e"));
}
fn state() -> Value {
    json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":"strata","model":"engine-model"},"ownership":{"data_version":1,"writable":true,"agent_id":AGENT},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],"tool_policy":{"verified":"driver"}})
}
fn history() -> Value {
    json!({"session_id":SESSION,"read_only":false,"meta":{"id":SESSION},"messages":[]})
}
fn queue() -> Value {
    json!({"session_id":SESSION,"backend":"strata","model":"engine-model","policy":"hold","held":0,"snapshot":"e".repeat(64),"entries":[]})
}
fn reply(m: &mut Management, c: &Value, v: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":v}))
}
fn write(s: Step) -> Value {
    let Step::Write(c) = s else { panic!("{s:?}") };
    c
}
fn connected() -> Management {
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,"backend":"strata","model":"engine-model","desired_model":"engine-model","tool_profile":"strict","management_instance":"1".repeat(64),"engine_generation":0,"admission":"management_only"})).unwrap();
    let st=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",e::INSPECT,e::VALIDATE,e::ACTION,e::READBACK,"gui.conversations.read","gui.restored_input.list"]})).unwrap());
    reply(&mut m, &st, state()).unwrap();
    m
}
fn inspected(m: &mut Management) {
    let c = m.request_engine(Intent::Read).unwrap();
    assert!(matches!(
        reply(m, &c, data()).unwrap(),
        Step::Engine {
            update: Update::Snapshot(_),
            ..
        }
    ));
}
fn reviewed(m: &mut Management) -> Value {
    inspected(m);
    let c = m
        .request_engine(Intent::Validate {
            snapshot: "a".repeat(64),
            action: Action::Start,
        })
        .unwrap();
    let c = write(reply(m, &c, state()).unwrap());
    assert_eq!(c["max_response_bytes"], 1048576);
    let c = write(reply(m, &c, history()).unwrap());
    let c = write(reply(m, &c, queue()).unwrap());
    assert_eq!(c["type"], e::VALIDATE);
    assert!(matches!(
        reply(m, &c, review_data(&snapshot(), Action::Start)).unwrap(),
        Step::Engine {
            update: Update::Review(_),
            ..
        }
    ));
    m.request_engine(Intent::Execute {
        review: "c".repeat(64),
    })
    .unwrap()
}
#[test]
fn management_admission_changes_only_after_all_action_bound_readbacks() {
    let mut m = connected();
    assert!(m.prompt("no replay").is_err());
    let c = reviewed(&mut m);
    let c = write(reply(&mut m, &c, receipt_data()).unwrap());
    assert_eq!(c["type"], e::READBACK);
    assert!(m.prompt("early").is_err());
    let c = write(reply(&mut m, &c, readback_data()).unwrap());
    let c = write(reply(&mut m, &c, state()).unwrap());
    assert_eq!(c["max_response_bytes"], 1048576);
    let c = write(reply(&mut m, &c, history()).unwrap());
    assert!(m.prompt("early").is_err());
    assert!(matches!(
        reply(&mut m, &c, queue()).unwrap(),
        Step::Engine {
            update: Update::Settled {
                admission: Admission::PromptReady,
                ..
            },
            ..
        }
    ));
    assert!(m.prompt("fresh explicit prompt").is_ok());
    assert!(
        m.request_engine(Intent::Execute {
            review: "c".repeat(64)
        })
        .is_err()
    );
}
#[test]
fn generic_action_error_is_fatal_but_preflight_refusal_keeps_management() {
    let mut m = connected();
    let c = reviewed(&mut m);
    assert!(
        m.accept(
            json!({"type":"response","id":c["id"],"ok":false,"error":"unobserved action failure"})
        )
        .is_err()
    );
    for held in [false, true] {
        let mut m = connected();
        inspected(&mut m);
        let c = m
            .request_engine(Intent::Validate {
                snapshot: "a".repeat(64),
                action: Action::Start,
            })
            .unwrap();
        let mut s = state();
        if held {
            s["restored_input"]["held"] = json!(1);
        } else {
            s["busy"] = json!(true);
        }
        assert!(matches!(
            reply(&mut m, &c, s).unwrap(),
            Step::Engine {
                update: Update::Rejected(_),
                ..
            }
        ));
        assert!(m.ready());
        assert!(m.prompt("still disabled").is_err());
    }
}
#[test]
fn contradictory_post_action_history_or_policy_closes() {
    for stage in [0, 1] {
        let mut m = connected();
        let c = reviewed(&mut m);
        let c = write(reply(&mut m, &c, receipt_data()).unwrap());
        let c = write(reply(&mut m, &c, readback_data()).unwrap());
        if stage == 0 {
            let mut s = state();
            s["tool_policy"] = json!({"changed":true});
            assert!(reply(&mut m, &c, s).is_err());
        } else {
            let c = write(reply(&mut m, &c, state()).unwrap());
            let mut h = history();
            h["messages"] = json!([{"role":"assistant","content":"changed"}]);
            assert!(reply(&mut m, &c, h).is_err());
        }
    }
}
#[test]
fn launch_choice_is_explicit_and_never_queues_a_prompt() {
    use litetui_terminal::runtime_launch::{Backend, LaunchOptions};
    let (l, rest) = LaunchOptions::extract(&[
        "--engine-management".into(),
        "--model".into(),
        "wanted".into(),
    ])
    .unwrap();
    assert!(l.engine_management);
    assert_eq!(rest, ["--model", "wanted"]);
    assert!(
        l.python_args(Backend::Strata, "wanted")
            .unwrap()
            .contains(&"--engine-management".into())
    );
    assert!(l.validate(Backend::Custom, "wanted").is_err());
    let mut start = l.clone();
    start.server_mode = Some(litetui_terminal::runtime_launch::ServerMode::Start);
    assert!(start.validate(Backend::Strata, "wanted").is_err());
    let args = [
        "--python",
        "python.exe",
        "--source",
        "repo",
        "--data-root",
        "root",
        "--backend",
        "strata",
        "--model",
        "wanted",
        "--prompt",
        "queued",
        "--engine-management",
    ]
    .map(str::to_owned);
    assert!(litetui_terminal::live::Options::parse(&args).is_err());
    let mut empty = args.to_vec();
    empty[11] = String::new();
    assert!(litetui_terminal::live::Options::parse(&empty).is_err());
}

#[test]
fn all_builtin_themes_render_inspection_and_review_at_minimum_geometry() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut v = view();
        let mut terminal = Terminal::new(TestBackend::new(42, 18)).unwrap();
        terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>();
        assert!(text.contains("Owned engine"));
        assert!(text.contains("Esc"));
        v.observe(Update::Review(Box::new(review())));
        terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        assert!(
            terminal
                .backend()
                .buffer()
                .content
                .iter()
                .any(|c| c.fg == theme.foreground)
        );
    }
}
#[test]
fn foreign_or_unscoped_engine_events_cannot_change_pending_state() {
    for foreign in [false, true] {
        let mut m = connected();
        let c = reviewed(&mut m);
        let event = json!({"type":"model_state","backend":"strata","model":"engine-model"});
        assert!(matches!(m.accept(event.clone()).unwrap(), Step::Ignore));
        let envelope = json!({"type":"gui.event","event_id":"33333333-3333-4333-8333-333333333333","session_id":if foreign {AGENT}else{SESSION},"operation_id":if foreign{c["id"].clone()}else{json!("other-operation")},"execution_id":null,"hook_id":null,"source":"rpc","event":event});
        assert!(m.accept(envelope).is_err());
    }
}

#[test]
fn shutdown_consumes_engine_receipt_without_registering_an_unsent_readback() {
    let mut m = connected();
    let c = reviewed(&mut m);
    m.begin_close();
    assert!(matches!(
        reply(&mut m, &c, receipt_data()).unwrap(),
        Step::Engine {
            update: Update::Rejected(_),
            ..
        }
    ));
    m.ensure_drained().unwrap();
    assert!(!m.ready());
    assert!(m.prompt("never").is_err());
    let mut m = connected();
    inspected(&mut m);
    let c = m
        .request_engine(Intent::Validate {
            snapshot: "a".repeat(64),
            action: Action::Start,
        })
        .unwrap();
    m.begin_close();
    assert!(matches!(
        reply(&mut m, &c, state()).unwrap(),
        Step::Engine {
            update: Update::Rejected(_),
            ..
        }
    ));
    m.ensure_drained().unwrap();
}

#[test]
fn shutdown_requires_joined_startup_owned_handle_proof_without_resource_claim() {
    let identity = e::ShutdownIdentity {
        instance: "a".repeat(64),
        session: SESSION.into(),
        backend: "strata".into(),
    };
    assert!(e::verify_shutdown(&json!({"ok":true}), Some(&identity)).is_err());
    let good = json!({"ok":true,"engine_shutdown":{"version":1,"management_instance":"a".repeat(64),"session_id":SESSION,"backend":"strata","worker_settled":true,"resource_release_verified":false,"shutdown":{"owned":true,"attempted":true,"main_exited":true,"tree":"unknown","pid":123,"retained":false,"error":null}}});
    e::verify_shutdown(&good, Some(&identity)).unwrap();
    for (key, value) in [
        ("management_instance", json!("b".repeat(64))),
        ("worker_settled", json!(false)),
        ("resource_release_verified", json!(true)),
    ] {
        let mut v = good.clone();
        v["engine_shutdown"][key] = value;
        assert!(e::verify_shutdown(&v, Some(&identity)).is_err());
    }
    for (key, value) in [("main_exited", json!(false)), ("retained", json!(true))] {
        let mut v = good.clone();
        v["engine_shutdown"]["shutdown"][key] = value;
        assert!(e::verify_shutdown(&v, Some(&identity)).is_err());
    }
    e::verify_shutdown(&json!({"ok":true}), None).unwrap();
}

#[test]
fn pointer_selection_matches_visible_action_at_minimum_and_wide_geometry() {
    use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
    for width in [42, 79, 100] {
        let mut v = view();
        let text = draw(&mut v, width, 24);
        let row: String = text
            .chars()
            .skip(usize::from(width) * 2)
            .take(usize::from(width))
            .collect();
        assert_eq!(
            row.chars()
                .skip(usize::from(width / 2) + 2)
                .take(4)
                .collect::<String>(),
            "Stop"
        );
        let event = |kind, column| MouseEvent {
            kind,
            column,
            row: 2,
            modifiers: KeyModifiers::NONE,
        };
        // An unmatched release cannot select Stop.
        v.mouse(event(MouseEventKind::Up(MouseButton::Left), width / 2 + 2));
        assert!(draw(&mut v, width, 24).contains("> Start"));
        v.mouse(event(
            MouseEventKind::Down(MouseButton::Left),
            width / 2 + 2,
        ));
        v.mouse(event(MouseEventKind::Up(MouseButton::Left), width / 2 + 2));
        assert!(draw(&mut v, width, 24).contains("> Stop"));
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none(), "unowned Stop remains disabled");
        v.mouse(event(MouseEventKind::Down(MouseButton::Left), 4));
        v.mouse(event(MouseEventKind::Up(MouseButton::Left), 4));
        draw(&mut v, width, 24);
        v.key(key(KeyCode::Enter));
        assert!(matches!(
            v.take_intent(),
            Some(Intent::Validate {
                action: Action::Start,
                ..
            })
        ));
    }
}

#[test]
fn held_input_recovery_is_available_without_prompt_admission() {
    use litetui_terminal::{demo::Demo, live::Observation};
    for admission in [Admission::ManagementOnly, Admission::RetainedRecovery] {
        let mut app = Demo::live("strata".into(), "engine-model".into());
        app.insert_text("KEEP UNSENT DRAFT");
        app.observe_live(Observation::EngineAdmission(admission))
            .unwrap();
        app.live.as_mut().unwrap().engine.close();
        app.open_queue();
        assert!(app.has_live_queue());
        assert_eq!(
            app.live.as_mut().unwrap().queue.take_intent(),
            Some(litetui_terminal::restored_queue::Intent::Open)
        );
        assert!(app.live.as_mut().unwrap().submit("no replay").is_err());
        assert_eq!(app.composer.text(), "KEEP UNSENT DRAFT");
    }
}

#[test]
fn peer_review_does_not_grant_a_different_seat_or_unconfirmed_process_start() {
    use sha2::{Digest, Sha256};
    let mut v = data();
    v["generation"] = json!(1);
    let row = |id: &str, pid: u32| json!({"agent_id":id,"name":"Local seat","backend":"strata","pid":pid,"born_us":1234});
    let mut peer = json!({"refused_action_id":ACTION_ID,"generation":1,"selection_id":v["launch"]["selection_id"],
        "current":row(AGENT,12),"peers":[row(SESSION,34)],"spawn_new_engine":true,"other_engine_ownership":"unverified","resource_checks":"required"});
    peer["sha256"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&peer).unwrap())
    ));
    v["local_peer_start"] = peer.clone();
    let snap = Snapshot::parse(&v, &identity()).unwrap();
    let mut validation = review_data(&snap, Action::Start);
    validation["local_peer_start"] = peer.clone();
    let reviewed = Review::parse(&validation, snap.clone(), Action::Start).unwrap();
    assert_eq!(
        reviewed.request().unwrap()["consent_scope"],
        "one_start_with_local_peers"
    );
    validation["local_peer_start"]["peers"][0]["born_us"] = json!(2345);
    assert!(Review::parse(&validation, snap, Action::Start).is_err());
    for (key, bad) in [
        ("resource_checks", json!("bypassed")),
        ("spawn_new_engine", json!(false)),
        ("sha256", json!("e".repeat(64))),
    ] {
        let mut altered = v.clone();
        altered["local_peer_start"][key] = bad;
        assert!(Snapshot::parse(&altered, &identity()).is_err());
    }
    let mut receipt = receipt_data();
    receipt["consumed_generation"] = json!(1);
    receipt["generation"] = json!(2);
    receipt["peer_sha256"] = peer["sha256"].clone();
    receipt["peer_accepted"] = json!(false);
    assert!(Receipt::parse(&receipt, reviewed.clone()).is_err());
    receipt["peer_accepted"] = json!(true);
    let accepted = Receipt::parse(&receipt, reviewed).unwrap();
    let mut read = readback_data();
    read["generation"] = json!(2);
    read["peer_sha256"] = peer["sha256"].clone();
    read["peer_accepted"] = json!(true);
    accepted.verify_readback(&read).unwrap();
    read["peer_sha256"] = json!("2".repeat(64));
    assert!(accepted.verify_readback(&read).is_err());
}

#[test]
fn reconnect_review_never_permits_remote_alias_loading_or_an_owned_process() {
    use sha2::{Digest, Sha256};
    let mut v = data();
    let mut target = json!({"endpoint":"http://127.0.0.1:33123","model":"engine-model","start_process":false,"load_model":false,"acquire_handle":false});
    target["selection_id"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(&target).unwrap())
    ));
    v["reconnect"] = target.clone();
    let snap = Snapshot::parse(&v, &identity()).unwrap();
    for (k, bad) in [
        ("start_process", json!(true)),
        ("load_model", json!(true)),
        ("acquire_handle", json!(true)),
        ("endpoint", json!("http://localhost@foreign.example:1234")),
    ] {
        let mut badsnap = v.clone();
        badsnap["reconnect"][k] = bad;
        assert!(Snapshot::parse(&badsnap, &identity()).is_err());
    }
    let mut rev = review_data(&snap, Action::Reconnect);
    rev["summary"] = target.clone();
    let reviewed = Review::parse(&rev, snap, Action::Reconnect).unwrap();
    let mut receipt = receipt_data();
    receipt["action"] = json!("reconnect");
    receipt["status"] = json!("connected");
    receipt["handle_token"] = Value::Null;
    receipt["spawn_attempted"] = json!(false);
    receipt["reconnect"] = target.clone();
    let accepted = Receipt::parse(&receipt, reviewed.clone()).unwrap();
    let mut read = readback_data();
    read["handle_token"] = Value::Null;
    read["reconnect"] = target.clone();
    accepted.verify_readback(&read).unwrap();
    read["reconnect"]["endpoint"] = json!("http://localhost:3333");
    assert!(accepted.verify_readback(&read).is_err());
    receipt["spawn_attempted"] = json!(true);
    assert!(Receipt::parse(&receipt, reviewed).is_err());
}

#[test]
fn engine_command_stop_never_falls_back_to_start_after_fresh_inspection() {
    use litetui_terminal::{command_palette as c, demo::Demo, live::Observation};
    for argument in ["stop", "STOP", "start", "status", ""] {
        let mut app = Demo::live("strata".into(), "engine-model".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("preserved draft");
        let raw = format!("/engine {argument}");
        app.live.as_mut().unwrap().commands.raw(&raw);
        app.live.as_mut().unwrap().commands.take_intent();
        app.observe_live(Observation::Commands(c::Update::Route {
            catalog: c::Catalog {
                session: SESSION.into(),
                agent: AGENT.into(),
                backend: "strata".into(),
                model: "engine-model".into(),
                snapshot: "a".repeat(64),
                entries: Vec::new(),
                help_text: None,
                key_table: None,
                skills_picker: None,
            },
            route: c::Route {
                row: "b".repeat(64),
                action: c::Action::Engine,
                arguments: argument.into(),
            },
        }))
        .unwrap();
        assert_eq!(
            app.live.as_mut().unwrap().engine.take_intent(),
            Some(Intent::Read)
        );
        app.observe_live(Observation::Engine(Update::Snapshot(Box::new(snapshot()))))
            .unwrap();
        assert!(app.live.as_mut().unwrap().engine.take_intent().is_none());
        let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
        terminal
            .draw(|frame| litetui_terminal::ui::draw(frame, &mut app))
            .unwrap();
        app.live
            .as_mut()
            .unwrap()
            .engine
            .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        let intent = app.live.as_mut().unwrap().engine.take_intent();
        if argument.eq_ignore_ascii_case("stop") {
            // This snapshot has no owned handle. A lost argument would validate Start.
            assert!(intent.is_none());
        } else {
            assert!(matches!(
                intent,
                Some(Intent::Validate {
                    action: Action::Start,
                    ..
                })
            ));
        }
        assert_eq!(app.composer.text(), "preserved draft");
    }
}
