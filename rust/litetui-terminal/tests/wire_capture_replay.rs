//! Selected Python-produced capture replay. No child, transport or provider.
use litetui_terminal::protocol::{ClientFault, Delivery, JsonlDecoder, Notice, WireClient};
use serde_json::Value;
use sha2::{Digest, Sha256};

// Hashes copied from completed.json of the recorded Python run, NOT computed
// from this import. Counts are the source producer's expected output records.
const CAPTURES: &[(&str, &str, &str, usize)] = &[
    (
        "negotiated-mirrors",
        include_str!("fixtures/wire/negotiated-mirrors.jsonl"),
        "6fee10a354c533ef5866c8adcd25ecb2e492c1b8ae152caa95bda23c730e3ace",
        6,
    ),
    (
        "reader-framing",
        include_str!("fixtures/wire/reader-framing.jsonl"),
        "eb9e80a4d8e448ff837bc4907c61392bc76999d7ce0d48e3f2c4333e09e41674",
        5,
    ),
    (
        "unsupported-protocol",
        include_str!("fixtures/wire/unsupported-protocol.jsonl"),
        "84f237cd42b9382a4c8590a906a33e57efd288ce73aadc817ae48ab84950ccf1",
        1,
    ),
    (
        "cached-event-mirrors",
        include_str!("fixtures/wire/cached-event-mirrors.jsonl"),
        "3d68074ff50497a18998e246c19f710718c1ce89e02afa18feda969290c91fcb",
        4,
    ),
    (
        "approval-denial",
        include_str!("fixtures/wire/approval-denial.jsonl"),
        "d4094ef3488f5d356ea7d1683c64ac2ced3a35ac9017ecd3f8858703ba38d3db",
        4,
    ),
    (
        "operation-context",
        include_str!("fixtures/wire/operation-context.jsonl"),
        "db830e441cb21584f15b074de296d8ff36e6fe5c7283bb45201d0bbbbd339999",
        4,
    ),
    (
        "refusal-before-ack-raw",
        include_str!("fixtures/wire/refusal-before-ack-raw.jsonl"),
        "e21f456c35eb3e25ca9761a19a7dd2187e7a3070fbb5aaef13b1740c2354b43a",
        2,
    ),
    (
        "refusal-before-ack-gui",
        include_str!("fixtures/wire/refusal-before-ack-gui.jsonl"),
        "ccee9f6e0376689a06e8b52af2674f262780ff71ff10e0f9674b38d46192c9ae",
        3,
    ),
    (
        "terminal-null-rates",
        include_str!("fixtures/wire/terminal-null-rates.jsonl"),
        "17d2bab2b52e8e6f5ec3c56292c28001eb670f8e9023c11f4679239cae4e6f40",
        2,
    ),
];

fn captured_wire(name: &str) -> Vec<u8> {
    let (_, source, sha, _) = CAPTURES
        .iter()
        .find(|(case, _, _, _)| *case == name)
        .unwrap();
    // Native text tools / Windows checkout may use CRLF. Reconstruct the LF
    // wire stream, then require its ORIGINAL byte digest before any replay.
    // No JSON reserialization, fixture-ID rebinding or assumed negotiation.
    let bytes = source.replace("\r\n", "\n").into_bytes();
    assert_eq!(format!("{:x}", Sha256::digest(&bytes)), *sha, "{name}");
    bytes
}
fn decode(chunks: impl IntoIterator<Item = Vec<u8>>) -> Vec<Value> {
    let mut decoder = JsonlDecoder::new(16 * 1024).unwrap(); // fixture-only bound
    let mut records = vec![];
    for chunk in chunks {
        decoder.push(&chunk, |value| records.push(value)).unwrap();
    }
    decoder.finish().unwrap();
    records
}
fn client() -> WireClient {
    WireClient::new(7, 4, [("fixture_event".into(), Delivery::Mirrored)]).unwrap()
}
#[test]
fn captured_output_digest_and_records_survive_every_transport_split() {
    let bytes = captured_wire("negotiated-mirrors");
    let expected = decode([bytes.clone()]);
    assert_eq!(expected.len(), 6);
    assert_eq!(expected[0]["text"], "before negotiation");
    assert_eq!(expected[1]["id"], "fixture-hello");
    assert_eq!(expected[1]["result"]["protocol_version"], 1);
    assert_eq!(expected[1]["result"]["data_version"], 1);
    assert_eq!(expected[2]["text"], "line one\n雪 \"quoted\" \\ tail");
    assert_eq!(expected[2], expected[3]["event"]);
    assert_eq!(expected[2], expected[4]);
    assert_eq!(expected[3], expected[5]);
    assert_eq!(expected[3]["session_id"], "fixture-session");
    assert_eq!(expected[3]["operation_id"], "fixture-turn-command");
    let mut total = 0;
    for (case, _, _, count) in CAPTURES {
        let bytes = captured_wire(case);
        let expected = decode([bytes.clone()]);
        assert_eq!(expected.len(), *count, "{case}");
        total += expected.len();
        for split in 0..=bytes.len() {
            assert_eq!(
                decode([bytes[..split].to_vec(), bytes[split..].to_vec()]),
                expected,
                "{case} split {split}"
            );
        }
        assert_eq!(
            decode(bytes.iter().map(|byte| vec![*byte])),
            expected,
            "{case} byte chunks"
        );
    }
    assert_eq!(total, 31);
}
#[test]
fn captured_hello_is_unmatched_and_cannot_complete_a_generated_request() {
    let records = decode([captured_wire("negotiated-mirrors")]);
    for pending in [false, true] {
        let mut client = client();
        if pending {
            let request = client.begin_hello().unwrap();
            assert_ne!(request["id"], records[1]["id"]);
        }
        let notice = client.ingest(7, records[1].clone());
        assert!(matches!(notice, Notice::Response { matched: false, .. }));
        assert_eq!(client.pending_count(), usize::from(pending));
        assert!(!client.compatible());
        assert!(!client.ready_for_reads());
    }
}
#[test]
fn literal_negotiated_capture_does_not_negotiate_a_cold_rust_client() {
    let mut client = client();
    let notices: Vec<_> = decode([captured_wire("negotiated-mirrors")])
        .into_iter()
        .map(|value| client.ingest(7, value))
        .collect();
    assert!(matches!(
        notices.as_slice(),
        [
            Notice::LegacyEvent(_),
            Notice::Response { matched: false, .. },
            Notice::LegacyEvent(_),
            Notice::Fault {
                reason: ClientFault::ActivationUncertain,
                ..
            },
            Notice::Quarantined(_),
            Notice::Quarantined(_),
        ]
    ));
    assert!(!client.compatible());
    assert!(!client.ready_for_reads());
}

#[test]
fn captures_preserve_refusal_before_ack_null_provenance_and_terminal_nulls() {
    for name in ["refusal-before-ack-raw", "refusal-before-ack-gui"] {
        let records = decode([captured_wire(name)]);
        assert_eq!(records[0]["type"], "submit_refused");
        assert_eq!(records[0]["reason"], "empty");
        let ack = records.last().unwrap();
        assert_eq!(ack["ok"], true);
        assert_eq!(ack["result"]["turn"], "accepted"); // NOT evidence execution started
        if name.ends_with("gui") {
            assert_eq!(records[1]["event"], records[0]);
            assert_eq!(records[1].get("operation_id"), Some(&Value::Null));
        }
    }
    let records = decode([captured_wire("terminal-null-rates")]);
    assert_eq!(records[0]["type"], "turn_end");
    assert_eq!(records[0]["stopReason"], "cancelled");
    assert_eq!(records[0].get("tps"), Some(&Value::Null));
    assert_eq!(records[0].get("tpsSource"), Some(&Value::Null));
    assert!(records[0].get("turn_id").is_none());
    assert_eq!(records[1]["event"], records[0]);
}
#[test]
fn captures_preserve_operation_ids_and_cached_payload_pairs() {
    let records = decode([captured_wire("operation-context")]);
    assert_eq!(records[1]["operation_id"], "fixture-management-command");
    assert_eq!(records[3]["operation_id"], "fixture-turn-command");
    assert_eq!(records[0], records[1]["event"]);
    assert_eq!(records[2], records[3]["event"]);
    let records = decode([captured_wire("cached-event-mirrors")]);
    assert_eq!(records[0]["usage"]["input_tokens"], 4);
    assert_eq!(records[2]["type"], "tool_approval_requested");
    assert_eq!(records[2]["id"], "fixture-approval");
    assert_eq!(records[2]["profile"], "interactive");
    assert_eq!(records[0], records[1]["event"]);
    assert_eq!(records[2], records[3]["event"]);
}
#[test]
fn captured_direct_errors_and_responses_do_not_forge_correlated_commands() {
    for case in ["reader-framing", "unsupported-protocol", "approval-denial"] {
        let mut client = client();
        let records = decode([captured_wire(case)]);
        for record in &records {
            match client.ingest(7, record.clone()) {
                Notice::DirectError(frame) => {
                    assert_eq!(case, "reader-framing");
                    assert_eq!(frame, *record);
                }
                Notice::Response { id, matched, frame } => {
                    assert!(!matched);
                    assert_eq!(id.unwrap().to_value(), record["id"]);
                    assert_eq!(frame, *record);
                }
                other => panic!("unexpected {case} notice: {other:?}"),
            }
        }
        assert!(!client.compatible());
        assert!(!client.ready_for_reads());
        if case == "reader-framing" {
            assert!(records[..4].iter().all(|record| record["type"] == "error"));
            assert_eq!(records[4]["id"], "fixture-command");
        } else if case == "approval-denial" {
            let ids: Vec<_> = records
                .iter()
                .map(|record| record["id"].as_str().unwrap())
                .collect();
            assert_eq!(
                ids,
                [
                    "fixture-malformed",
                    "fixture-stale",
                    "fixture-deny",
                    "fixture-late"
                ]
            );
            let outcomes: Vec<_> = records
                .iter()
                .map(|record| record["ok"].as_bool().unwrap())
                .collect();
            assert_eq!(outcomes, [false, false, true, false]);
            assert_eq!(records[2]["result"]["allow"], false);
        } else {
            assert_eq!(records[0]["ok"], false);
            assert!(
                records[0]["error"]
                    .as_str()
                    .unwrap()
                    .contains("Unsupported GUI protocol version")
            );
        }
    }
}
