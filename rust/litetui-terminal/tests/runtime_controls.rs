use litetui_terminal::{
    runtime_controls as r,
    tool_mode::{Expected, Profile, ValidationMode},
    work_lifecycle as life,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn id(n: u64) -> String {
    format!("00000000-0000-4000-8000-{n:012x}")
}
fn hash(v: &Value) -> String {
    format!("{:x}", Sha256::digest(serde_json::to_vec(v).unwrap()))
}
fn binding() -> r::Binding {
    r::Binding {
        identity: life::Identity {
            session: id(1),
            agent: id(2),
            backend: "custom".into(),
            model: "controlled".into(),
        },
        work_instance: "a".repeat(64),
        provider: life::Provider::Host,
        tool_selection: false,
    }
}
fn base(kind: &str) -> Value {
    let mut v = binding().request("unused");
    v.as_object_mut().unwrap().remove("type");
    v["kind"] = json!(kind);
    v
}
fn facts() -> Value {
    json!({"tools_enabled":true,"settings_profile":"strict","chosen_profile":"strict","seat_profile":"strict","effective_profile":"strict","requested_cli_profile":"strict","plan_mode":false,"mcp_enabled":false,"approval_route":"host","cwd":std::env::current_dir().unwrap().to_string_lossy(),"provider":"host","inventory":"host_request"})
}
fn owner(active: bool) -> Value {
    json!({"state":if active {"active"}else{"idle"},"origin":if active {json!({"session_id":id(1),"operation_id":"real-prompt","hook_id":null,"source":"rpc","execution_id":id(3)})}else{Value::Null}})
}
fn snapshot_value(active: bool) -> Value {
    let mut v = base("runtime_controls_snapshot");
    v["generation"] = json!(0);
    v["snapshot"] = json!("b".repeat(64));
    v["facts"] = facts();
    v["owner"] = owner(active);
    v["saved"] = json!({"tools_enabled":true,"tool_profile":"strict"});
    v["revisions"] = json!({"global":"absent","conversation":"absent","agent":"absent"});
    v["first_system"] = Value::Null;
    v["plan"] = json!({"present":false,"sha256":null,"text":null});
    v["profiles"] = json!([{"name":"strict","label":"Strict","summary":"Strict source summary"},{"name":"interactive","label":"Interactive","summary":"Interactive source summary"},{"name":"autonomous","label":"Autonomous","summary":"Autonomous source summary"}]);
    v["next_profile"] = json!("autonomous");
    v["editable"] = json!({"tools_enabled":null,"tool_profile":null,"plan_mode":null});
    v
}
fn review_value(s: &Value, change: &r::Change) -> Value {
    let mut v = base("runtime_controls_review");
    v["action_id"] = json!(id(4));
    v["review_token"] = json!("c".repeat(64));
    v["snapshot"] = s["snapshot"].clone();
    v["generation"] = s["generation"].clone();
    v["change"] = change.wire();
    v["before"] = s["facts"].clone();
    v["after"] = s["facts"].clone();
    match change {
        r::Change::ToolSelection { .. } => {
            unreachable!("per-tool transactions use exported source evidence")
        }
        r::Change::Tools(x) => v["after"]["tools_enabled"] = json!(x),
        r::Change::Plan(x) => v["after"]["plan_mode"] = json!(x),
        r::Change::Profile(x) => {
            for k in [
                "settings_profile",
                "chosen_profile",
                "seat_profile",
                "effective_profile",
            ] {
                v["after"][k] = json!(x.as_str());
            }
            v["after"]["requested_cli_profile"] = Value::Null;
        }
    }
    v["owner_before"] = s["owner"].clone();
    v["saved_before"] = s["saved"].clone();
    v["revisions"] = s["revisions"].clone();
    v["first_system_before"] = s["first_system"].clone();
    v["first_system_after"] = s["first_system"].clone();
    v["plan"] = s["plan"].clone();
    v["context_sha256"] = json!("d".repeat(64));
    v["consequence"] = json!(
        "The next new authorization uses the reviewed state; existing captured calls retain their authority."
    );
    v
}
fn setup(change: r::Change, active: bool) -> r::Review {
    let s = snapshot_value(active);
    let snapshot = r::Snapshot::parse(&s, &binding()).unwrap();
    r::Review::parse(&review_value(&s, &change), &snapshot, &change).unwrap()
}
fn result(review: &r::Review, status: r::Status) -> Value {
    let mut v = base("runtime_controls_result");
    v["action_id"] = json!(review.action_id);
    v["review_token"] = json!(review.token);
    v["apply_operation_id"] = json!("apply");
    v["generation_before"] = json!(review.generation);
    v["generation_after"] = json!(1);
    v["status"] = json!(status.key());
    v["effect_attempted"] = json!(true);
    v["changed"] = json!(true);
    v["runtime_after"] = review.after.wire.clone();
    v["owner_after"] = review.owner_before.wire.clone();
    v["persistence"] = json!(if matches!(review.change, r::Change::Plan(_)) {
        "not_applicable"
    } else {
        "saved"
    });
    v["saved_after"] = review.wire["saved_before"].clone();
    v["revisions_after"] = review.wire["revisions"].clone();
    match &review.change {
        r::Change::ToolSelection { .. } => {
            unreachable!("per-tool transactions use exported source evidence")
        }
        r::Change::Tools(x) => v["saved_after"]["tools_enabled"] = json!(x),
        r::Change::Profile(x) => v["saved_after"]["tool_profile"] = json!(x.as_str()),
        r::Change::Plan(_) => {}
    }
    if !matches!(review.change, r::Change::Plan(_)) {
        v["revisions_after"]["conversation"] = json!("e".repeat(64));
    }
    v["first_system_after"] = review.wire["first_system_after"].clone();
    v["reason"] = Value::Null;
    v["readback_token"] = json!("f".repeat(64));
    v["close_required"] = json!(false);
    if status == r::Status::SessionOnly {
        v["persistence"] = json!("failed");
        v["reason"] = json!("Canonical save failed without changing saved bytes");
        v["saved_after"] = review.wire["saved_before"].clone();
        v["revisions_after"] = review.wire["revisions"].clone();
    }
    if status == r::Status::Refused {
        for k in [
            "generation_after",
            "runtime_after",
            "owner_after",
            "saved_after",
            "revisions_after",
            "first_system_after",
        ] {
            v[k] = Value::Null;
        }
        v["effect_attempted"] = json!(false);
        v["changed"] = json!(false);
        v["reason"] = json!("Stale reviewed context");
        v["persistence"] = json!("not_attempted");
    }
    v
}
fn readback(a: &r::Receipt) -> Value {
    let mut v = base("runtime_controls_readback");
    for k in [
        "action_id",
        "review_token",
        "apply_operation_id",
        "generation_before",
        "generation_after",
        "status",
        "reason",
    ] {
        v[k] = a.wire[k].clone();
    }
    v["readback_operation_id"] = json!("readback");
    v["result_sha256"] = json!(hash(&a.wire));
    v["verified"] = json!(true);
    v["current"] = a.wire["runtime_after"].clone();
    v["owner"] = a.wire["owner_after"].clone();
    v["effective_transition"] = json!("unchanged");
    v["saved"] = a.wire["saved_after"].clone();
    v["revisions"] = a.wire["revisions_after"].clone();
    v["first_system"] = a.wire["first_system_after"].clone();
    v["release"] = json!("install");
    if a.status == r::Status::Refused {
        v["release"] = json!("unchanged");
        v["effective_transition"] = Value::Null;
    }
    v
}
fn shutdown(review: &r::Review, a: Option<&r::Receipt>, v: Option<&r::Readback>) -> Value {
    let mut e = base("runtime_controls_shutdown");
    e["published_generation"] = json!(if v.is_some_and(|v| v.release == r::Release::Install) {
        1
    } else {
        0
    });
    e["action_id"] = json!(review.action_id);
    e["review_token"] = json!(review.token);
    e["apply_operation_id"] = json!("apply");
    e["readback_operation_id"] = v.map_or(Value::Null, |_| json!("readback"));
    e["result_sha256"] = a.map_or(Value::Null, |a| json!(hash(&a.wire)));
    e["readback_sha256"] = v.map_or(Value::Null, |v| json!(hash(&v.wire)));
    e["pending_at_close"] = json!(v.is_none());
    e["readback_published"] = json!(v.is_some());
    e["effect_attempted"] = json!(a.is_some_and(|a| a.effect_attempted));
    e["disposition"] = json!(if a.is_some_and(|a| a.status == r::Status::Refused) {
        "no_effect"
    } else if v.is_some() {
        "verified"
    } else if a.is_some_and(|a| a.effect_attempted) {
        "uncertain"
    } else {
        "no_effect"
    });
    for k in [
        "authorization_closed",
        "admission_closed",
        "authorization_owners_joined",
        "waiter_owners_joined",
    ] {
        e[k] = json!(true);
    }
    e["control_owner"] = json!("joined");
    e["publication_owner"] = json!(if a.is_some() { "joined" } else { "absent" });
    e["remaining_authorizations"] = json!(0);
    e["remaining_waiters"] = json!(0);
    e["effects_verified"] = json!(false);
    e
}
#[test]
fn verifies_exported_source_transactions_through_shutdown() {
    let fixture: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/runtime_controls.json"
    ))
    .unwrap();
    assert_eq!(fixture["version"], 1);
    let cases = fixture["cases"].as_array().unwrap();
    let mut names = std::collections::BTreeSet::new();
    let mut operations = r::OPERATIONS.map(str::to_owned).to_vec();
    operations.extend([life::CAPTURE, life::ACTIVATE, life::SETTLED].map(str::to_owned));
    let caps = r::Capabilities::negotiate(&operations).unwrap().unwrap();
    for case in cases {
        let name = case["name"].as_str().unwrap();
        assert!(names.insert(name), "duplicate case {name}");
        let capture = &case["capture"];
        assert_eq!(capture["provider"], "host");
        let binding = r::Binding {
            identity: life::Identity::parse(&capture["identity"]).unwrap(),
            work_instance: capture["instance"].as_str().unwrap().to_owned(),
            provider: life::Provider::Host,
            tool_selection: false,
        };
        let initial = caps.parse_capture(capture, &binding).unwrap();
        caps.parse_activation(&case["activation"], &binding, &initial)
            .unwrap();
        let snapshot = r::Snapshot::parse(&case["snapshot"], &binding).unwrap();
        let (change, status) = match name {
            "plan" => (r::Change::Plan(true), r::Status::Applied),
            "stale-plan" => (r::Change::Plan(true), r::Status::Refused),
            "profile" => (r::Change::Profile(Profile::Strict), r::Status::Applied),
            "tools" | "saved-before-error" => (r::Change::Tools(true), r::Status::Applied),
            "failed-save" => (r::Change::Tools(true), r::Status::SessionOnly),
            _ => panic!("unexpected source case {name}"),
        };
        let review = r::Review::parse(&case["review"], &snapshot, &change).unwrap();
        let apply = case["result"]["apply_operation_id"].as_str().unwrap();
        let receipt = r::Receipt::parse(&case["result"], &review, apply).unwrap();
        assert_eq!(receipt.status, status, "{name}");
        let readback = r::Readback::parse(&case["readback"], &receipt).unwrap();
        let shutdown = r::Shutdown::parse(&case["shutdown"], &binding).unwrap();
        shutdown
            .verify_invocation(
                &review,
                apply,
                Some(&receipt),
                Some(&readback),
                case["readback"]["readback_operation_id"].as_str(),
                initial.generation,
            )
            .unwrap();
    }
    assert_eq!(names.len(), 6);
}
#[test]
fn exact_capabilities_capture_and_activation_are_independent_and_inert() {
    assert!(r::Capabilities::negotiate(&[]).unwrap().is_none());
    let mut ops = r::OPERATIONS.map(str::to_owned).to_vec();
    assert!(r::Capabilities::negotiate(&ops).is_err());
    ops.extend([life::CAPTURE, life::ACTIVATE, life::SETTLED].map(str::to_owned));
    let caps = r::Capabilities::negotiate(&ops).unwrap().unwrap();
    let capture = json!({"runtime_controls_version":1,"runtime_policy":{"generation":0,"facts":facts()},"runtime_controls_limits":{"authorizations":128,"waiters":128}});
    let policy = caps.parse_capture(&capture, &binding()).unwrap();
    caps.parse_activation(&capture, &binding(), &policy)
        .unwrap();
    for val in [json!(true), json!(1.0), json!(2), Value::Null] {
        let mut bad = capture.clone();
        bad["runtime_controls_version"] = val;
        assert!(caps.parse_capture(&bad, &binding()).is_err());
    }
    let mut bad = capture;
    bad["runtime_policy"]["facts"]["plan_mode"] = json!(true);
    assert!(caps.parse_activation(&bad, &binding(), &policy).is_err());
    ops.push(r::APPLY.into());
    assert!(r::Capabilities::negotiate(&ops).is_err());
}
#[test]
fn review_result_readback_bind_one_explicit_field_and_all_protected_evidence() {
    for change in [
        r::Change::Tools(false),
        r::Change::Plan(true),
        r::Change::Profile(Profile::Autonomous),
        r::Change::Profile(Profile::Strict),
    ] {
        let review = setup(change, false);
        let a = r::Receipt::parse(&result(&review, r::Status::Applied), &review, "apply").unwrap();
        let v = r::Readback::parse(&readback(&a), &a).unwrap();
        assert_eq!(v.policy().unwrap().facts, review.after);
        assert_eq!(
            a.readback_request().unwrap()["result_sha256"],
            hash(&a.wire)
        );
        assert_eq!(review.request()["review"], review.wire);
        for key in [
            "action_id",
            "review_token",
            "apply_operation_id",
            "generation_before",
            "runtime_after",
            "saved_after",
            "first_system_after",
        ] {
            let mut bad = a.wire.clone();
            bad[key] = json!(false);
            assert!(r::Receipt::parse(&bad, &review, "apply").is_err(), "{key}");
        }
    }
}
#[test]
fn stale_no_effect_readback_is_not_required_to_equal_old_review_before() {
    let review = setup(r::Change::Tools(false), false);
    let a = r::Receipt::parse(&result(&review, r::Status::Refused), &review, "apply").unwrap();
    let mut v = readback(&a);
    v["current"] = facts();
    v["current"]["plan_mode"] = json!(true);
    v["owner"] = owner(false);
    v["saved"] = review.wire["saved_before"].clone();
    v["revisions"] = review.wire["revisions"].clone();
    let parsed = r::Readback::parse(&v, &a).unwrap();
    assert!(parsed.policy().is_none());
    assert_eq!(parsed.release, r::Release::Unchanged);
    v["verified"] = json!(false);
    v["release"] = json!("close");
    v["reason"] = json!("Current observation unavailable");
    assert_eq!(
        r::Readback::parse(&v, &a).unwrap().status,
        r::Status::Refused
    );
    v["status"] = json!("uncertain");
    assert!(r::Readback::parse(&v, &a).is_err());
}
#[test]
fn session_only_requires_unchanged_saved_bytes_and_plan_cannot_persist() {
    let review = setup(r::Change::Tools(false), false);
    let a = r::Receipt::parse(&result(&review, r::Status::SessionOnly), &review, "apply").unwrap();
    r::Readback::parse(&readback(&a), &a).unwrap();
    let mut bad = a.wire.clone();
    bad["revisions_after"]["conversation"] = json!("e".repeat(64));
    assert!(r::Receipt::parse(&bad, &review, "apply").is_err());
    let review = setup(r::Change::Plan(true), false);
    let mut bad = result(&review, r::Status::Applied);
    bad["revisions_after"]["conversation"] = json!("e".repeat(64));
    assert!(r::Receipt::parse(&bad, &review, "apply").is_err());
}
#[test]
fn effective_profile_progression_is_finite_and_does_not_change_old_owner() {
    let mut s = snapshot_value(true);
    s["facts"]["settings_profile"] = json!("autonomous");
    s["facts"]["chosen_profile"] = json!("autonomous");
    s["facts"]["seat_profile"] = json!("autonomous");
    s["facts"]["requested_cli_profile"] = Value::Null;
    s["next_profile"] = json!("interactive");
    let snapshot = r::Snapshot::parse(&s, &binding()).unwrap();
    let change = r::Change::Plan(true);
    let review = r::Review::parse(&review_value(&s, &change), &snapshot, &change).unwrap();
    let a = r::Receipt::parse(&result(&review, r::Status::Applied), &review, "apply").unwrap();
    let mut v = readback(&a);
    v["owner"]["state"] = json!("terminal");
    r::Readback::parse(&v, &a).unwrap();
    // Parser-only boundary case: manager must additionally prove actual canonical restoration.
    v["effective_transition"] = json!("restore_seat");
    v["current"]["effective_profile"] = json!("autonomous");
    r::Readback::parse(&v, &a).unwrap();
    v["current"]["effective_profile"] = json!("interactive");
    assert!(r::Readback::parse(&v, &a).is_err());
    v["current"]["effective_profile"] = json!("autonomous");
    v["owner"]["state"] = json!("active");
    assert!(r::Readback::parse(&v, &a).is_err());
    v["owner"]["state"] = json!("terminal");
    v["owner"]["origin"]["execution_id"] = json!(id(99));
    assert!(r::Readback::parse(&v, &a).is_err());
    assert_eq!(a.owner_after.unwrap(), review.owner_before);
}
#[test]
fn shutdown_binds_sent_readback_even_before_body_and_keeps_effect_claims_exact() {
    let review = setup(r::Change::Tools(false), false);
    let a = r::Receipt::parse(&result(&review, r::Status::Applied), &review, "apply").unwrap();
    let v = r::Readback::parse(&readback(&a), &a).unwrap();
    let e = shutdown(&review, Some(&a), Some(&v));
    r::Shutdown::parse(&e, &binding())
        .unwrap()
        .verify_invocation(&review, "apply", Some(&a), Some(&v), Some("readback"), 1)
        .unwrap();
    let mut early = shutdown(&review, Some(&a), None);
    early["readback_operation_id"] = json!("readback");
    r::Shutdown::parse(&early, &binding())
        .unwrap()
        .verify_invocation(&review, "apply", Some(&a), None, Some("readback"), 0)
        .unwrap();
    early["readback_operation_id"] = Value::Null;
    assert!(
        r::Shutdown::parse(&early, &binding())
            .unwrap()
            .verify_invocation(&review, "apply", Some(&a), None, Some("readback"), 0)
            .is_err()
    );
    for key in [
        "published_generation",
        "result_sha256",
        "readback_sha256",
        "effect_attempted",
        "authorization_owners_joined",
        "remaining_waiters",
    ] {
        let mut bad = e.clone();
        bad[key] = json!(false);
        let check = r::Shutdown::parse(&bad, &binding()).and_then(|e| {
            e.verify_invocation(&review, "apply", Some(&a), Some(&v), Some("readback"), 1)
        });
        assert!(check.is_err(), "{key}");
    }
}
#[test]
fn strict_bodies_reject_duplicate_keys_unknown_fields_and_numeric_versions() {
    assert!(r::strict_body("{\"version\":1,\"version\":1}").is_err());
    let mut s = snapshot_value(false);
    s["extra"] = Value::Null;
    assert!(r::Snapshot::parse(&s, &binding()).is_err());
    s.as_object_mut().unwrap().remove("extra");
    s["version"] = json!(1.0);
    assert!(r::Snapshot::parse(&s, &binding()).is_err());
    let mut s = snapshot_value(false);
    s["generation"] = json!(9_007_199_254_740_992_u64);
    assert!(r::Snapshot::parse(&s, &binding()).is_err());
    s = snapshot_value(false);
    s["facts"]["inventory"] = json!("claude_session");
    assert!(r::Snapshot::parse(&s, &binding()).is_err());
}
#[test]
fn current_policy_does_not_resurrect_launch_profile_or_cli_override() {
    let review = setup(r::Change::Profile(Profile::Autonomous), false);
    let a = r::Receipt::parse(&result(&review, r::Status::Applied), &review, "apply").unwrap();
    let p = r::Readback::parse(&readback(&a), &a)
        .unwrap()
        .policy()
        .unwrap();
    let expected = Expected {
        enabled: true,
        profile: Profile::Strict,
        workspace: std::env::current_dir().unwrap(),
    };
    assert!(
        expected
            .validate_facts(&p.facts, ValidationMode::Launch)
            .is_err()
    );
    expected
        .validate_facts(&p.facts, ValidationMode::Current(&p))
        .unwrap();
    expected
        .validate_facts(&p.facts, ValidationMode::Target)
        .unwrap();
    assert!(p.prompt_binding().get("tool_profile").is_none());
    assert_eq!(p.prompt_binding()["generation"], 1);
    let mut foreign = p.facts.clone();
    foreign.cwd = std::env::current_dir()
        .unwrap()
        .join("foreign")
        .to_string_lossy()
        .into_owned();
    assert!(
        expected
            .validate_facts(&foreign, ValidationMode::Target)
            .is_err()
    );
}
