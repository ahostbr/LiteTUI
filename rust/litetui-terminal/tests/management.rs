use litetui_terminal::management::{Management, Step};
use serde_json::{Value, json};

const EXECUTION: &str = "12345678-1234-4123-8123-123456789abc";
fn ready() -> Value {
    json!({"type":"ready","management_protocols":[1],"capabilities":["hello","state"],
        "conversation_id":"session-A","backend":"claude","model":"model-A","tool_profile":"strict"})
}
fn state() -> Value {
    json!({"session_id":"session-A","busy":false,"mode":"normal",
        "tool_profile":"strict","model":{"model":"model-A","backend":"claude"},
        "ownership":{"writable":false,"data_version":1},"schedules_paused":false,
        "conversations":[],"jobs":[],"tasks":[],"active_work":[],
        "pending_approvals":[],"pending_questions":[],"pending_model_loads":[],
        "settings":{"requested":{"secret":"DO-NOT-RETAIN-OR-DISPLAY"}}})
}
fn hello(id: &Value) -> Value {
    json!({"type":"response","id":id,"ok":true,"result":{"protocol_version":1,
        "data_version":1,"capabilities":["hello","state"],
        "operations":["gui.hello","gui.state"]}})
}
fn read_pending() -> (Management, Value) {
    let mut client = Management::default();
    assert!(matches!(
        client.accept(ready()).unwrap(),
        Step::Bootstrap(_)
    ));
    let command = client.begin(&ready()).unwrap();
    assert_eq!(command["type"], "gui.hello");
    assert!(!client.ready());
    let Step::Write(read) = client.accept(hello(&command["id"])).unwrap() else {
        panic!()
    };
    assert_eq!(read["type"], "gui.state");
    assert_ne!(command["id"], read["id"]);
    (client, read["id"].clone())
}
fn connected() -> Management {
    let (mut client, id) = read_pending();
    assert!(matches!(
        client
            .accept(json!({"type":"response","id":id,"ok":true,"result":state()}))
            .unwrap(),
        Step::Initialized(_)
    ));
    assert!(client.ready());
    client
}
fn envelope(event: &Value) -> Value {
    json!({"type":"gui.event","event_id":"abcdefab-1234-4123-8123-123456789abc",
        "session_id":"session-A","operation_id":"prompt-A","execution_id":EXECUTION,
        "hook_id":null,"source":"rpc","event":event})
}
fn pair(client: &mut Management, event: &Value) -> Step {
    assert!(matches!(
        client.accept(event.clone()).unwrap(),
        Step::Ignore
    ));
    client.accept(envelope(event)).unwrap()
}
fn started() -> Management {
    let mut client = connected();
    client.prompt("prompt-A").unwrap();
    pair(&mut client, &json!({"type":"turn_start","model":"model-A"}));
    client
}

#[test]
fn startup_requires_two_correlated_reads_and_retains_only_bounded_facts() {
    let (mut client, id) = read_pending();
    assert!(client.prompt("early").is_err());
    let Step::Initialized(snapshot) = client
        .accept(json!({
            "type":"response","id":id,"ok":true,"result":state()
        }))
        .unwrap()
    else {
        panic!()
    };
    assert!(!snapshot.writable); // A fresh pending store is not a broken runtime.
    assert_eq!(snapshot.session, "session-A");
    assert!(!format!("{snapshot:?}").contains("DO-NOT-RETAIN"));
    assert_eq!(client.operations(), &["gui.hello", "gui.state"]);
    client.prompt("prompt-A").unwrap();
}
#[test]
fn missing_versions_or_capabilities_never_activate() {
    for (key, value) in [
        ("management_protocols", json!([true])),
        ("capabilities", json!(["hello"])),
        ("capabilities", json!(["hello", "state", "state"])),
        ("capabilities", json!(vec!["x"; 257])),
        ("conversation_id", json!("")),
    ] {
        let mut client = Management::default();
        let mut value_ready = ready();
        value_ready[key] = value;
        assert!(client.begin(&value_ready).is_err(), "{key}");
        assert!(!client.ready());
        assert!(client.begin(&ready()).is_err());
    }
    for (key, value) in [
        ("protocol_version", json!(true)),
        ("data_version", json!(2)),
        ("operations", json!(["gui.hello"])),
        ("capabilities", json!(["hello"])),
    ] {
        let mut client = Management::default();
        let command = client.begin(&ready()).unwrap();
        let mut response = hello(&command["id"]);
        response["result"][key] = value;
        assert!(client.accept(response).is_err(), "{key}");
        assert!(!client.ready());
    }
}
#[test]
fn snapshot_identity_schema_and_busy_work_are_not_ready() {
    for (key, value) in [
        ("session_id", json!("different")),
        ("busy", json!(true)),
        ("busy", json!("false")),
        ("mode", json!("unknown")),
        ("tool_profile", json!("autonomous")),
        ("model", json!({"model":"B","backend":"claude"})),
        ("model", json!({"model":"model-A","backend":"other"})),
        ("ownership", json!({"data_version":true,"writable":true})),
        ("ownership", json!({"data_version":1,"writable":"true"})),
        ("schedules_paused", json!(null)),
        ("conversations", json!([4])),
        ("jobs", json!(vec![json!({}); 10_001])),
        ("active_work", json!([{}])),
        ("pending_approvals", json!([{}])),
        ("pending_questions", json!([{}])),
        ("pending_model_loads", json!([{}])),
    ] {
        let (mut client, id) = read_pending();
        let mut snapshot = state();
        snapshot[key] = value;
        assert!(
            client
                .accept(json!({"type":"response","id":id,"ok":true,"result":snapshot}))
                .is_err(),
            "{key}"
        );
        assert!(!client.ready());
        assert!(client.prompt("A").is_err());
    }
}
#[test]
fn selected_events_are_once_per_envelope_without_content_deduplication() {
    let mut client = started();
    let event = json!({"type":"text_delta","text":"same","card_index":0});
    for _ in 0..2 {
        let Step::Event(selected) = pair(&mut client, &event) else {
            panic!()
        };
        assert_eq!(selected, event);
    }
    client.ensure_drained().unwrap();
}
#[test]
fn missing_and_mismatched_mirror_pairs_cannot_be_clean_or_admit_new_work() {
    let event = json!({"type":"usage","usage":{"context_tokens":1}});
    let mut client = connected();
    assert!(client.accept(envelope(&event)).is_err());
    let mut client = connected();
    assert!(matches!(
        client.accept(event.clone()).unwrap(),
        Step::Ignore
    ));
    assert!(client.ensure_drained().is_err());
    assert!(client.prompt("A").is_err());
    let other = json!({"type":"usage","usage":{"context_tokens":2}});
    assert!(client.accept(envelope(&other)).is_err());
}
#[test]
fn raw_pair_interleaving_is_bounded_but_does_not_assume_adjacent_lines() {
    let a = json!({"type":"usage","usage":{"context_tokens":1}});
    let b = json!({"type":"usage","usage":{"context_tokens":2}});
    let mut client = connected();
    client.accept(a.clone()).unwrap();
    client.accept(b.clone()).unwrap();
    assert!(matches!(
        client.accept(envelope(&b)).unwrap(),
        Step::Event(_)
    ));
    assert!(matches!(
        client.accept(envelope(&a)).unwrap(),
        Step::Event(_)
    ));
    client.ensure_drained().unwrap();
    for _ in 0..64 {
        client.accept(a.clone()).unwrap();
    }
    assert!(client.accept(a).is_err());
}
#[test]
fn origin_drift_missing_start_and_reused_execution_are_rejected() {
    let event = json!({"type":"text_delta","text":"late"});
    for (key, value) in [
        ("operation_id", json!("prompt-B")),
        (
            "execution_id",
            json!("12345678-1234-4123-8123-123456789abd"),
        ),
        ("execution_id", json!("not-a-uuid")),
        ("session_id", json!("session-B")),
        ("hook_id", json!("different")),
        ("source", json!("different")),
        ("event_id", json!("invalid")),
    ] {
        let mut client = started();
        client.accept(event.clone()).unwrap();
        let mut frame = envelope(&event);
        frame[key] = value;
        assert!(client.accept(frame).is_err(), "{key}");
        assert!(!client.ready());
    }
    let mut client = connected();
    client.prompt("prompt-A").unwrap();
    client.accept(event.clone()).unwrap();
    assert!(client.accept(envelope(&event)).is_err());
    let mut client = started();
    client.prompt("prompt-B").unwrap();
    let event = json!({"type":"turn_start","model":"model-A"});
    client.accept(event.clone()).unwrap();
    let mut frame = envelope(&event);
    frame["operation_id"] = json!("prompt-B");
    assert!(client.accept(frame).is_err());
}
#[test]
fn startup_interleaving_and_unpaired_prefix_fail_before_snapshot_ready() {
    let event = json!({"type":"usage","usage":{"context_tokens":1}});
    let mut client = Management::default();
    client.begin(&ready()).unwrap();
    assert!(client.accept(event.clone()).is_err()); // Ambiguous activation.
    let (mut client, id) = read_pending();
    client.accept(event.clone()).unwrap();
    assert!(
        client
            .accept(json!({"type":"response","id":id,"ok":true,"result":state()}))
            .is_err()
    );
    let (mut client, _) = read_pending();
    client.accept(event.clone()).unwrap();
    assert!(client.accept(envelope(&event)).is_err());
}
#[test]
fn ambient_authority_closures_use_their_own_ledger_not_execution_identity() {
    let mut client = started();
    let event = json!({"type":"tool_approval_closed","id":"ask","outcome":"cancelled","reason":"cancelled"});
    client.accept(event.clone()).unwrap();
    let mut frame = envelope(&event);
    frame.as_object_mut().unwrap().remove("execution_id");
    frame["operation_id"] = json!("abort-command");
    assert!(matches!(client.accept(frame).unwrap(), Step::Event(_)));
}
#[test]
fn model_state_cannot_silently_change_launch_identity() {
    let mut client = connected();
    let event = json!({"type":"model_state","backend":"claude","model":"model-A"});
    assert!(matches!(pair(&mut client, &event), Step::Event(_)));
    let event = json!({"type":"model_state","backend":"claude","model":"another-model"});
    client.accept(event.clone()).unwrap();
    assert!(client.accept(envelope(&event)).is_err());
}

#[test]
fn unpaired_payload_bytes_have_an_exact_aggregate_limit() {
    let mut client = connected();
    let mut event = json!({"type":"usage","text":""});
    let overhead = serde_json::to_vec(&event).unwrap().len();
    event["text"] = json!("x".repeat(2 * 1024 * 1024 - overhead));
    assert_eq!(serde_json::to_vec(&event).unwrap().len(), 2 * 1024 * 1024);
    client.accept(event.clone()).unwrap();
    client.accept(event).unwrap();
    assert!(client.has_unpaired());
    assert!(client.accept(json!({"type":"usage"})).is_err());
}
