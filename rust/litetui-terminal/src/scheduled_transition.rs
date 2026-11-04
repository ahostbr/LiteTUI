//! Source-proposed scheduler transitions. Tokens never authorize prompt execution.
use crate::{
    tool_mode::Profile,
    work_lifecycle::{
        self as life, DeferredJob, Identity, bad, bounded, hash, keys, number, optional, profile,
        text, uuid,
    },
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const ACCEPT: &str = "gui.work.native_schedule_accept";
pub const DECLINE: &str = "gui.work.native_schedule_decline";
pub const READBACK: &str = "gui.work.native_schedule_readback";
pub const OPERATIONS: [&str; 6] = [
    ACCEPT,
    DECLINE,
    READBACK,
    "gui.work.native_deferred_validate",
    "gui.work.native_deferred_discard",
    "gui.work.native_deferred_readback",
];
pub const EVENT: &str = "work_transition_proposed";
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Capabilities(());
impl Capabilities {
    pub fn negotiate(operations: &[String]) -> io::Result<Option<Self>> {
        let counts: Vec<_> = OPERATIONS
            .iter()
            .map(|op| operations.iter().filter(|v| *v == op).count())
            .collect();
        if counts.iter().all(|n| *n == 0) {
            return Ok(None);
        }
        if counts.iter().any(|n| *n != 1) {
            return Err(bad(
                "Scheduled transition capability is partial or duplicated",
            ));
        }
        Ok(Some(Self(())))
    }
}
pub(crate) fn digest(v: &Value) -> io::Result<String> {
    Ok(format!("{:x}", Sha256::digest(serde_json::to_vec(v)?)))
}
pub(crate) fn identity_at(v: &Value, expected: &Identity) -> io::Result<()> {
    if Identity::parse(&v["identity"])? != *expected {
        return Err(bad("Scheduled control changed owned identity"));
    }
    Ok(())
}
pub(crate) fn version(v: &Value, field: &str) -> io::Result<()> {
    if number(v, field, true)? != 1 {
        return Err(bad("Unsupported scheduled control version"));
    }
    Ok(())
}
pub(crate) fn boolean(v: &Value, field: &str) -> io::Result<bool> {
    v[field]
        .as_bool()
        .ok_or_else(|| bad("Scheduled control boolean is malformed"))
}
fn slot(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 16)?;
    local_minute(&s)?;
    Ok(s)
}
/// Canonical local scheduler minute, ordered without assuming a timezone.
pub(crate) fn local_minute(s: &str) -> io::Result<i64> {
    let b = s.as_bytes();
    if b.len() != 16
        || !b.iter().enumerate().all(|(i, c)| match i {
            4 | 7 => *c == b'-',
            10 => *c == b'T',
            13 => *c == b':',
            _ => c.is_ascii_digit(),
        })
    {
        return Err(bad("Malformed scheduler due slot"));
    }
    let part = |a, b| s[a..b].parse::<u32>().unwrap();
    let (y, m, d, h, n) = (
        part(0, 4),
        part(5, 7),
        part(8, 10),
        part(11, 13),
        part(14, 16),
    );
    let days = match m {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        2 => {
            if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) {
                29
            } else {
                28
            }
        }
        _ => 0,
    };
    if y == 0 || d == 0 || d > days || h > 23 || n > 59 {
        return Err(bad("Invalid scheduler calendar slot"));
    }
    let prior_year = i64::from(y) - 1;
    let months = [
        31,
        if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) {
            29
        } else {
            28
        },
        31,
        30,
        31,
        30,
        31,
        31,
        30,
        31,
        30,
        31,
    ];
    let ordinal = prior_year * 365 + prior_year / 4 - prior_year / 100
        + prior_year / 400
        + months[..(m - 1) as usize].iter().sum::<i64>()
        + i64::from(d)
        - 1;
    Ok(ordinal * 1440 + i64::from(h) * 60 + i64::from(n))
}
pub(crate) fn job_snapshot(v: &Value) -> io::Result<(String, String, Value)> {
    let raw = text(v, "job_snapshot", life::PAYLOAD_BYTES)?;
    let digest = hash(v, "job_sha256")?;
    if format!("{:x}", Sha256::digest(raw.as_bytes())) != digest {
        return Err(bad("Scheduled job snapshot hash changed"));
    }
    let parsed = crate::deferred_control::strict_object(&raw)?;
    Ok((raw, digest, parsed))
}
/// Canonical source review evidence; unlike a frontend Review this contains no
/// independently inspected Snapshot or invented eligibility.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct NewReview {
    pub identity: Identity,
    pub snapshot: String,
    pub review: String,
    pub action_id: String,
    pub target: String,
    pub wire: Value,
}
impl NewReview {
    fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        keys(
            v,
            &[
                "version",
                "kind",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "snapshot",
                "review",
                "action_id",
                "action",
                "target_session_id",
                "title",
                "effects",
            ],
        )?;
        version(v, "version")?;
        crate::conversation_actions::Identity {
            session: identity.session.clone(),
            agent: identity.agent.clone(),
            backend: identity.backend.clone(),
            model: identity.model.clone(),
        }
        .verify(v)?;
        keys(
            &v["effects"],
            &["materialize", "provider_cleanup", "lifecycle_hooks"],
        )?;
        for key in ["materialize", "provider_cleanup", "lifecycle_hooks"] {
            boolean(&v["effects"], key)?;
        }
        let target = uuid(v, "target_session_id")?;
        if v["kind"] != "conversation_transition_review"
            || v["action"] != "new"
            || v["title"] != Value::Null
            || v["effects"]["materialize"] != true
            || target == identity.session
        {
            return Err(bad("Scheduled proposal lacks exact canonical New review"));
        }
        Ok(Self {
            identity: identity.clone(),
            snapshot: hash(v, "snapshot")?,
            review: hash(v, "review")?,
            action_id: uuid(v, "action_id")?,
            target,
            wire: v.clone(),
        })
    }
    pub fn target_identity(&self) -> Identity {
        let mut i = self.identity.clone();
        i.session = self.target.clone();
        i
    }
    fn receipt(&self, v: &Value, success: bool, instance: &str) -> io::Result<()> {
        let mut expected = vec![
            "version",
            "kind",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "from_session_id",
            "snapshot",
            "review",
            "action_id",
            "action",
            "target_session_id",
            "title",
            "status",
            "mutated",
            "close_required",
            "error",
            "persistence",
            "lifecycle",
        ];
        if v.get("work_shutdown").is_some() || v.get("work_recapture_required").is_some() {
            expected.extend(["work_shutdown", "work_recapture_required"]);
        }
        if v.get("work_execution_shutdown").is_some() {
            expected.push("work_execution_shutdown");
        }
        keys(v, &expected)?;
        if let Some(value) = v.get("work_execution_shutdown") {
            let proof = crate::work_execution_controls::Shutdown::parse(value)?;
            if proof.identity != self.identity || proof.work_instance != instance {
                return Err(bad("Scheduled New changed its execution retirement epoch"));
            }
        }
        version(v, "version")?;
        if v["kind"] != "conversation_transition_apply"
            || v["from_session_id"] != self.identity.session
        {
            return Err(bad("Scheduled transition receipt changed source"));
        }
        for k in [
            "agent_id",
            "backend",
            "model",
            "snapshot",
            "review",
            "action_id",
            "action",
            "target_session_id",
            "title",
        ] {
            if v[k] != self.wire[k] {
                return Err(bad("Scheduled transition changed its retained review"));
            }
        }
        if v["session_id"] != self.identity.session && v["session_id"] != self.target {
            return Err(bad("Scheduled transition returned a foreign session"));
        }
        keys(&v["persistence"], &["transcript", "settings"])?;
        keys(
            &v["lifecycle"],
            &["settled", "hooks_completed", "provider_cleanup_completed"],
        )?;
        for k in ["mutated", "close_required"] {
            boolean(v, k)?;
        }
        for k in ["transcript", "settings"] {
            boolean(&v["persistence"], k)?;
        }
        for k in ["settled", "hooks_completed", "provider_cleanup_completed"] {
            boolean(&v["lifecycle"], k)?;
        }
        let error = optional(v, "error", 4096)?;
        if success || v["status"] == "created" {
            if v["status"] != "created"
                || v["session_id"] != self.target
                || v["mutated"] != true
                || v["close_required"] != false
                || error.is_some()
                || v["persistence"]["transcript"] != true
                || v["persistence"]["settings"] != true
                || ["settled", "hooks_completed", "provider_cleanup_completed"]
                    .iter()
                    .any(|k| v["lifecycle"][*k] != true)
                || v["work_recapture_required"] != true
            {
                return Err(bad("Scheduled New lacks settled canonical persistence"));
            }
        } else if v["status"] == "refused" {
            if v["session_id"] != self.identity.session
                || v["mutated"] != false
                || v["close_required"] != false
                || error.is_none()
                || ["transcript", "settings"]
                    .iter()
                    .any(|k| v["persistence"][*k] != false)
                || ["settled", "hooks_completed", "provider_cleanup_completed"]
                    .iter()
                    .any(|k| v["lifecycle"][*k] != false)
            {
                return Err(bad("Refused canonical New receipt claims effects"));
            }
        } else if v["status"] != "failed" || v["close_required"] != true || error.is_none() {
            return Err(bad("Failed canonical New receipt lacks uncertainty"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Proposal {
    pub identity: Identity,
    pub instance: String,
    pub provider: life::Provider,
    pub id: String,
    pub token: String,
    pub job_id: String,
    pub kind: String,
    pub requested: Profile,
    pub due_slot: String,
    pub prior_run_count: u64,
    pub job_snapshot: String,
    pub job_sha256: String,
    pub transition: NewReview,
}
impl Proposal {
    pub fn parse(
        v: &Value,
        identity: &Identity,
        instance: &str,
        provider: life::Provider,
    ) -> io::Result<Self> {
        bounded(v, life::ENVELOPE_BYTES)?;
        keys(
            v,
            &[
                "proposal_id",
                "proposal_token",
                "trigger",
                "job_id",
                "kind",
                "source",
                "requested_profile",
                "due_slot",
                "prior_run_count",
                "job_snapshot",
                "job_sha256",
                "transition",
            ],
        )?;
        let (job_snapshot, job_sha256, job) = job_snapshot(v)?;
        let job_id = text(v, "job_id", 128)?;
        let kind = text(v, "kind", 16)?;
        let prior_run_count = number(v, "prior_run_count", false)?;
        let requested = profile(v, "requested_profile")?;
        let raw_profile = job["tool_profile"]
            .as_str()
            .filter(|s| s.len() <= 4096)
            .ok_or_else(|| bad("Invalid stored job profile"))?;
        let effective = Profile::parse(raw_profile).unwrap_or(Profile::Interactive);
        if !matches!(kind.as_str(), "cron" | "loop")
            || v["trigger"] != "automatic"
            || v["source"] != "scheduled"
            || job["id"] != job_id
            || job["kind"] != kind
            || job["new_conversation"] != true
            || job["enabled"] != true
            || number(&job, "run_count", false)? != prior_run_count
            || effective != requested
            || job["prompt"]
                .as_str()
                .is_none_or(|s| s.is_empty() || s.len() > life::PAYLOAD_BYTES)
        {
            return Err(bad("Scheduled proposal changed canonical job projection"));
        }
        let due_slot = slot(v, "due_slot")?;
        if job["last_fired_slot"] == due_slot {
            return Err(bad("Scheduled proposal already consumed its slot"));
        }
        Ok(Self {
            identity: identity.clone(),
            instance: instance.into(),
            provider,
            id: uuid(v, "proposal_id")?,
            token: hash(v, "proposal_token")?,
            job_id,
            kind,
            requested,
            due_slot,
            prior_run_count,
            job_snapshot,
            job_sha256,
            transition: NewReview::parse(&v["transition"], identity)?,
        })
    }
    fn request(&self, operation: &str) -> Value {
        let mut v = self.identity.request(operation);
        v["schedule_version"] = json!(1);
        v["instance"] = json!(self.instance);
        v["proposal_id"] = json!(self.id);
        v["proposal_token"] = json!(self.token);
        v
    }
    pub fn accept_request(&self) -> Value {
        self.request(ACCEPT)
    }
    pub fn decline_request(&self, reason: &str) -> io::Result<Value> {
        let mut v = self.request(DECLINE);
        v["reason"] = json!(reason);
        text(&v, "reason", 2048)?;
        Ok(v)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum DecisionRequest {
    Accept,
    Decline,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Declined,
    Refused,
    Staged,
    Uncertain,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Stamp {
    pub slot: String,
    pub generation: u64,
    pub job_snapshot: String,
    pub job_sha256: String,
}
impl Stamp {
    fn parse(v: &Value, p: &Proposal) -> io::Result<Self> {
        keys(v, &["slot", "run_count", "job_snapshot", "job_sha256"])?;
        let (job_snapshot, job_sha256, job) = job_snapshot(v)?;
        let generation = number(v, "run_count", true)?;
        if slot(v, "slot")? != p.due_slot
            || Some(generation) != p.prior_run_count.checked_add(1)
            || job["id"] != p.job_id
            || job["kind"] != p.kind
            || job["last_fired_slot"] != p.due_slot
            || job["run_count"] != generation
        {
            return Err(bad("Scheduled stamp changed canonical invocation"));
        }
        let before = crate::deferred_control::strict_object(&p.job_snapshot)?;
        let mut after = job.clone();
        let mut previous = before.clone();
        for k in ["last_fired_slot", "run_count"] {
            after.as_object_mut().unwrap().remove(k);
            previous.as_object_mut().unwrap().remove(k);
        }
        if p.kind == "loop" {
            after.as_object_mut().unwrap().remove("next_run_at");
            previous.as_object_mut().unwrap().remove("next_run_at");
            text(&job, "next_run_at", 128)?;
        }
        if after != previous {
            return Err(bad("Scheduled stamp changed unrelated job fields"));
        }
        Ok(Self {
            slot: p.due_slot.clone(),
            generation,
            job_snapshot,
            job_sha256,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Decision {
    pub proposal: Proposal,
    pub operation: String,
    pub status: Status,
    pub reason: Option<String>,
    pub stamp: Option<Stamp>,
    pub transition: Option<Value>,
    pub deferred: Option<DeferredJob>,
    pub deferred_journal_sha256: Option<String>,
    pub wire: Value,
}
impl Decision {
    pub fn parse(
        v: &Value,
        p: &Proposal,
        operation: &str,
        request: DecisionRequest,
    ) -> io::Result<Self> {
        bounded(v, life::REPLY_BYTES)?;
        keys(
            v,
            &[
                "kind",
                "version",
                "schedule_version",
                "operation_id",
                "old_instance",
                "proposal_id",
                "proposal_token",
                "pre_job_sha256",
                "status",
                "reason",
                "stamp",
                "transition",
                "deferred",
                "deferred_journal_sha256",
                "close_required",
            ],
        )?;
        version(v, "version")?;
        version(v, "schedule_version")?;
        if v["kind"] != "work_schedule_result"
            || text(v, "operation_id", 128)? != operation
            || hash(v, "old_instance")? != p.instance
            || uuid(v, "proposal_id")? != p.id
            || hash(v, "proposal_token")? != p.token
            || hash(v, "pre_job_sha256")? != p.job_sha256
        {
            return Err(bad("Scheduled decision changed request authority"));
        }
        let status = match text(v, "status", 16)?.as_str() {
            "declined" => Status::Declined,
            "refused" => Status::Refused,
            "staged" => Status::Staged,
            "uncertain" => Status::Uncertain,
            _ => return Err(bad("Unknown scheduled decision")),
        };
        let reason = optional(v, "reason", 2048)?;
        let close = boolean(v, "close_required")?;
        let stamp = if v["stamp"].is_null() {
            None
        } else {
            Some(Stamp::parse(&v["stamp"], p)?)
        };
        let transition = if v["transition"].is_null() {
            None
        } else {
            p.transition
                .receipt(&v["transition"], status == Status::Staged, &p.instance)?;
            Some(v["transition"].clone())
        };
        let deferred = if v["deferred"].is_null() {
            None
        } else {
            Some(DeferredJob::parse(
                &v["deferred"],
                &p.transition.target_identity(),
            )?)
        };
        let journal = if v["deferred_journal_sha256"].is_null() {
            None
        } else {
            Some(hash(v, "deferred_journal_sha256")?)
        };
        if request == DecisionRequest::Decline && status != Status::Declined
            || request == DecisionRequest::Accept && status == Status::Declined
        {
            return Err(bad("Scheduled decision differs from requested action"));
        }
        match status {
            Status::Declined | Status::Refused
                if close
                    || reason.is_none()
                    || stamp.is_some()
                    || transition.is_some()
                    || deferred.is_some()
                    || journal.is_some() =>
            {
                return Err(bad("Pre-effect scheduler refusal claims effects"));
            }
            Status::Staged
                if close
                    || reason.is_some()
                    || stamp.is_none()
                    || transition.is_none()
                    || deferred.is_none()
                    || journal.is_none() =>
            {
                return Err(bad("Scheduled stage lacks exact proofs"));
            }
            Status::Uncertain if !close || reason.is_none() => {
                return Err(bad("Scheduled uncertainty must retain closing obligation"));
            }
            _ => {}
        }
        if let Some(d) = &deferred {
            let s = stamp
                .as_ref()
                .ok_or_else(|| bad("Deferred scheduled result lacks stamp"))?;
            if d.job_id != p.job_id
                || d.generation != s.generation
                || d.job_snapshot != s.job_snapshot
                || d.job_sha256 != s.job_sha256
                || d.requested != p.requested
                || d.epoch.instance != p.instance
                || d.epoch.identity != p.identity
                || d.epoch.provider != p.provider
                || status == Status::Staged && d.state != life::InputState::Held
                || d.source != "scheduled"
                || (p.kind == "cron") != (d.kind == life::ProducerKind::Cron)
            {
                return Err(bad("Deferred scheduled result changed original authority"));
            }
            let job = crate::deferred_control::strict_object(&s.job_snapshot)?;
            if d.payload.content != job["prompt"].as_str().unwrap_or("")
                || d.payload.display_text() != d.payload.content
            {
                return Err(bad("Deferred scheduled payload differs from canonical job"));
            }
            if let Some(t) = &transition
                && t["work_shutdown"] != d.shutdown
            {
                return Err(bad("Deferred job changed old epoch retirement"));
            }
            let document =
                json!({"version":1,"state":"pending","input_id":null,"record":v["deferred"]});
            if journal.as_deref() != Some(digest(&document)?.as_str()) {
                return Err(bad(
                    "Deferred journal hash differs from exact staged document",
                ));
            }
        }
        Ok(Self {
            proposal: p.clone(),
            operation: operation.into(),
            status,
            reason,
            stamp,
            transition,
            deferred,
            deferred_journal_sha256: journal,
            wire: v.clone(),
        })
    }
    pub fn result_sha256(&self) -> io::Result<String> {
        digest(&self.wire)
    }
    pub fn after_identity(&self) -> Identity {
        if self.status == Status::Staged {
            self.proposal.transition.target_identity()
        } else {
            self.proposal.identity.clone()
        }
    }
    pub fn readback_request(&self) -> io::Result<Value> {
        if self.status == Status::Uncertain {
            return Err(bad("Uncertain scheduling cannot authorize recapture"));
        }
        let mut v = self.after_identity().request(READBACK);
        v["schedule_version"] = json!(1);
        v["old_instance"] = json!(self.proposal.instance);
        v["proposal_id"] = json!(self.proposal.id);
        v["proposal_token"] = json!(self.proposal.token);
        v["accepted_operation_id"] = json!(self.operation);
        v["result_sha256"] = json!(self.result_sha256()?);
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Readback {
    pub decision: Decision,
}
impl Readback {
    pub fn parse(v: &Value, d: &Decision) -> io::Result<Self> {
        bounded(v, life::REPLY_BYTES)?;
        keys(
            v,
            &[
                "kind",
                "version",
                "schedule_version",
                "identity",
                "old_instance",
                "proposal_id",
                "accepted_operation_id",
                "result_sha256",
                "result",
                "job_readback",
                "deferred_readback",
                "verified",
            ],
        )?;
        version(v, "version")?;
        version(v, "schedule_version")?;
        identity_at(v, &d.after_identity())?;
        if d.status == Status::Uncertain
            || v["kind"] != "work_schedule_readback"
            || v["old_instance"] != d.proposal.instance
            || v["proposal_id"] != d.proposal.id
            || v["accepted_operation_id"] != d.operation
            || v["result_sha256"] != d.result_sha256()?
            || v["result"] != d.wire
            || v["verified"] != true
        {
            return Err(bad("Scheduled readback changed its exact receipt"));
        }
        if d.status == Status::Staged {
            let s = d.stamp.as_ref().unwrap();
            let j = &v["job_readback"];
            keys(j, &["job_snapshot", "job_sha256"])?;
            if j["job_snapshot"] != s.job_snapshot || j["job_sha256"] != s.job_sha256 {
                return Err(bad("Independent scheduled stamp changed"));
            }
            let r = &v["deferred_readback"];
            keys(r, &["document_sha256", "record", "state", "input_id"])?;
            if r["document_sha256"] != d.wire["deferred_journal_sha256"]
                || r["record"] != d.wire["deferred"]
                || r["state"] != "pending"
                || r["input_id"] != Value::Null
            {
                return Err(bad("Independent staged invocation changed"));
            }
        } else if !v["job_readback"].is_null() || !v["deferred_readback"].is_null() {
            return Err(bad("Refused scheduled decision claims durable effects"));
        }
        Ok(Self {
            decision: d.clone(),
        })
    }
}
