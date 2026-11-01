#![cfg(windows)]
//! Actual native F8 inspection against source-owned reads and controlled SDK boundaries.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    conversation_browser::Update as BrowserUpdate,
    demo::Demo,
    history::Scope,
    history_view::{ReadKey, Target},
    live::{Interactive, Observation, Options},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
const PROMPT: &str = "Explicit original conversation prompt after saved-row inspection";
fn runtime_source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn launch(case: &str) -> (Interactive, PathBuf) {
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let actual = runtime_source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/history-inspection-process-tests")
        .join(format!(
            "{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    let package = base.join("source/src/litetui");
    fs::create_dir_all(&package).unwrap();
    let quote = |value: &str| serde_json::to_string(value).unwrap();
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
os.environ['RUST_HISTORY_SOURCE'] = {}
os.environ['RUST_HISTORY_CASE'] = {}
os.environ['RUST_HISTORY_PHASE'] = 'fresh'
os.environ['RUST_INSPECTION_CASE'] = {}
os.environ['RUST_INSPECTION_DRIVER'] = 'rpc'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000, max_tokens_chat=101, max_tokens_tools=202)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#, quote(&actual.to_string_lossy()),quote(&format!("inspection-{case}")),quote(case),quote(&fixtures.join("history_inspection_runtime.py").to_string_lossy()))).unwrap();
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
        "60".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let deadline = Instant::now() + Duration::from_secs(65);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(50)) {
            Ok(event) => return Ok(event),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done.err().unwrap_or_else(|| {
                        io::Error::other("Unexpected inspection runtime exit")
                    }));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Inspection observation deadline"));
                }
            }
        }
    }
}
fn wait(
    runtime: &mut Interactive,
    app: &mut Demo,
    ready: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    loop {
        let event = next(runtime)?;
        let done = ready(&event);
        app.observe_live(event)?;
        if done {
            return Ok(());
        }
    }
}
fn wait_history(runtime: &mut Interactive, app: &mut Demo, key: &ReadKey) -> io::Result<()> {
    wait(
        runtime,
        app,
        |event| matches!(event,Observation::SavedHistory(update) if update.key()==key),
    )
}
fn draw(app: &mut Demo, w: u16, h: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}
fn assert_inspector_surface(app: &mut Demo) {
    // At this size the exclusive inspector covers the full frame. Comparing all
    // cells detects any catalogue drawn over its body, even if its title survives.
    let actual = draw(app, 100, 32);
    let theme = app.palette().clone();
    let mut terminal = Terminal::new(TestBackend::new(100, 32)).unwrap();
    terminal
        .draw(|frame| {
            app.live
                .as_mut()
                .unwrap()
                .saved_history
                .draw(frame, frame.area(), &theme)
        })
        .unwrap();
    let expected: String = terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert_eq!(
        actual, expected,
        "The actual UI must show the full exclusive inspector"
    );
}
fn browser_key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .browser
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn history_key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .saved_history
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn route(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    // This is the same helper used by the actual main loop. Inspect never enters Open review.
    if let Some(intent) = app.take_browser_intent()? {
        runtime.browser(intent)?;
    }
    if let Some(intent) = app.live.as_mut().unwrap().saved_history.take_intent() {
        runtime.history(intent)?;
    }
    Ok(())
}
fn editor(app: &Demo) -> (String, String, String) {
    (
        app.composer.text().into(),
        app.composer.before_cursor().into(),
        app.composer.selected_text().into(),
    )
}
fn start(runtime: &mut Interactive) -> io::Result<Demo> {
    let mut app = Demo::live("custom".into(), "switch-old".into());
    app.insert_text("KEEP α 雪 THE COMPOSER DRAFT");
    app.composer.move_cursor(Motion::WordLeft, true);
    wait(runtime, &mut app, |event| {
        matches!(event, Observation::Ready)
    })?;
    Ok(app)
}
fn identity(root: &Path) -> Value {
    serde_json::from_slice(&fs::read(root.join("inspection-identity.json")).unwrap()).unwrap()
}
fn inspect(runtime: &mut Interactive, app: &mut Demo, session: &str) -> io::Result<ReadKey> {
    app.open_browser();
    route(runtime, app)?;
    wait(runtime, app, |event| {
        matches!(event, Observation::Browser(BrowserUpdate::Page(_)))
    })?;
    for _ in 0..201 {
        let page = app.live.as_ref().unwrap().browser.page.as_ref().unwrap();
        if let Some(index) = page
            .entries
            .iter()
            .position(|entry| entry.session == session)
        {
            draw(app, 100, 32);
            browser_key(app, KeyCode::Home);
            for _ in 0..index {
                browser_key(app, KeyCode::Down);
            }
            assert!(draw(app, 100, 32).contains("I inspect"));
            browser_key(app, KeyCode::Char('i'));
            route(runtime, app)?;
            return app
                .live
                .as_ref()
                .unwrap()
                .saved_history
                .pending_key()
                .cloned()
                .ok_or_else(|| {
                    io::Error::other("Inspect was not routed through the history view")
                });
        }
        if page.next.is_none() {
            break;
        }
        draw(app, 100, 32);
        browser_key(app, KeyCode::PageDown);
        route(runtime, app)?;
        wait(runtime, app, |event| {
            matches!(event, Observation::Browser(BrowserUpdate::Page(_)))
        })?;
    }
    Err(io::Error::other(
        "Exact target was not offered in the catalogue",
    ))
}
fn refresh(runtime: &Interactive, app: &mut Demo) -> io::Result<ReadKey> {
    draw(app, 100, 32);
    history_key(app, KeyCode::F(5));
    let key = app
        .live
        .as_ref()
        .unwrap()
        .saved_history
        .pending_key()
        .cloned()
        .expect("explicit refresh key");
    route(runtime, app)?;
    Ok(key)
}
fn close_views(app: &mut Demo) {
    history_key(app, KeyCode::Esc);
    assert!(!app.has_live_history());
    draw(app, 100, 32);
    browser_key(app, KeyCode::Esc);
    assert!(!app.has_live_browser());
}
fn report(root: &Path, prompts: usize) -> Value {
    let report: Value =
        serde_json::from_slice(&fs::read(root.join("history-fresh-report.json")).unwrap()).unwrap();
    assert_eq!(
        PathBuf::from(report["source_app"].as_str().unwrap())
            .canonicalize()
            .unwrap(),
        runtime_source()
            .join("src/litetui/app.py")
            .canonicalize()
            .unwrap()
    );
    assert!(report["denied"].as_array().unwrap().is_empty(), "{report}");
    assert!(report["interactive_events"].as_array().unwrap().is_empty());
    assert!(report["provider_calls"].as_array().unwrap().is_empty());
    assert!(report["provider_requests"].as_array().unwrap().is_empty());
    assert!(
        report["worker_settlement"]
            .as_array()
            .unwrap()
            .iter()
            .all(|worker| worker["finished"] == true)
    );
    for observation in report["observations"].as_array().unwrap() {
        let limit = if observation["type"] == "gui.conversations.page" {
            256 * 1024
        } else {
            1024 * 1024
        };
        assert!(observation["wire_bytes"].as_u64().unwrap() <= limit);
    }
    let original = report["active_context"].as_array().unwrap();
    let mut roles: Vec<Value> = original.iter().map(|row| row["role"].clone()).collect();
    roles.push(Value::from("user"));
    let mut users: Vec<Value> = original
        .iter()
        .filter(|row| row["role"] == "user")
        .map(|row| row["content"].clone())
        .collect();
    users.push(Value::from(PROMPT));
    assert!(report["read_checks"].as_u64().unwrap() >= 4);
    assert_eq!(report["requests"].as_array().unwrap().len(), prompts);
    for request in report["requests"].as_array().unwrap() {
        assert_eq!(request["complete_prepared_context"], true);
        assert_eq!(request["context_sha256"], request["prepared_sha256"]);
        assert_eq!(request["wire_row_hashes"], request["expected_row_hashes"]);
        assert_eq!(request["wire_roles"], Value::Array(roles.clone()));
        assert_eq!(request["users"], Value::Array(users.clone()));
        assert_eq!(request["session_id"], report["before"]["session_id"]);
        assert_eq!(request["session_id"], report["after"]["session_id"]);
        assert_eq!(request["agent_id"], report["before"]["agent_id"]);
        assert_eq!(request["backend"], "custom");
        assert_eq!(request["model"], "switch-old");
    }
    let commands = report["commands"].as_array().unwrap();
    for command in commands {
        assert!(
            matches!(
                command["type"].as_str().unwrap(),
                "gui.hello"
                    | "gui.state"
                    | "gui.conversations.page"
                    | "gui.history.native_snapshot"
                    | "gui.history.native_page"
                    | "gui.themes.native_snapshot"
                    | "prompt"
                    | "shutdown"
            ),
            "Inspection attempted an action: {command}"
        );
    }
    assert_eq!(
        commands
            .iter()
            .filter(|command| command["type"] == "prompt")
            .count(),
        prompts
    );
    if prompts == 0 {
        assert_eq!(report["before"], report["after"]);
    } else {
        assert_eq!(report["before"], report["before_prompt"]);
    }
    report
}
#[test]
#[ignore = "controlled actual source inspection; dependency Python required"]
fn saved_row_inspection_preserves_active_context_and_editor_before_explicit_prompt() {
    for case in [
        "current",
        "saved",
        "readonly",
        "incompatible",
        "large",
        "rich",
        "unicode",
        "pending",
        "held",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime)?;
            let ids = identity(&root);
            let target = ids[if case == "current" {
                "current"
            } else {
                "target"
            }]
            .as_str()
            .unwrap();
            let before_editor = editor(&app);
            let original = app.live.as_ref().unwrap().history.clone();
            let rows = app.live.as_ref().unwrap().rows.len();
            let key = inspect(runtime, &mut app, target)?;
            wait_history(runtime, &mut app, &key)?;
            assert_inspector_surface(&mut app);
            let view = &app.live.as_ref().unwrap().saved_history;
            assert_eq!(view.snapshot.as_ref().unwrap().identity.session, target);
            assert_eq!(
                view.snapshot.as_ref().unwrap().identity.scope,
                if case == "current" {
                    Scope::Current
                } else {
                    Scope::Preview
                }
            );
            if case == "readonly" {
                assert!(view.snapshot.as_ref().unwrap().read_only);
            }
            let catalogue = app.live.as_ref().unwrap().browser.page.clone();
            if case == "readonly" || case == "incompatible" {
                assert!(
                    !catalogue
                        .as_ref()
                        .unwrap()
                        .entries
                        .iter()
                        .find(|entry| entry.session == target)
                        .unwrap()
                        .can_open
                );
            }
            for (width, height) in [(100, 32), (42, 18), (36, 14)] {
                assert!(draw(&mut app, width, height).contains("Esc"));
            }
            draw(&mut app, 100, 32);
            history_key(&mut app, KeyCode::Home);
            let head = app
                .live
                .as_ref()
                .unwrap()
                .saved_history
                .pending_key()
                .cloned()
                .unwrap();
            route(runtime, &mut app)?;
            wait_history(runtime, &mut app, &head)?;
            let mut blocks = std::collections::HashMap::<String, (String, u64, Vec<u8>)>::new();
            for _ in 0..2000 {
                let page = app
                    .live
                    .as_ref()
                    .unwrap()
                    .saved_history
                    .page
                    .as_ref()
                    .unwrap();
                for fragment in &page.fragments {
                    let block = blocks.entry(fragment.block_id.clone()).or_insert_with(|| {
                        (fragment.block_sha256.clone(), fragment.total, Vec::new())
                    });
                    assert_eq!(block.0, fragment.block_sha256);
                    assert_eq!(block.1, fragment.total);
                    assert_eq!(block.2.len() as u64, fragment.start);
                    block.2.extend_from_slice(fragment.text.as_bytes());
                }
                if page.next.is_none() {
                    break;
                }
                draw(&mut app, 100, 32);
                history_key(&mut app, KeyCode::Right);
                let read = app
                    .live
                    .as_ref()
                    .unwrap()
                    .saved_history
                    .pending_key()
                    .cloned()
                    .unwrap();
                route(runtime, &mut app)?;
                wait_history(runtime, &mut app, &read)?;
            }
            assert_eq!(
                blocks.len() as u64,
                app.live
                    .as_ref()
                    .unwrap()
                    .saved_history
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .counts
                    .blocks
            );
            for (hash, total, bytes) in blocks.values() {
                assert_eq!(bytes.len() as u64, *total);
                assert_eq!(format!("{:x}", Sha256::digest(bytes)), *hash);
                assert!(std::str::from_utf8(bytes).is_ok());
            }
            let refresh = refresh(runtime, &mut app)?;
            assert_eq!(refresh.target, key.target);
            assert!(refresh.generation > key.generation);
            wait_history(runtime, &mut app, &refresh)?;
            assert_eq!(app.live.as_ref().unwrap().history, original);
            assert_eq!(app.live.as_ref().unwrap().rows.len(), rows);
            assert_eq!(editor(&app), before_editor);
            assert_eq!(app.live.as_ref().unwrap().browser.page, catalogue);
            close_views(&mut app);
            app.open_history();
            let current = app
                .live
                .as_ref()
                .unwrap()
                .saved_history
                .pending_key()
                .cloned()
                .unwrap();
            assert_eq!(current.target, Target::Current);
            route(runtime, &mut app)?;
            wait_history(runtime, &mut app, &current)?;
            assert_eq!(
                app.live
                    .as_ref()
                    .unwrap()
                    .saved_history
                    .snapshot
                    .as_ref()
                    .unwrap()
                    .identity
                    .session,
                ids["current"]
            );
            history_key(&mut app, KeyCode::Esc);
            assert_eq!(editor(&app), before_editor);
            if case != "held" {
                let chat = app.live.as_mut().unwrap();
                chat.submit(PROMPT)?;
                runtime.submit(chat.take_submission().unwrap())?;
                wait(runtime, &mut app, |event| {
                    matches!(event, Observation::Complete)
                })?;
            }
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?}, {}", root.display());
        report(&root, usize::from(case != "held"));
    }
}
fn delayed_stage(root: &Path, stage: &str) -> io::Result<()> {
    let target = identity(root)["target"].as_str().unwrap().to_owned();
    let until = Instant::now() + Duration::from_secs(10);
    while Instant::now() < until {
        if let Ok(bytes) = fs::read(root.join("inspection-delayed-stage.json"))
            && let Ok(value) = serde_json::from_slice::<Value>(&bytes)
            && value["stage"] == stage
            && value["session"].as_str() == Some(target.as_str())
        {
            return Ok(());
        }
        std::thread::sleep(Duration::from_millis(5));
    }
    Err(io::Error::other(
        "Source did not reach the controlled inspection stage",
    ))
}
#[test]
#[ignore = "controlled delayed source reads; dependency Python required"]
fn cancelling_each_saved_read_stage_retains_input_until_exact_settlement() {
    for stage in ["locate", "snapshot", "page"] {
        let case = format!("delay-{stage}");
        let (runtime, root) = launch(&case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime)?;
            let ids = identity(&root);
            let draft = editor(&app);
            let key = inspect(runtime, &mut app, ids["target"].as_str().unwrap())?;
            let catalogue = app.live.as_ref().unwrap().browser.page.clone();
            delayed_stage(&root, stage)?;
            history_key(&mut app, KeyCode::Esc);
            assert!(app.has_live_history());
            assert!(app.live.as_mut().unwrap().submit("must stay held").is_err());
            wait_history(runtime, &mut app, &key)?;
            assert!(!app.has_live_history());
            assert_eq!(editor(&app), draft);
            assert_eq!(app.live.as_ref().unwrap().browser.page, catalogue);
            draw(&mut app, 100, 32);
            browser_key(&mut app, KeyCode::Enter);
            assert!(app.take_browser_intent()?.is_none());
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?}, {}", root.display());
        report(&root, 0);
    }
}
#[test]
#[ignore = "controlled exact locator refresh; dependency Python required"]
fn refresh_relocates_exact_session_and_retains_stale_page_after_delete_or_edit() {
    for case in ["reorder-refresh", "delete-refresh", "stale-refresh"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime)?;
            let ids = identity(&root);
            let target = ids["target"].as_str().unwrap();
            let key = inspect(runtime, &mut app, target)?;
            wait_history(runtime, &mut app, &key)?;
            let prior = app.live.as_ref().unwrap().saved_history.page.clone();
            let catalogue = app.live.as_ref().unwrap().browser.page.clone();
            let draft = editor(&app);
            let refreshed = refresh(runtime, &mut app)?;
            wait_history(runtime, &mut app, &refreshed)?;
            assert_inspector_surface(&mut app);
            let view = &app.live.as_ref().unwrap().saved_history;
            if case == "reorder-refresh" {
                assert!(!view.is_stale());
                assert_eq!(view.snapshot.as_ref().unwrap().identity.session, target);
                assert_ne!(
                    view.page.as_ref().unwrap().snapshot,
                    prior.as_ref().unwrap().snapshot
                );
            } else {
                assert!(view.is_stale());
                assert_eq!(view.page, prior);
            }
            if case == "stale-refresh" {
                let retry = refresh(runtime, &mut app)?;
                wait_history(runtime, &mut app, &retry)?;
                assert!(!app.live.as_ref().unwrap().saved_history.is_stale());
                assert_eq!(
                    app.live
                        .as_ref()
                        .unwrap()
                        .saved_history
                        .snapshot
                        .as_ref()
                        .unwrap()
                        .identity
                        .session,
                    target
                );
            }
            assert_eq!(app.live.as_ref().unwrap().browser.page, catalogue);
            assert_eq!(editor(&app), draft);
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?}, {}", root.display());
        let report = report(&root, 0);
        let located: Vec<_> = report["commands"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| {
                c["type"] == "gui.conversations.page" && c.get("locate_session_id").is_some()
            })
            .collect();
        assert!(located.len() >= 2);
        assert!(
            located
                .iter()
                .all(|c| c["locate_session_id"] == located[0]["locate_session_id"])
        );
    }
}
#[test]
#[ignore = "controlled shutdown during source reads; dependency Python required"]
fn quit_during_saved_inspection_joins_the_child_without_adopting_target() {
    for stage in ["locate", "snapshot", "page"] {
        let case = format!("delay-{stage}");
        let (runtime, root) = launch(&case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime)?;
            let ids = identity(&root);
            inspect(runtime, &mut app, ids["target"].as_str().unwrap())?;
            delayed_stage(&root, stage)?;
            assert!(app.has_live_history());
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?}, {}", root.display());
        report(&root, 0);
    }
}
