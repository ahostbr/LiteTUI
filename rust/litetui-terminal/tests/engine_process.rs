#![cfg(windows)]
//! Real owned Python engine service; subprocess/hardware/HTTP boundaries are controlled.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    demo::Demo,
    engine_controls::Admission,
    live::{Interactive, Observation, Options},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
use std::{
    fs, io,
    path::PathBuf,
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
fn launch(backend: &str, case: &str, management: bool) -> (Interactive, PathBuf) {
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let actual = std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate_root.join("../.."))
        .canonicalize()
        .unwrap();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/engine-process-tests")
        .join(format!(
            "{}-{}-{backend}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
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
    fs::write(package.join("cli.py"),format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_ENGINE_MANAGEMENT_SOURCE'] = {}
os.environ['RUST_ENGINE_MANAGEMENT_CASE'] = {}
os.environ['RUST_ENGINE_MANAGEMENT_DRIVER'] = 'rpc'
os.environ.pop('LITETUI_MAX_TOKENS', None)
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, max_tokens_chat=101, max_tokens_tools=202)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(case),quote(&crate_root.join("tests/fixtures/engine_management_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        backend.into(),
        "--model".into(),
        "engine-model".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "30".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    if management {
        args.push("--engine-management".into());
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let until = Instant::now() + Duration::from_secs(45);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(100)) {
            Ok(e) => return Ok(e),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected exit")));
                }
                if Instant::now() > until {
                    return Err(io::Error::other("Engine observation timeout"));
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
fn wait_engine(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| matches!(e, Observation::Engine(_)))
}
fn send(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.engine(
        app.live
            .as_mut()
            .unwrap()
            .engine
            .take_intent()
            .expect("explicit engine intent"),
    )
}
fn draw(app: &mut Demo) {
    let mut t = Terminal::new(TestBackend::new(100, 32)).unwrap();
    t.draw(|f| ui::draw(f, app)).unwrap();
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .engine
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn inspect(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    if !app.has_live_engine() {
        app.open_engine();
    } else {
        key(app, KeyCode::Char('r'));
    }
    send(runtime, app)?;
    wait_engine(runtime, app)
}
fn review(runtime: &mut Interactive, app: &mut Demo, stop: bool) -> io::Result<()> {
    if stop {
        key(app, KeyCode::Down);
    }
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    wait_engine(runtime, app)
}
fn confirm(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    for _ in 0..100 {
        draw(app);
        key(app, KeyCode::PageDown);
    }
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    wait_engine(runtime, app)
}
fn report(root: &std::path::Path) -> Value {
    serde_json::from_str(&fs::read_to_string(root.join("engine-management-report.json")).unwrap())
        .unwrap()
}
fn commands(root: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(root.join("engine-management-commands.jsonl"))
        .unwrap_or_default()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
fn admission(app: &Demo) -> Option<Admission> {
    app.live.as_ref().unwrap().engine_admission
}
fn scenario(backend: &str, case: &str) {
    let (runtime, root) = launch(backend, case, true);
    let fatal = matches!(
        case,
        "policy-drift" | "history-drift" | "readback-drift" | "generic-action-error"
    );
    let refused = case.starts_with("resource-");
    let unowned = matches!(case, "attached" | "registered-foreign");
    let cancel = case == "cancel-review";
    let outcome = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live(backend.into(), "engine-model".into());
        app.insert_text("PRESERVE ENGINE DRAFT");
        wait(runtime, &mut app, |e| {
            matches!(e, Observation::EngineAdmission(_) | Observation::Ready)
        })?;
        if unowned {
            app.open_engine();
        }
        send(runtime, &mut app)?;
        wait_engine(runtime, &mut app)?;
        if unowned {
            key(&mut app, KeyCode::Down);
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            assert!(app.live.as_mut().unwrap().engine.take_intent().is_none());
            runtime.quit()?;
            return Ok(());
        }
        assert_eq!(admission(&app), Some(Admission::ManagementOnly));
        assert!(!app.live.as_ref().unwrap().ready);
        assert!(app.live.as_mut().unwrap().submit("must not queue").is_err());
        if cancel {
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            key(&mut app, KeyCode::Esc);
            wait_engine(runtime, &mut app)?;
            assert!(!app.has_live_engine());
            assert!(!app.live.as_ref().unwrap().ready);
            assert!(app.live.as_mut().unwrap().engine.take_intent().is_none());
            runtime.quit()?;
            return Ok(());
        }
        review(runtime, &mut app, false)?;
        let settled = confirm(runtime, &mut app);
        if fatal {
            assert!(settled.is_err());
            assert!(!app.live.as_ref().unwrap().ready);
            return settled;
        }
        settled?;
        if refused {
            assert_eq!(admission(&app), Some(Admission::ManagementOnly));
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("no automatic replay")
                    .is_err()
            );
            assert_eq!(app.composer.text(), "PRESERVE ENGINE DRAFT");
            runtime.quit()?;
            return Ok(());
        }
        if case == "retained-start" {
            assert_eq!(admission(&app), Some(Admission::RetainedRecovery));
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("no prompt while retained")
                    .is_err()
            );
        } else {
            assert_eq!(admission(&app), Some(Admission::PromptReady));
            key(&mut app, KeyCode::Esc);
            let text = "Explicit prompt after verified native engine start";
            app.live.as_mut().unwrap().submit(text)?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        }
        inspect(runtime, &mut app)?;
        review(runtime, &mut app, true)?;
        confirm(runtime, &mut app)?;
        if case == "retained-stop" {
            assert_eq!(admission(&app), Some(Admission::RetainedRecovery));
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("still no prompt")
                    .is_err()
            );
            inspect(runtime, &mut app)?;
            review(runtime, &mut app, false)?;
            confirm(runtime, &mut app)?;
        }
        assert_eq!(admission(&app), Some(Admission::ManagementOnly));
        assert!(!app.live.as_ref().unwrap().ready);
        assert_eq!(app.composer.text(), "PRESERVE ENGINE DRAFT");
        runtime.quit()?;
        Ok(())
    });
    if fatal {
        assert!(outcome.is_err(), "{backend}/{case}");
    } else {
        outcome.unwrap_or_else(|e| panic!("{backend}/{case}: {e}; root {}", root.display()));
    }
    let r = report(&root);
    assert!(r["denied"].as_array().unwrap().is_empty(), "{r}");
    let log = commands(&root);
    let prompts = log.iter().filter(|c| c["type"] == "prompt").count();
    assert_eq!(
        prompts,
        usize::from(!fatal && !refused && !unowned && !cancel && case != "retained-start"),
        "{case}"
    );
    if unowned || cancel || refused {
        assert!(
            !r["calls"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["effect"] == "spawn"),
            "{case}: {r}"
        );
    }
    if unowned {
        let close = r["events"]
            .as_array()
            .unwrap()
            .iter()
            .find(|v| v["id"] == "rust-live-shutdown")
            .unwrap();
        assert_eq!(
            close["result"]["work_shutdown"]["ledger_sha256"],
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        );
    }
    if cancel || unowned {
        assert!(
            !log.iter()
                .any(|c| c["type"] == litetui_terminal::engine_controls::ACTION)
        );
    }
    if !fatal && !unowned && !cancel && !refused {
        assert!(
            r["processes"]
                .as_array()
                .unwrap()
                .iter()
                .all(|p| p["returncode"].as_i64().is_some())
        );
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn both_engines_require_review_and_readback_then_explicit_prompt_and_owned_stop() {
    for backend in ["ninfer", "strata"] {
        scenario(backend, "success");
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn canonical_resource_refusals_never_spawn_or_enable_prompts() {
    for case in ["resource-memory", "resource-sibling"] {
        scenario("strata", case);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn retained_handles_require_fresh_explicit_stop_retry() {
    for case in ["retained-start", "retained-stop"] {
        scenario("strata", case);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn attached_and_registry_engines_are_never_stopped() {
    for case in ["attached", "registered-foreign"] {
        scenario("ninfer", case);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn contradictory_action_readback_closes_without_prompt() {
    for case in [
        "policy-drift",
        "history-drift",
        "readback-drift",
        "generic-action-error",
    ] {
        scenario("strata", case);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn cancelled_review_cannot_start_after_its_delayed_reply() {
    scenario("strata", "cancel-review");
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn ordinary_unavailable_launch_remains_fatal_without_management_opt_in() {
    for backend in ["ninfer", "strata"] {
        let (runtime, root) = launch(backend, "success", false);
        let outcome = runtime.with_cleanup(|runtime| {
            loop {
                match next(runtime)? {
                    Observation::Ready | Observation::EngineAdmission(_) => {
                        panic!("Unavailable ordinary launch was accepted")
                    }
                    _ => {}
                }
            }
        });
        assert!(outcome.is_err());
        let r = report(&root);
        assert!(r["denied"].as_array().unwrap().is_empty());
        assert!(
            !r["calls"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["effect"] == "spawn")
        );
        assert!(!commands(&root).iter().any(|c| c["type"] == "prompt"));
    }
}

#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn quit_during_start_joins_action_before_owned_shutdown_without_readback_or_prompt() {
    let (runtime, root) = launch("strata", "quit-during-start", true);
    let outcome = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("strata".into(), "engine-model".into());
        app.insert_text("KEEP QUIT DRAFT");
        wait(runtime, &mut app, |e| {
            matches!(e, Observation::EngineAdmission(_))
        })?;
        send(runtime, &mut app)?;
        wait_engine(runtime, &mut app)?;
        review(runtime, &mut app, false)?;
        for _ in 0..100 {
            draw(&mut app);
            key(&mut app, KeyCode::PageDown);
        }
        draw(&mut app);
        key(&mut app, KeyCode::Enter);
        send(runtime, &mut app)?;
        let until = Instant::now() + Duration::from_secs(10);
        while !root.join("engine-management-spawn-entered").is_file() {
            if Instant::now() >= until {
                return Err(io::Error::other("Controlled start did not enter spawn"));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        runtime.quit()?;
        let until = Instant::now() + Duration::from_secs(25);
        loop {
            for event in runtime.events.try_iter() {
                assert!(!matches!(
                    event,
                    Observation::Ready | Observation::EngineAdmission(Admission::PromptReady)
                ));
                app.observe_live(event)?;
            }
            if let Some(done) = runtime.poll_done() {
                done?;
                break;
            }
            if Instant::now() >= until {
                return Err(io::Error::other(
                    "Pending engine start did not join on quit",
                ));
            }
            std::thread::sleep(Duration::from_millis(10));
        }
        assert_eq!(app.composer.text(), "KEEP QUIT DRAFT");
        Ok(())
    });
    outcome.unwrap_or_else(|e| panic!("quit-during-start: {e}; root {}", root.display()));
    let r = report(&root);
    assert!(r["denied"].as_array().unwrap().is_empty());
    assert!(
        r["calls"]
            .as_array()
            .unwrap()
            .iter()
            .any(|c| c["effect"] == "shutdown-cancellation-requested")
    );
    assert!(!r["processes"].as_array().unwrap().is_empty());
    assert!(
        r["processes"]
            .as_array()
            .unwrap()
            .iter()
            .all(|p| p["returncode"].as_i64().is_some())
    );
    let log = commands(&root);
    assert_eq!(
        log.iter()
            .filter(|c| c["type"] == litetui_terminal::engine_controls::ACTION)
            .count(),
        1
    );
    assert_eq!(log.iter().filter(|c| c["type"] == "shutdown").count(), 1);
    assert!(
        !log.iter()
            .any(|c| c["type"] == "prompt"
                || c["type"] == litetui_terminal::engine_controls::READBACK)
    );
}

#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn held_input_requires_explicit_cancellation_before_fresh_engine_start() {
    use litetui_terminal::restored_queue::{Intent, Update};
    let (runtime, root) = launch("strata", "held-input", true);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("strata".into(), "engine-model".into());
            app.insert_text("KEEP HELD RECOVERY DRAFT");
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::EngineAdmission(_))
            })?;
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            assert!(
                app.live.as_mut().unwrap().engine.take_intent().is_none(),
                "held input blocks Start"
            );
            key(&mut app, KeyCode::Esc);
            app.open_queue();
            assert!(app.has_live_queue());
            runtime.queue(app.live.as_mut().unwrap().queue.take_intent().unwrap())?;
            loop {
                let event = next(runtime)?;
                let done = if let Observation::Queue(Update::Snapshot(menu)) = &event {
                    assert_eq!(menu.entries.len(), 1);
                    assert!(menu.entries[0].can_cancel);
                    assert!(!menu.entries[0].can_continue);
                    true
                } else {
                    false
                };
                app.observe_live(event)?;
                if done {
                    break;
                }
            }
            draw(&mut app);
            app.live
                .as_mut()
                .unwrap()
                .queue
                .key(KeyEvent::new(KeyCode::Delete, KeyModifiers::NONE));
            assert!(
                app.live.as_mut().unwrap().queue.take_intent().is_none(),
                "confirmation must be visible first"
            );
            draw(&mut app);
            app.live
                .as_mut()
                .unwrap()
                .queue
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            let intent = app.live.as_mut().unwrap().queue.take_intent().unwrap();
            assert!(matches!(intent, Intent::Cancel { .. }));
            runtime.queue(intent)?;
            loop {
                let event = next(runtime)?;
                let done = if let Observation::Queue(Update::Snapshot(menu)) = &event {
                    assert!(menu.entries.is_empty());
                    true
                } else {
                    false
                };
                app.observe_live(event)?;
                if done {
                    break;
                }
            }
            app.live.as_mut().unwrap().queue.close();
            assert!(!app.live.as_ref().unwrap().ready);
            inspect(runtime, &mut app)?;
            review(runtime, &mut app, false)?;
            confirm(runtime, &mut app)?;
            assert_eq!(admission(&app), Some(Admission::PromptReady));
            assert_eq!(app.composer.text(), "KEEP HELD RECOVERY DRAFT");
            key(&mut app, KeyCode::Esc);
            app.live
                .as_mut()
                .unwrap()
                .submit("Explicit prompt after cancelling saved held input")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            runtime.quit()?;
            Ok(())
        })
        .unwrap_or_else(|e| panic!("held-input: {e}; root {}", root.display()));
    let r = report(&root);
    assert!(r["denied"].as_array().unwrap().is_empty());
    let log = commands(&root);
    let cancel = log
        .iter()
        .position(|c| c["type"] == litetui_terminal::restored_queue::CANCEL)
        .unwrap();
    let start = log
        .iter()
        .position(|c| c["type"] == litetui_terminal::engine_controls::ACTION)
        .unwrap();
    let prompt = log.iter().position(|c| c["type"] == "prompt").unwrap();
    assert!(cancel < start && start < prompt);
    assert_eq!(log.iter().filter(|c| c["type"] == "prompt").count(), 1);
    assert!(
        r["processes"]
            .as_array()
            .unwrap()
            .iter()
            .all(|p| p["returncode"].as_i64().is_some())
    );
}

#[test]
#[ignore = "compiled F12 against both controlled source adapters; requires LITETUI_AUTHORITY_PYTHON"]
fn configure_both_engines_saves_one_conversation_preference_and_never_starts() {
    use litetui_terminal::{engine_config as cfg, engine_controls::Update};
    for backend in ["ninfer", "strata"] {
        let (runtime, root) = launch(backend, "config-only", true);
        let outcome = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live(backend.into(), "engine-model".into());
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::EngineAdmission(_) | Observation::Ready)
            })?;
            app.insert_text("F12 private draft");
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Char('c'));
            send(runtime, &mut app)?;
            let mut snapshot = None;
            while snapshot.is_none() {
                let event = next(runtime)?;
                if let Observation::Engine(Update::Config(reply)) = &event
                    && let cfg::Update::Snapshot(s) = reply.as_ref()
                {
                    snapshot = Some(s.clone());
                }
                if let Observation::Engine(Update::Rejected(reason)) = &event {
                    return Err(io::Error::other(reason.clone()));
                }
                app.observe_live(event)?;
            }
            let snapshot = snapshot.unwrap();
            // {backend}_max_context starts the engine, so it is device-wide (develop 5f5ef63f):
            // it saves to the global document and leaves the conversation's untouched.
            let destination =
                PathBuf::from(snapshot.wire["destination"]["device"].as_str().unwrap());
            let conversation = PathBuf::from(
                snapshot.wire["destination"]["conversation"]
                    .as_str()
                    .unwrap(),
            );
            assert!(
                destination.starts_with(root.canonicalize()?)
                    && destination.file_name().unwrap() == "settings.json"
            );
            let before = if destination.exists() {
                Some(fs::read(&destination)?)
            } else {
                None
            };
            assert_eq!(destination, root.canonicalize()?.join("settings.json"));
            let conversation_before = fs::read(&conversation).ok();
            let setting = format!("{backend}_max_context");
            assert_eq!(snapshot.names[3], setting);
            for _ in 0..3 {
                draw(&mut app);
                key(&mut app, KeyCode::Down);
            }
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            draw(&mut app);
            assert!(app.live.as_ref().unwrap().engine.accepts_config_paste());
            app.live.as_mut().unwrap().engine.config_paste("12001");
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            assert_eq!(fs::read(&destination).ok(), before);
            draw(&mut app);
            key(&mut app, KeyCode::Esc);
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            let mut altered: Value = match &before {
                Some(bytes) => serde_json::from_slice(bytes)?,
                None => serde_json::json!({}),
            };
            fs::create_dir_all(destination.parent().unwrap())?;
            altered["future_extension"] = serde_json::json!({"keep":"disk"});
            fs::write(&destination, serde_json::to_vec(&altered)?)?;
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            assert_eq!(
                serde_json::from_slice::<Value>(&fs::read(&destination)?)?,
                altered
            );
            draw(&mut app);
            key(&mut app, KeyCode::F(5));
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?; // automatic post-save refresh
            let saved: Value = serde_json::from_slice(&fs::read(&destination)?)?;
            assert_eq!(saved["future_extension"], altered["future_extension"]);
            assert_eq!(saved[&setting], 12001);
            assert_eq!(fs::read(&conversation).ok(), conversation_before);
            assert_eq!(admission(&app), Some(Admission::ManagementOnly));
            assert_eq!(app.composer.text(), "F12 private draft");
            runtime.quit()?;
            Ok(())
        });
        outcome.unwrap_or_else(|e| panic!("{backend} configuration {}: {e}", root.display()));
        let report = report(&root);
        assert!(
            report["calls"]
                .as_array()
                .unwrap()
                .iter()
                .any(|v| v["effect"] == "verified-offline-refusal")
        );
        assert_eq!(report["processes"].as_array().unwrap().len(), 0);
        assert_eq!(report["requests"].as_array().unwrap().len(), 0);
        let commands = commands(&root);
        assert_eq!(
            commands.iter().filter(|v| v["type"] == cfg::SAVE).count(),
            2
        );
        assert_eq!(
            commands
                .iter()
                .filter(|v| v["type"] == cfg::READBACK)
                .count(),
            1
        );
        assert!(!commands.iter().any(
            |v| v["type"] == "gui.engine.native_action" || v["type"] == "gui.models.reconnect"
        ));
    }
}

#[test]
#[ignore = "controlled source, registry and engine processes only"]
fn second_instance_requires_separate_consent_preserves_process_limits_and_detects_pid_reuse() {
    for case in ["peer-start", "peer-reused-pid", "peer-competing-engine"] {
        let (runtime, root) = launch("strata", case, true);
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live("strata".into(), "engine-model".into());
                app.insert_text("KEEP THE PEER DRAFT");
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::EngineAdmission(_) | Observation::Ready)
                })?;
                send(runtime, &mut app)?;
                wait_engine(runtime, &mut app)?;
                review(runtime, &mut app, false)?;
                confirm(runtime, &mut app)?;
                assert_eq!(admission(&app), Some(Admission::ManagementOnly));
                inspect(runtime, &mut app)?;
                review(runtime, &mut app, false)?;
                assert!(
                    commands(&root)
                        .iter()
                        .any(|v| v["type"] == "gui.engine.native_peer_validate")
                );
                key(&mut app, KeyCode::Esc);
                assert_eq!(
                    commands(&root)
                        .iter()
                        .filter(|v| v["type"] == "gui.engine.native_action")
                        .count(),
                    1
                );
                inspect(runtime, &mut app)?;
                review(runtime, &mut app, false)?;
                if case == "peer-reused-pid" {
                    fs::write(root.join("replace-peer"), "changed")?;
                }
                confirm(runtime, &mut app)?;
                assert_eq!(
                    admission(&app),
                    Some(if case == "peer-start" {
                        Admission::PromptReady
                    } else {
                        Admission::ManagementOnly
                    })
                );
                if case == "peer-start" {
                    inspect(runtime, &mut app)?;
                    review(runtime, &mut app, true)?;
                    confirm(runtime, &mut app)?;
                }
                assert_eq!(app.composer.text(), "KEEP THE PEER DRAFT");
                runtime.quit()?;
                Ok(())
            })
            .unwrap_or_else(|e| panic!("{case}: {e}; root {}", root.display()));
        let r = report(&root);
        let results = r["results"].as_array().unwrap();
        let proofs: Vec<_> = results
            .iter()
            .filter(|r| {
                r["command"]["type"] == "gui.engine.native_action"
                    && r["command"]["consent_scope"] == "one_start_with_local_peers"
            })
            .collect();
        assert_eq!(proofs.len(), 1);
        let a = &proofs[0]["result"];
        assert_eq!(a["peer_accepted"], case != "peer-reused-pid");
        assert_eq!(a["spawn_attempted"], case == "peer-start");
        assert!(
            results
                .iter()
                .any(|r| r["command"]["type"] == "gui.engine.native_readback"
                    && r["result"]["peer_sha256"] == a["peer_sha256"]
                    && r["result"]["peer_accepted"] == a["peer_accepted"])
        );
        let peer = results
            .iter()
            .find(|r| r["command"]["type"] == "gui.engine.native_peer_validate")
            .unwrap();
        assert_eq!(
            peer["result"]["local_peer_start"]["peers"][0]["pid"],
            2000000444
        );
        assert!(r["denied"].as_array().unwrap().is_empty());
    }
}

#[test]
#[ignore = "controlled source, owned conversation and synthetic existing local services"]
fn explicit_attach_only_accepts_serving_reviewed_model_and_never_acquires_an_engine() {
    for (backend, case, success) in [
        ("ninfer", "attach-online", true),
        ("strata", "attach-online", true),
        ("strata", "attach-wrong-model", false),
        ("ninfer", "attach-offline", false),
    ] {
        let (runtime, root) = launch(backend, case, true);
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live(backend.into(), "engine-model".into());
                app.insert_text("KEEP THE ATTACH DRAFT");
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::EngineAdmission(_) | Observation::Ready)
                })?;
                send(runtime, &mut app)?;
                wait_engine(runtime, &mut app)?;
                draw(&mut app);
                key(&mut app, KeyCode::Char('a'));
                send(runtime, &mut app)?;
                wait_engine(runtime, &mut app)?;
                key(&mut app, KeyCode::Esc);
                assert!(
                    !commands(&root)
                        .iter()
                        .any(|v| v["type"] == "gui.engine.native_action")
                );
                inspect(runtime, &mut app)?;
                draw(&mut app);
                key(&mut app, KeyCode::Char('a'));
                send(runtime, &mut app)?;
                wait_engine(runtime, &mut app)?;
                if case != "attach-offline" {
                    fs::write(root.join("engine-ready"), "controlled available service")?;
                }
                confirm(runtime, &mut app)?;
                assert_eq!(
                    admission(&app),
                    Some(if success {
                        Admission::PromptReady
                    } else {
                        Admission::ManagementOnly
                    })
                );
                if success {
                    key(&mut app, KeyCode::Esc);
                    app.live
                        .as_mut()
                        .unwrap()
                        .submit("Explicit user turn on attached endpoint")?;
                    runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
                    wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
                    inspect(runtime, &mut app)?;
                    draw(&mut app);
                    key(&mut app, KeyCode::Down);
                    draw(&mut app);
                    key(&mut app, KeyCode::Enter);
                    assert!(app.live.as_mut().unwrap().engine.take_intent().is_none());
                }
                assert_eq!(app.composer.text(), "KEEP THE ATTACH DRAFT");
                runtime.quit()?;
                Ok(())
            })
            .unwrap_or_else(|e| panic!("{backend}/{case}: {e}; root {}", root.display()));
        let r = report(&root);
        let results = r["results"].as_array().unwrap();
        let a = &results
            .iter()
            .find(|r| r["command"]["type"] == "gui.engine.native_action")
            .unwrap()["result"];
        let v = &results
            .iter()
            .find(|r| r["command"]["type"] == "gui.engine.native_readback")
            .unwrap()["result"];
        assert_eq!(a["action"], "reconnect");
        assert_eq!(a["spawn_attempted"], false);
        assert!(a["handle_token"].is_null());
        assert_eq!(a["reconnect"], v["reconnect"]);
        assert_eq!(v["connected"], success);
        assert!(r["processes"].as_array().unwrap().is_empty());
        assert!(r["denied"].as_array().unwrap().is_empty());
        for call in r["calls"].as_array().unwrap() {
            assert_ne!(call["effect"], "job-terminate");
            if call["effect"] == "http" {
                assert_eq!(call["method"], "GET");
            }
        }
    }
}

#[test]
#[ignore = "controlled source and attached endpoint only"]
fn changed_attachment_during_readback_never_admits_a_turn() {
    let (runtime, root) = launch("strata", "attach-readback-drift", true);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("strata".into(), "engine-model".into());
        wait(runtime, &mut app, |e| {
            matches!(e, Observation::EngineAdmission(_) | Observation::Ready)
        })?;
        send(runtime, &mut app)?;
        wait_engine(runtime, &mut app)?;
        draw(&mut app);
        key(&mut app, KeyCode::Char('a'));
        send(runtime, &mut app)?;
        wait_engine(runtime, &mut app)?;
        fs::write(root.join("engine-ready"), "temporarily correct")?;
        let settled = confirm(runtime, &mut app);
        assert!(settled.is_err());
        assert!(!app.live.as_ref().unwrap().ready);
        settled
    });
    assert!(result.is_err());
    let r = report(&root);
    assert!(r["requests"].as_array().unwrap().is_empty());
    assert!(r["processes"].as_array().unwrap().is_empty());
    assert!(
        r["results"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["result"]["status"] == "connected")
    );
    assert!(
        r["events"]
            .as_array()
            .unwrap()
            .iter()
            .any(|r| r["ok"] == false)
    );
}

#[test]
#[ignore = "controlled blocking metadata read and actual shutdown"]
fn quit_during_attach_joins_the_read_without_admitting_a_prompt() {
    let (runtime, root) = launch("strata", "attach-quit", true);
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("strata".into(), "engine-model".into());
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::EngineAdmission(_) | Observation::Ready)
            })?;
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Char('a'));
            send(runtime, &mut app)?;
            wait_engine(runtime, &mut app)?;
            fs::write(root.join("engine-ready"), "controlled available service")?;
            for _ in 0..100 {
                draw(&mut app);
                key(&mut app, KeyCode::PageDown);
            }
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            let until = Instant::now() + Duration::from_secs(7);
            while !root.join("attach-in-flight").exists() {
                assert!(Instant::now() < until);
                std::thread::sleep(Duration::from_millis(10));
            }
            runtime.quit()?;
            Ok(())
        })
        .unwrap_or_else(|e| panic!("attach-quit: {e}; root {}", root.display()));
    let r = report(&root);
    assert!(r["requests"].as_array().unwrap().is_empty());
    assert!(r["processes"].as_array().unwrap().is_empty());
    assert!(
        r["calls"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["effect"] == "attach-metadata-read-returned")
    );
    assert!(
        r["results"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["result"]["status"] == "cancelled" && v["result"]["worker_settled"] == true)
    );
}
