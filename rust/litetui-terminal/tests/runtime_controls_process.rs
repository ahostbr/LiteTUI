#![cfg(windows)]
//! Actual owned runtime controls through compiled Interactive and full painted Review.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    composer::Motion,
    demo::Demo,
    live::{Interactive, Observation, Options},
    runtime_controls_view as view, ui,
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
fn launch(case: &str) -> (Interactive, PathBuf) {
    let source = source();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/runtime-controls-process-tests")
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
    let quote = |s: &str| serde_json::to_string(s).unwrap();
    fs::write(
        package.join("__init__.py"),
        format!(
            "__path__.append({})\n",
            quote(&source.join("src/litetui").to_string_lossy())
        ),
    )
    .unwrap();
    let fixture =
        PathBuf::from(env!("CARGO_MANIFEST_DIR")).join(if case.starts_with("claude-queue-") {
            "tests/fixtures/claude_queue_runtime.py"
        } else if case.starts_with("codex-midturn-") {
            "tests/fixtures/codex_midturn_runtime.py"
        } else {
            "tests/fixtures/runtime_controls_runtime.py"
        });
    fs::write(package.join("cli.py"),format!(r#"import json,os,runpy
from pathlib import Path
os.environ['RUST_RUNTIME_CONTROLS_SOURCE']={}
os.environ['RUST_RUNTIME_CONTROLS_CASE']={}
os.environ['PYTHONDONTWRITEBYTECODE']='1'
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(codex_native_engine=os.environ['RUST_RUNTIME_CONTROLS_CASE'].startswith('codex-midturn-'),enter_interrupts=os.environ['RUST_RUNTIME_CONTROLS_CASE'] in ('midturn-swapped','midturn-interrupt-swapped'),user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False,tool_auto_background_s=0,custom_context_length=1000000)
p.write_text(json.dumps(s),encoding='utf-8')
runpy.run_path({},run_name='__main__')
"#,quote(&source.to_string_lossy()),quote(case),quote(&fixture.to_string_lossy()))).unwrap();
    let root = base.join("data");
    let workspace = base.join("workspace");
    fs::create_dir(&workspace).unwrap();
    let mut args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("controlled dependency Python"),
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        if case.starts_with("claude-queue-") {
            "claude".into()
        } else if case.starts_with("codex-midturn-") {
            "codex".into()
        } else {
            "custom".into()
        },
        "--model".into(),
        if case.starts_with("claude-queue-") {
            "claude-fixture".into()
        } else if case.starts_with("codex-midturn-") {
            "gpt-6.1-sol".into()
        } else {
            "switch-old".into()
        },
        "--tool-profile".into(),
        if case.starts_with("host-") {
            "autonomous".into()
        } else {
            "strict".into()
        },
        "--tools".into(),
        if case.starts_with("host-")
            || case == "claude-queue-tools"
            || case.starts_with("claude-queue-image")
        {
            "on".into()
        } else {
            "off".into()
        },
        "--thinking-level".into(),
        if case.starts_with("codex-midturn-") {
            "high".into()
        } else {
            "default".into()
        },
        "--timeout".into(),
        "120".into(),
        "--server-timeout".into(),
        "5".into(),
        "--base-url".into(),
        "http://127.0.0.1:45120/v1".into(),
        "--server-mode".into(),
        "connect".into(),
    ];
    if case.starts_with("claude-queue-") || case.starts_with("codex-midturn-") {
        args.truncate(args.len() - 4);
    }
    (Interactive::start(Options::parse(&args).unwrap()), root)
}
fn next(runtime: &mut Interactive) -> io::Result<Observation> {
    let deadline = Instant::now() + Duration::from_secs(125);
    loop {
        match runtime.events.recv_timeout(Duration::from_millis(50)) {
            Ok(event) => return Ok(event),
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return Err(done
                        .err()
                        .unwrap_or_else(|| io::Error::other("Unexpected runtime-controls exit")));
                }
                if Instant::now() > deadline {
                    return Err(io::Error::other("Runtime-controls observation deadline"));
                }
            }
        }
    }
}
fn send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    if let Some(operation) = app.live.as_mut().unwrap().take_abort() {
        runtime.abort(operation)?;
    }
    if let Some(intent) = app.live.as_mut().unwrap().runtime_controls.take_intent() {
        runtime.runtime_controls(intent)?;
    }
    Ok(())
}
fn wait(
    runtime: &mut Interactive,
    app: &mut Demo,
    predicate: impl Fn(&Observation) -> bool,
) -> io::Result<()> {
    loop {
        let event = next(runtime)?;
        if let Observation::RuntimeControls(view::Update::Rejected(reason)) = &event {
            return Err(io::Error::other(reason.clone()));
        }
        let done = predicate(&event);
        app.observe_live(event)?;
        send(runtime, app)?;
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
        .chunks(120)
        .map(|cells| cells.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .runtime_controls
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn open(runtime: &mut Interactive, app: &mut Demo, field: view::Field) -> io::Result<()> {
    app.live.as_mut().unwrap().runtime_controls.open(field);
    send(runtime, app)?;
    wait(runtime, app, |e| {
        matches!(e, Observation::RuntimeControls(view::Update::Review(_)))
    })
}
fn confirm(runtime: &Interactive, app: &mut Demo, frames: &mut Vec<String>) -> io::Result<()> {
    key(app, KeyCode::Tab);
    key(app, KeyCode::Enter);
    assert!(
        app.live
            .as_mut()
            .unwrap()
            .runtime_controls
            .take_intent()
            .is_none(),
        "Unpainted Review cannot authorize Apply"
    );
    // Do not let first-page confirmation skip a long plan or its consequences.
    if draw(app).contains("Read all pages") {
        key(app, KeyCode::Tab);
        key(app, KeyCode::Enter);
        assert!(
            app.live
                .as_mut()
                .unwrap()
                .runtime_controls
                .take_intent()
                .is_none()
        );
    }
    let mut previous = None;
    for _ in 0..1024 {
        let frame = draw(app);
        let done = previous.as_ref() == Some(&frame);
        previous = Some(frame.clone());
        frames.push(frame);
        if done {
            break;
        }
        key(app, KeyCode::PageDown);
    }
    key(app, KeyCode::Tab);
    key(app, KeyCode::Enter);
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .runtime_controls
        .take_intent()
        .ok_or_else(|| io::Error::other("Fully painted review did not authorize Apply"))?;
    assert!(matches!(intent, view::Intent::Apply(_)));
    runtime.runtime_controls(intent)
}
fn collect(root: &Path) -> Value {
    let r: Value =
        serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
            .unwrap();
    for (key, value) in r
        .as_object()
        .unwrap()
        .iter()
        .filter(|(k, _)| k.starts_with("source_"))
    {
        assert_eq!(
            PathBuf::from(value.as_str().unwrap())
                .canonicalize()
                .unwrap(),
            source()
                .join(format!(
                    "src/litetui/{}.py",
                    key.trim_start_matches("source_")
                ))
                .canonicalize()
                .unwrap()
        );
    }
    if matches!(r["case"].as_str(), Some("identity-new" | "identity-open")) {
        let old = &r["before"]["identity"];
        let new = &r["after"]["identity"];
        assert_ne!(old["session_id"], new["session_id"]);
        for key in ["agent_id", "backend", "model"] {
            assert_eq!(old[key], new[key]);
        }
        assert_eq!(r["before"]["cwd"], r["after"]["cwd"]);
        assert_eq!(r["before"]["mcp_enabled"], r["after"]["mcp_enabled"]);
    } else {
        assert_eq!(r["before"], r["after"]);
    }
    assert_eq!(r["history"], r["durable"]);
    for field in ["denied", "questions", "approvals", "authorities"] {
        assert_eq!(r[field], json!([]), "{field}");
    }
    for field in ["startup_held", "reader_retired", "helper_retired"] {
        assert_eq!(r[field], true);
    }
    assert!(
        r["workers"]
            .as_array()
            .unwrap()
            .iter()
            .all(|w| w["finished"] == true)
    );
    assert!(
        r["streams"]
            .as_array()
            .unwrap()
            .iter()
            .all(|s| s["closed"] == true)
    );
    let leases = r["lease_release"].as_array().unwrap();
    assert!(!leases.is_empty());
    assert!(leases.iter().all(|v| v["active_workers"] == json!([])
        && v["questions"] == json!([])
        && v["approvals"] == json!([])));
    assert_eq!(r["shutdown"]["ok"], true);
    let proof = &r["shutdown"]["result"]["runtime_controls_shutdown"];
    assert_eq!(proof["runtime_controls_version"], 1);
    for field in [
        "authorization_closed",
        "admission_closed",
        "authorization_owners_joined",
        "waiter_owners_joined",
    ] {
        assert_eq!(proof[field], true);
    }
    assert_eq!(proof["remaining_authorizations"], 0);
    assert_eq!(proof["remaining_waiters"], 0);
    assert_eq!(proof["effects_verified"], false);
    r
}
#[test]
#[ignore = "actual-source native runtime controls; controlled dependency Python required"]
fn tools_profile_and_plan_use_painted_review_and_independent_publication() {
    for (case, field) in [
        ("basic-tools", view::Field::Tools),
        ("basic-profile", view::Field::Profile),
        ("basic-plan", view::Field::Plan),
    ] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live("custom".into(), "switch-old".into());
                wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
                app.insert_text("PRESERVE RUNTIME CONTROL COMPOSER");
                app.composer.move_cursor(Motion::WordLeft, true);
                let draft = (
                    app.composer.text().to_string(),
                    app.composer.before_cursor().to_string(),
                    app.composer.selected_text().to_string(),
                );
                open(runtime, &mut app, field)?;
                confirm(runtime, &mut app, &mut frames)?;
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::RuntimeControls(view::Update::Settled(_)))
                })?;
                assert_eq!(
                    draft,
                    (
                        app.composer.text().to_string(),
                        app.composer.before_cursor().to_string(),
                        app.composer.selected_text().to_string()
                    )
                );
                assert!(!app.live.as_ref().unwrap().busy);
                assert!(app.live.as_ref().unwrap().rows.is_empty());
                frames.push(draw(&mut app));
                Ok(())
            })
            .unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
        let r = collect(&root);
        assert_eq!(r["requests"], json!([]));
        for name in ["snapshots", "reviews", "results", "readbacks"] {
            assert_eq!(r[name].as_array().unwrap().len(), 1, "{name}");
        }
        assert_eq!(r["results"][0]["status"], "applied");
        assert_eq!(r["readbacks"][0]["verified"], true);
        assert_eq!(r["readbacks"][0]["release"], "install");
        assert_eq!(r["readbacks"][0]["generation_after"], 1);
        assert_eq!(r["work"]["producers"], json!([]));
        assert_eq!(r["work"]["inputs"], json!([]));
        assert!(!r["commands"].as_array().unwrap().iter().any(|c| matches!(
            c["type"].as_str(),
            Some("prompt" | "set" | "gui.prompt.submit")
        )));
        fs::write(
            root.join("native-runtime-controls-frames.txt"),
            frames.join("\n\nNEXT FRAME\n\n"),
        )
        .unwrap();
        fs::write(
            root.join("native-runtime-controls-result.json"),
            serde_json::to_vec_pretty(&json!({"case":case,"error":null,"composer_preserved":true}))
                .unwrap(),
        )
        .unwrap();
        eprintln!("Runtime controls {case} report: {}", root.display());
    }
}

fn stage(runtime: &mut Interactive, app: &mut Demo, root: &Path, wanted: &str) -> io::Result<()> {
    let deadline = Instant::now() + Duration::from_secs(35);
    loop {
        if let Ok(raw) = fs::read(root.join(format!("stage-{wanted}.json")))
            && let Ok(value) = serde_json::from_slice::<Value>(&raw)
            && value["stage"] == wanted
        {
            return Ok(());
        }
        match runtime.events.recv_timeout(Duration::from_millis(20)) {
            Ok(event) => {
                app.observe_live(event)?;
                send(runtime, app)?;
            }
            Err(_) => {
                if let Some(done) = runtime.poll_done() {
                    return done.and(Err(io::Error::other(
                        "Runtime exited before controlled stage",
                    )));
                }
            }
        }
        if Instant::now() > deadline {
            return Err(io::Error::other(format!(
                "Missing actual controlled stage {wanted}"
            )));
        }
    }
}

#[test]
#[ignore = "actual mid-turn old captures and parked next authorization; dependency Python required"]
fn captured_host_tools_drain_old_authority_and_next_calls_wait_for_verified_policy() {
    for case in [
        "host-old-tool-profile",
        "host-old-authorize",
        "host-next-tool-parked",
        "host-old-tool-tools",
    ] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live("custom".into(), "switch-old".into());
                wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("EXPLICIT CONTROLLED TWO TOOL TURN")?;
                runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
                app.insert_text("PRESERVE DRAFT WHILE OLD TOOL IS RUNNING");
                app.composer.move_cursor(Motion::WordLeft, true);
                let draft = (
                    app.composer.text().to_string(),
                    app.composer.before_cursor().to_string(),
                    app.composer.selected_text().to_string(),
                );
                stage(
                    runtime,
                    &mut app,
                    &root,
                    if case == "host-old-authorize" {
                        "old-authorization-captured"
                    } else {
                        "old-tool-running"
                    },
                )?;
                assert!(app.live.as_ref().unwrap().busy);
                open(
                    runtime,
                    &mut app,
                    if case.ends_with("tools") {
                        view::Field::Tools
                    } else {
                        view::Field::Profile
                    },
                )?;
                confirm(runtime, &mut app, &mut frames)?;
                if case == "host-next-tool-parked" {
                    stage(runtime, &mut app, &root, "readback-prepared")?;
                    fs::write(
                        root.join("release-tool"),
                        b"after actual setter and before exact readback publication",
                    )
                    .unwrap();
                    stage(runtime, &mut app, &root, "next-tool-attempt")?;
                    assert!(app.live.as_ref().unwrap().runtime_controls.pending());
                    frames.push(draw(&mut app));
                    fs::write(
                        root.join("release-publication"),
                        b"explicit fixture release of actual readback response",
                    )
                    .unwrap();
                }
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::RuntimeControls(view::Update::Settled(_)))
                })?;
                fs::write(root.join("release-tool"), b"after verified new policy").unwrap();
                fs::write(
                    root.join("release-authorization"),
                    b"old captured authorization may drain",
                )
                .unwrap();
                while app.live.as_ref().unwrap().busy {
                    let event = next(runtime)?;
                    app.observe_live(event)?;
                    send(runtime, &mut app)?;
                }
                assert_eq!(
                    draft,
                    (
                        app.composer.text().to_string(),
                        app.composer.before_cursor().to_string(),
                        app.composer.selected_text().to_string()
                    )
                );
                frames.push(draw(&mut app));
                Ok(())
            })
            .unwrap_or_else(|e| panic!("{case}: {e}; {}", root.display()));
        let r = collect(&root);
        let commands = r["commands"].as_array().unwrap();
        let prompts: Vec<_> = commands.iter().filter(|c| c["type"] == "prompt").collect();
        assert_eq!(prompts.len(), 1);
        assert_eq!(prompts[0]["message"], "EXPLICIT CONTROLLED TWO TOOL TURN");
        assert!(prompts[0].get("tool_profile").is_none());
        assert_eq!(prompts[0]["runtime_policy"]["version"], 1);
        assert_eq!(r["results"].as_array().unwrap().len(), 1);
        assert_eq!(r["readbacks"].as_array().unwrap().len(), 1);
        let events = r["events"].as_array().unwrap();
        let sequence = |kind: &str, ordinal: i64| {
            events
                .iter()
                .find(|e| {
                    e["kind"] == kind
                        && if kind.starts_with("authorization") {
                            e["args"]["ordinal"] == ordinal && e["name"] == "runtime_fixture_tool"
                        } else {
                            e["ordinal"] == ordinal
                        }
                })
                .unwrap()["sequence"]
                .as_u64()
                .unwrap()
        };
        let boundary = events
            .iter()
            .find(|e| {
                e["kind"] == "wire" && e["frame"]["result"]["kind"] == "runtime_controls_readback"
            })
            .unwrap()["sequence"]
            .as_u64()
            .unwrap();
        let old = events
            .iter()
            .find(|e| {
                e["kind"] == "authorization-start"
                    && e["name"] == "runtime_fixture_tool"
                    && e["args"]["ordinal"] == 0
            })
            .unwrap();
        assert_eq!(old["profile"], "autonomous");
        if case == "host-next-tool-parked" {
            assert!(sequence("tool-exit", 0) < boundary);
        } else {
            assert!(sequence("tool-exit", 0) > boundary);
        }
        assert_eq!(
            events
                .iter()
                .filter(|e| e["kind"] == "tool-enter" && e["ordinal"] == 0)
                .count(),
            1
        );
        if case.ends_with("tools") {
            assert!(
                !events
                    .iter()
                    .any(|e| e["kind"] == "tool-enter" && e["ordinal"] == 1)
            );
            assert!(
                !events
                    .iter()
                    .any(|e| e["kind"] == "authorization-start" && e["args"]["ordinal"] == 1)
            );
        } else {
            let next = events
                .iter()
                .find(|e| {
                    e["kind"] == "authorization-start"
                        && e["name"] == "runtime_fixture_tool"
                        && e["args"]["ordinal"] == 1
                })
                .unwrap();
            assert_eq!(
                next["profile"],
                r["reviews"][0]["after"]["effective_profile"]
            );
            assert!(sequence("authorization-start", 1) > boundary);
            assert_eq!(
                events
                    .iter()
                    .filter(|e| e["kind"] == "tool-enter" && e["ordinal"] == 1)
                    .count(),
                1
            );
        }
        if case == "host-next-tool-parked" {
            let attempt = events
                .iter()
                .find(|e| e["kind"] == "tool-attempt" && e["args"]["ordinal"] == 1)
                .unwrap()["sequence"]
                .as_u64()
                .unwrap();
            assert!(attempt < boundary);
        }
        let old_after = events
            .iter()
            .find(|e| {
                e["kind"] == "hook-dispatch"
                    && e["event"] == "tool_after"
                    && e["payload"]["args"]["ordinal"] == 0
            })
            .unwrap();
        assert_eq!(old_after["profile"], "autonomous");
        fs::write(
            root.join("native-runtime-controls-frames.txt"),
            frames.join("\n\nNEXT FRAME\n\n"),
        )
        .unwrap();
        fs::write(root.join("native-runtime-controls-result.json"), serde_json::to_vec_pretty(&json!({"case":case,"error":null,"composer_preserved":true,"old_capture_preserved":true,"verified_boundary_sequence":boundary})).unwrap()).unwrap();
        eprintln!("Runtime controls {case} report: {}", root.display());
    }
}

fn native_result(root: &Path, case: &str, result: Value, frames: &[String]) {
    let mut result = result;
    result["case"] = json!(case);
    result["error"] = Value::Null;
    fs::write(
        root.join("native-runtime-controls-result.json"),
        serde_json::to_vec_pretty(&result).unwrap(),
    )
    .unwrap();
    fs::write(
        root.join("native-runtime-controls-frames.txt"),
        frames.join("\n\nNEXT FRAME\n\n"),
    )
    .unwrap();
    eprintln!("Runtime controls {case} report: {}", root.display());
}

#[test]
#[ignore = "actual source publication race and joined Stop/Quit; dependency Python required"]
fn finish_stop_and_quit_while_control_readback_is_held() {
    for case in [
        "owner-finish-readback",
        "host-stop-parked",
        "host-quit-parked",
        // The source order the load flake produced, forced: the quit-drained stream's
        // turn_end reaches this side before the held readback is published.
        "host-quit-parked-late",
        "host-stop-captured",
        "host-quit-captured",
    ] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        let has_tool = case.starts_with("host-");
        let parked = case.contains("-parked");
        let quit = case.starts_with("host-quit-");
        runtime
            .with_cleanup(|runtime| {
                let mut app = Demo::live("custom".into(), "switch-old".into());
                wait(runtime, &mut app, |event| {
                    matches!(event, Observation::Ready)
                })?;
                app.live
                    .as_mut()
                    .unwrap()
                    .submit("Explicit turn for control publication and closure")?;
                runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
                app.insert_text("Draft survives a pending runtime choice");
                app.composer.move_cursor(Motion::WordLeft, true);
                let draft = (
                    app.composer.text().to_owned(),
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned(),
                );
                stage(
                    runtime,
                    &mut app,
                    &root,
                    if has_tool {
                        "old-tool-running"
                    } else {
                        "stream-active"
                    },
                )?;
                open(runtime, &mut app, view::Field::Plan)?;
                confirm(runtime, &mut app, &mut frames)?;
                stage(runtime, &mut app, &root, "readback-prepared")?;
                if parked {
                    fs::write(root.join("release-tool"), b"allow the old ticket to retire")?;
                    stage(runtime, &mut app, &root, "next-tool-attempt")?;
                }
                match case {
                    "host-quit-parked" | "host-quit-parked-late" | "host-quit-captured" => {
                        app.live.as_mut().unwrap().begin_quit();
                        runtime.quit()?;
                    }
                    "host-stop-parked" | "host-stop-captured" => {
                        app.live.as_mut().unwrap().request_stop()?;
                        send(runtime, &mut app)?;
                        stage(runtime, &mut app, &root, "command-abort")?;
                    }
                    _ => fs::write(
                        root.join("release-stream"),
                        b"finish the exact stream while V is held",
                    )?,
                }
                if !quit {
                    while app.live.as_ref().unwrap().busy {
                        let event = next(runtime)?;
                        assert!(
                            !matches!(
                                event,
                                Observation::RuntimeControls(view::Update::Settled(_))
                            ),
                            "control readback escaped controlled hold"
                        );
                        app.observe_live(event)?;
                        send(runtime, &mut app)?;
                    }
                    fs::write(
                        root.join("release-publication"),
                        b"publish after source stream retirement",
                    )?;
                    wait(runtime, &mut app, |event| {
                        matches!(
                            event,
                            Observation::RuntimeControls(view::Update::Settled(_))
                        )
                    })?;
                }
                assert_eq!(
                    draft,
                    (
                        app.composer.text().to_owned(),
                        app.composer.before_cursor().to_owned(),
                        app.composer.selected_text().to_owned()
                    )
                );
                Ok(())
            })
            .unwrap_or_else(|error| panic!("{case}: {error}; {}", root.display()));
        let r = collect(&root);
        let apply = &r["results"][0];
        assert_eq!(apply["status"], "applied");
        assert_eq!(apply["owner_after"]["state"], "active");
        let e = &r["shutdown"]["result"]["runtime_controls_shutdown"];
        assert_eq!(e["action_id"], apply["action_id"]);
        assert_eq!(e["apply_operation_id"], apply["apply_operation_id"]);
        let rb = r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .find(|c| c["type"] == "gui.runtime.native_readback")
            .unwrap();
        assert_eq!(e["readback_operation_id"], rb["id"]);
        let events = r["events"].as_array().unwrap();
        if case == "host-quit-parked-late" {
            let end = events
                .iter()
                .find(|v| v["kind"] == "wire" && v["frame"]["type"] == "turn_end")
                .expect("the quit-drained stream ended");
            let published = events
                .iter()
                .find(|v| {
                    v["kind"] == "wire"
                        && v["frame"]["type"] == "response"
                        && v["frame"]["id"] == rb["id"]
                })
                .expect("the held readback was published");
            assert!(end["sequence"].as_u64() < published["sequence"].as_u64());
            // Past Active at publication, though this side never settles a quit-drained turn.
            assert_ne!(published["frame"]["result"]["owner"]["state"], "active");
        }
        if has_tool {
            assert!(
                !events
                    .iter()
                    .any(|v| v["kind"] == "tool-enter" && v["ordinal"] == 1)
            );
            assert!(!events.iter().any(|v| v["kind"] == "authorization-start"
                && v["name"] == "runtime_fixture_tool"
                && v["args"]["ordinal"] == 1));
        }
        if case.ends_with("-captured") {
            let command = if quit { "shutdown" } else { "abort" };
            let close = events
                .iter()
                .find(|v| v["kind"] == "command" && v["command"]["type"] == command)
                .unwrap();
            let exit = events
                .iter()
                .find(|v| v["kind"] == "tool-exit" && v["ordinal"] == 0)
                .unwrap();
            let shutdown = events
                .iter()
                .find(|v| {
                    v["kind"] == "wire"
                        && v["frame"]["result"]
                            .get("runtime_controls_shutdown")
                            .is_some()
                })
                .unwrap();
            assert!(close["sequence"].as_u64().unwrap() < exit["sequence"].as_u64().unwrap());
            assert!(exit["sequence"].as_u64().unwrap() < shutdown["sequence"].as_u64().unwrap());
        }
        let work = &r["shutdown"]["result"]["work_shutdown"];
        assert_eq!(work["instance"], e["work_instance"]);
        assert_eq!(work["worker_settled"], true);
        assert_eq!(work["callback_settled"], true);
        if !quit {
            let v = &r["readbacks"][0];
            assert_eq!(v["verified"], true);
            assert_eq!(v["release"], "install");
            assert!(matches!(
                v["owner"]["state"].as_str(),
                Some("terminal" | "idle")
            ));
            if v["owner"]["state"] == "terminal" {
                assert_eq!(v["owner"]["origin"], apply["owner_after"]["origin"]);
            }
            assert_eq!(
                v["current"]["effective_profile"],
                apply["runtime_after"]["effective_profile"]
            );
            assert_eq!(v["effective_transition"], "unchanged");
            assert_eq!(v["generation_after"], 1);
            assert_eq!(e["readback_published"], true);
            assert_eq!(e["disposition"], "verified");
        }
        native_result(
            &root,
            case,
            json!({"composer_preserved":true, "readback_command_accounted":true}),
            &frames,
        );
    }
}

fn browser_send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .browser
        .take_intent()
        .ok_or_else(|| io::Error::other("Missing explicit conversation action"))?;
    runtime.browser(intent)
}
fn browser_key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .browser
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}

#[test]
#[ignore = "actual verified New after changed authority and fresh target capture; dependency Python required"]
fn human_new_and_open_install_target_policy_at_fresh_generation_zero() {
    for case in ["identity-new", "identity-open"] {
        identity_change(case);
    }
}

fn identity_change(case: &str) {
    use litetui_terminal::conversation_browser::Update;
    let (runtime, root) = launch(case);
    let mut frames = Vec::new();
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Keep this draft through a change of conversation");
            open(runtime, &mut app, view::Field::Profile)?;
            confirm(runtime, &mut app, &mut frames)?;
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::RuntimeControls(view::Update::Settled(_)))
            })?;
            key(&mut app, KeyCode::Esc);
            app.open_browser();
            browser_send(runtime, &mut app)?;
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::Browser(Update::Page(_)))
            })?;
            if case == "identity-open" {
                let page = app.live.as_ref().unwrap().browser.page.as_ref().unwrap();
                let target = page
                    .entries
                    .iter()
                    .position(|entry| !entry.current)
                    .unwrap();
                draw(&mut app);
                browser_key(&mut app, KeyCode::Home);
                for _ in 0..target {
                    browser_key(&mut app, KeyCode::Down);
                }
                browser_key(&mut app, KeyCode::Char('o'));
                draw(&mut app);
                browser_key(&mut app, KeyCode::Enter);
                browser_send(runtime, &mut app)?;
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::Browser(Update::Preview(_)))
                })?;
            } else {
                draw(&mut app);
                browser_key(&mut app, KeyCode::Char('n'));
                browser_send(runtime, &mut app)?;
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::Browser(Update::Action(_)))
                })?;
                draw(&mut app);
                browser_key(&mut app, KeyCode::Enter);
                browser_send(runtime, &mut app)?;
                wait(runtime, &mut app, |e| {
                    matches!(e, Observation::Browser(Update::Action(_)))
                })?;
            }
            for _ in 0..100 {
                draw(&mut app);
                browser_key(&mut app, KeyCode::PageDown);
            }
            draw(&mut app);
            browser_key(&mut app, KeyCode::Enter);
            browser_send(runtime, &mut app)?;
            wait(runtime, &mut app, |e| {
                matches!(
                    (case, e),
                    ("identity-new", Observation::ConversationChanged(_))
                        | ("identity-open", Observation::Opened(_))
                )
            })?;
            assert_eq!(
                app.composer.text(),
                "Keep this draft through a change of conversation"
            );
            let chat = app.live.as_ref().unwrap();
            assert!(chat.rows.is_empty());
            if case == "identity-open" {
                let descriptor = chat.history.as_ref().unwrap().descriptor.as_ref().unwrap();
                assert_eq!(descriptor.counts.messages, 2);
                assert_eq!(descriptor.counts.non_system, 1);
            }
            Ok(())
        })
        .unwrap_or_else(|error| panic!("{case}: {error}; {}", root.display()));
    let r = collect(&root);
    let active: Vec<_> = r["wire"]
        .as_array()
        .unwrap()
        .iter()
        .filter_map(|row| {
            let result = row.get("result")?;
            (result["kind"] == "work_active").then_some(result)
        })
        .collect();
    assert_eq!(active.len(), 2);
    assert_ne!(active[0]["instance"], active[1]["instance"]);
    assert_eq!(active[1]["runtime_policy"]["generation"], 0);
    assert_eq!(
        active[1]["runtime_policy"]["facts"]["requested_cli_profile"],
        Value::Null
    );
    let target_profile = &active[1]["runtime_policy"]["facts"]["chosen_profile"];
    let previous_profile = &r["readbacks"][0]["current"]["chosen_profile"];
    if case == "identity-open" {
        assert_eq!(
            active[1]["identity"]["session_id"],
            r["saved_target"]["session_id"]
        );
        assert_eq!(target_profile, &r["saved_target"]["profile"]);
        assert_ne!(target_profile, previous_profile);
    } else {
        assert_eq!(target_profile, previous_profile);
    }
    let e = &r["shutdown"]["result"]["runtime_controls_shutdown"];
    assert_eq!(e["work_instance"], active[1]["instance"]);
    assert_eq!(e["published_generation"], 0);
    assert_eq!(e["disposition"], "idle");
    let prior = r["wire"]
        .as_array()
        .unwrap()
        .iter()
        .find(|row| {
            row["result"]["runtime_controls_shutdown"]["work_instance"] == active[0]["instance"]
        })
        .unwrap();
    assert_eq!(
        prior["result"]["runtime_controls_shutdown"]["work_instance"],
        active[0]["instance"]
    );
    assert_eq!(
        prior["result"]["runtime_controls_shutdown"]["published_generation"],
        1
    );
    assert_eq!(r["requests"], json!([]));
    native_result(
        &root,
        case,
        json!({"composer_preserved":true,"target_generation":0}),
        &frames,
    );
}

#[test]
#[ignore = "actual captured host stream and queued user-input ledger"]
fn midturn_queues_source_bound_draft_and_preserves_an_unconfirmed_interrupt() {
    for case in [
        "midturn-normal",
        "midturn-swapped",
        "host-midturn-boundary",
        "midturn-stale",
        "midturn-quit",
    ] {
        let swapped = case == "midturn-swapped";
        let tool = case == "host-midturn-boundary";
        let (runtime, root) = launch(case);
        let draft = "  Exact queue draft with Unicode 雪  ";
        let result=runtime.with_cleanup(|runtime| {
            let mut app=Demo::live("custom".into(),"switch-old".into());
            wait(runtime,&mut app,|e|matches!(e,Observation::Ready))?;
            let chat=app.live.as_mut().unwrap();
            chat.submit("Original foreground request")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime,&mut app,&root,if tool{"old-tool-running"}else{"stream-active"})?;
            app.insert_text(draft);
            app.composer.move_cursor(Motion::WordLeft,true);
            let caret=(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned());
            for (chord,accepted) in [(!swapped,false),(swapped,true)] {
                app.submit_chord(chord);
                let intent=app.live.as_mut().unwrap().work.take_intent().ok_or_else(||io::Error::other(format!("No queue intent: {}",app.notice)))?;
                runtime.work(intent)?;
                wait(runtime,&mut app,|e|matches!(e,Observation::Work(_)))?;
                let screen=draw(&mut app);
                assert_eq!(app.composer.text(), draft);
                assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
                if !accepted {
                    assert!(screen.contains("interrupt"),"{screen}");
                    app.live.as_mut().unwrap().work.key(crossterm::event::KeyEvent::new(crossterm::event::KeyCode::Esc,crossterm::event::KeyModifiers::NONE));
                } else {
                    assert!(screen.contains("next safe boundary") && screen.contains("Exact queue draft"),"{screen}");
                    if case=="midturn-stale" {
                        fs::write(root.join("release-stream"),b"retire reviewed owner before Apply")?;
                        while app.live.as_ref().unwrap().busy {app.observe_live(next(runtime)?)?;}
                    }
                    app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                    let intent=app.live.as_mut().unwrap().work.take_intent().expect("one reviewed Apply");
                    assert_eq!(app.composer.text(),draft);
                    runtime.work(intent)?;
                    wait(runtime,&mut app,|e|matches!(e,Observation::Work(_)))?;
                    if case=="midturn-stale" {
                        assert!(draw(&mut app).contains("foreground turn changed"));
                        assert_eq!(app.composer.text(),draft);
                        assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
                        return runtime.quit();
                    }
                    assert_eq!(app.composer.text(),"");
                }
            }
            if case=="midturn-quit" {return runtime.quit()}
            fs::write(root.join(if tool{"release-tool"}else{"release-stream"}),b"finish original and consume exactly one queue entry")?;
            let mut settled=false;
            // The old reply and the genuine next Work admission both complete.
            for _ in 0..160 {
                let e=next(runtime)?;
                settled |=matches!(&e,Observation::WorkExecution(update) if matches!(update.as_ref(),litetui_terminal::live::WorkExecution::Settled{..}));
                app.observe_live(e)?;
                if settled && !app.live.as_ref().unwrap().busy {return runtime.quit()}
            }
            Err(io::Error::other("Queued input did not settle"))
        });
        assert!(result.is_ok(), "{case} {result:?} {}", root.display());
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        let requests = r["requests"].as_array().unwrap();
        let inputs = r["work"]["inputs"].as_array().unwrap();
        if case == "midturn-normal" {
            let refused = &r["queue_refusals"];
            assert!(refused["replay"].as_str().unwrap().contains("consumed"));
            assert!(
                refused["wrong-policy"]
                    .as_str()
                    .unwrap()
                    .contains("different runtime policy")
            );
            assert!(
                refused["wrong-instance"]
                    .as_str()
                    .unwrap()
                    .contains("another version or Work session")
            );
            for label in ["wrong-operation", "wrong-hook"] {
                assert!(
                    refused[label]
                        .as_str()
                        .unwrap()
                        .contains("foreground turn finished or changed")
                );
            }
        }
        match case {
            "midturn-stale" => {
                assert!(inputs.is_empty());
                assert_eq!(requests.len(), 1);
            }
            "midturn-quit" => {
                assert_eq!(requests.len(), 1);
                assert_eq!(inputs.len(), 1);
                assert_eq!(inputs[0]["state"], "held");
                assert_eq!(inputs[0]["admission"], Value::Null);
                assert_eq!(inputs[0]["payload"]["content"], draft);
                let recovered = r["queue_recovered"].as_array().unwrap();
                assert_eq!(recovered.len(), 1);
                assert_eq!(recovered[0]["state"], "held");
                assert_eq!(recovered[0]["input"], inputs[0]["ref"]);
                assert_eq!(recovered[0]["producer"]["kind"], "user_queue");
            }
            _ => {
                assert_eq!(requests.len(), 2, "{case}");
                let messages = requests[1]["messages"].as_array().unwrap();
                assert_eq!(
                    messages
                        .iter()
                        .filter(|m| m["role"] == "user" && m["content"] == draft)
                        .count(),
                    1
                );
                assert_eq!(inputs.len(), 1);
                assert_eq!(inputs[0]["state"], "settled");
                let admission = &inputs[0]["admission"];
                assert_eq!(admission["mode"], if tool { "in_turn" } else { "new_turn" });
                let parent = r["wire"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|row| row["result"]["kind"] == "midturn_review")
                    .unwrap();
                if tool {
                    assert_eq!(admission["origin"], parent["result"]["origin"]);
                    let queued = messages.iter().position(|m| m["content"] == draft).unwrap();
                    assert_eq!(
                        messages[..queued]
                            .iter()
                            .filter(|m| m["role"] == "tool")
                            .count(),
                        2
                    );
                } else {
                    assert_ne!(
                        admission["origin"]["operation_id"],
                        parent["result"]["origin"]["operation_id"]
                    );
                }
            }
        }
        assert_eq!(r["denied"], json!([]));
    }
}

const TINY_PNG: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
fn work_key(runtime: &mut Interactive, app: &mut Demo, code: KeyCode) -> io::Result<()> {
    draw(app);
    let work = &mut app.live.as_mut().unwrap().work;
    work.key(KeyEvent::new(code, KeyModifiers::NONE));
    if let Some(intent) = work.take_intent() {
        runtime.work(intent)?;
        wait(runtime, app, |e| matches!(e, Observation::Work(_)))?;
    }
    Ok(())
}
fn attach_image(runtime: &mut Interactive, app: &mut Demo, path: &Path) -> io::Result<()> {
    app.open_image();
    assert!(app.live.as_ref().unwrap().work.image.open, "{}", app.notice);
    draw(app);
    app.live
        .as_mut()
        .unwrap()
        .work
        .paste(path.to_str().unwrap());
    work_key(runtime, app, KeyCode::Enter)?;
    let screen = draw(app);
    assert!(screen.contains("Enter attaches"), "{screen}");
    work_key(runtime, app, KeyCode::Enter)?;
    assert!(app.live.as_ref().unwrap().work.image.attached.is_some());
    Ok(())
}
fn find(root: &Path, name: &str) -> Option<PathBuf> {
    for entry in fs::read_dir(root).ok()?.flatten() {
        let path = entry.path();
        if path.file_name().is_some_and(|n| n == name) {
            return Some(path);
        }
        if path.is_dir()
            && let Some(found) = find(&path, name)
        {
            return Some(found);
        }
    }
    None
}

#[test]
#[ignore = "actual captured host stream, reviewed image and queued user-input ledger"]
fn midturn_queues_the_reviewed_image_with_the_message_and_never_replays_it() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use sha2::{Digest, Sha256};
    for case in ["midturn-image", "midturn-image-quit", "midturn-image-stale"] {
        let (runtime, root) = launch(case);
        let draft = "  Look at this image 雪  ";
        let picture = root.join("queued picture.png");
        let digest = std::cell::RefCell::new(String::new());
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            let chat = app.live.as_mut().unwrap();
            chat.submit("Original foreground request")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime, &mut app, &root, "stream-active")?;
            fs::write(&picture, STANDARD.decode(TINY_PNG).unwrap())?;
            attach_image(runtime, &mut app, &picture)?;
            let attached = app.live.as_ref().unwrap().work.image.attached.clone().unwrap();
            *digest.borrow_mut() = attached.wire()["png_sha256"].as_str().unwrap().into();
            // The reviewed pixels are the source's retained copy; replacing the file changes nothing.
            fs::remove_file(&picture)?;
            app.insert_text(draft);
            app.submit_chord(false);
            let intent = app.live.as_mut().unwrap().work.take_intent().ok_or_else(|| {
                io::Error::other(format!("No queue intent: {}", app.notice))
            })?;
            runtime.work(intent)?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Work(_)))?;
            let screen = draw(&mut app);
            assert!(
                screen.contains("next safe boundary")
                    && screen.contains("Attached image: queued picture.png")
                    && screen.contains(digest.borrow().as_str()),
                "{screen}"
            );
            assert_eq!(app.composer.text(), draft);
            if case == "midturn-image-stale" {
                fs::write(root.join("release-stream"), b"retire reviewed owner before Apply")?;
                while app.live.as_ref().unwrap().busy {
                    app.observe_live(next(runtime)?)?;
                }
            }
            app.live
                .as_mut()
                .unwrap()
                .work
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            let intent = app.live.as_mut().unwrap().work.take_intent().expect("one reviewed Apply");
            // Nothing leaves the composer until the source has confirmed the queue.
            assert_eq!(app.composer.text(), draft);
            assert_eq!(app.live.as_ref().unwrap().work.image.attached, Some(attached.clone()));
            runtime.work(intent)?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Work(_)))?;
            if case == "midturn-image-stale" {
                assert!(draw(&mut app).contains("foreground turn changed"));
                assert_eq!(app.composer.text(), draft);
                assert_eq!(app.live.as_ref().unwrap().work.image.attached, Some(attached));
                return runtime.quit();
            }
            assert_eq!(app.composer.text(), "");
            assert!(app.live.as_ref().unwrap().work.image.attached.is_none());
            if case == "midturn-image-quit" {
                return runtime.quit();
            }
            fs::write(root.join("release-stream"), b"finish original and consume exactly one queue entry")?;
            let mut settled = false;
            for _ in 0..160 {
                let e = next(runtime)?;
                settled |= matches!(&e, Observation::WorkExecution(update) if matches!(update.as_ref(), litetui_terminal::live::WorkExecution::Settled { .. }));
                app.observe_live(e)?;
                if settled && !app.live.as_ref().unwrap().busy {
                    return runtime.quit();
                }
            }
            Err(io::Error::other("Queued image input did not settle"))
        });
        assert!(result.is_ok(), "{case} {result:?} {}", root.display());
        let digest = digest.into_inner();
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        let requests = r["requests"].as_array().unwrap();
        let inputs = r["work"]["inputs"].as_array().unwrap();
        let stored = find(&root, &format!("queued-{digest}.png"));
        if case == "midturn-image-stale" {
            assert!(inputs.is_empty() && stored.is_none());
            assert_eq!(requests.len(), 1);
            continue;
        }
        let identity = &inputs[0]["payload"]["image"];
        assert_eq!(identity["png_sha256"], digest);
        assert_eq!(identity["name"], "queued picture.png");
        let stored = fs::read(stored.expect("conversation-owned queued image file")).unwrap();
        assert_eq!(format!("{:x}", Sha256::digest(&stored)), digest);
        assert_eq!(identity["png_bytes"], stored.len());
        assert_eq!(inputs[0]["payload"]["content"], draft);
        if case == "midturn-image-quit" {
            assert_eq!(requests.len(), 1, "a held queued image is never replayed");
            assert_eq!(inputs.len(), 1);
            assert_eq!(inputs[0]["state"], "held");
            assert_eq!(inputs[0]["admission"], Value::Null);
            let recovered = r["queue_recovered"].as_array().unwrap();
            assert_eq!(recovered.len(), 1);
            assert_eq!(recovered[0]["state"], "held");
            assert_eq!(recovered[0]["input"], inputs[0]["ref"]);
        } else {
            assert_eq!(requests.len(), 2);
            let messages = requests[1]["messages"].as_array().unwrap();
            let sent: Vec<&Value> = messages
                .iter()
                .filter(|m| m["role"] == "user" && m["content"].is_array())
                .collect();
            assert_eq!(sent.len(), 1);
            let parts = sent[0]["content"].as_array().unwrap();
            assert_eq!(parts[1], json!({"type": "text", "text": draft}));
            let url = parts[0]["image_url"]["url"].as_str().unwrap();
            let pixels = STANDARD
                .decode(url.strip_prefix("data:image/png;base64,").unwrap())
                .unwrap();
            assert_eq!(pixels, stored);
            assert_eq!(inputs[0]["state"], "settled");
            assert_eq!(inputs[0]["admission"]["mode"], "new_turn");
        }
        assert_eq!(r["denied"], json!([]));
    }
}

#[test]
#[ignore = "actual source-owned soft interrupt and queued successor"]
fn midturn_interrupt_preserves_order_worker_and_draft() {
    for case in [
        "midturn-interrupt-priority",
        "midturn-interrupt-swapped",
        "midturn-interrupt-stop",
        "midturn-interrupt-quit",
        "host-midturn-interrupt-tool",
    ] {
        let (runtime, root) = launch(case);
        let older = "Earlier queued text";
        let urgent = "  Please follow this instead • 雪  ";
        let swapped = case == "midturn-interrupt-swapped";
        let tool = case == "host-midturn-interrupt-tool";
        let result=runtime.with_cleanup(|runtime|{
            let mut app=Demo::live("custom".into(),"switch-old".into());
            wait(runtime,&mut app,|e|matches!(e,Observation::Ready))?;
            let chat=app.live.as_mut().unwrap();
            chat.submit("Original foreground request")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime,&mut app,&root,if tool{"old-tool-running"}else{"stream-active"})?;
            if case=="midturn-interrupt-priority"{
                app.insert_text(older);
                app.submit_chord(swapped);
                dispatch_midturn(runtime,&mut app)?;
                assert!(draw(&mut app).contains("next safe boundary"));
                app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
                dispatch_midturn(runtime,&mut app)?;
                assert_eq!(app.composer.text(),"");
            }
            app.insert_text(urgent);
            app.composer.move_cursor(Motion::WordLeft,true);
            app.submit_chord(!swapped);
            dispatch_midturn(runtime,&mut app)?;
            let screen=draw(&mut app);
            let expected_prior=if case=="midturn-interrupt-priority"{1}else{0};
            assert!(screen.contains("Interrupt the current reply") && screen.contains(&format!("ahead of {expected_prior} queued")),"{screen}");
            let chat=app.live.as_mut().unwrap();
            chat.work.key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
            assert!(chat.work.take_intent().is_none());
            if case=="midturn-interrupt-stop" {
                app.open_stop();
                let chat=app.live.as_mut().unwrap();
                chat.request_stop()?;
                let abort=chat.take_abort().unwrap();
                runtime.abort(abort)?;
                while app.live.as_ref().unwrap().busy {app.observe_live(next(runtime)?)?;}
                let _=draw(&mut app);
                app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Char('i'),KeyModifiers::NONE));
                dispatch_midturn(runtime,&mut app)?;
                assert_eq!(app.composer.text(),urgent);
                return runtime.quit()
            }
            chat.work.key(KeyEvent::new(KeyCode::Char('i'),KeyModifiers::NONE));
            assert_eq!(app.composer.text(),urgent);
            dispatch_midturn(runtime,&mut app)?;
            assert_eq!(app.composer.text(),"");
            let chat=app.live.as_mut().unwrap();
            assert!(chat.cancelling() && chat.take_abort().is_none());
            assert!(chat.notice.contains("Interrupt accepted"));
            if case=="midturn-interrupt-quit" {return runtime.quit()}
            fs::write(root.join(if tool{"release-tool"}else{"release-stream"}),b"observe the cooperative soft stop")?;
            let mut stopped=false;
            let mut settled=0;
            for _ in 0..200 {
                let e=next(runtime)?;
                stopped|=matches!(&e,Observation::Cancelled(_));
                if matches!(&e,Observation::WorkExecution(update) if matches!(update.as_ref(),litetui_terminal::live::WorkExecution::Settled{..})){settled+=1;}
                app.observe_live(e)?;
                if stopped && settled==expected_prior+1 && !app.live.as_ref().unwrap().busy{return runtime.quit()}
            }
            Err(io::Error::other("Missing interrupt cancellation or successor settlement"))
        });
        assert!(result.is_ok(), "{case} {} {result:?}", root.display());
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        let requests = r["requests"].as_array().unwrap();
        let inputs = r["work"]["inputs"].as_array().unwrap();
        assert_eq!(r["denied"], json!([]));
        if case == "midturn-interrupt-stop" {
            assert!(inputs.is_empty());
            assert_eq!(requests.len(), 1);
            continue;
        }
        let urgent_input = inputs
            .iter()
            .find(|v| v["payload"]["text"] == urgent)
            .unwrap();
        if case == "midturn-interrupt-quit" {
            assert_eq!(requests.len(), 1);
            assert_eq!(urgent_input["state"], "held");
            assert_eq!(
                r["queue_recovered"][0]["producer"]["kind"],
                "user_interrupt"
            );
            assert_eq!(r["queue_recovered"][0]["input"], urgent_input["ref"]);
            continue;
        }
        assert_eq!(urgent_input["state"], "settled");
        assert_eq!(urgent_input["admission"]["mode"], "new_turn");
        assert_eq!(
            requests.len(),
            if case == "midturn-interrupt-priority" {
                3
            } else {
                2
            }
        );
        let messages = requests[1]["messages"].as_array().unwrap();
        assert_eq!(messages.last().unwrap()["content"], urgent);
        assert!(!messages.iter().any(|m| m["content"] == older));
        if tool {
            let calls: Vec<_> = messages
                .iter()
                .filter_map(|m| m["tool_calls"].as_array())
                .flatten()
                .map(|call| call["id"].clone())
                .collect();
            let paired: Vec<_> = messages
                .iter()
                .filter(|m| m["role"] == "tool")
                .map(|m| m["tool_call_id"].clone())
                .collect();
            assert_eq!(calls.len(), 2);
            assert_eq!(calls, paired);
        }
        if requests.len() == 3 {
            assert!(
                r["legacy_queue_refusal"]
                    .as_str()
                    .unwrap()
                    .contains("would interrupt")
            );
            assert!(
                r["changed_order_refusal"]
                    .as_str()
                    .unwrap()
                    .contains("stale or consumed")
            );
            let next = requests[2]["messages"].as_array().unwrap();
            assert_eq!(next.last().unwrap()["content"], older);
            assert_eq!(next.iter().filter(|m| m["content"] == urgent).count(), 1);
        }
    }
}
fn dispatch_midturn(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .work
        .take_intent()
        .ok_or_else(|| io::Error::other(format!("Missing mid-turn intent: {}", app.notice)))?;
    runtime.work(intent)?;
    wait(runtime, app, |e| matches!(e, Observation::Work(_)))
}

fn pause_key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .work
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn pause_send(runtime: &Interactive, app: &mut Demo) -> io::Result<()> {
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .work
        .take_intent()
        .ok_or_else(|| io::Error::other(format!("No chat pause intent: {}", app.notice)))?;
    runtime.work(intent)
}
fn pause_review(runtime: &mut Interactive, app: &mut Demo) -> io::Result<String> {
    app.open_chat_pause();
    pause_send(runtime, app)?;
    wait(runtime, app, |e| matches!(e, Observation::Work(_)))?;
    Ok(draw(app))
}
fn pause_confirm(runtime: &mut Interactive, app: &mut Demo) -> io::Result<String> {
    pause_key(app, KeyCode::Enter);
    pause_send(runtime, app)?;
    wait(runtime, app, |e| matches!(e, Observation::Work(_)))?;
    Ok(draw(app))
}
#[test]
#[ignore = "actual host pause round boundary, explicit resume, Stop and Quit"]
fn host_chat_pause_holds_next_round_and_preserves_draft_through_resume_stop_and_quit() {
    for case in [
        "pause-idle",
        "host-pause-resume",
        "host-pause-stop",
        "host-pause-quit",
        "host-pause-readback-error",
    ] {
        let (runtime, root) = launch(case);
        let draft = "  Preserved pause draft 雪  ";
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text(draft);
            app.composer.move_cursor(Motion::WordLeft, true);
            let caret = (
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned(),
            );
            if case != "pause-idle" {
                let chat = app.live.as_mut().unwrap();
                chat.submit("One model round, two controlled tools, then one continuation")?;
                runtime.submit(chat.take_submission().unwrap())?;
                stage(runtime, &mut app, &root, "old-tool-running")?;
            }
            let screen = pause_review(runtime, &mut app)?;
            assert!(
                screen.contains("Pause before the next host model round?"),
                "{screen}"
            );
            let screen = pause_confirm(runtime, &mut app)?;
            if case == "host-pause-readback-error" {
                assert!(
                    screen.contains("needs a fresh review") && !screen.contains("Verified."),
                    "{screen}"
                );
            } else {
                assert!(screen.contains("Verified."), "{screen}");
            }
            if case == "pause-idle" {
                assert!(
                    screen.contains("The next host model round will wait"),
                    "{screen}"
                );
                pause_key(&mut app, KeyCode::Esc);
                let chat = app.live.as_mut().unwrap();
                chat.submit("Prompt admitted while paused")?;
                runtime.submit(chat.take_submission().unwrap())?;
            } else {
                if case != "host-pause-readback-error" {
                    assert!(screen.contains("Pause requested"), "{screen}");
                }
                pause_key(&mut app, KeyCode::Esc);
                fs::write(
                    root.join("release-tool"),
                    b"finish existing tools, hold next model round",
                )?;
            }
            stage(runtime, &mut app, &root, "chat-held")?;
            let screen = pause_review(runtime, &mut app)?;
            assert!(
                screen.contains("Paused at the next model-round boundary."),
                "{screen}"
            );
            assert_eq!(app.composer.text(), draft);
            assert_eq!(
                caret,
                (
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned()
                )
            );
            if case == "host-pause-quit" {
                return runtime.quit();
            }
            if case == "host-pause-stop" {
                app.live.as_mut().unwrap().request_stop()?;
                send(runtime, &mut app)?;
                while app.live.as_ref().unwrap().busy {
                    app.observe_live(next(runtime)?)?;
                }
                pause_key(&mut app, KeyCode::Esc);
                assert!(pause_review(runtime, &mut app)?.contains("Resume the host chat loop?"));
            }
            let screen = pause_confirm(runtime, &mut app)?;
            assert!(screen.contains("Verified. Running."), "{screen}");
            pause_key(&mut app, KeyCode::Esc);
            while app.live.as_ref().unwrap().busy {
                app.observe_live(next(runtime)?)?;
            }
            assert_eq!(app.composer.text(), draft);
            assert_eq!(
                caret,
                (
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned()
                )
            );
            runtime.quit()
        });
        assert!(result.is_ok(), "{case}: {result:?} {}", root.display());
        let r = collect(&root);
        let requests = r["requests"].as_array().unwrap();
        assert_eq!(
            requests.len(),
            if matches!(case, "host-pause-resume" | "host-pause-readback-error") {
                2
            } else {
                1
            }
        );
        let pauses = r["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e["kind"] == "chat-pause")
            .collect::<Vec<_>>();
        assert!(pauses.iter().any(|e| e["result"]["stage"] == "held"));
        let held = pauses
            .iter()
            .find(|e| e["result"]["stage"] == "held")
            .unwrap();
        assert_eq!(held["requests"], if case == "pause-idle" { 0 } else { 1 });
        let exits = r["events"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|e| e["kind"] == "tool-exit")
            .count();
        assert_eq!(exits, if case == "pause-idle" { 0 } else { 2 });
    }
}

#[test]
#[ignore = "actual stale host pause owner and declined pause review"]
fn host_chat_pause_refuses_retired_review_without_changing_prompt_or_pause() {
    let (runtime, root) = launch("host-pause-stale");
    let result = runtime.with_cleanup(|runtime| {
        let mut app = Demo::live("custom".into(), "switch-old".into());
        wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
        app.insert_text(" /pause ");
        let chat = app.live.as_mut().unwrap();
        chat.submit("Finish this exact foreground turn")?;
        runtime.submit(chat.take_submission().unwrap())?;
        stage(runtime, &mut app, &root, "old-tool-running")?;
        app.submit_chord(false);
        pause_send(runtime, &mut app)?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Work(_)))?;
        assert!(draw(&mut app).contains("Pause before the next host model round?"));
        pause_key(&mut app, KeyCode::Esc);
        assert_eq!(app.composer.text(), " /pause ");
        app.submit_chord(false);
        pause_send(runtime, &mut app)?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Work(_)))?;
        assert!(draw(&mut app).contains("Pause before the next host model round?"));
        fs::write(
            root.join("release-tool"),
            b"Retire the reviewed source execution",
        )?;
        while app.live.as_ref().unwrap().busy {
            app.observe_live(next(runtime)?)?;
        }
        let screen = pause_confirm(runtime, &mut app)?;
        assert!(
            screen.contains("reviewed chat pause state changed") && !screen.contains("Verified."),
            "{screen}"
        );
        assert_eq!(app.composer.text(), " /pause ");
        pause_key(&mut app, KeyCode::F(5));
        pause_send(runtime, &mut app)?;
        wait(runtime, &mut app, |e| matches!(e, Observation::Work(_)))?;
        assert!(draw(&mut app).contains("Running. Host model rounds may proceed."));
        runtime.quit()
    });
    assert!(result.is_ok(), "{result:?} {}", root.display());
    let r = collect(&root);
    assert_eq!(r["requests"].as_array().unwrap().len(), 2);
    assert!(
        !r["events"]
            .as_array()
            .unwrap()
            .iter()
            .any(|e| e["kind"] == "chat-pause" && e["result"]["paused"] == true)
    );
}

#[test]
#[ignore = "actual source-managed dictation, private generated helper, no devices"]
fn dictation_voice_route_preserves_draft_and_joins_capture() {
    for case in [
        "dictation-apply",
        "dictation-empty",
        "dictation-failed",
        "dictation-cancel",
        "dictation-quit",
    ] {
        let (runtime, root) = launch(case);
        let original = "Keep the existing draft";
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text(original);
            app.composer.move_cursor(Motion::WordLeft, true);
            let caret = (
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned(),
            );
            app.open_settings();
            runtime.settings(app.live.as_mut().unwrap().settings.take_intent().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Settings(_)))?;
            let _ = draw(&mut app);
            app.live
                .as_mut()
                .unwrap()
                .settings
                .key(KeyEvent::new(KeyCode::Char('v'), KeyModifiers::NONE));
            runtime.settings(app.live.as_mut().unwrap().settings.take_intent().unwrap())?;
            wait(runtime, &mut app, |e| matches!(e, Observation::Settings(_)))?;
            assert!(draw(&mut app).contains("D dictate to composer"));
            assert!(
                app.live
                    .as_ref()
                    .unwrap()
                    .settings
                    .dictation_key(KeyEvent::new(KeyCode::Char('d'), KeyModifiers::NONE))
            );
            app.live.as_mut().unwrap().settings.close();
            app.open_dictation();
            dictation_send(runtime, &mut app)?;
            let screen = draw(&mut app);
            assert!(
                screen.contains("60 seconds") && screen.contains("must already be"),
                "{screen}"
            );
            app.live
                .as_mut()
                .unwrap()
                .work
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            dictation_send(runtime, &mut app)?;
            assert_eq!(app.composer.text(), original);
            assert_eq!(
                caret,
                (
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned()
                )
            );
            if case == "dictation-quit" {
                return runtime.quit();
            }
            let _ = draw(&mut app);
            let key = if case == "dictation-cancel" {
                KeyCode::Esc
            } else {
                KeyCode::Enter
            };
            app.live
                .as_mut()
                .unwrap()
                .work
                .key(KeyEvent::new(key, KeyModifiers::NONE));
            dictation_send(runtime, &mut app)?;
            if matches!(
                case,
                "dictation-apply" | "dictation-empty" | "dictation-failed"
            ) {
                let expected = match case {
                    "dictation-empty" => "No speech was recognized",
                    "dictation-failed" => "Dictation failed",
                    _ => "Fixture dictated words",
                };
                for _ in 0..90 {
                    let screen = draw(&mut app);
                    if screen.contains(expected) {
                        break;
                    }
                    std::thread::sleep(Duration::from_millis(50));
                    if let Some(intent) = app.live.as_mut().unwrap().work.take_intent() {
                        runtime.work(intent)?;
                        wait(runtime, &mut app, |e| matches!(e, Observation::Work(_)))?;
                    }
                }
                assert!(draw(&mut app).contains(expected));
                assert_eq!(app.composer.text(), original);
                assert_eq!(
                    caret,
                    (
                        app.composer.before_cursor().to_owned(),
                        app.composer.selected_text().to_owned()
                    )
                );
                app.live
                    .as_mut()
                    .unwrap()
                    .work
                    .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
                if case == "dictation-apply" {
                    dictation_send(runtime, &mut app)?;
                    assert_eq!(
                        app.composer.text(),
                        format!("{original} Fixture dictated words")
                    );
                } else {
                    assert!(app.live.as_mut().unwrap().work.take_intent().is_none());
                    assert_eq!(app.composer.text(), original);
                    app.live
                        .as_mut()
                        .unwrap()
                        .work
                        .key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
                    dictation_send(runtime, &mut app)?;
                }
            } else {
                assert_eq!(app.composer.text(), original);
            }
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
            runtime.quit()
        });
        assert!(result.is_ok(), "{case} {result:?} {}", root.display());
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        assert_eq!(r["denied"], json!([]));
        assert_eq!(r["requests"], json!([]));
        assert_eq!(r["dictation_retired"], true);
        assert_eq!(r["dictation_directories_removed"], true);
        assert_eq!(r["dictation_directories"].as_array().unwrap().len(), 1);
    }
}
fn dictation_send(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    runtime.work(
        app.live
            .as_mut()
            .unwrap()
            .work
            .take_intent()
            .ok_or_else(|| {
                io::Error::other(format!("Missing dictation request: {}", app.notice))
            })?,
    )?;
    wait(runtime, app, |e| matches!(e, Observation::Work(_)))
}

#[test]
#[ignore = "real registered source and pinned SDK fixture, no live provider"]
fn claude_queue_preserves_draft_and_delivers_only_after_original_turn_settles() {
    for case in [
        "claude-queue-normal",
        "claude-queue-tools",
        "claude-queue-stale",
        "claude-queue-quit",
        "claude-queue-uncertain",
        "claude-queue-emission",
    ] {
        let (runtime, root) = launch(case);
        let draft = "  Claude queued input 雪  ";
        let result=runtime.with_cleanup(|runtime| {
            let mut app=Demo::live("claude".into(),"claude-fixture".into());
            wait(runtime,&mut app,|e| matches!(e,Observation::Ready))?;
            let chat=app.live.as_mut().unwrap();
            chat.submit("Original controlled Claude input")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime,&mut app,&root,"stream-active")?;
            app.insert_text(draft);
            app.composer.move_cursor(Motion::WordLeft,true);
            let caret=(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned());
            // The alternate chord has its own explicit interrupt review; cancel preserves the draft.
            app.submit_chord(true);
            dispatch_midturn(runtime,&mut app)?;
            assert!(draw(&mut app).contains("Interrupt the current reply"));
            app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE));
            for cancel in [true,false] {
                assert_eq!(app.composer.text(),draft);
                assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
                app.submit_chord(false);
                dispatch_midturn(runtime,&mut app)?;
                let screen=draw(&mut app);
                assert!(screen.contains("next Claude turn") && screen.contains("does not interrupt"),"{screen}");
                assert_eq!(app.composer.text(),draft);
                if cancel {
                    app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Esc,KeyModifiers::NONE));
                    continue;
                }
                if case=="claude-queue-stale" {
                    fs::write(root.join("release-stream"),b"retire before Apply")?;
                    while app.live.as_ref().unwrap().busy { app.observe_live(next(runtime)?)?; }
                }
                app.live.as_mut().unwrap().work.key(KeyEvent::new(KeyCode::Enter,KeyModifiers::NONE));
                if case=="claude-queue-emission" {
                    let intent=app.live.as_mut().unwrap().work.take_intent().expect("reviewed Apply");
                    runtime.work(intent)?;
                    stage(runtime,&mut app,&root,"queue-emission-failed")?;
                    assert_eq!(app.composer.text(),draft);
                    assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
                    return runtime.quit();
                }
                dispatch_midturn(runtime,&mut app)?;
                if case=="claude-queue-stale" {
                    assert_eq!(app.composer.text(),draft);
                    assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
                    return runtime.quit();
                }
                if case=="claude-queue-uncertain" {
                    assert!(draw(&mut app).contains("uncertain"));
                    assert_eq!(app.composer.text(),draft);
                    assert_eq!(caret,(app.composer.before_cursor().to_owned(),app.composer.selected_text().to_owned()));
                    return runtime.quit();
                }
                assert_eq!(app.composer.text(),"");
            }
            if case=="claude-queue-quit" { return runtime.quit(); }
            fs::write(root.join("release-stream"),b"finish original before second query")?;
            let mut settled=false;
            for _ in 0..160 {
                let event=next(runtime)?;
                settled |=matches!(&event,Observation::WorkExecution(update) if matches!(update.as_ref(),litetui_terminal::live::WorkExecution::Settled{..}));
                app.observe_live(event)?;
                if settled && !app.live.as_ref().unwrap().busy { return runtime.quit(); }
            }
            Err(io::Error::other("Claude queued input did not settle"))
        });
        assert!(result.is_ok(), "{case}: {result:?} {}", root.display());
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        assert_eq!(r["denied"], json!([]));
        assert_eq!(r["sdk_retired"], true);
        assert_eq!(r["reader_retired"], true);
        assert!(
            r["workers"]
                .as_array()
                .unwrap()
                .iter()
                .all(|w| w["finished"] == true)
        );
        let queries = r["claude_queries"].as_array().unwrap();
        let inputs = r["work"]["inputs"].as_array().unwrap();
        if case == "claude-queue-stale" {
            assert_eq!(queries.len(), 1);
            assert!(inputs.is_empty());
            continue;
        }
        if case == "claude-queue-uncertain" {
            assert_eq!(queries.len(), 1);
            assert!(inputs.is_empty());
            let segment = r["claude_ledger"]["selected_segment"].as_str().unwrap();
            let prepared = r["claude_ledger"]["segments"][segment]["entries"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|e| e["state"] == "prepared")
                .collect::<Vec<_>>();
            assert_eq!(prepared.len(), 1);
            assert_eq!(prepared[0]["content"], draft);
            assert!(
                !r["wire"]
                    .as_array()
                    .unwrap()
                    .iter()
                    .any(|row| row["result"]["kind"] == "midturn_queued")
            );
            continue;
        }
        let wire = r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["result"]["kind"] == "midturn_queued")
            .unwrap();
        let delivery = &wire["result"]["native_delivery"];
        assert_eq!(inputs.len(), 1);
        assert_eq!(delivery["state"], "prepared");
        let segment = delivery["segment_id"].as_str().unwrap();
        let entry = r["claude_ledger"]["segments"][segment]["entries"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["id"] == delivery["id"])
            .unwrap();
        assert_eq!(entry["content"], draft);
        if matches!(case, "claude-queue-quit" | "claude-queue-emission") {
            assert_eq!(queries.len(), 1);
            if case == "claude-queue-emission" {
                assert_eq!(r["queue_emission_failed"], true);
            }
            assert_eq!(inputs[0]["state"], "held");
            assert_eq!(entry["state"], "prepared");
            let recovered = r["queue_recovered"].as_array().unwrap();
            assert_eq!(recovered.len(), 1);
            assert_eq!(recovered[0]["input"], inputs[0]["ref"]);
            assert_eq!(recovered[0]["state"], "held");
        } else {
            assert_eq!(queries.len(), 2);
            assert_eq!(queries[0]["tools_enabled"], case == "claude-queue-tools");
            assert_eq!(queries[0]["tools_enabled"], queries[1]["tools_enabled"]);
            assert_eq!(queries[0]["tools"], queries[1]["tools"]);
            assert_eq!(queries[0]["generation"], queries[1]["generation"]);
            assert!(queries[1]["text"].as_str().unwrap().ends_with(draft));
            assert_eq!(
                queries[1]["delivery"]["_claude_entry"]["id"],
                delivery["id"]
            );
            assert_eq!(inputs[0]["admission"]["mode"], "new_turn");
            assert_eq!(
                inputs[0]["admission"]["provider_delivery"]["id"],
                delivery["id"]
            );
            assert_eq!(inputs[0]["state"], "settled");
            assert_eq!(entry["state"], "terminal");
        }
    }
}

#[test]
#[ignore = "real registered source and pinned SDK fixture, no live provider"]
fn claude_queue_carries_the_reviewed_image_as_an_owned_file_without_replay() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use sha2::{Digest, Sha256};
    for case in ["claude-queue-image", "claude-queue-image-quit"] {
        let (runtime, root) = launch(case);
        let draft = "Claude queued picture 雪";
        let picture = root.join("claude picture.png");
        let digest = std::cell::RefCell::new(String::new());
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("claude".into(), "claude-fixture".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            let chat = app.live.as_mut().unwrap();
            chat.submit("Original controlled Claude input")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime, &mut app, &root, "stream-active")?;
            fs::write(&picture, STANDARD.decode(TINY_PNG).unwrap())?;
            attach_image(runtime, &mut app, &picture)?;
            let attached = app.live.as_ref().unwrap().work.image.attached.clone().unwrap();
            assert_eq!(attached.wire()["delivery"], "file");
            *digest.borrow_mut() = attached.wire()["png_sha256"].as_str().unwrap().into();
            app.insert_text(draft);
            app.submit_chord(false);
            dispatch_midturn(runtime, &mut app)?;
            let screen = draw(&mut app);
            assert!(
                screen.contains("next Claude turn")
                    && screen.contains("Attached image: claude picture.png")
                    && screen.contains("conversation-owned file path"),
                "{screen}"
            );
            app.live
                .as_mut()
                .unwrap()
                .work
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            assert_eq!(app.composer.text(), draft);
            assert_eq!(app.live.as_ref().unwrap().work.image.attached, Some(attached));
            dispatch_midturn(runtime, &mut app)?;
            assert_eq!(app.composer.text(), "");
            assert!(app.live.as_ref().unwrap().work.image.attached.is_none());
            if case == "claude-queue-image-quit" {
                return runtime.quit();
            }
            fs::write(root.join("release-stream"), b"finish original before second query")?;
            let mut settled = false;
            for _ in 0..160 {
                let event = next(runtime)?;
                settled |= matches!(&event, Observation::WorkExecution(update) if matches!(update.as_ref(), litetui_terminal::live::WorkExecution::Settled { .. }));
                app.observe_live(event)?;
                if settled && !app.live.as_ref().unwrap().busy {
                    return runtime.quit();
                }
            }
            Err(io::Error::other("Claude queued image input did not settle"))
        });
        assert!(result.is_ok(), "{case}: {result:?} {}", root.display());
        let digest = digest.into_inner();
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        assert_eq!(r["denied"], json!([]));
        assert_eq!(r["sdk_retired"], true);
        let queries = r["claude_queries"].as_array().unwrap();
        let inputs = r["work"]["inputs"].as_array().unwrap();
        let stored = find(&root, &format!("queued-{digest}.png")).expect("owned image file");
        assert_eq!(
            format!("{:x}", Sha256::digest(fs::read(&stored).unwrap())),
            digest
        );
        let wire = r["wire"]
            .as_array()
            .unwrap()
            .iter()
            .find(|row| row["result"]["kind"] == "midturn_queued")
            .unwrap();
        let delivery = &wire["result"]["native_delivery"];
        assert_eq!(inputs.len(), 1);
        assert_eq!(inputs[0]["payload"]["content"], draft);
        assert_eq!(inputs[0]["payload"]["image"]["png_sha256"], digest);
        let segment = delivery["segment_id"].as_str().unwrap();
        let entry = r["claude_ledger"]["segments"][segment]["entries"]
            .as_array()
            .unwrap()
            .iter()
            .find(|e| e["id"] == delivery["id"])
            .unwrap();
        let content = entry["content"].as_str().unwrap();
        assert!(content.starts_with(draft) && content.contains(&format!("queued-{digest}.png")));
        if case == "claude-queue-image-quit" {
            assert_eq!(
                queries.len(),
                1,
                "a prepared queued image is never replayed"
            );
            assert_eq!(inputs[0]["state"], "held");
            assert_eq!(entry["state"], "prepared");
            let recovered = r["queue_recovered"].as_array().unwrap();
            assert_eq!(recovered.len(), 1);
            assert_eq!(recovered[0]["input"], inputs[0]["ref"]);
            assert_eq!(recovered[0]["state"], "held");
        } else {
            assert_eq!(queries.len(), 2);
            assert_eq!(queries[1]["text"].as_str().unwrap(), content);
            assert_eq!(inputs[0]["state"], "settled");
            assert_eq!(entry["state"], "terminal");
        }
    }
}

#[test]
#[ignore = "actual source-owned response playback through synthetic device-free speech helpers"]
fn selected_response_playback_reviews_exact_text_and_preserves_remote_ownership() {
    for case in [
        "playback-local",
        "playback-edge",
        "playback-queued",
        "playback-uncertain",
        "playback-stop",
        "playback-quit",
        "playback-disabled",
    ] {
        let (runtime, root) = launch(case);
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("Preserve my unsent playback draft");
            app.composer.move_cursor(Motion::WordLeft, true);
            let original = (
                app.composer.text().to_owned(),
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned(),
            );
            app.open_history();
            playback_send(runtime, &mut app)?;
            let history_screen = draw(&mut app);
            assert!(
                history_screen.contains("S speak")
                    || history_screen.contains("S selects an answer"),
                "{history_screen}"
            );
            playback_key(&mut app, KeyCode::Char('s'));
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .saved_history
                    .take_intent()
                    .is_none()
            );
            assert!(draw(&mut app).contains("Select saved answer"));
            playback_key(&mut app, KeyCode::Right);
            assert!(draw(&mut app).contains("Selected second answer"));
            playback_key(&mut app, KeyCode::Enter);
            playback_send(runtime, &mut app)?;
            let screen = draw(&mut app);
            if case == "playback-disabled" {
                assert!(screen.contains("Enable response speech"), "{screen}");
                playback_key(&mut app, KeyCode::Esc);
                return runtime.quit();
            }
            assert!(
                screen.contains("Selected second answer with and a final sentence."),
                "{screen}"
            );
            assert!(!screen.contains("inline code"));
            assert!(screen.contains("Read every page"));
            playback_key(&mut app, KeyCode::Enter);
            playback_send(runtime, &mut app)?;
            let expected = match case {
                "playback-queued" => "LiteSuite acknowledged queued speech",
                "playback-uncertain" => "Speech acknowledgement is uncertain",
                "playback-stop" | "playback-quit" => "Local playback is active",
                _ => "local playback helper finished",
            };
            for _ in 0..120 {
                if draw(&mut app).contains(expected) {
                    break;
                }
                std::thread::sleep(Duration::from_millis(50));
                if let Some(intent) = app.live.as_mut().unwrap().saved_history.take_intent() {
                    runtime.history(intent)?;
                    wait(runtime, &mut app, |e| {
                        matches!(e, Observation::SavedHistory(_))
                    })?;
                }
            }
            assert!(
                draw(&mut app).contains(expected),
                "{case} {}",
                draw(&mut app)
            );
            assert_eq!(
                original,
                (
                    app.composer.text().to_owned(),
                    app.composer.before_cursor().to_owned(),
                    app.composer.selected_text().to_owned()
                )
            );
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
            if case == "playback-quit" {
                return runtime.quit();
            }
            // A second Enter never resubmits a queued, uncertain, or completed response.
            if !matches!(case, "playback-stop") {
                playback_key(&mut app, KeyCode::Enter);
                assert!(
                    app.live
                        .as_mut()
                        .unwrap()
                        .saved_history
                        .take_intent()
                        .is_none()
                );
            }
            playback_key(&mut app, KeyCode::Esc);
            playback_send(runtime, &mut app)?;
            if case == "playback-queued" {
                assert!(app.notice.contains("owns its queue"), "{}", app.notice)
            }
            runtime.quit()
        });
        assert!(result.is_ok(), "{case} {result:?} {}", root.display());
        let report: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        assert_eq!(report["denied"], json!([]));
        assert_eq!(report["requests"], json!([]));
        assert_eq!(report["playback_retired"], true);
        assert_eq!(report["playback_directories_removed"], true);
        if case == "playback-disabled" {
            assert!(report["playback_directories"].is_null());
        } else {
            assert_eq!(report["playback_directories"].as_array().unwrap().len(), 1);
            let parameters: Value =
                serde_json::from_slice(&fs::read(root.join("speech-parameters.json")).unwrap())
                    .unwrap();
            assert_eq!(
                parameters["text"],
                "Selected second answer with and a final sentence."
            );
            assert_eq!(
                parameters["engine"],
                if case == "playback-edge" {
                    "edge"
                } else {
                    "pyttsx3"
                }
            );
            assert_eq!(
                parameters["voice"],
                if case == "playback-edge" {
                    "Private edge voice"
                } else {
                    "Private fixture voice"
                }
            );
            assert_eq!(parameters["timeout"], 10);
            if matches!(case, "playback-stop" | "playback-quit") {
                // The fixture pinned the helper directory past close/shutdown: the
                // source had to outlast a late directory release, not race it.
                assert_eq!(
                    report["playback_pinned"], report["playback_directories"][0],
                    "{case} directory pin"
                );
                std::thread::sleep(Duration::from_millis(8100));
                assert!(
                    !root.join("speech-completed").exists(),
                    "local speech descendant escaped cancellation"
                );
            }
            if matches!(case, "playback-queued" | "playback-uncertain") {
                assert!(
                    !root.join("speech-completed").exists(),
                    "remote dispatch replayed locally"
                );
            }
        }
    }
}
fn playback_key(app: &mut Demo, code: KeyCode) {
    app.live
        .as_mut()
        .unwrap()
        .saved_history
        .key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn playback_send(runtime: &mut Interactive, app: &mut Demo) -> io::Result<()> {
    let intent = app
        .live
        .as_mut()
        .unwrap()
        .saved_history
        .take_intent()
        .ok_or_else(|| io::Error::other("Missing playback/history intent"))?;
    runtime.history(intent)?;
    wait(runtime, app, |e| matches!(e, Observation::SavedHistory(_)))
}

#[test]
#[ignore = "real registered source with controlled native JSON-RPC and SDK"]
fn native_provider_midturn_steering_and_interrupt_use_exact_owned_delivery() {
    for case in [
        "claude-queue-interrupt",
        "codex-midturn-queue",
        "codex-midturn-rejected",
        "codex-midturn-uncertain",
        "codex-midturn-interrupt",
        "codex-midturn-emission",
        "codex-midturn-stale",
    ] {
        let (runtime, root) = launch(case);
        let claude = case.starts_with("claude-");
        let interrupt = case.ends_with("interrupt");
        let draft = "  Reviewed native follow-up 雪  ";
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live(if claude {"claude"} else {"codex"}.into(), if claude {"claude-fixture"} else {"gpt-6.1-sol"}.into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            let chat = app.live.as_mut().unwrap();
            chat.submit("Original controlled native input")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime, &mut app, &root, "stream-active")?;
            app.insert_text(draft);
            app.submit_chord(interrupt);
            dispatch_midturn(runtime, &mut app)?;
            let screen = draw(&mut app);
            assert!(screen.contains(if interrupt {"Interrupt the current reply"} else {"current Codex turn"}), "{case}: {screen}");
            assert_eq!(app.composer.text(), draft);
            if case.ends_with("stale") {
                fs::write(root.join("release-stream"), b"finish before apply")?;
                while app.live.as_ref().unwrap().busy { app.observe_live(next(runtime)?)?; }
            }
            app.live.as_mut().unwrap().work.key(KeyEvent::new(if interrupt {KeyCode::Char('i')} else {KeyCode::Enter}, KeyModifiers::NONE));
            if case.ends_with("emission") {
                runtime.work(app.live.as_mut().unwrap().work.take_intent().unwrap())?;
                stage(runtime, &mut app, &root, "queue-emission-failed")?;
                assert_eq!(app.composer.text(), draft);
                return runtime.quit();
            }
            dispatch_midturn(runtime, &mut app)?;
            if case.ends_with("stale") {
                assert_eq!(app.composer.text(), draft);
                return runtime.quit();
            }
            assert_eq!(app.composer.text(), "");
            if !interrupt { stage(runtime, &mut app, &root, "native-steer")?; }
            if !interrupt { fs::write(root.join("release-stream"), b"settle reviewed original")?; }
            if case.ends_with("uncertain") {
                stage(runtime, &mut app, &root, "native-uncertain")?;
                return runtime.quit();
            }
            let mut settled = false;
            for _ in 0..180 {
                let event = next(runtime)?;
                settled |= matches!(&event, Observation::WorkExecution(update) if matches!(update.as_ref(), litetui_terminal::live::WorkExecution::Settled{..}));
                app.observe_live(event)?;
                if settled && !app.live.as_ref().unwrap().busy { return runtime.quit(); }
            }
            Err(io::Error::other("Native reviewed input did not settle"))
        });
        assert!(result.is_ok(), "{case}: {result:?} {}", root.display());
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        assert_eq!(r["denied"], json!([]), "{case}");
        assert_eq!(r["reader_retired"], true);
        assert!(
            r["workers"]
                .as_array()
                .unwrap()
                .iter()
                .all(|w| w["finished"] == true)
        );
        let inputs = r["work"]["inputs"].as_array().unwrap();
        if claude {
            assert_eq!(r["sdk_retired"], true);
            assert_eq!(r["claude_interrupts"], 1);
            assert_eq!(r["claude_queries"].as_array().unwrap().len(), 2);
            assert_eq!(inputs[0]["admission"]["mode"], "new_turn");
            assert_eq!(inputs[0]["state"], "settled");
            continue;
        }
        assert_eq!(r["native_retired"], true);
        let calls = r["codex_calls"].as_array().unwrap();
        let starts = calls.iter().filter(|c| c["method"] == "turn/start").count();
        let steers = calls.iter().filter(|c| c["method"] == "turn/steer").count();
        if case.ends_with("stale") {
            assert!(inputs.is_empty());
            assert_eq!(steers, 0);
            continue;
        }
        assert_eq!(inputs.len(), 1);
        if case.ends_with("emission") {
            assert_eq!(starts, 1);
            assert_eq!(steers, 0);
            assert_eq!(inputs[0]["state"], "held");
            assert_eq!(r["queue_recovered"][0]["state"], "held");
        } else if case.ends_with("uncertain") {
            assert_eq!(starts, 1);
            assert_eq!(steers, 1);
            assert_eq!(inputs[0]["state"], "uncertain");
            assert!(inputs[0]["admission"].is_null());
        } else {
            assert_eq!(inputs[0]["state"], "settled");
            assert_eq!(
                starts,
                if interrupt || case.ends_with("rejected") {
                    2
                } else {
                    1
                }
            );
            assert_eq!(steers, usize::from(!interrupt));
            assert_eq!(
                inputs[0]["admission"]["mode"],
                if interrupt || case.ends_with("rejected") {
                    "new_turn"
                } else {
                    "in_turn"
                }
            );
        }
    }
}

#[test]
#[ignore = "real registered source with controlled native JSON-RPC"]
fn codex_midturn_steers_or_defers_the_exact_reviewed_image_without_replay() {
    use base64::{Engine, engine::general_purpose::STANDARD};
    use sha2::{Digest, Sha256};
    for case in ["codex-midturn-image", "codex-midturn-image-rejected"] {
        let (runtime, root) = launch(case);
        let rejected = case.ends_with("rejected");
        let draft = "Codex queued picture 雪";
        let picture = root.join("codex picture.png");
        let digest = std::cell::RefCell::new(String::new());
        let result = runtime.with_cleanup(|runtime| {
            let mut app = Demo::live("codex".into(), "gpt-6.1-sol".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            let chat = app.live.as_mut().unwrap();
            chat.submit("Original controlled native input")?;
            runtime.submit(chat.take_submission().unwrap())?;
            stage(runtime, &mut app, &root, "stream-active")?;
            fs::write(&picture, STANDARD.decode(TINY_PNG).unwrap())?;
            attach_image(runtime, &mut app, &picture)?;
            let attached = app.live.as_ref().unwrap().work.image.attached.clone().unwrap();
            assert_eq!(attached.wire()["delivery"], "inline");
            *digest.borrow_mut() = attached.wire()["png_sha256"].as_str().unwrap().into();
            app.insert_text(draft);
            app.submit_chord(false);
            dispatch_midturn(runtime, &mut app)?;
            let screen = draw(&mut app);
            assert!(
                screen.contains("current Codex turn")
                    && screen.contains("Attached image: codex picture.png")
                    && screen.contains("exact PNG"),
                "{screen}"
            );
            app.live
                .as_mut()
                .unwrap()
                .work
                .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
            assert_eq!(app.composer.text(), draft);
            assert_eq!(app.live.as_ref().unwrap().work.image.attached, Some(attached));
            dispatch_midturn(runtime, &mut app)?;
            assert_eq!(app.composer.text(), "");
            assert!(app.live.as_ref().unwrap().work.image.attached.is_none());
            stage(runtime, &mut app, &root, "native-steer")?;
            fs::write(root.join("release-stream"), b"settle reviewed original")?;
            let mut settled = false;
            for _ in 0..180 {
                let event = next(runtime)?;
                settled |= matches!(&event, Observation::WorkExecution(update) if matches!(update.as_ref(), litetui_terminal::live::WorkExecution::Settled { .. }));
                app.observe_live(event)?;
                if settled && !app.live.as_ref().unwrap().busy {
                    return runtime.quit();
                }
            }
            Err(io::Error::other("Codex queued image did not settle"))
        });
        assert!(result.is_ok(), "{case}: {result:?} {}", root.display());
        let digest = digest.into_inner();
        let r: Value =
            serde_json::from_slice(&fs::read(root.join("runtime-controls-report.json")).unwrap())
                .unwrap();
        assert_eq!(r["denied"], json!([]), "{case}");
        assert_eq!(r["native_retired"], true);
        let stored =
            fs::read(find(&root, &format!("queued-{digest}.png")).expect("owned image")).unwrap();
        assert_eq!(format!("{:x}", Sha256::digest(&stored)), digest);
        let calls = r["codex_calls"].as_array().unwrap();
        let starts: Vec<&Value> = calls
            .iter()
            .filter(|c| c["method"] == "turn/start")
            .collect();
        let steers: Vec<&Value> = calls
            .iter()
            .filter(|c| c["method"] == "turn/steer")
            .collect();
        let carries = |input: &Value| {
            let blocks = input.as_array().unwrap();
            blocks
                .iter()
                .any(|b| b["type"] == "text" && b["text"] == draft)
                && blocks.iter().any(|b| {
                    b["type"] == "image"
                        && STANDARD
                            .decode(
                                b["url"]
                                    .as_str()
                                    .unwrap()
                                    .strip_prefix("data:image/png;base64,")
                                    .unwrap(),
                            )
                            .unwrap()
                            == stored
                })
        };
        assert_eq!(steers.len(), 1);
        assert!(carries(&steers[0]["params"]["input"]));
        let inputs = r["work"]["inputs"].as_array().unwrap();
        assert_eq!(inputs.len(), 1);
        assert_eq!(inputs[0]["payload"]["content"], draft);
        assert_eq!(inputs[0]["payload"]["image"]["png_sha256"], digest);
        assert_eq!(inputs[0]["state"], "settled");
        if rejected {
            // A known pre-injection rejection keeps the input for the next turn, image included.
            assert_eq!(starts.len(), 2);
            assert_eq!(inputs[0]["admission"]["mode"], "new_turn");
            assert!(starts.iter().any(|s| carries(&s["params"]["input"])));
        } else {
            assert_eq!(starts.len(), 1);
            assert_eq!(inputs[0]["admission"]["mode"], "in_turn");
        }
        let history = r["native_history"].as_array().unwrap();
        let rows: Vec<&Value> = history
            .iter()
            .filter(|m| m["role"] == "user" && m["content"].is_array())
            .collect();
        assert_eq!(rows.len(), 1, "{history:?}");
        assert_eq!(rows[0]["content"][1]["text"], draft);
    }
}

#[test]
#[ignore = "actual per-tool mid-turn policy publication; controlled dependency Python required"]
fn individual_tool_waits_for_publication_without_changing_global_authority() {
    let (runtime, root) = launch("host-tool-selection-parked");
    let mut frames = Vec::new();
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.live
                .as_mut()
                .unwrap()
                .submit("EXPLICIT CONTROLLED TWO TOOL TURN")?;
            runtime.submit(app.live.as_mut().unwrap().take_submission().unwrap())?;
            app.insert_text("PRESERVE DRAFT DURING INDIVIDUAL TOOL REVIEW");
            app.composer.move_cursor(Motion::WordLeft, true);
            let draft = (
                app.composer.text().to_string(),
                app.composer.before_cursor().to_string(),
                app.composer.selected_text().to_string(),
            );
            stage(runtime, &mut app, &root, "old-tool-running")?;
            app.open_runtime_controls(view::Field::ToolSelection);
            send(runtime, &mut app)?;
            let index = loop {
                let event = next(runtime)?;
                let index = match &event {
                    Observation::RuntimeControls(view::Update::Snapshot { snapshot, .. }) => Some(
                        snapshot
                            .tools
                            .as_ref()
                            .unwrap()
                            .rows
                            .iter()
                            .position(|row| row.name == "runtime_fixture_tool")
                            .unwrap(),
                    ),
                    Observation::RuntimeControls(view::Update::Rejected(reason)) => {
                        return Err(io::Error::other(reason.clone()));
                    }
                    _ => None,
                };
                app.observe_live(event)?;
                send(runtime, &mut app)?;
                if let Some(index) = index {
                    break index;
                }
            };
            key(&mut app, KeyCode::Enter);
            assert!(
                app.live
                    .as_mut()
                    .unwrap()
                    .runtime_controls
                    .take_intent()
                    .is_none()
            );
            for _ in 0..index {
                key(&mut app, KeyCode::Down);
            }
            frames.push(draw(&mut app));
            key(&mut app, KeyCode::Enter);
            send(runtime, &mut app)?;
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::RuntimeControls(view::Update::Review(_)))
            })?;
            confirm(runtime, &mut app, &mut frames)?;
            stage(runtime, &mut app, &root, "readback-prepared")?;
            fs::write(
                root.join("release-tool"),
                b"finish original authorization before publication",
            )
            .unwrap();
            stage(runtime, &mut app, &root, "next-tool-attempt")?;
            assert!(app.live.as_ref().unwrap().runtime_controls.pending());
            fs::write(
                root.join("release-publication"),
                b"publish exact disabled list",
            )
            .unwrap();
            wait(runtime, &mut app, |e| {
                matches!(e, Observation::RuntimeControls(view::Update::Settled(_)))
            })?;
            while app.live.as_ref().unwrap().busy {
                let event = next(runtime)?;
                app.observe_live(event)?;
                send(runtime, &mut app)?;
            }
            assert_eq!(
                draft,
                (
                    app.composer.text().to_string(),
                    app.composer.before_cursor().to_string(),
                    app.composer.selected_text().to_string()
                )
            );
            frames.push(draw(&mut app));
            Ok(())
        })
        .unwrap_or_else(|e| panic!("{e}; {}", root.display()));
    let r = collect(&root);
    let review = &r["reviews"][0];
    assert_eq!(
        review["change"],
        json!({"field":"tool_selection","value":{"name":"runtime_fixture_tool","enabled":false}})
    );
    assert_eq!(review["after"]["tools_enabled"], true);
    assert_eq!(review["after"]["effective_profile"], "autonomous");
    assert_eq!(
        review["after"]["tools_disabled"],
        json!(["runtime_fixture_tool"])
    );
    assert_eq!(r["readbacks"][0]["verified"], true);
    let events = r["events"].as_array().unwrap();
    let boundary = events
        .iter()
        .find(|e| {
            e["kind"] == "wire" && e["frame"]["result"]["kind"] == "runtime_controls_readback"
        })
        .unwrap()["sequence"]
        .as_u64()
        .unwrap();
    for (kind, ordinal) in [("tool-exit", 0), ("tool-attempt", 1)] {
        let sequence = events
            .iter()
            .find(|e| {
                e["kind"] == kind && (e["ordinal"] == ordinal || e["args"]["ordinal"] == ordinal)
            })
            .unwrap()["sequence"]
            .as_u64()
            .unwrap();
        assert!(sequence < boundary);
    }
    assert_eq!(
        events
            .iter()
            .filter(|e| e["kind"] == "tool-enter" && e["ordinal"] == 0)
            .count(),
        1
    );
    assert!(
        !events
            .iter()
            .any(|e| e["kind"] == "tool-enter" && e["ordinal"] == 1)
    );
    assert!(
        !events
            .iter()
            .any(|e| e["kind"] == "authorization-start" && e["args"]["ordinal"] == 1)
    );
    assert_eq!(
        r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|c| c["type"] == "prompt")
            .count(),
        1
    );
    native_result(
        &root,
        "host-tool-selection-parked",
        json!({"composer_preserved":true,"global_authority_preserved":true,"published_boundary":boundary}),
        &frames,
    );
}

#[test]
#[ignore = "registered source /tools command through compiled Interactive; controlled Python required"]
fn registered_tools_command_opens_inert_list_and_preserves_composer() {
    let (runtime, root) = launch("tool-selection-command");
    runtime
        .with_cleanup(|runtime| {
            let mut app = Demo::live("custom".into(), "switch-old".into());
            wait(runtime, &mut app, |e| matches!(e, Observation::Ready))?;
            app.insert_text("/tools");
            app.submit();
            runtime.commands(
                app.live
                    .as_mut()
                    .unwrap()
                    .commands
                    .take_intent()
                    .expect("registered command route"),
            )?;
            wait(runtime, &mut app, |e| {
                matches!(
                    e,
                    Observation::RuntimeControls(view::Update::Snapshot {
                        field: view::Field::ToolSelection,
                        ..
                    })
                )
            })?;
            assert!(draw(&mut app).contains("Individual tools"));
            assert_eq!(app.composer.text(), "/tools");
            assert!(app.live.as_mut().unwrap().take_submission().is_none());
            key(&mut app, KeyCode::Esc);
            Ok(())
        })
        .unwrap_or_else(|e| panic!("{e}; {}", root.display()));
    let r = collect(&root);
    assert_eq!(r["tool_lists"].as_array().unwrap().len(), 1);
    assert!(r["reviews"].as_array().unwrap().is_empty());
    assert!(r["results"].as_array().unwrap().is_empty());
    assert!(r["requests"].as_array().unwrap().is_empty());
    assert!(
        r["commands"]
            .as_array()
            .unwrap()
            .iter()
            .all(|c| c["type"] != "prompt")
    );
}
