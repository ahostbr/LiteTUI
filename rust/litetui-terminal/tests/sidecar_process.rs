#![cfg(windows)]
//! Optional Sidecar through normal Interactive transport and a real controlled stdio child.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
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
        .join("target/sidecar-process-tests")
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
        if matches!(
            fixture_file,
            "model_preferences_effort_runtime.py" | "model_settings_runtime.py"
        ) {
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
            "model_preferences_full_runtime.py"
                | "model_preferences_effort_runtime.py"
                | "model_settings_runtime.py"
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
        "model_preferences_full_runtime.py"
            | "model_preferences_effort_runtime.py"
            | "model_settings_runtime.py"
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

fn reviewed_enter(runtime: &mut Interactive, app: &mut Demo) -> io::Result<String> {
    for _ in 0..256 {
        let page = draw(app);
        let Some(tail) = page.split("Review page ").nth(1) else {
            return settings_key(runtime, app, KeyCode::Enter);
        };
        let count = tail.split_whitespace().next().expect("review page count");
        let (current, total) = count.split_once('/').expect("current/total pages");
        let (current, total): (usize, usize) = (current.parse().unwrap(), total.parse().unwrap());
        if current == total {
            return settings_key(runtime, app, KeyCode::Enter);
        }
        settings_key(runtime, app, KeyCode::PageDown)?;
    }
    Err(io::Error::other(
        "Sidecar review exceeded bounded visible pages",
    ))
}

fn wait_file(path: &Path) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(20);
    while !path.exists() {
        if Instant::now() > deadline {
            return Err(io::Error::other(format!(
                "Missing fixture evidence {}",
                path.display()
            )));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
    Ok(())
}
fn replies(data: &Path) -> Vec<Value> {
    fs::read_to_string(data.join("sidecar-final-replies.jsonl"))
        .unwrap_or_default()
        .lines()
        .map(|row| serde_json::from_str(row).unwrap())
        .collect()
}
fn review_save(runtime: &mut Interactive, app: &mut Demo, field: &str) -> io::Result<()> {
    let reviewed = settings_key(runtime, app, KeyCode::Char('p'))?;
    assert!(reviewed.contains(field), "{reviewed}");
    assert!(
        reviewed.contains("Destination:") || field == "job",
        "{reviewed}"
    );
    let saved = reviewed_enter(runtime, app)?;
    assert!(
        !saved.contains("response changed") && !saved.contains("refused"),
        "{saved}"
    );
    Ok(())
}

#[test]
#[ignore = "compiled Interactive and actual controlled Python/stdio child; requires LITETUI_AUTHORITY_PYTHON"]
fn sidecar_review_each_destination_then_reply_once_and_submit_with_updated_policy() {
    let (runtime, data) = launch("sidecar_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |event| {
                matches!(event, Observation::Ready)
            })?;
            app.insert_text("Preserve draft through Sidecar");
            app.open_settings();
            settings_drain(runtime, &mut app)?;
            let page = settings_key(runtime, &mut app, KeyCode::Char('s'))?;
            assert!(
                page.contains("Optional Sidecar") && page.contains("Enabled: false"),
                "{page}"
            );
            let enable = settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            assert!(
                enable.contains("sidecar_enabled") && enable.contains("Destination:"),
                "{enable}"
            );
            reviewed_enter(runtime, &mut app)?;
            assert!(json_file(&data.join("settings.json"))["sidecar_enabled"] == true);
            assert!(!data.join("sidecar-delivered.json").exists());
            reviewed_enter(runtime, &mut app)?;
            reviewed_enter(runtime, &mut app)?;
            wait_file(&data.join("sidecar-delivered.json"))?;
            let seed = json_file(&data.join("sidecar-seed.json"));
            let change = json!({"show_stop_time": !seed["show_stop_time"].as_bool().unwrap(),
            "tool_iterations": 29, "temperature": 0.23, "tool_policy_profile": "interactive"});
            fs::write(
                data.join("sidecar-propose-settings.json"),
                serde_json::to_vec(&change)?,
            )?;
            wait_file(&data.join("sidecar-sent-settings"))?;
            let pending = settings_key(runtime, &mut app, KeyCode::Char('r'))?;
            assert!(pending.contains("show_stop_time"), "{pending}");
            assert!(replies(&data).is_empty());
            for field in [
                "show_stop_time",
                "temperature",
                "tool_iterations",
                "tool_policy_profile",
            ] {
                review_save(runtime, &mut app, field)?;
                if field != "tool_policy_profile" {
                    assert!(replies(&data).is_empty());
                }
            }
            wait_file(&data.join("sidecar-final-replies.jsonl"))?;
            let reply = replies(&data);
            assert_eq!(reply.len(), 1);
            assert_eq!(reply[0]["id"], 700);
            assert_eq!(reply[0]["payload"]["saved"], true);
            assert_eq!(reply[0]["payload"]["verified"].as_array().unwrap().len(), 4);
            fs::write(
                data.join("sidecar-propose-job.json"),
                serde_json::to_vec(&json!({
            "prompt": "Controlled scheduled proposal only", "schedule": "0 0 * * *",
            "label": "Sidecar fixture", "tool_profile": "strict"}))?,
            )?;
            wait_file(&data.join("sidecar-sent-job"))?;
            settings_key(runtime, &mut app, KeyCode::Char('r'))?;
            review_save(runtime, &mut app, "job")?;
            let deadline = Instant::now() + Duration::from_secs(10);
            while replies(&data).len() < 2 && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
            let all = replies(&data);
            assert_eq!(all.len(), 2);
            assert_eq!(all[1]["id"], 701);
            assert_eq!(all[1]["payload"]["saved"], true);
            assert_eq!(
                all[1]["payload"]["job"]["prompt"],
                "Controlled scheduled proposal only"
            );
            let close = settings_key(runtime, &mut app, KeyCode::Char('c'))?;
            assert!(close.contains("Review close"), "{close}");
            reviewed_enter(runtime, &mut app)?;
            // Reopen and refresh the same optional process owner after exact retirement.
            reviewed_enter(runtime, &mut app)?;
            reviewed_enter(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('f'))?;
            reviewed_enter(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('c'))?;
            reviewed_enter(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            settings_key(runtime, &mut app, KeyCode::Esc)?;
            assert_eq!(app.composer.text(), "Preserve draft through Sidecar");
            app.submit();
            runtime.submit(
                app.live
                    .as_mut()
                    .unwrap()
                    .take_submission()
                    .expect("explicit subsequent request"),
            )?;
            observe(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            Ok(())
        })
        .unwrap();
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"].as_array().unwrap().len(), 1);
    assert_eq!(report["requests"][0]["temperature"], 0.23);
    let request = report["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["kind"] == "provider-request")
        .unwrap();
    assert_eq!(request["profile"], "interactive");
    assert_eq!(report["denied"], json!([]));
    assert!(
        json_file(&data.join("sidecar-retired.json"))
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["retained"] == false && r["reader_alive"] == false)
    );
    eprintln!("Sidecar compiled child evidence: {}", data.display());
}

#[test]
#[ignore = "compiled Interactive and actual controlled Python/stdio child; requires LITETUI_AUTHORITY_PYTHON"]
fn sidecar_partial_batch_discard_retains_only_published_group() {
    let (runtime, data) = launch("sidecar_runtime.py");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.open_settings();
            settings_drain(runtime, &mut app)?;
            settings_key(runtime, &mut app, KeyCode::Char('s'))?;
            settings_key(runtime, &mut app, KeyCode::Char('e'))?;
            reviewed_enter(runtime, &mut app)?;
            reviewed_enter(runtime, &mut app)?;
            reviewed_enter(runtime, &mut app)?;
            let seed = json_file(&data.join("sidecar-seed.json"));
            fs::write(
                data.join("sidecar-propose-settings.json"),
                serde_json::to_vec(&json!({
            "show_stop_time":!seed["show_stop_time"].as_bool().unwrap(),"tool_iterations":29}))?,
            )?;
            wait_file(&data.join("sidecar-sent-settings"))?;
            settings_key(runtime, &mut app, KeyCode::Char('r'))?;
            review_save(runtime, &mut app, "show_stop_time")?;
            assert!(replies(&data).is_empty());
            settings_key(runtime, &mut app, KeyCode::Char('d'))?;
            wait_file(&data.join("sidecar-final-replies.jsonl"))?;
            let reply = replies(&data);
            assert_eq!(reply.len(), 1);
            assert_eq!(reply[0]["payload"]["saved"], false);
            assert_eq!(reply[0]["payload"]["verified"].as_array().unwrap().len(), 1);
            assert_eq!(
                reply[0]["payload"]["snapshot"]["fields"]["tool_iterations"]["effective"],
                seed["tool_iterations"]
            );
            Ok(())
        })
        .unwrap();
    assert!(
        json_file(&data.join("sidecar-retired.json"))
            .as_array()
            .unwrap()
            .iter()
            .all(|r| r["retained"] == false && r["reader_alive"] == false)
    );
    eprintln!("Sidecar partial-batch child evidence: {}", data.display());
}
