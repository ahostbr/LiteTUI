#![cfg(windows)]
//! Rust supervisor -> real owned source settings API; external inference is controlled.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    demo::Demo,
    live::{Interactive, Observation, Options},
    settings_editor::{self as edit},
    settings_view, ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
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
fn launch(case: &str, tools: bool) -> (Interactive, PathBuf) {
    let actual = source();
    let fixture =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures/settings_edit_runtime.py");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/settings-edit-process-tests")
        .join(format!(
            "{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    fs::create_dir_all(base.join("workspace")).unwrap();
    let quote = |s: &str| serde_json::to_string(s).unwrap();
    fs::write(
        package.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            quote(&actual.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    fs::write(package.join("cli.py"),format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_SETTINGS_EDIT_SOURCE'] = {}
os.environ['RUST_SETTINGS_EDIT_CASE'] = {}
os.environ['RUST_SETTINGS_EDIT_DRIVER'] = 'rpc'
os.environ['RUST_SETTINGS_EDIT_FIELD'] = {}
os.environ['RUST_SETTINGS_EDIT_VALUE'] = '257'
os.environ.pop('LITETUI_MAX_TOKENS', None)
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=8192, max_tokens_chat=101, max_tokens_tools=202)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(case),quote(if tools{"max_tokens_tools"}else{"max_tokens_chat"}),quote(&fixture.to_string_lossy()))).unwrap();
    let root = base.join("data");
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
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
        "--timeout".into(),
        "30".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    if tools {
        args.extend([
            "--tools".into(),
            "on".into(),
            "--workspace".into(),
            base.join("workspace").to_string_lossy().into_owned(),
        ]);
    }
    if case == "cli-override" {
        args.extend(["--max-tokens".into(), "307".into()]);
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let until = Instant::now() + Duration::from_secs(35);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(100)) {
            Ok(e) => return Ok(e),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected clean exit")));
                }
                if Instant::now() > until {
                    return Err(io::Error::other("Settings observation timeout"));
                }
            }
        }
    }
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
        if done {
            return Ok(());
        }
    }
}
fn draw(app: &mut Demo) {
    let mut t = Terminal::new(TestBackend::new(100, 32)).unwrap();
    t.draw(|f| ui::draw(f, app)).unwrap();
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn send(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.settings(
        app.live
            .as_mut()
            .unwrap()
            .settings
            .take_intent()
            .expect("explicit UI intent"),
    )
}
fn wait_edit(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Settings(
                settings_view::Update::Editor(_) | settings_view::Update::Edited { .. }
            )
        )
    })
}
fn open(runtime: &mut Interactive, app: &mut Demo, field: &str) -> io::Result<()> {
    app.open_settings();
    send(runtime, app)?;
    wait(runtime, app, |e| matches!(e, Observation::Settings(_)))?;
    draw(app);
    let view = &app.live.as_ref().unwrap().settings;
    let index = view
        .snapshot
        .as_ref()
        .expect("source inspector")
        .rows
        .iter()
        .position(|r| r.name == field)
        .unwrap();
    key(app, KeyCode::Home);
    for _ in 0..index {
        key(app, KeyCode::Down);
    }
    key(app, KeyCode::Char('e'));
    send(runtime, app)?;
    wait_edit(runtime, app)
}
fn validate(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    wait_edit(runtime, app)
}
fn save(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().settings.take_intent().is_none(),
        "must draw review before save"
    );
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    wait_edit(runtime, app)
}
fn commands(root: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(root.join("settings-edit-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
fn scenario(case: &str, tools: bool) {
    let (runtime, root) = launch(case, tools);
    let field = if tools {
        "max_tokens_tools"
    } else {
        "max_tokens_chat"
    };
    let fatal = [
        "runtime-raised",
        "write-then-error",
        "value-drift",
        "policy-drift",
    ]
    .contains(&case);
    let mut got_saved = false;
    let mut refused = false;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("PRESERVE MAIN COMPOSER");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let rows = app.live.as_ref().unwrap().rows.len();
        let session = app
            .live
            .as_ref()
            .unwrap()
            .runtime_state
            .startup()
            .unwrap()
            .session
            .clone();
        open(runtime, &mut app, field)?;
        draw(&mut app);
        if case == "cli-override" {
            let view = &mut app.live.as_mut().unwrap().settings.editor;
            assert!(
                !view
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .budget(field)
                    .unwrap()
                    .editable
            );
            view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            assert!(view.take_intent().is_none());
        } else {
            key(&mut app, KeyCode::Home);
            for _ in 0..3 {
                key(&mut app, KeyCode::Delete);
            }
            app.live.as_mut().unwrap().settings.editor.paste("257");
            validate(runtime, &mut app)?;
            save(runtime, &mut app)?;
            let mut rejected = app
                .live
                .as_ref()
                .unwrap()
                .settings
                .editor
                .snapshot
                .is_none();
            if rejected {
                refused = true;
                assert_eq!(app.live.as_ref().unwrap().settings.editor.draft(), "257");
                assert_eq!(
                    commands(&root)
                        .iter()
                        .filter(|c| c["type"] == edit::SAVE)
                        .count(),
                    1,
                    "no automatic retry"
                );
            }
            if case == "one-shot-stale" {
                assert!(rejected);
                draw(&mut app);
                key(&mut app, KeyCode::Char('r'));
                send(runtime, &mut app)?;
                wait_edit(runtime, &mut app)?;
                assert_eq!(app.live.as_ref().unwrap().settings.editor.draft(), "257");
                validate(runtime, &mut app)?;
                save(runtime, &mut app)?;
                rejected = false;
            }
            if !rejected {
                got_saved = true;
                let b = app
                    .live
                    .as_ref()
                    .unwrap()
                    .settings
                    .editor
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .budget(field)
                    .unwrap();
                assert_eq!(b.saved, 257);
                assert_eq!(
                    b.effective,
                    if case == "runtime-failed" { 101 } else { 257 }
                );
            }
        }
        assert!(!fatal, "Fatal readback must not publish success");
        assert_eq!(app.composer.text(), "PRESERVE MAIN COMPOSER");
        assert_eq!(app.live.as_ref().unwrap().rows.len(), rows);
        assert_eq!(
            app.live
                .as_ref()
                .unwrap()
                .runtime_state
                .startup()
                .unwrap()
                .session,
            session
        );
        key(&mut app, KeyCode::Esc);
        draw(&mut app);
        if got_saved {
            let row = app
                .live
                .as_ref()
                .unwrap()
                .settings
                .snapshot
                .as_ref()
                .unwrap()
                .rows
                .iter()
                .find(|r| r.name == field)
                .unwrap();
            assert_eq!(
                row.effective,
                if case == "runtime-failed" {
                    "101"
                } else {
                    "257"
                }
            );
            assert_eq!(
                row.requested, row.effective,
                "Inspector reflects verified live state; editor shows durable separately"
            );
        }
        app.live.as_mut().unwrap().settings.close();
        app.live
            .as_mut()
            .unwrap()
            .submit("Explicit request after settings editor")?;
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        assert_eq!(app.composer.text(), "PRESERVE MAIN COMPOSER");
        Ok(())
    });
    fs::write(
        root.parent().unwrap().join("native-result.txt"),
        format!("{result:?}"),
    )
    .unwrap();
    assert_eq!(
        result.is_err(),
        fatal,
        "{case}: {result:?} at {}",
        root.display()
    );
    if fatal {
        assert!(!got_saved);
    }
    if case == "one-shot-stale" || case == "persistence-refused" {
        assert!(refused);
    }
    let sent = commands(&root);
    assert_eq!(
        sent.iter().filter(|c| c["type"] == edit::SAVE).count(),
        if case == "cli-override" {
            0
        } else if case == "one-shot-stale" {
            2
        } else {
            1
        }
    );
    assert_eq!(
        sent.iter().filter(|c| c["type"] == "prompt").count(),
        usize::from(!fatal)
    );
    assert!(
        sent.iter()
            .filter(|c| c["type"] == "gui.state")
            .all(|c| c["include_conversations"] == false)
    );
    let report: Value = serde_json::from_str(
        &fs::read_to_string(root.join("settings-edit-fresh-report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        PathBuf::from(report["source_app"].as_str().unwrap())
            .canonicalize()
            .unwrap(),
        source().join("src/litetui/app.py").canonicalize().unwrap(),
        "The output-budget runtime must import the selected packaged/worktree source"
    );
    assert_eq!(report["denied"], serde_json::json!([]));
    let requests = report["requests"].as_array().unwrap();
    assert_eq!(requests.len(), usize::from(!fatal));
    if !fatal {
        assert_eq!(
            requests[0]["max_tokens"],
            if case == "cli-override" {
                307
            } else if ["runtime-failed", "persistence-refused"].contains(&case) {
                101
            } else {
                257
            }
        );
        assert!(
            requests[0]["messages"]
                .as_array()
                .unwrap()
                .iter()
                .any(|m| m["content"] == "Preserved settings history")
        );
    }
}
#[test]
#[ignore = "controlled real owned CLI; dependency Python required"]
fn owned_output_budgets_reach_next_request_and_keep_draft() {
    scenario("success", false);
    scenario("success", true);
}
#[test]
#[ignore = "controlled real owned CLI; dependency Python required"]
fn stale_confirmation_requires_explicit_refresh_and_revalidation() {
    scenario("one-shot-stale", false);
}
#[test]
#[ignore = "controlled real owned CLI; dependency Python required"]
fn launch_owned_and_refused_settings_preserve_connection() {
    scenario("cli-override", false);
    scenario("persistence-refused", false);
}
#[test]
#[ignore = "controlled real owned CLI; dependency Python required"]
fn durable_save_with_known_runtime_failure_remains_honest() {
    scenario("runtime-failed", false);
}
#[test]
#[ignore = "controlled real owned CLI; dependency Python required"]
fn uncertain_save_or_contradictory_readback_closes() {
    for case in [
        "runtime-raised",
        "write-then-error",
        "value-drift",
        "policy-drift",
    ] {
        scenario(case, false);
    }
}
