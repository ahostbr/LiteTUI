use litetui_terminal::protocol::{ClientError, CommandId, Delivery, Notice, WireClient};
use serde_json::{Value, json};

#[test]
fn raw_object_versions_and_ids_cannot_impersonate_integers() {
    use litetui_terminal::protocol::JsonlDecoder;
    for field in ["protocol_version", "data_version", "id", "operation_id"] {
        let mut c = client(1);
        let command = c.begin_hello().unwrap();
        let mut frame = hello(command["id"].clone());
        let disguised_number = json!({"$serde_json::private::Number":"1"});
        if field == "operation_id" {
            c.ingest(1, frame);
            frame = json!({"type":"gui.event","session_id":"s","operation_id":disguised_number,"event":{"type":"fixture_event"}});
        } else if field == "id" {
            frame["id"] = disguised_number;
        } else {
            frame["result"][field] = disguised_number;
        }
        // Exercise raw bytes through the decoder, not only constructed Values.
        let wire = format!("{frame}\n");
        let mut decoder = JsonlDecoder::new(2048).unwrap();
        let mut notices = Vec::new();
        decoder
            .push(wire.as_bytes(), |value| notices.push(c.ingest(1, value)))
            .unwrap();
        decoder.finish().unwrap();
        assert!(
            matches!(notices.as_slice(), [Notice::Fault { .. }]),
            "{field}: {notices:?}"
        );
        assert!(!c.ready_for_reads());
    }
}

fn client(generation: u64) -> WireClient {
    WireClient::new(
        generation,
        4,
        [
            ("fixture_event".into(), Delivery::Mirrored),
            ("submit_refused".into(), Delivery::Mirrored),
            ("fixture_direct".into(), Delivery::DirectOnly),
        ],
    )
    .unwrap()
}
fn hello(id: Value) -> Value {
    json!({"type":"response","id":id,"ok":true,"result":{
        "protocol_version":1,"data_version":1,"capabilities":["hello"],"operations":["gui.hello"]}})
}
fn ready(client: &mut WireClient, generation: u64) {
    let command = client.begin_hello().unwrap();
    assert_eq!(command["type"], "gui.hello");
    assert_eq!(command["protocol_version"], 1);
    assert!(matches!(
        client.ingest(generation, hello(command["id"].clone())),
        Notice::Hello(_)
    ));
    assert!(client.ready_for_reads());
}

#[test]
fn only_a_matching_successful_versioned_hello_can_activate() {
    let mut c = client(1);
    assert!(matches!(
        c.ingest(1, hello(json!("unsolicited"))),
        Notice::Response { matched: false, .. }
    ));
    assert!(!c.ready_for_reads());
    let command = c.begin_hello().unwrap();
    c.ingest(1, hello(json!("other")));
    assert!(!c.ready_for_reads());
    c.ingest(1, hello(command["id"].clone()));
    assert!(c.ready_for_reads());
    assert!(matches!(
        c.ingest(1, hello(command["id"].clone())),
        Notice::Response { matched: false, .. }
    ));
    assert_eq!(c.begin_hello(), Err(ClientError::HelloAlreadyStarted));
}

#[test]
fn malformed_or_incompatible_hello_never_enables_controls() {
    for bad in [json!(true), json!(1.0), json!(2), Value::Null] {
        let mut c = client(1);
        let command = c.begin_hello().unwrap();
        let mut reply = hello(command["id"].clone());
        reply["result"]["protocol_version"] = bad;
        assert!(matches!(c.ingest(1, reply), Notice::Fault { .. }));
        assert!(!c.ready_for_reads());
    }
    let mut c = client(1);
    let command = c.begin_hello().unwrap();
    let mut reply = hello(command["id"].clone());
    reply["result"]["operations"] = json!([1]);
    assert!(matches!(c.ingest(1, reply), Notice::Fault { .. }));
}

#[test]
fn mirrored_events_are_selected_once_but_equal_events_are_not_deduplicated() {
    let mut c = client(1);
    let event = json!({"type":"fixture_event","text":"same"});
    assert!(matches!(c.ingest(1, event.clone()), Notice::LegacyEvent(_)));
    ready(&mut c, 1);
    for _ in 0..2 {
        assert!(matches!(
            c.ingest(1, event.clone()),
            Notice::IgnoredMirror(_)
        ));
        assert!(matches!(c.ingest(1, json!({"type":"gui.event","session_id":"session", "operation_id":null,"event":event})), Notice::Event { .. }));
    }
    assert!(matches!(
        c.ingest(1, json!({"type":"fixture_direct","value":3})),
        Notice::Event { .. }
    ));
}

#[test]
fn early_envelope_or_state_event_during_hello_latches_uncertainty() {
    for early in [
        json!({"type":"fixture_event"}),
        json!({"type":"gui.event","session_id":"s","operation_id":null,"event":{"type":"fixture_event"}}),
    ] {
        let mut c = client(1);
        let command = c.begin_hello().unwrap();
        assert!(matches!(c.ingest(1, early), Notice::Fault { .. }));
        c.ingest(1, hello(command["id"].clone()));
        assert!(c.compatible());
        assert!(!c.ready_for_reads());
        assert!(c.fault().is_some());
    }
}

#[test]
fn unknown_event_blocks_but_pending_responses_and_direct_errors_still_drain() {
    let mut c = client(1);
    ready(&mut c, 1);
    let request = c.register_read().unwrap();
    assert!(matches!(
        c.ingest(1, json!({"type":"future_state_event"})),
        Notice::Fault { .. }
    ));
    assert_eq!(c.register_read(), Err(ClientError::NotReady));
    assert!(matches!(
        c.ingest(
            1,
            json!({"type":"response","id":request.to_value(),"ok":true,"result":null})
        ),
        Notice::Response { matched: true, .. }
    ));
    assert!(matches!(
        c.ingest(1, json!({"type":"error","error":"fixture transport error"})),
        Notice::DirectError(_)
    ));
    assert!(matches!(
        c.ingest(1, json!({"type":"fixture_event"})),
        Notice::Quarantined(_)
    ));
    assert!(!c.ready_for_reads());
}

#[test]
fn malformed_response_does_not_fall_through_as_an_event() {
    for reply in [
        json!({"type":"response","id":null,"ok":true,"result":null}),
        json!({"type":"response","id":false,"ok":true,"result":null}),
        json!({"type":"response","id":1.5,"ok":true,"result":null}),
        json!({"type":"response","ok":"true","result":{}}),
        json!({"type":"response","ok":true}),
        json!({"type":"response","ok":false,"error":null}),
    ] {
        let mut c = client(1);
        assert!(matches!(c.ingest(1, reply), Notice::Fault { .. }));
    }
}

#[test]
fn command_identity_does_not_coerce_numbers_or_reuse_completed_ids() {
    assert_ne!(
        CommandId::try_from(&json!("7")).unwrap(),
        CommandId::try_from(&json!(7)).unwrap()
    );
    let mut c = client(8);
    ready(&mut c, 8);
    let request = c.register_read().unwrap();
    let reply = json!({"type":"response","id":request.to_value(),"ok":true,"result":{}});
    assert!(matches!(
        c.ingest(7, reply.clone()),
        Notice::StaleGeneration
    ));
    assert_eq!(c.pending_count(), 1);
    assert!(matches!(
        c.ingest(8, reply.clone()),
        Notice::Response { matched: true, .. }
    ));
    let next = c.register_read().unwrap();
    assert_ne!(request, next);
    assert!(matches!(
        c.ingest(8, reply),
        Notice::Response { matched: false, .. }
    ));
    assert_eq!(c.pending_count(), 1);
}

#[test]
fn pending_capacity_and_disconnect_are_explicit_without_resend() {
    let mut c = client(1);
    ready(&mut c, 1);
    let request = c.register_read().unwrap();
    for _ in 0..3 {
        c.register_read().unwrap();
    }
    assert_eq!(c.register_read(), Err(ClientError::PendingLimit));
    c.disconnect();
    assert!(!c.ready_for_reads());
    assert_eq!(
        c.pending_count(),
        4,
        "uncertain requests must not be reported completed"
    );
    assert!(matches!(
        c.ingest(
            1,
            json!({"type":"response","id":request.to_value(),"ok":true,"result":{}})
        ),
        Notice::Quarantined(_)
    ));
    assert_eq!(c.pending_count(), 4);
}

#[test]
fn refusal_keeps_null_identity_and_does_not_become_a_completed_turn() {
    let mut c = client(1);
    ready(&mut c, 1);
    let event = json!({"type":"submit_refused","reason":"empty","detail":"nothing to send"});
    let frame = json!({"type":"gui.event","session_id":"s","operation_id":null,"event":event});
    assert_eq!(c.ingest(1, frame.clone()), Notice::Event { frame, event });
}

#[test]
fn identity_round_trips_without_coercion_or_precision_loss() {
    for value in [
        json!(i64::MIN),
        json!(-1),
        json!(0),
        json!(i64::MAX),
        json!(u64::MAX),
        json!(""),
        json!("7"),
    ] {
        let id = CommandId::try_from(&value).unwrap();
        assert_eq!(id.to_value(), value);
        assert_eq!(CommandId::try_from(&id.to_value()).unwrap(), id);
    }
    for value in [
        json!(1.0),
        json!(false),
        Value::Null,
        json!([]),
        json!({}),
        serde_json::from_str("18446744073709551617").unwrap(),
    ] {
        assert!(CommandId::try_from(&value).is_err());
    }
    let mut first = client(1);
    let mut next_generation = client(2);
    assert_ne!(
        first.begin_hello().unwrap()["id"],
        next_generation.begin_hello().unwrap()["id"]
    );
}

#[test]
fn idless_responses_do_not_drain_pending_requests_or_fault() {
    let mut c = client(1);
    ready(&mut c, 1);
    c.register_read().unwrap();
    for frame in [
        json!({"type":"response","ok":true,"result":null}),
        json!({"type":"response","ok":false,"error":"invalid command"}),
    ] {
        assert_eq!(
            c.ingest(1, frame.clone()),
            Notice::Response {
                id: None,
                matched: false,
                frame
            }
        );
        assert_eq!(c.pending_count(), 1);
        assert!(c.ready_for_reads());
    }
}

#[test]
fn rejected_hello_drains_but_never_activates() {
    let mut c = client(1);
    let command = c.begin_hello().unwrap();
    let frame =
        json!({"type":"response","id":command["id"],"ok":false,"error":"unsupported version"});
    assert!(matches!(
        c.ingest(1, frame),
        Notice::Fault {
            reason: litetui_terminal::protocol::ClientFault::HelloRejected,
            ..
        }
    ));
    assert_eq!(c.pending_count(), 0);
    assert!(!c.compatible());
    assert!(!c.ready_for_reads());
}

#[test]
fn malformed_response_does_not_retire_id_and_valid_response_still_drains() {
    let mut c = client(1);
    ready(&mut c, 1);
    let id = c.register_read().unwrap().to_value();
    assert!(matches!(
        c.ingest(1, json!({"type":"response","id":id,"ok":"yes","result":{}})),
        Notice::Fault { .. }
    ));
    assert_eq!(c.pending_count(), 1);
    assert!(matches!(
        c.ingest(1, json!({"type":"response","id":id,"ok":true,"result":{}})),
        Notice::Response { matched: true, .. }
    ));
    assert_eq!(c.pending_count(), 0);
    assert!(!c.ready_for_reads());
}

#[test]
fn invalid_envelopes_fault_without_selecting_payloads() {
    let valid = json!({"type":"gui.event","session_id":"s","operation_id":null,"event":{"type":"fixture_event"}});
    let mut bad = Vec::new();
    for key in ["session_id", "operation_id"] {
        let mut missing = valid.clone();
        missing.as_object_mut().unwrap().remove(key);
        bad.push(missing);
    }
    for (key, value) in [
        ("session_id", json!(12)),
        ("session_id", json!(false)),
        ("operation_id", json!(false)),
        ("operation_id", json!(1.0)),
        ("operation_id", json!([])),
        ("event", Value::Null),
        ("event", json!([])),
        ("event", json!({})),
        ("event", json!({"type":2})),
        ("event", json!({"type":"future_event"})),
        ("event", json!({"type":"fixture_direct"})),
    ] {
        let mut frame = valid.clone();
        frame[key] = value;
        bad.push(frame);
    }
    for frame in bad {
        let mut c = client(1);
        ready(&mut c, 1);
        assert!(matches!(c.ingest(1, frame), Notice::Fault { .. }));
        assert!(!c.ready_for_reads());
        assert!(matches!(c.ingest(1, valid.clone()), Notice::Quarantined(_)));
    }
}

#[test]
fn explicitly_null_envelope_identity_is_preserved_not_invented() {
    let mut c = client(1);
    ready(&mut c, 1);
    let event = json!({"type":"fixture_event","extra":"preserved"});
    let frame =
        json!({"type":"gui.event","session_id":null,"operation_id":null,"event":event,"extra":123});
    assert_eq!(c.ingest(1, frame.clone()), Notice::Event { frame, event });
}

#[test]
fn additive_envelope_ids_survive_decoding_without_becoming_a_dedup_policy() {
    use litetui_terminal::protocol::JsonlDecoder;

    let mut c = client(1);
    ready(&mut c, 1);
    let event = json!({"type":"fixture_event","event_id":"payload-id","text":"same"});
    let mut decoder = JsonlDecoder::new(2048).unwrap();
    // Old producers omit the field. New producers supply distinct IDs; replay
    // may repeat one. This transport layer preserves all three cases verbatim.
    for id in [
        None,
        Some("dc64e516-21be-4f55-97f7-bdd0e0b56e19"),
        Some("5b9232ef-42d6-4afd-9b21-ddab8bff870d"),
        Some("5b9232ef-42d6-4afd-9b21-ddab8bff870d"),
    ] {
        let mut frame = json!({"type":"gui.event","session_id":"session",
            "operation_id":null,"event":event});
        if let Some(id) = id {
            frame["event_id"] = json!(id);
        }
        let wire = format!("{event}\n{frame}\n");
        let mut notices = Vec::new();
        for byte in wire.as_bytes() {
            decoder
                .push(&[*byte], |value| notices.push(c.ingest(1, value)))
                .unwrap();
        }
        assert_eq!(
            notices,
            vec![
                Notice::IgnoredMirror(event.clone()),
                Notice::Event {
                    frame,
                    event: event.clone()
                }
            ]
        );
    }
    decoder.finish().unwrap();
    assert!(c.ready_for_reads());
}

#[test]
fn explicit_execution_origin_survives_without_filling_unknown_input_identity() {
    use litetui_terminal::protocol::JsonlDecoder;

    let mut c = client(1);
    ready(&mut c, 1);
    let event = json!({"type":"fixture_event","text":"same"});
    let mut decoder = JsonlDecoder::new(2048).unwrap();
    // Only explicitly participating Python producers supply these fields.
    // This transport preserves them; it neither infers nor validates execution
    // ownership and must not borrow identity from a preceding frame.
    for fields in [
        json!({"execution_id":"b4d5db8b-9827-42de-983e-1b7ac183ba49",
               "operation_id":"A","hook_id":"hook-A","source":"typed"}),
        json!({"execution_id":"ef78dd70-b2a2-46d3-a325-e45d9c31b330",
               "operation_id":null,"hook_id":null,"source":null}),
        json!({"operation_id":"legacy"}),
    ] {
        let mut frame = json!({"type":"gui.event","session_id":"session","event":event});
        frame
            .as_object_mut()
            .unwrap()
            .extend(fields.as_object().unwrap().clone());
        let wire = format!("{event}\n{frame}\n");
        let mut notices = Vec::new();
        for byte in wire.as_bytes() {
            decoder
                .push(&[*byte], |value| notices.push(c.ingest(1, value)))
                .unwrap();
        }
        assert_eq!(
            notices,
            vec![
                Notice::IgnoredMirror(event.clone()),
                Notice::Event {
                    frame,
                    event: event.clone()
                }
            ]
        );
    }
    decoder.finish().unwrap();
    assert!(c.ready_for_reads());
}

#[test]
fn delivery_policy_and_capacity_are_explicit_and_unambiguous() {
    assert!(matches!(
        WireClient::new(1, 0, []),
        Err(ClientError::InvalidLimit)
    ));
    for name in ["", "response", "error", "gui.event"] {
        assert!(matches!(
            WireClient::new(1, 1, [(name.into(), Delivery::Mirrored)]),
            Err(ClientError::InvalidPolicy)
        ));
    }
    assert!(matches!(
        WireClient::new(
            1,
            1,
            [
                ("same".into(), Delivery::Mirrored),
                ("same".into(), Delivery::DirectOnly)
            ]
        ),
        Err(ClientError::InvalidPolicy)
    ));
    let mut c = client(1);
    assert_eq!(c.register_read(), Err(ClientError::NotReady));
    c.disconnect();
    assert_eq!(c.begin_hello(), Err(ClientError::NotReady));
}
