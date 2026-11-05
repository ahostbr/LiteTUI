//! Redacted file declarations and single-use edits under the active Work identity.
mod form;
use crate::{theme::Theme, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use form::{Form, Spec};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::HashSet, io};

pub const LIST: &str = "gui.mcp.native_config_list";
pub const REVIEW: &str = "gui.mcp.native_config_review";
pub const ADD_REVIEW: &str = "gui.mcp.native_config_add_review";
pub const REMOVE_REVIEW: &str = "gui.mcp.native_config_remove_review";
pub const APPLY: &str = "gui.mcp.native_config_apply";
pub const READBACK: &str = "gui.mcp.native_config_readback";
pub const OPERATIONS: [&str; 6] = [LIST, REVIEW, ADD_REVIEW, REMOVE_REVIEW, APPLY, READBACK];
const WRITTEN: &str = ".mcp.json";

fn bad() -> io::Error {
    io::Error::other("MCP declaration response changed its reviewed identity or contents")
}
fn hash(v: &Value) -> io::Result<String> {
    v.as_str()
        .filter(|s| {
            s.len() == 64
                && s.bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        })
        .map(str::to_owned)
        .ok_or_else(bad)
}
fn exact(v: &Value, keys: &[&str]) -> bool {
    v.as_object()
        .is_some_and(|map| map.len() == keys.len() && keys.iter().all(|k| map.contains_key(*k)))
}
fn flag(v: &Value, key: &str) -> io::Result<bool> {
    v[key].as_bool().ok_or_else(bad)
}
fn binding(value: &Value, identity: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if value["scope"] != "shared"
        || value["kind"] != kind
        || value["work_instance"] != instance
        || !identity
            .request()
            .as_object()
            .expect("object")
            .iter()
            .all(|(k, v)| value[k] == *v)
    {
        return Err(bad());
    }
    Ok(())
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct Row {
    name: String,
    file: String,
    transport: String,
    disabled: Option<bool>,
    args: bool,
    env: bool,
    headers: bool,
    shadowed: bool,
    removable: bool,
    editable: bool,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    revision: String,
    rows: Vec<Row>,
    hidden: u64,
}

impl Snapshot {
    fn parse(v: &Value, identity: &Identity, instance: &str) -> io::Result<Self> {
        binding(v, identity, instance, "mcp_config")?;
        if !exact(
            v,
            &[
                "version",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "kind",
                "scope",
                "revision",
                "rows",
                "hidden",
            ],
        ) {
            return Err(bad());
        }
        let values = v["rows"]
            .as_array()
            .filter(|rows| rows.len() <= 64)
            .ok_or_else(bad)?;
        let mut rows = Vec::new();
        let mut seen = HashSet::new();
        let mut dot = false;
        for v in values {
            if !exact(
                v,
                &[
                    "name",
                    "file",
                    "transport",
                    "disabled",
                    "has_args",
                    "has_env",
                    "has_headers",
                    "shadowed",
                    "removable",
                    "editable",
                ],
            ) {
                return Err(bad());
            }
            let name = v["name"]
                .as_str()
                .filter(|s| {
                    !s.is_empty()
                        && s.len() <= 96
                        && s.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
                })
                .ok_or_else(bad)?;
            let file = v["file"]
                .as_str()
                .filter(|s| matches!(*s, "mcp.json" | ".mcp.json"))
                .ok_or_else(bad)?;
            if dot && file == "mcp.json" || !seen.insert((file, name)) {
                return Err(bad());
            }
            dot = file == ".mcp.json";
            let shadowed = flag(v, "shadowed")?;
            if shadowed != (dot && seen.contains(&("mcp.json", name))) {
                return Err(bad());
            }
            let transport = v["transport"]
                .as_str()
                .filter(|s| matches!(*s, "stdio" | "http" | "unknown"))
                .ok_or_else(bad)?;
            let disabled = if v["disabled"].is_null() {
                None
            } else {
                Some(flag(v, "disabled")?)
            };
            let editable = flag(v, "editable")?;
            let removable = flag(v, "removable")?;
            if removable && (!dot || shadowed)
                || editable && (!removable || disabled.is_none() || transport == "unknown")
            {
                return Err(bad());
            }
            rows.push(Row {
                name: name.into(),
                file: file.into(),
                transport: transport.into(),
                disabled,
                args: flag(v, "has_args")?,
                env: flag(v, "has_env")?,
                headers: flag(v, "has_headers")?,
                shadowed,
                removable,
                editable,
            });
        }
        Ok(Self {
            revision: hash(&v["revision"])?,
            rows,
            hidden: v["hidden"]
                .as_u64()
                .filter(|x| *x <= 524288)
                .ok_or_else(bad)?,
        })
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Change {
    Toggle,
    Add,
    Remove,
}
/// The source's redacted view of one declaration: a target and names, never values.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Entry {
    transport: String,
    target: String,
    args: u64,
    env: Vec<String>,
    headers: Vec<String>,
    omitted: u64,
}
fn names(v: &Value) -> io::Result<Vec<String>> {
    v.as_array()
        .filter(|list| list.len() <= 32)
        .ok_or_else(bad)?
        .iter()
        .map(|n| {
            n.as_str()
                .filter(|s| {
                    !s.is_empty()
                        && s.len() <= 96
                        && s.bytes()
                            .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b))
                })
                .map(str::to_owned)
                .ok_or_else(bad)
        })
        .collect()
}
impl Entry {
    fn parse(v: &Value) -> io::Result<Self> {
        if !exact(
            v,
            &["transport", "target", "args", "env", "headers", "omitted"],
        ) {
            return Err(bad());
        }
        Ok(Self {
            transport: v["transport"]
                .as_str()
                .filter(|s| matches!(*s, "stdio" | "http" | "unknown"))
                .ok_or_else(bad)?
                .into(),
            target: v["target"]
                .as_str()
                .filter(|s| s.len() <= 200 && !s.chars().any(char::is_control))
                .ok_or_else(bad)?
                .into(),
            args: v["args"].as_u64().filter(|n| *n <= 4096).ok_or_else(bad)?,
            env: names(&v["env"])?,
            headers: names(&v["headers"])?,
            omitted: v["omitted"]
                .as_u64()
                .filter(|n| *n <= 4096)
                .ok_or_else(bad)?,
        })
    }
    fn sorted(list: &[String]) -> Vec<&str> {
        let mut list: Vec<&str> = list.iter().map(String::as_str).collect();
        list.sort_unstable();
        list
    }
    /// The entry the user typed must come back as exactly that shape.
    fn matches_spec(&self, spec: &Spec) -> bool {
        self.transport == spec.transport()
            && self.args == spec.arg_count() as u64
            && Self::sorted(&self.env) == Self::sorted(&spec.env_names())
            && Self::sorted(&self.headers) == Self::sorted(&spec.header_names())
            && self.omitted == 0
    }
    /// A removed entry is only ever a subset of what its row advertised.
    fn matches_row(&self, row: &Row) -> bool {
        let listed = |names: &[String], flagged: bool| {
            if self.omitted > 0 {
                names.is_empty() || flagged
            } else {
                names.is_empty() != flagged
            }
        };
        self.transport == row.transport
            && (self.args == 0 || row.args)
            && listed(&self.env, row.env)
            && listed(&self.headers, row.headers)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    index: usize,
    token: String,
    after: String,
    change: Change,
    name: String,
    entry: Option<Entry>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(Snapshot, usize),
    AddReview(Snapshot, Box<Spec>),
    RemoveReview(Snapshot, usize),
    Apply(Snapshot, Review),
    Readback(Snapshot, Review, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Snapshot, Review),
    Saved(Snapshot, usize, Change),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
const COMMON: [&str; 8] = [
    "version",
    "session_id",
    "agent_id",
    "backend",
    "model",
    "work_instance",
    "kind",
    "scope",
];
impl Request {
    fn change(&self) -> Change {
        match self {
            Self::List | Self::Review(..) => Change::Toggle,
            Self::AddReview(..) => Change::Add,
            Self::RemoveReview(..) => Change::Remove,
            Self::Apply(_, review) | Self::Readback(_, review, _) => review.change,
        }
    }
    pub fn command(&self, identity: &Identity, instance: &str) -> io::Result<Value> {
        let mut v = identity.request();
        v["work_instance"] = json!(instance);
        v["type"] = json!(match self {
            Self::List => LIST,
            Self::Review(old, index) => {
                let row = old
                    .rows
                    .get(*index)
                    .filter(|r| r.editable)
                    .ok_or_else(bad)?;
                v["name"] = json!(row.name);
                v["revision"] = json!(old.revision);
                v["disabled"] = json!(!row.disabled.ok_or_else(bad)?);
                REVIEW
            }
            Self::AddReview(old, spec) => {
                v["revision"] = json!(old.revision);
                v["spec"] = spec.wire();
                ADD_REVIEW
            }
            Self::RemoveReview(old, index) => {
                // Any visible row is asked about, so the source names why a shadowed or
                // foreign declaration cannot be removed.
                let row = old.rows.get(*index).ok_or_else(bad)?;
                v["name"] = json!(row.name);
                v["revision"] = json!(old.revision);
                REMOVE_REVIEW
            }
            Self::Apply(_, review) => {
                v["token"] = json!(review.token);
                APPLY
            }
            Self::Readback(_, review, id) => {
                v["token"] = json!(review.token);
                v["apply_id"] = json!(id);
                READBACK
            }
        });
        Ok(v)
    }
    pub fn response(
        self,
        v: &Value,
        identity: &Identity,
        instance: &str,
        id: &str,
    ) -> io::Result<Reply> {
        if self == Self::List {
            return Ok(Reply::Update(Update::List(Snapshot::parse(
                v, identity, instance,
            )?)));
        }
        if self.change() != Change::Toggle {
            return self.change_response(v, identity, instance, id);
        }
        let (kind, index, old, prior, apply_id) = match &self {
            Self::Review(old, index) => ("mcp_config_review", *index, old, None, None),
            Self::Apply(old, r) => ("mcp_config_applied", r.index, old, Some(r), Some(id)),
            Self::Readback(old, r, id) => {
                ("mcp_config_saved", r.index, old, Some(r), Some(id.as_str()))
            }
            _ => unreachable!(),
        };
        binding(v, identity, instance, kind)?;
        let mut keys = vec![
            "version",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "kind",
            "scope",
            "name",
            "previous",
            "disabled",
            "token",
            "before",
            "after",
            "apply_id",
        ];
        if kind == "mcp_config_saved" {
            keys.push("config");
        }
        if !exact(v, &keys) {
            return Err(bad());
        }
        let row = old.rows.get(index).filter(|r| r.editable).ok_or_else(bad)?;
        let token = hash(&v["token"])?;
        let after = hash(&v["after"])?;
        if v["name"] != row.name
            || v["before"] != old.revision
            || after == old.revision
            || Some(flag(v, "previous")?) != row.disabled
            || Some(flag(v, "disabled")?) == row.disabled
            || prior.is_some_and(|p| p.token != token || p.after != after)
            || v["apply_id"] != json!(apply_id)
        {
            return Err(bad());
        }
        let review = Review {
            index,
            token,
            after,
            change: Change::Toggle,
            name: row.name.clone(),
            entry: None,
        };
        match self {
            Self::Review(old, _) => Ok(Reply::Update(Update::Review(old, review))),
            Self::Apply(old, _) => Ok(Reply::Next(Self::Readback(old, review, id.into()))),
            Self::Readback(mut old, _, _) => {
                let actual = Snapshot::parse(&v["config"], identity, instance)?;
                old.revision = review.after;
                old.rows[index].disabled = Some(flag(v, "disabled")?);
                if actual != old {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Saved(actual, index, Change::Toggle)))
            }
            _ => unreachable!(),
        }
    }
    /// Add and remove receipts: the same single-use token, but the answer is the source's own
    /// redacted entry, checked against what was asked for, and the reread must differ from the
    /// reviewed rows by exactly that one declaration.
    fn change_response(
        self,
        v: &Value,
        identity: &Identity,
        instance: &str,
        id: &str,
    ) -> io::Result<Reply> {
        let change = self.change();
        let (phase, old, prior, apply_id) = match &self {
            Self::AddReview(old, _) | Self::RemoveReview(old, _) => (0, old, None, None),
            Self::Apply(old, r) => (1, old, Some(r), Some(id)),
            Self::Readback(old, r, id) => (2, old, Some(r), Some(id.as_str())),
            _ => unreachable!(),
        };
        let kind = match (change, phase) {
            (Change::Add, 0) => "mcp_config_add_review",
            (Change::Add, 1) => "mcp_config_add_applied",
            (Change::Add, _) => "mcp_config_add_saved",
            (_, 0) => "mcp_config_remove_review",
            (_, 1) => "mcp_config_remove_applied",
            (_, _) => "mcp_config_remove_saved",
        };
        binding(v, identity, instance, kind)?;
        let mut keys = COMMON.to_vec();
        keys.extend([
            "token",
            "name",
            "file",
            "transport",
            "entry",
            "before",
            "after",
            "apply_id",
        ]);
        if phase == 2 {
            keys.push("config");
        }
        if !exact(v, &keys) {
            return Err(bad());
        }
        let token = hash(&v["token"])?;
        let after = hash(&v["after"])?;
        let entry = Entry::parse(&v["entry"])?;
        let (name, index, expected) = match &self {
            Self::AddReview(_, spec) => {
                (spec.name.clone(), old.rows.len(), entry.matches_spec(spec))
            }
            Self::RemoveReview(_, index) => {
                let row = old
                    .rows
                    .get(*index)
                    .filter(|r| r.removable)
                    .ok_or_else(bad)?;
                (row.name.clone(), *index, entry.matches_row(row))
            }
            _ => {
                let p = prior.ok_or_else(bad)?;
                (
                    p.name.clone(),
                    p.index,
                    p.token == token && p.after == after && p.entry.as_ref() == Some(&entry),
                )
            }
        };
        if !expected
            || v["name"] != name
            || v["file"] != WRITTEN
            || v["transport"] != entry.transport
            || v["before"] != old.revision
            || after == old.revision
            || v["apply_id"] != json!(apply_id)
        {
            return Err(bad());
        }
        let review = Review {
            index,
            token,
            after,
            change,
            name,
            entry: Some(entry),
        };
        match self {
            Self::AddReview(old, _) | Self::RemoveReview(old, _) => {
                Ok(Reply::Update(Update::Review(old, review)))
            }
            Self::Apply(old, _) => Ok(Reply::Next(Self::Readback(old, review, id.into()))),
            Self::Readback(mut old, _, _) => {
                let actual = Snapshot::parse(&v["config"], identity, instance)?;
                let entry = review.entry.as_ref().ok_or_else(bad)?;
                let mut selected = index;
                if change == Change::Add {
                    old.rows.push(Row {
                        name: review.name.clone(),
                        file: WRITTEN.into(),
                        transport: entry.transport.clone(),
                        disabled: Some(false),
                        args: entry.args > 0,
                        env: !entry.env.is_empty(),
                        headers: !entry.headers.is_empty(),
                        shadowed: false,
                        removable: true,
                        editable: true,
                    });
                } else {
                    old.rows.remove(index);
                    selected = index.min(old.rows.len().saturating_sub(1));
                }
                old.revision = review.after;
                if actual != old {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Saved(actual, selected, change)))
            }
            _ => unreachable!(),
        }
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    data: Option<Snapshot>,
    review: Option<Review>,
    form: Option<Form>,
    index: usize,
    ready: bool,
    waiting: bool,
    asking: bool,
    note: String,
}
fn listed(names: &[String], omitted: u64) -> String {
    let mut text = if names.is_empty() {
        "none".to_owned()
    } else {
        names.join(", ")
    };
    if omitted > 0 {
        text.push_str(&format!(" (+{omitted} not shown)"));
    }
    text
}
impl View {
    pub fn open(&mut self) {
        *self = Self {
            open: true,
            waiting: true,
            note: "Reading shared declarations…".into(),
            ..Self::default()
        }
    }
    pub fn can_switch(&self) -> bool {
        !self.waiting && self.review.is_none() && self.form.is_none()
    }
    pub fn hide(&mut self) {
        self.ready = false;
    }
    pub fn accepts_paste(&self) -> bool {
        self.open
            && !self.waiting
            && self.review.is_none()
            && self.form.as_ref().is_some_and(Form::accepts_paste)
    }
    pub fn paste(&mut self, text: &str) {
        if self.accepts_paste()
            && let Some(form) = self.form.as_mut()
        {
            form.paste(text);
        }
    }
    pub fn update(&mut self, result: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        let asked = std::mem::take(&mut self.asking);
        match result {
            Ok(Update::List(data)) => {
                self.index = self.index.min(data.rows.len().saturating_sub(1));
                self.data = Some(data);
                self.review = None;
                self.form = None;
                self.note.clear();
            }
            Ok(Update::Review(data, review)) => {
                if review.change != Change::Add {
                    self.index = review.index;
                }
                self.data = Some(data);
                self.review = Some(review);
                self.note.clear();
            }
            Ok(Update::Saved(data, index, change)) => {
                self.index = index;
                self.data = Some(data);
                self.review = None;
                self.form = None;
                self.note = match change {
                    Change::Toggle => "Saved and verified on disk.",
                    Change::Add => "Declaration added and verified on disk. No server was connected or started.",
                    Change::Remove => "Declaration removed and verified on disk. No server was started or stopped.",
                }
                .into();
            }
            Err(reason) if asked => {
                // A refused review changed nothing, so the draft and the list stay as they were.
                self.review = None;
                self.note = crate::live::terminal_text(&reason);
            }
            Err(reason) => {
                self.data = None;
                self.review = None;
                self.form = None;
                self.note = crate::live::terminal_text(&reason);
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind == KeyEventKind::Release {
            return None;
        }
        let editing = self.form.is_some() && self.review.is_none();
        if key.code == KeyCode::Esc
            && key.kind == KeyEventKind::Press
            && key.modifiers.is_empty()
            && !self.waiting
        {
            if self.review.take().is_some() || self.form.take().is_some() {
                self.hide()
            } else {
                *self = Self::default()
            }
            return None;
        }
        if self.waiting || !self.ready {
            return None;
        }
        if editing {
            let form = self.form.as_mut()?;
            if form.key(key) {
                match form.spec() {
                    Ok(spec) => {
                        self.waiting = true;
                        self.asking = true;
                        self.hide();
                        return Some(Request::AddReview(self.data.clone()?, Box::new(spec)));
                    }
                    Err(note) => form.note = note,
                }
            }
            return None;
        }
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() {
            return None;
        }
        let req = match key.code {
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Up | KeyCode::Down if self.review.is_none() => {
                let length = self.data.as_ref()?.rows.len();
                self.index = if key.code == KeyCode::Up {
                    self.index.saturating_sub(1)
                } else {
                    self.index.saturating_add(1).min(length.saturating_sub(1))
                };
                self.hide();
                return None;
            }
            KeyCode::Char('e')
                if self.review.is_none() && self.data.as_ref()?.rows.get(self.index)?.editable =>
            {
                self.asking = true;
                Request::Review(self.data.clone()?, self.index)
            }
            KeyCode::Char('a') if self.review.is_none() && self.data.is_some() => {
                self.form = Some(Form::default());
                self.note.clear();
                self.hide();
                return None;
            }
            KeyCode::Char('x')
                if self.review.is_none() && self.data.as_ref()?.rows.get(self.index).is_some() =>
            {
                self.asking = true;
                Request::RemoveReview(self.data.clone()?, self.index)
            }
            KeyCode::Enter if self.review.is_some() => {
                Request::Apply(self.data.clone()?, self.review.take()?)
            }
            _ => return None,
        };
        self.waiting = true;
        self.hide();
        Some(req)
    }
    fn change_lines(&self, data: &Snapshot, review: &Review) -> Vec<String> {
        let add = review.change == Change::Add;
        let name = crate::live::terminal_text(&review.name);
        let mut lines = vec![format!(
            "{} \"{name}\" {} {WRITTEN}?",
            if add { "Add" } else { "Remove" },
            if add { "to" } else { "from" }
        )];
        if let Some(entry) = &review.entry {
            lines.extend([
                format!(
                    "{WRITTEN}   {}   target: {}",
                    entry.transport,
                    crate::live::terminal_text(&entry.target)
                ),
                format!(
                    "Arguments: {} (hidden)   environment names: {}   header names: {}",
                    entry.args,
                    listed(&entry.env, entry.omitted),
                    listed(&entry.headers, 0)
                ),
                "Values are never shown.".into(),
            ]);
        }
        lines.push(format!(
            "Revision {} → {}",
            &data.revision[..8],
            &review.after[..8]
        ));
        lines.push(
            if add {
                "Saving writes this declaration to .mcp.json only. It does not connect or start any server; use the MCP runtime controls (Tab) to connect it."
            } else {
                "Saving deletes this declaration from .mcp.json only. It does not connect, start or stop any server; MCP must already be off with every server stopped."
            }
            .into(),
        );
        lines
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let available = usize::from(area.width.saturating_sub(2)).max(1);
        let mut lines = vec!["Shared LiteTUI data".to_owned()];
        let changing = self
            .review
            .as_ref()
            .is_some_and(|r| r.change != Change::Toggle);
        if let (Some(form), false) = (&self.form, changing) {
            lines.extend([
                "New declaration for .mcp.json, the only file the source writes.".into(),
                "Saving only writes the declaration. It never connects or starts a server.".into(),
            ]);
            lines.extend(form.lines(available));
            if !form.note.is_empty() {
                lines.push(crate::live::terminal_text(&form.note));
            }
        } else if let (Some(data), Some(review), true) = (&self.data, &self.review, changing) {
            lines.extend(self.change_lines(data, review));
        } else if let Some(data) = &self.data {
            if let Some(row) = data.rows.get(self.index) {
                let state = match row.disabled {
                    Some(true) => "disabled",
                    Some(false) => "enabled",
                    None => "unknown",
                };
                lines.extend([
                    format!("{} of {}   {}", self.index + 1, data.rows.len(), row.name),
                    format!("{}   {}   {}", row.file, row.transport, state),
                    format!(
                        "Private arguments: {}   environment: {}   headers: {}",
                        row.args, row.env, row.headers
                    ),
                ]);
                if row.shadowed {
                    lines.push("mcp.json has precedence over this declaration.".into());
                } else if !row.editable {
                    lines.push("Read-only declaration. Edits require an unshadowed mcpServers entry in .mcp.json.".into());
                }
                if self.review.is_some() {
                    lines.push(format!(
                        "{} {} in .mcp.json?",
                        if row.disabled == Some(true) {
                            "Enable"
                        } else {
                            "Disable"
                        },
                        row.name
                    ));
                    lines.push("Saving updates the declaration on disk. A later reload or startup reads the new setting.".into());
                }
            } else {
                lines.push("No visible server declarations.".into());
            }
            if data.hidden > 0 {
                lines.push(format!(
                    "{} entries omitted because their names cannot be displayed safely.",
                    data.hidden
                ));
            }
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone());
        }
        if self.waiting {
            lines.push("Waiting for the source…".into());
        } else if self.review.is_some() {
            lines.push(
                if self.form.is_some() {
                    "Enter save   Esc back to the form"
                } else {
                    "Enter save   Esc cancel"
                }
                .into(),
            );
        } else if self.form.is_some() {
            lines.push(
                "Tab/Shift+Tab field   Enter review   Ctrl+N next line   Ctrl+U clear   Esc cancel"
                    .into(),
            );
        } else {
            lines.push(
                "↑/↓ select   A add   X remove   E enable/disable   F5 refresh   Esc back".into(),
            );
        }
        let wrapped = crate::ui::wrap(&lines.join("\n"), available);
        self.ready =
            area.width >= 42 && wrapped.len() <= usize::from(area.height.saturating_sub(2));
        let body = if self.ready {
            wrapped.join("\n")
        } else {
            "Resize to see the full declaration and its controls. Esc back.".into()
        };
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(body)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" MCP declarations · Tab: Runtime ")),
            area,
        );
    }
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
        let mut v = id.request();
        v["kind"] = json!("mcp_config");
        v["scope"] = json!("shared");
        v["work_instance"] = json!("instance");
        v["revision"] = json!("a".repeat(64));
        v["hidden"] = json!(0);
        v["rows"] = json!([{"name":"server","file":".mcp.json","transport":"stdio","disabled":false,
            "has_args":true,"has_env":false,"has_headers":false,"shadowed":false,"removable":true,"editable":true}]);
        (id, v)
    }
    #[test]
    fn redacted_rows_reject_private_fields_and_foreign_authority() {
        let (id, value) = fixture();
        assert!(Snapshot::parse(&value, &id, "instance").is_ok());
        for (key, bad_value) in [
            ("url", json!("secret")),
            ("file", json!("mcp.json")),
            ("shadowed", json!(true)),
            ("disabled", Value::Null),
            ("name", json!("../../forged")),
        ] {
            let mut bad = value.clone();
            bad["rows"][0][key] = bad_value;
            assert!(Snapshot::parse(&bad, &id, "instance").is_err());
        }
        assert!(Snapshot::parse(&value, &id, "foreign").is_err());
    }
    #[test]
    fn verified_readback_checks_every_other_projected_field() {
        let (id, mut wire) = fixture();
        let old = Snapshot::parse(&wire, &id, "instance").unwrap();
        let review = Review {
            index: 0,
            token: "b".repeat(64),
            after: "c".repeat(64),
            change: Change::Toggle,
            name: "server".into(),
            entry: None,
        };
        let req = Request::Readback(old.clone(), review.clone(), "apply-id".into());
        wire["rows"][0]["disabled"] = json!(true);
        wire["revision"] = json!(review.after);
        let mut saved = id.request();
        saved["kind"] = json!("mcp_config_saved");
        saved["scope"] = json!("shared");
        saved["work_instance"] = json!("instance");
        saved["name"] = json!("server");
        saved["previous"] = json!(false);
        saved["disabled"] = json!(true);
        saved["before"] = json!(old.revision);
        saved["after"] = json!(review.after);
        saved["token"] = json!(review.token);
        saved["apply_id"] = json!("apply-id");
        saved["config"] = wire;
        assert!(
            req.clone()
                .response(&saved, &id, "instance", "read-id")
                .is_ok()
        );
        saved["config"]["rows"][0]["has_env"] = json!(true);
        assert!(req.response(&saved, &id, "instance", "read-id").is_err());
    }

    fn row_wire(name: &str, file: &str, removable: bool, flags: [bool; 3]) -> Value {
        json!({"name":name,"file":file,"transport":"stdio","disabled":false,
            "has_args":flags[0],"has_env":flags[1],"has_headers":flags[2],
            "shadowed":false,"removable":removable,"editable":removable})
    }
    fn snapshot_wire(id: &Identity, revision: &str, rows: Vec<Value>) -> Value {
        let mut v = id.request();
        v["kind"] = json!("mcp_config");
        v["scope"] = json!("shared");
        v["work_instance"] = json!("instance");
        v["revision"] = json!(revision);
        v["hidden"] = json!(0);
        v["rows"] = json!(rows);
        v
    }
    fn receipt(id: &Identity, kind: &str, name: &str, entry: Value, after: &str) -> Value {
        let mut v = id.request();
        v["kind"] = json!(kind);
        v["scope"] = json!("shared");
        v["work_instance"] = json!("instance");
        v["token"] = json!("b".repeat(64));
        v["name"] = json!(name);
        v["file"] = json!(".mcp.json");
        v["transport"] = entry["transport"].clone();
        v["entry"] = entry;
        v["before"] = json!("a".repeat(64));
        v["after"] = json!(after);
        v["apply_id"] = Value::Null;
        v
    }
    fn spec() -> Spec {
        Spec {
            name: "added".into(),
            kind: form::Kind::Stdio {
                command: "npx".into(),
                args: vec!["-y".into(), "pkg".into()],
                env: vec![("TOKEN".into(), "hunter2".into())],
            },
        }
    }
    fn added_entry() -> Value {
        json!({"transport":"stdio","target":"npx","args":2,"env":["TOKEN"],"headers":[],"omitted":0})
    }
    fn review_of(reply: Reply) -> (Snapshot, Review) {
        match reply {
            Reply::Update(Update::Review(old, review)) => (old, review),
            _ => panic!("expected a review"),
        }
    }

    #[test]
    fn removable_and_editable_must_agree_with_file_and_shadowing() {
        let (id, value) = fixture();
        for bad_value in [json!(false), Value::Null] {
            let mut bad = value.clone();
            bad["rows"][0]["removable"] = bad_value;
            assert!(Snapshot::parse(&bad, &id, "instance").is_err());
        }
        let mut shadowed = value.clone();
        shadowed["rows"][0]["file"] = json!("mcp.json");
        assert!(Snapshot::parse(&shadowed, &id, "instance").is_err());
        let mut missing = value;
        missing["rows"][0]
            .as_object_mut()
            .unwrap()
            .remove("removable");
        assert!(Snapshot::parse(&missing, &id, "instance").is_err());
    }
    #[test]
    fn add_cycle_accepts_only_the_declared_entry_and_one_new_row() {
        let (id, doc) = fixture();
        let old = Snapshot::parse(&doc, &id, "instance").unwrap();
        let ask = Request::AddReview(old.clone(), Box::new(spec()));
        let command = ask.command(&id, "instance").unwrap();
        assert_eq!(command["type"], ADD_REVIEW);
        assert_eq!(command["revision"], "a".repeat(64));
        assert_eq!(command["spec"]["env"]["TOKEN"], "hunter2");
        assert!(!format!("{ask:?}").contains("hunter2"));
        let after = "c".repeat(64);
        let review_wire = receipt(&id, "mcp_config_add_review", "added", added_entry(), &after);
        let mutations: [fn(&mut Value); 15] = [
            |v| v["name"] = json!("other"),
            |v| v["file"] = json!("mcp.json"),
            |v| v["transport"] = json!("http"),
            |v| v["before"] = json!("d".repeat(64)),
            |v| v["after"] = json!("a".repeat(64)),
            |v| v["entry"]["env"] = json!(["TOKEN", "EXTRA"]),
            |v| v["entry"]["env"] = json!([]),
            |v| v["entry"]["args"] = json!(1),
            |v| v["entry"]["omitted"] = json!(1),
            |v| v["entry"]["env_values"] = json!({"TOKEN":"hunter2"}),
            |v| v["entry"]["target"] = json!("bad\u{1}"),
            |v| v["entry"]["env"] = json!(["bad name"]),
            |v| v["values"] = json!("hunter2"),
            |v| v["apply_id"] = json!("early"),
            |v| v["token"] = json!("short"),
        ];
        for mutate in mutations {
            let mut bad = review_wire.clone();
            mutate(&mut bad);
            assert!(
                ask.clone().response(&bad, &id, "instance", "r").is_err(),
                "{bad}"
            );
        }
        let (old, review) = review_of(
            ask.response(&review_wire, &id, "instance", "review-id")
                .unwrap(),
        );
        assert_eq!((review.change, review.index), (Change::Add, 1));
        let mut applied = receipt(
            &id,
            "mcp_config_add_applied",
            "added",
            added_entry(),
            &after,
        );
        applied["apply_id"] = json!("apply-id");
        let apply = Request::Apply(old, review.clone());
        let Reply::Next(readback) = apply
            .clone()
            .response(&applied, &id, "instance", "apply-id")
            .unwrap()
        else {
            panic!("apply continues to readback")
        };
        let mut other_id = applied.clone();
        other_id["apply_id"] = json!("different");
        assert!(
            apply
                .response(&other_id, &id, "instance", "apply-id")
                .is_err()
        );
        let rows = || {
            vec![
                row_wire("server", ".mcp.json", true, [true, false, false]),
                row_wire("added", ".mcp.json", true, [true, true, false]),
            ]
        };
        let mut saved = receipt(&id, "mcp_config_add_saved", "added", added_entry(), &after);
        saved["apply_id"] = json!("apply-id");
        saved["config"] = snapshot_wire(&id, &after, rows());
        match readback
            .clone()
            .response(&saved, &id, "instance", "read-id")
            .unwrap()
        {
            Reply::Update(Update::Saved(actual, index, Change::Add)) => {
                assert_eq!((index, actual.rows.len()), (1, 2));
            }
            _ => panic!("add reread"),
        }
        let mutations: [fn(&mut Value); 9] = [
            |v| v["config"]["rows"][1]["has_env"] = json!(false),
            |v| v["config"]["rows"][1]["has_headers"] = json!(true),
            |v| v["config"]["rows"][1]["transport"] = json!("http"),
            |v| v["config"]["rows"][0]["disabled"] = json!(true),
            |v| v["config"]["rows"][1]["name"] = json!("sneaky"),
            |v| {
                v["config"]["rows"].as_array_mut().unwrap().pop();
            },
            |v| v["config"]["revision"] = json!("d".repeat(64)),
            |v| v["config"]["hidden"] = json!(1),
            |v| v["entry"]["args"] = json!(3),
        ];
        for mutate in mutations {
            let mut bad = saved.clone();
            mutate(&mut bad);
            assert!(
                readback
                    .clone()
                    .response(&bad, &id, "instance", "r")
                    .is_err(),
                "{bad}"
            );
        }
    }
    #[test]
    fn remove_cycle_drops_exactly_the_reviewed_row() {
        let (id, _) = fixture();
        let rows = vec![
            row_wire("pinned", "mcp.json", false, [false; 3]),
            row_wire("first", ".mcp.json", true, [true, true, false]),
            row_wire("second", ".mcp.json", true, [false; 3]),
        ];
        let old =
            Snapshot::parse(&snapshot_wire(&id, &"a".repeat(64), rows), &id, "instance").unwrap();
        let after = "c".repeat(64);
        let entry = json!({"transport":"stdio","target":"runner","args":1,"env":["TOKEN"],"headers":[],"omitted":0});
        let ask = Request::RemoveReview(old.clone(), 1);
        let command = ask.command(&id, "instance").unwrap();
        assert_eq!(command["type"], REMOVE_REVIEW);
        assert_eq!(command["name"], "first");
        // The source is asked about any visible row so it can say why a foreign one is refused,
        // but a receipt for a row it does not own is never accepted.
        let foreign = Request::RemoveReview(old.clone(), 0);
        assert!(foreign.command(&id, "instance").is_ok());
        let wire = receipt(
            &id,
            "mcp_config_remove_review",
            "pinned",
            entry.clone(),
            &after,
        );
        assert!(foreign.response(&wire, &id, "instance", "r").is_err());
        let wire = receipt(
            &id,
            "mcp_config_remove_review",
            "first",
            entry.clone(),
            &after,
        );
        let mutations: [fn(&mut Value); 4] = [
            |v| v["name"] = json!("second"),
            |v| v["entry"]["transport"] = json!("http"),
            |v| v["entry"]["headers"] = json!(["Authorization"]),
            |v| v["entry"]["env"] = json!([]),
        ];
        for mutate in mutations {
            let mut bad = wire.clone();
            mutate(&mut bad);
            assert!(
                ask.clone().response(&bad, &id, "instance", "r").is_err(),
                "{bad}"
            );
        }
        let (old, review) = review_of(ask.response(&wire, &id, "instance", "r").unwrap());
        assert_eq!((review.change, review.index), (Change::Remove, 1));
        let mut applied = receipt(
            &id,
            "mcp_config_remove_applied",
            "first",
            entry.clone(),
            &after,
        );
        applied["apply_id"] = json!("apply-id");
        let Reply::Next(readback) = Request::Apply(old, review)
            .response(&applied, &id, "instance", "apply-id")
            .unwrap()
        else {
            panic!("apply continues to readback")
        };
        let mut saved = receipt(&id, "mcp_config_remove_saved", "first", entry, &after);
        saved["apply_id"] = json!("apply-id");
        saved["config"] = snapshot_wire(
            &id,
            &after,
            vec![
                row_wire("pinned", "mcp.json", false, [false; 3]),
                row_wire("second", ".mcp.json", true, [false; 3]),
            ],
        );
        match readback
            .clone()
            .response(&saved, &id, "instance", "r")
            .unwrap()
        {
            Reply::Update(Update::Saved(actual, index, Change::Remove)) => {
                assert_eq!((index, actual.rows.len()), (1, 2));
            }
            _ => panic!("remove reread"),
        }
        let mut kept = saved.clone();
        kept["config"]["rows"] = json!([
            row_wire("pinned", "mcp.json", false, [false; 3]),
            row_wire("first", ".mcp.json", true, [true, true, false]),
            row_wire("second", ".mcp.json", true, [false; 3]),
        ]);
        assert!(readback.response(&kept, &id, "instance", "r").is_err());
    }

    fn paint(view: &mut View, terminal: &mut Terminal<TestBackend>) -> String {
        let theme = crate::theme::builtin_themes().remove(0);
        terminal
            .draw(|frame| view.draw(frame, frame.area(), &theme))
            .unwrap();
        let buffer = terminal.backend().buffer();
        buffer
            .content
            .chunks(usize::from(buffer.area.width))
            .map(|row| row.iter().map(|cell| cell.symbol()).collect::<String>())
            .collect::<Vec<_>>()
            .join("\n")
    }
    fn tap(
        view: &mut View,
        terminal: &mut Terminal<TestBackend>,
        code: KeyCode,
    ) -> Option<Request> {
        paint(view, terminal);
        view.key(KeyEvent::new(code, crossterm::event::KeyModifiers::NONE))
    }
    fn typed(view: &mut View, terminal: &mut Terminal<TestBackend>, text: &str) {
        for c in text.chars() {
            assert!(tap(view, terminal, KeyCode::Char(c)).is_none());
        }
    }

    #[test]
    fn form_review_and_save_paint_only_redacted_text_and_keep_the_draft_until_saved() {
        let (id, doc) = fixture();
        let mut terminal = Terminal::new(TestBackend::new(106, 28)).unwrap();
        let mut view = View::default();
        view.open();
        view.update(Ok(Update::List(
            Snapshot::parse(&doc, &id, "instance").unwrap(),
        )));
        let list = paint(&mut view, &mut terminal);
        assert!(
            list.contains("A add") && list.contains("X remove"),
            "{list}"
        );
        assert!(view.can_switch() && !view.accepts_paste());
        assert!(tap(&mut view, &mut terminal, KeyCode::Char('a')).is_none());
        paint(&mut view, &mut terminal);
        assert!(!view.can_switch() && view.accepts_paste());
        typed(&mut view, &mut terminal, "added");
        tap(&mut view, &mut terminal, KeyCode::Tab);
        tap(&mut view, &mut terminal, KeyCode::Tab);
        typed(&mut view, &mut terminal, "npx");
        tap(&mut view, &mut terminal, KeyCode::Tab);
        typed(&mut view, &mut terminal, "-y pkg");
        tap(&mut view, &mut terminal, KeyCode::Tab);
        view.paste("TOKEN=hunter2");
        let screen = paint(&mut view, &mut terminal);
        assert!(
            screen.contains("New declaration for .mcp.json")
                && screen.contains("never connects or starts a server")
                && screen.contains("TOKEN=••••"),
            "{screen}"
        );
        assert!(!screen.contains("hunter2"), "{screen}");
        let Some(request) = tap(&mut view, &mut terminal, KeyCode::Enter) else {
            panic!("Enter asks the source to review")
        };
        assert!(matches!(request, Request::AddReview(..)));
        let wire = request.command(&id, "instance").unwrap();
        assert_eq!(wire["spec"]["env"]["TOKEN"], "hunter2");
        assert!(!format!("{request:?}").contains("hunter2"));
        // A refusal changes nothing, so the draft survives for correction.
        view.update(Err("'added' is already declared in .mcp.json".into()));
        let refused = paint(&mut view, &mut terminal);
        assert!(refused.contains("already declared") && refused.contains("TOKEN=••••"));
        let Some(request) = tap(&mut view, &mut terminal, KeyCode::Enter) else {
            panic!("resubmit")
        };
        let (_, review) = review_of(
            request
                .response(
                    &receipt(
                        &id,
                        "mcp_config_add_review",
                        "added",
                        added_entry(),
                        &"c".repeat(64),
                    ),
                    &id,
                    "instance",
                    "r",
                )
                .unwrap(),
        );
        view.update(Ok(Update::Review(
            Snapshot::parse(&doc, &id, "instance").unwrap(),
            review,
        )));
        let review_screen = paint(&mut view, &mut terminal);
        for needle in [
            "Add \"added\" to .mcp.json?",
            "target: npx",
            "Arguments: 2 (hidden)",
            "environment names: TOKEN",
            "does not connect or start any server",
            "Revision aaaaaaaa → cccccccc",
        ] {
            assert!(review_screen.contains(needle), "{needle}\n{review_screen}");
        }
        assert!(!review_screen.contains("hunter2") && !review_screen.contains("••••"));
        assert!(!view.can_switch() && !view.accepts_paste());
        // Esc returns to the form with the draft intact; nothing was sent.
        assert!(tap(&mut view, &mut terminal, KeyCode::Esc).is_none());
        let back = paint(&mut view, &mut terminal);
        assert!(back.contains("TOKEN=••••") && back.contains("New declaration"));
    }
    #[test]
    fn remove_review_states_the_no_server_consequence_and_failure_is_not_success() {
        let (id, doc) = fixture();
        let mut terminal = Terminal::new(TestBackend::new(106, 28)).unwrap();
        let mut view = View::default();
        view.open();
        let old = Snapshot::parse(&doc, &id, "instance").unwrap();
        view.update(Ok(Update::List(old.clone())));
        let Some(request) = tap(&mut view, &mut terminal, KeyCode::Char('x')) else {
            panic!("X asks the source")
        };
        assert!(matches!(request, Request::RemoveReview(_, 0)));
        let entry =
            json!({"transport":"stdio","target":"npx","args":1,"env":[],"headers":[],"omitted":0});
        let (_, review) = review_of(
            request
                .response(
                    &receipt(
                        &id,
                        "mcp_config_remove_review",
                        "server",
                        entry,
                        &"c".repeat(64),
                    ),
                    &id,
                    "instance",
                    "r",
                )
                .unwrap(),
        );
        view.update(Ok(Update::Review(old, review)));
        let screen = paint(&mut view, &mut terminal);
        assert!(
            screen.contains("Remove \"server\" from .mcp.json?")
                && screen.contains("does not connect, start or stop any server"),
            "{screen}"
        );
        let Some(Request::Apply(..)) = tap(&mut view, &mut terminal, KeyCode::Enter) else {
            panic!("Enter applies the reviewed removal")
        };
        view.update(Err("Shared MCP declarations changed since review".into()));
        let failed = paint(&mut view, &mut terminal);
        assert!(failed.contains("changed since review") && !failed.contains("Declaration removed"));
        assert!(view.can_switch());
    }
}
