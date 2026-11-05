#![cfg(windows)]
//! Actual source CLI/ownership/storage with external provider boundaries controlled.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_actions::Action,
    conversation_browser,
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
fn action_for(case: &str) -> Action {
    if case.starts_with("new-")
        || case.starts_with("hooks-")
        || matches!(
            case,
            "claude-new"
                | "codex-new"
                | "codex-correction"
                | "claude-cleanup-failed"
                | "quit-during-apply"
        )
    {
        Action::New
    } else {
        Action::Rename
    }
}
fn backend_for(case: &str) -> &'static str {
    if case.starts_with("claude-") {
        "claude"
    } else if case.starts_with("codex-") {
        "codex"
    } else {
        "custom"
    }
}
fn model_for(case: &str) -> &'static str {
    if backend_for(case) == "codex" {
        "gpt-6.1-sol"
    } else {
        "switch-old"
    }
}
fn native(case: &str) -> bool {
    matches!(backend_for(case), "claude" | "codex")
}
fn runtime_source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}

fn launch(case: &str) -> (Interactive, PathBuf) {
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let actual = runtime_source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/conversation-process-tests")
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
os.environ['RUST_SESSION_MUTATION_SOURCE'] = {}
os.environ['RUST_SESSION_MUTATION_CASE'] = {}
os.environ['RUST_SESSION_MUTATION_DRIVER'] = 'rpc'
os.environ['RUST_SESSION_MUTATION_ACTION'] = {}
os.environ.pop('LITETUI_MAX_TOKENS', None)
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, max_tokens_chat=101, max_tokens_tools=202, custom_context_length=8192)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(case),quote(action_for(case).as_str()),quote(&crate_root.join("tests/fixtures/session_mutation_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        backend_for(case).into(),
        "--model".into(),
        model_for(case).into(),
        "--tool-profile".into(),
        "strict".into(),
        "--thinking-level".into(),
        if backend_for(case) == "codex" {
            "high"
        } else {
            "default"
        }
        .into(),
        "--timeout".into(),
        "30".into(),
        "--server-mode".into(),
        "connect".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
    ];
    if native(case) {
        args.truncate(args.len() - 4);
    }
    if backend_for(case) == "codex" {
        args.extend(["--codex-engine".into(), "native".into()]);
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let until = Instant::now() + Duration::from_secs(40);
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
                    return Err(io::Error::other("Conversation observation timeout"));
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
        let e = next(runtime)?;
        let done = predicate(&e);
        app.observe_live(e)?;
        if done {
            return Ok(());
        }
    }
}
fn wait_action(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Browser(conversation_browser::Update::Action(_))
        )
    })
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.browser(
        app.live
            .as_mut()
            .unwrap()
            .browser
            .take_intent()
            .expect("explicit browser intent"),
    )
}
fn key(app: &mut Demo, k: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .browser
        .key(KeyEvent::new(k, KeyModifiers::NONE));
}
fn draw(app: &mut Demo) {
    let mut t = Terminal::new(TestBackend::new(100, 32)).unwrap();
    t.draw(|f| ui::draw(f, app)).unwrap();
}
fn start(runtime: &mut Interactive, app: &mut Demo, action: Action) -> io::Result<()> {
    app.open_browser();
    send(runtime, app)?;
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Browser(conversation_browser::Update::Page(_))
        )
    })?;
    draw(app);
    key(
        app,
        if action == Action::New {
            KeyCode::Char('n')
        } else {
            KeyCode::Char('e')
        },
    );
    send(runtime, app)?;
    wait_action(runtime, app)?;
    draw(app);
    if action == Action::Rename {
        for _ in 0..4096 {
            key(app, KeyCode::Backspace);
        }
        app.live
            .as_mut()
            .unwrap()
            .browser
            .paste("  Renamed  雪\ncurrent\tconversation  ");
    }
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)
}
fn confirm(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    for _ in 0..100 {
        draw(app);
        key(app, KeyCode::PageDown);
    }
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)
}
fn report(root: &Path) -> Value {
    let r: Value = serde_json::from_str(
        &fs::read_to_string(root.join("session-mutation-fresh-report.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(
        Path::new(r["source_app"].as_str().unwrap())
            .canonicalize()
            .unwrap(),
        runtime_source().join("src/litetui/app.py")
    );
    assert_eq!(r["pending_after_teardown"], false);
    r
}
fn commands(root: &Path) -> Vec<Value> {
    fs::read_to_string(root.join("session-mutation-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
fn scenario(case: &str) {
    let action = action_for(case);
    let (runtime, root) = launch(case);
    let fatal = matches!(
        case,
        "write-before"
            | "write-after"
            | "title-drift"
            | "policy-drift"
            | "history-drift"
            | "session-drift"
            | "generic-apply-error"
            | "hooks-failed-leave"
            | "hooks-failed-start"
            | "hooks-approval"
            | "hooks-drift"
            | "claude-cleanup-failed"
    );
    let cancel = case == "cancel-review";
    let outcome = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live(backend_for(case).into(), model_for(case).into());
        app.insert_text("KEEP ORIGINAL COMPOSER");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        if native(case) && case != "codex-correction" {
            app.live
                .as_mut()
                .unwrap()
                .submit("Explicit old native conversation prompt")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        }
        start(runtime, &mut app, action)?;
        if cancel {
            key(&mut app, KeyCode::Esc);
            wait_action(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
            runtime.quit()?;
            return Ok(());
        }
        wait_action(runtime, &mut app)?;
        confirm(runtime, &mut app)?;
        if case == "one-shot-stale" {
            wait_action(runtime, &mut app)?;
            assert_eq!(app.composer.text(), "KEEP ORIGINAL COMPOSER");
            assert_eq!(
                app.live.as_ref().unwrap().browser.actions.draft(),
                "  Renamed  雪\ncurrent\tconversation  "
            );
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
            key(&mut app, KeyCode::F(5));
            send(runtime, &mut app)?;
            wait_action(runtime, &mut app)?;
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_action(runtime, &mut app)?;
            confirm(runtime, &mut app)?;
        }
        let result = wait(runtime, &mut app, |e| {
            matches!(e, Observation::ConversationChanged(_))
        });
        if fatal {
            assert!(result.is_err());
            return result;
        }
        result?;
        assert_eq!(app.composer.text(), "KEEP ORIGINAL COMPOSER");
        if app.has_live_browser() {
            key(&mut app, KeyCode::Esc);
        }
        app.live
            .as_mut()
            .unwrap()
            .submit("Explicit prompt after verified conversation action")?;
        runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        runtime.quit()?;
        Ok(())
    });
    if fatal {
        assert!(outcome.is_err(), "{case}");
    } else {
        outcome.unwrap_or_else(|e| panic!("{case}: {e}; root {}", root.display()));
    }
    let r = report(&root);
    assert!(r["denied"].as_array().unwrap().is_empty(), "{r}");
    assert_eq!(
        r["store_owned_after_teardown"],
        case == "claude-cleanup-failed"
    );
    if case == "hooks-success" {
        let executed: Vec<_> = r["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["effect"] == "hook")
            .map(|row| {
                (
                    &row["document"]["event"],
                    &row["document"]["conversation_id"],
                )
            })
            .collect();
        assert_eq!(executed.len(), 2);
        assert_eq!(executed[0].0, "conversation_leave");
        assert_eq!(executed[0].1, &r["before"]["session_id"]);
        assert_eq!(executed[1].0, "conversation_start");
        assert_eq!(executed[1].1, &r["after"]["session_id"]);
    }
    if case == "hooks-approval" {
        assert!(r["hook_process_codes"].as_array().unwrap().is_empty());
        assert!(
            r["proof"]["hook_results"]
                .as_object()
                .unwrap()
                .values()
                .any(|result| result["allowed"] == false)
        );
    }
    let log = commands(&root);
    assert_eq!(
        log.iter().filter(|c| c["type"] == "prompt").count(),
        usize::from(!fatal && !cancel) + usize::from(native(case) && case != "codex-correction")
    );
    if cancel {
        assert!(
            !log.iter()
                .any(|c| c["type"] == litetui_terminal::conversation_actions::APPLY)
        );
    } else if !fatal {
        let before = r["before"]["session"]
            .as_str()
            .or_else(|| r["before"]["session_id"].as_str())
            .unwrap();
        let after = r["after"]["session"]
            .as_str()
            .or_else(|| r["after"]["session_id"].as_str())
            .unwrap();
        assert_eq!(before == after, action == Action::Rename);
        let requests = r["requests"].as_array().unwrap();
        assert_eq!(requests.len(), 1 + usize::from(native(case)));
        assert_eq!(requests.last().unwrap()["session_id"], after);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn rename_and_new_verify_saved_and_pending_owned_sessions() {
    for case in ["rename-saved", "rename-pending", "new-saved", "new-pending"] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn escape_during_review_never_applies_after_delayed_reply() {
    scenario("cancel-review");
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn ambiguous_mutation_and_contradictory_readback_close_without_prompt() {
    for case in [
        "write-before",
        "write-after",
        "title-drift",
        "policy-drift",
        "history-drift",
        "session-drift",
        "generic-apply-error",
    ] {
        scenario(case);
    }
}

#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn stale_review_requires_explicit_refresh_and_new_confirmation() {
    scenario("one-shot-stale");
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn new_waits_for_hooks_and_never_treats_confirmation_as_shell_approval() {
    for case in [
        "hooks-success",
        "hooks-failed-leave",
        "hooks-failed-start",
        "hooks-approval",
        "hooks-drift",
    ] {
        scenario(case);
    }
}
#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn actual_claude_session_is_preserved_for_rename_and_retired_for_new() {
    for case in ["claude-new", "claude-rename", "claude-cleanup-failed"] {
        scenario(case);
    }
}

#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn quit_during_new_joins_hook_and_never_publishes_completion_or_follows_readbacks() {
    let (runtime, root) = launch("quit-during-apply");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            app.insert_text("KEEP ORIGINAL COMPOSER");
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            start(runtime, &mut app, Action::New)?;
            wait_action(runtime, &mut app)?;
            confirm(runtime, &mut app)?;
            let until = Instant::now() + Duration::from_secs(10);
            while !root.join("session-mutation-hook-entered").exists() {
                if Instant::now() > until {
                    return Err(io::Error::other("Hook did not enter"));
                }
                if let Some(done) = runtime.poll_done() {
                    return done;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            runtime.quit()?;
            loop {
                match runtime.events.recv_timeout(Duration::from_millis(100)) {
                    Ok(Observation::ConversationChanged(_)) => {
                        panic!("Late mutation published after quit")
                    }
                    Ok(e) => app.observe_live(e)?,
                    Err(_) => {
                        if let Some(done) = runtime.poll_done() {
                            done?;
                            break;
                        }
                    }
                }
                if Instant::now() > until + Duration::from_secs(20) {
                    return Err(io::Error::other("Quit did not settle"));
                }
            }
            assert_eq!(app.composer.text(), "KEEP ORIGINAL COMPOSER");
            assert!(
                app.live
                    .as_ref()
                    .unwrap()
                    .conversation_exit_notice
                    .as_ref()
                    .is_some_and(
                        |s| s.contains("saved effects remain unverified") && s.contains("Reopen")
                    )
            );
            Ok(())
        })
        .unwrap_or_else(|e| panic!("{e}; root {}", root.display()));
    let log = commands(&root);
    let at = log
        .iter()
        .position(|v| v["type"] == litetui_terminal::conversation_actions::APPLY)
        .unwrap();
    assert!(log[at + 1..].iter().all(|v| v["type"] == "shutdown"));
    let r = report(&root);
    assert_eq!(r["shutdown"]["pending_at_close"], true);
    for field in [
        "worker_settled",
        "lifecycle_settled",
        "native_cleanup_settled",
    ] {
        assert_eq!(r["shutdown"][field], true);
    }
    assert_eq!(r["shutdown"]["effects_verified"], false);
    assert_eq!(r["store_owned_after_teardown"], false);
    assert_eq!(r["hook_process_codes"], serde_json::json!([-15]));
    let events = r["events"].as_array().unwrap();
    let pos = |kind: &str| events.iter().position(|e| e["effect"] == kind).unwrap();
    assert!(pos("hook-kill") < pos("shutdown-ack") && pos("shutdown-ack") < pos("lease-release"));
    assert!(r["requests"].as_array().unwrap().is_empty());
    assert!(r["denied"].as_array().unwrap().is_empty());
}

#[test]
#[ignore = "controlled actual source subprocess; requires dependency Python"]
fn actual_codex_thread_is_preserved_for_rename_and_new_starts_fresh_only_on_prompt() {
    for case in ["codex-new", "codex-rename", "codex-correction"] {
        scenario(case);
    }
}
