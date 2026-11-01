use litetui_terminal::{
    tool_mode::Profile,
    work_lifecycle::{
        self as work, Capture, CurrentExecution, Effect, Identity, InputState, Ledger, Mode,
        Origin, ProducerState, StorageStatus,
    },
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn id(n: u64) -> String {
    format!("00000000-0000-4000-8000-{n:012x}")
}
fn identity() -> Value {
    json!({"session_id":id(1),"agent_id":id(2),"backend":"custom","model":"model"})
}
fn capture() -> Value {
    json!({"kind":"work_capture","version":1,"instance":"a".repeat(64),"activation_token":"b".repeat(64),"sequence":0,"identity":identity(),"provider":"host","limits":{"producers":256,"inputs":256,"payload_bytes":65536,"ledger_bytes":33554432,"envelope_bytes":131072,"reply_bytes":1048576},"recovered":[],"deferred_jobs":[]})
}
fn parse_capture(v: &Value) -> std::io::Result<Capture> {
    Capture::parse(v, &Identity::parse(&identity()).unwrap())
}
fn active() -> Value {
    json!({"kind":"work_active","version":1,"instance":"a".repeat(64),"sequence":0,"identity":identity()})
}
fn ledger() -> Ledger {
    let mut l = Ledger::new(parse_capture(&capture()).unwrap(), Profile::Strict);
    l.activate(&active()).unwrap();
    l
}
fn producer(n: u64) -> Value {
    json!({"producer_id":id(100+n),"kind":"host_task","object_id":format!("t-{n:032x}"),"producer_session_id":id(1),"owner_instance":"c".repeat(32),"owner_created":"creation-123","generation":1,"causal_action_id":null,"causal_operation_id":null,"causal_execution_id":null})
}
fn payload(n: u64) -> Value {
    json!({"content":format!("Canonical completion {n}"),"source":"harness","tool_profile":"strict"})
}
fn epoch() -> Value {
    json!({"instance":"9".repeat(64),"identity":identity(),"provider":"host"})
}
fn input(n: u64) -> Value {
    json!({"input_id":id(200+n),"producer_id":id(100+n),"payload_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&payload(n)).unwrap())),"source":"harness","requested_profile":"strict","delivery_session_id":id(1),"queue_revision":n+1})
}
fn origin(n: u64) -> Value {
    json!({"session_id":id(1),"operation_id":id(300+n),"hook_id":id(400+n),"source":"harness","execution_id":id(500+n)})
}
fn admission(n: u64) -> Value {
    json!({"admission_id":id(600+n),"input_id":id(200+n),"mode":"new_turn","operation_id":id(300+n),"origin":origin(n),"parent_execution_id":null,"boundary":0,"requested_profile":"strict","effective_profile":"strict","seat_profile":"strict","provider":"host","provider_delivery":null})
}
fn producer_event(n: u64, phase: &str) -> Value {
    json!({"type":"work_producer","phase":phase,"producer":producer(n),"worker_id":if phase=="registered"{Value::Null}else{json!(id(700+n))},"task_state":if phase=="retired"{"done"}else{"running"},"reason":null,"proof":if phase=="retired"{json!({"worker_retired":true,"callback_retired":true,"process_retired":true,"persistence":{"task":{"status":"verified","reason":null},"output":{"status":"verified","reason":null}}})}else{Value::Null}})
}
fn input_event(n: u64, phase: &str) -> Value {
    json!({"type":"work_input","phase":phase,"input":input(n),"reason":if matches!(phase,"queued"|"claiming"){Value::Null}else{json!("Retained canonical disposition")}})
}
fn admission_event(n: u64) -> Value {
    json!({"type":"work_admission","status":"accepted","input":input(n),"admission":admission(n),"payload":payload(n)})
}
fn settled_event(n: u64) -> Value {
    json!({"type":"work_settled","input":input(n),"admission":admission(n),"outcome":"completed","proof":{"worker_retired":true,"callbacks_retired":true,"streams_retired":true,"input_durable":true},"history_sha256":"e".repeat(64),"ledger_sha256":"f".repeat(64)})
}
fn envelope(sequence: u64, mut event: Value) -> Value {
    event["version"] = json!(1);
    event["instance"] = json!("a".repeat(64));
    event["sequence"] = json!(sequence);
    event["identity"] = identity();
    let mut v = if matches!(
        event["type"].as_str(),
        Some("work_admission" | "work_settled")
    ) {
        event["admission"]["origin"].clone()
    } else if event["type"] == "work_reconciled" {
        json!({"session_id":id(1),"operation_id":event["reconciliation"]["operation_id"],"hook_id":null,"source":"rpc","execution_id":null})
    } else {
        json!({"session_id":id(1),"operation_id":null,"hook_id":null,"source":"work","execution_id":null})
    };
    v["type"] = json!("gui.event");
    v["event_id"] = json!(id(9000 + sequence));
    v["event"] = event;
    v
}
fn emit(l: &mut Ledger, e: Value) -> Effect {
    l.observe(&envelope(l.sequence() + 1, e), None).unwrap()
}
fn running(l: &mut Ledger, n: u64) {
    emit(l, producer_event(n, "registered"));
    emit(l, producer_event(n, "running"));
}
fn claiming(l: &mut Ledger, n: u64) {
    running(l, n);
    emit(l, input_event(n, "queued"));
    emit(l, input_event(n, "claiming"));
}
fn close_event(producers: &[u64], inputs: &[u64], admissions: &[u64], sequence: u64) -> Value {
    json!({"type":"work_closing","cutoff_sequence":sequence,"producer_ids":producers.iter().map(|n|id(100+n)).collect::<Vec<_>>(),"input_ids":inputs.iter().map(|n|id(200+n)).collect::<Vec<_>>(),"admission_ids":admissions.iter().map(|n|id(600+n)).collect::<Vec<_>>()})
}
fn shutdown(
    closing: u64,
    last: u64,
    producers: &[u64],
    inputs: &[(u64, &str)],
    admissions: &[u64],
) -> Value {
    json!({"version":1,"instance":"a".repeat(64),"closing_sequence":closing,"final_sequence":last,"admission_closed":true,"worker_settled":true,"callback_settled":true,"process_settled":true,"producer_ids":producers.iter().map(|n|id(100+n)).collect::<Vec<_>>(),"inputs":inputs.iter().map(|(n,s)|json!({"input_id":id(200+n),"disposition":s})).collect::<Vec<_>>(),"admission_ids":admissions.iter().map(|n|id(600+n)).collect::<Vec<_>>(),"ledger_sha256":"f".repeat(64),"effects_verified":false})
}

#[test]
fn capture_and_activation_bind_exact_identity_capacity_and_one_epoch() {
    let c = parse_capture(&capture()).unwrap();
    let cmd = c.activation_request();
    assert_eq!(cmd["type"], work::ACTIVATE);
    assert_eq!(cmd["activation_token"], "b".repeat(64));
    assert_eq!(cmd["session_id"], id(1));
    let mut l = Ledger::new(c, Profile::Strict);
    assert!(
        l.observe(&envelope(1, producer_event(0, "registered")), None)
            .is_err()
    );
    assert!(l.failed());
    assert!(l.activate(&active()).is_err());
    let mut l = ledger();
    assert!(l.activate(&active()).is_err());
}
#[test]
fn capture_rejects_unknown_wrong_typed_and_unbounded_fields() {
    for fault in [
        "extra", "bool", "identity", "instance", "limit", "provider", "sequence", "oversize",
        "missing",
    ] {
        let mut v = capture();
        match fault {
            "extra" => v["surprise"] = json!(true),
            "bool" => v["version"] = json!(true),
            "identity" => v["identity"]["agent_id"] = json!(id(3)),
            "instance" => v["instance"] = json!("A".repeat(64)),
            "limit" => v["limits"]["inputs"] = json!(257),
            "provider" => v["provider"] = json!("external"),
            "sequence" => v["sequence"] = json!(1),
            "oversize" => v["identity"]["model"] = json!("m".repeat(1025)),
            "missing" => {
                v.as_object_mut().unwrap().remove("recovered");
            }
            _ => unreachable!(),
        }
        assert!(parse_capture(&v).is_err(), "{fault}");
    }
}
#[test]
fn model_identity_preserves_the_existing_1024_byte_budget() {
    let mut v = capture();
    v["identity"]["model"] = json!("é".repeat(512));
    let expected = Identity::parse(&v["identity"]).unwrap();
    assert!(Capture::parse(&v, &expected).is_ok());
    v["identity"]["model"] = json!("é".repeat(513));
    assert!(Identity::parse(&v["identity"]).is_err());
}
#[test]
fn complete_new_turn_is_preannounced_and_receipt_is_not_applied_twice() {
    let mut l = ledger();
    claiming(&mut l, 0);
    let Effect::Admission {
        input,
        admission,
        drain_only,
        ..
    } = emit(&mut l, admission_event(0))
    else {
        panic!()
    };
    assert_eq!(input.id, id(200));
    assert_eq!(admission.mode, Mode::NewTurn);
    assert!(!drain_only);
    assert_eq!(
        l.settled_request(&admission.id).unwrap()["admission_id"],
        id(600)
    );
    let wire = envelope(l.sequence() + 1, settled_event(0));
    let receipt = wire["event"].clone();
    assert!(l.verify_settled(&receipt).is_err());
    l.observe(&wire, None).unwrap();
    let seq = l.sequence();
    l.verify_settled(&receipt).unwrap();
    assert_eq!(l.sequence(), seq);
    assert!(l.observe(&wire, None).is_err());
}
#[test]
fn bad_event_is_atomic_and_poisoned_against_later_admission() {
    for fault in [
        "sequence",
        "instance",
        "identity",
        "event_id",
        "version",
        "extra",
        "envelope_source",
        "envelope_operation",
        "oversize",
    ] {
        let mut l = ledger();
        let mut e = envelope(1, producer_event(0, "registered"));
        match fault {
            "sequence" => e["event"]["sequence"] = json!(2),
            "instance" => e["event"]["instance"] = json!("f".repeat(64)),
            "identity" => e["event"]["identity"]["session_id"] = json!(id(7)),
            "event_id" => e["event_id"] = json!("not-an-emission"),
            "version" => e["event"]["version"] = json!(true),
            "extra" => e["event"]["surprise"] = json!(1),
            "envelope_source" => e["source"] = json!("rpc"),
            "envelope_operation" => e["operation_id"] = json!("last-prompt"),
            "oversize" => e["padding"] = json!("x".repeat(work::ENVELOPE_BYTES)),
            _ => unreachable!(),
        }
        assert!(l.observe(&e, None).is_err(), "{fault}");
        assert_eq!(l.sequence(), 0);
        assert!(l.failed());
        assert!(
            l.observe(&envelope(1, producer_event(0, "registered")), None)
                .is_err()
        );
    }
}
#[test]
fn unregistered_input_or_admission_cannot_borrow_another_producer() {
    for e in [
        input_event(0, "queued"),
        admission_event(0),
        producer_event(0, "running"),
        settled_event(0),
    ] {
        let mut l = ledger();
        assert!(l.observe(&envelope(1, e), None).is_err());
    }
    let mut l = ledger();
    claiming(&mut l, 0);
    let mut e = admission_event(0);
    e["input"]["producer_id"] = json!(id(101));
    assert!(l.observe(&envelope(5, e), None).is_err());
}
#[test]
fn producer_and_input_references_are_immutable_and_one_completion_per_invocation() {
    for fault in ["producer", "input", "second"] {
        let mut l = ledger();
        running(&mut l, 0);
        let e = if fault == "producer" {
            let mut e = producer_event(0, "retired");
            e["producer"]["owner_instance"] = json!("foreign");
            e
        } else {
            emit(&mut l, input_event(0, "queued"));
            let mut e = input_event(
                0,
                if fault == "second" {
                    "queued"
                } else {
                    "claiming"
                },
            );
            if fault == "second" {
                e["input"]["input_id"] = json!(id(999));
            } else {
                e["input"]["payload_sha256"] = json!("e".repeat(64));
            }
            e
        };
        assert!(
            l.observe(&envelope(l.sequence() + 1, e), None).is_err(),
            "{fault}"
        );
    }
}
#[test]
fn concurrent_completions_have_independent_claims_and_exact_stream_origins() {
    let mut l = ledger();
    running(&mut l, 0);
    running(&mut l, 1);
    emit(&mut l, input_event(1, "queued"));
    emit(&mut l, input_event(0, "queued"));
    emit(&mut l, input_event(1, "claiming"));
    let Effect::Admission { admission: a, .. } = emit(&mut l, admission_event(1)) else {
        panic!()
    };
    let current = CurrentExecution {
        origin: a.origin,
        action_id: None,
    };
    emit(&mut l, input_event(0, "claiming"));
    let mut e = admission_event(0);
    e["admission"]["mode"] = json!("in_turn");
    e["admission"]["origin"] = current.origin.value();
    e["admission"]["operation_id"] = json!(current.origin.operation);
    e["admission"]["parent_execution_id"] = json!(current.origin.execution);
    e["admission"]["boundary"] = json!(1);
    let Effect::Admission { admission: a, .. } = l
        .observe(&envelope(l.sequence() + 1, e), Some(&current))
        .unwrap()
    else {
        panic!()
    };
    assert_eq!(a.mode, Mode::InTurn);
    assert_eq!(a.origin, current.origin);
    assert_eq!(a.input, id(200));
}
#[test]
fn in_turn_admission_requires_exact_parent_origin_and_monotonic_safe_boundary() {
    for fault in [
        "missing",
        "parent",
        "operation",
        "source",
        "hook",
        "boundary",
    ] {
        let mut l = ledger();
        claiming(&mut l, 0);
        let current = CurrentExecution {
            origin: Origin::parse(&origin(9)).unwrap(),
            action_id: None,
        };
        let mut e = admission_event(0);
        e["admission"]["mode"] = json!("in_turn");
        e["admission"]["origin"] = current.origin.value();
        e["admission"]["operation_id"] = json!(current.origin.operation);
        e["admission"]["parent_execution_id"] = json!(current.origin.execution);
        e["admission"]["boundary"] = json!(1);
        match fault {
            "parent" => e["admission"]["parent_execution_id"] = json!(id(999)),
            "operation" => e["admission"]["origin"]["operation_id"] = json!(id(998)),
            "source" => e["admission"]["origin"]["source"] = json!("rpc"),
            "hook" => e["admission"]["origin"]["hook_id"] = json!(id(997)),
            "boundary" => e["admission"]["boundary"] = json!(2),
            _ => {}
        }
        assert!(
            l.observe(
                &envelope(5, e),
                if fault == "missing" {
                    None
                } else {
                    Some(&current)
                }
            )
            .is_err(),
            "{fault}"
        );
    }
}
#[test]
fn action_segment_requires_exact_causal_action_not_copied_context() {
    for valid in [true, false] {
        let mut l = ledger();
        let current = CurrentExecution {
            origin: Origin::parse(&origin(9)).unwrap(),
            action_id: Some(id(800)),
        };
        for phase in ["registered", "running"] {
            let mut e = producer_event(0, phase);
            e["producer"]["causal_action_id"] = json!(if valid { id(800) } else { id(801) });
            e["producer"]["causal_operation_id"] = json!(current.origin.operation);
            e["producer"]["causal_execution_id"] = json!(current.origin.execution);
            emit(&mut l, e);
        }
        emit(&mut l, input_event(0, "queued"));
        emit(&mut l, input_event(0, "claiming"));
        let mut e = admission_event(0);
        e["admission"]["mode"] = json!("action_segment");
        e["admission"]["operation_id"] = json!(current.origin.operation);
        e["admission"]["origin"]["operation_id"] = json!(current.origin.operation);
        e["admission"]["origin"]["source"] = json!("rpc");
        e["admission"]["parent_execution_id"] = json!(current.origin.execution);
        assert_eq!(l.observe(&envelope(5, e), Some(&current)).is_ok(), valid);
    }
}
#[test]
fn held_refusal_preserves_identity_but_uncertainty_cannot_be_retried() {
    let mut l = ledger();
    claiming(&mut l, 0);
    emit(&mut l, input_event(0, "held"));
    emit(&mut l, input_event(0, "claiming"));
    emit(&mut l, input_event(0, "uncertain"));
    assert!(
        l.observe(
            &envelope(l.sequence() + 1, input_event(0, "claiming")),
            None
        )
        .is_err()
    );
}
#[test]
fn accepted_admission_needs_original_authority_and_real_native_delivery() {
    for fault in [
        "input",
        "source",
        "requested",
        "effective",
        "seat",
        "host_delivery",
        "provider",
        "claim",
    ] {
        let mut l = ledger();
        if fault == "claim" {
            running(&mut l, 0);
            emit(&mut l, input_event(0, "queued"));
        } else {
            claiming(&mut l, 0);
        }
        let mut e = admission_event(0);
        match fault {
            "input" => e["admission"]["input_id"] = json!(id(99)),
            "source" => e["admission"]["origin"]["source"] = json!("rpc"),
            "requested" => e["admission"]["requested_profile"] = json!("interactive"),
            "effective" => e["admission"]["effective_profile"] = json!("autonomous"),
            "seat" => e["admission"]["seat_profile"] = json!("autonomous"),
            "host_delivery" => {
                e["admission"]["provider_delivery"] = json!({"id":"invented","state":"accepted"})
            }
            "provider" => e["admission"]["provider"] = json!("codex"),
            _ => {}
        }
        assert!(
            l.observe(&envelope(l.sequence() + 1, e), None).is_err(),
            "{fault}"
        );
    }
}
#[test]
fn optional_task_output_failure_is_honest_and_does_not_suppress_completion() {
    let mut l = ledger();
    running(&mut l, 0);
    emit(&mut l, input_event(0, "queued"));
    let mut e = producer_event(0, "retired");
    e["proof"]["persistence"]["output"] = json!({"status":"failed","reason":"disk full"});
    let Effect::Producer {
        state,
        storage: Some(storage),
        ..
    } = emit(&mut l, e)
    else {
        panic!()
    };
    assert_eq!(state, ProducerState::Retired);
    assert_eq!(storage.output, StorageStatus::Failed("disk full".into()));
    emit(&mut l, input_event(0, "claiming"));
    assert!(matches!(
        emit(&mut l, admission_event(0)),
        Effect::Admission { .. }
    ));
}
#[test]
fn task_row_killed_or_missing_callback_proof_is_not_retirement() {
    for key in ["worker_retired", "callback_retired", "process_retired"] {
        let mut l = ledger();
        running(&mut l, 0);
        let mut e = producer_event(0, "retired");
        e["task_state"] = json!("killed");
        e["proof"][key] = json!(false);
        assert!(l.observe(&envelope(3, e), None).is_err());
    }
}
#[test]
fn cancelled_before_worker_launch_has_no_invented_worker_or_input() {
    let mut l = ledger();
    emit(&mut l, producer_event(0, "registered"));
    let mut e = producer_event(0, "retired");
    e["worker_id"] = Value::Null;
    e["reason"] = json!("Cancelled before launch");
    e["task_state"] = json!("killed");
    emit(&mut l, e);
    assert!(
        l.observe(&envelope(3, input_event(0, "held")), None)
            .is_err()
    );
}
#[test]
fn local_quit_drains_buffered_admission_until_source_barrier() {
    let mut l = ledger();
    claiming(&mut l, 0);
    l.begin_close();
    let Effect::Admission { drain_only, .. } = emit(&mut l, admission_event(0)) else {
        panic!()
    };
    assert!(drain_only);
    assert!(l.settled_request(&id(600)).is_err());
    emit(&mut l, close_event(&[0], &[0], &[0], 6));
    emit(&mut l, settled_event(0));
    emit(&mut l, producer_event(0, "retired"));
    l.verify_shutdown(&shutdown(6, 8, &[0], &[(0, "settled")], &[0]))
        .unwrap();
    assert!(
        l.observe(&envelope(9, producer_event(1, "registered")), None)
            .is_err()
    );
}
#[test]
fn closing_source_accepts_only_held_late_completions_from_exact_cutoff_producers() {
    let mut l = ledger();
    running(&mut l, 0);
    l.begin_close();
    emit(&mut l, close_event(&[0], &[], &[], 3));
    emit(&mut l, input_event(0, "held"));
    emit(&mut l, producer_event(0, "retired"));
    l.verify_shutdown(&shutdown(3, 5, &[0], &[(0, "held")], &[]))
        .unwrap();
    for phase in ["queued", "claiming"] {
        let mut l = ledger();
        running(&mut l, 0);
        l.begin_close();
        emit(&mut l, close_event(&[0], &[], &[], 3));
        assert!(
            l.observe(&envelope(4, input_event(0, phase)), None)
                .is_err()
        );
    }
}
#[test]
fn source_barrier_rejects_new_admission_even_when_local_quit_was_not_observed() {
    let mut l = ledger();
    claiming(&mut l, 0);
    emit(&mut l, close_event(&[0], &[0], &[], 5));
    assert!(l.observe(&envelope(6, admission_event(0)), None).is_err());
}
#[test]
fn close_and_shutdown_sets_must_cover_exact_outstanding_objects() {
    for fault in ["producer", "input", "admission"] {
        let mut l = ledger();
        claiming(&mut l, 0);
        emit(&mut l, admission_event(0));
        let mut e = close_event(&[0], &[0], &[0], 6);
        e[format!("{fault}_ids")] = json!([]);
        assert!(l.observe(&envelope(6, e), None).is_err(), "{fault}");
    }
    for fault in [
        "missing",
        "unknown",
        "duplicate",
        "state",
        "sequence",
        "effects",
        "worker",
    ] {
        let mut l = ledger();
        claiming(&mut l, 0);
        l.begin_close();
        emit(&mut l, close_event(&[0], &[0], &[], 5));
        let mut v = shutdown(5, 5, &[0], &[(0, "held")], &[]);
        match fault {
            "missing" => v["inputs"] = json!([]),
            "unknown" => v["inputs"][0]["input_id"] = json!(id(999)),
            "duplicate" => {
                let row = v["inputs"][0].clone();
                v["inputs"].as_array_mut().unwrap().push(row);
            }
            "state" => v["inputs"][0]["disposition"] = json!("settled"),
            "sequence" => v["final_sequence"] = json!(6),
            "effects" => v["effects_verified"] = json!(true),
            "worker" => v["worker_settled"] = json!(false),
            _ => {}
        }
        assert!(l.verify_shutdown(&v).is_err(), "{fault}");
    }
}
#[test]
fn recovered_uncertain_admission_is_evidence_not_a_live_worker_or_replay_grant() {
    let mut c = capture();
    c["recovered"] = json!([{"epoch":epoch(),"producer":producer(0),"input":input(0),"state":"uncertain","reason":"Previous append uncertain","admission":admission(0)}]);
    let mut l = Ledger::new(parse_capture(&c).unwrap(), Profile::Strict);
    l.activate(&active()).unwrap();
    assert!(l.settled_request(&id(600)).is_err());
    l.begin_close();
    emit(&mut l, close_event(&[], &[0], &[], 1));
    l.verify_shutdown(&shutdown(1, 1, &[], &[(0, "uncertain")], &[]))
        .unwrap();
    let mut l = Ledger::new(parse_capture(&c).unwrap(), Profile::Strict);
    l.activate(&active()).unwrap();
    assert!(
        l.observe(&envelope(1, input_event(0, "claiming")), None)
            .is_err()
    );
}
#[test]
fn settlement_cannot_rebind_or_claim_unretired_work() {
    for fault in [
        "admission",
        "input",
        "worker",
        "streams",
        "digest",
        "outcome",
    ] {
        let mut l = ledger();
        claiming(&mut l, 0);
        emit(&mut l, admission_event(0));
        let mut e = settled_event(0);
        match fault {
            "admission" => e["admission"]["admission_id"] = json!(id(999)),
            "input" => e["input"]["payload_sha256"] = json!("c".repeat(64)),
            "worker" => e["proof"]["worker_retired"] = json!(false),
            "streams" => e["proof"]["streams_retired"] = json!(false),
            "digest" => e["history_sha256"] = json!("unknown"),
            "outcome" => e["outcome"] = json!("success"),
            _ => {}
        }
        assert!(l.observe(&envelope(6, e), None).is_err(), "{fault}");
    }
}
#[test]
fn held_and_queued_dispositions_are_not_terminal_foreground_results() {
    let mut l = ledger();
    running(&mut l, 0);
    let Effect::Input { state, .. } = emit(&mut l, input_event(0, "queued")) else {
        panic!()
    };
    assert_eq!(state, InputState::Queued);
    let Effect::Input { state, .. } = emit(&mut l, input_event(0, "held")) else {
        panic!()
    };
    assert_eq!(state, InputState::Held);
}

#[test]
fn a_new_turn_cannot_reuse_a_retired_or_recovered_operation() {
    for recovered in [false, true] {
        let mut l = if recovered {
            let mut c = capture();
            c["recovered"] = json!([{"epoch":epoch(),"producer":producer(0),"input":input(0),"state":"uncertain","reason":"Original admission uncertain","admission":admission(0)}]);
            let mut l = Ledger::new(parse_capture(&c).unwrap(), Profile::Strict);
            l.activate(&active()).unwrap();
            l
        } else {
            let mut l = ledger();
            claiming(&mut l, 0);
            emit(&mut l, admission_event(0));
            emit(&mut l, settled_event(0));
            emit(&mut l, producer_event(0, "retired"));
            l
        };
        claiming(&mut l, 1);
        let mut event = admission_event(1);
        event["admission"]["operation_id"] = json!(id(300));
        event["admission"]["origin"]["operation_id"] = json!(id(300));
        assert!(
            l.observe(&envelope(l.sequence() + 1, event), None).is_err(),
            "recovered={recovered}"
        );
    }
}
#[test]
fn new_turn_operation_is_distinct_from_its_causal_user_operation() {
    let mut l = ledger();
    for phase in ["registered", "running"] {
        let mut e = producer_event(0, phase);
        e["producer"]["causal_operation_id"] = json!(id(300));
        e["producer"]["causal_execution_id"] = json!(id(888));
        emit(&mut l, e);
    }
    emit(&mut l, input_event(0, "queued"));
    emit(&mut l, input_event(0, "claiming"));
    assert!(
        l.observe(&envelope(l.sequence() + 1, admission_event(0)), None)
            .is_err()
    );
}
#[test]
fn complete_envelope_and_hook_identity_are_exact() {
    let mut l = ledger();
    let mut e = envelope(1, producer_event(0, "registered"));
    e["extra"] = json!(1);
    assert!(l.observe(&e, None).is_err());
    let mut l = ledger();
    claiming(&mut l, 0);
    let mut e = admission_event(0);
    e["admission"]["origin"]["hook_id"] = json!("not-a-uuid");
    assert!(l.observe(&envelope(5, e), None).is_err());
}
#[test]
fn completed_lifetimes_do_not_consume_active_slot_capacity_forever() {
    let mut l = ledger();
    for n in 0..257 {
        claiming(&mut l, n);
        emit(&mut l, admission_event(n));
        emit(&mut l, settled_event(n));
        emit(&mut l, producer_event(n, "retired"));
    }
    assert_eq!(l.sequence(), 257 * 7);
    // Historical IDs remain protected even though their active slot was freed.
    assert!(
        l.observe(
            &envelope(l.sequence() + 1, producer_event(0, "registered")),
            None
        )
        .is_err()
    );
}
#[test]
fn pending_input_slots_are_reserved_before_another_producer_runs() {
    let mut l = ledger();
    for n in 0..256 {
        running(&mut l, n);
        emit(&mut l, input_event(n, "held"));
        emit(&mut l, producer_event(n, "retired"));
    }
    assert!(
        l.observe(
            &envelope(l.sequence() + 1, producer_event(256, "registered")),
            None
        )
        .is_err()
    );
}
#[test]
fn a_retained_worker_cannot_be_claimed_by_another_producer() {
    let mut l = ledger();
    running(&mut l, 0);
    emit(&mut l, producer_event(1, "registered"));
    let mut e = producer_event(1, "running");
    e["worker_id"] = json!(id(700));
    assert!(l.observe(&envelope(4, e), None).is_err());
}
#[test]
fn uncertainty_after_accepted_admission_retains_cleanup_identity_without_replay() {
    let mut l = ledger();
    claiming(&mut l, 0);
    emit(&mut l, admission_event(0));
    let Effect::Input { state, .. } = emit(&mut l, input_event(0, "uncertain")) else {
        panic!()
    };
    assert_eq!(state, InputState::Uncertain);
    l.begin_close();
    emit(&mut l, close_event(&[0], &[0], &[0], 7));
    emit(&mut l, producer_event(0, "retired"));
    l.verify_shutdown(&shutdown(7, 8, &[0], &[(0, "uncertain")], &[0]))
        .unwrap();
}

#[test]
fn exact_known_worker_retirement_can_resolve_post_admission_uncertainty_without_dispatch() {
    let mut l = ledger();
    claiming(&mut l, 0);
    emit(&mut l, admission_event(0));
    emit(&mut l, input_event(0, "uncertain"));
    let wire = envelope(l.sequence() + 1, settled_event(0));
    assert!(matches!(
        l.observe(&wire, None).unwrap(),
        Effect::Settled { .. }
    ));
    l.verify_settled(&wire["event"]).unwrap();
    assert!(
        l.observe(
            &envelope(l.sequence() + 1, input_event(0, "claiming")),
            None
        )
        .is_err()
    );
}

fn claim_with_payload(l: &mut Ledger, value: &Value, digest: &str) -> Value {
    running(l, 0);
    for phase in ["queued", "claiming"] {
        let mut event = input_event(0, phase);
        event["input"]["payload_sha256"] = json!(digest);
        emit(l, event);
    }
    let mut event = admission_event(0);
    event["payload"] = value.clone();
    event["input"]["payload_sha256"] = json!(digest);
    event
}
#[test]
fn payload_digest_matches_python_sorted_utf8_with_exact_display_text() {
    let value = json!({"content":"Résultat\n\t\"quote\" \\ café 🙂 \u{2028}","text":"",
        "source":"harness","tool_profile":"strict"});
    // Independently computed by Python json.dumps(ensure_ascii=False, sort_keys=True,
    // separators=(',', ':')) and hashlib.sha256.
    let digest = "0152ba889ebc4a3b7e98f9559484f2f2637d5c34eaf171fc64aaca68e391ed12";
    assert_eq!(
        format!("{:x}", Sha256::digest(serde_json::to_vec(&value).unwrap())),
        digest
    );
    let mut l = ledger();
    let event = claim_with_payload(&mut l, &value, digest);
    let Effect::Admission { payload, .. } = emit(&mut l, event) else {
        panic!()
    };
    assert_eq!(payload.content, value["content"].as_str().unwrap());
    assert_eq!(payload.display_text(), "");
}
#[test]
fn payload_mutation_type_source_profile_and_unknown_fields_are_rejected() {
    for fault in [
        "digest",
        "extra",
        "missing",
        "empty",
        "display-null",
        "display-array",
        "source",
        "profile",
    ] {
        let mut value = payload(0);
        match fault {
            "digest" => value["content"] = json!("Changed content"),
            "extra" => value["unexpected"] = json!(true),
            "missing" => {
                value.as_object_mut().unwrap().remove("content");
            }
            "empty" => value["content"] = json!(""),
            "display-null" => value["text"] = Value::Null,
            "display-array" => value["text"] = json!([]),
            "source" => value["source"] = json!("scheduled"),
            "profile" => value["tool_profile"] = json!("autonomous"),
            _ => unreachable!(),
        }
        let digest = if fault == "digest" {
            input(0)["payload_sha256"].as_str().unwrap().to_owned()
        } else {
            format!("{:x}", Sha256::digest(serde_json::to_vec(&value).unwrap()))
        };
        let mut l = ledger();
        let event = claim_with_payload(&mut l, &value, &digest);
        assert!(
            l.observe(&envelope(l.sequence() + 1, event), None).is_err(),
            "{fault}"
        );
    }
}
#[test]
fn full_64k_payload_fits_new_envelope_but_one_extra_payload_byte_is_rejected() {
    let mut value = payload(0);
    value["content"] = json!("");
    let overhead = serde_json::to_vec(&value).unwrap().len();
    for extra in [0, 1] {
        value["content"] = json!("x".repeat(work::PAYLOAD_BYTES - overhead + extra));
        let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&value).unwrap()));
        let mut l = ledger();
        let event = claim_with_payload(&mut l, &value, &digest);
        let frame = envelope(l.sequence() + 1, event);
        assert!(serde_json::to_vec(&frame).unwrap().len() > 65536);
        assert_eq!(l.observe(&frame, None).is_ok(), extra == 0);
    }
}
#[test]
fn recovered_epoch_preserves_historical_provider_across_same_owner_backend_change() {
    let mut c = capture();
    c["identity"]["backend"] = json!("codex");
    c["identity"]["model"] = json!("new-model");
    c["provider"] = json!("codex");
    c["recovered"] = json!([{"epoch":epoch(),"producer":producer(0),"input":input(0),
        "state":"uncertain","reason":"Prior host append uncertain","admission":admission(0)}]);
    let expected = Identity::parse(&c["identity"]).unwrap();
    let capture = Capture::parse(&c, &expected).unwrap();
    assert_eq!(capture.recovered[0].epoch.identity.backend, "custom");
    assert_eq!(capture.recovered[0].epoch.provider, work::Provider::Host);
    let l = Ledger::new(capture, Profile::Strict);
    assert!(l.settled_request(&id(600)).is_err());
}
#[test]
fn recovered_epoch_requires_original_owner_and_matching_historic_admission_provider() {
    for fault in [
        "missing", "instance", "session", "agent", "provider", "extra",
    ] {
        let mut c = capture();
        c["recovered"] = json!([{"epoch":epoch(),"producer":producer(0),"input":input(0),
            "state":"uncertain","reason":"Prior append uncertain","admission":admission(0)}]);
        let e = &mut c["recovered"][0]["epoch"];
        match fault {
            "missing" => *e = Value::Null,
            "instance" => e["instance"] = json!("old"),
            "session" => e["identity"]["session_id"] = json!(id(90)),
            "agent" => e["identity"]["agent_id"] = json!(id(91)),
            "provider" => e["provider"] = json!("codex"),
            "extra" => e["extra"] = json!(true),
            _ => unreachable!(),
        }
        assert!(parse_capture(&c).is_err(), "{fault}");
    }
}
fn deferred_job() -> Value {
    let job = r#"{"id":"job-a","prompt":"Retained scheduled prompt"}"#;
    let mut old_epoch = epoch();
    old_epoch["identity"]["session_id"] = json!(id(9));
    let mut old_shutdown = shutdown(1, 1, &[], &[], &[]);
    old_shutdown["instance"] = old_epoch["instance"].clone();
    json!({"invocation_id":id(880),"job_id":"job-a","kind":"cron","generation":1,
        "source":"scheduled","requested_profile":"strict",
        "payload":{"content":"Retained scheduled prompt","source":"scheduled","tool_profile":"strict"},
        "job_snapshot":job,"job_sha256":format!("{:x}",Sha256::digest(job.as_bytes())),
        "epoch":old_epoch,"target":{"session_id":id(1),"agent_id":id(2)},
        "shutdown":old_shutdown,"state":"held","reason":"Staged before prior runtime retirement"})
}
#[test]
fn deferred_job_is_bounded_cross_session_evidence_without_live_ownership() {
    let mut c = capture();
    c["deferred_jobs"] = json!([deferred_job()]);
    let capture = parse_capture(&c).unwrap();
    assert_eq!(capture.deferred_jobs[0].epoch.identity.session, id(9));
    assert_eq!(capture.deferred_jobs[0].target_session, id(1));
    let mut l = Ledger::new(capture, Profile::Strict);
    l.activate(&active()).unwrap();
    l.begin_close();
    emit(&mut l, close_event(&[], &[], &[], 1));
    l.verify_shutdown(&shutdown(1, 1, &[], &[], &[])).unwrap();
}
#[test]
fn deferred_reconciled_input_is_historical_and_never_live_admission() {
    for disposition in ["settled", "held", "cancelled", "uncertain", "reconciled"] {
        let mut row = deferred_job();
        row["shutdown"]["inputs"] = json!([{"input_id":id(200),"disposition":disposition}]);
        let mut c = capture();
        c["deferred_jobs"] = json!([row]);
        let capture = parse_capture(&c).unwrap();
        assert_eq!(capture.deferred_jobs[0].epoch.identity.session, id(9));
        let mut l = Ledger::new(capture, Profile::Strict);
        l.activate(&active()).unwrap();
        assert!(l.settled_request(&id(600)).is_err());
        l.begin_close();
        emit(&mut l, close_event(&[], &[], &[], 1));
        l.verify_shutdown(&shutdown(1, 1, &[], &[], &[])).unwrap();
    }
}
#[test]
fn deferred_retirement_rejects_unknown_dispositions_and_duplicate_input_ids() {
    for disposition in [
        json!("admitted"),
        json!("queued"),
        json!("Reconciled"),
        json!(""),
        Value::Null,
        json!(true),
    ] {
        let mut row = deferred_job();
        row["shutdown"]["inputs"] = json!([{"input_id":id(200),"disposition":disposition}]);
        let mut c = capture();
        c["deferred_jobs"] = json!([row]);
        assert!(parse_capture(&c).is_err());
    }
    let mut row = deferred_job();
    row["shutdown"]["inputs"] = json!([
        {"input_id":id(200),"disposition":"reconciled"},
        {"input_id":id(200),"disposition":"reconciled"}]);
    let mut c = capture();
    c["deferred_jobs"] = json!([row]);
    assert!(parse_capture(&c).is_err());
}
#[test]
fn deferred_job_rejects_foreign_target_changed_job_and_fabricated_cleanup() {
    for fault in [
        "count",
        "target",
        "agent",
        "epoch-agent",
        "digest",
        "source",
        "profile",
        "state",
        "proof-null",
        "proof-worker",
        "proof-instance",
        "proof-extra",
        "kind",
        "generation",
    ] {
        let mut row = deferred_job();
        match fault {
            "target" => row["target"]["session_id"] = json!(id(9)),
            "agent" => row["target"]["agent_id"] = json!(id(8)),
            "epoch-agent" => row["epoch"]["identity"]["agent_id"] = json!(id(8)),
            "digest" => row["job_snapshot"] = json!("{}"),
            "source" => row["source"] = json!("harness"),
            "profile" => row["payload"]["tool_profile"] = json!("autonomous"),
            "state" => row["state"] = json!("admitted"),
            "proof-null" => row["shutdown"] = Value::Null,
            "proof-worker" => row["shutdown"]["worker_settled"] = json!(false),
            "proof-instance" => row["shutdown"]["instance"] = json!("a".repeat(64)),
            "proof-extra" => row["shutdown"]["unknown"] = json!(true),
            "kind" => row["kind"] = json!("host_task"),
            "generation" => row["generation"] = json!(0),
            _ => {}
        }
        let mut c = capture();
        c["deferred_jobs"] = if fault == "count" {
            json!([row.clone(), row])
        } else {
            json!([row])
        };
        assert!(parse_capture(&c).is_err(), "{fault}");
    }
}
#[test]
#[ignore = "requires explicit actual-source exported fixture directory"]
fn source_generated_lifecycle_fixtures_match_native_contract() {
    replay_source_fixtures(8, false);
}
#[test]
#[ignore = "requires explicit actual-source provider fixture directory"]
fn source_generated_provider_fixtures_match_native_contract() {
    replay_source_fixtures(7, true);
}
fn replay_source_fixtures(minimum: usize, require_reconciliations: bool) {
    let dir = std::env::var("LITETUI_WORK_LIFECYCLE_FIXTURES")
        .expect("explicit source fixture directory");
    let mut paths = std::fs::read_dir(&dir)
        .unwrap()
        .map(|e| e.unwrap().path())
        .filter(|p| p.extension().is_some_and(|e| e == "json"))
        .collect::<Vec<_>>();
    paths.sort();
    assert!(
        paths.len() >= minimum,
        "actual source matrix lacks its required scenarios"
    );
    let mut totals = (0, 0, 0);
    let mut reconciliation_totals = (0, 0);
    for path in paths {
        let bytes = std::fs::read(&path).unwrap();
        let doc: Value = serde_json::from_slice(&bytes).unwrap();
        let expected = Identity::parse(&doc["capture"]["identity"]).unwrap();
        let c = Capture::parse(&doc["capture"], &expected)
            .unwrap_or_else(|e| panic!("{}: {e}", path.display()));
        let mut l = Ledger::new(c, Profile::Strict);
        l.activate(&doc["active"]).unwrap();
        let authority = doc["authority"]
            .as_array()
            .expect("independent fixture authority");
        let mut raw = Vec::<Value>::new();
        let mut events = 0;
        for frame in doc["events"].as_array().unwrap() {
            if frame["type"]
                .as_str()
                .is_some_and(|k| work::EVENTS.contains(&k))
            {
                raw.push(frame.clone());
                continue;
            }
            if frame["type"] != "gui.event"
                || !frame["event"]["type"]
                    .as_str()
                    .is_some_and(|k| work::EVENTS.contains(&k))
            {
                continue;
            }
            let pos = raw
                .iter()
                .position(|r| r == &frame["event"])
                .expect("exact raw occurrence");
            raw.remove(pos);
            let seq = frame["event"]["sequence"].as_u64().unwrap();
            let contexts = authority
                .iter()
                .filter(|a| a["sequence"].as_u64() == Some(seq))
                .collect::<Vec<_>>();
            assert_eq!(
                contexts.len(),
                1,
                "one independent authority per source emission"
            );
            let current = &contexts[0]["current"];
            let current = if current.is_null() {
                None
            } else {
                Some(CurrentExecution {
                    origin: Origin::parse(&current["origin"]).unwrap(),
                    action_id: current["action_id"].as_str().map(str::to_owned),
                })
            };
            if frame["event"]["type"] == "work_closing" {
                l.begin_close();
            }
            let effect = l
                .observe(frame, current.as_ref())
                .unwrap_or_else(|e| panic!("{} seq {seq}: {e}", path.display()));
            if let Effect::Reconciled {
                input,
                reconciliation,
                recovered,
            } = effect
            {
                let selected = doc["reconciliations"]
                    .as_array()
                    .expect("independent explicit reconciliation command")
                    .iter()
                    .filter(|selected| selected["operation_id"] == reconciliation.operation)
                    .collect::<Vec<_>>();
                assert_eq!(
                    selected.len(),
                    1,
                    "one explicit native-history review owns each reconciliation"
                );
                assert_eq!(selected[0]["instance"], frame["event"]["instance"]);
                assert_eq!(selected[0]["delivery_id"], reconciliation.delivery);
                assert_eq!(selected[0]["input_id"], input.id);
                assert_eq!(
                    selected[0]["journal_instance"],
                    reconciliation.journal_instance
                );
                assert_eq!(selected[0]["payload_sha256"], reconciliation.payload_sha256);
                let before = l.sequence();
                l.verify_reconciled(&frame["event"]).unwrap();
                assert_eq!(l.sequence(), before);
                if recovered {
                    reconciliation_totals.1 += 1;
                } else {
                    reconciliation_totals.0 += 1;
                }
            }
            if frame["event"]["type"] == "work_settled" {
                let before = l.sequence();
                l.verify_settled(&frame["event"]).unwrap();
                assert_eq!(l.sequence(), before);
                totals.2 += 1;
            }
            events += 1;
        }
        assert!(raw.is_empty(), "source raw envelopes must be paired");
        if !doc["shutdown"].is_null() {
            l.verify_shutdown(&doc["shutdown"]).unwrap();
            totals.1 += 1;
        }
        totals.0 += events;
        println!(
            "source_fixture={} sha256={:x} lifecycle_events={events}",
            path.display(),
            Sha256::digest(&bytes)
        );
    }
    assert!(
        totals.1 >= 1,
        "source matrix must include a verified shutdown"
    );
    if require_reconciliations {
        assert!(
            reconciliation_totals.0 >= 1 && reconciliation_totals.1 >= 1,
            "provider matrix must prove current and recovered reconciliation"
        );
    }
    println!(
        "source_totals events={} shutdowns={} settlements={} reconciled_current={} reconciled_recovered={}",
        totals.0, totals.1, totals.2, reconciliation_totals.0, reconciliation_totals.1
    );
}

#[test]
fn completion_payload_preserves_nul_and_controls_in_content_and_display() {
    let value = json!({"content":"canonical\0\u{1}\n🙂", "text":"display\0\t",
        "source":"harness", "tool_profile":"strict"});
    let digest = format!("{:x}", Sha256::digest(serde_json::to_vec(&value).unwrap()));
    let mut ledger = ledger();
    let event = claim_with_payload(&mut ledger, &value, &digest);
    let Effect::Admission { payload, .. } = emit(&mut ledger, event) else {
        panic!()
    };
    assert_eq!(payload.content, value["content"].as_str().unwrap());
    assert_eq!(payload.display_text(), value["text"].as_str().unwrap());
}

fn committed_payload() -> Value {
    json!({"delivery":"committed_history", "source":"child-result", "tool_profile":"strict",
        "text":"Continuing from 2 saved child results.", "history":{"session_id":id(1),
            "transcript_sha256":"d".repeat(64), "context_sha256":"e".repeat(64),
            "receipt_ids":["completion-first", "completion_second"]}})
}
fn committed_claim(l: &mut Ledger, payload: &Value, provider: &str, kind: &str) -> Value {
    for phase in ["registered", "running"] {
        let mut event = producer_event(0, phase);
        event["producer"]["kind"] = json!(kind);
        event["task_state"] = Value::Null;
        emit(l, event);
    }
    let mut input = input(0);
    input["source"] = json!("child-result");
    input["payload_sha256"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(payload).unwrap())
    ));
    for phase in ["queued", "claiming"] {
        let mut event = input_event(0, phase);
        event["input"] = input.clone();
        emit(l, event);
    }
    let mut event = admission_event(0);
    event["input"] = input;
    event["payload"] = payload.clone();
    event["admission"]["origin"]["source"] = json!("child-result");
    event["admission"]["provider"] = json!(provider);
    event
}
fn ledger_provider(provider: &str) -> Ledger {
    let mut value = capture();
    value["provider"] = json!(provider);
    let mut ledger = Ledger::new(parse_capture(&value).unwrap(), Profile::Strict);
    ledger.activate(&active()).unwrap();
    ledger
}
#[test]
fn committed_history_grants_exact_status_and_references_without_user_content() {
    for provider in ["host", "claude"] {
        let mut ledger = ledger_provider(provider);
        let event = committed_claim(&mut ledger, &committed_payload(), provider, "child_result");
        let Effect::Admission {
            payload, admission, ..
        } = emit(&mut ledger, event)
        else {
            panic!()
        };
        let work::Delivery::CommittedHistory(history) = &payload.delivery else {
            panic!()
        };
        assert!(payload.content.is_empty());
        assert_eq!(
            history.receipt_ids,
            ["completion-first", "completion_second"]
        );
        assert_eq!(history.session, id(1));
        assert_eq!(
            payload.display_text(),
            "Continuing from 2 saved child results."
        );
        assert!(admission.delivery.is_none());
    }
}
#[test]
fn committed_history_rejects_content_aliases_forged_references_and_wrong_producer() {
    for fault in [
        "append",
        "extra",
        "empty",
        "duplicate",
        "invalid-id",
        "foreign-session",
        "hash",
        "kind",
        "provider-delivery",
        "codex",
    ] {
        let mut value = committed_payload();
        match fault {
            "append" => value["content"] = json!("Never append this"),
            "extra" => value["history"]["unverified"] = json!(true),
            "empty" => value["history"]["receipt_ids"] = json!([]),
            "duplicate" => value["history"]["receipt_ids"] = json!(["same", "same"]),
            "invalid-id" => value["history"]["receipt_ids"] = json!(["has space"]),
            "foreign-session" => value["history"]["session_id"] = json!(id(10)),
            "hash" => value["history"]["transcript_sha256"] = json!("invalid"),
            _ => {}
        }
        let provider = if fault == "codex" { "codex" } else { "host" };
        let mut ledger = ledger_provider(provider);
        let mut event = committed_claim(
            &mut ledger,
            &value,
            provider,
            if fault == "kind" {
                "inbox"
            } else {
                "child_result"
            },
        );
        if fault == "provider-delivery" {
            event["admission"]["provider_delivery"] = json!({"id":"invented", "state":"prepared"});
        }
        assert!(
            ledger.observe(&envelope(5, event), None).is_err(),
            "{fault}"
        );
    }
}
#[test]
fn claude_append_still_requires_exact_prepared_delivery() {
    let mut ledger = ledger_provider("claude");
    claiming(&mut ledger, 0);
    let mut event = admission_event(0);
    event["admission"]["provider"] = json!("claude");
    assert!(ledger.observe(&envelope(5, event), None).is_err());
}
#[test]
fn inbox_and_provider_producers_retain_normal_append_delivery_contract() {
    for kind in [
        "inbox",
        "compact_wake",
        "provider_question",
        "provider_recovery",
    ] {
        let mut ledger = ledger();
        for phase in ["registered", "running"] {
            let mut event = producer_event(0, phase);
            event["producer"]["kind"] = json!(kind);
            event["task_state"] = Value::Null;
            emit(&mut ledger, event);
        }
        emit(&mut ledger, input_event(0, "queued"));
        emit(&mut ledger, input_event(0, "claiming"));
        let Effect::Admission { payload, .. } = emit(&mut ledger, admission_event(0)) else {
            panic!()
        };
        assert_eq!(payload.delivery, work::Delivery::Append);
        assert_eq!(payload.content, "Canonical completion 0");
    }
}

fn reconciliation_event(n: u64, recovered: bool) -> Value {
    json!({"type":"work_reconciled","input":input(n),"reconciliation":{
        "operation_id":id(800+n),"provider":"codex","delivery_id":format!("delivery-{n}"),
        "thread_id":"provider-thread","turn_id":"accepted-turn",
        "provider_history_sha256":"1".repeat(64),
        "journal_instance":if recovered{"9".repeat(64)}else{"a".repeat(64)},
        "journal_sha256":"2".repeat(64),"payload_sha256":input(n)["payload_sha256"]},
        "proof":{"current_workers_retired":true,"current_callbacks_retired":true,"input_durable":true,"no_replay":true},
        "history_sha256":"3".repeat(64),"ledger_sha256":"4".repeat(64)})
}
fn retained_for_reconciliation(state: &str, recovered: bool) -> Ledger {
    let mut c = capture();
    c["provider"] = json!("codex");
    if recovered {
        let mut old = epoch();
        old["identity"]["backend"] = json!("historic-codex");
        old["provider"] = json!("codex");
        c["recovered"] = json!([{"epoch":old,"producer":producer(0),"input":input(0),
            "state":state,"reason":"Positive native history must be checked","admission":null}]);
    }
    let mut l = Ledger::new(parse_capture(&c).unwrap(), Profile::Strict);
    l.activate(&active()).unwrap();
    if !recovered {
        claiming(&mut l, 0);
        emit(&mut l, input_event(0, state));
        emit(&mut l, producer_event(0, "retired"));
    }
    l
}
#[test]
fn reconciliation_resolves_only_retained_input_and_echo_never_reapplies_it() {
    for recovered in [false, true] {
        for state in ["held", "uncertain"] {
            let mut l = retained_for_reconciliation(state, recovered);
            let frame = envelope(l.sequence() + 1, reconciliation_event(0, recovered));
            assert!(l.verify_reconciled(&frame["event"]).is_err());
            let Effect::Reconciled {
                input: i,
                reconciliation: r,
                recovered: was_recovered,
            } = l.observe(&frame, None).unwrap()
            else {
                panic!("reconciliation must never become an admission or append");
            };
            assert_eq!(i.id, id(200));
            assert_eq!(was_recovered, recovered);
            assert_eq!(r.operation, id(800));
            assert_eq!(r.provider, work::Provider::Codex);
            assert_eq!(r.delivery, "delivery-0");
            assert_eq!(r.thread, "provider-thread");
            assert_eq!(r.turn, "accepted-turn");
            assert_eq!(r.payload_sha256, i.payload_sha256);
            let sequence = l.sequence();
            l.verify_reconciled(&frame["event"]).unwrap();
            assert_eq!(l.sequence(), sequence);
            let mut wrong = frame["event"].clone();
            wrong["history_sha256"] = json!("5".repeat(64));
            assert!(l.verify_reconciled(&wrong).is_err());
            assert!(l.settled_request(&id(600)).is_err());
            // Resolved input no longer occupies the unresolved close/capacity set.
            l.begin_close();
            emit(&mut l, close_event(&[], &[], &[], sequence + 1));
            l.verify_shutdown(&shutdown(sequence + 1, sequence + 1, &[], &[], &[]))
                .unwrap();
        }
    }
}
#[test]
fn reconciliation_requires_exact_input_epoch_schema_proof_and_hashes() {
    for recovered in [false, true] {
        for fault in [
            "unknown",
            "input-source",
            "epoch",
            "payload",
            "provider",
            "proof-false",
            "proof-missing",
            "proof-extra",
            "event-extra",
            "receipt-extra",
            "history",
            "ledger",
            "journal",
            "provider-history",
            "operation",
            "delivery",
            "thread",
            "turn",
        ] {
            let mut l = retained_for_reconciliation("uncertain", recovered);
            let before = l.sequence();
            let mut e = reconciliation_event(0, recovered);
            match fault {
                "unknown" => e["input"]["input_id"] = json!(id(999)),
                "input-source" => e["input"]["source"] = json!("changed"),
                "epoch" => {
                    e["reconciliation"]["journal_instance"] = json!(if recovered {
                        "a".repeat(64)
                    } else {
                        "9".repeat(64)
                    })
                }
                "payload" => e["reconciliation"]["payload_sha256"] = json!("6".repeat(64)),
                "provider" => e["reconciliation"]["provider"] = json!("claude"),
                "proof-false" => e["proof"]["current_callbacks_retired"] = json!(false),
                "proof-missing" => {
                    e["proof"].as_object_mut().unwrap().remove("no_replay");
                }
                "proof-extra" => e["proof"]["historical_process_retired"] = json!(true),
                "event-extra" => e["payload"] = payload(0),
                "receipt-extra" => e["reconciliation"]["execution_id"] = json!(id(500)),
                "history" => e["history_sha256"] = json!("A".repeat(64)),
                "ledger" => e["ledger_sha256"] = json!("a".repeat(63)),
                "journal" => e["reconciliation"]["journal_sha256"] = Value::Null,
                "provider-history" => {
                    e["reconciliation"]["provider_history_sha256"] = json!("not-a-hash")
                }
                "operation" => e["reconciliation"]["operation_id"] = json!(""),
                "delivery" => e["reconciliation"]["delivery_id"] = json!("x".repeat(129)),
                "thread" => e["reconciliation"]["thread_id"] = json!("native\0thread"),
                "turn" => e["reconciliation"]["turn_id"] = Value::Null,
                _ => unreachable!(),
            }
            assert!(
                l.observe(&envelope(before + 1, e), None).is_err(),
                "{fault} recovered={recovered}"
            );
            assert!(l.failed());
            assert_eq!(l.sequence(), before);
        }
    }
}
#[test]
fn reconciliation_cannot_invent_a_retired_current_worker_or_clear_an_admission() {
    for phase in [
        "queued",
        "claiming",
        "admitted",
        "uncertain-with-admission",
        "cancelled",
        "live-producer",
    ] {
        let mut l = ledger();
        running(&mut l, 0);
        emit(&mut l, input_event(0, "queued"));
        if !matches!(phase, "queued" | "cancelled") {
            emit(&mut l, input_event(0, "claiming"));
        }
        if matches!(phase, "admitted" | "uncertain-with-admission") {
            emit(&mut l, admission_event(0));
        }
        if matches!(phase, "uncertain-with-admission" | "live-producer") {
            emit(&mut l, input_event(0, "uncertain"));
        }
        if phase == "cancelled" {
            emit(&mut l, input_event(0, "cancelled"));
        }
        if phase != "live-producer" {
            emit(&mut l, producer_event(0, "retired"));
        }
        let next = l.sequence() + 1;
        assert!(
            l.observe(&envelope(next, reconciliation_event(0, false)), None)
                .is_err(),
            "{phase}"
        );
    }
    let mut c = capture();
    c["recovered"] = json!([{"epoch":epoch(),"producer":producer(0),"input":input(0),"state":"uncertain","reason":"Old accepted execution","admission":admission(0)}]);
    let mut l = Ledger::new(parse_capture(&c).unwrap(), Profile::Strict);
    l.activate(&active()).unwrap();
    assert!(
        l.observe(&envelope(1, reconciliation_event(0, true)), None)
            .is_err()
    );
}
#[test]
fn reconciliation_requires_exact_rpc_origin_and_rejects_duplicate_or_replay() {
    for fault in ["source", "operation", "hook", "execution", "session"] {
        let mut l = retained_for_reconciliation("held", false);
        let mut frame = envelope(l.sequence() + 1, reconciliation_event(0, false));
        match fault {
            "source" => frame["source"] = json!("work"),
            "operation" => frame["operation_id"] = json!("foreign-operation"),
            "hook" => frame["hook_id"] = json!(id(400)),
            "execution" => frame["execution_id"] = json!(id(500)),
            "session" => frame["session_id"] = json!(id(888)),
            _ => unreachable!(),
        }
        assert!(l.observe(&frame, None).is_err(), "{fault}");
    }
    for next in [
        reconciliation_event(0, false),
        input_event(0, "claiming"),
        input_event(0, "reconciled"),
        admission_event(0),
    ] {
        let mut l = retained_for_reconciliation("held", false);
        emit(&mut l, reconciliation_event(0, false));
        let sequence = l.sequence() + 1;
        assert!(l.observe(&envelope(sequence, next), None).is_err());
    }
}
#[test]
fn reconciliation_drains_buffered_local_close_but_cannot_cross_source_barrier() {
    let mut l = retained_for_reconciliation("uncertain", false);
    l.begin_close();
    assert!(matches!(
        emit(&mut l, reconciliation_event(0, false)),
        Effect::Reconciled { .. }
    ));
    let mut l = retained_for_reconciliation("uncertain", false);
    l.begin_close();
    let sequence = l.sequence() + 1;
    emit(&mut l, close_event(&[], &[0], &[], sequence));
    assert!(
        l.observe(
            &envelope(sequence + 1, reconciliation_event(0, false)),
            None
        )
        .is_err()
    );
}
#[test]
fn reconciliation_operation_is_single_use_and_cannot_launch_a_new_turn() {
    for reuse in [true, false] {
        let mut l = retained_for_reconciliation("uncertain", false);
        emit(&mut l, reconciliation_event(0, false));
        claiming(&mut l, 1);
        let mut next = if reuse {
            emit(&mut l, input_event(1, "held"));
            emit(&mut l, producer_event(1, "retired"));
            let mut e = reconciliation_event(1, false);
            e["reconciliation"]["operation_id"] = json!(id(800));
            e
        } else {
            let mut e = admission_event(1);
            e["admission"]["provider"] = json!("codex");
            e["admission"]["operation_id"] = json!(id(800));
            e["admission"]["origin"]["operation_id"] = json!(id(800));
            e
        };
        let sequence = l.sequence() + 1;
        assert!(l.observe(&envelope(sequence, next.take()), None).is_err());
    }
}
