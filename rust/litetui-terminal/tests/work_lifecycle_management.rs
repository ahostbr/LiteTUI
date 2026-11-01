use litetui_terminal::{
    management::{Management, Step},
    work_lifecycle::{self as work, CurrentExecution, Effect, Mode},
    work_protocol, work_view,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn id(n: u64) -> String {
    format!("00000000-0000-4000-8000-{n:012x}")
}
fn identity() -> Value {
    json!({"session_id":id(1),"agent_id":id(2),"backend":"custom","model":"model"})
}
fn ready() -> Value {
    json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":id(1),
        "backend":"custom","model":"model","tool_profile":"strict"})
}
fn state() -> Value {
    json!({"session_id":id(1),"busy":false,"mode":"normal","tool_profile":"strict",
        "model":{"backend":"custom","model":"model"},
        "ownership":{"data_version":1,"writable":true,"agent_id":id(2)},
        "restored_input":{"policy":"hold","held":0},"schedules_paused":false,
        "conversations":[],"jobs":[],"tasks":[],"active_work":[],
        "pending_approvals":[],"pending_questions":[],"pending_model_loads":[]})
}
fn capture() -> Value {
    json!({"kind":"work_capture","version":1,"instance":"a".repeat(64),"activation_token":"b".repeat(64),
        "sequence":0,"identity":identity(),"provider":"host",
        "limits":{"producers":256,"inputs":256,"payload_bytes":65536,"ledger_bytes":33554432,
            "envelope_bytes":131072,"reply_bytes":1048576},"recovered":[],"deferred_jobs":[]})
}
fn activation() -> Value {
    json!({"kind":"work_active","version":1,"instance":"a".repeat(64),"sequence":0,"identity":identity()})
}
fn write(step: Step) -> Value {
    let Step::Write(value) = step else {
        panic!("{step:?}")
    };
    value
}
fn reply(m: &mut Management, command: &Value, result: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":command["id"],"ok":true,"result":result}))
}
fn negotiate(m: &mut Management, extra: &[&str]) -> Value {
    if extra.contains(&work::CAPTURE) {
        m.enable_work_lifecycle(litetui_terminal::tool_mode::Profile::Strict)
            .unwrap();
    }
    let hello = m.begin(&ready()).unwrap();
    let operations = ["gui.hello", "gui.state"]
        .into_iter()
        .chain(extra.iter().copied())
        .collect::<Vec<_>>();
    write(
        reply(
            m,
            &hello,
            json!({"protocol_version":1,"data_version":1,
        "capabilities":["hello","state"],"operations":operations}),
        )
        .unwrap(),
    )
}
fn connect() -> Management {
    let mut m = Management::default();
    let command = negotiate(
        &mut m,
        &[
            work::CAPTURE,
            work::ACTIVATE,
            work::SETTLED,
            work_protocol::READ,
            "gui.models.list",
            "gui.models.select",
        ],
    );
    let cap = write(reply(&mut m, &command, state()).unwrap());
    let activate = write(reply(&mut m, &cap, capture()).unwrap());
    assert!(matches!(
        reply(&mut m, &activate, activation()).unwrap(),
        Step::Initialized(_)
    ));
    m
}
fn producer() -> Value {
    json!({"producer_id":id(100),"kind":"host_task","object_id":format!("t-{:032x}",0),
        "producer_session_id":id(1),"owner_instance":"c".repeat(32),"owner_created":"creation-123",
        "generation":1,"causal_action_id":null,"causal_operation_id":null,"causal_execution_id":null})
}
fn payload() -> Value {
    json!({"content":"Canonical completion\n[policy]","text":"Canonical completion",
        "source":"harness","tool_profile":"strict"})
}
fn input() -> Value {
    json!({"input_id":id(200),"producer_id":id(100),
        "payload_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&payload()).unwrap())),
        "source":"harness","requested_profile":"strict","delivery_session_id":id(1),"queue_revision":1})
}
fn origin() -> Value {
    json!({"session_id":id(1),"operation_id":id(300),"hook_id":id(400),
        "source":"harness","execution_id":id(500)})
}
fn admission() -> Value {
    json!({"admission_id":id(600),"input_id":id(200),"mode":"new_turn","operation_id":id(300),
        "origin":origin(),"parent_execution_id":null,"boundary":0,
        "requested_profile":"strict","effective_profile":"strict","seat_profile":"strict",
        "provider":"host","provider_delivery":null})
}
fn envelope(sequence: u64, mut event: Value) -> Value {
    event["version"] = json!(1);
    event["instance"] = json!("a".repeat(64));
    event["sequence"] = json!(sequence);
    event["identity"] = identity();
    let mut frame = if matches!(
        event["type"].as_str(),
        Some("work_admission" | "work_settled")
    ) {
        event["admission"]["origin"].clone()
    } else {
        json!({"session_id":id(1),"operation_id":null,"hook_id":null,"source":"work","execution_id":null})
    };
    frame["type"] = json!("gui.event");
    frame["event_id"] = json!(id(9000 + sequence));
    frame["event"] = event;
    frame
}
fn emit(
    m: &mut Management,
    frame: Value,
    current: Option<&CurrentExecution>,
) -> std::io::Result<Step> {
    assert!(matches!(m.accept(frame["event"].clone())?, Step::Ignore));
    let bytes = serde_json::to_vec(&frame).unwrap().len();
    m.accept_wire_with_work(frame, bytes, current)
}
fn claiming(m: &mut Management) {
    for (sequence, phase) in [(1, "registered"), (2, "running")] {
        emit(
            m,
            envelope(
                sequence,
                json!({"type":"work_producer","phase":phase,"producer":producer(),
            "worker_id":if phase=="registered"{Value::Null}else{json!(id(700))},
            "task_state":"running","reason":null,"proof":null}),
            ),
            None,
        )
        .unwrap();
    }
    for (sequence, phase) in [(3, "queued"), (4, "claiming")] {
        emit(
            m,
            envelope(
                sequence,
                json!({"type":"work_input","phase":phase,"input":input(),"reason":null}),
            ),
            None,
        )
        .unwrap();
    }
}
fn accepted() -> Value {
    envelope(
        5,
        json!({"type":"work_admission","status":"accepted","input":input(),
        "admission":admission(),"payload":payload()}),
    )
}
fn stream(kind: &str, execution_origin: Value, emission: u64) -> Value {
    let mut frame = execution_origin;
    frame["type"] = json!("gui.event");
    frame["event_id"] = json!(id(emission));
    frame["event"] = json!({"type":kind,"execution_id":frame["execution_id"]});
    frame
}

#[test]
fn capture_activation_precede_frontend_readiness_and_use_separate_epoch() {
    let mut m = Management::default();
    let state_command = negotiate(&mut m, &[work::CAPTURE, work::ACTIVATE, work::SETTLED]);
    let capture_command = write(reply(&mut m, &state_command, state()).unwrap());
    assert_eq!(capture_command["type"], work::CAPTURE);
    assert_eq!(capture_command["agent_id"], id(2));
    assert!(!m.ready() && m.work_lifecycle_pending() && !m.work_lifecycle_active());
    let activate = write(reply(&mut m, &capture_command, capture()).unwrap());
    assert_eq!(activate["type"], work::ACTIVATE);
    assert_eq!(activate["activation_token"], "b".repeat(64));
    assert_ne!(capture_command["id"], activate["id"]);
    assert!(!m.ready());
    assert!(matches!(
        reply(&mut m, &activate, activation()).unwrap(),
        Step::Initialized(_)
    ));
    assert!(m.ready() && m.work_lifecycle_active() && !m.work_lifecycle_pending());
}
#[test]
fn unsupported_legacy_fixture_keeps_its_original_startup() {
    let mut m = Management::default();
    let command = negotiate(&mut m, &[]);
    assert!(matches!(
        reply(&mut m, &command, state()).unwrap(),
        Step::Initialized(_)
    ));
    assert!(m.ready() && !m.work_lifecycle_active());
}
#[test]
fn partial_or_explicitly_unsupported_lifecycle_negotiation_fails() {
    for operations in [vec![work::CAPTURE], vec![work::CAPTURE, work::ACTIVATE]] {
        let mut m = Management::default();
        let hello = m.begin(&ready()).unwrap();
        let ops = ["gui.hello", "gui.state"]
            .into_iter()
            .chain(operations)
            .collect::<Vec<_>>();
        assert!(
            reply(
                &mut m,
                &hello,
                json!({"protocol_version":1,"data_version":1,
            "capabilities":["hello","state"],"operations":ops})
            )
            .is_err()
        );
        assert!(!m.ready());
    }
    let mut bad = ready();
    bad["work_lifecycle_version"] = json!(true);
    assert!(Management::default().begin(&bad).is_err());
}
#[test]
fn startup_theme_fallback_finishes_before_capture() {
    let mut m = Management::default();
    let command = negotiate(
        &mut m,
        &[
            work::CAPTURE,
            work::ACTIVATE,
            work::SETTLED,
            litetui_terminal::theme_controls::SNAPSHOT,
        ],
    );
    let theme = write(reply(&mut m, &command, state()).unwrap());
    assert_eq!(theme["type"], litetui_terminal::theme_controls::SNAPSHOT);
    assert!(!m.work_lifecycle_pending());
    let cap = write(
        m.accept(json!({"type":"response","id":theme["id"],"ok":false,
        "error":"Controlled theme catalog unavailable"}))
            .unwrap(),
    );
    assert_eq!(cap["type"], work::CAPTURE);
    let activate = write(reply(&mut m, &cap, capture()).unwrap());
    assert!(matches!(
        reply(&mut m, &activate, activation()).unwrap(),
        Step::Theme {
            startup: Some(_),
            ..
        }
    ));
}
#[test]
fn resumed_history_is_preserved_through_lifecycle_handshake() {
    let mut m = Management::new(Some(id(1)));
    let command = negotiate(
        &mut m,
        &[
            work::CAPTURE,
            work::ACTIVATE,
            work::SETTLED,
            "gui.conversations.read",
        ],
    );
    let history = write(reply(&mut m, &command, state()).unwrap());
    assert_eq!(history["type"], "gui.conversations.read");
    let cap = write(
        reply(
            &mut m,
            &history,
            json!({"session_id":id(1),"read_only":false,
        "meta":{"id":id(1)},"messages":[{"role":"user","content":"Retained history"}]}),
        )
        .unwrap(),
    );
    let activate = write(reply(&mut m, &cap, capture()).unwrap());
    let Step::Restored { history, .. } = reply(&mut m, &activate, activation()).unwrap() else {
        panic!()
    };
    assert_eq!(history.messages[0].text, "Retained history");
}
#[test]
fn legacy_accept_does_not_turn_missing_execution_authority_into_idle() {
    let mut m = connect();
    claiming(&mut m);
    let frame = accepted();
    assert!(matches!(
        m.accept(frame["event"].clone()).unwrap(),
        Step::Ignore
    ));
    assert!(m.accept(frame).is_err());
    assert!(!m.ready());
}
#[test]
fn full_typed_admission_is_accepted_during_ordinary_work_read() {
    let mut m = connect();
    claiming(&mut m);
    let _read = m
        .request_work(work_view::Intent {
            generation: 1,
            request: work_view::Request::Snapshot,
        })
        .unwrap();
    assert!(!m.ready());
    let Step::WorkLifecycle(effect) = emit(&mut m, accepted(), None).unwrap() else {
        panic!()
    };
    let Effect::Admission {
        admission,
        payload,
        drain_only,
        ..
    } = *effect
    else {
        panic!()
    };
    assert_eq!(admission.mode, Mode::NewTurn);
    assert_eq!(payload.content, "Canonical completion\n[policy]");
    assert_eq!(payload.text.as_deref(), Some("Canonical completion"));
    assert!(!drain_only && m.work_pending());
    assert!(matches!(
        emit(&mut m, stream("turn_start", origin(), 12300), None).unwrap(),
        Step::Event(_)
    ));
}
#[test]
fn stream_execution_must_match_the_exact_preannouncement() {
    for field in ["execution_id", "hook_id", "source"] {
        let mut m = connect();
        claiming(&mut m);
        emit(&mut m, accepted(), None).unwrap();
        let mut wrong = origin();
        wrong[field] = json!(if field == "source" {
            "rpc".into()
        } else {
            id(999)
        });
        assert!(
            emit(&mut m, stream("turn_start", wrong, 12301), None).is_err(),
            "{field}"
        );
    }
}
#[test]
fn source_operation_cannot_reuse_a_prior_user_operation() {
    let mut m = connect();
    m.prompt(&id(300)).unwrap();
    claiming(&mut m);
    assert!(emit(&mut m, accepted(), None).is_err());
}
#[test]
fn in_turn_requires_and_preserves_the_actual_foreground_execution() {
    let mut m = connect();
    m.prompt("user-operation").unwrap();
    let mut parent = origin();
    parent["operation_id"] = json!("user-operation");
    parent["source"] = json!("rpc");
    emit(&mut m, stream("turn_start", parent.clone(), 12302), None).unwrap();
    let authority = CurrentExecution {
        origin: m
            .known_work_origin()
            .expect("already validated foreground origin"),
        action_id: None,
    };
    claiming(&mut m);
    let mut frame = accepted();
    frame["event"]["admission"]["mode"] = json!("in_turn");
    frame["event"]["admission"]["operation_id"] = json!("user-operation");
    frame["event"]["admission"]["origin"] = parent.clone();
    frame["event"]["admission"]["parent_execution_id"] = parent["execution_id"].clone();
    frame["event"]["admission"]["boundary"] = json!(1);
    for key in [
        "session_id",
        "operation_id",
        "hook_id",
        "source",
        "execution_id",
    ] {
        frame[key] = parent[key].clone();
    }
    assert!(matches!(
        emit(&mut m, frame, Some(&authority)).unwrap(),
        Step::WorkLifecycle(_)
    ));
    assert!(matches!(
        emit(
            &mut m,
            stream("text_delta", parent, 12303),
            Some(&authority)
        )
        .unwrap(),
        Step::Event(_)
    ));
}
#[test]
fn lifecycle_raw_budgets_apply_before_mirror_or_error_projection() {
    let mut m = connect();
    let frame = envelope(
        1,
        json!({"type":"work_producer","phase":"registered","producer":producer(),
        "worker_id":null,"task_state":"running","reason":null,"proof":null}),
    );
    assert!(
        m.accept_wire_with_work(frame, work::ENVELOPE_BYTES + 1, None)
            .is_err()
    );
    let mut m = Management::default();
    let command = negotiate(&mut m, &[work::CAPTURE, work::ACTIVATE, work::SETTLED]);
    let capture = write(reply(&mut m, &command, state()).unwrap());
    assert!(
        m.accept_wire(
            json!({"type":"response","id":capture["id"],"ok":false,
        "error":"bounded"}),
            work::REPLY_BYTES + 1
        )
        .is_err()
    );
}
#[test]
fn closing_during_capture_never_activates_or_publishes_startup() {
    let mut m = Management::default();
    let command = negotiate(&mut m, &[work::CAPTURE, work::ACTIVATE, work::SETTLED]);
    let cap = write(reply(&mut m, &command, state()).unwrap());
    m.begin_close();
    assert!(matches!(
        reply(&mut m, &cap, capture()).unwrap(),
        Step::Ignore
    ));
    assert!(!m.work_lifecycle_active() && !m.work_lifecycle_pending() && !m.ready());
}
#[test]
fn closing_during_activation_validates_ack_and_retains_cleanup_ownership() {
    let mut m = Management::default();
    let command = negotiate(&mut m, &[work::CAPTURE, work::ACTIVATE, work::SETTLED]);
    let cap = write(reply(&mut m, &command, state()).unwrap());
    let activate = write(reply(&mut m, &cap, capture()).unwrap());
    m.begin_close();
    assert!(matches!(
        reply(&mut m, &activate, activation()).unwrap(),
        Step::Ignore
    ));
    assert!(m.work_lifecycle_active() && !m.ready());
    emit(
        &mut m,
        envelope(
            1,
            json!({"type":"work_closing","cutoff_sequence":1,
        "producer_ids":[],"input_ids":[],"admission_ids":[]}),
        ),
        None,
    )
    .unwrap();
    m.verify_work_shutdown(
        &json!({"version":1,"instance":"a".repeat(64),"closing_sequence":1,
        "final_sequence":1,"admission_closed":true,"worker_settled":true,"callback_settled":true,
        "process_settled":true,"producer_ids":[],"inputs":[],"admission_ids":[],
        "ledger_sha256":"f".repeat(64),"effects_verified":false}),
    )
    .unwrap();
}
#[test]
fn settlement_read_verifies_an_emitted_receipt_without_applying_its_sequence_twice() {
    let mut m = connect();
    claiming(&mut m);
    emit(&mut m, accepted(), None).unwrap();
    let command = m.request_work_settled(&id(600)).unwrap();
    let settled = envelope(
        6,
        json!({"type":"work_settled","input":input(),"admission":admission(),
        "outcome":"completed","proof":{"worker_retired":true,"callbacks_retired":true,
            "streams_retired":true,"input_durable":true},
        "history_sha256":"e".repeat(64),"ledger_sha256":"f".repeat(64)}),
    );
    emit(&mut m, settled.clone(), None).unwrap();
    assert!(
        matches!(reply(&mut m,&command,settled["event"].clone()).unwrap(),
        Step::WorkLifecycleVerified{admission_id} if admission_id==id(600))
    );
    assert!(!m.work_lifecycle_pending());
}

#[test]
fn explicit_opt_in_requires_operations_but_disabled_supervisor_never_activates() {
    let mut m = Management::default();
    m.enable_work_lifecycle(litetui_terminal::tool_mode::Profile::Strict)
        .unwrap();
    let hello = m.begin(&ready()).unwrap();
    assert!(
        reply(
            &mut m,
            &hello,
            json!({"protocol_version":1,"data_version":1,
        "capabilities":["hello","state"],"operations":["gui.hello","gui.state"]})
        )
        .is_err()
    );
    let mut m = Management::default();
    let hello = m.begin(&ready()).unwrap();
    let state_cmd = write(
        reply(
            &mut m,
            &hello,
            json!({"protocol_version":1,"data_version":1,
        "capabilities":["hello","state"],"operations":["gui.hello","gui.state",
            work::CAPTURE,work::ACTIVATE,work::SETTLED]}),
        )
        .unwrap(),
    );
    assert!(matches!(
        reply(&mut m, &state_cmd, state()).unwrap(),
        Step::Initialized(_)
    ));
    assert!(!m.work_lifecycle_active() && !m.work_lifecycle_pending());
}
#[test]
fn settlement_readback_cannot_substitute_another_known_admission_receipt() {
    let mut m = connect();
    claiming(&mut m);
    emit(&mut m, accepted(), None).unwrap();
    let first = envelope(
        6,
        json!({"type":"work_settled","input":input(),"admission":admission(),
        "outcome":"completed","proof":{"worker_retired":true,"callbacks_retired":true,
            "streams_retired":true,"input_durable":true},
        "history_sha256":"e".repeat(64),"ledger_sha256":"f".repeat(64)}),
    );
    emit(&mut m, first, None).unwrap();
    let mut p = producer();
    p["producer_id"] = json!(id(101));
    p["object_id"] = json!(format!("t-{:032x}", 1));
    let mut i = input();
    i["input_id"] = json!(id(201));
    i["producer_id"] = json!(id(101));
    i["queue_revision"] = json!(2);
    for (sequence, phase) in [(7, "registered"), (8, "running")] {
        emit(
            &mut m,
            envelope(
                sequence,
                json!({"type":"work_producer","phase":phase,"producer":p,
            "worker_id":if phase=="registered"{Value::Null}else{json!(id(701))},
            "task_state":"running","reason":null,"proof":null}),
            ),
            None,
        )
        .unwrap();
    }
    for (sequence, phase) in [(9, "queued"), (10, "claiming")] {
        emit(
            &mut m,
            envelope(
                sequence,
                json!({"type":"work_input","phase":phase,"input":i,"reason":null}),
            ),
            None,
        )
        .unwrap();
    }
    let mut a = admission();
    a["admission_id"] = json!(id(601));
    a["input_id"] = json!(id(201));
    a["operation_id"] = json!(id(301));
    a["origin"]["operation_id"] = json!(id(301));
    a["origin"]["execution_id"] = json!(id(501));
    emit(
        &mut m,
        envelope(
            11,
            json!({"type":"work_admission","status":"accepted",
        "input":i,"admission":a,"payload":payload()}),
        ),
        None,
    )
    .unwrap();
    let second = envelope(
        12,
        json!({"type":"work_settled","input":i,"admission":a,
        "outcome":"completed","proof":{"worker_retired":true,"callbacks_retired":true,
            "streams_retired":true,"input_durable":true},
        "history_sha256":"e".repeat(64),"ledger_sha256":"f".repeat(64)}),
    );
    emit(&mut m, second.clone(), None).unwrap();
    let request = m.request_work_settled(&id(600)).unwrap();
    assert!(
        reply(&mut m, &request, second["event"].clone()).is_err(),
        "a valid receipt for another known admission must not settle this request"
    );
}

fn model_choices(model: &str) -> Value {
    json!({"backend":"custom", "model":model, "models":[
        {"slug":"model", "name":"Old", "current":model=="model", "loaded":true},
        {"slug":"next", "name":"Next", "current":model=="next", "loaded":true}]})
}
fn select_model(m: &mut Management, value: &str) -> Value {
    use litetui_terminal::model_controls::{Intent, Kind, Update};
    let read = m.request_control(Intent::Open(Kind::Model)).unwrap();
    let Step::Control(Update::Choices(menu)) = reply(m, &read, model_choices("model")).unwrap()
    else {
        panic!()
    };
    m.request_control(Intent::Select {
        revision: menu.revision,
        value: value.into(),
    })
    .unwrap()
}
fn transition_barrier(m: &mut Management) {
    assert!(m.work_identity_transition_pending());
    assert!(matches!(
        emit(
            m,
            envelope(
                1,
                json!({"type":"work_closing", "cutoff_sequence":1,
        "producer_ids":[], "input_ids":[], "admission_ids":[]})
            ),
            None
        )
        .unwrap(),
        Step::WorkLifecycle(_)
    ));
}
fn transition_receipt() -> Value {
    let mut result = model_choices("next");
    result["work_recapture_required"] = json!(true);
    result["work_shutdown"] = json!({"version":1,"instance":"a".repeat(64),"closing_sequence":1,
        "final_sequence":1,"admission_closed":true,"worker_settled":true,"callback_settled":true,
        "process_settled":true,"producer_ids":[],"inputs":[],"admission_ids":[],
        "ledger_sha256":"f".repeat(64),"effects_verified":false});
    result
}
fn new_capture() -> Value {
    let mut result = capture();
    result["instance"] = json!("c".repeat(64));
    result["identity"]["model"] = json!("next");
    result
}
#[test]
fn identity_change_waits_for_verified_old_retirement_and_new_activation() {
    let mut m = connect();
    m.prompt("prior-user-operation").unwrap();
    let apply = select_model(&mut m, "next");
    transition_barrier(&mut m);
    let read = write(reply(&mut m, &apply, transition_receipt()).unwrap());
    assert!(!m.ready() && !m.work_lifecycle_active() && m.work_identity_transition_pending());
    let cap = write(reply(&mut m, &read, model_choices("next")).unwrap());
    assert_eq!(cap["type"], work::CAPTURE);
    assert_eq!(cap["model"], "next");
    assert!(m.prompt("premature").is_err());
    let activate = write(reply(&mut m, &cap, new_capture()).unwrap());
    assert_eq!(activate["instance"], "c".repeat(64));
    let mut ack = activation();
    ack["instance"] = json!("c".repeat(64));
    ack["identity"]["model"] = json!("next");
    assert!(matches!(
        reply(&mut m, &activate, ack).unwrap(),
        Step::Control(_)
    ));
    assert!(m.ready() && m.work_lifecycle_active() && !m.work_identity_transition_pending());
    assert!(m.prompt("prior-user-operation").is_err());
}
#[test]
fn identity_mutation_rejects_missing_forged_and_unclean_retirement() {
    for fault in ["missing", "epoch", "worker", "recapture", "barrier"] {
        let mut m = connect();
        let apply = select_model(&mut m, "next");
        if fault != "barrier" {
            transition_barrier(&mut m);
        }
        let mut receipt = transition_receipt();
        match fault {
            "missing" => {
                receipt.as_object_mut().unwrap().remove("work_shutdown");
            }
            "epoch" => receipt["work_shutdown"]["instance"] = json!("f".repeat(64)),
            "worker" => receipt["work_shutdown"]["worker_settled"] = json!(false),
            "recapture" => receipt["work_recapture_required"] = json!(false),
            "barrier" => {}
            _ => unreachable!(),
        }
        assert!(reply(&mut m, &apply, receipt).is_err(), "{fault}");
        assert!(!m.ready());
    }
}
#[test]
fn recapture_rejects_old_epoch_and_different_verified_identity() {
    for fault in ["epoch", "identity"] {
        let mut m = connect();
        let apply = select_model(&mut m, "next");
        transition_barrier(&mut m);
        let read = write(reply(&mut m, &apply, transition_receipt()).unwrap());
        let cap = write(reply(&mut m, &read, model_choices("next")).unwrap());
        let mut result = new_capture();
        if fault == "epoch" {
            result["instance"] = json!("a".repeat(64));
        } else {
            result["identity"]["model"] = json!("model");
        }
        assert!(reply(&mut m, &cap, result).is_err(), "{fault}");
    }
}
#[test]
fn same_model_reselection_needs_no_retirement_or_capture() {
    let mut m = connect();
    let apply = select_model(&mut m, "model");
    assert!(!m.work_identity_transition_pending());
    let read = write(reply(&mut m, &apply, model_choices("model")).unwrap());
    assert!(matches!(
        reply(&mut m, &read, model_choices("model")).unwrap(),
        Step::Control(_)
    ));
    assert!(m.ready() && m.work_lifecycle_active());
}
#[test]
fn failed_model_publication_cannot_change_captured_identity_without_retirement() {
    let mut m = connect();
    let apply = select_model(&mut m, "next");
    let read = write(
        m.accept(json!({"type":"response", "id":apply["id"], "ok":false,
        "error":"publication uncertain"}))
            .unwrap(),
    );
    assert!(reply(&mut m, &read, model_choices("next")).is_err());
}
#[test]
fn failed_reply_after_retirement_cannot_reopen_the_old_epoch() {
    let mut m = connect();
    let apply = select_model(&mut m, "next");
    transition_barrier(&mut m);
    assert!(
        m.accept(json!({"type":"response", "id":apply["id"], "ok":false,
        "error":"publication uncertain"}))
            .is_err()
    );
    assert!(!m.ready());
}
