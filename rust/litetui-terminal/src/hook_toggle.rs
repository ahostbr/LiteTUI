//! Wire contract for a reviewed one-boolean change to a source-owned lifecycle hook.
use crate::{work_lifecycle::keys, work_protocol::Identity};
use serde_json::{Value, json};
use std::io;

fn invalid() -> io::Error {
    io::Error::other("Hook evidence differs from its reviewed scope, document, or execution effect")
}
fn hex(value: &Value, len: usize) -> bool {
    value.as_str().is_some_and(|s| {
        s.len() == len
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn name(value: &Value) -> bool {
    value.as_str().is_some_and(|s| {
        (1..=96).contains(&s.len())
            && s.bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
    })
}
fn label(v: &Value) -> &str {
    v.as_str().expect("validated string")
}
#[derive(Clone, Copy, Debug, Eq, PartialEq, Default)]
pub enum Scope {
    #[default]
    Project,
    Global,
}
impl Scope {
    pub fn key(self) -> &'static str {
        match self {
            Self::Project => "project",
            Self::Global => "global",
        }
    }
    pub fn other(self) -> Self {
        match self {
            Self::Project => Self::Global,
            Self::Global => Self::Project,
        }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Row(Value);
impl Row {
    pub fn id(&self) -> &str {
        label(&self.0["id"])
    }
    pub fn enabled(&self) -> bool {
        self.0["enabled"].as_bool().expect("validated flag")
    }
    pub fn effect(&self) -> &str {
        label(&self.0["effect"])
    }
    pub fn resolved(&self) -> String {
        if let Some(scope) = self.0["effective_scope"].as_str() {
            format!(
                "{} · {} · command {}",
                scope,
                if self.0["effective_enabled"] == true {
                    "enabled"
                } else {
                    "disabled"
                },
                label(&self.0["effective_command"])
            )
        } else {
            "hooks switched off".into()
        }
    }
    pub fn summary(&self) -> String {
        format!(
            "{} · {} · {}",
            self.id(),
            if self.enabled() {
                "enabled"
            } else {
                "disabled"
            },
            self.effect()
        )
    }
    pub fn test_event(&self, index: usize) -> &str {
        let events = self.0["events"].as_array().expect("validated events");
        label(&events[index % events.len()])
    }
    pub fn details(&self) -> Vec<String> {
        let r = &self.0;
        let names = |key: &str| {
            r[key]
                .as_array()
                .unwrap()
                .iter()
                .map(label)
                .collect::<Vec<_>>()
                .join(", ")
        };
        vec![
            format!("Executable: {}", label(&r["executable"])),
            format!("Command fingerprint: {}", label(&r["command_id"])),
            format!("Mode: {} · timeout {} s", label(&r["mode"]), r["timeout"]),
            format!("Events: {}", names("events")),
            format!(
                "Sources: {}",
                if names("sources").is_empty() {
                    "all".into()
                } else {
                    names("sources")
                }
            ),
            format!(
                "{} argument(s), {} private environment value(s), {} tool filter(s)",
                r["argument_count"], r["env_count"], r["filter_count"]
            ),
            format!(
                "Working directory: {}",
                if r["custom_cwd"] == true {
                    "configured override"
                } else {
                    "hook workspace"
                }
            ),
        ]
    }
    fn parse(v: &Value, scope: Scope) -> io::Result<Self> {
        keys(
            v,
            &[
                "id",
                "scope",
                "enabled",
                "effect",
                "effective_scope",
                "effective_enabled",
                "effective_command",
                "mode",
                "executable",
                "command_id",
                "events",
                "sources",
                "argument_count",
                "env_count",
                "custom_cwd",
                "filter_count",
                "timeout",
            ],
        )?;
        let bounded = |k: &str| v[k].as_u64().is_some_and(|n| n <= 262144);
        let list = |key: &str, allowed: &[&str]| {
            v[key].as_array().is_some_and(|values| {
                let mut seen = std::collections::HashSet::new();
                values.len() <= allowed.len()
                    && values.iter().all(|value| {
                        value
                            .as_str()
                            .is_some_and(|item| allowed.contains(&item) && seen.insert(item))
                    })
            })
        };
        if !name(&v["id"])
            || v["scope"] != scope.key()
            || !v["enabled"].is_boolean()
            || !matches!(
                v["effect"].as_str(),
                Some("enabled" | "disabled" | "shadowed" | "off")
            )
            || !matches!(v["mode"].as_str(), Some("observe" | "gate"))
            || !(name(&v["executable"]) || v["executable"] == "(name withheld)")
            || !hex(&v["command_id"], 24)
            || !list(
                "events",
                &[
                    "app_start",
                    "app_shutdown",
                    "conversation_start",
                    "conversation_resume",
                    "conversation_leave",
                    "prompt_before",
                    "tool_before",
                    "tool_after",
                    "completion_before",
                    "completion_after",
                ],
            )
            || v["events"].as_array().unwrap().is_empty()
            || !list(
                "sources",
                &[
                    "typed",
                    "queued",
                    "interrupted",
                    "rpc",
                    "scheduled",
                    "harness",
                ],
            )
            || !v["custom_cwd"].is_boolean()
            || !["argument_count", "env_count", "filter_count"]
                .iter()
                .all(|k| bounded(k))
            || !v["timeout"]
                .as_f64()
                .is_some_and(|t| (1.0..=300.0).contains(&t))
        {
            return Err(invalid());
        }
        let active_identity = match v["effect"].as_str() {
            Some("off") => {
                v["effective_scope"].is_null()
                    && v["effective_enabled"].is_null()
                    && v["effective_command"].is_null()
            }
            Some("shadowed") => {
                scope == Scope::Global
                    && v["effective_scope"] == "project"
                    && v["effective_enabled"].is_boolean()
                    && hex(&v["effective_command"], 24)
            }
            _ => {
                v["effective_scope"] == scope.key()
                    && v["effective_enabled"] == v["enabled"]
                    && v["effective_command"] == v["command_id"]
            }
        };
        if !active_identity {
            return Err(invalid());
        }
        let row = Self(v.clone());
        if matches!(row.effect(), "enabled" | "disabled")
            && (row.effect() == "enabled") != row.enabled()
            || scope == Scope::Project && row.effect() == "shadowed"
        {
            return Err(invalid());
        }
        Ok(row)
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct List {
    pub scope: Scope,
    pub revision: String,
    pub rows: Vec<Row>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub scope: Scope,
    pub before: Row,
    pub after: Row,
    pub before_hash: String,
    pub after_hash: String,
    after_revision: String,
    token: String,
}
#[derive(Clone, Debug, PartialEq)]
pub struct TestReview {
    pub scope: Scope,
    pub row: Row,
    pub event: String,
    pub profile: String,
    pub sample: Value,
    token: String,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    List(Scope),
    Review {
        scope: Scope,
        revision: String,
        row: Row,
    },
    Apply(Box<Review>),
    TestReview {
        scope: Scope,
        revision: String,
        row: Row,
        event: String,
    },
    Run(Box<TestReview>),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Reply {
    List(List),
    Review(Review),
    Saved(Row),
    TestReview(TestReview),
    Tested(String),
}
impl Request {
    pub fn operation(&self) -> &'static str {
        match self {
            Self::List(_) => "gui.hooks.native_list",
            Self::Review { .. } => "gui.hooks.native_review",
            Self::Apply(_) => "gui.hooks.native_apply",
            Self::TestReview { .. } => "gui.hooks.native_test_review",
            Self::Run(_) => "gui.hooks.native_test_run",
        }
    }
    fn scope(&self) -> Scope {
        match self {
            Self::List(s) | Self::Review { scope: s, .. } | Self::TestReview { scope: s, .. } => *s,
            Self::Run(r) => r.scope,
            Self::Apply(r) => r.scope,
        }
    }
    pub fn command(&self, id: &Identity, instance: &str) -> Value {
        let mut cmd = id.request();
        cmd["type"] = json!(self.operation());
        cmd["work_instance"] = json!(instance);
        cmd["scope"] = json!(self.scope().key());
        match self {
            Self::Review { revision, row, .. } => {
                cmd["revision"] = json!(revision);
                cmd["hook_id"] = json!(row.id());
            }
            Self::TestReview {
                revision,
                row,
                event,
                ..
            } => {
                cmd["revision"] = json!(revision);
                cmd["hook_id"] = json!(row.id());
                cmd["event"] = json!(event);
            }
            Self::Run(r) => cmd["token"] = json!(r.token),
            Self::Apply(r) => cmd["token"] = json!(r.token),
            Self::List(_) => {}
        }
        cmd
    }
    fn check(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        kind: &str,
        fields: &[&str],
    ) -> io::Result<()> {
        let mut all = vec![
            "kind",
            "version",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "scope",
        ];
        all.extend_from_slice(fields);
        keys(v, &all)?;
        if v["kind"] != kind
            || v["version"].as_u64() != Some(1)
            || v["session_id"] != id.session
            || v["agent_id"] != id.agent
            || v["backend"] != id.backend
            || v["model"] != id.model
            || v["work_instance"] != instance
            || v["scope"] != self.scope().key()
        {
            return Err(invalid());
        }
        Ok(())
    }
    pub fn readback(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        op: &str,
    ) -> io::Result<Value> {
        let Self::Apply(r) = self else {
            return Err(invalid());
        };
        self.check(v, id, instance, "hook_apply", &["token", "apply_id"])?;
        if v["token"] != r.token || v["apply_id"] != op {
            return Err(invalid());
        }
        let mut cmd = self.command(id, instance);
        cmd["type"] = json!("gui.hooks.native_readback");
        cmd["apply_id"] = json!(op);
        Ok(cmd)
    }
    pub fn reply(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        apply_id: Option<&str>,
    ) -> io::Result<Reply> {
        match self {
            Self::List(scope) => {
                self.check(v, id, instance, "hook_list", &["revision", "rows"])?;
                if !hex(&v["revision"], 64) {
                    return Err(invalid());
                }
                let rows = v["rows"]
                    .as_array()
                    .filter(|a| a.len() <= 64)
                    .ok_or_else(invalid)?
                    .iter()
                    .map(|r| Row::parse(r, *scope))
                    .collect::<io::Result<Vec<_>>>()?;
                let mut seen = std::collections::HashSet::new();
                if !rows.iter().all(|r| seen.insert(r.id())) {
                    return Err(invalid());
                }
                Ok(Reply::List(List {
                    scope: *scope,
                    revision: label(&v["revision"]).into(),
                    rows,
                }))
            }
            Self::TestReview {
                scope,
                revision,
                row,
                event,
            } => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_test_review",
                    &[
                        "revision", "row", "event", "source", "sample", "profile", "token",
                    ],
                )?;
                let expected = match event.as_str() {
                    "tool_before" | "tool_after" => json!({"tool":"read","args":{}}),
                    "prompt_before" => json!({"text":"Hook test"}),
                    _ => json!({}),
                };
                if v["revision"] != *revision
                    || Row::parse(&v["row"], *scope)? != *row
                    || !row.0["events"]
                        .as_array()
                        .is_some_and(|events| events.iter().any(|e| e == event))
                    || v["event"] != *event
                    || v["source"] != "typed"
                    || v["sample"] != expected
                    || !matches!(
                        v["profile"].as_str(),
                        Some("strict" | "interactive" | "autonomous")
                    )
                    || !hex(&v["token"], 64)
                {
                    return Err(invalid());
                }
                Ok(Reply::TestReview(TestReview {
                    scope: *scope,
                    row: row.clone(),
                    event: event.clone(),
                    sample: expected,
                    profile: label(&v["profile"]).into(),
                    token: label(&v["token"]).into(),
                }))
            }
            Self::Run(r) => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_test_result",
                    &[
                        "token",
                        "run_id",
                        "event",
                        "status",
                        "exit_code",
                        "cleanup_verified",
                        "truncated",
                        "stdout_bytes",
                        "stderr_bytes",
                    ],
                )?;
                let status = label(&v["status"]);
                let code = if v["exit_code"].is_null() {
                    None
                } else {
                    Some(v["exit_code"].as_i64().ok_or_else(invalid)?)
                };
                let counts =
                    |key: &str| v[key].as_u64().filter(|n| *n <= 65_536).ok_or_else(invalid);
                let out = counts("stdout_bytes")?;
                let err = counts("stderr_bytes")?;
                let clipped = v["truncated"].as_bool().ok_or_else(invalid)?;
                let verified = v["cleanup_verified"].as_bool().ok_or_else(invalid)?;
                if apply_id.is_none()
                    || v["run_id"].as_str() != apply_id
                    || v["token"] != r.token
                    || v["event"] != r.event
                {
                    return Err(invalid());
                }
                let message = match status {
                    "allowed" if code == Some(0) => "allowed",
                    "denied" if code.is_some() => "denied or failed",
                    "timeout" => "timed out",
                    "cancelled" => "cancelled",
                    "unavailable" if code.is_none() => "authorization or launch unavailable",
                    _ => return Err(invalid()),
                };
                Ok(Reply::Tested(format!(
                    "Test {message}{} · exit {} · stdout {out} B · stderr {err} B{}",
                    if verified {
                        ""
                    } else {
                        " · cleanup unverified"
                    },
                    code.map_or_else(|| "unknown".into(), |n| n.to_string()),
                    if clipped { " · output clipped" } else { "" }
                )))
            }
            Self::Review {
                scope,
                row,
                revision,
            } => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_review",
                    &[
                        "token",
                        "revision",
                        "before",
                        "after",
                        "before_sha256",
                        "after_sha256",
                        "after_revision",
                    ],
                )?;
                if v["revision"] != *revision
                    || !["token", "before_sha256", "after_sha256", "after_revision"]
                        .iter()
                        .all(|k| hex(&v[*k], 64))
                    || v["before_sha256"] == v["after_sha256"]
                    || v["revision"] == v["after_revision"]
                {
                    return Err(invalid());
                }
                let before = Row::parse(&v["before"], *scope)?;
                let after = Row::parse(&v["after"], *scope)?;
                let mut expected = row.0.clone();
                expected["enabled"] = json!(!row.enabled());
                if matches!(row.effect(), "enabled" | "disabled") {
                    expected["effective_enabled"] = json!(!row.enabled());
                }
                expected["effect"] = json!(match row.effect() {
                    "enabled" => "disabled",
                    "disabled" => "enabled",
                    status => status,
                });
                if before != *row || after.0 != expected {
                    return Err(invalid());
                }
                Ok(Reply::Review(Review {
                    scope: *scope,
                    before,
                    after,
                    before_hash: label(&v["before_sha256"]).into(),
                    after_hash: label(&v["after_sha256"]).into(),
                    after_revision: label(&v["after_revision"]).into(),
                    token: label(&v["token"]).into(),
                }))
            }
            Self::Apply(r) => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_saved",
                    &["token", "apply_id", "revision", "sha256", "actual"],
                )?;
                let actual = Row::parse(&v["actual"], r.scope)?;
                if apply_id.is_none()
                    || v["apply_id"].as_str() != apply_id
                    || v["token"] != r.token
                    || v["revision"] != r.after_revision
                    || v["sha256"] != r.after_hash
                    || actual != r.after
                {
                    return Err(invalid());
                }
                Ok(Reply::Saved(actual))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn verify_sample_and_receipt(id: &Identity, base: &Value, list: &List) {
        let req = Request::TestReview {
            scope: list.scope,
            revision: list.revision.clone(),
            row: list.rows[0].clone(),
            event: "tool_before".into(),
        };
        let mut wire = base.clone();
        for (key, value) in [
            ("kind", json!("hook_test_review")),
            ("row", list.rows[0].0.clone()),
            ("event", json!("tool_before")),
            ("source", json!("typed")),
            ("token", json!("b".repeat(64))),
            ("revision", json!(list.revision)),
            ("profile", json!("strict")),
            ("sample", json!({"args":{},"tool":"read"})),
        ] {
            wire[key] = value;
        }
        let Reply::TestReview(review) = req.reply(&wire, id, "work", None).unwrap() else {
            panic!()
        };
        for (key, value) in [
            ("sample", json!({"secret":"unreviewed"})),
            ("event", json!("prompt_before")),
            ("profile", json!("forged")),
        ] {
            let mut forged = wire.clone();
            forged[key] = value;
            assert!(req.reply(&forged, id, "work", None).is_err());
        }
        let run = Request::Run(Box::new(review));
        let mut result = base.clone();
        for (key, value) in [
            ("kind", json!("hook_test_result")),
            ("event", json!("tool_before")),
            ("token", json!("b".repeat(64))),
            ("run_id", json!("test-id")),
            ("status", json!("allowed")),
            ("exit_code", json!(0)),
            ("cleanup_verified", json!(true)),
            ("truncated", json!(false)),
            ("stdout_bytes", json!(4)),
            ("stderr_bytes", json!(0)),
        ] {
            result[key] = value;
        }
        assert!(matches!(
            run.reply(&result, id, "work", Some("test-id")),
            Ok(Reply::Tested(_))
        ));
        for (key, value) in [
            ("run_id", json!("other")),
            ("stdout_bytes", json!(65537)),
            ("stderr", json!("private")),
            ("exit_code", json!(1)),
        ] {
            let mut forged = result.clone();
            forged[key] = value;
            assert!(run.reply(&forged, id, "work", Some("test-id")).is_err());
        }
    }
    #[test]
    fn rejects_extra_command_data_and_forged_shadowed_effect_or_readback() {
        let id = Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let base = json!({"kind":"hook_list","version":1,"session_id":"session","agent_id":"agent","backend":"custom","model":"m","work_instance":"work","scope":"global"});
        let row = json!({"id":"audit","scope":"global","enabled":true,"mode":"observe","effect":"shadowed",
            "effective_scope":"project","effective_enabled":false,"effective_command":"a".repeat(24),
            "executable":"runner.exe","command_id":"b".repeat(24),"events":["tool_before"],"sources":[],
            "argument_count":1,"env_count":0,"custom_cwd":false,"filter_count":0,"timeout":10});
        let mut catalog = base.clone();
        catalog["rows"] = json!([row.clone()]);
        catalog["revision"] = json!("1".repeat(64));
        let list = Request::List(Scope::Global);
        let Reply::List(result) = list.reply(&catalog, &id, "work", None).unwrap() else {
            panic!()
        };
        verify_sample_and_receipt(&id, &base, &result);
        catalog["rows"][0]["argv"] = json!(["private"]);
        assert!(list.reply(&catalog, &id, "work", None).is_err());
        let req = Request::Review {
            scope: Scope::Global,
            revision: result.revision.clone(),
            row: result.rows[0].clone(),
        };
        let mut review = base;
        review["kind"] = json!("hook_review");
        review["revision"] = json!(result.revision);
        review["before"] = row.clone();
        review["after"] = row;
        review["after"]["enabled"] = json!(false);
        for (key, label) in [
            ("token", "2"),
            ("before_sha256", "3"),
            ("after_sha256", "4"),
            ("after_revision", "5"),
        ] {
            review[key] = json!(label.repeat(64));
        }
        let Reply::Review(verified) = req.reply(&review, &id, "work", None).unwrap() else {
            panic!()
        };
        review["after"]["effective_enabled"] = json!(true);
        assert!(req.reply(&review, &id, "work", None).is_err());
        let apply = Request::Apply(Box::new(verified));
        let mut receipt = json!({"kind":"hook_apply","version":1,"session_id":"session","agent_id":"agent","backend":"custom",
            "model":"m","work_instance":"work","scope":"global","token":"2".repeat(64),"apply_id":"actual"});
        assert!(apply.readback(&receipt, &id, "work", "wrong").is_err());
        assert_eq!(
            apply.readback(&receipt, &id, "work", "actual").unwrap()["type"],
            "gui.hooks.native_readback"
        );
        receipt["kind"] = json!("hook_saved");
        receipt["sha256"] = json!("4".repeat(64));
        receipt["revision"] = json!("5".repeat(64));
        receipt["actual"] = review["after"].clone();
        assert!(apply.reply(&receipt, &id, "work", Some("actual")).is_err());
    }
}
