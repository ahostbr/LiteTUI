#![cfg(windows)]
//! Compiled native slash route, painted review and actual source SDK lifecycle; no provider calls.
use litetui_terminal::{
    approvals,
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};
fn launch() -> (Interactive, PathBuf) {
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source = std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate_root.join("../.."))
        .canonicalize()
        .unwrap();
    let base = crate_root
        .join("target/approval-remember-tests")
        .join(format!(
            "{}-{}",
            std::process::id(),
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    let quote = |p: &Path| serde_json::to_string(&p.to_string_lossy()).unwrap();
    fs::write(
        package.join("__init__.py"),
        format!("__path__.append({})\n", quote(&source.join("src/litetui"))),
    )
    .unwrap();
    let fixture = crate_root.join("tests/fixtures/approval_remember_runtime.py");
    fs::write(package.join("cli.py"),format!(r#"import json,os,runpy
from pathlib import Path
os.environ.update(RUST_RUNTIME_CONTROLS_SOURCE={},RUST_RUNTIME_CONTROLS_CASE='approval-remember',PYTHONDONTWRITEBYTECODE='1')
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False,tool_auto_background_s=0)
p.write_text(json.dumps(s),encoding='utf-8')
runpy.run_path({},run_name='__main__')
"#,quote(&source),quote(&fixture))).unwrap();
    let root = base.join("data");
    let workspace = base.join("workspace");
    fs::create_dir(&workspace).unwrap();
    let args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("controlled dependency Python"),
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
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
        "--tools".into(),
        "on".into(),
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "120".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-timeout".into(),
        "5".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn wait(
    runtime: &mut Interactive,
    app: &mut Demo,
    until: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(50)) {
            let done = until(&event);
            app.observe_live(event)?;
            if done {
                return Ok(());
            }
        } else if let Some(done) = runtime.poll_done() {
            return Err(done
                .err()
                .unwrap_or_else(|| io::Error::other("Unexpected approval fixture exit")));
        } else if Instant::now() > deadline {
            return Err(io::Error::other("Approval fixture timeout"));
        }
    }
}
fn draw(app: &mut Demo) -> String {
    let mut term = Terminal::new(TestBackend::new(120, 42)).unwrap();
    term.draw(|f| ui::draw(f, app)).unwrap();
    term.backend()
        .buffer()
        .content
        .chunks(120)
        .map(|r| r.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
#[test]
#[ignore = "actual source with private provider and in-memory host tool; requires LITETUI_AUTHORITY_PYTHON"]
fn always_allow_is_saved_for_exact_scope_and_wider_request_still_asks() {
    let (runtime, root) = launch();
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.live
                .as_mut()
                .unwrap()
                .submit("First explicit private fixture tool")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::Approval(approvals::Event::Requested(_)))
            })?;
            app.insert_text("UNSENT preserved draft");
            app.composer.move_cursor(Motion::Left, true);
            let draft = app.composer.text().to_owned();
            let caret = app.composer.before_cursor().len();
            let selection = app.composer.selected_text().to_owned();
            app.live
                .as_mut()
                .unwrap()
                .approvals
                .current_mut()
                .unwrap()
                .select(2);
            app.live.as_mut().unwrap().approvals.confirm();
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .approvals
                    .take_decision()
                    .is_none()
            );
            for _ in 0..20 {
                draw(&mut app);
                app.live
                    .as_mut()
                    .unwrap()
                    .approvals
                    .current_mut()
                    .unwrap()
                    .scroll += 5;
            }
            assert!(
                app.live
                    .as_ref()
                    .unwrap()
                    .approvals
                    .current()
                    .unwrap()
                    .reviewed
            );
            app.live.as_mut().unwrap().approvals.confirm();
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .approvals
                    .take_decision()
                    .is_none()
            );
            assert!(draw(&mut app).contains("Confirm always"));
            app.live.as_mut().unwrap().approvals.confirm();
            runtime.approve(
                app.live
                    .as_mut()
                    .unwrap()
                    .approvals
                    .take_decision()
                    .unwrap(),
            )?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            assert_eq!(app.composer.text(), draft);
            assert_eq!(app.composer.before_cursor().len(), caret);
            assert_eq!(app.composer.selected_text(), selection);
            app.live
                .as_mut()
                .unwrap()
                .submit("Second explicit same-scope private tool")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            app.live
                .as_mut()
                .unwrap()
                .submit("Third explicit wider-scope private tool")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::Approval(approvals::Event::Requested(_)))
            })?;
            let pending = app.live.as_ref().unwrap().approvals.current().unwrap();
            assert_eq!(
                pending.request.remember_scope.as_ref().unwrap()["capabilities"],
                json!(["process_execution", "workspace_write"])
            );
            app.live.as_mut().unwrap().approvals.choose(false);
            runtime.approve(
                app.live
                    .as_mut()
                    .unwrap()
                    .approvals
                    .take_decision()
                    .unwrap(),
            )?;
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::ApprovalStopped(_))
            })?;
            runtime.quit()
        })
        .unwrap();
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
            .unwrap();
    assert_eq!(
        report["private_tool_calls"],
        json!([{"wider":false},{"wider":false}])
    );
    let requests = report["wire"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["type"] == "tool_approval_requested")
        .count();
    assert_eq!(requests, 2);
    assert!(
        report["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["type"] == "tool_approval_closed" && e["remembered"]["saved"] == true)
    );
    assert_eq!(report["reader_retired"], true);
    assert_eq!(report["helper_retired"], true);
    assert_eq!(report["approvals"], json!([]));
    assert_eq!(report["authorities"], json!([]));
    assert!(report["denied"].as_array().unwrap().is_empty());
}
