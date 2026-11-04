#![cfg(windows)]
//! Actual reviewed Work execution through F1; immutable receipt and genuine Work streams.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options},
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
        .join("target/work-execution-controls-process-tests")
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
os.environ['RUST_EXECUTION_CONTROLS_SOURCE'] = {}
os.environ['RUST_EXECUTION_CONTROLS_CASE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()), quote(case), quote(&fixtures.join("work_execution_controls_runtime.py").to_string_lossy()))).unwrap();
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
        "120".into(),
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
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let deadline = Instant::now() + Duration::from_secs(125);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(50)) {
            Ok(event) => return Ok(event),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected Work runtime exit")));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other(
                        "Work execution controls observation deadline",
                    ));
                }
            }
        }
    }
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    if let Some(action) = app.live.as_mut().unwrap().take_execution_stop() {
        runtime.execution_stop(action)?;
    }
    if let Some(operation) = app.live.as_mut().unwrap().take_abort() {
        runtime.abort(operation)?;
    }
    if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
        app.live.as_mut().unwrap().prepare_work(&intent)?;
        runtime.work(intent)?;
    }
    Ok(())
}
fn wait(
    runtime: &mut Interactive,
    app: &mut Demo,
    predicate: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    loop {
        let event = next(runtime)?;
        if let Observation::Work(work::Update {
            data: work::Data::Rejected(reason),
            ..
        }) = &event
        {
            return Err(io::Error::other(reason.clone()));
        }
        let done = predicate(&event);
        app.observe_live(event)?;
        send(runtime, app)?;
        if done {
            return Ok(());
        }
    }
}
fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(120)
        .map(|cells| cells.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .work
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn page(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::Page(_),
                ..
            })
        )
    })
}
fn press(runtime: &Interactive, app: &mut Demo, code: KeyCode) -> io::Result<()> {
    draw(app);
    key(app, code);
    send(runtime, app)
}
fn paste(app: &mut Demo, value: &str, clear: bool) {
    draw(app);
    if clear {
        app.live
            .as_mut()
            .unwrap()
            .work
            .key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
    }
    app.live.as_mut().unwrap().work.paste(value);
}
fn refresh(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    press(runtime, app, KeyCode::F(5))?;
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::Snapshot(_),
                ..
            })
        )
    })?;
    if app.live.as_ref().unwrap().work.waiting {
        page(runtime, app)?;
    }
    Ok(())
}
fn tab(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    press(runtime, app, KeyCode::Tab)?;
    if app.live.as_ref().unwrap().work.waiting {
        page(runtime, app)?;
    }
    Ok(())
}
fn select(runtime: &Interactive, app: &mut Demo, id: &str) -> io::Result<()> {
    let index = app
        .live
        .as_ref()
        .unwrap()
        .work
        .page
        .as_ref()
        .unwrap()
        .rows
        .iter()
        .position(|row| row.id == id)
        .ok_or_else(|| io::Error::other(format!("Missing selected row {id}")))?;
    press(runtime, app, KeyCode::Home)?;
    for _ in 0..index {
        press(runtime, app, KeyCode::Down)?;
    }
    Ok(())
}

fn confirm(runtime: &mut Interactive, app: &mut Demo, frames: &mut Vec<String>) -> io::Result<()> {
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::ExecutionReview(_),
                ..
            })
        )
    })?;
    key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().work.take_intent().is_none(),
        "Unpainted execution review cannot confirm"
    );
    for _ in 0..160 {
        frames.push(draw(app));
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
            assert!(matches!(intent.request, work::Request::ExecutionApply(_)));
            app.live.as_mut().unwrap().prepare_work(&intent)?;
            runtime.work(intent)?;
            return Ok(());
        }
    }
    Err(io::Error::other(
        "Complete visible execution review never confirmed",
    ))
}

fn collect_report(root: &Path) -> Value {
    let value: Value = serde_json::from_slice(
        &fs::read(root.join("work-execution-controls-report.json")).unwrap(),
    )
    .unwrap();
    for module in [
        "app",
        "background_input",
        "native_work",
        "native_work_execution",
        "goal_loop",
        "scheduler",
        "row_store",
        "gui_rpc",
        "rpc",
        "hook_host",
    ] {
        assert_eq!(
            PathBuf::from(value[format!("source_{module}")].as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join(format!("src/litetui/{module}.py"))
                .canonicalize()
                .unwrap()
        );
    }
    for field in ["denied", "questions", "approvals", "authorities"] {
        assert_eq!(value[field], json!([]), "{field}: {}", root.display());
    }
    assert_eq!(value["before"], value["after"]);
    assert_eq!(value["history"], value["durable"]);
    for field in ["startup_held", "reader_retired", "helper_retired"] {
        assert_eq!(value[field], true, "{field}");
    }
    for worker in value["workers"].as_array().unwrap() {
        assert_eq!(worker["finished"], true);
    }
    for stream in value["streams"].as_array().unwrap() {
        assert_eq!(stream["closed"], true);
    }
    for lease in value["lease_release"].as_array().unwrap() {
        for field in ["active_execution", "questions", "approvals"] {
            assert_eq!(lease[field], json!([]));
        }
    }
    assert!(!value["lease_release"].as_array().unwrap().is_empty());
    let commands = value["commands"].as_array().unwrap();
    assert!(!commands.iter().any(|c| matches!(
        c["type"].as_str(),
        Some("prompt" | "gui.prompt.submit" | "gui.jobs.run")
    )));
    for op in ["gui.work.native_capture", "gui.work.native_activate"] {
        let matching: Vec<_> = commands.iter().filter(|c| c["type"] == op).collect();
        let expected = if value["case"] == "quit-capture" && op == "gui.work.native_activate" {
            0
        } else {
            1
        };
        assert_eq!(matching.len(), expected);
        if expected == 1 {
            assert_eq!(matching[0]["execution_version"], 1);
        }
    }
    assert_eq!(value["shutdown"]["ok"], true);
    let proof = &value["shutdown"]["result"]["work_execution_shutdown"];
    if value["case"] == "quit-capture" {
        assert!(proof.is_null());
    } else {
        assert_eq!(proof["execution_version"], 1);
        for key in ["admission_closed", "worker_settled", "callback_settled"] {
            assert_eq!(proof[key], true, "{key}");
        }
        assert_eq!(proof["effects_verified"], false);
        assert!(
            proof["owners"]
                .as_object()
                .unwrap()
                .values()
                .all(|v| v != "pending")
        );
    }
    fs::write(root.join("work-execution-controls-source-verification.json"), serde_json::to_vec_pretty(&json!({
        "version":1,"case":value["case"],"source_report":"work-execution-controls-report.json",
        "imports_verified":true,"cleanup_verified":true,"normal_source_shutdown":true,
        "no_synthetic_prompt":true,"unchanged_runtime_authority":true,
        "qualification":"Common source import, ownership, stream/worker/reader/lease and shutdown assertions passed; case-specific assertions are recorded by the Cargo test result."
    })).unwrap()).unwrap();
    value
}

fn receipt(report: &Value) -> Value {
    let text = report["final_saved"]["receipt"]["text"].as_str().unwrap();
    let value: Value = serde_json::from_str(text).unwrap();
    assert_eq!(value["kind"], "work_execution_launch");
    assert_eq!(value["identity"], report["identity"]);
    assert_ne!(value["apply_operation_id"], value["input"]["input_id"]);
    let matching = report["lifecycle"]["inputs"]
        .as_array()
        .unwrap()
        .iter()
        .find(|i| i["ref"] == value["input"])
        .unwrap();
    assert_eq!(matching["payload"], value["payload"]);
    assert_eq!(
        value["producer"]["producer_id"],
        matching["ref"]["producer_id"]
    );
    assert!(
        report["lifecycle"]["producers"]
            .as_array()
            .unwrap()
            .iter()
            .any(|p| p["ref"] == value["producer"])
    );
    value
}

fn run_case(case: &str) -> Value {
    let (runtime, root) = launch(case);
    let mut frames = Vec::new();
    let mut observations = Vec::new();
    let mut preserved = false;
    let runs = if case.ends_with("same-minute") { 2 } else { 1 };
    let no_effect = matches!(case, "row-drift" | "automatic-generation-race");
    let held_only = case == "hook-refused";
    let closing = matches!(
        case,
        "save-then-raise" | "receipt-failure" | "receipt-save-then-raise" | "receipt-drift"
    );
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("KEEP EXECUTION COMPOSER AND SELECTION");
        app.composer.move_cursor(Motion::WordLeft, true);
        let draft = (
            app.composer.text().to_string(),
            app.composer.before_cursor().to_string(),
            app.composer.selected_text().to_string(),
        );
        let execution_result = (|| {
            app.open_work();
            send(runtime, &mut app)?;
            page(runtime, &mut app)?;
            let goal = case.starts_with("goal-");
            for _ in 0..if goal {
                4
            } else if case.starts_with("loop-") {
                3
            } else {
                2
            } {
                tab(runtime, &mut app)?;
            }
            for index in 0..runs {
                if index > 0 {
                    refresh(runtime, &mut app)?;
                }
                if goal && case != "goal-resume" {
                    press(runtime, &mut app, KeyCode::Char('n'))?;
                    assert!(draw(&mut app).contains("Objective"));
                    let objective = case.strip_prefix("goal-literal-").unwrap_or("pause");
                    paste(&mut app, &format!("  {objective}  "), false);
                    frames.push(draw(&mut app));
                    press(runtime, &mut app, KeyCode::Enter)?;
                } else {
                    if !goal {
                        select(runtime, &mut app, "selected")?;
                    }
                    press(runtime, &mut app, KeyCode::Char('r'))?;
                }
                confirm(runtime, &mut app, &mut frames)?;
                let mut verified = false;
                loop {
                    let event = next(runtime)?;
                    if held_only && matches!(&event, Observation::WorkLaunchPending { .. }) {
                        verified = true;
                    }
                    observations.push(format!("{event:?}"));
                    if matches!(
                        &event,
                        Observation::WorkLaunchFinished {
                            update: work::Update {
                                data: work::Data::ExecutionVerified { .. },
                                ..
                            },
                            ..
                        }
                    ) {
                        verified = true;
                    }
                    app.observe_live(event)?;
                    send(runtime, &mut app)?;
                    let chat = app.live.as_ref().unwrap();
                    if verified && (no_effect || held_only || chat.rows.len() > index && !chat.busy)
                    {
                        break;
                    }
                }
                assert_eq!(
                    draft,
                    (
                        app.composer.text().to_string(),
                        app.composer.before_cursor().to_string(),
                        app.composer.selected_text().to_string()
                    )
                );
                frames.push(draw(&mut app));
            }
            Ok(())
        })();
        assert_eq!(
            draft,
            (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string()
            )
        );
        preserved = true;
        execution_result
    });
    let error = result.as_ref().err().map(ToString::to_string);
    fs::write(root.join("native-work-execution-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":case,"error":error,"composer_preserved":preserved,"observations":observations})).unwrap()).unwrap();
    fs::write(
        root.join("native-work-execution-controls-frames.txt"),
        frames.join("\n\nNEXT FRAME\n\n"),
    )
    .unwrap();
    eprintln!("Execution controls {case} report: {}", root.display());
    assert!(
        preserved,
        "Composer must survive expected native failure too"
    );
    if closing {
        assert!(result.is_err(), "Uncertain execution must close");
        assert!(
            error.as_ref().is_some_and(|e| e.contains("cleanup=Ok(())")),
            "{error:?}"
        );
        let expected = if case == "receipt-drift" {
            "Immutable execution receipt changed"
        } else {
            "Execution effects are uncertain; close without retry"
        };
        assert!(
            error.as_ref().is_some_and(|e| e.contains(expected)),
            "{error:?}"
        );
    } else {
        result.unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
        assert!(preserved);
    }
    let report = collect_report(&root);
    if no_effect {
        assert_eq!(report["results"][0]["status"], "refused");
        assert_eq!(report["results"][0]["effect_attempted"], false);
        if case == "row-drift" {
            assert_eq!(report["requests"], json!([]));
            assert_eq!(report["final_saved"], report["injected_drift"]);
            assert_eq!(report["lifecycle"]["producers"], json!([]));
            assert_eq!(report["lifecycle"]["inputs"], json!([]));
        } else {
            assert_eq!(report["requests"].as_array().unwrap().len(), 1);
            assert_eq!(report["automatic_without_execution_receipt"], true);
            assert!(report["final_saved"]["receipt"].is_null());
            let e = &report["shutdown"]["result"]["work_execution_shutdown"];
            assert_eq!(e["disposition"], "no_effect");
            assert_eq!(e["action_id"], report["reviews"][0]["action_id"]);
            for key in ["input_id", "admission_id", "document_sha256"] {
                assert!(e[key].is_null());
            }
            let automatic = &report["automatic_before_explicit"];
            assert_eq!(
                automatic["producer"]["generation"],
                report["reviews"][0]["producer_generation"]
            );
            assert_eq!(
                automatic["producer"]["object_id"],
                report["reviews"][0]["target_id"]
            );
            assert_eq!(
                automatic["input"]["payload_sha256"],
                report["reviews"][0]["payload_sha256"]
            );
            assert!(!automatic["admission"].is_null() && !automatic["settlement"].is_null());
            assert_eq!(report["lifecycle"]["inputs"].as_array().unwrap().len(), 1);
            assert_eq!(report["readbacks"].as_array().unwrap().len(), 1);
            assert!(report["results"][0]["document"].is_null());
        }
    } else if !closing {
        assert_eq!(report["reviews"].as_array().unwrap().len(), runs);
        assert_eq!(report["results"].as_array().unwrap().len(), runs);
        assert_eq!(report["readbacks"].as_array().unwrap().len(), runs);
        assert_eq!(
            report["requests"].as_array().unwrap().len(),
            if held_only { 0 } else { runs }
        );
        for result in report["results"].as_array().unwrap() {
            assert_eq!(result["status"], "published");
        }
        for read in report["readbacks"].as_array().unwrap() {
            assert_eq!(read["scope"], "launch");
            assert_eq!(read["verified"], true);
        }
        receipt(&report);
    }
    report
}

#[test]
#[ignore = "controlled actual-source F1 manual execution; dependency Python required"]
fn reviewed_manual_cron_and_loop_allow_disabled_not_due_and_fresh_same_minute_runs() {
    for case in [
        "cron-disabled",
        "cron-notdue",
        "cron-same-minute",
        "loop-disabled",
        "loop-notdue",
        "loop-same-minute",
    ] {
        let r = run_case(case);
        let initial: Value =
            serde_json::from_str(r["initial_saved"]["jobs"]["text"].as_str().unwrap()).unwrap();
        let final_rows: Value =
            serde_json::from_str(r["final_saved"]["jobs"]["text"].as_str().unwrap()).unwrap();
        assert_eq!(final_rows[1], initial[1]);
        assert_eq!(
            final_rows[0]["future_extension"],
            initial[0]["future_extension"]
        );
        assert_eq!(final_rows[0]["enabled"], initial[0]["enabled"]);
        assert_eq!(
            final_rows[0]["run_count"],
            if case.ends_with("same-minute") { 9 } else { 8 }
        );
        assert_eq!(final_rows[0]["last_fired_slot"], "2026-11-04T09:15");
        if case.starts_with("loop-") {
            assert_eq!(final_rows[0]["next_run_at"], "2026-11-04T09:32:20");
        }
    }
}

#[test]
#[ignore = "controlled actual-source literal goal/start/resume execution; dependency Python required"]
fn literal_goal_objectives_replacement_resume_and_fast_evaluation_use_exact_launch_receipts() {
    for case in [
        "goal-literal-pause",
        "goal-literal-resume",
        "goal-literal-clear",
        "goal-literal-status",
        "goal-literal-show",
        "goal-replacement",
        "goal-resume",
        "goal-fast-evaluation",
    ] {
        let r = run_case(case);
        let d = receipt(&r);
        let after: Value = serde_json::from_str(d["after"]["json"].as_str().unwrap()).unwrap();
        if case == "goal-resume" {
            let before: Value =
                serde_json::from_str(d["before"]["json"].as_str().unwrap()).unwrap();
            for key in [
                "id",
                "objective",
                "tool_profile",
                "started_by",
                "evaluated_turns",
                "max_turns",
                "future_extension",
            ] {
                assert_eq!(after[key], before[key]);
            }
        } else {
            assert_eq!(
                after["objective"],
                case.strip_prefix("goal-literal-").unwrap_or("pause")
            );
            assert_eq!(after["evaluated_turns"], 0);
            assert_eq!(after["max_turns"], 0);
            assert_eq!(after["started_by"], "gui");
        }
        assert_eq!(after["status"], "active");
        if case == "goal-fast-evaluation" {
            let current: Value = serde_json::from_str(
                r["readbacks"][0]["current"]["row"]["json"]
                    .as_str()
                    .unwrap(),
            )
            .unwrap();
            assert_eq!(current["status"], "met");
            assert!(r["early_wire_count_before_apply_reply"].as_u64().unwrap() > 0);
        }
    }
}

#[test]
#[ignore = "controlled early Work event and selected raw CAS evidence; dependency Python required"]
fn early_work_and_concurrent_sibling_changes_preserve_exact_control_ownership() {
    for case in [
        "early-work",
        "peer-drift",
        "row-drift",
        "automatic-generation-race",
    ] {
        let r = run_case(case);
        if case == "early-work" {
            assert!(r["early_wire_count_before_apply_reply"].as_u64().unwrap() > 0);
        }
        if case == "peer-drift" {
            let jobs: Value =
                serde_json::from_str(r["final_saved"]["jobs"]["text"].as_str().unwrap()).unwrap();
            assert_eq!(jobs[1], r["peer_edit"]);
        }
    }
}

#[test]
#[ignore = "controlled durable and immutable receipt ambiguity; dependency Python required"]
fn persistence_ambiguity_never_rolls_back_or_retries_owned_execution() {
    for case in [
        "save-then-raise",
        "receipt-failure",
        "receipt-save-then-raise",
        "receipt-drift",
    ] {
        let r = run_case(case);
        let jobs: Value =
            serde_json::from_str(r["final_saved"]["jobs"]["text"].as_str().unwrap()).unwrap();
        assert_eq!(jobs[0]["run_count"], 8);
        assert_eq!(
            r["commands"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|c| c["type"] == "gui.work.native_execution_apply")
                .count(),
            1
        );
        if case != "receipt-drift" {
            assert_eq!(r["requests"], json!([]));
            assert_eq!(r["results"][0]["status"], "uncertain");
            assert_eq!(r["results"][0]["effect_attempted"], true);
            assert_eq!(r["results"][0]["close_required"], true);
        }
    }
}

#[test]
#[ignore = "controlled receipt crash and historical restart; dependency Python required"]
fn receipt_restart_never_reconstructs_launch_or_replays_saved_input() {
    let (runtime, root) = launch("crash-receipt");
    let mut frames = Vec::new();
    let mut crash_composer_preserved = false;
    let first = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("PRESERVE COMPOSER THROUGH INTENTIONAL CRASH");
        app.open_work();
        send(runtime, &mut app)?;
        page(runtime, &mut app)?;
        tab(runtime, &mut app)?;
        tab(runtime, &mut app)?;
        select(runtime, &mut app, "selected")?;
        press(runtime, &mut app, KeyCode::Char('r'))?;
        confirm(runtime, &mut app, &mut frames)?;
        let stopped = wait(runtime, &mut app, |_| false);
        assert_eq!(
            app.composer.text(),
            "PRESERVE COMPOSER THROUGH INTENTIONAL CRASH"
        );
        crash_composer_preserved = true;
        stopped
    });
    let error = first
        .expect_err("Receipt crash cannot report successful shutdown")
        .to_string();
    assert!(
        error.contains("Python exited with") && error.contains("74"),
        "{error}"
    );
    fs::write(
        root.join("work-execution-controls-crash-native-error.txt"),
        &error,
    )
    .unwrap();
    assert!(crash_composer_preserved);
    fs::write(root.join("native-work-execution-controls-crash-verification.json"), serde_json::to_vec_pretty(&json!({"case":"crash-receipt","expected_abnormal_exit":74,"composer_preserved":crash_composer_preserved,"error":error,"normal_cleanup_claimed":false})).unwrap()).unwrap();
    let crash: Value = serde_json::from_slice(
        &fs::read(root.join("work-execution-controls-crash-report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(crash["expected_abnormal_exit"], 74);
    assert_eq!(
        crash["crash_stage"],
        "after-receipt-replace-before-readback"
    );
    assert_eq!(crash["requests"], json!([]));
    assert_eq!(crash["denied"], json!([]));
    assert_eq!(crash["history"], crash["initial_history"]);
    assert!(!crash["final_saved"]["receipt"].is_null());
    let session = crash["identity"]["session_id"].as_str().unwrap();
    let (runtime, resumed_root) = launch_owned("historical-restart", Some((&root, session)));
    assert_eq!(root, resumed_root);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("PRESERVE RECOVERY COMPOSER");
            app.open_work();
            send(runtime, &mut app)?;
            page(runtime, &mut app)?;
            frames.push(draw(&mut app));
            assert!(app.live.as_ref().unwrap().rows.is_empty());
            assert_eq!(app.composer.text(), "PRESERVE RECOVERY COMPOSER");
            Ok(())
        })
        .unwrap();
    fs::write(root.join("native-work-execution-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":"historical-restart","error":null,"composer_preserved":true,"receipt_unchanged":true})).unwrap()).unwrap();
    let r = collect_report(&root);
    assert_eq!(r["requests"], json!([]));
    assert_eq!(r["final_saved"], crash["final_saved"]);
    // Canonical resume may hydrate its owned storage instructions into the
    // system prompt; no user/assistant input is replayed or added.
    assert_eq!(r["history"], r["initial_history"]);
    let old_messages: Vec<_> = crash["history"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|m| m["role"] != "system")
        .collect();
    let resumed_messages: Vec<_> = r["history"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|m| m["role"] != "system")
        .collect();
    assert_eq!(resumed_messages, old_messages);
    assert!(!r["commands"].as_array().unwrap().iter().any(|c| {
        c["type"]
            .as_str()
            .is_some_and(|kind| kind.starts_with("gui.work.native_execution_"))
    }));
    let capture = r["wire"]
        .as_array()
        .unwrap()
        .iter()
        .find(|w| !w["result"]["execution_receipt"].is_null())
        .unwrap();
    let historical = &capture["result"]["execution_receipt"];
    assert_eq!(historical["status"], "historical");
    assert_eq!(
        historical["document"],
        crash["final_saved"]["receipt"]["text"]
    );
    assert_eq!(
        historical["document_sha256"],
        crash["final_saved"]["receipt"]["sha256"]
    );
    assert_eq!(
        r["shutdown"]["result"]["work_execution_shutdown"]["disposition"],
        "idle"
    );
    fs::write(
        root.join("native-work-execution-recovery-frames.txt"),
        frames.join("\n\nNEXT FRAME\n\n"),
    )
    .unwrap();
    eprintln!(
        "Execution receipt crash exit74 and separate normal restart report: {}",
        root.display()
    );
}

#[test]
#[ignore = "controlled pre-turn lifecycle hook authority refusal; dependency Python required"]
fn preturn_hook_refusal_cannot_create_interactive_authority_or_provider_admission() {
    let r = run_case("hook-refused");
    assert_eq!(r["requests"], json!([]));
    assert!(!r["hook_calls"].as_array().unwrap().is_empty());
    assert!(
        r["hook_calls"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["allow_prompt"] == false)
    );
    assert!(
        r["lifecycle"]["inputs"]
            .as_array()
            .unwrap()
            .iter()
            .all(|row| row["admission"].is_null())
    );
}

fn pending_gate(
    runtime: &mut Interactive,
    app: &mut Demo,
    root: &Path,
    admitted: bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(25);
    loop {
        if root.join("execution-gate.json").exists()
            && (!admitted || app.live.as_ref().unwrap().busy)
        {
            return Ok(());
        }
        match runtime.events.recv_timeout(Duration::from_millis(25)) {
            Ok(event) => {
                app.observe_live(event)?;
                send(runtime, app)?;
            }
            Err(_) => {
                if let Some(result) = runtime.poll_done() {
                    return result.and(Err(io::Error::other(
                        "Execution exited before controlled gate",
                    )));
                }
            }
        }
        if Instant::now() > deadline {
            return Err(io::Error::other("Controlled execution gate deadline"));
        }
    }
}

#[test]
#[ignore = "controlled actual hook/admitted Stop and Quit ownership; dependency Python required"]
fn stop_and_quit_join_exact_hook_or_admitted_owners_without_replay() {
    for case in [
        "stop-before-producer",
        "quit-before-producer",
        "stop-hook",
        "quit-hook",
        "stop-admitted",
        "quit-admitted",
    ] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        let mut observed_release = false;
        let mut preserved = false;
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("KEEP DRAFT DURING OWNED STOP");
            app.composer.move_cursor(Motion::WordLeft, true);
            let draft = (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string(),
            );
            let ui_result = (|| {
                app.open_work();
                send(runtime, &mut app)?;
                page(runtime, &mut app)?;
                tab(runtime, &mut app)?;
                tab(runtime, &mut app)?;
                select(runtime, &mut app, "selected")?;
                press(runtime, &mut app, KeyCode::Char('r'))?;
                confirm(runtime, &mut app, &mut frames)?;
                pending_gate(runtime, &mut app, &root, case.ends_with("admitted"))?;
                assert!(
                    app.live.as_ref().unwrap().execution_pending(),
                    "Apply must still own its exact launch reservation at the race"
                );
                if case.starts_with("stop-") {
                    app.open_stop();
                    assert!(app.stop_open());
                    frames.push(draw(&mut app));
                    app.live.as_mut().unwrap().request_stop()?;
                    send(runtime, &mut app)?;
                    loop {
                        let event = next(runtime)?;
                        if matches!(&event, Observation::WorkLaunchFinished { .. }) {
                            observed_release = true;
                        }
                        app.observe_live(event)?;
                        send(runtime, &mut app)?;
                        let chat = app.live.as_ref().unwrap();
                        if observed_release && !chat.execution_pending() && !chat.busy {
                            break;
                        }
                    }
                } else {
                    app.live.as_mut().unwrap().begin_quit();
                }
                Ok::<(), io::Error>(())
            })();
            assert_eq!(
                draft,
                (
                    app.composer.text().to_string(),
                    app.composer.before_cursor().to_string(),
                    app.composer.selected_text().to_string()
                )
            );
            preserved = true;
            frames.push(draw(&mut app));
            ui_result?;
            runtime.quit()
        });
        fs::write(root.join("native-work-execution-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":case,"error":result.as_ref().err().map(ToString::to_string),"composer_preserved":preserved,"observed_release":observed_release})).unwrap()).unwrap();
        assert!(preserved);
        if case == "stop-hook" {
            let error = result
                .expect_err("Cancelling an already claimed hook is uncertain")
                .to_string();
            assert!(error.contains("cleanup=Ok(())"), "{case}: {error}");
            assert!(
                error.contains("Execution effects are uncertain; close without retry"),
                "{error}"
            );
            fs::write(
                root.join("native-work-execution-expected-stop-error.txt"),
                error,
            )
            .unwrap();
        } else {
            result.unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
        }
        let r = collect_report(&root);
        fs::write(
            root.join("native-work-execution-stop-frames.txt"),
            frames.join("\n\nNEXT FRAME\n\n"),
        )
        .unwrap();
        if case.ends_with("hook") {
            assert_eq!(r["hook_gate_retired"], true);
            assert_eq!(r["requests"], json!([]));
            assert!(
                r["lifecycle"]["inputs"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .all(|entry| entry["admission"].is_null())
            );
        }
        let commands = r["commands"].as_array().unwrap();
        assert_eq!(
            commands
                .iter()
                .filter(|c| c["type"] == "gui.work.native_execution_apply")
                .count(),
            1
        );
        if case.starts_with("stop-") && case != "stop-hook" {
            assert!(observed_release);
            assert_eq!(
                commands
                    .iter()
                    .filter(|c| c["type"] == "gui.work.native_execution_stop")
                    .count(),
                1
            );
            assert_eq!(
                commands
                    .iter()
                    .filter(|c| c["type"] == "gui.work.native_execution_readback"
                        && c["scope"] == "stop")
                    .count(),
                1
            );
        }
        let shutdown = &r["shutdown"]["result"]["work_execution_shutdown"];
        assert!(!shutdown["action_id"].is_null());
        assert!(!shutdown["apply_operation_id"].is_null());
        assert!(
            shutdown["owners"]
                .as_object()
                .unwrap()
                .values()
                .all(|owner| owner != "pending")
        );
        let jobs: Value =
            serde_json::from_str(r["final_saved"]["jobs"]["text"].as_str().unwrap()).unwrap();
        assert_eq!(
            jobs[0]["run_count"],
            if case.ends_with("before-producer") {
                7
            } else {
                8
            }
        );
        if case.ends_with("before-producer") {
            assert_eq!(r["launch_gate_retired"], true);
            assert_eq!(r["requests"], json!([]));
            assert_eq!(r["lifecycle"]["producers"], json!([]));
            assert_eq!(r["lifecycle"]["inputs"], json!([]));
            assert_eq!(shutdown["effect_attempted"], false);
            assert_eq!(shutdown["disposition"], "no_effect");
        }
        eprintln!("Execution controls {case} report: {}", root.display());
    }
}

#[test]
#[ignore = "controlled independent launch/Stop readback cross-order; dependency Python required"]
fn launch_and_stop_readbacks_cross_without_releasing_the_newer_stop_obligation() {
    for case in [
        "stop-overlap-stop-first",
        "stop-overlap-launch-first",
        "stop-overlap-admitted",
    ] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        let mut retained_after_old_finish = false;
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("KEEP DRAFT ACROSS BOTH PROOF SCOPES");
            app.open_work();
            send(runtime, &mut app)?;
            page(runtime, &mut app)?;
            tab(runtime, &mut app)?;
            tab(runtime, &mut app)?;
            select(runtime, &mut app, "selected")?;
            press(runtime, &mut app, KeyCode::Char('r'))?;
            confirm(runtime, &mut app, &mut frames)?;
            let mut buffered = Vec::new();
            if case == "stop-overlap-launch-first" {
                // The supervisor has finished. The actual UI event has not yet
                // reached Chat; a local Stop wins this concrete consumer gap.
                loop {
                    let event = next(runtime)?;
                    let finished = matches!(
                        &event,
                        Observation::WorkLaunchFinished {
                            stop_verified: false,
                            ..
                        }
                    );
                    buffered.push(event);
                    if finished {
                        break;
                    }
                }
            } else {
                pending_gate(runtime, &mut app, &root, false)?;
            }
            assert!(app.live.as_ref().unwrap().execution_pending());
            app.open_stop();
            assert!(app.stop_open());
            frames.push(draw(&mut app));
            app.live.as_mut().unwrap().request_stop()?;
            send(runtime, &mut app)?;
            for event in buffered {
                let old_finish = matches!(
                    &event,
                    Observation::WorkLaunchFinished {
                        stop_verified: false,
                        ..
                    }
                );
                app.observe_live(event)?;
                if old_finish {
                    assert!(
                        app.live.as_ref().unwrap().execution_pending(),
                        "Old launch readback released a newer local Stop"
                    );
                    retained_after_old_finish = true;
                }
                send(runtime, &mut app)?;
            }
            let mut stop_verified = false;
            loop {
                let event = next(runtime)?;
                if matches!(
                    &event,
                    Observation::WorkLaunchFinished {
                        stop_verified: true,
                        ..
                    }
                ) {
                    stop_verified = true;
                }
                app.observe_live(event)?;
                send(runtime, &mut app)?;
                let chat = app.live.as_ref().unwrap();
                if stop_verified && !chat.execution_pending() && !chat.busy {
                    break;
                }
            }
            assert_eq!(app.composer.text(), "KEEP DRAFT ACROSS BOTH PROOF SCOPES");
            frames.push(draw(&mut app));
            Ok(())
        });
        fs::write(root.join("native-work-execution-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":case,"error":result.as_ref().err().map(ToString::to_string),"composer_preserved":result.is_ok(),"retained_after_old_finish":retained_after_old_finish})).unwrap()).unwrap();
        result.unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
        if case == "stop-overlap-launch-first" {
            assert!(retained_after_old_finish);
        }
        let r = collect_report(&root);
        let commands = r["commands"].as_array().unwrap();
        for op in [
            "gui.work.native_execution_apply",
            "gui.work.native_execution_stop",
        ] {
            assert_eq!(commands.iter().filter(|c| c["type"] == op).count(), 1);
        }
        for scope in ["launch", "stop"] {
            assert_eq!(
                commands
                    .iter()
                    .filter(|c| c["type"] == "gui.work.native_execution_readback"
                        && c["scope"] == scope)
                    .count(),
                1
            );
        }
        let readbacks: Vec<_> = r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|w| w["result"]["kind"] == "work_execution_readback")
            .collect();
        assert_eq!(readbacks.len(), 2);
        assert_eq!(
            readbacks[0]["result"]["scope"],
            if case != "stop-overlap-launch-first" {
                "stop"
            } else {
                "launch"
            }
        );
        assert_eq!(r["requests"].as_array().unwrap().len(), 1);
        if case == "stop-overlap-admitted" {
            assert_eq!(r["launch_gate_released_after_settlement"], true);
            assert_eq!(commands.iter().filter(|c| c["type"] == "abort").count(), 1);
            let wire = r["wire"].as_array().unwrap();
            let stop_readback = wire
                .iter()
                .position(|w| {
                    w["result"]["kind"] == "work_execution_readback"
                        && w["result"]["scope"] == "stop"
                })
                .unwrap();
            let settled = wire
                .iter()
                .position(|w| w["type"] == "work_settled")
                .unwrap();
            let launch_readback = wire
                .iter()
                .position(|w| {
                    w["result"]["kind"] == "work_execution_readback"
                        && w["result"]["scope"] == "launch"
                })
                .unwrap();
            assert!(stop_readback < settled && settled < launch_readback);
        }
        fs::write(
            root.join("native-work-execution-overlap-frames.txt"),
            frames.join("\n\nNEXT FRAME\n\n"),
        )
        .unwrap();
        eprintln!("Execution controls {case} report: {}", root.display());
    }
}

#[test]
#[ignore = "controlled post-readback pre-admission Stop reachability; dependency Python required"]
fn verified_launch_keeps_exact_stop_available_until_real_admission_or_cancellation() {
    for case in ["stop-hook-after-readback", "stop-hook-refused"] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        let mut pending_seen = false;
        let mut preserved = false;
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("KEEP DRAFT WHILE PRE-ADMISSION WORK IS HELD");
            let ui_result = (|| {
                app.open_work();
                send(runtime, &mut app)?;
                page(runtime, &mut app)?;
                tab(runtime, &mut app)?;
                tab(runtime, &mut app)?;
                select(runtime, &mut app, "selected")?;
                press(runtime, &mut app, KeyCode::Char('r'))?;
                confirm(runtime, &mut app, &mut frames)?;
                loop {
                    let event = next(runtime)?;
                    let held = matches!(&event, Observation::WorkLaunchPending { .. });
                    app.observe_live(event)?;
                    send(runtime, &mut app)?;
                    if held {
                        pending_seen = true;
                        break;
                    }
                }
                let chat = app.live.as_ref().unwrap();
                assert!(chat.execution_pending() && !chat.busy && chat.rows.is_empty());
                app.open_stop();
                assert!(app.stop_open());
                frames.push(draw(&mut app));
                app.live.as_mut().unwrap().request_stop()?;
                send(runtime, &mut app)?;
                loop {
                    let event = next(runtime)?;
                    let ended = matches!(
                        &event,
                        Observation::WorkLaunchFinished {
                            stop_verified: true,
                            ..
                        }
                    );
                    app.observe_live(event)?;
                    send(runtime, &mut app)?;
                    if ended {
                        assert!(!app.live.as_ref().unwrap().execution_pending());
                        assert_eq!(
                            app.composer.text(),
                            "KEEP DRAFT WHILE PRE-ADMISSION WORK IS HELD"
                        );
                        return Ok(());
                    }
                }
            })();
            assert_eq!(
                app.composer.text(),
                "KEEP DRAFT WHILE PRE-ADMISSION WORK IS HELD"
            );
            preserved = true;
            ui_result
        });
        fs::write(root.join("native-work-execution-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":case,"error":result.as_ref().err().map(ToString::to_string),"composer_preserved":preserved,"pending_seen":pending_seen})).unwrap()).unwrap();
        assert!(preserved);
        assert!(pending_seen);
        if case == "stop-hook-after-readback" {
            let error = result
                .expect_err("Claimed hook cancellation remains uncertain")
                .to_string();
            assert!(error.contains("cleanup=Ok(())"), "{error}");
            assert!(
                error.contains("Execution effects are uncertain; close without retry"),
                "{error}"
            );
            fs::write(
                root.join("native-work-execution-expected-stop-error.txt"),
                error,
            )
            .unwrap();
        } else {
            result.unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
        }
        let r = collect_report(&root);
        assert_eq!(r["requests"], json!([]));
        let commands = r["commands"].as_array().unwrap();
        assert_eq!(
            commands
                .iter()
                .filter(|c| c["type"] == "gui.work.native_execution_apply")
                .count(),
            1
        );
        assert_eq!(
            commands
                .iter()
                .filter(|c| c["type"] == "gui.work.native_execution_stop")
                .count(),
            1
        );
        assert_eq!(
            commands
                .iter()
                .filter(
                    |c| c["type"] == "gui.work.native_execution_readback" && c["scope"] == "launch"
                )
                .count(),
            1
        );
        if case == "stop-hook-refused" {
            assert_eq!(
                commands
                    .iter()
                    .filter(|c| c["type"] == "gui.work.native_execution_readback"
                        && c["scope"] == "stop")
                    .count(),
                1
            );
            assert_eq!(r["lifecycle"]["inputs"][0]["state"], "cancelled");
        } else {
            assert_eq!(r["hook_gate_retired"], true);
            assert_eq!(r["lifecycle"]["inputs"][0]["state"], "uncertain");
            assert_eq!(
                r["shutdown"]["result"]["work_execution_shutdown"]["disposition"],
                "uncertain"
            );
        }
        fs::write(
            root.join("native-work-execution-post-readback-stop-frames.txt"),
            frames.join("\n\nNEXT FRAME\n\n"),
        )
        .unwrap();
        eprintln!("Execution controls {case} report: {}", root.display());
    }
}

#[test]
#[ignore = "controlled Quit during actual initial capture response; dependency Python required"]
fn quit_before_initial_execution_activation_has_no_invocation_or_shutdown_receipt() {
    let case = "quit-capture";
    let (runtime, root) = launch(case);
    let mut frames = Vec::new();
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            app.insert_text("PRESERVE COMPOSER BEFORE CAPTURE ACTIVATION");
            pending_gate(runtime, &mut app, &root, false)?;
            assert!(!app.live.as_ref().unwrap().execution_pending());
            assert!(!app.live.as_ref().unwrap().busy);
            assert!(app.live.as_ref().unwrap().rows.is_empty());
            app.live.as_mut().unwrap().begin_quit();
            assert_eq!(
                app.composer.text(),
                "PRESERVE COMPOSER BEFORE CAPTURE ACTIVATION"
            );
            frames.push(draw(&mut app));
            runtime.quit()
        })
        .unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
    let r = collect_report(&root);
    assert_eq!(r["requests"], json!([]));
    assert_eq!(r["final_saved"], r["initial_saved"]);
    assert_eq!(r["history"], r["initial_history"]);
    assert!(r["final_saved"]["receipt"].is_null());
    assert_eq!(r["held_capture"]["execution_version"], 1);
    assert!(!r["commands"].as_array().unwrap().iter().any(|c| {
        c["type"]
            .as_str()
            .is_some_and(|v| v.starts_with("gui.work.native_execution_"))
    }));
    assert_eq!(r["lifecycle"]["active"], false);
    assert_eq!(r["lifecycle"]["producers"], json!([]));
    assert_eq!(r["lifecycle"]["inputs"], json!([]));
    fs::write(
        root.join("native-work-execution-controls-result.json"),
        serde_json::to_vec_pretty(
            &json!({"case":case,"error":null,"composer_preserved":true,"activated":false}),
        )
        .unwrap(),
    )
    .unwrap();
    fs::write(
        root.join("native-work-execution-capture-frames.txt"),
        frames.join("\n\nNEXT FRAME\n\n"),
    )
    .unwrap();
    eprintln!("Execution controls {case} report: {}", root.display());
}
