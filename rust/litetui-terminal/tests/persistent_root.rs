//! Ryan 2026-11-12 (via Sentinel 07ceb755): "no, copy all settings keep tehm seprate marked as rust".
//! One persistent Rust data root; its first run copies the Python settings; after that the two
//! never share or write each other's files; the Python data is never modified.
use litetui_terminal::{
    live::Options, live::prepare_root, runtime_launch::Backend, tool_mode::Profile,
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, fs, path::Path};

fn scratch(name: &str) -> std::path::PathBuf {
    let dir = std::env::temp_dir().join(format!(
        "litetui-persistent-root-{name}-{}",
        std::process::id()
    ));
    let _ = fs::remove_dir_all(&dir);
    fs::create_dir_all(&dir).unwrap();
    dir
}
fn options(root: &Path, seed: Option<&Path>) -> Options {
    let mut args: Vec<String> = [
        "--python",
        "C:/fixture/python.exe",
        "--source",
        "C:/fixture/source",
        "--backend",
        "claude",
        "--model",
        "claude-sonnet-5-5",
        "--tool-profile",
        "strict",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    args.extend(["--data-root".into(), root.display().to_string()]);
    if let Some(seed) = seed {
        args.extend(["--seed-from".into(), seed.display().to_string()]);
    }
    Options::parse(&args).unwrap()
}
#[test]
fn agent_option_names_the_seat_and_refuses_an_invalid_folder_name() {
    let root = Path::new("C:/fixture/root");
    let mut args: Vec<String> = [
        "--python",
        "C:/fixture/python.exe",
        "--source",
        "C:/fixture/source",
        "--backend",
        "claude",
        "--model",
        "claude-sonnet-5-5",
        "--tool-profile",
        "strict",
        "--data-root",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();
    args.push(root.display().to_string());
    assert_eq!(Options::parse(&args).unwrap().agent, None);
    for (name, ok) in [
        ("Nova", true),
        ("Bad/Name", false),
        ("trailing.", false),
        ("CON", false),
        (" lead", false),
    ] {
        let mut with = args.clone();
        with.extend(["--agent".into(), name.into()]);
        match Options::parse(&with) {
            Ok(options) => assert!(ok && options.agent.as_deref() == Some(name), "{name}"),
            Err(_) => assert!(!ok, "{name}"),
        }
    }
}
fn snapshot(root: &Path) -> BTreeMap<String, Vec<u8>> {
    let mut out = BTreeMap::new();
    let mut stack = vec![root.to_path_buf()];
    while let Some(dir) = stack.pop() {
        for entry in fs::read_dir(dir).unwrap() {
            let path = entry.unwrap().path();
            if path.is_dir() {
                stack.push(path);
            } else {
                out.insert(path.display().to_string(), fs::read(&path).unwrap());
            }
        }
    }
    out
}
fn settings(root: &Path) -> Value {
    serde_json::from_slice(&fs::read(root.join("settings.json")).unwrap()).unwrap()
}
fn python_root(base: &Path) -> std::path::PathBuf {
    let python = base.join("LiteTUI");
    fs::create_dir_all(python.join(".agents/LiteTUI/conversations")).unwrap();
    fs::write(
        python.join("settings.json"),
        serde_json::to_vec(&json!({
            "theme_name": "amber-ledger", "custom_themes": {"mine": {"primary": "#112233"}},
            "model_context_overrides": {"lmstudio:qwen": 131072}, "default_context_length": 65536,
            "thinking_by_backend": {"codex": "high"}, "backend": "lmstudio", "seat_name": "LiteTUI",
            "tools_enabled": true, "mcp_enabled": true, "tool_policy_profile": "autonomous",
        }))
        .unwrap(),
    )
    .unwrap();
    fs::write(python.join("mcp.json"), b"{\"mcpServers\": {}}").unwrap();
    fs::write(
        python.join(".agents/LiteTUI/conversations/old.jsonl"),
        b"{\"role\":\"user\"}\n",
    )
    .unwrap();
    fs::write(python.join("jobs.json"), b"[]").unwrap();
    python
}

#[test]
fn first_run_copies_the_python_settings_and_marks_the_root_as_rust() {
    let base = scratch("first");
    let python = python_root(&base);
    let before = snapshot(&python);
    let rust = base.join("LiteTUI-rust-data");
    let root = prepare_root(
        &options(&rust, Some(&python)),
        Backend::Claude,
        Profile::Strict,
    )
    .unwrap();
    let saved = settings(&root);
    // His settings came across: themes, per-model context, per-backend thinking.
    assert_eq!(saved["theme_name"], "amber-ledger");
    assert_eq!(saved["custom_themes"]["mine"]["primary"], "#112233");
    assert_eq!(saved["model_context_overrides"]["lmstudio:qwen"], 131072);
    assert_eq!(saved["default_context_length"], 65536);
    assert_eq!(saved["thinking_by_backend"]["codex"], "high");
    // The launch's own keys, and marked as the Rust frontend's.
    assert_eq!(saved["backend"], "claude");
    assert_eq!(saved["seat_name"], "RustLive");
    assert_eq!(saved["tools_enabled"], false);
    assert_eq!(saved["mcp_enabled"], false);
    assert!(root.join("rust-frontend.json").is_file());
    assert!(root.join("mcp.json").is_file());
    // Conversations, agents and jobs are not settings: not copied.
    assert!(!root.join(".agents").exists() && !root.join("jobs.json").exists());
    // The Python data is untouched.
    assert_eq!(snapshot(&python), before);
    let _ = fs::remove_dir_all(&base);
}

#[test]
fn later_runs_reuse_the_rust_root_without_copying_again() {
    let base = scratch("reuse");
    let python = python_root(&base);
    let rust = base.join("LiteTUI-rust-data");
    let root = prepare_root(
        &options(&rust, Some(&python)),
        Backend::Claude,
        Profile::Strict,
    )
    .unwrap();
    // The Rust frontend changes its own settings; Python changes its own.
    let mut mine = settings(&root);
    mine["theme_name"] = json!("rust-choice");
    mine["thinking_level"] = json!("high");
    fs::write(
        root.join("settings.json"),
        serde_json::to_vec(&mine).unwrap(),
    )
    .unwrap();
    let mut theirs = settings(&python);
    theirs["theme_name"] = json!("python-choice");
    fs::write(
        python.join("settings.json"),
        serde_json::to_vec(&theirs).unwrap(),
    )
    .unwrap();
    let before = snapshot(&python);
    let again = prepare_root(
        &options(&rust, Some(&python)),
        Backend::Claude,
        Profile::Strict,
    )
    .unwrap();
    assert_eq!(again, root);
    let saved = settings(&root);
    assert_eq!(
        saved["theme_name"], "rust-choice",
        "never re-copied over the Rust settings"
    );
    assert_eq!(
        saved["thinking_level"], "high",
        "a reused root keeps its thinking level"
    );
    assert_eq!(saved["seat_name"], "RustLive");
    assert_eq!(
        snapshot(&python),
        before,
        "Python's files are never written"
    );
    let _ = fs::remove_dir_all(&base);
}

#[test]
fn a_root_without_the_rust_marker_or_overlapping_the_python_root_is_refused() {
    let base = scratch("refuse");
    let python = python_root(&base);
    let before = snapshot(&python);
    // The Python root itself (no Rust marker) is never adopted.
    assert!(prepare_root(&options(&python, None), Backend::Claude, Profile::Strict).is_err());
    // A Rust root inside the Python root, or the Python root as its own seed, is refused.
    let inside = python.join("rust");
    assert!(
        prepare_root(
            &options(&inside, Some(&python)),
            Backend::Claude,
            Profile::Strict
        )
        .is_err()
    );
    assert_eq!(
        snapshot(&python)
            .keys()
            .filter(|k| !k.contains("rust"))
            .count(),
        before.len()
    );
    for (path, bytes) in &before {
        assert_eq!(&fs::read(path).unwrap(), bytes);
    }
    let _ = fs::remove_dir_all(&base);
}
