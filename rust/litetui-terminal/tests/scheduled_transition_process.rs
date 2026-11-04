#![cfg(windows)]
//! Native Interactive runtime driving the actual captured Python Work lifecycle.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    conversation_browser,
    demo::Demo,
    live::{Interactive, Observation, Options, WorkExecution},
    live_chat::ReplyState,
    ui, work_view as work,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
fn source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn launch(case: &str) -> (Interactive, PathBuf) {
    launch_owned(case, None)
}
fn launch_owned(case: &str, resume: Option<(&Path, &str)>) -> (Interactive, PathBuf) {
    let actual = source();
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/scheduled-transition-process-tests")
        .join(format!(
            "{}-{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    let quote = |s: &str| serde_json::to_string(s).unwrap();
    fs::write(
        package.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            quote(&actual.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    fs::write(package.join("cli.py"), format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_SCHEDULE_SOURCE'] = {}
os.environ['RUST_SCHEDULE_CASE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()), quote(case), quote(&fixtures.join("scheduled_transition_runtime.py").to_string_lossy()))).unwrap();
    let root = resume
        .map(|(path, _)| path.to_path_buf())
        .unwrap_or_else(|| base.join("data"));
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("controlled dependency Python"),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        "custom".into(),
        "--model".into(),
        "switch-old".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--thinking-level".into(),
        "default".into(),
        "--tools".into(),
        "off".into(),
        "--timeout".into(),
        if case == "timeout-readback" {
            "20".into()
        } else {
            "100".into()
        },
        "--server-timeout".into(),
        "5".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    if let Some((_, conversation)) = resume {
        args.extend(["--resume".into(), conversation.into()]);
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn pump(
    runtime: &mut Interactive,
    app: &mut Demo,
    predicate: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(100);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(30)) {
            Ok(event) => {
                let found = predicate(&event);
                if !found && matches!(event, Observation::ScheduledHeld(_)) {
                    return Err(io::Error::other(format!(
                        "Scheduled handoff unexpectedly held: {event:?}"
                    )));
                }
                app.observe_live(event)?;
                if let Some((id, accept)) = app.take_schedule_decision() {
                    runtime.scheduled_decision(id, accept)?;
                }
                if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
                    runtime.work(intent)?;
                }
                if found {
                    return Ok(());
                }
            }
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Scheduled runtime exited early")));
                }
            }
        }
        if Instant::now() > deadline {
            return Err(io::Error::other("Scheduled observation timed out"));
        }
    }
}
fn ready(runtime: &mut Interactive) -> io::Result<Demo> {
    let mut app = Demo::live("custom".into(), "switch-old".into());
    app.insert_text("KEEP SCHEDULED COMPOSER SELECTION");
    app.composer.move_cursor(Motion::WordLeft, true);
    pump(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
    Ok(app)
}
fn report(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("scheduled-transition-report.json")).unwrap())
            .unwrap();
    for name in [
        "app",
        "background_input",
        "native_schedule",
        "native_deferred",
        "native_work",
        "rpc",
        "conversation_transition",
        "gui_rpc",
        "scheduler",
    ] {
        assert_eq!(
            PathBuf::from(r[format!("source_{name}")].as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join(format!("src/litetui/{name}.py"))
                .canonicalize()
                .unwrap()
        );
    }
    for key in ["denied", "questions", "approvals", "authorities"] {
        assert_eq!(r[key], json!([]));
    }
    assert_eq!(r["original_before"], r["original_after"]);
    assert_eq!(r["history"], r["durable"]);
    assert_eq!(r["reader_retired"], true);
    assert_eq!(r["helper_retired"], true);
    assert_eq!(r["shutdown"]["ok"], true);
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["finished"] == true)
    );
    assert!(
        r["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["closed"] == true)
    );
    for e in r["epochs"].as_array().unwrap() {
        for field in ["producers", "inputs"] {
            assert!(
                e[field]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|v| v["worker_finished"] == true && v["observer_done"] == true)
            );
        }
    }
    for command in r["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["type"] == "gui.work.native_activate")
    {
        assert_eq!(command["scheduled_transition_version"], 1);
    }
    assert!(
        !r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["type"] == "prompt")
    );
    r
}
#[test]
#[ignore = "controlled actual-source scheduled transition; dependency Python required"]
fn automatic_cron_and_loop_new_adopt_exact_target_then_run_once() {
    for case in ["cron", "loop"] {
        let (runtime, root) = launch(case);
        let result=runtime.with_cleanup(|runtime|{
   let mut app=ready(runtime)?;let draft=(app.composer.text().to_owned(),app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned());fs::write(root.join("tick"),b"")?;
   pump(runtime,&mut app,|e|matches!(e,Observation::ScheduledConversationChanged(_)))?;assert!(app.live.as_ref().unwrap().rows.is_empty());
   pump(runtime,&mut app,|e|matches!(e,Observation::WorkExecution(v) if matches!(v.as_ref(),WorkExecution::Settled{..})))?;
   let chat=app.live.as_ref().unwrap();assert_eq!(chat.rows.len(),1);assert!(chat.rows[0].source_owned);assert_eq!(chat.rows[0].state,ReplyState::Complete);assert_eq!(chat.rows[0].cards[0].answer,"CONTROLLED SCHEDULED ANSWER");assert!(chat.rows[0].prompt.contains("CANONICAL SCHEDULED INPUT Δ"));
   assert_eq!(draft,(app.composer.text().to_owned(),app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));Ok(())
  });
        if let Err(e) = &result {
            eprintln!("Scheduled {case} at {}: {e}", root.display());
        }
        result.unwrap();
        let r = report(&root);
        assert_eq!(r["requests"].as_array().unwrap().len(), 1);
        assert_eq!(r["jobs_after"][0]["run_count"], 1);
        assert_ne!(
            r["identity"]["session_id"],
            r["original_identity"]["session_id"]
        );
        assert_eq!(r["deferred"]["state"], "delivered");
        let commands = r["commands"].as_array().unwrap();
        let activations: Vec<_> = commands
            .iter()
            .filter(|c| c["type"] == "gui.work.native_activate")
            .collect();
        assert_eq!(activations.len(), 2);
        let wire = r["wire"].as_array().unwrap();
        let ack = wire
            .iter()
            .position(|w| {
                w["type"] == "response" && w["id"] == activations[1]["id"] && w["ok"] == true
            })
            .unwrap();
        let admission = wire
            .iter()
            .position(|w| w["type"] == "work_admission")
            .unwrap();
        assert!(ack < admission);
        assert_eq!(wire[ack]["result"]["instance"], wire[admission]["instance"]);
        assert_eq!(
            wire[admission]["identity"]["session_id"],
            r["identity"]["session_id"]
        );
    }
}
#[test]
#[ignore = "controlled actual-source scheduled transition; dependency Python required"]
fn native_editor_and_changed_canonical_job_refuse_before_stamp() {
    for case in ["decline", "mutate"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = ready(runtime)?;
            if case == "decline" {
                app.themes_open = true;
            }
            fs::write(root.join("tick"), b"")?;
            pump(runtime, &mut app, |e| {
                matches!(e, Observation::ScheduledHeld(_))
            })?;
            assert!(app.live.as_ref().unwrap().rows.is_empty());
            assert_eq!(app.composer.text(), "KEEP SCHEDULED COMPOSER SELECTION");
            Ok(())
        });
        if let Err(e) = &result {
            eprintln!("Scheduled {case} at {}: {e}", root.display());
        }
        result.unwrap();
        let r = report(&root);
        assert_eq!(r["requests"], json!([]));
        assert_eq!(r["identity"], r["original_identity"]);
        assert_eq!(r["jobs_after"][0]["run_count"], 0);
        assert_eq!(r["jobs_after"][0]["last_fired_slot"], Value::Null);
        assert_eq!(
            r["jobs_after"],
            if case == "mutate" {
                r["expected_refusal_jobs"].clone()
            } else {
                r["jobs_before"].clone()
            }
        );
        assert_eq!(r["deferred"], Value::Null);
    }
}
#[test]
#[ignore = "controlled actual-source scheduled transition; dependency Python required"]
fn quit_during_scheduled_handoff_joins_exact_owner_without_activation_or_input() {
    for case in [
        "quit-accept",
        "quit-readback",
        "quit-capture",
        "quit-activate",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = ready(runtime)?;
            fs::write(root.join("tick"), b"")?;
            let deadline = Instant::now() + Duration::from_secs(30);
            while !root.join("gate.json").exists() {
                if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(30)) {
                    app.observe_live(event)?;
                    if let Some((id, accept)) = app.take_schedule_decision() {
                        runtime.scheduled_decision(id, accept)?;
                    }
                }
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Scheduled Quit exited before gate")));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Scheduled Quit gate not reached"));
                }
            }
            app.live.as_mut().unwrap().begin_quit();
            runtime.quit()?;
            assert!(app.live.as_ref().unwrap().rows.is_empty());
            Ok(())
        });
        if let Err(e) = &result {
            eprintln!("Scheduled {case} at {}: {e}", root.display());
        }
        result.unwrap();
        let r = report(&root);
        assert_eq!(r["requests"], json!([]));
        let wire = r["wire"].as_array().unwrap();
        assert!(
            !wire
                .iter()
                .any(|w| w["type"] == "work_admission" || w["type"] == "turn_start")
        );
        assert_eq!(
            wire.iter()
                .filter(|w| w["type"] == "response"
                    && w["ok"] == true
                    && w["result"]["kind"] == "work_active")
                .count(),
            1
        );
        let proof = &r["shutdown"]["result"]["conversation_shutdown"];
        for field in [
            "worker_settled",
            "lifecycle_settled",
            "native_cleanup_settled",
        ] {
            assert_eq!(proof[field], true);
        }
        assert_eq!(proof["effects_verified"], false);
    }
}

fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}
#[test]
#[ignore = "controlled actual-source crash and recovery; dependency Python required"]
fn interrupted_staged_response_restarts_as_reviewable_hold_without_replay() {
    let (runtime, root) = launch("crash-staged");
    let first = runtime.with_cleanup(|runtime| {
        let mut app = ready(runtime)?;
        fs::write(root.join("tick"), b"")?;
        // The first child intentionally exits 73 after publishing staged evidence.
        // A successful native completion here would be an incorrect acceptance.
        pump(runtime, &mut app, |_| false)
    });
    assert!(
        first.is_err(),
        "controlled abnormal child was reported successful"
    );
    let failure = first.as_ref().unwrap_err().to_string();
    assert!(
        failure.contains("Python exited with") && failure.contains("73"),
        "unexpected abnormal exit: {failure}"
    );
    fs::write(
        root.join("scheduled-transition-crash-native-error.txt"),
        &failure,
    )
    .unwrap();
    eprintln!(
        "Controlled first child exited 73 and was reaped; saved evidence: {}",
        root.display()
    );
    let crash: Value = serde_json::from_slice(
        &fs::read(root.join("scheduled-transition-crash-report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(crash["expected_abnormal_exit"], 73);
    assert_eq!(crash["crash_stage"], "after-staged-response");
    assert_eq!(crash["requests"], json!([]));
    assert_eq!(crash["denied"], json!([]));
    assert_eq!(crash["deferred"]["state"], "pending");
    assert_eq!(crash["jobs_after"][0]["run_count"], 1);
    assert_eq!(crash["original_before"], crash["original_after"]);
    assert!(
        crash["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["result"]["kind"] == "work_schedule_result"
                && row["result"]["status"] == "staged")
    );
    assert!(
        !crash["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["type"] == "work_admission")
    );
    let target = crash["identity"]["session_id"].as_str().unwrap();
    let (runtime, resumed_root) = launch_owned("recover-staged", Some((&root, target)));
    assert_eq!(resumed_root, root);
    let second = runtime.with_cleanup(|runtime| {
        let mut app = ready(runtime)?;
        let draft = (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned());
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        app.open_work(); // Native F1 entry point.
        runtime.work(app.live.as_mut().unwrap().work.take_intent().unwrap())?;
        pump(runtime, &mut app, |event| matches!(event, Observation::Work(work::Update{data:work::Data::Page(_), ..})))?;
        let selected = app.live.as_ref().unwrap().work.snapshot.as_ref().unwrap().deferred_job.as_ref().unwrap();
        assert_eq!(selected.wire["record"], crash["deferred"]["record"]);
        assert!(selected.eligible);
        draw(&mut app);
        app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::BackTab, KeyModifiers::SHIFT));
        assert!(draw(&mut app).contains("Recovery"));
        app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::NONE));
        let validate = app.live.as_mut().unwrap().work.take_intent().unwrap();
        assert!(matches!(validate.request, work::Request::DeferredValidate));
        runtime.work(validate)?;
        pump(runtime, &mut app, |event| matches!(event, Observation::Work(work::Update{data:work::Data::DeferredReview(_), ..})))?;
        let mut pages = 0;
        loop {
            let screen = draw(&mut app);
            if pages == 0 { assert!(screen.contains("Discard this staged invocation")); }
            app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            pages += 1;
            if let Some(discard) = app.live.as_mut().unwrap().work.take_intent() {
                assert!(pages > 1, "full retained evidence must span and visit review pages");
                assert!(matches!(discard.request, work::Request::DeferredApply(_)));
                runtime.work(discard)?;
                break;
            }
            assert!(pages < 100, "discard review did not reach explicit final-page confirmation");
        }
        pump(runtime, &mut app, |event| matches!(event, Observation::Work(work::Update{data:work::Data::DeferredDiscarded(snapshot), ..}) if snapshot.deferred_job.is_none()))?;
        assert!(draw(&mut app).contains("Staged invocation discarded and read back"));
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        assert_eq!(draft, (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned()));
        Ok(())
    });
    if let Err(error) = &second {
        eprintln!(
            "Scheduled recovery at {}: {error}; expected first child: {first:?}",
            root.display()
        );
    }
    second.unwrap();
    let after = report(&root);
    assert_eq!(after["identity"], crash["identity"]);
    assert_eq!(after["jobs_after"], crash["jobs_after"]);
    assert_eq!(after["requests"], json!([]));
    assert_eq!(after["deferred"]["state"], "discarded");
    assert_eq!(after["deferred"]["record"], crash["deferred"]["record"]);
    assert_eq!(after["deferred"]["input_id"], crash["deferred"]["input_id"]);
    assert_eq!(after["deferred"]["retirement"]["no_replay"], true);
    assert_eq!(after["deferred"]["retirement"]["effects_undone"], false);
    let commands = after["commands"].as_array().unwrap();
    for operation in [
        "gui.work.native_deferred_validate",
        "gui.work.native_deferred_discard",
        "gui.work.native_deferred_readback",
    ] {
        assert_eq!(
            commands
                .iter()
                .filter(|command| command["type"] == operation)
                .count(),
            1
        );
    }
    assert!(
        !after["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| matches!(row["type"].as_str(), Some("work_admission" | "turn_start")))
    );
}

fn next_event(runtime: &mut Interactive, deadline: Instant) -> io::Result<Observation> {
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(30)) {
            Ok(event) => return Ok(event),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done.err().unwrap_or_else(|| {
                        io::Error::other("Scheduled fixture exited before expected observation")
                    }));
                }
            }
        }
        if Instant::now() > deadline {
            return Err(io::Error::other("Scheduled test observation deadline"));
        }
    }
}
#[test]
#[ignore = "controlled actual-source queued native submission; dependency Python required"]
fn queued_local_submission_after_source_proposal_is_refused_without_fabricated_work() {
    let (runtime, root) = launch("queued-local");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = ready(runtime)?;
        fs::write(root.join("tick"), b"")?;
        let deadline = Instant::now() + Duration::from_secs(30);
        let proposal = loop {
            let event = next_event(runtime, deadline)?;
            if matches!(&event, Observation::ScheduledProposal(_)) { break event; }
            app.observe_live(event)?;
        };
        // The supervisor has proposed, but the UI has not drained that event.
        let original = app.composer.text().to_owned();
        app.submit();
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        app.insert_text("NEWER LOCAL DRAFT Δ");
        app.composer.move_cursor(Motion::WordLeft, true);
        let draft = (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned());
        app.observe_live(proposal)?;
        loop {
            let event = next_event(runtime, deadline)?;
            let refused = matches!(&event, Observation::ScheduledLocalNotSent { prompt, .. } if prompt == &original);
            app.observe_live(event)?;
            if refused { break; }
        }
        assert!(!app.live.as_ref().unwrap().busy && !app.live.as_ref().unwrap().ready);
        let (id, accept) = app.take_schedule_decision().unwrap();
        assert!(!accept, "batch-drained local refusal must still decline this firing");
        runtime.scheduled_decision(id, accept)?;
        pump(runtime, &mut app, |event| matches!(event, Observation::ScheduledHeld(_)))?;
        let chat = app.live.as_mut().unwrap();
        assert!(chat.ready && !chat.busy);
        assert_eq!(chat.rows.len(), 1);
        assert_eq!(chat.rows[0].prompt, original);
        assert_eq!(chat.rows[0].state, ReplyState::Refused);
        assert!(!chat.rows[0].source_owned);
        assert!(chat.take_submission().is_none());
        assert_eq!(draft, (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned()));
        Ok(())
    });
    if let Err(error) = &result {
        eprintln!("Scheduled queued-local at {}: {error}", root.display());
    }
    result.unwrap();
    let r = report(&root);
    assert_eq!(r["requests"], json!([]));
    assert_eq!(r["jobs_after"], r["jobs_before"]);
    assert_eq!(r["identity"], r["original_identity"]);
    assert_eq!(r["deferred"], Value::Null);
    assert!(
        !r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["type"] == "gui.work.native_schedule_accept")
    );
    assert!(
        !r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["type"] == "work_admission")
    );
}
#[test]
#[ignore = "controlled actual-source scheduled deadline; dependency Python required"]
fn scheduled_readback_deadline_survives_repeated_unrelated_source_events() {
    let (runtime, root) = launch("timeout-readback");
    let mut observed_usage = 0;
    let started = Instant::now();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = ready(runtime)?;
        fs::write(root.join("tick"), b"")?;
        let deadline = Instant::now() + Duration::from_secs(45);
        loop {
            let event = next_event(runtime, deadline)?;
            if matches!(&event, Observation::Information(kind) if kind == "usage") {
                observed_usage += 1;
            }
            app.observe_live(event)?;
            if let Some((id, accept)) = app.take_schedule_decision() {
                runtime.scheduled_decision(id, accept)?;
            }
        }
    });
    let failure = result
        .expect_err("withheld scheduled readback unexpectedly completed")
        .to_string();
    assert!(
        failure.contains("Live deadline elapsed; no prompt retry"),
        "{failure}"
    );
    assert!(
        failure.contains("cleanup=Ok(())"),
        "owned cleanup not proven: {failure}"
    );
    assert!(started.elapsed() < Duration::from_secs(45));
    assert!(
        observed_usage >= 20,
        "unrelated events did not reach native reducer"
    );
    fs::write(
        root.join("scheduled-transition-expected-timeout.txt"),
        &failure,
    )
    .unwrap();
    let r = report(&root);
    assert!(r["unrelated_usage_events"].as_u64().unwrap() >= observed_usage);
    assert_eq!(r["requests"], json!([]));
    assert_eq!(r["jobs_after"][0]["run_count"], 1);
    assert!(
        !r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["type"] == "work_admission")
    );
    assert_eq!(
        r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["type"] == "gui.work.native_activate")
            .count(),
        1
    );
}

fn browser_send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.browser(
        app.live
            .as_mut()
            .unwrap()
            .browser
            .take_intent()
            .expect("explicit human browser intent"),
    )
}
fn browser_key(app: &mut Demo, key: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .browser
        .key(KeyEvent::new(key, KeyModifiers::NONE));
}
fn human_new(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_browser();
    browser_send(runtime, app)?;
    pump(runtime, app, |e| {
        matches!(
            e,
            Observation::Browser(conversation_browser::Update::Page(_))
        )
    })?;
    draw(app);
    browser_key(app, KeyCode::Char('n'));
    browser_send(runtime, app)?;
    pump(runtime, app, |e| {
        matches!(
            e,
            Observation::Browser(conversation_browser::Update::Action(_))
        )
    })?;
    draw(app);
    browser_key(app, KeyCode::Enter);
    browser_send(runtime, app)?;
    pump(runtime, app, |e| {
        matches!(
            e,
            Observation::Browser(conversation_browser::Update::Action(_))
        )
    })?;
    for _ in 0..100 {
        draw(app);
        browser_key(app, KeyCode::PageDown);
    }
    draw(app);
    browser_key(app, KeyCode::Enter);
    browser_send(runtime, app)
}

#[test]
#[ignore = "controlled actual-source human New/scheduled proposal race; dependency Python required"]
fn competing_human_new_retires_or_declines_old_proposal_without_scheduled_effects() {
    for case in [
        "human-new-success",
        "human-new-preclose",
        "human-new-postclose",
    ] {
        let (runtime, root) = launch(case);
        let mut held = false;
        let mut changed = false;
        let result = runtime.with_cleanup(|runtime| {
            let mut app = ready(runtime)?;
            let draft = (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned());
            human_new(runtime, &mut app)?;
            let until = Instant::now() + Duration::from_secs(45);
            loop {
                match runtime.events.recv_timeout(Duration::from_millis(30)) {
                    Ok(event) => {
                        assert!(!matches!(event, Observation::ScheduledProposal(_) | Observation::ScheduledConversationChanged(_) | Observation::WorkExecution(_)), "human-pending proposal must be source-declined without UI reservation/admission: {event:?}");
                        held |= matches!(event, Observation::ScheduledHeld(_));
                        changed |= matches!(event, Observation::ConversationChanged(_));
                        let action = matches!(event, Observation::Browser(conversation_browser::Update::Action(_)));
                        app.observe_live(event)?;
                        assert!(app.take_schedule_decision().is_none());
                        assert_eq!(draft, (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned()));
                        if held && (changed || case == "human-new-preclose" && action) {
                            assert!(app.live.as_ref().unwrap().rows.is_empty());
                            runtime.quit()?;
                            return Ok(());
                        }
                    }
                    Err(_) => {
                        if let Some(done) = runtime.poll_done() {
                            return done;
                        }
                    }
                }
                if Instant::now() > until { return Err(io::Error::other("Human New scheduled race timed out")); }
            }
        });
        if case == "human-new-postclose" {
            let error = result
                .expect_err("source human New failure must close native")
                .to_string();
            assert!(
                error.contains("Conversation receipt changed session identity")
                    && error.contains("cleanup=Ok(())"),
                "{case}: {error}; {}",
                root.display()
            );
            fs::write(
                root.join("scheduled-transition-human-native-error.txt"),
                error,
            )
            .unwrap();
        } else {
            result.unwrap_or_else(|error| panic!("{case}: {error}; {}", root.display()));
        }
        assert!(
            held,
            "exact old proposal decline/readback must settle before human Apply reply"
        );
        assert_eq!(changed, case == "human-new-success");
        let r = report(&root);
        assert_eq!(r["requests"], json!([]));
        assert_eq!(r["jobs_after"], r["jobs_before"]);
        assert!(r["deferred"].is_null());
        assert_eq!(
            r["identity"] == r["original_identity"],
            case != "human-new-success"
        );
        let commands = r["commands"].as_array().unwrap();
        for (op, count) in [
            ("gui.conversations.native_apply", 1),
            ("gui.work.native_schedule_decline", 1),
            ("gui.work.native_schedule_readback", 1),
            ("gui.work.native_schedule_accept", 0),
        ] {
            assert_eq!(
                commands.iter().filter(|c| c["type"] == op).count(),
                count,
                "{case}: {op}"
            );
        }
        let wire = r["wire"].as_array().unwrap();
        for kind in ["work_schedule_result", "work_schedule_readback"] {
            let result = wire
                .iter()
                .find(|v| v["result"]["kind"] == kind)
                .expect("actual source historical reply");
            assert_eq!(result["ok"], true);
            let decision = if kind == "work_schedule_readback" {
                &result["result"]["result"]
            } else {
                &result["result"]
            };
            assert_eq!(decision["status"], "declined");
            for field in ["stamp", "transition", "deferred", "deferred_journal_sha256"] {
                assert!(decision[field].is_null(), "{case}: no scheduled effects");
            }
        }
        assert!(
            !wire
                .iter()
                .any(|w| w["type"] == "work_admission" || w["type"] == "turn_start")
        );
        let human = &r["human_apply_result"];
        assert_eq!(
            human["status"],
            match case {
                "human-new-success" => "created",
                "human-new-preclose" => "refused",
                _ => "failed",
            }
        );
        if case != "human-new-preclose" {
            // Source proof observed and the native owner's cleanup reported Ok.
            // Post-close failure is deliberately not a successful human handoff.
            let proof = &r["shutdown"]["result"]["conversation_shutdown"];
            assert_eq!(proof["action_id"], human["action_id"]);
            assert_eq!(
                proof["from_session_id"],
                r["original_identity"]["session_id"]
            );
            assert_eq!(proof["target_session_id"], human["target_session_id"]);
            for field in [
                "worker_settled",
                "lifecycle_settled",
                "native_cleanup_settled",
            ] {
                assert_eq!(proof[field], true);
            }
            assert_eq!(proof["effects_verified"], false);
        }
        if case == "human-new-postclose" {
            assert_eq!(r["postclose_bridge_closing"], true);
            assert_eq!(human["close_required"], true);
            assert_eq!(human["mutated"], true);
            assert_eq!(
                human["error"],
                "Controlled human New failure after old Work closed"
            );
        }
    }
}
