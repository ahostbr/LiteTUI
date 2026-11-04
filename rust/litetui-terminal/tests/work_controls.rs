use litetui_terminal::{work_controls as c, work_protocol as p};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
fn fixture() -> Value {
    serde_json::from_str(include_str!("fixtures/work_controls.json")).unwrap()
}
fn snapshot(flow: &Value) -> p::Snapshot {
    let v = &flow["snapshot"];
    p::Snapshot::parse(
        v,
        &p::Identity {
            session: v["session_id"].as_str().unwrap().into(),
            agent: v["agent_id"].as_str().unwrap().into(),
            backend: v["backend"].as_str().unwrap().into(),
            model: v["model"].as_str().unwrap().into(),
        },
    )
    .unwrap()
}
fn action(flow: &Value) -> c::Action {
    match flow["review"]["action"].as_str().unwrap() {
        "job_create" => c::Action::JobCreate,
        "job_update" => c::Action::JobUpdate,
        "job_delete" => c::Action::JobDelete,
        "loop_enable" => c::Action::LoopEnable,
        "goal_pause" => c::Action::GoalPause,
        "goal_clear" => c::Action::GoalClear,
        _ => panic!(),
    }
}
fn selected(flow: &Value) -> Option<p::Row> {
    let target = flow.get("target")?;
    let s = snapshot(flow);
    let section = if target["kind"] == "loop" {
        p::Section::Loops
    } else {
        p::Section::Jobs
    };
    let mut page = s.request();
    page["kind"] = json!("work_page");
    page["section"] = json!(section.key());
    page["offset"] = json!(0);
    page["total"] = json!(1);
    page["next_offset"] = Value::Null;
    page["rows"] = json!([target]);
    Some(
        p::Page::parse(&page, &s, section, 0, 32)
            .unwrap()
            .rows
            .remove(0),
    )
}
fn edit(flow: &Value) -> c::Edit {
    c::Edit::new(
        &snapshot(flow),
        action(flow),
        selected(flow).as_ref(),
        flow["review"]["draft"].clone(),
    )
    .unwrap()
}
fn review(flow: &Value) -> c::Review {
    c::Review::parse(&flow["review"], &edit(flow)).unwrap()
}
fn resign(result: &mut Value) {
    result.as_object_mut().unwrap().remove("receipt");
    result["receipt"] = json!(format!(
        "{:x}",
        Sha256::digest(serde_json::to_vec(result).unwrap())
    ));
}
#[test]
fn every_source_export_requires_review_publication_and_independent_readback() {
    let flows = fixture();
    assert_eq!(flows.as_object().unwrap().len(), 11);
    for (name, flow) in flows.as_object().unwrap() {
        let edit = edit(flow);
        let r = c::Review::parse(&flow["review"], &edit).unwrap_or_else(|e| panic!("{name}: {e}"));
        let receipt =
            c::Receipt::parse(&flow["result"], &r).unwrap_or_else(|e| panic!("{name}: {e}"));
        let after = c::Readback::parse(&flow["readback"], &r, &receipt)
            .unwrap_or_else(|e| panic!("{name}: {e}"));
        assert_eq!(after.row, flow["readback"]["row"]);
        assert_eq!(r.request()["review"], flow["review"]["review"]);
        assert!(r.request().get("kind").is_none());
        assert_eq!(receipt.request(&r)["receipt"], flow["result"]["receipt"]);
        assert_eq!(edit.request()["action"], flow["review"]["action"]);
    }
}
#[test]
fn review_refuses_changed_effect_identity_scope_unknown_fields_and_authority() {
    let flows = fixture();
    let flow = &flows["jobs_job_update_off"];
    let edit = edit(flow);
    let mutations = [
        ("session_id", json!("foreign")),
        ("version", json!(true)),
        ("instance", json!("0".repeat(64))),
        ("snapshot", json!("0".repeat(64))),
        ("action", json!("job_delete")),
        ("target_id", json!("another")),
        ("target_revision", json!("0".repeat(64))),
        ("scope", json!("conversation")),
        ("raw_revision", json!("absent")),
        ("rearm_minutes", json!(1)),
        ("consequence", json!("Starts work")),
        ("extra", json!(false)),
        ("review", json!("short")),
    ];
    for (key, value) in mutations {
        let mut v = flow["review"].clone();
        v[key] = value;
        assert!(c::Review::parse(&v, &edit).is_err(), "{key}");
    }
    for key in ["prompt", "tool_profile", "owner_convo_id", "created"] {
        let mut v = flow["review"].clone();
        v["after"][key] = json!("forged");
        assert!(c::Review::parse(&v, &edit).is_err(), "{key}");
    }
    let mut v = flow["review"].clone();
    v["after"]["run_count"] = json!(8);
    assert!(c::Review::parse(&v, &edit).is_err());
}
#[test]
fn create_cannot_smuggle_runtime_state_or_loop_ownership() {
    let flows = fixture();
    for name in ["cron_create", "loop_create"] {
        let flow = &flows[name];
        let edit = edit(flow);
        for (key, value) in [
            ("run_count", json!(1)),
            ("last_fired_slot", json!("2026-11-01T12:00:00")),
            (
                "owner_convo_id",
                json!("11111111-2222-4333-8444-555555555555"),
            ),
            ("id", json!("foreign")),
            ("enabled", json!(true)),
        ] {
            let mut v = flow["review"].clone();
            v["after"][key] = value;
            assert!(c::Review::parse(&v, &edit).is_err(), "{name}/{key}");
        }
    }
}
#[test]
fn raw_draft_types_and_utf8_bounds_refuse_before_request() {
    let flows = fixture();
    let flow = &flows["jobs_job_update_off"];
    let s = snapshot(flow);
    let selected = selected(flow).unwrap();
    for draft in [
        json!({"enabled":1}),
        json!({"label":"Ω".repeat(2049)}),
        json!({"prompt":"x".repeat(65537)}),
        json!({"run_count":2}),
        json!({"tool_profile":"autonomous"}),
        json!({}),
        json!({"prompt":"  "}),
    ] {
        assert!(c::Edit::new(&s, c::Action::JobUpdate, Some(&selected), draft).is_err());
    }
    let exact = "a\0b\nΩ";
    assert_eq!(
        c::Edit::new(
            &s,
            c::Action::JobUpdate,
            Some(&selected),
            json!({"prompt":exact})
        )
        .unwrap()
        .draft["prompt"],
        exact
    );
}
#[test]
fn loop_resume_binds_policy_and_permits_only_source_timer_change() {
    let flows = fixture();
    let flow = &flows["loops_loop_enable_on"];
    let r = review(flow);
    assert_eq!(r.rearm_minutes, Some(15));
    let mut v = flow["result"].clone();
    v["row"]["interval_minutes"] = json!(16);
    resign(&mut v);
    assert!(c::Receipt::parse(&v, &r).is_err());
    let mut v = flow["result"].clone();
    v["row"]["next_run_at"] = Value::Null;
    resign(&mut v);
    assert!(c::Receipt::parse(&v, &r).is_err());
    let mut v = flow["review"].clone();
    v["rearm_minutes"] = json!(16);
    assert!(c::Review::parse(&v, &edit(flow)).is_err());
}
#[test]
fn goal_controls_preserve_evidence_and_only_clear_or_pause() {
    let flows = fixture();
    let flow = &flows["goal_pause"];
    let edit = edit(flow);
    for (key, value) in [
        ("status", json!("active")),
        ("evaluated_turns", json!(0)),
        ("objective", json!("Other")),
        ("started_by", json!("gui")),
    ] {
        let mut v = flow["review"].clone();
        v["after"][key] = value;
        assert!(c::Review::parse(&v, &edit).is_err(), "{key}");
    }
    let flow = &flows["goal_clear"];
    let r = review(flow);
    let receipt = c::Receipt::parse(&flow["result"], &r).unwrap();
    let mut v = flow["readback"].clone();
    v["row"] = flow["review"]["before"].clone();
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
}
#[test]
fn publication_requires_matching_digest_and_exact_saved_configuration() {
    let flows = fixture();
    let flow = &flows["jobs_job_update_off"];
    let r = review(flow);
    for (key, value) in [
        ("status", json!("pending")),
        ("mutated", json!(false)),
        ("close_required", json!(true)),
        ("receipt", json!("0".repeat(64))),
        ("store_revision", json!("absent")),
    ] {
        let mut v = flow["result"].clone();
        v[key] = value;
        assert!(c::Receipt::parse(&v, &r).is_err(), "{key}");
    }
    let mut v = flow["result"].clone();
    v["row"]["run_count"] = json!(8);
    resign(&mut v);
    assert!(c::Receipt::parse(&v, &r).is_err());
}
#[test]
fn independent_readback_allows_new_execution_facts_without_new_config_authority() {
    let flows = fixture();
    let flow = &flows["jobs_job_update_off"];
    let r = review(flow);
    let receipt = c::Receipt::parse(&flow["result"], &r).unwrap();
    let mut v = flow["readback"].clone();
    v["row"]["run_count"] = json!(8);
    v["row"]["last_fired_slot"] = json!("2026-11-01T12:01");
    assert!(c::Readback::parse(&v, &r, &receipt).is_ok());
    v["row"]["prompt"] = json!("Changed config");
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
    let mut v = flow["readback"].clone();
    v["row"]["run_count"] = json!(6);
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
    let mut v = flow["readback"].clone();
    v["verified"] = json!(false);
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
    let mut v = flow["readback"].clone();
    v["receipt"] = json!("0".repeat(64));
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
}

#[test]
fn advanced_cron_count_requires_consistent_slot_and_preserved_cadence() {
    let flows = fixture();
    let flow = &flows["jobs_job_update_off"];
    let r = review(flow);
    let mut receipt = c::Receipt::parse(&flow["result"], &r).unwrap();
    // The retained source receipt is the comparison authority for newer facts.
    receipt.row["last_fired_slot"] = json!("2026-11-01T12:00");
    let mut v = flow["readback"].clone();
    v["row"]["run_count"] = json!(8);
    for slot in [
        Value::Null,
        json!("2026-11-01T11:59"),
        json!("2026-11-01T12:00:00"),
        json!("2026-02-30T12:00"),
        json!("2026-11-01T24:00"),
        json!("2026-11-01 12:00"),
    ] {
        v["row"]["last_fired_slot"] = slot.clone();
        assert!(c::Readback::parse(&v, &r, &receipt).is_err(), "{slot}");
    }
    for slot in ["2026-11-01T12:00", "2026-11-01T12:01"] {
        v["row"]["last_fired_slot"] = json!(slot);
        c::Readback::parse(&v, &r, &receipt).unwrap();
    }
    v["row"]["next_run_at"] = json!("2026-11-01T12:16:00");
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
    v["row"]["next_run_at"] = Value::Null;
    v["instance"] = json!("0".repeat(64));
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
}
#[test]
fn advanced_loop_count_binds_canonical_cadence_but_allows_early_manual_run() {
    let flows = fixture();
    let flow = &flows["loops_loop_enable_on"];
    let r = review(flow);
    let receipt = c::Receipt::parse(&flow["result"], &r).unwrap();
    let mut v = flow["readback"].clone();
    v["row"]["run_count"] = json!(1);
    v["row"]["last_fired_slot"] = json!("2026-11-01T15:01");
    for next in [
        Value::Null,
        receipt.row["next_run_at"].clone(),
        json!("2026-11-01T15:16"),
        json!("2026-11-01T15:16:60"),
        json!("2026-11-01T15:15:59"),
        json!("2026-11-01T15:17:00"),
        json!("2026-11-01T15:16:00Z"),
        json!("2026-11-01T15:16:00.1"),
    ] {
        v["row"]["next_run_at"] = next.clone();
        assert!(c::Readback::parse(&v, &r, &receipt).is_err(), "{next}");
    }
    for (slot, next) in [
        ("2026-11-01T15:01", "2026-11-01T15:16:49"),
        ("2026-12-31T23:50", "2027-01-01T00:05:59"),
        ("2028-02-29T23:50", "2028-03-01T00:05:00"),
    ] {
        v["row"]["last_fired_slot"] = json!(slot);
        v["row"]["next_run_at"] = json!(next);
        c::Readback::parse(&v, &r, &receipt).unwrap();
    }
    let mut previous = receipt.clone();
    previous.row = v["row"].clone();
    v["row"]["run_count"] = json!(2);
    c::Readback::parse(&v, &r, &previous).unwrap(); // Manual re-fire in same second.
    v["row"]["last_fired_slot"] = json!("2027-02-29T23:50");
    assert!(c::Readback::parse(&v, &r, &receipt).is_err());
}
