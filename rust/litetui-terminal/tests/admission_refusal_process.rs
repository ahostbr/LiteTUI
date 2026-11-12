#![cfg(windows)]
//! Actual Interactive refusal and recovery with a private controlled Codex transport.
use litetui_terminal::{
    demo::Demo,
    live::{Interactive, Observation, Options},
    model_controls::{Intent, Kind, Update},
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

fn launch() -> (Interactive, PathBuf) {
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
    RUST_ADMISSION_FIXTURE={})
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False,thinking_level="low")
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,
        literal(&source),
        literal(&fixture.join("runtime_controls_runtime.py")),
        literal(&fixture.join("admission_refusal_runtime.py")),
        literal(&fixture.join("admission_refusal_runtime.py"))
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
        "codex".into(),
        "--codex-engine".into(),
        "native".into(),
        "--model".into(),
        "gpt-6.1-sol".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--tools".into(),
        "off".into(),
        "--thinking-level".into(),
        "low".into(),
        "--timeout".into(),
        "120".into(),
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

fn prompt(runtime: &mut Interactive, app: &mut Demo, text: &str) -> io::Result<()> {
    app.live.as_mut().unwrap().submit(text)?;
    runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())
}

#[test]
#[ignore = "actual source and private Codex boundary; requires LITETUI_AUTHORITY_PYTHON"]
fn floor_and_async_hook_refusals_settle_exact_attempts_then_medium_recovers() {
    let (runtime, data) = launch();
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("codex".into(), "gpt-6.1-sol".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("preserved composer draft");
        let draft = app.composer.text().to_owned();
        prompt(runtime, &mut app, "below-floor input")?;
        observe(runtime, &mut app, |e| matches!(e, Observation::Refused(_)))?;
        assert!(draw(&mut app).contains("FLEET FLOOR"));
        let capture = json_file(&data.join("admission-observations.json"));
        assert_eq!(capture["provider_requests"], json!([]));
        assert_eq!(capture["refusals"][0]["executions"], json!([]));
        assert_eq!(capture["refusals"][0]["inputs"], json!([]));
        assert_eq!(capture["refusals"][0]["history"], capture["initial_history"]);
        runtime.control(Intent::Open(Kind::Thinking))?;
        let mut menu = None;
        let deadline = Instant::now()+Duration::from_secs(20);
        while menu.is_none() {
            let event=runtime.events.recv_timeout(Duration::from_secs(2)).map_err(io::Error::other)?;
            if let Observation::Controls(Update::Choices(value))=&event { menu=Some(value.clone()); }
            app.observe_live(event)?;
            if Instant::now()>deadline { return Err(io::Error::other("thinking read timeout")); }
        }
        let menu=menu.unwrap();
        assert_eq!(menu.current,"low");
        assert!(menu.rows.iter().any(|row|row.value=="medium"));
        runtime.control(Intent::Select{revision:menu.revision,value:"medium".into(),context:None})?;
        observe(runtime,&mut app,|event|matches!(event,Observation::Controls(Update::Choices(value)) if value.current=="medium"))?;
        prompt(runtime,&mut app,"deny this hook")?;
        observe(runtime,&mut app,|e|matches!(e,Observation::Refused(_)))?;
        let capture=json_file(&data.join("admission-observations.json"));
        assert_eq!(capture["provider_requests"],json!([]));
        assert_eq!(capture["refusals"].as_array().unwrap().len(),2);
        assert_eq!(capture["refusals"][1]["executions"],json!([]));
        assert!(capture["refusals"][1]["frame"]["event"]["detail"].as_str().unwrap().contains("asynchronous hook denial"));
        assert_eq!(capture["refusals"][1]["history"],capture["initial_history"]);
        prompt(runtime,&mut app,"explicit valid recovery")?;
        observe(runtime,&mut app,|e|matches!(e,Observation::Complete))?;
        assert_eq!(app.composer.text(),draft);
        assert!(draw(&mut app).contains("CONTROLLED CODEX RECOVERY"));
        Ok(())
    });
    assert!(result.is_ok(), "{result:?}; evidence={}", data.display());
    let capture = json_file(&data.join("admission-observations.json"));
    assert_eq!(capture["provider_requests"].as_array().unwrap().len(), 1);
    assert_eq!(capture["provider_requests"][0]["effort"], "medium");
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["denied"], json!([]));
    assert_eq!(report["reader_retired"], true);
    assert_eq!(report["shutdown"]["ok"], true);
    let wire = report["wire"].as_array().unwrap();
    let refused: Vec<_> = wire
        .iter()
        .filter(|row| row["type"] == "gui.event" && row["event"]["phase"] == "pre_admission")
        .collect();
    assert_eq!(refused.len(), 2);
    assert_ne!(refused[0]["operation_id"], refused[1]["operation_id"]);
    for event in refused {
        assert!(event["execution_id"].is_null() && event["hook_id"].is_null());
        assert_eq!(event["source"], "rpc");
        assert!(wire.iter().any(|row| row["type"] == "response"
            && row["id"] == event["operation_id"]
            && row["result"]["turn"] == "accepted"));
    }
    assert_eq!(
        wire.iter()
            .filter(|row| row["type"] == "turn_start")
            .count(),
        1
    );
    assert_eq!(
        wire.iter().filter(|row| row["type"] == "turn_end").count(),
        1
    );
}
