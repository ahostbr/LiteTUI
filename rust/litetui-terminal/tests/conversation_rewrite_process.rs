#![cfg(windows)]
//! Actual F6 edit/retry controls through owned source RPC and controlled SDK boundaries.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    conversation_rewrite as r,
    demo::Demo,
    live::{Interactive, Observation, Options},
    live_chat::ReplyState,
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
const ORIGINAL: &str = "Original selected user prompt";
const REPLACEMENT: &str =
    "Explicit replacement: preserve prefix, discard only the reviewed suffix 雪";
const UNICODE: &str = "  雪🛰️e\u{301}\r\nExact trailing spaces  ";
const QUESTION_NOTE: &str = "Explicit controlled answer 雪";
fn runtime_source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn backend(case: &str) -> &'static str {
    if case == "self-compact" || case.contains("question-") {
        "lmstudio"
    } else {
        "custom"
    }
}
fn aftercare(case: &str) -> bool {
    matches!(
        case,
        "auto-compact" | "self-compact" | "quit-compaction" | "stop-compaction"
    ) || case.contains("question-") && case.ends_with("compaction")
}
fn launch(case: &str) -> (Interactive, PathBuf) {
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let actual = runtime_source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/conversation-rewrite-process-tests")
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
    let workspace = base.join("workspace");
    fs::create_dir_all(&workspace).unwrap();
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
os.environ['RUST_HISTORY_SOURCE'] = {}
os.environ['RUST_HISTORY_CASE'] = {}
os.environ['RUST_HISTORY_PHASE'] = 'fresh'
os.environ['RUST_REWRITE_CASE'] = {}
os.environ['RUST_REWRITE_DRIVER'] = 'rpc'
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000, max_tokens_chat=101, max_tokens_tools=202)
if {}:
    s.update(compact_keep_recent=2, autocompact_enabled={}, autocompact_at_percent=80, wake_after_compact=True)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(&format!("{}rewrite-{case}",if backend(case)=="lmstudio"{"lmstudio-"}else{""})),quote(case),
        if aftercare(case){"True"}else{"False"},if case=="self-compact"{"False"}else{"True"},quote(&fixtures.join("history_edit_retry_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("controlled dependency Python"),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        backend(case).into(),
        "--model".into(),
        "switch-old".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--tools".into(),
        if backend(case) == "lmstudio" {
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
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), root)
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
                        .unwrap_or_else(|| io::Error::other("Unexpected rewrite runtime exit")));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Rewrite observation deadline"));
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
        .draw(|f| app.live.as_mut().unwrap().rewrite.draw(f, f.area(), &theme))
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
        "Rewrite review must own the entire visible surface"
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
    let mut app = Demo::live(backend(case).into(), "switch-old".into());
    app.insert_text("KEEP α 雪 THE COMPOSER DRAFT");
    app.composer.move_cursor(Motion::WordLeft, true);
    wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
    Ok(app)
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .rewrite
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn select(runtime: &mut Interactive, app: &mut Demo, action: r::Action) -> io::Result<()> {
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
        |e| matches!(e,Observation::SavedHistory(update) if update.key()==&request),
    )?;
    for _ in 0..20 {
        draw(app);
        app.live
            .as_mut()
            .unwrap()
            .saved_history
            .key(KeyEvent::new(KeyCode::Tab, KeyModifiers::NONE));
        if app
            .live
            .as_ref()
            .unwrap()
            .saved_history
            .selected_message()
            .is_some_and(|(index, _)| index == 5)
        {
            break;
        }
    }
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .saved_history
            .selected_message()
            .map(|(n, _)| n),
        Some(5)
    );
    draw(app);
    app.live.as_mut().unwrap().saved_history.key(KeyEvent::new(
        KeyCode::Char(if action == r::Action::Edit { 'e' } else { 'r' }),
        KeyModifiers::NONE,
    ));
    // The exact production bridge, not a manually constructed rewrite selection.
    app.take_history_rewrite()?;
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .rewrite
        .take_intent()
        .expect("F6 selection routed into rewrite Read");
    let request = intent.key().clone();
    runtime.rewrite(intent)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Rewrite(update) if update.key()==&request),
    )
}
fn validate(runtime: &mut Interactive, app: &mut Demo, text: Option<&str>) -> io::Result<()> {
    surface(app);
    if let Some(text) = text {
        let count = app.live.as_ref().unwrap().rewrite.draft().chars().count();
        key(app, KeyCode::End);
        for _ in 0..count {
            key(app, KeyCode::Backspace);
        }
        assert_eq!(app.live.as_ref().unwrap().rewrite.draft(), "");
        app.live.as_mut().unwrap().rewrite.paste(text);
        assert_eq!(app.live.as_ref().unwrap().rewrite.draft(), text);
    }
    draw(app);
    key(app, KeyCode::Enter);
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .rewrite
        .take_intent()
        .expect("visible validation");
    let request = intent.key().clone();
    runtime.rewrite(intent)?;
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::Rewrite(update) if update.key()==&request),
    )
}
fn apply(runtime: &Interactive, app: &mut Demo) -> io::Result<r::RequestKey> {
    surface(app);
    let mut apply = None;
    for _ in 0..64 {
        draw(app);
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().rewrite.take_intent() {
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
    assert!(app.live.as_mut().unwrap().rewrite.take_intent().is_none());
    app.live.as_mut().unwrap().prepare_rewrite(&intent)?;
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    runtime.rewrite(intent)?;
    Ok(request)
}
fn settle(runtime: &mut Interactive, app: &mut Demo, request: &r::RequestKey) -> io::Result<()> {
    wait(
        runtime,
        app,
        |e| matches!(e,Observation::RewriteSettled(changed) if &changed.key==request),
    )?;
    let chat = app.live.as_ref().unwrap();
    assert!(!chat.rewrite_pending());
    assert!(!chat.busy);
    assert!(!chat.rewrite.owns_input());
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
        let visible_stage =
            submitted
                && (!matches!(expected, "compaction" | "question-compaction")
                    || app.live.as_ref().unwrap().rows.iter().any(|row| {
                        row.state == litetui_terminal::live_chat::ReplyState::Maintenance
                    }));
        if let Ok(bytes) = fs::read(root.join("rewrite-stage.json"))
            && let Ok(value) = serde_json::from_slice::<Value>(&bytes)
            && value["stage"] == expected
            && visible_stage
            && (!expected.starts_with("question-")
                || app
                    .live
                    .as_ref()
                    .unwrap()
                    .questions
                    .current()
                    .is_some_and(|pending| {
                        value["ask_id"].as_str() == Some(pending.request.id.as_str())
                    }))
        {
            return Ok(());
        }
        if let Some(done) = runtime.poll_done() {
            return Err(done
                .err()
                .unwrap_or_else(|| io::Error::other("Runtime exited before held stage")));
        }
        if Instant::now() > deadline {
            return Err(io::Error::other(format!(
                "Missing controlled {expected} marker"
            )));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
fn report(root: &Path, case: &str) -> Value {
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
    assert_eq!(report["denied"], json!([]));
    assert_eq!(report["provider_requests"], json!([]));
    let providers = report["provider_calls"].as_array().unwrap();
    if backend(case) == "lmstudio" {
        assert!(!providers.is_empty());
        assert!(providers.iter().all(|p| p["method"] == "models/read"));
    } else {
        assert!(providers.is_empty());
    }
    for field in ["worker_settlement", "rewrite_workers"] {
        assert!(
            report[field]
                .as_array()
                .unwrap()
                .iter()
                .all(|w| w["finished"] == true),
            "{field}"
        );
    }
    assert!(
        report["hook_processes"]
            .as_array()
            .unwrap()
            .iter()
            .all(|p| p["returncode"].is_number())
    );
    assert!(
        report["observations"]
            .as_array()
            .unwrap()
            .iter()
            .all(|o| o["wire_bytes"].as_u64().is_some_and(|n| n <= 1024 * 1024))
    );
    let commands = report["commands"].as_array().unwrap();
    assert!(
        commands.iter().all(|c| matches!(
            c["type"].as_str(),
            Some(
                "gui.hello"
                    | "gui.state"
                    | "gui.history.native_snapshot"
                    | "gui.history.native_page"
                    | "gui.themes.native_snapshot"
                    | "gui.work.native_capture"
                    | "gui.work.native_activate"
                    | "gui.work.native_settled"
                    | "gui.restored_input.list"
                    | "gui.conversations.native_rewrite_snapshot"
                    | "gui.conversations.native_rewrite_validate"
                    | "gui.conversations.native_rewrite_apply"
                    | "gui.conversations.native_rewrite_settled"
                    | "abort"
                    | "answer"
                    | "shutdown"
            )
        )),
        "{commands:?}"
    );
    for operation in ["gui.work.native_capture", "gui.work.native_activate"] {
        assert_eq!(
            commands.iter().filter(|c| c["type"] == operation).count(),
            1
        );
    }
    assert!(commands.iter().filter(|c| c["type"] == r::APPLY).count() <= 1);
    if !case.contains("question-") {
        assert!(!commands.iter().any(|command| command["type"] == "answer"));
    }
    assert_eq!(report["durable"], report["live"]);
    for request in report["requests"].as_array().unwrap() {
        assert_eq!(request["complete_prepared_context"], true);
        assert_eq!(request["wire_row_hashes"], request["expected_row_hashes"]);
        assert_eq!(request["context_sha256"], request["prepared_sha256"]);
        assert_eq!(
            request["session_id"],
            report["before"]["identity"]["session_id"]
        );
        assert_eq!(
            request["agent_id"],
            report["before"]["identity"]["agent_id"]
        );
        assert_eq!(request["backend"], backend(case));
        assert_eq!(request["model"], "switch-old");
    }
    for request in report["all_sdk_requests"].as_array().unwrap() {
        assert_eq!(
            request["session_id"],
            report["before"]["identity"]["session_id"]
        );
        assert_eq!(
            request["agent_id"],
            report["before"]["identity"]["agent_id"]
        );
        assert_eq!(request["backend"], backend(case));
        assert_eq!(request["model"], "switch-old");
        assert!(!request["messages"].to_string().contains("DISCARDED"));
    }
    let releases: Vec<_> = report["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["effect"] == "rewrite-lease-release")
        .collect();
    assert!(!releases.is_empty());
    assert!(
        releases
            .iter()
            .all(|r| r["active_chat"] == json!([]) && r["active_hooks"] == 0)
    );
    report
}
fn assert_unchanged(report: &Value) {
    use sha2::{Digest, Sha256};
    // The reviewed action changes nothing. Orderly shutdown independently
    // publishes the captured empty Work ledger, which is checked in full.
    assert_eq!(report["before"], report["before_shutdown"]);
    let proof = &report["work_shutdown_journal"];
    let path = proof["path"].as_str().expect("owned shutdown ledger path");
    let raw = proof["raw"]
        .as_str()
        .expect("independently read shutdown ledger");
    let journal: Value = serde_json::from_str(raw).unwrap();
    let receipts: Vec<_> = report["responses"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|r| r["result"].get("work_shutdown"))
        .collect();
    assert_eq!(receipts.len(), 1);
    let receipt = receipts[0];
    assert_eq!(receipt["version"], 1);
    assert_eq!(receipt["effects_verified"], false);
    for kind in ["work_capture", "work_active"] {
        let matching: Vec<_> = report["responses"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|r| r["result"]["kind"] == kind)
            .collect();
        assert_eq!(matching.len(), 1);
        assert_eq!(matching[0]["result"]["instance"], receipt["instance"]);
    }
    let identity = &report["before"]["identity"];
    assert_eq!(
        journal,
        json!({
            "version": 1, "revision": 1, "provider": "host",
            "instance": receipt["instance"], "inputs": [], "producers": [],
            "identity": {"session_id": identity["session_id"], "agent_id": identity["agent_id"],
                "backend": identity["backend"], "model": identity["model"]}
        })
    );
    assert_eq!(receipt["closing_sequence"], 1);
    assert_eq!(receipt["final_sequence"], 1);
    for key in ["producer_ids", "inputs", "admission_ids"] {
        assert_eq!(receipt[key], json!([]));
    }
    for key in [
        "admission_closed",
        "worker_settled",
        "callback_settled",
        "process_settled",
    ] {
        assert_eq!(receipt[key], true);
    }
    assert!(path.replace('\\', "/").ends_with(&format!(
        "/{}/native-work-inputs.json",
        identity["session_id"].as_str().unwrap()
    )));
    assert!(report["before"]["disk"].get(path).is_none());
    let mut after = report["after"].clone();
    let persisted_hash = after["disk"].as_object_mut().unwrap().remove(path).unwrap();
    let file_hash = format!("{:x}", Sha256::digest(raw.as_bytes()));
    assert_eq!(persisted_hash, file_hash);
    assert_eq!(receipt["ledger_sha256"], file_hash);
    assert_eq!(report["before"], after);
    assert_eq!(report["live"], report["original"]);
    assert_eq!(report["requests"], json!([]));
    assert_eq!(report["snapshots_before"], report["snapshots_after"]);
}
fn assert_chain(report: &Value, segments: usize) {
    let envelopes: Vec<_> = report["wire_events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|r| r["type"] == "gui.event")
        .collect();
    let starts: Vec<_> = envelopes
        .iter()
        .filter(|r| r["event"]["type"] == "turn_start")
        .collect();
    let ends: Vec<_> = envelopes
        .iter()
        .filter(|r| r["event"]["type"] == "turn_end")
        .collect();
    assert_eq!(starts.len(), segments);
    assert_eq!(ends.len(), segments);
    let mut previous = Value::Null;
    for (index, start) in starts.iter().enumerate() {
        assert_eq!(
            start["event"]["rewrite_segment"],
            json!({"action_id":report["review"]["action_id"],"index":index,"previous_execution_id":previous})
        );
        assert_eq!(start["execution_id"], ends[index]["execution_id"]);
        assert_ne!(start["execution_id"], previous);
        for row in [*start, ends[index]] {
            assert_eq!(row["source"], "rpc");
            assert_eq!(
                row["session_id"],
                report["before"]["identity"]["session_id"]
            );
            assert_eq!(row["operation_id"], report["apply"]["operation_id"]);
        }
        previous = start["execution_id"].clone();
    }
    assert_eq!(
        report["settled"]["execution"],
        json!({"segments":segments,"last_execution_id":previous})
    );
}
#[test]
#[ignore = "controlled actual source rewrite; dependency Python required"]
fn f6_reviewed_edit_retry_and_compaction_keep_exact_context_and_draft() {
    for case in [
        "edit",
        "retry",
        "unicode",
        "hooks-allow",
        "auto-compact",
        "self-compact",
    ] {
        let (runtime, root) = launch(case);
        let replacement = if case == "retry" {
            ORIGINAL
        } else if case == "unicode" {
            UNICODE
        } else {
            REPLACEMENT
        };
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let before = editor(&app);
            select(
                runtime,
                &mut app,
                if case == "retry" {
                    r::Action::Retry
                } else {
                    r::Action::Edit
                },
            )?;
            validate(
                runtime,
                &mut app,
                if case == "retry" {
                    None
                } else {
                    Some(replacement)
                },
            )?;
            assert_eq!(
                app.live.as_ref().unwrap().rewrite.review().unwrap().text,
                replacement
            );
            assert_eq!(editor(&app), before);
            let request = apply(runtime, &mut app)?;
            assert!(app.live.as_ref().unwrap().rewrite_pending());
            settle(runtime, &mut app, &request)?;
            assert_eq!(editor(&app), before);
            let rows = &app.live.as_ref().unwrap().rows;
            assert_eq!(rows.len(), if aftercare(case) { 3 } else { 1 });
            assert_eq!(rows[0].prompt, replacement);
            if aftercare(case) {
                assert!(
                    rows.iter()
                        .any(|r| r.state == ReplyState::MaintenanceSettled)
                );
                let wake = rows.last().unwrap();
                if wake.state != ReplyState::Complete {
                    return Err(io::Error::other(format!(
                        "{case}: wake {:?}: {:?}",
                        wake.state, wake.stop_reason
                    )));
                }
            } else {
                assert_eq!(rows[0].state, ReplyState::Complete);
            }
            assert!(
                rows.iter()
                    .filter(|r| r.state == ReplyState::Complete)
                    .all(|r| r.cards.iter().any(|c| !c.answer.is_empty()))
            );
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, case);
        assert_eq!(report["apply"]["turn"], "accepted");
        assert_eq!(
            report["apply"]["persistence"],
            json!({"snapshot_written":true,"readback_verified":true})
        );
        assert_eq!(report["review"]["text"], replacement);
        assert_eq!(
            report["candidate"].as_array().unwrap().last().unwrap(),
            &json!({"role":"user","content":replacement})
        );
        let before = report["snapshots_before"].as_array().unwrap();
        let after = report["snapshots_after"].as_array().unwrap();
        assert_eq!(&after[..before.len()], before.as_slice());
        assert_eq!(after[before.len()]["messages"], report["candidate"]);
        let first = &report["requests"][0];
        let mut roles: Vec<Value> = report["prefix"]
            .as_array()
            .unwrap()
            .iter()
            .map(|r| r["role"].clone())
            .collect();
        roles.push(json!("user"));
        assert_eq!(first["wire_roles"], Value::Array(roles));
        assert_eq!(first["last"], json!({"role":"user","content":replacement}));
        assert_eq!(
            report["requests"].as_array().unwrap().len(),
            if aftercare(case) { 2 } else { 1 }
        );
        assert_chain(&report, if aftercare(case) { 2 } else { 1 });
        if aftercare(case) {
            let calls = report["all_sdk_requests"].as_array().unwrap();
            assert_eq!(calls[0]["messages"], report["initial_prepared"]);
            assert_eq!(
                calls.iter().find(|r| r["kind"] == "compaction").unwrap()["messages"],
                report["expected_compaction_request"]
            );
            assert_eq!(
                calls.last().unwrap()["messages"],
                report["expected_wake_request"]
            );
            let kinds: Vec<_> = calls.iter().map(|r| r["kind"].as_str().unwrap()).collect();
            assert_eq!(
                kinds,
                if case == "self-compact" {
                    vec!["self-tool", "card-summary", "compaction", "wake"]
                } else {
                    vec!["turn", "compaction", "wake"]
                }
            );
        } else {
            assert_eq!(after.len(), before.len() + 1);
            for field in [
                "settings",
                "authority",
                "handles",
                "store_identity",
                "store_injected",
            ] {
                assert_eq!(report["before"][field], report["after"][field], "{field}");
            }
        }
        if case == "hooks-allow" {
            assert_eq!(report["hook_documents"].as_array().unwrap().len(), 1);
            assert_eq!(report["hook_documents"][0]["event"], "prompt_before");
        }
    }
}
#[test]
#[ignore = "controlled actual source rewrite; dependency Python required"]
fn f6_refusals_leave_current_history_and_composer_intact() {
    for case in ["hooks-deny", "hooks-approval", "held", "pending-image"] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let before = editor(&app);
            let history = app.live.as_ref().unwrap().history.clone();
            select(runtime, &mut app, r::Action::Edit)?;
            if case.starts_with("hooks-") {
                validate(runtime, &mut app, Some(REPLACEMENT))?;
                let request = apply(runtime, &mut app)?;
                settle(runtime, &mut app, &request)?;
                assert_eq!(
                    app.live.as_ref().unwrap().rows[0].state,
                    ReplyState::Refused
                );
            } else {
                assert!(!app.live.as_ref().unwrap().rewrite.editing());
                assert!(app.live.as_ref().unwrap().rewrite.review().is_none());
                assert_eq!(app.live.as_ref().unwrap().history, history);
            }
            assert_eq!(editor(&app), before);
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, case);
        assert_unchanged(&report);
        if case.starts_with("hooks-") {
            assert_eq!(report["apply"]["turn"], "refused");
            assert_eq!(
                report["apply"]["refusal_history"],
                report["settled"]["history"]
            );
            assert_chain(&report, 0);
        } else {
            assert!(
                !report["commands"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|c| c["type"] == r::APPLY)
            );
        }
        if case == "hooks-approval" {
            assert_eq!(report["hook_documents"], json!([]));
            assert_eq!(report["interactive_events"], json!([]));
        }
    }
}
#[test]
#[ignore = "controlled actual source rewrite; dependency Python required"]
fn cancelling_edit_or_review_never_submits_a_turn_or_touches_main_draft() {
    for reviewed in [false, true] {
        let (runtime, root) = launch("edit");
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, "edit")?;
            let before = editor(&app);
            let history = app.live.as_ref().unwrap().history.clone();
            select(runtime, &mut app, r::Action::Edit)?;
            if reviewed {
                validate(runtime, &mut app, Some(REPLACEMENT))?;
            }
            draw(&mut app);
            key(&mut app, KeyCode::Esc);
            if reviewed {
                draw(&mut app);
                key(&mut app, KeyCode::Esc);
            }
            assert!(!app.has_live_rewrite());
            assert!(app.has_live_history());
            assert_eq!(editor(&app), before);
            assert_eq!(app.live.as_ref().unwrap().history, history);
            assert!(app.live.as_mut().unwrap().rewrite.take_intent().is_none());
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "reviewed={reviewed}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, "edit");
        assert_unchanged(&report);
        assert!(
            !report["commands"]
                .as_array()
                .unwrap()
                .iter()
                .any(|c| c["type"] == r::APPLY)
        );
    }
}
#[test]
#[ignore = "controlled actual source rewrite; dependency Python required"]
fn quit_and_stop_join_owned_admission_stream_or_compaction_before_release() {
    for case in [
        "quit-admission",
        "quit-worker",
        "quit-compaction",
        "stop-compaction",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let before = editor(&app);
            select(runtime, &mut app, r::Action::Edit)?;
            validate(runtime, &mut app, Some(REPLACEMENT))?;
            let request = apply(runtime, &mut app)?;
            stage(runtime, &mut app, &root, case.split_once('-').unwrap().1)?;
            assert_eq!(editor(&app), before);
            if case == "stop-compaction" {
                let chat = app.live.as_mut().unwrap();
                assert!(chat.busy && chat.rewrite_pending());
                chat.request_stop()?;
                runtime.abort(chat.take_abort().expect("Stop keeps exact Apply operation"))?;
                settle(runtime, &mut app, &request)?;
                assert_eq!(editor(&app), before);
                assert_eq!(app.live.as_ref().unwrap().rows.len(), 2);
                assert!(
                    app.live.as_ref().unwrap().rows[1].cards[0]
                        .answer
                        .contains("Compaction: cancelled.")
                );
            }
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, case);
        if case == "stop-compaction" {
            assert_eq!(report["compaction_abort_released"], true);
            assert_chain(&report, 1);
            assert_eq!(report["live"], report["before_compaction"]);
        } else {
            let proof = &report["shutdown"];
            for field in ["worker_settled", "admission_settled", "hooks_settled"] {
                assert_eq!(proof[field], true);
            }
            assert_eq!(proof["pending_at_close"], true);
            assert_eq!(proof["effects_verified"], false);
            assert_eq!(proof["operation_id"], report["apply"]["operation_id"]);
            assert_eq!(proof["action_id"], report["review"]["action_id"]);
            assert_eq!(
                proof["session_id"],
                report["before"]["identity"]["session_id"]
            );
            if case == "quit-admission" {
                assert_eq!(report["live"], report["original"]);
                assert_eq!(report["requests"], json!([]));
                assert_eq!(proof["mutated"], false);
            } else {
                assert_eq!(proof["mutated"], true);
            }
            if case == "quit-compaction" {
                assert_eq!(report["live"], report["before_compaction"]);
            }
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
    assert!(rendered.contains("Which continuation is authorized?"));
    assert!(rendered.contains("Keep reviewed context"));
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
fn assert_question_retirement(report: &Value, compaction: bool, cancelled: bool) {
    let asked = &report["question_request"];
    let events: Vec<_> = report["wire_events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|row| row["type"] == "gui.event")
        .collect();
    let requests: Vec<_> = events
        .iter()
        .copied()
        .filter(|row| row["event"]["type"] == "user_input_requested")
        .collect();
    assert_eq!(requests, vec![asked]);
    assert_eq!(asked["operation_id"], report["apply"]["operation_id"]);
    assert_eq!(
        asked["session_id"],
        report["before"]["identity"]["session_id"]
    );
    assert_eq!(asked["source"], "rpc");
    let parent_type = if compaction {
        "rewrite_aftercare"
    } else {
        "turn_start"
    };
    let owner = events
        .iter()
        .find(|row| row["event"]["type"] == parent_type)
        .unwrap();
    assert_eq!(asked["execution_id"], owner["execution_id"]);
    assert_eq!(
        asked["event"]["questions"][0]["question"],
        "Which continuation is authorized?"
    );
    assert_eq!(asked["event"]["questions"][0]["multiSelect"], false);
    let threads = report["question_threads"].as_array().unwrap();
    let futures = report["question_futures"].as_array().unwrap();
    assert_eq!(threads.len(), 1);
    assert_eq!(futures.len(), 1);
    let thread = &threads[0];
    let future = &futures[0];
    assert!(thread["thread_id"].as_u64().is_some_and(|id| id > 0));
    assert_eq!(thread["retired"], true);
    for field in ["done", "producer_done", "callback_retired"] {
        assert_eq!(future[field], true, "{field}");
    }
    assert_eq!(future["cancelled"], false);
    assert_eq!(future["lifetime_cancelled"], cancelled);
    assert_eq!(thread["origin"], future["origin"]);
    for field in [
        "operation_id",
        "execution_id",
        "session_id",
        "source",
        "hook_id",
    ] {
        assert_eq!(thread["origin"][field], asked[field], "{field}");
    }
    assert_eq!(report["question_pending"], json!([]));
    let commands = report["commands"].as_array().unwrap();
    let answers: Vec<_> = commands
        .iter()
        .filter(|command| command["type"] == "answer")
        .collect();
    let result = thread["result"].as_str().unwrap();
    if cancelled {
        assert!(answers.is_empty());
        assert!(result.contains("ABORTED"));
        assert!(!result.contains("SUBMITTED"));
        assert!(!result.contains(QUESTION_NOTE));
        let resolved: Vec<_> = events
            .iter()
            .copied()
            .filter(|row| row["event"]["type"] == "user_input_resolved")
            .collect();
        assert_eq!(resolved.len(), 1);
        assert_eq!(resolved[0]["event"]["id"], asked["event"]["id"]);
        assert_eq!(resolved[0]["event"]["cancelled"], true);
        for field in [
            "operation_id",
            "execution_id",
            "session_id",
            "source",
            "hook_id",
        ] {
            assert_eq!(resolved[0][field], asked[field], "{field}");
        }
    } else {
        assert!(result.contains("SUBMITTED — 1 of 1 answered"));
        assert!(result.contains(QUESTION_NOTE));
        assert_eq!(answers.len(), 1);
        let answer = answers[0];
        assert_eq!(answer["ask_id"], asked["event"]["id"]);
        assert_eq!(answer["action"], "submit");
        assert_eq!(
            answer["answers"],
            json!([{"selected":[0],"note":QUESTION_NOTE}])
        );
        let receipts: Vec<_> = report["responses"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["id"] == answer["id"])
            .collect();
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0]["ok"], true);
        assert_eq!(receipts[0]["result"]["answered"], asked["event"]["id"]);
        assert_eq!(receipts[0]["result"]["id_key"], "ask_id");
    }
}
#[test]
#[ignore = "controlled actual source rewrite questions; dependency Python required"]
fn native_question_answers_keep_draft_and_exact_stream_or_compaction_context() {
    for case in ["question-stream", "question-compaction"] {
        let compaction = case.ends_with("compaction");
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let before = editor(&app);
            select(runtime, &mut app, r::Action::Edit)?;
            validate(runtime, &mut app, Some(REPLACEMENT))?;
            let request = apply(runtime, &mut app)?;
            stage(runtime, &mut app, &root, case)?;
            assert!(app.has_live_question());
            assert!(!app.has_live_rewrite());
            assert!(app.live.as_ref().unwrap().rewrite_pending());
            assert!(app.live.as_ref().unwrap().busy);
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("Unsent while the question is pending")
                    .is_err()
            );
            assert_eq!(editor(&app), before);
            let id = answer_question(runtime, &mut app)?;
            let marker: Value =
                serde_json::from_slice(&fs::read(root.join("rewrite-stage.json"))?)?;
            assert_eq!(marker["ask_id"], id);
            assert_eq!(editor(&app), before);
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("Unsent before question settlement")
                    .is_err()
            );
            settle(runtime, &mut app, &request)?;
            assert!(!app.has_live_question());
            assert_eq!(editor(&app), before);
            let chat = app.live.as_ref().unwrap();
            assert_eq!(chat.rows.len(), if compaction { 3 } else { 1 });
            assert_eq!(chat.rows.last().unwrap().state, ReplyState::Complete);
            if compaction {
                assert!(
                    chat.rows
                        .iter()
                        .any(|row| row.state == ReplyState::MaintenanceSettled)
                );
            }
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, case);
        assert_question_retirement(&report, compaction, false);
        assert_eq!(report["apply"]["turn"], "accepted");
        assert_eq!(report["apply"]["close_required"], false);
        assert_eq!(report["settled"]["worker_settled"], true);
        assert_eq!(report["requests"].as_array().unwrap().len(), 2);
        assert_chain(&report, if compaction { 2 } else { 1 });
        let calls = report["all_sdk_requests"].as_array().unwrap();
        let kinds: Vec<_> = calls
            .iter()
            .map(|row| row["kind"].as_str().unwrap())
            .collect();
        assert_eq!(
            kinds,
            if compaction {
                vec!["turn", "compaction-question", "compaction-followup", "wake"]
            } else {
                vec!["question-stream", "question-followup"]
            }
        );
        assert_eq!(calls[0]["messages"], report["initial_prepared"]);
        let followup = calls
            .iter()
            .find(|row| row["kind"].as_str().unwrap().ends_with("followup"))
            .unwrap();
        assert_eq!(followup["messages"], report["expected_question_followup"]);
        assert_eq!(
            followup["messages"].as_array().unwrap().last().unwrap()["content"],
            report["question_threads"][0]["result"]
        );
        if compaction {
            assert_eq!(calls[1]["messages"], report["expected_compaction_request"]);
            assert_eq!(
                calls.last().unwrap()["messages"],
                report["expected_wake_request"]
            );
        } else {
            let candidate = report["candidate"].as_array().unwrap();
            assert_eq!(
                &report["live"].as_array().unwrap()[..candidate.len()],
                candidate.as_slice()
            );
        }
    }
}
#[test]
#[ignore = "controlled actual source rewrite questions; dependency Python required"]
fn quitting_pending_native_questions_joins_threads_and_callbacks_without_answer_or_wake() {
    for case in ["quit-question-stream", "quit-question-compaction"] {
        let compaction = case.ends_with("compaction");
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let before = editor(&app);
            select(runtime, &mut app, r::Action::Edit)?;
            validate(runtime, &mut app, Some(REPLACEMENT))?;
            apply(runtime, &mut app)?;
            stage(
                runtime,
                &mut app,
                &root,
                case.strip_prefix("quit-").unwrap(),
            )?;
            assert!(draw(&mut app).contains("Which continuation is authorized?"));
            assert!(app.has_live_question());
            assert!(!app.has_live_rewrite());
            let chat = app.live.as_mut().unwrap();
            assert!(chat.busy && chat.rewrite_pending());
            assert!(chat.submit("Unsent during pending question").is_err());
            assert!(chat.questions.take_decision().is_none());
            assert_eq!(editor(&app), before);
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, case);
        assert_question_retirement(&report, compaction, true);
        let proof = &report["shutdown"];
        for field in [
            "worker_settled",
            "admission_settled",
            "hooks_settled",
            "pending_at_close",
            "mutated",
        ] {
            assert_eq!(proof[field], true, "{field}");
        }
        assert_eq!(proof["effects_verified"], false);
        assert_eq!(proof["operation_id"], report["apply"]["operation_id"]);
        assert_eq!(proof["action_id"], report["review"]["action_id"]);
        assert_eq!(proof["action"], "edit");
        assert_eq!(
            proof["session_id"],
            report["before"]["identity"]["session_id"]
        );
        assert_eq!(proof["current_session_id"], proof["session_id"]);
        assert_eq!(proof["agent_id"], report["before"]["identity"]["agent_id"]);
        assert_eq!(proof["backend"], "lmstudio");
        assert_eq!(proof["model"], "switch-old");
        assert_eq!(report["requests"].as_array().unwrap().len(), 1);
        let calls = report["all_sdk_requests"].as_array().unwrap();
        let kinds: Vec<_> = calls
            .iter()
            .map(|row| row["kind"].as_str().unwrap())
            .collect();
        assert_eq!(
            kinds,
            if compaction {
                vec!["turn", "compaction-question"]
            } else {
                vec!["question-stream"]
            }
        );
        if compaction {
            assert_eq!(report["live"], report["before_compaction"]);
        } else {
            let candidate = report["candidate"].as_array().unwrap();
            assert_eq!(
                &report["live"].as_array().unwrap()[..candidate.len()],
                candidate.as_slice()
            );
        }
    }
}

#[test]
#[ignore = "controlled actual source rewrite questions; dependency Python required"]
fn stopping_pending_native_questions_settles_without_answer_or_wake() {
    for case in ["stop-question-stream", "stop-question-compaction"] {
        let compaction = case.ends_with("compaction");
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime, case)?;
            let before = editor(&app);
            select(runtime, &mut app, r::Action::Edit)?;
            validate(runtime, &mut app, Some(REPLACEMENT))?;
            let request = apply(runtime, &mut app)?;
            stage(
                runtime,
                &mut app,
                &root,
                case.strip_prefix("stop-").unwrap(),
            )?;
            assert!(app.has_live_question());
            let chat = app.live.as_mut().unwrap();
            assert!(chat.busy && chat.rewrite_pending());
            assert!(chat.questions.take_decision().is_none());
            chat.request_stop()?;
            runtime.abort(
                chat.take_abort()
                    .expect("Stop owns the exact reviewed action"),
            )?;
            settle(runtime, &mut app, &request)?;
            assert!(!app.has_live_question());
            assert_eq!(editor(&app), before);
            let rows = &app.live.as_ref().unwrap().rows;
            assert_eq!(rows.len(), if compaction { 2 } else { 1 });
            assert_eq!(
                rows.last().unwrap().state,
                if compaction {
                    ReplyState::MaintenanceSettled
                } else {
                    ReplyState::Cancelled
                }
            );
            runtime.quit()
        });
        assert!(
            result.is_ok(),
            "{case}: {result:?}; artifacts {}",
            root.display()
        );
        let report = report(&root, case);
        assert_question_retirement(&report, compaction, true);
        assert_chain(&report, 1);
        assert_eq!(report["apply"]["turn"], "accepted");
        assert_eq!(report["settled"]["worker_settled"], true);
        let commands = report["commands"].as_array().unwrap();
        let aborts: Vec<_> = commands
            .iter()
            .filter(|row| row["type"] == "abort")
            .collect();
        assert_eq!(aborts.len(), 1);
        let receipts: Vec<_> = report["responses"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|row| row["id"] == aborts[0]["id"])
            .collect();
        assert_eq!(receipts.len(), 1);
        assert_eq!(receipts[0]["ok"], true);
        assert_eq!(receipts[0]["result"]["stopped"], true);
        assert_eq!(
            receipts[0]["result"]["cancelled_asks"],
            json!([report["question_request"]["event"]["id"]])
        );
        let calls = report["all_sdk_requests"].as_array().unwrap();
        let kinds: Vec<_> = calls
            .iter()
            .map(|row| row["kind"].as_str().unwrap())
            .collect();
        assert_eq!(
            kinds,
            if compaction {
                vec!["turn", "compaction-question"]
            } else {
                vec!["question-stream"]
            }
        );
        assert_eq!(report["requests"].as_array().unwrap().len(), 1);
        if compaction {
            assert_eq!(report["live"], report["before_compaction"]);
        } else {
            let candidate = report["candidate"].as_array().unwrap();
            let live = report["live"].as_array().unwrap();
            assert_eq!(&live[..candidate.len()], candidate.as_slice());
            assert_eq!(live.len(), candidate.len() + 2);
            assert_eq!(
                live.last().unwrap()["content"],
                report["question_threads"][0]["result"]
            );
        }
    }
}
