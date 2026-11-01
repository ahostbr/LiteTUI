#![cfg(windows)]
//! Actual canonical TaskStop, controlled child process, and independent source retirement.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options, WorkExecution},
    ui, work_view as work,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::PathBuf,
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
        .join("target/work-task-stop-process-tests")
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
os.environ['RUST_TASK_STOP_SOURCE'] = {}
os.environ['RUST_TASK_STOP_CASE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()), quote(case), quote(&fixtures.join("work_task_stop_runtime.py").to_string_lossy()))).unwrap();
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
fn response<'a>(report: &'a Value, kind: &str) -> &'a Value {
    let command = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["type"] == kind)
        .unwrap();
    &report["wire"]
        .as_array()
        .unwrap()
        .iter()
        .find(|r| r["type"] == "response" && r["id"] == command["id"])
        .unwrap()["result"]
}
#[test]
#[ignore = "controlled actual-source TaskStop process; dependency Python required"]
fn canonical_shell_stop_waits_for_process_pipes_worker_and_independent_readback() {
    let (runtime, root) = launch("process");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("KEEP COMPOSER AND SELECTION");
        app.composer.move_cursor(Motion::WordLeft, true);
        let draft = (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.open_work();
        send(runtime, &mut app)?;
        page(runtime, &mut app)?;
        let rows = &app.live.as_ref().unwrap().work.page.as_ref().unwrap().rows;
        assert_eq!(rows.len(), 1);
        assert!(rows[0].can_stop());
        let task_id = rows[0].id.clone();
        press(runtime, &mut app, KeyCode::Char('k'))?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Work(work::Update { data: work::Data::StopReview(_), .. })))?;
        let mut shown = String::new();
        let mut sent = false;
        for _ in 0..100 {
            shown.push_str(&draw(&mut app));
            key(&mut app, KeyCode::Enter);
            if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
                assert!(matches!(intent.request, work::Request::StopApply(_)));
                runtime.work(intent)?;
                sent = true;
                break;
            }
        }
        assert!(sent && shown.contains("CONTROLLED OWNED TASK") && shown.contains("lifecycle_instance"));
        let deadline = Instant::now() + Duration::from_secs(20);
        while !root.join("readback-waiting").exists() {
            match runtime.events.recv_timeout(Duration::from_millis(20)) {
                Ok(event) => {
                    assert!(!matches!(&event, Observation::Work(work::Update {data: work::Data::Stopped(_), ..})));
                    app.observe_live(event)?;
                    send(runtime, &mut app)?;
                }
                Err(_) => { if let Some(done) = runtime.poll_done() { return Err(done.err().unwrap_or_else(|| io::Error::other("TaskStop exited before readback"))); } }
            }
            assert!(Instant::now() < deadline, "TaskStop independent readback did not arrive at {}", root.display());
        }
        // The source has verified the durable result but has not published its
        // independent response. Drain interleaved lifecycle events during this
        // interval; a transport ACK alone must never publish Stopped.
        let hold = Instant::now() + Duration::from_millis(150);
        while Instant::now() < hold {
            if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(20)) {
                assert!(!matches!(&event, Observation::Work(work::Update { data: work::Data::Stopped(_), .. })));
                app.observe_live(event)?;
                send(runtime, &mut app)?;
            }
        }
        assert!(app.live.as_ref().unwrap().work.waiting);
        assert!(!draw(&mut app).contains("Task Stop independently verified"));
        fs::write(root.join("release-readback"), b"release")?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Work(work::Update {data: work::Data::Stopped(row), ..}) if row.id == task_id))?;
        assert!(!app.live.as_ref().unwrap().work.waiting);
        assert!(draw(&mut app).contains("Task Stop independently verified"));
        fs::write(root.join("release-answer"), b"release")?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(update) if matches!(update.as_ref(), WorkExecution::Settled{..})))?;
        let chat = app.live.as_ref().unwrap();
        assert_eq!(chat.rows.len(), 1);
        assert!(chat.rows[0].source_owned && chat.rows[0].prompt.contains(&task_id));
        assert_eq!(chat.rows[0].cards[0].answer, "CONTROLLED STOP COMPLETION ANSWER");
        assert!(chat.work.open);
        assert_eq!(draft, (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned()));
        Ok(())
    });
    if let Err(error) = &result {
        eprintln!("TaskStop process {}: {error}", root.display());
    }
    result.unwrap();
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("task-stop-report.json")).unwrap()).unwrap();
    for name in [
        "app",
        "background_input",
        "gui_rpc",
        "native_work",
        "tasks",
        "core_tools",
    ] {
        let relative = if name == "core_tools" {
            "plugins/core_tools.py".to_owned()
        } else {
            format!("{name}.py")
        };
        assert_eq!(
            PathBuf::from(r[format!("source_{name}")].as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join("src/litetui")
                .join(relative)
                .canonicalize()
                .unwrap()
        );
    }
    for key in ["denied", "questions", "approvals", "authorities"] {
        assert_eq!(r[key], json!([]), "{key}: {}", root.display());
    }
    assert_eq!(r["reader_retired"], true);
    assert_eq!(r["review_without_effect"], true);
    let arguments = r["launch_arguments"].as_array().unwrap();
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == [json!("--work-lifecycle-version"), json!("1")])
    );
    assert!(
        r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|row| row["type"] == "ready" && row["work_lifecycle_version"] == 1)
    );
    assert!(r.get("emergency_cleanup").is_none());
    assert_eq!(r["history"], r["durable"]);
    assert_eq!(r["requests"].as_array().unwrap().len(), 1);
    assert_eq!(r["kills"].as_array().unwrap().len(), 1);
    assert_eq!(r["processes"].as_array().unwrap().len(), 1);
    let process = &r["processes"][0];
    for key in ["retired", "stdout_closed", "stderr_closed"] {
        assert_eq!(process[key], true, "{key}");
    }
    for key in [
        "process_exited",
        "pipes_settled",
        "kill_requested",
        "kill_confirmed",
    ] {
        assert_eq!(process["evidence"][key], true, "{key}");
    }
    assert_eq!(r["kills"][0]["pid"], process["pid"]);
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(
        r["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["closed"] == true)
    );
    assert!(!r["lease_release"].as_array().unwrap().is_empty());
    assert!(r["lease_release"].as_array().unwrap().iter().all(|l| {
        [
            "active_workers",
            "questions",
            "approvals",
            "processes_alive",
        ]
        .iter()
        .all(|k| l[k] == json!([]))
    }));
    for key in ["producers", "inputs"] {
        assert_eq!(r["lifecycle"][key].as_array().unwrap().len(), 1);
        assert!(
            r["lifecycle"][key]
                .as_array()
                .unwrap()
                .iter()
                .all(|v| v["worker_finished"] == true && v["observer_done"] == true)
        );
    }
    assert_eq!(r["lifecycle"]["inputs"][0]["state"], "settled");
    assert_eq!(r["shutdown"]["ok"], true);
    for key in [
        "worker_settled",
        "process_settled",
        "callback_settled",
        "admission_closed",
    ] {
        assert_eq!(r["shutdown"]["result"]["work_shutdown"][key], true, "{key}");
    }
    for kind in [
        "gui.work.native_stop_validate",
        "gui.work.native_stop_apply",
        "gui.work.native_stop_settled",
    ] {
        assert_eq!(
            r["commands"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|c| c["type"] == kind)
                .count(),
            1
        );
    }
    assert!(
        !r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["type"] == "prompt")
    );
    let applied = response(&r, "gui.work.native_stop_apply");
    let settled = response(&r, "gui.work.native_stop_settled");
    assert_eq!(applied["status"], "stopped");
    assert_eq!(settled, &r["independent_readback"]);
    assert_eq!(applied["row"], settled["row"]);
    assert_eq!(applied["proof"], settled["proof"]);
    assert_eq!(applied["proof"]["process_id"], process["pid"]);
    assert_eq!(applied["row"]["row"], r["tasks"][0]);
    assert_eq!(r["tasks"][0]["state"], "killed");
    assert!(
        r["durable_tasks"]
            .as_array()
            .unwrap()
            .iter()
            .any(|t| t == &r["tasks"][0])
    );
}
