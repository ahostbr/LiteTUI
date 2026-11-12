//! Where to connect (*_host) belongs to the conversation; what starts the engine is device-wide
//! (develop 5f5ef63f, Ryan 2026-10-24). Saving cannot authorize Start or Stop.
use crate::{
    engine_controls::{Admission, Identity},
    work_lifecycle as life,
};
use serde_json::{Value, json};
use std::io;

pub const SNAPSHOT: &str = "gui.engine.native_config_snapshot";
pub const REVIEW: &str = "gui.engine.native_config_review";
pub const SAVE: &str = "gui.engine.native_config_save";
pub const READBACK: &str = "gui.engine.native_config_readback";
pub const OPERATIONS: [&str; 4] = [SNAPSHOT, REVIEW, SAVE, READBACK];
const NINFER: [&str; 8] = [
    "ninfer_host",
    "ninfer_executable",
    "ninfer_artifact",
    "ninfer_max_context",
    "ninfer_max_concurrency",
    "ninfer_kv_dtype",
    "ninfer_kv_capacity",
    "ninfer_host_kv_mib",
];
const STRATA: [&str; 4] = [
    "strata_host",
    "strata_root",
    "strata_config",
    "strata_max_context",
];
const COMMON: [&str; 7] = [
    "kind",
    "version",
    "session_id",
    "agent_id",
    "backend",
    "desired_model",
    "management_instance",
];

fn header(v: &Value, id: &Identity, kind: &str, fields: &[&str]) -> io::Result<()> {
    life::bounded(v, 131072)?;
    life::keys(v, &[COMMON.as_slice(), fields].concat())?;
    id.verify(v)?;
    if v["kind"] != kind || v["version"].as_u64() != Some(1) {
        return Err(life::bad("Unexpected engine preference reply"));
    }
    Ok(())
}
fn revisions(v: &Value) -> io::Result<()> {
    life::keys(v, &["global", "agent", "conversation"])?;
    for key in ["global", "agent", "conversation"] {
        if v[key] != "absent" {
            life::hash(v, key)?;
        }
    }
    Ok(())
}
/// The canonical scope of one engine preference, and the revision key of its document.
fn scope(name: &str) -> (&'static str, &'static str) {
    if name.ends_with("_host") {
        ("conversation", "conversation")
    } else {
        ("device", "global")
    }
}
fn numeric(name: &str) -> bool {
    matches!(
        name,
        "ninfer_max_context"
            | "ninfer_max_concurrency"
            | "strata_max_context"
            | "ninfer_host_kv_mib"
    )
}
fn nullable(name: &str) -> bool {
    matches!(name, "strata_max_context" | "ninfer_host_kv_mib")
}
fn typed(name: &str, v: &Value) -> bool {
    if numeric(name) {
        v.as_i64().is_some() || nullable(name) && v.is_null()
    } else {
        v.as_str().is_some_and(|s| s.len() <= 4096)
    }
}
pub fn input(name: &str, s: &str) -> io::Result<Value> {
    if numeric(name) {
        if s.is_empty() && nullable(name) {
            return Ok(Value::Null);
        }
        let n = s
            .parse::<i32>()
            .ok()
            .filter(|v| *v > 0 && (name != "ninfer_max_concurrency" || *v <= 8))
            .ok_or_else(|| {
                life::bad(
                    "Enter a supported positive integer; NInfer concurrency ranges from 1 to 8",
                )
            })?;
        return Ok(json!(n));
    }
    if s.len() > 4096 || s.chars().any(char::is_control) {
        return Err(life::bad("Use a single line of at most 4096 UTF-8 bytes"));
    }
    Ok(json!(s))
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub id: Identity,
    pub token: String,
    pub names: Vec<String>,
    pub wire: Value,
}
impl Snapshot {
    pub fn parse(
        v: &Value,
        id: &Identity,
        generation: u64,
        admission: Admission,
    ) -> io::Result<Self> {
        header(
            v,
            id,
            "engine_config_snapshot",
            &[
                "scope",
                "generation",
                "admission",
                "fields",
                "revisions",
                "destination",
                "snapshot",
            ],
        )?;
        if v["scope"] != "mixed"
            || v["generation"].as_u64() != Some(generation)
            || Admission::parse(&v["admission"])? != admission
        {
            return Err(life::bad(
                "Engine configuration changed scope, generation or admission",
            ));
        }
        revisions(&v["revisions"])?;
        life::keys(&v["destination"], &["conversation", "device"])?;
        life::text(&v["destination"], "conversation", 4096)?;
        life::text(&v["destination"], "device", 4096)?;
        let names: &[&str] = if id.backend == "ninfer" {
            &NINFER
        } else {
            &STRATA
        };
        life::keys(&v["fields"], names)?;
        for key in names {
            let row = &v["fields"][key];
            life::keys(row, &["saved", "effective", "timing", "scope", "locked"])?;
            let timing = if key.ends_with("_host") {
                "reconnect"
            } else {
                "restart"
            };
            if !typed(key, &row["saved"])
                || !typed(key, &row["effective"])
                || row["timing"] != timing
                || row["scope"] != scope(key).0
                || !(row["locked"].is_null()
                    || row["locked"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 512))
            {
                return Err(life::bad("Invalid engine preference metadata"));
            }
        }
        Ok(Self {
            id: id.clone(),
            token: life::hash(v, "snapshot")?,
            names: names.iter().map(|k| (*k).into()).collect(),
            wire: v.clone(),
        })
    }
    pub fn review_command(&self, field: &str, value: &Value) -> io::Result<Value> {
        let row = &self.wire["fields"][field];
        if !self.names.iter().any(|n| n == field)
            || !row["locked"].is_null()
            || !typed(field, value)
            || row["saved"] == *value
        {
            return Err(life::bad(
                "Choose an unlocked engine preference and a changed value",
            ));
        }
        let mut v = self.id.command(REVIEW);
        v["snapshot"] = json!(self.token);
        v["field"] = json!(field);
        v["value"] = value.clone();
        Ok(v)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub snap: Snapshot,
    pub field: String,
    pub value: Value,
    pub token: String,
}
impl Review {
    pub fn parse(v: &Value, snap: &Snapshot, field: &str, value: &Value) -> io::Result<Self> {
        header(
            v,
            &snap.id,
            "engine_config_review",
            &[
                "scope",
                "generation",
                "admission",
                "revisions",
                "destination",
                "snapshot",
                "field",
                "value",
                "saved_before",
                "effective_before",
                "timing",
                "review",
            ],
        )?;
        snap.review_command(field, value)?;
        let row = &snap.wire["fields"][field];
        let (field_scope, _) = scope(field);
        if ["generation", "admission", "revisions", "snapshot"]
            .iter()
            .any(|k| v[*k] != snap.wire[*k])
            || v["scope"] != field_scope
            || v["destination"] != snap.wire["destination"][field_scope]
            || v["field"] != field
            || v["value"] != *value
            || v["saved_before"] != row["saved"]
            || v["effective_before"] != row["effective"]
            || v["timing"] != row["timing"]
        {
            return Err(life::bad(
                "Configuration review differs from the selected preference or draft",
            ));
        }
        Ok(Self {
            snap: snap.clone(),
            field: field.into(),
            value: value.clone(),
            token: life::hash(v, "review")?,
        })
    }
    pub fn save_command(&self) -> Value {
        let mut v = self.snap.id.command(SAVE);
        v["review"] = json!(self.token);
        v
    }
    pub fn refusal(&self, v: &Value, request_id: &str) -> io::Result<String> {
        header(
            v,
            &self.snap.id,
            "engine_config_refused",
            &["review", "save_id", "field", "mutated", "reason"],
        )?;
        if v["review"] != self.token
            || v["save_id"] != request_id
            || v["field"] != self.field
            || v["mutated"] != false
        {
            return Err(life::bad(
                "Engine preference refusal lacks the exact unchanged selection",
            ));
        }
        life::text(v, "reason", 4096)
    }
    pub fn receipt(&self, v: &Value, id: &str) -> io::Result<Value> {
        header(
            v,
            &self.snap.id,
            "engine_config_save",
            &[
                "review",
                "save_id",
                "field",
                "saved",
                "effective",
                "timing",
                "revisions",
                "generation",
                "admission",
            ],
        )?;
        revisions(&v["revisions"])?;
        let before = &self.snap.wire;
        let row = &before["fields"][&self.field];
        if v["review"] != self.token
            || v["save_id"] != id
            || v["field"] != self.field
            || v["saved"] != self.value
            || v["effective"] != row["effective"]
            || v["timing"] != row["timing"]
            || v["generation"] != before["generation"]
            || v["admission"] != before["admission"]
            || v["revisions"][scope(&self.field).1] == "absent"
            || v["revisions"][scope(&self.field).1] == before["revisions"][scope(&self.field).1]
            || ["global", "agent", "conversation"]
                .iter()
                .filter(|k| **k != scope(&self.field).1)
                .any(|k| v["revisions"][*k] != before["revisions"][*k])
        {
            return Err(life::bad(
                "Engine preference save differs from the review or changed live configuration",
            ));
        }
        let mut cmd = self.snap.id.command(READBACK);
        cmd["review"] = json!(self.token);
        cmd["save_id"] = json!(id);
        Ok(cmd)
    }
    pub fn verify(&self, v: &Value, receipt: &Value) -> io::Result<()> {
        let mut expected = receipt.clone();
        expected["kind"] = json!("engine_config_readback");
        if v != &expected {
            return Err(life::bad(
                "Independent engine preference readback disagrees with its save",
            ));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Read,
    Review {
        snapshot: String,
        field: String,
        value: Value,
    },
    Save(String),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Snapshot(Snapshot),
    Review(Review),
    Saved(String),
    Rejected(String),
}
