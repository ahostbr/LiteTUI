#![cfg(windows)]
//! Rust supervisor -> real owned source settings API; external inference is controlled.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    command_palette::{self as commands},
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
fn launch(case: &str, tools: bool) -> (Interactive, PathBuf) {
    let actual = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/command-skill-process-tests")
        .join(format!(
            "{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    fs::create_dir_all(base.join("workspace")).unwrap();
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
os.environ['RUST_COMMAND_SKILL_SOURCE'] = {}
os.environ['RUST_COMMAND_SKILL_CASE'] = {}
os.environ['RUST_COMMAND_SKILL_DRIVER'] = 'rpc'
os.environ['RUST_COMMAND_SKILL_UNUSED_FIELD'] = {}
os.environ['RUST_COMMAND_SKILL_UNUSED_VALUE'] = '257'
os.environ.pop('LITETUI_MAX_TOKENS', None)
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=True, skill_roots=[], footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=8192, max_tokens_chat=101, max_tokens_tools=202)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(case),quote(if tools{"max_tokens_tools"}else{"max_tokens_chat"}),quote(&actual.join("rust/litetui-terminal/tests/fixtures/command_skill_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
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
        "--timeout".into(),
        "30".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    if tools {
        args.extend([
            "--tools".into(),
            "on".into(),
            "--workspace".into(),
            base.join("workspace").to_string_lossy().into_owned(),
        ]);
    }
    if case == "cli-override" {
        args.extend(["--max-tokens".into(), "307".into()]);
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
                        .unwrap_or_else(|| io::Error::other("Unexpected clean exit")));
                }
                if Instant::now() > until {
                    return Err(io::Error::other("Command observation timeout"));
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
fn wait_command(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| matches!(e, Observation::Commands(_)))
}
fn draw(app: &mut Demo) {
    let mut t = Terminal::new(TestBackend::new(100, 32)).unwrap();
    t.draw(|f| ui::draw(f, app)).unwrap();
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .commands
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn send(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.commands(
        app.live
            .as_mut()
            .unwrap()
            .commands
            .take_intent()
            .expect("explicit command intent"),
    )
}
fn open_skill(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_commands();
    send(runtime, app)?;
    wait_command(runtime, app)?;
    draw(app);
    key(app, KeyCode::Tab);
    draw(app);
    app.live.as_mut().unwrap().commands.paste("ExactSkill");
    draw(app);
    key(app, KeyCode::Enter);
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .commands
        .paste("User Δ arguments\r\nKeep exact trailing whitespace  ");
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    wait_command(runtime, app)
}
fn review_and_send(runtime: &mut Interactive, app: &mut Demo, cancel: bool) -> io::Result<()> {
    for _ in 0..50 {
        draw(app);
        key(app, KeyCode::PageDown);
    }
    draw(app);
    key(app, KeyCode::Enter);
    send(runtime, app)?;
    if cancel {
        key(app, KeyCode::Esc);
    }
    wait_command(runtime, app)
}
fn commands_log(root: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(root.join("command-skill-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|s| serde_json::from_str(s).unwrap())
        .collect()
}
fn report(root: &std::path::Path) -> Value {
    serde_json::from_str(&fs::read_to_string(root.join("command-skill-report.json")).unwrap())
        .unwrap()
}
fn skill_scenario(case: &str, tools: bool) {
    let (runtime, root) = launch(case, tools);
    let fatal = [
        "foreign-prepared",
        "digest-drift",
        "prepared-snapshot-drift",
        "policy-drift",
    ]
    .contains(&case);
    let refuse = [
        "invalid-utf8",
        "oversized",
        "binding-expansion",
        "display-budget",
        "encoded-budget",
        "file-drift",
    ]
    .contains(&case);
    let cancel = case == "cancel" || case == "cancel-verify";
    let recovery = ["display-budget", "encoded-budget"].contains(&case);
    let mut expected = None;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        app.insert_text("PRESERVE ORIGINAL DRAFT");
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        let rows = app.live.as_ref().unwrap().rows.len();
        open_skill(runtime, &mut app)?;
        if case == "one-shot-stale" {
            assert!(app.live.as_ref().unwrap().commands.prepared.is_none());
            let args = app.live.as_ref().unwrap().commands.arguments().to_owned();
            draw(&mut app);
            key(&mut app, KeyCode::F(5));
            send(runtime, &mut app)?;
            wait_command(runtime, &mut app)?;
            assert_eq!(app.live.as_ref().unwrap().commands.arguments(), args);
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait_command(runtime, &mut app)?;
        }
        assert_eq!(app.composer.text(), "PRESERVE ORIGINAL DRAFT");
        assert_eq!(app.live.as_ref().unwrap().rows.len(), rows);
        assert!(
            !fatal,
            "Foreign/malformed prepared response must fail closed"
        );
        if refuse {
            assert!(app.live.as_ref().unwrap().commands.prepared.is_none());
            key(&mut app, KeyCode::Esc);
            assert!(!app.has_live_commands());
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
            if recovery {
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("Explicit ordinary prompt after refused skill")?;
                runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
                wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
                assert_eq!(app.composer.text(), "PRESERVE ORIGINAL DRAFT");
            }
            return Ok(());
        }
        let p = app
            .live
            .as_ref()
            .unwrap()
            .commands
            .prepared
            .as_ref()
            .expect("prepared source skill")
            .clone();
        assert!(p.body.contains("---\r\nname: ExactSkill\r\n"));
        assert!(p.prompt.ends_with("Keep exact trailing whitespace  "));
        expected = Some(p.prompt.clone());
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        if case == "cancel" {
            key(&mut app, KeyCode::Esc);
        } else {
            review_and_send(runtime, &mut app, case == "cancel-verify")?;
        }
        if cancel {
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
            assert!(!app.has_live_commands());
        } else {
            let prompt = app
                .live
                .as_mut()
                .unwrap()
                .take_submission()
                .expect("explicit normal attended submission");
            assert_eq!(prompt, p.prompt);
            runtime.submit(prompt)?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
        }
        assert_eq!(app.composer.text(), "PRESERVE ORIGINAL DRAFT");
        Ok(())
    });
    fs::write(
        root.parent().unwrap().join("native-result.txt"),
        format!("{result:?}"),
    )
    .unwrap();
    assert_eq!(
        result.is_err(),
        fatal,
        "{case}: {result:?} at {}",
        root.display()
    );
    let sent = commands_log(&root);
    let prompts: Vec<_> = sent.iter().filter(|c| c["type"] == "prompt").collect();
    let should_submit = !fatal && !refuse && !cancel;
    assert_eq!(prompts.len(), usize::from(should_submit || recovery));
    let r = report(&root);
    assert_eq!(r["callbacks"], serde_json::json!([]));
    assert_eq!(r["denied"], serde_json::json!([]));
    let requests = r["requests"].as_array().unwrap();
    assert_eq!(requests.len(), usize::from(should_submit || recovery));
    if should_submit {
        let exact = expected.unwrap();
        assert_eq!(prompts[0]["message"], exact);
        assert!(
            requests[0]["messages"]
                .as_array()
                .unwrap()
                .iter()
                .any(|m| m["role"] == "user" && m["content"] == exact)
        );
        assert_eq!(requests[0]["profile"], "strict");
        assert!(
            r["events"]
                .as_array()
                .unwrap()
                .iter()
                .any(|e| e["type"] == "gui.event"
                    && e["operation_id"] == prompts[0]["id"]
                    && !e["execution_id"].is_null())
        );
    }
    if (refuse || cancel) && !recovery {
        assert_eq!(r["before"], r["after"]);
        assert_eq!(r["initial_disk"], r["final_disk"]);
    }
}
fn route_scenario(case: &str, raw: &str, action: &str) {
    let (runtime, root) = launch(case, false);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text(raw);
        app.submit();
        send(runtime, &mut app)?;
        if case == "cancel-route" {
            key(&mut app, KeyCode::Esc);
        }
        wait_command(runtime, &mut app)?;
        assert_eq!(app.composer.text(), raw);
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        if case == "large-raw" {
            assert!(app.has_live_commands());
            app.live.as_mut().unwrap().commands.close();
            app.live
                .as_mut()
                .unwrap()
                .submit("Explicit ordinary prompt after large raw skill refusal")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            assert_eq!(app.composer.text(), raw);
            return Ok(());
        }
        if case == "cancel-route" {
            assert!(!app.command_quit);
            assert!(!app.has_live_input());
        } else {
            match action {
                "settings" => assert!(app.has_live_settings()),
                "models" | "thinking" => assert!(app.has_live_controls()),
                "backend" => assert!(app.has_live_backend()),
                "conversations" => assert!(app.has_live_browser()),
                "quit" => assert!(app.command_quit),
                "skills" => assert!(app.has_live_commands()),
                "refused" => assert!(app.has_live_commands()),
                _ => panic!(),
            }
        }
        Ok(())
    });
    assert!(
        result.is_ok(),
        "{case} {raw}: {result:?} at {}",
        root.display()
    );
    let sent = commands_log(&root);
    assert_eq!(
        sent.iter().filter(|c| c["type"] == "prompt").count(),
        usize::from(case == "large-raw")
    );
    assert!(
        sent.iter()
            .any(|c| c["type"] == commands::RESOLVE && c["command"] == raw)
    );
    let r = report(&root);
    assert_eq!(r["callbacks"], serde_json::json!([]));
    assert_eq!(
        r["requests"].as_array().unwrap().len(),
        usize::from(case == "large-raw")
    );
    assert_eq!(r["denied"], serde_json::json!([]));
}
#[test]
#[ignore = "controlled real source runtime"]
fn exact_prepared_skill_reaches_attended_chat_and_tool_requests() {
    skill_scenario("success", false);
    skill_scenario("success", true);
}
#[test]
#[ignore = "controlled real source runtime"]
fn prepared_cancel_and_stale_retry_never_auto_submit() {
    for case in ["cancel", "cancel-verify", "one-shot-stale"] {
        skill_scenario(case, false);
    }
}
#[test]
#[ignore = "controlled real source runtime"]
fn malformed_prepared_or_policy_readback_closes_before_prompt() {
    for case in [
        "foreign-prepared",
        "digest-drift",
        "prepared-snapshot-drift",
        "policy-drift",
    ] {
        skill_scenario(case, false);
    }
}
#[test]
#[ignore = "controlled real source runtime"]
fn unsupported_skill_data_refuses_without_replacing_draft() {
    for case in [
        "invalid-utf8",
        "oversized",
        "binding-expansion",
        "display-budget",
        "encoded-budget",
        "file-drift",
    ] {
        skill_scenario(case, false);
    }
}
#[test]
#[ignore = "controlled real source runtime"]
fn raw_aliases_route_only_to_verified_native_controls() {
    for (raw, action) in [
        (" /CONFIG   ", "settings"),
        ("/models", "models"),
        ("/thinking", "thinking"),
        ("/backend", "backend"),
        ("/conversations", "conversations"),
        ("/skill", "skills"),
        ("/exit", "quit"),
    ] {
        route_scenario("success", raw, action);
    }
}
#[test]
#[ignore = "controlled real source runtime"]
fn raw_unknown_unsupported_arguments_and_cancel_never_fall_back() {
    for raw in ["/not-a-command", "/hooks", "/settings extra", "/caseskill"] {
        route_scenario("success", raw, "refused");
    }
    route_scenario("cancel-route", "/exit", "quit");
    route_scenario(
        "large-raw",
        &format!("/ExactSkill {}", "😀".repeat(16372)),
        "refused",
    );
}
