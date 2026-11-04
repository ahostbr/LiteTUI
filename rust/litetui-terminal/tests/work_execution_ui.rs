use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    demo::Demo, live::Observation, work_execution_controls as x, work_lifecycle as life,
    work_protocol as p, work_view as v,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
fn fixture(name: &str) -> Value {
    let all: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/work_execution_controls.json"
    ))
    .unwrap();
    all["cases"]
        .as_array()
        .unwrap()
        .iter()
        .find(|f| f["name"] == name)
        .unwrap()
        .clone()
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
fn paint(app: &mut Demo) {
    Terminal::new(TestBackend::new(70, 25))
        .unwrap()
        .draw(|frame| litetui_terminal::ui::draw(frame, app))
        .unwrap();
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .work
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn reads(app: &mut Demo, f: &Value) {
    loop {
        let Some(intent) = app.live.as_mut().unwrap().work.take_intent() else {
            break;
        };
        let s = snapshot(f);
        let data = match intent.request {
            v::Request::Snapshot => v::Data::Snapshot(Box::new(s)),
            v::Request::Page { section, offset } => {
                let mut raw = s.request();
                let rows = &f["selected"][section.key()];
                raw["kind"] = json!("work_page");
                raw["section"] = json!(section.key());
                raw["offset"] = json!(offset);
                raw["total"] = json!(rows.as_array().unwrap().len());
                raw["next_offset"] = Value::Null;
                raw["rows"] = rows.clone();
                v::Data::Page(p::Page::parse(&raw, &s, section, offset, 32).unwrap())
            }
            other => panic!("Unexpected read {other:?}"),
        };
        app.observe_live(Observation::Work(v::Update {
            generation: intent.generation,
            data,
        }))
        .unwrap();
    }
}
fn setup(f: &Value) -> Demo {
    let s = snapshot(f);
    let mut app = Demo::live(s.identity.backend, s.identity.model);
    app.observe_live(Observation::Ready).unwrap();
    app.composer.insert("main draft α 雪");
    app.open_work();
    reads(&mut app, f);
    app
}
fn category(app: &mut Demo, f: &Value, index: usize) {
    for _ in 0..index {
        paint(app);
        key(app, KeyCode::Tab);
        reads(app, f);
    }
    paint(app);
}
fn review(app: &mut Demo, f: &Value) -> x::Review {
    let intent = app.live.as_mut().unwrap().work.take_intent().unwrap();
    let v::Request::ExecutionValidate(edit) = intent.request else {
        panic!("Expected explicit execution validation")
    };
    let r = x::Review::parse(
        &f["review"],
        &edit,
        f["capture"]["instance"].as_str().unwrap(),
        life::Provider::Host,
    )
    .unwrap();
    app.observe_live(Observation::Work(v::Update {
        generation: intent.generation,
        data: v::Data::ExecutionReview(Box::new(r.clone())),
    }))
    .unwrap();
    r
}
fn confirm(app: &mut Demo) -> v::Intent {
    for _ in 0..2000 {
        paint(app);
        key(app, KeyCode::Enter);
        if let Some(i) = app.live.as_mut().unwrap().work.take_intent() {
            return i;
        }
    }
    panic!("Full review did not finish");
}
fn goal_apply() -> (Demo, x::Review, v::Intent) {
    let f = fixture("goal_start-cron");
    let mut app = setup(&f);
    category(&mut app, &f, 4);
    key(&mut app, KeyCode::Char('n'));
    paint(&mut app);
    app.live
        .as_mut()
        .unwrap()
        .work
        .paste(f["review"]["draft"]["objective"].as_str().unwrap());
    key(&mut app, KeyCode::Enter);
    let r = review(&mut app, &f);
    let intent = confirm(&mut app);
    (app, r, intent)
}
#[test]
fn objective_form_is_literal_lossless_and_review_requires_visible_pages() {
    let f = fixture("goal_start-cron");
    let mut app = setup(&f);
    let before = app.composer.text().to_owned();
    category(&mut app, &f, 4);
    key(&mut app, KeyCode::Char('n'));
    paint(&mut app);
    let literal = "pause\r\n  resume α 雪  ";
    app.live.as_mut().unwrap().work.paste(literal);
    key(&mut app, KeyCode::Enter);
    let i = app.live.as_mut().unwrap().work.take_intent().unwrap();
    let v::Request::ExecutionValidate(edit) = i.request else {
        panic!()
    };
    assert_eq!(edit.action, x::Action::GoalStart);
    assert_eq!(edit.draft, json!({"objective":literal}));
    assert_eq!(app.composer.text(), before);
    assert!(app.live.as_ref().unwrap().rows.is_empty());
    // A separate actual-source review proves confirmation is paint gated.
    let (mut app, r, _) = goal_apply();
    let f = fixture("goal_start-cron");
    let mut fresh = setup(&f);
    category(&mut fresh, &f, 4);
    key(&mut fresh, KeyCode::Char('n'));
    paint(&mut fresh);
    fresh
        .live
        .as_mut()
        .unwrap()
        .work
        .paste(r.edit.draft["objective"].as_str().unwrap());
    key(&mut fresh, KeyCode::Enter);
    review(&mut fresh, &f);
    key(&mut fresh, KeyCode::Enter);
    assert!(fresh.live.as_mut().unwrap().work.take_intent().is_none());
    paint(&mut fresh);
    key(&mut fresh, KeyCode::Esc);
    paint(&mut fresh);
    key(&mut fresh, KeyCode::Enter);
    let i = fresh.live.as_mut().unwrap().work.take_intent().unwrap();
    let v::Request::ExecutionValidate(edit) = i.request else {
        panic!()
    };
    assert_eq!(edit.draft, r.edit.draft);
    app.live.as_mut().unwrap().begin_quit();
    assert_eq!(fresh.composer.text(), "main draft α 雪");
}
#[test]
fn local_stop_wins_against_already_inflight_launch_release() {
    let (mut app, r, intent) = goal_apply();
    let before = app.composer.text().to_owned();
    let chat = app.live.as_mut().unwrap();
    chat.prepare_work(&intent).unwrap();
    assert!(chat.execution_pending() && chat.ready && !chat.busy);
    assert!(chat.rows.is_empty());
    chat.request_stop().unwrap();
    assert_eq!(chat.take_execution_stop(), Some(r.action_id.clone()));
    assert!(chat.take_execution_stop().is_none());
    assert!(chat.take_abort().is_none());
    let update = v::Update {
        generation: intent.generation,
        data: v::Data::ExecutionVerified {
            action: x::Action::GoalStart,
            status: "published".into(),
            reason: None,
        },
    };
    app.observe_live(Observation::WorkLaunchFinished {
        action_id: r.action_id.clone(),
        update: update.clone(),
        stopped_operation: None,
        stop_verified: false,
    })
    .unwrap();
    assert!(app.live.as_ref().unwrap().execution_pending());
    assert!(app.live.as_ref().unwrap().work.waiting);
    app.observe_live(Observation::WorkLaunchFinished {
        action_id: r.action_id,
        update,
        stopped_operation: None,
        stop_verified: true,
    })
    .unwrap();
    assert!(!app.live.as_ref().unwrap().execution_pending());
    assert!(!app.live.as_ref().unwrap().work.waiting);
    assert_eq!(app.composer.text(), before);
    assert!(app.live.as_ref().unwrap().rows.is_empty());
}
#[test]
fn foreign_launch_release_cannot_clear_reservation_and_quit_keeps_its_identity() {
    let (mut app, r, intent) = goal_apply();
    let chat = app.live.as_mut().unwrap();
    chat.prepare_work(&intent).unwrap();
    let update = v::Update {
        generation: intent.generation,
        data: v::Data::Rejected("before-send refusal".into()),
    };
    assert!(
        app.observe_live(Observation::WorkLaunchFinished {
            action_id: "00000000-0000-4000-8000-000000000099".into(),
            update: update.clone(),
            stopped_operation: None,
            stop_verified: true
        })
        .is_err()
    );
    assert!(app.live.as_ref().unwrap().execution_pending());
    let chat = app.live.as_mut().unwrap();
    chat.begin_quit();
    assert!(chat.execution_pending());
    assert!(chat.take_execution_stop().is_none());
    assert!(chat.rows.is_empty());
    assert_ne!(r.action_id, "");
}
#[test]
fn both_job_kinds_and_goal_resume_take_typed_inert_review_paths() {
    for (name, index, action) in [
        ("job_run-cron", 2, x::Action::JobRun),
        ("job_run-loop", 3, x::Action::JobRun),
        ("goal_resume-cron", 4, x::Action::GoalResume),
    ] {
        let f = fixture(name);
        let mut app = setup(&f);
        category(&mut app, &f, index);
        key(&mut app, KeyCode::Char('r'));
        let r = review(&mut app, &f);
        assert_eq!(r.action, action);
        assert!(!app.live.as_ref().unwrap().execution_pending());
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        assert_eq!(app.composer.text(), "main draft α 雪");
    }
}

#[test]
fn verified_stop_handoff_retains_launch_reservation_and_never_replays_or_aborts_twice() {
    let (mut app, review, intent) = goal_apply();
    let draft = app.composer.text().to_owned();
    let chat = app.live.as_mut().unwrap();
    chat.prepare_work(&intent).unwrap();
    chat.request_stop().unwrap();
    assert_eq!(chat.take_execution_stop(), Some(review.action_id.clone()));
    let observation = || Observation::WorkLaunchStop {
        action_id: review.action_id.clone(),
        generation: intent.generation,
        stopped_operation: None,
    };
    app.observe_live(observation()).unwrap();
    assert!(app.live.as_ref().unwrap().execution_pending());
    assert!(app.live.as_ref().unwrap().work.waiting);
    assert!(app.live.as_mut().unwrap().take_execution_stop().is_none());
    assert!(app.live.as_mut().unwrap().take_abort().is_none());
    assert!(app.observe_live(observation()).is_err());
    assert_eq!(app.composer.text(), draft);
    assert!(app.live.as_ref().unwrap().rows.is_empty());
    app.observe_live(Observation::WorkLaunchFinished {
        action_id: review.action_id,
        update: v::Update {
            generation: intent.generation,
            data: v::Data::ExecutionVerified {
                action: x::Action::GoalStart,
                status: "published".into(),
                reason: None,
            },
        },
        stopped_operation: None,
        stop_verified: true,
    })
    .unwrap();
    assert!(!app.live.as_ref().unwrap().execution_pending());
    assert!(app.live.as_mut().unwrap().take_abort().is_none());
    assert_eq!(app.composer.text(), draft);
}
#[test]
fn unrelated_stop_handoff_cannot_release_or_modify_launch() {
    let (mut app, review, intent) = goal_apply();
    app.live.as_mut().unwrap().prepare_work(&intent).unwrap();
    assert!(
        app.observe_live(Observation::WorkLaunchStop {
            action_id: review.action_id.clone(),
            generation: intent.generation,
            stopped_operation: None
        })
        .is_err()
    );
    app.live.as_mut().unwrap().request_stop().unwrap();
    app.live.as_mut().unwrap().take_execution_stop().unwrap();
    assert!(
        app.observe_live(Observation::WorkLaunchStop {
            action_id: "00000000-0000-4000-8000-000000000099".into(),
            generation: intent.generation,
            stopped_operation: None
        })
        .is_err()
    );
    assert!(app.live.as_ref().unwrap().execution_pending());
    assert!(app.live.as_ref().unwrap().rows.is_empty());
}
