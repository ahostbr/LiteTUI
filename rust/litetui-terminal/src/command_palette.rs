//! Source-owned command discovery and explicit routes into native controls.
use crate::{
    live::terminal_text,
    prepared_skill::{Draft, Prepared},
    theme::Theme,
};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::HashSet, io};
pub const LIST: &str = "gui.commands.list";
pub const RESOLVE: &str = "gui.commands.resolve";
pub const PREPARE: &str = "gui.skills.native_prepare";
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
fn text(v: &Value, key: &str, max: usize) -> io::Result<String> {
    v[key]
        .as_str()
        .filter(|s| s.len() <= max)
        .map(str::to_owned)
        .ok_or_else(|| fail("Invalid command metadata text"))
}
/// A command argument may only preselect a row in a control that still requires
/// its own review: one bounded line, and no whitespace unless `spaced` (model names).
fn preselection_ok(wanted: &str, max: usize, spaced: bool) -> bool {
    wanted.len() <= max
        && !wanted.chars().any(char::is_control)
        && (spaced || !wanted.contains(char::is_whitespace))
}
pub(crate) fn token(v: &Value, key: &str) -> io::Result<String> {
    let s = text(v, key, 64)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid command identity token"));
    }
    Ok(s)
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Engine,
    Plan,
    ModelLoad,
    ModelUnload,
    ModelReconnect,
    ConversationCompact,
    PluginReload,
    ToolList,
    AgentPreferences(crate::agent_preferences::Focus),
    ChatPause,
    Glassbox,
    Monitor,
    Mark,
    Sidecar,
    Models,
    Thinking,
    Conversations,
    Backend,
    Settings,
    Quit,
    Skills,
    SkillPrepare,
    ConversationNew,
    ConversationRename,
    SystemPrompt,
    Work(crate::work_view::Destination),
    Themes,
    ConversationResume,
    Claude,
    Help,
    Keys,
    Mcp,
    ModelPreferences,
    ClearScreen,
}
impl Action {
    fn parse(v: &Value) -> io::Result<Option<Self>> {
        use crate::work_view::Destination;
        Ok(match v.as_str() {
            None if v.is_null() => None,
            Some("engine") => Some(Self::Engine),
            Some("plan") => Some(Self::Plan),
            Some("model_load") => Some(Self::ModelLoad),
            Some("model_unload") => Some(Self::ModelUnload),
            Some("model_reconnect") => Some(Self::ModelReconnect),
            Some("conversation_compact") => Some(Self::ConversationCompact),
            Some("plugin_reload") => Some(Self::PluginReload),
            Some("hooks") => Some(Self::Work(Destination::Hooks)),
            Some("work_new_job") => Some(Self::Work(Destination::NewJob)),
            Some("tool_list") => Some(Self::ToolList),
            Some("agent_prefs_override") => Some(Self::AgentPreferences(
                crate::agent_preferences::Focus::Override,
            )),
            Some("agent_prefs_global") => Some(Self::AgentPreferences(
                crate::agent_preferences::Focus::Global,
            )),
            Some("chat_pause") => Some(Self::ChatPause),
            Some("glassbox") => Some(Self::Glassbox),
            Some("monitor") => Some(Self::Monitor),
            Some("mark") => Some(Self::Mark),
            Some("sidecar") => Some(Self::Sidecar),
            Some("models") => Some(Self::Models),
            Some("thinking") => Some(Self::Thinking),
            Some("conversations") => Some(Self::Conversations),
            Some("backend") => Some(Self::Backend),
            Some("settings") => Some(Self::Settings),
            Some("quit") => Some(Self::Quit),
            Some("skills") => Some(Self::Skills),
            Some("skill_prepare") => Some(Self::SkillPrepare),
            Some("conversation_new") => Some(Self::ConversationNew),
            Some("conversation_rename") => Some(Self::ConversationRename),
            Some("system_prompt") => Some(Self::SystemPrompt),
            Some("themes") => Some(Self::Themes),
            Some("conversation_resume") => Some(Self::ConversationResume),
            Some("claude") => Some(Self::Claude),
            Some("help") => Some(Self::Help),
            Some("keys") => Some(Self::Keys),
            Some("mcp") => Some(Self::Mcp),
            Some("model_preferences") => Some(Self::ModelPreferences),
            Some("clear_screen") => Some(Self::ClearScreen),
            Some("plugins") => Some(Self::Work(Destination::Plugins)),
            Some("work_tasks") => Some(Self::Work(Destination::Tasks)),
            Some("work_jobs") => Some(Self::Work(Destination::Jobs)),
            Some("work_loops") => Some(Self::Work(Destination::Loops)),
            Some("work_goal") => Some(Self::Work(Destination::Goal)),
            Some("work_calendar") => Some(Self::Work(Destination::Calendar)),
            _ => return Err(fail("Unknown native command action")),
        })
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Entry {
    pub id: String,
    pub kind: String,
    pub owner: String,
    pub title: String,
    pub help: String,
    pub group: String,
    pub group_label: String,
    pub tokens: Vec<String>,
    pub palette: bool,
    pub action: Option<Action>,
    pub arguments: bool,
    pub unavailable: String,
    pub palette_order: Option<usize>,
    pub completion_order: Option<usize>,
    pub skill_order: Option<usize>,
    pub source: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Catalog {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
    pub snapshot: String,
    pub entries: Vec<Entry>,
}
impl Catalog {
    pub fn parse(
        v: &Value,
        session: &str,
        agent: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<Self> {
        if serde_json::to_vec(v)?.len() > 512 * 1024
            || v["version"] != 1
            || v["kind"] != "command_catalog"
            || v["session_id"] != session
            || v["agent_id"] != agent
            || v["backend"] != backend
            || v["model"] != model
        {
            return Err(fail("Command catalog identity, version, or size changed"));
        }
        let rows = v["entries"]
            .as_array()
            .filter(|r| r.len() <= 1024)
            .ok_or_else(|| fail("Command catalog exceeds row budget"))?;
        let mut ids = HashSet::new();
        let mut aliases = HashSet::new();
        let mut palette_ranks = HashSet::new();
        let mut completion_ranks = HashSet::new();
        let mut skill_ranks = HashSet::new();
        let mut entries = Vec::new();
        for row in rows {
            let id = token(row, "id")?;
            if !ids.insert(id.clone()) {
                return Err(fail("Duplicate command row identity"));
            }
            let kind = text(row, "kind", 16)?;
            if !matches!(kind.as_str(), "command" | "palette" | "skill") {
                return Err(fail("Unknown command contribution kind"));
            }
            let tokens = row["tokens"]
                .as_array()
                .filter(|t| t.len() <= 32)
                .ok_or_else(|| fail("Invalid command aliases"))?
                .iter()
                .map(|t| {
                    t.as_str()
                        .filter(|s| {
                            !s.is_empty() && s.len() <= 256 && !s.chars().any(char::is_control)
                        })
                        .map(str::to_owned)
                        .ok_or_else(|| fail("Invalid command token"))
                })
                .collect::<io::Result<Vec<_>>>()?;
            if kind == "command"
                && (tokens.is_empty() || tokens.iter().any(|t| !aliases.insert(t.clone())))
            {
                return Err(fail("Duplicate or missing command aliases"));
            }
            if (kind == "palette" && !tokens.is_empty()) || (kind == "skill" && tokens.len() != 1) {
                return Err(fail("Contribution aliases disagree with kind"));
            }
            let palette = row["palette"]
                .as_bool()
                .ok_or_else(|| fail("Missing palette visibility"))?;
            let rank = |key: &str| -> io::Result<Option<usize>> {
                if row[key].is_null() {
                    Ok(None)
                } else {
                    row[key]
                        .as_u64()
                        .filter(|n| *n < 1024)
                        .map(|n| Some(n as usize))
                        .ok_or_else(|| fail("Invalid command rank"))
                }
            };
            let palette_order = rank("palette_order")?;
            let completion_order = rank("completion_order")?;
            let skill_order = rank("skill_order")?;
            if (kind == "skill") != skill_order.is_some()
                || skill_order.is_some_and(|r| !skill_ranks.insert(r))
            {
                return Err(fail("Invalid skill source rank"));
            }
            if palette != palette_order.is_some()
                || (kind == "command" && completion_order.is_none())
                || (kind == "palette" && completion_order.is_some())
                || palette_order.is_some_and(|r| !palette_ranks.insert(r))
                || completion_order.is_some_and(|r| !completion_ranks.insert(r))
            {
                return Err(fail("Command ordering disagrees with source visibility"));
            }
            if row["order"]
                .as_i64()
                .is_none_or(|n| i32::try_from(n).is_err())
            {
                return Err(fail("Invalid command source order"));
            }
            let action = Action::parse(&row["native_action"])?;
            let arguments = row["accepts_arguments"]
                .as_bool()
                .ok_or_else(|| fail("Missing command argument policy"))?;
            let unavailable = text(row, "unavailable_reason", 4096)?;
            if action.is_some() != unavailable.is_empty()
                || arguments
                    != (matches!(
                        action,
                        Some(
                            Action::SkillPrepare
                                | Action::ConversationRename
                                | Action::SystemPrompt
                                | Action::Glassbox
                                | Action::Monitor
                                | Action::Sidecar
                                | Action::Engine
                                | Action::Plan
                                | Action::ModelLoad
                                | Action::ModelUnload
                                | Action::ConversationCompact
                                | Action::PluginReload
                                | Action::Models
                                | Action::Thinking
                                | Action::Backend
                                | Action::ConversationResume
                                | Action::Claude
                                | Action::Mcp
                        )
                    ) || kind == "command"
                        && matches!(action, Some(Action::Work(dest)) if matches!(dest, crate::work_view::Destination::Tasks | crate::work_view::Destination::Jobs | crate::work_view::Destination::Loops | crate::work_view::Destination::Goal)))
                || (action == Some(Action::SkillPrepare)) != (kind == "skill")
            {
                return Err(fail("Command availability contradicts adapter policy"));
            }
            let source = if kind == "skill" {
                Some(text(row, "source", 256)?)
            } else {
                None
            };
            entries.push(Entry {
                id,
                kind,
                owner: text(row, "owner", 128)?,
                title: text(row, "title", 1024)?,
                help: text(row, "help", 4096)?,
                group: text(row, "group", 128)?,
                group_label: text(row, "group_label", 256)?,
                tokens,
                palette,
                action,
                arguments,
                unavailable,
                palette_order,
                completion_order,
                skill_order,
                source,
            });
        }
        for ranks in [&palette_ranks, &completion_ranks, &skill_ranks] {
            if !(0..ranks.len()).all(|r| ranks.contains(&r)) {
                return Err(fail("Command ranks are not contiguous"));
            }
        }
        Ok(Self {
            session: session.into(),
            agent: agent.into(),
            backend: backend.into(),
            model: model.into(),
            snapshot: token(v, "snapshot")?,
            entries,
        })
    }
    pub fn request(&self) -> Value {
        json!({"version":1,"session_id":self.session,"agent_id":self.agent,"snapshot":self.snapshot})
    }
    pub fn completions(&self) -> Vec<crate::completion::Completion> {
        let mut rows: Vec<_> = self
            .entries
            .iter()
            .filter(|r| r.completion_order.is_some())
            .collect();
        rows.sort_by_key(|r| r.completion_order);
        rows.into_iter()
            .filter_map(|r| {
                r.tokens
                    .first()
                    .and_then(|t| t.strip_prefix('/'))
                    .map(|name| crate::completion::Completion {
                        name: name.into(),
                        description: if r.action.is_some() {
                            r.help.clone()
                        } else {
                            format!("{} · {}", r.help, r.unavailable)
                        },
                        kind: if r.kind == "skill" {
                            crate::completion::CompletionKind::Skill
                        } else {
                            crate::completion::CompletionKind::Command
                        },
                    })
            })
            .collect()
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Route {
    pub row: String,
    pub action: Action,
    pub arguments: String,
}
impl Route {
    pub fn parse(
        v: &Value,
        c: &Catalog,
        row: Option<&str>,
        command: Option<&str>,
        arguments: &str,
    ) -> io::Result<Self> {
        if serde_json::to_vec(v)?.len() > 1024 * 1024
            || v["version"] != 1
            || v["kind"] != "command_route"
            || v["session_id"] != c.session
            || v["agent_id"] != c.agent
            || v["backend"] != c.backend
            || v["model"] != c.model
            || v["snapshot"] != c.snapshot
        {
            return Err(fail("Resolved command identity changed"));
        }
        let id = token(v, "row_id")?;
        if row.is_some_and(|r| r != id) {
            return Err(fail("Resolved command row changed"));
        }
        if let Some(command) = command {
            if v["command"] != command {
                return Err(fail("Resolved command echo differs from typed draft"));
            }
        } else if v.get("command").is_some() || v["arguments"] != arguments {
            return Err(fail("Selected command arguments changed"));
        }
        let entry = c
            .entries
            .iter()
            .find(|e| e.id == id)
            .ok_or_else(|| fail("Resolved command is absent from catalog"))?;
        let action =
            Action::parse(&v["native_action"])?.ok_or_else(|| fail("Unsupported command route"))?;
        let arguments = text(v, "arguments", 65536)?;
        if Some(action) != entry.action || (!arguments.is_empty() && !entry.arguments) {
            return Err(fail("Resolved command contradicts selected adapter"));
        }
        if action == Action::Glassbox
            && !matches!(
                arguments.trim().to_ascii_lowercase().as_str(),
                "" | "status" | "start" | "on" | "stop" | "off"
            )
        {
            return Err(fail("Unknown Glassbox review command argument"));
        }
        let verb = arguments.trim().to_ascii_lowercase();
        match action {
            Action::Engine if !matches!(verb.as_str(), "" | "status" | "start" | "stop") => {
                return Err(fail("Unsupported engine command arguments"));
            }
            Action::Plan if !matches!(verb.as_str(), "" | "on" | "off") => {
                return Err(fail("Unsupported plan command arguments"));
            }
            Action::ModelLoad | Action::ModelUnload
                if !preselection_ok(arguments.trim(), 1024, true) =>
            {
                return Err(fail("Unsupported lifecycle model target"));
            }
            Action::Models
                if !preselection_ok(arguments.trim(), 1024, true)
                    || arguments.trim().parse::<usize>().is_ok_and(|n| n == 0) =>
            {
                return Err(fail("Unsupported model command argument"));
            }
            Action::Thinking if !preselection_ok(arguments.trim(), 64, false) => {
                return Err(fail("Unsupported thinking command argument"));
            }
            Action::Backend
                if !preselection_ok(arguments.trim(), 128, false)
                    || arguments.trim().starts_with("--") =>
            {
                return Err(fail("Unsupported backend command argument"));
            }
            Action::ConversationResume
                if !arguments.trim().is_empty()
                    && (arguments.trim().len() > 36
                        || arguments.trim().bytes().all(|b| b.is_ascii_digit())
                        || !arguments.trim().bytes().all(|b| {
                            b == b'-' || b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
                        })) =>
            {
                return Err(fail(
                    "Resume accepts a lowercase conversation id prefix only",
                ));
            }
            Action::Claude
                if !matches!(
                    verb.as_str(),
                    "" | "status" | "resolve" | "continue" | "new"
                ) =>
            {
                return Err(fail("Unsupported Claude session command argument"));
            }
            Action::Claude if c.backend != "claude" => {
                return Err(fail("Claude session commands require the Claude backend"));
            }
            Action::Mcp if !matches!(verb.as_str(), "" | "list" | "ls" | "status") => {
                return Err(fail("MCP command accepts list only"));
            }
            Action::ConversationCompact => crate::conversation_compact::validate_hint(&arguments)?,
            Action::PluginReload
                if !arguments.is_empty()
                    && (arguments.trim() != arguments
                        || arguments.split_whitespace().count() != 1) =>
            {
                return Err(fail("Plugin refresh requires one exact target"));
            }
            _ => {}
        }
        if let Action::Work(destination) = action
            && !arguments.is_empty()
        {
            use crate::work_view::Destination;
            let verb = arguments.trim();
            let lower = verb.to_ascii_lowercase();
            let inspection = match destination {
                Destination::Tasks => verb == "list",
                Destination::Jobs => matches!(lower.as_str(), "list" | "ls"),
                Destination::Loops => matches!(lower.as_str(), "list" | "ls" | "status"),
                Destination::Goal => matches!(lower.as_str(), "status" | "show"),
                Destination::Calendar
                | Destination::Plugins
                | Destination::Hooks
                | Destination::NewJob => false,
            };
            if !inspection {
                return Err(fail("Work inspection cannot authorize this command"));
            }
        }
        if action == Action::ConversationRename
            && (arguments.len() > 4096 || arguments.contains('\0'))
        {
            return Err(fail("Rename prefill exceeds source title bounds"));
        }
        if action == Action::SystemPrompt
            && (arguments.len() > crate::context::MAX_DOCUMENT_BYTES || arguments.contains('\0'))
        {
            return Err(fail("System prompt prefill exceeds the editor bounds"));
        }
        Ok(Self {
            row: id,
            action,
            arguments,
        })
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum Intent {
    Read {
        skills: bool,
    },
    Raw(String),
    Resolve {
        snapshot: String,
        row: String,
    },
    Prepare {
        snapshot: String,
        row: String,
        arguments: String,
    },
    Submit {
        digest: String,
    },
}
#[derive(Clone, Debug, PartialEq)]
pub enum Update {
    Catalog {
        catalog: Catalog,
        skills: bool,
    },
    Route {
        catalog: Catalog,
        route: Route,
    },
    Prepared {
        catalog: Box<Catalog>,
        prepared: Box<Prepared>,
    },
    Verified(Box<Prepared>),
    Rejected(String),
}
#[derive(Clone, Copy, Default, PartialEq)]
enum Mode {
    #[default]
    Palette,
    Skills,
    Arguments,
    Preview,
}
#[derive(Default)]
pub struct View {
    pub catalog: Option<Catalog>,
    pub prepared: Option<Prepared>,
    pub notice: String,
    pub open: bool,
    pub waiting: bool,
    mode: Mode,
    query: String,
    selected: usize,
    refresh_arguments: bool,
    detail_scroll: usize,
    argument_row: Option<String>,
    arguments: Draft,
    intent: Option<Intent>,
    route: Option<Action>,
    route_arguments: String,
    visible: bool,
    reviewed: bool,
    geometry: Option<Rect>,
    page: usize,
    pages: usize,
    hits: Vec<(Rect, usize)>,
    pressed: Option<usize>,
}
impl View {
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn open(&mut self, skills: bool) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.mode = if skills { Mode::Skills } else { Mode::Palette };
        self.intent = Some(Intent::Read { skills });
        self.query.clear();
        self.selected = 0;
        self.notice = "Reading source command registry…".into();
        self.hide();
    }
    pub fn raw(&mut self, text: &str) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.waiting = true;
        self.intent = Some(Intent::Raw(text.into()));
        self.notice = "Resolving exact command with source…".into();
        self.hide();
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        self.intent.take()
    }
    pub fn take_route_arguments(&mut self) -> String {
        std::mem::take(&mut self.route_arguments)
    }
    pub fn take_route(&mut self) -> Option<Action> {
        self.route.take()
    }
    pub fn accepts_verified(&self, p: &Prepared) -> bool {
        self.open
            && self.waiting
            && self.mode == Mode::Preview
            && self.prepared.as_ref() == Some(p)
            && self.arguments.text() == p.arguments
    }
    pub fn settle_cancelled(&mut self) {
        self.waiting = false;
        self.intent = None;
        self.hide();
    }
    pub fn hide(&mut self) {
        self.visible = false;
        self.reviewed = false;
        self.pressed = None;
        self.hits.clear();
        self.page = 0;
    }
    pub fn close(&mut self) {
        self.open = false;
        if self.intent.take().is_some() {
            self.waiting = false;
        }
        self.hide();
    }
    pub fn finish(&mut self) {
        self.close();
        self.waiting = false;
        self.prepared = None;
        self.catalog = None;
        self.route = None;
        self.route_arguments.clear();
    }
    pub fn accepts_paste(&self) -> bool {
        self.open && self.visible && !self.waiting && self.mode != Mode::Preview
    }
    pub fn editing_arguments(&self) -> bool {
        self.open && self.visible && !self.waiting && self.mode == Mode::Arguments
    }
    pub fn arguments(&self) -> &str {
        self.arguments.text()
    }
    pub fn observe(&mut self, u: Update) {
        let active = self.open;
        self.waiting = false;
        self.intent = None;
        self.hide();
        if !active {
            if let Update::Catalog { catalog, .. } = u {
                self.catalog = Some(catalog);
            }
            return;
        }
        match u {
            Update::Catalog { catalog, skills } => {
                self.catalog = Some(catalog);
                self.mode = if skills { Mode::Skills } else { Mode::Palette };
                self.selected = 0;
                self.notice = "Choose a source entry. Enter opens its native control.".into();
                if self.refresh_arguments {
                    self.refresh_arguments = false;
                    if self.argument_row.as_ref().is_some_and(|id| {
                        self.catalog
                            .as_ref()
                            .unwrap()
                            .entries
                            .iter()
                            .any(|e| e.id == *id && e.kind == "skill")
                    }) {
                        self.mode = Mode::Arguments;
                        self.notice =
                            "Catalog refreshed; arguments retained. Enter prepares a new payload."
                                .into();
                    }
                }
            }
            Update::Route { catalog, route } => {
                self.catalog = Some(catalog);
                if route.action == Action::Skills {
                    self.mode = Mode::Skills;
                    self.query.clear();
                    self.selected = 0;
                } else {
                    self.close();
                    self.route = Some(route.action);
                    self.route_arguments = route.arguments;
                }
            }
            Update::Prepared {
                catalog,
                prepared: p,
            } => {
                self.catalog = Some(*catalog);
                self.argument_row = Some(p.row.clone());
                self.arguments
                    .set(&p.arguments)
                    .expect("bounded prepared arguments");
                self.prepared = Some(*p);
                self.mode = Mode::Preview;
                self.notice =
                    "Prepared only. Review every page; Enter on the final page submits.".into();
            }
            Update::Verified(_) => unreachable!("Chat owns verified prompt submission"),
            Update::Rejected(reason) => {
                self.notice = reason;
                self.reviewed = false;
            }
        }
    }
    fn rows(&self) -> Vec<usize> {
        let Some(c) = &self.catalog else {
            return Vec::new();
        };
        let query = self.query.to_lowercase();
        let mut rows: Vec<_> = c
            .entries
            .iter()
            .enumerate()
            .filter(|(_, r)| {
                if self.mode == Mode::Skills {
                    r.kind == "skill"
                } else {
                    r.palette
                }
            })
            .filter(|(_, r)| {
                format!("{} {} {} {}", r.title, r.help, r.owner, r.tokens.join(" "))
                    .to_lowercase()
                    .contains(&query)
            })
            .map(|(i, _)| i)
            .collect();
        rows.sort_by_key(|i| {
            if self.mode == Mode::Skills {
                c.entries[*i].skill_order
            } else {
                c.entries[*i].palette_order
            }
        });
        rows
    }
    pub fn paste(&mut self, text: &str) {
        if self.editing_arguments() {
            match self.arguments.insert(text) {
                Ok(()) => {
                    self.notice = "Arguments changed; Enter prepares a new exact payload.".into()
                }
                Err(e) => self.notice = e.to_string(),
            }
        } else if self.open
            && self.visible
            && !self.waiting
            && matches!(self.mode, Mode::Palette | Mode::Skills)
            && text.len() + self.query.len() <= 512
            && !text.chars().any(char::is_control)
        {
            self.query.push_str(text);
            self.selected = 0;
        }
        self.reviewed = false;
    }
    fn select(&mut self, index: usize) {
        self.detail_scroll = 0;
        self.selected = index.min(self.rows().len().saturating_sub(1));
        self.pressed = None;
    }
    fn prepare(&mut self) {
        if let (Some(c), Some(row)) = (&self.catalog, &self.argument_row) {
            self.intent = Some(Intent::Prepare {
                snapshot: c.snapshot.clone(),
                row: row.clone(),
                arguments: self.arguments.text().into(),
            });
            self.waiting = true;
            self.notice = "Preparing exact source skill…".into();
            self.hide();
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press && key.modifiers.is_empty() {
            self.close();
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if key.code == KeyCode::F(5)
            && key.kind == KeyEventKind::Press
            && key.modifiers.is_empty()
            && matches!(self.mode, Mode::Arguments | Mode::Preview)
        {
            self.refresh_arguments = true;
            self.waiting = true;
            self.intent = Some(Intent::Read { skills: true });
            self.notice = "Refreshing source catalog; argument draft kept…".into();
            self.hide();
            return;
        }
        if self.mode == Mode::Arguments
            && key.modifiers == KeyModifiers::ALT
            && key.code == KeyCode::Enter
        {
            self.paste("\n");
            return;
        }
        let shifted_text =
            key.modifiers == KeyModifiers::SHIFT && matches!(key.code, KeyCode::Char(_));
        if !key.modifiers.is_empty() && !shifted_text {
            self.reviewed = false;
            return;
        }
        match self.mode {
            Mode::Arguments => match key.code {
                KeyCode::Enter if key.kind == KeyEventKind::Press => self.prepare(),
                KeyCode::Char(c) => self.paste(&c.to_string()),
                other => self.arguments.key(other),
            },
            Mode::Preview => match key.code {
                KeyCode::PageDown if self.reviewed => {
                    self.page = (self.page + 1).min(self.pages.saturating_sub(1));
                    self.reviewed = false;
                }
                KeyCode::PageUp => {
                    self.page = self.page.saturating_sub(1);
                    self.reviewed = false;
                }
                KeyCode::Tab if key.kind == KeyEventKind::Press => {
                    self.mode = Mode::Arguments;
                    self.hide();
                }
                KeyCode::Enter
                    if key.kind == KeyEventKind::Press
                        && self.reviewed
                        && self.page + 1 == self.pages =>
                {
                    if let Some(p) = &self.prepared {
                        self.intent = Some(Intent::Submit {
                            digest: p.digest.clone(),
                        });
                        self.waiting = true;
                        self.notice =
                            "Verifying unchanged source before explicit submission…".into();
                        self.hide();
                    }
                }
                _ => self.reviewed = false,
            },
            Mode::Palette | Mode::Skills => match key.code {
                KeyCode::Up => self.select(self.selected.saturating_sub(1)),
                KeyCode::Down => self.select(self.selected.saturating_add(1)),
                KeyCode::PageUp => self.select(self.selected.saturating_sub(8)),
                KeyCode::PageDown => self.select(self.selected.saturating_add(8)),
                KeyCode::Home => self.select(0),
                KeyCode::End => self.select(usize::MAX),
                KeyCode::Backspace => {
                    self.query.pop();
                    self.selected = 0;
                }
                KeyCode::Right => self.detail_scroll = self.detail_scroll.saturating_add(3),
                KeyCode::Left => self.detail_scroll = self.detail_scroll.saturating_sub(3),
                KeyCode::F(5) if key.kind == KeyEventKind::Press => {
                    self.open(self.mode == Mode::Skills)
                }
                KeyCode::Tab if key.kind == KeyEventKind::Press => {
                    self.mode = if self.mode == Mode::Skills {
                        Mode::Palette
                    } else {
                        Mode::Skills
                    };
                    self.query.clear();
                    self.selected = 0;
                }
                KeyCode::Char(c) => self.paste(&c.to_string()),
                KeyCode::Enter if key.kind == KeyEventKind::Press => {
                    if let Some(i) = self.rows().get(self.selected) {
                        let r = &self.catalog.as_ref().unwrap().entries[*i];
                        if r.action.is_none() {
                            self.notice = r.unavailable.clone();
                        } else if r.kind == "skill" {
                            self.argument_row = Some(r.id.clone());
                            self.mode = Mode::Arguments;
                            self.prepared = None;
                            self.hide();
                        } else {
                            self.intent = Some(Intent::Resolve {
                                snapshot: self.catalog.as_ref().unwrap().snapshot.clone(),
                                row: r.id.clone(),
                            });
                            self.waiting = true;
                            self.hide();
                        }
                    }
                }
                _ => {}
            },
        }
    }
    pub fn mouse(&mut self, e: MouseEvent) {
        let reviewed = self.reviewed;
        self.reviewed = false;
        if !self.open || !self.visible || self.waiting || !e.modifiers.is_empty() {
            self.pressed = None;
            return;
        }
        if self.mode == Mode::Preview {
            match e.kind {
                MouseEventKind::ScrollDown if reviewed => {
                    self.page = (self.page + 1).min(self.pages.saturating_sub(1))
                }
                MouseEventKind::ScrollUp => self.page = self.page.saturating_sub(1),
                _ => {}
            }
            return;
        }
        if self.mode == Mode::Arguments {
            return;
        }
        let hit = self
            .hits
            .iter()
            .find(|(r, _)| r.contains((e.column, e.row).into()))
            .map(|(_, i)| *i);
        match e.kind {
            MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                if self.pressed.take().is_some_and(|p| Some(p) == hit) {
                    self.select(hit.unwrap());
                }
            }
            MouseEventKind::ScrollUp => self.select(self.selected.saturating_sub(3)),
            MouseEventKind::ScrollDown => self.select(self.selected.saturating_add(3)),
            _ => self.pressed = None,
        }
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, theme: &Theme) {
        self.visible = false;
        if !self.owns_input() {
            self.hide();
            return;
        }
        if area.width < 42 || area.height < 18 {
            self.hide();
            if area.width >= 36 && area.height >= 14 {
                let r = Rect::new(area.x + 1, area.y + 2, area.width - 2, 6);
                f.render_widget(Clear, r);
                f.render_widget(
                    Paragraph::new("Commands need 42×18.\nResize, or Esc to return.").block(
                        Block::bordered()
                            .title(" Commands ")
                            .style(Style::default().fg(theme.foreground).bg(theme.background))
                            .border_style(Style::default().fg(theme.title)),
                    ),
                    r,
                );
            }
            return;
        }
        let w = area.width.saturating_sub(2).min(110);
        let h = area.height.saturating_sub(2).min(36);
        let r = Rect::new(
            area.x + (area.width - w) / 2,
            area.y + (area.height - h) / 2,
            w,
            h,
        );
        if self.geometry != Some(r) {
            self.hide();
            self.geometry = Some(r);
        }
        self.hits.clear();
        f.render_widget(Clear, r);
        let block = Block::default()
            .borders(Borders::ALL)
            .title(match self.mode {
                Mode::Palette => " Commands · source registry ",
                Mode::Skills => " Skills · exact source rows ",
                Mode::Arguments => " Skill arguments · lossless draft ",
                Mode::Preview => " Prepared skill · not submitted ",
            })
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(r);
        f.render_widget(block, r);
        let body = Rect::new(
            inner.x,
            inner.y,
            inner.width,
            inner.height.saturating_sub(4),
        );
        let display = |s: &str| terminal_text(s).replace('\t', "\\t").replace('\n', "\\n");
        let footer = match self.mode {
            Mode::Preview => {
                if let Some(p) = &self.prepared {
                    let metadata = format!(
                        "Skill: {}\nSource: {}\nPath: {}\nBytes: {} · SHA256 {}\n\n",
                        display(&p.name),
                        display(&p.source),
                        display(&p.path),
                        p.prompt.len(),
                        p.digest
                    );
                    let lines = crate::ui::wrap(
                        &(metadata + &terminal_text(&p.prompt).replace('\t', "\\t")),
                        body.width as usize,
                    );
                    self.pages = lines.len().div_ceil(body.height as usize).max(1);
                    self.page = self.page.min(self.pages - 1);
                    f.render_widget(
                        Paragraph::new(
                            lines
                                .into_iter()
                                .skip(self.page * body.height as usize)
                                .take(body.height as usize)
                                .collect::<Vec<_>>()
                                .join("\n"),
                        ),
                        body,
                    );
                    self.reviewed = self.open && !self.waiting;
                }
                format!(
                    "Page {}/{} · PgDn · Enter submit",
                    self.page + 1,
                    self.pages
                )
            }
            Mode::Arguments => {
                let before = &self.arguments.text()[..self.arguments.cursor()];
                let content = format!(
                    "{}▏{}",
                    terminal_text(before).replace('\t', "\\t"),
                    terminal_text(&self.arguments.text()[self.arguments.cursor()..])
                        .replace('\t', "\\t")
                );
                let lines = crate::ui::wrap(&content, body.width as usize);
                let caret_line = crate::ui::wrap(
                    &terminal_text(before).replace('\t', "\\t"),
                    body.width as usize,
                )
                .len()
                .saturating_sub(1);
                let start = caret_line.saturating_sub(body.height as usize - 1);
                f.render_widget(
                    Paragraph::new(
                        lines
                            .into_iter()
                            .skip(start)
                            .take(body.height as usize)
                            .collect::<Vec<_>>()
                            .join("\n"),
                    ),
                    body,
                );
                "Enter prepare · Alt+Enter newline".into()
            }
            Mode::Palette | Mode::Skills => {
                let rows = self.rows();
                self.selected = self.selected.min(rows.len().saturating_sub(1));
                let head = format!("Search: {} · {} matches", display(&self.query), rows.len());
                f.render_widget(
                    Paragraph::new(head),
                    Rect::new(body.x, body.y, body.width, 1),
                );
                let height = body.height.saturating_sub(5).max(2);
                let start = self.selected.saturating_sub(height as usize - 1);
                for (pos, index) in rows.iter().enumerate().skip(start).take(height as usize) {
                    let entry = &self.catalog.as_ref().unwrap().entries[*index];
                    let line = format!(
                        "{}{}{}",
                        if pos == self.selected { "> " } else { "  " },
                        display(&entry.title),
                        if entry.action.is_none() {
                            " [unavailable]"
                        } else {
                            ""
                        }
                    );
                    let rect = Rect::new(body.x, body.y + 1 + (pos - start) as u16, body.width, 1);
                    f.render_widget(Paragraph::new(line), rect);
                    self.hits.push((rect, pos));
                }
                if let Some(index) = rows.get(self.selected) {
                    let e = &self.catalog.as_ref().unwrap().entries[*index];
                    let detail = format!(
                        "Owner: {} · {}\n{}\n{}",
                        display(&e.owner),
                        display(e.source.as_deref().unwrap_or(&e.group_label)),
                        display(&e.tokens.join(" ")),
                        display(if e.action.is_none() {
                            &e.unavailable
                        } else {
                            &e.help
                        })
                    );
                    f.render_widget(
                        Paragraph::new(
                            crate::ui::wrap(&detail, body.width as usize)
                                .into_iter()
                                .skip(self.detail_scroll)
                                .take(4)
                                .collect::<Vec<_>>()
                                .join("\n"),
                        ),
                        Rect::new(body.x, body.y + 1 + height, body.width, 4),
                    );
                }
                "↑↓ choose · Enter open · ←→ details".into()
            }
        };
        f.render_widget(
            Paragraph::new(
                crate::ui::wrap(&display(&self.notice), inner.width as usize)
                    .into_iter()
                    .take(2)
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            Rect::new(inner.x, inner.bottom() - 4, inner.width, 2),
        );
        f.render_widget(
            Paragraph::new(footer),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
        );
        f.render_widget(
            Paragraph::new(if self.mode == Mode::Preview {
                "Tab edit args · F5 refresh · Esc cancel"
            } else {
                "Tab commands/skills · F5 refresh · Esc"
            }),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
        self.visible = self.open && !self.waiting;
    }
}
