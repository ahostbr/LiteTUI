//! Reviewed, explicitly negotiated MCP transport ownership and policy publication.
use crate::{
    runtime_controls::{Facts, Policy},
    theme::Theme,
    work_lifecycle::{hash, keys},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{
    collections::{BTreeMap, BTreeSet},
    io,
};

pub const OPERATIONS: [&str; 4] = [
    "gui.mcp.native_lifecycle_snapshot",
    "gui.mcp.native_lifecycle_review",
    "gui.mcp.native_lifecycle_apply",
    "gui.mcp.native_lifecycle_readback",
];
const GLOBAL: [&str; 3] = ["enable", "disable", "reload"];
const SERVER: [&str; 3] = ["connect", "disconnect", "reconnect"];
const ID: [&str; 7] = [
    "version",
    "kind",
    "session_id",
    "agent_id",
    "backend",
    "model",
    "work_instance",
];
fn bad() -> io::Error {
    io::Error::other("MCP lifecycle differs from the reviewed owner or policy")
}
pub(crate) fn negotiate(ops: &[String], runtime: bool) -> io::Result<bool> {
    let counts = OPERATIONS.map(|op| ops.iter().filter(|s| *s == op).count());
    match counts {
        [0, 0, 0, 0] => Ok(false),
        [1, 1, 1, 1] if runtime => Ok(true),
        _ => Err(bad()),
    }
}
pub(crate) fn verify_capability(v: &Value, enabled: bool) -> io::Result<()> {
    if if enabled {
        v.get("mcp_lifecycle_version") != Some(&json!(1))
    } else {
        v.get("mcp_lifecycle_version").is_some()
    } {
        Err(bad())
    } else {
        Ok(())
    }
}
fn text(v: &Value, max: usize) -> io::Result<String> {
    v.as_str()
        .filter(|s| !s.is_empty() && s.len() <= max && !s.chars().any(crate::display_control))
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn boolean(v: &Value) -> io::Result<bool> {
    v.as_bool().ok_or_else(bad)
}
fn count(v: &Value) -> io::Result<u64> {
    v.as_u64().filter(|n| *n <= 4096).ok_or_else(bad)
}
fn name(v: &Value) -> io::Result<String> {
    let s = text(v, 96)?;
    if !s
        .bytes()
        .all(|c| c.is_ascii_alphanumeric() || b"_.-".contains(&c))
    {
        return Err(bad());
    }
    Ok(s)
}
fn binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    crate::work_lifecycle::bounded(v, 1_048_576)?;
    if v["kind"] != kind
        || v["work_instance"] != instance
        || !id
            .request()
            .as_object()
            .ok_or_else(bad)?
            .iter()
            .all(|(k, x)| v[k] == *x)
    {
        return Err(bad());
    }
    Ok(())
}
fn choices(v: &Value, names: &[&str]) -> io::Result<BTreeMap<String, Option<String>>> {
    keys(v, names)?;
    let mut out = BTreeMap::new();
    for action in names {
        let c = &v[*action];
        keys(c, &["eligible", "reason"])?;
        let reason = if boolean(&c["eligible"])? {
            if !c["reason"].is_null() {
                return Err(bad());
            }
            None
        } else {
            Some(text(&c["reason"], 512)?)
        };
        out.insert((*action).into(), reason);
    }
    Ok(out)
}
pub(crate) fn verify_policy_transition(before: &Policy, after: &Policy) -> io::Result<()> {
    if !before.binding.mcp_lifecycle
        || before.binding != after.binding
        || before.generation.checked_add(1) != Some(after.generation)
        || after.generation > 9_007_199_254_740_991
    {
        return Err(bad());
    }
    let mut left = before.facts.wire.clone();
    let mut right = after.facts.wire.clone();
    left.as_object_mut().ok_or_else(bad)?.remove("mcp_enabled");
    right.as_object_mut().ok_or_else(bad)?.remove("mcp_enabled");
    if left != right {
        return Err(bad());
    }
    Ok(())
}
fn policy(v: &Value, current: &Policy) -> io::Result<Policy> {
    keys(v, &["generation", "facts"])?;
    Ok(Policy {
        binding: current.binding.clone(),
        generation: v["generation"]
            .as_u64()
            .filter(|n| *n <= 9_007_199_254_740_991)
            .ok_or_else(bad)?,
        facts: Facts::parse_bound(&v["facts"], &current.binding)?,
    })
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct Row {
    name: String,
    transport: String,
    target: String,
    config: String,
    loaded: bool,
    disabled: bool,
    allowed: bool,
    state: String,
    tools: u64,
    active: Option<String>,
    args: u64,
    env: u64,
    headers: u64,
    actions: BTreeMap<String, Option<String>>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    revision: String,
    policy: Policy,
    enabled: bool,
    saved: bool,
    overridden: bool,
    inventory: String,
    config: String,
    loaded_config: String,
    dispatch: u64,
    dispatch_hash: String,
    actions: BTreeMap<String, Option<String>>,
    rows: Vec<Row>,
}
impl Snapshot {
    pub(crate) fn policy(&self) -> &Policy {
        &self.policy
    }
    fn parse(v: &Value, id: &Identity, instance: &str, p: &Policy) -> io::Result<Self> {
        binding(v, id, instance, "mcp_lifecycle")?;
        keys(
            v,
            &[
                ID.as_slice(),
                &[
                    "revision",
                    "enabled",
                    "saved_enabled",
                    "overridden",
                    "provider",
                    "tools_enabled",
                    "profile",
                    "approval_route",
                    "model_inventory",
                    "config_revision",
                    "loaded_config_revision",
                    "dispatch",
                    "actions",
                    "rows",
                ],
            ]
            .concat(),
        )?;
        if !p.binding.mcp_lifecycle
            || p.binding.work_instance != instance
            || p.binding.identity.session != id.session
            || p.binding.identity.agent != id.agent
            || p.binding.identity.backend != id.backend
            || p.binding.identity.model != id.model
        {
            return Err(bad());
        }
        let provider = p.facts.wire["provider"].as_str().ok_or_else(bad)?;
        let inventory = match provider {
            "host" => "host_dynamic",
            "claude" => "claude_fixed",
            "codex" => "codex_fixed",
            _ => return Err(bad()),
        };
        let enabled = boolean(&v["enabled"])?;
        if enabled != p.facts.mcp_enabled
            || v["provider"] != provider
            || v["model_inventory"] != inventory
            || v["tools_enabled"] != p.facts.tools_enabled
            || v["profile"] != p.facts.effective_profile.as_str()
            || v["approval_route"] != "host"
        {
            return Err(bad());
        }
        keys(&v["dispatch"], &["count", "sha256"])?;
        let dispatch = count(&v["dispatch"]["count"])?;
        if !enabled && dispatch != 0 {
            return Err(bad());
        }
        let actions = choices(&v["actions"], &GLOBAL)?;
        if provider == "codex" && actions.values().any(Option::is_none)
            || enabled && actions["enable"].is_none()
        {
            return Err(bad());
        }
        let mut rows = Vec::new();
        let mut names = BTreeSet::new();
        for row in v["rows"]
            .as_array()
            .filter(|a| a.len() <= 64)
            .ok_or_else(bad)?
        {
            keys(
                row,
                &[
                    "name",
                    "transport",
                    "target",
                    "config_sha256",
                    "loaded",
                    "disabled",
                    "allowed",
                    "state",
                    "tools",
                    "active_config_sha256",
                    "args",
                    "env",
                    "headers",
                    "actions",
                ],
            )?;
            let name = name(&row["name"])?;
            if !names.insert(name.clone()) {
                return Err(bad());
            }
            let transport = text(&row["transport"], 5)?;
            if !["stdio", "http"].contains(&transport.as_str()) {
                return Err(bad());
            }
            let target = text(&row["target"], 480)?;
            if target.chars().count() > 120
                || target.contains('@')
                || target.contains('?')
                || target.contains('#')
            {
                return Err(bad());
            }
            if transport == "stdio" && (target.contains('/') || target.contains('\\')) {
                return Err(bad());
            }
            let state = text(&row["state"], 12)?;
            if !["connected", "disconnected", "failed"].contains(&state.as_str()) {
                return Err(bad());
            }
            let active = if row["active_config_sha256"].is_null() {
                None
            } else {
                Some(hash(row, "active_config_sha256")?)
            };
            let tools = count(&row["tools"])?;
            if state == "connected" && active.is_none()
                || state == "disconnected" && active.is_some()
                || active.is_none() && tools != 0
            {
                return Err(bad());
            }
            let loaded = boolean(&row["loaded"])?;
            let disabled = boolean(&row["disabled"])?;
            let allowed = boolean(&row["allowed"])?;
            let row_actions = choices(&row["actions"], &SERVER)?;
            for (a, reason) in &row_actions {
                if reason.is_none()
                    && (provider == "codex"
                        || a != "disconnect" && (!enabled || !loaded || disabled || !allowed)
                        || a == "connect" && active.is_some()
                        || a == "disconnect" && active.is_none())
                {
                    return Err(bad());
                }
            }
            rows.push(Row {
                name,
                transport,
                target,
                config: hash(row, "config_sha256")?,
                loaded,
                disabled,
                allowed,
                state,
                tools,
                active,
                args: count(&row["args"])?,
                env: count(&row["env"])?,
                headers: count(&row["headers"])?,
                actions: row_actions,
            });
        }
        Ok(Self {
            revision: hash(v, "revision")?,
            policy: p.clone(),
            enabled,
            saved: boolean(&v["saved_enabled"])?,
            overridden: boolean(&v["overridden"])?,
            inventory: inventory.into(),
            config: hash(v, "config_revision")?,
            loaded_config: hash(v, "loaded_config_revision")?,
            dispatch,
            dispatch_hash: hash(&v["dispatch"], "sha256")?,
            actions,
            rows,
        })
    }
    fn choice(&self, action: &str, target: Option<&str>) -> io::Result<&Option<String>> {
        if GLOBAL.contains(&action) && target.is_none() {
            return self.actions.get(action).ok_or_else(bad);
        }
        if !SERVER.contains(&action) {
            return Err(bad());
        }
        self.rows
            .iter()
            .find(|r| Some(r.name.as_str()) == target)
            .and_then(|r| r.actions.get(action))
            .ok_or_else(bad)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Selection {
    before: Snapshot,
    action: String,
    target: Option<String>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    selection: Selection,
    token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Applied {
    id: String,
    after: String,
    status: String,
    policy: Policy,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(Selection),
    Apply(Review),
    Readback(Review, Box<Applied>),
}
// Transient reply value, consumed immediately; boxing would only add allocation.
#[allow(clippy::large_enum_variant)]
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Review),
    Settled(Snapshot, Policy, String),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
impl Request {
    pub(crate) fn verify_policy(&self, p: &Policy) -> io::Result<()> {
        if !p.binding.mcp_lifecycle {
            return Err(bad());
        }
        let before = match self {
            Self::List => return Ok(()),
            Self::Review(s) => &s.before.policy,
            Self::Apply(r) | Self::Readback(r, _) => &r.selection.before.policy,
        };
        if p != before {
            return Err(bad());
        }
        Ok(())
    }
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut v = id.request();
        v["work_instance"] = json!(instance);
        let op = match self {
            Self::List => 0,
            Self::Review(s) => {
                if s.before.choice(&s.action, s.target.as_deref())?.is_some() {
                    return Err(bad());
                }
                v["revision"] = json!(s.before.revision);
                v["action"] = json!(s.action);
                v["target"] = json!(s.target);
                1
            }
            Self::Apply(r) => {
                v["token"] = json!(r.token);
                2
            }
            Self::Readback(r, a) => {
                v["token"] = json!(r.token);
                v["apply_id"] = json!(a.id);
                3
            }
        };
        v["type"] = json!(OPERATIONS[op]);
        Ok(v)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        command_id: &str,
        current: &Policy,
    ) -> io::Result<Reply> {
        self.verify_policy(current)?;
        let (kind, s, token, applied) = match &self {
            Self::List => {
                return Ok(Reply::Update(Update::List(Snapshot::parse(
                    v, id, instance, current,
                )?)));
            }
            Self::Review(s) => ("mcp_lifecycle_review", s, None, None),
            Self::Apply(r) => (
                "mcp_lifecycle_applied",
                &r.selection,
                Some(&r.token),
                Some(command_id),
            ),
            Self::Readback(r, a) => (
                "mcp_lifecycle_settled",
                &r.selection,
                Some(&r.token),
                Some(a.id.as_str()),
            ),
        };
        binding(v, id, instance, kind)?;
        let mut fields = [
            ID.as_slice(),
            &[
                "token",
                "action",
                "target",
                "before",
                "after",
                "apply_id",
                "status",
                "policy_before",
                "policy_after",
            ],
        ]
        .concat();
        if matches!(self, Self::Readback(..)) {
            fields.push("state")
        }
        keys(v, &fields)?;
        let actual_token = hash(v, "token")?;
        if v["action"] != s.action
            || v["target"] != json!(s.target)
            || v["before"] != s.before.revision
            || token.is_some_and(|t| *t != actual_token)
        {
            return Err(bad());
        }
        let before = policy(&v["policy_before"], current)?;
        if *current != before || s.before.policy != before {
            return Err(bad());
        }
        let after = if let Some(a) = applied {
            if v["apply_id"] != a
                || !["completed", "failed"].contains(&v["status"].as_str().ok_or_else(bad)?)
            {
                return Err(bad());
            }
            hash(v, "after")?;
            let p = policy(&v["policy_after"], current)?;
            verify_policy_transition(&before, &p)?;
            if !["enable", "disable"].contains(&s.action.as_str())
                && p.facts.mcp_enabled != before.facts.mcp_enabled
            {
                return Err(bad());
            }
            Some(p)
        } else {
            if !v["apply_id"].is_null()
                || !v["after"].is_null()
                || !v["policy_after"].is_null()
                || v["status"] != "reviewed"
            {
                return Err(bad());
            }
            None
        };
        match self {
            Self::Review(selection) => Ok(Reply::Update(Update::Review(Review {
                selection,
                token: actual_token,
            }))),
            Self::Apply(r) => Ok(Reply::Next(Self::Readback(
                r,
                Box::new(Applied {
                    id: command_id.into(),
                    after: hash(v, "after")?,
                    status: text(&v["status"], 16)?,
                    policy: after.ok_or_else(bad)?,
                }),
            ))),
            Self::Readback(r, a) => {
                let p = after.ok_or_else(bad)?;
                if a.after != hash(v, "after")? || a.status != v["status"] || a.policy != p {
                    return Err(bad());
                }
                let actual = Snapshot::parse(&v["state"], id, instance, &p)?;
                if actual.revision != a.after {
                    return Err(bad());
                }
                let s = r.selection;
                if actual.config != s.before.config {
                    return Err(bad());
                }
                if s.action != "disable" {
                    for row in s
                        .before
                        .rows
                        .iter()
                        .filter(|row| Some(&row.name) != s.target.as_ref())
                    {
                        let other = actual.rows.iter().find(|other| other.name == row.name);
                        if row.active.is_some()
                            && other.is_none_or(|other| {
                                other.active != row.active
                                    || other.tools != row.tools
                                    || other.state != row.state
                            })
                        {
                            return Err(bad());
                        }
                        if row.active.is_none() && other.is_some_and(|other| other.active.is_some())
                        {
                            return Err(bad());
                        }
                    }
                    if actual.rows.iter().any(|row| {
                        row.active.is_some()
                            && Some(&row.name) != s.target.as_ref()
                            && !s
                                .before
                                .rows
                                .iter()
                                .any(|old| old.name == row.name && old.active.is_some())
                    }) {
                        return Err(bad());
                    }
                }
                if a.status == "completed" {
                    match s.action.as_str() {
                        "enable" if !actual.enabled || !actual.saved || actual.overridden => {
                            return Err(bad());
                        }
                        "disable"
                            if actual.enabled
                                || actual.saved
                                || actual.overridden
                                || actual.rows.iter().any(|r| r.active.is_some()) =>
                        {
                            return Err(bad());
                        }
                        "connect" | "reconnect" => {
                            let row = actual
                                .rows
                                .iter()
                                .find(|r| Some(&r.name) == s.target.as_ref())
                                .ok_or_else(bad)?;
                            if row.state != "connected" || row.active.as_ref() != Some(&row.config)
                            {
                                return Err(bad());
                            }
                        }
                        "disconnect" => {
                            if actual
                                .rows
                                .iter()
                                .find(|r| Some(&r.name) == s.target.as_ref())
                                .is_some_and(|r| r.active.is_some())
                            {
                                return Err(bad());
                            }
                        }
                        _ => {}
                    }
                }
                Ok(Reply::Update(Update::Settled(actual, before, a.status)))
            }
            _ => Err(bad()),
        }
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    visible: bool,
    data: Option<Snapshot>,
    review: Option<Review>,
    selected: usize,
    note: String,
    scroll: usize,
    height: usize,
    lines: usize,
    seen: Vec<bool>,
    geometry: Option<(u16, u16)>,
}
impl View {
    pub fn open(&mut self) {
        *self = Self {
            open: true,
            waiting: true,
            ..Self::default()
        }
    }
    pub fn hide(&mut self) {
        self.visible = false
    }
    pub fn can_switch(&self) -> bool {
        !self.waiting && self.review.is_none()
    }
    pub fn close(&mut self) {
        *self = Self::default()
    }
    pub fn update(&mut self, result: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        self.scroll = 0;
        self.note.clear();
        self.seen.clear();
        self.geometry = None;
        match result {
            Ok(Update::List(s)) => {
                self.data = Some(s);
                self.review = None
            }
            Ok(Update::Review(r)) => {
                self.data = Some(r.selection.before.clone());
                self.review = Some(r);
            }
            Ok(Update::Settled(s, _, status)) => {
                self.data = Some(s);
                self.review = None;
                self.note = if status == "completed" {
                    "Operation verified by independent readback."
                } else {
                    "Operation failed. Inspect the current state and review a retry."
                }
                .into()
            }
            Err(e) => {
                self.data = None;
                self.review = None;
                self.note = crate::live::terminal_text(&e)
            }
        }
        self.selected = self.selected.min(
            self.data
                .as_ref()
                .map_or(0, |s| s.rows.len().saturating_sub(1)),
        );
    }
    pub fn key(&mut self, k: KeyEvent) -> Option<Request> {
        if k.kind != KeyEventKind::Press || !k.modifiers.is_empty() || self.waiting {
            return None;
        }
        if k.code == KeyCode::Esc {
            if self.review.take().is_some() {
                self.hide()
            } else {
                self.close()
            }
            return None;
        }
        if matches!(k.code, KeyCode::PageUp | KeyCode::PageDown) {
            self.scroll = if k.code == KeyCode::PageUp {
                self.scroll.saturating_sub(self.height)
            } else {
                self.scroll
                    .saturating_add(self.height)
                    .min(self.lines.saturating_sub(self.height))
            };
            self.hide();
            return None;
        }
        if self.review.is_none() && matches!(k.code, KeyCode::Up | KeyCode::Down) {
            self.selected = if k.code == KeyCode::Up {
                self.selected.saturating_sub(1)
            } else {
                self.selected.saturating_add(1).min(
                    self.data
                        .as_ref()
                        .map_or(0, |s| s.rows.len().saturating_sub(1)),
                )
            };
            self.hide();
            return None;
        }
        let req = match k.code {
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Char(c)
                if self.visible
                    && self.review.is_none()
                    && ['e', 'd', 'r', 'c', 'x', 'n'].contains(&c) =>
            {
                let action = match c {
                    'e' => "enable",
                    'd' => "disable",
                    'r' => "reload",
                    'c' => "connect",
                    'x' => "disconnect",
                    _ => "reconnect",
                };
                let before = self.data.clone()?;
                let target = if GLOBAL.contains(&action) {
                    None
                } else {
                    Some(before.rows.get(self.selected)?.name.clone())
                };
                if let Some(reason) = before.choice(action, target.as_deref()).ok()? {
                    self.note = reason.clone();
                    return None;
                }
                Request::Review(Selection {
                    before,
                    action: action.into(),
                    target,
                })
            }
            _ => return None,
        };
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(req)
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["MCP · Runtime | Tab: declarations".into()];
        if let Some(s) = &self.data {
            lines.push(format!(
                "MCP {} · saved {} · invocation/environment override {}",
                s.enabled, s.saved, s.overridden
            ));
            lines.push(format!(
                "Tools {} · Authority {} · approvals: host",
                s.policy.facts.tools_enabled,
                s.policy.facts.effective_profile.as_str()
            ));
            lines.push(format!(
                "Host dispatch: {} tools · Model inventory: {}",
                s.dispatch, s.inventory
            ));
            lines.push(match s.inventory.as_str(){"host_dynamic"=>"Host model requests use eligible connected tools on the next request.","claude_fixed"=>"Claude keeps its existing fixed model inventory; host connections do not add SDK tools.",_=>"Codex owns its fixed inventory; runtime mutations are unavailable."}.into());
            if let Some(r) = &self.review {
                lines.push(format!(
                    "Review {} · {}",
                    r.selection.action,
                    r.selection.target.as_deref().unwrap_or("conversation MCP")
                ));
                if let Some(row) = s
                    .rows
                    .iter()
                    .find(|row| Some(&row.name) == r.selection.target.as_ref())
                {
                    lines.push(format!(
                        "{}: {} · {} · {} tools",
                        row.name, row.transport, row.target, row.tools
                    ));
                    lines.push(format!(
                        "Declaration {} · active {}",
                        row.config,
                        row.active.as_deref().unwrap_or("none")
                    ));
                    lines.push(format!(
                        "{} arguments · {} environment entries · {} headers (values hidden)",
                        row.args, row.env, row.headers
                    ));
                }
                lines.push(match r.selection.action.as_str(){
     "enable"=>"Saves MCP on for this conversation and reloads declarations. Opens no server.",
     "disable"=>"Joins all owned connections before saving MCP off. Failed cleanup remains owned.",
     "reload"=>"Reloads declarations. Existing connections remain owned with their active configuration.",
     "connect"=>"Starts this configured process or network connection and discovers its tools.",
     "disconnect"=>"Stops and joins this exact owned connection; failure retains it for retry.",
     _=>"Stops this connection, then starts the reviewed configuration and discovers tools."
    }.into());
                lines.push("Tools and Authority stay unchanged. No prompt will be sent.".into());
                lines.push("Disable MCP before changing conversation, backend or model.".into());
                if r.selection.target.is_none() {
                    for row in &s.rows {
                        lines.push(format!(
                            "{} · {} · {} · active {}",
                            row.name,
                            row.state,
                            row.target,
                            row.active.as_deref().unwrap_or("none")
                        ));
                    }
                }
                lines.push("Read every review page. PgUp/PgDn · Enter confirm · Esc cancel".into());
            } else {
                for (i, row) in s.rows.iter().enumerate() {
                    lines.push(format!(
                        "{} {} · {} · {} · {} tools",
                        if i == self.selected { ">" } else { " " },
                        row.name,
                        row.state,
                        row.target,
                        row.tools
                    ));
                }
                if s.rows.is_empty() {
                    lines.push("No configured servers. Tab opens declarations.".into())
                }
                if let Some(row) = s.rows.get(self.selected) {
                    lines.push(format!(
                        "Selected {} · loaded {} · disabled {} · allowed {}",
                        row.name, row.loaded, row.disabled, row.allowed
                    ));
                }
                lines.push(
                    "E enable · D disable · R reload · C connect · X disconnect · N reconnect"
                        .into(),
                );
                lines.push(
                    "↑↓ server · F5 refresh · Tab declarations · PgUp/PgDn scroll · Esc back"
                        .into(),
                );
            }
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone())
        }
        if self.waiting {
            lines.push("Waiting for the source; owned work must settle…".into())
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
        let geometry = (area.width, area.height);
        if self.geometry != Some(geometry) || self.seen.len() != self.lines {
            self.seen = vec![false; self.lines];
            self.geometry = Some(geometry);
        }
        let readable = area.width >= 40 && self.height >= 8;
        if readable {
            for index in self.scroll..self.scroll.saturating_add(self.height).min(self.lines) {
                self.seen[index] = true;
            }
        }
        self.visible = readable && (self.review.is_none() || self.seen.iter().all(|s| *s));
        let shown = if !readable {
            "Resize to see the full review. Esc back.".into()
        } else {
            wrapped
                .iter()
                .skip(self.scroll)
                .take(self.height)
                .cloned()
                .collect::<Vec<_>>()
                .join("\n")
        };
        f.render_widget(Clear, area);
        f.render_widget(
            Paragraph::new(shown)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" MCP runtime ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{runtime_controls::Binding, work_lifecycle::Provider};
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};
    fn fixture() -> (Identity, Policy, Value) {
        let id = Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let facts = json!({"tools_enabled":true,"settings_profile":"strict","chosen_profile":"strict","seat_profile":"strict","effective_profile":"strict","requested_cli_profile":"strict","plan_mode":false,"mcp_enabled":false,"approval_route":"host","cwd":std::env::current_dir().unwrap().to_string_lossy(),"provider":"host","inventory":"host_request"});
        let p = Policy {
            binding: Binding {
                mcp_lifecycle: true,
                tool_selection: false,
                identity: crate::work_lifecycle::Identity {
                    session: id.session.clone(),
                    agent: id.agent.clone(),
                    backend: id.backend.clone(),
                    model: id.model.clone(),
                },
                work_instance: "a".repeat(64),
                provider: Provider::Host,
            },
            generation: 0,
            facts: Facts::parse(&facts, Provider::Host).unwrap(),
        };
        let mut v = id.request();
        v.as_object_mut().unwrap().extend(json!({"kind":"mcp_lifecycle","work_instance":"a".repeat(64),"revision":"b".repeat(64),
   "enabled":false,"saved_enabled":false,"overridden":true,"provider":"host","tools_enabled":true,"profile":"strict","approval_route":"host","model_inventory":"host_dynamic",
   "config_revision":"c".repeat(64),"loaded_config_revision":"d".repeat(64),"dispatch":{"count":0,"sha256":"e".repeat(64)},
   "actions":{"enable":{"eligible":true,"reason":null},"disable":{"eligible":false,"reason":"Disabled"},"reload":{"eligible":true,"reason":null}},
   "rows":[{"name":"owned","transport":"stdio","target":"python.exe","config_sha256":"f".repeat(64),"loaded":true,"disabled":false,"allowed":true,"state":"disconnected","tools":0,"active_config_sha256":null,"args":2,"env":1,"headers":0,
    "actions":{"connect":{"eligible":false,"reason":"Enable first"},"disconnect":{"eligible":false,"reason":"Disconnected"},"reconnect":{"eligible":false,"reason":"Enable first"}}}]
  }).as_object().unwrap().clone());
        (id, p, v)
    }
    fn review() -> (Identity, Policy, Value, Review) {
        let (id, p, v) = fixture();
        let before = Snapshot::parse(&v, &id, &p.binding.work_instance, &p).unwrap();
        let r = Review {
            selection: Selection {
                before,
                action: "enable".into(),
                target: None,
            },
            token: "1".repeat(64),
        };
        (id, p, v, r)
    }
    fn applied(id: &Identity, p: &Policy) -> Value {
        let mut after = p.facts.wire.clone();
        after["mcp_enabled"] = json!(true);
        let mut v = id.request();
        v.as_object_mut().unwrap().extend(json!({"kind":"mcp_lifecycle_applied","work_instance":p.binding.work_instance,
   "token":"1".repeat(64),"action":"enable","target":null,"before":"b".repeat(64),"after":"2".repeat(64),"apply_id":"apply","status":"completed",
   "policy_before":{"generation":0,"facts":p.facts.wire},"policy_after":{"generation":1,"facts":after}}).as_object().unwrap().clone());
        v
    }
    #[test]
    fn capability_is_complete_and_startup_stays_off() {
        let ops = OPERATIONS.iter().map(|s| s.to_string()).collect::<Vec<_>>();
        assert!(negotiate(&ops, true).unwrap());
        assert!(negotiate(&ops, false).is_err());
        assert!(negotiate(&ops[..3], true).is_err());
        assert!(!negotiate(&[], true).unwrap());
        let (_, p, _) = fixture();
        let mut f = p.facts.wire.clone();
        f["mcp_enabled"] = json!(true);
        assert!(Facts::parse(&f, Provider::Host).is_err());
        assert!(Facts::parse_mcp(&f, Provider::Host, true).is_ok());
        assert!(Facts::parse_mcp(&f, Provider::Host, false).is_err());
        assert!(verify_capability(&json!({"mcp_lifecycle_version":true}), true).is_err());
        assert!(verify_capability(&json!({"mcp_lifecycle_version":1}), false).is_err());
    }
    #[test]
    fn snapshot_rejects_secret_targets_oversized_inventory_and_false_authority() {
        let (id, p, v) = fixture();
        for (pointer, value) in [
            ("/rows/0/target", json!("C:\\secret\\python.exe")),
            ("/rows/0/target", json!("https://user:secret@host")),
            ("/rows/0/tools", json!(4097)),
            ("/rows/0/name", json!("../bad")),
            ("/dispatch/count", json!(1)),
            (
                "/rows/0/actions/connect",
                json!({"eligible":true,"reason":null}),
            ),
            ("/enabled", json!(true)),
            ("/approval_route", json!("own")),
            ("/model_inventory", json!("claude_fixed")),
            ("/tools_enabled", json!(false)),
            ("/rows/0/state", json!("connected")),
            ("/rows/0/target", json!("bad\u{1b}escape")),
        ] {
            let mut wrong = v.clone();
            *wrong.pointer_mut(pointer).unwrap() = value;
            assert!(
                Snapshot::parse(&wrong, &id, &p.binding.work_instance, &p).is_err(),
                "{pointer}"
            );
        }
        let mut wrong = v.clone();
        wrong["rows"]
            .as_array_mut()
            .unwrap()
            .push(v["rows"][0].clone());
        assert!(Snapshot::parse(&wrong, &id, &p.binding.work_instance, &p).is_err());
    }
    #[test]
    fn apply_requires_exact_policy_and_readback_before_install() {
        let (id, p, mut state, r) = review();
        let mut a = applied(&id, &p);
        let Reply::Next(next) = Request::Apply(r)
            .response(&a, &id, &p.binding.work_instance, "apply", &p)
            .unwrap()
        else {
            panic!("apply cannot install")
        };
        state["enabled"] = json!(true);
        state["saved_enabled"] = json!(true);
        state["overridden"] = json!(false);
        state["revision"] = json!("2".repeat(64));
        state["actions"]["enable"] = json!({"eligible":false,"reason":"Enabled"});
        state["actions"]["disable"] = json!({"eligible":true,"reason":null});
        a["kind"] = json!("mcp_lifecycle_settled");
        a["state"] = state;
        assert!(matches!(
            next.clone()
                .response(&a, &id, &p.binding.work_instance, "read", &p),
            Ok(Reply::Update(Update::Settled(_, _, _)))
        ));
        for (pointer, value) in [
            ("/policy_after/generation", json!(2)),
            ("/policy_after/facts/tools_enabled", json!(false)),
            ("/policy_after/facts/effective_profile", json!("autonomous")),
            ("/policy_after/facts/seat_profile", json!("autonomous")),
            ("/state/revision", json!("9".repeat(64))),
            ("/apply_id", json!("other")),
            ("/status", json!("failed")),
        ] {
            let mut wrong = a.clone();
            *wrong.pointer_mut(pointer).unwrap() = value;
            assert!(
                next.clone()
                    .response(&wrong, &id, &p.binding.work_instance, "read", &p)
                    .is_err(),
                "{pointer}"
            );
        }
        let mut stale = p.clone();
        stale.generation = 1;
        assert!(
            next.response(&a, &id, &p.binding.work_instance, "read", &stale)
                .is_err()
        );
    }
    #[test]
    fn failed_cleanup_readback_advances_policy_without_claiming_success() {
        let (id, p, state, mut r) = review();
        r.selection.action = "disable".into();
        let mut a = applied(&id, &p);
        a["action"] = json!("disable");
        a["status"] = json!("failed");
        a["policy_after"]["facts"] = p.facts.wire.clone();
        let Reply::Next(next) = Request::Apply(r)
            .response(&a, &id, &p.binding.work_instance, "apply", &p)
            .unwrap()
        else {
            panic!()
        };
        a["kind"] = json!("mcp_lifecycle_settled");
        a["state"] = state;
        a["state"]["revision"] = json!("2".repeat(64));
        let Reply::Update(Update::Settled(s, _, status)) = next
            .response(&a, &id, &p.binding.work_instance, "read", &p)
            .unwrap()
        else {
            panic!()
        };
        assert_eq!(s.policy.generation, 1);
        assert_eq!(status, "failed");
    }
    #[test]
    fn complete_visible_review_required_and_switching_does_not_apply() {
        let (_, _, _, r) = review();
        let mut v = View::default();
        v.open();
        v.update(Ok(Update::Review(r)));
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        assert!(v.key(enter).is_none());
        assert!(!v.can_switch());
        let mut small = Terminal::new(TestBackend::new(40, 8)).unwrap();
        small
            .draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(v.key(enter).is_none());
        let mut wide = Terminal::new(TestBackend::new(140, 40)).unwrap();
        wide.draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(matches!(v.key(enter), Some(Request::Apply(_))));
        assert!(!v.can_switch());
    }
    #[test]
    fn every_wrapped_review_page_must_be_seen_and_resize_invalidates_coverage() {
        let (_, _, _, mut r) = review();
        let row = r.selection.before.rows[0].clone();
        for n in 0..12 {
            let mut extra = row.clone();
            extra.name = format!("owned-{n}");
            r.selection.before.rows.push(extra);
        }
        let mut v = View::default();
        v.open();
        v.update(Ok(Update::Review(r)));
        let mut t = Terminal::new(TestBackend::new(60, 14)).unwrap();
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        t.draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(v.key(enter).is_none());
        for _ in 0..30 {
            v.key(KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE));
            t.draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
                .unwrap();
        }
        assert!(v.visible);
        let mut resized = Terminal::new(TestBackend::new(80, 14)).unwrap();
        resized
            .draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        assert!(v.key(enter).is_none());
        for _ in 0..30 {
            v.key(KeyEvent::new(KeyCode::PageUp, KeyModifiers::NONE));
            resized
                .draw(|f| v.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
                .unwrap();
        }
        assert!(matches!(v.key(enter), Some(Request::Apply(_))));
    }
    #[test]
    fn enabled_policy_is_accepted_only_as_current_never_as_capture_or_launch() {
        let (_, mut p, _) = fixture();
        let mut facts = p.facts.wire.clone();
        facts["mcp_enabled"] = json!(true);
        p.facts = Facts::parse_mcp(&facts, p.binding.provider, true).unwrap();
        p.generation = 1;
        let options = crate::tool_mode::Expected {
            enabled: true,
            profile: crate::tool_mode::Profile::Strict,
            workspace: std::env::current_dir().unwrap(),
        };
        assert!(
            options
                .validate_facts(&p.facts, crate::tool_mode::ValidationMode::Current(&p))
                .is_ok()
        );
        assert!(
            options
                .validate_facts(&p.facts, crate::tool_mode::ValidationMode::Launch)
                .is_err()
        );
        assert!(
            options
                .validate_facts(&p.facts, crate::tool_mode::ValidationMode::Target)
                .is_err()
        );
        let caps = crate::runtime_controls::Capabilities::negotiate(
            &crate::runtime_controls::OPERATIONS
                .iter()
                .chain(
                    [
                        crate::work_lifecycle::CAPTURE,
                        crate::work_lifecycle::ACTIVATE,
                        crate::work_lifecycle::SETTLED,
                    ]
                    .iter(),
                )
                .map(|s| s.to_string())
                .collect::<Vec<_>>(),
        )
        .unwrap()
        .unwrap();
        let capture = json!({"runtime_controls_version":1,"runtime_controls_limits":{"authorizations":128,"waiters":128},"runtime_policy":{"generation":0,"facts":facts}});
        assert!(caps.parse_capture(&capture, &p.binding).is_err());
        p.binding.mcp_lifecycle = false;
        assert!(
            options
                .validate_facts(&p.facts, crate::tool_mode::ValidationMode::Current(&p))
                .is_err()
        );
    }
    #[test]
    fn receipt_cannot_change_both_before_and_after_to_forge_effective_authority() {
        let (id, p, _, r) = review();
        let mut a = applied(&id, &p);
        a["policy_before"]["facts"]["effective_profile"] = json!("autonomous");
        a["policy_after"]["facts"]["effective_profile"] = json!("autonomous");
        assert!(
            Request::Apply(r.clone())
                .response(&a, &id, &p.binding.work_instance, "apply", &p)
                .is_err()
        );
        let mut drift = p.clone();
        drift.facts.effective_profile = crate::tool_mode::Profile::Autonomous;
        drift.facts.wire["effective_profile"] = json!("autonomous");
        assert!(Request::Apply(r).verify_policy(&drift).is_err());
    }
}
