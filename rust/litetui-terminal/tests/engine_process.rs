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
    let actual = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
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
"#,quote(&actual.to_string_lossy()),quote(case),quote(&actual.join("rust/litetui-terminal/tests/fixtures/engine_management_runtime.py").to_string_lossy()))).unwrap();
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
