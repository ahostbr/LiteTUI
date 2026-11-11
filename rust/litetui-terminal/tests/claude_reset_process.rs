#![cfg(windows)]
//! Compiled native slash route, painted review and actual source SDK lifecycle; no provider calls.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    claude_reset,
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options},
    settings_view, ui,
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
    let base = crate_root.join("target/claude-reset-tests").join(format!(
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
    let fixture = crate_root.join("tests/fixtures/claude_reset_runtime.py");
    fs::write(package.join("cli.py"),format!(r#"import json,os,runpy
from pathlib import Path
os.environ.update(RUST_RUNTIME_CONTROLS_SOURCE={},RUST_RUNTIME_CONTROLS_CASE='claude-reset',PYTHONDONTWRITEBYTECODE='1')
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
        "claude".into(),
        "--model".into(),
        "claude-fixture".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--tools".into(),
        "off".into(),
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "120".into(),
        "--server-timeout".into(),
        "5".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    if let Some(intent) = app.live.as_mut().unwrap().commands.take_intent() {
        runtime.commands(intent)?;
    }
    if let Some(intent) = app.live.as_mut().unwrap().settings.take_intent() {
        runtime.settings(intent)?;
    }
    Ok(())
}
fn wait(
    runtime: &mut Interactive,
    app: &mut Demo,
    until: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(45);
    loop {
        if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(50)) {
            if let Observation::Settings(settings_view::Update::ClaudeReset(Err(reason))) = &event {
                return Err(io::Error::other(reason.clone()));
            }
            let done = until(&event);
            app.observe_live(event)?;
            send(runtime, app)?;
            if done {
                return Ok(());
            }
        } else if let Some(done) = runtime.poll_done() {
            return Err(done
                .err()
                .unwrap_or_else(|| io::Error::other("Unexpected reset runtime exit")));
        } else if Instant::now() > deadline {
            return Err(io::Error::other("Reset event timeout"));
        }
    }
}
fn draw(app: &mut Demo) -> String {
    let mut term = Terminal::new(TestBackend::new(120, 40)).unwrap();
    term.draw(|f| ui::draw(f, app)).unwrap();
    term.backend()
        .buffer()
        .content
        .chunks(120)
        .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn review(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.live.as_mut().unwrap().commands.raw("/claude new");
    send(runtime, app)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Settings(settings_view::Update::ClaudeReset(Ok(update))) if matches!(**update,claude_reset::Update::Review(_))),
    )
}
#[test]
#[ignore = "compiled Interactive with controlled pinned SDK transport; requires LITETUI_AUTHORITY_PYTHON, no provider"]
fn claude_new_preserves_same_conversation_and_draft_until_explicit_next_prompt() {
    let (runtime, root) = launch();
    let mut frames = Vec::new();
    let result=runtime.with_cleanup(|runtime|{
        let mut app=Demo::live("claude".into(),"claude-fixture".into());
        wait(runtime,&mut app,|e|matches!(e,Observation::Ready))?;
        app.live.as_mut().unwrap().submit("First explicit prompt establishes SDK session")?;
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        wait(runtime,&mut app,|e|matches!(e,Observation::Complete))?;
        app.insert_text("PRESERVE editable draft 🐾");app.composer.move_cursor(Motion::Left,true);
        let draft=app.composer.text().to_owned();let caret=app.composer.before_cursor().len();let selection=app.composer.selected_text().to_owned();
        review(runtime,&mut app)?;
        assert!(app.live.as_mut().unwrap().settings.take_intent().is_none());
        key(&mut app,KeyCode::Enter);assert!(app.live.as_mut().unwrap().settings.take_intent().is_none(),"unpainted reset must not apply");
        frames.push(draw(&mut app));assert!(frames.last().unwrap().contains("Claude session reset"));
        key(&mut app,KeyCode::Esc);key(&mut app,KeyCode::Esc);
        assert_eq!(app.composer.text(),draft);assert_eq!(app.composer.before_cursor().len(),caret);assert_eq!(app.composer.selected_text(),selection);
        review(runtime,&mut app)?;
        for _ in 0..32{frames.push(draw(&mut app));key(&mut app,KeyCode::PageDown);}
        key(&mut app,KeyCode::Enter);send(runtime,&mut app)?;
        wait(runtime,&mut app,|e|matches!(e,Observation::Settings(settings_view::Update::ClaudeReset(Ok(update))) if matches!(**update,claude_reset::Update::Saved(_))))?;
        frames.push(draw(&mut app));assert!(frames.last().unwrap().contains("reset verified"));
        assert_eq!(app.composer.text(),draft);assert_eq!(app.composer.before_cursor().len(),caret);assert_eq!(app.composer.selected_text(),selection);
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        key(&mut app,KeyCode::Esc);key(&mut app,KeyCode::Esc);
        app.live.as_mut().unwrap().submit("Second explicit prompt opens the fresh Claude session")?;
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        wait(runtime,&mut app,|e|matches!(e,Observation::Complete))?;
        runtime.quit()
    });
    fs::write(
        root.join("reset-frames.json"),
        serde_json::to_vec(&frames).unwrap(),
    )
    .unwrap();
    result.unwrap();
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
            .unwrap();
    let before = &report["reset_before"];
    let after = &report["reset_after"];
    for key in ["identity", "history", "transcript", "owner", "session"] {
        assert_eq!(before[key], after[key], "{key}");
    }
    assert_eq!(after["queries"], 1);
    assert_eq!(after["retired"], true);
    assert_eq!(report["reset_clear_on_release"], true);
    assert_eq!(report["sdk_retired"], true);
    assert_eq!(report["claude_queries"].as_array().unwrap().len(), 2);
    assert_eq!(report["reset_sdk_closes"], 2);
    assert_eq!(
        report["reset_phases"],
        json!(["review", "review", "apply", "readback"])
    );
    let old = before["ledger"]["selected_segment"].as_str().unwrap();
    let new = after["ledger"]["selected_segment"].as_str().unwrap();
    assert_ne!(old, new);
    assert_eq!(
        before["ledger"]["segments"][old],
        after["ledger"]["segments"][old]
    );
    assert_eq!(after["ledger"]["segments"][new]["entries"], json!([]));
    assert!(
        report["reset_sdk_options"]
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["resume"].is_null())
    );
}
