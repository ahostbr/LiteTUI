//! Optional desktop lifecycle and deferred edits, with one review per canonical destination.
use crate::{theme::Theme, work_protocol::Identity};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::io;

pub const OPERATIONS: [&str; 9] = [
    "gui.sidecar.native_snapshot",
    "gui.sidecar.native_review",
    "gui.sidecar.native_apply",
    "gui.sidecar.native_readback",
    "gui.sidecar.native_step_review",
    "gui.sidecar.native_step_apply",
    "gui.sidecar.native_step_readback",
    "gui.sidecar.native_cancel",
    "gui.sidecar.native_configure",
];
fn bad() -> io::Error {
    io::Error::other(
        "Sidecar response changed its reviewed child, proposal, destination or saved readback",
    )
}
fn text(v: &Value, max: usize) -> io::Result<&str> {
    v.as_str()
        .filter(|s| !s.is_empty() && s.len() <= max)
        .ok_or_else(bad)
}
fn hash(v: &Value) -> io::Result<&str> {
    let s = text(v, 64)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(bad());
    }
    Ok(s)
}
fn object(v: &Value) -> io::Result<&serde_json::Map<String, Value>> {
    v.as_object().ok_or_else(bad)
}
fn base(id: &Identity, instance: &str) -> Value {
    json!({"version":1,"session_id":id.session,"agent_id":id.agent,"backend":id.backend,"model":id.model,"work_instance":instance})
}
fn binding(v: &Value, id: &Identity, instance: &str) -> io::Result<()> {
    if !object(&base(id, instance))?
        .iter()
        .all(|(k, val)| v[k] == *val)
        || serde_json::to_vec(v)?.len() > 1024 * 1024
    {
        return Err(bad());
    }
    hash(&v["instance"])?;
    Ok(())
}
fn same(v: &Value, before: &Value, fields: &[&str]) -> io::Result<()> {
    if fields.iter().all(|key| v[*key] == before[*key]) {
        Ok(())
    } else {
        Err(bad())
    }
}
fn process(v: &Value) -> io::Result<()> {
    for key in ["retained", "running", "reader_alive", "closing"] {
        v[key].as_bool().ok_or_else(bad)?;
    }
    if !v["generation"].is_null() {
        hash(&v["generation"])?;
    }
    if !v["snapshot_sha256"].is_null() {
        hash(&v["snapshot_sha256"])?;
    }
    if v["running"] == true
        && (v["retained"] != true || v["pid"].as_u64().filter(|x| *x > 0).is_none())
    {
        return Err(bad());
    }
    if !v["view"].is_null()
        && !["timeline", "calendar", "settings", "job"].contains(&text(&v["view"], 16)?)
    {
        return Err(bad());
    }
    Ok(())
}
fn snapshot(v: &Value) -> io::Result<()> {
    if v["kind"] != "sidecar_snapshot" {
        return Err(bad());
    }
    hash(&v["revision"])?;
    v["enabled"].as_bool().ok_or_else(bad)?;
    v["hold"].as_bool().ok_or_else(bad)?;
    process(&v["process"])?;
    let path = text(&v["executable"]["path"], 4096)?;
    if !std::path::Path::new(path).is_absolute() {
        return Err(bad());
    }
    if v["executable"]["available"] == true {
        hash(&v["executable"]["sha256"])?;
    }
    let rows = v["proposals"]
        .as_array()
        .filter(|r| r.len() <= 16)
        .ok_or_else(bad)?;
    for p in rows {
        hash(&p["proposal"])?;
        if !["settings_patch", "job_create"].contains(&text(&p["command"], 32)?)
            || !["queued", "reviewing", "complete", "cancelled", "uncertain"]
                .contains(&text(&p["status"], 32)?)
        {
            return Err(bad());
        }
        object(&p["payload"])?;
    }
    Ok(())
}
fn route(v: &Value) -> io::Result<&str> {
    match text(v, 32)? {
        "presentation" => Ok("gui.presentation.native_"),
        "voice" => Ok("gui.voice.native_"),
        "connection" => Ok("gui.connection.native_"),
        "agent" => Ok("gui.agent.native_"),
        "sidecar" => Ok("gui.sidecar_preferences.native_"),
        "defaults" => Ok("gui.models.native_preferences_"),
        "budget" => Ok("gui.settings.native_"),
        "compaction" => Ok("gui.settings.native_compact_"),
        "theme" => Ok("gui.themes.native_"),
        "engine" => Ok("gui.engine.native_config_"),
        "runtime" => Ok("gui.runtime.native_"),
        "job" => Ok("gui.work.native_control_"),
        _ => Err(bad()),
    }
}
fn step_review(v: &Value, proposal: Option<&Value>, configure: Option<bool>) -> io::Result<()> {
    if v["kind"] != "sidecar_step_review" {
        return Err(bad());
    }
    hash(&v["proposal"])?;
    let index = v["index"].as_u64().ok_or_else(bad)?;
    let count = v["count"]
        .as_u64()
        .filter(|n| *n > 0 && *n <= 160)
        .ok_or_else(bad)?;
    if index >= count {
        return Err(bad());
    }
    let g = &v["group"];
    let key = text(&g["field"], 128)?;
    let prefix = route(&g["route"])?;
    if !text(&v["command"]["type"], 128)?.starts_with(prefix) {
        return Err(bad());
    }
    if let Some(p) = proposal {
        if v["proposal"] != p["proposal"] {
            return Err(bad());
        }
        if g["route"] == "job" {
            let mut draft = p["payload"].clone();
            draft["kind"] = json!("cron");
            if g["value"] != draft
                || v["review"]["draft"] != draft
                || v["review"]["action"] != "job_create"
            {
                return Err(bad());
            }
        } else {
            let change = p["payload"]["changes"]
                .as_array()
                .ok_or_else(bad)?
                .iter()
                .find(|c| c["key"] == key)
                .ok_or_else(bad)?;
            if g["scope"] != change["scope"] {
                return Err(bad());
            }
            if key == "custom_themes"
                && !p["payload"]["changes"]
                    .as_array()
                    .ok_or_else(bad)?
                    .iter()
                    .any(|c| {
                        c["key"] == "theme_name"
                            && c["value"] == g["value"]["name"]
                            && c["scope"] == change["scope"]
                    })
            {
                return Err(bad());
            }
            if g.get("op").is_none() && g.get("expected").is_none() && g["value"] != change["value"]
            {
                return Err(bad());
            }
            if key == "tools_disabled" {
                let before = v["before"][key].as_array().ok_or_else(bad)?;
                let target = change["value"].as_array().ok_or_else(bad)?;
                let name = text(&g["value"]["name"], 128)?;
                let enabled = g["value"]["enabled"].as_bool().ok_or_else(bad)?;
                if target.contains(&json!(name)) == enabled {
                    return Err(bad());
                }
                let mut after = before.clone();
                if enabled {
                    after.retain(|item| item != name);
                } else if !after.contains(&json!(name)) {
                    after.push(json!(name));
                }
                if g["expected"] != json!(after) {
                    return Err(bad());
                }
            }
            if let Some(op) = g["op"].as_str() {
                let before = v["before"][key].as_array().ok_or_else(bad)?;
                let after = v["expected"][key].as_array().ok_or_else(bad)?;
                let mut expected = before.clone();
                match op {
                    "add" => expected.push(g["value"].clone()),
                    "remove" => {
                        let pos = expected
                            .iter()
                            .position(|e| e == &g["value"])
                            .ok_or_else(bad)?;
                        expected.remove(pos);
                    }
                    _ => return Err(bad()),
                }
                if *after != expected || g["expected"] != v["expected"][key] {
                    return Err(bad());
                }
            }
        }
    } else if let Some(enabled) = configure {
        if g["field"] != "sidecar_enabled"
            || g["value"] != enabled
            || g["scope"] != "device"
            || v["expected"]["sidecar_enabled"] != enabled
        {
            return Err(bad());
        }
    } else {
        return Err(bad());
    }
    let expected = object(&v["expected"])?;
    let destinations = object(&v["destinations"])?;
    if expected.len() != destinations.len()
        || expected.keys().any(|k| {
            !destinations.contains_key(k) || !object(&v["before"]).is_ok_and(|b| b.contains_key(k))
        })
    {
        return Err(bad());
    }
    for path in destinations.values() {
        if !std::path::Path::new(text(path, 4096)?).is_absolute() {
            return Err(bad());
        }
    }
    if g["route"] == "job" {
        if !expected.is_empty() {
            return Err(bad());
        }
    } else if key == "custom_themes" {
        if expected.len() != 2 || v["expected"]["theme_name"] != g["value"]["name"] {
            return Err(bad());
        }
        let name = text(&g["value"]["name"], 256)?;
        let mut palettes = object(&v["before"]["custom_themes"])?.clone();
        let mut tokens = serde_json::Map::new();
        for (key, value) in object(&g["value"]["tokens"])? {
            tokens.insert(key.clone(), json!(value.as_str().ok_or_else(bad)?.trim()));
        }
        palettes.insert(name.into(), json!(tokens));
        if v["expected"]["custom_themes"] != json!(palettes) {
            return Err(bad());
        }
    } else {
        let wanted = g.get("expected").unwrap_or(&g["value"]);
        let wanted = if key == "stop" && wanted.is_null() {
            json!([])
        } else {
            wanted.clone()
        };
        if expected.len() != 1 || expected.get(key) != Some(&wanted) {
            return Err(bad());
        }
    }
    Ok(())
}

pub(crate) fn runtime_review(
    v: &Value,
    policy: &crate::runtime_controls::Policy,
) -> io::Result<crate::runtime_controls::Review> {
    use crate::runtime_controls as controls;
    let change = match v["group"]["field"].as_str() {
        Some("tools_enabled") => {
            controls::Change::Tools(v["group"]["value"].as_bool().ok_or_else(bad)?)
        }
        Some("tool_policy_profile") => controls::Change::Profile(crate::tool_mode::Profile::parse(
            text(&v["group"]["value"], 64)?,
        )?),
        Some("tools_disabled") => controls::Change::ToolSelection {
            name: text(&v["group"]["value"]["name"], 128)?.into(),
            enabled: v["group"]["value"]["enabled"].as_bool().ok_or_else(bad)?,
        },
        _ => return Err(bad()),
    };
    let snapshot = if matches!(change, controls::Change::ToolSelection { .. }) {
        controls::Snapshot::parse_tool_list(&v["native_snapshot"], &policy.binding)?
    } else {
        controls::Snapshot::parse(&v["native_snapshot"], &policy.binding)?
    };
    policy.verify_stable(&snapshot.binding, snapshot.generation, &snapshot.facts)?;
    controls::Review::parse(&v["review"], &snapshot, &change)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    Snapshot,
    Review {
        snapshot: Value,
        action: String,
        view: Option<String>,
    },
    Apply(Value),
    Readback {
        review: Value,
        applied: Value,
    },
    StepReview(Value),
    Configure(bool),
    StepApply(Value),
    StepReadback {
        review: Value,
        applied: Value,
    },
    Cancel(Value),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    Snapshot(Value),
    Review(Value),
    StepReview(Value),
    Saved(Value),
    Cancelled(Value),
}
impl Request {
    pub fn mutates(&self) -> bool {
        matches!(self, Self::Apply(_) | Self::StepApply(_))
    }
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut v = base(id, instance);
        let op = match self {
            Self::Snapshot => 0,
            Self::Review { action, view, .. } => {
                v["action"] = json!(action);
                v["view"] = json!(view);
                1
            }
            Self::Apply(r) => {
                v["token"] = r["token"].clone();
                2
            }
            Self::Readback { review, applied } => {
                v["token"] = review["token"].clone();
                v["apply_id"] = applied["apply_id"].clone();
                3
            }
            Self::StepReview(p) => {
                v["proposal"] = p["proposal"].clone();
                4
            }
            Self::Configure(enabled) => {
                v["value"] = json!(enabled);
                8
            }
            Self::StepApply(r) => {
                v["proposal"] = r["proposal"].clone();
                v["index"] = r["index"].clone();
                5
            }
            Self::StepReadback { review, applied } => {
                v["proposal"] = review["proposal"].clone();
                v["index"] = review["index"].clone();
                v["apply_id"] = applied["command"]["id"].clone();
                6
            }
            Self::Cancel(p) => {
                v["proposal"] = p["proposal"].clone();
                7
            }
        };
        v["type"] = json!(OPERATIONS[op]);
        Ok(v)
    }
    pub fn response(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        command_id: &str,
        policy: Option<&crate::runtime_controls::Policy>,
    ) -> io::Result<Result<Request, Update>> {
        binding(v, id, instance)?;
        match self {
            Self::Snapshot => {
                snapshot(v)?;
                Ok(Err(Update::Snapshot(v.clone())))
            }
            Self::Review {
                snapshot: s,
                action,
                view,
            } => {
                if v["kind"] != "sidecar_review"
                    || v["action"] != *action
                    || v["view"] != json!(view)
                    || v["before"] != s["process"]
                    || v["executable"] != s["executable"]
                {
                    return Err(bad());
                }
                hash(&v["token"])?;
                Ok(Err(Update::Review(v.clone())))
            }
            Self::Apply(r) => {
                if v["kind"] != "sidecar_applied"
                    || v["verified"] != false
                    || v["apply_id"] != command_id
                {
                    return Err(bad());
                }
                same(v, r, &["instance", "token", "action", "view", "before"])?;
                process(&v["after"])?;
                if r["action"] == "close" {
                    if v["after"]["retained"] != false || v["after"]["reader_alive"] != false {
                        return Err(bad());
                    }
                } else if v["after"]["running"] != true
                    || v["after"]["reader_alive"] != true
                    || v["after"]["view"] != r["view"]
                    || v["after"]["snapshot_sha256"] != r["snapshot_sha256"]
                {
                    return Err(bad());
                }
                Ok(Ok(Self::Readback {
                    review: r.clone(),
                    applied: v.clone(),
                }))
            }
            Self::Readback { applied, .. } => {
                if v["kind"] != "sidecar_readback" || v["verified"] != true {
                    return Err(bad());
                }
                same(
                    v,
                    applied,
                    &[
                        "instance", "token", "apply_id", "action", "view", "before", "after",
                    ],
                )?;
                Ok(Err(Update::Saved(v.clone())))
            }
            Self::StepReview(p) => {
                step_review(v, Some(p), None)?;
                if v["group"]["route"] == "runtime" {
                    runtime_review(v, policy.ok_or_else(bad)?)?;
                }
                Ok(Err(Update::StepReview(v.clone())))
            }
            Self::Configure(enabled) => {
                step_review(v, None, Some(*enabled))?;
                Ok(Err(Update::StepReview(v.clone())))
            }
            Self::StepApply(r) => {
                if v["kind"] != "sidecar_step_applied"
                    || v["command"]["id"] != format!("{command_id}/apply")
                {
                    return Err(bad());
                }
                same(
                    v,
                    r,
                    &[
                        "instance",
                        "proposal",
                        "index",
                        "group",
                        "expected",
                        "destinations",
                    ],
                )?;
                if !text(&v["command"]["type"], 128)?.starts_with(route(&r["group"]["route"])?) {
                    return Err(bad());
                }
                Ok(Ok(Self::StepReadback {
                    review: r.clone(),
                    applied: v.clone(),
                }))
            }
            Self::StepReadback { review: r, applied } => {
                if v["kind"] != "sidecar_step_readback"
                    || v["verified"] != true
                    || v["command"]["id"] != format!("{command_id}/readback")
                {
                    return Err(bad());
                }
                same(
                    v,
                    r,
                    &[
                        "instance",
                        "proposal",
                        "index",
                        "group",
                        "expected",
                        "destinations",
                    ],
                )?;
                if r["group"]["route"] == "job" {
                    let job = &v["verification"]["job"];
                    if job["verified"] != true
                        || job["target_id"] != applied["result"]["target_id"]
                        || job["receipt"] != applied["result"]["receipt"]
                    {
                        return Err(bad());
                    }
                    for key in ["prompt", "schedule", "label", "tool_profile", "kind"] {
                        if let Some(value) = r["group"]["value"].get(key)
                            && job["row"][key] != *value
                        {
                            return Err(bad());
                        }
                    }
                } else {
                    let fields = object(&v["verification"]["fields"])?;
                    let expected = object(&r["expected"])?;
                    if fields.len() != expected.len() {
                        return Err(bad());
                    }
                    for (key, value) in expected {
                        let field = fields.get(key).ok_or_else(bad)?;
                        if field["saved"] != *value
                            || field["destination"] != r["destinations"][key]
                            || field["scope"] != r["group"]["scope"]
                        {
                            return Err(bad());
                        }
                        let pending = field["pending"].as_bool().ok_or_else(bad)?;
                        if pending
                            != ["reload", "restart", "reconnect"]
                                .contains(&text(&field["timing"], 32)?)
                            || field["effective"]
                                != if pending {
                                    r["before"][key].clone()
                                } else {
                                    value.clone()
                                }
                        {
                            return Err(bad());
                        }
                    }
                }
                Ok(Err(Update::Saved(v.clone())))
            }
            Self::Cancel(p) => {
                if v["kind"] != "sidecar_cancelled"
                    || v["proposal"]["proposal"] != p["proposal"]
                    || v["proposal"]["status"] != "cancelled"
                {
                    return Err(bad());
                }
                Ok(Err(Update::Cancelled(v.clone())))
            }
        }
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    snapshot: Option<Value>,
    review: Option<Value>,
    step: bool,
    selected: usize,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    geometry: Option<Rect>,
    painted_review: Option<Value>,
    view: usize,
    waiting: bool,
    notice: String,
}
const VIEWS: [&str; 4] = ["timeline", "calendar", "settings", "job"];
impl View {
    fn invalidate_paint(&mut self) {
        self.page = 0;
        self.pages = 0;
        self.seen.clear();
        self.geometry = None;
        self.painted_review = None;
    }
    pub fn open(&mut self, argument: &str) {
        self.invalidate_paint();
        self.review = None;
        self.open = true;
        self.waiting = true;
        self.view = VIEWS.iter().position(|x| *x == argument).unwrap_or(0);
        self.notice = "Reading optional Sidecar and queued desktop proposals…".into();
    }
    pub(crate) fn occlude(&mut self) {
        self.invalidate_paint();
    }
    pub fn hide(&mut self) {
        self.occlude();
        self.open = false;
    }
    pub fn update(&mut self, result: Result<Update, String>) -> Option<Request> {
        self.invalidate_paint();
        self.open = true;
        self.waiting = false;
        match result {
            Ok(Update::Snapshot(v)) => {
                self.review = None;
                self.snapshot = Some(v);
                self.notice="Desktop Save/Create waits here. R refreshes proposals; each group is reviewed separately.".into();
            }
            Ok(Update::Review(v)) => {
                self.review = Some(v);
                self.step = false;
                self.notice =
                    "Enter confirms this exact child lifecycle; Esc keeps it unchanged.".into();
            }
            Ok(Update::StepReview(v)) => {
                self.review = Some(v);
                self.step = true;
                self.notice="Review the exact native change below. Enter saves this group; D discards the remaining proposal.".into();
            }
            Ok(Update::Saved(_)) => {
                self.review = None;
                self.waiting = true;
                self.notice = "Verified saved/readback. Refreshing the remaining proposals…".into();
                return Some(Request::Snapshot);
            }
            Ok(Update::Cancelled(_)) => {
                self.review = None;
                self.waiting = true;
                self.notice =
                    "Remaining draft discarded; previously verified groups stay saved.".into();
                return Some(Request::Snapshot);
            }
            Err(reason) => {
                self.notice = reason;
            }
        }
        None
    }
    fn proposal(&self) -> Option<&Value> {
        self.snapshot.as_ref()?["proposals"]
            .as_array()?
            .iter()
            .filter(|p| p["status"] == "queued" || p["status"] == "reviewing")
            .nth(self.selected)
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if !self.open
            || key.kind != KeyEventKind::Press
            || !key.modifiers.is_empty()
            || self.waiting
        {
            return None;
        }
        let request = if let Some(r) = self.review.as_ref() {
            match key.code {
                KeyCode::Enter
                    if self.geometry.is_some()
                        && self.painted_review.as_ref() == Some(r)
                        && !self.seen.is_empty()
                        && self.seen.iter().all(|seen| *seen) =>
                {
                    Some(if self.step {
                        Request::StepApply(r.clone())
                    } else {
                        Request::Apply(r.clone())
                    })
                }
                KeyCode::Char('d' | 'D') | KeyCode::Esc if self.step => {
                    Some(Request::Cancel(json!({"proposal":r["proposal"]})))
                }
                KeyCode::Esc => {
                    self.review = None;
                    None
                }
                KeyCode::Down | KeyCode::PageDown => {
                    self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                    None
                }
                KeyCode::Up | KeyCode::PageUp => {
                    self.page = self.page.saturating_sub(1);
                    None
                }
                _ => None,
            }
        } else {
            match key.code {
                KeyCode::Esc => {
                    self.hide();
                    None
                }
                KeyCode::PageDown => {
                    self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                    None
                }
                KeyCode::PageUp => {
                    self.page = self.page.saturating_sub(1);
                    None
                }
                KeyCode::Char('r' | 'R') => Some(Request::Snapshot),
                KeyCode::Left => {
                    self.view = (self.view + 3) % 4;
                    None
                }
                KeyCode::Right => {
                    self.view = (self.view + 1) % 4;
                    None
                }
                KeyCode::Down => {
                    let count = self
                        .snapshot
                        .as_ref()
                        .and_then(|s| s["proposals"].as_array())
                        .map_or(0, Vec::len);
                    self.selected = (self.selected + 1).min(count.saturating_sub(1));
                    None
                }
                KeyCode::Up => {
                    self.selected = self.selected.saturating_sub(1);
                    None
                }
                KeyCode::Char('e' | 'E') => self
                    .snapshot
                    .as_ref()
                    .map(|s| Request::Configure(s["enabled"] != true)),
                KeyCode::Enter => self.snapshot.as_ref().map(|s| Request::Review {
                    snapshot: s.clone(),
                    action: "open".into(),
                    view: Some(VIEWS[self.view].into()),
                }),
                KeyCode::Char('c' | 'C') => self.snapshot.as_ref().map(|s| Request::Review {
                    snapshot: s.clone(),
                    action: "close".into(),
                    view: None,
                }),
                KeyCode::Char('f' | 'F') => self.snapshot.as_ref().map(|s| Request::Review {
                    snapshot: s.clone(),
                    action: "refresh".into(),
                    view: Some(VIEWS[self.view].into()),
                }),
                KeyCode::Char('p' | 'P') => self.proposal().cloned().map(Request::StepReview),
                KeyCode::Char('d' | 'D') => self.proposal().cloned().map(Request::Cancel),
                _ => None,
            }
        };
        if request.is_some() {
            self.invalidate_paint();
            self.waiting = true;
        }
        request
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open {
            return;
        }
        frame.render_widget(Clear, area);
        let mut content = format!("Sidecar · {}\n{}\n\n", VIEWS[self.view], self.notice);
        if let Some(r) = &self.review {
            if self.step {
                let g = &r["group"];
                content.push_str(&format!(
                    "Review change {} of {} · {}\nScope: {} · Effective: {}\n\n",
                    r["index"].as_u64().unwrap_or(0) + 1,
                    r["count"],
                    g["field"].as_str().unwrap_or_default(),
                    g["scope"].as_str().unwrap_or_default(),
                    g["timing"].as_str().unwrap_or_default()
                ));
                if g["route"] == "job" {
                    content.push_str(&format!("Create scheduled job:\n{}\n", g["value"]));
                } else if let Some(fields) = r["expected"].as_object() {
                    for (field, after) in fields {
                        content.push_str(&format!(
                            "{}\nCurrent: {}\nSave: {}\nDestination: {}\n\n",
                            field,
                            r["before"][field],
                            after,
                            r["destinations"][field].as_str().unwrap_or_default()
                        ));
                    }
                }
                if g["route"] == "defaults" {
                    let field = g["field"].as_str().unwrap_or_default();
                    content.push_str(&format!(
                        "Next request for this model: {}\n",
                        r["review"]["transaction"]["next_request"][field]
                    ));
                    if r["review"]["transaction"]["next_request"][field] != r["expected"][field] {
                        content.push_str("A model override masks this conversation default.\n");
                    }
                }
                if ["reload", "restart", "reconnect"]
                    .contains(&g["timing"].as_str().unwrap_or_default())
                {
                    content.push_str("Saved now; the current runtime keeps its prior value until the stated lifecycle action.\n");
                }
                content.push_str("Each change is saved and verified separately. Earlier saved changes remain if you discard the rest.\nEnter confirm · D discard remaining · PgUp/PgDn review pages");
            } else {
                content.push_str(&format!("Review {} · {}\nExecutable: {}\nRetained PID: {}\n\n{}\n\nEnter confirm · Esc back",
                    r["action"].as_str().unwrap_or_default(), r["view"].as_str().unwrap_or("all views"),
                    r["executable"]["path"].as_str().unwrap_or_default(), r["before"]["pid"],
                    r["consequence"].as_str().unwrap_or_default()));
            }
        } else if let Some(s) = &self.snapshot {
            content.push_str(&format!(
                "Enabled: {}  Running: {}  PID: {}\nExecutable: {}\n{}\n{}\n\n",
                s["enabled"],
                s["process"]["running"],
                s["process"]["pid"],
                s["executable"]["path"].as_str().unwrap_or_default(),
                s["stale"].as_str().unwrap_or_default(),
                s["error"].as_str().unwrap_or_default()
            ));
            for (n, p) in s["proposals"]
                .as_array()
                .into_iter()
                .flatten()
                .filter(|p| p["status"] == "queued" || p["status"] == "reviewing")
                .enumerate()
            {
                let fields = if p["command"] == "job_create" {
                    "Scheduled job".into()
                } else {
                    p["payload"]["changes"]
                        .as_array()
                        .into_iter()
                        .flatten()
                        .filter_map(|c| c["key"].as_str())
                        .collect::<Vec<_>>()
                        .join(", ")
                };
                content.push_str(&format!(
                    "{} {} · {} verified change(s)\n",
                    if n == self.selected { ">" } else { " " },
                    fields,
                    p["index"]
                ));
            }
            content.push_str("\nLeft/Right view · Enter review Open · F review Refresh · C review Close\nE review Enable/Disable · P review selected proposal · D discard · R inspect · Esc back");
        }
        frame.render_widget(
            Block::bordered()
                .title(" Optional Sidecar ")
                .style(Style::default().fg(theme.text()).bg(theme.screen())),
            area,
        );
        let body = area.inner(Margin::new(1, 1));
        if body.width < 20 || body.height < 5 {
            self.invalidate_paint();
            return;
        }
        let lines = crate::ui::wrap(&crate::ui::display_safe(&content), usize::from(body.width));
        let height = usize::from(body.height - 1);
        self.pages = lines.len().div_ceil(height).max(1);
        if self.geometry != Some(body)
            || self.painted_review != self.review
            || self.seen.len() != self.pages
        {
            self.geometry = Some(body);
            self.painted_review = self.review.clone();
            self.seen = vec![false; self.pages];
        }
        self.page = self.page.min(self.pages - 1);
        self.seen[self.page] = true;
        frame.render_widget(
            Paragraph::new(
                lines
                    .iter()
                    .skip(self.page * height)
                    .take(height)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n"),
            )
            .style(Style::default().fg(theme.text()).bg(theme.screen())),
            Rect::new(body.x, body.y, body.width, body.height - 1),
        );
        let footer = if self.review.is_some() {
            format!(
                "Review page {}/{} · PgUp/PgDn · Enter apply",
                self.page + 1,
                self.pages
            )
        } else {
            format!("Page {}/{} · PgUp/PgDn", self.page + 1, self.pages)
        };
        frame.render_widget(
            Paragraph::new(footer),
            Rect::new(body.x, body.bottom() - 1, body.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn scalar() -> (Value, Value) {
        let path = std::env::temp_dir().join("sidecar-owned-settings.json");
        let proposal = json!({"proposal":"a".repeat(64),"payload":{"changes":[
            {"key":"show_stop_time","value":true,"scope":"device"}]}});
        let review = json!({"kind":"sidecar_step_review","proposal":"a".repeat(64),"index":0,"count":1,
            "group":{"route":"presentation","field":"show_stop_time","value":true,"scope":"device","timing":"immediate"},
            "before":{"show_stop_time":false},"expected":{"show_stop_time":true},
            "destinations":{"show_stop_time":path},
            "command":{"type":"gui.presentation.native_review"}});
        (review, proposal)
    }

    #[test]
    fn scalar_review_rejects_changed_or_extra_saved_values() {
        let (review, proposal) = scalar();
        assert!(step_review(&review, Some(&proposal), None).is_ok());
        let mut wrong = review.clone();
        wrong["expected"]["show_stop_time"] = json!(false);
        assert!(step_review(&wrong, Some(&proposal), None).is_err());
        let mut extra = review.clone();
        extra["expected"]["other"] = json!(1);
        extra["before"]["other"] = json!(0);
        extra["destinations"]["other"] = extra["destinations"]["show_stop_time"].clone();
        assert!(step_review(&extra, Some(&proposal), None).is_err());
        let mut destination = review.clone();
        destination["destinations"]["show_stop_time"] = json!("relative.json");
        assert!(step_review(&destination, Some(&proposal), None).is_err());
    }

    #[test]
    fn tool_selection_must_advance_toward_the_original_desktop_list() {
        let (mut review, mut proposal) = scalar();
        proposal["payload"]["changes"] =
            json!([{"key":"tools_disabled","value":["read_file"],"scope":"conversation"}]);
        review["group"] = json!({"route":"runtime","field":"tools_disabled","value":{"name":"read_file","enabled":false},
            "scope":"conversation","timing":"immediate","expected":["read_file"]});
        let path = review["destinations"]["show_stop_time"].clone();
        review["destinations"] = json!({"tools_disabled":path});
        review["before"] = json!({"tools_disabled":[]});
        review["expected"] = json!({"tools_disabled":["read_file"]});
        review["command"]["type"] = json!("gui.runtime.native_validate");
        assert!(step_review(&review, Some(&proposal), None).is_ok());
        review["group"]["value"]["enabled"] = json!(true);
        assert!(step_review(&review, Some(&proposal), None).is_err());
    }

    #[test]
    fn review_screen_explains_destination_without_protocol_tickets() {
        let (review, _) = scalar();
        let mut view = View::default();
        view.update(Ok(Update::StepReview(review)));
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(100, 32)).unwrap();
        terminal
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        let screen = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(
            screen.contains("Destination:")
                && screen.contains("Current: false")
                && screen.contains("Save: true")
        );
        assert!(!screen.contains("gui.presentation") && !screen.contains("sidecar_step_review"));
    }
    fn press(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, crossterm::event::KeyModifiers::NONE)
    }
    fn paint(view: &mut View, width: u16, height: u16) -> String {
        let mut terminal =
            ratatui::Terminal::new(ratatui::backend::TestBackend::new(width, height)).unwrap();
        terminal
            .draw(|f| view.draw(f, f.area(), &crate::theme::builtin_themes()[0]))
            .unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect()
    }
    fn paint_all(view: &mut View, width: u16, height: u16) {
        paint(view, width, height);
        for _ in 0..view.pages {
            view.key(press(KeyCode::PageDown));
            paint(view, width, height);
        }
        // The first page must also be revisited if geometry changed on a later page.
        for _ in 0..view.pages {
            view.key(press(KeyCode::PageUp));
            paint(view, width, height);
        }
    }

    #[test]
    fn step_apply_requires_all_painted_pages_and_fresh_unmodified_press() {
        let (mut review, _) = scalar();
        review["destinations"]["show_stop_time"] = json!(format!(
            "C:/{}settings.json",
            "long-destination/".repeat(18)
        ));
        let mut view = View::default();
        view.update(Ok(Update::StepReview(review)));
        assert!(view.key(press(KeyCode::Enter)).is_none());
        let first = paint(&mut view, 40, 10);
        assert!(!first.contains("settings.json"));
        assert!(view.pages > 3);
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint_all(&mut view, 40, 10);
        let mut repeat = press(KeyCode::Enter);
        repeat.kind = KeyEventKind::Repeat;
        assert!(view.key(repeat).is_none());
        let mut modified = press(KeyCode::Enter);
        modified.modifiers = crossterm::event::KeyModifiers::CONTROL;
        assert!(view.key(modified).is_none());
        assert!(matches!(
            view.key(press(KeyCode::Enter)),
            Some(Request::StepApply(_))
        ));
        assert!(view.key(press(KeyCode::Enter)).is_none());
    }

    #[test]
    fn painted_review_is_invalidated_by_resize_replacement_hide_and_reopen() {
        let (review, _) = scalar();
        let mut view = View::default();
        view.update(Ok(Update::StepReview(review.clone())));
        paint_all(&mut view, 40, 10);
        paint(&mut view, 41, 10);
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint_all(&mut view, 41, 10);
        let mut replaced = review.clone();
        replaced["proposal"] = json!("b".repeat(64));
        view.review = Some(replaced);
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint(&mut view, 41, 10);
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint_all(&mut view, 41, 10);
        view.hide();
        assert!(view.key(press(KeyCode::Enter)).is_none());
        view.open = true;
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint_all(&mut view, 41, 10);
        view.update(Ok(Update::StepReview(review)));
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint_all(&mut view, 41, 10);
        paint(&mut view, 10, 4);
        assert!(view.key(press(KeyCode::Enter)).is_none());
        view.open("settings");
        assert!(view.review.is_none());
        assert!(view.key(press(KeyCode::Enter)).is_none());
    }

    #[test]
    fn exact_close_cleanup_is_reachable_after_paint_and_cancel_needs_no_paint() {
        let mut view = View::default();
        let review = json!({"action":"close", "view":null,
            "executable":{"path":"C:/sidecar.exe"}, "before":{"pid":1234},
            "consequence":"Close and join this exact retained child."});
        view.update(Ok(Update::Review(review.clone())));
        assert!(view.key(press(KeyCode::Enter)).is_none());
        paint_all(&mut view, 40, 10);
        assert!(matches!(view.key(press(KeyCode::Enter)), Some(Request::Apply(r)) if r == review));
        let (step, _) = scalar();
        view.update(Ok(Update::StepReview(step)));
        assert!(matches!(
            view.key(press(KeyCode::Esc)),
            Some(Request::Cancel(_))
        ));
    }
}
