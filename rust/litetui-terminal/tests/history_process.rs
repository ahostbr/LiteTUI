#![cfg(windows)]
//! Native supervisor -> actual source replay, with provider/OS boundaries controlled.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_browser::Update as BrowserUpdate,
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
fn runtime_source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn launch(case: &str) -> (Interactive, PathBuf) {
    let fixture_source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../..")
        .canonicalize()
        .unwrap();
    let source = runtime_source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/history-process-tests")
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
            quote(&source.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    fs::write(package.join("cli.py"),format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_HISTORY_SOURCE'] = {}
os.environ['RUST_HISTORY_CASE'] = {}
os.environ['RUST_HISTORY_PHASE'] = 'fresh'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000, max_tokens_chat=101, max_tokens_tools=202)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&source.to_string_lossy()),quote(case),quote(&fixture_source.join("rust/litetui-terminal/tests/fixtures/native_history_runtime.py").to_string_lossy()))).unwrap();
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
        "--timeout".into(),
        "30".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
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
                    return Err(io::Error::other("History observation timeout"));
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
fn draw(app: &mut Demo, w: u16, h: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn key(app: &mut Demo, k: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .saved_history
        .key(KeyEvent::new(k, KeyModifiers::NONE));
}
fn browser_key(app: &mut Demo, k: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .browser
        .key(KeyEvent::new(k, KeyModifiers::NONE));
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.history(
        app.live
            .as_mut()
            .unwrap()
            .saved_history
            .take_intent()
            .expect("explicit history intent"),
    )
}
fn send_browser(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.browser(
        app.live
            .as_mut()
            .unwrap()
            .browser
            .take_intent()
            .expect("explicit browser intent"),
    )
}
fn wait_history(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| matches!(e, Observation::SavedHistory(_)))
}
fn report(root: &Path) -> Value {
    let report: Value =
        serde_json::from_str(&fs::read_to_string(root.join("history-fresh-report.json")).unwrap())
            .unwrap();
    assert_eq!(
        PathBuf::from(report["source_app"].as_str().unwrap())
            .canonicalize()
            .unwrap(),
        runtime_source()
            .join("src/litetui/app.py")
            .canonicalize()
            .unwrap()
    );
    report
}
fn assert_inert(report: &Value) {
    for request in report["requests"].as_array().unwrap() {
        assert_eq!(request["complete_prepared_context"], true);
        assert_eq!(request["context_sha256"], request["prepared_sha256"]);
        assert_eq!(request["wire_row_hashes"], request["expected_row_hashes"]);
    }
    assert!(report["denied"].as_array().unwrap().is_empty(), "{report}");
    assert!(report["interactive_events"].as_array().unwrap().is_empty());
    assert!(
        report["observations"]
            .as_array()
            .unwrap()
            .iter()
            .all(|v| v["wire_bytes"].as_u64().unwrap() <= 1_048_576)
    );
    assert!(
        !report["commands"]
            .as_array()
            .unwrap()
            .iter()
            .any(|v| v["type"] == "gui.conversations.read")
    );
}
#[test]
#[ignore = "controlled actual-source child, requires LITETUI_AUTHORITY_PYTHON"]
fn bounded_saved_history_walks_preserve_live_rows_draft_and_full_context() {
    for case in [
        "many",
        "giant",
        "repeated",
        "rich",
        "wire-heavy",
        "astral",
        "mixed",
        "torn",
        "pending",
        "held",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            app.insert_text("KEEP THE UNSENT DRAFT");
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            let saved = app.live.as_ref().unwrap().history.as_ref().unwrap();
            assert!(saved.messages.is_empty());
            let context = saved.descriptor.as_ref().unwrap().clone();
            if case == "many" {
                assert!(context.counts.messages > 1000);
            }
            let live_rows = app.live.as_ref().unwrap().rows.len();
            app.open_history();
            send(runtime, &mut app)?;
            wait_history(runtime, &mut app)?;
            draw(&mut app, 100, 32);
            key(&mut app, KeyCode::Home);
            send(runtime, &mut app)?;
            wait_history(runtime, &mut app)?;
            let mut blocks = std::collections::HashSet::new();
            let mut pages = 0;
            loop {
                let page = app
                    .live
                    .as_ref()
                    .unwrap()
                    .saved_history
                    .page
                    .as_ref()
                    .unwrap()
                    .clone();
                assert!(page.fragments.len() <= 50);
                pages += 1;
                assert!(pages < 2000);
                for fragment in &page.fragments {
                    blocks.insert(fragment.block_id.clone());
                }
                for (w, h) in [(100, 32), (42, 18), (36, 14)] {
                    assert!(draw(&mut app, w, h).contains("Esc"));
                }
                if page.next.is_none() {
                    break;
                }
                draw(&mut app, 100, 32);
                key(&mut app, KeyCode::Right);
                send(runtime, &mut app)?;
                wait_history(runtime, &mut app)?;
            }
            assert_eq!(blocks.len() as u64, context.counts.blocks);
            let tail = app.live.as_ref().unwrap().saved_history.page.clone();
            if tail.as_ref().unwrap().previous.is_some() {
                draw(&mut app, 100, 32);
                key(&mut app, KeyCode::Left);
                send(runtime, &mut app)?;
                wait_history(runtime, &mut app)?;
                draw(&mut app, 100, 32);
                key(&mut app, KeyCode::Right);
                send(runtime, &mut app)?;
                wait_history(runtime, &mut app)?;
                assert_eq!(app.live.as_ref().unwrap().saved_history.page, tail);
            }
            if case == "many" {
                assert!(pages > 20);
            }
            assert_eq!(app.live.as_ref().unwrap().rows.len(), live_rows);
            key(&mut app, KeyCode::Esc);
            assert!(!app.has_live_history());
            assert_eq!(app.composer.text(), "KEEP THE UNSENT DRAFT");
            if case == "many" {
                let chat = app.live.as_mut().unwrap();
                chat.submit("Explicit prompt after saved history inspection")?;
                runtime.submit(chat.take_submission().unwrap())?;
                wait(runtime, &mut app, |e| matches!(e, Observation::Complete))?;
            }
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?}");
        let report = report(&root);
        assert_inert(&report);
        assert_eq!(
            report["requests"].as_array().unwrap().len(),
            usize::from(case == "many")
        );
        if case != "many" {
            assert_eq!(report["before"], report["after"]);
            assert_eq!(report["initial_disk"], report["final_disk"]);
        }
    }
}
#[test]
#[ignore = "controlled actual-source child, requires LITETUI_AUTHORITY_PYTHON"]
fn stale_and_cancelled_pages_keep_visible_history_and_never_reopen() {
    for case in ["one-shot-stale", "cancel-page"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            app.insert_text("PRESERVE DRAFT");
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.open_history();
            send(runtime, &mut app)?;
            wait_history(runtime, &mut app)?;
            let prior = app.live.as_ref().unwrap().saved_history.page.clone();
            draw(&mut app, 100, 32);
            key(&mut app, KeyCode::Left);
            send(runtime, &mut app)?;
            if case == "cancel-page" {
                key(&mut app, KeyCode::Esc);
                assert!(app.has_live_history());
            }
            wait_history(runtime, &mut app)?;
            assert_eq!(app.live.as_ref().unwrap().saved_history.page, prior);
            if case == "cancel-page" {
                assert!(!app.has_live_history());
            } else {
                assert!(app.live.as_ref().unwrap().saved_history.is_stale());
                draw(&mut app, 100, 32);
                key(&mut app, KeyCode::Left);
                assert!(
                    app.live
                        .as_mut()
                        .unwrap()
                        .saved_history
                        .take_intent()
                        .is_none()
                );
                key(&mut app, KeyCode::F(5));
                send(runtime, &mut app)?;
                wait_history(runtime, &mut app)?;
                assert!(!app.live.as_ref().unwrap().saved_history.is_stale());
            }
            assert_eq!(app.composer.text(), "PRESERVE DRAFT");
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?}");
        let report = report(&root);
        assert_inert(&report);
        assert!(report["requests"].as_array().unwrap().is_empty());
    }
}
#[test]
#[ignore = "controlled actual-source child, requires LITETUI_AUTHORITY_PYTHON"]
fn contradictory_projection_never_enables_the_composer() {
    for case in [
        "descriptor-drift",
        "fragment-drift",
        "span-drift",
        "order-drift",
        "cursor-loop",
        "source-budget",
    ] {
        let (runtime, root) = launch(case);
        let mut ready = false;
        let result = runtime.with_cleanup(|runtime| {
            loop {
                if matches!(next(runtime)?, Observation::Ready) {
                    ready = true;
                    return Ok(());
                }
            }
        });
        assert!(result.is_err() && !ready, "{case}: {result:?}");
        let report = report(&root);
        assert!(report["requests"].as_array().unwrap().is_empty());
    }
}
fn preview(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_browser();
    send_browser(runtime, app)?;
    wait(runtime, app, |e| {
        matches!(e, Observation::Browser(BrowserUpdate::Page(_)))
    })?;
    let index = app
        .live
        .as_ref()
        .unwrap()
        .browser
        .page
        .as_ref()
        .unwrap()
        .entries
        .iter()
        .position(|e| !e.current)
        .unwrap();
    draw(app, 100, 32);
    browser_key(app, KeyCode::Home);
    for _ in 0..index {
        browser_key(app, KeyCode::Down);
    }
    browser_key(app, KeyCode::Char('o'));
    draw(app, 100, 32);
    browser_key(app, KeyCode::Enter);
    send_browser(runtime, app)?;
    wait(runtime, app, |e| {
        matches!(e, Observation::Browser(BrowserUpdate::Preview(_)))
    })
}
fn confirm(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    browser_key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().browser.take_intent().is_none(),
        "fresh visible review required"
    );
    for _ in 0..200 {
        let screen = draw(app, 58, 20);
        if screen.contains("Enter: open once") {
            browser_key(app, KeyCode::Enter);
            return send_browser(runtime, app);
        }
        browser_key(app, KeyCode::PageDown);
    }
    Err(io::Error::other("Open review pagination did not finish"))
}
#[test]
#[ignore = "controlled actual-source child, requires LITETUI_AUTHORITY_PYTHON"]
fn compact_open_waits_for_explicit_review_and_independent_full_context_proof() {
    for case in [
        "preview-open",
        "hooks-success",
        "hooks-approval",
        "hooks-drift",
        "open-readback-drift",
        "hooks-failed-leave",
        "hooks-failed-resume",
    ] {
        let (runtime, root) = launch(case);
        let mut opened = false;
        let fatal = [
            "hooks-approval",
            "open-readback-drift",
            "hooks-failed-leave",
            "hooks-failed-resume",
        ]
        .contains(&case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            app.insert_text("DRAFT ACROSS OPEN");
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            preview(runtime, &mut app)?;
            confirm(runtime, &mut app)?;
            wait(runtime, &mut app, |e| {
                matches!(
                    e,
                    Observation::Opened(_) | Observation::Browser(BrowserUpdate::Rejected(_))
                )
            })?;
            opened = !app.live.as_ref().unwrap().browser.open;
            assert_eq!(app.composer.text(), "DRAFT ACROSS OPEN");
            if opened {
                assert!(
                    app.live
                        .as_ref()
                        .unwrap()
                        .saved_history
                        .snapshot
                        .as_ref()
                        .unwrap()
                        .counts
                        .messages
                        > 1000
                );
            }
            Ok(())
        });
        assert_eq!(result.is_err(), fatal, "{case}: {result:?}");
        assert_eq!(opened, !fatal && case != "hooks-drift", "{case}");
        let report = report(&root);
        assert_inert(&report);
        assert!(report["requests"].as_array().unwrap().is_empty());
        assert_eq!(
            report["commands"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|v| v["type"] == "gui.conversations.native_open")
                .count(),
            1
        );
    }
}
#[test]
#[ignore = "controlled actual-source child, requires LITETUI_AUTHORITY_PYTHON"]
fn cancelled_preview_and_open_shutdown_never_publish_a_connection() {
    for case in ["preview-open", "quit-during-open"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            preview(runtime, &mut app)?;
            if case == "preview-open" {
                browser_key(&mut app, KeyCode::Esc);
                browser_key(&mut app, KeyCode::Enter);
                assert!(app.live.as_mut().unwrap().browser.take_intent().is_none());
                return Ok(());
            }
            confirm(runtime, &mut app)?;
            let until = Instant::now() + Duration::from_secs(5);
            while !root.join("history-hook-entered").exists() {
                if Instant::now() > until {
                    return Err(io::Error::other("Open hook was not entered"));
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            runtime.quit()?;
            loop {
                match runtime.events.recv_timeout(Duration::from_millis(100)) {
                    Ok(Observation::Opened(_)) => panic!("Quit must not publish an opened session"),
                    Ok(_) => {}
                    Err(_) => {
                        if let Some(done) = runtime.poll_done() {
                            return done;
                        }
                    }
                }
            }
        });
        assert!(result.is_ok(), "{case}: {result:?}");
        let report = report(&root);
        assert_inert(&report);
        assert_eq!(
            report["commands"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|v| v["type"] == "gui.conversations.native_open")
                .count(),
            usize::from(case == "quit-during-open")
        );
    }
}
