#![cfg(windows)]
//! Owned Rust supervisor -> actual theme service. External inference is controlled.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    demo::Demo,
    live::{Interactive, Observation, Options},
    theme_controls::{self as themes},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
fn launch(case: &str, resume: Option<(&Path, &str)>) -> (Interactive, PathBuf) {
    let source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/theme-process-tests")
        .join(format!(
            "{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    let quote = |s: &str| serde_json::to_string(s).unwrap();
    fs::write(
        package.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            quote(&source.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    fs::write(package.join("cli.py"),format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_THEME_SOURCE'] = {}
os.environ['RUST_THEME_CASE'] = {}
os.environ['RUST_THEME_DRIVER'] = 'rpc'
os.environ['RUST_THEME_PHASE'] = {}
if os.environ['RUST_THEME_PHASE'] == 'fresh':
    p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
    s = json.loads(p.read_text(encoding='utf-8'))
    s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=8192)
    p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&source.to_string_lossy()),quote(case),quote(if resume.is_some(){"resumed"}else{"fresh"}),quote(&source.join("rust/litetui-terminal/tests/fixtures/theme_runtime.py").to_string_lossy()))).unwrap();
    let root = resume
        .map(|(p, _)| p.to_owned())
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
        "--timeout".into(),
        "30".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    if let Some((_, session)) = resume {
        args.extend(["--resume".into(), session.into()]);
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
                    return Err(io::Error::other("Theme observation timeout"));
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
        let e = next(runtime)?;
        if let Observation::Theme(themes::Update::Rejected(ref reason)) = e {
            eprintln!("THEME REFUSAL: {reason}");
        }
        let done = predicate(&e);
        app.observe_live(e)?;
        if done {
            return Ok(());
        }
    }
}
fn draw(app: &mut Demo) -> String {
    let mut t = Terminal::new(TestBackend::new(100, 32)).unwrap();
    t.draw(|f| ui::draw(f, app)).unwrap();
    t.backend()
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
        .themes
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn send(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.theme(
        app.live
            .as_mut()
            .unwrap()
            .themes
            .take_intent()
            .expect("explicit theme intent"),
    )
}
fn wait_theme(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| matches!(e, Observation::Theme(_)))
}
fn validate(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    wait_theme(runtime, app)
}
fn save(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().themes.take_intent().is_none(),
        "review must first be visible"
    );
    for _ in 0..200 {
        if draw(app).contains("Enter: save once") {
            key(app, KeyCode::Enter);
            send(runtime, app)?;
            return wait_theme(runtime, app);
        }
        key(app, KeyCode::PageDown);
    }
    Err(io::Error::other("Theme review pagination failed"))
}
fn replace_field(app: &mut Demo, text: &str) {
    draw(app);
    key(app, KeyCode::Home);
    for _ in 0..256 {
        key(app, KeyCode::Delete);
    }
    app.live.as_mut().unwrap().themes.paste(text);
}
fn custom(app: &mut Demo, overwrite: bool) {
    draw(app);
    key(
        app,
        if overwrite {
            KeyCode::F(3)
        } else {
            KeyCode::F(2)
        },
    );
    replace_field(app, "  Theme 雪  ");
    key(app, KeyCode::Tab);
    replace_field(app, "#123456");
    // Reach tool-text and preserve invalid optional input for source fallback.
    for _ in 0..12 {
        key(app, KeyCode::Tab);
    }
    replace_field(app, "invalid optional");
    key(app, KeyCode::Tab);
    replace_field(app, "");
    key(app, KeyCode::Tab);
    replace_field(app, "");
}
fn select(app: &mut Demo, name: &str) {
    let index = app
        .live
        .as_ref()
        .unwrap()
        .themes
        .snapshot
        .as_ref()
        .unwrap()
        .rows
        .iter()
        .position(|r| r.name == name)
        .expect("source theme row");
    draw(app);
    key(app, KeyCode::Home);
    for _ in 0..index {
        key(app, KeyCode::Down);
    }
}
fn report(root: &Path, phase: &str) -> Value {
    serde_json::from_str(
        &fs::read_to_string(root.join(format!("theme-{phase}-report.json"))).unwrap(),
    )
    .unwrap()
}
fn scenario(case: &str) -> (PathBuf, String) {
    let (runtime, root) = launch(case, None);
    let fatal = [
        "write-corrupt",
        "generic-save-error",
        "partial-palette-drift",
        "palette-drift",
        "definition-drift",
        "policy-drift",
    ]
    .contains(&case);
    let startup =
        case.starts_with("startup-") || case.starts_with("shadow-") || case == "empty-selection";
    let unavailable = ["unsupported-catalog", "oversized-catalog"].contains(&case);
    let cancellation = case == "cancel-validate";
    let mut session = String::new();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("PRESERVE THE MAIN DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        session = app
            .live
            .as_ref()
            .unwrap()
            .runtime_state
            .startup()
            .unwrap()
            .session
            .clone();
        let before_rows = app.live.as_ref().unwrap().rows.len();
        if unavailable {
            assert!(app.live.as_ref().unwrap().effective_theme.is_none());
            assert!(draw(&mut app).contains("local fallback"));
        } else {
            let s = app
                .live
                .as_ref()
                .unwrap()
                .themes
                .snapshot
                .as_ref()
                .expect("startup source theme");
            if ["active-alpha", "repair-alpha"].contains(&case) {
                assert!(s.effective().is_none());
                assert!(app.live.as_ref().unwrap().effective_theme.is_none());
            } else {
                assert_eq!(app.palette().name, s.selection.effective);
            }
            if case == "startup-custom" {
                assert_eq!(s.selection.effective, "Theme 雪");
            }
            if let Some(target) = case.strip_prefix("shadow-") {
                assert_eq!(s.selection.effective, target);
            }
        }
        if !startup && !unavailable {
            app.open_themes();
            send(runtime, &mut app)?;
            wait_theme(runtime, &mut app)?;
            if [
                "custom",
                "overwrite",
                "register-failed",
                "refresh-failed",
                "write-after",
                "repair-alpha",
            ]
            .contains(&case)
            {
                custom(&mut app, case == "overwrite" || case == "refresh-failed");
                if case == "repair-alpha" {
                    for _ in 0..15 {
                        key(&mut app, KeyCode::Up);
                    }
                    replace_field(&mut app, "unsupported-color");
                }
            } else {
                select(
                    &mut app,
                    if case == "unselectable-name" {
                        "comma,name"
                    } else if case == "select-current" {
                        "textual-dark"
                    } else {
                        "matrix"
                    },
                );
            }
            if case == "unselectable-name" {
                draw(&mut app);
                key(&mut app, KeyCode::Enter);
                assert!(app.live.as_mut().unwrap().themes.take_intent().is_none());
            } else if cancellation {
                draw(&mut app);
                key(&mut app, KeyCode::Enter);
                send(runtime, &mut app)?;
                key(&mut app, KeyCode::Esc);
                wait_theme(runtime, &mut app)?;
                assert!(!app.live.as_ref().unwrap().themes.owns_input());
            } else {
                validate(runtime, &mut app)?;
                if app.live.as_ref().unwrap().themes.review.is_some() {
                    save(runtime, &mut app)?;
                }
                let rejected = app.live.as_ref().unwrap().themes.snapshot.is_none();
                if rejected {
                    assert!(
                        ["stale", "one-shot-stale", "registry-drift", "write-before"]
                            .contains(&case),
                        "{case}: {}",
                        app.live.as_ref().unwrap().themes.notice
                    );
                    if case == "one-shot-stale" {
                        draw(&mut app);
                        key(&mut app, KeyCode::F(5));
                        send(runtime, &mut app)?;
                        wait_theme(runtime, &mut app)?;
                        select(&mut app, "matrix");
                        validate(runtime, &mut app)?;
                        save(runtime, &mut app)?;
                    }
                }
            }
        }
        assert!(!fatal, "contradictory readback must never publish success");
        assert_eq!(app.composer.text(), "PRESERVE THE MAIN DRAFT");
        assert_eq!(app.live.as_ref().unwrap().rows.len(), before_rows);
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
        key(&mut app, KeyCode::Esc);
        app.live.as_mut().unwrap().themes.close();
        app.live
            .as_mut()
            .unwrap()
            .submit("Explicit prompt after theme work")?;
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        runtime.quit()?;
        loop {
            if let Some(done) = runtime.poll_done() {
                return done;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    });
    if fatal {
        assert!(result.is_err(), "{case}");
    } else {
        assert!(result.is_ok(), "{case}: {result:?}");
    }
    let r = report(&root, "fresh");
    assert_eq!(
        r["requests"].as_array().unwrap().len(),
        usize::from(!fatal),
        "{case}"
    );
    let commands = r["commands"].as_array().unwrap();
    assert!(
        commands
            .iter()
            .filter(|c| c["type"] == "gui.state")
            .all(|c| c["include_conversations"] == false)
    );
    if startup || unavailable || cancellation || case == "unselectable-name" {
        assert!(
            !commands.iter().any(|c| c["type"] == themes::SAVE),
            "{case}"
        );
    }
    (root, session)
}
#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn source_palette_adopted_at_startup_including_custom_shadows() {
    for case in [
        "startup-custom",
        "shadow-matrix",
        "shadow-textual-dark",
        "shadow-textual-light",
        "startup-unknown",
        "startup-invalid",
        "startup-case",
        "startup-light",
        "empty-selection",
    ] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn explicit_select_create_and_same_name_edit_keep_main_draft() {
    for case in [
        "select",
        "select-current",
        "custom",
        "overwrite",
        "active-alpha",
        "repair-alpha",
    ] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn refused_stale_cancelled_and_unselectable_themes_never_save_automatically() {
    for case in [
        "stale",
        "one-shot-stale",
        "registry-drift",
        "cancel-validate",
        "unselectable-name",
        "write-before",
    ] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn known_theme_persistence_or_application_partial_keeps_chat_usable() {
    for case in [
        "write-after",
        "register-failed",
        "select-failed",
        "refresh-failed",
    ] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn contradictory_theme_readback_fails_closed_and_unavailable_catalog_is_visible() {
    for case in [
        "write-corrupt",
        "generic-save-error",
        "partial-palette-drift",
        "palette-drift",
        "definition-drift",
        "policy-drift",
        "unsupported-catalog",
        "oversized-catalog",
    ] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn selected_theme_is_read_back_on_second_owned_lifetime() {
    let (root, session) = scenario("select");
    let (runtime, _) = launch("select", Some((&root, &session)));
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        assert_eq!(app.palette().name, "matrix");
        assert_eq!(
            app.live
                .as_ref()
                .unwrap()
                .themes
                .snapshot
                .as_ref()
                .unwrap()
                .selection
                .saved,
            "matrix"
        );
        runtime.quit()?;
        loop {
            if let Some(done) = runtime.poll_done() {
                return done;
            }
            std::thread::sleep(Duration::from_millis(10));
        }
    });
    assert!(result.is_ok(), "{result:?}");
    assert!(
        report(&root, "resumed")["requests"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
#[ignore = "controlled actual owned Python source; dependency runtime required"]
fn source_palette_route_and_session_prefill_open_only_guarded_views() {
    use litetui_terminal::{
        command_palette as commands, conversation_actions as actions, conversation_browser,
    };
    let (runtime, root) = launch("control-routes", None);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("PRESERVE ROUTE DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.open_commands();
        runtime.commands(app.live.as_mut().unwrap().commands.take_intent().unwrap())?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Commands(_)))?;
        let c = app
            .live
            .as_ref()
            .unwrap()
            .commands
            .catalog
            .as_ref()
            .unwrap();
        let row = c
            .entries
            .iter()
            .find(|r| r.action == Some(commands::Action::Themes))
            .expect("real Theme palette contribution")
            .id
            .clone();
        runtime.commands(commands::Intent::Resolve {
            snapshot: c.snapshot.clone(),
            row,
        })?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Commands(_)))?;
        assert!(app.has_live_themes());
        send(runtime, &mut app)?;
        wait_theme(runtime, &mut app)?;
        app.live.as_mut().unwrap().themes.close();
        for (raw, action, title) in [
            (
                "/rename Exact 雪 title  ",
                actions::Action::Rename,
                "Exact 雪 title  ",
            ),
            ("/new", actions::Action::New, ""),
        ] {
            app.live.as_mut().unwrap().commands.raw(raw);
            runtime.commands(app.live.as_mut().unwrap().commands.take_intent().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Commands(_)))?;
            assert!(app.has_live_browser());
            if action == actions::Action::Rename {
                assert_eq!(app.live.as_ref().unwrap().browser.actions.draft(), title);
            }
            let intent = app
                .live
                .as_mut()
                .unwrap()
                .browser
                .take_intent()
                .expect("source read only");
            assert_eq!(
                intent,
                conversation_browser::Intent::Action(actions::Intent::Read { action })
            );
            runtime.browser(intent)?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Browser(_)))?;
            if action == actions::Action::Rename {
                assert_eq!(
                    app.live.as_ref().unwrap().browser.actions.draft(),
                    title,
                    "delayed snapshot must not replace prefilled title"
                );
            }
            app.live.as_mut().unwrap().browser.close();
        }
        app.live.as_mut().unwrap().commands.raw("/new");
        runtime.commands(app.live.as_mut().unwrap().commands.take_intent().unwrap())?;
        app.live.as_mut().unwrap().commands.close();
        wait(runtime, &mut app, |e| matches!(e, Observation::Commands(_)))?;
        assert!(!app.has_live_browser());
        assert!(!app.has_live_themes());
        assert_eq!(app.composer.text(), "PRESERVE ROUTE DRAFT");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        Ok(())
    });
    assert!(result.is_ok(), "{result:?}");
    let r = report(&root, "fresh");
    assert!(r["requests"].as_array().unwrap().is_empty());
    assert!(
        !r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["type"] == themes::SAVE || c["type"] == actions::APPLY)
    );
}
