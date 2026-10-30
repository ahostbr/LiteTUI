use litetui_terminal::live::{Observation, Options, Turn, terminal_text};
use serde_json::json;

fn ready() -> serde_json::Value {
    json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"claude-sonnet-5-5"})
}
fn accept(turn: &mut Turn, value: serde_json::Value) -> std::io::Result<Observation> {
    turn.accept(&value, "claude", "claude-sonnet-5-5")
}
fn started_with_id() -> (Turn, String) {
    let mut turn = Turn::default();
    assert_eq!(accept(&mut turn, ready()).unwrap(), Observation::Ready);
    let prompt = turn.submit("Hello").unwrap();
    accept(
        &mut turn,
        json!({"type":"response","id":prompt["id"],"ok":true,"result":{"turn":"accepted"}}),
    )
    .unwrap();
    accept(
        &mut turn,
        json!({"type":"turn_start","model":"claude-sonnet-5-5"}),
    )
    .unwrap();
    (turn, prompt["id"].as_str().unwrap().to_owned())
}
fn started() -> Turn {
    started_with_id().0
}
#[test]
fn real_native_shapes_stream_and_complete_but_ack_does_not_complete() {
    let mut turn = started();
    assert_eq!(
        accept(&mut turn, json!({"type":"text_delta","text":"Hello"})).unwrap(),
        Observation::Text("Hello".into())
    );
    assert_eq!(
        accept(
            &mut turn,
            json!({"type":"native_text_snapshot","provider":"claude","message_id":null,"text":"Hi"})
        )
        .unwrap(),
        Observation::Snapshot("Hi".into())
    );
    assert_eq!(
        accept(
            &mut turn,
            json!({"type":"turn_end","stopReason":"stop","error":null})
        )
        .unwrap(),
        Observation::Complete
    );
    assert!(accept(&mut turn, json!({"type":"text_delta","text":"late"})).is_err());
}
#[test]
fn ready_refuses_fallback_blocked_duplicate_or_wrong_version() {
    for (key, value) in [
        ("model", json!("other")),
        ("backend", json!("lmstudio")),
        ("launch_status", json!("blocked")),
        ("protocol_version", json!(2)),
    ] {
        let mut event = ready();
        event[key] = value;
        assert!(accept(&mut Turn::default(), event).is_err());
    }
    let mut turn = Turn::default();
    accept(&mut turn, ready()).unwrap();
    assert!(accept(&mut turn, ready()).is_err());
}
#[test]
fn errors_interactions_unknowns_and_gui_envelopes_never_grant_authority() {
    for kind in [
        "error",
        "submit_refused",
        "tool_approval_requested",
        "user_input_requested",
        "model_load_requested",
        "native_activity",
        "gui.event",
        "new_event",
    ] {
        assert!(
            accept(&mut started(), json!({"type":kind})).is_err(),
            "{kind}"
        );
    }
}
#[test]
fn malformed_missing_answer_or_failed_terminal_is_not_success() {
    assert!(
        accept(
            &mut started(),
            json!({"type":"turn_end","stopReason":"stop"})
        )
        .is_err()
    );
    assert!(accept(&mut started(), json!({"type":"text_delta","text":12})).is_err());
    for event in [
        json!({"type":"turn_end","stopReason":"error"}),
        json!({"type":"turn_end","stopReason":"stop","error":"oops"}),
    ] {
        let mut turn = started();
        accept(&mut turn, json!({"type":"text_delta","text":"partial"})).unwrap();
        assert!(accept(&mut turn, event).is_err());
    }
}
#[test]
fn text_without_started_turn_and_unmatched_ack_fail() {
    assert!(
        accept(
            &mut Turn::default(),
            json!({"type":"text_delta","text":"no"})
        )
        .is_err()
    );
    let mut turn = Turn::default();
    accept(&mut turn, ready()).unwrap();
    assert!(
        accept(
            &mut turn,
            json!({"type":"response","id":"other","ok":true,"result":{"turn":"accepted"}})
        )
        .is_err()
    );
}
#[test]
fn terminal_controls_are_visible_not_executable() {
    assert_eq!(
        terminal_text("hi\x1b]52;steal\x07\r\n\t\u{202e}😀"),
        "hi\\u{1b}]52;steal\\u{7}\\u{d}\n\t\\u{202e}😀"
    );
}
#[test]
fn options_require_a_canonical_backend_and_bounded_deadline() {
    let args: Vec<String> = [
        "--python",
        "python",
        "--source",
        "source",
        "--data-root",
        "data",
        "--backend",
        "claude",
        "--model",
        "claude-sonnet-5-5",
        "--prompt",
        "Say hi",
    ]
    .into_iter()
    .map(String::from)
    .collect();
    assert!(Options::parse(&args).is_ok());
    for backend in litetui_terminal::runtime_launch::Backend::ALL {
        let mut args = args.clone();
        args[7] = backend.as_str().into();
        let options = Options::parse(&args).unwrap();
        assert_eq!(options.backend, backend.as_str());
        assert!(!options.launch.load_model);
    }
    let mut unknown = args.clone();
    unknown[7] = "invented-backend".into();
    assert!(Options::parse(&unknown).is_err());
    let mut duplicate = args.clone();
    duplicate.extend(["--prompt".into(), "again".into()]);
    assert!(Options::parse(&duplicate).is_err());
    let mut timeout = args;
    timeout.extend(["--timeout".into(), "0".into()]);
    assert!(Options::parse(&timeout).is_err());
}

#[test]
fn explicit_tool_launch_options_are_accepted() {
    let args = [
        "--python",
        "python",
        "--source",
        "source",
        "--data-root",
        "data",
        "--backend",
        "claude",
        "--model",
        "fixture",
        "--tools",
        "on",
        "--tool-profile",
        "strict",
        "--workspace",
        "C:/fixture",
    ]
    .into_iter()
    .map(String::from)
    .collect::<Vec<_>>();
    assert!(Options::parse(&args).is_ok());
}

#[test]
fn continuation_refusal_is_not_a_completed_turn_and_rejects_late_execution() {
    let mut turn = Turn::default();
    accept(&mut turn, ready()).unwrap();
    let id = turn
        .submit_continuation("saved", "session", "entry")
        .unwrap();
    let refused = json!({"type":"response","id":id,"ok":true,
        "result":{"turn":"refused","session_id":"session","entry_id":"entry","held":1,"reason":"below floor"}});
    assert_eq!(
        accept(&mut turn, refused.clone()).unwrap(),
        Observation::Refused("below floor".into())
    );
    assert!(
        accept(
            &mut turn,
            json!({"type":"turn_start","model":"claude-sonnet-5-5"})
        )
        .is_err()
    );
    assert!(accept(&mut turn, refused).is_err());
    assert!(turn.submit("next explicit").is_ok());
}
#[test]
fn continuation_receipt_validates_identity_and_does_not_hide_failed_execution() {
    for fault in ["session", "entry", "held", "reason", "negative", "started"] {
        let mut turn = Turn::default();
        accept(&mut turn, ready()).unwrap();
        let id = turn
            .submit_continuation("saved", "session", "entry")
            .unwrap();
        let mut frame = json!({"type":"response","id":id,"ok":true,
            "result":{"turn":"refused","session_id":"session","entry_id":"entry","held":1,"reason":"below floor"}});
        match fault {
            "session" => frame["result"]["session_id"] = json!("foreign"),
            "entry" => frame["result"]["entry_id"] = json!("foreign"),
            "held" => frame["result"]["held"] = json!(-1),
            "reason" => frame["result"]["reason"] = json!(""),
            "negative" => frame["ok"] = json!(false),
            "started" => {
                accept(
                    &mut turn,
                    json!({"type":"turn_start","model":"claude-sonnet-5-5"}),
                )
                .unwrap();
            }
            _ => unreachable!(),
        }
        assert!(accept(&mut turn, frame).is_err(), "{fault}");
        assert!(turn.submit("next").is_err());
    }
}
#[test]
fn continuation_refusal_waits_for_stop_receipt_without_faking_turn_end() {
    let mut turn = Turn::default();
    accept(&mut turn, ready()).unwrap();
    let id = turn
        .submit_continuation("saved", "session", "entry")
        .unwrap();
    let abort = turn.abort(&id).unwrap().unwrap();
    assert!(matches!(accept(&mut turn, json!({"type":"response","id":id,"ok":true,
        "result":{"turn":"refused","session_id":"session","entry_id":"entry","held":1,"reason":"stopped"}})).unwrap(),
        Observation::Information(_)));
    assert!(turn.submit("early").is_err());
    assert_eq!(
        accept(
            &mut turn,
            json!({"type":"response","id":abort["id"],"ok":true,
        "result":{"stopped":true,"cancelled_asks":[]}})
        )
        .unwrap(),
        Observation::Refused("stopped".into())
    );
}

#[test]
fn scheduled_claude_refusal_waits_for_admission_and_allows_only_explicit_followup() {
    for ack_first in [false, true] {
        for continuation in [false, true] {
            let mut turn = Turn::default();
            accept(&mut turn, ready()).unwrap();
            let id = if continuation {
                turn.submit_continuation("saved", "session", "entry")
                    .unwrap()
            } else {
                turn.submit("fresh").unwrap()["id"]
                    .as_str()
                    .unwrap()
                    .to_owned()
            };
            let ack = json!({"type":"response","id":id,"ok":true,
                "result":{"turn":"accepted","session_id":"session","entry_id":"entry","held":0}});
            if ack_first {
                accept(&mut turn, ack.clone()).unwrap();
            }
            assert!(matches!(
                accept(
                    &mut turn,
                    json!({"type":"cache_warning","provider":"claude",
                "reason":"Effort changes the cache","kind":"effort"})
                )
                .unwrap(),
                Observation::Information(_)
            ));
            assert!(turn.submit("automatic retry forbidden").is_err());
            let refused = json!({"type":"turn_refused","provider":"claude","reason":"Not sent"});
            let observation = accept(&mut turn, refused.clone()).unwrap();
            if ack_first {
                assert_eq!(observation, Observation::Refused("Not sent".into()));
            } else {
                assert!(matches!(observation, Observation::Information(_)));
                assert!(turn.submit("still waiting for ACK").is_err());
                assert_eq!(
                    accept(&mut turn, ack).unwrap(),
                    Observation::Refused("Not sent".into())
                );
            }
            for late in [
                refused,
                json!({"type":"turn_start","model":"claude-sonnet-5-5"}),
                json!({"type":"text_delta","text":"late"}),
                json!({"type":"turn_end","stopReason":"stop"}),
            ] {
                assert!(accept(&mut turn, late).is_err());
            }
            let next = turn.submit("explicit next prompt").unwrap();
            assert_ne!(next["id"], id);
        }
    }
}

#[test]
fn prestart_warning_and_refusal_require_valid_provider_reason_and_order() {
    for kind in ["cache_warning", "turn_refused"] {
        for fault in [
            "empty",
            "whitespace",
            "oversized",
            "missing",
            "provider",
            "before-submit",
            "after-start",
            "backend",
        ] {
            let mut turn = Turn::default();
            accept(&mut turn, ready()).unwrap();
            if fault != "before-submit" {
                turn.submit("hello").unwrap();
            }
            if fault == "after-start" {
                accept(
                    &mut turn,
                    json!({"type":"turn_start","model":"claude-sonnet-5-5"}),
                )
                .unwrap();
            }
            let mut event = json!({"type":kind,"provider":"claude","reason":"Not sent"});
            match fault {
                "empty" => event["reason"] = json!(""),
                "whitespace" => event["reason"] = json!(" \n"),
                "oversized" => event["reason"] = json!("x".repeat(4097)),
                "missing" => {
                    event.as_object_mut().unwrap().remove("reason");
                }
                "provider" => event["provider"] = json!("codex"),
                _ => {}
            }
            assert!(
                turn.accept(
                    &event,
                    if fault == "backend" {
                        "codex"
                    } else {
                        "claude"
                    },
                    "claude-sonnet-5-5"
                )
                .is_err(),
                "{kind}: {fault}"
            );
        }
    }
}

#[test]
fn scheduled_refusal_waits_for_existing_stop_receipt() {
    let mut turn = Turn::default();
    accept(&mut turn, ready()).unwrap();
    let id = turn.submit("hello").unwrap()["id"]
        .as_str()
        .unwrap()
        .to_owned();
    let abort = turn.abort(&id).unwrap().unwrap();
    accept(
        &mut turn,
        json!({"type":"response","id":id,"ok":true,"result":{"turn":"accepted"}}),
    )
    .unwrap();
    assert!(matches!(
        accept(
            &mut turn,
            json!({"type":"turn_refused","provider":"claude","reason":"Not sent"})
        )
        .unwrap(),
        Observation::Information(_)
    ));
    assert!(turn.submit("too early").is_err());
    assert_eq!(
        accept(
            &mut turn,
            json!({"type":"response","id":abort["id"],"ok":true,
        "result":{"stopped":false,"cancelled_asks":[]}})
        )
        .unwrap(),
        Observation::Refused("Not sent".into())
    );
}

#[test]
fn failed_native_turn_retains_partial_output_and_waits_for_stop_settlement() {
    for stopping in [false, true] {
        let (mut turn, id) = started_with_id();
        accept(&mut turn, json!({"type":"text_delta","text":"partial"})).unwrap();
        let abort = if stopping {
            turn.abort(&id).unwrap()
        } else {
            None
        };
        let failure =
            json!({"type":"turn_end","stopReason":"error","error":"Provider connection lost"});
        let observation = accept(&mut turn, failure.clone()).unwrap();
        if let Some(abort) = abort {
            assert!(matches!(observation, Observation::Information(_)));
            assert!(turn.submit("too early").is_err());
            assert_eq!(
                accept(
                    &mut turn,
                    json!({"type":"response","id":abort["id"],"ok":true,
                "result":{"stopped":true,"cancelled_asks":[]}})
                )
                .unwrap(),
                Observation::Failed("Provider connection lost".into())
            );
        } else {
            assert_eq!(
                observation,
                Observation::Failed("Provider connection lost".into())
            );
        }
        assert!(accept(&mut turn, failure).is_err());
        assert!(accept(&mut turn, json!({"type":"text_delta","text":"late"})).is_err());
        assert!(turn.submit("explicit next prompt").is_ok());
    }
    for reason in [
        json!(null),
        json!(""),
        json!(" \n"),
        json!(12),
        json!("x".repeat(16385)),
    ] {
        assert!(
            accept(
                &mut started(),
                json!({"type":"turn_end","stopReason":"error","error":reason})
            )
            .is_err()
        );
    }
}

#[test]
fn prompt_identities_are_fresh_across_reconstructed_runtime_instances() {
    let mut ids = std::collections::HashSet::new();
    for _ in 0..128 {
        let mut turn = Turn::default();
        accept(&mut turn, ready()).unwrap();
        let id = turn
            .submit_continuation("saved", "same-session", "same-entry")
            .unwrap();
        let suffix = id.strip_prefix("rust-live-prompt-").unwrap();
        assert_eq!(suffix.len(), 32);
        assert!(suffix.bytes().all(|b| b.is_ascii_hexdigit()));
        assert!(ids.insert(id));
    }
}

#[test]
fn launch_flags_preserve_prompt_values_and_remain_invocation_only_options() {
    let args = [
        "--python",
        "python",
        "--source",
        "source",
        "--data-root",
        "data",
        "--backend",
        "custom",
        "--model",
        "chosen",
        "--prompt",
        "--load-model",
        "--host",
        "http://localhost:45123/v1",
        "--reasoning-effort",
        "default",
        "--max-tokens",
        "17",
        "--api-key-env",
        "FIXTURE_KEY",
    ]
    .map(str::to_owned);
    let options = Options::parse(&args).unwrap();
    assert_eq!(options.prompt, "--load-model");
    assert!(!options.launch.load_model);
    assert_eq!(options.launch.thinking_level.as_deref(), Some("default"));
    assert_eq!(options.launch.max_tokens, Some(17));
    assert_eq!(options.launch.api_key_env.as_deref(), Some("FIXTURE_KEY"));
    assert_eq!(
        options.launch.base_url.as_deref(),
        Some("http://localhost:45123/v1")
    );
    let mut invalid = args.to_vec();
    invalid.extend(["--server-mode".into(), "auto".into()]);
    assert!(Options::parse(&invalid).is_err());
}

#[test]
fn programmatic_launch_intent_is_revalidated_before_paths_or_persistence() {
    use litetui_terminal::{live::Interactive, runtime_launch::ServerMode};
    let root = std::env::temp_dir().join(format!("litetui-invalid-launch-{}", std::process::id()));
    assert!(!root.exists());
    let args = vec![
        "--python".into(),
        "missing-python".into(),
        "--source".into(),
        "missing-source".into(),
        "--data-root".into(),
        root.to_string_lossy().into_owned(),
        "--backend".into(),
        "custom".into(),
        "--model".into(),
        "chosen".into(),
        "--tool-profile".into(),
        "strict".into(),
    ];
    let mut options = Options::parse(&args).unwrap();
    options.launch.server_mode = Some(ServerMode::Auto);
    let runtime = Interactive::start(options);
    let error = runtime.with_cleanup(|_| Ok(())).unwrap_err();
    assert!(error.to_string().contains("Local auto"), "{error}");
    assert!(!root.exists());
}
