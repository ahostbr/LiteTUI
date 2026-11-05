//! Redacted file declarations and single-use edits under the active Work identity.
use crate::{theme::Theme, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
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
pub const APPLY: &str = "gui.mcp.native_config_apply";
pub const READBACK: &str = "gui.mcp.native_config_readback";
pub const OPERATIONS: [&str; 4] = [LIST, REVIEW, APPLY, READBACK];

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
            if editable && (!dot || shadowed || disabled.is_none() || transport == "unknown") {
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
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    index: usize,
    token: String,
    after: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(Snapshot, usize),
    Apply(Snapshot, Review),
    Readback(Snapshot, Review, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Snapshot, Review),
    Saved(Snapshot, usize),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
impl Request {
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
        let (kind, index, old, prior, apply_id) = match &self {
            Self::Review(old, index) => ("mcp_config_review", *index, old, None, None),
            Self::Apply(old, r) => ("mcp_config_applied", r.index, old, Some(r), Some(id)),
            Self::Readback(old, r, id) => {
                ("mcp_config_saved", r.index, old, Some(r), Some(id.as_str()))
            }
            Self::List => unreachable!(),
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
                Ok(Reply::Update(Update::Saved(actual, index)))
            }
            Self::List => unreachable!(),
        }
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    data: Option<Snapshot>,
    review: Option<Review>,
    index: usize,
    ready: bool,
    waiting: bool,
    note: String,
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
    pub fn hide(&mut self) {
        self.ready = false;
    }
    pub fn update(&mut self, result: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        match result {
            Ok(Update::List(data)) => {
                self.index = self.index.min(data.rows.len().saturating_sub(1));
                self.data = Some(data);
                self.review = None;
                self.note.clear();
            }
            Ok(Update::Review(data, review)) => {
                self.index = review.index;
                self.data = Some(data);
                self.review = Some(review);
                self.note.clear();
            }
            Ok(Update::Saved(data, index)) => {
                self.index = index;
                self.data = Some(data);
                self.review = None;
                self.note = "Saved and verified on disk.".into();
            }
            Err(reason) => {
                self.data = None;
                self.review = None;
                self.note = crate::live::terminal_text(&reason);
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() {
            return None;
        }
        if key.code == KeyCode::Esc && !self.waiting {
            if self.review.take().is_some() {
                self.hide()
            } else {
                *self = Self::default()
            }
            return None;
        }
        if self.waiting || !self.ready {
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
                Request::Review(self.data.clone()?, self.index)
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
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Shared LiteTUI data".to_owned()];
        if let Some(data) = &self.data {
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
            lines.push("Enter save   Esc cancel".into());
        } else {
            lines.push("↑/↓ select   E review enable/disable   F5 refresh   Esc back".into());
        }
        let available = usize::from(area.width.saturating_sub(2)).max(1);
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
                .block(Block::bordered().title(" MCP declarations ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

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
            "has_args":true,"has_env":false,"has_headers":false,"shadowed":false,"editable":true}]);
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
}
