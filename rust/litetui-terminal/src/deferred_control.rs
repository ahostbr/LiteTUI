//! Reviewed discard of historical staged invocations; never replay or input settlement.
use crate::{
    scheduled_transition::{boolean, digest, identity_at, version},
    work_lifecycle::{
        self as life, DeferredJob, Identity, bad, bounded, hash, keys, number, optional,
        optional_uuid, text,
    },
};
use serde::de::{self, Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Value, json};
use std::{collections::BTreeSet, fmt, io};
pub const VALIDATE: &str = "gui.work.native_deferred_validate";
pub const DISCARD: &str = "gui.work.native_deferred_discard";
pub const READBACK: &str = "gui.work.native_deferred_readback";
pub const CONSEQUENCE: &str = "This will not run the job again. If delivery was uncertain, discarding this hold does not establish whether it ran or resolve an existing input.";

// A separate structural pass catches duplicate keys without normalizing opaque
// Python numeric spellings retained by serde_json's arbitrary_precision feature.
struct Check;
impl<'de> Deserialize<'de> for Check {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        d.deserialize_any(CheckVisitor)
    }
}
struct CheckVisitor;
impl<'de> Visitor<'de> for CheckVisitor {
    type Value = Check;
    fn expecting(&self, f: &mut fmt::Formatter) -> fmt::Result {
        f.write_str("duplicate-free JSON")
    }
    fn visit_bool<E: de::Error>(self, _: bool) -> Result<Check, E> {
        Ok(Check)
    }
    fn visit_i64<E: de::Error>(self, _: i64) -> Result<Check, E> {
        Ok(Check)
    }
    fn visit_u64<E: de::Error>(self, _: u64) -> Result<Check, E> {
        Ok(Check)
    }
    fn visit_f64<E: de::Error>(self, v: f64) -> Result<Check, E> {
        if v.is_finite() {
            Ok(Check)
        } else {
            Err(E::custom("nonfinite JSON"))
        }
    }
    fn visit_str<E: de::Error>(self, _: &str) -> Result<Check, E> {
        Ok(Check)
    }
    fn visit_unit<E: de::Error>(self) -> Result<Check, E> {
        Ok(Check)
    }
    fn visit_none<E: de::Error>(self) -> Result<Check, E> {
        Ok(Check)
    }
    fn visit_seq<A: SeqAccess<'de>>(self, mut a: A) -> Result<Check, A::Error> {
        while a.next_element::<Check>()?.is_some() {}
        Ok(Check)
    }
    fn visit_map<A: MapAccess<'de>>(self, mut a: A) -> Result<Check, A::Error> {
        let mut seen = BTreeSet::new();
        while let Some(k) = a.next_key::<String>()? {
            if !seen.insert(k) {
                return Err(de::Error::custom("duplicate JSON key"));
            }
            a.next_value::<Check>()?;
        }
        Ok(Check)
    }
}
pub(crate) fn strict_object(raw: &str) -> io::Result<Value> {
    let mut decoder = serde_json::Deserializer::from_str(raw);
    Check::deserialize(&mut decoder)?;
    decoder.end()?;
    let v: Value = serde_json::from_str(raw)?;
    if !v.is_object() {
        return Err(bad("Historical JSON must be an object"));
    }
    Ok(v)
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum JournalState {
    Pending,
    Delivered,
    Cancelled,
    Uncertain,
    Discarded,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Retirement {
    pub operation: String,
    pub review: String,
    pub previous_sha256: String,
    pub previous_state: JournalState,
    pub identity: Identity,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Journal {
    pub state: JournalState,
    pub input: Option<String>,
    pub record: DeferredJob,
    pub retirement: Option<Retirement>,
    pub sha256: String,
    pub wire: Value,
}
impl Journal {
    pub fn parse(raw: &str, identity: &Identity) -> io::Result<Self> {
        if raw.len() > life::REPLY_BYTES {
            return Err(bad("Deferred journal exceeds byte bound"));
        }
        let v = strict_object(raw)?;
        let n = number(&v, "version", true)?;
        keys(
            &v,
            if n == 2 {
                &["version", "state", "input_id", "record", "retirement"]
            } else {
                &["version", "state", "input_id", "record"]
            },
        )?;
        let record = DeferredJob::parse(&v["record"], identity)?;
        let input = optional_uuid(&v, "input_id")?;
        let state = match (n, text(&v, "state", 16)?.as_str()) {
            (1, "pending") => JournalState::Pending,
            (1, "delivered") => JournalState::Delivered,
            (1, "cancelled") => JournalState::Cancelled,
            (1, "uncertain") => JournalState::Uncertain,
            (2, "discarded") => JournalState::Discarded,
            _ => return Err(bad("Unknown deferred journal version/disposition")),
        };
        let retirement = if state == JournalState::Discarded {
            let r = &v["retirement"];
            keys(
                r,
                &[
                    "operation_id",
                    "review",
                    "previous_sha256",
                    "previous_state",
                    "identity",
                    "reason",
                    "no_replay",
                    "effects_undone",
                ],
            )?;
            let prior = match text(r, "previous_state", 16)?.as_str() {
                "pending" => JournalState::Pending,
                "uncertain" => JournalState::Uncertain,
                _ => return Err(bad("Discarded journal lacks historical retained state")),
            };
            let retired_identity = Identity::parse(&r["identity"])?;
            if retired_identity.session != identity.session
                || retired_identity.agent != identity.agent
                || r["reason"] != "user_discarded"
                || r["no_replay"] != true
                || r["effects_undone"] != false
            {
                return Err(bad(
                    "Deferred retirement changed ownership or claims rollback",
                ));
            }
            Some(Retirement {
                operation: text(r, "operation_id", 128)?,
                review: hash(r, "review")?,
                previous_sha256: hash(r, "previous_sha256")?,
                previous_state: prior,
                identity: retired_identity,
            })
        } else {
            None
        };
        use sha2::{Digest, Sha256};
        Ok(Self {
            state,
            input,
            record,
            retirement,
            sha256: format!("{:x}", Sha256::digest(raw.as_bytes())),
            wire: v,
        })
    }
    pub fn held(&self) -> bool {
        matches!(self.state, JournalState::Pending | JournalState::Uncertain)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub record: DeferredJob,
    pub journal_sha256: String,
    pub document_state: JournalState,
    pub input: Option<String>,
    pub eligible: bool,
    pub reason: Option<String>,
    pub wire: Value,
}
impl Snapshot {
    pub fn parse(v: &Value, identity: &Identity) -> io::Result<Self> {
        bounded(v, life::REPLY_BYTES)?;
        keys(
            v,
            &[
                "record",
                "journal_sha256",
                "document_state",
                "input_id",
                "discard",
            ],
        )?;
        keys(&v["discard"], &["eligible", "reason"])?;
        let eligible = boolean(&v["discard"], "eligible")?;
        let reason = optional(&v["discard"], "reason", 2048)?;
        if eligible == reason.is_some() {
            return Err(bad("Deferred discard capability contradicts reason"));
        }
        let document_state = match text(v, "document_state", 16)?.as_str() {
            "pending" => JournalState::Pending,
            "uncertain" => JournalState::Uncertain,
            _ => return Err(bad("Deferred snapshot must retain unresolved evidence")),
        };
        let record = DeferredJob::parse(&v["record"], identity)?;
        Ok(Self {
            record,
            journal_sha256: hash(v, "journal_sha256")?,
            document_state,
            input: optional_uuid(v, "input_id")?,
            eligible,
            reason,
            wire: v.clone(),
        })
    }
    pub fn validate_request(
        &self,
        identity: &Identity,
        instance: &str,
        snapshot: &str,
    ) -> io::Result<Value> {
        if !self.eligible {
            return Err(bad(self
                .reason
                .as_deref()
                .unwrap_or("Deferred discard unavailable")));
        }
        let mut v = identity.request(VALIDATE);
        v["deferred_version"] = json!(1);
        v["instance"] = json!(instance);
        v["snapshot"] = json!(snapshot);
        v["invocation_id"] = json!(self.record.invocation_id);
        v["journal_sha256"] = json!(self.journal_sha256);
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub identity: Identity,
    pub instance: String,
    pub snapshot: String,
    pub token: String,
    pub selected: Snapshot,
    pub wire: Value,
}
impl Review {
    pub fn parse(
        v: &Value,
        identity: &Identity,
        instance: &str,
        snapshot: &str,
        selected: &Snapshot,
    ) -> io::Result<Self> {
        bounded(v, life::REPLY_BYTES)?;
        keys(
            v,
            &[
                "kind",
                "version",
                "deferred_version",
                "identity",
                "instance",
                "snapshot",
                "review",
                "invocation_id",
                "journal_sha256",
                "record",
                "document_state",
                "input_id",
                "action",
                "no_replay",
            ],
        )?;
        version(v, "version")?;
        version(v, "deferred_version")?;
        identity_at(v, identity)?;
        if !selected.eligible
            || v["kind"] != "work_deferred_review"
            || v["instance"] != instance
            || v["snapshot"] != snapshot
            || v["invocation_id"] != selected.record.invocation_id
            || v["journal_sha256"] != selected.journal_sha256
            || v["record"] != selected.wire["record"]
            || v["document_state"] != selected.wire["document_state"]
            || v["input_id"] != selected.wire["input_id"]
            || v["action"] != "discard"
            || v["no_replay"] != true
        {
            return Err(bad("Deferred review changed selected historical evidence"));
        }
        Ok(Self {
            identity: identity.clone(),
            instance: instance.into(),
            snapshot: snapshot.into(),
            token: hash(v, "review")?,
            selected: selected.clone(),
            wire: v.clone(),
        })
    }
    pub fn request(&self) -> Value {
        let mut v = self.identity.request(DISCARD);
        v["deferred_version"] = json!(1);
        v["instance"] = json!(self.instance);
        v["snapshot"] = json!(self.snapshot);
        v["review"] = json!(self.token);
        v["invocation_id"] = json!(self.selected.record.invocation_id);
        v["journal_sha256"] = json!(self.selected.journal_sha256);
        v
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Discarded,
    Refused,
    Uncertain,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResultReceipt {
    pub review: Review,
    pub operation: String,
    pub status: Status,
    pub after_sha256: Option<String>,
    pub reason: Option<String>,
    pub wire: Value,
}
impl ResultReceipt {
    pub fn parse(v: &Value, r: &Review, operation: &str) -> io::Result<Self> {
        bounded(v, life::REPLY_BYTES)?;
        keys(
            v,
            &[
                "kind",
                "version",
                "deferred_version",
                "identity",
                "instance",
                "operation_id",
                "review",
                "invocation_id",
                "status",
                "before_sha256",
                "after_sha256",
                "close_required",
                "reason",
            ],
        )?;
        version(v, "version")?;
        version(v, "deferred_version")?;
        identity_at(v, &r.identity)?;
        if v["kind"] != "work_deferred_result"
            || v["instance"] != r.instance
            || text(v, "operation_id", 128)? != operation
            || v["review"] != r.token
            || v["invocation_id"] != r.selected.record.invocation_id
            || v["before_sha256"] != r.selected.journal_sha256
        {
            return Err(bad("Deferred result changed reviewed operation"));
        }
        let status = match text(v, "status", 16)?.as_str() {
            "discarded" => Status::Discarded,
            "refused" => Status::Refused,
            "uncertain" => Status::Uncertain,
            _ => return Err(bad("Unknown deferred discard disposition")),
        };
        let after = if v["after_sha256"].is_null() {
            None
        } else {
            Some(hash(v, "after_sha256")?)
        };
        let reason = optional(v, "reason", 2048)?;
        let close = boolean(v, "close_required")?;
        match status {
            Status::Discarded if after.is_none() || reason.is_some() || close => {
                return Err(bad("Deferred discard lacks exact persistence"));
            }
            Status::Refused if after.is_some() || reason.is_none() || close => {
                return Err(bad("Deferred refusal claims effects"));
            }
            Status::Uncertain if reason.is_none() || !close => {
                return Err(bad(
                    "Uncertain deferred write must retain closing obligation",
                ));
            }
            _ => {}
        }
        if status == Status::Discarded {
            let document = json!({"version":2,"state":"discarded","input_id":r.selected.wire["input_id"],"record":r.selected.wire["record"],
                "retirement":{"operation_id":operation,"review":r.token,"previous_sha256":r.selected.journal_sha256,"previous_state":r.selected.wire["document_state"],
                    "identity":r.identity.value(),"reason":"user_discarded","no_replay":true,"effects_undone":false}});
            if after.as_deref() != Some(digest(&document)?.as_str()) {
                return Err(bad("Deferred discard changed its terminal audit bytes"));
            }
        }
        Ok(Self {
            review: r.clone(),
            operation: operation.into(),
            status,
            after_sha256: after,
            reason,
            wire: v.clone(),
        })
    }
    pub fn readback_request(&self) -> io::Result<Value> {
        if self.status != Status::Discarded {
            return Err(bad("Only verified discarded writes have success readback"));
        }
        let mut v = self.review.identity.request(READBACK);
        v["deferred_version"] = json!(1);
        v["instance"] = json!(self.review.instance);
        v["operation_id"] = json!(self.operation);
        v["review"] = json!(self.review.token);
        v["invocation_id"] = json!(self.review.selected.record.invocation_id);
        v["after_sha256"] = json!(self.after_sha256);
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Readback {
    pub result: ResultReceipt,
}
impl Readback {
    pub fn parse(v: &Value, r: &ResultReceipt) -> io::Result<Self> {
        bounded(v, life::REPLY_BYTES)?;
        keys(
            v,
            &[
                "kind",
                "version",
                "deferred_version",
                "identity",
                "instance",
                "operation_id",
                "review",
                "invocation_id",
                "after_sha256",
                "verified",
                "effects_undone",
                "no_replay",
            ],
        )?;
        version(v, "version")?;
        version(v, "deferred_version")?;
        identity_at(v, &r.review.identity)?;
        if r.status != Status::Discarded
            || v["kind"] != "work_deferred_readback"
            || v["instance"] != r.review.instance
            || v["operation_id"] != r.operation
            || v["review"] != r.review.token
            || v["invocation_id"] != r.review.selected.record.invocation_id
            || v["after_sha256"] != r.wire["after_sha256"]
            || v["verified"] != true
            || v["no_replay"] != true
            || v["effects_undone"] != false
        {
            return Err(bad(
                "Deferred readback changed audited discard or claims replay authority",
            ));
        }
        Ok(Self { result: r.clone() })
    }
}
