#![cfg(windows)]
//! Actual owned Codex/Work reconciliation crosses native selection and published readback.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options, WorkExecution},
    live_chat::ReplyState,
    restored_queue::{Intent, Update},
    ui,
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
fn launch(phase: &str, resume: Option<(&Path, &str)>) -> (Interactive, PathBuf) {
    let actual = source();
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/work-reconciliation-process-tests")
        .join(format!(
            "{}-{}-{}-{phase}",
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
os.environ['RUST_RECONCILIATION_SOURCE'] = {}
os.environ['RUST_RECONCILIATION_PHASE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()), quote(phase), quote(&fixtures.join("work_reconciliation_runtime.py").to_string_lossy()))).unwrap();
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
        "codex".into(),
        "--codex-engine".into(),
        "native".into(),
        "--model".into(),
        "gpt-6.1-sol".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--thinking-level".into(),
        "high".into(),
        "--tools".into(),
        "off".into(),
        "--timeout".into(),
        "100".into(),
    ];
    if let Some((_, conversation)) = resume {
        args.extend(["--resume".into(), conversation.into()]);
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}

fn observe(runtime: &mut Interactive, deadline: Instant) -> io::Result<Option<Observation>> {
    match runtime.events.recv_timeout(Duration::from_millis(30)) {
        Ok(event) => Ok(Some(event)),
        Err(_) => {
            if let Some(done) = runtime.poll_done() {
                return Err(done.err().unwrap_or_else(|| {
                    io::Error::other("Unexpected reconciliation runtime exit")
                }));
            }
            if Instant::now() > deadline {
                return Err(io::Error::other("Reconciliation observation deadline"));
            }
            Ok(None)
        }
    }
}
fn wait(
    runtime: &mut Interactive,
    app: &mut Demo,
    predicate: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(90);
    loop {
        if let Some(event) = observe(runtime, deadline)? {
            let done = predicate(&event);
            app.observe_live(event)?;
            if done {
                return Ok(());
            }
        }
    }
}
fn file(runtime: &mut Interactive, app: &mut Demo, path: &Path) -> io::Result<Value> {
    let deadline = Instant::now() + Duration::from_secs(30);
    while !path.exists() {
        if let Some(event) = observe(runtime, deadline)? {
            app.observe_live(event)?;
        }
    }
    Ok(serde_json::from_slice(&fs::read(path)?)?)
}
fn draft(app: &Demo) -> (String, String, String) {
    (
        app.composer.text().into(),
        app.composer.before_cursor().into(),
        app.composer.selected_text().into(),
    )
}
fn start_unknown(
    runtime: &mut Interactive,
    app: &mut Demo,
    root: &Path,
    phase: &str,
) -> io::Result<()> {
    wait(runtime, app, |e| matches!(e, Observation::Ready))?;
    let chat = app.live.as_mut().unwrap();
    chat.submit("Controlled parent for uncertain Work delivery")?;
    runtime.submit(chat.take_submission().unwrap())?;
    file(runtime, app, &root.join(format!("{phase}-uncertain.json")))?;
    Ok(())
}
fn reconcile(
    runtime: &mut Interactive,
    app: &mut Demo,
    root: &Path,
    phase: &str,
) -> io::Result<()> {
    app.insert_text("PRESERVE COMPOSER DURING RECONCILIATION");
    app.composer.move_cursor(Motion::WordLeft, true);
    let original = draft(app);
    app.open_queue(); // Same entry point as the native F7 key.
    let open = app.live.as_mut().unwrap().queue.take_intent().unwrap();
    assert_eq!(open, Intent::Open);
    runtime.queue(open)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Queue(Update::Snapshot(s)) if s.entries.len()==1),
    )?;
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    let chat = app.live.as_mut().unwrap();
    chat.queue
        .key(KeyEvent::new(KeyCode::Char('h'), KeyModifiers::NONE));
    assert!(chat.queue.take_intent().is_none()); // Review precedes explicit Enter.
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    let chat = app.live.as_mut().unwrap();
    chat.queue
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let selected = chat.queue.take_intent().ok_or_else(|| {
        io::Error::other("Native recovery review did not produce explicit reconcile intent")
    })?;
    assert!(matches!(selected, Intent::Reconcile { .. }));
    chat.prepare_reconciliation(&selected)?;
    runtime.queue(selected)?;
    let held = file(
        runtime,
        app,
        &root.join(format!("{phase}-readback-held.json")),
    )?;
    assert!(held["selection"]["input_id"].is_string());
    assert_eq!(draft(app), original);
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .rows
            .iter()
            .all(|r| !r.source_owned)
    );
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .rows
            .iter()
            .all(|r| r.state != ReplyState::Reconciled)
    );
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    fs::write(
        root.join(format!("{phase}-release-readback")),
        b"publish exact readback",
    )?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Queue(Update::Snapshot(s)) if s.entries.is_empty() && s.recovered.is_some()),
    )?;
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .rows
            .iter()
            .filter(|r| r.state == ReplyState::Reconciled)
            .count(),
        1
    );
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::WorkExecution(u) if matches!(u.as_ref(),WorkExecution::Settled{..})),
    )?;
    let chat = app.live.as_ref().unwrap();
    let row = chat.rows.iter().find(|r| r.source_owned).unwrap();
    assert!(
        row.prompt
            .contains("FRESH BACKGROUND RESULT AFTER RECONCILIATION")
    );
    assert_eq!(row.state, ReplyState::Complete);
    assert_eq!(row.cards[0].answer, "AFTER VERIFIED RECONCILIATION");
    assert_eq!(draft(app), original);
    Ok(())
}
fn report(root: &Path, phase: &str) -> Value {
    let value: Value = serde_json::from_slice(
        &fs::read(root.join(format!("{phase}-work-reconciliation-report.json"))).unwrap(),
    )
    .unwrap();
    for name in [
        "background_input",
        "codex_steering",
        "gui_rpc",
        "restored_input",
    ] {
        assert_eq!(
            PathBuf::from(value[format!("source_{name}")].as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join(format!("src/litetui/{name}.py"))
                .canonicalize()
                .unwrap()
        );
    }
    for key in ["denied", "questions", "approvals", "authorities"] {
        assert_eq!(value[key], json!([]), "{key} at {}", root.display());
    }
    assert_eq!(value["history"], value["durable"]);
    assert_eq!(value["reader_retired"], true);
    assert_eq!(value["helpers_retired"], true);
    assert!(
        value["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["retired"] == true)
    );
    assert!(
        value["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["retired"] == true)
    );
    assert_eq!(value["lifecycle"]["producers_retired"], true);
    assert!(
        value["lifecycle"]["inputs"]
            .as_array()
            .unwrap()
            .iter()
            .all(|i| i["worker_retired"] == true && i["observer_retired"] == true)
    );
    assert!(
        value["lease_release"]
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["active"] == json!([])
                && r["questions"] == json!([])
                && r["approvals"] == json!([]))
    );
    assert_eq!(value["shutdown"]["ok"], true);
    assert_eq!(
        value["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
    let commands = value["commands"].as_array().unwrap();
    for kind in ["gui.work.native_capture", "gui.work.native_activate"] {
        assert_eq!(commands.iter().filter(|c| c["type"] == kind).count(), 1);
    }
    value
}
fn verify_success(value: &Value, recovered: bool) {
    assert_eq!(
        value["checks"],
        json!([
            "admission-held-through-independent-readback",
            "admission-held-at-response-publication"
        ])
    );
    assert_eq!(value["pending"], 0);
    assert_eq!(value["lifecycle"]["readbacks"], 0);
    let inputs = value["lifecycle"]["inputs"].as_array().unwrap();
    assert_eq!(inputs.len(), 2);
    let old = inputs.iter().find(|i| i["state"] == "reconciled").unwrap();
    assert_eq!(old["recovered"], recovered);
    assert!(old["admission"].is_null());
    let wire = value["wire"].as_array().unwrap();
    let event = wire
        .iter()
        .find(|e| e["type"] == "work_reconciled")
        .unwrap();
    let native_id = &event["reconciliation"]["delivery_id"];
    assert_eq!(
        value["history"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|m| &m["codex_delivery"]["id"] == native_id)
            .count(),
        1
    );
    let published = wire
        .iter()
        .position(|e| {
            e["type"] == "response" && e["result"].get("work_reconciliation_readback").is_some()
        })
        .unwrap();
    let admitted = wire
        .iter()
        .position(|e| e["type"] == "work_admission")
        .unwrap();
    assert!(published < admitted);
    assert_eq!(
        value["provider"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["method"] == "turn/steer")
            .count(),
        usize::from(!recovered)
    );
    assert_eq!(
        value["provider"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["method"] == "turn/start")
            .count(),
        if recovered { 1 } else { 2 }
    );
}
#[test]
#[ignore = "actual controlled Codex/Work process; dependency Python required"]
fn current_unknown_delivery_requires_native_selection_and_published_readback() {
    let (runtime, root) = launch("current", None);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("codex".into(), "gpt-6.1-sol".into());
            start_unknown(runtime, &mut app, &root, "current")?;
            reconcile(runtime, &mut app, &root, "current")
        })
        .unwrap_or_else(|error| panic!("{}: {error}", root.display()));
    verify_success(&report(&root, "current"), false);
}
#[test]
#[ignore = "actual controlled Codex/Work restart processes; dependency Python required"]
fn recovered_unknown_delivery_cannot_replay_and_recovers_only_after_explicit_readback() {
    let (runtime, root) = launch("prepare", None);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("codex".into(), "gpt-6.1-sol".into());
            start_unknown(runtime, &mut app, &root, "prepare")
        })
        .unwrap_or_else(|error| panic!("{}: {error}", root.display()));
    let old = report(&root, "prepare");
    assert_eq!(old["pending"], 1);
    assert_eq!(old["lifecycle"]["inputs"][0]["state"], "uncertain");
    let conversation = old["session_id"].as_str().unwrap();
    let (runtime, same_root) = launch("recovered", Some((&root, conversation)));
    assert_eq!(root, same_root);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("codex".into(), "gpt-6.1-sol".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            reconcile(runtime, &mut app, &root, "recovered")
        })
        .unwrap_or_else(|error| panic!("{}: {error}", root.display()));
    let recovered = report(&root, "recovered");
    assert_eq!(
        recovered["lifecycle"]["inputs"][0]["epoch"]["instance"],
        old["lifecycle"]["instance"]
    );
    verify_success(&recovered, true);
}
