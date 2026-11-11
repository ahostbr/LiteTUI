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
    launch_fixture(fixture_file, false)
}
fn launch_fixture(fixture_file: &str, command_controls: bool) -> (Interactive, PathBuf) {
    launch_fixture_resume(fixture_file, command_controls, None)
}
fn launch_fixture_resume(
    fixture_file: &str,
    command_controls: bool,
    resume: Option<(&Path, &str)>,
) -> (Interactive, PathBuf) {
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
    let data = resume.map_or_else(|| directory.join("data"), |(root, _)| root.to_path_buf());
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
if {}:
    s['thinking_level']='medium'
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,
        literal(&source),
        literal(&fixture.join("runtime_controls_runtime.py")),
        literal(&fixture.join(fixture_file)),
        if fixture_file == "model_preferences_effort_runtime.py" {
            "True"
        } else {
            "False"
        },
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
        if matches!(
            fixture_file,
            "model_preferences_full_runtime.py" | "model_preferences_effort_runtime.py"
        ) {
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
    if matches!(
        fixture_file,
        "model_preferences_full_runtime.py" | "model_preferences_effort_runtime.py"
    ) {
        let index = args.iter().position(|s| s == "--thinking-level").unwrap();
        args.drain(index..index + 2);
    }
    if let Some((_, session)) = resume {
        args.extend(["--resume".into(), session.into()]);
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
        assert!(key(runtime, &mut app, KeyCode::Right)?.contains("No memory topics."));
        let system = key(runtime, &mut app, KeyCode::Right)?;
        assert!(system.contains("CURRENT SYSTEM ROW"));
        assert!(key(runtime, &mut app, KeyCode::Left)?.contains("No memory topics."));
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
#[ignore = "compiled Settings Voice preferences against registered source RPC"]
fn voice_preferences_review_and_reread_device_values_without_audio() {
    let (runtime, data) = launch("voice_preferences_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve the draft and selection.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selected = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let opened = settings_key(runtime, &mut app, KeyCode::Char('v'))?;
            assert!(
                opened.contains("Voice preferences on this device")
                    && opened.contains("Microphone:"),
                "{opened}"
            );
            let path = data.join("settings.json");
            let mut expected = json_file(&path);
            assert_eq!(expected["tts_enabled"], false);
            assert!(opened.contains("Response speech: off"), "{opened}");
            for (index, (name, value, label)) in [
                ("stt_model", json!("small.en"), "Dictation model"),
                ("tts_engine", json!("edge"), "Fallback speech engine"),
                (
                    "tts_litesuite_first",
                    json!(false),
                    "Prefer LiteSuite speech",
                ),
                ("tts_enabled", json!(true), "Response speech"),
                ("stt_mic", json!("Microphone \u{0394}"), "Microphone"),
                ("stt_hotkey", json!("alt+r"), "Dictation shortcut"),
                (
                    "tts_voice",
                    json!("Configured SAPI Voice \u{03a9}"),
                    "Windows voice",
                ),
                ("tts_edge_voice", json!("en-US-AriaNeural"), "Edge voice"),
                ("tts_timeout", json!(450), "Local speech timeout"),
            ]
            .into_iter()
            .enumerate()
            {
                if index > 0 {
                    settings_key(runtime, &mut app, KeyCode::Down)?;
                }
                if index < 4 {
                    settings_key(runtime, &mut app, KeyCode::Right)?;
                } else {
                    settings_key(runtime, &mut app, KeyCode::Char('e'))?;
                    settings_key(runtime, &mut app, KeyCode::Delete)?;
                    let text = value
                        .as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| value.to_string());
                    assert!(app.live.as_ref().unwrap().settings.accepts_paste());
                    app.live.as_mut().unwrap().settings.paste(&text);
                    draw(&mut app);
                }
                let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(review.contains(&format!("Save {label}?")), "{review}");
                assert_eq!(json_file(&path), expected);
                if index == 0 {
                    settings_key(runtime, &mut app, KeyCode::Esc)?;
                    assert_eq!(json_file(&path), expected);
                    settings_key(runtime, &mut app, KeyCode::Right)?;
                    settings_key(runtime, &mut app, KeyCode::Enter)?;
                }
                let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(saved.contains("Saved and verified on disk."), "{saved}");
                expected[name] = value;
                assert_eq!(json_file(&path), expected);
            }
            let refreshed = settings_key(runtime, &mut app, KeyCode::F(5))?;
            assert!(refreshed.contains("Response speech: on"), "{refreshed}");
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve the draft and selection.");
            assert_eq!(app.composer.before_cursor(), selected);
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["type"].as_str()?.strip_prefix("gui.voice.native_"))
        .collect::<Vec<_>>();
    assert_eq!(
        commands,
        [
            "preferences",
            "review",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "preferences"
        ]
    );
    assert!(report["requests"].as_array().unwrap().is_empty());
    assert_eq!(json_file(&data.join("voice-guards.json")), json!([]));
}

fn settings_files(data: &Path) -> Vec<(PathBuf, Vec<u8>)> {
    fn walk(dir: &Path, out: &mut Vec<(PathBuf, Vec<u8>)>) {
        let Ok(entries) = fs::read_dir(dir) else {
            return;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                walk(&path, out);
            } else if path.file_name().is_some_and(|name| name == "settings.json") {
                out.push((path.clone(), fs::read(&path).unwrap()));
            }
        }
    }
    let mut out = Vec::new();
    walk(data, &mut out);
    out.sort();
    out
}

#[test]
#[ignore = "compiled discovery settings against private source with scan and network tripwires"]
fn discovery_preferences_review_device_scope_without_scanning() {
    let (runtime, data) = launch("connection_preferences_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Keep discovery draft \u{96ea}, selection.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let draft = app.composer.text().to_owned();
            let cursor = app.composer.before_cursor().to_owned();
            let before = settings_files(&data);
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('n'))?;
            for _ in 0..12 {
                settings_key(runtime, &mut app, KeyCode::Down)?;
            }
            for _ in 0..3 {
                settings_key(runtime, &mut app, KeyCode::Right)?;
                let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(
                    review.contains("Scope: device") && review.contains("next model-list refresh"),
                    "{review}"
                );
                let done = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(done.contains("Saved and verified on disk."), "{done}");
                settings_key(runtime, &mut app, KeyCode::Down)?;
            }
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            for _ in 0..10 {
                settings_key(runtime, &mut app, KeyCode::Backspace)?;
            }
            let paths = json!([r"C:\models, local", "D:/models/\u{96ea}"]);
            for c in paths.to_string().chars() {
                settings_key(runtime, &mut app, KeyCode::Char(c))?;
            }
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Extra model folders")
                    && review.contains("C:")
                    && review.contains("models, local"),
                "{review}"
            );
            let done = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(done.contains("Saved and verified on disk."), "{done}");
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), draft);
            assert_eq!(app.composer.before_cursor(), cursor);
            assert_ne!(settings_files(&data), before);
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    assert_eq!(json_file(&data.join("connection-guards.json")), json!([]));
    let report = json_file(&data.join("connection-saved.json"));
    assert_eq!(report.as_array().unwrap().len(), 4);
    let expected = json!({"llama_scan_litesuite":false, "llama_scan_lmstudio":false,
        "llama_scan_hf_cache":false, "llama_models_dirs":[r"C:\models, local", "D:/models/\u{96ea}"]});
    assert_eq!(report[3]["discovery"]["saved"], expected);
    assert_eq!(report[3]["discovery"]["effective"], expected);
}

#[test]
#[ignore = "compiled Settings connection preferences against registered source RPC"]
fn connection_preferences_review_save_and_reread_without_connecting() {
    let (runtime, data) = launch("connection_preferences_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve the draft and selection.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selected = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let opened = settings_key(runtime, &mut app, KeyCode::Char('n'))?;
            assert!(
                opened.contains("Connection and local-server preferences")
                    && opened.contains("LM Studio address")
                    && opened.contains("never read or shown here"),
                "{opened}"
            );
            let before = settings_files(&data);
            // Review a toggle, then cancel: nothing is written.
            settings_key(runtime, &mut app, KeyCode::Down)?;
            settings_key(runtime, &mut app, KeyCode::Right)?;
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Re-select the default model on connect?")
                    && review.contains("Applies: the next reconnect or relaunch")
                    && review
                        .replace(
                            "│
│", ""
                        )
                        .contains("Saving never reconnects"),
                "{review}"
            );
            assert_eq!(settings_files(&data), before);
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(settings_files(&data), before);
            // The same toggle again, saved.
            settings_key(runtime, &mut app, KeyCode::Right)?;
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk."), "{saved}");
            assert_ne!(settings_files(&data), before);
            // A text field: replace the LM Studio address.
            settings_key(runtime, &mut app, KeyCode::Up)?;
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            for _ in 0..80 {
                settings_key(runtime, &mut app, KeyCode::Backspace)?;
            }
            for c in "http://h.test:9".chars() {
                settings_key(runtime, &mut app, KeyCode::Char(c))?;
            }
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save LM Studio address?")
                    && review.contains("Requested: http://h.test:9"),
                "{review}"
            );
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk."), "{saved}");
            let pending = settings_key(runtime, &mut app, KeyCode::F(5))?;
            assert!(
                pending.contains("saved http://h.test:9; applies the next reconnect"),
                "{pending}"
            );
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve the draft and selection.");
            assert_eq!(app.composer.before_cursor(), selected);
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["type"].as_str()?.strip_prefix("gui.connection.native_"))
        .collect::<Vec<_>>();
    assert_eq!(
        commands,
        [
            "preferences",
            "review",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "preferences"
        ]
    );
    assert_eq!(json_file(&data.join("connection-guards.json")), json!([]));
    // The reconnect fields were saved but stay pending: nothing applied them to the session.
    let saved = json_file(&data.join("connection-saved.json"));
    assert_eq!(saved[0]["saved"]["pin_default_model"], true);
    assert_eq!(saved[0]["effective"]["pin_default_model"], false);
    assert_eq!(saved[1]["saved"]["lm_host"], "http://h.test:9");
    assert_ne!(saved[1]["effective"]["lm_host"], "http://h.test:9");
}

#[test]
#[ignore = "compiled Settings Interface preferences against registered source RPC"]
fn presentation_preferences_review_save_and_reread_change_what_is_rendered() {
    let (runtime, data) = launch("presentation_preferences_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve the draft and selection.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selected = app.composer.before_cursor().to_owned();
            // The real startup state carried the effective values, and they are the source defaults.
            let startup = app.live.as_ref().unwrap().runtime_state.startup().unwrap();
            assert_eq!(startup.presentation, Default::default());
            assert!(app.presentation().show_thinking && !app.presentation().sidebar_dialogs);
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let opened = settings_key(runtime, &mut app, KeyCode::Char('u'))?;
            assert!(
                opened.contains("Interface preferences on this device")
                    && opened.contains("Show thinking blocks: on"),
                "{opened}"
            );
            let path = data.join("settings.json");
            let mut expected = json_file(&path);
            // Cancelling a review writes nothing; the draft survives for another try.
            settings_key(runtime, &mut app, KeyCode::Right)?;
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(review.contains("Save Show thinking blocks?"), "{review}");
            assert!(
                review.contains("Requested: off") && review.contains("immediately"),
                "{review}"
            );
            assert_eq!(json_file(&path), expected);
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(json_file(&path), expected);
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk."), "{saved}");
            expected["show_thinking"] = json!(false);
            assert_eq!(json_file(&path), expected);
            assert!(!app.presentation().show_thinking);

            for _ in 0..5 {
                settings_key(runtime, &mut app, KeyCode::Down)?;
            }
            settings_key(runtime, &mut app, KeyCode::Right)?;
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk."), "{saved}");
            expected["dialog_style"] = json!("sidebar");
            assert_eq!(json_file(&path), expected);
            assert!(app.presentation().sidebar_dialogs);

            for _ in 0..13 {
                settings_key(runtime, &mut app, KeyCode::Down)?;
            }
            for _ in 0..7 {
                settings_key(runtime, &mut app, KeyCode::Right)?;
            }
            let draft = settings_key(runtime, &mut app, KeyCode::Char('['))?;
            assert!(
                draft.contains("[tps] cache") && draft.contains("(draft)"),
                "{draft}"
            );
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Footer order?") && review.contains("Requested:"),
                "{review}"
            );
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(saved.contains("Saved and verified on disk."), "{saved}");
            let disk = json_file(&path);
            assert_eq!(disk["footer_order"][6], "tps");
            assert_eq!(disk["footer_order"][7], "cache");
            assert_eq!(app.presentation().order()[6], "tps");
            settings_key(runtime, &mut app, KeyCode::F(5))?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve the draft and selection.");
            assert_eq!(app.composer.before_cursor(), selected);
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["type"].as_str()?.strip_prefix("gui.presentation.native_"))
        .collect::<Vec<_>>();
    assert_eq!(
        commands,
        [
            "preferences",
            "review",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "review",
            "apply",
            "readback",
            "preferences"
        ]
    );
    assert_eq!(json_file(&data.join("presentation-guards.json")), json!([]));
}

#[test]
#[ignore = "compiled Settings MCP declaration review; dependency Python required"]
fn shared_mcp_declaration_requires_review_and_fresh_disk_readback() {
    let (runtime, data) = launch("mcp_declaration_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Keep this composer draft.");
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let first = settings_key(runtime, &mut app, KeyCode::Char('m'))?;
            assert!(
                first.contains("primary") && !first.contains("PRIVATE_MCP_SENTINEL"),
                "{first}"
            );
            let next = settings_key(runtime, &mut app, KeyCode::Down)?;
            assert!(
                next.contains("offline") && !next.contains("example.invalid"),
                "{next}"
            );
            let review = settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            assert!(review.contains("Disable offline in .mcp.json?"), "{review}");
            assert!(
                json_file(&data.join(".mcp.json"))["mcpServers"]["offline"]
                    .get("disabled")
                    .is_none()
            );
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                saved.contains("Saved and verified on disk.") && saved.contains("disabled"),
                "{saved}"
            );
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Keep this composer draft.");
            Ok(())
        })
        .unwrap_or_else(|error| panic!("{}: {error}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| c["type"].as_str())
        .filter(|s| s.starts_with("gui.mcp."))
        .collect::<Vec<_>>();
    assert_eq!(
        commands,
        [
            "gui.mcp.native_config_list",
            "gui.mcp.native_config_review",
            "gui.mcp.native_config_review",
            "gui.mcp.native_config_apply",
            "gui.mcp.native_config_readback"
        ]
    );
    assert_eq!(
        json_file(&data.join("mcp-declaration-guards.json")),
        json!([])
    );
    let doc = json_file(&data.join(".mcp.json"));
    assert_eq!(doc["shared-extra"], "PRIVATE_MCP_SENTINEL");
    assert_eq!(
        doc["mcpServers"]["offline"]["headers"]["Authorization"],
        "PRIVATE_MCP_SENTINEL"
    );
}

fn settings_text(runtime: &mut Interactive, app: &mut Demo, text: &str) -> io::Result<()> {
    for c in text.chars() {
        settings_key(runtime, app, KeyCode::Char(c))?;
    }
    Ok(())
}

#[test]
#[ignore = "compiled Settings MCP add/remove review; dependency Python required"]
fn shared_mcp_declaration_add_and_remove_are_reviewed_written_and_read_back() {
    const TYPED: &str = "TYPED_MCP_SENTINEL";
    let (runtime, data) = launch("mcp_declaration_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Keep this composer draft.");
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let list = settings_key(runtime, &mut app, KeyCode::Char('m'))?;
            assert!(
                list.contains("A add") && list.contains("X remove"),
                "{list}"
            );
            let dot = data.join(".mcp.json");
            let before = json_file(&dot);

            // Add: form, masked value, source review, back to the form, save, reread.
            let form = settings_key(runtime, &mut app, KeyCode::Char('a'))?;
            assert!(form.contains("never connects or starts a server"), "{form}");
            settings_text(runtime, &mut app, "added")?;
            settings_key(runtime, &mut app, KeyCode::Tab)?;
            settings_key(runtime, &mut app, KeyCode::Tab)?;
            settings_text(runtime, &mut app, "npx")?;
            settings_key(runtime, &mut app, KeyCode::Tab)?;
            settings_text(runtime, &mut app, "-y pkg")?;
            settings_key(runtime, &mut app, KeyCode::Tab)?;
            settings_text(runtime, &mut app, &format!("TOKEN={TYPED}"))?;
            let typed = settings_key(runtime, &mut app, KeyCode::Tab)?;
            assert!(
                typed.contains("TOKEN=••••") && !typed.contains(TYPED),
                "{typed}"
            );
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            for needle in [
                "Add \"added\" to .mcp.json?",
                "target: npx",
                "environment names: TOKEN",
                "does not connect or start any server",
            ] {
                assert!(review.contains(needle), "{needle}\n{review}");
            }
            assert!(!review.contains(TYPED), "{review}");
            assert_eq!(json_file(&dot), before);
            let back = settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert!(
                back.contains("TOKEN=••••") && !back.contains(TYPED),
                "{back}"
            );
            assert_eq!(json_file(&dot), before);
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                saved.contains("Declaration added and verified on disk.")
                    && saved.contains("added")
                    && !saved.contains(TYPED),
                "{saved}"
            );
            let doc = json_file(&dot);
            assert_eq!(
                doc["mcpServers"]["added"],
                json!({"command":"npx","args":["-y","pkg"],"env":{"TOKEN":TYPED}})
            );
            assert_eq!(
                doc["mcpServers"]["offline"],
                before["mcpServers"]["offline"]
            );
            assert_eq!(doc["shared-extra"], before["shared-extra"]);

            // The shadowing file's declaration cannot be removed; the source says why.
            settings_key(runtime, &mut app, KeyCode::Up)?;
            settings_key(runtime, &mut app, KeyCode::Up)?;
            let refused = settings_key(runtime, &mut app, KeyCode::Char('x'))?;
            assert!(
                refused.contains("declared in mcp.json, which /mcp does not write"),
                "{refused}"
            );
            assert_eq!(json_file(&dot), doc);

            // Remove: select the new entry, review, save, reread.
            settings_key(runtime, &mut app, KeyCode::Down)?;
            settings_key(runtime, &mut app, KeyCode::Down)?;
            let review = settings_key(runtime, &mut app, KeyCode::Char('x'))?;
            assert!(
                review.contains("Remove \"added\" from .mcp.json?")
                    && review.contains("does not connect, start or stop any server"),
                "{review}"
            );
            assert_eq!(json_file(&dot), doc);
            let gone = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                gone.contains("Declaration removed and verified on disk.")
                    && !gone.contains("added   "),
                "{gone}"
            );
            assert_eq!(json_file(&dot), before);
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Keep this composer draft.");
            Ok(())
        })
        .unwrap_or_else(|error| panic!("{}: {error}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"].as_array().unwrap();
    let kinds = commands
        .iter()
        .filter_map(|c| c["type"].as_str())
        .filter(|s| s.starts_with("gui.mcp."))
        .collect::<Vec<_>>();
    assert_eq!(
        kinds,
        [
            "gui.mcp.native_config_list",
            "gui.mcp.native_config_add_review",
            "gui.mcp.native_config_add_review",
            "gui.mcp.native_config_apply",
            "gui.mcp.native_config_readback",
            "gui.mcp.native_config_remove_review",
            "gui.mcp.native_config_remove_review",
            "gui.mcp.native_config_apply",
            "gui.mcp.native_config_readback"
        ]
    );
    // The typed value crossed the wire only inside the two add reviews.
    let carrying = commands
        .iter()
        .filter(|c| c.to_string().contains(TYPED))
        .map(|c| c["type"].as_str().unwrap())
        .collect::<Vec<_>>();
    assert_eq!(
        carrying,
        [
            "gui.mcp.native_config_add_review",
            "gui.mcp.native_config_add_review"
        ]
    );
    assert!(report["requests"].as_array().unwrap().is_empty());
    assert_eq!(
        json_file(&data.join("mcp-declaration-guards.json")),
        json!([])
    );
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
        assert!(visible.contains("Glassbox") && visible.contains("127.0.0.1:7580"));
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
        .filter(|cmd| cmd["type"] == "gui.inspection.native_status_with_glassbox")
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
        let pixels=key(runtime,&mut app,KeyCode::Char('v'))?;
        assert!(pixels.contains("Image preview"));
        let mut terminal=Terminal::new(TestBackend::new(80,24)).unwrap();
        terminal.draw(|frame| ui::draw(frame,&mut app)).unwrap();
        let content=&terminal.backend().buffer().content;
        if crossterm::style::Colored::ansi_color_disabled_memoized() {
            assert!(content.iter().filter(|cell| cell.symbol()=="·").count()>100);
        } else {
            assert!(content.iter().any(|cell| cell.symbol()=="▀"
                && cell.fg==ratatui::style::Color::Rgb(0,0,128) && cell.bg==cell.fg));
        }
        assert!(key(runtime,&mut app,KeyCode::Enter)?.contains("Image preview"));
        assert!(app.live.as_ref().unwrap().work.image.attached.is_none());
        assert!(key(runtime,&mut app,KeyCode::Esc)?.contains("Clipboard DIB #201"));
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

#[test]
#[ignore = "private lifecycle configuration; real owned source never executes a hook or provider"]
fn hook_toggles_refuse_stale_review_and_verify_global_and_project_effects() {
    let (runtime, data) = launch("hook_toggle_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("keep this hook-edit draft");
        app.open_work();
        drain(runtime, &mut app)?;
        let mut context = key(runtime, &mut app, KeyCode::Char('i'))?;
        for _ in 0..litetui_terminal::context::Document::ALL.len() {
            if context.contains("Effective lifecycle hooks") { break; }
            context = key(runtime, &mut app, KeyCode::Right)?;
        }
        assert!(context.contains("Effective lifecycle hooks"), "{context}");
        let list = key(runtime, &mut app, KeyCode::Char('e'))?;
        assert!(list.contains("Lifecycle hooks · project") && list.contains("audit-file"), "{list}");
        let review = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(review.contains("Saved: false → true") && review.contains("Effective: disabled → enabled"), "{review}");
        assert!(review.contains("fixture-audit.exe") && review.contains("Future matching events may run"));
        assert!(!review.contains("PRIVATE_"));
        let mut small = Terminal::new(TestBackend::new(50, 16)).unwrap();
        small.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let work = &mut app.live.as_mut().unwrap().work;
        work.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        assert!(work.take_intent().is_none(), "unseen review pages must prevent Apply");
        let paths = json_file(&data.join("hook-inputs.json"));
        let global = PathBuf::from(paths[0].as_str().unwrap()).canonicalize()?;
        let project = PathBuf::from(paths[1].as_str().unwrap()).canonicalize()?;
        assert!(global.starts_with(data.canonicalize()?) && project.starts_with(data.canonicalize()?));
        let before_project = fs::read(&project)?;
        let mut changed_global = fs::read(&global)?;
        changed_global.push(b'\n');
        fs::write(&global, &changed_global)?;
        draw(&mut app);
        let work = &mut app.live.as_mut().unwrap().work;
        work.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        runtime.work(work.take_intent().unwrap())?;
        loop {
            let event = runtime.events.recv_timeout(Duration::from_secs(20)).map_err(io::Error::other)?;
            let refused = matches!(&event, Observation::Work(u) if matches!(&u.data, Data::Rejected(why) if why.contains("changed")));
            app.observe_live(event)?;
            if refused { break; }
        }
        assert_eq!(fs::read(&project)?, before_project);
        key(runtime, &mut app, KeyCode::F(5))?;
        key(runtime, &mut app, KeyCode::Enter)?;
        let saved = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(saved.contains("Verified audit-file · enabled · enabled"), "{saved}");
        assert_eq!(fs::read(&global)?, changed_global);
        key(runtime, &mut app, KeyCode::Tab)?;
        let shadow = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(shadow.contains("Effective: shadowed → shadowed"), "{shadow}");
        key(runtime, &mut app, KeyCode::Esc)?;
        assert_eq!(fs::read(&global)?, changed_global);
        key(runtime, &mut app, KeyCode::Enter)?;
        assert!(key(runtime, &mut app, KeyCode::Enter)?.contains("Verified audit-file · disabled · shadowed"));
        key(runtime, &mut app, KeyCode::Esc)?;
        let page = key(runtime, &mut app, KeyCode::F(5))?;
        assert!(page.contains("project/") && page.contains("enabled"), "{page}");
        assert_eq!(app.composer.text(), "keep this hook-edit draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        Ok(())
    });
    result.unwrap();
    let state = json_file(&data.join("hook-after.json"));
    assert_eq!(state["scopes"]["global"][0]["enabled"], false);
    assert_eq!(state["scopes"]["project"][0]["enabled"], true);
    assert_eq!(
        state["effective"][0],
        json!({"id":"audit-file","scope":"project","enabled":true})
    );
    assert_eq!(state["executed"], json!([]));
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"], json!([]));
    let replies = report["wire"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|v| {
            v["result"]["kind"]
                .as_str()
                .is_some_and(|kind| kind.starts_with("hook_"))
        })
        .collect::<Vec<_>>();
    assert!(
        !serde_json::to_string(&replies)
            .unwrap()
            .contains("PRIVATE_")
    );
    for kind in ["hook_apply", "hook_saved"] {
        assert_eq!(
            replies
                .iter()
                .filter(|v| v["result"]["kind"] == kind)
                .count(),
            2
        );
    }
}

#[test]
#[ignore = "owned Interactive source, isolated hook configuration and a disposable fixture executable"]
fn reviewed_hook_test_uses_canonical_authority_private_bounded_output_and_timeout() {
    let (runtime, data) = launch("hook_test_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("keep test draft");
        app.open_work();
        drain(runtime, &mut app)?;
        let mut context = key(runtime, &mut app, KeyCode::Char('i'))?;
        for _ in 0..litetui_terminal::context::Document::ALL.len() {
            if context.contains("Effective lifecycle hooks") {
                break;
            }
            context = key(runtime, &mut app, KeyCode::Right)?;
        }
        let list = key(runtime, &mut app, KeyCode::Char('e'))?;
        assert!(
            list.contains("authorized") && list.contains("requires-confirm"),
            "{list}"
        );
        let marker = data.join("test-workspace/executed.jsonl");
        let config = data.join("test-workspace/.litetui/hooks.json");
        let before = fs::read(&config)?;
        key(runtime, &mut app, KeyCode::Char(']'))?;
        let review = key(runtime, &mut app, KeyCode::Char('t'))?;
        assert!(
            review.contains("Test project hook authorized once")
                && review.contains("Event: prompt_before")
                && review.contains("Hook test")
                && review.contains("Profile: strict")
                && !review.contains("SYNTHETIC_"),
            "{review}"
        );
        key(runtime, &mut app, KeyCode::Esc)?;
        assert!(!marker.exists());
        key(runtime, &mut app, KeyCode::Char('t'))?;
        let allowed = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            allowed.contains("Test allowed")
                && allowed.contains("65536 B")
                && allowed.contains("clipped")
                && !allowed.contains("SYNTHETIC_"),
            "{allowed}"
        );
        let rows = fs::read_to_string(&marker)?;
        assert_eq!(rows.lines().count(), 1);
        let event: Value = serde_json::from_str(rows.trim()).unwrap();
        assert_eq!(event["event"], "prompt_before");
        assert_eq!(event["source"], "typed");
        assert_eq!(event["data"], json!({"text":"Hook test"}));
        key(runtime, &mut app, KeyCode::Down)?;
        key(runtime, &mut app, KeyCode::Char('t'))?;
        let refused = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            refused.contains("authorization or launch unavailable")
                && !refused.contains("SYNTHETIC_"),
            "{refused}"
        );
        assert_eq!(fs::read_to_string(&marker)?, rows);
        key(runtime, &mut app, KeyCode::Down)?;
        key(runtime, &mut app, KeyCode::Char('t'))?;
        let timed = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            timed.contains("Test timed out") && !timed.contains("unverified"),
            "{timed}"
        );
        assert_eq!(fs::read_to_string(&marker)?.lines().count(), 2);
        assert_eq!(fs::read(&config)?, before);
        assert_eq!(app.composer.text(), "keep test draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        key(runtime, &mut app, KeyCode::Down)?;
        key(runtime, &mut app, KeyCode::Char('t'))?;
        let work = &mut app.live.as_mut().unwrap().work;
        work.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        runtime.work(work.take_intent().unwrap())?;
        let deadline = Instant::now() + Duration::from_secs(15);
        while fs::read_to_string(&marker)?.lines().count() != 3 {
            assert!(
                Instant::now() < deadline,
                "The private quit fixture did not start"
            );
            std::thread::sleep(Duration::from_millis(10));
        }
        app.live.as_mut().unwrap().begin_quit();
        runtime.quit()?;
        Ok(())
    });
    result.unwrap();
    let exits = json_file(&data.join("tested-exits.json"));
    assert_eq!(exits.as_array().unwrap().len(), 3);
    assert!(exits.as_array().unwrap().iter().all(Value::is_i64));
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"].as_array().unwrap().len(), 0);
    let wire = report["wire"].as_array().unwrap();
    let receipts = wire
        .iter()
        .filter(|f| f["result"]["kind"] == "hook_test_result")
        .collect::<Vec<_>>();
    assert_eq!(receipts.len(), 4);
    assert_eq!(receipts.last().unwrap()["result"]["status"], "cancelled");
    assert!(
        receipts
            .iter()
            .all(|f| f["result"]["cleanup_verified"] == true)
    );
    assert!(!serde_json::to_string(wire).unwrap().contains("SYNTHETIC_"));
    assert!(!wire.iter().any(|f| f["type"] == "tool_approval_requested"));
    assert_eq!(report["shutdown"]["ok"], true);
}

#[test]
#[ignore = "real Interactive owned history with two private embedded images and no external image access"]
fn f6_saved_images_project_exact_sibling_pixels_refuse_stale_and_preserve_draft() {
    fn load(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
        while let Some(intent) = app.live.as_mut().unwrap().saved_history.take_intent() {
            runtime.history(intent)?;
            observe(runtime, app, |e| matches!(e, Observation::SavedHistory(_)))?;
        }
        Ok(())
    }
    fn press(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<String> {
        draw(app);
        app.live
            .as_mut()
            .unwrap()
            .saved_history
            .key(KeyEvent::new(code, KeyModifiers::NONE));
        load(runtime, app)?;
        Ok(draw(app))
    }
    fn pixel(app: &mut Demo) -> (String, ratatui::style::Color) {
        let mut terminal = Terminal::new(TestBackend::new(106, 32)).unwrap();
        terminal.draw(|frame| ui::draw(frame, app)).unwrap();
        let cell = &terminal.backend().buffer()[(53, 16)];
        (cell.symbol().to_owned(), cell.fg)
    }
    let (runtime, data) = launch("saved_image_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("keep saved image draft");
        app.open_history();
        load(runtime, &mut app)?;
        let page = draw(&mut app);
        assert!(
            page.contains("I image") || page.contains("I previews saved images"),
            "{page}"
        );
        assert!(!page.contains("SYNTHETIC_") && !page.contains("data:image"));
        let data_path = json_file(&data.join("saved-image-input.json"));
        let path = PathBuf::from(data_path["path"].as_str().unwrap()).canonicalize()?;
        assert!(path.starts_with(data.canonicalize()?));
        let before = fs::read(&path)?;
        let first = press(runtime, &mut app, KeyCode::Char('i'))?;
        assert!(first.contains("Saved image 1/2"), "{first}");
        let red = pixel(&mut app);
        let second = press(runtime, &mut app, KeyCode::Right)?;
        assert!(second.contains("Saved image 2/2"), "{second}");
        let green = pixel(&mut app);
        assert_ne!(red, green);
        if red.0 == "▀" {
            assert_eq!(red.1, ratatui::style::Color::Rgb(255, 0, 0));
            assert_eq!(green.1, ratatui::style::Color::Rgb(0, 255, 0));
        }
        assert!(press(runtime, &mut app, KeyCode::Left)?.contains("Saved image 1/2"));
        let returned = press(runtime, &mut app, KeyCode::Esc)?;
        assert!(
            returned.contains("Saved History") && !returned.contains("Saved image 1/2"),
            "{returned}"
        );
        assert_eq!(fs::read(&path)?, before);
        let mut changed = before.clone();
        changed.extend_from_slice(b"\n{\"type\":\"rename\",\"name\":\"changed externally\"}\n");
        fs::write(&path, &changed)?;
        let stale = press(runtime, &mut app, KeyCode::Char('i'))?;
        assert!(
            app.live.as_ref().unwrap().saved_history.is_stale(),
            "{stale}"
        );
        assert!(!stale.contains("Saved image 1/2"));
        press(runtime, &mut app, KeyCode::F(5))?;
        assert!(press(runtime, &mut app, KeyCode::Char('i'))?.contains("Saved image 1/2"));
        press(runtime, &mut app, KeyCode::Esc)?;
        press(runtime, &mut app, KeyCode::Esc)?;
        assert_eq!(app.composer.text(), "keep saved image draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        assert_eq!(fs::read(&path)?, changed);
        Ok(())
    });
    result.unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    let wire = report["wire"].as_array().unwrap();
    let previews = wire
        .iter()
        .filter(|e| e["result"]["kind"] == "history_image")
        .map(|e| &e["result"])
        .collect::<Vec<_>>();
    assert_eq!(previews.len(), 4);
    use base64::Engine as _;
    let rgba = |v: &Value| {
        base64::engine::general_purpose::STANDARD
            .decode(v["rgba"].as_str().unwrap())
            .unwrap()
    };
    assert_eq!(rgba(previews[0]), [255u8, 0, 0, 255].repeat(64 * 32));
    assert_eq!(rgba(previews[1]), [0u8, 255, 0, 255].repeat(64 * 32));
    assert_eq!(previews[0]["message_sha256"], previews[1]["message_sha256"]);
    assert_ne!(previews[0]["block_id"], previews[1]["block_id"]);
    assert_ne!(previews[0]["snapshot"], previews[3]["snapshot"]);
    assert_eq!(report["requests"].as_array().unwrap().len(), 0);
    assert_eq!(report["shutdown"]["ok"], true);
}

#[test]
#[ignore = "real leased topic writer through Interactive; requires LITETUI_AUTHORITY_PYTHON"]
fn nested_topics_browse_create_and_edit_the_current_agents_file() {
    let (runtime, data) = launch("context_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let seed = json_file(&data.join("context-seed.json"));
        let dir = PathBuf::from(seed["directory"].as_str().unwrap()).canonicalize()?;
        assert!(dir.starts_with(data.canonicalize()?));
        let target = dir.join("memories/project/nested/detail.md");
        app.insert_text("Keep ordinary draft");
        app.open_work();
        drain(runtime, &mut app)?;
        key(runtime, &mut app, KeyCode::Char('i'))?;
        let list = key(runtime, &mut app, KeyCode::Left)?;
        assert!(list.contains("Topics") && list.contains("n new"), "{list}");
        let new_path = key(runtime, &mut app, KeyCode::Char('n'))?;
        assert!(
            new_path.contains("Topic path beneath memories/"),
            "{new_path}"
        );
        let work = &mut app.live.as_mut().unwrap().work;
        assert!(work.accepts_paste());
        work.paste("project/nested/detail.md");
        let missing = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(missing.contains("absent"), "{missing}");
        key(runtime, &mut app, KeyCode::Char('e'))?;
        let draft = "Decision\r\n雪\n";
        app.live.as_mut().unwrap().work.paste(draft);
        let preview = key(runtime, &mut app, KeyCode::F(6))?;
        assert!(
            preview.contains("(absent)") && preview.contains("Decision"),
            "{preview}"
        );
        assert!(!target.parent().unwrap().exists());
        key(runtime, &mut app, KeyCode::Esc)?;
        assert!(!target.exists());
        key(runtime, &mut app, KeyCode::F(6))?;
        assert!(key(runtime, &mut app, KeyCode::F(6))?.contains("Saved and verified"));
        assert_eq!(fs::read(&target)?, draft.as_bytes());
        key(runtime, &mut app, KeyCode::Esc)?;
        let listed = key(runtime, &mut app, KeyCode::Esc)?;
        assert!(listed.contains("project/nested/detail.md"), "{listed}");
        let read = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(read.contains("Decision"), "{read}");
        key(runtime, &mut app, KeyCode::Char('e'))?;
        app.live.as_mut().unwrap().work.paste("more");
        key(runtime, &mut app, KeyCode::F(6))?;
        fs::write(&target, b"another author")?;
        assert!(
            key(runtime, &mut app, KeyCode::F(6))
                .unwrap_err()
                .to_string()
                .contains("changed since review")
        );
        assert_eq!(fs::read(&target)?, b"another author");
        assert!(key(runtime, &mut app, KeyCode::F(5))?.contains("Current file reloaded"));
        key(runtime, &mut app, KeyCode::F(6))?;
        assert!(key(runtime, &mut app, KeyCode::F(6))?.contains("Saved and verified"));
        let saved = fs::read_to_string(&target)?;
        assert!(
            saved.contains("more") && saved.contains(draft),
            "saved {saved:?}"
        );
        assert_eq!(app.composer.text(), "Keep ordinary draft");
        Ok(())
    });
    result.unwrap_or_else(|e| panic!("Nested topics failed in {}: {e}", data.display()));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"].as_array().unwrap();
    assert_eq!(
        commands
            .iter()
            .filter(|v| v["type"] == "gui.context.native_topic_apply")
            .count(),
        3
    );
    assert_eq!(
        commands
            .iter()
            .filter(|v| v["type"] == "gui.context.native_topic_readback")
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
#[ignore = "real owned source with private static schema and controlled provider"]
fn plugins_control_reviews_one_description_and_verifies_the_current_registry() {
    let (runtime, data) = launch("plugin_reload_runtime.py");
    let run = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let seed = json_file(&data.join("plugin-seed.json"));
        app.insert_text("keep chat draft");
        app.open_work();
        drain(runtime, &mut app)?;
        key(runtime, &mut app, KeyCode::Char('i'))?;
        let mut screen = String::new();
        for _ in 0..5 {
            screen = key(runtime, &mut app, KeyCode::Right)?;
        }
        assert!(screen.contains("Context · Plugins"), "{screen}");
        let list = key(runtime, &mut app, KeyCode::Char('r'))?;
        assert!(
            list.contains(seed["name"].as_str().unwrap()) && list.contains("Enter review"),
            "{list}"
        );
        let first = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(first.contains("Current description"), "{first}");
        key(runtime, &mut app, KeyCode::Esc)?;
        let mut review = key(runtime, &mut app, KeyCode::Enter)?;
        for _ in 0..64 {
            if review.contains("F6 reload this description") {
                break;
            }
            review = key(runtime, &mut app, KeyCode::PageDown)?;
        }
        assert!(
            review.contains("A reviewed replacement")
                && review.contains("F6 reload this description"),
            "{review}"
        );
        let done = key(runtime, &mut app, KeyCode::F(6))?;
        assert!(done.contains("Reloaded and verified"), "{done}");
        assert_eq!(app.composer.text(), "keep chat draft");
        Ok(())
    });
    run.unwrap_or_else(|e| panic!("Plugin process in {}: {e}", data.display()));
    let after = json_file(&data.join("plugin-after.json"));
    assert_eq!(after["generation_changed"], true);
    assert_eq!(after["same_handler"], true);
    assert_eq!(after["same_gate"], true);
    assert_eq!(after["same_policy"], true);
    assert_eq!(
        after["description"],
        json_file(&data.join("plugin-seed.json"))["after"]
    );
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"].as_array().unwrap();
    for op in ["gui.plugins.native_apply", "gui.plugins.native_readback"] {
        assert_eq!(commands.iter().filter(|v| v["type"] == op).count(), 1);
    }
    assert!(
        !commands
            .iter()
            .any(|v| v["type"] == "prompt" || v["type"] == "gui.tools.execute")
    );
}

#[test]
#[ignore = "real owned source with private skill library and verified cache"]
fn plugins_control_refreshes_the_live_skills_index_and_private_cache() {
    let (runtime, data) = launch("skills_index_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("keep skill refresh draft");
            app.open_work();
            drain(runtime, &mut app)?;
            key(runtime, &mut app, KeyCode::Char('i'))?;
            for _ in 0..5 {
                key(runtime, &mut app, KeyCode::Right)?;
            }
            let list = key(runtime, &mut app, KeyCode::Char('r'))?;
            assert!(list.contains("skills · refresh index and cache"), "{list}");
            let selected = key(runtime, &mut app, KeyCode::Down)?;
            assert!(selected.contains("› skills · refresh index"), "{selected}");
            let first = key(runtime, &mut app, KeyCode::Enter)?;
            assert!(first.contains("Current index"), "{first}");
            key(runtime, &mut app, KeyCode::Esc)?;
            let mut review = key(runtime, &mut app, KeyCode::Enter)?;
            for _ in 0..30 {
                if review.contains("F6 refresh this skills index") {
                    break;
                }
                review = key(runtime, &mut app, KeyCode::PageDown)?;
            }
            assert!(review.contains("F6 refresh this skills index"), "{review}");
            let done = key(runtime, &mut app, KeyCode::F(6))?;
            assert!(done.contains("verified the live skills index"), "{done}");
            assert_eq!(app.composer.text(), "keep skill refresh draft");
            Ok(())
        })
        .unwrap_or_else(|e| panic!("Skills refresh in {}: {e}", data.display()));
    let live = json_file(&data.join("skills-after.json"));
    assert_eq!(live, json_file(&data.join("skills/index.json"))["skills"]);
    assert_eq!(live[0]["description"], "Private refreshed metadata");
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"].as_array().unwrap();
    for op in ["gui.plugins.native_apply", "gui.plugins.native_readback"] {
        assert_eq!(
            commands
                .iter()
                .filter(|v| v["type"] == op && v["name"] == "skills")
                .count(),
            1
        );
    }
    assert!(
        !commands
            .iter()
            .any(|v| v["type"] == "prompt" || v["type"] == "gui.tools.execute")
    );
    assert!(report["denied"].as_array().unwrap().is_empty());
}

#[test]
#[ignore = "registered model preference operations and a controlled real source turn"]
fn model_preferences_review_reread_and_reach_the_normal_request_consumer() {
    let (runtime, data) = launch("model_preferences_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Keep this exact draft through preferences.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selection = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let opened = settings_key(runtime, &mut app, KeyCode::Char('p'))?;
            assert!(
                opened.contains("Model preferences for this conversation"),
                "{opened}"
            );
            assert!(opened.contains("Requested model: switch-old"), "{opened}");
            assert!(opened.contains("client declared"), "{opened}");
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            for c in "0.35".chars() {
                settings_key(runtime, &mut app, KeyCode::Char(c))?;
            }
            let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                review.contains("Save Temperature for switch-old?"),
                "{review}"
            );
            assert!(review.contains("Requested preference: 0.35"), "{review}");
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            for c in "0.35".chars() {
                settings_key(runtime, &mut app, KeyCode::Char(c))?;
            }
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                saved.contains("Saved and verified. Inference: next request."),
                "{saved}"
            );
            settings_key(runtime, &mut app, KeyCode::Down)?;
            settings_key(runtime, &mut app, KeyCode::Down)?;
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            for c in "321".chars() {
                settings_key(runtime, &mut app, KeyCode::Char(c))?;
            }
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(
                app.composer.text(),
                "Keep this exact draft through preferences."
            );
            assert_eq!(app.composer.before_cursor(), selection);
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
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    let requests = report["requests"].as_array().unwrap();
    assert_eq!(requests.len(), 1);
    assert_eq!(requests[0]["model"], "switch-old");
    assert_eq!(requests[0]["temperature"], json!(0.35));
    assert_eq!(requests[0]["top_p"], json!(0.9));
    assert_eq!(requests[0]["max_tokens"], 321);
    assert_eq!(
        requests[0]["messages"].as_array().unwrap().last().unwrap()["content"],
        "Keep this exact draft through preferences."
    );
    let initial = &report["initial_documents"];
    let final_docs = &report["final_documents"];
    assert_eq!(initial["global"], final_docs["global"]);
    assert_eq!(initial["agent"], final_docs["agent"]);
    let doc: Value =
        serde_json::from_str(final_docs["conversation"]["text"].as_str().unwrap()).unwrap();
    assert_eq!(
        doc["execution"]["model_infer_overrides"]["unselected-model"]["temperature"],
        json!(0.61)
    );
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| {
            c["type"]
                .as_str()?
                .strip_prefix("gui.models.native_preferences_")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        commands,
        [
            "snapshot", "review", "review", "apply", "readback", "review", "apply", "readback"
        ]
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
    eprintln!("Model preference process evidence: {}", data.display());
}

#[test]
#[ignore = "real private loopback Glassbox control, subscribers and joined Quit"]
fn glassbox_controls_review_bind_failure_stop_and_quit_join_exact_listener_workers() {
    let (runtime, data) = launch("glassbox_controls_runtime.py");
    let draft = "  Preserve Glassbox composer 雪  ";
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text(draft);
        app.composer.move_cursor(Motion::WordLeft, true);
        let caret = (
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        app.open_settings();
        settings_drain(runtime, &mut app)?;
        let screen = settings_key(runtime, &mut app, KeyCode::Char('g'))?;
        assert!(
            screen.contains("Event recording disabled") && screen.contains("Listener stopped"),
            "{screen}"
        );
        let review = settings_key(runtime, &mut app, KeyCode::Char('s'))?;
        assert!(review.contains("Start enables event recording"), "{review}");
        settings_key(runtime, &mut app, KeyCode::Esc)?;
        let fresh = settings_key(runtime, &mut app, KeyCode::Char('g'))?;
        assert!(
            fresh.contains("Event recording disabled") && fresh.contains("Listener stopped"),
            "{fresh}"
        );
        for expected in ["partial", "applied", "stopped", "applied"] {
            let action = if expected == "stopped" { 't' } else { 's' };
            let screen = settings_key(runtime, &mut app, KeyCode::Char(action))?;
            assert!(
                screen
                    .chars()
                    .filter(|c| !c.is_whitespace() && *c != '│')
                    .collect::<String>()
                    .contains("glassbox.jsonl")
                    && screen.contains(if action == 't' {
                        "Stop disables recording"
                    } else {
                        "Start enables event recording"
                    }),
                "{screen}"
            );
            let screen = settings_key(runtime, &mut app, KeyCode::Enter)?;
            match expected {
                "partial" => assert!(
                    screen.contains("listener did not reach")
                        && screen.contains("Event recording enabled"),
                    "{screen}"
                ),
                "stopped" => assert!(
                    screen.contains("Verified requested state.")
                        && screen.contains("Event recording disabled")
                        && screen.contains("Listener stopped"),
                    "{screen}"
                ),
                _ => assert!(
                    screen.contains("Verified requested state.")
                        && screen.contains("Listener running"),
                    "{screen}"
                ),
            }
            assert_eq!(app.composer.text(), draft);
            assert_eq!(
                caret,
                (
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned()
                )
            );
        }
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?} {}", data.display());
    let evidence = json_file(&data.join("glassbox-controls.json"));
    assert_eq!(evidence["exit_joined"], true);
    assert_eq!(evidence["recording"], false);
    assert!(evidence["threads"].as_array().unwrap().len() >= 4);
    assert!(
        evidence["threads"]
            .as_array()
            .unwrap()
            .iter()
            .all(|t| t["alive"] == false)
    );
    assert_eq!(
        evidence["file_preserved"]
            .as_str()
            .unwrap()
            .lines()
            .filter(|line| line.contains("private fixture event"))
            .count(),
        2
    );
    let requests = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(requests["requests"], json!([]));
    assert_eq!(requests["denied"], json!([]));
    let commands = requests["commands"].as_array().unwrap();
    for (operation, expected) in [
        ("gui.glassbox.native_review", 5),
        ("gui.glassbox.native_apply", 4),
        ("gui.glassbox.native_readback", 4),
    ] {
        assert_eq!(
            commands
                .iter()
                .filter(|command| command["type"] == operation)
                .count(),
            expected,
            "{operation}"
        );
    }
}

#[test]
#[ignore = "registered source reader, real connection workers and exact model lifecycle readback"]
fn model_lifecycle_reconnect_review_preserves_draft_and_owned_runtime() {
    let (runtime, data) = launch("model_lifecycle_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Retain this model lifecycle draft.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selection = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            let opened = settings_key(runtime, &mut app, KeyCode::Char('l'))?;
            assert!(opened.contains("Current model lifecycle"), "{opened}");
            assert!(opened.contains("http://127.0.0.1:45120/v1"), "{opened}");
            assert!(opened.contains("Reported loaded: unknown"), "{opened}");
            let refused = settings_key(runtime, &mut app, KeyCode::Char('l'))?;
            assert!(refused.contains("no standard model load"), "{refused}");
            let review = settings_key(runtime, &mut app, KeyCode::Char('r'))?;
            assert!(review.contains("Review reconnect"), "{review}");
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Char('r'))?;
            let settled = settings_key(runtime, &mut app, KeyCode::Enter)?;
            assert!(
                settled.contains("Operation and connection verified by independent readback."),
                "{settled}"
            );
            settings_key(runtime, &mut app, KeyCode::F(5))?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Retain this model lifecycle draft.");
            assert_eq!(app.composer.before_cursor(), selection);
            Ok(())
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["initial_documents"], report["final_documents"]);
    assert_eq!(report["initial_history"], report["history"]);
    assert_eq!(report["before"], report["after"]);
    assert!(report["requests"].as_array().unwrap().is_empty());
    assert!(report["denied"].as_array().unwrap().is_empty());
    let commands = report["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|c| {
            c["type"]
                .as_str()?
                .strip_prefix("gui.models.native_lifecycle_")
        })
        .collect::<Vec<_>>();
    assert_eq!(
        commands,
        [
            "snapshot", "review", "review", "apply", "readback", "snapshot"
        ]
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
    eprintln!("Model lifecycle process evidence: {}", data.display());
}

#[test]
#[ignore = "registered source Quit joins a controlled synchronous model metadata read"]
fn model_lifecycle_quit_joins_inflight_metadata_before_shutdown_receipt() {
    let (runtime, data) = launch("model_lifecycle_quit_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            runtime.settings(litetui_terminal::settings_view::Intent::ModelLifecycle(
                Box::new(litetui_terminal::model_lifecycle::Request::List),
            ))?;
            let deadline = Instant::now() + Duration::from_secs(15);
            while !data.join("stage-model-read-enter.json").is_file() {
                assert!(Instant::now() < deadline, "Metadata thread did not start");
                std::thread::sleep(Duration::from_millis(10));
            }
            app.live.as_mut().unwrap().begin_quit();
            runtime.quit()?;
            Ok(())
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    let events = report["events"].as_array().unwrap();
    let exited = events
        .iter()
        .position(|e| e["kind"] == "stage" && e["stage"] == "model-read-exit")
        .unwrap();
    let shutdown = events
        .iter()
        .position(|e| e["kind"] == "wire" && e["frame"] == report["shutdown"])
        .unwrap();
    assert!(exited < shutdown);
    assert_eq!(report["shutdown"]["ok"], true);
    assert_eq!(report["reader_retired"], true);
    assert!(
        report["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(report["requests"].as_array().unwrap().is_empty());
    eprintln!("Model lifecycle Quit evidence: {}", data.display());
}
#[test]
#[ignore = "registered source Quit joins a controlled synchronous model metadata read"]
fn model_lifecycle_quit_joins_canonical_connection_read_before_shutdown_receipt() {
    let (runtime, data) = launch("model_lifecycle_quit_connect_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('l'))?;
            settings_key(runtime, &mut app, KeyCode::Char('r'))?;
            let settings = &mut app.live.as_mut().unwrap().settings;
            settings.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            runtime.settings(settings.take_intent().unwrap())?;
            let deadline = Instant::now() + Duration::from_secs(15);
            while !data.join("stage-model-read-enter.json").is_file() {
                assert!(Instant::now() < deadline, "Metadata thread did not start");
                std::thread::sleep(Duration::from_millis(10));
            }
            app.live.as_mut().unwrap().begin_quit();
            runtime.quit()?;
            Ok(())
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    let events = report["events"].as_array().unwrap();
    let exited = events
        .iter()
        .position(|e| e["kind"] == "stage" && e["stage"] == "model-read-exit")
        .unwrap();
    let shutdown = events
        .iter()
        .position(|e| e["kind"] == "wire" && e["frame"] == report["shutdown"])
        .unwrap();
    assert!(exited < shutdown);
    assert_eq!(report["shutdown"]["ok"], true);
    assert_eq!(report["reader_retired"], true);
    assert!(
        report["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(report["requests"].as_array().unwrap().is_empty());
    eprintln!("Model lifecycle Quit evidence: {}", data.display());
}

#[test]
#[ignore = "registered Glassbox commands and F11 against private loopback source"]
fn registered_glassbox_commands_and_palette_require_fresh_visible_review() {
    use litetui_terminal::command_palette;
    fn commands(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
        while let Some(intent) = app.live.as_mut().unwrap().commands.take_intent() {
            runtime.commands(intent)?;
            observe(runtime, app, |e| matches!(e, Observation::Commands(_)))?;
        }
        Ok(())
    }
    fn close(app: &mut Demo) {
        for _ in 0..2 {
            draw(app);
            app.live
                .as_mut()
                .unwrap()
                .settings
                .key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        }
        assert!(!app.has_live_input());
    }
    let (runtime, data) = launch("glassbox_controls_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let draft = "  Original palette draft 雪  ";
        app.insert_text(draft);
        app.composer.move_cursor(Motion::WordLeft, true);
        let caret = (
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        app.open_commands(); // The same public entry used by F11.
        commands(runtime, &mut app)?;
        draw(&mut app);
        app.live.as_mut().unwrap().commands.paste("Glass box feed");
        draw(&mut app);
        app.live
            .as_mut()
            .unwrap()
            .commands
            .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
        commands(runtime, &mut app)?;
        settings_drain(runtime, &mut app)?;
        let screen = draw(&mut app);
        assert!(
            screen.contains("Event recording disabled") && screen.contains("Listener stopped"),
            "{screen}"
        );
        assert_eq!(app.composer.text(), draft);
        assert_eq!(
            caret,
            (
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            )
        );
        close(&mut app);
        for (raw, outcome) in [
            ("/glassbox", "inspect"),
            ("/glassbox STATUS", "inspect"),
            ("/glassbox start now", "reject"),
            ("/glassbox on", "decline"),
            ("/glassbox START", "partial"),
            ("/glassbox on", "running"),
            ("/glassbox off", "stopped"),
            ("/glassbox stop", "decline"),
            ("/glassbox start", "running"),
        ] {
            app.composer.clear();
            app.insert_text(raw);
            app.composer.move_cursor(Motion::WordLeft, true);
            let caret = (
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned(),
            );
            app.submit();
            commands(runtime, &mut app)?;
            if outcome == "reject" {
                assert!(draw(&mut app).contains("Glassbox accepts"));
                assert!(app.live.as_mut().unwrap().settings.take_intent().is_none());
                app.live
                    .as_mut()
                    .unwrap()
                    .commands
                    .key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
                continue;
            }
            // A queued Enter before the source review arrives cannot apply.
            app.live
                .as_mut()
                .unwrap()
                .settings
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            settings_drain(runtime, &mut app)?;
            app.live
                .as_mut()
                .unwrap()
                .settings
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            assert!(
                app.live.as_mut().unwrap().settings.take_intent().is_none(),
                "hidden review cannot apply"
            );
            let review = draw(&mut app);
            if outcome == "inspect" {
                assert!(
                    review.contains("Event recording disabled")
                        && review.contains("Listener stopped")
                );
            } else {
                assert!(
                    review.contains(if raw.contains("stop") || raw.contains("off") {
                        "Stop disables recording"
                    } else {
                        "Start enables event recording"
                    }),
                    "{review}"
                );
                let mut repeat = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
                repeat.kind = crossterm::event::KeyEventKind::Repeat;
                app.live.as_mut().unwrap().settings.key(repeat);
                assert!(app.live.as_mut().unwrap().settings.take_intent().is_none());
                if outcome != "decline" {
                    let verified = settings_key(runtime, &mut app, KeyCode::Enter)?;
                    assert!(
                        verified.contains(match outcome {
                            "partial" => "listener did not reach",
                            "stopped" => "Listener stopped",
                            _ => "Listener running",
                        }),
                        "{verified}"
                    );
                }
            }
            assert_eq!(app.composer.text(), raw);
            assert_eq!(
                caret,
                (
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned()
                )
            );
            close(&mut app);
        }
        // Retain the exact source catalog route in normal discovery.
        assert!(
            app.live
                .as_ref()
                .unwrap()
                .commands
                .catalog
                .as_ref()
                .unwrap()
                .entries
                .iter()
                .any(|row| row.owner == "glassbox"
                    && row.action == Some(command_palette::Action::Glassbox)
                    && row.tokens == ["glassbox", "/glassbox"])
        );
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?} {}", data.display());
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"], json!([]));
    assert_eq!(report["denied"], json!([]));
    let commands = report["commands"].as_array().unwrap();
    assert_eq!(
        commands
            .iter()
            .filter(|c| c["type"] == "gui.glassbox.native_apply")
            .count(),
        4
    );
    assert_eq!(
        commands
            .iter()
            .filter(|c| c["type"] == "gui.glassbox.native_readback")
            .count(),
        4
    );
    assert_eq!(
        commands
            .iter()
            .filter(|c| c["type"] == "gui.glassbox.native_review")
            .count(),
        6
    );
    let proof = json_file(&data.join("glassbox-controls.json"));
    assert_eq!(proof["exit_joined"], true);
    assert_eq!(proof["recording"], false);
    assert!(
        proof["threads"]
            .as_array()
            .unwrap()
            .iter()
            .all(|t| t["alive"] == false)
    );
    assert!(
        proof["results"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["status"] == "partial")
    );
    eprintln!("Glassbox registered command evidence: {}", data.display());
}

#[test]
#[ignore = "disposable source-owned MCP child; retained stop retry and joined native Quit"]
fn mcp_owned_shutdown_joins_retained_stdio_server_before_ack_and_lease_release() {
    let (runtime, data) = launch("mcp_shutdown_runtime.py");
    let draft = "Preserve MCP cleanup draft 雪";
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |event| {
            matches!(event, Observation::Ready)
        })?;
        app.insert_text(draft);
        app.open_settings();
        settings_drain(runtime, &mut app)?;
        let screen = settings_key(runtime, &mut app, KeyCode::Char('i'))?;
        assert!(
            screen.contains("private") && screen.contains("failed"),
            "{screen}"
        );
        assert_eq!(app.composer.text(), draft);
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?} {}", data.display());
    let proof = json_file(&data.join("mcp-shutdown.json"));
    for field in [
        "exit_joined",
        "lease_joined",
        "proc_exited",
        "reader_joined",
    ] {
        assert_eq!(proof[field], true, "{field}: {proof}");
    }
    assert_eq!(proof["spawns"], 1);
    assert_eq!(proof["stop_calls"], 2);
    assert_eq!(proof["mcp_enabled"], false);
    assert_eq!(proof["retained"]["servers"][0]["state"], "failed");
    let transport = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(transport["requests"], json!([]));
    assert_eq!(transport["denied"], json!([]));
    assert!(
        !transport["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|command| {
                command["type"]
                    .as_str()
                    .is_some_and(|name| name.starts_with("gui.mcp.") || name == "prompt")
            })
    );
    eprintln!("MCP owned shutdown evidence: {}", data.display());
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

#[test]
#[ignore = "registered command routes against the actual controlled source child"]
fn registered_command_controls_retain_arguments_and_do_not_apply_on_route() {
    let (runtime, data) = launch_fixture("model_lifecycle_runtime.py", true);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |event| {
                matches!(event, Observation::Ready)
            })?;
            for raw in ["/plan off", "/plan ON", "/plan"] {
                route_registered_command(runtime, &mut app, raw)?;
                while let Some(intent) = app.live.as_mut().unwrap().runtime_controls.take_intent() {
                    assert!(!matches!(
                        intent,
                        litetui_terminal::runtime_controls_view::Intent::Apply(_)
                    ));
                    if let litetui_terminal::runtime_controls_view::Intent::Review {
                        change, ..
                    } = &intent
                    {
                        assert_eq!(
                            *change,
                            litetui_terminal::runtime_controls::Change::Plan(raw != "/plan off")
                        );
                    }
                    runtime.runtime_controls(intent)?;
                    observe(runtime, &mut app, |event| {
                        matches!(event, Observation::RuntimeControls(_))
                    })?;
                }
                let screen = draw(&mut app);
                assert!(screen.contains("Apply"), "{screen}");
                app.live.as_mut().unwrap().runtime_controls.close();
            }
            for raw in ["/reconnect", "/load switch-old", "/unload switch-old"] {
                route_registered_command(runtime, &mut app, raw)?;
                settings_drain(runtime, &mut app)?;
                let screen = draw(&mut app);
                assert!(screen.contains("Current model lifecycle"), "{screen}");
                if raw == "/reconnect" {
                    assert!(screen.contains("Review reconnect"), "{screen}");
                }
                for _ in 0..3 {
                    settings_key(runtime, &mut app, KeyCode::Esc)?;
                }
                assert!(!app.has_live_input());
            }
            route_registered_command(runtime, &mut app, "/job")?;
            drain(runtime, &mut app)?;
            key(runtime, &mut app, KeyCode::Tab)?;
            let screen = key(runtime, &mut app, KeyCode::Tab)?;
            assert!(
                screen.contains("Cron schedule") && screen.contains("0 9 * * *"),
                "{screen}"
            );
            let screen = key(runtime, &mut app, KeyCode::Tab)?;
            assert!(
                screen.contains("Enabled") && screen.contains("true"),
                "{screen}"
            );
            app.live.as_mut().unwrap().work.close();
            route_registered_command(runtime, &mut app, "/engine stop")?;
            assert!(app.notice.contains("NInfer or Strata"));
            let hint = "Keep  these exact facts 雪 and trailing  ";
            route_registered_command(runtime, &mut app, &format!("/compact {hint}"))?;
            let intent = app
                .live
                .as_mut()
                .unwrap()
                .saved_history
                .take_intent()
                .expect("fresh saved history");
            runtime.history(intent)?;
            observe(runtime, &mut app, |event| {
                matches!(event, Observation::SavedHistory(_))
            })?;
            app.take_history_compact()?;
            assert!(app.has_live_compact());
            assert_eq!(app.live.as_ref().unwrap().compact.draft(), hint);
            let intent = app
                .live
                .as_mut()
                .unwrap()
                .compact
                .take_intent()
                .expect("fresh compact snapshot");
            runtime.compact(intent)?;
            observe(runtime, &mut app, |event| {
                matches!(event, Observation::Compact(_))
            })?;
            assert_eq!(app.live.as_ref().unwrap().compact.draft(), hint);
            app.live.as_mut().unwrap().compact.close();
            app.live.as_mut().unwrap().saved_history.close();
            runtime.quit()
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"], json!([]));
    assert_eq!(report["initial_documents"], report["final_documents"]);
    let commands = report["commands"].as_array().unwrap();
    assert!(!commands.iter().any(|c| {
        c["type"]
            .as_str()
            .is_some_and(|name| name.ends_with("apply") || name.ends_with("execute"))
    }));
    assert_eq!(report["shutdown"]["ok"], true);
}

#[test]
#[ignore = "registered hook and plugin commands against disposable source fixtures"]
fn registered_hooks_and_plugin_target_open_existing_review_controls() {
    for fixture in ["hook_toggle_runtime.py", "plugin_reload_runtime.py"] {
        let (runtime, data) = launch_fixture(fixture, true);
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live("custom".into(), "switch-old".into());
                observe(runtime, &mut app, |event| {
                    matches!(event, Observation::Ready)
                })?;
                if fixture == "hook_toggle_runtime.py" {
                    route_registered_command(runtime, &mut app, "/hooks")?;
                    drain(runtime, &mut app)?;
                    let screen = draw(&mut app);
                    assert!(screen.contains("audit-file"), "{screen}");
                } else {
                    let seed = json_file(&data.join("plugin-seed.json"));
                    let target = seed["name"].as_str().unwrap();
                    route_registered_command(
                        runtime,
                        &mut app,
                        &format!("/reload-plugins {target}"),
                    )?;
                    drain(runtime, &mut app)?;
                    let screen = key(runtime, &mut app, KeyCode::Enter)?;
                    assert!(screen.contains(target) && screen.contains("F6"), "{screen}");
                    app.live.as_mut().unwrap().work.close();
                    route_registered_command(
                        runtime,
                        &mut app,
                        "/reload-plugins nonexistent_tool",
                    )?;
                    drain(runtime, &mut app)?;
                    let screen = draw(&mut app);
                    assert!(
                        screen.contains("nonexistent_tool") && screen.contains("not eligible"),
                        "{screen}"
                    );
                    key(runtime, &mut app, KeyCode::Enter)?;
                    assert!(app.live.as_mut().unwrap().work.take_intent().is_none());
                }
                app.live.as_mut().unwrap().work.close();
                runtime.quit()
            })
            .unwrap();
        let report = json_file(&data.join("runtime-controls-report.json"));
        assert_eq!(report["requests"], json!([]));
        assert!(!report["commands"].as_array().unwrap().iter().any(|c| {
            c["type"]
                .as_str()
                .is_some_and(|name| name.ends_with("apply"))
        }));
        if fixture == "hook_toggle_runtime.py" {
            assert_eq!(
                json_file(&data.join("hook-after.json"))["executed"],
                json!([])
            );
        } else {
            assert_eq!(
                json_file(&data.join("plugin-after.json"))["generation_changed"],
                false
            );
        }
    }
}

#[test]
#[ignore = "registered native MCP lifecycle with private disposable stdio, host policy and joined Quit"]
fn mcp_runtime_review_readback_dispatch_retry_and_joined_quit() {
    use litetui_terminal::runtime_controls_view::Field;
    let (runtime, data) = launch("mcp_lifecycle_runtime.py");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let draft = "Keep MCP lifecycle draft 雪";
        app.insert_text(draft);
        app.composer.move_cursor(Motion::WordLeft, true);
        let cursor = app.composer.before_cursor().to_owned();
        app.open_settings();
        settings_drain(runtime, &mut app)?;
        settings_key(runtime, &mut app, KeyCode::Char('m'))?;
        let off = settings_key(runtime, &mut app, KeyCode::Tab)?;
        assert!(
            off.contains("MCP · Runtime") && off.contains("MCP false"),
            "{off}"
        );
        let review = settings_key(runtime, &mut app, KeyCode::Char('e'))?;
        assert!(
            review.contains("Review enable") && review.contains("Opens no server"),
            "{review}"
        );
        settings_key(runtime, &mut app, KeyCode::Esc)?;
        settings_key(runtime, &mut app, KeyCode::Char('e'))?;
        settings_key(runtime, &mut app, KeyCode::Enter)?;
        let review = settings_key(runtime, &mut app, KeyCode::Char('c'))?;
        assert!(
            review.contains("Review connect") && !review.contains("SECRET"),
            "{review}"
        );
        let connected = settings_key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            connected.contains("connected") && connected.contains("Host dispatch: 1"),
            "{connected}"
        );
        settings_key(runtime, &mut app, KeyCode::Esc)?;
        settings_key(runtime, &mut app, KeyCode::Esc)?;
        for field in [Field::Tools, Field::Profile, Field::Profile] {
            app.live.as_mut().unwrap().runtime_controls.open(field);
            while let Some(intent) = app.live.as_mut().unwrap().runtime_controls.take_intent() {
                runtime.runtime_controls(intent)?;
                observe(runtime, &mut app, |e| {
                    matches!(e, Observation::RuntimeControls(_))
                })?;
            }
            for _ in 0..4 {
                draw(&mut app);
                app.live
                    .as_mut()
                    .unwrap()
                    .runtime_controls
                    .key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            }
            draw(&mut app);
            app.live
                .as_mut()
                .unwrap()
                .runtime_controls
                .key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
            app.live
                .as_mut()
                .unwrap()
                .runtime_controls
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            let intent = app
                .live
                .as_mut()
                .unwrap()
                .runtime_controls
                .take_intent()
                .ok_or_else(|| io::Error::other("Runtime policy review did not reach Apply"))?;
            runtime.runtime_controls(intent)?;
            observe(runtime, &mut app, |e| {
                matches!(e, Observation::RuntimeControls(_))
            })?;
            assert!(draw(&mut app).contains("Applied and verified"));
            app.live.as_mut().unwrap().runtime_controls.close();
        }
        assert_eq!(app.composer.text(), draft);
        assert_eq!(app.composer.before_cursor(), cursor);
        app.submit();
        let prompt = app
            .live
            .as_mut()
            .unwrap()
            .take_submission()
            .expect("explicit private fixture prompt");
        runtime.submit(prompt)?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        app.insert_text(draft);
        app.composer.move_cursor(Motion::WordLeft, true);
        let tool = json_file(&data.join("private-tool-proof.json"));
        assert_eq!(tool["result"][1], true, "{tool}");
        assert!(
            tool["result"][0]
                .as_str()
                .unwrap()
                .contains("CONTROLLED:registered-proof")
        );
        assert_eq!(tool["generation"], 5);
        assert_eq!(tool["approvals"], json!(["mcp__private__private"]));
        app.open_settings();
        settings_drain(runtime, &mut app)?;
        settings_key(runtime, &mut app, KeyCode::Char('m'))?;
        settings_key(runtime, &mut app, KeyCode::Tab)?;
        for action in ['r', 'n', 'd', 'x', 'd', 'e', 'c'] {
            let review = settings_key(runtime, &mut app, KeyCode::Char(action))?;
            assert!(review.contains("Review"), "{action}: {review}");
            let settled = settings_key(runtime, &mut app, KeyCode::Enter)?;
            if action == 'x' {
                assert!(settled.contains("disconnected"), "{settled}");
            }
            if action == 'd' && settled.contains("MCP true") {
                assert!(settled.contains("failed"), "{settled}");
            }
        }
        assert_eq!(app.composer.text(), draft);
        assert_eq!(app.composer.before_cursor(), cursor);
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?} {}", data.display());
    let proof = json_file(&data.join("mcp-lifecycle.json"));
    for key in [
        "exit_joined",
        "lease_joined",
        "proc_exited",
        "reader_joined",
    ] {
        assert_eq!(proof[key], true, "{key}: {proof}");
    }
    assert_eq!(proof["spawns"], 3);
    assert_eq!(proof["final_generation"], 12);
    let operations = proof["operations"].as_array().unwrap();
    assert!(
        operations
            .iter()
            .any(|row| row["result"]["kind"] == "mcp_lifecycle_settled"
                && row["result"]["status"] == "failed")
    );
    let runtime = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(runtime["requests"].as_array().unwrap().len(), 2);
    assert!(
        runtime["requests"][0]["tools"]
            .as_array()
            .unwrap()
            .iter()
            .any(|tool| tool["function"]["name"] == "mcp__private__private")
    );
    assert_eq!(runtime["denied"], json!([]));
    assert_eq!(
        runtime["shutdown"]["result"]["runtime_controls_shutdown"]["published_generation"],
        12
    );
    assert_eq!(runtime["reader_retired"], true);
    eprintln!(
        "MCP lifecycle registered child evidence: {}",
        data.display()
    );
}

#[test]
#[ignore = "registered private MCP child, lost result/readback publication and joined Quit"]
fn mcp_runtime_lost_publication_holds_generation_and_joins_quit() {
    for phase in ["applied", "settled"] {
        let (runtime, data) = launch("mcp_lifecycle_runtime.py");
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Keep uncertain MCP draft");
            fs::write(data.join("mcp-fail-publication"), phase)?;
            app.open_settings();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('m'))?;
            settings_key(runtime, &mut app, KeyCode::Tab)?;
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            settings_key(runtime, &mut app, KeyCode::Enter)?;
            settings_key(runtime, &mut app, KeyCode::Char('c'))?;
            draw(&mut app);
            app.live
                .as_mut()
                .unwrap()
                .settings
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            let intent = app
                .live
                .as_mut()
                .unwrap()
                .settings
                .take_intent()
                .ok_or_else(|| io::Error::other("Missing exact MCP apply"))?;
            runtime.settings(intent)?;
            let deadline = Instant::now() + Duration::from_secs(12);
            while !data.join("mcp-publication-failed.json").exists() {
                if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(50)) {
                    app.observe_live(event)?;
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Controlled publication did not fail"));
                }
            }
            let held = json_file(&data.join("mcp-publication-failed.json"));
            if held["hold"] != true || held["generation"] != 1 {
                return Err(io::Error::other("Unpublished MCP policy was installed"));
            }
            if app.composer.text() != "Keep uncertain MCP draft" {
                return Err(io::Error::other("Uncertain lifecycle replaced composer"));
            }
            runtime.quit()
        });
        assert!(result.is_ok(), "{phase}: {result:?} {}", data.display());
        let proof = json_file(&data.join("mcp-lifecycle.json"));
        for key in [
            "exit_joined",
            "lease_joined",
            "proc_exited",
            "reader_joined",
            "failed_hold",
            "final_hold",
        ] {
            assert_eq!(proof[key], true, "{phase} {key}: {proof}");
        }
        assert_eq!(proof["publication_failed"], phase);
        assert_eq!(proof["spawns"], 1);
        assert_eq!(proof["failed_generation"], 1);
        assert_eq!(proof["final_generation"], 1);
        let transport = json_file(&data.join("runtime-controls-report.json"));
        assert_eq!(transport["requests"], json!([]));
        assert_eq!(
            transport["shutdown"]["result"]["runtime_controls_shutdown"]["published_generation"],
            1
        );
        assert_eq!(transport["reader_retired"], true);
        eprintln!("MCP lost {phase} publication evidence: {}", data.display());
    }
}

#[test]
#[ignore = "all model preference types through an actual registered child with controlled llama boundaries"]
fn full_model_preferences_keyboard_preserves_draft_pending_load_and_actual_request() {
    let (runtime, data) = launch("model_preferences_full_runtime.py");
    let values = [
        ("temperature", "0.42"),
        ("top_p", "0.81"),
        ("max_tokens", "423"),
        ("top_k", "41"),
        ("min_p", "0.06"),
        ("repeat_penalty", "1.12"),
        ("presence_penalty", "-0.4"),
        ("frequency_penalty", "0.25"),
        ("stop", "END,STOP"),
        ("reasoning_effort", "off"),
        ("enable_thinking", "false"),
        (
            "json_schema",
            r#"{"type":"object","properties":{"ok":{"type":"boolean"}}}"#,
        ),
        ("ctx", "4096"),
        ("ngl", "-1"),
        ("threads", "8"),
        ("batch", "512"),
        ("ubatch", "128"),
        ("parallel", "2"),
        ("flash_attn", "auto"),
        ("cache_k", "q8_0"),
        ("cache_v", "f16"),
        ("kv_offload", "false"),
        ("kv_unified", "true"),
        ("mlock", "false"),
        ("mmap", "true"),
        ("context_shift", "false"),
        ("seed", "-1"),
        ("rope_base", "10000"),
        ("rope_scale", "1.5"),
        ("spec_type", "none"),
        ("draft_model", "later-draft.gguf"),
        ("draft_max", "4"),
        ("draft_min", "0"),
        ("draft_p_min", "0.4"),
        ("mmproj", "none"),
        ("chat_template_file", "template.jinja"),
    ];
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("llamacpp".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve exact model preference draft \u{96ea}.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selected = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('p'))?;
            for (index, (field, text)) in values.iter().enumerate() {
                settings_key(runtime, &mut app, KeyCode::Char('e'))?;
                for c in text.chars() {
                    settings_key(runtime, &mut app, KeyCode::Char(c))?;
                }
                let review = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(review.contains("Enter save"), "{field}: {review}");
                if index >= 12 {
                    assert!(
                        review.contains("Running settings remain unchanged"),
                        "{field}: {review}"
                    );
                }
                let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(saved.contains("Saved and verified."), "{field}: {saved}");
                if index + 1 < values.len() {
                    settings_key(runtime, &mut app, KeyCode::Down)?;
                }
            }
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(
                app.composer.text(),
                "Preserve exact model preference draft \u{96ea}."
            );
            assert_eq!(app.composer.before_cursor(), selected);
            app.submit();
            runtime.submit(
                app.live
                    .as_mut()
                    .unwrap()
                    .take_submission()
                    .expect("explicit turn"),
            )?;
            observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            Ok(())
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    let commands = report["commands"].as_array().unwrap();
    let reviews = commands
        .iter()
        .filter(|c| c["type"] == "gui.models.native_preferences_review")
        .collect::<Vec<_>>();
    assert_eq!(reviews.len(), 36);
    for (review, (field, _)) in reviews.iter().zip(values) {
        assert_eq!(review["field"], field);
    }
    let reads = report["wire"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|row| {
            (row["result"]["kind"] == "model_preferences_saved")
                .then_some(&row["result"]["preferences"])
        })
        .collect::<Vec<_>>();
    assert_eq!(reads.len(), 36);
    let final_state = reads.last().unwrap();
    assert_eq!(
        final_state["raw"]["load"]["active"],
        json!({"future-load":{"keep":true}})
    );
    assert_eq!(final_state["raw"]["load"]["saved"]["ctx"], 4096);
    assert_eq!(final_state["raw"]["load"]["saved"]["mmproj"], "none");
    assert_eq!(final_state["reported_loaded"], true);
    assert_eq!(final_state["context_length"], 8192);
    let requests = report["requests"].as_array().unwrap();
    assert_eq!(requests.len(), 1);
    let request = &requests[0];
    assert_eq!(request["temperature"], json!(0.42));
    assert_eq!(request["max_tokens"], 423);
    assert_eq!(request["stop"], json!(["END", "STOP"]));
    assert_eq!(request["extra_body"]["top_k"], 41);
    assert_eq!(request["extra_body"]["reasoning_effort"], "none");
    assert_eq!(
        request["extra_body"]["chat_template_kwargs"]["enable_thinking"],
        false
    );
    assert_eq!(
        request["response_format"]["json_schema"]["schema"]["type"],
        "object"
    );
    assert_eq!(
        report["initial_documents"]["global"],
        report["final_documents"]["global"]
    );
    assert_eq!(
        report["initial_documents"]["agent"],
        report["final_documents"]["agent"]
    );
    let document: Value = serde_json::from_str(
        report["final_documents"]["conversation"]["text"]
            .as_str()
            .unwrap(),
    )
    .unwrap();
    assert_eq!(
        document["execution"]["llama_load_settings"]["unselected"]["ctx"],
        8192
    );
    assert_eq!(
        document["execution"]["model_infer_overrides"]["unselected"]["top_k"],
        99
    );
    assert_eq!(report["reader_retired"], true);
    assert!(
        report["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(report["denied"].as_array().unwrap().is_empty());
    assert_eq!(
        report["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
    eprintln!("Full model preferences child evidence: {}", data.display());
}

fn effort_submit(runtime: &mut Interactive, app: &mut Demo, text: &str) -> io::Result<()> {
    app.live.as_mut().unwrap().submit(text)?;
    runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
    observe(runtime, app, |event| matches!(event, Observation::Complete))
}

fn effort_switch(runtime: &mut Interactive, app: &mut Demo, model: &str) -> io::Result<()> {
    app.open_backend();
    runtime.backend(
        app.live
            .as_mut()
            .unwrap()
            .backend_controls
            .take_intent()
            .unwrap(),
    )?;
    observe(runtime, app, |event| {
        matches!(
            event,
            Observation::Backend(litetui_terminal::backend_controls::Update::Catalog(_))
        )
    })?;
    draw(app);
    let view = &mut app.live.as_mut().unwrap().backend_controls;
    let index = view
        .catalog
        .as_ref()
        .unwrap()
        .entries
        .iter()
        .position(|row| row.name == "llamacpp")
        .unwrap();
    view.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
    for _ in 0..index {
        view.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    }
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    view.paste(model);
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    draw(app);
    let view = &mut app.live.as_mut().unwrap().backend_controls;
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    runtime.backend(view.take_intent().expect("drawn exact model selection"))?;
    observe(runtime, app, |event| {
        matches!(event, Observation::BackendChanged(_))
    })
}

#[test]
#[ignore = "real unpinned CLI effort settings, model switching and fresh-process resume"]
fn model_effort_modes_reach_requests_and_survive_unpinned_reopen() {
    for mode in ["value", "inherit", "server"] {
        let (runtime, data) = launch("model_preferences_effort_runtime.py");
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live("llamacpp".into(), "switch-old".into());
                observe(runtime, &mut app, |event| {
                    matches!(event, Observation::Ready)
                })?;
                app.live.as_mut().unwrap().settings.open();
                settings_drain(runtime, &mut app)?;
                settings_key(runtime, &mut app, KeyCode::Char('p'))?;
                for _ in 0..9 {
                    settings_key(runtime, &mut app, KeyCode::Down)?;
                }
                settings_key(runtime, &mut app, KeyCode::Char('e'))?;
                for c in "off".chars() {
                    settings_key(runtime, &mut app, KeyCode::Char(c))?;
                }
                settings_key(runtime, &mut app, KeyCode::Enter)?;
                let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(saved.contains("Saved and verified."), "{saved}");
                if mode != "value" {
                    settings_key(
                        runtime,
                        &mut app,
                        KeyCode::Char(if mode == "inherit" { 'i' } else { 'd' }),
                    )?;
                    settings_key(runtime, &mut app, KeyCode::Enter)?;
                    let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
                    assert!(saved.contains("Saved and verified."), "{mode}: {saved}");
                }
                settings_key(runtime, &mut app, KeyCode::Esc)?;
                settings_key(runtime, &mut app, KeyCode::Esc)?;
                effort_submit(
                    runtime,
                    &mut app,
                    "Explicit request after effort preference",
                )?;
                effort_switch(runtime, &mut app, "switch-new")?;
                effort_submit(runtime, &mut app, "Explicit request on model B")?;
                effort_switch(runtime, &mut app, "switch-old")?;
                effort_submit(runtime, &mut app, "Explicit request back on model A")?;
                Ok(())
            })
            .unwrap();
        let report = json_file(&data.join("runtime-controls-report.json"));
        assert_eq!(report["effort_launch_unpinned"], true);
        assert_eq!(report["effort_reopened"], false);
        assert_eq!(report["before"]["authority"]["thinking_level"], "medium");
        assert_eq!(report["after"]["authority"]["thinking_level"], "medium");
        let requests = report["requests"].as_array().unwrap();
        assert_eq!(requests.len(), 3);
        let expected = match mode {
            "value" => json!("none"),
            "inherit" => json!("medium"),
            _ => Value::Null,
        };
        assert_eq!(
            requests[0]["extra_body"]["reasoning_effort"], expected,
            "{mode}"
        );
        assert_eq!(requests[1]["model"], "switch-new");
        assert_eq!(requests[1]["extra_body"]["reasoning_effort"], "medium");
        assert_eq!(requests[2]["model"], "switch-old");
        assert_eq!(
            requests[2]["extra_body"]["reasoning_effort"], expected,
            "{mode}"
        );
        assert_eq!(
            report["initial_documents"]["global"],
            report["final_documents"]["global"]
        );
        assert_eq!(report["denied"], json!([]));
        let session = report["after"]["identity"]["session_id"].as_str().unwrap();
        let (reopened, same_data) = launch_fixture_resume(
            "model_preferences_effort_runtime.py",
            false,
            Some((&data, session)),
        );
        assert_eq!(same_data, data);
        reopened
            .with_cleanup(|runtime| {
                let mut app = Demo::live("llamacpp".into(), "switch-old".into());
                observe(runtime, &mut app, |event| {
                    matches!(event, Observation::Ready)
                })?;
                effort_submit(runtime, &mut app, "Explicit request after process reopen")?;
                Ok(())
            })
            .unwrap();
        let reopened = json_file(&data.join("runtime-controls-report.json"));
        assert_eq!(reopened["effort_launch_unpinned"], true);
        assert_eq!(reopened["effort_reopened"], true);
        assert_eq!(reopened["after"]["identity"]["session_id"], session);
        assert_eq!(reopened["after"]["authority"]["thinking_level"], "medium");
        assert_eq!(reopened["requests"].as_array().unwrap().len(), 1);
        assert_eq!(
            reopened["requests"][0]["extra_body"]["reasoning_effort"], expected,
            "{mode}"
        );
        assert_eq!(reopened["denied"], json!([]));
        assert_eq!(reopened["reader_retired"], true);
        assert_eq!(
            reopened["shutdown"]["result"]["work_shutdown"]["worker_settled"],
            true
        );
        assert!(
            reopened["launch_arguments"]
                .as_array()
                .unwrap()
                .iter()
                .all(|arg| arg != "--thinking-level" && arg != "--reasoning-effort")
        );
        eprintln!(
            "Unpinned effort {mode} process evidence: {}",
            data.display()
        );
    }
}

#[test]
#[ignore = "compiled Voice catalog selection and native shortcut capture with private inventories"]
fn voice_catalog_names_and_shortcut_capture_require_review_without_audio() {
    let (runtime, data) = launch("voice_catalog_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve composer and selection.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selected = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('v'))?;
            for _ in 0..4 {
                settings_key(runtime, &mut app, KeyCode::Down)?;
            }
            let path = data.join("settings.json");
            let mut expected = json_file(&path);
            for (index, field, value) in [
                (4, "stt_mic", "Microphone Δ"),
                (5, "stt_hotkey", "ctrl+alt+r"),
                (6, "tts_voice", "SAPI Ω"),
            ] {
                if index > 4 {
                    settings_key(runtime, &mut app, KeyCode::Down)?;
                }
                if index == 5 {
                    settings_key(runtime, &mut app, KeyCode::Char('k'))?;
                    app.live.as_mut().unwrap().settings.key(KeyEvent::new(
                        KeyCode::Char('r'),
                        KeyModifiers::CONTROL | KeyModifiers::ALT,
                    ));
                    assert!(draw(&mut app).contains("Captured into draft"));
                } else {
                    let opened = settings_key(runtime, &mut app, KeyCode::Char('c'))?;
                    assert!(opened.contains("Discover"), "{opened}");
                    let deadline = Instant::now() + Duration::from_secs(5);
                    loop {
                        let shown = settings_key(runtime, &mut app, KeyCode::F(5))?;
                        if shown.contains("Status: ready") {
                            break;
                        }
                        assert!(Instant::now() < deadline, "{shown}");
                        std::thread::sleep(Duration::from_millis(20));
                    }
                    settings_key(runtime, &mut app, KeyCode::Down)?;
                    let draft = settings_key(runtime, &mut app, KeyCode::Enter)?;
                    assert!(draft.contains("Selected name is a draft"), "{draft}");
                }
                assert_eq!(json_file(&path), expected);
                let reviewed = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(reviewed.contains("Save "), "{reviewed}");
                assert_eq!(json_file(&path), expected);
                let saved = settings_key(runtime, &mut app, KeyCode::Enter)?;
                assert!(saved.contains("Saved and verified on disk"), "{saved}");
                expected[field] = json!(value);
                assert_eq!(json_file(&path), expected);
            }
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve composer and selection.");
            assert_eq!(app.composer.before_cursor(), selected);
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    assert_eq!(json_file(&data.join("voice-guards.json")), json!([]));
    assert!(
        json_file(&data.join("runtime-controls-report.json"))["requests"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}

#[test]
#[ignore = "compiled Voice action review/apply with contained fake installers and audio"]
fn voice_actions_require_visible_apply_and_verified_result_without_real_effects() {
    let (runtime, data) = launch("voice_actions_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve action composer selection.");
            app.composer.move_cursor(Motion::WordLeft, true);
            let selected = app.composer.before_cursor().to_owned();
            app.live.as_mut().unwrap().settings.open();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('v'))?;
            let before = fs::read(data.join("settings.json"))?;
            let effects = data.join("voice-action-effects.json");
            let mut expected = Vec::<&str>::new();
            for (key, kind) in [('t', "fallback"), ('i', "edge"), ('w', "stt")] {
                if kind == "stt" {
                    settings_key(runtime, &mut app, KeyCode::Right)?;
                }
                settings_key(runtime, &mut app, KeyCode::Char(key))?;
                let deadline = Instant::now() + Duration::from_secs(5);
                loop {
                    let shown = settings_key(runtime, &mut app, KeyCode::F(5))?;
                    if shown.contains("Status: review") {
                        assert!(shown.contains("Enter Apply"), "{shown}");
                        if kind == "stt" {
                            assert!(shown.contains("small.en"), "{shown}");
                        }
                        break;
                    }
                    assert!(Instant::now() < deadline, "{shown}");
                    std::thread::sleep(Duration::from_millis(20));
                }
                assert_eq!(
                    if effects.exists() {
                        json_file(&effects)
                    } else {
                        json!([])
                    },
                    json!(expected)
                );
                assert_eq!(fs::read(data.join("settings.json"))?, before);
                settings_key(runtime, &mut app, KeyCode::Enter)?;
                let deadline = Instant::now() + Duration::from_secs(5);
                loop {
                    let shown = settings_key(runtime, &mut app, KeyCode::F(5))?;
                    if shown.contains("Status: verified") {
                        break;
                    }
                    assert!(Instant::now() < deadline, "{shown}");
                    std::thread::sleep(Duration::from_millis(20));
                }
                expected.push(kind);
                assert_eq!(json_file(&effects), json!(expected));
                let closed = settings_key(runtime, &mut app, KeyCode::Esc)?;
                assert!(
                    closed.contains("Voice preferences on this device"),
                    "{closed}"
                );
            }
            assert_eq!(fs::read(data.join("settings.json"))?, before);
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve action composer selection.");
            assert_eq!(app.composer.before_cursor(), selected);
            Ok(())
        })
        .unwrap_or_else(|err| panic!("{}: {err}", data.display()));
    assert_eq!(json_file(&data.join("voice-guards.json")), json!([]));
    assert!(
        json_file(&data.join("runtime-controls-report.json"))["requests"]
            .as_array()
            .unwrap()
            .is_empty()
    );
}
