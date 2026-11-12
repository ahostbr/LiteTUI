//! Reviewed agent execution, subagent-route and identity preferences, one field per review.
//!
//! The Python source owns every scope, timing, bound and choice; this module pins the
//! same table and refuses any response that disagrees with it. The three standing
//! tool-authority lists change one entry at a time, and the review states whether that
//! entry WIDENS or NARROWS authority from this table, never from the source's prose.
use crate::{
    theme::Theme,
    work_lifecycle::{hash, keys},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Map, Value, json};
use std::{collections::BTreeMap, io};

pub const OPERATIONS: [&str; 4] = [
    "gui.agent.native_preferences",
    "gui.agent.native_review",
    "gui.agent.native_apply",
    "gui.agent.native_readback",
];
const MAX: i64 = 2_147_483_647;
const MAX_NAME: usize = 128;
const MAX_TEXT: usize = 256;
const MAX_ENTRY: usize = 512;
const MAX_ENTRIES: usize = 64;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Kind {
    Integer,
    Boolean,
    Choice,
    Text,
    OptionalText,
    Route,
    Override,
    TaskRoute,
    List,
}
struct Spec {
    key: &'static str,
    label: &'static str,
    group: &'static str,
    kind: Kind,
    scope: &'static str,
    timing: &'static str,
    min: Option<i64>,
    max: Option<i64>,
    choices: &'static [Option<&'static str>],
    help: &'static str,
}
const fn spec(
    key: &'static str,
    label: &'static str,
    group: &'static str,
    kind: Kind,
    scope: &'static str,
    timing: &'static str,
    help: &'static str,
) -> Spec {
    Spec {
        key,
        label,
        group,
        kind,
        scope,
        timing,
        min: None,
        max: None,
        choices: &[],
        help,
    }
}
const fn int(mut s: Spec, min: i64) -> Spec {
    s.min = Some(min);
    s.max = Some(MAX);
    s
}
const fn choices(mut s: Spec, choices: &'static [Option<&'static str>]) -> Spec {
    s.choices = choices;
    s
}
static SPECS: [Spec; 21] = [
    int(
        spec(
            "tool_iterations",
            "Tool rounds per turn",
            "execution",
            Kind::Integer,
            "conversation",
            "next_turn",
            "Maximum tool-use rounds in one turn before the agent is stopped.",
        ),
        1,
    ),
    int(
        spec(
            "tool_auto_background_s",
            "Auto-background after (s)",
            "execution",
            Kind::Integer,
            "conversation",
            "next_turn",
            "Seconds before a running shell call moves to the background; 0 disables it.",
        ),
        0,
    ),
    spec(
        "enter_interrupts",
        "Enter interrupts mid-turn",
        "execution",
        Kind::Boolean,
        "device",
        "immediate",
        "Off: Enter queues a mid-turn message. On: Enter interrupts and sends.",
    ),
    int(
        spec(
            "relay_approval_timeout_s",
            "Spawner approval wait (s)",
            "execution",
            Kind::Integer,
            "conversation",
            "next_turn",
            "Seconds a launched seat waits for its spawner's approval before refusing.",
        ),
        1,
    ),
    choices(
        spec(
            "permission_judge",
            "Count covered approvals",
            "execution",
            Kind::Choice,
            "device",
            "immediate",
            "Off, or count: logs whether a plain-read check would have covered each approval. Authorizes nothing.",
        ),
        &[None, Some("count")],
    ),
    choices(
        spec(
            "tool_context_mode",
            "Tool output context",
            "execution",
            Kind::Choice,
            "conversation",
            "next_turn",
            "Raw, masked or summarized tool output in context; the original is always kept.",
        ),
        &[Some("off"), Some("llm-tool-mask"), Some("llm-tool-summ")],
    ),
    int(
        spec(
            "tool_context_threshold_chars",
            "Tool context threshold",
            "execution",
            Kind::Integer,
            "conversation",
            "next_turn",
            "Results shorter than this many characters enter context unchanged.",
        ),
        0,
    ),
    spec(
        "tool_always_allow",
        "Always-allow rules",
        "authority",
        Kind::List,
        "conversation",
        "next_turn",
        "tool:capability rules that stop asking at exactly that authority. Deny rules still win.",
    ),
    spec(
        "tool_deny",
        "Always-refuse rules",
        "authority",
        Kind::List,
        "conversation",
        "next_turn",
        "tool:capability rules checked before any allow rule or profile.",
    ),
    spec(
        "tool_trusted_interpreters",
        "Trusted interpreters",
        "authority",
        Kind::List,
        "conversation",
        "next_turn",
        "Exact absolute interpreter paths that skip foreign-program confirmation; danger and deny rules still apply.",
    ),
    spec(
        "subagent_route",
        "Global subagent route",
        "routes",
        Kind::Route,
        "device",
        "immediate",
        "backend model for every instance; empty follows each parent. Loads nothing.",
    ),
    spec(
        "small_task_route",
        "Small-task worker route",
        "routes",
        Kind::TaskRoute,
        "device",
        "immediate",
        "backend model cognitive thinking_level; empty disables named-worker dispatch.",
    ),
    spec(
        "allow_local_subagents",
        "Allow local subagents",
        "routes",
        Kind::Boolean,
        "device",
        "immediate",
        "Expert-only local dispatch. It never loads a model for a child.",
    ),
    spec(
        "subagent_route_override",
        "Conversation route override",
        "routes",
        Kind::Override,
        "conversation",
        "next_turn",
        "backend model, follow (follow the parent) or empty (inherit the global route).",
    ),
    spec(
        "subagent_model",
        "Conversation subagent model",
        "routes",
        Kind::OptionalText,
        "conversation",
        "next_turn",
        "Legacy model for children when no override is set; empty clears it.",
    ),
    spec(
        "tool_summary_model",
        "Tool-summary model",
        "routes",
        Kind::OptionalText,
        "conversation",
        "next_turn",
        "Model for the tool-summary side call; empty uses the main model. Never loaded for you.",
    ),
    spec(
        "user_name",
        "Your name",
        "identity",
        Kind::Text,
        "defaults",
        "next_turn",
        "Included in the system prompt; blank omits it.",
    ),
    spec(
        "seat_name",
        "Fleet seat name",
        "identity",
        Kind::Text,
        "defaults",
        "next_turn",
        "Name requested from the registry the next time this seat registers; blank is LiteTUI.",
    ),
    int(
        spec(
            "owner_domain_cap",
            "Owner DOMAIN.md cap",
            "identity",
            Kind::Integer,
            "conversation",
            "next_turn",
            "Owner mode: characters of DOMAIN.md injected at conversation start.",
        ),
        1,
    ),
    spec(
        "skills_enabled",
        "Skills",
        "identity",
        Kind::Boolean,
        "conversation",
        "reconnect",
        "Load the skills index. Saved now; applies after reconnect, then use the reviewed Skills refresh.",
    ),
    spec(
        "skill_roots",
        "Extra skill libraries",
        "identity",
        Kind::List,
        "conversation",
        "reconnect",
        "Absolute or ~ library paths. Saved now; applies after reconnect, then use the reviewed Skills refresh.",
    ),
];
pub const FIELDS: [&str; 21] = [
    "tool_iterations",
    "tool_auto_background_s",
    "enter_interrupts",
    "relay_approval_timeout_s",
    "permission_judge",
    "tool_context_mode",
    "tool_context_threshold_chars",
    "tool_always_allow",
    "tool_deny",
    "tool_trusted_interpreters",
    "subagent_route",
    "small_task_route",
    "allow_local_subagents",
    "subagent_route_override",
    "subagent_model",
    "tool_summary_model",
    "user_name",
    "seat_name",
    "owner_domain_cap",
    "skills_enabled",
    "skill_roots",
];
fn spec_of(key: &str) -> io::Result<&'static Spec> {
    SPECS.iter().find(|s| s.key == key).ok_or_else(bad)
}
fn group_label(group: &str) -> &'static str {
    match group {
        "execution" => "Execution limits",
        "authority" => "Tool authority (standing rules)",
        "routes" => "Subagent routes",
        _ => "Identity and skills",
    }
}
fn scope_label(scope: &str) -> &'static str {
    match scope {
        "device" => "this device, every conversation",
        "defaults" => "this device's defaults",
        _ => "this conversation",
    }
}
fn timing_label(timing: &str) -> &'static str {
    match timing {
        "immediate" => "Applies immediately after the save is verified.",
        "reconnect" => {
            "Saved only: the running value is unchanged until reconnect. Nothing is refreshed now."
        }
        _ => "Applies from the next turn.",
    }
}

/// The preference a registered command opens first. Opening never applies anything.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Focus {
    Override,
    Global,
}
impl Focus {
    fn key(self) -> &'static str {
        match self {
            Self::Override => "subagent_route_override",
            Self::Global => "subagent_route",
        }
    }
}

fn bad() -> io::Error {
    io::Error::other("Agent preference response differs from its reviewed settings")
}
fn clean(s: &str, max: usize) -> bool {
    s.len() <= max && !s.chars().any(crate::display_control)
}
fn text_of(v: &Value, max: usize) -> Option<&str> {
    v.as_str().filter(|s| clean(s, max))
}
fn route_ok(v: &Value, names: &[&str], follow: bool) -> bool {
    if v.is_null() {
        return true;
    }
    let Some(o) = v.as_object() else {
        return false;
    };
    if follow && o.is_empty() {
        return true;
    }
    o.len() == names.len()
        && names.iter().all(|n| {
            o.get(*n).is_some_and(|x| {
                text_of(x, if *n == "model" { MAX_ENTRY } else { MAX_NAME })
                    .is_some_and(|s| !s.is_empty() && s == s.trim())
            })
        })
}
fn valid(spec: &Spec, v: &Value) -> bool {
    match spec.kind {
        Kind::Integer => v
            .as_i64()
            .is_some_and(|n| n >= spec.min.unwrap_or(0) && n <= spec.max.unwrap_or(MAX)),
        Kind::Boolean => v.is_boolean(),
        Kind::Choice => spec.choices.iter().any(|c| match c {
            None => v.is_null(),
            Some(s) => v.as_str() == Some(s),
        }),
        Kind::Text => text_of(v, MAX_NAME).is_some(),
        Kind::OptionalText => v.is_null() || text_of(v, MAX_TEXT).is_some(),
        Kind::Route => route_ok(v, &["backend", "model"], false),
        Kind::Override => route_ok(v, &["backend", "model"], true),
        Kind::TaskRoute => route_ok(
            v,
            &["backend", "model", "cognitive", "thinking_level"],
            false,
        ),
        Kind::List => v.as_array().is_some_and(|a| {
            a.len() <= MAX_ENTRIES && a.iter().all(|e| text_of(e, MAX_ENTRY).is_some())
        }),
    }
}
fn choice_values(spec: &Spec) -> Vec<Value> {
    spec.choices.iter().map(|c| json!(c)).collect()
}
fn show(spec: &Spec, v: &Value) -> String {
    match spec.kind {
        Kind::Boolean => if v.as_bool() == Some(true) {
            "on"
        } else {
            "off"
        }
        .into(),
        Kind::Choice => v.as_str().unwrap_or("off").into(),
        Kind::Text => match v.as_str() {
            Some("") | None => "(blank)".into(),
            Some(s) => s.into(),
        },
        Kind::OptionalText => v.as_str().unwrap_or("(none)").into(),
        Kind::Route | Kind::Override | Kind::TaskRoute => match v.as_object() {
            None => match spec.kind {
                Kind::Override => "(inherit the global route)".into(),
                Kind::TaskRoute => "(disabled)".into(),
                _ => "(follow the parent)".into(),
            },
            Some(o) if o.is_empty() => "(follow the parent)".into(),
            Some(o) => o
                .values()
                .filter_map(Value::as_str)
                .collect::<Vec<_>>()
                .join(" "),
        },
        Kind::List => match v.as_array() {
            Some(a) if a.len() == 1 => "1 entry".into(),
            Some(a) => format!("{} entries", a.len()),
            None => "?".into(),
        },
        Kind::Integer => v.to_string(),
    }
}
/// What a single-entry or switch change does to what runs without asking. Pinned here so
/// the review cannot be talked into a gentler description by the source's own prose.
fn effect(key: &str, op: Op, before: &Value, after: &Value) -> &'static str {
    match key {
        "tool_always_allow" | "tool_trusted_interpreters" => {
            if op == Op::Add {
                "widens"
            } else {
                "narrows"
            }
        }
        "tool_deny" => {
            if op == Op::Add {
                "narrows"
            } else {
                "widens"
            }
        }
        "allow_local_subagents" => {
            if after.as_bool() == Some(true) {
                "widens"
            } else {
                "narrows"
            }
        }
        "small_task_route" if before.is_null() != after.is_null() => {
            if before.is_null() {
                "widens"
            } else {
                "narrows"
            }
        }
        _ => "none",
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Op {
    Set,
    Add,
    Remove,
}
impl Op {
    fn name(self) -> &'static str {
        match self {
            Self::Set => "set",
            Self::Add => "add",
            Self::Remove => "remove",
        }
    }
}
fn proposed(spec: &Spec, current: &Value, op: Op, value: &Value) -> io::Result<Value> {
    if spec.kind == Kind::List {
        let list = current.as_array().ok_or_else(bad)?;
        let entry = text_of(value, MAX_ENTRY)
            .filter(|s| !s.is_empty() && *s == s.trim())
            .ok_or_else(bad)?;
        let at = list.iter().position(|e| e.as_str() == Some(entry));
        let mut next = list.clone();
        match (op, at) {
            (Op::Add, None) if list.len() < MAX_ENTRIES => next.push(value.clone()),
            (Op::Remove, Some(i)) => {
                next.remove(i);
            }
            _ => return Err(bad()),
        }
        return Ok(Value::Array(next));
    }
    if op != Op::Set || !valid(spec, value) || value == current {
        return Err(bad());
    }
    Ok(value.clone())
}

fn verify_binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if v["kind"] != kind
        || v["scope"] != "mixed"
        || v["work_instance"] != instance
        || !id
            .request()
            .as_object()
            .expect("identity")
            .iter()
            .all(|(k, val)| v[k] == *val)
    {
        return Err(bad());
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq, Eq)]
struct Field {
    saved: Value,
    effective: Value,
    editable: bool,
    overridden: bool,
    reason: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    fields: BTreeMap<String, Field>,
    revision: String,
}
impl Snapshot {
    fn parse(v: &Value, id: &Identity, instance: &str) -> io::Result<Self> {
        verify_binding(v, id, instance, "agent_preferences")?;
        keys(
            v,
            &[
                "version",
                "scope",
                "kind",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "fields",
                "revision",
            ],
        )?;
        if v["version"] != 1 {
            return Err(bad());
        }
        keys(&v["fields"], &FIELDS)?;
        let mut fields = BTreeMap::new();
        for spec in &SPECS {
            let raw = &v["fields"][spec.key];
            keys(
                raw,
                &[
                    "scope",
                    "apply_timing",
                    "minimum",
                    "maximum",
                    "choices",
                    "saved",
                    "effective",
                    "overridden",
                    "editable",
                    "reason",
                ],
            )?;
            if raw["scope"] != spec.scope
                || raw["apply_timing"] != spec.timing
                || raw["minimum"] != json!(spec.min)
                || raw["maximum"] != json!(spec.max)
                || raw["choices"] != Value::Array(choice_values(spec))
                || !valid(spec, &raw["saved"])
                || !valid(spec, &raw["effective"])
            {
                return Err(bad());
            }
            let editable = raw["editable"].as_bool().ok_or_else(bad)?;
            let overridden = raw["overridden"].as_bool().ok_or_else(bad)?;
            let reason = match &raw["reason"] {
                Value::Null if editable => None,
                Value::String(s) if !editable && !s.is_empty() && clean(s, 1024) => Some(s.clone()),
                _ => return Err(bad()),
            };
            if editable && (overridden || raw["saved"] != raw["effective"]) {
                return Err(bad());
            }
            fields.insert(
                spec.key.into(),
                Field {
                    saved: raw["saved"].clone(),
                    effective: raw["effective"].clone(),
                    editable,
                    overridden,
                    reason,
                },
            );
        }
        Ok(Self {
            fields,
            revision: hash(v, "revision")?,
        })
    }
    fn shown(&self, spec: &Spec) -> String {
        let f = &self.fields[spec.key];
        let mut s = show(spec, &f.effective);
        if f.saved != f.effective {
            s.push_str(&format!(" (saved {})", show(spec, &f.saved)));
        }
        if f.overridden {
            s.push_str(" (override)");
        }
        s
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Edit {
    before: Snapshot,
    key: String,
    op: Op,
    value: Value,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    edit: Edit,
    token: String,
    proposed: Value,
    effect: &'static str,
    consequence: String,
    destination: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(Edit),
    Apply(Review),
    Readback(Review, String, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Review),
    Saved(Snapshot, String),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
impl Request {
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut cmd = id.request();
        cmd["work_instance"] = json!(instance);
        cmd["type"] = json!(match self {
            Self::List => OPERATIONS[0],
            Self::Review(e) => {
                let spec = spec_of(&e.key)?;
                let row = e
                    .before
                    .fields
                    .get(&e.key)
                    .filter(|r| r.editable)
                    .ok_or_else(bad)?;
                proposed(spec, &row.effective, e.op, &e.value)?;
                cmd["revision"] = json!(e.before.revision);
                cmd["field"] = json!(e.key);
                cmd["op"] = json!(e.op.name());
                cmd["value"] = e.value.clone();
                OPERATIONS[1]
            }
            Self::Apply(r) => {
                cmd["token"] = json!(r.token);
                OPERATIONS[2]
            }
            Self::Readback(r, apply, _) => {
                cmd["token"] = json!(r.token);
                cmd["apply_id"] = json!(apply);
                OPERATIONS[3]
            }
        });
        Ok(cmd)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        cmd_id: &str,
    ) -> io::Result<Reply> {
        let (kind, e, token, applied) = match &self {
            Self::List => {
                return Ok(Reply::Update(Update::List(Snapshot::parse(
                    v, id, instance,
                )?)));
            }
            Self::Review(e) => ("agent_review", e, None, None),
            Self::Apply(r) => ("agent_applied", &r.edit, Some(&r.token), Some(cmd_id)),
            Self::Readback(r, apply, _) => {
                ("agent_saved", &r.edit, Some(&r.token), Some(apply.as_str()))
            }
        };
        verify_binding(v, id, instance, kind)?;
        let mut members = vec![
            "kind",
            "version",
            "scope",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "token",
            "field",
            "op",
            "value",
            "proposed",
            "effect",
            "consequence",
            "destination",
            "setting_scope",
            "apply_timing",
            "before",
            "after",
            "apply_id",
        ];
        if kind == "agent_saved" {
            members.push("preferences");
        }
        keys(v, &members)?;
        let spec = spec_of(&e.key)?;
        let current = e
            .before
            .fields
            .get(&e.key)
            .ok_or_else(bad)?
            .effective
            .clone();
        let want = proposed(spec, &current, e.op, &e.value)?;
        let stated = effect(&e.key, e.op, &current, &want);
        let actual = hash(v, "token")?;
        if v["version"] != 1
            || v["field"] != e.key
            || v["op"] != e.op.name()
            || v["value"] != e.value
            || v["proposed"] != want
            || v["effect"] != stated
            || v["setting_scope"] != spec.scope
            || v["apply_timing"] != spec.timing
            || v["before"] != e.before.revision
            || token.is_some_and(|t| *t != actual)
            || v["apply_id"] != json!(applied)
            || !v["consequence"]
                .as_str()
                .is_some_and(|s| !s.is_empty() && clean(s, 1024))
            || !v["destination"]
                .as_str()
                .is_some_and(|s| !s.is_empty() && clean(s, 4096))
        {
            return Err(bad());
        }
        match self {
            Self::List => unreachable!(),
            Self::Review(edit) if v["after"].is_null() => {
                Ok(Reply::Update(Update::Review(Review {
                    consequence: v["consequence"].as_str().unwrap_or_default().into(),
                    destination: v["destination"].as_str().unwrap_or_default().into(),
                    proposed: want,
                    effect: stated,
                    edit,
                    token: actual,
                })))
            }
            Self::Apply(review) => {
                let after = hash(v, "after")?;
                if after == review.edit.before.revision {
                    return Err(bad());
                }
                Ok(Reply::Next(Self::Readback(review, cmd_id.into(), after)))
            }
            Self::Readback(review, _, after) => {
                let saved = Snapshot::parse(&v["preferences"], id, instance)?;
                let spec = spec_of(&review.edit.key)?;
                let key = review.edit.key.clone();
                let mut expected = review.edit.before;
                expected.revision = after.clone();
                let row = expected.fields.get_mut(&key).ok_or_else(bad)?;
                row.saved = review.proposed.clone();
                if spec.timing == "reconnect" {
                    // Saved but pending: the source must say why it is no longer editable.
                    row.editable = false;
                    row.reason = saved.fields.get(&key).and_then(|f| f.reason.clone());
                } else {
                    row.effective = review.proposed.clone();
                }
                if saved != expected || v["after"] != after {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Saved(saved, key)))
            }
            _ => Err(bad()),
        }
    }
}

#[derive(Clone, Debug, PartialEq)]
enum Draft {
    Value(Value),
    Text(String),
    Add(String),
    Remove(usize),
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    visible: bool,
    data: Option<Snapshot>,
    selected: usize,
    draft: Option<Draft>,
    review: Option<Review>,
    note: String,
    scroll: usize,
    height: usize,
    lines: usize,
    review_seen: usize,
    review_width: u16,
}
fn prefill(spec: &Spec, v: &Value) -> String {
    match spec.kind {
        Kind::Integer => v.to_string(),
        Kind::Text | Kind::OptionalText => v.as_str().unwrap_or_default().into(),
        Kind::Override if v.as_object().is_some_and(Map::is_empty) => "follow".into(),
        _ => v
            .as_object()
            .map(|o| {
                o.values()
                    .filter_map(Value::as_str)
                    .collect::<Vec<_>>()
                    .join(" ")
            })
            .unwrap_or_default(),
    }
}
fn parse_draft(spec: &Spec, text: &str) -> Result<Value, &'static str> {
    let text = text.trim();
    let route = |names: &[&str]| -> Result<Value, &'static str> {
        if text.is_empty() {
            return Ok(Value::Null);
        }
        let parts: Vec<&str> = if names.len() == 2 {
            text.split_once(char::is_whitespace)
                .map(|(a, b)| vec![a, b.trim()])
                .unwrap_or_default()
        } else {
            text.split_whitespace().collect()
        };
        if parts.len() != names.len() || parts.iter().any(|p| p.is_empty()) {
            return Err(
                "Write the route as: backend model (small-task: backend model cognitive level).",
            );
        }
        Ok(Value::Object(
            names
                .iter()
                .zip(parts)
                .map(|(n, p)| ((*n).to_owned(), json!(p)))
                .collect(),
        ))
    };
    match spec.kind {
        Kind::Integer => text
            .bytes()
            .all(|b| b.is_ascii_digit())
            .then(|| text.parse::<i64>().ok())
            .flatten()
            .map(|n| json!(n))
            .ok_or("Enter a whole number."),
        Kind::Text => Ok(json!(text)),
        Kind::OptionalText => Ok(if text.is_empty() {
            Value::Null
        } else {
            json!(text)
        }),
        Kind::Route => route(&["backend", "model"]),
        Kind::Override if text.eq_ignore_ascii_case("follow") => Ok(json!({})),
        Kind::Override => route(&["backend", "model"]),
        Kind::TaskRoute => route(&["backend", "model", "cognitive", "thinking_level"]),
        _ => Err("Use the keys shown for this field."),
    }
}
impl View {
    pub fn open(&mut self, focus: Option<Focus>) {
        *self = Self {
            open: true,
            waiting: true,
            selected: focus
                .and_then(|f| FIELDS.iter().position(|k| *k == f.key()))
                .unwrap_or(0),
            ..Self::default()
        };
    }
    pub fn open_field(&mut self, key: &str) {
        self.open(None);
        self.selected = FIELDS.iter().position(|k| *k == key).unwrap_or(0);
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn update(&mut self, r: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        self.scroll = 0;
        self.note.clear();
        self.review_seen = 0;
        match r {
            Ok(Update::List(s)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
            }
            Ok(Update::Review(r)) => self.review = Some(r),
            Ok(Update::Saved(s, key)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
                self.note = match spec_of(&key).map(|s| s.timing) {
                    Ok("reconnect") => "Saved and verified on disk. Pending reconnect: the running value is unchanged.".into(),
                    _ => "Saved and verified on disk; the running value matches.".into(),
                };
            }
            Err(reason) => {
                self.data = None;
                self.review = None;
                self.draft = None;
                self.note = crate::live::terminal_text(&reason);
            }
        }
    }
    fn begin(&mut self, key: KeyCode) -> Option<()> {
        let spec = &SPECS[self.selected];
        let row = self.data.as_ref()?.fields.get(spec.key)?;
        if !row.editable {
            self.note = row.reason.clone().unwrap_or_default();
            return None;
        }
        self.draft = match (key, spec.kind) {
            (
                KeyCode::Char('e'),
                Kind::Integer
                | Kind::Text
                | Kind::OptionalText
                | Kind::Route
                | Kind::Override
                | Kind::TaskRoute,
            ) => Some(Draft::Text(prefill(spec, &row.effective))),
            (KeyCode::Char('a'), Kind::List) => Some(Draft::Add(String::new())),
            (KeyCode::Char('x'), Kind::List)
                if row.effective.as_array().is_some_and(|a| !a.is_empty()) =>
            {
                Some(Draft::Remove(0))
            }
            _ => {
                self.note = "That key does not apply to this field.".into();
                None
            }
        };
        self.note = match &self.draft {
            Some(Draft::Remove(_)) => "←/→ choose the entry to remove, Enter reviews.".into(),
            Some(_) => "Type the value, Enter reviews, Esc cancels.".into(),
            None => std::mem::take(&mut self.note),
        };
        Some(())
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || self.waiting {
            return None;
        }
        let typing =
            self.review.is_none() && matches!(self.draft, Some(Draft::Text(_) | Draft::Add(_)));
        if !(key.modifiers.is_empty() || typing && key.modifiers == KeyModifiers::SHIFT) {
            return None;
        }
        if key.code == KeyCode::Esc {
            // Cancelling a review also discards its draft: nothing stays half-confirmed.
            if self.review.take().is_some() | self.draft.take().is_some() {
                self.review_seen = 0;
                self.note.clear();
                self.hide();
            } else {
                *self = Self::default();
            }
            return None;
        }
        if matches!(key.code, KeyCode::PageUp | KeyCode::PageDown) {
            self.scroll = if key.code == KeyCode::PageUp {
                self.scroll.saturating_sub(self.height)
            } else {
                self.scroll
                    .saturating_add(self.height)
                    .min(self.lines.saturating_sub(self.height))
            };
            self.hide();
            return None;
        }
        if typing && let Some(Draft::Text(text) | Draft::Add(text)) = &mut self.draft {
            match key.code {
                KeyCode::Char(c) if !crate::display_control(c) => {
                    if text.len() < MAX_ENTRY {
                        text.push(c);
                    }
                    self.hide();
                    return None;
                }
                KeyCode::Backspace => {
                    text.pop();
                    self.hide();
                    return None;
                }
                _ => {}
            }
        }
        let spec = &SPECS[self.selected];
        let request = match key.code {
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Up | KeyCode::Down if self.review.is_none() => {
                self.selected = if key.code == KeyCode::Up {
                    self.selected.saturating_sub(1)
                } else {
                    (self.selected + 1).min(SPECS.len() - 1)
                };
                self.draft = None;
                self.note.clear();
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Char('e' | 'a' | 'x') if self.review.is_none() && !typing => {
                self.begin(key.code);
                self.hide();
                return None;
            }
            KeyCode::Left | KeyCode::Right if self.review.is_none() && !typing => {
                let row = self.data.as_ref()?.fields.get(spec.key)?;
                let forward = key.code == KeyCode::Right;
                if let (Kind::List, Some(Draft::Remove(at))) = (spec.kind, &self.draft) {
                    let n = row.effective.as_array().map_or(1, Vec::len).max(1);
                    self.draft = Some(Draft::Remove(if forward {
                        (at + 1) % n
                    } else {
                        (at + n - 1) % n
                    }));
                } else if matches!(spec.kind, Kind::Boolean | Kind::Choice) {
                    if !row.editable {
                        self.note = row.reason.clone().unwrap_or_default();
                        self.hide();
                        return None;
                    }
                    let options = if spec.kind == Kind::Boolean {
                        vec![json!(false), json!(true)]
                    } else {
                        choice_values(spec)
                    };
                    let old = match &self.draft {
                        Some(Draft::Value(v)) => v,
                        _ => &row.effective,
                    };
                    let pos = options.iter().position(|v| v == old)?;
                    let step = if forward { 1 } else { options.len() - 1 };
                    self.draft = Some(Draft::Value(options[(pos + step) % options.len()].clone()));
                }
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Enter if self.visible => {
                let before = self.data.clone()?;
                let current = before.fields.get(spec.key)?.effective.clone();
                let (op, value) = match self.draft.clone()? {
                    Draft::Value(v) => (Op::Set, v),
                    Draft::Text(t) => match parse_draft(spec, &t) {
                        Ok(v) => (Op::Set, v),
                        Err(why) => {
                            self.note = why.into();
                            self.hide();
                            return None;
                        }
                    },
                    Draft::Add(t) => (Op::Add, json!(t.trim())),
                    Draft::Remove(i) => (Op::Remove, current.get(i)?.clone()),
                };
                if proposed(spec, &current, op, &value).is_err() {
                    self.note =
                        "Choose a different value that fits this field's bounds and form.".into();
                    self.hide();
                    return None;
                }
                Request::Review(Edit {
                    before,
                    key: spec.key.into(),
                    op,
                    value,
                })
            }
            _ => return None,
        };
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(request)
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Agent preferences".to_owned()];
        if let Some(r) = &self.review {
            let spec = spec_of(&r.edit.key).expect("reviewed field");
            let current = &r.edit.before.fields[&r.edit.key].effective;
            lines.push(format!("Save {}?", spec.label));
            lines.push(format!("Saved in: {}", scope_label(spec.scope)));
            lines.push(match r.edit.op {
                Op::Add => format!("Add: {}", show_entry(&r.edit.value)),
                Op::Remove => format!("Remove: {}", show_entry(&r.edit.value)),
                Op::Set => format!(
                    "Current: {}   New: {}",
                    show(spec, current),
                    show(spec, &r.proposed)
                ),
            });
            match r.effect {
                "widens" => lines.push("AUTHORITY WIDENS: more can run than before without a fresh prompt or dispatch.".into()),
                "narrows" => lines.push("AUTHORITY NARROWS: less can run than before.".into()),
                _ => {}
            }
            lines.push(r.consequence.clone());
            lines.push(timing_label(spec.timing).into());
            lines.push(format!("Writes: {}", r.destination));
            lines.push("Enter save   Esc cancel".into());
        } else if let Some(s) = &self.data {
            let first = self.selected.saturating_sub(3);
            let mut group = "";
            for (i, spec) in SPECS.iter().enumerate().skip(first).take(8) {
                if spec.group != group {
                    group = spec.group;
                    lines.push(group_label(group).to_owned());
                }
                let f = &s.fields[spec.key];
                lines.push(format!(
                    "{} {}: {}{}",
                    if i == self.selected { ">" } else { " " },
                    spec.label,
                    s.shown(spec),
                    if f.editable { "" } else { " [locked]" }
                ));
            }
            let spec = &SPECS[self.selected];
            let f = &s.fields[spec.key];
            lines.push(format!(
                "Field {}/{} · {}",
                self.selected + 1,
                SPECS.len(),
                spec.help
            ));
            lines.push(format!(
                "Saved in {} · {}",
                scope_label(spec.scope),
                timing_label(spec.timing)
            ));
            if let (Kind::List, Some(a)) = (spec.kind, f.effective.as_array()) {
                for (i, e) in a.iter().enumerate().take(8) {
                    lines.push(format!(
                        "{} {}. {}",
                        if self.draft == Some(Draft::Remove(i)) {
                            "x"
                        } else {
                            " "
                        },
                        i + 1,
                        show_entry(e)
                    ));
                }
            }
            if let Some(why) = &f.reason {
                lines.push(format!("Read-only: {why}"));
            }
            match &self.draft {
                Some(Draft::Value(v)) => lines.push(format!("New value: {}", show(spec, v))),
                Some(Draft::Text(t)) => lines.push(format!("New value: {t}_")),
                Some(Draft::Add(t)) => lines.push(format!("Add entry: {t}_")),
                Some(Draft::Remove(_)) => {
                    lines.push("Choose the entry to remove (x marks it).".into())
                }
                None => {}
            }
            lines.push(
                match (f.editable, spec.kind) {
                    (false, _) => "↑/↓ field   F5 refresh   Esc back",
                    (_, Kind::Boolean | Kind::Choice) => {
                        "↑/↓ field   ←/→ choose   Enter review   F5 refresh   Esc back"
                    }
                    (_, Kind::List) => {
                        "↑/↓ field   A add   X remove   Enter review   F5 refresh   Esc back"
                    }
                    _ => "↑/↓ field   E edit   Enter review   F5 refresh   Esc back",
                }
                .into(),
            );
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone());
        }
        if self.waiting {
            lines.push("Waiting for the source…".into());
        }
        if self.data.is_none() && !self.waiting && self.review.is_none() {
            lines.push("F5 refresh   Esc back".into());
        }
        let safe = lines
            .iter()
            .map(|s| crate::ui::display_safe(s))
            .collect::<Vec<_>>()
            .join("\n");
        let wrapped = crate::ui::wrap(&safe, usize::from(area.width.saturating_sub(2)));
        self.height = usize::from(area.height.saturating_sub(2));
        self.lines = wrapped.len();
        self.scroll = self.scroll.min(self.lines.saturating_sub(self.height));
        let drawable = area.width >= 36 && self.height >= 5;
        if self.review_width != area.width {
            self.review_width = area.width;
            self.review_seen = 0;
        }
        if self.review.is_some() && drawable && self.scroll <= self.review_seen {
            self.review_seen = self
                .review_seen
                .max(self.scroll + self.height)
                .min(self.lines);
        }
        self.visible = drawable && (self.review.is_none() || self.review_seen == self.lines);
        let shown = if !drawable {
            "Resize to read the review. Esc back.".into()
        } else {
            wrapped
                .iter()
                .skip(self.scroll)
                .take(self.height)
                .cloned()
                .collect::<Vec<_>>()
                .join("\n")
        };
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(shown)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" Agent preferences ")),
            area,
        );
    }
}
fn show_entry(v: &Value) -> String {
    v.as_str()
        .map_or_else(|| v.to_string(), |s| format!("\"{s}\""))
}

#[cfg(test)]
mod tests {
    use super::*;
    use ratatui::{Terminal, backend::TestBackend};

    fn fixture() -> (Identity, Value) {
        let id = Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let mut root = id.request();
        for (key, value) in [
            ("kind", json!("agent_preferences")),
            ("scope", json!("mixed")),
            ("version", json!(1)),
            ("work_instance", json!("i")),
            ("revision", json!("1".repeat(64))),
        ] {
            root[key] = value;
        }
        for spec in &SPECS {
            let val = match spec.kind {
                Kind::Integer => json!(spec.min.unwrap_or(0).max(5)),
                Kind::Boolean => json!(false),
                Kind::Choice => json!(spec.choices[0]),
                Kind::Text => json!("name"),
                Kind::List => json!([]),
                _ => Value::Null,
            };
            root["fields"][spec.key] = json!({
                "scope": spec.scope, "apply_timing": spec.timing, "minimum": spec.min, "maximum": spec.max,
                "choices": choice_values(spec), "saved": val, "effective": val,
                "overridden": false, "editable": true, "reason": null,
            });
        }
        (id, root)
    }
    fn open_view(doc: &Value, id: &Identity) -> (View, Terminal<TestBackend>) {
        view_again(doc, id, None)
    }
    fn paint(view: &mut View, terminal: &mut Terminal<TestBackend>) -> String {
        let theme = crate::theme::builtin_themes().remove(0);
        terminal
            .draw(|frame| view.draw(frame, frame.area(), &theme))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content()
            .iter()
            .map(|c| c.symbol())
            .collect()
    }
    fn press(view: &mut View, code: KeyCode) -> Option<Request> {
        view.key(KeyEvent::new(code, KeyModifiers::NONE))
    }
    fn go_to(view: &mut View, key: &str) {
        let at = FIELDS.iter().position(|k| *k == key).unwrap();
        for _ in 0..at {
            press(view, KeyCode::Down);
        }
    }

    #[test]
    fn the_pinned_table_agrees_with_itself() {
        assert_eq!(SPECS.iter().map(|s| s.key).collect::<Vec<_>>(), FIELDS);
        for s in &SPECS {
            assert_eq!(s.min.is_some(), s.kind == Kind::Integer, "{}", s.key);
            assert_eq!(!s.choices.is_empty(), s.kind == Kind::Choice, "{}", s.key);
        }
    }

    #[test]
    fn snapshot_pins_the_source_table_and_refuses_inconsistent_editability() {
        let (id, doc) = fixture();
        assert!(Snapshot::parse(&doc, &id, "i").is_ok());
        for (path, value) in [
            ("/work_instance", json!("old")),
            ("/version", json!(2)),
            ("/scope", json!("device")),
            ("/fields/tool_iterations/minimum", json!(0)),
            ("/fields/tool_iterations/apply_timing", json!("immediate")),
            ("/fields/enter_interrupts/scope", json!("conversation")),
            ("/fields/tool_iterations/saved", json!(0)),
            ("/fields/tool_iterations/effective", json!(true)),
            ("/fields/tool_context_mode/effective", json!("banana")),
            ("/fields/tool_context_mode/choices", json!(["off"])),
            ("/fields/tool_iterations/overridden", json!(true)),
            ("/fields/tool_iterations/editable", json!(false)),
            ("/fields/tool_iterations/reason", json!("why")),
            ("/fields/tool_deny/saved", json!(["a\u{202e}b"])),
            ("/fields/subagent_route/saved", json!({"backend": "codex"})),
            ("/fields/subagent_route/saved", json!({})),
            (
                "/fields/subagent_route_override/saved",
                json!({"backend": ""}),
            ),
            ("/fields/seat_name/saved", json!("x".repeat(129))),
        ] {
            let mut drift = doc.clone();
            *drift.pointer_mut(path).unwrap() = value;
            assert!(Snapshot::parse(&drift, &id, "i").is_err(), "{path}");
        }
        let mut extra = doc.clone();
        extra["fields"]["added"] = json!({});
        assert!(Snapshot::parse(&extra, &id, "i").is_err());
        let mut locked = doc;
        locked["fields"]["tool_iterations"]["editable"] = json!(false);
        locked["fields"]["tool_iterations"]["reason"] =
            json!("Environment LM_TOOL_ITERS owns this preference");
        locked["fields"]["tool_iterations"]["overridden"] = json!(true);
        assert!(Snapshot::parse(&locked, &id, "i").is_ok());
    }

    #[test]
    fn authority_effects_come_from_the_pinned_table() {
        let none = Value::Null;
        for (key, op, widens) in [
            ("tool_always_allow", Op::Add, "widens"),
            ("tool_always_allow", Op::Remove, "narrows"),
            ("tool_deny", Op::Add, "narrows"),
            ("tool_deny", Op::Remove, "widens"),
            ("tool_trusted_interpreters", Op::Add, "widens"),
            ("tool_trusted_interpreters", Op::Remove, "narrows"),
        ] {
            assert_eq!(effect(key, op, &none, &none), widens, "{key}");
        }
        assert_eq!(
            effect(
                "allow_local_subagents",
                Op::Set,
                &json!(false),
                &json!(true)
            ),
            "widens"
        );
        assert_eq!(
            effect(
                "allow_local_subagents",
                Op::Set,
                &json!(true),
                &json!(false)
            ),
            "narrows"
        );
        let route =
            json!({"backend": "b", "model": "m", "cognitive": "c", "thinking_level": "low"});
        assert_eq!(effect("small_task_route", Op::Set, &none, &route), "widens");
        assert_eq!(
            effect("small_task_route", Op::Set, &route, &none),
            "narrows"
        );
        assert_eq!(effect("small_task_route", Op::Set, &route, &route), "none");
        assert_eq!(
            effect("tool_iterations", Op::Set, &json!(1), &json!(2)),
            "none"
        );
    }

    #[test]
    fn integer_edit_is_bounded_and_review_needs_a_painted_screen() {
        let (id, doc) = fixture();
        let (mut view, mut terminal) = open_view(&doc, &id);
        paint(&mut view, &mut terminal);
        press(&mut view, KeyCode::Char('e'));
        for _ in 0..3 {
            press(&mut view, KeyCode::Backspace);
        }
        for c in "0".chars() {
            press(&mut view, KeyCode::Char(c));
        }
        paint(&mut view, &mut terminal);
        assert!(
            press(&mut view, KeyCode::Enter).is_none(),
            "below the source minimum"
        );
        assert!(paint(&mut view, &mut terminal).contains("fits this field's bounds"));
        press(&mut view, KeyCode::Backspace);
        press(&mut view, KeyCode::Char('9'));
        press(&mut view, KeyCode::Char('x'));
        paint(&mut view, &mut terminal);
        assert!(
            press(&mut view, KeyCode::Enter).is_none(),
            "not a whole number"
        );
        press(&mut view, KeyCode::Backspace);
        // Unpainted edits cannot be confirmed; paint, then review.
        assert!(press(&mut view, KeyCode::Enter).is_none());
        paint(&mut view, &mut terminal);
        let request = press(&mut view, KeyCode::Enter).unwrap();
        let command = request.command(&id, "i").unwrap();
        assert_eq!(command["type"], "gui.agent.native_review");
        assert_eq!(
            (
                command["field"].as_str(),
                command["op"].as_str(),
                &command["value"]
            ),
            (Some("tool_iterations"), Some("set"), &json!(9))
        );
    }

    #[test]
    fn widening_list_review_states_authority_and_apply_needs_a_second_painted_confirmation() {
        let (id, doc) = fixture();
        let (mut view, mut terminal) = open_view(&doc, &id);
        go_to(&mut view, "tool_always_allow");
        paint(&mut view, &mut terminal);
        press(&mut view, KeyCode::Char('a'));
        for c in "bash:process_execution".chars() {
            press(&mut view, KeyCode::Char(c));
        }
        paint(&mut view, &mut terminal);
        let request = press(&mut view, KeyCode::Enter).unwrap();
        assert!(matches!(request, Request::Review(_)));
        let command = request.command(&id, "i").unwrap();
        assert_eq!(
            (command["op"].as_str(), command["value"].as_str()),
            (Some("add"), Some("bash:process_execution"))
        );
        let mut reply = id.request();
        for (k, v) in [
            ("kind", json!("agent_review")),
            ("version", json!(1)),
            ("scope", json!("mixed")),
            ("work_instance", json!("i")),
            ("token", json!("a".repeat(64))),
            ("field", json!("tool_always_allow")),
            ("op", json!("add")),
            ("value", json!("bash:process_execution")),
            ("proposed", json!(["bash:process_execution"])),
            ("effect", json!("widens")),
            (
                "consequence",
                json!("Standing approval will run without asking."),
            ),
            ("destination", json!("C:/data/convo/settings.json")),
            ("setting_scope", json!("conversation")),
            ("apply_timing", json!("next_turn")),
            ("before", json!("1".repeat(64))),
            ("after", Value::Null),
            ("apply_id", Value::Null),
        ] {
            reply[k] = v;
        }
        for (path, value) in [
            ("/effect", json!("narrows")),
            ("/effect", json!("none")),
            ("/proposed", json!(["other:network"])),
            ("/setting_scope", json!("device")),
            ("/apply_timing", json!("immediate")),
            ("/op", json!("remove")),
            ("/before", json!("2".repeat(64))),
            ("/apply_id", json!("early")),
            ("/consequence", json!("")),
        ] {
            let mut drift = reply.clone();
            *drift.pointer_mut(path).unwrap() = value;
            assert!(
                request.clone().response(&drift, &id, "i", "c").is_err(),
                "{path}"
            );
        }
        let Ok(Reply::Update(update)) = request.response(&reply, &id, "i", "c") else {
            panic!("reply expected");
        };
        view.update(Ok(update));
        // The review is not confirmable until it has been painted in full.
        assert!(press(&mut view, KeyCode::Enter).is_none());
        let screen = paint(&mut view, &mut terminal);
        assert!(screen.contains("AUTHORITY WIDENS"), "{screen}");
        assert!(screen.contains("Save Always-allow rules?"), "{screen}");
        let apply = press(&mut view, KeyCode::Enter).unwrap();
        assert_eq!(
            apply.command(&id, "i").unwrap()["type"],
            "gui.agent.native_apply"
        );
        // Cancelling instead leaves no draft or review behind.
        let (mut view, mut terminal) = open_view(&doc, &id);
        go_to(&mut view, "tool_deny");
        paint(&mut view, &mut terminal);
        press(&mut view, KeyCode::Char('a'));
        press(&mut view, KeyCode::Char('x'));
        paint(&mut view, &mut terminal);
        assert!(press(&mut view, KeyCode::Enter).is_some());
        view.waiting = false;
        view.review = Some(Review {
            edit: Edit {
                before: Snapshot::parse(&doc, &id, "i").unwrap(),
                key: "tool_deny".into(),
                op: Op::Add,
                value: json!("x"),
            },
            token: "a".repeat(64),
            proposed: json!(["x"]),
            effect: "narrows",
            consequence: "c".into(),
            destination: "d".into(),
        });
        press(&mut view, KeyCode::Esc);
        assert!(view.review.is_none() && view.draft.is_none());
    }

    #[test]
    fn readback_accepts_only_the_reviewed_change_and_reports_pending_reconnect() {
        let (id, doc) = fixture();
        let before = Snapshot::parse(&doc, &id, "i").unwrap();
        let review = |key: &str, op: Op, value: Value, want: Value| {
            let current = before.fields[key].effective.clone();
            Review {
                edit: Edit {
                    before: before.clone(),
                    key: key.into(),
                    op,
                    value,
                },
                token: "a".repeat(64),
                effect: effect(key, op, &current, &want),
                proposed: want,
                consequence: "c".into(),
                destination: "d".into(),
            }
        };
        let receipt = |r: &Review, saved: Value| {
            let mut v = id.request();
            let spec = spec_of(&r.edit.key).unwrap();
            for (k, x) in [
                ("kind", json!("agent_saved")),
                ("version", json!(1)),
                ("scope", json!("mixed")),
                ("work_instance", json!("i")),
                ("token", json!(r.token)),
                ("field", json!(r.edit.key)),
                ("op", json!(r.edit.op.name())),
                ("value", r.edit.value.clone()),
                ("proposed", r.proposed.clone()),
                ("effect", json!(r.effect)),
                ("consequence", json!("c")),
                ("destination", json!("d")),
                ("setting_scope", json!(spec.scope)),
                ("apply_timing", json!(spec.timing)),
                ("before", json!(r.edit.before.revision)),
                ("after", json!("b".repeat(64))),
                ("apply_id", json!("apply")),
                ("preferences", saved),
            ] {
                v[k] = x;
            }
            v
        };
        // An immediate field saves and takes effect.
        let r = review("enter_interrupts", Op::Set, json!(true), json!(true));
        let mut saved = doc.clone();
        saved["revision"] = json!("b".repeat(64));
        saved["fields"]["enter_interrupts"]["saved"] = json!(true);
        saved["fields"]["enter_interrupts"]["effective"] = json!(true);
        let request = Request::Readback(r.clone(), "apply".into(), "b".repeat(64));
        let good = receipt(&r, saved.clone());
        assert!(matches!(
            request.clone().response(&good, &id, "i", "readback"),
            Ok(Reply::Update(Update::Saved(_, _)))
        ));
        for (path, value) in [
            ("/field", json!("tool_deny")),
            ("/apply_id", json!("replay")),
            ("/after", json!("c".repeat(64))),
            (
                "/preferences/fields/seat_name/effective",
                json!("different"),
            ),
            ("/preferences/fields/seat_name/saved", json!("different")),
            ("/preferences/fields/tool_deny/saved", json!(["x:network"])),
            (
                "/preferences/fields/enter_interrupts/effective",
                json!(false),
            ),
            ("/preferences/revision", json!("d".repeat(64))),
        ] {
            let mut drift = good.clone();
            *drift.pointer_mut(path).unwrap() = value;
            assert!(
                request
                    .clone()
                    .response(&drift, &id, "i", "readback")
                    .is_err(),
                "{path}"
            );
        }
        // A reconnect field is saved but its running value must stay exactly as it was.
        let r = review("skills_enabled", Op::Set, json!(true), json!(true));
        let mut pending = doc.clone();
        pending["revision"] = json!("b".repeat(64));
        pending["fields"]["skills_enabled"]["saved"] = json!(true);
        pending["fields"]["skills_enabled"]["editable"] = json!(false);
        pending["fields"]["skills_enabled"]["reason"] = json!("Saved; reconnect is required");
        let request = Request::Readback(r.clone(), "apply".into(), "b".repeat(64));
        assert!(
            request
                .clone()
                .response(&receipt(&r, pending.clone()), &id, "i", "readback")
                .is_ok()
        );
        pending["fields"]["skills_enabled"]["effective"] = json!(true);
        pending["fields"]["skills_enabled"]["editable"] = json!(true);
        pending["fields"]["skills_enabled"]["reason"] = Value::Null;
        assert!(
            request
                .response(&receipt(&r, pending), &id, "i", "readback")
                .is_err(),
            "a reconnect-timed field cannot claim to have applied"
        );
    }

    #[test]
    fn route_and_choice_keyboard_forms_build_exact_values() {
        let (id, doc) = fixture();
        let (mut view, mut terminal) = open_view(&doc, &id);
        go_to(&mut view, "permission_judge");
        paint(&mut view, &mut terminal);
        press(&mut view, KeyCode::Right);
        paint(&mut view, &mut terminal);
        let review = press(&mut view, KeyCode::Enter).unwrap();
        assert_eq!(review.command(&id, "i").unwrap()["value"], "count");
        view.update(Err("stale".into()));
        let (mut view, mut terminal) = view_again(&doc, &id, Some(Focus::Override));
        paint(&mut view, &mut terminal);
        press(&mut view, KeyCode::Char('e'));
        for c in "Codex gpt 5.1".chars() {
            view.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::NONE));
        }
        view.key(KeyEvent::new(KeyCode::Char('Q'), KeyModifiers::SHIFT));
        paint(&mut view, &mut terminal);
        let request = press(&mut view, KeyCode::Enter).unwrap();
        assert_eq!(
            request.command(&id, "i").unwrap()["value"],
            json!({"backend": "Codex", "model": "gpt 5.1Q"})
        );
        let spec = spec_of("subagent_route_override").unwrap();
        assert_eq!(parse_draft(spec, "FOLLOW").unwrap(), json!({}));
        assert_eq!(parse_draft(spec, "").unwrap(), Value::Null);
        assert!(parse_draft(spec, "codex").is_err());
        let task = spec_of("small_task_route").unwrap();
        assert!(parse_draft(task, "codex gpt").is_err());
        assert_eq!(
            parse_draft(task, "codex gpt worker low").unwrap(),
            json!({"backend":"codex","model":"gpt","cognitive":"worker","thinking_level":"low"})
        );
        assert!(parse_draft(spec_of("tool_iterations").unwrap(), "-1").is_err());
    }
    fn view_again(
        doc: &Value,
        id: &Identity,
        focus: Option<Focus>,
    ) -> (View, Terminal<TestBackend>) {
        let mut view = View::default();
        view.open(focus);
        view.update(Ok(Update::List(Snapshot::parse(doc, id, "i").unwrap())));
        (view, Terminal::new(TestBackend::new(110, 34)).unwrap())
    }

    #[test]
    fn registered_command_focus_opens_inert_on_the_named_field() {
        let (id, doc) = fixture();
        for (focus, key) in [
            (Focus::Override, "subagent_route_override"),
            (Focus::Global, "subagent_route"),
        ] {
            let (mut view, mut terminal) = view_again(&doc, &id, Some(focus));
            let screen = paint(&mut view, &mut terminal);
            assert!(
                screen.contains(&format!("> {}", spec_of(key).unwrap().label)),
                "{screen}"
            );
            assert!(view.draft.is_none() && view.review.is_none());
        }
    }

    #[test]
    fn locked_fields_show_their_source_reason_and_cannot_be_drafted() {
        let (id, mut doc) = fixture();
        doc["fields"]["tool_iterations"]["editable"] = json!(false);
        doc["fields"]["tool_iterations"]["overridden"] = json!(true);
        doc["fields"]["tool_iterations"]["reason"] =
            json!("Environment LM_TOOL_ITERS owns this preference");
        let (mut view, mut terminal) = open_view(&doc, &id);
        let screen = paint(&mut view, &mut terminal);
        assert!(
            screen.contains("[locked]") && screen.contains("(override)"),
            "{screen}"
        );
        press(&mut view, KeyCode::Char('e'));
        assert!(view.draft.is_none());
        assert!(paint(&mut view, &mut terminal).contains("LM_TOOL_ITERS"));
        assert!(press(&mut view, KeyCode::Enter).is_none());
    }

    #[test]
    fn list_removal_chooses_an_existing_entry_and_refuses_duplicates() {
        let (id, mut doc) = fixture();
        doc["fields"]["tool_deny"]["saved"] = json!(["a:network", "b:network"]);
        doc["fields"]["tool_deny"]["effective"] = json!(["a:network", "b:network"]);
        let snapshot = Snapshot::parse(&doc, &id, "i").unwrap();
        let spec = spec_of("tool_deny").unwrap();
        let current = snapshot.fields["tool_deny"].effective.clone();
        assert!(proposed(spec, &current, Op::Add, &json!("a:network")).is_err());
        assert!(proposed(spec, &current, Op::Remove, &json!("c:network")).is_err());
        assert!(proposed(spec, &current, Op::Set, &json!(["x"])).is_err());
        assert_eq!(
            proposed(spec, &current, Op::Remove, &json!("a:network")).unwrap(),
            json!(["b:network"])
        );
        let (mut view, mut terminal) = open_view(&doc, &id);
        go_to(&mut view, "tool_deny");
        paint(&mut view, &mut terminal);
        press(&mut view, KeyCode::Char('x'));
        press(&mut view, KeyCode::Right);
        paint(&mut view, &mut terminal);
        let command = press(&mut view, KeyCode::Enter)
            .unwrap()
            .command(&id, "i")
            .unwrap();
        assert_eq!(
            (command["op"].as_str(), command["value"].as_str()),
            (Some("remove"), Some("b:network"))
        );
    }
}
