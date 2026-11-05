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
    let fixture = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("tests/fixtures/runtime_controls_runtime.py");
    fs::write(package.join("cli.py"),format!(r#"import json,os,runpy
from pathlib import Path
os.environ['RUST_RUNTIME_CONTROLS_SOURCE']={}
os.environ['RUST_RUNTIME_CONTROLS_CASE']={}
os.environ['PYTHONDONTWRITEBYTECODE']='1'
p=Path(os.environ['LITETUI_DATA_ROOT'])/'settings.json'
s=json.loads(p.read_text(encoding='utf-8'))
s.update(enter_interrupts=os.environ['RUST_RUNTIME_CONTROLS_CASE'] in ('midturn-swapped','midturn-interrupt-swapped'),user_name_asked=True,skills_enabled=False,footer_task_manager=False,tts_enabled=False,sidecar_enabled=False,autocompact_enabled=False,tool_auto_background_s=0,custom_context_length=1000000)
p.write_text(json.dumps(s),encoding='utf-8')
runpy.run_path({},run_name='__main__')
"#,quote(&source.to_string_lossy()),quote(case),quote(&fixture.to_string_lossy()))).unwrap();
    let root = base.join("data");
    let workspace = base.join("workspace");
    fs::create_dir(&workspace).unwrap();
    let args = vec![
        "--python".into(),
        std::env::var("LITETUI_AUTHORITY_PYTHON").expect("controlled dependency Python"),
        "--workspace".into(),
        workspace.to_string_lossy().into_owned(),
        "--source".into(),
        base.join("source").to_string_lossy().into_owned(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        "custom".into(),
        "--model".into(),
        "switch-old".into(),
        "--tool-profile".into(),
        if case.starts_with("host-") {
            "autonomous".into()
        } else {
            "strict".into()
        },
        "--tools".into(),
        if case.starts_with("host-") {
            "on".into()
        } else {
            "off".into()
        },
        "--thinking-level".into(),
        "default".into(),
        "--timeout".into(),
        "120".into(),
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
        "host-stop-captured",
        "host-quit-captured",
    ] {
        let (runtime, root) = launch(case);
        let mut frames = Vec::new();
        let has_tool = case.starts_with("host-");
        let parked = case.ends_with("-parked");
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
                    "host-quit-parked" | "host-quit-captured" => {
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
