#![cfg(windows)]
//! F1 Work → Context, through the normal Interactive transport and an isolated source process.
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
    RUST_RUNTIME_CONTROLS_CASE='context-read-only', PYTHONDONTWRITEBYTECODE='1')
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,
        literal(&source),
        literal(&fixture.join("runtime_controls_runtime.py")),
        literal(&fixture.join(fixture_file))
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
            if let Observation::Work(update) = &event
                && let Data::Rejected(message) = &update.data
            {
                return Err(io::Error::other(message.clone()));
            }
            let done = until(&event);
            app.observe_live(event)?;
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

#[test]
#[ignore = "compiled Interactive and actual controlled Python source, requires LITETUI_AUTHORITY_PYTHON"]
fn context_pages_current_system_and_agent_files_without_sending_or_mutating() {
    let (runtime, data) = launch("context_runtime.py");
    let mut frames = Vec::new();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |event| {
            matches!(event, Observation::Ready)
        })?;
        let seed = json_file(&data.join("context-seed.json"));
        let owner = PathBuf::from(seed["directory"].as_str().unwrap()).canonicalize()?;
        assert!(
            owner.starts_with(data.canonicalize()?),
            "Only the disposable fixture may be changed"
        );
        app.insert_text("Draft selection stays here");
        app.composer.move_cursor(Motion::WordLeft, true);
        let original = (
            app.composer.text().to_string(),
            app.composer.before_cursor().to_string(),
            app.composer.selected_text().to_string(),
        );
        app.open_work();
        drain(runtime, &mut app)?;
        for code in [KeyCode::Char('i'), KeyCode::Right, KeyCode::Char(']')] {
            frames.push(key(runtime, &mut app, code)?);
        }
        assert!(frames[0].contains("CURRENT SYSTEM ROW"));
        assert!(frames[1].contains("MMMMMMMMMM"));
        assert!(
            frames[2].contains("雪")
                && frames[2].contains("not terminal color")
                && !frames[2].contains('\x1b')
        );
        frames.push(key(runtime, &mut app, KeyCode::Char('['))?);
        fs::write(
            owner.join("memory.md"),
            seed["changed_memory"].as_str().unwrap().as_bytes(),
        )?;
        let stale = key(runtime, &mut app, KeyCode::Char(']'))?;
        assert!(!stale.contains("Second page line") && !stale.contains("CHANGED memory"));
        frames.push(stale);
        let fresh = key(runtime, &mut app, KeyCode::F(5))?;
        assert!(fresh.contains("CHANGED memory"));
        frames.push(fresh);
        frames.push(key(runtime, &mut app, KeyCode::Right)?); // soul.md exists, zero bytes
        frames.push(key(runtime, &mut app, KeyCode::Right)?); // handoff.md does not exist
        fs::write(owner.join("handoff.md"), vec![b'x'; 262_145])?;
        frames.push(key(runtime, &mut app, KeyCode::F(5))?); // refuse oversized bytes
        let hooks = key(runtime, &mut app, KeyCode::Right)?;
        assert!(hooks.contains(r#"global/"audit-review"  disabled  observe"#));
        assert!(hooks.contains("tool_before") && !hooks.contains("private-test"));
        frames.push(hooks);
        let system = key(runtime, &mut app, KeyCode::Right)?;
        assert!(system.contains("CURRENT SYSTEM ROW"));
        assert!(key(runtime, &mut app, KeyCode::Left)?.contains("audit-review"));
        frames.push(key(runtime, &mut app, KeyCode::Esc)?);
        assert!(app.live.as_ref().unwrap().work.owns_input());
        frames.push(key(runtime, &mut app, KeyCode::Esc)?);
        assert!(!app.live.as_ref().unwrap().work.owns_input());
        assert_eq!(
            original,
            (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string()
            )
        );
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        Ok(())
    });
    fs::write(
        data.join("context-native-frames.txt"),
        frames.join("\n\nNEXT FRAME\n\n"),
    )
    .unwrap();
    result.unwrap_or_else(|error| panic!("{}: {error}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let seed = json_file(&data.join("context-seed.json"));
    let commands = report["commands"].as_array().unwrap();
    let owned = commands
        .iter()
        .filter(|c| c["type"] == "gui.context.native_page")
        .collect::<Vec<_>>();
    assert_eq!(owned.len(), 12);
    let wire = report["wire"].as_array().unwrap();
    let expected = [
        ("system", "page"),
        ("memory.md", "page"),
        ("memory.md", "page"),
        ("memory.md", "page"),
        ("memory.md", "changed"),
        ("memory.md", "page"),
        ("soul.md", "empty"),
        ("handoff.md", "missing"),
        ("handoff.md", "oversize"),
        ("hooks", "page"),
        ("system", "page"),
        ("hooks", "page"),
    ];
    for (command, (document, status)) in owned.iter().zip(expected) {
        let frame = wire
            .iter()
            .find(|row| row["type"] == "response" && row["id"] == command["id"])
            .unwrap();
        assert_eq!(frame["ok"], true);
        let page = &frame["result"];
        assert_eq!(page["session_id"], seed["system_session"]);
        assert_eq!(page["work_instance"], report["work"]["instance"]);
        assert_eq!(page["document"], document);
        assert_eq!(page["status"], status);
        if document == "system" {
            assert_eq!(page["text"], seed["system"]);
        }
        if status == "changed" {
            assert_eq!(page["text"], "");
            assert_eq!(page["revision"], Value::Null);
        }
        if document == "memory.md" && command["offset"] == 4094 && status == "page" {
            assert!(page["text"].as_str().unwrap().starts_with('雪'));
        }
    }
    assert!(owned[2]["offset"] == 4094 && owned[2]["revision"] == seed["initial_digest"]);
    assert!(owned[4]["offset"] == 4094 && owned[4]["revision"] == seed["initial_digest"]);
    assert!(owned[5]["offset"] == 0 && owned[5]["revision"].is_null());
    assert_eq!(report["history"], report["initial_history"]);
    assert_eq!(report["history"], report["durable"]);
    assert_eq!(report["initial_documents"], report["final_documents"]);
    assert_eq!(report["before"], report["after"]);
    assert_eq!(
        json_file(&data.join("context-after.json"))["memory.md"],
        seed["changed_digest"]
    );
    for name in [
        "requests",
        "denied",
        "questions",
        "approvals",
        "authorities",
    ] {
        assert_eq!(report[name], json!([]), "{name}");
    }
    assert!(
        !commands
            .iter()
            .any(|c| c["type"] == "prompt"
                || c["type"].as_str().is_some_and(|k| k.contains("apply")))
    );
    assert_eq!(
        report["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
    assert_eq!(report["reader_retired"], true);
    assert!(
        report["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    eprintln!("Controlled Context process report: {}", data.display());
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

#[test]
#[ignore = "compiled Settings Runtime status, controlled source/cache; requires LITETUI_AUTHORITY_PYTHON"]
fn runtime_status_uses_only_bounded_cached_sanitized_facts_on_open_and_refresh() {
    let (runtime, data) = launch("inspection_runtime.py");
    let mut frames = Vec::new();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |event| {
            matches!(event, Observation::Ready)
        })?;
        app.insert_text("Keep this private composer draft.");
        app.composer.move_cursor(Motion::WordLeft, true);
        let draft = (
            app.composer.text().to_string(),
            app.composer.before_cursor().to_string(),
            app.composer.selected_text().to_string(),
        );
        app.open_settings();
        settings_drain(runtime, &mut app)?;
        frames.push(settings_key(runtime, &mut app, KeyCode::Char('i'))?);
        frames.push(settings_key(runtime, &mut app, KeyCode::End)?);
        frames.push(settings_key(runtime, &mut app, KeyCode::F(5))?);
        frames.push(settings_key(runtime, &mut app, KeyCode::Home)?);
        let visible = frames.join("\n");
        assert!(visible.contains("Runtime status") && visible.contains("Runtime version"));
        assert!(visible.contains("cached-alpha"));
        assert!(!visible.contains("SYNTHETIC_MCP_SECRET") && !visible.contains("example.invalid"));
        frames.push(settings_key(runtime, &mut app, KeyCode::Esc)?);
        assert!(app.live.as_ref().unwrap().settings.owns_input());
        frames.push(settings_key(runtime, &mut app, KeyCode::Esc)?);
        assert!(!app.live.as_ref().unwrap().settings.owns_input());
        assert_eq!(
            draft,
            (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string()
            )
        );
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        Ok(())
    });
    fs::write(
        data.join("inspection-native-frames.txt"),
        frames.join("\n\nNEXT FRAME\n\n"),
    )
    .unwrap();
    result.unwrap_or_else(|error| panic!("{}: {error}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let boundaries = json_file(&data.join("inspection-boundaries.json"));
    assert_eq!(boundaries["attempts"], json!([]));
    let commands = report["commands"].as_array().unwrap();
    let requests: Vec<_> = commands
        .iter()
        .filter(|cmd| cmd["type"] == "gui.inspection.native_status")
        .collect();
    assert_eq!(requests.len(), 2);
    let values: Vec<_> = requests
        .iter()
        .map(|cmd| {
            let frame = report["wire"]
                .as_array()
                .unwrap()
                .iter()
                .find(|row| row["type"] == "response" && row["id"] == cmd["id"])
                .unwrap();
            assert_eq!(frame["ok"], true);
            &frame["result"]
        })
        .collect();
    for (idx, value) in values.iter().enumerate() {
        assert_eq!(value["kind"], "runtime_inspection");
        assert_eq!(
            value["session_id"],
            report["before"]["identity"]["session_id"]
        );
        assert_eq!(value["work_instance"], report["work"]["instance"]);
        let mcp = &value["mcp"];
        assert_eq!(mcp["enabled"], false);
        assert_eq!(mcp["available"], true);
        assert_eq!(mcp["complete"], false);
        let sample = &boundaries["samples"][idx];
        assert_eq!(sample["declared"], 39);
        let servers = mcp["servers"].as_array().unwrap();
        assert_eq!(servers.len(), 32);
        if let Some(remaining) = mcp["remaining"].as_u64() {
            assert_eq!(remaining, 7);
        }
        let connected = servers
            .iter()
            .find(|row| row["name"] == "cached-alpha")
            .unwrap();
        assert_eq!(connected["transport"], "http");
        assert_eq!(connected["state"], "connected");
        assert_eq!(connected["tool_count"], sample["cached_alpha_tools"]);
        let failed = servers
            .iter()
            .find(|row| row["name"] == "cached-failed")
            .unwrap();
        assert_eq!(failed["state"], "failed");
        assert_eq!(failed["transport"], "stdio");
        let text = serde_json::to_string(value).unwrap();
        assert!(!text.contains("SYNTHETIC_MCP_SECRET") && !text.contains("example.invalid"));
    }
    assert_eq!(
        values[0]["diagnostics"]["runtime_version"],
        values[1]["diagnostics"]["runtime_version"]
    );
    assert_ne!(
        values[0]["diagnostics"]["conversation_persistence"],
        values[1]["diagnostics"]["conversation_persistence"]
    );
    assert_ne!(
        values[0]["diagnostics"]["settings_persistence"],
        values[1]["diagnostics"]["settings_persistence"]
    );
    assert_eq!(report["initial_documents"], report["final_documents"]);
    assert_eq!(report["history"], report["durable"]);
    assert_eq!(report["before"], report["after"]);
    for field in [
        "requests",
        "denied",
        "questions",
        "approvals",
        "authorities",
    ] {
        assert_eq!(report[field], json!([]), "{field}");
    }
    assert!(commands.iter().all(
        |cmd| cmd["type"].as_str().is_some_and(|name| name != "prompt"
            && !name.starts_with("gui.mcp.")
            && !name.contains("apply"))
    ));
    assert_eq!(report["reader_retired"], true);
    assert_eq!(
        report["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
    eprintln!(
        "Controlled Runtime status process report: {}",
        data.display()
    );
}
