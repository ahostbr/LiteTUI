//! Manual maintenance admission stays reserved through independent saved-state reads.
use litetui_terminal::{
    conversation_compact as r, history as h, history_view as hv,
    management::{CompactChanged, Management, Step},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
const ROOT: &str = "42345678-1234-4123-8123-123456789abc";
const OP: &str = "compact-op";
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
    "context_sha256":"c".repeat(64),"replay_sha256":"d".repeat(64),
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
    }
}
fn key(generation: u64) -> r::RequestKey {
    r::RequestKey {
        selection: selection(),
        generation,
    }
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

fn snapshot_value() -> Value {
    let config = json!({"keep_recent":2,"max_tokens":1024,"max_tool_iters":4,"thinking_level":"default","tools_enabled":false,"wake_after_compact":false,"clear_screen_after_compact":true});
    let mut v = selection().command(r::SNAPSHOT);
    for(k,x)in json!({"kind":"conversation_compact_snapshot","snapshot":"1".repeat(64),"eligible":true,"reason":null,"config":config,"plan":{"messages":3,"system_messages":1,"head_messages":0,"tail_messages":2,"head_context_sha256":sha("[]"),"tail_context_sha256":"d".repeat(64),"prompt_sha256":"e".repeat(64),"config_sha256":sha(&serde_json::to_string(&config).unwrap())}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn snapshot() -> r::Snapshot {
    r::Snapshot::parse(&snapshot_value(), &selection()).unwrap()
}
fn review_value() -> Value {
    let before = snapshot_value();
    let mut v = snapshot().request("");
    for(k,x)in json!({"kind":"conversation_compact_review","plan":before["plan"],"config":before["config"],"review":"2".repeat(64),"action_id":ACTION,"action":"compact","root_execution_id":ROOT,"hint_sha256":sha(""),"request_prompt_sha256":"e".repeat(64),"effects":{"provider_execution":true,"provider_rewind":false,"tools_enabled":false,"wake_after_compact":false,"clear_screen_after_compact":true}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn review() -> r::Review {
    r::Review::parse(&review_value(), snapshot(), "").unwrap()
}
fn read(m: &mut Management) -> Value {
    m.request_compact(r::Intent::Read { key: key(1) }).unwrap()
}
fn reviewed(m: &mut Management) -> r::Intent {
    let c = read(m);
    let c = write(m.accept(reply(&c, snapshot_value())).unwrap());
    assert!(matches!(
        m.accept(reply(&c, state())).unwrap(),
        Step::Compact(r::Update::Snapshot { .. })
    ));
    let c = m
        .request_compact(r::Intent::Validate {
            key: key(2),
            snapshot: snapshot().token,
            hint: "".into(),
        })
        .unwrap();
    assert!(matches!(
        m.accept(reply(&c, review_value())).unwrap(),
        Step::Compact(r::Update::Review { .. })
    ));
    r::Intent::Apply {
        key: key(3),
        review: review().token,
    }
}
fn pair(m: &mut Management, event: Value) -> std::io::Result<Step> {
    assert!(matches!(m.accept(event.clone())?, Step::Ignore));
    m.accept(json!({"type":"gui.event","event_id":"abcdefab-1234-4123-8123-123456789abc","session_id":SESSION,"operation_id":OP,"execution_id":ROOT,"hook_id":null,"source":"rpc","event":event}))
}
fn root(phase: &str) -> Value {
    json!({"type":"compact_maintenance","action_id":ACTION,"aftercare_id":ROOT,"phase":phase,"kind":"compaction","root":true,"after_execution_id":null})
}
fn receipt(accepted: bool) -> Value {
    let mut v = review().request();
    for(k,x)in json!({"kind":"conversation_compact_apply","operation_id":OP,"status":if accepted{"accepted"}else{"refused"},"reason":if accepted{Value::Null}else{json!("not admitted")},"close_required":false,"refusal_history":if accepted{Value::Null}else{descriptor(true)}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn applied(m: &mut Management, accepted: bool) -> (Value, r::Admission) {
    let i = reviewed(m);
    let p = m.prepare_compact(&i).unwrap();
    let c = m.begin_compact(p, OP).unwrap();
    assert_eq!(c["type"], r::APPLY);
    assert!(m.prepare_compact(&i).is_err());
    if accepted {
        pair(m, root("start")).unwrap();
        pair(m, root("end")).unwrap();
    }
    let a = r::Admission::parse(&receipt(accepted), &review(), OP).unwrap();
    (m.read_compact(a.clone()).unwrap(), a)
}
fn settled(a: &r::Admission) -> Value {
    let accepted = a.status == r::AdmissionStatus::Accepted;
    let mut v = receipt(accepted);
    for(k,x)in json!({"kind":"conversation_compact_settled","worker_settled":true,"outcome":"no_change","reason_code":if accepted{"too_short"}else{"admission_refused"},"history_changed":false,"history":descriptor(true),"root_history":if accepted{descriptor(true)}else{Value::Null},"witness":{"root_started":accepted,"input_context_sha256":"c".repeat(64),"root_context_sha256":"c".repeat(64),"publication_attempted":false,"publication_verified":false},"execution":{"segments":0,"last_execution_id":null}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn finish(m: &mut Management, c: Value, a: &r::Admission) -> CompactChanged {
    let mut c = write(m.accept(reply(&c, settled(a))).unwrap());
    for value in [state(), descriptor(true), page(&descriptor(true))] {
        assert!(!m.ready());
        c = write(m.accept(reply(&c, value)).unwrap());
    }
    let Step::CompactSettled(changed) = m.accept(reply(&c, queue())).unwrap() else {
        panic!()
    };
    *changed
}
#[test]
fn current_descriptor_generation_and_exact_context_are_required() {
    let mut m = connected(false);
    assert!(m.request_compact(r::Intent::Read { key: key(1) }).is_err());
    for fault in 0..4 {
        let mut m = connected(true);
        let mut k = key(1);
        match fault {
            0 => k.selection.history_key.generation = 2,
            1 => k.selection.context_sha256 = "0".repeat(64),
            2 => k.selection.messages = 4,
            _ => {
                k.selection.history_key.target = hv::Target::Saved {
                    session: SESSION.into(),
                }
            }
        };
        assert!(m.request_compact(r::Intent::Read { key: k }).is_err());
    }
}
#[test]
fn zero_stream_root_and_refusal_both_require_all_independent_readbacks() {
    for accepted in [true, false] {
        let mut m = connected(true);
        let (c, a) = applied(&mut m, accepted);
        assert!(!m.ready());
        assert!(m.prompt("too-early").is_err());
        let change = finish(&mut m, c, &a);
        assert_eq!(change.key, key(3));
        assert_eq!(change.admission, a);
        assert_eq!(change.settlement.outcome, r::Outcome::NoChange);
        assert!(!change.settlement.history_changed);
        assert!(change.review.clear_display());
        assert!(m.ready());
        m.prompt("next-explicit").unwrap();
    }
}
#[test]
fn every_independent_read_failure_closes_the_dispatched_action() {
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
            m.accept(json!({"type":"response","id":c["id"],"ok":false,"error":"failed read"}))
                .is_err()
        );
        assert!(!m.ready());
    }
}
#[test]
fn raw_envelope_budget_applies_to_success_error_and_closing_reads() {
    for error in [true, false] {
        let mut m = connected(true);
        let c = read(&mut m);
        let frame = if error {
            json!({"type":"response","id":c["id"],"ok":false,"error":"stale"})
        } else {
            reply(&c, snapshot_value())
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
#[test]
fn read_refusal_recovers_but_missing_root_or_pending_question_cannot_settle() {
    let mut m = connected(true);
    let c = read(&mut m);
    assert!(matches!(
        m.accept(json!({"type":"response","id":c["id"],"ok":false,"error":"stale"}))
            .unwrap(),
        Step::Compact(r::Update::Rejected { .. })
    ));
    assert!(m.ready());
    for pending in [false, true] {
        let mut m = connected(true);
        let i = reviewed(&mut m);
        let p = m.prepare_compact(&i).unwrap();
        m.begin_compact(p, OP).unwrap();
        if pending {
            pair(&mut m, root("start")).unwrap();
            pair(&mut m,json!({"type":"user_input_requested","id":"ask","title":"Question","questions":[{"question":"continue?","options":[],"multiSelect":false}]})).unwrap();
            pair(&mut m, root("end")).unwrap();
        }
        let a = r::Admission::parse(&receipt(true), &review(), OP).unwrap();
        let c = m.read_compact(a.clone()).unwrap();
        assert!(m.accept(reply(&c, settled(&a))).is_err());
    }
}

fn draw_compact(chat: &mut litetui_terminal::live_chat::Chat) {
    ratatui::Terminal::new(ratatui::backend::TestBackend::new(110, 40))
        .unwrap()
        .draw(|f| {
            chat.compact.draw(
                f,
                f.area(),
                &litetui_terminal::theme::builtin_themes().remove(0),
            )
        })
        .unwrap();
}
fn compact_key(chat: &mut litetui_terminal::live_chat::Chat, code: crossterm::event::KeyCode) {
    chat.compact.key(crossterm::event::KeyEvent::new(
        code,
        crossterm::event::KeyModifiers::NONE,
    ));
}
fn chat_compact_case(clear: bool, outcome: r::Outcome) {
    use crossterm::event::KeyCode;
    use litetui_terminal::{live::Observation, live_chat::Chat, live_chat::ReplyState};
    let mut chat = Chat::new("custom".into(), "test".into());
    let original = h::Snapshot::parse(&descriptor(false), &identity()).unwrap();
    let mut history = litetui_terminal::resume::History::projected(original.clone());
    history.page = Some(Box::new(
        h::Page::parse(&page(&descriptor(false)), &original).unwrap(),
    ));
    chat.observe(Observation::History(history)).unwrap();
    chat.observe(Observation::Ready).unwrap();
    chat.submit("original").unwrap();
    assert_eq!(chat.take_submission().as_deref(), Some("original"));
    chat.observe(Observation::Submission {
        id: "prior-explicit-prompt".into(),
    })
    .unwrap();
    chat.observe(Observation::Text("answer".into())).unwrap();
    chat.observe(Observation::Complete).unwrap();
    assert_eq!(chat.rows.len(), 1);
    assert_eq!(chat.rows[0].state, ReplyState::Complete);
    assert!(!chat.rows[0].source_owned);

    // Drive the actual view and manager phases, including visible confirmation.
    let mut m = connected(true);
    chat.compact.open(selection()).unwrap();
    let intent = chat.compact.take_intent().unwrap();
    let c = m.request_compact(intent).unwrap();
    let mut before = snapshot_value();
    before["config"]["clear_screen_after_compact"] = json!(clear);
    before["plan"]["config_sha256"] =
        json!(sha(&serde_json::to_string(&before["config"]).unwrap()));
    let c = write(m.accept(reply(&c, before.clone())).unwrap());
    let Step::Compact(update) = m.accept(reply(&c, state())).unwrap() else {
        panic!()
    };
    chat.observe(Observation::Compact(update)).unwrap();
    draw_compact(&mut chat);
    compact_key(&mut chat, KeyCode::Enter);
    let intent = chat.compact.take_intent().unwrap();
    let c = m.request_compact(intent).unwrap();
    let mut reviewed = review_value();
    reviewed["plan"] = before["plan"].clone();
    reviewed["config"] = before["config"].clone();
    reviewed["effects"]["clear_screen_after_compact"] = json!(clear);
    let Step::Compact(update) = m.accept(reply(&c, reviewed)).unwrap() else {
        panic!()
    };
    chat.observe(Observation::Compact(update)).unwrap();
    let intent = (0..8)
        .find_map(|_| {
            draw_compact(&mut chat);
            compact_key(&mut chat, KeyCode::Enter);
            let intent = chat.compact.take_intent();
            if intent.is_none() {
                draw_compact(&mut chat);
                compact_key(&mut chat, KeyCode::PageDown);
            }
            intent
        })
        .expect("visible compaction review reaches Apply");
    let action_key = intent.key().clone();
    assert_eq!(action_key, key(3));
    let review = chat.compact.prepare_apply(&intent).unwrap().clone();
    assert_eq!(review.clear_display(), clear);
    chat.prepare_compact(&intent).unwrap();
    assert_eq!(chat.rows.len(), 1, "no fake prompt/turn is appended");
    assert!(chat.take_submission().is_none());
    let prepared = m.prepare_compact(&intent).unwrap();
    m.begin_compact(prepared, OP).unwrap();
    chat.observe(Observation::Compact(r::Update::Submitted {
        key: action_key,
    }))
    .unwrap();
    chat.observe(Observation::Submission { id: OP.into() })
        .unwrap();
    assert!(!chat.compact.owns_input());
    assert!(chat.rewrite_pending());
    assert!(chat.submit("cannot run before readback").is_err());
    for running in [true, false] {
        assert!(matches!(
            pair(&mut m, root(if running { "start" } else { "end" })).unwrap(),
            Step::Event(_)
        ));
        chat.observe(Observation::RewriteAftercare {
            operation_id: OP.into(),
            aftercare_id: ROOT.into(),
            running,
            previous_outcome: None,
        })
        .unwrap();
        if running {
            chat.observe(Observation::RewriteStatus {
                execution_id: ROOT.into(),
                text: "Captured source compaction status".into(),
            })
            .unwrap();
        }
    }
    assert_eq!(chat.rows.len(), 2);
    assert_eq!(chat.rows[0].prompt, "original");
    assert_eq!(chat.rows[0].cards[0].answer, "answer");
    assert_eq!(chat.rows[1].state, ReplyState::MaintenanceSettled);
    assert!(chat.busy, "root end alone does not release the composer");
    assert!(chat.submit("still cannot run before readback").is_err());

    let mut admitted = review.request();
    for (k, v) in json!({"kind":"conversation_compact_apply","operation_id":OP,"status":"accepted","reason":null,"close_required":false,"refusal_history":null}).as_object().unwrap() { admitted[k] = v.clone(); }
    let admission = r::Admission::parse(&admitted, &review, OP).unwrap();
    let c = m.read_compact(admission.clone()).unwrap();
    let mut after = descriptor(true);
    // A failed/no-op/cancelled action may retain audited tool effects. That
    // history change does not authorize clearing the old visible transcript.
    after["context_sha256"] = json!("8".repeat(64));
    let reason = match outcome {
        r::Outcome::Compacted => "compacted",
        r::Outcome::NoChange => "too_short",
        r::Outcome::Cancelled => "cancelled",
        r::Outcome::Failed => "no_summary",
    };
    let published = outcome == r::Outcome::Compacted;
    let mut settled = admitted;
    for (k, v) in json!({"kind":"conversation_compact_settled","worker_settled":true,"outcome":outcome.as_str(),"reason_code":reason,"history_changed":true,"history":after,"root_history":after,
        "witness":{"root_started":true,"input_context_sha256":"c".repeat(64),"root_context_sha256":"8".repeat(64),"publication_attempted":published,"publication_verified":published},"execution":{"segments":0,"last_execution_id":null}}).as_object().unwrap() { settled[k] = v.clone(); }
    let mut c = write(m.accept(reply(&c, settled)).unwrap());
    for value in [state(), after.clone(), page(&after)] {
        assert!(!m.ready());
        assert!(chat.rewrite_pending());
        c = write(m.accept(reply(&c, value)).unwrap());
    }
    let Step::CompactSettled(changed) = m.accept(reply(&c, queue())).unwrap() else {
        panic!()
    };
    assert_eq!(changed.settlement.outcome, outcome);
    assert!(changed.settlement.history_changed);
    assert_eq!(changed.settlement.execution_segments, 0);
    chat.observe(Observation::CompactSettled(changed)).unwrap();
    assert!(!chat.busy && !chat.rewrite_pending());
    assert_eq!(
        chat.history
            .as_ref()
            .unwrap()
            .descriptor
            .as_ref()
            .unwrap()
            .context_sha256,
        "8".repeat(64)
    );
    let removed = published && clear;
    assert_eq!(chat.rows.len(), if removed { 1 } else { 2 });
    assert_eq!(chat.prior_context_rows, usize::from(published && !clear));
    if !removed {
        assert_eq!(chat.rows[0].prompt, "original");
        assert_eq!(chat.rows[0].cards[0].answer, "answer");
        assert_eq!(chat.rows[0].state, ReplyState::Complete);
        assert!(!chat.rows[0].source_owned);
    }
    let maintenance = chat.rows.last().unwrap();
    assert!(maintenance.source_owned);
    assert_eq!(maintenance.prompt, "Conversation compaction");
    assert_eq!(
        maintenance.cards[0].answer,
        "Captured source compaction status\n"
    );
    assert_eq!(maintenance.state, ReplyState::MaintenanceSettled);
    assert!(m.ready());
    chat.submit("next explicit user").unwrap();
}
#[test]
fn verified_compaction_applies_reviewed_clear_or_retained_scrollback_boundary() {
    for clear in [true, false] {
        chat_compact_case(clear, r::Outcome::Compacted);
    }
}
#[test]
fn no_change_failed_and_cancelled_keep_prior_rows_despite_verified_audit_changes() {
    for outcome in [
        r::Outcome::NoChange,
        r::Outcome::Failed,
        r::Outcome::Cancelled,
    ] {
        chat_compact_case(true, outcome);
    }
}
