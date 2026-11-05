//! Wire contract for creating, editing or deleting one source-owned lifecycle hook.
//!
//! The source validates the form with its own hook validator and publishes with its own writer.
//! This module only bounds what is sent, forbids what the review did not show, and refuses any
//! reply that differs from the reviewed change. Environment values and working directories never
//! cross the wire; an edit keeps the stored ones.
use crate::{hook_toggle::Scope, work_lifecycle::keys, work_protocol::Identity};
use serde_json::{Value, json};
use std::{collections::HashSet, io};

pub const SNAPSHOT: &str = "gui.hooks.native_edit_snapshot";
pub const REVIEW: &str = "gui.hooks.native_edit_review";
pub const APPLY: &str = "gui.hooks.native_edit_apply";
pub const READBACK: &str = "gui.hooks.native_edit_readback";
pub const REPAIR_REVIEW: &str = "gui.hooks.native_repair_review";
pub const REPAIR_APPLY: &str = "gui.hooks.native_repair_apply";
pub const REPAIR_READBACK: &str = "gui.hooks.native_repair_readback";
pub const OPERATIONS: [&str; 7] = [
    SNAPSHOT,
    REVIEW,
    APPLY,
    READBACK,
    REPAIR_REVIEW,
    REPAIR_APPLY,
    REPAIR_READBACK,
];
/// An unreadable scope document is repaired in the native editor only up to this size.
pub const MAX_REPAIR: usize = 65_536;
const MAX_DOCUMENT: usize = 262_144;
pub const MAX_HOOKS: usize = 64;
pub const MAX_EXECUTABLE: usize = 1024;
pub const MAX_ARGV: usize = 64;
pub const MAX_ARGUMENT: usize = 1024;
pub const MAX_TOOLS: usize = 32;
pub const MAX_TOOL: usize = 128;
pub const MAX_ID: usize = 96;

fn invalid() -> io::Error {
    io::Error::other("Hook authoring evidence differs from the reviewed scope, document, or change")
}
fn hex(value: &Value, len: usize) -> bool {
    value.as_str().is_some_and(|s| {
        s.len() == len
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    })
}
fn name(s: &str, max: usize) -> bool {
    (1..=max).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'_' | b'.' | b'-'))
}
fn text(v: &Value, max: usize) -> io::Result<String> {
    v.as_str()
        .filter(|s| !s.is_empty() && s.len() <= max && !s.contains('\0'))
        .map(str::to_owned)
        .ok_or_else(invalid)
}
fn strings(v: &Value, count: usize, max: usize, allow_empty: bool) -> io::Result<Vec<String>> {
    v.as_array()
        .filter(|a| a.len() <= count)
        .ok_or_else(invalid)?
        .iter()
        .map(|s| {
            s.as_str()
                .filter(|s| s.len() <= max && !s.contains('\0') && (allow_empty || !s.is_empty()))
                .map(str::to_owned)
                .ok_or_else(invalid)
        })
        .collect()
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Action {
    Create,
    Update,
    Delete,
}
impl Action {
    pub fn key(self) -> &'static str {
        match self {
            Self::Create => "create",
            Self::Update => "update",
            Self::Delete => "delete",
        }
    }
}

/// The choices the source accepts, as reported by the source.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Catalog {
    pub events: Vec<String>,
    pub gate_events: Vec<String>,
    pub sources: Vec<String>,
}
impl Catalog {
    fn parse(v: &Value) -> io::Result<Self> {
        let list = |key: &str, max: usize| -> io::Result<Vec<String>> {
            let items = strings(&v[key], max, 48, false)?;
            let mut seen = HashSet::new();
            if !items
                .iter()
                .all(|s| name(s, 48) && !s.contains(['.', '-']) && seen.insert(s.clone()))
            {
                return Err(invalid());
            }
            Ok(items)
        };
        let catalog = Self {
            events: list("events", 32)?,
            gate_events: list("gate_events", 32)?,
            sources: list("sources", 32)?,
        };
        if catalog.events.is_empty()
            || !catalog
                .gate_events
                .iter()
                .all(|e| catalog.events.contains(e))
        {
            return Err(invalid());
        }
        Ok(catalog)
    }
}

/// One hook as the source describes it. `env_count` and `custom_cwd` are read-only evidence.
#[derive(Clone, Debug, PartialEq)]
pub struct Hook {
    pub id: String,
    pub events: Vec<String>,
    pub executable: String,
    pub argv: Vec<String>,
    pub enabled: bool,
    pub mode: String,
    pub timeout: f64,
    pub sources: Vec<String>,
    pub tools: Vec<String>,
    pub env_count: u64,
    pub custom_cwd: bool,
}
impl Hook {
    /// The bounded form the source validates. Private fields are never sent.
    fn form(&self) -> Value {
        let timeout = if self.timeout.fract() == 0.0 && self.timeout.abs() < 1e9 {
            json!(self.timeout as i64)
        } else {
            json!(self.timeout)
        };
        json!({"id":self.id,"events":self.events,"executable":self.executable,"argv":self.argv,
            "enabled":self.enabled,"mode":self.mode,"timeout":timeout,"sources":self.sources,"tools":self.tools})
    }
    fn parse(v: &Value, catalog: &Catalog) -> io::Result<Self> {
        keys(
            v,
            &[
                "id",
                "events",
                "executable",
                "argv",
                "enabled",
                "mode",
                "timeout",
                "sources",
                "tools",
                "env_count",
                "custom_cwd",
            ],
        )?;
        let choices = |key: &str, allowed: &[String]| -> io::Result<Vec<String>> {
            let items = strings(&v[key], allowed.len(), 48, false)?;
            let mut seen = HashSet::new();
            if !items
                .iter()
                .all(|s| allowed.contains(s) && seen.insert(s.clone()))
            {
                return Err(invalid());
            }
            Ok(items)
        };
        let id = text(&v["id"], MAX_ID).ok().filter(|s| name(s, MAX_ID));
        let hook = Self {
            id: id.ok_or_else(invalid)?,
            events: choices("events", &catalog.events)?,
            executable: text(&v["executable"], MAX_EXECUTABLE)?,
            argv: strings(&v["argv"], MAX_ARGV, MAX_ARGUMENT, true)?,
            enabled: v["enabled"].as_bool().ok_or_else(invalid)?,
            mode: match v["mode"].as_str() {
                Some(mode @ ("observe" | "gate")) => mode.to_owned(),
                _ => return Err(invalid()),
            },
            timeout: v["timeout"]
                .as_f64()
                .filter(|t| t.is_finite() && (1.0..=300.0).contains(t))
                .ok_or_else(invalid)?,
            sources: choices("sources", &catalog.sources)?,
            tools: strings(&v["tools"], MAX_TOOLS, MAX_TOOL, true)?,
            env_count: v["env_count"]
                .as_u64()
                .filter(|n| *n <= 4096)
                .ok_or_else(invalid)?,
            custom_cwd: v["custom_cwd"].as_bool().ok_or_else(invalid)?,
        };
        if hook.events.is_empty() {
            return Err(invalid());
        }
        Ok(hook)
    }
    /// Every editable field equals the other's; private evidence is compared separately.
    fn same_form(&self, other: &Self) -> bool {
        self.form() == other.form()
    }
    pub fn summary(&self) -> String {
        format!(
            "{} · {} · {} · {}",
            self.id,
            if self.enabled { "enabled" } else { "disabled" },
            self.mode,
            self.events.join(",")
        )
    }
    pub fn command_line(&self) -> String {
        std::iter::once(self.executable.as_str())
            .chain(self.argv.iter().map(String::as_str))
            .map(|part| {
                if part.is_empty() || part.contains(char::is_whitespace) {
                    format!("{part:?}")
                } else {
                    part.to_owned()
                }
            })
            .collect::<Vec<_>>()
            .join(" ")
    }
    /// Field-by-field lines for the review; both sides use the same labels so changes line up.
    pub fn lines(&self) -> Vec<(&'static str, String)> {
        let list = |items: &[String], empty: &str| {
            if items.is_empty() {
                empty.to_owned()
            } else {
                items.join(", ")
            }
        };
        vec![
            ("id", self.id.clone()),
            ("enabled", self.enabled.to_string()),
            ("mode", self.mode.clone()),
            ("events", list(&self.events, "(none)")),
            ("sources", list(&self.sources, "(all)")),
            ("tool filters", list(&self.tools, "(all)")),
            ("command", self.command_line()),
            ("timeout", format!("{} s", self.timeout)),
            (
                "private",
                format!(
                    "{} environment value(s) kept in the source{}",
                    self.env_count,
                    if self.custom_cwd {
                        "; custom working directory kept"
                    } else {
                        ""
                    }
                ),
            ),
        ]
    }
}

/// A scope document the source's parser refuses: its stored text and the source's diagnostic.
#[derive(Clone, Debug, PartialEq)]
pub struct Unreadable {
    pub text: String,
    pub error: String,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub scope: Scope,
    pub revision: String,
    pub file: String,
    pub hooks_off: bool,
    pub rows: Vec<Hook>,
    pub catalog: Catalog,
    pub unreadable: Option<Unreadable>,
}

/// What the host would run for the hook's ID.
#[derive(Clone, Debug, PartialEq)]
pub struct Effective {
    pub scope: Scope,
    pub enabled: bool,
    command: String,
}
impl Effective {
    fn parse(v: &Value) -> io::Result<Option<Self>> {
        if v.is_null() {
            return Ok(None);
        }
        keys(v, &["scope", "enabled", "command_id"])?;
        Ok(Some(Self {
            scope: match v["scope"].as_str() {
                Some("project") => Scope::Project,
                Some("global") => Scope::Global,
                _ => return Err(invalid()),
            },
            enabled: v["enabled"].as_bool().ok_or_else(invalid)?,
            command: hex(&v["command_id"], 24)
                .then(|| v["command_id"].as_str().unwrap().to_owned())
                .ok_or_else(invalid)?,
        }))
    }
    pub fn describe(effective: &Option<Self>, off: bool) -> String {
        match (effective, off) {
            (_, true) => "lifecycle hooks are switched off for this runtime".into(),
            (None, _) => "no hook with this ID runs".into(),
            (Some(e), _) => format!(
                "{} hook {} (command {})",
                e.scope.key(),
                if e.enabled { "runs" } else { "is disabled" },
                e.command
            ),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub scope: Scope,
    pub action: Action,
    pub hook_id: String,
    pub file: String,
    pub created_file: bool,
    pub hooks_off: bool,
    pub before: Option<Hook>,
    pub after: Option<Hook>,
    pub before_effective: Option<Effective>,
    pub after_effective: Option<Effective>,
    pub others: u64,
    pub before_hash: Option<String>,
    pub after_hash: String,
    after_revision: String,
    token: String,
    catalog: Catalog,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Saved {
    pub scope: Scope,
    pub action: Action,
    pub file: String,
    pub hook_id: String,
    pub saved: Option<Hook>,
    pub effective: Option<Effective>,
    pub hooks_off: bool,
}

/// The reviewed replacement of an unreadable scope document by the source's canonical document.
#[derive(Clone, Debug, PartialEq)]
pub struct RepairReview {
    pub scope: Scope,
    pub file: String,
    pub before_hash: String,
    pub before_bytes: u64,
    pub after_text: String,
    pub rows: Vec<Hook>,
    pub after_hash: String,
    pub hooks_off: bool,
    after_revision: String,
    token: String,
    catalog: Catalog,
}

#[derive(Clone, Debug, PartialEq)]
pub struct RepairSaved {
    pub scope: Scope,
    pub file: String,
    pub rows: Vec<Hook>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    Snapshot(Scope),
    Repair {
        scope: Scope,
        revision: String,
        catalog: Catalog,
        text: String,
    },
    RepairApply(Box<RepairReview>),
    Review {
        scope: Scope,
        revision: String,
        rows: usize,
        catalog: Catalog,
        action: Action,
        before: Option<Box<Hook>>,
        hook: Option<Box<Hook>>,
    },
    Apply(Box<Review>),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Reply {
    Snapshot(Box<Snapshot>),
    Review(Box<Review>),
    Saved(Box<Saved>),
    RepairReview(Box<RepairReview>),
    RepairSaved(Box<RepairSaved>),
}

fn sha256(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

impl Request {
    /// True for the requests that write: they are followed by an independent readback.
    pub fn applies(&self) -> bool {
        matches!(self, Self::Apply(_) | Self::RepairApply(_))
    }
    pub fn operation(&self) -> &'static str {
        match self {
            Self::Repair { .. } => REPAIR_REVIEW,
            Self::RepairApply(_) => REPAIR_APPLY,
            Self::Snapshot(_) => SNAPSHOT,
            Self::Review { .. } => REVIEW,
            Self::Apply(_) => APPLY,
        }
    }
    pub fn scope(&self) -> Scope {
        match self {
            Self::Snapshot(scope) | Self::Review { scope, .. } | Self::Repair { scope, .. } => {
                *scope
            }
            Self::Apply(review) => review.scope,
            Self::RepairApply(review) => review.scope,
        }
    }
    pub fn command(&self, id: &Identity, instance: &str) -> Value {
        let mut cmd = id.request();
        cmd["type"] = json!(self.operation());
        cmd["work_instance"] = json!(instance);
        cmd["scope"] = json!(self.scope().key());
        match self {
            Self::Snapshot(_) => {}
            Self::Repair { revision, text, .. } => {
                cmd["revision"] = json!(revision);
                cmd["text"] = json!(text);
            }
            Self::RepairApply(review) => cmd["token"] = json!(review.token),
            Self::Review {
                revision,
                action,
                before,
                hook,
                ..
            } => {
                cmd["revision"] = json!(revision);
                cmd["action"] = json!(action.key());
                cmd["hook_id"] = json!(before.as_ref().map(|h| h.id.as_str()));
                cmd["hook"] = hook.as_ref().map_or(Value::Null, |h| h.form());
            }
            Self::Apply(review) => cmd["token"] = json!(review.token),
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
    /// Validates the apply receipt and builds the independent readback command.
    pub fn readback(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        op: &str,
    ) -> io::Result<Value> {
        let (kind, token, readback) = match self {
            Self::Apply(review) => ("hook_edit_apply", &review.token, READBACK),
            Self::RepairApply(review) => ("hook_repair_apply", &review.token, REPAIR_READBACK),
            _ => return Err(invalid()),
        };
        self.check(v, id, instance, kind, &["token", "apply_id"])?;
        if v["token"] != *token || v["apply_id"] != op {
            return Err(invalid());
        }
        let mut cmd = self.command(id, instance);
        cmd["type"] = json!(readback);
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
            Self::Snapshot(scope) => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_edit_snapshot",
                    &[
                        "state",
                        "error",
                        "text",
                        "revision",
                        "hooks_off",
                        "file",
                        "rows",
                        "events",
                        "gate_events",
                        "sources",
                    ],
                )?;
                let catalog = Catalog::parse(v)?;
                let rows = v["rows"]
                    .as_array()
                    .filter(|a| a.len() <= MAX_HOOKS)
                    .ok_or_else(invalid)?
                    .iter()
                    .map(|row| Hook::parse(row, &catalog))
                    .collect::<io::Result<Vec<_>>>()?;
                let mut seen = HashSet::new();
                if !hex(&v["revision"], 64)
                    || !rows.iter().all(|h| seen.insert(h.id.as_str()))
                    || v["file"] != file_label(*scope)
                {
                    return Err(invalid());
                }
                let unreadable = match v["state"].as_str() {
                    Some("ok") if v["error"].is_null() && v["text"].is_null() => None,
                    Some("invalid") if rows.is_empty() => Some(Unreadable {
                        text: v["text"]
                            .as_str()
                            .filter(|s| s.len() <= MAX_REPAIR && !s.contains('\0'))
                            .ok_or_else(invalid)?
                            .to_owned(),
                        error: v["error"]
                            .as_str()
                            .filter(|s| s.len() <= 1200 && !s.is_empty())
                            .ok_or_else(invalid)?
                            .to_owned(),
                    }),
                    _ => return Err(invalid()),
                };
                Ok(Reply::Snapshot(Box::new(Snapshot {
                    unreadable,
                    scope: *scope,
                    revision: v["revision"].as_str().unwrap().to_owned(),
                    file: file_label(*scope).into(),
                    hooks_off: v["hooks_off"].as_bool().ok_or_else(invalid)?,
                    rows,
                    catalog,
                })))
            }
            Self::Repair {
                scope,
                revision,
                catalog,
                text,
            } => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_repair_review",
                    &[
                        "revision",
                        "token",
                        "file",
                        "before_sha256",
                        "before_bytes",
                        "after_text",
                        "rows",
                        "after_sha256",
                        "after_revision",
                        "hooks_off",
                    ],
                )?;
                let rows = v["rows"]
                    .as_array()
                    .filter(|a| a.len() <= MAX_HOOKS)
                    .ok_or_else(invalid)?
                    .iter()
                    .map(|row| Hook::parse(row, catalog))
                    .collect::<io::Result<Vec<_>>>()?;
                let after_text = v["after_text"]
                    .as_str()
                    .filter(|s| s.len() <= MAX_DOCUMENT)
                    .ok_or_else(invalid)?;
                // The document on screen is the document whose bytes the source will have written.
                let hash_matches = [after_text.to_owned(), after_text.replace('\n', "\r\n")]
                    .iter()
                    .any(|candidate| v["after_sha256"] == sha256(candidate.as_bytes()));
                let written: Value = serde_json::from_str(after_text).map_err(|_| invalid())?;
                let typed: Value = serde_json::from_str(text).map_err(|_| invalid())?;
                let documented = written["hooks"].as_array().ok_or_else(invalid)?;
                let intended = typed["hooks"].as_array().ok_or_else(invalid)?;
                let canonical = documented.len() == rows.len()
                    && intended.len() == rows.len()
                    && documented
                        .iter()
                        .zip(intended)
                        .zip(&rows)
                        .all(|((doc, typed), row)| {
                            let mut seen = doc.clone();
                            seen["env_count"] =
                                json!(doc["env"].as_object().map_or(0, |m| m.len()));
                            seen["custom_cwd"] = json!(!doc["cwd"].is_null());
                            ["env", "cwd"].iter().for_each(|k| {
                                seen.as_object_mut().unwrap().remove(*k);
                            });
                            typed["id"] == doc["id"]
                                && Hook::parse(&seen, catalog).is_ok_and(|h| h == *row)
                        });
                if v["revision"] != *revision
                    || v["before_sha256"] != *revision
                    || v["before_bytes"]
                        .as_u64()
                        .is_none_or(|n| n == 0 || n > MAX_REPAIR as u64)
                    || v["file"] != file_label(*scope)
                    || !hex(&v["token"], 64)
                    || !hex(&v["after_sha256"], 64)
                    || !hex(&v["after_revision"], 64)
                    || v["after_sha256"] == v["before_sha256"]
                    || !hash_matches
                    || !canonical
                {
                    return Err(invalid());
                }
                Ok(Reply::RepairReview(Box::new(RepairReview {
                    scope: *scope,
                    file: file_label(*scope).into(),
                    before_hash: revision.clone(),
                    before_bytes: v["before_bytes"].as_u64().unwrap(),
                    after_text: after_text.to_owned(),
                    rows,
                    after_hash: v["after_sha256"].as_str().unwrap().to_owned(),
                    hooks_off: v["hooks_off"].as_bool().ok_or_else(invalid)?,
                    after_revision: v["after_revision"].as_str().unwrap().to_owned(),
                    token: v["token"].as_str().unwrap().to_owned(),
                    catalog: catalog.clone(),
                })))
            }
            Self::RepairApply(review) => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_repair_saved",
                    &[
                        "token",
                        "apply_id",
                        "revision",
                        "sha256",
                        "hooks_off",
                        "rows",
                    ],
                )?;
                let rows = v["rows"]
                    .as_array()
                    .filter(|a| a.len() <= MAX_HOOKS)
                    .ok_or_else(invalid)?
                    .iter()
                    .map(|row| Hook::parse(row, &review.catalog))
                    .collect::<io::Result<Vec<_>>>()?;
                if apply_id.is_none()
                    || v["apply_id"].as_str() != apply_id
                    || v["token"] != review.token
                    || v["revision"] != review.after_revision
                    || v["sha256"] != review.after_hash.as_str()
                    || v["hooks_off"] != review.hooks_off
                    || rows != review.rows
                {
                    return Err(invalid());
                }
                Ok(Reply::RepairSaved(Box::new(RepairSaved {
                    scope: review.scope,
                    file: review.file.clone(),
                    rows,
                })))
            }
            Self::Review {
                scope,
                revision,
                rows,
                catalog,
                action,
                before,
                hook,
            } => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_edit_review",
                    &[
                        "revision",
                        "token",
                        "action",
                        "hook_id",
                        "file",
                        "created_file",
                        "hooks_off",
                        "before",
                        "after",
                        "before_effective",
                        "after_effective",
                        "others",
                        "before_sha256",
                        "after_sha256",
                        "after_revision",
                    ],
                )?;
                let optional = |key: &str| -> io::Result<Option<Hook>> {
                    if v[key].is_null() {
                        Ok(None)
                    } else {
                        Hook::parse(&v[key], catalog).map(Some)
                    }
                };
                let (shown_before, shown_after) = (optional("before")?, optional("after")?);
                let hooks_off = v["hooks_off"].as_bool().ok_or_else(invalid)?;
                let (before_effective, after_effective) = (
                    Effective::parse(&v["before_effective"])?,
                    Effective::parse(&v["after_effective"])?,
                );
                let created_file = v["created_file"].as_bool().ok_or_else(invalid)?;
                // The review must describe exactly the change that was asked for.
                let expected_id = match (action, hook, before) {
                    (Action::Create, Some(h), None) => &h.id,
                    (Action::Update, Some(h), Some(b)) if h.id == b.id => &h.id,
                    (Action::Delete, None, Some(b)) => &b.id,
                    _ => return Err(invalid()),
                };
                let after_ok = match (action, hook, &shown_after, before) {
                    (Action::Delete, _, None, _) => true,
                    (_, Some(want), Some(got), kept) => {
                        want.same_form(got)
                            && got.env_count == kept.as_ref().map_or(0, |b| b.env_count)
                            && got.custom_cwd == kept.as_ref().is_some_and(|b| b.custom_cwd)
                    }
                    _ => false,
                };
                let foreign = |e: &Option<Effective>| e.as_ref().is_none_or(|e| e.scope != *scope);
                let effect_ok = hooks_off
                    && before_effective.is_none()
                    && after_effective.is_none()
                    || !hooks_off && {
                        let present = match action {
                            Action::Create => {
                                foreign(&before_effective) && after_effective.is_some()
                            }
                            Action::Update => {
                                before_effective.is_some() && after_effective.is_some()
                            }
                            Action::Delete => {
                                before_effective.is_some() && foreign(&after_effective)
                            }
                        };
                        // The project scope always wins; the global scope can be overridden by it.
                        let wins = |e: &Effective| {
                            shown_after.as_ref().is_some_and(|a| a.enabled == e.enabled)
                                && (e.scope == *scope || *scope == Scope::Global)
                        };
                        present
                            && (*action == Action::Delete
                                || after_effective.as_ref().is_some_and(|e| {
                                    wins(e) || *scope == Scope::Global && e.scope == Scope::Project
                                }))
                    };
                let others_expected = rows - usize::from(before.is_some());
                if v["revision"] != *revision
                    || v["action"] != action.key()
                    || v["hook_id"] != expected_id.as_str()
                    || v["file"] != file_label(*scope)
                    || shown_before.as_ref() != before.as_deref()
                    || !after_ok
                    || !effect_ok
                    || v["others"].as_u64() != Some(others_expected as u64)
                    || !hex(&v["token"], 64)
                    || !hex(&v["after_sha256"], 64)
                    || !hex(&v["after_revision"], 64)
                    || v["revision"] == v["after_revision"]
                    || created_file != v["before_sha256"].is_null()
                    || !(created_file || hex(&v["before_sha256"], 64))
                    || v["before_sha256"] == v["after_sha256"]
                    || created_file && *action != Action::Create
                {
                    return Err(invalid());
                }
                Ok(Reply::Review(Box::new(Review {
                    scope: *scope,
                    action: *action,
                    hook_id: expected_id.clone(),
                    file: file_label(*scope).into(),
                    created_file,
                    hooks_off,
                    before: shown_before,
                    after: shown_after,
                    before_effective,
                    after_effective,
                    others: others_expected as u64,
                    before_hash: v["before_sha256"].as_str().map(str::to_owned),
                    after_hash: v["after_sha256"].as_str().unwrap().to_owned(),
                    after_revision: v["after_revision"].as_str().unwrap().to_owned(),
                    token: v["token"].as_str().unwrap().to_owned(),
                    catalog: catalog.clone(),
                })))
            }
            Self::Apply(review) => {
                self.check(
                    v,
                    id,
                    instance,
                    "hook_edit_saved",
                    &[
                        "token",
                        "apply_id",
                        "revision",
                        "sha256",
                        "hooks_off",
                        "saved",
                        "effective",
                    ],
                )?;
                let saved = if v["saved"].is_null() {
                    None
                } else {
                    Some(Hook::parse(&v["saved"], &review.catalog)?)
                };
                let effective = Effective::parse(&v["effective"])?;
                if apply_id.is_none()
                    || v["apply_id"].as_str() != apply_id
                    || v["token"] != review.token
                    || v["revision"] != review.after_revision
                    || v["sha256"] != review.after_hash.as_str()
                    || v["hooks_off"] != review.hooks_off
                    || saved != review.after
                    || effective != review.after_effective
                {
                    return Err(invalid());
                }
                Ok(Reply::Saved(Box::new(Saved {
                    scope: review.scope,
                    action: review.action,
                    file: review.file.clone(),
                    hook_id: review.hook_id.clone(),
                    saved,
                    effective,
                    hooks_off: review.hooks_off,
                })))
            }
        }
    }
}

pub fn file_label(scope: Scope) -> &'static str {
    match scope {
        Scope::Project => "project .litetui/hooks.json",
        Scope::Global => "global hooks.json",
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn id() -> Identity {
        Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "m".into(),
        }
    }
    fn base(kind: &str) -> Value {
        json!({"kind":kind,"version":1,"session_id":"session","agent_id":"agent","backend":"custom",
            "model":"m","work_instance":"work","scope":"project"})
    }
    fn catalog() -> Catalog {
        Catalog {
            events: vec!["app_start".into(), "tool_before".into()],
            gate_events: vec!["tool_before".into()],
            sources: vec!["typed".into(), "rpc".into()],
        }
    }
    fn hook(id: &str) -> Hook {
        Hook {
            id: id.into(),
            events: vec!["tool_before".into()],
            executable: "audit.exe".into(),
            argv: vec!["--quiet".into(), "two words".into()],
            enabled: true,
            mode: "observe".into(),
            timeout: 5.0,
            sources: vec![],
            tools: vec!["read*".into()],
            env_count: 0,
            custom_cwd: false,
        }
    }
    fn wire(h: &Hook) -> Value {
        let mut v = h.form();
        v["env_count"] = json!(h.env_count);
        v["custom_cwd"] = json!(h.custom_cwd);
        v
    }
    fn snapshot_wire(rows: &[Hook]) -> Value {
        let mut v = base("hook_edit_snapshot");
        v["revision"] = json!("1".repeat(64));
        v["hooks_off"] = json!(false);
        v["state"] = json!("ok");
        v["error"] = Value::Null;
        v["text"] = Value::Null;
        v["file"] = json!(file_label(Scope::Project));
        v["rows"] = json!(rows.iter().map(wire).collect::<Vec<_>>());
        v["events"] = json!(catalog().events);
        v["gate_events"] = json!(catalog().gate_events);
        v["sources"] = json!(catalog().sources);
        v
    }
    fn request(action: Action, before: Option<&Hook>, hook: Option<&Hook>, rows: usize) -> Request {
        Request::Review {
            scope: Scope::Project,
            revision: "1".repeat(64),
            rows,
            catalog: catalog(),
            action,
            before: before.cloned().map(Box::new),
            hook: hook.cloned().map(Box::new),
        }
    }
    fn effective(enabled: bool, scope: &str) -> Value {
        json!({"scope":scope,"enabled":enabled,"command_id":"a".repeat(24)})
    }
    fn review_wire(
        action: Action,
        before: Option<&Hook>,
        after: Option<&Hook>,
        others: u64,
    ) -> Value {
        let mut v = base("hook_edit_review");
        let hook_id = after.or(before).unwrap().id.clone();
        for (key, value) in [
            ("revision", json!("1".repeat(64))),
            ("token", json!("2".repeat(64))),
            ("action", json!(action.key())),
            ("hook_id", json!(hook_id)),
            ("file", json!(file_label(Scope::Project))),
            ("created_file", json!(before.is_none() && others == 0)),
            ("hooks_off", json!(false)),
            ("before", before.map_or(Value::Null, wire)),
            ("after", after.map_or(Value::Null, wire)),
            (
                "before_effective",
                if before.is_some() {
                    effective(true, "project")
                } else {
                    Value::Null
                },
            ),
            (
                "after_effective",
                after.map_or(Value::Null, |a| effective(a.enabled, "project")),
            ),
            ("others", json!(others)),
            (
                "before_sha256",
                if before.is_none() && others == 0 {
                    Value::Null
                } else {
                    json!("3".repeat(64))
                },
            ),
            ("after_sha256", json!("4".repeat(64))),
            ("after_revision", json!("5".repeat(64))),
        ] {
            v[key] = value;
        }
        v
    }

    #[test]
    fn snapshot_is_strict_and_bound_to_scope_identity_and_catalog() {
        let req = Request::Snapshot(Scope::Project);
        let good = snapshot_wire(&[hook("a"), hook("b")]);
        let Ok(Reply::Snapshot(shot)) = req.reply(&good, &id(), "work", None) else {
            panic!()
        };
        assert_eq!(shot.rows.len(), 2);
        assert_eq!(shot.catalog, catalog());
        for (key, value) in [
            ("scope", json!("global")),
            ("work_instance", json!("other")),
            ("session_id", json!("other")),
            ("file", json!("global hooks.json")),
            ("revision", json!("zz")),
            ("extra", json!(1)),
        ] {
            let mut forged = good.clone();
            forged[key] = value;
            assert!(req.reply(&forged, &id(), "work", None).is_err(), "{key}");
        }
        let mut forged = good.clone();
        forged["rows"][0]["events"] = json!(["unlisted_event"]);
        assert!(req.reply(&forged, &id(), "work", None).is_err());
        let mut forged = good.clone();
        forged["rows"][1] = forged["rows"][0].clone();
        assert!(
            req.reply(&forged, &id(), "work", None).is_err(),
            "duplicate id"
        );
        let mut forged = good;
        forged["rows"][0]["env"] = json!({"SECRET":"x"});
        assert!(
            req.reply(&forged, &id(), "work", None).is_err(),
            "private field"
        );
    }

    #[test]
    fn command_carries_only_the_bounded_form_and_never_private_fields() {
        let mut before = hook("a");
        before.env_count = 2;
        before.custom_cwd = true;
        let mut edited = before.clone();
        edited.timeout = 2.5;
        let cmd = request(Action::Update, Some(&before), Some(&edited), 1).command(&id(), "work");
        assert_eq!(cmd["type"], REVIEW);
        assert_eq!(cmd["hook_id"], "a");
        assert_eq!(cmd["hook"]["timeout"], 2.5);
        let form = cmd["hook"].as_object().unwrap();
        assert_eq!(form.len(), 9);
        assert!(
            !form.contains_key("env")
                && !form.contains_key("cwd")
                && !form.contains_key("env_count")
        );
        let create = request(Action::Create, None, Some(&hook("n")), 0).command(&id(), "work");
        assert!(create["hook_id"].is_null() && create["hook"]["timeout"] == 5);
        let delete = request(Action::Delete, Some(&before), None, 1).command(&id(), "work");
        assert!(delete["hook"].is_null() && delete["action"] == "delete");
    }

    #[test]
    fn review_accepts_exactly_the_requested_create_update_and_delete() {
        let created = hook("n");
        let req = request(Action::Create, None, Some(&created), 0);
        let wire_review = review_wire(Action::Create, None, Some(&created), 0);
        assert!(matches!(
            req.reply(&wire_review, &id(), "work", None),
            Ok(Reply::Review(_))
        ));

        let mut before = hook("a");
        before.env_count = 1;
        let mut edited = before.clone();
        edited.executable = "new.exe".into();
        let req = request(Action::Update, Some(&before), Some(&edited), 2);
        let good = review_wire(Action::Update, Some(&before), Some(&edited), 1);
        assert!(matches!(
            req.reply(&good, &id(), "work", None),
            Ok(Reply::Review(_))
        ));
        let mut lost_private = good.clone();
        lost_private["after"]["env_count"] = json!(0);
        assert!(req.reply(&lost_private, &id(), "work", None).is_err());
        let mut other_command = good.clone();
        other_command["after"]["executable"] = json!("evil.exe");
        assert!(req.reply(&other_command, &id(), "work", None).is_err());
        let mut other_hook = good.clone();
        other_hook["hook_id"] = json!("elsewhere");
        assert!(req.reply(&other_hook, &id(), "work", None).is_err());
        let mut hidden_removal = good.clone();
        hidden_removal["others"] = json!(0);
        assert!(req.reply(&hidden_removal, &id(), "work", None).is_err());
        let mut stale = good.clone();
        stale["revision"] = json!("9".repeat(64));
        assert!(req.reply(&stale, &id(), "work", None).is_err());
        let mut same_hash = good;
        same_hash["after_sha256"] = same_hash["before_sha256"].clone();
        assert!(req.reply(&same_hash, &id(), "work", None).is_err());

        let req = request(Action::Delete, Some(&before), None, 2);
        let mut delete = review_wire(Action::Delete, Some(&before), None, 1);
        delete["after_effective"] = json!(effective(true, "global"));
        assert!(matches!(
            req.reply(&delete, &id(), "work", None),
            Ok(Reply::Review(_))
        ));
        delete["after_effective"] = json!(effective(true, "project"));
        assert!(
            req.reply(&delete, &id(), "work", None).is_err(),
            "deleted project hook cannot stay effective"
        );
        let mut resurrected = review_wire(Action::Delete, Some(&before), None, 1);
        resurrected["after"] = wire(&before);
        assert!(req.reply(&resurrected, &id(), "work", None).is_err());
        let mut mismatched = review_wire(Action::Update, Some(&before), Some(&edited), 1);
        mismatched["action"] = json!("create");
        assert!(
            request(Action::Update, Some(&before), Some(&edited), 2)
                .reply(&mismatched, &id(), "work", None)
                .is_err()
        );
    }

    #[test]
    fn apply_receipt_and_saved_readback_must_match_the_reviewed_change() {
        let created = hook("n");
        let req = request(Action::Create, None, Some(&created), 0);
        let Ok(Reply::Review(review)) = req.reply(
            &review_wire(Action::Create, None, Some(&created), 0),
            &id(),
            "work",
            None,
        ) else {
            panic!()
        };
        let apply = Request::Apply(review);
        let mut receipt = base("hook_edit_apply");
        receipt["token"] = json!("2".repeat(64));
        receipt["apply_id"] = json!("op-1");
        assert!(apply.readback(&receipt, &id(), "work", "other").is_err());
        let command = apply.readback(&receipt, &id(), "work", "op-1").unwrap();
        assert_eq!(command["type"], READBACK);
        assert_eq!(command["apply_id"], "op-1");
        assert_eq!(command["token"], "2".repeat(64));

        let mut saved = base("hook_edit_saved");
        for (key, value) in [
            ("token", json!("2".repeat(64))),
            ("apply_id", json!("op-1")),
            ("revision", json!("5".repeat(64))),
            ("sha256", json!("4".repeat(64))),
            ("hooks_off", json!(false)),
            ("saved", wire(&created)),
            ("effective", effective(true, "project")),
        ] {
            saved[key] = value;
        }
        assert!(matches!(
            apply.reply(&saved, &id(), "work", Some("op-1")),
            Ok(Reply::Saved(_))
        ));
        for (key, value) in [
            ("apply_id", json!("op-2")),
            ("revision", json!("6".repeat(64))),
            ("sha256", json!("7".repeat(64))),
            ("saved", Value::Null),
            ("effective", effective(false, "project")),
            ("hooks_off", json!(true)),
        ] {
            let mut forged = saved.clone();
            forged[key] = value;
            assert!(
                apply.reply(&forged, &id(), "work", Some("op-1")).is_err(),
                "{key}"
            );
        }
        let mut changed = saved.clone();
        changed["saved"]["executable"] = json!("drifted.exe");
        assert!(apply.reply(&changed, &id(), "work", Some("op-1")).is_err());
        assert!(apply.reply(&saved, &id(), "work", None).is_err());
    }

    fn sha(text: &str) -> String {
        sha256(text.as_bytes())
    }
    const TYPED: &str = r#"{"version":1,"hooks":[{"id":"keep","events":["tool_before"],"executable":"x.exe","env":{"K":"V"}}]}"#;
    fn canonical_document() -> String {
        serde_json::to_string_pretty(&json!({"version":1,"hooks":[{"id":"keep","events":["tool_before"],
            "executable":"x.exe","argv":[],"enabled":true,"mode":"observe","cwd":null,"env":{"K":"V"},
            "timeout":10,"sources":[],"tools":[]}]}))
        .unwrap()
    }
    fn repair_request() -> Request {
        Request::Repair {
            scope: Scope::Project,
            revision: "6".repeat(64),
            catalog: catalog(),
            text: TYPED.into(),
        }
    }
    fn repair_wire() -> Value {
        let document = canonical_document();
        let mut row = hook("keep");
        row.argv = vec![];
        row.timeout = 10.0;
        row.tools = vec![];
        row.executable = "x.exe".into();
        row.env_count = 1;
        let mut v = base("hook_repair_review");
        for (key, value) in [
            ("revision", json!("6".repeat(64))),
            ("token", json!("2".repeat(64))),
            ("file", json!(file_label(Scope::Project))),
            ("before_sha256", json!("6".repeat(64))),
            ("before_bytes", json!(120)),
            ("after_text", json!(document)),
            ("rows", json!([wire(&row)])),
            ("after_sha256", json!(sha(&document.replace('\n', "\r\n")))),
            ("after_revision", json!("5".repeat(64))),
            ("hooks_off", json!(false)),
        ] {
            v[key] = value;
        }
        v
    }

    #[test]
    fn unreadable_snapshot_carries_its_text_and_diagnostic_only_with_no_rows() {
        let req = Request::Snapshot(Scope::Project);
        let mut wire = snapshot_wire(&[]);
        wire["state"] = json!("invalid");
        wire["error"] = json!("unknown hook fields: bogus");
        wire["text"] = json!("{\"broken\": ");
        let Ok(Reply::Snapshot(shot)) = req.reply(&wire, &id(), "work", None) else {
            panic!()
        };
        let unreadable = shot.unreadable.expect("unreadable");
        assert_eq!(unreadable.text, "{\"broken\": ");
        assert!(unreadable.error.contains("bogus"));
        for (key, value) in [
            ("state", json!("fixed")),
            ("error", Value::Null),
            ("text", Value::Null),
            ("rows", json!([wire_row_for_test()])),
        ] {
            let mut forged = wire.clone();
            forged[key] = value;
            assert!(req.reply(&forged, &id(), "work", None).is_err(), "{key}");
        }
        let mut readable = snapshot_wire(&[hook("a")]);
        readable["text"] = json!("stray");
        assert!(req.reply(&readable, &id(), "work", None).is_err());
        let mut oversize = wire;
        oversize["text"] = json!("x".repeat(MAX_REPAIR + 1));
        assert!(req.reply(&oversize, &id(), "work", None).is_err());
    }
    fn wire_row_for_test() -> Value {
        wire(&hook("a"))
    }

    #[test]
    fn repair_review_must_show_the_document_whose_hash_the_source_reports() {
        let req = repair_request();
        let good = repair_wire();
        let command = req.command(&id(), "work");
        assert_eq!(command["type"], REPAIR_REVIEW);
        assert_eq!(command["text"], TYPED);
        assert!(matches!(
            req.reply(&good, &id(), "work", None),
            Ok(Reply::RepairReview(_))
        ));
        for (key, value) in [
            ("revision", json!("7".repeat(64))),
            ("before_sha256", json!("7".repeat(64))),
            ("after_sha256", json!("8".repeat(64))),
            (
                "after_text",
                json!(canonical_document().replace("x.exe", "evil.exe")),
            ),
            ("before_bytes", json!(0)),
            ("file", json!(file_label(Scope::Global))),
            ("rows", json!([])),
            ("token", json!("zz")),
        ] {
            let mut forged = good.clone();
            forged[key] = value;
            assert!(req.reply(&forged, &id(), "work", None).is_err(), "{key}");
        }
        let mut changed_row = good.clone();
        changed_row["rows"][0]["executable"] = json!("other.exe");
        assert!(req.reply(&changed_row, &id(), "work", None).is_err());
        let mut lost_hook = repair_request();
        if let Request::Repair { text, .. } = &mut lost_hook {
            *text = r#"{"version":1,"hooks":[]}"#.into();
        }
        assert!(
            lost_hook.reply(&good, &id(), "work", None).is_err(),
            "typed text and written document must agree"
        );
    }

    #[test]
    fn repair_apply_and_saved_readback_are_bound_to_the_review() {
        let Ok(Reply::RepairReview(review)) =
            repair_request().reply(&repair_wire(), &id(), "work", None)
        else {
            panic!()
        };
        let apply = Request::RepairApply(review);
        assert!(apply.applies() && apply.operation() == REPAIR_APPLY);
        let mut receipt = base("hook_repair_apply");
        receipt["token"] = json!("2".repeat(64));
        receipt["apply_id"] = json!("op-1");
        let command = apply.readback(&receipt, &id(), "work", "op-1").unwrap();
        assert_eq!(command["type"], REPAIR_READBACK);
        assert!(apply.readback(&receipt, &id(), "work", "op-2").is_err());
        let mut row = hook("keep");
        row.argv = vec![];
        row.timeout = 10.0;
        row.tools = vec![];
        row.executable = "x.exe".into();
        row.env_count = 1;
        let mut saved = base("hook_repair_saved");
        for (key, value) in [
            ("token", json!("2".repeat(64))),
            ("apply_id", json!("op-1")),
            ("revision", json!("5".repeat(64))),
            (
                "sha256",
                json!(sha(&canonical_document().replace('\n', "\r\n"))),
            ),
            ("hooks_off", json!(false)),
            ("rows", json!([wire(&row)])),
        ] {
            saved[key] = value;
        }
        assert!(matches!(
            apply.reply(&saved, &id(), "work", Some("op-1")),
            Ok(Reply::RepairSaved(_))
        ));
        for (key, value) in [
            ("revision", json!("6".repeat(64))),
            ("sha256", json!("9".repeat(64))),
            ("rows", json!([])),
            ("apply_id", json!("op-2")),
        ] {
            let mut forged = saved.clone();
            forged[key] = value;
            assert!(
                apply.reply(&forged, &id(), "work", Some("op-1")).is_err(),
                "{key}"
            );
        }
    }
}
