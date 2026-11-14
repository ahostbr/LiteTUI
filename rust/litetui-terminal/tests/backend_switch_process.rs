#![cfg(windows)]
//! Native supervisor -> actual owned CLI/source transition. External providers,
//! network, model loading and server start are controlled or tripwired.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    backend_controls::{Intent, Update},
    demo::Demo,
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
fn runtime_source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn launch(initial: &str, target: &str, case: &str) -> (Interactive, PathBuf) {
    launch_with_thinking(initial, target, case, Some("default"))
}
fn launch_with_thinking(
    initial: &str,
    target: &str,
    case: &str,
    thinking: Option<&str>,
) -> (Interactive, PathBuf) {
    let fixture_source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let actual = runtime_source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/backend-switch-process-tests")
        .join(format!(
            "{}-{}-{case}",
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
os.environ['RUST_BACKEND_SWITCH_SOURCE'] = {}
os.environ['RUST_BACKEND_SWITCH_TARGET'] = {}
os.environ['RUST_BACKEND_SWITCH_CASE'] = {}
os.environ['RUST_BACKEND_SWITCH_DRIVER'] = 'rpc'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text())
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=8192, lm_host='http://127.0.0.1:45121', llama_host='http://127.0.0.1:45122', llama_attach_hosts=['http://127.0.0.1:45129'])
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(target),quote(case),quote(&fixture_source.join("rust/litetui-terminal/tests/fixtures/backend_switch_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let python = std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python required");
    let mut args = vec![
        "--python".into(),
        python,
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        initial.into(),
        "--model".into(),
        "switch-old".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--timeout".into(),
        "30".into(),
        "--server-timeout".into(),
        "5".into(),
    ];
    if initial == "codex" && case == "launch-defaults" {
        args.extend(["--codex-engine".into(), "native".into()]);
    }
    if let Some(level) = thinking {
        args.extend(["--thinking-level".into(), level.into()]);
    }
    if initial == "custom" {
        args.extend([
            "--base-url".into(),
            "http://127.0.0.1:45120/v1".into(),
            "--server-mode".into(),
            "connect".into(),
        ]);
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let until = Instant::now() + Duration::from_secs(35);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(100)) {
            Ok(e) => return Ok(e),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected clean runtime exit")));
                }
                if Instant::now() > until {
                    return Err(io::Error::other("Native switch observation timeout"));
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
fn draw(app: &mut Demo) {
    let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
}
fn read_catalog(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_backend();
    runtime.backend(
        app.live
            .as_mut()
            .unwrap()
            .backend_controls
            .take_intent()
            .unwrap(),
    )?;
    wait(runtime, app, |e| {
        matches!(e, Observation::Backend(Update::Catalog(_)))
    })
}
fn switch(runtime: &mut Interactive, app: &mut Demo, target: &str) -> io::Result<()> {
    // Python's /backend: Enter on the engine switches it at once, the model unchosen (Ryan
    // 2026-11-12: "/backend switches backend only /model displays models that backend provides").
    draw(app);
    let view = &mut app.live.as_mut().unwrap().backend_controls;
    let c = view.catalog.as_ref().unwrap();
    let index = c.entries.iter().position(|r| r.name == target).unwrap();
    view.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
    for _ in 0..index {
        view.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    }
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let intent = view.take_intent().unwrap();
    assert!(matches!(&intent, Intent::Switch { name, .. } if name == target));
    runtime.backend(intent)
}
/// The /model the switch opened (App._offer_unchosen_model) picks the target's model.
fn pick_model(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    use litetui_terminal::model_controls::{Intent as Control, Kind, Update as Menu};
    let open = app.live.as_mut().unwrap().controls.take_intent();
    assert!(
        matches!(open, Some(Control::Open(Kind::Model))),
        "/model is offered after /backend"
    );
    runtime.control(open.unwrap())?;
    let revision = std::cell::Cell::new(None);
    wait(runtime, app, |e| match e {
        Observation::Controls(Menu::Choices(menu)) => {
            assert!(
                menu.current.is_empty(),
                "no model is current after /backend"
            );
            assert!(menu.rows.iter().any(|r| r.value == "switch-new"));
            revision.set(Some(menu.revision));
            true
        }
        _ => false,
    })?;
    runtime.control(Control::Select {
        revision: revision.get().unwrap(),
        value: "switch-new".into(),
        context: None,
    })?;
    wait(
        runtime,
        app,
        |e| matches!(e, Observation::Controls(Menu::Choices(menu)) if menu.current == "switch-new"),
    )
}
fn inspect_settings(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_settings();
    runtime.settings(app.live.as_mut().unwrap().settings.take_intent().unwrap())?;
    wait(runtime, app, |e| matches!(e, Observation::Settings(_)))?;
    let view = &mut app.live.as_mut().unwrap().settings;
    assert!(!view.notice.contains("Invalid"), "{}", view.notice);
    view.close();
    Ok(())
}
fn submit(runtime: &mut Interactive, app: &mut Demo, prompt: &str) -> io::Result<()> {
    app.live.as_mut().unwrap().submit(prompt)?;
    runtime.submit(
        app.live
            .as_mut()
            .unwrap()
            .take_submission()
            .expect("reserved prompt"),
    )?;
    wait(runtime, app, |e| matches!(e, Observation::Complete))
}
fn commands(root: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(root.join("switch-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
/// What Python's /backend does in each case (App.apply_backend_change, measured through the
/// actual source with tests/test_rust_backend_switch_process.py).
#[derive(Clone, Copy, Debug, PartialEq)]
enum Outcome {
    /// Saved and connected: /model opens, the pick and the next prompt reach the target.
    Switched,
    /// Saved, but the backend did not answer: the seat stays on it, disconnected, no model.
    Disconnected,
    /// Refused before anything changed; the session goes on.
    Refused,
    /// Published but uncertain: the session closes.
    Closed,
}
fn scenario(initial: &str, target: &str, case: &str, outcome: Outcome, retry: bool) {
    let (runtime, root) = launch(initial, target, case);
    let switched = outcome == Outcome::Switched;
    let mut changed = false;
    let mut refused = false;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live(initial.into(), "switch-old".into());
        app.insert_text("UNSENT HUMAN DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        inspect_settings(runtime, &mut app)?;
        if switched && initial == "custom" && case != "empty" {
            submit(runtime, &mut app, "Explicit pre-switch prompt")?;
        }
        let before_rows = app.live.as_ref().unwrap().rows.len();
        let before_session = app
            .live
            .as_ref()
            .unwrap()
            .runtime_state
            .startup()
            .unwrap()
            .session
            .clone();
        read_catalog(runtime, &mut app)?;
        switch(runtime, &mut app, target)?;
        loop {
            let event = next(runtime)?;
            let got_change = matches!(&event, Observation::BackendChanged(_));
            let rejection = matches!(&event, Observation::Backend(Update::Rejected(_)));
            app.observe_live(event)?;
            assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
            if rejection {
                refused = true;
                assert_eq!(app.live.as_ref().unwrap().backend, initial);
                assert_eq!(app.live.as_ref().unwrap().model, "switch-old");
                assert_eq!(
                    commands(&root)
                        .iter()
                        .filter(|c| c["type"] == "gui.backend.set")
                        .count(),
                    if case == "oversized-history" { 0 } else { 1 },
                    "No automatic retry"
                );
                if case == "oversized-history" {
                    app.live.as_mut().unwrap().backend_controls.close();
                    submit(
                        runtime,
                        &mut app,
                        "Explicit original-backend prompt after history refusal",
                    )?;
                    assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
                    assert_eq!(app.live.as_ref().unwrap().backend, initial);
                    return Ok(());
                }
                if retry {
                    app.live.as_mut().unwrap().backend_controls.close();
                    read_catalog(runtime, &mut app)?;
                    switch(runtime, &mut app, target)?;
                    continue;
                }
                return Ok(());
            }
            if got_change {
                changed = true;
                let chat = app.live.as_ref().unwrap();
                assert_eq!(chat.backend, target);
                assert_eq!(chat.model, "", "/backend leaves the model unchosen");
                assert_eq!(chat.rows.len(), before_rows);
                assert_eq!(
                    chat.runtime_state.startup().unwrap().session,
                    before_session
                );
                assert_eq!(
                    chat.runtime_state.startup().unwrap().held_restored_input,
                    Some(0)
                );
                assert!(
                    chat.runtime_state
                        .footer_fields()
                        .iter()
                        .filter(|f| f.key == litetui_terminal::footer::FieldKey::Context)
                        .all(|f| f.text.contains('—'))
                );
                break;
            }
        }
        assert!(
            matches!(outcome, Outcome::Switched | Outcome::Disconnected),
            "Unexpected completed switch: {case}"
        );
        if !switched {
            // Nothing to pick from a backend that did not answer, and Python offers no /model.
            assert!(app.live.as_mut().unwrap().controls.take_intent().is_none());
            return Ok(());
        }
        pick_model(runtime, &mut app)?;
        assert_eq!(app.live.as_ref().unwrap().model, "switch-new");
        inspect_settings(runtime, &mut app)?;
        submit(runtime, &mut app, "Explicit next prompt")?;
        assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
        assert_eq!(app.live.as_ref().unwrap().rows.len(), before_rows + 1);
        Ok(())
    });
    fs::write(
        root.parent().unwrap().join("native-result.txt"),
        format!("{result:?}"),
    )
    .unwrap();
    if outcome == Outcome::Closed {
        assert!(
            result.is_err(),
            "{case} must close after uncertain transition"
        );
    } else {
        assert!(
            result.is_ok(),
            "{case} {initial}->{target}: {result:?} at {}",
            root.display()
        );
    }
    assert_eq!(
        changed,
        matches!(outcome, Outcome::Switched | Outcome::Disconnected)
    );
    if retry || outcome == Outcome::Refused {
        assert!(refused);
    }
    let sent = commands(&root);
    let mutations = sent
        .iter()
        .filter(|c| c["type"] == "gui.backend.set")
        .count();
    assert_eq!(mutations, if retry { 2 } else { 1 });
    // The switch names no model; /model picks it afterwards.
    assert!(
        sent.iter()
            .filter(|c| c["type"] == "gui.backend.set")
            .all(|c| c["model"].is_null() && c.get("context").is_none())
    );
    assert_eq!(
        sent.iter()
            .filter(|c| c["type"] == "gui.models.select")
            .count(),
        usize::from(switched)
    );
    let prompts = sent.iter().filter(|c| c["type"] == "prompt").count();
    assert_eq!(
        prompts,
        if switched && initial == "custom" && case != "empty" {
            2
        } else if switched || case == "oversized-history" {
            1
        } else {
            0
        }
    );
    assert!(
        sent.iter()
            .filter(|c| c["type"] == "gui.state")
            .all(|c| c["include_conversations"] == false)
    );
    assert!(!sent.iter().any(|c| c["type"] == "gui.conversations.read"));
    assert!(
        sent.iter()
            .filter(|c| c["type"] == "gui.history.native_snapshot")
            .count()
            >= 2
    );
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("switch-report.json")).unwrap()).unwrap();
    assert_eq!(
        PathBuf::from(report["source_app"].as_str().unwrap())
            .canonicalize()
            .unwrap(),
        runtime_source()
            .join("src/litetui/app.py")
            .canonicalize()
            .unwrap()
    );
    assert!(report["denied"].as_array().unwrap().is_empty());
    if changed {
        assert_eq!(report["after"]["backend"], target);
        assert_eq!(
            report["before"]["session_id"],
            report["after"]["session_id"]
        );
    }
    if switched {
        assert_eq!(report["after"]["model"], "switch-new");
        assert_eq!(report["requests"].as_array().unwrap().len(), prompts);
        if case == "oversized-history" {
            let complete_large_answer = "X".repeat(2200 * 1024);
            for request in report["requests"].as_array().unwrap() {
                assert!(
                    request["messages"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|row| row["role"] == "assistant"
                            && row["content"].as_str() == Some(complete_large_answer.as_str())),
                    "The entire large saved assistant row must reach both explicit SDK requests"
                );
            }
        }
        if case != "empty" {
            assert!(
                report["requests"].as_array().unwrap().last().unwrap()["messages"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|v| v["content"] == "History before backend switch")
            );
        }
    } else {
        assert!(report["requests"].as_array().unwrap().len() == prompts);
    }
}
#[test]
#[ignore = "controlled actual owned CLI backend switch; dependency Python required"]
fn owned_backend_switch_preserves_draft_history_and_routes_next_explicit_prompt() {
    for (initial, target) in [
        ("custom", "lmstudio"),
        ("custom", "llamacpp"),
        ("claude", "lmstudio"),
    ] {
        scenario(initial, target, "success", Outcome::Switched, false);
    }
}
#[test]
#[ignore = "controlled source stale selection; dependency Python required"]
fn owned_backend_switch_stale_selection_requires_refresh_and_fresh_confirmation() {
    scenario(
        "custom",
        "lmstudio",
        "one-shot-stale",
        Outcome::Switched,
        true,
    );
}
#[test]
#[ignore = "controlled actual held input refusal; dependency Python required"]
fn owned_backend_switch_held_input_refusal_preserves_current_connection() {
    scenario("custom", "lmstudio", "held", Outcome::Refused, false);
}
#[test]
#[ignore = "controlled owned backend failures/readback drift; dependency Python required"]
fn owned_backend_switch_failures_and_contradictory_readback_never_publish_changed() {
    for (case, initial, target) in [
        ("persistence-failed", "custom", "lmstudio"),
        ("policy-drift", "custom", "lmstudio"),
        ("history-drift", "custom", "lmstudio"),
    ] {
        scenario(initial, target, case, Outcome::Closed, false);
    }
}
#[test]
#[ignore = "controlled unanswered backends and SDK cleanup; dependency Python required"]
fn owned_backend_switch_goes_on_as_python_when_the_backend_or_old_cleanup_fails() {
    // App.apply_backend_change: a backend that does not answer leaves the seat on it,
    // disconnected; an old native session's cleanup failure is reported, not fatal.
    scenario(
        "custom",
        "lmstudio",
        "connection-failed",
        Outcome::Disconnected,
        false,
    );
    scenario(
        "custom",
        "llamacpp",
        "llama-dead",
        Outcome::Disconnected,
        false,
    );
    scenario(
        "claude",
        "lmstudio",
        "cleanup-failed",
        Outcome::Switched,
        false,
    );
}
#[test]
#[ignore = "controlled model-naming switch; dependency Python required"]
fn owned_backend_switch_that_names_a_model_is_refused_and_the_session_goes_on() {
    scenario(
        "custom",
        "lmstudio",
        "missing-model",
        Outcome::Refused,
        false,
    );
}

#[test]
#[ignore = "controlled fresh owned pending session; dependency Python required"]
fn owned_backend_switch_accepts_empty_pending_conversation_without_materializing_input() {
    scenario("custom", "lmstudio", "empty", Outcome::Switched, false);
}

#[test]
#[ignore = "controlled oversized owned history; dependency Python required"]
fn large_saved_history_backend_switch_uses_full_context_proof_and_keeps_draft() {
    scenario(
        "custom",
        "lmstudio",
        "oversized-history",
        Outcome::Switched,
        false,
    );
}

#[test]
#[ignore = "actual provider/local startup with controlled metadata and no authentication"]
fn fresh_launch_defaults_are_durable_and_only_explicit_effort_pins_the_invocation() {
    for (backend, explicit, expected) in [
        ("custom", None, "default"),
        ("custom", Some("default"), "default"),
        ("claude", None, "low"),
        ("codex", None, "low"),
    ] {
        let (runtime, root) = launch_with_thinking(backend, backend, "launch-defaults", explicit);
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live(backend.into(), "switch-old".into());
                wait(runtime, &mut app, |event| {
                    matches!(event, Observation::Ready)
                })?;
                Ok(())
            })
            .unwrap();
        let report: Value =
            serde_json::from_slice(&fs::read(root.join("switch-report.json")).unwrap()).unwrap();
        assert_eq!(report["before"]["thinking"], expected);
        assert_eq!(
            report["before"]["cli_thinking"],
            serde_json::json!(explicit)
        );
        let saved: Value =
            serde_json::from_slice(&fs::read(root.join("settings.json")).unwrap()).unwrap();
        assert_eq!(saved["thinking_level"], expected);
        assert_eq!(
            report["launch_arguments"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|arg| *arg == "--thinking-level")
                .count(),
            usize::from(explicit.is_some())
        );
        assert!(report["requests"].as_array().unwrap().is_empty());
        assert!(report["denied"].as_array().unwrap().is_empty());
    }
}
