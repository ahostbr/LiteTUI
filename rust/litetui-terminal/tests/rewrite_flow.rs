//! Reviewed rewrite integration: no receipt alone releases composer admission.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_rewrite as r, history as h, history_view as hv,
    live::{Observation, Turn},
    live_chat::Chat,
    management::{Management, RewriteChanged, Step},
    theme::builtin_themes,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
const OP: &str = "rewrite-op";
const EXECUTION: &str = "42345678-1234-4123-8123-123456789abc";
const SECOND: &str = "52345678-1234-4123-8123-123456789abc";
const CARE: &str = "62345678-1234-4123-8123-123456789abc";
fn sha(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
fn reply(c: &Value, v: Value) -> Value {
    json!({"type":"response","id":c["id"],"ok":true,"result":v})
}
fn write(s: Step) -> Value {
    match s {
        Step::Write(v) => v,
        other => panic!("Expected read: {other:?}"),
    }
}
fn state() -> Value {
    json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict",
    "tool_policy":{},"model":{"model":"test","backend":"custom"},"ownership":{"writable":true,"data_version":1,"agent_id":AGENT},
    "schedules_paused":false,"restored_input":{"policy":"hold","held":0},"conversations":[],"jobs":[],"tasks":[],
    "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]})
}
fn descriptor(fresh: bool) -> Value {
    json!({"version":1,"kind":"history_snapshot","projection_version":1,
    "scope":"current","from_session_id":SESSION,"session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test",
    "catalog_snapshot":null,"snapshot":if fresh{"9".repeat(64)}else{"a".repeat(64)},"transcript_revision":"b".repeat(64),
    "context_sha256":if fresh{"8".repeat(64)}else{"c".repeat(64)},"replay_sha256":"d".repeat(64),
    "pending":false,"materialized":true,"title":"Current","read_only":false,"open_effects":null,"open_action_id":null,
    "fresh_system_context":false,"counts":{"messages":3,"system":1,"non_system":2,"blocks":2}})
}
fn page(d: &Value) -> Value {
    let mut p = json!({"version":1,"kind":"history_page","projection_version":1,"previous_cursor":null,"next_cursor":null,"at_start":true,"at_end":true,"fragments":[]});
    for k in [
        "scope",
        "from_session_id",
        "session_id",
        "agent_id",
        "backend",
        "model",
        "catalog_snapshot",
        "snapshot",
        "context_sha256",
        "replay_sha256",
        "counts",
    ] {
        p[k] = d[k].clone();
    }
    for (i, role, text) in [(0, "user", "original"), (1, "assistant", "answer")] {
        p["fragments"].as_array_mut().unwrap().push(json!({"block_index":i,"block_id":format!("{:064x}",i+1),
            "message_index":i+1,"message_sha256":"e".repeat(64),"kind":role,"role":role,"label":role,"state":"complete",
            "metadata":{"provider":null,"thread_id":null,"turn_id":null,"item_id":null,"tool_call_id":null,"ok":null,"duration_ms":null,"attachment_kind":null},
            "block_sha256":sha(text),"part":{"utf8_start":0,"utf8_end":text.len(),"utf8_total":text.len(),"sha256":sha(text)},"text":text}));
    }
    p
}
fn identity() -> h::Identity {
    h::Identity {
        from_session: SESSION.into(),
        session: SESSION.into(),
        agent: AGENT.into(),
        backend: "custom".into(),
        model: "test".into(),
        scope: h::Scope::Current,
        catalog: None,
    }
}
fn selection() -> r::Selection {
    r::Selection {
        history_key: hv::ReadKey {
            target: hv::Target::Current,
            generation: 1,
        },
        identity: identity(),
        history_snapshot: "a".repeat(64),
        context_sha256: "c".repeat(64),
        messages: 3,
        message_index: 1,
        message_sha256: "e".repeat(64),
    }
}
fn key(generation: u64) -> r::RequestKey {
    r::RequestKey {
        selection: selection(),
        generation,
    }
}
fn snapshot_value() -> Value {
    let mut v = selection().command(r::SNAPSHOT);
    for (k,value) in json!({"kind":"conversation_rewrite_snapshot","snapshot":"1".repeat(64),"original_text":"original","original_bytes":8,
        "prefix_messages":1,"prefix_context_sha256":"2".repeat(64),"discarded_messages":1,
        "actions":{"edit":{"eligible":true,"reason":null},"retry":{"eligible":true,"reason":null}}}).as_object().unwrap() {v[k]=value.clone();}
    v
}
fn snapshot() -> r::Snapshot {
    r::Snapshot::parse(&snapshot_value(), &selection()).unwrap()
}
fn review_value(text: &str) -> Value {
    let mut v = snapshot().request(r::Action::Edit, text);
    for (k,value) in json!({"kind":"conversation_rewrite_review","review":"3".repeat(64),"action_id":ACTION,"text":text,"text_sha256":sha(text),
        "prepared_context_sha256":"4".repeat(64),"prepared_messages":2,"prefix_context_sha256":"2".repeat(64),"prefix_messages":1,"discarded_messages":1,
        "effects":{"prompt_hooks":false,"provider_execution":true,"provider_rewind":false}}).as_object().unwrap() {v[k]=value.clone();}
    v
}
fn review() -> r::Review {
    r::Review::parse(
        &review_value("original"),
        snapshot(),
        r::Action::Edit,
        "original",
    )
    .unwrap()
}
fn receipt(review: &r::Review, operation: &str, accepted: bool) -> Value {
    let mut v = review.request();
    for (k,value) in json!({"kind":"conversation_rewrite_apply","operation_id":operation,"turn":if accepted{"accepted"}else{"refused"},
        "mutated":accepted,"close_required":false,"reason":if accepted{Value::Null}else{json!("not admitted")},
        "persistence":{"snapshot_written":accepted,"readback_verified":accepted},"refusal_history":if accepted{Value::Null}else{descriptor(true)}}).as_object().unwrap() {v[k]=value.clone();}
    v
}
fn queue() -> Value {
    json!({"session_id":SESSION,"backend":"custom","model":"test","policy":"hold","held":0,"snapshot":"f".repeat(64),"entries":[]})
}
fn connected(refresh: bool) -> Management {
    let mut m = Management::new(None);
    let c=m.begin(&json!({"type":"ready","history_projection_version":1,"management_protocols":[1],"capabilities":["hello","state"],
        "conversation_id":SESSION,"backend":"custom","model":"test","tool_profile":"strict"})).unwrap();
    let mut operations = vec![
        "gui.hello",
        "gui.state",
        h::SNAPSHOT,
        h::PAGE,
        h::OPEN,
        litetui_terminal::restored_queue::READ,
    ];
    operations.extend(r::OPERATIONS);
    let c=write(m.accept(reply(&c,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":operations}))).unwrap());
    let c = write(m.accept(reply(&c, state())).unwrap());
    let c = write(m.accept(reply(&c, descriptor(false))).unwrap());
    assert!(matches!(
        m.accept(reply(&c, page(&descriptor(false)))).unwrap(),
        Step::Restored { .. }
    ));
    if refresh {
        let c = m
            .request_history(hv::Intent::Refresh {
                key: selection().history_key,
                anchor: h::Anchor::Tail,
            })
            .unwrap();
        let c = write(m.accept(reply(&c, descriptor(false))).unwrap());
        assert!(matches!(
            m.accept(reply(&c, page(&descriptor(false)))).unwrap(),
            Step::SavedHistory(_)
        ));
    }
    m
}
fn read(m: &mut Management) -> Value {
    m.request_rewrite(r::Intent::Read {
        key: key(1),
        action: r::Action::Edit,
    })
    .unwrap()
}
fn reviewed(m: &mut Management) -> r::Intent {
    let c = read(m);
    let c = write(m.accept(reply(&c, snapshot_value())).unwrap());
    assert!(matches!(
        m.accept(reply(&c, state())).unwrap(),
        Step::Rewrite(r::Update::Snapshot { .. })
    ));
    let c = m
        .request_rewrite(r::Intent::Validate {
            key: key(2),
            snapshot: snapshot().token,
            action: r::Action::Edit,
            message: "original".into(),
        })
        .unwrap();
    assert!(matches!(
        m.accept(reply(&c, review_value("original"))).unwrap(),
        Step::Rewrite(r::Update::Review { .. })
    ));
    r::Intent::Apply {
        key: key(3),
        review: review().token,
    }
}
fn applied(m: &mut Management, accepted: bool) -> (Value, r::Admission) {
    let intent = reviewed(m);
    let p = m.prepare_rewrite(&intent).unwrap();
    let c = m.begin_rewrite(p, OP).unwrap();
    assert_eq!(c["type"], r::APPLY);
    assert_eq!(c["id"], OP);
    if accepted {
        pair(m, segment(0, Value::Null), EXECUTION).unwrap();
        pair(
            m,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
            EXECUTION,
        )
        .unwrap();
    }
    let admission = r::Admission::parse(&receipt(&review(), OP, accepted), &review(), OP).unwrap();
    (m.read_rewrite(admission.clone()).unwrap(), admission)
}
fn settled(admission: &r::Admission) -> Value {
    let mut v = receipt(
        &review(),
        OP,
        admission.outcome == r::AdmissionOutcome::Accepted,
    );
    v["kind"] = json!("conversation_rewrite_settled");
    v["worker_settled"] = json!(true);
    v["history"] = descriptor(true);
    v["execution"] = json!({"segments":if admission.outcome==r::AdmissionOutcome::Accepted{1}else{0},
        "last_execution_id":if admission.outcome==r::AdmissionOutcome::Accepted{json!(EXECUTION)}else{Value::Null}});
    v
}
fn finish(m: &mut Management, command: Value, admission: &r::Admission) -> RewriteChanged {
    let c = write(m.accept(reply(&command, settled(admission))).unwrap());
    finish_reads(m, c)
}
fn finish_reads(m: &mut Management, c: Value) -> RewriteChanged {
    assert_eq!(c["type"], "gui.state");
    let c = write(m.accept(reply(&c, state())).unwrap());
    assert_eq!(c["type"], h::SNAPSHOT);
    let c = write(m.accept(reply(&c, descriptor(true))).unwrap());
    assert_eq!(c["type"], h::PAGE);
    let c = write(m.accept(reply(&c, page(&descriptor(true)))).unwrap());
    assert_eq!(c["type"], litetui_terminal::restored_queue::READ);
    let Step::RewriteSettled(changed) = m.accept(reply(&c, queue())).unwrap() else {
        panic!()
    };
    *changed
}
#[test]
fn only_fresh_resident_current_user_selection_can_begin_review() {
    let mut stale = connected(false);
    assert!(
        stale
            .request_rewrite(r::Intent::Read {
                key: key(1),
                action: r::Action::Edit
            })
            .is_err()
    );
    for fault in ["generation", "message", "hash", "context", "target"] {
        let mut m = connected(true);
        let mut k = key(1);
        match fault {
            "generation" => k.selection.history_key.generation = 2,
            "message" => k.selection.message_index = 2,
            "hash" => k.selection.message_sha256 = "0".repeat(64),
            "context" => k.selection.context_sha256 = "0".repeat(64),
            "target" => {
                k.selection.history_key.target = hv::Target::Saved {
                    session: SESSION.into(),
                }
            }
            _ => unreachable!(),
        }
        assert!(
            m.request_rewrite(r::Intent::Read {
                key: k,
                action: r::Action::Edit
            })
            .is_err(),
            "{fault}"
        );
    }
}
#[test]
fn accepted_and_fresh_refused_checkpoint_require_all_independent_reads() {
    for accepted in [true, false] {
        let mut m = connected(true);
        let (c, a) = applied(&mut m, accepted);
        assert!(!m.ready());
        assert!(m.prompt("too-early").is_err());
        let changed = finish(&mut m, c, &a);
        assert_eq!(changed.admission, a);
        assert_eq!(changed.key, key(3));
        assert!(m.ready());
        m.prompt("next-explicit-prompt").unwrap();
    }
}
#[test]
fn read_refusals_are_recoverable_but_post_dispatch_read_failures_close() {
    for phase in 0..3 {
        let mut m = connected(true);
        let mut c = read(&mut m);
        if phase > 0 {
            c = write(m.accept(reply(&c, snapshot_value())).unwrap());
        }
        if phase > 1 {
            m.accept(reply(&c, state())).unwrap();
            c = m
                .request_rewrite(r::Intent::Validate {
                    key: key(2),
                    snapshot: snapshot().token,
                    action: r::Action::Edit,
                    message: "original".into(),
                })
                .unwrap();
        }
        let s = m
            .accept(json!({"type":"response","id":c["id"],"ok":false,"error":"stale"}))
            .unwrap();
        assert!(matches!(s, Step::Rewrite(r::Update::Rejected { .. })));
        assert!(m.ready());
    }
    for phase in 0..5 {
        let mut m = connected(true);
        let (mut c, a) = applied(&mut m, true);
        for value in [
            settled(&a),
            state(),
            descriptor(true),
            page(&descriptor(true)),
        ]
        .into_iter()
        .take(phase)
        {
            c = write(m.accept(reply(&c, value)).unwrap());
        }
        assert!(
            m.accept(json!({"type":"response","id":c["id"],"ok":false,"error":"readback failed"}))
                .is_err()
        );
        assert!(!m.ready());
    }
}
#[test]
fn readback_rejects_policy_context_and_queue_drift() {
    for fault in ["policy", "held", "context", "queue"] {
        let mut m = connected(true);
        let (c, a) = applied(&mut m, true);
        let c = write(m.accept(reply(&c, settled(&a))).unwrap());
        let mut s = state();
        if fault == "policy" {
            s["tool_policy"] = json!({"changed":true});
        }
        if fault == "held" {
            s["restored_input"]["held"] = json!(1);
        }
        let next = m.accept(reply(&c, s));
        if matches!(fault, "policy" | "held") {
            assert!(next.is_err(), "{fault}");
            continue;
        }
        let c = write(next.unwrap());
        let mut d = descriptor(true);
        if fault == "context" {
            d["context_sha256"] = json!("0".repeat(64));
        }
        let next = m.accept(reply(&c, d));
        if fault == "context" {
            assert!(next.is_err());
            continue;
        }
        let c = write(next.unwrap());
        let c = write(m.accept(reply(&c, page(&descriptor(true)))).unwrap());
        let mut q = queue();
        q["session_id"] = json!(AGENT);
        assert!(m.accept(reply(&c, q)).is_err());
    }
}
#[test]
fn original_response_bytes_include_errors_and_closing_never_schedules_followup() {
    for success in [true, false] {
        let mut m = connected(true);
        let c = read(&mut m);
        let frame = if success {
            reply(&c, snapshot_value())
        } else {
            json!({"type":"response","id":c["id"],"ok":false,"error":"small decoded error"})
        };
        assert!(m.accept_wire(frame, r::RESPONSE_BYTES + 1).is_err());
    }
    let mut m = connected(true);
    let c = read(&mut m);
    m.begin_close();
    assert!(matches!(
        m.accept(reply(&c, snapshot_value())).unwrap(),
        Step::Ignore
    ));
}
fn ready_turn() -> Turn {
    let mut t = Turn::default();
    t.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"custom","model":"test"}),"custom","test").unwrap();
    t
}
#[test]
fn rewrite_acknowledgement_is_not_completion_and_stream_can_precede_ack() {
    for early in [true, false] {
        let mut t = ready_turn();
        let op = t.submit_rewrite(&review()).unwrap();
        let ack = reply(&json!({"id":op}), receipt(&review(), &op, true));
        if early {
            assert!(matches!(
                t.accept(&ack, "custom", "test").unwrap(),
                Observation::Information(_)
            ));
        }
        t.accept(&identified_start(&op), "custom", "test").unwrap();
        assert!(matches!(
            t.accept(
                &json!({"type":"text_delta","text":"new answer"}),
                "custom",
                "test"
            )
            .unwrap(),
            Observation::Text(_)
        ));
        let end = t
            .accept(
                &json!({"type":"turn_end","stopReason":"stop","error":null}),
                "custom",
                "test",
            )
            .unwrap();
        if early {
            assert!(matches!(end, Observation::Complete));
        } else {
            assert!(matches!(end, Observation::Information(_)));
            assert!(matches!(
                t.accept(&ack, "custom", "test").unwrap(),
                Observation::Complete
            ));
        }
    }
}
#[test]
fn refusal_cannot_follow_execution_or_mask_malformed_persistence() {
    let mut t = ready_turn();
    let op = t.submit_rewrite(&review()).unwrap();
    assert!(matches!(
        t.accept(
            &reply(&json!({"id":op}), receipt(&review(), &op, false)),
            "custom",
            "test"
        )
        .unwrap(),
        Observation::Refused(_)
    ));
    for fault in ["started", "persistence"] {
        let mut t = ready_turn();
        let op = t.submit_rewrite(&review()).unwrap();
        if fault == "started" {
            t.accept(&identified_start(&op), "custom", "test").unwrap();
        }
        let mut v = receipt(&review(), &op, fault != "started");
        if fault == "persistence" {
            v["persistence"]["readback_verified"] = json!(false);
        }
        assert!(
            t.accept(&reply(&json!({"id":op}), v), "custom", "test")
                .is_err()
        );
    }
}
fn draw(chat: &mut Chat) {
    Terminal::new(TestBackend::new(100, 40))
        .unwrap()
        .draw(|f| chat.rewrite.draw(f, f.area(), &builtin_themes().remove(0)))
        .unwrap();
}
fn chat_rewrite(chat: &mut Chat) -> r::Intent {
    chat.rewrite.open(r::Action::Edit, selection()).unwrap();
    let i = chat.rewrite.take_intent().unwrap();
    chat.observe(Observation::Rewrite(r::Update::Snapshot {
        key: i.key().clone(),
        snapshot: Box::new(snapshot()),
    }))
    .unwrap();
    draw(chat);
    chat.rewrite
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let i = chat.rewrite.take_intent().unwrap();
    chat.observe(Observation::Rewrite(r::Update::Review {
        key: i.key().clone(),
        review: Box::new(review()),
    }))
    .unwrap();
    draw(chat);
    chat.rewrite
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    chat.rewrite.take_intent().unwrap()
}
#[test]
fn visible_history_changes_only_after_verified_settlement_and_new_answer_survives() {
    for accepted in [true, false] {
        let mut chat = Chat::new("custom".into(), "test".into());
        chat.observe(Observation::Ready).unwrap();
        chat.submit("old user").unwrap();
        chat.take_submission();
        chat.observe(Observation::Text("old answer".into()))
            .unwrap();
        chat.observe(Observation::Complete).unwrap();
        let intent = chat_rewrite(&mut chat);
        chat.prepare_rewrite(&intent).unwrap();
        assert!(chat.take_submission().is_none());
        chat.observe(Observation::Rewrite(r::Update::Submitted {
            key: intent.key().clone(),
        }))
        .unwrap();
        chat.observe(Observation::Submission { id: OP.into() })
            .unwrap();
        assert!(!chat.rewrite.owns_input());
        assert!(chat.rewrite_pending());
        if accepted {
            chat.observe(Observation::Text("new answer".into()))
                .unwrap();
            chat.observe(Observation::Complete).unwrap();
        } else {
            chat.observe(Observation::Refused("not admitted".into()))
                .unwrap();
        }
        assert_eq!(chat.rows.len(), 2);
        assert!(chat.submit("too early").is_err());
        let mut m = connected(true);
        let (c, a) = applied(&mut m, accepted);
        let changed = finish(&mut m, c, &a);
        chat.observe(Observation::RewriteSettled(Box::new(changed)))
            .unwrap();
        assert!(!chat.rewrite_pending());
        assert_eq!(chat.rows.len(), if accepted { 1 } else { 2 });
        assert_eq!(
            chat.rows[0].cards[0].answer,
            if accepted { "new answer" } else { "old answer" }
        );
        chat.submit("next explicit user").unwrap();
    }
}

fn segment(index: u64, previous: Value) -> Value {
    json!({"type":"turn_start","model":"test",
    "rewrite_segment":{"action_id":ACTION,"index":index,"previous_execution_id":previous}})
}
fn identified_start(op: &str) -> Value {
    let mut v = segment(0, Value::Null);
    v["rewrite_origin"] =
        json!({"session_id":SESSION,"operation_id":op,"execution_id":EXECUTION,"source":"rpc"});
    v
}
fn pair(m: &mut Management, event: Value, execution: &str) -> std::io::Result<Step> {
    pair_hook(
        m,
        event,
        execution,
        if execution == CARE {
            Value::Null
        } else {
            json!(ACTION)
        },
    )
}
fn pair_hook(
    m: &mut Management,
    event: Value,
    execution: &str,
    hook: Value,
) -> std::io::Result<Step> {
    assert!(matches!(m.accept(event.clone())?, Step::Ignore));
    m.accept(json!({"type":"gui.event","event_id":"abcdefab-1234-4123-8123-123456789abc",
        "session_id":SESSION,"operation_id":OP,"execution_id":execution,"hook_id":hook,"source":"rpc","event":event}))
}

fn care(phase: &str) -> Value {
    json!({"type":"rewrite_aftercare","action_id":ACTION,"aftercare_id":CARE,
    "phase":phase,"kind":"compaction","after_execution_id":EXECUTION})
}
#[test]
fn exact_compaction_worker_can_enclose_a_new_stream_segment_before_settled() {
    let mut m = connected(true);
    let (c, a) = applied(&mut m, true);
    pair(&mut m, care("start"), CARE).unwrap();
    let Step::Event(v) = pair(&mut m, segment(1, json!(EXECUTION)), SECOND).unwrap() else {
        panic!()
    };
    assert_eq!(v["rewrite_origin"]["execution_id"], SECOND);
    pair(
        &mut m,
        json!({"type":"turn_end","stopReason":"stop","error":null}),
        SECOND,
    )
    .unwrap();
    pair(&mut m, care("end"), CARE).unwrap();
    let mut final_value = settled(&a);
    final_value["execution"] = json!({"segments":2,"last_execution_id":SECOND});
    assert!(matches!(
        m.accept(reply(&c, final_value)).unwrap(),
        Step::Write(_)
    ));
}
#[test]
fn segment_replays_missing_terminals_and_unretired_workers_cannot_settle() {
    for fault in [
        "replay",
        "parent",
        "overlap",
        "worker",
        "summary",
        "forged_origin",
    ] {
        let mut m = connected(true);
        let (c, a) = applied(&mut m, true);
        match fault {
            "replay" => assert!(pair(&mut m, segment(1, json!(EXECUTION)), EXECUTION).is_err()),
            "parent" => assert!(pair(&mut m, segment(1, json!(SECOND)), SECOND).is_err()),
            "overlap" => {
                pair(&mut m, segment(1, json!(EXECUTION)), SECOND).unwrap();
                assert!(pair(&mut m, segment(2, json!(SECOND)), CARE).is_err());
            }
            "worker" => {
                pair(&mut m, care("start"), CARE).unwrap();
                assert!(m.accept(reply(&c, settled(&a))).is_err());
            }
            "summary" => {
                let mut v = settled(&a);
                v["execution"]["segments"] = json!(2);
                assert!(m.accept(reply(&c, v)).is_err());
            }
            "forged_origin" => {
                let mut v = segment(1, json!(EXECUTION));
                v["rewrite_origin"] = json!({});
                assert!(pair(&mut m, v, SECOND).is_err());
            }
            _ => unreachable!(),
        }
    }
}

#[test]
fn verified_readback_retires_only_old_rows_and_keeps_every_new_segment_and_maintenance_row() {
    let mut chat = Chat::new("custom".into(), "test".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("old user").unwrap();
    chat.take_submission();
    chat.observe(Observation::Text("old answer".into()))
        .unwrap();
    chat.observe(Observation::Complete).unwrap();
    let intent = chat_rewrite(&mut chat);
    chat.prepare_rewrite(&intent).unwrap();
    chat.observe(Observation::Rewrite(r::Update::Submitted {
        key: intent.key().clone(),
    }))
    .unwrap();
    chat.observe(Observation::Submission { id: OP.into() })
        .unwrap();
    chat.observe(Observation::RewriteSegment {
        operation_id: OP.into(),
        execution_id: EXECUTION.into(),
        index: 0,
        previous_outcome: None,
    })
    .unwrap();
    chat.observe(Observation::Text("first new answer".into()))
        .unwrap();
    chat.observe(Observation::Complete).unwrap();
    chat.observe(Observation::RewriteAftercare {
        operation_id: OP.into(),
        aftercare_id: CARE.into(),
        running: true,
        previous_outcome: None,
    })
    .unwrap();
    chat.observe(Observation::RewriteStatus {
        execution_id: CARE.into(),
        text: "Canonical compaction status".into(),
    })
    .unwrap();
    chat.observe(Observation::RewriteSegment {
        operation_id: OP.into(),
        execution_id: SECOND.into(),
        index: 1,
        previous_outcome: None,
    })
    .unwrap();
    chat.observe(Observation::Text("continued answer".into()))
        .unwrap();
    chat.observe(Observation::Complete).unwrap();
    assert!(chat.busy, "compaction worker still owns maintenance");
    chat.observe(Observation::RewriteAftercare {
        operation_id: OP.into(),
        aftercare_id: CARE.into(),
        running: false,
        previous_outcome: None,
    })
    .unwrap();
    assert!(!chat.busy);
    assert!(chat.rewrite_pending());
    assert_eq!(chat.rows.len(), 4);
    let mut m = connected(true);
    let (c, a) = applied(&mut m, true);
    pair(&mut m, care("start"), CARE).unwrap();
    pair(&mut m, segment(1, json!(EXECUTION)), SECOND).unwrap();
    pair(
        &mut m,
        json!({"type":"turn_end","stopReason":"stop","error":null}),
        SECOND,
    )
    .unwrap();
    pair(&mut m, care("end"), CARE).unwrap();
    let mut v = settled(&a);
    v["execution"] = json!({"segments":2,"last_execution_id":SECOND});
    let c = write(m.accept(reply(&c, v)).unwrap());
    let changed = finish_reads(&mut m, c);
    chat.observe(Observation::RewriteSettled(Box::new(changed)))
        .unwrap();
    assert!(!chat.rewrite_pending());
    assert_eq!(chat.rows.len(), 3);
    assert_eq!(chat.rows[0].cards[0].answer, "first new answer");
    assert_eq!(
        chat.rows[1].state,
        litetui_terminal::live_chat::ReplyState::MaintenanceSettled
    );
    assert_eq!(
        chat.rows[1].cards[0].answer,
        "Canonical compaction status\n"
    );
    assert_eq!(chat.rows[2].cards[0].answer, "continued answer");
    chat.submit("explicit next user").unwrap();
}

#[test]
fn canonical_nonnull_hook_identity_is_captured_and_cannot_drift_within_a_worker() {
    let mut m = connected(true);
    let intent = reviewed(&mut m);
    let prepared = m.prepare_rewrite(&intent).unwrap();
    m.begin_rewrite(prepared, OP).unwrap();
    pair(&mut m, segment(0, Value::Null), EXECUTION).unwrap();
    pair(
        &mut m,
        json!({"type":"text_delta","text":"accepted canonical hook origin"}),
        EXECUTION,
    )
    .unwrap();
    assert!(
        pair_hook(
            &mut m,
            json!({"type":"text_delta","text":"wrong hook"}),
            EXECUTION,
            json!(AGENT)
        )
        .is_err()
    );
    for drift in ["notice", "end"] {
        let mut m = connected(true);
        let (_c, _a) = applied(&mut m, true);
        pair_hook(&mut m, care("start"), CARE, json!(ACTION)).unwrap();
        let event = if drift == "end" {
            care("end")
        } else {
            json!({"type":"compaction","reason":"failed"})
        };
        assert!(
            pair_hook(&mut m, event, CARE, json!(AGENT)).is_err(),
            "{drift}"
        );
    }
}

#[test]
fn exact_input_closures_survive_worker_retirement_without_new_input_authority() {
    for maintenance in [false, true] {
        for (requested, closed) in [
            ("user_input_requested", "user_input_resolved"),
            ("tool_approval_requested", "tool_approval_closed"),
        ] {
            let mut m = connected(true);
            let intent = reviewed(&mut m);
            let prepared = m.prepare_rewrite(&intent).unwrap();
            m.begin_rewrite(prepared, OP).unwrap();
            pair(&mut m, segment(0, Value::Null), EXECUTION).unwrap();
            let owner = if maintenance {
                pair(
                    &mut m,
                    json!({"type":"turn_end","stopReason":"stop","error":null}),
                    EXECUTION,
                )
                .unwrap();
                pair(&mut m, care("start"), CARE).unwrap();
                CARE
            } else {
                EXECUTION
            };
            pair(
                &mut m,
                json!({"type":requested,"id":"owned-request"}),
                owner,
            )
            .unwrap();
            if maintenance {
                pair(&mut m, care("end"), CARE).unwrap();
            } else {
                pair(
                    &mut m,
                    json!({"type":"turn_end","stopReason":"cancelled","error":null}),
                    owner,
                )
                .unwrap();
            }
            pair(
                &mut m,
                json!({"type":closed,"id":"owned-request","cancelled":true}),
                owner,
            )
            .unwrap();
            assert!(pair(&mut m, json!({"type":requested,"id":"late-request"}), owner).is_err());
        }
    }
}
#[test]
fn retired_worker_input_closure_requires_the_exact_request_kind_and_full_origin() {
    for fault in ["unknown", "execution", "hook", "kind", "duplicate"] {
        let mut m = connected(true);
        let (_c, _a) = applied(&mut m, true);
        pair(&mut m, care("start"), CARE).unwrap();
        pair(
            &mut m,
            json!({"type":"user_input_requested","id":"owned-request"}),
            CARE,
        )
        .unwrap();
        pair(&mut m, care("end"), CARE).unwrap();
        let mut closed =
            json!({"type":"user_input_resolved","id":"owned-request","cancelled":true});
        if fault == "duplicate" {
            pair(&mut m, closed.clone(), CARE).unwrap();
        }
        if fault == "unknown" {
            closed["id"] = json!("other");
        }
        if fault == "kind" {
            closed["type"] = json!("tool_approval_closed");
        }
        let result = if fault == "hook" {
            pair_hook(&mut m, closed, CARE, json!(AGENT))
        } else {
            pair(
                &mut m,
                closed,
                if fault == "execution" {
                    EXECUTION
                } else {
                    CARE
                },
            )
        };
        assert!(result.is_err(), "{fault}");
    }
}

#[test]
fn source_settlement_cannot_erase_a_retired_workers_pending_question() {
    for closed in [false, true] {
        let mut m = connected(true);
        let (command, admission) = applied(&mut m, true);
        pair(&mut m, care("start"), CARE).unwrap();
        pair(
            &mut m,
            json!({"type":"user_input_requested","id":"pending"}),
            CARE,
        )
        .unwrap();
        pair(&mut m, care("end"), CARE).unwrap();
        if closed {
            pair(
                &mut m,
                json!({"type":"user_input_resolved","id":"pending","cancelled":true}),
                CARE,
            )
            .unwrap();
        }
        let result = m.accept(reply(&command, settled(&admission)));
        assert_eq!(result.is_ok(), closed);
    }
}

#[test]
fn interrupted_rewrite_receipt_during_quit_is_not_action_success_or_runtime_failure() {
    for closing in [false, true] {
        for malformed in [false, true] {
            let mut turn = ready_turn();
            let op = turn.submit_rewrite(&review()).unwrap();
            if closing {
                turn.begin_close();
            }
            let mut value = receipt(&review(), &op, true);
            value["turn"] = json!("failed");
            value["mutated"] = json!(false);
            value["close_required"] = json!(true);
            value["reason"] = if malformed {
                Value::Null
            } else {
                json!("CancelledError")
            };
            value["persistence"] = json!({"snapshot_written":false,"readback_verified":false});
            let response = reply(&json!({"id":op}), value);
            let result = turn.accept(&response, "custom", "test");
            if closing && !malformed {
                assert!(matches!(result.unwrap(), Observation::Information(_)));
                assert!(turn.submit("not admitted during shutdown").is_err());
                assert!(turn.accept(&response, "custom", "test").is_err());
            } else {
                assert!(result.is_err());
            }
        }
    }
}
