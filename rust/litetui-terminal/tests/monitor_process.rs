#![cfg(windows)]
//! Registered monitor commands through actual Interactive and a contained fake browser helper.
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
    launch_fixture(fixture_file, false)
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
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False)
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
            "custom".into()
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
    if fixture_file == "model_preferences_full_runtime.py" {
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
    let mut terminal = Terminal::new(TestBackend::new(106, 32)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(106)
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
fn paint_all(app: &mut Demo) {
    for _ in 0..100 {
        let before = draw(app);
        app.live
            .as_mut()
            .unwrap()
            .settings
            .key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
        assert!(app.live.as_mut().unwrap().settings.take_intent().is_none());
        let after = draw(app);
        if before == after {
            return;
        }
    }
    panic!("Monitor review exceeded bounded pages");
}
fn starts(data: &Path) -> u64 {
    if data.join("monitor-current.json").exists() {
        json_file(&data.join("monitor-current.json"))["count"]
            .as_u64()
            .unwrap()
    } else {
        0
    }
}
fn confirm(runtime: &mut Interactive, app: &mut Demo, data: &Path) -> io::Result<()> {
    let count = starts(data);
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(
        app.live.as_mut().unwrap().settings.take_intent().is_none(),
        "Hidden snapshot cannot review"
    );
    paint_all(app);
    settings_key(runtime, app, KeyCode::Enter)?;
    // settings_key painted only the first new review page; reset its painted geometry.
    app.live.as_mut().unwrap().settings.hide();
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(
        app.live.as_mut().unwrap().settings.take_intent().is_none(),
        "Hidden review cannot apply"
    );
    let mut repeat = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
    repeat.kind = crossterm::event::KeyEventKind::Repeat;
    app.live.as_mut().unwrap().settings.key(repeat);
    assert!(app.live.as_mut().unwrap().settings.take_intent().is_none());
    paint_all(app);
    assert_eq!(
        starts(data),
        count,
        "No helper before explicit painted confirmation"
    );
    settings_key(runtime, app, KeyCode::Enter)?;
    Ok(())
}
fn completed(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let screen = draw(app);
        if screen.contains("Monitor completed.") {
            return Ok(());
        }
        if screen.contains("Monitor failed.") || screen.contains("Inspection failed") {
            return Err(io::Error::other(screen));
        }
        if Instant::now() > deadline {
            return Err(io::Error::other(format!(
                "Monitor completion timed out: {screen}"
            )));
        }
        std::thread::sleep(Duration::from_millis(20));
        settings_key(runtime, app, KeyCode::Char('r'))?;
    }
}
fn close(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    settings_key(runtime, app, KeyCode::Esc)?;
    settings_key(runtime, app, KeyCode::Esc)?;
    Ok(())
}
#[test]
#[ignore = "actual registered source and contained fake browser child; requires LITETUI_AUTHORITY_PYTHON"]
fn monitor_commands_review_sweep_cancel_close_and_quit_preserve_draft() {
    let (runtime, data) = launch("monitor_runtime.py");
    let result=runtime.with_cleanup(|runtime| {
        let mut app=Demo::live("custom".into(),"switch-old".into());
        observe(runtime,&mut app,|e|matches!(e,Observation::Ready))?;
        raw(runtime,&mut app,"/monitor targets")?;
        assert_eq!(starts(&data),0);assert!(!data.parent().unwrap().join("workspace/monitor-targets.json").exists());
        let screen=draw(&mut app);assert!(screen.contains("Monitor targets"),"{screen}");
        close(runtime,&mut app)?;
        for command in ["/monitor init","/monitor demo","/monitor"] {
            if command=="/monitor" {
                fs::write(data.parent().unwrap().join("workspace/monitor-targets.json"),serde_json::to_vec(&json!({"targets":[{"name":"private","url":"https://user:password@example.test/page?secret=value","screenshot":true}]}))?)?;
            }
            raw(runtime,&mut app,command)?;
            let caret=(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned());
            confirm(runtime,&mut app,&data)?;completed(runtime,&mut app)?;
            assert_eq!(app.composer.text(),command);
            assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
            close(runtime,&mut app)?;
        }
        assert_eq!(fs::read(data.parent().unwrap().join("workspace/monitor-shots/private.png"))?,b"controlled screenshot bytes");
        let log=fs::read_to_string(data.parent().unwrap().join("workspace/monitor-log.jsonl"))?;
        assert!(!log.contains("password")&&!log.contains("secret=value"));
        fs::write(data.join("monitor-wait"),b"wait")?;
        for quit in [false,true] {
            raw(runtime,&mut app,"/monitor")?;confirm(runtime,&mut app,&data)?;
            let folder=PathBuf::from(json_file(&data.join("monitor-current.json"))["directory"].as_str().unwrap());
            let deadline=Instant::now()+Duration::from_secs(15);
            while !folder.join("child.pid").exists() {assert!(Instant::now()<deadline);std::thread::sleep(Duration::from_millis(10));}
            if quit {runtime.quit()?;} else {close(runtime,&mut app)?;assert!(!folder.exists());}
        }
        Ok(())
    });
    assert!(result.is_ok(), "{result:?}: {}", data.display());
    let after = json_file(&data.join("monitor-after.json"));
    assert_eq!(after["released"], true);
    assert_eq!(after["remaining"], json!([]));
    assert_eq!(after["attempts"].as_array().unwrap().len(), 5);
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"], json!([]));
    assert_eq!(report["denied"], json!([]));
    assert_eq!(report["shutdown"]["ok"], true);
    eprintln!("Controlled monitor process evidence: {}", data.display());
}
