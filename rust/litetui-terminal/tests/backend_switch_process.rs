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
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "30".into(),
        "--server-timeout".into(),
        "5".into(),
    ];
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
fn select(runtime: &mut Interactive, app: &mut Demo, target: &str) -> io::Result<()> {
    draw(app);
    let view = &mut app.live.as_mut().unwrap().backend_controls;
    let c = view.catalog.as_ref().unwrap();
    let index = c.entries.iter().position(|r| r.name == target).unwrap();
    view.key(KeyEvent::new(KeyCode::Home, KeyModifiers::NONE));
    for _ in 0..index {
        view.key(KeyEvent::new(KeyCode::Down, KeyModifiers::NONE));
    }
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    view.paste("switch-new");
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    assert!(
        view.take_intent().is_none(),
        "confirmation has not been drawn"
    );
    draw(app);
    let view = &mut app.live.as_mut().unwrap().backend_controls;
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let intent = view.take_intent().unwrap();
    assert!(matches!(&intent,Intent::Select{name,model,..} if name==target&&model=="switch-new"));
    runtime.backend(intent)
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
fn scenario(initial: &str, target: &str, case: &str, success: bool, retry: bool) {
    let (runtime, root) = launch(initial, target, case);
    let mut changed = false;
    let mut refused = false;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live(initial.into(), "switch-old".into());
        app.insert_text("UNSENT HUMAN DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        inspect_settings(runtime, &mut app)?;
        if success && initial == "custom" && case != "empty" {
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
        select(runtime, &mut app, target)?;
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
                    select(runtime, &mut app, target)?;
                    continue;
                }
                return Ok(());
            }
            if got_change {
                changed = true;
                let chat = app.live.as_ref().unwrap();
                assert_eq!(chat.backend, target);
                assert_eq!(chat.model, "switch-new");
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
        assert!(success, "Unexpected completed switch: {case}");
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
    if success || ["held", "oversized-history"].contains(&case) {
        assert!(
            result.is_ok(),
            "{case} {initial}->{target}: {result:?} at {}",
            root.display()
        );
    } else {
        assert!(
            result.is_err(),
            "{case} must close after uncertain transition"
        );
    }
    assert_eq!(changed, success);
    if retry || case == "held" {
        assert!(refused);
    }
    let sent = commands(&root);
    let mutations = sent
        .iter()
        .filter(|c| c["type"] == "gui.backend.set")
        .count();
    assert_eq!(mutations, if retry { 2 } else { 1 });
    let prompts = sent.iter().filter(|c| c["type"] == "prompt").count();
    assert_eq!(
        prompts,
        if success && initial == "custom" && case != "empty" {
            2
        } else if success || case == "oversized-history" {
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
    if success {
        assert!(report["denied"].as_array().unwrap().is_empty());
        assert_eq!(report["after"]["backend"], target);
        assert_eq!(
            report["before"]["session_id"],
            report["after"]["session_id"]
        );
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
        scenario(initial, target, "success", true, false);
    }
}
#[test]
#[ignore = "controlled source stale selection; dependency Python required"]
fn owned_backend_switch_stale_selection_requires_refresh_and_fresh_confirmation() {
    scenario("custom", "lmstudio", "one-shot-stale", true, true);
}
#[test]
#[ignore = "controlled actual held input refusal; dependency Python required"]
fn owned_backend_switch_held_input_refusal_preserves_current_connection() {
    scenario("custom", "lmstudio", "held", false, false);
}
#[test]
#[ignore = "controlled owned backend failures/readback drift; dependency Python required"]
fn owned_backend_switch_failures_and_contradictory_readback_never_publish_changed() {
    for (case, initial, target) in [
        ("connection-failed", "custom", "lmstudio"),
        ("persistence-failed", "custom", "lmstudio"),
        ("cleanup-failed", "claude", "lmstudio"),
        ("policy-drift", "custom", "lmstudio"),
        ("history-drift", "custom", "lmstudio"),
        ("missing-model", "custom", "lmstudio"),
        ("llama-dead", "custom", "llamacpp"),
    ] {
        scenario(initial, target, case, false, false);
    }
}

#[test]
#[ignore = "controlled fresh owned pending session; dependency Python required"]
fn owned_backend_switch_accepts_empty_pending_conversation_without_materializing_input() {
    scenario("custom", "lmstudio", "empty", true, false);
}

#[test]
#[ignore = "controlled oversized owned history; dependency Python required"]
fn large_saved_history_backend_switch_uses_full_context_proof_and_keeps_draft() {
    scenario("custom", "lmstudio", "oversized-history", true, false);
}
