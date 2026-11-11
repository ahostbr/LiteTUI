//! Read-only requested/effective settings inspection. Source owns every value.
use crate::{live::terminal_text, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseButton, MouseEvent, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Modifier, Style},
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::Value;
use std::{collections::HashSet, io};

const MAX_BYTES: usize = 256 * 1024;
const MAX_VALUE: usize = 16 * 1024;

#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub name: String,
    pub label: String,
    pub value_type: String,
    pub source: String,
    pub read_only: bool,
    pub secret: bool,
    pub apply: String,
    pub requested: String,
    pub effective: String,
    pub deferred: bool,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub session: String,
    pub rows: Vec<Row>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Intent {
    Glassbox(Box<crate::glassbox_controls::Request>),
    Monitor(Box<crate::monitor_controls::Request>),
    Mcp(Box<crate::mcp_config::Request>),
    McpLifecycle(Box<crate::mcp_lifecycle::Request>),
    ModelPreferences(Box<crate::model_preferences::Request>),
    ModelLifecycle(Box<crate::model_lifecycle::Request>),
    Voice(Box<crate::voice_preferences::Request>),
    Presentation(Box<crate::presentation_preferences::Request>),
    Connection(Box<crate::connection_preferences::Request>),
    Agent(Box<crate::agent_preferences::Request>),
    RuntimeStatus,
    Read,
    Editor(crate::settings_editor::Intent),
    Compaction(crate::compaction_settings::Intent),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Glassbox(Result<Box<crate::glassbox_controls::Update>, String>),
    Monitor(Result<Box<crate::monitor_controls::Update>, String>),
    Mcp(Result<Box<crate::mcp_config::Update>, String>),
    McpLifecycle(Result<Box<crate::mcp_lifecycle::Update>, String>),
    ModelPreferences(Result<Box<crate::model_preferences::Update>, String>),
    ModelLifecycle(Result<Box<crate::model_lifecycle::Update>, String>),
    Voice(Result<Box<crate::voice_preferences::Update>, String>),
    Presentation(Result<Box<crate::presentation_preferences::Update>, String>),
    Connection(Result<Box<crate::connection_preferences::Update>, String>),
    Agent(Result<Box<crate::agent_preferences::Update>, String>),
    RuntimeStatus(Result<Box<crate::inspection::Snapshot>, String>),
    Snapshot(Snapshot),
    Rejected(String),
    Editor(crate::settings_editor::Update),
    Compaction(crate::compaction_settings::Update),
    CompactionEdited {
        update: crate::compaction_settings::Update,
        inspection: Snapshot,
    },
    Edited {
        update: crate::settings_editor::Update,
        inspection: Snapshot,
    },
}

impl Update {
    /// Verified effective interface preferences the renderer should adopt, if this carries any.
    pub fn presentation(&self) -> Option<crate::presentation::Presentation> {
        match self {
            Self::Presentation(Ok(update)) => update.effective(),
            _ => None,
        }
    }
}

fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text<'a>(value: &'a Value, name: &str, limit: usize) -> io::Result<&'a str> {
    value
        .get(name)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= limit)
        .ok_or_else(|| fail(&format!("Invalid settings {name}")))
}
fn value_text(value: &Value) -> io::Result<String> {
    let rendered = serde_json::to_string(value)?;
    if rendered.len() > MAX_VALUE {
        return Err(fail("Setting value exceeds inspection budget"));
    }
    Ok(rendered)
}
impl Snapshot {
    /// Management must first validate the fresh idle state's ownership and policy.
    pub fn parse(state: &Value, session: &str) -> io::Result<Self> {
        if text(state, "session_id", 128)? != session {
            return Err(fail("Settings session changed"));
        }
        let settings = state
            .get("settings")
            .ok_or_else(|| fail("Missing settings snapshot"))?;
        if serde_json::to_vec(settings)?.len() > MAX_BYTES {
            return Err(fail("Settings snapshot exceeds byte budget"));
        }
        let metadata = settings
            .get("metadata")
            .and_then(Value::as_array)
            .filter(|rows| !rows.is_empty() && rows.len() <= 512)
            .ok_or_else(|| fail("Invalid settings metadata"))?;
        let requested = settings
            .get("requested")
            .and_then(Value::as_object)
            .ok_or_else(|| fail("Invalid requested settings"))?;
        let effective = settings
            .get("effective")
            .and_then(Value::as_object)
            .ok_or_else(|| fail("Invalid effective settings"))?;
        let deferred = settings
            .get("deferred")
            .and_then(Value::as_object)
            .ok_or_else(|| fail("Invalid deferred settings"))?;
        let mut seen = HashSet::new();
        let mut rows = Vec::new();
        for field in metadata {
            let name = text(field, "name", 128)?;
            if !name
                .bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
                || !seen.insert(name)
            {
                return Err(fail("Invalid or duplicate setting name"));
            }
            let apply = text(field, "apply", 16)?;
            if !matches!(
                apply,
                "immediate" | "next_turn" | "reload" | "reconnect" | "restart"
            ) {
                return Err(fail("Unknown setting application mode"));
            }
            let request = requested
                .get(name)
                .ok_or_else(|| fail("Requested setting is missing"))?;
            let effect = effective
                .get(name)
                .ok_or_else(|| fail("Effective setting is missing"))?;
            let secret = field
                .get("secret")
                .and_then(Value::as_bool)
                .ok_or_else(|| fail("Missing setting sensitivity metadata"))?;
            let render = |value: &Value| -> io::Result<String> {
                if secret {
                    let value = value
                        .as_str()
                        .filter(|s| s.len() <= MAX_VALUE)
                        .ok_or_else(|| fail("Invalid secret setting value"))?;
                    Ok(if value.is_empty() {
                        "Not set"
                    } else {
                        "Set (hidden)"
                    }
                    .to_owned())
                } else {
                    value_text(value)
                }
            };
            let differs = request != effect;
            if differs {
                if matches!(apply, "immediate" | "next_turn")
                    || deferred.get(name).and_then(Value::as_str) != Some(apply)
                {
                    return Err(fail(
                        "Deferred setting disagrees with requested/effective values",
                    ));
                }
            } else if deferred.contains_key(name) {
                return Err(fail("Unchanged setting cannot be deferred"));
            }
            rows.push(Row {
                name: name.to_owned(),
                label: text(field, "label", 1024)?.to_owned(),
                value_type: text(field, "type", 1024)?.to_owned(),
                source: text(field, "source", 1024)?.to_owned(),
                read_only: field
                    .get("read_only")
                    .and_then(Value::as_bool)
                    .ok_or_else(|| fail("Invalid setting authority"))?,
                secret,
                apply: apply.to_owned(),
                requested: render(request)?,
                effective: render(effect)?,
                deferred: differs,
            });
        }
        if requested.len() != seen.len()
            || effective.len() != seen.len()
            || deferred.keys().any(|name| !seen.contains(name.as_str()))
        {
            return Err(fail("Settings values and metadata disagree"));
        }
        Ok(Self {
            session: session.to_owned(),
            rows,
        })
    }
}
fn display(value: &str) -> String {
    terminal_text(value)
        .replace('\n', "\\n")
        .replace('\t', "\\t")
}

#[derive(Default)]
pub struct View {
    glassbox: crate::glassbox_controls::View,
    monitor: crate::monitor_controls::View,
    runtime_status: crate::inspection::View,
    mcp: crate::mcp_config::View,
    mcp_lifecycle: crate::mcp_lifecycle::View,
    mcp_read_only: bool,
    model_preferences: crate::model_preferences::View,
    model_lifecycle: crate::model_lifecycle::View,
    voice: crate::voice_preferences::View,
    presentation: crate::presentation_preferences::View,
    connection: crate::connection_preferences::View,
    agent: crate::agent_preferences::View,
    pub editor: crate::settings_editor::View,
    pub compaction: crate::compaction_settings::View,
    pub open: bool,
    pub waiting: bool,
    pub snapshot: Option<Snapshot>,
    pub notice: String,
    lifecycle_command: Option<(String, String)>,
    selected: usize,
    scroll: usize,
    max_scroll: usize,
    intent: Option<Intent>,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<(Rect, usize)>,
    details: Option<Rect>,
    pressed: Option<usize>,
}
impl View {
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.intent = Some(Intent::Read);
        self.notice = "Reading current settings…".into();
        self.hide();
    }
    pub(crate) fn open_monitor(&mut self, arguments: &str) {
        self.hide();
        self.monitor.open(arguments);
        self.waiting = true;
        self.intent = Some(Intent::Monitor(Box::new(
            crate::monitor_controls::Request::Snapshot(arguments.into()),
        )));
    }
    pub(crate) fn open_glassbox(&mut self, action: Option<&str>) {
        self.hide();
        self.glassbox.open();
        self.waiting = true;
        self.intent = Some(Intent::Glassbox(Box::new(match action {
            Some(action) => crate::glassbox_controls::Request::Review(action.into()),
            None => crate::glassbox_controls::Request::Snapshot,
        })));
    }
    /// `/mcp` opens the same declarations panel as `M`; `list` keeps it inspection-only.
    pub(crate) fn open_mcp(&mut self, read_only: bool) {
        self.hide();
        self.mcp.open();
        self.mcp_read_only = read_only;
        self.waiting = true;
        self.intent = Some(Intent::Mcp(Box::new(crate::mcp_config::Request::List)));
    }
    /// The `U` interface preferences, with the cursor on `field`; review and apply are unchanged.
    pub(crate) fn open_presentation(&mut self, field: &str) {
        self.hide();
        self.presentation.open_field(field);
        self.waiting = true;
        self.intent = Some(Intent::Presentation(Box::new(
            crate::presentation_preferences::Request::List,
        )));
    }
    pub(crate) fn open_model_preferences(&mut self) {
        self.hide();
        self.model_preferences.open();
        self.waiting = true;
        self.intent = Some(Intent::ModelPreferences(Box::new(
            crate::model_preferences::Request::List,
        )));
    }
    /// `/claude status` shows the read-only runtime inspector, as `I` does.
    pub(crate) fn open_runtime_status(&mut self) {
        self.hide();
        self.runtime_status.open();
        self.waiting = true;
        self.intent = Some(Intent::RuntimeStatus);
    }
    pub(crate) fn open_model_lifecycle(&mut self, action: Option<(&str, &str)>) {
        self.model_lifecycle.open();
        self.lifecycle_command =
            action.map(|(action, model)| (action.to_owned(), model.to_owned()));
        self.waiting = true;
        self.intent = Some(Intent::ModelLifecycle(Box::new(
            match action.filter(|(_, model)| !model.is_empty()) {
                Some((_, model)) => {
                    crate::model_lifecycle::Request::Inspect(model.into(), Default::default())
                }
                None => crate::model_lifecycle::Request::List,
            },
        )));
        self.hide();
    }
    pub(crate) fn open_agent(&mut self, focus: Option<crate::agent_preferences::Focus>) {
        self.agent.open(focus);
        self.waiting = true;
        self.intent = Some(Intent::Agent(Box::new(
            crate::agent_preferences::Request::List,
        )));
        self.hide();
    }
    pub fn runtime_status_open(&self) -> bool {
        self.runtime_status.is_open()
    }
    pub fn subpanel_open(&self) -> bool {
        self.runtime_status_open()
            || self.mcp.open
            || self.mcp_lifecycle.open
            || self.voice.open
            || self.presentation.open
            || self.connection.open
            || self.agent.open
            || self.model_preferences.open
            || self.glassbox.open
            || self.monitor.open
            || self.model_lifecycle.open
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting || self.editor.owns_input() || self.compaction.owns_input()
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.editor
            .take_intent()
            .map(Intent::Editor)
            .or_else(|| self.compaction.take_intent().map(Intent::Compaction))
            .or_else(|| self.intent.take())
    }
    pub fn hide(&mut self) {
        self.glassbox.hide();
        self.monitor.hide();
        self.mcp.hide();
        self.mcp_lifecycle.hide();
        self.model_preferences.hide();
        self.model_lifecycle.hide();
        self.voice.hide();
        self.presentation.hide();
        self.connection.hide();
        self.agent.hide();
        self.runtime_status.hide();
        self.editor.hide();
        self.compaction.hide();
        self.visible = false;
        self.hits.clear();
        self.details = None;
        self.pressed = None;
    }
    pub fn dictation_key(&self, key: KeyEvent) -> bool {
        self.voice.dictation_key(key)
    }
    pub fn close(&mut self) {
        self.glassbox = Default::default();
        self.monitor = Default::default();
        self.mcp = Default::default();
        self.mcp_read_only = false;
        self.mcp_lifecycle.close();
        self.model_preferences = Default::default();
        self.model_lifecycle = Default::default();
        self.voice = Default::default();
        self.presentation = Default::default();
        self.connection = Default::default();
        self.agent = Default::default();
        self.runtime_status.close();
        self.editor.close();
        self.compaction.close();
        self.open = false;
        if self.intent.take().is_some() {
            self.waiting = false;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        self.editor.finish();
        self.compaction.finish();
        self.close();
        self.waiting = false;
        self.snapshot = None;
    }
    pub fn accepts_paste(&self) -> bool {
        self.mcp.accepts_paste()
            || self.editor.editing()
            || self.compaction.accepts_paste()
            || self.connection.accepts_paste()
            || self.voice.accepts_paste()
    }
    pub fn paste(&mut self, text: &str) {
        if self.mcp.open {
            self.mcp.paste(text);
        } else if self.voice.accepts_paste() {
            self.voice.paste(text);
        } else if self.connection.accepts_paste() {
            self.connection.paste(text);
        } else if self.compaction.owns_input() {
            self.compaction.paste(text);
        } else {
            self.editor.paste(text);
        }
    }
    pub fn clipboard_error(&mut self, error: String) {
        if self.voice.accepts_paste() {
            self.voice.clipboard_error(error);
        } else if self.compaction.owns_input() {
            self.compaction.notice = error;
        } else {
            self.editor.notice = error;
        }
    }
    pub fn observe(&mut self, update: Update) {
        if let Update::CompactionEdited { update, inspection } = update {
            self.observe(Update::Snapshot(inspection));
            self.compaction.observe(update);
            self.notice = self.compaction.notice.clone();
            return;
        }
        if let Update::Compaction(update) = update {
            self.compaction.observe(update);
            self.notice = self.compaction.notice.clone();
            return;
        }
        if let Update::Edited { update, inspection } = update {
            self.observe(Update::Snapshot(inspection));
            self.editor.observe(update);
            self.notice = self.editor.notice.clone();
            return;
        }
        if let Update::Editor(update) = update {
            self.editor.observe(update);
            self.notice = self.editor.notice.clone();
            return;
        }
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.scroll = 0;
        match update {
            Update::ModelPreferences(result) => {
                self.model_preferences.update(result.map(|update| *update))
            }
            Update::ModelLifecycle(result) => {
                self.model_lifecycle.update(result.map(|update| *update));
                if let Some((action, model)) = self.lifecycle_command.take()
                    && let Some(request) = self.model_lifecycle.review_action(&action, &model)
                {
                    self.waiting = true;
                    self.intent = Some(Intent::ModelLifecycle(Box::new(request)));
                }
            }
            Update::Voice(result) => self.voice.update(result.map(|update| *update)),
            Update::Presentation(result) => self.presentation.update(result.map(|update| *update)),
            Update::Connection(result) => self.connection.update(result.map(|update| *update)),
            Update::Agent(result) => self.agent.update(result.map(|update| *update)),
            Update::Mcp(result) => self.mcp.update(result.map(|update| *update)),
            Update::McpLifecycle(result) => self.mcp_lifecycle.update(result.map(|update| *update)),
            Update::Glassbox(result) => self.glassbox.update(result.map(|update| *update)),
            Update::Monitor(result) => {
                if let Some(request) = self.monitor.update(result.map(|update| *update)) {
                    self.waiting = true;
                    self.intent = Some(Intent::Monitor(Box::new(request)));
                }
            }
            Update::RuntimeStatus(status) => {
                if self.runtime_status.is_open() {
                    match status {
                        Ok(snapshot) => self.runtime_status.apply(*snapshot),
                        Err(reason) => self.runtime_status.reject(&reason),
                    }
                }
            }
            Update::Editor(_)
            | Update::Edited { .. }
            | Update::Compaction(_)
            | Update::CompactionEdited { .. } => unreachable!("handled above"),
            Update::Snapshot(snapshot) => {
                let previous = self
                    .snapshot
                    .as_ref()
                    .and_then(|s| s.rows.get(self.selected))
                    .map(|r| &r.name);
                self.selected = previous
                    .and_then(|name| snapshot.rows.iter().position(|r| &r.name == name))
                    .unwrap_or(0);
                self.snapshot = Some(snapshot);
                self.notice =
                    "Read-only snapshot. Requested values may need reconnect or restart.".into();
            }
            Update::Rejected(reason) => {
                self.snapshot = None;
                self.notice = reason;
            }
        }
    }
    fn select(&mut self, index: usize) {
        self.selected = index.min(
            self.snapshot
                .as_ref()
                .map_or(0, |s| s.rows.len().saturating_sub(1)),
        );
        self.scroll = 0;
        self.max_scroll = 0;
        self.pressed = None;
    }
    pub fn key(&mut self, key: KeyEvent) {
        if self.monitor.open {
            if let Some(request) = self.monitor.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Monitor(Box::new(request)));
            }
            return;
        }
        if self.model_preferences.open {
            if let Some(request) = self.model_preferences.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::ModelPreferences(Box::new(request)));
            }
            return;
        }
        if self.glassbox.open {
            if let Some(request) = self.glassbox.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Glassbox(Box::new(request)));
            }
            return;
        }
        if self.model_lifecycle.open {
            if let Some(request) = self.model_lifecycle.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::ModelLifecycle(Box::new(request)));
            }
            return;
        }
        if self.voice.open {
            if let Some(request) = self.voice.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Voice(Box::new(request)));
            }
            return;
        }
        if self.presentation.open {
            if let Some(request) = self.presentation.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Presentation(Box::new(request)));
            }
            return;
        }
        if self.connection.open {
            if let Some(request) = self.connection.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Connection(Box::new(request)));
            }
            return;
        }
        if self.agent.open {
            if let Some(request) = self.agent.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Agent(Box::new(request)));
            }
            return;
        }
        if self.mcp_lifecycle.open {
            if key.kind == KeyEventKind::Press
                && key.modifiers.is_empty()
                && key.code == KeyCode::Tab
                && self.mcp_lifecycle.can_switch()
            {
                self.mcp_lifecycle.close();
                self.mcp.open();
                self.waiting = true;
                self.intent = Some(Intent::Mcp(Box::new(crate::mcp_config::Request::List)));
            } else if let Some(request) = self.mcp_lifecycle.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::McpLifecycle(Box::new(request)));
            }
            return;
        }
        if self.mcp.open {
            // `/mcp list`: refresh, scroll and leave only; no review or runtime tab.
            if self.mcp_read_only
                && !matches!(
                    key.code,
                    KeyCode::Esc | KeyCode::Up | KeyCode::Down | KeyCode::F(5)
                )
            {
                return;
            }
            if key.kind == KeyEventKind::Press
                && key.modifiers.is_empty()
                && key.code == KeyCode::Tab
                && self.mcp.can_switch()
            {
                self.mcp = Default::default();
                self.mcp_lifecycle.open();
                self.waiting = true;
                self.intent = Some(Intent::McpLifecycle(Box::new(
                    crate::mcp_lifecycle::Request::List,
                )));
                return;
            }
            if let Some(request) = self.mcp.key(key) {
                self.waiting = true;
                self.intent = Some(Intent::Mcp(Box::new(request)));
            }
            return;
        }
        if self.runtime_status.is_open() {
            if self.runtime_status.key(key) && !self.waiting {
                self.waiting = true;
                self.intent = Some(Intent::RuntimeStatus);
            }
            return;
        }
        if self.compaction.owns_input() {
            self.compaction.key(key);
            return;
        }
        if self.editor.owns_input() {
            self.editor.key(key);
            return;
        }
        if key.kind == KeyEventKind::Release || !key.modifiers.is_empty() {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            self.close();
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        self.pressed = None;
        match key.code {
            KeyCode::Up => self.select(self.selected.saturating_sub(1)),
            KeyCode::Down => self.select(self.selected.saturating_add(1)),
            KeyCode::Home => self.select(0),
            KeyCode::End => self.select(usize::MAX),
            KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(4),
            KeyCode::PageDown => self.scroll = self.scroll.saturating_add(4).min(self.max_scroll),
            KeyCode::Char('p') if key.kind == KeyEventKind::Press => {
                self.model_preferences.open();
                self.waiting = true;
                self.intent = Some(Intent::ModelPreferences(Box::new(
                    crate::model_preferences::Request::List,
                )));
                self.hide();
            }
            KeyCode::Char('g') if key.kind == KeyEventKind::Press => {
                self.open_glassbox(None);
            }
            KeyCode::Char('l') if key.kind == KeyEventKind::Press => {
                self.open_model_lifecycle(None);
            }
            KeyCode::Char('a') if key.kind == KeyEventKind::Press => self.open_agent(None),
            KeyCode::Char('v') if key.kind == KeyEventKind::Press => {
                self.voice.open();
                self.waiting = true;
                self.intent = Some(Intent::Voice(Box::new(
                    crate::voice_preferences::Request::List,
                )));
                self.hide();
            }
            KeyCode::Char('u') if key.kind == KeyEventKind::Press => {
                self.presentation.open();
                self.waiting = true;
                self.intent = Some(Intent::Presentation(Box::new(
                    crate::presentation_preferences::Request::List,
                )));
                self.hide();
            }
            KeyCode::Char('n') if key.kind == KeyEventKind::Press => {
                self.connection.open();
                self.waiting = true;
                self.intent = Some(Intent::Connection(Box::new(
                    crate::connection_preferences::Request::List,
                )));
                self.hide();
            }
            KeyCode::Char('m') if key.kind == KeyEventKind::Press => {
                self.mcp.open();
                self.mcp_read_only = false;
                self.waiting = true;
                self.intent = Some(Intent::Mcp(Box::new(crate::mcp_config::Request::List)));
                self.hide();
            }
            KeyCode::Char('i') if key.kind == KeyEventKind::Press => {
                self.runtime_status.open();
                self.waiting = true;
                self.intent = Some(Intent::RuntimeStatus);
                self.hide();
            }
            KeyCode::Char('r') if key.kind == KeyEventKind::Press => self.open(),
            KeyCode::Char('c') if key.kind == KeyEventKind::Press => self.compaction.open(),
            KeyCode::Char('e') if key.kind == KeyEventKind::Press => {
                if let Some(row) = self
                    .snapshot
                    .as_ref()
                    .and_then(|s| s.rows.get(self.selected))
                {
                    if crate::settings_editor::FIELDS.contains(&row.name.as_str()) {
                        self.editor.open(&row.name);
                    } else if crate::compaction_settings::FIELDS.contains(&row.name.as_str()) {
                        self.compaction.open_field(&row.name);
                    } else if crate::agent_preferences::FIELDS.contains(&row.name.as_str()) {
                        let name = row.name.clone();
                        self.open_agent(None);
                        self.agent.open_field(&name);
                    } else {
                        self.notice = "This setting is available for inspection only.".into();
                    }
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if self.mcp.open
            || self.voice.open
            || self.presentation.open
            || self.connection.open
            || self.agent.open
            || self.model_preferences.open
            || self.glassbox.open
            || self.monitor.open
            || self.model_lifecycle.open
        {
            return;
        }
        if self.runtime_status.is_open() {
            self.runtime_status.mouse(event);
            return;
        }
        if self.compaction.owns_input() {
            self.compaction.mouse(event);
            return;
        }
        if self.editor.owns_input() {
            self.editor.mouse(event);
            return;
        }
        if !self.open || !self.visible || self.waiting || !event.modifiers.is_empty() {
            self.pressed = None;
            return;
        }
        let point = (event.column, event.row).into();
        let hit = self
            .hits
            .iter()
            .find(|(rect, _)| rect.contains(point))
            .map(|(_, index)| *index);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take().is_some_and(|old| Some(old) == hit) {
                    self.select(hit.expect("paired setting row"));
                }
            }
            MouseEventKind::ScrollUp | MouseEventKind::ScrollDown => {
                self.pressed = None;
                let up = event.kind == MouseEventKind::ScrollUp;
                if self.details.is_some_and(|rect| rect.contains(point)) {
                    self.scroll = if up {
                        self.scroll.saturating_sub(3)
                    } else {
                        self.scroll.saturating_add(3).min(self.max_scroll)
                    };
                } else if hit.is_some() {
                    self.select(if up {
                        self.selected.saturating_sub(1)
                    } else {
                        self.selected.saturating_add(1)
                    });
                }
            }
            _ => {
                self.pressed = None;
            }
        }
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if self.model_preferences.open {
            self.model_preferences.draw(frame, area, theme);
            return;
        }
        if self.monitor.open {
            self.monitor.draw(frame, area, theme);
            return;
        }
        if self.glassbox.open {
            self.glassbox.draw(frame, area, theme);
            return;
        }
        if self.model_lifecycle.open {
            self.model_lifecycle.draw(frame, area, theme);
            return;
        }
        if self.voice.open {
            self.voice.draw(frame, area, theme);
            return;
        }
        if self.presentation.open {
            self.presentation.draw(frame, area, theme);
            return;
        }
        if self.connection.open {
            self.connection.draw(frame, area, theme);
            return;
        }
        if self.agent.open {
            self.agent.draw(frame, area, theme);
            return;
        }
        if self.mcp_lifecycle.open {
            self.mcp_lifecycle.draw(frame, area, theme);
            return;
        }
        if self.mcp.open {
            self.mcp.draw(frame, area, theme);
            return;
        }
        if self.runtime_status.is_open() {
            self.runtime_status.draw(frame, area, theme);
            return;
        }
        if self.compaction.owns_input() {
            self.compaction.draw(frame, area, theme);
            return;
        }
        if self.editor.owns_input() {
            self.editor.draw(frame, area, theme);
            return;
        }
        self.visible = false;
        if !self.owns_input() {
            self.hide();
            return;
        }
        if area.width < 42 || area.height < 18 {
            self.hide();
            if area.width >= 36 && area.height >= 14 {
                let rect = Rect::new(
                    area.x + 1,
                    area.y + (area.height - 6) / 2,
                    area.width - 2,
                    6,
                );
                frame.render_widget(Clear, rect);
                let block = Block::default()
                    .title(" Settings ")
                    .borders(Borders::ALL)
                    .style(Style::default().fg(theme.foreground).bg(theme.background))
                    .border_style(Style::default().fg(theme.title));
                let inner = block.inner(rect);
                frame.render_widget(block, rect);
                frame.render_widget(
                    Paragraph::new("Settings need at least 42×18.\nResize, or Esc to close."),
                    inner,
                );
            }
            return;
        }
        let width = area.width.saturating_sub(2).min(110);
        let height = area.height.saturating_sub(2).min(34);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if self.geometry != Some(rect) {
            self.pressed = None;
            self.geometry = Some(rect);
        }
        self.hits.clear();
        frame.render_widget(Clear, rect);
        let block = Block::default()
            .title(" Settings · inspection ")
            .borders(Borders::ALL)
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let count = self.snapshot.as_ref().map_or(0, |s| s.rows.len());
        let header = format!(
            "{} / {count} · * pending · locked: source",
            if count > 0 { self.selected + 1 } else { 0 }
        );
        frame.render_widget(
            Paragraph::new(header),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let list_height = (inner.height / 3).clamp(2, 8);
        let list = Rect::new(inner.x, inner.y + 1, inner.width, list_height);
        let detail = Rect::new(
            inner.x,
            list.bottom() + 1,
            inner.width,
            inner.height.saturating_sub(list_height + 6),
        );
        self.details = Some(detail);
        if let Some(snapshot) = &self.snapshot {
            let start = self
                .selected
                .saturating_sub(usize::from(list.height).saturating_sub(1));
            for (index, row) in snapshot
                .rows
                .iter()
                .enumerate()
                .skip(start)
                .take(list.height as usize)
            {
                let selected = index == self.selected;
                let line = format!(
                    "{}{} {}{}",
                    if selected { ">" } else { " " },
                    if row.deferred { "*" } else { " " },
                    display(&row.label),
                    if row.read_only { " [locked]" } else { "" }
                );
                let style = Style::default()
                    .fg(if selected {
                        theme.title
                    } else {
                        theme.foreground
                    })
                    .add_modifier(if selected {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    });
                let hit = Rect::new(list.x, list.y + (index - start) as u16, list.width, 1);
                frame.render_widget(Paragraph::new(line).style(style), hit);
                self.hits.push((hit, index));
            }
            if let Some(row) = snapshot.rows.get(self.selected) {
                let fields = [
                    format!("{} ({})", row.label, row.name),
                    format!(
                        "Type: {} | Source: {}{}",
                        row.value_type,
                        row.source,
                        if row.read_only { " (locked)" } else { "" }
                    ),
                    format!(
                        "Applies: {} | {}",
                        row.apply,
                        if row.deferred { "PENDING" } else { "current" }
                    ),
                    format!("Requested: {}", row.requested),
                    format!("Effective: {}", row.effective),
                ];
                let lines: Vec<String> = fields
                    .iter()
                    .flat_map(|text| crate::ui::wrap(&display(text), detail.width as usize))
                    .collect();
                self.max_scroll = lines.len().saturating_sub(detail.height as usize);
                self.scroll = self.scroll.min(self.max_scroll);
                frame.render_widget(
                    Paragraph::new(
                        lines
                            .iter()
                            .skip(self.scroll)
                            .take(detail.height as usize)
                            .cloned()
                            .collect::<Vec<_>>()
                            .join("\n"),
                    ),
                    detail,
                );
            }
        }
        frame.render_widget(
            Paragraph::new(display(&self.notice)),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new("↑↓ setting · PgUp/PgDn details"),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new(
                "P preferences · A agent · L model lifecycle · V voice · U interface · N connections · M MCP · I status · G Glassbox · E edit · C compact · R refresh · Esc back",
            ),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
    }
}
