#[path = "../src/runtime_launch.rs"]
mod runtime_launch;
use runtime_launch::{Backend, LaunchOptions, ServerMode};

fn parse(args: &[&str]) -> LaunchOptions {
    let strings = args
        .iter()
        .map(|value| (*value).to_owned())
        .collect::<Vec<_>>();
    let (options, remaining) = LaunchOptions::extract(&strings).unwrap();
    assert!(remaining.is_empty());
    options
}
#[test]
fn backend_names_match_the_canonical_source_catalog_in_order() {
    let source = include_str!("../../../src/litetui/llm_backend.py");
    let catalog = source
        .split("BACKENDS: tuple[tuple[str, str], ...] = (")
        .nth(1)
        .unwrap();
    let names: Vec<_> = catalog
        .lines()
        .take_while(|line| line.trim() != ")")
        .filter_map(|line| line.trim().strip_prefix("(\""))
        .map(|line| line.split('"').next().unwrap())
        .collect();
    assert_eq!(names, Backend::ALL.map(Backend::as_str));
    for backend in Backend::ALL {
        assert_eq!(Backend::parse(backend.as_str()).unwrap(), backend);
    }
    assert!(Backend::parse("another").is_err());
}
#[test]
fn implicit_local_mode_is_connect_and_never_loads_or_starts() {
    for backend in Backend::ALL {
        let options = LaunchOptions::default();
        let args = options.python_args(backend, "selected").unwrap();
        assert_eq!(
            args,
            [
                "--server-mode",
                if backend.local() { "connect" } else { "auto" },
                "--server-timeout",
                "600"
            ]
        );
    }
}
#[test]
fn explicit_auto_is_not_an_authorization_to_spawn_local_servers() {
    let options = parse(&["--server-mode", "auto"]);
    for backend in Backend::ALL {
        assert_eq!(
            options.validate(backend, "selected").is_err(),
            backend.local()
        );
    }
}
#[test]
fn custom_command_and_named_credential_remain_separate_arguments() {
    let options = parse(&[
        "--base-url",
        "http://localhost:8888/v1",
        "--start-server",
        "--server-command",
        r#"["C:\\Ryan's models\\server.exe","--model","{model}","spaces and $()"]"#,
        "--model-path",
        "C:\\models\\file with spaces",
        "--context-length",
        "8192",
        "--max-tokens",
        "128",
        "--api-key-env",
        "FIXTURE_KEY",
        "--server-timeout",
        "17",
    ]);
    let args = options.python_args(Backend::Custom, "selected").unwrap();
    assert_eq!(options.effective_mode(Backend::Custom), ServerMode::Start);
    let value = |flag| &args[args.iter().position(|arg| arg == flag).unwrap() + 1];
    assert_eq!(value("--base-url"), "http://localhost:8888/v1");
    assert_eq!(value("--api-key-env"), "FIXTURE_KEY");
    let command: Vec<String> = serde_json::from_str(value("--server-command")).unwrap();
    assert_eq!(command[0], "C:\\Ryan's models\\server.exe");
    assert_eq!(command[3], "spaces and $()");
}
#[test]
fn extraction_preserves_existing_pairs_and_flag_shaped_prompt_values() {
    let args: Vec<_> = [
        "--prompt",
        "--load-model",
        "--backend",
        "lmstudio",
        "--load-model",
        "--context-length",
        "8192",
        "--model",
        "chosen",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    let (options, rest) = LaunchOptions::extract(&args).unwrap();
    assert!(options.load_model);
    assert_eq!(
        rest,
        [
            "--prompt",
            "--load-model",
            "--backend",
            "lmstudio",
            "--model",
            "chosen"
        ]
    );
    assert!(
        options
            .python_args(Backend::LmStudio, "chosen")
            .unwrap()
            .contains(&"--load-model".into())
    );
}
#[test]
fn aliases_share_duplicate_detection_and_malformed_values_fail() {
    for args in [
        vec!["--base-url", "x", "--host", "y"],
        vec!["--start-server", "--server-mode", "start"],
        vec!["--load-model", "--load-model"],
        vec!["--server-timeout", "0"],
        vec!["--max-tokens", "-1"],
        vec!["--context-length", "1.5"],
        vec!["--server-timeout", "18446744073709551616"],
        vec!["--server-command", "{}"],
        vec!["--server-command", "[1]"],
        vec!["--server-command", "null"],
        vec!["--base-url"],
        vec!["--base-url", " "],
    ] {
        let strings = args.into_iter().map(str::to_owned).collect::<Vec<_>>();
        assert!(LaunchOptions::extract(&strings).is_err(), "{strings:?}");
    }
}
#[test]
fn invalid_backend_combinations_refuse_before_child_or_filesystem_use() {
    for (backend, args) in [
        (Backend::Claude, vec!["--base-url", "http://localhost"]),
        (Backend::Codex, vec!["--max-tokens", "32"]),
        (Backend::Cline, vec!["--start-server"]),
        (Backend::Free, vec!["--base-url", "http://localhost"]),
        (Backend::Custom, vec!["--load-model"]),
        (Backend::Custom, vec!["--start-server"]),
        (Backend::Custom, vec!["--server-executable", "server"]),
        (Backend::Custom, vec!["--api-key-env", "raw-secret-value"]),
        (Backend::LlamaCpp, vec!["--context-length", "8192"]),
        (
            Backend::LlamaCpp,
            vec!["--load-model", "--context-length", "8192"],
        ),
        (
            Backend::LlamaCpp,
            vec![
                "--server-mode",
                "connect",
                "--load-model",
                "--context-length",
                "8192",
            ],
        ),
        (Backend::LmStudio, vec!["--model-path", "file"]),
        (Backend::Strata, vec!["--server-executable", "server"]),
        (Backend::Strata, vec!["--context-length", "8192"]),
        (Backend::Ninfer, vec!["--model-path", "artifact"]),
        (
            Backend::Ninfer,
            vec!["--start-server", "--base-url", "http://localhost"],
        ),
        (
            Backend::Custom,
            vec!["--server-mode", "start", "--server-command", "[]"],
        ),
        (
            Backend::Custom,
            vec!["--server-mode", "start", "--server-command", r#"[""]"#],
        ),
        (
            Backend::Custom,
            vec![
                "--server-mode",
                "start",
                "--server-command",
                r#"["x\u0000"]"#,
            ],
        ),
        (Backend::LlamaCpp, vec!["--api-key-env", "VARIABLE"]),
        (Backend::LmStudio, vec!["--codex-engine", "native"]),
        (Backend::Codex, vec!["--codex-engine", "invalid"]),
    ] {
        assert!(
            parse(&args).validate(backend, "selected").is_err(),
            "{backend:?} {args:?}"
        );
    }
}
#[test]
fn explicit_model_load_and_engine_start_are_preserved() {
    for (backend, args, mode) in [
        (
            Backend::LlamaCpp,
            vec!["--start-server", "--load-model", "--context-length", "8192"],
            ServerMode::Start,
        ),
        (
            Backend::LmStudio,
            vec!["--load-model", "--context-length", "8192"],
            ServerMode::Connect,
        ),
        (Backend::LlamaCpp, vec!["--load-model"], ServerMode::Connect),
    ] {
        let options = parse(&args);
        assert!(options.validate(backend, "selected").is_ok());
        assert_eq!(options.effective_mode(backend), mode);
    }
    for backend in [Backend::Ninfer, Backend::Strata] {
        assert!(
            parse(&[
                "--start-server",
                "--model-path",
                "explicit file",
                "--context-length",
                "8192"
            ])
            .validate(backend, "selected")
            .is_ok()
        );
    }
}
#[test]
fn codex_engine_is_only_forwarded_when_explicit() {
    let args = LaunchOptions::default()
        .python_args(Backend::Codex, "selected")
        .unwrap();
    assert!(!args.iter().any(|arg| arg == "--codex-engine"));
    for engine in ["native", "http"] {
        let options = parse(&["--codex-engine", engine]);
        let args = options.python_args(Backend::Codex, "selected").unwrap();
        assert_eq!(&args[args.len() - 2..], ["--codex-engine", engine]);
    }
}
#[test]
fn direct_struct_construction_cannot_bypass_bounds_or_command_checks() {
    let mut options = LaunchOptions {
        max_tokens: Some(0),
        ..LaunchOptions::default()
    };
    assert!(options.python_args(Backend::Custom, "selected").is_err());
    options.max_tokens = None;
    options.server_timeout = 0;
    assert!(options.python_args(Backend::Custom, "selected").is_err());
    assert!(
        LaunchOptions::default()
            .validate(Backend::Custom, " ")
            .is_err()
    );
    options.server_timeout = 600;
    options.server_mode = Some(ServerMode::Start);
    options.server_command = Some(vec!["server".to_owned(); 129]);
    assert!(options.python_args(Backend::Custom, "selected").is_err());
}

#[test]
fn thinking_defaults_seed_fresh_authority_without_fabricating_invocation_flags() {
    for backend in Backend::ALL {
        let options = LaunchOptions::default();
        assert!(options.thinking_args().is_empty());
        assert_eq!(
            options.initial_thinking(backend),
            if matches!(backend, Backend::Claude | Backend::Codex) {
                "low"
            } else {
                "default"
            }
        );
        let explicit = parse(&["--thinking-level", "model-specific-level"]);
        assert_eq!(
            explicit.thinking_args(),
            ["--thinking-level", "model-specific-level"]
        );
        assert_eq!(explicit.initial_thinking(backend), "model-specific-level");
    }
    let args = ["--thinking-level", "low", "--reasoning-effort", "high"].map(str::to_owned);
    assert!(LaunchOptions::extract(&args).is_err());
    let too_long = LaunchOptions {
        thinking_level: Some("a".repeat(65)),
        ..LaunchOptions::default()
    };
    assert!(too_long.validate(Backend::Custom, "selected").is_err());
}
