#![cfg(windows)]
//! Actual durable Work controls through the native F1 forms, review and readback.
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
    let actual = source();
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/work-controls-process-tests")
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
os.environ['RUST_WORK_SOURCE'] = {}
os.environ['RUST_WORK_CASE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()), quote(case), quote(&fixtures.join("work_controls_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let args = vec![
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
        "100".into(),
        "--server-timeout".into(),
        "5".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let deadline = Instant::now() + Duration::from_secs(105);
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
                    return Err(io::Error::other("Work controls observation deadline"));
                }
            }
        }
    }
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
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
fn confirm(
    runtime: &mut Interactive,
    app: &mut Demo,
    frames: &mut Vec<String>,
) -> io::Result<Value> {
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::ControlReview(_),
                ..
            })
        )
    })?;
    key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().work.take_intent().is_none(),
        "Unpainted review cannot confirm"
    );
    let mut dispatched = false;
    for _ in 0..80 {
        frames.push(draw(app));
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
            assert!(matches!(intent.request, work::Request::ControlApply(_)));
            runtime.work(intent)?;
            dispatched = true;
            break;
        }
    }
    if !dispatched {
        return Err(io::Error::other(
            "Complete visible control review never confirmed",
        ));
    }
    loop {
        let event = next(runtime)?;
        let saved = match &event {
            Observation::Work(work::Update {
                data: work::Data::ControlSaved { row, .. },
                ..
            }) => Some(row.clone()),
            Observation::Work(work::Update {
                data: work::Data::Rejected(reason),
                ..
            }) => return Err(io::Error::other(reason.clone())),
            _ => None,
        };
        app.observe_live(event)?;
        send(runtime, app)?;
        if let Some(row) = saved {
            let screen = draw(app);
            assert!(screen.contains("independently verified"));
            frames.push(screen);
            return Ok(row);
        }
    }
}
fn edit_selected(
    runtime: &mut Interactive,
    app: &mut Demo,
    frames: &mut Vec<String>,
) -> io::Result<Value> {
    select(runtime, app, "selected")?;
    press(runtime, app, KeyCode::Char('e'))?;
    assert!(draw(app).contains("Edit cron job"));
    paste(app, "UI edited prompt — exact selected row", true);
    frames.push(draw(app));
    press(runtime, app, KeyCode::Enter)?;
    confirm(runtime, app, frames)
}
fn create(
    runtime: &mut Interactive,
    app: &mut Demo,
    frames: &mut Vec<String>,
    kind: &str,
) -> io::Result<Value> {
    press(runtime, app, KeyCode::Char('c'))?;
    assert!(draw(app).contains(&format!("Create {kind} job")));
    paste(
        app,
        &format!("UI created {kind} prompt — never executed"),
        false,
    );
    press(runtime, app, KeyCode::Tab)?;
    paste(app, &format!("UI {kind} label"), false);
    press(runtime, app, KeyCode::Tab)?;
    paste(
        app,
        if kind == "cron" { "0 9 * * 1-5" } else { "23" },
        kind == "loop",
    );
    frames.push(draw(app));
    press(runtime, app, KeyCode::Enter)?;
    let row = confirm(runtime, app, frames)?;
    assert_eq!(row["kind"], kind);
    assert_eq!(row["enabled"], false);
    assert_eq!(row["run_count"], 0);
    Ok(row)
}
fn report(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("work-controls-report.json")).unwrap()).unwrap();
    for name in [
        "app",
        "native_work",
        "native_work_controls",
        "scheduler",
        "row_store",
        "goal_loop",
        "gui_rpc",
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
    for key in [
        "denied",
        "provider_requests",
        "questions",
        "approvals",
        "authorities",
        "producers",
        "inputs",
    ] {
        assert_eq!(r[key], json!([]), "{key} at {}", root.display());
    }
    assert_eq!(r["before"], r["after"]);
    assert_eq!(r["before"]["history"], r["durable"]);
    assert_eq!(r["startup_held"], true);
    assert_eq!(r["reader_retired"], true);
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(!r["lease_release"].as_array().unwrap().is_empty());
    for l in r["lease_release"].as_array().unwrap() {
        for key in ["active_execution", "questions", "approvals"] {
            assert_eq!(l[key], json!([]));
        }
    }
    assert!(
        r["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["unchanged"] == true)
    );
    let commands = r["commands"].as_array().unwrap();
    assert!(!commands.iter().any(|c| matches!(
        c["type"].as_str(),
        Some("prompt" | "gui.prompt.submit" | "gui.jobs.run" | "gui.work.native_settled")
    )));
    assert_eq!(
        commands
            .iter()
            .filter(|c| c["type"] == "gui.work.native_capture")
            .count(),
        1
    );
    assert_eq!(
        commands
            .iter()
            .filter(|c| c["type"] == "gui.work.native_activate")
            .count(),
        1
    );
    assert!(r["wire"].as_array().unwrap().iter().all(|row| !matches!(
        row["event"]["type"].as_str(),
        Some("work_producer" | "work_input" | "work_admission")
    )));
    r
}
fn scenario(case: &str) {
    let (runtime, root) = launch(case);
    let mut frames = Vec::new();
    let mut successes = Vec::new();
    let mut composer_preserved = false;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("KEEP COMPOSER AND SELECTION");
        app.composer.move_cursor(Motion::WordLeft, true);
        let composer = (
            app.composer.text().to_string(),
            app.composer.before_cursor().to_string(),
            app.composer.selected_text().to_string(),
        );
        // The F1 handler calls this exact entrypoint; all subsequent input uses Work's actual visible-key guards.
        app.open_work();
        send(runtime, &mut app)?;
        page(runtime, &mut app)?;
        tab(runtime, &mut app)?;
        tab(runtime, &mut app)?;
        let edited = edit_selected(runtime, &mut app, &mut frames)?;
        successes.push(json!({"action":"job_update","row":edited}));
        assert_eq!(edited["prompt"], "UI edited prompt — exact selected row");
        assert_eq!(edited["run_count"], 7);
        refresh(runtime, &mut app)?;
        let cron = create(runtime, &mut app, &mut frames, "cron")?;
        successes.push(json!({"action":"job_create","row":cron}));
        refresh(runtime, &mut app)?;
        select(runtime, &mut app, cron["id"].as_str().unwrap())?;
        press(runtime, &mut app, KeyCode::Char('d'))?;
        assert!(confirm(runtime, &mut app, &mut frames)?.is_null());
        successes.push(json!({"action":"job_delete","id":cron["id"]}));
        refresh(runtime, &mut app)?;
        tab(runtime, &mut app)?;
        let loop_row = create(runtime, &mut app, &mut frames, "loop")?;
        assert_eq!(loop_row["interval_minutes"], 23);
        successes.push(json!({"action":"job_create","row":loop_row}));
        for enabled in [true, false] {
            refresh(runtime, &mut app)?;
            select(runtime, &mut app, "loop-owned")?;
            press(runtime, &mut app, KeyCode::Char(' '))?;
            let row = confirm(runtime, &mut app, &mut frames)?;
            assert_eq!(row["enabled"], enabled);
            successes.push(json!({"action":"loop_enable","row":row}));
        }
        refresh(runtime, &mut app)?;
        select(runtime, &mut app, loop_row["id"].as_str().unwrap())?;
        press(runtime, &mut app, KeyCode::Char('d'))?;
        assert!(confirm(runtime, &mut app, &mut frames)?.is_null());
        successes.push(json!({"action":"job_delete","id":loop_row["id"]}));
        refresh(runtime, &mut app)?;
        tab(runtime, &mut app)?;
        press(runtime, &mut app, KeyCode::Char('p'))?;
        let goal = confirm(runtime, &mut app, &mut frames)?;
        assert_eq!(goal["status"], "paused");
        assert_eq!(goal["evaluated_turns"], 4);
        successes.push(json!({"action":"goal_pause","row":goal}));
        refresh(runtime, &mut app)?;
        press(runtime, &mut app, KeyCode::Char('d'))?;
        assert!(confirm(runtime, &mut app, &mut frames)?.is_null());
        successes.push(json!({"action":"goal_clear"}));
        assert_eq!(
            composer,
            (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string()
            )
        );
        composer_preserved = true;
        Ok(())
    });
    fs::write(
        root.join("native-work-controls-frames.txt"),
        frames
            .iter()
            .enumerate()
            .map(|(n, f)| format!("FRAME {n}\n{f}\n\n"))
            .collect::<String>(),
    )
    .unwrap();
    fs::write(root.join("native-work-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":case,"successes":successes,"composer_preserved":composer_preserved,"error":result.as_ref().err().map(ToString::to_string)})).unwrap()).unwrap();
    eprintln!("Work controls {case} report: {}", root.display());
    let r = report(&root);
    let commands = r["commands"].as_array().unwrap();
    let count = |suffix: &str| {
        commands
            .iter()
            .filter(|c| c["type"] == format!("gui.work.native_{suffix}"))
            .count()
    };
    if case == "readback-drift" {
        assert!(result.is_err() && successes.is_empty(), "{result:?}");
        assert_eq!(r["injected_readback_drift"], true);
        assert_eq!(count("control_apply"), 1);
        assert_eq!(count("control_readback"), 1);
        assert!(r["readbacks"].as_array().unwrap().is_empty());
        return;
    }
    result.unwrap();
    assert!(composer_preserved);
    assert_eq!(successes.len(), 9);
    for suffix in ["control_validate", "control_apply", "control_readback"] {
        assert_eq!(count(suffix), 9);
    }
    assert_eq!(r["readbacks"].as_array().unwrap().len(), 9);
    let rows = r["final_saved"]["jobs"].as_array().unwrap();
    assert_eq!(rows.len(), 3);
    assert_eq!(r["final_saved"]["goal"], Value::Null);
    assert_eq!(
        rows.iter().find(|r| r["id"] == "sibling").unwrap(),
        &r["peer_edit"]
    );
    let selected = rows.iter().find(|r| r["id"] == "selected").unwrap();
    assert_eq!(
        selected["future_extension"],
        json!({"opaque":["keep",1,true]})
    );
    assert_eq!(selected["last_fired_slot"], "2026-10-01T09:00");
    assert_eq!(
        rows.iter().find(|r| r["id"] == "loop-owned").unwrap()["enabled"],
        false
    );
}
#[test]
#[ignore = "controlled actual-source F1 durable Work controls; LITETUI_AUTHORITY_PYTHON required; LITETUI_TEST_RUNTIME_SOURCE optionally selects relocated runtime"]
fn owned_f1_forms_review_save_and_readback_preserve_history_composer_and_siblings() {
    scenario("success");
}
#[test]
#[ignore = "controlled actual-source F1 durable Work controls; dependency Python required"]
fn changed_independent_readback_never_announces_saved_configuration() {
    scenario("readback-drift");
}
