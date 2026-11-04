use litetui_terminal::{
    deferred_control as deferred, scheduled_transition as schedule,
    tool_mode::Profile,
    work_lifecycle::{self as life, Capture, Effect, Identity, Ledger},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn id(n: u64) -> String {
    format!("00000000-0000-4000-8000-{n:012x}")
}
fn hash(v: &Value) -> String {
    format!("{:x}", Sha256::digest(serde_json::to_vec(v).unwrap()))
}
fn identity() -> Identity {
    Identity {
        session: id(1),
        agent: id(2),
        backend: "custom".into(),
        model: "model".into(),
    }
}
fn target() -> Identity {
    Identity {
        session: id(3),
        ..identity()
    }
}
fn capabilities() -> schedule::Capabilities {
    schedule::Capabilities::negotiate(
        &schedule::OPERATIONS
            .iter()
            .map(|s| s.to_string())
            .collect::<Vec<_>>(),
    )
    .unwrap()
    .unwrap()
}
fn job() -> Value {
    json!({"id":"job-one","kind":"cron","prompt":"Canonical job Δ\0","tool_profile":"strict","new_conversation":true,"run_count":2,"last_fired_slot":null,"enabled":true})
}
fn proposal() -> Value {
    let job = job();
    let i = identity();
    json!({"proposal_id":id(10),"proposal_token":"c".repeat(64),"trigger":"automatic","job_id":"job-one","kind":"cron","source":"scheduled","requested_profile":"strict","due_slot":"2026-11-01T12:01","prior_run_count":2,"job_snapshot":serde_json::to_string(&job).unwrap(),"job_sha256":hash(&job),
 "transition":{"version":1,"kind":"conversation_transition_review","session_id":i.session,"agent_id":i.agent,"backend":i.backend,"model":i.model,"snapshot":"d".repeat(64),"review":"e".repeat(64),"action_id":id(11),"action":"new","target_session_id":id(3),"title":null,"effects":{"materialize":true,"provider_cleanup":false,"lifecycle_hooks":false}}})
}
fn parsed() -> schedule::Proposal {
    schedule::Proposal::parse(
        &proposal(),
        &identity(),
        &"a".repeat(64),
        life::Provider::Host,
    )
    .unwrap()
}
fn shutdown() -> Value {
    json!({"version":1,"instance":"a".repeat(64),"closing_sequence":2,"final_sequence":2,"admission_closed":true,"worker_settled":true,"callback_settled":true,"process_settled":true,"producer_ids":[],"inputs":[],"admission_ids":[],"ledger_sha256":"f".repeat(64),"effects_verified":false})
}
fn staged() -> Value {
    let p = proposal();
    let mut job = job();
    job["run_count"] = json!(3);
    job["last_fired_slot"] = json!("2026-11-01T12:01");
    let post = serde_json::to_string(&job).unwrap();
    let d = json!({"invocation_id":id(12),"job_id":"job-one","kind":"cron","generation":3,"source":"scheduled","requested_profile":"strict","payload":{"content":job["prompt"],"text":job["prompt"],"source":"scheduled","tool_profile":"strict"},"job_snapshot":post,"job_sha256":hash(&job),"epoch":{"instance":"a".repeat(64),"identity":identity().value(),"provider":"host"},"target":{"session_id":id(3),"agent_id":id(2)},"shutdown":shutdown(),"state":"held","reason":"Live owned invocation awaits a fresh Work activation"});
    let mut t = p["transition"].clone();
    t["kind"] = json!("conversation_transition_apply");
    t.as_object_mut().unwrap().remove("effects");
    t["session_id"] = json!(id(3));
    t["from_session_id"] = json!(id(1));
    t["status"] = json!("created");
    t["mutated"] = json!(true);
    t["close_required"] = json!(false);
    t["error"] = Value::Null;
    t["persistence"] = json!({"transcript":true,"settings":true});
    t["lifecycle"] =
        json!({"settled":true,"hooks_completed":true,"provider_cleanup_completed":true});
    t["work_shutdown"] = shutdown();
    t["work_recapture_required"] = json!(true);
    let document = json!({"version":1,"state":"pending","input_id":null,"record":d});
    json!({"kind":"work_schedule_result","version":1,"schedule_version":1,"operation_id":"accept-1","old_instance":"a".repeat(64),"proposal_id":p["proposal_id"],"proposal_token":p["proposal_token"],"pre_job_sha256":p["job_sha256"],"status":"staged","reason":null,"stamp":{"slot":"2026-11-01T12:01","run_count":3,"job_snapshot":post,"job_sha256":hash(&job)},"transition":t,"deferred":document["record"],"deferred_journal_sha256":hash(&document),"close_required":false})
}
fn decision() -> schedule::Decision {
    schedule::Decision::parse(
        &staged(),
        &parsed(),
        "accept-1",
        schedule::DecisionRequest::Accept,
    )
    .unwrap()
}
fn readback() -> Value {
    let d = staged();
    json!({"kind":"work_schedule_readback","version":1,"schedule_version":1,"identity":target().value(),"old_instance":"a".repeat(64),"proposal_id":d["proposal_id"],"accepted_operation_id":"accept-1","result_sha256":hash(&d),"result":d,"job_readback":{"job_snapshot":d["stamp"]["job_snapshot"],"job_sha256":d["stamp"]["job_sha256"]},"deferred_readback":{"document_sha256":d["deferred_journal_sha256"],"record":d["deferred"],"state":"pending","input_id":null},"verified":true})
}
fn capture() -> Capture {
    Capture::parse(&json!({"kind":"work_capture","version":1,"instance":"a".repeat(64),"activation_token":"b".repeat(64),"sequence":0,"identity":identity().value(),"provider":"host","limits":{"producers":256,"inputs":256,"payload_bytes":65536,"ledger_bytes":33554432,"envelope_bytes":131072,"reply_bytes":1048576},"recovered":[],"deferred_jobs":[]}),&identity()).unwrap()
}
fn active(scheduled: bool) -> Value {
    let mut v = json!({"kind":"work_active","version":1,"instance":"a".repeat(64),"sequence":0,"identity":identity().value()});
    if scheduled {
        v["scheduled_transition_version"] = json!(1);
    }
    v
}
fn ledger() -> Ledger {
    let mut l = Ledger::new(capture(), Profile::Strict);
    l.request_scheduled_transitions(&capabilities()).unwrap();
    l.activate(&active(true)).unwrap();
    l
}
fn event(n: u64) -> Value {
    json!({"type":"gui.event","event_id":id(100+n),"session_id":id(1),"operation_id":null,"hook_id":null,"source":"work","execution_id":null,"event":{"type":"work_transition_proposed","version":1,"instance":"a".repeat(64),"sequence":n,"identity":identity().value(),"scheduled_transition_version":1,"proposal":proposal()}})
}
#[test]
fn explicit_all_or_none_capability_and_exact_activation_echo() {
    assert!(schedule::Capabilities::negotiate(&[]).unwrap().is_none());
    for count in 1..6 {
        assert!(
            schedule::Capabilities::negotiate(
                &schedule::OPERATIONS[..count]
                    .iter()
                    .map(|s| s.to_string())
                    .collect::<Vec<_>>()
            )
            .is_err()
        );
    }
    let mut ops = schedule::OPERATIONS
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    ops.push(ops[0].clone());
    assert!(schedule::Capabilities::negotiate(&ops).is_err());
    assert_eq!(
        capture().activation_request_scheduled(&capabilities())["scheduled_transition_version"],
        1
    );
    for echo in [Value::Null, json!(true), json!("1"), json!(1.0), json!(2)] {
        let mut l = Ledger::new(capture(), Profile::Strict);
        l.request_scheduled_transitions(&capabilities()).unwrap();
        let mut a = active(true);
        a["scheduled_transition_version"] = echo;
        assert!(l.activate(&a).is_err());
        assert!(l.failed());
    }
    let mut l = Ledger::new(capture(), Profile::Strict);
    assert!(l.activate(&active(true)).is_err());
    let mut l = Ledger::new(capture(), Profile::Strict);
    l.activate(&active(false)).unwrap();
    assert!(!l.scheduled_transitions_active());
    assert!(l.observe(&event(1), None).is_err());
    let mut l = ledger();
    assert!(l.scheduled_transitions_active());
    assert!(l.request_scheduled_transitions(&capabilities()).is_err());
}
#[test]
fn proposal_is_evidence_without_admission_and_preserves_empty_shutdown() {
    let mut l = ledger();
    let Effect::ScheduledProposal { proposal: p } = l.observe(&event(1), None).unwrap() else {
        panic!()
    };
    assert_eq!(*p, parsed());
    assert_eq!(p.accept_request()["type"], schedule::ACCEPT);
    assert_eq!(
        p.decline_request("Local review is open").unwrap()["reason"],
        "Local review is open"
    );
    let mut closing = event(2);
    closing["event"] = json!({"type":"work_closing","version":1,"instance":"a".repeat(64),"sequence":2,"identity":identity().value(),"cutoff_sequence":2,"producer_ids":[],"input_ids":[],"admission_ids":[]});
    assert_eq!(l.observe(&closing, None).unwrap(), Effect::Closing);
    l.begin_close();
    l.verify_shutdown(&shutdown()).unwrap();
    assert!(l.observe(&event(3), None).is_err());
}
#[test]
fn proposal_rejects_stale_reused_envelope_and_review_authority() {
    for mutation in [
        "instance",
        "identity",
        "origin",
        "version",
        "duplicate",
        "token",
        "action",
        "closing",
    ] {
        let mut l = ledger();
        let mut e = event(1);
        match mutation {
            "instance" => e["event"]["instance"] = json!("9".repeat(64)),
            "identity" => e["event"]["identity"]["agent_id"] = json!(id(99)),
            "origin" => e["operation_id"] = json!("fake"),
            "version" => e["event"]["scheduled_transition_version"] = json!(true),
            "duplicate" | "token" | "action" => {
                l.observe(&e, None).unwrap();
                e = event(2);
                if mutation != "duplicate" {
                    e["event"]["proposal"]["proposal_id"] = json!(id(99));
                }
                if mutation == "action" {
                    e["event"]["proposal"]["proposal_token"] = json!("9".repeat(64));
                }
            }
            "closing" => {
                let mut c = event(1);
                c["event"] = json!({"type":"work_closing","version":1,"instance":"a".repeat(64),"sequence":1,"identity":identity().value(),"cutoff_sequence":1,"producer_ids":[],"input_ids":[],"admission_ids":[]});
                l.observe(&c, None).unwrap();
                e = event(2);
            }
            _ => unreachable!(),
        }
        let seq = l.sequence();
        assert!(l.observe(&e, None).is_err(), "{mutation}");
        assert_eq!(l.sequence(), seq);
        assert!(l.failed());
    }
}
#[test]
fn proposal_rejects_changed_job_hash_projection_target_and_calendar() {
    for mutation in [
        "hash",
        "source",
        "target",
        "review",
        "future-version",
        "slot",
        "profile",
        "extra",
        "snapshot-array",
        "duplicate-json",
    ] {
        let mut p = proposal();
        match mutation {
            "hash" => p["job_sha256"] = json!("0".repeat(64)),
            "source" => p["source"] = json!("user"),
            "target" => p["transition"]["target_session_id"] = json!(id(1)),
            "review" => p["transition"]["effects"]["materialize"] = json!(false),
            "future-version" => p["transition"]["version"] = json!(2),
            "slot" => p["due_slot"] = json!("2026-02-30T12:00"),
            "profile" => p["requested_profile"] = json!("autonomous"),
            "extra" => p["unreviewed"] = json!(true),
            "snapshot-array" | "duplicate-json" => {
                let raw = if mutation == "snapshot-array" {
                    "[]"
                } else {
                    "{\"id\":\"a\",\"id\":\"b\"}"
                };
                p["job_snapshot"] = json!(raw);
                p["job_sha256"] = json!(format!("{:x}", Sha256::digest(raw.as_bytes())));
            }
            _ => unreachable!(),
        }
        assert!(
            schedule::Proposal::parse(&p, &identity(), &"a".repeat(64), life::Provider::Host)
                .is_err(),
            "{mutation}"
        );
    }
}
#[test]
fn staged_result_requires_exact_pre_post_stamp_retirement_payload_and_document() {
    let d = decision();
    assert_eq!(d.status, schedule::Status::Staged);
    assert_eq!(d.after_identity(), target());
    assert_eq!(d.readback_request().unwrap()["session_id"], id(3));
    for mutation in [
        "operation",
        "generation",
        "post-hash",
        "pre-hash",
        "target",
        "retirement",
        "provider",
        "held",
        "payload",
        "journal",
        "refused-effects",
        "uncertain-open",
    ] {
        let mut v = staged();
        match mutation {
            "operation" => v["operation_id"] = json!("another"),
            "generation" => v["stamp"]["run_count"] = json!(4),
            "post-hash" => v["stamp"]["job_sha256"] = json!("0".repeat(64)),
            "pre-hash" => v["pre_job_sha256"] = json!("0".repeat(64)),
            "target" => v["deferred"]["target"]["session_id"] = json!(id(8)),
            "retirement" => v["transition"]["work_shutdown"]["callback_settled"] = json!(false),
            "provider" => v["deferred"]["epoch"]["provider"] = json!("codex"),
            "held" => v["deferred"]["state"] = json!("uncertain"),
            "payload" => v["deferred"]["payload"]["content"] = json!("unreviewed"),
            "journal" => v["deferred_journal_sha256"] = json!("0".repeat(64)),
            "refused-effects" => v["status"] = json!("refused"),
            "uncertain-open" => {
                v["status"] = json!("uncertain");
                v["reason"] = json!("unknown");
            }
            _ => unreachable!(),
        }
        assert!(
            schedule::Decision::parse(&v, &parsed(), "accept-1", schedule::DecisionRequest::Accept)
                .is_err(),
            "{mutation}"
        );
    }
    let mut uncertain = staged();
    uncertain["status"] = json!("uncertain");
    uncertain["close_required"] = json!(true);
    uncertain["reason"] = json!("Response publication uncertain");
    let u = schedule::Decision::parse(
        &uncertain,
        &parsed(),
        "accept-1",
        schedule::DecisionRequest::Accept,
    )
    .unwrap();
    assert!(u.readback_request().is_err());
}
#[test]
fn readback_requires_independent_exact_document_and_same_result() {
    let d = decision();
    schedule::Readback::parse(&readback(), &d).unwrap();
    for key in [
        "result_sha256",
        "result",
        "job_readback",
        "deferred_readback",
        "verified",
        "identity",
    ] {
        let mut r = readback();
        r[key] = Value::Null;
        assert!(schedule::Readback::parse(&r, &d).is_err(), "{key}");
    }
    let mut r = readback();
    r["deferred_readback"]["state"] = json!("delivered");
    assert!(schedule::Readback::parse(&r, &d).is_err());
}
fn selected() -> Value {
    json!({"record":staged()["deferred"],"journal_sha256":"4".repeat(64),"document_state":"uncertain","input_id":id(80),"discard":{"eligible":true,"reason":null}})
}
fn review() -> Value {
    let s = selected();
    json!({"kind":"work_deferred_review","version":1,"deferred_version":1,"identity":target().value(),"instance":"5".repeat(64),"snapshot":"6".repeat(64),"review":"7".repeat(64),"invocation_id":s["record"]["invocation_id"],"journal_sha256":s["journal_sha256"],"record":s["record"],"document_state":s["document_state"],"input_id":s["input_id"],"action":"discard","no_replay":true})
}
fn reviewed() -> deferred::Review {
    deferred::Review::parse(
        &review(),
        &target(),
        &"5".repeat(64),
        &"6".repeat(64),
        &deferred::Snapshot::parse(&selected(), &target()).unwrap(),
    )
    .unwrap()
}
fn terminal() -> Value {
    let r = review();
    json!({"version":2,"state":"discarded","input_id":r["input_id"],"record":r["record"],"retirement":{"operation_id":"discard-1","review":r["review"],"previous_sha256":r["journal_sha256"],"previous_state":r["document_state"],"identity":target().value(),"reason":"user_discarded","no_replay":true,"effects_undone":false}})
}
fn discarded() -> Value {
    let r = review();
    json!({"kind":"work_deferred_result","version":1,"deferred_version":1,"identity":target().value(),"instance":r["instance"],"operation_id":"discard-1","review":r["review"],"invocation_id":r["invocation_id"],"status":"discarded","before_sha256":r["journal_sha256"],"after_sha256":hash(&terminal()),"close_required":false,"reason":null})
}
#[test]
fn historical_discard_preserves_exact_raw_record_and_existing_input() {
    let selected = deferred::Snapshot::parse(&selected(), &target()).unwrap();
    assert_eq!(selected.document_state, deferred::JournalState::Uncertain);
    assert_eq!(selected.record.state, life::InputState::Held);
    let r = reviewed();
    let d = deferred::ResultReceipt::parse(&discarded(), &r, "discard-1").unwrap();
    assert_eq!(d.status, deferred::Status::Discarded);
    let j =
        deferred::Journal::parse(&serde_json::to_string(&terminal()).unwrap(), &target()).unwrap();
    assert!(!j.held());
    assert_eq!(j.input, Some(id(80)));
    assert_eq!(j.record, selected.record);
    let cmd = d.readback_request().unwrap();
    let receipt = json!({"kind":"work_deferred_readback","version":1,"deferred_version":1,"identity":target().value(),"instance":r.instance,"operation_id":"discard-1","review":r.token,"invocation_id":selected.record.invocation_id,"after_sha256":cmd["after_sha256"],"verified":true,"effects_undone":false,"no_replay":true});
    deferred::Readback::parse(&receipt, &d).unwrap();
    let mut bad = receipt;
    bad["effects_undone"] = json!(true);
    assert!(deferred::Readback::parse(&bad, &d).is_err());
}
#[test]
fn discard_review_result_and_audit_fail_closed_on_changed_authority() {
    for key in [
        "record",
        "journal_sha256",
        "input_id",
        "action",
        "identity",
        "no_replay",
    ] {
        let mut v = review();
        v[key] = Value::Null;
        assert!(
            deferred::Review::parse(
                &v,
                &target(),
                &"5".repeat(64),
                &"6".repeat(64),
                &deferred::Snapshot::parse(&selected(), &target()).unwrap()
            )
            .is_err(),
            "{key}"
        );
    }
    for key in [
        "operation_id",
        "before_sha256",
        "after_sha256",
        "close_required",
        "reason",
    ] {
        let mut v = discarded();
        v[key] = if key == "close_required" {
            json!(true)
        } else if key == "reason" {
            json!("unexpected")
        } else {
            Value::Null
        };
        assert!(
            deferred::ResultReceipt::parse(&v, &reviewed(), "discard-1").is_err(),
            "{key}"
        );
    }
    for key in ["previous_state", "identity", "no_replay", "effects_undone"] {
        let mut v = terminal();
        v["retirement"][key] = Value::Null;
        assert!(
            deferred::Journal::parse(&serde_json::to_string(&v).unwrap(), &target()).is_err(),
            "{key}"
        );
    }
}
#[test]
fn legacy_deferred_journal_remains_historical_and_rejects_version_confusion() {
    for state in ["pending", "uncertain", "delivered", "cancelled"] {
        let v = json!({"version":1,"state":state,"input_id":null,"record":staged()["deferred"]});
        let j = deferred::Journal::parse(&serde_json::to_string(&v).unwrap(), &target()).unwrap();
        assert_eq!(j.held(), matches!(state, "pending" | "uncertain"));
    }
    let mut v = terminal();
    v["version"] = json!(1);
    assert!(deferred::Journal::parse(&serde_json::to_string(&v).unwrap(), &target()).is_err());
    let raw = serde_json::to_string(&terminal()).unwrap().replacen(
        "\"version\":2",
        "\"version\":2,\"version\":2",
        1,
    );
    assert!(deferred::Journal::parse(&raw, &target()).is_err());
}

#[test]
fn canonical_unknown_stored_profile_migrates_without_rewriting_job_evidence() {
    for raw in ["", "scheduled", "future-profile"] {
        let mut p = proposal();
        let mut j = job();
        j["tool_profile"] = json!(raw);
        p["job_snapshot"] = json!(serde_json::to_string(&j).unwrap());
        p["job_sha256"] = json!(hash(&j));
        p["requested_profile"] = json!("interactive");
        let parsed =
            schedule::Proposal::parse(&p, &identity(), &"a".repeat(64), life::Provider::Host)
                .unwrap();
        assert_eq!(parsed.requested, Profile::Interactive);
        assert_eq!(
            serde_json::from_str::<Value>(&parsed.job_snapshot).unwrap()["tool_profile"],
            raw
        );
    }
}
#[test]
fn negotiated_work_snapshot_distinguishes_missing_from_empty_deferred_evidence() {
    use litetui_terminal::work_protocol as p;
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/work_inspection.json")).unwrap();
    let mut raw = fixture["snapshot"].clone();
    let i = p::Identity {
        session: id(3),
        agent: id(2),
        backend: "custom".into(),
        model: "model".into(),
    };
    raw["session_id"] = json!(i.session);
    raw["agent_id"] = json!(i.agent);
    raw["backend"] = json!(i.backend);
    raw["model"] = json!(i.model);
    let old = p::Snapshot::parse(&raw, &i).unwrap();
    assert!(!old.deferred_advertised);
    assert!(old.require_deferred(&capabilities()).is_err());
    raw["deferred_job"] = Value::Null;
    let empty = p::Snapshot::parse(&raw, &i).unwrap();
    empty.require_deferred(&capabilities()).unwrap();
    assert!(empty.deferred_job.is_none());
    raw["deferred_job"] = selected();
    let held = p::Snapshot::parse(&raw, &i).unwrap();
    held.require_deferred(&capabilities()).unwrap();
    assert!(held.deferred_job.is_some());
    raw["deferred_job"]["discard"]["reason"] = json!("Uncertainty remains");
    assert!(p::Snapshot::parse(&raw, &i).is_err());
    raw["deferred_job"] = json!(false);
    assert!(p::Snapshot::parse(&raw, &i).is_err());
}
#[test]
fn actual_source_cron_and_loop_proposals_follow_published_opt_in_without_effects() {
    for raw in [
        include_str!("fixtures/scheduled_cron_proposal.json"),
        include_str!("fixtures/scheduled_loop_proposal.json"),
    ] {
        let fixture: Value = serde_json::from_str(raw).unwrap();
        let identity = Identity::parse(&fixture["capture"]["identity"]).unwrap();
        let capture = Capture::parse(&fixture["capture"], &identity).unwrap();
        let mut ledger = Ledger::new(capture, Profile::Strict);
        ledger
            .request_scheduled_transitions(&capabilities())
            .unwrap();
        ledger.activate(&fixture["active"]).unwrap();
        let wire = fixture["wire"].as_array().unwrap();
        let envelope = wire
            .iter()
            .find(|w| w["type"] == "gui.event" && w["event"]["type"] == schedule::EVENT)
            .unwrap();
        let Effect::ScheduledProposal { proposal } = ledger.observe(envelope, None).unwrap() else {
            panic!("source proposal became execution")
        };
        assert_eq!(
            proposal.job_id,
            fixture["proposal"]["proposal"]["job_id"].as_str().unwrap()
        );
        assert_eq!(ledger.sequence(), 1);
        assert_eq!(fixture["unchanged"], true);
        assert_eq!(fixture["worker_started"], false);
        assert_eq!(wire[0]["type"], "response");
        assert_eq!(wire[0]["result"], fixture["active"]);
    }
}

#[test]
fn actual_source_staged_receipts_readbacks_and_target_capture_agree() {
    for raw in [
        include_str!("fixtures/scheduled_cron_handoff.json"),
        include_str!("fixtures/scheduled_loop_handoff.json"),
    ] {
        let fixture: Value = serde_json::from_str(raw).unwrap();
        let old = Identity::parse(&fixture["source_identity"]).unwrap();
        let target = Identity::parse(&fixture["capture"]["identity"]).unwrap();
        let capture = Capture::parse(&fixture["capture"], &target).unwrap();
        let proposal = schedule::Proposal::parse(
            &fixture["proposal"]["proposal"],
            &old,
            fixture["proposal"]["instance"].as_str().unwrap(),
            capture.provider,
        )
        .unwrap();
        let decision = schedule::Decision::parse(
            &fixture["result"],
            &proposal,
            fixture["accept_command"]["id"].as_str().unwrap(),
            schedule::DecisionRequest::Accept,
        )
        .unwrap();
        schedule::Readback::parse(&fixture["readback"], &decision).unwrap();
        assert_eq!(decision.status, schedule::Status::Staged);
        assert_eq!(decision.after_identity(), target);
        assert_eq!(
            capture.deferred_jobs.as_slice(),
            std::slice::from_ref(decision.deferred.as_ref().unwrap())
        );
        assert!(capture.recovered.is_empty());
        let mut source_request = fixture["readback_command"].clone();
        source_request.as_object_mut().unwrap().remove("id");
        assert_eq!(decision.readback_request().unwrap(), source_request);
        assert_eq!(fixture["provider_requests"], 1);
    }
}
#[test]
fn actual_source_pending_and_uncertain_discard_preserve_audit_and_verify_absence() {
    use litetui_terminal::work_protocol as work;
    let fixtures: Value =
        serde_json::from_str(include_str!("fixtures/deferred_source_flow.json")).unwrap();
    for state in ["pending", "uncertain"] {
        let fixture = &fixtures[state];
        let raw = &fixture["snapshot"];
        let identity = work::Identity {
            session: raw["session_id"].as_str().unwrap().into(),
            agent: raw["agent_id"].as_str().unwrap().into(),
            backend: raw["backend"].as_str().unwrap().into(),
            model: raw["model"].as_str().unwrap().into(),
        };
        let snapshot = work::Snapshot::parse(raw, &identity).unwrap();
        snapshot.require_deferred(&capabilities()).unwrap();
        let owned = Identity {
            session: identity.session.clone(),
            agent: identity.agent.clone(),
            backend: identity.backend.clone(),
            model: identity.model.clone(),
        };
        let selected = snapshot.deferred_job.as_ref().unwrap();
        let review = deferred::Review::parse(
            &fixture["review"],
            &owned,
            &snapshot.instance,
            &snapshot.token,
            selected,
        )
        .unwrap();
        let result = deferred::ResultReceipt::parse(
            &fixture["result"],
            &review,
            fixture["result"]["operation_id"].as_str().unwrap(),
        )
        .unwrap();
        deferred::Readback::parse(&fixture["readback"], &result).unwrap();
        let before =
            deferred::Journal::parse(fixture["before_journal"].as_str().unwrap(), &owned).unwrap();
        let after =
            deferred::Journal::parse(fixture["after_journal"].as_str().unwrap(), &owned).unwrap();
        assert!(before.held());
        assert!(!after.held());
        assert_eq!(before.record, after.record);
        assert_eq!(before.input, after.input);
        assert_eq!(result.after_sha256.as_ref(), Some(&after.sha256));
        let fresh = work::Snapshot::parse(&fixture["after_snapshot"], &identity).unwrap();
        fresh.require_deferred(&capabilities()).unwrap();
        assert!(fresh.deferred_job.is_none());
        assert_eq!(fresh.instance, snapshot.instance);
    }
}
