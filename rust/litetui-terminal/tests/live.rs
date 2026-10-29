use litetui_terminal::live::{Observation, Options, Turn, terminal_text};
use serde_json::json;

fn ready() -> serde_json::Value {
    json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"claude-sonnet-5-5"})
}
fn accept(turn: &mut Turn, value: serde_json::Value) -> std::io::Result<Observation> {
    turn.accept(&value, "claude", "claude-sonnet-5-5")
}
fn started() -> Turn {
    let mut turn = Turn::default();
    assert_eq!(accept(&mut turn, ready()).unwrap(), Observation::Ready);
    turn.submit("Hello").unwrap();
    accept(
        &mut turn,
        json!({"type":"response","id":"rust-live-prompt","ok":true,"result":{"turn":"accepted"}}),
    )
    .unwrap();
    accept(
        &mut turn,
        json!({"type":"turn_start","model":"claude-sonnet-5-5"}),
    )
    .unwrap();
    turn
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
fn options_require_explicit_frontier_route_and_bounded_deadline() {
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
    for backend in ["lmstudio", "llamacpp", "ninfer"] {
        let mut args = args.clone();
        args[7] = backend.into();
        assert!(Options::parse(&args).is_err());
    }
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
