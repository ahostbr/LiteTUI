#![cfg(windows)]
//! F10 agent preferences through the normal Interactive transport and an isolated source process.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
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
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let directory = crate_root
        .join("target/agent-preferences-process-tests")
        .join(format!("{}-{epoch}", std::process::id()));
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
for name in ('LM_TOOL_ITERS', 'LITETUI_SEAT_NAME'):
    os.environ.pop(name, None)
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,
        literal(&source),
        literal(&fixture.join("runtime_controls_runtime.py")),
        literal(&fixture.join("agent_preferences_runtime.py")),
        literal(&fixture.join("agent_preferences_runtime.py")),
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
fn conversation_settings(data: &Path) -> PathBuf {
    fn walk(dir: &Path, depth: usize, found: &mut Vec<PathBuf>) {
        if depth == 0 {
            return;
        }
        for entry in fs::read_dir(dir).into_iter().flatten().flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, depth - 1, found);
            } else if path.file_name().is_some_and(|n| n == "settings.json")
                && path
                    .parent()
                    .and_then(Path::parent)
                    .is_some_and(|p| p.file_name().is_some_and(|n| n == "conversations"))
            {
                found.push(path);
            }
        }
    }
    let mut found = Vec::new();
    walk(data, 8, &mut found);
    assert_eq!(
        found.len(),
        1,
        "one conversation settings document: {found:?}"
    );
    found.remove(0)
}
fn observe(
    runtime: &mut Interactive,
    app: &mut Demo,
    until: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(40);
    loop {
        if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(100)) {
            let done = until(&event);
            app.observe_live(event)?;
            if done {
                return Ok(());
            }
        } else if let Some(exit) = runtime.poll_done() {
            return Err(exit
                .err()
                .unwrap_or_else(|| io::Error::other("Unexpected agent preference runtime exit")));
        } else if Instant::now() > deadline {
            return Err(io::Error::other("Agent preference observation timed out"));
        }
    }
}
fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(110, 36)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(110)
        .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn drain(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    while let Some(intent) = app.live.as_mut().unwrap().settings.take_intent() {
        runtime.settings(intent)?;
        observe(runtime, app, |event| {
            matches!(event, Observation::Settings(_))
        })?;
    }
    Ok(())
}
fn press(
    runtime: &mut Interactive,
    app: &mut Demo,
    code: KeyCode,
    modifiers: KeyModifiers,
) -> io::Result<String> {
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(code, modifiers));
    drain(runtime, app)?;
    Ok(draw(app))
}
fn key(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<String> {
    press(runtime, app, code, KeyModifiers::NONE)
}
fn type_text(runtime: &mut Interactive, app: &mut Demo, text: &str) -> io::Result<String> {
    let mut screen = String::new();
    for c in text.chars() {
        let shift = if c.is_uppercase() {
            KeyModifiers::SHIFT
        } else {
            KeyModifiers::NONE
        };
        screen = press(runtime, app, KeyCode::Char(c), shift)?;
    }
    Ok(screen)
}
fn go_to(runtime: &mut Interactive, app: &mut Demo, from: usize, to: usize) -> io::Result<()> {
    for _ in from..to {
        key(runtime, app, KeyCode::Down)?;
    }
    for _ in to..from {
        key(runtime, app, KeyCode::Up)?;
    }
    Ok(())
}

#[test]
#[ignore = "compiled Settings agent preferences against registered source RPC"]
fn agent_preferences_review_every_group_and_authority_change_without_side_effects() {
    let (runtime, data) = launch();
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve the draft.");
            app.live.as_mut().unwrap().settings.open();
            drain(runtime, &mut app)?;
            let opened = key(runtime, &mut app, KeyCode::Char('a'))?;
            assert!(
                opened.contains("Agent preferences") && opened.contains("Tool rounds per turn: 48"),
                "{opened}"
            );
            let global = data.join("settings.json");
            let conversation = conversation_settings(&data);
            let mut globals = json_file(&global);
            let mut execution = json_file(&conversation)["execution"].clone();

            // Execution limits (conversation, next turn): review is inert until confirmed.
            key(runtime, &mut app, KeyCode::Char('e'))?;
            for _ in 0..2 {
                key(runtime, &mut app, KeyCode::Backspace)?;
            }
            type_text(runtime, &mut app, "17")?;
            let review = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Tool rounds per turn?")
                    && review.contains("this conversation")
                    && review.contains("Applies from the next turn."),
                "{review}"
            );
            assert_eq!(json_file(&conversation)["execution"], execution);
            let saved = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk"), "{saved}");
            execution["tool_iterations"] = json!(17);
            assert_eq!(json_file(&conversation)["execution"], execution);
            assert_eq!(json_file(&global), globals);

            // Subagent route (device, immediate): writes the shared device document only.
            go_to(runtime, &mut app, 0, 10)?;
            key(runtime, &mut app, KeyCode::Char('e'))?;
            type_text(runtime, &mut app, "codex gpt-5.1")?;
            let review = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Global subagent route?")
                    && review.contains("Saving loads no model"),
                "{review}"
            );
            let saved = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk"), "{saved}");
            globals["subagent_route"] = json!({"backend": "codex", "model": "gpt-5.1"});
            assert_eq!(json_file(&global), globals);
            assert_eq!(json_file(&conversation)["execution"], execution);

            // Authority list: a widening change is stated, cancellable, then verified.
            go_to(runtime, &mut app, 10, 7)?;
            key(runtime, &mut app, KeyCode::Char('a'))?;
            type_text(runtime, &mut app, "bash:process_execution")?;
            let review = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Always-allow rules?")
                    && review.contains("AUTHORITY WIDENS")
                    && review.contains("Add: \"bash:process_execution\""),
                "{review}"
            );
            key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(json_file(&conversation)["execution"], execution);
            key(runtime, &mut app, KeyCode::Char('a'))?;
            type_text(runtime, &mut app, "bash:process_execution")?;
            key(runtime, &mut app, KeyCode::Enter)?;
            let saved = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk"), "{saved}");
            execution["tool_always_allow"] = json!(["bash:process_execution"]);
            assert_eq!(json_file(&conversation)["execution"], execution);
            // The source refuses a hand-written approval for irreversible actions.
            key(runtime, &mut app, KeyCode::Char('a'))?;
            type_text(runtime, &mut app, "bash:destructive_irreversible")?;
            let refused = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                refused.contains("approval prompt that shows the call"),
                "{refused}"
            );
            assert_eq!(json_file(&conversation)["execution"], execution);
            let refreshed = key(runtime, &mut app, KeyCode::F(5))?;
            assert!(
                refreshed.contains("Always-allow rules: 1 entry"),
                "{refreshed}"
            );
            // Removing it narrows authority and states so.
            key(runtime, &mut app, KeyCode::Char('x'))?;
            let review = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("AUTHORITY NARROWS")
                    && review.contains("Remove: \"bash:process_execution\""),
                "{review}"
            );
            key(runtime, &mut app, KeyCode::Enter)?;
            execution["tool_always_allow"] = json!([]);
            assert_eq!(json_file(&conversation)["execution"], execution);

            // Identity (defaults scope) and a reconnect-timed switch.
            go_to(runtime, &mut app, 7, 16)?;
            key(runtime, &mut app, KeyCode::Char('e'))?;
            type_text(runtime, &mut app, "Ryan")?;
            key(runtime, &mut app, KeyCode::Enter)?;
            key(runtime, &mut app, KeyCode::Enter)?;
            globals["user_name"] = json!("Ryan");
            assert_eq!(json_file(&global), globals);
            go_to(runtime, &mut app, 16, 19)?;
            key(runtime, &mut app, KeyCode::Right)?;
            let review = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Skills?") && review.contains("Saved only"),
                "{review}"
            );
            let saved = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Pending reconnect"), "{saved}");
            assert!(
                saved.contains("Skills: off (saved on)") && saved.contains("[locked]"),
                "{saved}"
            );
            execution["skills_enabled"] = json!(true);
            assert_eq!(json_file(&conversation)["execution"], execution);
            assert_eq!(json_file(&global), globals);

            key(runtime, &mut app, KeyCode::F(5))?;
            key(runtime, &mut app, KeyCode::Esc)?;
            key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve the draft.");
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["type"].as_str()?.strip_prefix("gui.agent.native_"))
        .collect::<Vec<_>>();
    let triple = ["review", "apply", "readback"];
    let expected: Vec<&str> = ["preferences"]
        .into_iter()
        .chain(triple) // tool_iterations
        .chain(triple) // subagent_route
        .chain(["review"]) // always-allow review, cancelled
        .chain(triple) // always-allow add
        .chain(["review", "preferences"]) // refused irreversible approval, refresh
        .chain(triple) // always-allow remove
        .chain(triple) // user_name
        .chain(triple) // skills_enabled (pending reconnect)
        .chain(["preferences"])
        .collect();
    assert_eq!(commands, expected);
    assert!(report["requests"].as_array().unwrap().is_empty());
    assert_eq!(json_file(&data.join("agent-guards.json")), json!([]));
}
