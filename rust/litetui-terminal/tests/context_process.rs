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
        let plugins = key(runtime, &mut app, KeyCode::Right)?;
        assert!(plugins.contains("Context · Plugins") && plugins.contains("fixture-parser"));
        assert!(plugins.contains("disabled") && plugins.contains("load order"));
        frames.push(plugins);
        let system = key(runtime, &mut app, KeyCode::Right)?;
        assert!(system.contains("CURRENT SYSTEM ROW"));
        assert!(key(runtime, &mut app, KeyCode::Left)?.contains("fixture-parser"));
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
    assert_eq!(owned.len(), 13);
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
        ("plugins", "page"),
        ("system", "page"),
        ("plugins", "page"),
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

#[test]
#[ignore = "real owned Python memory writer through compiled Interactive; requires LITETUI_AUTHORITY_PYTHON"]
fn context_edits_keep_draft_through_stale_review_and_verify_exact_publication() {
    let (runtime, data) = launch("context_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let seed = json_file(&data.join("context-seed.json"));
        let directory = PathBuf::from(seed["directory"].as_str().unwrap()).canonicalize()?;
        assert!(directory.starts_with(data.canonicalize()?));
        let target = directory.join("soul.md");
        app.insert_text("Ordinary draft stays here");
        app.composer.move_cursor(Motion::WordLeft, true);
        let composer = (
            app.composer.text().to_owned(),
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        app.open_work();
        drain(runtime, &mut app)?;
        for code in [
            KeyCode::Char('i'),
            KeyCode::Right,
            KeyCode::Right,
            KeyCode::Char('e'),
        ] {
            key(runtime, &mut app, code)?;
        }
        assert!(draw(&mut app).contains("Edit soul.md"));
        let draft = "Two exact lines\r\n雪\n";
        app.live.as_mut().unwrap().work.paste(draft);
        assert!(key(runtime, &mut app, KeyCode::F(6))?.contains("Review soul.md"));
        assert_eq!(fs::read(&target)?, b"");
        assert!(key(runtime, &mut app, KeyCode::Esc)?.contains("Edit soul.md"));
        assert!(key(runtime, &mut app, KeyCode::Esc)?.contains("Discard this memory draft?"));
        assert!(key(runtime, &mut app, KeyCode::Esc)?.contains("Two exact lines"));
        key(runtime, &mut app, KeyCode::F(6))?;
        fs::write(&target, b"outside update")?;
        assert!(
            key(runtime, &mut app, KeyCode::F(6))
                .unwrap_err()
                .to_string()
                .contains("changed since review")
        );
        assert!(draw(&mut app).contains("Draft kept."));
        assert_eq!(fs::read(&target)?, b"outside update");
        assert!(key(runtime, &mut app, KeyCode::F(5))?.contains("Current file reloaded"));
        let review = key(runtime, &mut app, KeyCode::F(6))?;
        assert!(review.contains("outside update") && review.contains("Two exact lines"));
        assert!(key(runtime, &mut app, KeyCode::F(6))?.contains("Saved and verified"));
        assert_eq!(fs::read(&target)?, draft.as_bytes());
        key(runtime, &mut app, KeyCode::Esc)?;
        let absent = key(runtime, &mut app, KeyCode::Right)?;
        assert!(absent.contains("absent"));
        key(runtime, &mut app, KeyCode::Char('e'))?;
        assert!(!directory.join("handoff.md").exists());
        assert!(key(runtime, &mut app, KeyCode::F(6))?.contains("(absent)"));
        assert!(key(runtime, &mut app, KeyCode::F(6))?.contains("Saved and verified"));
        assert_eq!(fs::read(directory.join("handoff.md"))?, b"");
        for _ in 0..3 {
            key(runtime, &mut app, KeyCode::Esc)?;
        }
        assert_eq!(
            composer,
            (
                app.composer.text().to_owned(),
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            )
        );
        Ok(())
    });
    result.unwrap_or_else(|e| panic!("Context edit failed in {}: {e}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"].as_array().unwrap();
    assert_eq!(
        commands
            .iter()
            .filter(|v| v["type"] == "gui.context.native_apply")
            .count(),
        3
    );
    assert_eq!(
        commands
            .iter()
            .filter(|v| v["type"] == "gui.context.native_readback")
            .count(),
        2
    );
    assert!(!commands.iter().any(|v| v["type"] == "prompt"));
    assert_eq!(
        report["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
}

#[test]
#[ignore = "real Interactive with generated local image, dependency Python and controlled provider"]
fn image_selection_clear_and_explicit_submission_use_the_reviewed_pixels() {
    let (runtime, data) = launch("image_runtime.py");
    let path = data.join("selected image.png");
    let mut digest = String::new();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("Inspect these pixels.");
        app.open_image();
        draw(&mut app);
        let work=&mut app.live.as_mut().unwrap().work;
        work.paste(data.join("missing.png").to_str().unwrap());
        work.key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
        runtime.work(work.take_intent().unwrap())?;
        loop {
            let event=runtime.events.recv_timeout(Duration::from_secs(20)).map_err(io::Error::other)?;
            let refused=matches!(&event,Observation::Work(update) if matches!(&update.data,Data::Rejected(reason) if reason.contains("does not exist")));
            app.observe_live(event)?;
            if refused {break;}
        }
        assert!(draw(&mut app).contains("does not exist"));
        key(runtime,&mut app,KeyCode::Esc)?;
        assert_eq!(app.composer.text(),"Inspect these pixels.");
        for clear in [true, false] {
            app.open_image();
            assert!(draw(&mut app).contains("Local image file"));
            app.live
                .as_mut()
                .unwrap()
                .work
                .paste(path.to_str().unwrap());
            let screen = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                screen.contains("selected image.png")
                    && screen.contains("1920 × 1080")
                    && screen.contains("1536 × 864")
                    && screen.contains("Enter attaches"),
                "{screen}"
            );
            key(runtime, &mut app, KeyCode::Enter)?;
            let image = app
                .live
                .as_ref()
                .unwrap()
                .work
                .image
                .attached
                .as_ref()
                .unwrap();
            assert!(image.wire()["png_bytes"].as_u64().unwrap() > 384 * 1024);
            digest = image.wire()["png_sha256"].as_str().unwrap().into();
            assert_eq!(app.composer.text(), "Inspect these pixels.");
            assert!(app.live.as_ref().unwrap().rows.is_empty());
            if clear {
                app.open_image();
                assert!(key(runtime, &mut app, KeyCode::Delete)?.contains("Image cleared"));
                assert!(app.live.as_ref().unwrap().work.image.attached.is_none());
            }
        }
        // A subsequent replacement of the local file cannot change the already reviewed image.
        fs::remove_file(&path)?;
        app.submit();
        let chat = app.live.as_mut().unwrap();
        assert!(chat.take_submission().is_none());
        let (text, image) = chat.take_image_submission().unwrap();
        runtime.submit_image(text, image)?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        assert!(draw(&mut app).contains("selected image.png"));
        Ok(())
    });
    result.unwrap();
    let saved = json_file(&data.join("image-resume-reader.json"));
    assert_eq!(saved["digest"], digest);
    assert_eq!(saved["text"], "Inspect these pixels.");
    assert_eq!(saved["pending_image"], false);
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"].as_array().unwrap().len(), 1);
    let messages = report["requests"][0]["messages"].as_array().unwrap();
    let user = messages
        .iter()
        .rev()
        .find(|row| row["role"] == "user")
        .unwrap();
    assert_eq!(user["content"][1]["text"], "Inspect these pixels.");
    assert!(
        user["content"][0]["image_url"]["url"]
            .as_str()
            .unwrap()
            .starts_with("data:image/png;base64,")
    );
    assert_eq!(
        report["commands"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["type"] == "prompt")
            .count(),
        1
    );
}

#[test]
#[ignore = "owned Interactive source with private clipboard allocations; no desktop clipboard access"]
fn clipboard_review_replace_clear_and_file_choice_use_the_captured_version() {
    fn review(runtime: &mut Interactive, app: &mut Demo) -> io::Result<String> {
        app.open_image();
        app.live.as_mut().unwrap().work.read_clipboard_image();
        drain(runtime, app)?;
        Ok(draw(app))
    }
    let (runtime, data) = launch("clipboard_runtime.py");
    let mut final_digest = String::new();
    let result=runtime.with_cleanup(|runtime| {
        let mut app=Demo::live("custom".into(),"switch-old".into());
        observe(runtime,&mut app,|e|matches!(e,Observation::Ready))?;
        app.insert_text("Which pixels did I choose?");
        let screen=review(runtime,&mut app)?;
        assert!(screen.contains("Clipboard DIB #201") && screen.contains("F2 chooses a file"),"{screen}");
        key(runtime,&mut app,KeyCode::Enter)?;
        let held=|app:&Demo|app.live.as_ref().unwrap().work.image.attached.as_ref().unwrap().wire().clone();
        let first=held(&app);
        // A valid PNG is already offered. F2 cancels it and allows a manual replacement instead.
        assert!(review(runtime,&mut app)?.contains("Clipboard PNG #202"));
        assert!(key(runtime,&mut app,KeyCode::F(2))?.contains("Local image file"));
        app.live.as_mut().unwrap().work.paste(data.join("selected image.png").to_str().unwrap());
        assert!(key(runtime,&mut app,KeyCode::Enter)?.contains("Enter replaces"));
        key(runtime,&mut app,KeyCode::Esc)?;
        assert_eq!(held(&app),first);
        assert!(review(runtime,&mut app)?.contains("Clipboard FILES #203"));
        key(runtime,&mut app,KeyCode::Enter)?;
        assert_ne!(held(&app)["token"],first["token"]);
        app.open_image();
        key(runtime,&mut app,KeyCode::Delete)?;
        assert!(app.live.as_ref().unwrap().work.image.attached.is_none());
        // Empty/unsupported clipboard gives a clear message, still allows file entry, keeps the draft.
        app.open_image();
        let work=&mut app.live.as_mut().unwrap().work;
        work.read_clipboard_image();
        runtime.work(work.take_intent().unwrap())?;
        loop {
            let event=runtime.events.recv_timeout(Duration::from_secs(20)).map_err(io::Error::other)?;
            let refused=matches!(&event,Observation::Work(update) if matches!(update.data,Data::Rejected(_)));
            app.observe_live(event)?;
            if refused{break;}
        }
        assert!(draw(&mut app).contains("No supported image"));
        key(runtime,&mut app,KeyCode::Esc)?;
        app.open_image();
        draw(&mut app);
        app.live.as_mut().unwrap().work.paste(data.join("selected image.png").to_str().unwrap());
        key(runtime, &mut app, KeyCode::Enter)?;
        key(runtime, &mut app, KeyCode::Enter)?;
        let previous = held(&app);
        assert!(review(runtime,&mut app)?.contains("Clipboard DIBV5 #205"));
        key(runtime,&mut app,KeyCode::Enter)?;
        assert_ne!(previous["png_sha256"], held(&app)["png_sha256"]);
        final_digest=held(&app)["png_sha256"].as_str().unwrap().to_owned();
        assert_eq!(app.composer.text(),"Which pixels did I choose?");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        app.submit();
        let (text,image)=app.live.as_mut().unwrap().take_image_submission().unwrap();
        runtime.submit_image(text,image)?;
        observe(runtime,&mut app,|e|matches!(e,Observation::Complete))?;
        Ok(())
    });
    result.unwrap();
    let saved = json_file(&data.join("image-resume-reader.json"));
    assert_eq!(saved["digest"], final_digest);
    assert_eq!(saved["text"], "Which pixels did I choose?");
    assert_eq!(saved["pending_image"], false);
    let ownership = json_file(&data.join("clipboard-ownership.json"));
    assert_eq!(ownership.as_array().unwrap().len(), 5);
    for (i, entry) in ownership.as_array().unwrap().iter().enumerate() {
        let events = entry.as_array().unwrap();
        assert_eq!(events.first().unwrap(), "open");
        assert_eq!(events.last().unwrap(), "close");
        assert_eq!(events.contains(&json!("unlock")), i != 3);
    }
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"].as_array().unwrap().len(), 1);
    assert_eq!(
        report["commands"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["type"] == "prompt")
            .count(),
        1
    );
}

#[test]
#[ignore = "owned source refusal/recovery with private clipboard images; no desktop clipboard access"]
fn clipboard_declining_b_keeps_a_across_refused_caption_then_submits_a_once() {
    let (runtime, data) = launch("clipboard_decline_runtime.py");
    let mut digest = String::new();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let long = "This long caption exceeds the source window and must remain editable after the source has refused it.".repeat(2);
        app.insert_text(&long);
        app.open_image();
        app.live.as_mut().unwrap().work.read_clipboard_image();
        drain(runtime, &mut app)?;
        key(runtime, &mut app, KeyCode::Enter)?;
        let first = app.live.as_ref().unwrap().work.image.attached.as_ref().unwrap().wire().clone();
        digest = first["png_sha256"].as_str().unwrap().into();
        app.open_image();
        app.live.as_mut().unwrap().work.read_clipboard_image();
        drain(runtime, &mut app)?;
        assert!(draw(&mut app).contains("Clipboard PNG #202"));
        key(runtime, &mut app, KeyCode::Esc)?;
        assert_eq!(app.live.as_ref().unwrap().work.image.attached.as_ref().unwrap().wire(), &first);
        app.submit();
        let (prompt, image) = app.live.as_mut().unwrap().take_image_submission().unwrap();
        runtime.submit_image(prompt, image)?;
        observe(runtime, &mut app, |e| matches!(e, Observation::ImageRefused { .. }))?;
        assert_eq!(app.composer.text(), long);
        assert_eq!(app.live.as_ref().unwrap().work.image.attached.as_ref().unwrap().wire(), &first);
        app.composer.clear();
        app.insert_text("Inspect retained A.");
        app.submit();
        let (prompt, image) = app.live.as_mut().unwrap().take_image_submission().unwrap();
        runtime.submit_image(prompt, image)?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        Ok(())
    });
    result.unwrap();
    let saved = json_file(&data.join("image-resume-reader.json"));
    assert_eq!(saved["digest"], digest);
    assert_eq!(saved["text"], "Inspect retained A.");
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"].as_array().unwrap().len(), 1);
    let refused = report["wire"]
        .as_array()
        .unwrap()
        .iter()
        .find(|v| v["result"]["turn"] == "image_refused")
        .unwrap();
    assert_eq!(refused["result"]["retained"]["png_sha256"], digest);
    assert_eq!(
        report["commands"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["type"] == "prompt")
            .count(),
        2
    );
}
