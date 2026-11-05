#![cfg(windows)]
//! F1 Work → Context → Hooks → A: create, edit and delete hooks through an isolated source process.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    demo::Demo,
    live::{Interactive, Observation, Options},
    ui,
    work_view::Data,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use std::{
    fs, io,
    path::{Path, PathBuf},
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

fn launch_fixture(fixture_file: &str, command_controls: bool) -> (Interactive, PathBuf) {
    static NEXT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let ordinal = NEXT.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let source = std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate_root.join("../.."))
        .canonicalize()
        .unwrap();
    let epoch = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_nanos();
    let directory = crate_root
        .join("target/context-process-tests")
        .join(format!("{}-{epoch}-{ordinal}", std::process::id()));
    let wrapper = directory.join("source/src/litetui");
    let data = directory.join("data");
    fs::create_dir_all(&wrapper).unwrap();
    let literal = |path: &Path| serde_json::to_string(&path.to_string_lossy()).unwrap();
    fs::write(
        wrapper.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            literal(&source.join("src/litetui"))
        ),
    )
    .unwrap();
    let fixture = crate_root.join("tests/fixtures");
    let script = format!(
        r#"import json,os,runpy
from pathlib import Path
os.environ.update(RUST_RUNTIME_CONTROLS_SOURCE={}, RUST_CONTEXT_COMMON_FIXTURE={},
    RUST_RUNTIME_CONTROLS_CASE='context-read-only', PYTHONDONTWRITEBYTECODE='1',
    RUST_COMMAND_CONTROLS_BASE={})
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False)
p.write_text(json.dumps(s), encoding='utf-8')
runpy.run_path({}, run_name='__main__')
"#,
        literal(&source),
        literal(&fixture.join("runtime_controls_runtime.py")),
        literal(&fixture.join(fixture_file)),
        literal(&fixture.join(if command_controls {
            "command_controls_runtime.py"
        } else {
            fixture_file
        }))
    );
    fs::write(wrapper.join("cli.py"), script).unwrap();
    let args = [
        "--python".to_owned(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("dependency Python"),
        "--source".into(),
        directory.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        data.to_string_lossy().into_owned(),
        "--backend".into(),
        "custom".into(),
        "--model".into(),
        "switch-old".into(),
        "--tool-profile".into(),
        "strict".into(),
        "--tools".into(),
        "off".into(),
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "120".into(),
        "--server-timeout".into(),
        "5".into(),
        "--server-mode".into(),
        "connect".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
    ];
    (Interactive::start(Options::parse(&args).unwrap()), data)
}

fn json_file(path: &Path) -> Value {
    serde_json::from_slice(&fs::read(path).unwrap()).unwrap()
}
fn observe(
    runtime: &mut Interactive,
    app: &mut Demo,
    until: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(40);
    loop {
        if let Ok(event) = runtime.events.recv_timeout(Duration::from_millis(100)) {
            let rejected = match &event {
                Observation::Work(update) => match &update.data {
                    Data::Rejected(message) => Some(message.clone()),
                    _ => None,
                },
                _ => None,
            };
            let done = until(&event);
            app.observe_live(event)?;
            if let Some(reason) = rejected {
                return Err(io::Error::other(reason));
            }
            if done {
                return Ok(());
            }
        } else if let Some(exit) = runtime.poll_done() {
            return Err(exit
                .err()
                .unwrap_or_else(|| io::Error::other("Unexpected Context runtime exit")));
        } else if Instant::now() > deadline {
            return Err(io::Error::other("Context observation timed out"));
        }
    }
}
fn drain(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    while let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
        runtime.work(intent)?;
        observe(runtime, app, |event| matches!(event, Observation::Work(_)))?;
    }
    Ok(())
}
fn draw(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(106, 32)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(106)
        .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<String> {
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .work
        .key(KeyEvent::new(code, KeyModifiers::NONE));
    drain(runtime, app)?;
    Ok(draw(app))
}

fn press(runtime: &mut Interactive, app: &mut Demo, event: KeyEvent) -> io::Result<String> {
    draw(app);
    app.live.as_mut().unwrap().work.key(event);
    drain(runtime, app)?;
    Ok(draw(app))
}
/// Types plain characters into the focused field without a source round trip.
fn typing(runtime: &mut Interactive, app: &mut Demo, text: &str) -> io::Result<String> {
    draw(app);
    for ch in text.chars() {
        app.live
            .as_mut()
            .unwrap()
            .work
            .key(KeyEvent::new(KeyCode::Char(ch), KeyModifiers::NONE));
    }
    drain(runtime, app)?;
    Ok(draw(app))
}
fn clear(runtime: &mut Interactive, app: &mut Demo) -> io::Result<String> {
    press(
        runtime,
        app,
        KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL),
    )
}
/// Pages through the whole review, so that every page has been painted, and returns all of it.
fn read_review(runtime: &mut Interactive, app: &mut Demo) -> io::Result<String> {
    let mut all = String::new();
    let mut frame = draw(app);
    for _ in 0..8 {
        all.push_str(&frame);
        let Some(at) = frame.find("Page ") else {
            return Err(io::Error::other(format!("not a review:\n{frame}")));
        };
        let numbers = frame[at + 5..].split_whitespace().next().unwrap_or("");
        let (page, count) = numbers.split_once('/').unwrap_or(("0", "0"));
        if page == count {
            return Ok(all);
        }
        frame = key(runtime, app, KeyCode::Down)?;
    }
    Err(io::Error::other("review has too many pages"))
}
fn rejected(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<String> {
    draw(app);
    let work = &mut app.live.as_mut().unwrap().work;
    work.key(KeyEvent::new(code, KeyModifiers::NONE));
    runtime.work(work.take_intent().expect("a source request"))?;
    loop {
        let event = runtime
            .events
            .recv_timeout(Duration::from_secs(20))
            .map_err(io::Error::other)?;
        let refused =
            matches!(&event, Observation::Work(u) if matches!(&u.data, Data::Rejected(_)));
        app.observe_live(event)?;
        if refused {
            return Ok(draw(app));
        }
    }
}

/// Presses a key and drains its requests, treating a source refusal as part of the scenario.
fn tolerant(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<String> {
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .work
        .key(KeyEvent::new(code, KeyModifiers::NONE));
    while let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
        runtime.work(intent)?;
        observe(runtime, app, |event| matches!(event, Observation::Work(_))).or_else(|e| {
            if e.to_string().contains("hook") {
                Ok(())
            } else {
                Err(e)
            }
        })?;
    }
    Ok(draw(app))
}

#[test]
#[ignore = "private lifecycle configuration; the real owned source never executes a hook or provider"]
fn hook_authoring_creates_edits_and_deletes_through_review_and_readback_without_running_a_hook() {
    let (runtime, data) = launch_fixture("hook_edit_runtime.py", false);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text("keep this hook-authoring draft");
        app.open_work();
        drain(runtime, &mut app)?;
        let mut context = key(runtime, &mut app, KeyCode::Char('i'))?;
        for _ in 0..litetui_terminal::context::Document::ALL.len() {
            if context.contains("Effective lifecycle hooks") {
                break;
            }
            context = key(runtime, &mut app, KeyCode::Right)?;
        }
        assert!(context.contains("Effective lifecycle hooks"), "{context}");
        let paths = json_file(&data.join("hook-inputs.json"));
        let global = PathBuf::from(paths[0].as_str().unwrap());
        let project = PathBuf::from(paths[1].as_str().unwrap());
        assert!(global.starts_with(data.canonicalize()?) || global.starts_with(&data));
        assert!(!project.exists() && !project.parent().unwrap().exists());
        let toggles = key(runtime, &mut app, KeyCode::Char('e'))?;
        assert!(toggles.contains("A author"), "{toggles}");

        // Create the first project hook; this makes the scope file and its directory.
        let author = key(runtime, &mut app, KeyCode::Char('a'))?;
        assert!(
            author.contains("Author lifecycle hooks · project"),
            "{author}"
        );
        assert!(author.contains("No hooks in this scope"), "{author}");
        let form = key(runtime, &mut app, KeyCode::Char('n'))?;
        assert!(form.contains("New hook · field 1/9: Hook ID"), "{form}");
        typing(runtime, &mut app, "audit-new")?;
        key(runtime, &mut app, KeyCode::Tab)?;
        let events = key(runtime, &mut app, KeyCode::Char(' '))?;
        assert!(events.contains("[x] app_start"), "{events}");
        key(runtime, &mut app, KeyCode::Tab)?;
        typing(runtime, &mut app, "fixture-new.exe")?;
        key(runtime, &mut app, KeyCode::Enter)?;
        let review = read_review(runtime, &mut app)?;
        for expected in [
            "Create project hook audit-new",
            "Scope file that changes: project .litetui/hooks.json (new file)",
            "Not touched: global hooks.json",
            "+ command: fixture-new.exe",
            "+ events: app_start",
            "File SHA-256 before: (file does not exist)",
            "Authoring never runs the hook",
        ] {
            assert!(review.contains(expected), "{expected}\n{review}");
        }
        assert!(!project.exists(), "review must not write");
        let saved = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            saved.contains("Verified create of audit-new in project .litetui/hooks.json"),
            "{saved}"
        );
        let created = json_file(&project);
        assert_eq!(created["hooks"].as_array().unwrap().len(), 1);
        assert_eq!(created["hooks"][0]["executable"], "fixture-new.exe");
        assert_eq!(created["hooks"][0]["events"], json!(["app_start"]));

        // Edit it: only the executable changes, and the review says so.
        let listed = key(runtime, &mut app, KeyCode::F(5))?;
        assert!(
            listed.contains("audit-new · enabled · observe · app_start"),
            "{listed}"
        );
        let edit = key(runtime, &mut app, KeyCode::Char('e'))?;
        assert!(edit.contains("Edit hook · field 3/9: Executable"), "{edit}");
        clear(runtime, &mut app)?;
        typing(runtime, &mut app, "fixture-edited.exe")?;
        key(runtime, &mut app, KeyCode::Enter)?;
        let review = read_review(runtime, &mut app)?;
        assert!(review.contains("- command: fixture-new.exe"), "{review}");
        assert!(review.contains("+ command: fixture-edited.exe"), "{review}");
        assert!(review.contains("  timeout: 10 s"), "{review}");
        let saved = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(saved.contains("Verified edit of audit-new"), "{saved}");
        assert_eq!(
            json_file(&project)["hooks"][0]["executable"],
            "fixture-edited.exe"
        );

        // A hook the source's validator refuses is never written, and the draft survives.
        key(runtime, &mut app, KeyCode::F(5))?;
        let before = fs::read(&project)?;
        key(runtime, &mut app, KeyCode::Char('n'))?;
        typing(runtime, &mut app, "no-event")?;
        key(runtime, &mut app, KeyCode::Tab)?;
        key(runtime, &mut app, KeyCode::Tab)?;
        typing(runtime, &mut app, "x.exe")?;
        let refusal = rejected(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            refusal.contains("select at least one supported event"),
            "{refusal}"
        );
        assert!(
            refusal.contains("New hook · field"),
            "the draft is kept\n{refusal}"
        );
        assert_eq!(fs::read(&project)?, before);
        let discarded = key(runtime, &mut app, KeyCode::Esc)?;
        assert!(discarded.contains("Draft discarded"), "{discarded}");

        // A scope file changed after review refuses Apply and leaves it as the other writer left it.
        key(runtime, &mut app, KeyCode::Char('d'))?;
        let review = read_review(runtime, &mut app)?;
        assert!(review.contains("Delete project hook audit-new"), "{review}");
        assert!(review.contains("- command: fixture-edited.exe"), "{review}");
        assert!(
            review.contains("Effective after: no hook with this ID runs"),
            "{review}"
        );
        let mut moved = fs::read(&project)?;
        moved.extend_from_slice(b"\r\n");
        fs::write(&project, &moved)?;
        let stale = rejected(runtime, &mut app, KeyCode::Enter)?;
        assert!(stale.contains("changed"), "{stale}");
        assert_eq!(fs::read(&project)?, moved);
        assert_eq!(json_file(&project)["hooks"].as_array().unwrap().len(), 1);

        key(runtime, &mut app, KeyCode::F(5))?;
        key(runtime, &mut app, KeyCode::Char('d'))?;
        read_review(runtime, &mut app)?;
        let saved = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(saved.contains("Verified delete of audit-new"), "{saved}");
        assert_eq!(json_file(&project)["hooks"], json!([]));

        // The global scope: edit one flag. Its private environment and directory stay in the source.
        let global_list = key(runtime, &mut app, KeyCode::Tab)?;
        assert!(
            global_list.contains("audit-file · enabled · observe · tool_before"),
            "{global_list}"
        );
        assert!(!global_list.contains("PRIVATE_"), "{global_list}");
        key(runtime, &mut app, KeyCode::Char('e'))?;
        for _ in 0..4 {
            key(runtime, &mut app, KeyCode::Tab)?;
        }
        key(runtime, &mut app, KeyCode::Char(' '))?;
        key(runtime, &mut app, KeyCode::Enter)?;
        let review = read_review(runtime, &mut app)?;
        assert!(
            review.contains("- enabled: true") && review.contains("+ enabled: false"),
            "{review}"
        );
        assert!(
            review.contains(
                "1 environment value(s) kept in the source; custom working directory kept"
            ),
            "{review}"
        );
        assert!(
            review.contains("Scope file that changes: global hooks.json"),
            "{review}"
        );
        assert!(!review.contains("PRIVATE_"), "{review}");
        let saved = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            saved.contains("Verified edit of audit-file in global hooks.json"),
            "{saved}"
        );

        // Leaving reloads the toggle list underneath, which shows the scope it was browsing.
        key(runtime, &mut app, KeyCode::Esc)?;
        let toggles = draw(&mut app);
        assert!(toggles.contains("Lifecycle hooks · project"), "{toggles}");
        let global = key(runtime, &mut app, KeyCode::Tab)?;
        assert!(global.contains("audit-file · disabled"), "{global}");
        key(runtime, &mut app, KeyCode::Esc)?;
        assert_eq!(app.composer.text(), "keep this hook-authoring draft");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        Ok(())
    });
    result.unwrap();
    let state = json_file(&data.join("hook-edit-after.json"));
    assert_eq!(state["scopes"]["project"], json!([]));
    let global = &state["scopes"]["global"][0];
    assert_eq!(global["id"], "audit-file");
    assert_eq!(global["enabled"], false);
    assert_eq!(global["env"], json!({"PRIVATE_KEY": "PRIVATE_VALUE"}));
    assert_eq!(global["cwd"], "PRIVATE_DIRECTORY");
    assert_eq!(global["argv"], json!(["PUBLIC_ARGUMENT"]));
    assert_eq!(state["executed"], json!([]));
    let report = json_file(&data.join("runtime-controls-report.json"));
    assert_eq!(report["requests"], json!([]));
    let replies = report["wire"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|v| {
            v["result"]["kind"]
                .as_str()
                .is_some_and(|kind| kind.starts_with("hook_edit_"))
        })
        .collect::<Vec<_>>();
    assert!(
        !serde_json::to_string(&replies)
            .unwrap()
            .contains("PRIVATE_")
    );
    for kind in ["hook_edit_apply", "hook_edit_saved"] {
        assert_eq!(
            replies
                .iter()
                .filter(|v| v["result"]["kind"] == kind)
                .count(),
            4,
            "{kind}: create, edit, delete and the global edit"
        );
    }
}

#[test]
#[ignore = "private lifecycle configuration; the real owned source never executes a hook or provider"]
fn unreadable_hook_document_is_repaired_only_after_review_and_exact_byte_compare() {
    let (runtime, data) = launch_fixture("hook_repair_runtime.py", false);
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        observe(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        // One closing brace short of a hook document, planted once the runtime is up.
        let planted = json_file(&data.join("hook-inputs.json"));
        fs::write(
            planted[1].as_str().unwrap(),
            concat!(
                r#"{"version": 1, "hooks": [{"id": "mend-me", "events": ["app_start"], "#,
                r#""executable": "fixture-fixed.exe", "env": {"PRIVATE_KEY": "PRIVATE_VALUE"}}]"#
            ),
        )?;
        app.insert_text("keep this repair draft");
        app.open_work();
        drain(runtime, &mut app)?;
        let mut context = key(runtime, &mut app, KeyCode::Char('i'))?;
        // The Hooks page itself cannot be read while the scope document is unreadable.
        for _ in 0..litetui_terminal::context::Document::ALL.len() {
            if context.contains("Read failed") {
                break;
            }
            context = tolerant(runtime, &mut app, KeyCode::Right)?;
        }
        assert!(context.contains("Read failed"), "{context}");
        let paths = json_file(&data.join("hook-inputs.json"));
        let project = PathBuf::from(paths[1].as_str().unwrap());
        let broken = fs::read(&project)?;
        rejected(runtime, &mut app, KeyCode::Char('e'))?;
        let author = key(runtime, &mut app, KeyCode::Char('a'))?;
        assert!(
            author.contains("project .litetui/hooks.json cannot be read:"),
            "{author}"
        );
        assert!(author.contains("line 1 column"), "{author}");
        assert!(
            author.contains("Press R to edit the stored text"),
            "{author}"
        );
        let refused = key(runtime, &mut app, KeyCode::Char('n'))?;
        assert!(refused.contains("Press R to repair it first"), "{refused}");

        // Unchanged text is still not a document: the source's parser refuses it and the draft stays.
        let editor = key(runtime, &mut app, KeyCode::Char('r'))?;
        assert!(
            editor.contains("Repair project .litetui/hooks.json: edit the text"),
            "{editor}"
        );
        let refusal = rejected(runtime, &mut app, KeyCode::F(6))?;
        assert!(refusal.contains("not valid JSON"), "{refusal}");
        assert!(
            refusal.contains("Repair project .litetui/hooks.json"),
            "the draft is kept\n{refusal}"
        );
        assert_eq!(fs::read(&project)?, broken);

        // Supply the missing brace; the review shows the document that will replace the file.
        key(runtime, &mut app, KeyCode::End)?;
        typing(runtime, &mut app, "}")?;
        key(runtime, &mut app, KeyCode::F(6))?;
        let review = read_review(runtime, &mut app)?;
        for expected in [
            "Repair project hook document",
            "Scope file that changes: project .litetui/hooks.json",
            "Not touched: global hooks.json",
            "--- document that will be written ---",
            "\"executable\": \"fixture-fixed.exe\"",
            "1 hook(s)",
            "The unreadable text is not kept",
        ] {
            assert!(review.contains(expected), "{expected}\n{review}");
        }
        assert_eq!(fs::read(&project)?, broken, "review must not write");

        // Another writer touched the file after the review: the byte compare refuses, nothing is replaced.
        let mut moved = broken.clone();
        moved.push(b' ');
        fs::write(&project, &moved)?;
        let stale = rejected(runtime, &mut app, KeyCode::Enter)?;
        assert!(stale.contains("changed"), "{stale}");
        assert_eq!(fs::read(&project)?, moved);

        key(runtime, &mut app, KeyCode::F(5))?;
        key(runtime, &mut app, KeyCode::Char('r'))?;
        key(runtime, &mut app, KeyCode::End)?;
        typing(runtime, &mut app, "}")?;
        key(runtime, &mut app, KeyCode::F(6))?;
        read_review(runtime, &mut app)?;
        let saved = key(runtime, &mut app, KeyCode::Enter)?;
        assert!(
            saved.contains("Verified repair of project .litetui/hooks.json: 1 hook(s) read back"),
            "{saved}"
        );
        let mended = json_file(&project);
        assert_eq!(mended["hooks"][0]["id"], "mend-me");
        assert_eq!(
            mended["hooks"][0]["env"],
            json!({"PRIVATE_KEY": "PRIVATE_VALUE"})
        );
        let listed = key(runtime, &mut app, KeyCode::F(5))?;
        assert!(
            listed.contains("mend-me · enabled · observe · app_start"),
            "{listed}"
        );

        key(runtime, &mut app, KeyCode::Esc)?;
        key(runtime, &mut app, KeyCode::Esc)?;
        assert_eq!(app.composer.text(), "keep this repair draft");
        Ok(())
    });
    result.unwrap();
    let state = json_file(&data.join("hook-edit-after.json"));
    assert_eq!(state["scopes"]["project"][0]["id"], "mend-me");
    assert_eq!(state["executed"], json!([]));
    let report = json_file(&data.join("runtime-controls-report.json"));
    let replies = report["wire"].as_array().unwrap().iter().filter(|v| {
        v["result"]["kind"]
            .as_str()
            .is_some_and(|kind| kind.starts_with("hook_repair_"))
    });
    for kind in [
        "hook_repair_review",
        "hook_repair_apply",
        "hook_repair_saved",
    ] {
        let found = replies
            .clone()
            .filter(|v| v["result"]["kind"] == kind)
            .count();
        assert_eq!(
            found,
            if kind == "hook_repair_review" { 2 } else { 1 },
            "{kind}"
        );
    }
}
