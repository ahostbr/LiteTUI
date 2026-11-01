use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use litetui_terminal::{
    management::{Management, Step},
    work_protocol as p, work_view as v,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/work_task_stop.json")).unwrap()
}
fn identity(s: &Value) -> p::Identity {
    p::Identity {
        session: s["session_id"].as_str().unwrap().into(),
        agent: s["agent_id"].as_str().unwrap().into(),
        backend: s["backend"].as_str().unwrap().into(),
        model: s["model"].as_str().unwrap().into(),
    }
}
fn snapshot(raw: &Value) -> p::Snapshot {
    p::Snapshot::parse(raw, &identity(raw)).unwrap()
}
fn state(s: &Value) -> Value {
    json!({"session_id":s["session_id"],"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":s["backend"],"model":s["model"]},"ownership":{"data_version":1,"writable":true,"agent_id":s["agent_id"]},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]})
}
fn reply(m: &mut Management, c: &Value, result: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":result}))
}
fn write(step: Step) -> Value {
    let Step::Write(c) = step else {
        panic!("{step:?}")
    };
    c
}
fn connected(s: &Value) -> Management {
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":s["session_id"],"backend":s["backend"],"model":s["model"],"tool_profile":"strict"})).unwrap();
    let c=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",p::READ,p::PAGE,p::TAIL,p::CALENDAR,p::VALIDATE,p::APPLY,p::READBACK,p::STOP_VALIDATE,p::STOP_APPLY,p::STOP_SETTLED]})).unwrap());
    assert!(matches!(
        reply(&mut m, &c, state(s)).unwrap(),
        Step::Initialized(_)
    ));
    m
}
fn request(m: &mut Management, generation: u64, request: v::Request) -> Value {
    m.request_work(v::Intent {
        generation,
        request,
    })
    .unwrap()
}
fn capture(m: &mut Management, s: &Value) {
    let c = request(m, 1, v::Request::Snapshot);
    assert!(matches!(
        reply(m, &c, s.clone()).unwrap(),
        Step::Work(v::Update {
            data: v::Data::Snapshot(_),
            ..
        })
    ));
}
fn draw(view: &mut v::View, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    let app = litetui_terminal::demo::Demo::live("custom".into(), "fixture".into());
    let theme = app.palette().clone();
    terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn key(view: &mut v::View, code: KeyCode) {
    view.key(KeyEvent::new(code, KeyModifiers::NONE));
}

fn page(flow: &Value) -> Value {
    let s = snapshot(&flow["snapshot"]);
    let mut value = s.request();
    value["kind"] = json!("work_page");
    value["section"] = json!("tasks");
    value["offset"] = json!(0);
    value["total"] = json!(1);
    value["next_offset"] = Value::Null;
    value["rows"] = json!([flow["row"]]);
    value
}
fn row(flow: &Value) -> p::Row {
    p::Page::parse(
        &page(flow),
        &snapshot(&flow["snapshot"]),
        p::Section::Tasks,
        0,
        32,
    )
    .unwrap()
    .rows
    .remove(0)
}
fn review(flow: &Value) -> p::StopReview {
    p::StopReview::parse(&flow["review"], &snapshot(&flow["snapshot"]), &row(flow)).unwrap()
}
fn selected(flow: &Value) -> Management {
    let mut manager = connected(&flow["snapshot"]);
    capture(&mut manager, &flow["snapshot"]);
    let command = request(
        &mut manager,
        2,
        v::Request::Page {
            section: p::Section::Tasks,
            offset: 0,
        },
    );
    reply(&mut manager, &command, page(flow)).unwrap();
    manager
}
fn reviewed(manager: &mut Management, flow: &Value) -> Box<p::StopReview> {
    let command = request(manager, 3, v::Request::StopValidate(row(flow)));
    assert_eq!(command["type"], p::STOP_VALIDATE);
    assert_eq!(command["task_revision"], flow["row"]["revision"]);
    let Step::Work(v::Update {
        data: v::Data::StopReview(review),
        ..
    }) = reply(manager, &command, flow["review"].clone()).unwrap()
    else {
        panic!()
    };
    review
}
fn stopped(flow: &Value) -> p::StopReceipt {
    let p::StopResult::Stopped(receipt) =
        p::StopResult::parse(&flow["result"], &review(flow)).unwrap()
    else {
        panic!()
    };
    *receipt
}
#[test]
fn actual_source_stop_requires_complete_proof_and_independent_terminal_readback() {
    let f = fixture();
    let flow = &f["success"];
    let review = review(flow);
    let receipt = stopped(flow);
    receipt.verify(&flow["settled"], &review).unwrap();
    assert_eq!(review.request()["task_revision"], flow["row"]["revision"]);
    assert!(review.request().get("kind").is_none());
    assert_eq!(receipt.row.status, "killed");
    let mut later = flow["settled"].clone();
    later["store_revision"] = json!("f".repeat(64));
    receipt.verify(&later, &review).unwrap(); // Another task may update the store.
}
#[test]
fn stop_review_rejects_changed_owner_process_scope_and_unknown_fields() {
    let f = fixture();
    let flow = &f["success"];
    let s = snapshot(&flow["snapshot"]);
    let row = row(flow);
    for (key, value) in [
        ("task_id", json!("other")),
        ("task_revision", json!("f".repeat(64))),
        ("owner_pid", json!(true)),
        ("owner_instance", json!("other")),
        ("owner_created", Value::Null),
        ("scope", json!("runtime")),
        ("consequence", json!("Stop all tasks")),
        ("process_id", json!(5)),
        ("stop_mode", json!("unknown")),
        ("producer_id", json!("not-uuid")),
        ("worker_id", json!("not-uuid")),
        ("lifecycle_instance", json!("wrong")),
        ("extra", json!(true)),
    ] {
        let mut raw = flow["review"].clone();
        raw[key] = value;
        assert!(p::StopReview::parse(&raw, &s, &row).is_err(), "{key}");
    }
    for (owner, status) in [
        ("other", "running"),
        ("unknown", "running"),
        ("current", "done"),
    ] {
        let mut foreign = row.clone();
        foreign.owner = owner.into();
        foreign.status = status.into();
        assert!(p::StopReview::parse(&flow["review"], &s, &foreign).is_err());
    }
}
#[test]
fn every_retirement_fact_is_required_and_boolean_typed() {
    let f = fixture();
    let flow = &f["success"];
    let r = review(flow);
    for key in [
        "worker_retired",
        "callbacks_retired",
        "process_retired",
        "pending_spawn_settled",
        "task_persisted",
    ] {
        for value in [json!(false), json!(1), Value::Null] {
            let mut raw = flow["result"].clone();
            raw["proof"][key] = value;
            assert!(p::StopResult::parse(&raw, &r).is_err(), "{key}");
        }
    }
    for (key, value) in [
        ("producer_id", json!("foreign")),
        ("process_state", json!("running")),
        ("process_id", json!(3)),
    ] {
        let mut raw = flow["result"].clone();
        raw["proof"][key] = value;
        assert!(p::StopResult::parse(&raw, &r).is_err(), "{key}");
    }
    let mut raw = flow["result"].clone();
    raw["row"]["row"]["owner_pid"] = json!(99);
    assert!(p::StopResult::parse(&raw, &r).is_err());
    raw = flow["result"].clone();
    raw["row"]["row"]["ended"] = Value::Null;
    assert!(p::StopResult::parse(&raw, &r).is_err());
}
#[test]
fn known_refusal_and_uncertain_effects_remain_distinct() {
    let f = fixture();
    let flow = &f["success"];
    let r = review(flow);
    for status in ["refused", "uncertain"] {
        let mut raw = flow["result"].clone();
        raw["status"] = json!(status);
        raw["mutated"] = json!(status == "uncertain");
        raw["close_required"] = json!(status == "uncertain");
        raw["error"] = json!("Controlled failure");
        for key in ["row", "proof", "store_revision"] {
            raw[key] = Value::Null;
        }
        let parsed = p::StopResult::parse(&raw, &r).unwrap();
        assert_eq!(
            matches!(parsed, p::StopResult::Uncertain(_)),
            status == "uncertain"
        );
        raw["mutated"] = json!(status != "uncertain");
        assert!(p::StopResult::parse(&raw, &r).is_err());
    }
}
#[test]
fn stop_settlement_cannot_switch_receipt_row_or_worker() {
    let f = fixture();
    let flow = &f["success"];
    let r = review(flow);
    let receipt = stopped(flow);
    for key in [
        "review",
        "action_id",
        "receipt",
        "task_id",
        "task_revision",
        "instance",
        "snapshot",
    ] {
        let mut raw = flow["settled"].clone();
        raw[key] = json!("other");
        assert!(receipt.verify(&raw, &r).is_err(), "{key}");
    }
    let mut raw = flow["settled"].clone();
    raw["row"]["row"]["ok"] = json!(false);
    assert!(receipt.verify(&raw, &r).is_err());
    raw = flow["settled"].clone();
    raw["proof"]["worker_retired"] = json!(false);
    assert!(receipt.verify(&raw, &r).is_err());
}
#[test]
fn manager_waits_for_independent_stop_readback_then_invalidates_old_capture() {
    let f = fixture();
    let flow = &f["success"];
    let mut m = selected(flow);
    let r = reviewed(&mut m, flow);
    let c = request(&mut m, 4, v::Request::StopApply(r.clone()));
    assert_eq!(c["type"], p::STOP_APPLY);
    let mut echoed = c.clone();
    echoed.as_object_mut().unwrap().remove("type");
    echoed.as_object_mut().unwrap().remove("id");
    assert_eq!(echoed, r.request());
    let read = write(reply(&mut m, &c, flow["result"].clone()).unwrap());
    assert_eq!(read["type"], p::STOP_SETTLED);
    assert!(!m.ready());
    let Step::Work(v::Update {
        data: v::Data::Stopped(row),
        ..
    }) = reply(&mut m, &read, flow["settled"].clone()).unwrap()
    else {
        panic!()
    };
    assert_eq!(row.status, "killed");
    assert!(m.ready());
    assert!(
        m.request_work(v::Intent {
            generation: 5,
            request: v::Request::StopValidate(row)
        })
        .is_err()
    );
}
#[test]
fn closing_drains_stop_review_apply_and_readback_without_success_or_followup() {
    let f = fixture();
    let flow = &f["success"];
    for phase in 0..3 {
        let mut m = selected(flow);
        let mut c = request(&mut m, 3, v::Request::StopValidate(row(flow)));
        let mut result = flow["review"].clone();
        if phase > 0 {
            let Step::Work(v::Update {
                data: v::Data::StopReview(r),
                ..
            }) = reply(&mut m, &c, result).unwrap()
            else {
                panic!()
            };
            c = request(&mut m, 4, v::Request::StopApply(r));
            result = flow["result"].clone();
        }
        if phase > 1 {
            c = write(reply(&mut m, &c, result).unwrap());
            result = flow["settled"].clone();
        }
        m.begin_close();
        assert!(matches!(reply(&mut m, &c, result).unwrap(), Step::Ignore));
        assert!(!m.work_pending());
    }
}
#[test]
fn failed_stop_readback_keeps_management_closed() {
    let f = fixture();
    let flow = &f["success"];
    let mut m = selected(flow);
    let r = reviewed(&mut m, flow);
    let c = request(&mut m, 4, v::Request::StopApply(r));
    let read = write(reply(&mut m, &c, flow["result"].clone()).unwrap());
    let mut bad = flow["settled"].clone();
    bad["proof"]["process_retired"] = json!(false);
    assert!(reply(&mut m, &read, bad).is_err());
    assert!(!m.ready());
}
#[test]
fn visible_complete_stop_review_confirms_once_and_preserves_wait_after_close() {
    let f = fixture();
    let flow = &f["success"];
    let mut view = v::View::default();
    view.open();
    let i = view.take_intent().unwrap();
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::Snapshot(Box::new(snapshot(&flow["snapshot"]))),
    });
    let i = view.take_intent().unwrap();
    let page = p::Page::parse(
        &page(flow),
        &snapshot(&flow["snapshot"]),
        p::Section::Tasks,
        0,
        32,
    )
    .unwrap();
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::Page(page),
    });
    key(&mut view, KeyCode::Char('k'));
    assert!(view.take_intent().is_none());
    draw(&mut view, 65, 18);
    key(&mut view, KeyCode::Char('k'));
    let i = view.take_intent().unwrap();
    assert!(matches!(i.request, v::Request::StopValidate(_)));
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::StopReview(Box::new(review(flow))),
    });
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    let mut seen = String::new();
    let mut action = None;
    for _ in 0..100 {
        seen.push_str(&draw(&mut view, 65, 18));
        let mut repeat = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        repeat.kind = KeyEventKind::Repeat;
        view.key(repeat);
        assert!(view.take_intent().is_none());
        key(&mut view, KeyCode::Enter);
        if let Some(intent) = view.take_intent() {
            action = Some(intent);
            break;
        }
    }
    assert!(seen.contains("controlled pending spawn") && seen.contains("lifecycle_instance"));
    let action = action.expect("complete bounded review");
    assert!(matches!(action.request, v::Request::StopApply(_)));
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    view.close();
    assert!(view.waiting);
    view.observe(v::Update {
        generation: action.generation,
        data: v::Data::Stopped(stopped(flow).row),
    });
    assert!(!view.waiting && !view.open);
}

#[test]
fn all_source_stop_flows_preserve_real_process_and_failure_semantics() {
    let f = fixture();
    for (name, expected) in [
        ("success", "stopped"),
        ("process", "stopped"),
        ("late_spawn", "stopped"),
        ("refused", "refused"),
        ("uncertain", "uncertain"),
        ("unconfirmed_kill", "uncertain"),
        ("quit", "stopped"),
    ] {
        let flow = &f[name];
        let r = review(flow);
        let result = p::StopResult::parse(&flow["result"], &r)
            .unwrap_or_else(|error| panic!("{name}: {error}"));
        match result {
            p::StopResult::Stopped(receipt) => {
                assert_eq!(expected, "stopped", "{name}");
                if let Some(settled) = flow.get("settled") {
                    receipt.verify(settled, &r).unwrap();
                } else {
                    assert_eq!(name, "quit"); // Quit drains but never publishes another success.
                }
            }
            p::StopResult::Refused(_) => assert_eq!(expected, "refused", "{name}"),
            p::StopResult::Uncertain(_) => assert_eq!(expected, "uncertain", "{name}"),
        }
    }
    assert_eq!(f["process"]["review"]["stop_mode"], "process");
    assert_eq!(f["late_spawn"]["review"]["stop_mode"], "pending_spawn");
    assert_eq!(
        f["late_spawn"]["result"]["proof"]["process_state"],
        "retired"
    );
}
#[test]
fn changed_process_identity_or_unverified_pending_spawn_cannot_report_stopped() {
    let f = fixture();
    for name in ["process", "late_spawn"] {
        let flow = &f[name];
        let r = review(flow);
        let mut result = flow["result"].clone();
        result["proof"]["process_state"] = json!("not_spawned");
        assert!(p::StopResult::parse(&result, &r).is_err());
    }
    let flow = &f["process"];
    let r = review(flow);
    let mut result = flow["result"].clone();
    result["proof"]["process_id"] = json!(flow["review"]["process_id"].as_u64().unwrap() + 1);
    assert!(p::StopResult::parse(&result, &r).is_err());
}
#[test]
fn actual_source_refusal_is_visible_but_uncertain_stop_closes_management() {
    let f = fixture();
    for name in ["refused", "uncertain", "unconfirmed_kill"] {
        let flow = &f[name];
        let mut m = selected(flow);
        let r = reviewed(&mut m, flow);
        let command = request(&mut m, 4, v::Request::StopApply(r));
        let result = reply(&mut m, &command, flow["result"].clone());
        if name == "refused" {
            assert!(matches!(
                result.unwrap(),
                Step::Work(v::Update {
                    data: v::Data::Rejected(_),
                    ..
                })
            ));
            assert!(m.ready());
        } else {
            assert!(result.is_err());
            assert!(!m.ready());
        }
    }
}
