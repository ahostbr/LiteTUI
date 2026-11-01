#![cfg(windows)]
//! Native Interactive runtime driving the actual captured Python Work lifecycle.
use litetui_terminal::{
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options, WorkExecution},
    live_chat::ReplyState,
};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
fn source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn launch(case: &str) -> (Interactive, PathBuf) {
    let actual = source();
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/work-execution-process-tests")
        .join(format!(
            "{}-{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    let quote = |s: &str| serde_json::to_string(s).unwrap();
    fs::write(
        package.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            quote(&actual.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    fs::write(package.join("cli.py"), format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_WORK_EXECUTION_SOURCE'] = {}
os.environ['RUST_WORK_EXECUTION_CASE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()), quote(case), quote(&fixtures.join("work_execution_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let args = vec![
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
        "--tools".into(),
        "off".into(),
        "--timeout".into(),
        "100".into(),
        "--server-timeout".into(),
        "5".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let deadline = Instant::now() + Duration::from_secs(105);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(50)) {
            Ok(event) => return Ok(event),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected Work runtime exit")));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Work observation deadline"));
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
        let event = next(runtime)?;
        let done = predicate(&event);
        app.observe_live(event)?;
        if done {
            return Ok(());
        }
    }
}
fn report(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("work-execution-report.json")).unwrap())
            .unwrap();
    for name in ["app", "background_input", "gui_rpc"] {
        assert_eq!(
            PathBuf::from(r[format!("source_{name}")].as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join(format!("src/litetui/{name}.py"))
                .canonicalize()
                .unwrap()
        );
    }
    for key in ["denied", "questions", "approvals", "authorities"] {
        assert_eq!(r[key], json!([]), "{key} at {}", root.display());
    }
    assert_eq!(r["startup_held"], true);
    let arguments = r["launch_arguments"].as_array().unwrap();
    assert!(
        arguments
            .windows(2)
            .any(|pair| pair == [json!("--work-lifecycle-version"), json!("1")])
    );
    let commands = r["commands"].as_array().unwrap();
    for kind in ["gui.work.native_capture", "gui.work.native_activate"] {
        assert_eq!(commands.iter().filter(|c| c["type"] == kind).count(), 1);
    }
    let activation = commands
        .iter()
        .find(|c| c["type"] == "gui.work.native_activate")
        .unwrap();
    let wire = r["wire"].as_array().unwrap();
    let published = wire
        .iter()
        .position(|row| {
            row["type"] == "response" && row["id"] == activation["id"] && row["ok"] == true
        })
        .unwrap();
    assert_eq!(
        wire.iter()
            .filter(|row| row["type"] == "ready" && row["work_lifecycle_version"] == 1)
            .count(),
        1
    );
    assert!(
        wire.iter()
            .take(published + 1)
            .all(|row| !row["type"].as_str().unwrap_or("").starts_with("work_"))
    );
    assert_eq!(r["reader_retired"], true);
    assert_eq!(r["helper_retired"], true);
    assert_eq!(r["history"], r["durable"]);
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(
        r["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["closed"] == true)
    );
    let leases = r["lease_release"].as_array().unwrap();
    assert!(!leases.is_empty());
    assert!(leases.iter().all(|l| l["active_workers"] == json!([])
        && l["questions"] == json!([])
        && l["approvals"] == json!([])));
    for key in ["producers", "inputs"] {
        assert!(
            r["lifecycle"][key]
                .as_array()
                .unwrap()
                .iter()
                .all(|v| v["worker_finished"] == true && v["observer_done"] == true)
        );
    }
    assert_eq!(r["shutdown"]["ok"], true);
    assert_eq!(
        r["shutdown"]["result"]["work_shutdown"]["worker_settled"],
        true
    );
    assert_eq!(
        r["shutdown"]["result"]["work_shutdown"]["admission_closed"],
        true
    );
    r
}
#[test]
#[ignore = "controlled actual-source Work execution process; dependency Python required"]
fn actual_background_completion_adopts_source_turn_then_verifies_retirement() {
    let (runtime, root) = launch("success");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("KEEP COMPOSER AND SELECTION");
        app.composer.move_cursor(Motion::WordLeft, true);
        let draft = (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(update) if matches!(update.as_ref(), WorkExecution::Admitted{..})))?;
        let chat = app.live.as_mut().unwrap();
        assert_eq!(chat.rows.len(), 1);
        assert!(chat.rows[0].source_owned);
        assert!(chat.rows[0].prompt.contains("CANONICAL BACKGROUND TASK OUTPUT"));
        assert!(chat.take_submission().is_none());
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(update) if matches!(update.as_ref(), WorkExecution::Ended{..})))?;
        assert_eq!(app.live.as_ref().unwrap().rows[0].state, ReplyState::AwaitingRetirement);
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(update) if matches!(update.as_ref(), WorkExecution::Settled{..})))?;
        let chat = app.live.as_ref().unwrap();
        assert_eq!(chat.rows[0].state, ReplyState::Complete);
        assert_eq!(chat.rows[0].cards[0].answer, "CONTROLLED SOURCE WORK ANSWER");
        assert!(!chat.busy);
        assert_eq!(draft, (app.composer.text().to_owned(), app.composer.before_cursor().to_owned(), app.composer.selected_text().to_owned()));
        Ok(())
    });
    if let Err(error) = &result {
        eprintln!("Work execution at {}: {error}", root.display());
    }
    result.unwrap();
    let r = report(&root);
    assert_eq!(r["requests"].as_array().unwrap().len(), 1);
    let inputs = r["lifecycle"]["inputs"].as_array().unwrap();
    assert_eq!(inputs.len(), 1);
    assert_eq!(inputs[0]["state"], "settled");
    assert_eq!(inputs[0]["admission"]["mode"], "new_turn");
    assert!(
        r["history"]
            .as_array()
            .unwrap()
            .iter()
            .any(|m| m["work_input_id"] == inputs[0]["ref"]["input_id"])
    );
    let commands = r["commands"].as_array().unwrap();
    for kind in [
        "gui.work.native_capture",
        "gui.work.native_activate",
        "gui.work.native_settled",
    ] {
        assert_eq!(
            commands.iter().filter(|c| c["type"] == kind).count(),
            1,
            "{kind}"
        );
    }
    assert!(!commands.iter().any(|c| c["type"] == "prompt"));
}

#[test]
#[ignore = "controlled actual-source Work execution process; dependency Python required"]
fn second_source_turn_keeps_streaming_while_old_retirement_readback_completes() {
    let (runtime, root) = launch("overlap");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("DRAFT ACROSS TWO SOURCE TURNS");
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Ended{..})))?;
        assert_eq!(app.live.as_ref().unwrap().rows[0].state, ReplyState::AwaitingRetirement);
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Admitted{..})))?;
        assert_eq!(app.live.as_ref().unwrap().rows.len(), 2);
        assert_eq!(app.live.as_ref().unwrap().rows[0].state, ReplyState::AwaitingRetirement);
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Settled{..})))?;
        let chat = app.live.as_ref().unwrap();
        assert_eq!(chat.rows[0].state, ReplyState::Complete);
        assert_eq!(chat.rows[1].state, ReplyState::Streaming);
        assert_eq!(chat.rows[1].cards[0].answer, "SECOND SOURCE WORK ANSWER");
        assert!(chat.busy);
        fs::write(root.join("release-second"), b"release")?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Settled{..})))?;
        assert_eq!(app.live.as_ref().unwrap().rows[1].state, ReplyState::Complete);
        assert!(!app.live.as_ref().unwrap().busy);
        assert_eq!(app.composer.text(), "DRAFT ACROSS TWO SOURCE TURNS");
        Ok(())
    });
    if let Err(e) = &result {
        eprintln!("Overlap fixture {}: {e}", root.display());
    }
    result.unwrap();
    let r = report(&root);
    assert_eq!(r["requests"].as_array().unwrap().len(), 2);
    let inputs = r["lifecycle"]["inputs"].as_array().unwrap();
    assert_eq!(inputs.len(), 2);
    assert_ne!(
        inputs[0]["admission"]["operation_id"],
        inputs[1]["admission"]["operation_id"]
    );
    assert!(
        inputs
            .iter()
            .all(|i| i["admission"]["mode"] == "new_turn" && i["state"] == "settled")
    );
}
#[test]
#[ignore = "controlled actual-source Work execution process; dependency Python required"]
fn created_worker_binding_fault_retires_without_stream_and_keeps_inspection_reachable() {
    let (runtime, root) = launch("binding-fault");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("UNCHANGED UNCERTAIN DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Uncertain{..})))?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Settled{..})))?;
        let chat = app.live.as_mut().unwrap();
        assert_eq!(chat.rows[0].state, ReplyState::Cancelled);
        assert!(chat.rows[0].cards[0].answer.is_empty());
        assert!(chat.rows[0].stop_reason.as_ref().unwrap().contains("before execution started"));
        assert!(chat.busy && chat.submit("must not send").is_err());
        assert_eq!(app.composer.text(), "UNCHANGED UNCERTAIN DRAFT");
        app.open_work();
        runtime.work(app.live.as_mut().unwrap().work.take_intent().unwrap())?;
        loop {
            let event = next(runtime)?;
            let done = matches!(&event, Observation::Work(litetui_terminal::work_view::Update { data: litetui_terminal::work_view::Data::Page(_), .. }));
            app.observe_live(event)?;
            if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() { runtime.work(intent)?; }
            if done { break; }
        }
        assert!(app.live.as_ref().unwrap().work.owns_input());
        Ok(())
    });
    if let Err(e) = &result {
        eprintln!("Binding fixture {}: {e}", root.display());
    }
    result.unwrap();
    let r = report(&root);
    assert_eq!(r["requests"], json!([]));
    assert_eq!(r["streams"], json!([]));
    assert_eq!(
        r["lifecycle"]["inputs"][0]["receipt"]["outcome"],
        "cancelled"
    );
    assert!(
        !r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["type"] == "turn_start")
    );
}
#[test]
#[ignore = "controlled actual-source Work execution process; dependency Python required"]
fn quit_drains_buffered_source_admission_without_publishing_or_replaying_it() {
    let (runtime, root) = launch("quit-buffered");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let until = Instant::now() + Duration::from_secs(15);
        while !root.join("buffered.json").exists() {
            if Instant::now() >= until {
                return Err(io::Error::other("Source admission did not reach buffer"));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        app.live.as_mut().unwrap().begin_quit();
        runtime.quit()?;
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        Ok(())
    });
    if let Err(e) = &result {
        eprintln!("Quit fixture {}: {e}", root.display());
    }
    result.unwrap();
    let r = report(&root);
    assert_eq!(r["lifecycle"]["inputs"].as_array().unwrap().len(), 1);
    assert!(!r["commands"].as_array().unwrap().iter().any(|c| matches!(
        c["type"].as_str(),
        Some("prompt" | "gui.work.native_settled")
    )));
}
#[test]
#[ignore = "controlled actual-source Work execution process; dependency Python required"]
fn losing_reserved_local_prompt_is_refused_without_queue_or_history_append() {
    let (runtime, root) = launch("local-race");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let chat = app.live.as_mut().unwrap();
        chat.submit("LOCAL LOSING PROMPT MUST NOT APPEND")?;
        runtime.submit(chat.take_submission().unwrap())?;
        app.insert_text("NEXT COMPOSER DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Submission{..}))?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Admitted{..})))?;
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::LocalRefused{..})))?;
        assert_eq!(app.live.as_ref().unwrap().rows[0].state, ReplyState::Refused);
        assert_eq!(app.live.as_ref().unwrap().rows.len(), 2);
        wait(runtime, &mut app, |e| matches!(e, Observation::WorkExecution(u) if matches!(u.as_ref(), WorkExecution::Settled{..})))?;
        assert_eq!(app.live.as_ref().unwrap().rows[1].state, ReplyState::Complete);
        assert_eq!(app.composer.text(), "NEXT COMPOSER DRAFT");
        Ok(())
    });
    if let Err(e) = &result {
        eprintln!("Local race fixture {}: {e}", root.display());
    }
    result.unwrap();
    let r = report(&root);
    assert!(
        !r["history"]
            .to_string()
            .contains("LOCAL LOSING PROMPT MUST NOT APPEND")
    );
    assert_eq!(r["requests"].as_array().unwrap().len(), 1);
    let command = r["commands"]
        .as_array()
        .unwrap()
        .iter()
        .find(|c| c["type"] == "prompt")
        .unwrap();
    assert!(
        r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["type"] == "response" && e["id"] == command["id"] && e["ok"] == false)
    );
}
