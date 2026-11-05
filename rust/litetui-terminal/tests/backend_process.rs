#![cfg(windows)]
//! Native binary -> actual Python CLI/owned session/generic stream contracts.
//! Provider, network, GPU and server methods are tripwired or explicit controls;
//! these tests do not establish live backend or hardware availability.
use serde_json::{Value, json};
use std::{
    fs,
    path::PathBuf,
    process::{Command, Output},
    sync::atomic::{AtomicUsize, Ordering},
};
static NEXT: AtomicUsize = AtomicUsize::new(0);
const GENERIC: &[&str] = &[
    "custom", "lmstudio", "llamacpp", "ninfer", "strata", "cline", "free",
];
fn launch(backend: &str, case: &str, gpu: &str, flags: &[&str]) -> (Output, PathBuf) {
    let python = std::env::var_os("LITETUI_AUTHORITY_PYTHON").expect("dependency Python required");
    let crate_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let actual = std::env::var_os("LITETUI_TEST_RUNTIME_SOURCE")
        .map(PathBuf::from)
        .unwrap_or_else(|| crate_root.join("../.."))
        .canonicalize()
        .unwrap();
    let base = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("target/backend-process-tests")
        .join(format!(
            "{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
    let overlay = base.join("source");
    let package = overlay.join("src/litetui");
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
    // The wrapper sets only test-owned controls; the Rust launcher chooses every
    // actual CLI flag and creates/settings-marks its own fresh data root.
    fs::write(package.join("cli.py"),format!(
        "import os, runpy\nos.environ['RUST_BACKEND_SOURCE'] = {}\nos.environ['RUST_BACKEND_CASE'] = {}\nos.environ['RUST_BACKEND_GPU'] = {}\nos.environ['RUST_BACKEND_DRIVER'] = 'rpc'\nos.environ['FIXTURE_BACKEND_KEY'] = 'synthetic-only-token'\nrunpy.run_path({}, run_name='__main__')\n",
        quote(&actual.to_string_lossy()),quote(case),quote(gpu),quote(&crate_root.join("tests/fixtures/backend_runtime.py").to_string_lossy()))).unwrap();
    let root = base.join("data");
    let mut command = Command::new(env!("CARGO_BIN_EXE_litetui-rs"));
    command
        .args(["--live", "--python"])
        .arg(python)
        .arg("--source")
        .arg(&overlay)
        .arg("--data-root")
        .arg(&root)
        .args([
            "--backend",
            backend,
            "--model",
            "fixture-model",
            "--prompt",
            "Controlled prompt",
            "--tool-profile",
            "strict",
            "--timeout",
            "35",
            "--server-timeout",
            "5",
        ]);
    // Explicit fixture endpoint; the source never performs a real request.
    if GENERIC[..5].contains(&backend)
        && !(backend == "ninfer" && flags.contains(&"--start-server"))
    {
        command.args(["--base-url", "http://127.0.0.1:45123/v1"]);
    }
    command.args(flags);
    let output = command.output().unwrap();
    fs::write(base.join("native-stdout.txt"), &output.stdout).unwrap();
    fs::write(base.join("native-stderr.txt"), &output.stderr).unwrap();
    (output, root)
}
fn output_text(output: &Output) -> String {
    format!(
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    )
}
fn report(root: &std::path::Path) -> Value {
    serde_json::from_slice(&fs::read(root.join("backend-report.json")).unwrap()).unwrap()
}
fn commands(root: &std::path::Path) -> Vec<Value> {
    fs::read_to_string(root.join("backend-commands.jsonl"))
        .unwrap()
        .lines()
        .map(|line| serde_json::from_str(line).unwrap())
        .collect()
}
fn assert_no_start_or_load(report: &Value) {
    assert!(report["denied"].as_array().unwrap().is_empty());
    assert!(
        !report["calls"]
            .as_array()
            .unwrap()
            .iter()
            .any(|call| matches!(
                call["effect"].as_str(),
                Some("load" | "engine-start" | "server-start")
            ))
    );
}
#[test]
#[ignore = "controlled actual Python CLI/RPC/generic stream; dependency Python required"]
fn canonical_generic_backends_complete_through_native_owned_rpc_without_implicit_starts() {
    for backend in GENERIC {
        let (output, root) = launch(backend, "success", "5090", &[]);
        assert!(
            output.status.success(),
            "{backend}: {}",
            output_text(&output)
        );
        let text = output_text(&output);
        assert!(
            text.contains("Controlled backend answer"),
            "{backend}: {text}"
        );
        assert!(text.contains("Controlled reasoning"), "{backend}: {text}");
        let report = report(&root);
        assert_no_start_or_load(&report);
        assert_eq!(
            report["authority"],
            json!({"backend":backend,"model":"fixture-model"})
        );
        assert_eq!(report["backend"], *backend);
        assert_eq!(
            report["launch"]["server_mode"],
            if GENERIC[..5].contains(backend) {
                "connect"
            } else {
                "auto"
            }
        );
        assert_eq!(
            report["requests"],
            json!([{"backend":backend,"model":"fixture-model","max_tokens":4096}])
        );
        let sent = commands(&root);
        assert_eq!(sent.iter().filter(|cmd| cmd["type"] == "prompt").count(), 1);
        assert!(
            sent.iter()
                .any(|cmd| cmd["type"] == "gui.state" && cmd["include_conversations"] == false)
        );
        assert_eq!(sent.last().unwrap()["type"], "shutdown");
        assert!(sent.iter().all(|cmd| cmd["type"] != "gui.backend.set"));
    }
}
#[test]
#[ignore = "controlled unavailable endpoints and models, no providers; dependency Python required"]
fn blocked_generic_readiness_never_submits_changes_backend_or_retries() {
    for (backend, case) in GENERIC
        .iter()
        .map(|backend| (*backend, "unavailable"))
        .chain(GENERIC[..5].iter().map(|backend| (*backend, "dead")))
    {
        let (output, root) = launch(backend, case, "5090", &[]);
        assert!(
            !output.status.success(),
            "{backend} {case}: {}",
            output_text(&output)
        );
        let report = report(&root);
        assert_no_start_or_load(&report);
        assert_eq!(report["backend"], backend);
        assert_eq!(
            report["authority"],
            json!({"backend":backend,"model":"fixture-model"})
        );
        assert!(report["requests"].as_array().unwrap().is_empty());
        assert!(!commands(&root).iter().any(|cmd| cmd["type"] == "prompt"));
    }
}
#[test]
#[ignore = "controlled source GPU observation, no hardware probing; dependency Python required"]
fn ineligible_ninfer_gate_fails_before_agent_or_submission_without_fallback() {
    for gpu in ["5080", "absent", "error"] {
        let (output, root) = launch("ninfer", "success", gpu, &[]);
        assert!(!output.status.success());
        assert!(output_text(&output).contains("NInfer requires an RTX 5090"));
        assert!(!root.join(".agents").exists());
        assert!(!root.join("backend-commands.jsonl").exists());
    }
}
#[test]
#[ignore = "explicit controlled server/load boundaries; dependency Python required"]
fn explicit_start_load_limits_and_named_credentials_reach_only_selected_source_backend() {
    for (backend, flags, effect) in [
        (
            "custom",
            vec![
                "--start-server",
                "--server-command",
                r#"["fixture-server","--model","{model}","--port","{port}"]"#,
            ],
            "server-start",
        ),
        ("lmstudio", vec!["--start-server"], "server-start"),
        (
            "ninfer",
            vec![
                "--start-server",
                "--model-path",
                "synthetic-model-artifact",
                "--context-length",
                "8192",
            ],
            "engine-start",
        ),
        (
            "strata",
            vec![
                "--start-server",
                "--model-path",
                "synthetic-model-artifact",
                "--context-length",
                "8192",
            ],
            "engine-start",
        ),
        (
            "llamacpp",
            vec!["--start-server", "--load-model", "--context-length", "8192"],
            "load",
        ),
        // Existing preset: loading without changing context may stay connected.
        ("llamacpp", vec!["--load-model"], "load"),
        (
            "lmstudio",
            vec!["--load-model", "--context-length", "8192"],
            "load",
        ),
    ] {
        let (output, root) = launch(backend, "success", "5090", &flags);
        assert!(
            output.status.success(),
            "{backend}: {}",
            output_text(&output)
        );
        let report = report(&root);
        assert!(report["denied"].as_array().unwrap().is_empty());
        assert_eq!(
            report["calls"]
                .as_array()
                .unwrap()
                .iter()
                .filter(|call| call["effect"] == effect)
                .count(),
            1
        );
        assert_eq!(report["requests"][0]["backend"], backend);
        if backend == "llamacpp" {
            let changing_context = flags.contains(&"--context-length");
            assert_eq!(
                report["launch"]["server_mode"],
                if changing_context { "start" } else { "connect" }
            );
            let load = report["calls"]
                .as_array()
                .unwrap()
                .iter()
                .find(|call| call["effect"] == "load")
                .unwrap();
            assert_eq!(
                load["context"],
                if changing_context {
                    json!(8192)
                } else {
                    Value::Null
                }
            );
            if !changing_context {
                assert!(
                    !report["calls"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .any(|call| matches!(
                            call["effect"].as_str(),
                            Some("server-start" | "engine-start")
                        ))
                );
            }
        }
    }
    let (output, root) = launch(
        "custom",
        "success",
        "5090",
        &["--max-tokens", "17", "--api-key-env", "FIXTURE_BACKEND_KEY"],
    );
    assert!(output.status.success(), "{}", output_text(&output));
    let report = report(&root);
    assert_eq!(report["requests"][0]["max_tokens"], 17);
    assert_eq!(report["launch"]["api_key_env"], "FIXTURE_BACKEND_KEY");
    assert!(!output_text(&output).contains("synthetic-only-token"));
}
#[test]
#[ignore = "native invalid launch combinations before source startup; dependency Python required"]
fn invalid_local_launch_intent_never_creates_the_owned_data_root() {
    for (backend, flags) in [
        ("custom", vec!["--server-mode", "auto"]),
        ("custom", vec!["--start-server"]),
        ("lmstudio", vec!["--context-length", "8192"]),
        ("llamacpp", vec!["--load-model", "--context-length", "8192"]),
        ("ninfer", vec!["--codex-engine", "native"]),
        ("cline", vec!["--load-model"]),
    ] {
        let (output, root) = launch(backend, "success", "5090", &flags);
        assert!(!output.status.success());
        assert!(
            !root.exists(),
            "{backend}: runtime wrote data for invalid launch"
        );
    }
}
