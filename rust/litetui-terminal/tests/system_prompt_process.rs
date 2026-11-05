#![cfg(windows)]
//! `/system` through the normal Interactive transport and an isolated source process.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
    work_view::Data,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

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
    let args = [
        "--python".to_owned(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
        "--source".into(),
        directory.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        data.to_string_lossy().into_owned(),
        "--backend".into(),
        "custom".into(),
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
fn drain(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    while let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
        runtime.work(intent)?;
        observe(runtime, app, |event| matches!(event, Observation::Work(_)))?;
    }
    Ok(())
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
fn key(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<String> {
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .work
        .key(KeyEvent::new(code, KeyModifiers::NONE));
    drain(runtime, app)?;
    Ok(draw(app))
}
fn route_registered_command(
    runtime: &mut Interactive,
    app: &mut Demo,
    raw: &str,
) -> io::Result<()> {
    app.composer.clear();
    app.insert_text(raw);
    app.submit();
    while let Some(intent) = app.live.as_mut().unwrap().commands.take_intent() {
        runtime.commands(intent)?;
        observe(runtime, app, |event| {
            matches!(event, Observation::Commands(_))
        })?;
    }
    assert_eq!(
        app.composer.text(),
        raw,
        "command routing preserves the composer"
    );
    Ok(())
}

fn screen_has(app: &mut Demo, needles: &[&str]) -> io::Result<String> {
    let screen = draw(app);
    for needle in needles {
        if !screen.contains(needle) {
            return Err(io::Error::other(format!("missing {needle:?} in\n{screen}")));
        }
    }
    Ok(screen)
}

#[test]
#[ignore = "registered /system, real owned store and a controlled provider request; requires LITETUI_AUTHORITY_PYTHON"]
fn system_command_prefills_reviews_applies_reads_back_and_reaches_the_next_provider_request() {
    let (runtime, data) = launch_fixture("system_prompt_runtime.py", true);
    let raw = "/system Prefilled  line 雪  ";
    let edited = "Prefilled  line 雪  \r\n\tCRLF tail\n";
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let seed = json_file(&data.join("system-seed.json"));
        let transcript = PathBuf::from(seed["transcript"].as_str().unwrap());
        let before = fs::read(&transcript)?;

        // Routing reads the prompt and opens the editor; it saves nothing.
        route_registered_command(runtime, &mut app, raw)?;
        drain(runtime, &mut app)?;
        screen_has(
            &mut app,
            &[
                "Edit System prompt",
                "Prefilled from /system",
                "Prefilled  line",
            ],
        )?;
        assert_eq!(fs::read(&transcript)?, before);

        // Review shows before and after plus when it takes effect, and still writes nothing.
        app.live.as_mut().unwrap().work.paste("\r\n\tCRLF tail\n");
        let review = key(runtime, &mut app, KeyCode::F(6))?;
        for needle in [
            "Review System prompt",
            "Takes effect:",
            "next request",
            "CRLF tail",
            "CURRENT SYSTEM ROW",
        ] {
            assert!(review.contains(needle), "{needle}\n{review}");
        }
        assert_eq!(fs::read(&transcript)?, before);

        // Explicit confirmation after the review was painted applies and reads back.
        let saved = key(runtime, &mut app, KeyCode::F(6))?;
        assert!(
            saved.contains("Saved to the transcript and verified"),
            "{saved}"
        );
        let written = fs::read(&transcript)?;
        assert!(written.starts_with(&before));
        let record: Value = serde_json::from_slice(
            written[before.len()..]
                .split(|b| *b == b'\n')
                .find(|line| !line.is_empty())
                .unwrap(),
        )
        .unwrap();
        assert_eq!(record["type"], "edit");
        assert_eq!(record["index"], 0);
        assert_eq!(record["message"]["content"], edited);
        assert_eq!(record["store_injected"], false);

        // Leaving Work keeps the composer draft exactly.
        for _ in 0..6 {
            if !app.has_live_input() {
                break;
            }
            key(runtime, &mut app, KeyCode::Esc)?;
        }
        assert!(!app.has_live_input());
        assert_eq!(app.composer.text(), raw);

        // The next explicit prompt carries the new system text.
        app.composer.clear();
        app.insert_text("Say hi");
        app.submit();
        let prompt = app
            .live
            .as_mut()
            .unwrap()
            .take_submission()
            .expect("explicit turn");
        runtime.submit(prompt)?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        Ok(())
    });
    result.unwrap_or_else(|e| panic!("System prompt scenario failed in {}: {e}", data.display()));
    let old = json_file(&data.join("system-seed.json"))["system"]
        .as_str()
        .unwrap()
        .to_owned();
    let report = json_file(&data.join("runtime-controls-report.json"));
    let requests = report["requests"].as_array().unwrap();
    assert_eq!(requests.len(), 1);
    let messages = requests[0]["messages"].as_array().unwrap();
    assert_eq!(messages[0]["role"], "system");
    let system = messages[0]["content"].as_str().unwrap();
    assert!(system.starts_with(edited), "{system}");
    assert!(
        system.contains("## Your store"),
        "the host re-adds its store snapshot after reviewed text"
    );
    assert!(!system.contains(&old));
    assert_eq!(messages.last().unwrap()["content"], "Say hi");
    let commands = report["commands"].as_array().unwrap();
    let system_ops: Vec<_> = commands
        .iter()
        .filter_map(|c| {
            c["type"]
                .as_str()?
                .strip_prefix("gui.context.native_system_")
        })
        .collect();
    assert_eq!(system_ops, ["edit", "review", "apply", "readback"]);
    assert!(!commands.iter().any(|c| matches!(
        c["type"].as_str(),
        Some("gui.system.set" | "gui.commands.execute")
    )));
    assert_eq!(
        report["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
}
