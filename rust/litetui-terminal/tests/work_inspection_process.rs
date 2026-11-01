#![cfg(windows)]
//! Actual owned Python Work controls through the native Runtime and view.
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
        .join("target/work-inspection-process-tests")
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
"#, quote(&actual.to_string_lossy()), quote(case), quote(&fixtures.join("work_inspection_runtime.py").to_string_lossy()))).unwrap();
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
                    return Err(io::Error::other("Work observation deadline"));
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
        .iter()
        .map(|c| c.symbol())
        .collect()
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
fn inspect(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_work();
    send(runtime, app)?;
    page(runtime, app)?;
    let s = app.live.as_ref().unwrap().work.snapshot.as_ref().unwrap();
    assert!(s.busy && s.can_pause && !s.paused);
    assert_eq!(s.sources[0].count, 35);
    assert!(!s.sources[1].available);
    assert_eq!(s.goal["value"]["objective"], "Inspect paused goal");
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .work
            .page
            .as_ref()
            .unwrap()
            .rows
            .len(),
        32
    );
    press(runtime, app, KeyCode::Char('n'))?;
    page(runtime, app)?;
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .work
            .page
            .as_ref()
            .unwrap()
            .offset,
        32
    );
    press(runtime, app, KeyCode::Char('b'))?;
    page(runtime, app)?;
    press(runtime, app, KeyCode::Char('t'))?;
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::Tail(_),
                ..
            })
        )
    })?;
    let tail = app.live.as_ref().unwrap().work.tail.as_ref().unwrap();
    assert_eq!(tail.status, "available");
    assert!(tail.offset > 0 && tail.text.ends_with("CANONICAL WORK TAIL"));
    assert!(!tail.path.contains("untrusted-display-path"));
    press(runtime, app, KeyCode::Esc)?;
    press(runtime, app, KeyCode::Tab)?; // Provider unavailability is local source truth.
    assert!(app.live.as_ref().unwrap().work.notice.contains("provider"));
    press(runtime, app, KeyCode::Tab)?;
    page(runtime, app)?;
    assert_eq!(
        app.live.as_ref().unwrap().work.page.as_ref().unwrap().total,
        3
    );
    press(runtime, app, KeyCode::Tab)?;
    page(runtime, app)?;
    assert_eq!(
        app.live.as_ref().unwrap().work.page.as_ref().unwrap().total,
        1
    );
    press(runtime, app, KeyCode::Tab)?; // Goal is part of the immutable snapshot.
    assert!(draw(app).contains("Inspect paused goal"));
    press(runtime, app, KeyCode::Tab)?;
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::Calendar(_),
                ..
            })
        )
    })?;
    let c = app.live.as_ref().unwrap().work.calendar.as_ref().unwrap();
    assert_eq!(c.days.len(), 42);
    assert!(
        c.days
            .iter()
            .filter(|d| d.date.is_some())
            .all(|d| d.total == 3)
    );
    press(runtime, app, KeyCode::Enter)?;
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::Calendar(_),
                ..
            })
        )
    })?;
    let c = app.live.as_ref().unwrap().work.calendar.as_ref().unwrap();
    assert!(c.date.is_some());
    assert_eq!(c.rows.len(), 3);
    assert!(c.rows.iter().any(|r| r.broken));
    press(runtime, app, KeyCode::Char('p'))?;
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Work(work::Update {
                data: work::Data::Review(_),
                ..
            })
        )
    })?;
    key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().work.take_intent().is_none(),
        "Unpainted review cannot confirm"
    );
    for _ in 0..30 {
        draw(app);
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
            assert!(matches!(intent.request, work::Request::PauseApply(_)));
            return runtime.work(intent);
        }
    }
    Err(io::Error::other("Complete visible review never confirmed"))
}
fn report(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("work-inspection-report.json")).unwrap())
            .unwrap();
    for name in ["app", "native_work", "gui_rpc"] {
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
        assert_eq!(r[key], json!([]), "{key} at {}", root.display());
    }
    assert_eq!(r["requests"].as_array().unwrap().len(), 1);
    assert_eq!(r["before"], r["after_work"]);
    assert_eq!(r["before"], r["after"]);
    assert_eq!(r["history"], r["durable"]);
    assert_eq!(r["lifecycle_active"], true);
    assert_eq!(r["startup_held"], true);
    assert_eq!(r["lifecycle_producers"], 0);
    assert_eq!(r["lifecycle_inputs"], 0);
    assert!(
        r["launch_arguments"]
            .as_array()
            .unwrap()
            .windows(2)
            .any(|pair| pair == [json!("--work-lifecycle-version"), json!("1")])
    );
    assert_eq!(r["reader_retired"], true);
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert_eq!(r["streams"], json!([{"closed":true}]));
    let leases = r["lease_release"].as_array().unwrap();
    assert!(!leases.is_empty());
    assert!(leases.iter().all(|l| l["active_chat"] == json!([])
        && l["questions"] == json!([])
        && l["approvals"] == json!([])));
    assert!(
        r["checks"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["busy"] == true && c["unchanged"] == true)
    );
    let commands = r["commands"].as_array().unwrap();
    for kind in ["gui.work.native_capture", "gui.work.native_activate"] {
        assert_eq!(commands.iter().filter(|c| c["type"] == kind).count(), 1);
    }
    assert!(!commands.iter().any(|c| matches!(
        c["type"].as_str(),
        Some("gui.work.native_settled" | "gui.work.cancel")
    )));
    let activation = commands
        .iter()
        .find(|c| c["type"] == "gui.work.native_activate")
        .unwrap();
    let wire = r["wire"].as_array().unwrap();
    let published = wire
        .iter()
        .position(|row| {
            row["type"] == "response" && row["id"] == activation["id"] && row["ok"] == true
        })
        .unwrap();
    assert!(
        wire.iter()
            .take(published + 1)
            .all(|row| !row["type"].as_str().unwrap_or("").starts_with("work_"))
    );
    assert!(wire.iter().all(|row| !matches!(
        row["type"].as_str(),
        Some("work_producer" | "work_input" | "work_admission")
    )));
    r
}
fn scenario(case: &str) {
    let (runtime, root) = launch(case);
    let mut paused = false;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let chat = app.live.as_mut().unwrap();
        chat.submit("Controlled ordinary chat for Work inspection")?;
        runtime.submit(chat.take_submission().unwrap())?;
        wait(
            runtime,
            &mut app,
            |e| matches!(e,Observation::Content{text,..} | Observation::Text(text) | Observation::Snapshot(text) if text.contains("WORK CHAT BUSY")),
        )?;
        app.insert_text("KEEP COMPOSER AND SELECTION");
        app.composer.move_cursor(Motion::WordLeft, true);
        let composer = (
            app.composer.text().to_string(),
            app.composer.before_cursor().to_string(),
            app.composer.selected_text().to_string(),
        );
        inspect(runtime, &mut app)?;
        assert_eq!(
            composer,
            (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string()
            )
        );
        if case == "quit-after-pause" {
            let deadline = Instant::now() + Duration::from_secs(20);
            while !root.join("work-stage.json").exists() {
                if Instant::now() > deadline {
                    return Err(io::Error::other("Pause buffer was not reached"));
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            runtime.quit()?;
            return Ok(());
        }
        wait(runtime, &mut app, |e| {
            matches!(
                e,
                Observation::Work(work::Update {
                    data: work::Data::Paused,
                    ..
                })
            )
        })?;
        paused = true;
        assert!(
            app.live
                .as_ref()
                .unwrap()
                .work
                .snapshot
                .as_ref()
                .unwrap()
                .paused
        );
        wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        Ok(())
    });
    if let Err(error) = &result {
        eprintln!("Work runtime at {}: {error}", root.display());
    }
    let r = report(&root);
    let commands = r["commands"].as_array().unwrap();
    let count = |kind: &str| commands.iter().filter(|c| c["type"] == kind).count();
    assert_eq!(count("gui.work.native_pause_apply"), 1);
    assert_eq!(
        count("gui.work.native_pause_readback"),
        usize::from(case != "quit-after-pause")
    );
    if case == "readback-drift" {
        assert!(result.is_err() && !paused, "{result:?}");
    } else {
        result.unwrap();
        assert_eq!(paused, case == "success");
        assert_eq!(r["pause_after"], true);
    }
}
#[test]
#[ignore = "controlled actual-source Work process; dependency Python required"]
fn busy_chat_inspection_and_reviewed_pause_preserve_saved_work_and_composer() {
    scenario("success");
}
#[test]
#[ignore = "controlled actual-source Work process; dependency Python required"]
fn changed_pause_readback_never_reports_success() {
    scenario("readback-drift");
}
#[test]
#[ignore = "controlled actual-source Work process; dependency Python required"]
fn quit_drains_applied_pause_without_readback_or_success() {
    scenario("quit-after-pause");
}
