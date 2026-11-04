use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    management::{Management, Step},
    work_controls as c, work_protocol as p, work_view as v,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn fixtures() -> Value {
    serde_json::from_str(include_str!("fixtures/work_controls.json")).unwrap()
}
fn snapshot(f: &Value) -> p::Snapshot {
    let s = &f["snapshot"];
    p::Snapshot::parse(
        s,
        &p::Identity {
            session: s["session_id"].as_str().unwrap().into(),
            agent: s["agent_id"].as_str().unwrap().into(),
            backend: s["backend"].as_str().unwrap().into(),
            model: s["model"].as_str().unwrap().into(),
        },
    )
    .unwrap()
}
fn page(s: &p::Snapshot, section: p::Section, target: Option<&Value>) -> Value {
    let mut p = s.request();
    p["kind"] = json!("work_page");
    p["section"] = json!(section.key());
    p["offset"] = json!(0);
    p["total"] = json!(usize::from(target.is_some()));
    p["next_offset"] = Value::Null;
    p["rows"] = json!(target.into_iter().collect::<Vec<_>>());
    p
}
fn action(f: &Value) -> c::Action {
    match f["review"]["action"].as_str().unwrap() {
        "job_create" => c::Action::JobCreate,
        "job_update" => c::Action::JobUpdate,
        "job_delete" => c::Action::JobDelete,
        "loop_enable" => c::Action::LoopEnable,
        "goal_pause" => c::Action::GoalPause,
        _ => c::Action::GoalClear,
    }
}
fn write(s: Step) -> Value {
    if let Step::Write(c) = s {
        c
    } else {
        panic!("{s:?}")
    }
}
fn reply(m: &mut Management, c: &Value, v: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":v}))
}
fn request(m: &mut Management, generation: u64, request: v::Request) -> Value {
    m.request_work(v::Intent {
        generation,
        request,
    })
    .unwrap()
}
fn connected(f: &Value) -> Management {
    let s = snapshot(f);
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":s.identity.session,"backend":s.identity.backend,"model":s.identity.model,"tool_profile":"strict"})).unwrap();
    let state=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",p::READ,p::PAGE,c::VALIDATE,c::APPLY,c::READBACK]})).unwrap());
    assert!(matches!(reply(&mut m,&state,json!({"session_id":s.identity.session,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":s.identity.backend,"model":s.identity.model},"ownership":{"data_version":1,"writable":true,"agent_id":s.identity.agent},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]})).unwrap(),Step::Initialized(_)));
    let read = request(&mut m, 1, v::Request::Snapshot);
    reply(&mut m, &read, f["snapshot"].clone()).unwrap();
    m
}
fn selected(f: &Value) -> Option<p::Row> {
    let target = f.get("target")?;
    let s = snapshot(f);
    let section = if target["kind"] == "loop" {
        p::Section::Loops
    } else {
        p::Section::Jobs
    };
    Some(
        p::Page::parse(&page(&s, section, Some(target)), &s, section, 0, 32)
            .unwrap()
            .rows
            .remove(0),
    )
}
fn validate(m: &mut Management, f: &Value) -> (Value, c::Review) {
    let row = selected(f);
    if let Some(target) = f.get("target") {
        let s = snapshot(f);
        let section = if target["kind"] == "loop" {
            p::Section::Loops
        } else {
            p::Section::Jobs
        };
        let read = request(m, 2, v::Request::Page { section, offset: 0 });
        reply(m, &read, page(&s, section, Some(target))).unwrap();
    }
    let edit = c::Edit::new(
        &snapshot(f),
        action(f),
        row.as_ref(),
        f["review"]["draft"].clone(),
    )
    .unwrap();
    let cmd = request(m, 3, v::Request::ControlValidate(Box::new(edit)));
    let Step::Work(v::Update {
        data: v::Data::ControlReview(review),
        ..
    }) = reply(m, &cmd, f["review"].clone()).unwrap()
    else {
        panic!()
    };
    (
        request(m, 4, v::Request::ControlApply(review.clone())),
        *review,
    )
}
#[test]
fn every_source_control_flow_requires_its_exact_independent_readback() {
    for (name, f) in fixtures().as_object().unwrap() {
        let mut m = connected(f);
        let (apply, review) = validate(&mut m, f);
        let check = write(reply(&mut m, &apply, f["result"].clone()).unwrap());
        assert_eq!(check["type"], c::READBACK, "{name}");
        assert!(!m.ready());
        let Step::Work(v::Update {
            data: v::Data::ControlSaved { row, .. },
            ..
        }) = reply(&mut m, &check, f["readback"].clone()).unwrap()
        else {
            panic!("{name}")
        };
        assert_eq!(row, f["readback"]["row"]);
        assert!(m.ready());
        assert!(
            m.request_work(v::Intent {
                generation: 5,
                request: v::Request::ControlApply(Box::new(review))
            })
            .is_err()
        );
    }
}
#[test]
fn forged_readback_and_missing_apply_proof_never_publish_saved() {
    let f = &fixtures()["jobs_job_update_off"];
    for mutate in [false, true] {
        let mut m = connected(f);
        let (apply, _) = validate(&mut m, f);
        if !mutate {
            assert!(
                m.accept(
                    json!({"type":"response","id":apply["id"],"ok":false,"error":"write uncertain"})
                )
                .is_err()
            );
        } else {
            let check = write(reply(&mut m, &apply, f["result"].clone()).unwrap());
            let mut bad = f["readback"].clone();
            bad["row"]["prompt"] = json!("not reviewed");
            assert!(reply(&mut m, &check, bad).is_err());
        }
    }
}
#[test]
fn closing_drains_control_result_without_readback_or_success() {
    let f = &fixtures()["goal_clear"];
    let mut m = connected(f);
    let (apply, _) = validate(&mut m, f);
    m.begin_close();
    assert!(matches!(
        reply(&mut m, &apply, f["result"].clone()).unwrap(),
        Step::Ignore
    ));
}
fn draw(view: &mut v::View) -> String {
    let mut t = Terminal::new(TestBackend::new(90, 32)).unwrap();
    let app = litetui_terminal::demo::Demo::live("custom".into(), "fixture".into());
    t.draw(|f| view.draw(f, f.area(), app.palette())).unwrap();
    t.backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn key(view: &mut v::View, code: KeyCode) {
    view.key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn jobs_view(f: &Value) -> v::View {
    let s = snapshot(f);
    let mut view = v::View::default();
    view.open();
    let intent = view.take_intent().unwrap();
    view.observe(v::Update {
        generation: intent.generation,
        data: v::Data::Snapshot(Box::new(s.clone())),
    });
    for index in 0..3 {
        if let Some(intent) = view.take_intent() {
            let v::Request::Page { section, .. } = intent.request else {
                panic!()
            };
            let target = if index == 2 { f.get("target") } else { None };
            let p = p::Page::parse(&page(&s, section, target), &s, section, 0, 32).unwrap();
            view.observe(v::Update {
                generation: intent.generation,
                data: v::Data::Page(p),
            });
        }
        draw(&mut view);
        if index < 2 {
            key(&mut view, KeyCode::Tab);
        }
    }
    view
}
#[test]
fn create_form_preserves_multiline_prompt_and_starts_disabled() {
    let f = &fixtures()["cron_create"];
    let mut view = jobs_view(f);
    key(&mut view, KeyCode::Char('c'));
    draw(&mut view);
    view.paste("first\nsecond Δ");
    key(&mut view, KeyCode::Tab);
    view.paste("label");
    key(&mut view, KeyCode::Tab);
    view.paste("* * * * *");
    key(&mut view, KeyCode::Enter);
    let v::Request::ControlValidate(edit) = view.take_intent().unwrap().request else {
        panic!()
    };
    assert_eq!(edit.action, c::Action::JobCreate);
    assert_eq!(edit.draft["prompt"], "first\nsecond Δ");
    assert_eq!(edit.draft["enabled"], false);
    assert_eq!(edit.draft["new_conversation"], false);
}
#[test]
fn delete_review_requires_all_visible_pages_and_can_only_dispatch_once() {
    let f = &fixtures()["jobs_job_delete_off"];
    let mut view = jobs_view(f);
    key(&mut view, KeyCode::Char('d'));
    let intent = view.take_intent().unwrap();
    let v::Request::ControlValidate(edit) = intent.request else {
        panic!()
    };
    let review = c::Review::parse(&f["review"], &edit).unwrap();
    view.observe(v::Update {
        generation: intent.generation,
        data: v::Data::ControlReview(Box::new(review)),
    });
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    let mut sent = false;
    for _ in 0..200 {
        draw(&mut view);
        key(&mut view, KeyCode::Enter);
        if let Some(intent) = view.take_intent() {
            assert!(matches!(intent.request, v::Request::ControlApply(_)));
            sent = true;
            break;
        }
    }
    assert!(sent);
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
}

#[test]
fn fresh_inspection_invalidates_an_older_durable_review() {
    let f = &fixtures()["cron_create"];
    let mut m = connected(f);
    let edit = c::Edit::new(
        &snapshot(f),
        c::Action::JobCreate,
        None,
        f["review"]["draft"].clone(),
    )
    .unwrap();
    let cmd = request(&mut m, 2, v::Request::ControlValidate(Box::new(edit)));
    let Step::Work(v::Update {
        data: v::Data::ControlReview(review),
        ..
    }) = reply(&mut m, &cmd, f["review"].clone()).unwrap()
    else {
        panic!()
    };
    let refresh = request(&mut m, 3, v::Request::Snapshot);
    reply(&mut m, &refresh, f["snapshot"].clone()).unwrap();
    assert!(
        m.request_work(v::Intent {
            generation: 4,
            request: v::Request::ControlApply(review)
        })
        .is_err()
    );
}
