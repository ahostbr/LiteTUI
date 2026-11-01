use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use litetui_terminal::{
    management::{Management, Step},
    work_protocol as p, work_view as v,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/work_inspection.json")).unwrap()
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
    let c=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",p::READ,p::PAGE,p::TAIL,p::CALENDAR,p::VALIDATE,p::APPLY,p::READBACK]})).unwrap());
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
fn read_view(s: &Value) -> v::View {
    let mut view = v::View::default();
    view.open();
    let intent = view.take_intent().unwrap();
    view.observe(v::Update {
        generation: intent.generation,
        data: v::Data::Snapshot(Box::new(snapshot(s))),
    });
    view
}
#[test]
fn source_fixture_parses_all_canonical_sections_tail_and_pause_receipts() {
    let f = fixture();
    let s = snapshot(&f["snapshot"]);
    for (key, section) in [
        ("tasks", p::Section::Tasks),
        ("jobs", p::Section::Jobs),
        ("loops", p::Section::Loops),
    ] {
        let page = p::Page::parse(&f[key], &s, section, 0, 32).unwrap();
        assert_eq!(page.rows.len(), 1);
    }
    let calendar = &f["calendar"];
    p::Calendar::parse(
        calendar,
        &s,
        calendar["month"].as_str().unwrap(),
        None,
        0,
        32,
    )
    .unwrap();
    let flow = &f["tail_flow"];
    let s = snapshot(&flow["snapshot"]);
    let mut page = s.request();
    page["kind"] = json!("work_page");
    page["section"] = json!("tasks");
    page["offset"] = json!(0);
    page["total"] = json!(1);
    page["next_offset"] = Value::Null;
    page["rows"] = json!([flow["task"]]);
    let row = p::Page::parse(&page, &s, p::Section::Tasks, 0, 32)
        .unwrap()
        .rows
        .remove(0);
    assert!(
        !p::Tail::parse(&flow["tail"], &s, &row)
            .unwrap()
            .text
            .is_empty()
    );
    let flow = &f["pause_flow"];
    let s = snapshot(&flow["snapshot"]);
    let review = p::PauseReview::parse(&flow["review"], &s).unwrap();
    p::PauseReceipt::parse(&flow["result"], &review)
        .unwrap()
        .verify(&flow["readback"], &review)
        .unwrap();
}
#[test]
fn unavailable_is_distinct_from_empty_and_malformed_scope_is_rejected() {
    let f = fixture();
    let raw = &f["snapshot"];
    let id = identity(raw);
    for (field, value) in [
        ("session_id", json!("wrong")),
        ("model", Value::Null),
        ("version", json!(true)),
        ("busy", json!(0)),
        ("calendar_month", json!("2026-13")),
    ] {
        let mut bad = raw.clone();
        bad[field] = value;
        assert!(p::Snapshot::parse(&bad, &id).is_err(), "{field}");
    }
    let s = snapshot(raw);
    assert!(!s.source(p::Section::ProviderTasks).available);
    let mut bad = raw.clone();
    bad["sources"]["provider_tasks"]["reason"] = Value::Null;
    assert!(p::Snapshot::parse(&bad, &id).is_err());
}
#[test]
fn pages_reject_wrong_selection_omissions_duplicates_and_boolean_counts() {
    let f = fixture();
    let s = snapshot(&f["snapshot"]);
    let raw = &f["tasks"];
    for (field, value) in [
        ("snapshot", json!("f".repeat(64))),
        ("offset", json!(1)),
        ("total", json!(true)),
        ("next_offset", json!(1)),
        ("rows", json!([])),
    ] {
        let mut bad = raw.clone();
        bad[field] = value;
        assert!(
            p::Page::parse(&bad, &s, p::Section::Tasks, 0, 32).is_err(),
            "{field}"
        );
    }
    let mut bad = raw.clone();
    bad["rows"] = json!([raw["rows"][0], raw["rows"][0]]);
    assert!(p::Page::parse(&bad, &s, p::Section::Tasks, 0, 32).is_err());
}
#[test]
fn calendar_validates_monday_grid_and_rejects_unicode_dates_without_panic() {
    let f = fixture();
    let s = snapshot(&f["snapshot"]);
    let raw = &f["calendar"];
    let month = raw["month"].as_str().unwrap();
    let mut bad = raw.clone();
    bad["days"].as_array_mut().unwrap().rotate_left(1);
    assert!(p::Calendar::parse(&bad, &s, month, None, 0, 32).is_err());
    let mut bad = raw.clone();
    let day = bad["days"]
        .as_array_mut()
        .unwrap()
        .iter_mut()
        .find(|d| !d["date"].is_null())
        .unwrap();
    day["date"] = json!("ééééé");
    assert!(p::Calendar::parse(&bad, &s, month, None, 0, 32).is_err());
    assert_eq!(p::shift_month("2024-12", true), "2025-01");
    assert_eq!(p::shift_month("0001-01", false), "0001-01");
}
#[test]
fn busy_chat_can_inspect_work_without_replacing_active_prompt() {
    let f = fixture();
    let s = &f["snapshot"];
    let mut m = connected(s);
    m.prompt("foreground-turn").unwrap();
    capture(&mut m, s);
    let c = request(
        &mut m,
        2,
        v::Request::Page {
            section: p::Section::Tasks,
            offset: 0,
        },
    );
    assert_eq!(c["type"], p::PAGE);
    assert!(!m.ready());
    assert!(matches!(
        reply(&mut m, &c, f["tasks"].clone()).unwrap(),
        Step::Work(v::Update {
            data: v::Data::Page(_),
            ..
        })
    ));
    assert!(m.ready());
}
#[test]
fn request_generation_and_selected_task_are_bound_to_the_capture() {
    let f = fixture();
    let mut m = connected(&f["snapshot"]);
    capture(&mut m, &f["snapshot"]);
    assert!(
        m.request_work(v::Intent {
            generation: 1,
            request: v::Request::Snapshot
        })
        .is_err()
    );
    let row = p::Page::parse(
        &f["tasks"],
        &snapshot(&f["snapshot"]),
        p::Section::Tasks,
        0,
        32,
    )
    .unwrap()
    .rows
    .remove(0);
    assert!(
        m.request_work(v::Intent {
            generation: 2,
            request: v::Request::Tail(row)
        })
        .is_err()
    );
}
fn pause_apply(m: &mut Management, flow: &Value) -> (Value, p::PauseReview) {
    capture(m, &flow["snapshot"]);
    let c = request(m, 2, v::Request::PauseValidate);
    let Step::Work(v::Update {
        data: v::Data::Review(review),
        ..
    }) = reply(m, &c, flow["review"].clone()).unwrap()
    else {
        panic!()
    };
    (
        request(m, 3, v::Request::PauseApply(review.clone())),
        *review,
    )
}
#[test]
fn pause_success_requires_exact_independent_readback_and_is_single_use() {
    let f = fixture();
    let flow = &f["pause_flow"];
    let mut m = connected(&flow["snapshot"]);
    let (c, r) = pause_apply(&mut m, flow);
    let read = write(reply(&mut m, &c, flow["result"].clone()).unwrap());
    assert_eq!(read["type"], p::READBACK);
    assert!(!m.ready());
    assert!(matches!(
        reply(&mut m, &read, flow["readback"].clone()).unwrap(),
        Step::Work(v::Update {
            generation: 3,
            data: v::Data::Paused
        })
    ));
    assert!(m.ready());
    assert!(
        m.request_work(v::Intent {
            generation: 4,
            request: v::Request::PauseApply(Box::new(r))
        })
        .is_err()
    );
}
#[test]
fn forged_pause_readback_and_changed_effects_fail_closed() {
    let f = fixture();
    let flow = &f["pause_flow"];
    for field in ["receipt", "session_id", "action_id"] {
        let mut m = connected(&flow["snapshot"]);
        let (c, _) = pause_apply(&mut m, flow);
        let read = write(reply(&mut m, &c, flow["result"].clone()).unwrap());
        let mut bad = flow["readback"].clone();
        bad[field] = json!("other");
        assert!(reply(&mut m, &read, bad).is_err());
        assert!(!m.ready());
    }
    let r = p::PauseReview::parse(&flow["review"], &snapshot(&flow["snapshot"])).unwrap();
    let mut bad = flow["result"].clone();
    bad["close_required"] = json!(true);
    assert!(p::PauseReceipt::parse(&bad, &r).is_err());
}
#[test]
fn closing_drains_each_phase_without_followup_or_success() {
    let f = fixture();
    let flow = &f["pause_flow"];
    for phase in 0..4 {
        let mut m = connected(&flow["snapshot"]);
        let mut c = request(&mut m, 1, v::Request::Snapshot);
        let mut result = flow["snapshot"].clone();
        if phase > 0 {
            reply(&mut m, &c, result).unwrap();
            c = request(&mut m, 2, v::Request::PauseValidate);
            result = flow["review"].clone();
        }
        if phase > 1 {
            let Step::Work(v::Update {
                data: v::Data::Review(r),
                ..
            }) = reply(&mut m, &c, result).unwrap()
            else {
                panic!()
            };
            c = request(&mut m, 3, v::Request::PauseApply(r));
            result = flow["result"].clone();
        }
        if phase > 2 {
            c = write(reply(&mut m, &c, result).unwrap());
            result = flow["readback"].clone();
        }
        m.begin_close();
        assert!(matches!(reply(&mut m, &c, result).unwrap(), Step::Ignore));
        assert!(!m.work_pending());
    }
}
#[test]
fn raw_error_envelope_budget_is_enforced_before_error_projection() {
    let f = fixture();
    let mut m = connected(&f["snapshot"]);
    let c = request(&mut m, 1, v::Request::Snapshot);
    let frame = json!({"type":"response","id":c["id"],"ok":false,"error":"not available"});
    assert!(m.accept_wire(frame, p::MAX_BYTES + 1).is_err());
    assert!(!m.ready());
}
#[test]
fn byte_shortened_pages_go_back_to_actual_prior_offsets() {
    let f = fixture();
    let s = &f["snapshot"];
    let mut view = read_view(s);
    let row = p::Page::parse(&f["tasks"], &snapshot(s), p::Section::Tasks, 0, 32)
        .unwrap()
        .rows
        .remove(0);
    for (offset, next) in [(0, Some(7)), (7, Some(12)), (12, None)] {
        let i = view.take_intent().unwrap();
        assert_eq!(
            i.request,
            v::Request::Page {
                section: p::Section::Tasks,
                offset
            }
        );
        view.observe(v::Update {
            generation: i.generation,
            data: v::Data::Page(p::Page {
                section: p::Section::Tasks,
                offset,
                total: 14,
                next,
                rows: vec![row.clone()],
            }),
        });
        draw(&mut view, 90, 28);
        if next.is_some() {
            key(&mut view, KeyCode::Char('n'));
        }
    }
    key(&mut view, KeyCode::Char('b'));
    assert_eq!(
        view.take_intent().unwrap().request,
        v::Request::Page {
            section: p::Section::Tasks,
            offset: 7
        }
    );
}
#[test]
fn stale_updates_and_hidden_confirmation_cannot_publish_an_action() {
    let f = fixture();
    let flow = &f["pause_flow"];
    let mut view = read_view(&flow["snapshot"]);
    let i = view.take_intent().unwrap();
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::Rejected("unavailable".into()),
    });
    draw(&mut view, 90, 28);
    key(&mut view, KeyCode::Char('p'));
    let i = view.take_intent().unwrap();
    let review = p::PauseReview::parse(&flow["review"], &snapshot(&flow["snapshot"])).unwrap();
    view.observe(v::Update {
        generation: i.generation - 1,
        data: v::Data::Review(Box::new(review.clone())),
    });
    assert!(view.waiting);
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::Review(Box::new(review)),
    });
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none(), "must draw before confirm");
    draw(&mut view, 90, 35);
    view.hide();
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    draw(&mut view, 90, 35);
    let mut repeat = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
    repeat.kind = KeyEventKind::Repeat;
    view.key(repeat);
    assert!(view.take_intent().is_none());
    key(&mut view, KeyCode::Enter);
    assert!(matches!(
        view.take_intent().unwrap().request,
        v::Request::PauseApply(_)
    ));
    view.close();
    assert!(view.waiting, "dispatched mutation must retain its wait");
}
#[test]
fn unavailable_details_and_shift_tab_are_reachable() {
    let f = fixture();
    let mut view = read_view(&f["snapshot"]);
    let i = view.take_intent().unwrap();
    let page = p::Page::parse(
        &f["tasks"],
        &snapshot(&f["snapshot"]),
        p::Section::Tasks,
        0,
        32,
    )
    .unwrap();
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::Page(page),
    });
    draw(&mut view, 90, 28);
    key(&mut view, KeyCode::Tab);
    assert!(view.take_intent().is_none());
    draw(&mut view, 90, 28);
    key(&mut view, KeyCode::Enter);
    assert!(draw(&mut view, 90, 28).contains("provider"));
    key(&mut view, KeyCode::Esc);
    draw(&mut view, 90, 28);
    view.key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT));
    assert!(matches!(
        view.take_intent().unwrap().request,
        v::Request::Page {
            section: p::Section::Tasks,
            ..
        }
    ));
}

#[test]
fn calendar_overview_omits_pagination_and_selected_day_includes_it() {
    let f = fixture();
    let mut m = connected(&f["snapshot"]);
    capture(&mut m, &f["snapshot"]);
    let month = f["calendar"]["month"].as_str().unwrap();
    let c = request(
        &mut m,
        2,
        v::Request::Calendar {
            month: month.into(),
            date: None,
            offset: 0,
        },
    );
    assert!(c.get("date").is_none() && c.get("offset").is_none() && c.get("limit").is_none());
    reply(&mut m, &c, f["calendar"].clone()).unwrap();
    let c = request(
        &mut m,
        3,
        v::Request::Calendar {
            month: month.into(),
            date: Some(format!("{month}-01")),
            offset: 0,
        },
    );
    assert_eq!(c["offset"], 0);
    assert_eq!(c["limit"], 32);
}

#[test]
fn closing_dispatched_read_keeps_ownership_until_validated_drain() {
    let mut view = v::View::default();
    view.open();
    let i = view.take_intent().unwrap();
    view.close();
    assert!(!view.open && view.owns_input() && view.waiting);
    view.observe(v::Update {
        generation: i.generation,
        data: v::Data::Rejected("completed refusal".into()),
    });
    assert!(!view.owns_input());
    assert!(!view.open);
    view.open();
    view.close();
    assert!(
        !view.owns_input(),
        "an unsent intent can be dropped locally"
    );
}

#[test]
fn source_absent_and_running_tails_never_become_empty_success() {
    let f = fixture();
    for (name, status) in [
        ("running_tail_flow", "running"),
        ("absent_tail_flow", "absent"),
    ] {
        let flow = &f[name];
        let s = snapshot(&flow["snapshot"]);
        let mut page = s.request();
        page["kind"] = json!("work_page");
        page["section"] = json!("tasks");
        page["offset"] = json!(0);
        page["total"] = json!(1);
        page["next_offset"] = Value::Null;
        page["rows"] = json!([flow["task"]]);
        let row = p::Page::parse(&page, &s, p::Section::Tasks, 0, 32)
            .unwrap()
            .rows
            .remove(0);
        let tail = p::Tail::parse(&flow["tail"], &s, &row).unwrap();
        assert_eq!(tail.status, status);
        assert!(tail.text.is_empty() && tail.reason.is_some());
        let mut bad = flow["tail"].clone();
        bad["text"] = json!("invented output");
        assert!(p::Tail::parse(&bad, &s, &row).is_err());
    }
}

#[test]
fn scheduler_hold_is_visible_and_bounded_without_changing_schedule_state() {
    let fixture = fixture();
    let mut raw = fixture["snapshot"].clone();
    let notice = "Scheduled job held: new conversation handoff is not available yet.";
    raw["scheduler_notice"] = json!(notice);
    let captured = snapshot(&raw);
    assert_eq!(captured.scheduler_notice.as_deref(), Some(notice));
    assert!(!captured.paused);
    let mut view = read_view(&raw);
    assert!(draw(&mut view, 110, 36).contains("Schedule held:"));
    assert!(draw(&mut view, 110, 36).contains(notice));
    assert!(view.take_intent().is_some()); // Ordinary page read remains available.
    for invalid in [json!(false), json!(1), json!(""), json!("x".repeat(2049))] {
        raw["scheduler_notice"] = invalid;
        assert!(p::Snapshot::parse(&raw, &identity(&raw)).is_err());
    }
    raw["scheduler_notice"] = Value::Null;
    assert!(snapshot(&raw).scheduler_notice.is_none());
    raw.as_object_mut().unwrap().remove("scheduler_notice");
    assert!(snapshot(&raw).scheduler_notice.is_none());
}
