use litetui_terminal::{
    scheduled_transition::{Decision, DecisionRequest, ManualReview, Proposal, Readback, Status},
    work_lifecycle::{Identity, Provider},
    work_protocol::{self, Page, Row, Section, Snapshot},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn source_case(kind: &str) -> Value {
    serde_json::from_str::<Value>(include_str!("fixtures/manual_new_handoff.json")).unwrap()[kind]
        .clone()
}

struct Selected {
    snapshot: Snapshot,
    section: Section,
    row: Row,
    owner: Identity,
    instance: String,
}
impl Selected {
    fn from_source(case: &Value, section: Section) -> Self {
        let capture = &case["initial"]["capture"];
        let owner = Identity::parse(&capture["identity"]).unwrap();
        let inspection = &case["inspection"];
        let snapshot = Snapshot::parse(
            &inspection["snapshot"],
            &work_protocol::Identity {
                session: owner.session.clone(),
                agent: owner.agent.clone(),
                backend: owner.backend.clone(),
                model: owner.model.clone(),
            },
        )
        .unwrap();
        let row = Page::parse(&inspection["page"], &snapshot, section, 0, 1)
            .unwrap()
            .rows
            .remove(0);
        Self {
            snapshot,
            section,
            row,
            owner,
            instance: capture["instance"].as_str().unwrap().into(),
        }
    }
    fn review(&self, wire: &Value) -> std::io::Result<ManualReview> {
        ManualReview::parse(
            wire,
            &self.snapshot,
            self.section,
            &self.row,
            &self.instance,
            Provider::Host,
            "manual-review",
        )
    }
}

fn decision(case: &Value, review: &ManualReview) -> std::io::Result<Decision> {
    Decision::parse(
        &case["result"],
        &review.proposal,
        "accept",
        DecisionRequest::Accept,
    )
}

#[test]
fn source_disabled_cron_and_owned_loop_stamp_the_acceptance_minute_and_read_back() {
    for (kind, section) in [("cron", Section::Jobs), ("loop", Section::Loops)] {
        let case = source_case(kind);
        let selected = Selected::from_source(&case, section);
        let review = selected.review(&case["manual_review"]).unwrap();
        assert_eq!(review.wire, case["manual_review"]);
        assert_eq!(selected.row.raw["enabled"], false);
        assert_eq!(selected.row.raw["last_fired_slot"], "2026-11-04T12:04");
        let mut accept = review.proposal.accept_request();
        accept["id"] = json!("accept");
        assert_eq!(accept, case["accept"]);
        let decision = decision(&case, &review).unwrap();
        assert_eq!(decision.status, Status::Staged);
        let stamp = decision.stamp.as_ref().unwrap();
        assert_eq!(stamp.slot, "2026-11-04T12:06");
        assert_eq!(stamp.generation, 1);
        let post: Value = serde_json::from_str(&stamp.job_snapshot).unwrap();
        if kind == "loop" {
            assert_eq!(post["next_run_at"], "2026-11-04T12:23:17");
        }
        let mut read = decision.readback_request().unwrap();
        read["id"] = json!("readback");
        assert_eq!(read, case["readback_request"]);
        Readback::parse(&case["readback"], &decision).unwrap();
        assert_eq!(
            decision.after_identity().value(),
            case["target"]["capture"]["identity"]
        );
    }
}

#[test]
fn manual_review_binds_its_origin_selected_inspection_and_complete_pre_image() {
    let case = source_case("cron");
    let selected = Selected::from_source(&case, Section::Jobs);
    let wire = &case["manual_review"];
    for key in [
        "instance",
        "inspection_instance",
        "snapshot",
        "target_revision",
        "raw_before_sha256",
    ] {
        let mut forged = wire.clone();
        forged[key] = json!("2".repeat(64));
        assert!(selected.review(&forged).is_err(), "{key}");
    }
    for (key, value) in [
        ("operation_id", json!("other")),
        ("section", json!("loops")),
        ("extra", json!(null)),
    ] {
        let mut forged = wire.clone();
        forged[key] = value;
        assert!(selected.review(&forged).is_err(), "{key}");
    }
    let mut changed = wire.clone();
    let mut raw: Value = serde_json::from_str(changed["raw_before"].as_str().unwrap()).unwrap();
    raw["prompt"] = json!("different human selection");
    let text = serde_json::to_string(&raw).unwrap();
    changed["raw_before_sha256"] = json!(format!("{:x}", Sha256::digest(text.as_bytes())));
    changed["raw_before"] = json!(text);
    assert!(selected.review(&changed).is_err());
    for (key, value) in [
        ("trigger", json!("automatic")),
        ("due_slot", json!("2026-11-04T12:04")),
    ] {
        let mut forged = wire.clone();
        forged["proposal"][key] = value;
        assert!(selected.review(&forged).is_err(), "{key}");
    }
    assert!(
        Proposal::parse(
            &wire["proposal"],
            &selected.owner,
            &selected.instance,
            Provider::Host
        )
        .is_err()
    );
}

#[test]
fn manual_accept_checks_raw_extensions_and_source_loop_cadence() {
    let mut case = source_case("loop");
    let selected = Selected::from_source(&case, Section::Loops);
    let review = selected.review(&case["manual_review"]).unwrap();
    let staged = decision(&case, &review).unwrap();
    let mut readback = case["readback"].clone();
    readback["job_readback"]["raw_sha256"] = json!("2".repeat(64));
    assert!(Readback::parse(&readback, &staged).is_err());
    let stamp = &mut case["result"]["stamp"];
    stamp["raw_sha256"] = json!("2".repeat(64));
    assert!(
        decision(&case, &review)
            .unwrap_err()
            .to_string()
            .contains("raw job extension")
    );
    let stamp = &mut case["result"]["stamp"];
    let mut post: Value = serde_json::from_str(stamp["job_snapshot"].as_str().unwrap()).unwrap();
    post["next_run_at"] = json!("2026-11-04T12:21:17");
    let raw = serde_json::to_string(&post).unwrap();
    stamp["job_sha256"] = json!(format!("{:x}", Sha256::digest(raw.as_bytes())));
    stamp["job_snapshot"] = json!(raw);
    assert!(
        decision(&case, &review)
            .unwrap_err()
            .to_string()
            .contains("acceptance-time cadence")
    );
}
