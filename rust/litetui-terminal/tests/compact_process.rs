#![cfg(windows)]
//! Actual F6 manual compaction controls through owned source RPC and controlled SDK boundaries.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    conversation_compact as r,
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    sync::atomic::{AtomicUsize, Ordering},
    time::{Duration, Instant},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
const HINT: &str = " Preserve the exact tool result 雪🛰️e\u{301}\r\nand the unfinished task.  ";
const QUESTION_NOTE: &str = "Explicit controlled compaction answer 雪";
fn runtime_source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn backend(case: &str) -> &'static str {
    if case.starts_with("codex-native") {
        "codex"
    } else if case.starts_with("claude-segment") {
        "claude"
    } else {
        "custom"
    }
}
fn model(case: &str) -> &'static str {
    if case.starts_with("codex-native") {
        "gpt-6.1-sol"
    } else if case.starts_with("claude-segment") {
        "claude-fixture"
    } else {
        "switch-old"
    }
}
fn tools_on(case: &str) -> bool {
    matches!(
        case,
        "tools-on"
            | "tool-then-empty"
            | "tool-cap"
            | "claude-segment-tools"
            | "claude-segment-host"
            | "claude-segment-host-changed"
    ) || case.starts_with("owner-verified")
        || case.contains("question")
        || case.contains("approval")
}
fn launch(case: &str) -> (Interactive, PathBuf) {
    let (args, root) = launch_args(case);
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn launch_args(case: &str) -> (Vec<String>, PathBuf) {
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let actual = runtime_source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/compact-process-tests")
        .join(format!(
            "{}-{}-{}-{case}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
    let source = base.join("source/src/litetui");
    fs::create_dir_all(&source).unwrap();
    let workspace = base.join("workspace");
    fs::create_dir_all(&workspace).unwrap();
    let quote = |s: &str| serde_json::to_string(s).unwrap();
    fs::write(
        source.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            quote(&actual.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    fs::write(source.join("cli.py"),format!(r#"import json, os, runpy
from pathlib import Path
os.environ['RUST_HISTORY_SOURCE'] = {}
os.environ['RUST_HISTORY_CASE'] = {}
os.environ['RUST_HISTORY_PHASE'] = 'fresh'
os.environ['RUST_COMPACT_CASE'] = {}
os.environ['RUST_COMPACT_DRIVER'] = 'rpc'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000, compact_keep_recent=2, compact_max_tokens=256, compact_max_tool_iters=2, compact_thinking_level='off', wake_after_compact={}, clear_screen_after_compact={})
if '--convo' not in __import__('sys').argv:
    p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(&format!("compact-{case}")),quote(case),if case=="wake"||case.ends_with("-wake")||case=="claude-segment-wake-approval"{"True"}else{"False"},if case=="clear-screen"{"True"}else{"False"},quote(&fixtures.join("history_compact_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("controlled dependency Python"),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        backend(case).into(),
        "--model".into(),
        model(case).into(),
        "--tool-profile".into(),
        "strict".into(),
        "--tools".into(),
        if tools_on(case) {
            "on".into()
        } else {
            "off".into()
        },
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "90".into(),
    ];
    let launch_fields: &[&str] = if backend(case) == "codex" {
        &["--codex-engine", "native"]
    } else if backend(case) == "claude" {
        &[]
    } else {
        &[
            "--base-url",
            "http://127.0.0.1:45120/v1",
            "--server-mode",
            "connect",
        ]
    };
    if case.starts_with("owner-verified") {
        args.extend([
            "--owner".to_owned(),
            "OwnerFixture".into(),
            "--memory".into(),
            workspace.to_string_lossy().into_owned(),
        ]);
    }
    args.extend(launch_fields.iter().map(|field| (*field).to_owned()));
    (args, root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let deadline = Instant::now() + Duration::from_secs(95);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(50)) {
            Ok(event) => return Ok(event),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected compact runtime exit")));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Compact observation deadline"));
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
fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}
fn surface(app: &mut Demo) {
    let actual = draw(app);
    let theme = app.palette().clone();
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|f| app.live.as_mut().unwrap().compact.draw(f, f.area(), &theme))
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
        "Compact review must own the entire visible surface"
    );
}
fn editor(app: &Demo) -> (String, String, String) {
    (
        app.composer.text().into(),
        app.composer.before_cursor().into(),
        app.composer.selected_text().into(),
    )
}
fn start(runtime: &mut Interactive, case: &str) -> io::Result<Demo> {
    let mut app = Demo::live(backend(case).into(), model(case).into());
    app.insert_text("KEEP α 雪 THE COMPOSER DRAFT");
    app.composer.move_cursor(Motion::WordLeft, true);
    wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
    Ok(app)
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .compact
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn select(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    app.open_history();
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .saved_history
        .take_intent()
        .expect("F6 fresh read");
    let request = intent.key().clone();
    runtime.history(intent)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::SavedHistory(update)if update.key()==&request),
    )?;
    draw(app);
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .saved_history
            .selected_message()
            .is_none()
    );
    app.live
        .as_mut()
        .unwrap()
        .saved_history
        .key(KeyEvent::new(KeyCode::Char('c'), KeyModifiers::NONE));
    app.take_history_compact()?;
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .compact
        .take_intent()
        .expect("F6 C production bridge");
    let request = intent.key().clone();
    runtime.compact(intent)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Compact(update)if update.key()==&request),
    )
}
fn validate(runtime: &mut Interactive, app: &mut Demo, text: Option<&str>) -> io::Result<()> {
    surface(app);
    if let Some(text) = text {
        let count = app.live.as_ref().unwrap().compact.draft().chars().count();
        key(app, KeyCode::End);
        for _ in 0..count {
            key(app, KeyCode::Backspace);
        }
        assert_eq!(app.live.as_ref().unwrap().compact.draft(), "");
        app.live.as_mut().unwrap().compact.paste(text);
        assert_eq!(app.live.as_ref().unwrap().compact.draft(), text);
    }
    draw(app);
    key(app, KeyCode::Enter);
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .compact
        .take_intent()
        .ok_or_else(|| io::Error::other(format!("Missing validation: {}", draw(app))))?;
    let request = intent.key().clone();
    runtime.compact(intent)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Compact(update) if update.key()==&request),
    )
}
fn apply(runtime: &Interactive, app: &mut Demo) -> io::Result<r::RequestKey> {
    surface(app);
    let mut apply = None;
    for _ in 0..64 {
        draw(app);
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().compact.take_intent() {
            apply = Some(intent);
            break;
        }
        draw(app);
        key(app, KeyCode::PageDown);
    }
    let intent = apply.expect("explicit visible confirmation after every review page");
    let request = intent.key().clone();
    assert!(matches!(intent, r::Intent::Apply { .. }));
    key(app, KeyCode::Enter);
    assert!(app.live.as_mut().unwrap().compact.take_intent().is_none());
    app.live.as_mut().unwrap().prepare_compact(&intent)?;
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    runtime.compact(intent)?;
    Ok(request)
}
fn settle(runtime: &mut Interactive, app: &mut Demo, request: &r::RequestKey) -> io::Result<()> {
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::CompactSettled(changed) if &changed.key==request),
    )?;
    let chat = app.live.as_ref().unwrap();
    assert!(!chat.rewrite_pending());
    assert!(!chat.busy);
    assert!(!chat.compact.owns_input());
    Ok(())
}

fn stage(runtime: &mut Interactive, app: &mut Demo, root: &Path, expected: &str) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(40);
    let mut submitted = false;
    loop {
        while let Ok(event) = runtime.events.try_recv() {
            submitted |= matches!(&event, Observation::Submission { .. });
            app.observe_live(event)?;
        }
        if let Ok(bytes) = fs::read(root.join("compact-stage.json"))
            && let Ok(value) = serde_json::from_slice::<Value>(&bytes)
            && value["stage"] == expected
            && submitted
            && (expected != "question"
                || app
                    .live
                    .as_ref()
                    .unwrap()
                    .questions
                    .current()
                    .is_some_and(|q| value["request_id"] == q.request.id))
            && (expected != "approval"
                || app
                    .live
                    .as_ref()
                    .unwrap()
                    .approvals
                    .current()
                    .is_some_and(|p| value["request_id"] == p.request.id))
            && (expected != "root-maintenance"
                || app
                    .live
                    .as_ref()
                    .unwrap()
                    .rows
                    .iter()
                    .any(|r| r.state == litetui_terminal::live_chat::ReplyState::Maintenance))
        {
            return Ok(());
        }
        if let Some(done) = runtime.poll_done() {
            return Err(done
                .err()
                .unwrap_or_else(|| io::Error::other("Runtime ended before stage")));
        }
        if Instant::now() > deadline {
            return Err(io::Error::other(format!(
                "Missing {expected} stage at {}",
                root.display()
            )));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
fn report(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("history-fresh-report.json")).unwrap()).unwrap();
    assert_eq!(
        PathBuf::from(r["source_app"].as_str().unwrap())
            .canonicalize()
            .unwrap(),
        runtime_source()
            .join("src/litetui/app.py")
            .canonicalize()
            .unwrap()
    );
    assert_eq!(r["denied"], json!([]));
    assert_eq!(r["provider_requests"], json!([]));
    if r["snapshot"]["backend"] == "codex" {
        assert!(
            r["provider_calls"]
                .as_array()
                .unwrap()
                .iter()
                .all(|call| matches!(call["method"].as_str(), Some("account/read" | "model/list")))
        );
    } else {
        assert_eq!(r["provider_calls"], json!([]));
    }
    assert_eq!(r["durable"], r["live"]);
    assert_eq!(r["archive_prefix_preserved"], true);
    for field in ["worker_settlement", "compact_workers"] {
        assert!(
            r[field]
                .as_array()
                .unwrap()
                .iter()
                .all(|w| w["finished"] == true),
            "{field}"
        );
    }
    assert!(
        r["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["closed"] == true)
    );
    assert_eq!(r["question_pending"], json!([]));
    assert_eq!(r["approval_pending"], json!([]));
    assert_eq!(r["approval_authorities"], json!([]));
    assert!(
        r["question_threads"]
            .as_array()
            .unwrap()
            .iter()
            .all(|t| t["retired"] == true)
    );
    assert!(
        r["tool_futures"]
            .as_array()
            .unwrap()
            .iter()
            .all(|t| t["done"] == true
                && t["producer_done"] == true
                && t["callback_retired"] == true)
    );
    let commands = r["commands"].as_array().unwrap();
    assert!(
        !commands.iter().any(|c| c["type"] == "prompt"),
        "Compaction must not fabricate user admission"
    );
    assert!(commands.iter().filter(|c| c["type"] == r::APPLY).count() <= 1);
    let releases: Vec<_> = r["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["effect"] == "compact-lease-release")
        .collect();
    assert!(!releases.is_empty());
    assert!(releases.iter().all(|e| e["active_chat"] == json!([])
        && e["pending_questions"] == json!([])
        && e["pending_approvals"] == json!([])
        && e["threads_retired"] == true));
    r
}
#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_compact_preserves_hint_draft_and_real_zero_or_wake_streams() {
    for case in [
        "compact",
        "wake",
        "clear-screen",
        "no-system",
        "keep-zero",
        "tools-on",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let before = app.live.as_ref().unwrap().rows.len();
            let request = apply(runtime, &mut app)?;
            assert_eq!(
                app.live.as_ref().unwrap().rows.len(),
                before,
                "Apply reserves an action, not a user row"
            );
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            let chat = app.live.as_ref().unwrap();
            assert!(chat.rows.iter().all(|row| row.source_owned));
            let completed = chat
                .rows
                .iter()
                .filter(|row| row.state == litetui_terminal::live_chat::ReplyState::Complete)
                .count();
            if completed != usize::from(case == "wake") {
                return Err(io::Error::other(format!(
                    "{case}: expected {} completed wake rows, got {completed}; rows {:?}",
                    usize::from(case == "wake"),
                    chat.rows
                        .iter()
                        .map(|row| (&row.state, &row.stop_reason))
                        .collect::<Vec<_>>()
                )));
            }
            assert!(draw(&mut app).contains("Source: Conversation compaction"));
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        assert_eq!(r["settled"]["outcome"], "compacted");
        assert_eq!(r["review"]["hint"], HINT);
        assert_eq!(
            r["settled"]["execution"]["segments"],
            u64::from(case == "wake")
        );
        if case == "tools-on" {
            assert_eq!(r["artifact_exists"], true);
        }
    }
}
#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_codex_compact_verifies_native_thread_and_preserves_saved_transcript() {
    let case = "codex-native";
    let (runtime, root) = launch(case);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = start(runtime, case)?;
        let draft = editor(&app);
        select(runtime, &mut app)?;
        surface(&mut app);
        assert!(draw(&mut app).contains("Compact Codex context"));
        assert!(!app.live.as_ref().unwrap().compact.accepts_paste());
        validate(runtime, &mut app, None)?;
        assert!(draw(&mut app).contains("Confirm Codex context compaction"));
        let request = apply(runtime, &mut app)?;
        settle(runtime, &mut app, &request)?;
        assert_eq!(editor(&app), draft);
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?}; artifacts {}", root.display());
    let r = report(&root);
    assert_eq!(r["settled"]["history_changed"], false);
    assert_eq!(
        r["settled"]["witness"]["native_compaction"],
        json!({"thread_id":"history-native-thread",
        "turn_id":"native-compact-turn","item_id":"native-compact-item"})
    );
    assert_eq!(r["records_before"], r["records_after"]);
    assert_eq!(r["baseline_live"], r["live"]);
    assert_eq!(
        r["compaction_rpc"]
            .as_array()
            .unwrap()
            .iter()
            .map(|call| call["method"].as_str().unwrap())
            .collect::<Vec<_>>(),
        [
            "thread/read",
            "thread/compact/start",
            "thread/read",
            "thread/turns/list"
        ]
    );
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_claude_compaction_retires_old_session_and_publishes_the_saved_seed() {
    let case = "claude-segment";
    let (runtime, root) = launch(case);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = start(runtime, case)?;
        let draft = editor(&app);
        select(runtime, &mut app)?;
        validate(runtime, &mut app, Some(HINT))?;
        assert!(draw(&mut app).contains("Confirm Claude compaction"));
        let request = apply(runtime, &mut app)?;
        settle(runtime, &mut app, &request)?;
        assert_eq!(editor(&app), draft);
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?}; artifacts {}", root.display());
    let r = report(&root);
    let proof = &r["settled"]["witness"]["claude_compaction"];
    assert_eq!(r["settled"]["witness"]["publication_verified"], true);
    assert_eq!(proof["result_id"], r["claude_result"]);
    assert_eq!(proof["old_segment"], r["old_ledger"]["selected_segment"]);
    assert_eq!(proof["new_segment"], r["final_ledger"]["selected_segment"]);
    assert_eq!(r["claude_summary_prompts"].as_array().unwrap().len(), 1);
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_claude_compaction_with_tools_wake_and_approvals_is_reviewed_receipted_and_attributed() {
    for case in [
        "claude-segment-wake",
        "claude-segment-tools",
        "claude-segment-approval",
        "claude-segment-wake-approval",
    ] {
        let tools = case != "claude-segment-wake";
        let wake = case.contains("wake");
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let review = draw(&mut app);
            assert!(review.contains("Confirm Claude compaction"));
            assert!(review.contains(if tools {
                "Tools enabled: yes"
            } else {
                "Tools enabled: no"
            }));
            assert!(review.contains(if wake {
                "Wake after success: yes"
            } else {
                "Wake after success: no"
            }));
            let request = apply(runtime, &mut app)?;
            if case.contains("approval") {
                stage(runtime, &mut app, &root, "approval")?;
                assert!(app.has_live_approval());
                draw(&mut app);
                let approvals = &mut app.live.as_mut().unwrap().approvals;
                let id = approvals.current().unwrap().request.id.clone();
                approvals.choose(true);
                let decision = approvals
                    .take_decision()
                    .expect("visible approval decision");
                assert_eq!(decision.id, id);
                runtime.approve(decision)?;
            }
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            let completed = app
                .live
                .as_ref()
                .unwrap()
                .rows
                .iter()
                .filter(|row| row.state == litetui_terminal::live_chat::ReplyState::Complete)
                .count();
            assert_eq!(completed, usize::from(wake), "{case}: completed wake rows");
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        assert_eq!(r["settled"]["outcome"], "compacted", "{case}");
        assert_eq!(
            r["settled"]["execution"]["segments"],
            u64::from(wake),
            "{case}"
        );
        let proof = &r["settled"]["witness"]["claude_compaction"];
        assert_eq!(
            proof["new_segment"], r["final_ledger"]["selected_segment"],
            "{case}"
        );
        let summary_tool = matches!(case, "claude-segment-tools" | "claude-segment-approval");
        assert_eq!(proof["tool_calls"], u64::from(summary_tool), "{case}");
        assert_eq!(
            r["artifact_exists"],
            case != "claude-segment-wake",
            "{case}"
        );
        assert_eq!(
            proof["store_files"].as_array().unwrap().len(),
            usize::from(summary_tool),
            "{case}"
        );
    }
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_claude_compaction_discloses_and_runs_host_tools_in_the_summary_turn() {
    for case in [
        "claude-segment-host",
        "claude-segment-host-approval",
        "claude-segment-host-changed",
    ] {
        // Stop during a host approval is proven by test_rust_compact_process.py (a stopped Claude
        // compaction settles as a closed-and-inspect failure, not a settlement).
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let review = draw(&mut app);
            assert!(review.contains("Confirm Claude compaction"));
            assert!(review.contains("Tools enabled: yes"));
            // The review names the host tool the summary turn may run, not just "host tools".
            assert!(
                review.contains("convo_search") && review.contains("these host MCP tools"),
                "{case}: {review}"
            );
            assert!(!review.contains("host MCP tools are unavailable"), "{case}");
            let request = apply(runtime, &mut app)?;
            if case.contains("approval") {
                stage(runtime, &mut app, &root, "approval")?;
                assert!(app.has_live_approval());
                draw(&mut app);
                let approvals = &mut app.live.as_mut().unwrap().approvals;
                let id = approvals.current().unwrap().request.id.clone();
                approvals.choose(true);
                let decision = approvals
                    .take_decision()
                    .expect("visible approval decision");
                assert_eq!(decision.id, id);
                runtime.approve(decision)?;
            }
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        assert!(!r["artifact_exists"].as_bool().unwrap(), "{case}");
        let ran = r["host_runs"].as_array().map_or(0, Vec::len);
        assert_eq!(r["settled"]["outcome"], "compacted", "{case}");
        let proof = &r["settled"]["witness"]["claude_compaction"];
        assert_eq!(proof["tool_calls"], 1, "{case}");
        assert_eq!(proof["store_files"].as_array().unwrap().len(), 0, "{case}");
        // A schema that drifted after the review must never reach the tool door.
        assert_eq!(
            ran,
            usize::from(case != "claude-segment-host-changed"),
            "{case}"
        );
        if ran == 1 {
            assert_eq!(
                r["host_runs"][0]["origin"]["execution_id"], r["review"]["root_execution_id"],
                "{case}: the host tool runs under the reviewed root execution"
            );
        }
    }
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_compact_cancel_review_has_no_provider_or_history_effects() {
    for reviewed in [false, true] {
        let (runtime, root) = launch("compact");
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, "compact")?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            if reviewed {
                validate(runtime, &mut app, Some(HINT))?;
            }
            draw(&mut app);
            key(&mut app, KeyCode::Esc);
            if reviewed {
                let compact = &app.live.as_ref().unwrap().compact;
                assert!(compact.is_open());
                assert!(compact.review().is_none());
                assert_eq!(compact.draft(), HINT);
                assert_eq!(editor(&app), draft);
                draw(&mut app);
                key(&mut app, KeyCode::Esc);
            }
            assert!(!app.live.as_ref().unwrap().compact.blocks_composer());
            assert_eq!(editor(&app), draft);
            runtime.quit()
        });
        assert!(result.is_ok(), "{result:?}; artifacts {}", root.display());
        let r = report(&root);
        assert_eq!(r["live"], r["original"]);
        assert!(r["sdk_requests"].as_array().unwrap().is_empty());
        assert!(
            !r["commands"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["type"] == r::APPLY)
        );
    }
}
#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_compact_stop_and_quit_join_root_work_and_question_callbacks() {
    for case in [
        "stop-stream",
        "stop-readiness",
        "stop-question",
        "stop-question-wake",
        "quit-stream",
        "quit-question",
        "quit-question-wake",
        "stop-approval",
        "stop-approval-wake",
        "quit-approval",
        "quit-approval-wake",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let request = apply(runtime, &mut app)?;
            stage(
                runtime,
                &mut app,
                &root,
                if case.contains("question") {
                    "question"
                } else if case.contains("approval") {
                    "approval"
                } else if case.ends_with("readiness") {
                    "readiness"
                } else {
                    "root-maintenance"
                },
            )?;
            if case.starts_with("stop") {
                app.live.as_mut().unwrap().request_stop()?;
                runtime.abort(
                    app.live
                        .as_mut()
                        .unwrap()
                        .take_abort()
                        .expect("exact action Stop"),
                )?;
                settle(runtime, &mut app, &request)?;
                assert_eq!(editor(&app), draft);
            }
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        if case.starts_with("quit") {
            assert_eq!(r["shutdown"]["pending_at_close"], true);
            assert_eq!(r["shutdown"]["effects_verified"], false);
        }
        if !case.ends_with("wake") {
            assert!(
                !r["sdk_requests"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|r| r["kind"] == "wake")
            );
        }
    }
}
fn question_key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .questions
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn answer_question(runtime: &Interactive, app: &mut Demo) -> io::Result<String> {
    let rendered = draw(app);
    assert!(rendered.contains("How should the task continue?"));
    assert!(rendered.contains("Preserve the task"));
    assert!(app.has_live_question());
    let pending = app.live.as_ref().unwrap().questions.current().unwrap();
    let id = pending.request.id.clone();
    assert_eq!(pending.request.questions.len(), 1);
    assert_eq!(pending.question().options.len(), 2);
    assert!(!pending.question().multi_select);
    assert_eq!(pending.focus, 0);
    // Select the first visible option, focus its note, then the Submit control.
    question_key(app, KeyCode::Down);
    question_key(app, KeyCode::Char(' '));
    question_key(app, KeyCode::Down);
    question_key(app, KeyCode::Down);
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .questions
            .current()
            .unwrap()
            .note_focused()
    );
    app.live
        .as_mut()
        .unwrap()
        .questions
        .current_mut()
        .unwrap()
        .paste(QUESTION_NOTE, false);
    question_key(app, KeyCode::Down);
    question_key(app, KeyCode::Down);
    draw(app);
    let pending = app.live.as_ref().unwrap().questions.current().unwrap();
    assert_eq!(pending.focus, pending.question().options.len() + 3);
    question_key(app, KeyCode::Enter);
    let decision = app
        .live
        .as_mut()
        .unwrap()
        .questions
        .take_decision()
        .expect("Visible native question submission");
    assert_eq!(decision.id, id);
    assert_eq!(decision.action, litetui_terminal::questions::Action::Submit);
    assert_eq!(decision.answers.len(), 1);
    assert_eq!(
        decision.answers[0].selected,
        std::collections::BTreeSet::from([0])
    );
    assert_eq!(decision.answers[0].note, QUESTION_NOTE);
    assert!(
        app.live
            .as_ref()
            .unwrap()
            .questions
            .current()
            .unwrap()
            .waiting
    );
    question_key(app, KeyCode::Enter);
    assert!(
        app.live
            .as_mut()
            .unwrap()
            .questions
            .take_decision()
            .is_none()
    );
    runtime.answer(decision)?;
    Ok(id)
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_compact_answers_questions_in_root_and_genuine_wake() {
    for case in ["question", "question-wake"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let request = apply(runtime, &mut app)?;
            stage(runtime, &mut app, &root, "question")?;
            answer_question(runtime, &mut app)?;
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        assert_eq!(r["settled"]["outcome"], "compacted");
        assert_eq!(
            r["commands"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|c| c["type"] == "answer")
                .count(),
            1
        );
    }
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_compact_approval_decisions_cover_root_and_wake_tools() {
    for case in ["approval", "approval-denied", "approval-wake"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let request = apply(runtime, &mut app)?;
            stage(runtime, &mut app, &root, "approval")?;
            assert!(app.has_live_approval());
            assert!(!app.live.as_ref().unwrap().compact.owns_input());
            draw(&mut app);
            assert!(
                !root
                    .parent()
                    .unwrap()
                    .join("workspace/compact-owned-output.txt")
                    .exists()
            );
            let approvals = &mut app.live.as_mut().unwrap().approvals;
            let id = approvals.current().unwrap().request.id.clone();
            approvals.choose(case != "approval-denied");
            let decision = approvals
                .take_decision()
                .expect("visible approval decision");
            assert_eq!(decision.id, id);
            runtime.approve(decision)?;
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        assert_eq!(r["artifact_exists"], case != "approval-denied");
        assert_eq!(
            r["commands"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|c| c["type"] == "approve")
                .count(),
            1
        );
    }
}

#[test]
#[ignore = "controlled actual-source compaction; dependency Python required"]
fn f6_compact_nochange_and_clean_failures_release_only_after_verified_readback() {
    for (case, outcome) in [
        ("too-short", "no_change"),
        ("already-recent", "no_change"),
        ("empty-summary", "failed"),
        ("readiness-failure", "failed"),
        ("provider-failure", "failed"),
        ("tools-off", "compacted"),
        ("tool-then-empty", "failed"),
        ("tool-cap", "failed"),
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            select(runtime, &mut app)?;
            validate(runtime, &mut app, Some(HINT))?;
            let request = apply(runtime, &mut app)?;
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            assert!(
                app.live
                    .as_ref()
                    .unwrap()
                    .rows
                    .iter()
                    .all(|r| r.source_owned
                        && r.state == litetui_terminal::live_chat::ReplyState::MaintenanceSettled)
            );
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let r = report(&root);
        assert_eq!(r["settled"]["outcome"], outcome);
        assert_eq!(
            r["settled"]["execution"],
            json!({"segments":0,"last_execution_id":null})
        );
        if outcome != "compacted" {
            assert_eq!(r["live"], r["original"]);
            assert_eq!(r["settled"]["witness"]["publication_attempted"], false);
        }
        assert_eq!(
            r["artifact_exists"],
            matches!(case, "tool-then-empty" | "tool-cap")
        );
    }
}

#[test]
#[ignore = "actual owner-bound Python source and a private Git repository"]
fn owner_handoff_checks_lease_stale_review_and_commits_only_selected_files() {
    for case in ["owner-verified", "owner-verified-stale"] {
        let (runtime, root) = launch(case);
        let folder = root.parent().unwrap().join("workspace");
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let draft = editor(&app);
            let lease: Value = serde_json::from_slice(&fs::read(folder.join(".owner.lock"))?)?;
            assert!(lease["agent_id"].as_str().is_some_and(|id| !id.is_empty()));
            select(runtime, &mut app)?;
            validate(runtime, &mut app, None)?;
            let review = draw(&mut app);
            assert!(
                review.contains("Owner handoff") && review.contains("DOMAIN.md"),
                "{review}"
            );
            if case.ends_with("stale") {
                fs::write(folder.join("SOUL.md"), "External change after review")?;
            }
            let request = apply(runtime, &mut app)?;
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), draft);
            runtime.quit()
        });
        assert!(result.is_ok(), "{case}: {result:?}; {}", root.display());
        assert!(!folder.join(".owner.lock").exists());
        let r = report(&root);
        assert_eq!(r["owner"]["name"], "OwnerFixture");
        assert_eq!(r["owner"]["privileged"], false);
        if case.ends_with("stale") {
            assert_eq!(r["settled"]["reason_code"], "admission_refused");
            assert_eq!(r["sdk_requests"], json!([]));
            assert_eq!(
                fs::read_to_string(folder.join("NOW.md"))
                    .unwrap()
                    .trim_end(),
                "Private starting NOW.md"
            );
        } else {
            let receipt = &r["settled"]["witness"]["owner_handoff"];
            assert_eq!(r["settled"]["outcome"], "compacted");
            assert_eq!(
                receipt["parent"],
                r["review"]["plan"]["owner_handoff"]["head"]
            );
            assert_ne!(receipt["commit"], receipt["parent"]);
            assert_eq!(
                fs::read_to_string(folder.join("unrelated.txt"))
                    .unwrap()
                    .trim_end(),
                "Keep this unrelated staged edit."
            );
        }
    }
}

#[test]
#[ignore = "real private owner lease and retained source store"]
fn fresh_owner_refuses_another_holder_then_resumes_after_clean_quit() {
    let case = "owner-verified-resume";
    let (args, root) = launch_args(case);
    let folder = root.parent().unwrap().join("workspace");
    let runtime = Interactive::start(Options::parse(&args).unwrap());
    runtime
        .with_cleanup(|runtime| {
            let _app = start(runtime, case)?;
            let before = fs::read(folder.join(".owner.lock"))?;
            let mut other = args.clone();
            let index = other.iter().position(|s| s == "--data-root").unwrap();
            let other_root = root.with_file_name("other-data");
            other[index + 1] = other_root.to_string_lossy().into_owned();
            let mut contender = Interactive::start(Options::parse(&other)?);
            let msg = loop {
                match next(&mut contender) {
                    Err(error) => break error.to_string(),
                    Ok(Observation::Ready) => panic!("Concurrent owner was admitted"),
                    Ok(_) => {}
                }
            };
            assert!(msg.contains("in use"), "{msg}");
            assert_eq!(fs::read(folder.join(".owner.lock"))?, before);
            assert!(!other_root.join(".agents").exists());
            runtime.quit()
        })
        .unwrap();
    let original = report(&root);
    let session = original["owner"]["session"].as_str().unwrap();
    let transcript = PathBuf::from(original["transcript_path"].as_str().unwrap());
    let raw = fs::read(&transcript).unwrap();
    assert!(!folder.join(".owner.lock").exists());
    let settings = fs::read(root.join("settings.json")).unwrap();
    let mut resume = args;
    resume.extend(["--resume".into(), session.to_owned()]);
    let runtime = Interactive::start(Options::parse(&resume).unwrap());
    runtime
        .with_cleanup(|runtime| {
            let _app = start(runtime, case)?;
            let held: Value = serde_json::from_slice(&fs::read(folder.join(".owner.lock"))?)?;
            assert_eq!(held["agent_id"], original["owner"]["id"]);
            runtime.quit()
        })
        .unwrap();
    let resumed = report(&root);
    assert_eq!(resumed["owner"], original["owner"]);
    assert_eq!(resumed["sdk_requests"], json!([]));
    let reopened = fs::read(transcript).unwrap();
    assert!(reopened.starts_with(&raw));
    let edits: Vec<Value> = serde_json::Deserializer::from_slice(&reopened[raw.len()..])
        .into_iter()
        .collect::<Result<_, _>>()
        .unwrap();
    assert_eq!(
        edits.len(),
        1,
        "Canonical refresh of the fixture's legacy store address"
    );
    assert_eq!(edits[0]["type"], "edit");
    assert_eq!(edits[0]["index"], 0);
    assert_eq!(edits[0]["reason"], "owned store address refreshed");
    assert_eq!(edits[0]["message"]["role"], "system");
    assert_eq!(edits[0]["store_identity"], original["owner"]["store"]);
    assert_eq!(fs::read(root.join("settings.json")).unwrap(), settings);
    assert!(!folder.join(".owner.lock").exists());
}
