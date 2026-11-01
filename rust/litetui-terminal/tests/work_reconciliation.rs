//! Source-produced history receipts must pass explicit selection and independent readback.
use litetui_terminal::{
    management::{Management, Step},
    restored_queue::{self as queue, Intent},
    tool_mode::Profile,
    work_lifecycle::{self as life, Effect},
};
use serde_json::{Value, json};
fn fixture(recovered: bool) -> Value {
    serde_json::from_str(if recovered {
        include_str!("fixtures/work_reconciled_recovered.json")
    } else {
        include_str!("fixtures/work_reconciled_current.json")
    })
    .unwrap()
}
fn write(step: Step) -> Value {
    let Step::Write(v) = step else {
        panic!("{step:?}")
    };
    v
}
fn reply(m: &mut Management, cmd: &Value, result: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":cmd["id"],"ok":true,"result":result}))
}
fn emit(m: &mut Management, frame: Value) -> std::io::Result<Step> {
    assert!(matches!(m.accept(frame["event"].clone())?, Step::Ignore));
    let bytes = serde_json::to_vec(&frame).unwrap().len();
    m.accept_wire_with_work(frame, bytes, None)
}
fn connected(v: &Value) -> Management {
    let identity = &v["capture"]["identity"];
    let mut m = Management::default();
    m.enable_work_lifecycle(Profile::Strict).unwrap();
    let hello=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":identity["session_id"],"backend":identity["backend"],"model":identity["model"],"tool_profile":"strict","work_lifecycle_version":1})).unwrap();
    let state=write(reply(&mut m,&hello,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",life::CAPTURE,life::ACTIVATE,life::SETTLED,queue::READ,queue::RECONCILE]})).unwrap());
    let capture=write(reply(&mut m,&state,json!({"session_id":identity["session_id"],"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":identity["backend"],"model":identity["model"]},"ownership":{"data_version":1,"writable":true,"agent_id":identity["agent_id"]},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]})).unwrap());
    let activate = write(reply(&mut m, &capture, v["capture"].clone()).unwrap());
    assert!(matches!(
        reply(&mut m, &activate, v["active"].clone()).unwrap(),
        Step::Initialized(_)
    ));
    m
}
fn queue_data(v: &Value, held: bool) -> Value {
    let identity = &v["capture"]["identity"];
    let event = v["events"]
        .as_array()
        .unwrap()
        .iter()
        .find(|e| e["type"] == "work_reconciled")
        .unwrap();
    let proof = &event["reconciliation"];
    json!({"session_id":identity["session_id"],"backend":identity["backend"],"model":identity["model"],"policy":"hold","held":if held{1}else{0},"snapshot":"a".repeat(64),"entries":if held{vec![json!({"id":proof["delivery_id"],"provider":"codex","state":"uncertain","revision":1,"content":"captured provider input","source":"harness","tool_profile":"strict","thread_id":proof["thread_id"],"turn_id":proof["turn_id"],"can_cancel":false,"can_continue":false,"can_reconcile":true})]}else{vec![]}})
}
fn prepared(recovered: bool) -> (Management, Value, Value, Value) {
    let v = fixture(recovered);
    let mut m = connected(&v);
    let mut frame = Value::Null;
    for event in v["events"]
        .as_array()
        .unwrap()
        .iter()
        .filter(|e| e["type"] == "gui.event")
    {
        if event["event"]["type"] == "work_reconciled" {
            frame = event.clone();
            break;
        }
        emit(&mut m, event.clone()).unwrap();
    }
    let open = m.request_queue(Intent::Open).unwrap();
    let Step::Queue(queue::Update::Snapshot(menu)) =
        reply(&mut m, &open, queue_data(&v, true)).unwrap()
    else {
        panic!()
    };
    let command = m
        .request_queue(Intent::Reconcile {
            revision: menu.revision,
            entry: menu.entries[0].id.clone(),
        })
        .unwrap();
    // The real source fixture used its own explicit command ID. This native harness
    // rebinds only that correlation to the command just reserved by Management.
    frame["operation_id"] = command["id"].clone();
    frame["event"]["reconciliation"]["operation_id"] = command["id"].clone();
    (m, command, frame, v)
}
fn readback(frame: &Value, v: &Value, held: bool) -> Value {
    let mut result = queue_data(v, held);
    let event = &frame["event"];
    let proof = &event["reconciliation"];
    result["work_reconciliation_readback"] = json!({"kind":"work_reconciliation_readback","version":1,"instance":event["instance"],"input_id":event["input"]["input_id"],"operation_id":proof["operation_id"],"delivery_id":proof["delivery_id"],"journal_instance":proof["journal_instance"],"journal_sha256":proof["journal_sha256"],"payload_sha256":proof["payload_sha256"],"history_sha256":event["history_sha256"],"event_ledger_sha256":event["ledger_sha256"],"ledger_sha256":"e".repeat(64),"snapshot":result["snapshot"],"verified":true});
    result
}
fn result(frame: &Value) -> Value {
    json!({"session_id":frame["session_id"],"entry_id":frame["event"]["reconciliation"]["delivery_id"],"reconciled":true,"work_reconciliation":frame["event"]})
}
#[test]
fn exact_current_and_recovered_receipts_wait_for_independent_readback() {
    for recovered in [false, true] {
        let (mut m, command, frame, v) = prepared(recovered);
        assert!(matches!(emit(&mut m, frame.clone()).unwrap(), Step::Ignore));
        assert!(m.take_verified_work_reconciliation().is_none());
        let read = write(reply(&mut m, &command, result(&frame)).unwrap());
        assert_eq!(
            read["work_reconciliation"],
            json!({"instance":frame["event"]["instance"],"input_id":frame["event"]["input"]["input_id"],"operation_id":command["id"],"delivery_id":frame["event"]["reconciliation"]["delivery_id"]})
        );
        assert!(m.take_verified_work_reconciliation().is_none());
        assert!(!m.ready());
        assert!(matches!(
            reply(&mut m, &read, readback(&frame, &v, false)).unwrap(),
            Step::Queue(_)
        ));
        assert!(
            matches!(m.take_verified_work_reconciliation(),Some(Effect::Reconciled{recovered:r,..}) if r==recovered)
        );
        assert!(m.take_verified_work_reconciliation().is_none());
        assert!(m.ready());
    }
}
#[test]
fn foreign_or_unsolicited_reconciliation_cannot_resolve_input() {
    for fault in [
        "operation",
        "delivery",
        "thread",
        "turn",
        "provider",
        "unsolicited",
    ] {
        let (mut m, _, mut frame, v) = prepared(false);
        if fault == "unsolicited" {
            m = connected(&v);
        } else {
            let key = match fault {
                "operation" => "operation_id",
                "delivery" => "delivery_id",
                "thread" => "thread_id",
                "turn" => "turn_id",
                _ => "provider",
            };
            frame["event"]["reconciliation"][key] = json!("foreign");
        }
        assert!(emit(&mut m, frame).is_err(), "{fault}");
        assert!(m.take_verified_work_reconciliation().is_none());
    }
}
#[test]
fn missing_or_changed_response_echo_keeps_connection_uncertain() {
    for fault in ["missing", "hash", "refused", "error"] {
        let (mut m, command, frame, _) = prepared(false);
        emit(&mut m, frame.clone()).unwrap();
        let mut response = result(&frame);
        match fault {
            "missing" => {
                response
                    .as_object_mut()
                    .unwrap()
                    .remove("work_reconciliation");
            }
            "hash" => response["work_reconciliation"]["history_sha256"] = json!("f".repeat(64)),
            "refused" => response["reconciled"] = json!(false),
            _ => {}
        }
        let received = if fault == "error" {
            m.accept(
                json!({"type":"response","id":command["id"],"ok":false,"error":"write failed"}),
            )
        } else {
            reply(&mut m, &command, response)
        };
        assert!(received.is_err(), "{fault}");
        assert!(m.take_verified_work_reconciliation().is_none());
        assert!(!m.ready());
    }
}
#[test]
fn stale_or_failed_queue_readback_never_publishes_resolution() {
    for fault in ["held", "identity", "error"] {
        let (mut m, command, frame, v) = prepared(true);
        emit(&mut m, frame.clone()).unwrap();
        let read = write(reply(&mut m, &command, result(&frame)).unwrap());
        assert_eq!(
            read["work_reconciliation"],
            json!({"instance":frame["event"]["instance"],"input_id":frame["event"]["input"]["input_id"],"operation_id":command["id"],"delivery_id":frame["event"]["reconciliation"]["delivery_id"]})
        );
        let mut data = readback(&frame, &v, fault == "held");
        if fault == "identity" {
            data["session_id"] = json!("foreign");
        }
        let result = if fault == "error" {
            m.accept(json!({"type":"response","id":read["id"],"ok":false,"error":"disk failed"}))
        } else {
            reply(&mut m, &read, data)
        };
        assert!(result.is_err(), "{fault}");
        assert!(m.take_verified_work_reconciliation().is_none());
        assert!(!m.ready());
    }
}

#[test]
fn closing_drains_reconciliation_without_new_read_or_cached_success() {
    for phase in ["event", "apply", "readback"] {
        let (mut m, command, frame, v) = prepared(false);
        if phase == "event" {
            m.begin_close();
        }
        emit(&mut m, frame.clone()).unwrap();
        if phase == "apply" {
            m.begin_close();
        }
        let step = reply(&mut m, &command, result(&frame)).unwrap();
        if phase == "readback" {
            let read = write(step);
            m.begin_close();
            assert!(matches!(
                reply(&mut m, &read, readback(&frame, &v, false)).unwrap(),
                Step::Ignore
            ));
        } else {
            assert!(matches!(step, Step::Ignore));
        }
        assert!(m.take_verified_work_reconciliation().is_none());
        assert!(m.request_queue(Intent::Open).is_err());
    }
}

#[test]
fn independent_readback_requires_every_exact_ticket_and_durable_hash() {
    for fault in [
        "missing",
        "kind",
        "version",
        "instance",
        "input_id",
        "operation_id",
        "delivery_id",
        "journal_instance",
        "journal_sha256",
        "payload_sha256",
        "history_sha256",
        "event_ledger_sha256",
        "ledger_sha256",
        "snapshot",
        "verified",
        "extra",
    ] {
        let (mut m, command, frame, v) = prepared(false);
        emit(&mut m, frame.clone()).unwrap();
        let read = write(reply(&mut m, &command, result(&frame)).unwrap());
        let mut data = readback(&frame, &v, false);
        if fault == "missing" {
            data.as_object_mut()
                .unwrap()
                .remove("work_reconciliation_readback");
        } else {
            data["work_reconciliation_readback"][fault] = json!("altered");
        }
        assert!(reply(&mut m, &read, data).is_err(), "{fault}");
        assert!(m.take_verified_work_reconciliation().is_none());
        assert!(!m.ready());
    }
}
