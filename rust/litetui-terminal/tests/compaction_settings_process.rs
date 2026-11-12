#![cfg(windows)]
//! F10 compaction preferences through the actual owned source and native controls.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    compaction_settings as edit,
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options},
    settings_view, ui,
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
fn source() -> PathBuf {
    std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.."))
        .canonicalize()
        .unwrap()
}
fn launch(case: &str, field: &str, value: &Value) -> (Interactive, PathBuf) {
    let actual = source();
    let fixtures = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("tests/fixtures");
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/compaction-settings-process-tests")
        .join(format!(
            "{}-{}-{}-{case}-{field}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
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
os.environ['RUST_COMPACT_SETTINGS_SOURCE'] = {}
os.environ['RUST_COMPACT_SETTINGS_CASE'] = {}
os.environ['RUST_COMPACT_SETTINGS_DRIVER'] = 'rpc'
os.environ['RUST_COMPACT_SETTINGS_FIELD'] = {}
os.environ['RUST_COMPACT_SETTINGS_VALUE'] = {}
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ.pop('LITETUI_MAX_TOKENS', None)
os.environ.pop('LITETUI_TEST_COMPACT_CAP', None)
p = Path(os.environ['LITETUI_DATA_ROOT']) / 'settings.json'
s = json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True, skills_enabled=False, footer_task_manager=False, tts_enabled=False, sidecar_enabled=False, autocompact_enabled=False, tool_auto_background_s=0, custom_context_length=1000000, compact_keep_recent=4, compact_max_tokens=12288, compact_max_tool_iters=8, compact_thinking_level='low', wake_after_compact=False, clear_screen_after_compact=True)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,quote(&actual.to_string_lossy()),quote(case),quote(field),quote(&value.to_string()),quote(&fixtures.join("compaction_settings_runtime.py").to_string_lossy()))).unwrap();
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
        "--tools".into(),
        "off".into(),
        "--timeout".into(),
        "90".into(),
        "--server-timeout".into(),
        "5".into(),
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
                    return Err(done.err().unwrap_or_else(|| {
                        io::Error::other("Unexpected preference runtime exit")
                    }));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Preference observation deadline"));
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
fn wait_editor(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    wait(runtime, app, |e| {
        matches!(
            e,
            Observation::Settings(
                settings_view::Update::Compaction(_)
                    | settings_view::Update::CompactionEdited { .. }
            )
        )
    })
}
fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn surface(app: &mut Demo) {
    let mut composed = Terminal::new(TestBackend::new(120, 40)).unwrap();
    composed.draw(|f| ui::draw(f, app)).unwrap();
    let theme = app.palette().clone();
    let mut terminal = Terminal::new(TestBackend::new(120, 40)).unwrap();
    terminal
        .draw(|f| {
            app.live
                .as_mut()
                .unwrap()
                .settings
                .compaction
                .draw(f, f.area(), &theme)
        })
        .unwrap();
    // The preferences intentionally cover a centered 100x32 popup, with the
    // live header/composer still visible around it. Compare every painted cell
    // inside that popup, including blanks and styles, rather than blanking the
    // surrounding live UI in the expected buffer.
    for y in 4..36 {
        for x in 10..110 {
            assert_eq!(
                composed.backend().buffer()[(x, y)],
                terminal.backend().buffer()[(x, y)],
                "Nested preference controls must own popup cell {x},{y}"
            );
        }
    }
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
    app.insert_text("KEEP α 雪 COMPOSER AND SELECTION");
    app.composer.move_cursor(Motion::WordLeft, true);
    wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
    Ok(app)
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .settings
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<u64> {
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .settings
        .take_intent()
        .expect("Explicit native settings intent");
    let generation = match &intent {
        settings_view::Intent::Compaction(i) => i.generation(),
        _ => 0,
    };
    runtime.settings(intent)?;
    Ok(generation)
}
fn open_parent(runtime: &mut Interactive, app: &mut Demo, field: &str) -> io::Result<()> {
    app.open_settings();
    send(runtime, app)?;
    wait(runtime, app, |e| {
        matches!(e, Observation::Settings(settings_view::Update::Snapshot(_)))
    })?;
    draw(app);
    let index = app
        .live
        .as_ref()
        .unwrap()
        .settings
        .snapshot
        .as_ref()
        .unwrap()
        .rows
        .iter()
        .position(|r| r.name == field)
        .expect("Canonical compaction preference in F10");
    key(app, KeyCode::Home);
    for _ in 0..index {
        key(app, KeyCode::Down);
    }
    draw(app);
    Ok(())
}
fn open(runtime: &mut Interactive, app: &mut Demo, field: &str, via_c: bool) -> io::Result<u64> {
    open_parent(runtime, app, field)?;
    key(app, KeyCode::Char(if via_c { 'c' } else { 'e' }));
    let generation = send(runtime, app)?;
    wait_editor(runtime, app)?;
    surface(app);
    if via_c {
        for _ in 0..edit::FIELDS.iter().position(|f| *f == field).unwrap() {
            key(app, KeyCode::Down);
        }
    }
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .settings
            .compaction
            .selected_field(),
        field
    );
    draw(app);
    Ok(generation)
}
fn set_value(app: &mut Demo, value: &Value) {
    let field = app
        .live
        .as_ref()
        .unwrap()
        .settings
        .compaction
        .selected_field();
    if value.is_u64() {
        key(app, KeyCode::Home);
        for _ in 0..10 {
            key(app, KeyCode::Delete);
        }
        app.live
            .as_mut()
            .unwrap()
            .settings
            .paste(&value.to_string());
    } else {
        let expected = value
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| value.to_string());
        for _ in 0..10 {
            if app.live.as_ref().unwrap().settings.compaction.draft() == expected {
                break;
            }
            key(app, KeyCode::Right);
        }
        assert_eq!(
            app.live.as_ref().unwrap().settings.compaction.draft(),
            expected
        );
    }
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .settings
            .compaction
            .selected_field(),
        field
    );
}
fn validate(runtime: &mut Interactive, app: &mut Demo) -> io::Result<u64> {
    draw(app);
    key(app, KeyCode::Enter);
    let generation = send(runtime, app)?;
    wait_editor(runtime, app)?;
    Ok(generation)
}
fn dispatch_save(runtime: &Interactive, app: &mut Demo) -> io::Result<u64> {
    key(app, KeyCode::Enter);
    assert!(
        app.live.as_mut().unwrap().settings.take_intent().is_none(),
        "Review must be painted before confirmation"
    );
    for _ in 0..30 {
        surface(app);
        key(app, KeyCode::Enter);
        if let Some(intent) = app.live.as_mut().unwrap().settings.take_intent() {
            let settings_view::Intent::Compaction(edit::Intent::Save {
                generation,
                ref review,
            }) = intent
            else {
                panic!("Expected Save")
            };
            assert!(!review.token.is_empty());
            runtime.settings(intent)?;
            return Ok(generation);
        }
        draw(app);
        key(app, KeyCode::PageDown);
    }
    Err(io::Error::other(
        "All preference review pages did not reach confirmation",
    ))
}
fn parent_selection(app: &mut Demo, field: &str) {
    key(app, KeyCode::Esc);
    assert!(app.live.as_ref().unwrap().settings.open);
    draw(app);
    key(app, KeyCode::Char('e'));
    assert_eq!(
        app.live
            .as_ref()
            .unwrap()
            .settings
            .compaction
            .selected_field(),
        field,
        "Underlying F10 selection must survive nested editing"
    );
    key(app, KeyCode::Esc);
    assert!(
        app.live.as_mut().unwrap().settings.take_intent().is_none(),
        "Cancelled undispatched reopen must not leak a read"
    );
}
fn report(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("compaction-settings-report.json")).unwrap())
            .unwrap();
    for (key, file) in [
        ("source_app", "app.py"),
        ("source_preferences", "native_compaction_settings.py"),
    ] {
        assert_eq!(
            PathBuf::from(r[key].as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join("src/litetui")
                .join(file)
                .canonicalize()
                .unwrap()
        );
    }
    for key in [
        "denied",
        "sdk_requests",
        "streams",
        "clear_screen_calls",
        "question_pending",
        "approval_pending",
        "approval_authorities",
    ] {
        assert_eq!(r[key], json!([]), "{key} at {}", root.display());
    }
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    let leases = r["lease_release"].as_array().unwrap();
    assert!(!leases.is_empty());
    assert!(leases.iter().all(|r| r["active_chat"] == json!([])
        && r["questions"] == json!([])
        && r["approvals"] == json!([])));
    assert_eq!(r["archive_prefix_preserved"], true);
    assert_eq!(r["durable"], r["after"]["history"]);
    assert_eq!(r["before"]["history"], r["after"]["history"]);
    for key in ["identity", "authority", "tools", "pending", "handles"] {
        assert_eq!(r["before"][key], r["after"][key], "{key}");
    }
    let commands = r["commands"].as_array().unwrap();
    for c in commands {
        assert!(
            matches!(
                c["type"].as_str(),
                Some(
                    "gui.hello"
                        | "gui.state"
                        | "gui.themes.native_snapshot"
                        | "gui.history.native_snapshot"
                        | "gui.history.native_page"
                        | litetui_terminal::work_lifecycle::CAPTURE
                        | litetui_terminal::work_lifecycle::ACTIVATE
                        | edit::READ
                        | edit::VALIDATE
                        | edit::SAVE
                        | "shutdown"
                )
            ),
            "Unexpected native operation {c}"
        );
        if c["type"] == "gui.state" {
            assert_eq!(c["include_conversations"], false);
        }
    }
    for response in r["responses"].as_array().unwrap() {
        if commands.iter().any(|c| {
            c["id"] == response["id"]
                && matches!(
                    c["type"].as_str(),
                    Some(edit::READ | edit::VALIDATE | edit::SAVE)
                )
        }) {
            assert!(serde_json::to_vec(response).unwrap().len() <= edit::MAX_BYTES);
        }
    }
    r
}
// The Work handshake creates its own empty ledger on clean shutdown. Its bytes
// must describe this exact activation. Every other file must match the baseline.
fn assert_only_idle_ledger_published(r: &Value, root: &Path, baseline: &Value) {
    use sha2::{Digest, Sha256};

    let responses = r["responses"].as_array().unwrap();
    let captures: Vec<_> = responses
        .iter()
        .filter_map(|row| {
            let value = &row["result"];
            (row["ok"] == true && value["kind"] == "work_capture").then_some(value)
        })
        .collect();
    let [capture] = captures.as_slice() else {
        panic!("expected one Work capture")
    };
    let identity = &capture["identity"];
    let session = identity["session_id"].as_str().unwrap();
    assert_eq!(session, r["before"]["identity"]["session_id"]);
    let relative = Path::new(".agents")
        .join("RustLive")
        .join("conversations")
        .join(session)
        .join("native-work-inputs.json");
    let bytes = fs::read(root.join(&relative)).unwrap();
    let stored: Value = serde_json::from_slice(&bytes).unwrap();
    assert_eq!(
        stored,
        json!({
            "version": 1, "revision": 1, "provider": capture["provider"],
            "instance": capture["instance"], "identity": identity, "producers": [], "inputs": [],
        })
    );
    let key = relative.to_str().unwrap();
    let hash = json!(format!("{:x}", Sha256::digest(&bytes)));
    let mut expected = baseline.clone();
    if let Some(previous) = expected
        .as_object_mut()
        .unwrap()
        .insert(key.to_owned(), hash.clone())
    {
        assert_eq!(previous, hash);
    }
    assert_eq!(r["final_disk"], expected);
}

fn count(r: &Value, operation: &str) -> usize {
    r["commands"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|c| c["type"] == operation)
        .count()
}
fn exact_save(r: &Value, field: &str, value: &Value, partial: bool) {
    let rows = r["saves"].as_array().unwrap();
    assert_eq!(rows.len(), 1);
    let receipt = &rows[0];
    assert_eq!(receipt["status"], if partial { "partial" } else { "saved" });
    assert_eq!(receipt["saved"], *value);
    assert_eq!(
        receipt["effective"],
        if partial {
            r["before"]["settings"][field].clone()
        } else {
            value.clone()
        }
    );
    let destination = if field == "clear_screen_after_compact" {
        "global"
    } else {
        "conversation"
    };
    let raw = &r["initial_documents"][destination]["raw"];
    let mut expected: Value = serde_json::from_str(raw.as_str().unwrap()).unwrap();
    if destination == "global" {
        expected[field] = value.clone();
    } else {
        expected["execution"][field] = value.clone();
    }
    // Develop's conversation_document materializes every missing conversation key from the
    // inherited settings; an inherited unset field lands as an explicit null (no value change).
    let mut saved: Value =
        serde_json::from_str(r["documents"][destination]["raw"].as_str().unwrap()).unwrap();
    for doc in [&mut saved, &mut expected] {
        if let Some(map) = doc["execution"].as_object_mut() {
            map.retain(|_, v| !v.is_null());
        }
    }
    assert_eq!(saved, expected);
    assert_eq!(receipt["destination"], r["documents"][destination]["path"]);
    assert_eq!(
        receipt["persistence"][0]["destination"],
        receipt["destination"]
    );
    for key in ["global", "conversation", "agent"] {
        if key != destination {
            assert_eq!(r["documents"][key], r["initial_documents"][key]);
        }
    }
    let mut settings = r["before"]["settings"].clone();
    if !partial {
        settings[field] = value.clone();
    }
    assert_eq!(r["after"]["settings"], settings);
    let snapshots = r["snapshots"].as_array().unwrap();
    let after = snapshots.last().unwrap();
    assert_eq!(after["fields"][field]["saved"], *value);
    assert_eq!(after["fields"][field]["effective"], receipt["effective"]);
    let commands = r["commands"].as_array().unwrap();
    let position = commands
        .iter()
        .position(|c| c["type"] == edit::SAVE)
        .unwrap();
    assert_eq!(commands[position + 1]["type"], edit::READ);
    assert_eq!(commands[position + 2]["type"], "gui.state");
}
fn scenario(case: &str, field: &str, value: Value, via_c: bool) {
    let (runtime, root) = launch(case, field, &value);
    let fatal = matches!(
        case,
        "silent" | "torn" | "wrong-field" | "drop-unknown" | "write-then-error" | "runtime-raised"
    );
    let refused = matches!(case, "stale" | "persistence-refused");
    let partial = case == "runtime-failed";
    let mut succeeded = false;
    let result = runtime.with_cleanup(|runtime| {
        let mut app = start(runtime)?;
        let original = editor(&app);
        let rows = app.live.as_ref().unwrap().rows.len();
        let g = open(runtime, &mut app, field, via_c)?;
        let before_effective = app
            .live
            .as_ref()
            .unwrap()
            .settings
            .compaction
            .snapshot
            .as_ref()
            .unwrap()
            .field(field)
            .unwrap()
            .effective
            .clone();
        set_value(&mut app, &value);
        assert_eq!(editor(&app), original);
        let v = validate(runtime, &mut app)?;
        assert!(v > g);
        let s = dispatch_save(runtime, &mut app)?;
        assert!(s > v);
        wait_editor(runtime, &mut app)?;
        assert!(
            !fatal,
            "Uncertain save must close before publishing a successful editor result"
        );
        assert_eq!(editor(&app), original);
        assert_eq!(app.live.as_ref().unwrap().rows.len(), rows);
        let prefs = &app.live.as_ref().unwrap().settings.compaction;
        if refused {
            assert!(prefs.snapshot.is_none());
            assert!(prefs.notice.contains("F5"));
            assert_eq!(
                prefs.draft(),
                value
                    .as_str()
                    .map(str::to_owned)
                    .unwrap_or_else(|| value.to_string())
            );
        } else {
            let setting = prefs.snapshot.as_ref().unwrap().field(field).unwrap();
            assert_eq!(setting.saved, value);
            assert_eq!(
                setting.effective,
                if partial {
                    before_effective
                } else {
                    value.clone()
                }
            );
            succeeded = true;
        }
        parent_selection(&mut app, field);
        assert_eq!(editor(&app), original);
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
        "{case} {field}: {result:?} at {}",
        root.display()
    );
    assert_eq!(succeeded, !fatal && !refused);
    let r = report(&root);
    assert_eq!(count(&r, edit::SAVE), 1, "No automatic save retry");
    if refused {
        assert_eq!(r["saves"][0]["status"], "refused");
        assert_eq!(r["before"], r["after"]);
        let baseline = if case == "stale" {
            &r["stale_disk"]
        } else {
            &r["initial_disk"]
        };
        assert_only_idle_ledger_published(&r, &root, baseline);
    } else if fatal {
        assert_eq!(r["saves"][0]["status"], "failed");
        assert_eq!(r["saves"][0]["close_required"], true);
        assert_eq!(count(&r, edit::READ), 1);
    } else {
        exact_save(&r, field, &value, partial);
    }
}
#[test]
#[ignore = "controlled actual-source preference edits; dependency Python required"]
fn f10_eight_typed_scoped_preferences_save_without_inference_and_keep_composer() {
    for (i, (field, value)) in edit::FIELDS
        .iter()
        .zip([
            json!(2),
            json!(768),
            json!(3),
            json!("high"),
            json!(true),
            json!(false),
            json!(true),
            json!(65),
        ])
        .enumerate()
    {
        scenario("success", field, value, i % 2 == 0);
    }
}
#[test]
#[ignore = "controlled actual-source preference edits; dependency Python required"]
fn f10_editor_review_cancel_and_refresh_keep_drafts_without_publishing() {
    for mode in ["editor", "review", "refresh"] {
        let (runtime, root) = launch("cancel", "compact_max_tokens", &json!(768));
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime)?;
            let original = editor(&app);
            let g = open(runtime, &mut app, "compact_max_tokens", true)?;
            set_value(&mut app, &json!(768));
            if mode != "editor" {
                let next = validate(runtime, &mut app)?;
                assert!(next > g);
                if mode == "refresh" {
                    surface(&mut app);
                    key(&mut app, KeyCode::F(5));
                    let refreshed = send(runtime, &mut app)?;
                    assert!(refreshed > next);
                    wait_editor(runtime, &mut app)?;
                    assert_eq!(
                        app.live.as_ref().unwrap().settings.compaction.draft(),
                        "768"
                    );
                    let next = validate(runtime, &mut app)?;
                    assert!(next > refreshed);
                }
                key(&mut app, KeyCode::Esc);
                assert!(app.live.as_ref().unwrap().settings.compaction.open);
                assert_eq!(
                    app.live.as_ref().unwrap().settings.compaction.draft(),
                    "768"
                );
            }
            parent_selection(&mut app, "compact_max_tokens");
            assert_eq!(editor(&app), original);
            Ok(())
        });
        assert!(result.is_ok(), "{mode}: {result:?} at {}", root.display());
        let r = report(&root);
        assert_eq!(count(&r, edit::SAVE), 0);
        assert_only_idle_ledger_published(&r, &root, &r["initial_disk"]);
        assert_eq!(r["before"], r["after"]);
        assert_eq!(
            count(&r, edit::VALIDATE),
            if mode == "editor" {
                0
            } else if mode == "review" {
                1
            } else {
                2
            }
        );
    }
}
#[test]
#[ignore = "controlled actual-source preference edits; dependency Python required"]
fn f10_autocompact_percent_out_of_range_is_refused_before_any_source_operation() {
    let (runtime, root) = launch("cancel", "autocompact_at_percent", &json!(100));
    let result = runtime.with_cleanup(|runtime| {
        let mut app = start(runtime)?;
        let original = editor(&app);
        open(runtime, &mut app, "autocompact_at_percent", true)?;
        for bad in [0, 100] {
            set_value(&mut app, &json!(bad));
            draw(&mut app);
            key(&mut app, KeyCode::Enter);
            assert!(
                app.live.as_mut().unwrap().settings.take_intent().is_none(),
                "{bad} must be refused before the source is asked"
            );
            let prefs = &app.live.as_ref().unwrap().settings.compaction;
            assert!(!prefs.waiting && prefs.open);
            assert_eq!(prefs.draft(), bad.to_string());
            assert!(prefs.notice.contains("invalid scalar type or value"));
        }
        parent_selection(&mut app, "autocompact_at_percent");
        assert_eq!(editor(&app), original);
        Ok(())
    });
    assert!(result.is_ok(), "{result:?} at {}", root.display());
    let r = report(&root);
    assert_eq!(count(&r, edit::VALIDATE), 0);
    assert_eq!(count(&r, edit::SAVE), 0);
    assert_only_idle_ledger_published(&r, &root, &r["initial_disk"]);
    assert_eq!(r["before"], r["after"]);
}
#[test]
#[ignore = "controlled actual-source preference edits; dependency Python required"]
fn f10_refusals_and_known_runtime_failure_have_honest_results() {
    scenario("stale", "compact_max_tokens", json!(768), true);
    scenario(
        "persistence-refused",
        "clear_screen_after_compact",
        json!(false),
        false,
    );
    scenario("runtime-failed", "compact_max_tokens", json!(768), true);
    scenario(
        "runtime-failed",
        "clear_screen_after_compact",
        json!(false),
        false,
    );
}
#[test]
#[ignore = "controlled actual-source preference edits; dependency Python required"]
fn f10_uncertain_publication_and_apply_fail_closed_without_success_or_retry() {
    for (case, device) in [
        ("silent", false),
        ("torn", true),
        ("wrong-field", false),
        ("drop-unknown", true),
        ("write-then-error", false),
        ("runtime-raised", true),
    ] {
        let field = if device {
            "clear_screen_after_compact"
        } else {
            "compact_max_tokens"
        };
        scenario(
            case,
            field,
            if device { json!(false) } else { json!(768) },
            true,
        );
    }
}
fn stage(
    runtime: &mut Interactive,
    app: &mut Demo,
    root: &Path,
    expected: &str,
) -> io::Result<Value> {
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        let path = root.join("compaction-settings-stage.json");
        if let Ok(raw) = fs::read(&path)
            && let Ok(value) = serde_json::from_slice::<Value>(&raw)
            && value["stage"] == expected
        {
            return Ok(value);
        }
        while let Ok(event) = runtime.events.try_recv() {
            assert!(
                !matches!(
                    event,
                    Observation::Settings(settings_view::Update::CompactionEdited { .. })
                ),
                "Delayed receipt cannot publish success"
            );
            app.observe_live(event)?;
        }
        if let Some(done) = runtime.poll_done() {
            return Err(done
                .err()
                .unwrap_or_else(|| io::Error::other("Unexpected delayed preference exit")));
        }
        if Instant::now() > deadline {
            return Err(io::Error::other("Delayed preference stage deadline"));
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}
#[test]
#[ignore = "controlled actual-source preference edits; dependency Python required"]
fn f10_quit_consumes_late_read_or_saved_receipt_without_new_readbacks_or_success() {
    for case in ["delay-snapshot", "delay-save"] {
        let (runtime, root) = launch(case, "compact_max_tokens", &json!(768));
        let mut published = false;
        let mut stage_marker = Value::Null;
        let result = runtime.with_cleanup(|runtime| {
            let mut app = start(runtime)?;
            let original = editor(&app);
            if case == "delay-snapshot" {
                open_parent(runtime, &mut app, "compact_max_tokens")?;
                key(&mut app, KeyCode::Char('c'));
                send(runtime, &mut app)?;
            } else {
                open(runtime, &mut app, "compact_max_tokens", true)?;
                set_value(&mut app, &json!(768));
                validate(runtime, &mut app)?;
                dispatch_save(runtime, &mut app)?;
            }
            let marker = stage(
                runtime,
                &mut app,
                &root,
                if case == "delay-save" {
                    "save-response"
                } else {
                    "snapshot-response"
                },
            )?;
            assert!(
                marker["operation_id"]
                    .as_str()
                    .is_some_and(|s| !s.is_empty())
            );
            if case == "delay-save" {
                assert_eq!(marker["field"], "compact_max_tokens");
                assert_eq!(marker["value"], 768);
                assert_eq!(marker["status"], "saved");
            }
            stage_marker = marker;
            runtime.quit()?;
            let deadline = Instant::now() + Duration::from_secs(45);
            loop {
                match runtime.events.recv_timeout(Duration::from_millis(50)) {
                    Ok(event) => {
                        published |= matches!(
                            event,
                            Observation::Settings(settings_view::Update::CompactionEdited { .. })
                        );
                        app.observe_live(event)?;
                    }
                    Err(_) => {
                        if let Some(done) = runtime.poll_done() {
                            done?;
                            break;
                        }
                        if Instant::now() > deadline {
                            return Err(io::Error::other("Late preference shutdown deadline"));
                        }
                    }
                }
            }
            assert_eq!(editor(&app), original);
            assert!(!published);
            Ok(())
        });
        assert!(result.is_ok(), "{case}: {result:?} at {}", root.display());
        let r = report(&root);
        assert!(!published);
        assert_eq!(stage_marker["operation_id"], r["buffered_response"]["id"]);
        for field in ["action_id", "field", "value", "status"] {
            assert_eq!(
                stage_marker[field], r["buffered_response"]["result"][field],
                "{field}"
            );
        }
        assert_eq!(count(&r, edit::READ), 1);
        assert_eq!(count(&r, edit::SAVE), usize::from(case == "delay-save"));
        let commands = r["commands"].as_array().unwrap();
        let shutdown = commands
            .iter()
            .position(|c| c["type"] == "shutdown")
            .unwrap();
        assert_eq!(shutdown + 1, commands.len());
        assert_eq!(r["buffer_release"]["cause"], "quit");
        assert_eq!(
            r["buffer_release"]["operation_id"],
            commands[shutdown - 1]["id"]
        );
        let wire = r["wire_events"].as_array().unwrap();
        let response = wire
            .iter()
            .position(|event| {
                event["type"] == "response" && event["id"] == r["buffer_release"]["operation_id"]
            })
            .unwrap();
        let close = wire
            .iter()
            .position(|event| {
                event["type"] == "response" && event["id"] == commands[shutdown]["id"]
            })
            .unwrap();
        assert_eq!(
            wire[response], r["buffered_response"],
            "The exact canonical response must be flushed unchanged"
        );
        assert!(
            response < close,
            "Actual delayed wire response precedes shutdown receipt"
        );
        if case == "delay-save" {
            assert_eq!(r["saves"][0]["status"], "saved");
            assert_eq!(r["after"]["settings"]["compact_max_tokens"], 768);
        } else {
            assert_eq!(r["before"], r["after"]);
            assert_only_idle_ledger_published(&r, &root, &r["initial_disk"]);
        }
    }
}
