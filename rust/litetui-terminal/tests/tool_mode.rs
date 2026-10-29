use litetui_terminal::{
    live::{Options, Turn},
    tool_mode::{Expected, Facts, Profile},
};
use serde_json::{Value, json};
fn args() -> Vec<String> {
    [
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
    ]
    .into_iter()
    .map(String::from)
    .collect()
}
fn ready() -> Value {
    json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"fixture",
        "tools_enabled":true,"mcp_enabled":false,"tool_profile":"strict","tool_profile_requested":"strict",
        "approval_route":"host","cwd":std::env::current_dir().unwrap()})
}
fn expected() -> Expected {
    Expected {
        enabled: true,
        profile: Profile::Strict,
        workspace: std::env::current_dir().unwrap().canonicalize().unwrap(),
    }
}
#[test]
fn tool_mode_defaults_are_explicit_and_bad_or_missing_options_fail() {
    let options = Options::parse(&args()).unwrap();
    assert!(!options.tools_enabled && options.tool_profile == Profile::Strict);
    for tail in [
        vec!["--tools", "yes"],
        vec!["--tool-profile", "unknown"],
        vec!["--tools", "on"],
        vec!["--tools", "on", "--workspace", "relative"],
    ] {
        let mut a = args();
        a.extend(tail.into_iter().map(String::from));
        assert!(Options::parse(&a).is_err());
    }
    for profile in ["strict", "interactive", "autonomous"] {
        let mut a = args();
        a.extend(["--tool-profile".into(), profile.into()]);
        assert_eq!(Options::parse(&a).unwrap().tool_profile.as_str(), profile);
    }
}
#[test]
fn policy_mismatch_or_missing_facts_never_admits_prompt() {
    for (key, bad) in [
        ("tools_enabled", json!(false)),
        ("tools_enabled", json!("true")),
        ("mcp_enabled", json!(true)),
        ("tool_profile", json!("autonomous")),
        ("tool_profile_requested", Value::Null),
        ("approval_route", json!("spawner")),
        ("cwd", json!("C:/nonexistent-rust-fixture")),
        ("tools_enabled", Value::Null),
    ] {
        let mut event = ready();
        event[key] = bad;
        let mut turn = Turn::with_policy(expected());
        assert!(turn.accept(&event, "claude", "fixture").is_err(), "{key}");
        assert!(turn.submit("never sent").is_err());
    }
    let mut event = ready();
    event.as_object_mut().unwrap().remove("tools_enabled");
    assert!(expected().validate(&event).is_err());
}
#[test]
fn every_prompt_carries_the_verified_launch_profile() {
    for profile in [Profile::Strict, Profile::Interactive, Profile::Autonomous] {
        let mut policy = expected();
        policy.profile = profile;
        let mut event = ready();
        event["tool_profile"] = json!(profile.as_str());
        event["tool_profile_requested"] = json!(profile.as_str());
        let mut turn = Turn::with_policy(policy);
        turn.accept(&event, "claude", "fixture").unwrap();
        let command = turn.submit("fixture").unwrap();
        assert_eq!(command["tool_profile"], profile.as_str());
        assert!(
            Facts::from_ready(&event)
                .unwrap()
                .unwrap()
                .description()
                .contains(profile.as_str())
        );
    }
}
#[test]
fn missing_legacy_policy_remains_unknown_in_read_only_projection() {
    assert!(
        Facts::from_ready(&json!({"type":"ready"}))
            .unwrap()
            .is_none()
    );
}
