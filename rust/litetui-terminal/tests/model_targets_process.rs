#![cfg(windows)]
//! Named lifecycle operations through the actual registered source and controlled SDK.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
    work_view::Data,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

fn launch(fixture_file: &str) -> (Interactive, PathBuf) {
    launch_fixture(fixture_file, true)
}
fn launch_fixture(fixture_file: &str, command_controls: bool) -> (Interactive, PathBuf) {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let ordinal = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source = std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate_root.join("../.."))
        .canonicalize()
        .unwrap();
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let directory = crate_root
        .join("target/context-process-tests")
        .join(format!("{}-{epoch}-{ordinal}", std::process::id()));
    let wrapper = directory.join("source/src/litetui");
    let data = directory.join("data");
    fs::create_dir_all(&wrapper).unwrap();
    let literal = |path: &Path| serde_json::to_string(&path.to_string_lossy()).unwrap();
    fs::write(
        wrapper.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            literal(&source.join("src/litetui"))
        ),
    )
    .unwrap();
    let fixture = crate_root.join("tests/fixtures");
    let script = format!(
        r#"import json,os,runpy
from pathlib import Path
os.environ.update(RUST_RUNTIME_CONTROLS_SOURCE={}, RUST_CONTEXT_COMMON_FIXTURE={},
    RUST_RUNTIME_CONTROLS_CASE='context-read-only', PYTHONDONTWRITEBYTECODE='1',
    RUST_COMMAND_CONTROLS_BASE={})
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False,thinking_level="medium",lmstudio_graded_thinking_models=["switch-old"],model_infer_overrides={{"switch-new":{{"reasoning_effort":"off"}}}})
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,
        literal(&source),
        literal(&fixture.join("runtime_controls_runtime.py")),
        literal(&fixture.join(fixture_file)),
        literal(&fixture.join(if command_controls {
            "command_controls_runtime.py"
        } else {
            fixture_file
        }))
    );
    fs::write(wrapper.join("cli.py"), script).unwrap();
    fs::create_dir_all(data.parent().unwrap().join("workspace")).unwrap();
    let args = [
        "--workspace".into(),
        data.parent()
            .unwrap()
            .join("workspace")
            .to_string_lossy()
            .into_owned(),
        "--python".to_owned(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
        "--source".into(),
        directory.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        data.to_string_lossy().into_owned(),
        "--backend".into(),
        if fixture_file == "model_preferences_full_runtime.py" {
            "llamacpp".into()
        } else {
            "lmstudio".into()
        },
        "--model".into(),
        "switch-old".into(),
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
        "--server-mode".into(),
        "connect".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
    ];
    let mut args = args.to_vec();
    if fixture_file == "model_targets_runtime.py" {
        let index = args.iter().position(|s| s == "--thinking-level").unwrap();
        args.drain(index..index + 2);
    }
    (Interactive::start(Options::parse(&args).unwrap()), data)
}

fn json_file(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}
fn observe(
    runtime: &mut Interactive,
    app: &mut Demo,
    until: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(40);
    loop {
        if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(100)) {
            let rejected = match &event {
                Observation::Work(update) => match &update.data {
                    Data::Rejected(message) => Some(message.clone()),
                    _ => None,
                },
                _ => None,
            };
            let done = until(&event);
            app.observe_live(event)?;
            if let Some(reason) = rejected {
                return Err(io::Error::other(reason));
            }
            if done {
                return Ok(());
            }
        } else if let Some(exit) = runtime.poll_done() {
            return Err(exit
                .err()
                .unwrap_or_else(|| io::Error::other("Unexpected Context runtime exit")));
        } else if Instant::now() > deadline {
            return Err(io::Error::other("Context observation timed out"));
        }
    }
}
fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(130, 45)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(130)
        .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn settings_drain(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    while let Some(intent) = app.live.as_mut().unwrap().settings.take_intent() {
        runtime.settings(intent)?;
        observe(runtime, app, |event| {
            matches!(event, Observation::Settings(_))
        })?;
    }
    Ok(())
}

fn settings_key(runtime: &mut Interactive, app: &mut Demo, key: KeyCode) -> io::Result<String> {
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(key, KeyModifiers::NONE));
    settings_drain(runtime, app)?;
    Ok(draw(app))
}

fn commands(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    while let Some(intent) = app.live.as_mut().unwrap().commands.take_intent() {
        runtime.commands(intent)?;
        observe(runtime, app, |e| matches!(e, Observation::Commands(_)))?;
    }
    Ok(())
}
fn raw(runtime: &mut Interactive, app: &mut Demo, command: &str) -> io::Result<()> {
    app.composer.clear();
    app.insert_text(command);
    app.composer.move_cursor(Motion::WordLeft, true);
    app.submit();
    commands(runtime, app)?;
    settings_drain(runtime, app)
}

fn close(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    settings_key(runtime, app, KeyCode::Esc)?;
    settings_key(runtime, app, KeyCode::Esc)?;
    Ok(())
}
fn effects(data: &Path) -> Value {
    json_file(&data.join("model-target-effects.json"))
}

#[test]
#[ignore = "actual registered source and private fake SDK; requires LITETUI_AUTHORITY_PYTHON"]
fn named_load_unload_context_and_owned_recapture_preserve_draft() {
    let (runtime, data) = launch("model_targets_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("lmstudio".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        raw(runtime, &mut app, "/load switch-new")?;
        let before = (
            app.composer.text().to_owned(),
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        assert_eq!(effects(&data), json!([]));
        // Routing produces a review, but cannot apply it before a fresh paint.
        app.live
            .as_mut()
            .unwrap()
            .settings
            .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(app.live.as_mut().unwrap().settings.take_intent().is_none());
        let screen = draw(&mut app);
        assert!(screen.contains("Review load"), "{screen}");
        settings_key(runtime, &mut app, KeyCode::Esc)?;
        assert_eq!(effects(&data), json!([]));
        // Explicit one-use value; editing and inspection still have no effect.
        settings_key(runtime, &mut app, KeyCode::Char('c'))?;
        for _ in 0..10 {
            settings_key(runtime, &mut app, KeyCode::Backspace)?;
        }
        for c in "2048".chars() {
            settings_key(runtime, &mut app, KeyCode::Char(c))?;
        }
        settings_key(runtime, &mut app, KeyCode::Enter)?;
        settings_key(runtime, &mut app, KeyCode::Char('l'))?;
        assert_eq!(effects(&data), json!([]));
        let settled = settings_key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            settled.contains("Operation and connection verified"),
            "{settled}"
        );
        assert!(settled.contains("Selected model: switch-new"), "{settled}");
        assert_eq!(
            before,
            (
                app.composer.text().to_owned(),
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            )
        );
        assert_eq!(
            effects(&data),
            json!([["load","switch-new",{"contextLength":2048}]])
        );
        close(runtime, &mut app)?;
        app.live
            .as_mut()
            .unwrap()
            .submit("Controlled request after loading model B")?;
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        raw(runtime, &mut app, "/unload switch-old")?;
        assert_eq!(effects(&data).as_array().unwrap().len(), 1);
        let settled = settings_key(runtime, &mut app, KeyCode::Enter)?;
        assert!(settled.contains("Selected model: switch-new"), "{settled}");
        assert_eq!(app.composer.text(), "/unload switch-old");
        close(runtime, &mut app)?;
        // A second named load uses explicit server default, then recaptures again.
        raw(runtime, &mut app, "/load switch-old")?;
        settings_key(runtime, &mut app, KeyCode::Esc)?;
        settings_key(runtime, &mut app, KeyCode::Char('d'))?;
        settings_key(runtime, &mut app, KeyCode::Char('l'))?;
        settings_key(runtime, &mut app, KeyCode::Enter)?;
        close(runtime, &mut app)?;
        app.composer.clear();
        app.insert_text("Controlled request after target Work recapture");
        app.submit();
        let text = app
            .live
            .as_mut()
            .unwrap()
            .take_submission()
            .expect("prompt submission");
        runtime.submit(text)?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        Ok(())
    });
    assert!(result.is_ok(), "{result:?}: {}", data.display());
    assert_eq!(
        effects(&data),
        json!([["load","switch-new",{"contextLength":2048}],["unload","switch-old"],["load","switch-old",null]])
    );
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["denied"], json!([]));
    assert_eq!(report["shutdown"]["ok"], true);
    assert_eq!(report["requests"].as_array().unwrap().len(), 2);
    assert_eq!(report["requests"][0]["model"], "switch-new");
    assert_eq!(
        report["requests"][0]["extra_body"]["reasoning_effort"],
        "none"
    );
    assert_eq!(report["requests"][1]["model"], "switch-old");
    assert_eq!(
        report["requests"][1]["extra_body"]["reasoning_effort"],
        "medium"
    );
    assert_eq!(report["before"]["authority"]["thinking_level"], "medium");
    assert_eq!(report["after"]["authority"]["thinking_level"], "medium");
    assert_eq!(
        report["initial_documents"]["global"],
        report["final_documents"]["global"]
    );
    let capture_count = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["type"] == "gui.work.native_capture")
        .count();
    assert_eq!(capture_count, 3);
    eprintln!(
        "Controlled model-target process evidence: {}",
        data.display()
    );
}
