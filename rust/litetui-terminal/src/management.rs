//! Owned-runtime negotiation and bounded startup facts. A snapshot is not a
//! stream barrier or an authority grant. Only a fresh idle session is admitted.
use crate::model_controls::{Choices, Intent, Kind, Update};
use crate::protocol::{CommandId, Delivery, Notice, WireClient};
use crate::stream::{ExecutionId, Origin};
use serde_json::{Value, json};
use std::{collections::HashSet, io};

mod action;
mod compact;
mod execution;
mod runtime;
mod scheduled;
mod settings;
mod work;
mod work_lifecycle;
pub use compact::{Changed as CompactChanged, Prepared as CompactPrepared};
mod rewrite;
pub use rewrite::{Changed as RewriteChanged, Prepared as RewritePrepared};

const GENERATION: u64 = 1;
// One foreground request, its separately correlated execution Stop, and one
// genuine Work settlement read can overlap. Their controllers each retain one
// slot; increasing correlation capacity does not authorize another mutation.
const MAX_PENDING_REQUESTS: usize = 3;
// These producers call app._rpc_emit. Responses/errors use the direct RPC sink.
// Unknown producers remain fatal rather than being silently discarded.
const MIRRORED: &[&str] = &[
    "ready",
    "model_state",
    "usage",
    "turn_start",
    "turn_refused",
    "turn_end",
    "text_delta",
    "reasoning_delta",
    "native_text_snapshot",
    "native_activity",
    "native_turn_started",
    "native_usage",
    "native_event",
    "tool_approval_requested",
    "tool_approval_closed",
    "user_input_requested",
    "user_input_resolved",
    "submit_refused",
    "model_load_requested",
    "cache_warning",
    "tool_call",
    "generic_tool_requested",
    "generic_tool_returned",
    "rewrite_aftercare",
    "compact_maintenance",
    "compaction",
    "work_producer",
    "work_input",
    "work_admission",
    "work_settled",
    "work_reconciled",
    "work_closing",
    "work_transition_proposed",
];
const IDENTIFIED: &[&str] = &[
    "turn_start",
    "turn_refused",
    "turn_end",
    "text_delta",
    "reasoning_delta",
    "native_text_snapshot",
    "native_activity",
    "native_turn_started",
    "native_usage",
    "native_event",
    "generic_tool_requested",
    "generic_tool_returned",
];

fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text<'a>(value: &'a Value, key: &str, max: usize) -> io::Result<&'a str> {
    value
        .get(key)
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty() && s.len() <= max)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))
}
fn boolean(value: &Value, key: &str) -> io::Result<bool> {
    value
        .get(key)
        .and_then(Value::as_bool)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))
}
fn rows(value: &Value, key: &str) -> io::Result<usize> {
    value
        .get(key)
        .and_then(Value::as_array)
        .filter(|v| v.len() <= 10_000 && v.iter().all(Value::is_object))
        .map(Vec::len)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))
}
fn names(value: &Value, key: &str) -> io::Result<Vec<String>> {
    let entries = value
        .get(key)
        .and_then(Value::as_array)
        .filter(|v| v.len() <= 256)
        .ok_or_else(|| fail(&format!("Invalid management {key}")))?;
    let mut seen = HashSet::new();
    entries
        .iter()
        .map(|v| {
            let name = v
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 128)
                .ok_or_else(|| fail("Invalid management capability name"))?;
            if !seen.insert(name) {
                return Err(fail("Duplicate management capability"));
            }
            Ok(name.to_owned())
        })
        .collect()
}

fn restored_input(value: &Value) -> io::Result<Option<usize>> {
    let Some(status) = value.get("restored_input") else {
        return Ok(None);
    };
    let held = status["held"]
        .as_u64()
        .filter(|count| *count <= 1000)
        .ok_or_else(|| fail("Invalid restored-input count"))? as usize;
    match status["policy"].as_str() {
        Some("hold") => Ok(Some(held)),
        Some("automatic") if held == 0 => Ok(None),
        _ => Err(fail("Invalid restored-input policy")),
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Snapshot {
    pub session: String,
    pub model: String,
    pub backend: String,
    pub profile: String,
    pub mode: String,
    pub writable: bool,
    pub held_restored_input: Option<usize>,
    pub schedules_paused: bool,
    pub conversations: usize,
    pub jobs: usize,
    pub tasks: usize,
}
impl Snapshot {
    fn parse(value: &Value) -> io::Result<Self> {
        if boolean(value, "busy")?
            || rows(value, "active_work")? != 0
            || rows(value, "pending_approvals")? != 0
            || rows(value, "pending_questions")? != 0
            || rows(value, "pending_model_loads")? != 0
        {
            return Err(fail(
                "Runtime startup state is not idle; no prompt was admitted",
            ));
        }
        let ownership = &value["ownership"];
        if ownership.get("data_version").and_then(Value::as_u64) != Some(1) {
            return Err(fail("Incompatible management data version"));
        }
        let mode = text(value, "mode", 32)?;
        if !matches!(mode, "plan" | "normal") {
            return Err(fail("Unknown management mode"));
        }
        Ok(Self {
            session: text(value, "session_id", 128)?.to_owned(),
            model: text(&value["model"], "model", 1024)?.to_owned(),
            backend: text(&value["model"], "backend", 64)?.to_owned(),
            profile: text(value, "tool_profile", 32)?.to_owned(),
            mode: mode.to_owned(),
            writable: boolean(ownership, "writable")?,
            held_restored_input: restored_input(value)?,
            schedules_paused: boolean(value, "schedules_paused")?,
            conversations: rows(value, "conversations")?,
            jobs: rows(value, "jobs")?,
            tasks: rows(value, "tasks")?,
        })
    }
}
#[derive(Debug)]
pub enum Step {
    RuntimeControls(crate::runtime_controls_view::Update),
    WorkLaunchStop {
        action_id: String,
        generation: u64,
        admission: Box<crate::work_lifecycle::Admission>,
    },
    WorkLaunchPending {
        action_id: String,
        generation: u64,
        reason: String,
    },
    WorkLaunch {
        action_id: String,
        update: crate::work_view::Update,
        stop: Option<crate::work_lifecycle::Admission>,
        stop_verified: bool,
    },
    ScheduledProposal(String),
    ScheduledHeld {
        reason: String,
        shutdown_identity: Option<Value>,
    },
    ScheduledConversationChanged(Box<crate::conversation_actions::Changed>),
    WorkLifecycle(Box<crate::work_lifecycle::Effect>),
    WorkLifecycleVerified {
        admission_id: String,
    },
    Work(crate::work_view::Update),
    Compact(crate::conversation_compact::Update),
    CompactSettled(Box<CompactChanged>),
    Rewrite(crate::conversation_rewrite::Update),
    RewriteSettled(Box<RewriteChanged>),
    SavedHistory(crate::history_view::Update),
    Theme {
        update: crate::theme_controls::Update,
        policy: Option<Value>,
        startup: Option<(Snapshot, Option<crate::resume::History>)>,
    },
    ConversationAction {
        update: crate::conversation_actions::Update,
        policy: Option<Value>,
    },
    ConversationChanged(Box<crate::conversation_actions::Changed>),
    Engine {
        update: crate::engine_controls::Update,
        policy: Option<Value>,
        state: Option<Snapshot>,
    },
    Bootstrap(Value),
    Write(Value),
    Initialized(Snapshot),
    Restored {
        snapshot: Snapshot,
        history: crate::resume::History,
    },
    Control(Update),
    Queue(crate::restored_queue::Update),
    Commands {
        update: crate::command_palette::Update,
        policy: Option<Value>,
    },
    Settings {
        update: crate::settings_view::Update,
        policy: Option<Value>,
    },
    Backend(crate::backend_controls::Update),
    BackendChanged(Box<crate::backend_controls::Changed>),
    Browser(crate::conversation_browser::Update),
    Opened(Box<crate::conversation_browser::Opened>),
    Event(Value),
    Ignore,
}
#[derive(Clone, Copy, PartialEq)]
enum Phase {
    AwaitReady,
    Hello,
    State,
    History,
    HistoryPage,
    Active,
}

struct ControlPending {
    id: CommandId,
    kind: Kind,
    applying: bool,
    selection: Option<String>,
    mutation_id: Option<CommandId>,
    failure: Option<String>,
    succeeded: bool,
    target_menu: Option<Choices>,
}
pub struct Continuation {
    pub command: Value,
    pub prompt: String,
    pub session: String,
    pub entry: String,
    record: crate::restored_queue::Entry,
}
struct Reconciliation {
    entry: crate::restored_queue::Entry,
    accepted: Option<bool>,
}
struct QueueClosure {
    entry: String,
    resolved: bool,
}
struct QueuePending {
    id: CommandId,
    applying: bool,
    closed: Option<QueueClosure>,
    continued: Option<(String, crate::restored_queue::Entry, bool)>,
    reconciled: Option<Reconciliation>,
    failure: Option<String>,
    succeeded: bool,
}
enum BrowserRequest {
    PreviewPage {
        descriptor: crate::history::Snapshot,
    },
    Preview {
        target: String,
        agent: String,
        snapshot: String,
    },
    Page {
        previous: Option<crate::conversation_browser::Page>,
        cursor: Option<String>,
    },
    Open {
        preview: Option<Box<crate::history::Snapshot>>,
        target: String,
        agent: String,
        mutation: CommandId,
        stage: u8,
        history: Option<crate::resume::History>,
        state: Option<Snapshot>,
        policy: Option<Value>,
        model: Option<String>,
    },
}
struct BrowserPending {
    id: CommandId,
    request: BrowserRequest,
}
struct BackendPending {
    id: CommandId,
    stage: u8,
    selection: Option<(String, String)>,
    catalog: Option<crate::backend_controls::Catalog>,
    mutation: Option<CommandId>,
    history: Option<crate::resume::History>,
    state: Option<Snapshot>,
    policy: Option<Value>,
    observed_model: bool,
}
struct CommandPending {
    id: Option<CommandId>,
    stage: u8,
    raw: Option<String>,
    row: Option<String>,
    arguments: String,
    skills: bool,
    reviewed: Option<crate::prepared_skill::Prepared>,
}
struct EngineConfigPending {
    request: crate::engine_config::Intent,
    receipt: Option<Value>,
}
struct EnginePending {
    config: Option<EngineConfigPending>,
    id: Option<CommandId>,
    stage: u8,
    action: Option<crate::engine_controls::Action>,
    snapshot: Option<crate::engine_controls::Snapshot>,
    review: Option<crate::engine_controls::Review>,
    receipt: Option<crate::engine_controls::Receipt>,
    history: Option<crate::resume::History>,
    policy: Option<Value>,
    state: Option<Snapshot>,
    mutation: Option<CommandId>,
}
struct ConversationPending {
    id: CommandId,
    stage: u8,
    action: crate::conversation_actions::Action,
    title: String,
    snapshot: Option<crate::conversation_actions::Snapshot>,
    review: Option<crate::conversation_actions::Review>,
    history_before: Option<crate::resume::History>,
    history: Option<crate::resume::History>,
    policy: Option<Value>,
    state: Option<Snapshot>,
    current: Option<crate::conversation_actions::Snapshot>,
    queue: Option<crate::restored_queue::Snapshot>,
    mutation: Option<CommandId>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
enum HistoryPhase {
    Speech(crate::playback::Request),
    Image(crate::history::ImageSelection),
    Locate,
    Snapshot,
    Page,
}
struct SavedHistoryPending {
    id: CommandId,
    key: crate::history_view::ReadKey,
    active: crate::history::Identity,
    identity: crate::history::Identity,
    phase: HistoryPhase,
    descriptor: Option<crate::history::Snapshot>,
    position: crate::history::PageRequest,
    prior: Option<crate::history::Page>,
}
enum PreferencesRequest {
    Glassbox(Box<crate::glassbox_controls::Request>),
    Mcp(Box<crate::mcp_config::Request>),
    ModelPreferences(Box<crate::model_preferences::Request>),
    ModelLifecycle(Box<crate::model_lifecycle::Request>),
    Voice(Box<crate::voice_preferences::Request>),
}
enum PreferencesReply {
    Glassbox(Box<crate::glassbox_controls::Update>),
    Mcp(Box<crate::mcp_config::Update>),
    ModelPreferences(Box<crate::model_preferences::Update>),
    ModelLifecycle(Box<crate::model_lifecycle::Update>),
    Voice(Box<crate::voice_preferences::Update>),
    Next(PreferencesRequest),
}
impl PreferencesRequest {
    fn operations(&self) -> &[&str] {
        match self {
            Self::Mcp(_) => &crate::mcp_config::OPERATIONS,
            Self::ModelPreferences(_) => &crate::model_preferences::OPERATIONS,
            Self::Glassbox(_) => &crate::glassbox_controls::OPERATIONS,
            Self::ModelLifecycle(_) => &crate::model_lifecycle::OPERATIONS,
            Self::Voice(_) => &crate::voice_preferences::OPERATIONS,
        }
    }
    fn is_apply(&self) -> bool {
        match self {
            Self::Mcp(r) => matches!(**r, crate::mcp_config::Request::Apply(_, _)),
            Self::ModelPreferences(r) => matches!(**r, crate::model_preferences::Request::Apply(_)),
            Self::Glassbox(r) => matches!(**r, crate::glassbox_controls::Request::Apply(_)),
            Self::ModelLifecycle(r) => matches!(**r, crate::model_lifecycle::Request::Apply(_)),
            Self::Voice(r) => matches!(**r, crate::voice_preferences::Request::Apply(_)),
        }
    }
    fn command(&self, id: &crate::work_protocol::Identity, instance: &str) -> io::Result<Value> {
        match self {
            Self::Mcp(r) => r.command(id, instance),
            Self::ModelPreferences(r) => r.command(id, instance),
            Self::Glassbox(r) => r.command(id, instance),
            Self::ModelLifecycle(r) => r.command(id, instance),
            Self::Voice(r) => r.command(id, instance),
        }
    }
    fn rejected(&self) -> fn(String) -> crate::settings_view::Update {
        match self {
            Self::Mcp(_) => |reason| crate::settings_view::Update::Mcp(Err(reason)),
            Self::ModelPreferences(_) => {
                |reason| crate::settings_view::Update::ModelPreferences(Err(reason))
            }
            Self::Glassbox(_) => |reason| crate::settings_view::Update::Glassbox(Err(reason)),
            Self::ModelLifecycle(_) => {
                |reason| crate::settings_view::Update::ModelLifecycle(Err(reason))
            }
            Self::Voice(_) => |reason| crate::settings_view::Update::Voice(Err(reason)),
        }
    }
    fn response(
        self,
        v: &Value,
        id: &crate::work_protocol::Identity,
        instance: &str,
        command_id: &str,
    ) -> io::Result<PreferencesReply> {
        Ok(match self {
            Self::Glassbox(r) => match r.response(v, id, instance, command_id)? {
                crate::glassbox_controls::Reply::Next(next) => {
                    PreferencesReply::Next(Self::Glassbox(Box::new(next)))
                }
                crate::glassbox_controls::Reply::Update(update) => {
                    PreferencesReply::Glassbox(Box::new(update))
                }
            },
            Self::Mcp(r) => match r.response(v, id, instance, command_id)? {
                crate::mcp_config::Reply::Next(next) => {
                    PreferencesReply::Next(Self::Mcp(Box::new(next)))
                }
                crate::mcp_config::Reply::Update(update) => PreferencesReply::Mcp(Box::new(update)),
            },
            Self::ModelPreferences(r) => match r.response(v, id, instance, command_id)? {
                crate::model_preferences::Reply::Next(next) => {
                    PreferencesReply::Next(Self::ModelPreferences(Box::new(next)))
                }
                crate::model_preferences::Reply::Update(update) => {
                    PreferencesReply::ModelPreferences(Box::new(update))
                }
            },
            Self::ModelLifecycle(r) => match r.response(v, id, instance, command_id)? {
                crate::model_lifecycle::Reply::Next(next) => {
                    PreferencesReply::Next(Self::ModelLifecycle(Box::new(next)))
                }
                crate::model_lifecycle::Reply::Update(update) => {
                    PreferencesReply::ModelLifecycle(Box::new(update))
                }
            },
            Self::Voice(r) => match r.response(v, id, instance, command_id)? {
                crate::voice_preferences::Reply::Next(next) => {
                    PreferencesReply::Next(Self::Voice(Box::new(next)))
                }
                crate::voice_preferences::Reply::Update(update) => {
                    PreferencesReply::Voice(Box::new(update))
                }
            },
        })
    }
}
enum SettingsRead {
    Preferences(crate::work_protocol::Identity, String, PreferencesRequest),
    Values,
    RuntimeStatus {
        identity: crate::work_protocol::Identity,
        instance: String,
        glassbox: bool,
    },
}
pub struct Management {
    runtime_controls: runtime::Control,
    execution: execution::Control,
    scheduled: scheduled::Control,
    work_lifecycle: work_lifecycle::Control,
    work: work::Control,
    history_projection: bool,
    saved_history: Option<SavedHistoryPending>,
    history_snapshot: Option<crate::history::Snapshot>,
    playback: Option<crate::playback::State>,
    history_page: Option<crate::history::Page>,
    history_target: Option<(crate::history_view::Target, crate::history::Identity)>,
    history_generation: u64,
    compact: compact::Control,
    rewrite_pending: Option<rewrite::Pending>,
    rewrite_cache: Option<rewrite::Cache>,
    rewrite_review: Option<RewritePrepared>,
    rewrite_attempt: Option<rewrite::Attempt>,
    rewrite_generation: u64,
    rewrite_protocol: bool,
    rewrite_chain: Option<rewrite::Chain>,
    startup_history: Option<crate::resume::History>,
    theme: Option<ThemePending>,
    theme_snapshot: Option<crate::theme_controls::Snapshot>,
    theme_review: Option<crate::theme_controls::Review>,
    theme_state: Option<(Snapshot, Value)>,
    theme_startup: Option<(Snapshot, Option<crate::resume::History>)>,
    conversation: Option<ConversationPending>,
    conversation_snapshot: Option<crate::conversation_actions::Snapshot>,
    conversation_review: Option<(
        crate::conversation_actions::Review,
        crate::resume::History,
        Value,
        Snapshot,
    )>,
    closing: bool,
    engine: Option<EnginePending>,
    engine_instance: Option<(String, u64, crate::engine_controls::Admission)>,
    engine_snapshot: Option<crate::engine_controls::Snapshot>,
    engine_peer_refusal: Option<(String, u64, String)>,
    config_snapshot: Option<crate::engine_config::Snapshot>,
    config_review: Option<crate::engine_config::Review>,
    engine_review: Option<(crate::engine_controls::Review, crate::resume::History)>,

    wire: WireClient,
    control: Option<ControlPending>,
    model_target_state: Option<(Snapshot, Value)>,
    queue: Option<QueuePending>,
    queue_menu: Option<crate::restored_queue::Snapshot>,
    settings: Option<(CommandId, SettingsRead)>,
    command: Option<CommandPending>,
    command_catalog: Option<crate::command_palette::Catalog>,
    command_prepared: Option<crate::prepared_skill::Prepared>,
    settings_edit: Option<settings::Pending>,
    settings_snapshot: Option<settings::Snapshot>,
    settings_review: Option<settings::Review>,
    settings_generation: u64,
    backend: Option<BackendPending>,
    backend_catalog: Option<crate::backend_controls::Catalog>,
    browser: Option<BrowserPending>,
    browser_page: Option<crate::conversation_browser::Page>,
    browser_preview: Option<crate::history::Snapshot>,
    browser_authority: Option<(String, bool)>,
    continuation: Option<(String, crate::restored_queue::Entry)>,
    menu: Option<Choices>,
    revision: u64,
    phase: Phase,
    expected: Option<(String, String, String, String)>,
    owner_store: Option<(String, String)>,
    state_id: Option<CommandId>,
    resume: Option<String>,
    startup: Option<Snapshot>,
    history_id: Option<CommandId>,
    operations: Vec<String>,
    prompt: Option<String>,
    origin: Option<(ExecutionId, Origin)>,
    executions: HashSet<String>,
    failed: bool,
    mirrors: Vec<(Value, usize)>,
    mirror_bytes: usize,
}
impl Default for Management {
    fn default() -> Self {
        Self {
            runtime_controls: runtime::Control::default(),
            execution: execution::Control::default(),
            scheduled: scheduled::Control::default(),
            work_lifecycle: work_lifecycle::Control::default(),
            work: work::Control::default(),
            history_projection: false,
            saved_history: None,
            history_snapshot: None,
            playback: None,
            history_page: None,
            history_target: None,
            history_generation: 0,
            compact: compact::Control::default(),
            rewrite_pending: None,
            rewrite_cache: None,
            rewrite_review: None,
            rewrite_attempt: None,
            rewrite_generation: 0,
            rewrite_protocol: false,
            rewrite_chain: None,
            startup_history: None,
            theme: None,
            theme_snapshot: None,
            theme_review: None,
            theme_state: None,
            theme_startup: None,
            conversation: None,
            conversation_snapshot: None,
            conversation_review: None,
            wire: WireClient::new(
                GENERATION,
                MAX_PENDING_REQUESTS,
                MIRRORED
                    .iter()
                    .map(|name| ((*name).to_owned(), Delivery::Mirrored)),
            )
            .expect("static delivery policy"),
            closing: false,
            engine: None,
            engine_instance: None,
            engine_snapshot: None,
            engine_peer_refusal: None,
            config_snapshot: None,
            config_review: None,
            engine_review: None,
            control: None,
            model_target_state: None,
            queue: None,
            queue_menu: None,
            settings: None,
            command: None,
            command_catalog: None,
            command_prepared: None,
            settings_edit: None,
            settings_snapshot: None,
            settings_review: None,
            settings_generation: 0,
            backend: None,
            backend_catalog: None,
            browser: None,
            browser_page: None,
            browser_preview: None,
            browser_authority: None,
            continuation: None,
            menu: None,
            revision: 0,
            phase: Phase::AwaitReady,
            expected: None,
            owner_store: None,
            state_id: None,
            resume: None,
            startup: None,
            history_id: None,
            operations: Vec::new(),
            prompt: None,
            origin: None,
            executions: HashSet::new(),
            failed: false,
            mirrors: Vec::new(),
            mirror_bytes: 0,
        }
    }
}
impl Management {
    pub fn new(resume: Option<String>) -> Self {
        Self {
            resume,
            ..Self::default()
        }
    }
    // Called only after Turn and the read-only ready-fact parser validate readiness.
    pub fn begin(&mut self, ready: &Value) -> io::Result<Value> {
        if self.failed || self.phase != Phase::AwaitReady {
            return Err(fail("Management startup already attempted"));
        }
        let result = self.begin_inner(ready);
        if result.is_err() {
            self.failed = true;
        }
        result
    }
    fn begin_inner(&mut self, ready: &Value) -> io::Result<Value> {
        self.work_lifecycle_advertisement(ready)?;
        self.owner_store = ready["owner_store"]["agent_id"]
            .as_str()
            .zip(ready["owner_store"]["directory"].as_str())
            .map(|(id, directory)| (id.to_owned(), directory.to_owned()));
        self.history_projection = match ready.get("history_projection_version") {
            None | Some(Value::Null) => false,
            Some(value) if value.as_u64() == Some(1) => true,
            _ => return Err(fail("Unsupported history projection version")),
        };
        if !ready
            .get("management_protocols")
            .and_then(Value::as_array)
            .is_some_and(|v| v.iter().any(|v| v.as_u64() == Some(1)))
        {
            return Err(fail(
                "Runtime lacks management protocol 1; update the Python source",
            ));
        }
        let capabilities = names(ready, "capabilities")?;
        if !["hello", "state"]
            .iter()
            .all(|s| capabilities.iter().any(|v| v == s))
        {
            return Err(fail("Runtime lacks hello/state management capabilities"));
        }
        if ready.get("management_instance").is_some() {
            let instance = text(ready, "management_instance", 64)?.to_owned();
            if instance.len() != 64
                || !instance
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
                || ready["desired_model"] != ready["model"]
            {
                return Err(fail("Invalid engine startup identity"));
            }
            self.engine_instance = Some((
                instance,
                ready["engine_generation"]
                    .as_u64()
                    .ok_or_else(|| fail("Invalid engine generation"))?,
                crate::engine_controls::Admission::parse(&ready["admission"])?,
            ));
        }
        self.expected = Some((
            text(ready, "conversation_id", 128)?.to_owned(),
            text(ready, "backend", 64)?.to_owned(),
            text(ready, "model", 1024)?.to_owned(),
            text(ready, "tool_profile", 32)?.to_owned(),
        ));
        if self
            .resume
            .as_deref()
            .is_some_and(|id| ready["conversation_id"] != id)
        {
            return Err(fail("Runtime did not resume the requested conversation"));
        }
        self.phase = Phase::Hello;
        self.wire
            .begin_hello()
            .map_err(|_| fail("Cannot begin management negotiation"))
    }
    pub fn begin_close(&mut self) {
        self.closing = true;
        self.close_work_lifecycle();
        self.engine_review = None;
        self.engine_snapshot = None;
    }
    pub fn ready(&self) -> bool {
        self.phase == Phase::Active
            && !self.closing
            && !self.failed
            && !self.work_lifecycle_starting()
            && !self.schedule_pending()
            && !self.runtime_controls_pending()
            && !self.execution_barrier()
            && self.work.pending.is_none()
            && self.engine.is_none()
            && self.conversation.is_none()
            && self.command.is_none()
            && self.theme.is_none()
            && self.saved_history.is_none()
            && self.compact.idle()
            && self.rewrite_pending.is_none()
            && self.rewrite_attempt.is_none()
            && self.settings.is_none()
            && self.settings_edit.is_none()
            && self.backend.is_none()
            && self.browser.is_none()
            && self.control.is_none()
            && self.queue.is_none()
            && self.continuation.is_none()
    }
    pub fn operations(&self) -> &[String] {
        &self.operations
    }
    pub fn prompt(&mut self, id: &str) -> io::Result<()> {
        if !self.ready()
            || self
                .engine_instance
                .as_ref()
                .is_some_and(|v| v.2 != crate::engine_controls::Admission::PromptReady)
            || id.is_empty()
            || id.len() > 128
        {
            return Err(fail("Management is not ready for a prompt"));
        }
        self.ensure_drained()?;
        self.menu = None;
        self.queue_menu = None;
        self.backend_catalog = None;
        self.browser_page = None;
        self.conversation_snapshot = None;
        self.conversation_review = None;
        self.history_snapshot = None;
        self.playback = None;
        self.history_page = None;
        self.history_target = None;
        self.compact.invalidate();
        self.rewrite_cache = None;
        self.rewrite_review = None;
        self.browser_preview = None;
        self.record_work_operation(id)?;
        self.prompt = Some(id.to_owned());
        self.origin = None;
        Ok(())
    }
    /// Raw/envelope pairing is checked without deduplicating selected events.
    /// Cross-thread interleaving may reorder pairs; identical events still each
    /// consume one raw occurrence. An unmatched suffix can never be clean EOF.
    pub fn has_unpaired(&self) -> bool {
        !self.mirrors.is_empty()
    }
    pub fn ensure_drained(&self) -> io::Result<()> {
        if self.mirrors.is_empty() {
            Ok(())
        } else {
            Err(fail("Runtime omitted an expected GUI event envelope"))
        }
    }
    pub fn accept(&mut self, frame: Value) -> io::Result<Step> {
        // In-memory callers have no wire encoding; the real supervisor always
        // supplies the original frame size through accept_wire.
        let bytes = serde_json::to_vec(&frame)?.len();
        self.accept_wire(frame, bytes)
    }
    pub fn accept_wire(&mut self, frame: Value, original_json_bytes: usize) -> io::Result<Step> {
        self.accept_wire_context(frame, original_json_bytes, None)
    }
    /// None is explicit idle authority, supplied by the actual Turn supervisor.
    /// The legacy entrypoint above supplies no authority at all.
    pub fn accept_wire_with_work(
        &mut self,
        frame: Value,
        original_json_bytes: usize,
        current: Option<&crate::work_lifecycle::CurrentExecution>,
    ) -> io::Result<Step> {
        self.accept_wire_context(frame, original_json_bytes, Some(current))
    }
    fn accept_wire_context(
        &mut self,
        frame: Value,
        original_json_bytes: usize,
        authority: Option<Option<&crate::work_lifecycle::CurrentExecution>>,
    ) -> io::Result<Step> {
        if self.failed {
            return Err(fail("Management connection is uncertain"));
        }
        let lifecycle_event = if frame["type"] == "gui.event" {
            frame["event"]["type"].as_str()
        } else {
            frame["type"].as_str()
        }
        .is_some_and(|kind| crate::work_lifecycle::EVENTS.contains(&kind));
        let result =
            if lifecycle_event && original_json_bytes > crate::work_lifecycle::ENVELOPE_BYTES {
                Err(fail(
                    "Work lifecycle event exceeds the full 128 KiB wire budget",
                ))
            } else if self.work_lifecycle_response_pending(&frame)
                && original_json_bytes > crate::work_lifecycle::REPLY_BYTES
            {
                Err(fail(
                    "Work lifecycle reply exceeds the full 1 MiB wire budget",
                ))
            } else if self.schedule_response_pending(&frame)
                && original_json_bytes > crate::work_lifecycle::REPLY_BYTES
            {
                Err(fail("Scheduled response exceeds full wire budget"))
            } else if self.runtime_response_pending(&frame)
                && original_json_bytes > crate::work_lifecycle::REPLY_BYTES
            {
                Err(fail("Runtime controls response exceeds full wire budget"))
            } else if self.execution_response_pending(&frame)
                && original_json_bytes > crate::work_lifecycle::REPLY_BYTES
            {
                Err(fail("Execution response exceeds full wire budget"))
            } else if self.work_response_pending(&frame)
                && original_json_bytes > crate::work_protocol::MAX_BYTES
            {
                Err(fail("Work response exceeds the full 1 MiB wire budget"))
            } else if self.compaction_settings_response_pending(&frame)
                && original_json_bytes > crate::compaction_settings::MAX_BYTES
            {
                Err(fail(
                    "Compaction preferences response exceeds the full 64 KiB wire budget",
                ))
            } else if self.compact_response_pending(&frame)
                && original_json_bytes > crate::conversation_compact::RESPONSE_BYTES
            {
                Err(fail(
                    "Compaction response exceeds the full 1 MiB wire budget",
                ))
            } else if self.rewrite_response_pending(&frame)
                && original_json_bytes > crate::conversation_rewrite::RESPONSE_BYTES
            {
                Err(fail(
                    "Edit/retry response exceeds the full 1 MiB wire budget",
                ))
            } else if self.history_locate_response_pending(&frame)
                && original_json_bytes > crate::conversation_browser::PAGE_BYTES
            {
                Err(fail(
                    "Located conversation response exceeds the full 256 KiB wire budget",
                ))
            } else if self.history_response_pending(&frame)
                && original_json_bytes > crate::history::MAX_RESPONSE_BYTES
            {
                Err(fail("History response exceeds the full 1 MiB wire budget"))
            } else {
                self.accept_inner(frame, authority)
            };
        let result = result.and_then(|step| self.work_startup_gate(step));
        if result.is_err() {
            self.failed = true;
        }
        result
    }
    fn history_locate_response_pending(&self, frame: &Value) -> bool {
        self.history_projection
            && matches!(frame["type"].as_str(), Some("response" | "error"))
            && self
                .saved_history
                .as_ref()
                .is_some_and(|p| p.phase == HistoryPhase::Locate && frame["id"] == p.id.to_value())
    }
    fn history_response_pending(&self, frame: &Value) -> bool {
        if !self.history_projection || !matches!(frame["type"].as_str(), Some("response" | "error"))
        {
            return false;
        }
        let matches = |id: &CommandId| frame["id"] == id.to_value();
        (matches!(self.phase, Phase::History | Phase::HistoryPage)
            && self.history_id.as_ref().is_some_and(matches))
            || self.saved_history.as_ref().is_some_and(|p| matches(&p.id))
            || self
                .backend
                .as_ref()
                .is_some_and(|p| matches(&p.id) && matches!(p.stage, 1 | 4))
            || self
                .engine
                .as_ref()
                .is_some_and(|p| p.id.as_ref().is_some_and(matches) && matches!(p.stage, 2 | 8))
            || self
                .conversation
                .as_ref()
                .is_some_and(|p| matches(&p.id) && matches!(p.stage, 2 | 8))
            || self.browser.as_ref().is_some_and(|p| {
                matches(&p.id)
                    && match &p.request {
                        BrowserRequest::Preview { .. } | BrowserRequest::PreviewPage { .. } => true,
                        BrowserRequest::Open {
                            preview: Some(_),
                            stage,
                            ..
                        } => *stage <= 2,
                        _ => false,
                    }
            })
    }
    fn accept_inner(
        &mut self,
        frame: Value,
        authority: Option<Option<&crate::work_lifecycle::CurrentExecution>>,
    ) -> io::Result<Step> {
        match self.wire.ingest(GENERATION, frame) {
            Notice::LegacyEvent(value)
                if self.phase == Phase::AwaitReady && value["type"] == "ready" =>
            {
                Ok(Step::Bootstrap(value))
            }
            Notice::Hello(frame) if self.phase == Phase::Hello => {
                let result = &frame["result"];
                let capabilities = names(result, "capabilities")?;
                let operations = names(result, "operations")?;
                if !["hello", "state"]
                    .iter()
                    .all(|name| capabilities.iter().any(|v| v == name))
                    || !["gui.hello", "gui.state"]
                        .iter()
                        .all(|name| operations.iter().any(|v| v == name))
                {
                    return Err(fail("Negotiated runtime lacks hello/state capabilities"));
                }
                if self.history_projection
                    && !crate::history::OPERATIONS
                        .iter()
                        .all(|op| operations.iter().any(|v| v == op))
                {
                    return Err(fail(
                        "Negotiated history projection lacks required operations",
                    ));
                }
                self.configure_work_lifecycle(&operations)?;
                self.operations = operations;
                let id = self
                    .wire
                    .register_read()
                    .map_err(|_| fail("Cannot reserve state read"))?;
                let command =
                    json!({"type":"gui.state","id":id.to_value(),"include_conversations":false});
                self.state_id = Some(id);
                self.phase = Phase::State;
                Ok(Step::Write(command))
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if id == self.state_id && self.phase == Phase::State => {
                if frame["ok"] != true {
                    return Err(fail("Runtime rejected startup state read"));
                }
                self.ensure_drained()?;
                let snapshot = Snapshot::parse(&frame["result"])?;
                let (session, backend, model, profile) = self.expected.as_ref().expect("begun");
                if (
                    &snapshot.session,
                    &snapshot.backend,
                    &snapshot.model,
                    &snapshot.profile,
                ) != (session, backend, model, profile)
                {
                    return Err(fail(
                        "Runtime state differs from validated startup identity/policy",
                    ));
                }
                self.browser_authority = frame["result"]["ownership"]["agent_id"]
                    .as_str()
                    .filter(|id| !id.is_empty() && id.len() <= 128)
                    .map(|id| (id.to_owned(), snapshot.schedules_paused));
                if self.owner_store.as_ref().is_some_and(|(agent, _)| {
                    self.browser_authority
                        .as_ref()
                        .is_none_or(|(found, _)| found != agent)
                }) {
                    return Err(fail(
                        "Runtime ownership differs from the selected memory lease",
                    ));
                }
                self.state_id = None;
                if self.resume.is_some() || self.history_projection {
                    let session = &snapshot.session;
                    if snapshot.held_restored_input.is_none() {
                        return Err(fail(
                            "Resume requires verified held-input policy; update Python source",
                        ));
                    }
                    if (!snapshot.writable && self.resume.is_some())
                        || !self
                            .operations
                            .iter()
                            .any(|op| op == self.history_operation())
                    {
                        return Err(fail(
                            "Resume requires writable owned state and transcript read support",
                        ));
                    }
                    let id = self
                        .wire
                        .register_read()
                        .map_err(|_| fail("Cannot reserve saved transcript read"))?;
                    let mut command = self.history_command(session, backend, model)?;
                    command["id"] = id.to_value();
                    self.history_id = Some(id);
                    self.startup = Some(snapshot);
                    self.phase = Phase::History;
                    return Ok(Step::Write(command));
                }
                self.phase = Phase::Active;
                self.startup_theme(snapshot, None)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self.phase == Phase::History && id == self.history_id => {
                if frame["ok"] != true {
                    return Err(fail("Runtime rejected saved transcript read"));
                }
                self.ensure_drained()?;
                let (session, backend, model, _) = self.expected.as_ref().expect("startup");
                let history = if self.history_projection {
                    self.parse_history(&frame["result"], session, backend, model)?
                } else {
                    crate::resume::History::parse(&frame["result"], session)?
                };
                if let Some(descriptor) = &history.descriptor {
                    let id = self
                        .wire
                        .register_read()
                        .map_err(|_| fail("Cannot reserve startup history page"))?;
                    let mut command = descriptor.page_request(
                        &crate::history::PageRequest::Anchor(crate::history::Anchor::Tail),
                    )?;
                    command["id"] = id.to_value();
                    self.history_id = Some(id);
                    self.startup_history = Some(history);
                    self.phase = Phase::HistoryPage;
                    return Ok(Step::Write(command));
                }
                self.history_id = None;
                self.phase = Phase::Active;
                let snapshot = self.startup.take().expect("startup");
                self.startup_theme(snapshot, Some(history))
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self.phase == Phase::HistoryPage && id == self.history_id => {
                if frame["ok"] != true {
                    return Err(fail("Runtime rejected bounded startup history page"));
                }
                self.ensure_drained()?;
                let mut history = self.startup_history.take().expect("startup descriptor");
                let descriptor = history.descriptor.as_deref().expect("projected descriptor");
                let page = crate::history::Page::parse(&frame["result"], descriptor)?;
                page.accepts(&crate::history::PageRequest::Anchor(
                    crate::history::Anchor::Tail,
                ))?;
                self.history_snapshot = Some(descriptor.clone());
                self.history_page = Some(page.clone());
                history.page = Some(Box::new(page));
                self.history_id = None;
                self.phase = Phase::Active;
                let snapshot = self.startup.take().expect("startup");
                self.startup_theme(snapshot, Some(history))
            }
            Notice::Response {
                matched: true,
                frame,
                ..
            } if self.schedule_response_pending(&frame) => self.schedule_response(frame),
            Notice::Response {
                matched: true,
                frame,
                ..
            } if self.execution_response_pending(&frame) => self.execution_response(frame),
            Notice::Response {
                matched: true,
                frame,
                ..
            } if self.runtime_response_pending(&frame) => self.runtime_response(frame),
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .work_lifecycle
                .pending
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.work_lifecycle_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .work
                .pending
                .as_ref()
                .is_some_and(|p| Some(&p.id) == id.as_ref()) =>
            {
                self.work_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .compact
                .pending
                .as_ref()
                .is_some_and(|p| Some(&p.id) == id.as_ref()) =>
            {
                self.compact_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .rewrite_pending
                .as_ref()
                .is_some_and(|p| Some(&p.id) == id.as_ref()) =>
            {
                self.rewrite_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .saved_history
                .as_ref()
                .is_some_and(|p| Some(&p.id) == id.as_ref()) =>
            {
                self.saved_history_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .conversation
                .as_ref()
                .is_some_and(|p| Some(&p.id) == id.as_ref()) =>
            {
                self.conversation_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .engine
                .as_ref()
                .is_some_and(|p| p.id.as_ref() == id.as_ref()) =>
            {
                self.engine_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .control
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.control_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .queue
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.queue_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .browser
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.browser_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .backend
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.backend_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .command
                .as_ref()
                .is_some_and(|p| p.id.as_ref() == id.as_ref()) =>
            {
                self.command_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .settings_edit
                .as_ref()
                .is_some_and(|pending| Some(&pending.id) == id.as_ref()) =>
            {
                self.settings_edit_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .theme
                .as_ref()
                .is_some_and(|p| Some(&p.id) == id.as_ref()) =>
            {
                self.theme_response(frame)
            }
            Notice::Response {
                id,
                matched: true,
                frame,
            } if self
                .settings
                .as_ref()
                .is_some_and(|(pending, _)| id.as_ref() == Some(pending)) =>
            {
                self.settings_response(frame)
            }
            Notice::Response {
                matched: false,
                frame,
                ..
            } => Ok(Step::Event(frame)),
            Notice::DirectError(frame) => Ok(Step::Event(frame)),
            Notice::IgnoredMirror(value) => {
                let bytes = serde_json::to_vec(&value).map_err(io::Error::other)?.len();
                if self.mirrors.len() >= 64
                    || self.mirror_bytes.saturating_add(bytes) > 4 * 1024 * 1024
                {
                    return Err(fail("Unpaired runtime event capacity exceeded"));
                }
                self.mirror_bytes += bytes;
                self.mirrors.push((value, bytes));
                Ok(Step::Ignore)
            }
            Notice::Event { frame, event } if self.phase == Phase::Active => {
                let index = self
                    .mirrors
                    .iter()
                    .position(|(raw, _)| raw == &event)
                    .ok_or_else(|| fail("GUI event has no matching legacy occurrence"))?;
                let (_, bytes) = self.mirrors.remove(index);
                self.mirror_bytes -= bytes;
                if event["type"]
                    .as_str()
                    .is_some_and(|kind| crate::work_lifecycle::EVENTS.contains(&kind))
                {
                    return self.work_lifecycle_event(&frame, authority);
                }
                self.validate_event(&frame, &event)?;
                if self
                    .browser
                    .as_ref()
                    .is_some_and(|pending| matches!(pending.request, BrowserRequest::Open { .. }))
                    || self
                        .backend
                        .as_ref()
                        .is_some_and(|pending| pending.mutation.is_some())
                {
                    Ok(Step::Ignore)
                } else {
                    let mut event = event;
                    if self.compact.chain.is_some() {
                        event["compact_origin"] = json!({"session_id":frame["session_id"],
                            "operation_id":frame["operation_id"],"execution_id":frame["execution_id"],"source":frame["source"]});
                    }
                    if self.rewrite_chain.is_some() {
                        event["rewrite_origin"] = json!({"session_id":frame["session_id"],
                            "operation_id":frame["operation_id"],"execution_id":frame["execution_id"],"source":frame["source"]});
                    }
                    Ok(Step::Event(event))
                }
            }
            Notice::Fault { reason, .. } => {
                Err(fail(&format!("Management protocol fault: {reason:?}")))
            }
            _ => Err(fail(
                "Unexpected event during management startup; state continuity is uncertain",
            )),
        }
    }
    fn validate_event(&mut self, frame: &Value, event: &Value) -> io::Result<()> {
        if event.get("rewrite_origin").is_some() || event.get("compact_origin").is_some() {
            return Err(fail("Runtime supplied reserved native rewrite metadata"));
        }
        let (session, backend, model, _) = self.expected.as_ref().expect("begun");
        if let Some(pending) = &self.conversation
            && let Some(mutation) = &pending.mutation
        {
            let review = pending.review.as_ref().expect("review");
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Conversation action event lacks identity"))?;
            if review.action != crate::conversation_actions::Action::New
                || !matches!(event["type"].as_str(), Some("model_state" | "usage"))
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || (frame["session_id"] != *session && frame["session_id"] != review.target)
            {
                return Err(fail("Unexpected event during conversation mutation"));
            }
            if event["type"] == "model_state"
                && (frame["session_id"] != review.target
                    || event["backend"] != *backend
                    || event["model"] != *model)
            {
                return Err(fail("Conversation action changed provider"));
            }
            return Ok(());
        }

        if let Some(pending) = self.engine.as_ref()
            && let Some(mutation) = &pending.mutation
        {
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Engine lifecycle event lacks emission identity"))?;
            if frame["session_id"] != *session
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || !matches!(event["type"].as_str(), Some("model_state" | "usage"))
            {
                return Err(fail("Unexpected event during owned engine lifecycle"));
            }
            if event["type"] == "model_state"
                && (event["backend"] != *backend || event["model"] != *model)
            {
                return Err(fail("Engine event changed desired authority"));
            }
            return Ok(());
        }
        if let Some(pending) = self.backend.as_mut()
            && let Some(mutation) = &pending.mutation
        {
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Backend transition lacks emission identity"))?;
            let (target, target_model) = pending.selection.as_ref().expect("selection");
            let kind = event["type"].as_str();
            if frame["session_id"] != *session
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || !matches!(kind, Some("model_state" | "usage"))
            {
                return Err(fail("Unexpected event during owned backend transition"));
            }
            if kind == Some("model_state") {
                if event["backend"] != *target || event["model"] != *target_model {
                    return Err(fail("Backend transition event changed the explicit target"));
                }
                pending.observed_model = true;
            }
            return Ok(());
        }
        if let Some(BrowserPending {
            request:
                BrowserRequest::Open {
                    target,
                    mutation,
                    model: transition_model,
                    ..
                },
            ..
        }) = self.browser.as_mut()
        {
            crate::stream::required_uuid4(frame, "event_id")
                .map_err(|_| fail("Conversation transition lacks emission identity"))?;
            let event_session = frame["session_id"].as_str();
            let kind = event["type"].as_str();
            if !matches!(kind, Some("model_state" | "usage"))
                || frame["operation_id"] != mutation.to_value()
                || !matches!(frame.get("execution_id"), None | Some(Value::Null))
                || !(event_session == Some(session.as_str())
                    || event_session == Some(target.as_str()))
            {
                return Err(fail("Unexpected event during owned conversation opening"));
            }
            if kind == Some("model_state") {
                if event_session != Some(target.as_str()) || event["backend"] != *backend {
                    return Err(fail(
                        "Conversation transition changed backend or session unexpectedly",
                    ));
                }
                *transition_model = Some(text(event, "model", 1024)?.to_owned());
            }
            return Ok(());
        }
        if frame.get("session_id").and_then(Value::as_str) != Some(session.as_str()) {
            return Err(fail("Event belongs to another conversation"));
        }
        crate::stream::required_uuid4(frame, "event_id")
            .map_err(|_| fail("GUI event lacks a canonical emission identity"))?;
        let kind = event["type"].as_str().expect("wire validated");
        if kind == "model_state" && (event["backend"] != *backend || event["model"] != *model) {
            let owned_change = event["backend"] == *backend
                && self.control.as_ref().is_some_and(|pending| {
                    pending.kind == Kind::Model
                        && pending.selection.as_deref() == event["model"].as_str()
                        && pending
                            .mutation_id
                            .as_ref()
                            .is_some_and(|id| frame["operation_id"] == id.to_value())
                });
            if !owned_change {
                return Err(fail(
                    "Runtime model changed outside an owned management operation",
                ));
            }
        }
        if self.compact.chain.is_some()
            && (IDENTIFIED.contains(&kind)
                || matches!(
                    kind,
                    "compact_maintenance"
                        | "rewrite_aftercare"
                        | "compaction"
                        | "tool_call"
                        | "tool_approval_requested"
                        | "tool_approval_closed"
                        | "user_input_requested"
                        | "user_input_resolved"
                ))
        {
            return self.validate_compact_event(frame, event);
        }
        if self.rewrite_chain.is_some()
            && (IDENTIFIED.contains(&kind)
                || matches!(
                    kind,
                    "rewrite_aftercare"
                        | "compact_maintenance"
                        | "compaction"
                        | "tool_call"
                        | "tool_approval_requested"
                        | "tool_approval_closed"
                        | "user_input_requested"
                        | "user_input_resolved"
                ))
        {
            return self.validate_rewrite_event(frame, event);
        }
        if matches!(kind, "rewrite_aftercare" | "compact_maintenance")
            || event.get("rewrite_prewrite").is_some()
            || event.get("rewrite_segment").is_some()
            || event.get("compact_segment").is_some()
        {
            return Err(fail(
                "Rewrite execution metadata outside an admitted action",
            ));
        }
        if kind == "tool_call"
            && (self.prompt.is_none() || frame["operation_id"].as_str() != self.prompt.as_deref())
        {
            return Err(fail("Tool diagnostic belongs to another prompt operation"));
        }
        if IDENTIFIED.contains(&kind) {
            self.validate_work_stream_origin(frame)?;
            let (execution, origin, _) = crate::stream::origin(frame, 4096)
                .map_err(|_| fail("Runtime turn lacks a valid captured execution origin"))?;
            if origin.operation.as_ref().and_then(Value::as_str) != self.prompt.as_deref()
                || self.prompt.is_none()
            {
                return Err(fail("Runtime event belongs to another prompt operation"));
            }
            if matches!(kind, "turn_start" | "turn_refused") {
                if self.origin.is_some()
                    || self.executions.len() >= 1000
                    || !self.executions.insert(execution.as_str().to_owned())
                {
                    return Err(fail("Duplicate or excessive runtime execution identity"));
                }
                self.origin = Some((execution, origin));
            } else if self.origin.as_ref() != Some(&(execution, origin)) {
                return Err(fail("Runtime execution origin changed or has no start"));
            }
        }
        Ok(())
    }
}

impl Management {
    pub fn backend(&self) -> Option<&str> {
        self.expected.as_ref().map(|v| v.1.as_str())
    }
    pub fn model(&self) -> Option<&str> {
        self.expected.as_ref().map(|v| v.2.as_str())
    }
    pub fn request_control(&mut self, intent: Intent) -> io::Result<Value> {
        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        let (kind, selection) = match intent {
            Intent::Open(kind) => (kind, None),
            Intent::Select { revision, value } => {
                let menu = self
                    .menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Choices changed; reopen the selector"))?;
                if !menu.rows.iter().any(|row| row.value == value) {
                    return Err(fail("Choice was not offered by the current runtime"));
                }
                (menu.kind, Some(value))
            }
        };
        let operation = if selection.is_some() {
            kind.apply()
        } else {
            kind.read()
        };
        if !self.operations.iter().any(|v| v == operation)
            || !self.operations.iter().any(|v| v == kind.read())
        {
            return Err(fail("The connected runtime does not support this control"));
        }
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve model control"))?;
        let mut command = json!({"type":operation,"id":id.to_value()});
        if let Some(value) = &selection {
            command[if kind == Kind::Model { "slug" } else { "level" }] =
                Value::String(value.clone());
        } else if kind == Kind::Thinking {
            let (_, backend, model, _) = self.expected.as_ref().expect("active");
            command["backend"] = json!(backend);
            command["model"] = json!(model);
        }
        self.control = Some(ControlPending {
            id: id.clone(),
            kind,
            applying: selection.is_some(),
            mutation_id: selection.as_ref().map(|_| id),
            selection,
            failure: None,
            succeeded: false,
            target_menu: None,
        });
        self.menu = None;
        self.queue_menu = None;
        self.browser_page = None;
        Ok(command)
    }
    pub(super) fn take_model_target_state(&mut self) -> Option<(Snapshot, Value)> {
        self.model_target_state.take()
    }
    fn control_response(&mut self, frame: Value) -> io::Result<Step> {
        let mut pending = self.control.take().expect("matched control");
        let success = frame["ok"] == true;
        let error = if success {
            None
        } else {
            Some(text(&frame, "error", 4096)?.to_owned())
        };
        if let Some(menu) = pending.target_menu.take() {
            if !success {
                return Err(fail(
                    "Cannot verify the new model's effective runtime policy",
                ));
            }
            self.ensure_drained()?;
            let result = &frame["result"];
            let state = Snapshot::parse(result)?;
            let (agent, schedules) = self
                .browser_authority
                .as_ref()
                .ok_or_else(|| fail("Model change lacks captured ownership"))?;
            if state.session != menu.session
                || state.backend != menu.backend
                || state.model != menu.model
                || state.schedules_paused != *schedules
                || state.held_restored_input.is_none()
                || result["ownership"]["agent_id"] != *agent
            {
                return Err(fail(
                    "New model policy readback changed the captured target",
                ));
            }
            let profile = &self.expected.as_ref().expect("active").3;
            self.verify_target_profile(&state, &result["tool_policy"], profile)?;
            if self.model_target_state.is_some() {
                return Err(fail("Earlier model policy readback still awaits capture"));
            }
            let expected = self.expected.as_mut().expect("active");
            expected.2 = state.model.clone();
            expected.3 = state.profile.clone();
            self.model_target_state = Some((state, result["tool_policy"].clone()));
            self.origin = None;
            self.prompt = None;
            self.menu = Some(menu.clone());
            return Ok(Step::Control(Update::Choices(menu)));
        }
        if pending.applying {
            let changing_model = success
                && pending.kind == Kind::Model
                && pending.selection.as_deref() != self.model();
            self.work_transition_receipt(&frame["result"], changing_model)?;
            pending.applying = false;
            pending.failure = error;
            pending.succeeded = success;
            let id = self
                .wire
                .register_request()
                .map_err(|_| fail("Cannot reserve selection readback"))?;
            let mut command = json!({"type":pending.kind.read(),"id":id.to_value()});
            if pending.kind == Kind::Thinking {
                let (_, backend, model, _) = self.expected.as_ref().expect("active");
                command["backend"] = json!(backend);
                command["model"] = json!(model);
            }
            pending.id = id;
            self.control = Some(pending);
            return Ok(Step::Write(command));
        }
        if let Some(error) = error {
            return if pending.selection.is_some() {
                Err(fail(&format!(
                    "Cannot verify runtime after selection: {error}"
                )))
            } else {
                Ok(Step::Control(Update::Rejected(error)))
            };
        }
        self.ensure_drained()?;
        self.revision = self
            .revision
            .checked_add(1)
            .ok_or_else(|| fail("Choice revision exhausted"))?;
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        let mut menu = Choices::parse(pending.kind, &frame["result"], self.revision, session)?;
        if menu.backend != *backend {
            return Err(fail("Control readback changed the backend unexpectedly"));
        }
        if menu.model != *model
            && !(pending.kind == Kind::Model
                && pending.selection.as_deref() == Some(menu.model.as_str()))
        {
            return Err(fail("Control readback changed the model unexpectedly"));
        }
        if pending.succeeded && pending.selection.as_deref() != Some(menu.current.as_str()) {
            return Err(fail("Runtime did not retain the requested selection"));
        }
        if let Some(error) = pending.failure.take() {
            menu.message = Some(format!(
                "Request failed: {error}. Current setting read back from runtime."
            ));
        } else if pending.selection.is_some() {
            menu.message = Some(format!("Current setting: {}", menu.current));
        }
        if menu.model != *model {
            self.require_work_identity_rebind()?;
            if self.retired_runtime_policy().is_some() {
                let id = self
                    .wire
                    .register_read()
                    .map_err(|_| fail("Cannot reserve model target policy readback"))?;
                let command =
                    json!({"type":"gui.state","id":id.to_value(),"include_conversations":false});
                pending.id = id;
                pending.target_menu = Some(menu);
                self.control = Some(pending);
                return Ok(Step::Write(command));
            }
            self.expected.as_mut().expect("active").2 = menu.model.clone();
            self.origin = None;
            self.prompt = None;
        }
        self.menu = Some(menu.clone());
        Ok(Step::Control(Update::Choices(menu)))
    }
}

impl Management {
    pub fn request_queue(&mut self, intent: crate::restored_queue::Intent) -> io::Result<Value> {
        use crate::restored_queue::{CANCEL, Intent, READ, RECONCILE, RESOLVE};
        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        let mut command = json!({"type":READ, "session_id":session});
        let mut reconciled = None;
        let resolving = matches!(intent, Intent::Resolve { .. });
        let closed = match intent {
            Intent::Open => None,
            Intent::Continue { .. } => {
                return Err(fail("Continuation must reserve an execution command"));
            }
            Intent::Reconcile { revision, entry } => {
                let menu = self
                    .queue_menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
                let row = menu
                    .entries
                    .iter()
                    .find(|e| e.id == entry && e.can_reconcile)
                    .ok_or_else(|| fail("Entry was not offered for history reconciliation"))?;
                command["type"] = json!(RECONCILE);
                command["snapshot"] = json!(menu.token);
                command["entry_id"] = json!(entry);
                reconciled = Some(Reconciliation {
                    entry: row.clone(),
                    accepted: None,
                });
                None
            }
            Intent::Cancel { revision, entry } | Intent::Resolve { revision, entry } => {
                let menu = self
                    .queue_menu
                    .as_ref()
                    .filter(|m| m.revision == revision)
                    .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
                if !menu.entries.iter().any(|e| {
                    e.id == entry
                        && if resolving {
                            e.can_resolve
                        } else {
                            e.can_cancel
                        }
                }) {
                    return Err(fail(
                        "Entry was not offered for the requested recovery action",
                    ));
                }
                command["type"] = json!(if resolving { RESOLVE } else { CANCEL });
                command["snapshot"] = json!(menu.token);
                command["entry_id"] = json!(entry);
                Some(QueueClosure {
                    entry,
                    resolved: resolving,
                })
            }
        };
        if !self.operations.iter().any(|v| v == READ)
            || !self
                .operations
                .iter()
                .any(|v| Some(v.as_str()) == command["type"].as_str())
        {
            return Err(fail(
                "The connected runtime does not support held-input controls",
            ));
        }
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve queue control"))?;
        command["id"] = id.to_value();
        self.queue = Some(QueuePending {
            id,
            applying: closed.is_some() || reconciled.is_some(),
            reconciled,
            closed,
            continued: None,
            failure: None,
            succeeded: false,
        });
        self.queue_menu = None;
        self.browser_page = None;
        self.menu = None;
        Ok(command)
    }
    fn queue_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::restored_queue::{READ, Snapshot, Update};
        let mut pending = self.queue.take().expect("matched queue request");
        let success = frame["ok"] == true;
        let error = if success {
            None
        } else {
            Some(text(&frame, "error", 4096)?.to_owned())
        };
        if pending.applying {
            if let Some(closed) = &pending.closed
                && closed.resolved
                && success
            {
                let result = &frame["result"];
                if result["resolved"] != closed.entry
                    || result["session_id"] != self.expected.as_ref().expect("active").0
                    || result["replayed"] != false
                    || result["effects_verified"] != false
                {
                    return Err(fail(
                        "Resolution receipt changed identity or claimed provider effects",
                    ));
                }
            }
            if let Some(check) = &mut pending.reconciled
                && success
            {
                let result = &frame["result"];
                if result["entry_id"] != check.entry.id
                    || result["session_id"] != self.expected.as_ref().expect("active").0
                {
                    return Err(fail("History reconciliation receipt changed identity"));
                }
                check.accepted = Some(
                    result["reconciled"]
                        .as_bool()
                        .ok_or_else(|| fail("Missing history reconciliation outcome"))?,
                );
            }
            self.work_reconciliation_echo(
                &frame["result"],
                pending.reconciled.as_ref().and_then(|check| check.accepted),
            )?;
            if self.closing {
                self.discard_work_reconciliation();
                self.queue_menu = None;
                return Ok(Step::Ignore);
            }
            pending.applying = false;
            pending.failure = error;
            pending.succeeded = success;
            let id = self
                .wire
                .register_request()
                .map_err(|_| fail("Cannot reserve queue readback"))?;
            let mut command = json!({"type":READ,"id":id.to_value(),"session_id":self.expected.as_ref().expect("active").0});
            self.mark_work_reconciliation_readback(&mut command);
            pending.id = id;
            self.queue = Some(pending);
            return Ok(Step::Write(command));
        }
        if let Some(error) = error {
            return if pending.closed.is_some()
                || pending.continued.is_some()
                || pending.reconciled.is_some()
            {
                Err(fail(&format!(
                    "Cannot verify saved queue after mutation: {error}"
                )))
            } else {
                Ok(Step::Queue(Update::Rejected(error)))
            };
        }
        self.ensure_drained()?;
        self.revision = self
            .revision
            .checked_add(1)
            .ok_or_else(|| fail("Queue revision exhausted"))?;
        let mut snapshot = Snapshot::parse(&frame["result"], self.revision)?;
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        if (&snapshot.session, &snapshot.backend, &snapshot.model) != (session, backend, model) {
            return Err(fail("Queue readback changed runtime identity"));
        }
        if pending.succeeded
            && snapshot
                .entries
                .iter()
                .any(|e| Some(&e.id) == pending.closed.as_ref().map(|c| &c.entry))
        {
            return Err(fail("Runtime still holds the closed entry"));
        }
        if let Some((operation, entry, refused)) = &pending.continued {
            let result = &frame["result"];
            if result["operation_id"] != *operation
                || result["entry_id"] != entry.id
                || result["continuation_outcome"] != if *refused { "refused" } else { "accepted" }
            {
                return Err(fail(
                    "Continuation readback disagrees with its execution receipt",
                ));
            }
            let held = snapshot.entries.iter().find(|row| row.id == entry.id);
            if entry.provider == "claude" {
                let state = result["continuation_state"].as_str();
                if result["continuation_provider"] != "claude"
                    || !matches!(state, Some("prepared" | "uncertain" | "terminal"))
                    || (*refused && state != Some("prepared"))
                    || held.is_some() != (state != Some("terminal"))
                {
                    return Err(fail(
                        "Claude continuation readback lacks consistent delivery evidence",
                    ));
                }
                if let Some(row) = held
                    && (Some(row.state.as_str()) != state
                        || row.provider != entry.provider
                        || row.content != entry.content
                        || row.source != entry.source
                        || row.profile != entry.profile
                        || row.segment != entry.segment
                        || row.workspace != entry.workspace
                        || (!entry.thread.is_empty() && row.thread != entry.thread))
                {
                    return Err(fail("Held Claude delivery changed after continuation"));
                }
            } else if held.is_some() != *refused {
                return Err(fail("Continuation readback disagrees with held input"));
            }
        }
        if let Some(check) = &pending.reconciled {
            let held = snapshot.entries.iter().find(|e| e.id == check.entry.id);
            match check.accepted {
                Some(true) if held.is_none() => {
                    snapshot.recovered = Some(Box::new(check.entry.clone()))
                }
                Some(false) if held == Some(&check.entry) => {}
                None => {}
                _ => {
                    return Err(fail(
                        "Saved queue contradicts provider-history reconciliation",
                    ));
                }
            }
        }
        self.verify_work_reconciliation_readback(&frame["result"])?;
        if self.closing {
            self.discard_work_reconciliation();
            self.queue_menu = None;
            return Ok(Step::Ignore);
        }
        self.finish_work_reconciliation(
            pending.reconciled.as_ref().and_then(|check| check.accepted),
        )?;
        snapshot.message = pending
            .failure
            .map(|e| format!("Request failed: {e}. Saved queue read back."))
            .or_else(|| {
                pending.closed.map(|closed| {
                    if closed.resolved {
                        "Marked resolved without replay; provider effects remain unverified.".into()
                    } else {
                        "Cancellation saved. Queue read back; nothing replayed.".into()
                    }
                })
            });
        if let Some((_, entry, _)) = &pending.continued {
            snapshot.message = Some(match snapshot.entries.iter().find(|row| row.id == entry.id) {
                Some(row) if row.provider == "claude" && row.state == "prepared" =>
                    "Continuation settled; input remains unsent and held. Saved queue read back.".into(),
                Some(row) if row.provider == "claude" && row.state == "uncertain" =>
                    "Continuation settled; delivery is uncertain and held without replay. Saved queue read back.".into(),
                _ => "Continuation settled; saved queue read back.".into(),
            });
        }
        if let Some(check) = pending.reconciled
            && let Some(accepted) = check.accepted
        {
            snapshot.message = Some(
                if accepted {
                    "Provider history confirmed acceptance. Recorded locally; nothing resent."
                } else {
                    "No matching acceptance found. Input remains held; nothing resent."
                }
                .into(),
            );
        }
        self.queue_menu = Some(snapshot.clone());
        Ok(Step::Queue(Update::Snapshot(snapshot)))
    }
}

impl Management {
    pub fn prepare_continuation(&self, revision: u64, entry: &str) -> io::Result<Continuation> {
        use crate::restored_queue::{CONTINUE, SETTLED};
        if !self.ready() {
            return Err(fail("Runtime is not ready for continuation"));
        }
        self.ensure_drained()?;
        if ![CONTINUE, SETTLED]
            .iter()
            .all(|op| self.operations.iter().any(|value| value == op))
        {
            return Err(fail(
                "Runtime lacks continuation/settlement support; update Python source",
            ));
        }
        let menu = self
            .queue_menu
            .as_ref()
            .filter(|m| m.revision == revision)
            .ok_or_else(|| fail("Held input changed; reopen the inspector"))?;
        let row = menu
            .entries
            .iter()
            .find(|row| row.id == entry && row.can_continue)
            .ok_or_else(|| fail("Entry was not offered for continuation"))?;
        Ok(Continuation {
            command: json!({"type":CONTINUE,"session_id":menu.session,"entry_id":row.id,"snapshot":menu.token}),
            prompt: row.content.clone(),
            session: menu.session.clone(),
            entry: row.id.clone(),
            record: row.clone(),
        })
    }
    pub fn begin_continuation(
        &mut self,
        mut prepared: Continuation,
        id: &str,
    ) -> io::Result<Value> {
        self.prompt(id)?;
        self.continuation = Some((id.to_owned(), prepared.record));
        prepared.command["id"] = json!(id);
        Ok(prepared.command)
    }
    pub fn needs_continuation_readback(&self) -> bool {
        self.continuation.is_some()
    }
    pub fn read_continuation(&mut self, refused: bool) -> io::Result<Value> {
        let (operation, entry) = self
            .continuation
            .take()
            .ok_or_else(|| fail("No continuation to settle"))?;
        if self.queue.is_some() || self.control.is_some() {
            return Err(fail("Concurrent continuation readback"));
        }
        self.ensure_drained()?;
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve continuation readback"))?;
        let command = json!({"type":crate::restored_queue::SETTLED,"id":id.to_value(),
            "session_id":self.expected.as_ref().expect("active").0,"operation_id":operation,"entry_id":entry.id});
        self.queue = Some(QueuePending {
            id,
            applying: false,
            closed: None,
            continued: Some((operation, entry, refused)),
            reconciled: None,
            failure: None,
            succeeded: false,
        });
        Ok(command)
    }
}

impl Management {
    pub fn request_browser(
        &mut self,
        intent: crate::conversation_browser::Intent,
    ) -> io::Result<Value> {
        use crate::conversation_browser::{Intent, OPEN, PAGE, PREVIEW};
        if let Intent::Action(intent) = intent {
            return self.request_conversation_action(intent);
        }

        if !self.ready() {
            return Err(fail("Wait for the runtime operation to finish"));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail(
                "Runtime omitted owned-agent browser authority; update Python source",
            ));
        }

        if ![
            PAGE,
            if self.history_projection {
                crate::history::SNAPSHOT
            } else {
                PREVIEW
            },
            if self.history_projection {
                crate::history::OPEN
            } else {
                OPEN
            },
            "gui.state",
            crate::restored_queue::READ,
        ]
        .iter()
        .all(|op| self.operations.iter().any(|value| value == op))
        {
            return Err(fail(
                "Connected runtime does not support the conversation browser",
            ));
        }
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        if let Intent::ConfirmOpen {
            snapshot,
            action_id,
        } = &intent
        {
            let preview = self
                .browser_preview
                .as_ref()
                .filter(|p| {
                    p.token == *snapshot
                        && p.open_action_id.as_ref() == Some(action_id)
                        && p.identity.from_session == *session
                        && p.identity.backend == *backend
                        && p.identity.model == *model
                })
                .ok_or_else(|| fail("Conversation preview is stale; explicitly review again"))?
                .clone();
            let mut command = preview.open_request()?;
            let id = self
                .wire
                .register_request()
                .map_err(|_| fail("Cannot reserve reviewed open"))?;
            command["id"] = id.to_value();
            self.browser = Some(BrowserPending {
                id: id.clone(),
                request: BrowserRequest::Open {
                    target: preview.identity.session.clone(),
                    agent: preview.identity.agent.clone(),
                    preview: Some(Box::new(preview)),
                    mutation: id,
                    stage: 0,
                    history: None,
                    state: None,
                    policy: None,
                    model: None,
                },
            });
            self.browser_preview = None;
            self.menu = None;
            self.queue_menu = None;
            return Ok(command);
        }
        let (operation, request, mut command) = match intent {
            Intent::Action(_) | Intent::ConfirmOpen { .. } => unreachable!("handled action"),
            Intent::Inspect { .. } => {
                return Err(fail(
                    "Inspect requires the separate Saved History read route",
                ));
            }
            Intent::Refresh => (
                PAGE,
                BrowserRequest::Page {
                    previous: None,
                    cursor: None,
                },
                json!({"session_id":session,"cursor":null}),
            ),
            Intent::Page { revision, cursor } => {
                let page = self
                    .browser_page
                    .as_ref()
                    .filter(|page| page.revision == revision)
                    .ok_or_else(|| fail("Conversation page changed; refresh the browser"))?;
                if page.previous.as_deref() != Some(cursor.as_str())
                    && page.next.as_deref() != Some(cursor.as_str())
                {
                    return Err(fail("Conversation page cursor was not offered"));
                }
                (
                    PAGE,
                    BrowserRequest::Page {
                        previous: Some(page.clone()),
                        cursor: Some(cursor.clone()),
                    },
                    json!({"session_id":session,"cursor":cursor}),
                )
            }
            Intent::Open {
                revision,
                session: target,
            } => {
                let page = self
                    .browser_page
                    .as_ref()
                    .ok_or_else(|| fail("Refresh the conversation browser before opening"))?;
                page.selection(revision, &target)?;
                let command = if self.history_projection {
                    crate::history::Identity {
                        from_session: session.clone(),
                        session: target.clone(),
                        agent: page.agent.clone(),
                        backend: backend.clone(),
                        model: model.clone(),
                        scope: crate::history::Scope::Preview,
                        catalog: Some(page.snapshot.clone()),
                    }
                    .command(crate::history::SNAPSHOT)
                } else {
                    json!({"session_id":target,"from_session_id":session,"snapshot":page.snapshot})
                };
                self.browser_preview = None;
                (
                    if self.history_projection {
                        crate::history::SNAPSHOT
                    } else {
                        PREVIEW
                    },
                    BrowserRequest::Preview {
                        target: target.clone(),
                        agent: page.agent.clone(),
                        snapshot: page.snapshot.clone(),
                    },
                    command,
                )
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve conversation operation"))?;
        command["type"] = json!(operation);
        command["id"] = id.to_value();
        self.browser = Some(BrowserPending { id, request });
        self.menu = None;
        self.queue_menu = None;
        Ok(command)
    }
    fn browser_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::conversation_browser::{Opened, Page, Update};
        let mut pending = self.browser.take().expect("matched browser");
        self.ensure_drained()?;
        let success = frame["ok"] == true;
        match &mut pending.request {
            BrowserRequest::PreviewPage { descriptor } => {
                if !success {
                    self.browser_preview = None;
                    return Ok(Step::Browser(Update::Rejected(
                        text(&frame, "error", 4096)?.into(),
                    )));
                }
                let page = crate::history::Page::parse(&frame["result"], descriptor)?;
                page.accepts(&crate::history::PageRequest::Anchor(
                    crate::history::Anchor::Tail,
                ))?;
                if self.closing {
                    return Ok(Step::Ignore);
                }
                self.browser_preview = Some(descriptor.clone());
                Ok(Step::Browser(Update::Preview(Box::new(descriptor.clone()))))
            }
            BrowserRequest::Preview {
                target,
                agent,
                snapshot,
            } => {
                if !success {
                    self.browser_page = None;
                    return Ok(Step::Browser(Update::Rejected(
                        text(&frame, "error", 4096)?.to_owned(),
                    )));
                }
                let result = &frame["result"];
                let current = &self.expected.as_ref().expect("active").0;
                if self.history_projection {
                    let (_, backend, model, _) = self.expected.as_ref().expect("active");
                    let identity = crate::history::Identity {
                        from_session: current.clone(),
                        session: target.clone(),
                        agent: agent.clone(),
                        backend: backend.clone(),
                        model: model.clone(),
                        scope: crate::history::Scope::Preview,
                        catalog: Some(snapshot.clone()),
                    };
                    let descriptor = crate::history::Snapshot::parse(result, &identity)?;
                    if self.closing {
                        return Ok(Step::Ignore);
                    }
                    let id = self
                        .wire
                        .register_read()
                        .map_err(|_| fail("Cannot reserve preview page"))?;
                    let mut command = descriptor.page_request(
                        &crate::history::PageRequest::Anchor(crate::history::Anchor::Tail),
                    )?;
                    command["id"] = id.to_value();
                    self.browser = Some(BrowserPending {
                        id,
                        request: BrowserRequest::PreviewPage { descriptor },
                    });
                    return Ok(Step::Write(command));
                }
                if result["session_id"] != *target
                    || result["from_session_id"] != *current
                    || result["agent_id"] != *agent
                    || result["snapshot"] != *snapshot
                    || result["read_only"] != false
                {
                    return Err(fail(
                        "Conversation preview changed selection or owned authority",
                    ));
                }
                if let Err(error) = crate::resume::History::parse(result, target) {
                    self.browser_page = None;
                    return Ok(Step::Browser(Update::Rejected(format!(
                        "Conversation cannot be displayed: {error}. Current conversation remains open."
                    ))));
                }
                let id = self
                    .wire
                    .register_request()
                    .map_err(|_| fail("Cannot reserve verified conversation open"))?;
                let command = json!({"type":crate::conversation_browser::OPEN,"id":id.to_value(),"session_id":target,
                    "from_session_id":current,"snapshot":snapshot});
                self.browser = Some(BrowserPending {
                    id: id.clone(),
                    request: BrowserRequest::Open {
                        preview: None,
                        target: target.clone(),
                        agent: agent.clone(),
                        mutation: id,
                        stage: 0,
                        history: None,
                        state: None,
                        policy: None,
                        model: None,
                    },
                });
                Ok(Step::Write(command))
            }
            BrowserRequest::Page { previous, cursor } => {
                if !success {
                    self.browser_page = None;
                    return Ok(Step::Browser(Update::Rejected(
                        text(&frame, "error", 4096)?.to_owned(),
                    )));
                }
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Conversation revision exhausted"))?;
                let page = Page::parse(
                    &frame["result"],
                    self.revision,
                    &self.expected.as_ref().expect("active").0,
                )?;
                if self
                    .browser_authority
                    .as_ref()
                    .map(|value| value.0.as_str())
                    != Some(page.agent.as_str())
                {
                    return Err(fail("Conversation page changed owned agent"));
                }
                if let (Some(previous), Some(cursor)) = (previous, cursor) {
                    page.follows(previous, cursor)?;
                }
                self.browser_page = Some(page.clone());
                Ok(Step::Browser(Update::Page(page)))
            }
            BrowserRequest::Open {
                preview,
                target,
                agent,
                stage,
                history,
                state,
                policy,
                model: transition_model,
                ..
            } => {
                if !success {
                    return Err(fail(&format!(
                        "Conversation open/readback failed; session is uncertain: {}",
                        text(&frame, "error", 4096)?
                    )));
                }
                let result = &frame["result"];
                if self.closing {
                    return Ok(Step::Ignore);
                }
                if preview.is_some() && *stage == 1 {
                    let before = history
                        .as_ref()
                        .and_then(|h| h.descriptor.as_deref())
                        .expect("open receipt history");
                    let after = crate::history::Snapshot::parse(result, &before.identity)?;
                    if &after != before || after.read_only {
                        return Err(fail("Opened history changed before independent readback"));
                    }
                } else if preview.is_some() && *stage == 2 {
                    let history = history.as_mut().expect("open history");
                    let page =
                        crate::history::Page::parse(result, history.descriptor.as_ref().unwrap())?;
                    page.accepts(&crate::history::PageRequest::Anchor(
                        crate::history::Anchor::Tail,
                    ))?;
                    history.page = Some(Box::new(page));
                } else {
                    let logical_stage = if preview.is_some() && *stage >= 3 {
                        *stage - 2
                    } else {
                        *stage
                    };
                    match logical_stage {
                        0 => {
                            if let Some(preview) = preview {
                                match crate::history::OpenResult::parse(result, preview)? {
                                    crate::history::OpenResult::Refused(reason) => {
                                        self.work_transition_receipt(result, false)?;
                                        self.browser_preview = None;
                                        return Ok(Step::Browser(Update::Rejected(reason)));
                                    }
                                    crate::history::OpenResult::Connected(descriptor) => {
                                        *history =
                                            Some(crate::resume::History::projected(*descriptor));
                                    }
                                }
                            } else {
                                *history = Some(crate::conversation_browser::connected_history(
                                    result, target,
                                )?);
                            }
                            self.work_transition_receipt(result, true)?;
                        }
                        1 => {
                            let snapshot = Snapshot::parse(result)?;
                            let (_, backend, model, profile) =
                                self.expected.as_ref().expect("active");
                            self.verify_target_profile(&snapshot, &result["tool_policy"], profile)?;
                            if snapshot.session != *target
                                || snapshot.backend != *backend
                                || !snapshot.writable
                                || snapshot.held_restored_input.is_none()
                                || self.browser_authority.as_ref().map(|value| value.1)
                                    != Some(snapshot.schedules_paused)
                                || result["ownership"]["agent_id"] != *agent
                                || (snapshot.model != *model
                                    && transition_model.as_ref() != Some(&snapshot.model))
                                || transition_model
                                    .as_ref()
                                    .is_some_and(|value| value != &snapshot.model)
                            {
                                return Err(fail(
                                    "Opened conversation state contradicts its owned identity, policy, or model transition",
                                ));
                            }
                            *policy = Some(result["tool_policy"].clone());
                            *state = Some(snapshot);
                        }
                        2 => {
                            self.revision = self
                                .revision
                                .checked_add(1)
                                .ok_or_else(|| fail("Conversation revision exhausted"))?;
                            let queue =
                                crate::restored_queue::Snapshot::parse(result, self.revision)?;
                            let snapshot = state.take().expect("state read");
                            if queue.session != snapshot.session
                                || queue.backend != snapshot.backend
                                || queue.model != snapshot.model
                                || Some(queue.entries.len()) != snapshot.held_restored_input
                            {
                                return Err(fail(
                                    "Opened conversation held-input readback contradicts state",
                                ));
                            }
                            self.engine_instance = None;
                            self.engine_snapshot = None;
                            self.engine_review = None;
                            self.expected = Some((
                                snapshot.session.clone(),
                                snapshot.backend.clone(),
                                snapshot.model.clone(),
                                snapshot.profile.clone(),
                            ));
                            self.prompt = None;
                            self.origin = None;
                            self.menu = None;
                            self.browser_page = None;
                            self.queue_menu = Some(queue.clone());
                            return Ok(Step::Opened(Box::new(Opened {
                                snapshot,
                                history: history.take().expect("connected history"),
                                queue,
                                policy: policy.take().expect("policy read"),
                            })));
                        }
                        _ => return Err(fail("Invalid conversation opening phase")),
                    }
                }
                *stage += 1;
                let id = self
                    .wire
                    .register_read()
                    .map_err(|_| fail("Cannot reserve conversation readback"))?;
                let mut command = if preview.is_some() {
                    match *stage {
                        1 => history
                            .as_ref()
                            .unwrap()
                            .descriptor
                            .as_ref()
                            .unwrap()
                            .identity
                            .command(crate::history::SNAPSHOT),
                        2 => history
                            .as_ref()
                            .unwrap()
                            .descriptor
                            .as_ref()
                            .unwrap()
                            .page_request(&crate::history::PageRequest::Anchor(
                                crate::history::Anchor::Tail,
                            ))?,
                        3 => json!({"type":"gui.state","include_conversations":false}),
                        4 => json!({"type":crate::restored_queue::READ,"session_id":target}),
                        _ => return Err(fail("Invalid compact open phase")),
                    }
                } else if *stage == 1 {
                    json!({"type":"gui.state","include_conversations":false})
                } else {
                    json!({"type":crate::restored_queue::READ,"session_id":target})
                };
                command["id"] = id.to_value();
                pending.id = id;
                self.browser = Some(pending);
                Ok(Step::Write(command))
            }
        }
    }
}

impl Management {
    pub fn request_backend(
        &mut self,
        intent: crate::backend_controls::Intent,
    ) -> io::Result<Value> {
        use crate::backend_controls::{Intent, LIST, SET};
        if !self.ready() {
            return Err(fail("Wait for the current runtime operation"));
        }
        self.ensure_drained()?;
        let (session, _, _, _) = self.expected.as_ref().expect("active");
        if self.browser_authority.is_none()
            || ![
                LIST,
                SET,
                "gui.state",
                self.history_operation(),
                crate::restored_queue::READ,
            ]
            .iter()
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail(
                "Runtime lacks owned backend transition support; update Python source",
            ));
        }
        let (stage, selection, catalog, mut command) = match intent {
            Intent::Read => (0, None, None, json!({"type":LIST,"session_id":session})),
            Intent::Select {
                revision,
                name,
                model,
            } => {
                let catalog = self
                    .backend_catalog
                    .as_ref()
                    .ok_or_else(|| fail("Refresh the backend catalog before selecting"))?;
                catalog.selection(revision, &name, &model)?;
                (
                    1,
                    Some((name, model)),
                    Some(catalog.clone()),
                    self.history_command(
                        session,
                        &self.expected.as_ref().unwrap().1,
                        &self.expected.as_ref().unwrap().2,
                    )?,
                )
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve backend operation"))?;
        command["id"] = id.to_value();
        self.backend = Some(BackendPending {
            id,
            stage,
            selection,
            catalog,
            mutation: None,
            history: None,
            state: None,
            policy: None,
            observed_model: false,
        });
        self.menu = None;
        self.browser_page = None;
        self.queue_menu = None;
        Ok(command)
    }
    fn backend_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::backend_controls::{Catalog, Changed, LIST, SET, Update};
        self.ensure_drained()?;
        let mut pending = self.backend.take().expect("matched backend");
        let (session, old_backend, old_model, profile) =
            self.expected.as_ref().expect("active").clone();
        let (agent, schedules) = self
            .browser_authority
            .as_ref()
            .expect("owned agent")
            .clone();
        if frame["ok"] != true {
            if pending.stage <= 1 {
                self.backend_catalog = None;
                return Ok(Step::Backend(Update::Rejected(
                    text(&frame, "error", 4096)?.into(),
                )));
            }
            return Err(fail(
                "Backend mutation/readback failed; runtime identity is uncertain",
            ));
        }
        let result = &frame["result"];
        if pending.stage == 0 {
            self.revision = self
                .revision
                .checked_add(1)
                .ok_or_else(|| fail("Backend revision exhausted"))?;
            let catalog = Catalog::parse(result, self.revision, &session, &agent)?;
            if catalog.selected != old_backend || catalog.model != old_model {
                return Err(fail("Backend catalog changed active runtime identity"));
            }
            self.backend_catalog = Some(catalog.clone());
            return Ok(Step::Backend(Update::Catalog(catalog)));
        }
        let (name, model) = pending.selection.as_ref().expect("target").clone();
        match pending.stage {
            1 => match self.parse_history(result, &session, &old_backend, &old_model) {
                Ok(history) => pending.history = Some(history),
                Err(error) => {
                    self.backend_catalog = None;
                    return Ok(Step::Backend(Update::Rejected(format!(
                        "Cannot verify preserved conversation: {error}"
                    ))));
                }
            },
            2 => {
                let catalog = pending.catalog.as_ref().expect("catalog");
                if result["version"].as_u64() != Some(1)
                    || result["kind"] != "backend_transition"
                    || result["session_id"] != session
                    || result["agent_id"] != agent
                    || result["snapshot"] != catalog.snapshot
                {
                    return Err(fail("Backend transition receipt changed owned selection"));
                }
                match result["status"].as_str() {
                    Some("refused")
                        if result["mutated"] == false
                            && result["close_required"] == false
                            && result["backend"] == old_backend
                            && result["model"] == old_model
                            && !pending.observed_model =>
                    {
                        self.work_transition_receipt(result, false)?;
                        self.backend_catalog = None;
                        return Ok(Step::Backend(Update::Rejected(
                            text(result, "error", 4096)?.into(),
                        )));
                    }
                    Some("connected")
                        if result["mutated"] == true
                            && result["close_required"] == false
                            && result["backend"] == name
                            && result["model"] == model
                            && result["error"].is_null()
                            && pending.observed_model =>
                    {
                        self.work_transition_receipt(result, true)?;
                    }
                    _ => {
                        return Err(fail(
                            "Backend transition failed or contradicted its receipt; connection closed",
                        ));
                    }
                }
            }
            3 => {
                let snapshot = Snapshot::parse(result)?;
                self.verify_target_profile(&snapshot, &result["tool_policy"], &profile)?;
                if snapshot.session != session
                    || snapshot.backend != name
                    || snapshot.model != model
                    || (!snapshot.writable
                        && pending.history.as_ref().is_none_or(|history| {
                            !history
                                .descriptor
                                .as_ref()
                                .map_or(history.messages.is_empty(), |d| d.pending)
                        }))
                    || snapshot.held_restored_input != Some(0)
                    || snapshot.schedules_paused != schedules
                    || result["ownership"]["agent_id"] != agent
                {
                    return Err(fail(
                        "Backend state readback contradicts target, ownership, or held-input policy",
                    ));
                }
                pending.policy = Some(result["tool_policy"].clone());
                pending.state = Some(snapshot);
            }
            4 => {
                let history = self.parse_history(result, &session, &name, &model)?;
                if !pending
                    .history
                    .as_ref()
                    .is_some_and(|before| history.preserves_context(before))
                {
                    return Err(fail("Backend transition changed the saved conversation"));
                }
            }
            5 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Backend revision exhausted"))?;
                let catalog = Catalog::parse(result, self.revision, &session, &agent)?;
                if catalog.selected != name
                    || catalog.model != model
                    || catalog.configured != name
                    || catalog.requested_native
                        != pending
                            .catalog
                            .as_ref()
                            .expect("original catalog")
                            .requested_native
                {
                    return Err(fail("Backend catalog readback contradicts selected target"));
                }
                pending.catalog = Some(catalog);
            }
            6 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Backend revision exhausted"))?;
                let queue = crate::restored_queue::Snapshot::parse(result, self.revision)?;
                if queue.session != session
                    || queue.backend != name
                    || queue.model != model
                    || !queue.entries.is_empty()
                {
                    return Err(fail(
                        "Backend held-input readback contradicts verified state",
                    ));
                }
                let snapshot = pending.state.take().expect("state");
                let catalog = pending.catalog.take().expect("catalog");
                self.engine_instance = None;
                self.engine_snapshot = None;
                self.engine_review = None;
                self.expected = Some((session, name, model, snapshot.profile.clone()));
                self.prompt = None;
                self.origin = None;
                self.menu = None;
                self.browser_page = None;
                self.backend_catalog = Some(catalog.clone());
                self.queue_menu = Some(queue.clone());
                return Ok(Step::BackendChanged(Box::new(Changed {
                    snapshot,
                    catalog,
                    history: pending.history.take().expect("history"),
                    queue,
                    policy: pending.policy.take().expect("policy"),
                })));
            }
            _ => return Err(fail("Invalid backend transition phase")),
        }
        pending.stage += 1;
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve backend readback"))?;
        let mut command = match pending.stage {
            2 => {
                pending.mutation = Some(id.clone());
                json!({"type":SET,"version":1,"session_id":session,"agent_id":agent,"snapshot":pending.catalog.as_ref().expect("catalog").snapshot,"name":name,"model":model})
            }
            3 => json!({"type":"gui.state","include_conversations":false}),
            4 => self.history_command(&session, &name, &model)?,
            5 => json!({"type":LIST,"session_id":session}),
            6 => json!({"type":crate::restored_queue::READ,"session_id":session}),
            _ => return Err(fail("Invalid backend readback operation")),
        };
        command["id"] = id.to_value();
        pending.id = id;
        self.backend = Some(pending);
        Ok(Step::Write(command))
    }
}

impl Management {
    pub fn request_settings(&mut self, intent: crate::settings_view::Intent) -> io::Result<Value> {
        if matches!(
            intent,
            crate::settings_view::Intent::Editor(_) | crate::settings_view::Intent::Compaction(_)
        ) {
            return self.request_settings_editor(intent);
        }
        if !self.ready() {
            return Err(fail(
                "Wait for the runtime operation before reading settings",
            ));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail(
                "Runtime omitted owned settings authority; update Python source",
            ));
        }
        let (request, runtime_status) = match intent {
            crate::settings_view::Intent::Mcp(r) => (Some(PreferencesRequest::Mcp(r)), false),
            crate::settings_view::Intent::ModelPreferences(r) => {
                (Some(PreferencesRequest::ModelPreferences(r)), false)
            }
            crate::settings_view::Intent::Glassbox(r) => {
                (Some(PreferencesRequest::Glassbox(r)), false)
            }
            crate::settings_view::Intent::ModelLifecycle(r) => {
                (Some(PreferencesRequest::ModelLifecycle(r)), false)
            }
            crate::settings_view::Intent::Voice(r) => (Some(PreferencesRequest::Voice(r)), false),
            crate::settings_view::Intent::RuntimeStatus => (None, true),
            _ => (None, false),
        };
        let (mut command, read) = if let Some(request) = request {
            if request
                .operations()
                .iter()
                .any(|name| !self.operations.iter().any(|op| op == name))
            {
                return Err(fail("Update the runtime to use these settings controls"));
            }
            let identity = self.work_identity()?;
            let instance = self.context_instance()?.to_owned();
            (
                request.command(&identity, &instance)?,
                SettingsRead::Preferences(identity, instance, request),
            )
        } else if runtime_status {
            let glassbox = self
                .operations
                .iter()
                .any(|op| op == crate::inspection::WITH_GLASSBOX);
            if !glassbox
                && !self
                    .operations
                    .iter()
                    .any(|op| op == crate::inspection::OPERATION)
            {
                return Err(fail("This runtime does not support status inspection"));
            }
            let identity = self.work_identity()?;
            let instance = self.context_instance()?.to_owned();
            let mut command = crate::inspection::command(&identity, &instance);
            if glassbox {
                command["type"] = json!(crate::inspection::WITH_GLASSBOX);
            }
            (
                command,
                SettingsRead::RuntimeStatus {
                    identity,
                    instance,
                    glassbox,
                },
            )
        } else {
            (
                json!({"type":"gui.state","include_conversations":false}),
                SettingsRead::Values,
            )
        };
        let id = if matches!(&read, SettingsRead::Preferences(_,_,request) if request.is_apply()) {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve settings request"))?;
        command["id"] = id.to_value();
        self.settings = Some((id, read));
        Ok(command)
    }
    fn settings_response(&mut self, frame: Value) -> io::Result<Step> {
        let (_, read) = self.settings.take().expect("matched settings read");
        self.ensure_drained()?;
        if let SettingsRead::Preferences(identity, instance, request) = read {
            let reject = request.rejected();
            let result = if frame["ok"] != true {
                Err(text(&frame, "error", 4096)?.to_string())
            } else if identity != self.work_identity()? || instance != self.context_instance()? {
                Err("Settings owner changed. Refresh to inspect it.".into())
            } else {
                request
                    .response(
                        &frame["result"],
                        &identity,
                        &instance,
                        text(&frame, "id", 128)?,
                    )
                    .map_err(|error| error.to_string())
            };
            let update = match result {
                Ok(PreferencesReply::Next(next)) => {
                    let mut value = next.command(&identity, &instance)?;
                    let id = self
                        .wire
                        .register_read()
                        .map_err(|_| fail("Cannot reserve settings readback"))?;
                    value["id"] = id.to_value();
                    self.settings = Some((id, SettingsRead::Preferences(identity, instance, next)));
                    return Ok(Step::Write(value));
                }
                Ok(PreferencesReply::Mcp(update)) => crate::settings_view::Update::Mcp(Ok(update)),
                Ok(PreferencesReply::ModelPreferences(update)) => {
                    crate::settings_view::Update::ModelPreferences(Ok(update))
                }
                Ok(PreferencesReply::Glassbox(update)) => {
                    crate::settings_view::Update::Glassbox(Ok(update))
                }
                Ok(PreferencesReply::ModelLifecycle(update)) => {
                    crate::settings_view::Update::ModelLifecycle(Ok(update))
                }
                Ok(PreferencesReply::Voice(update)) => {
                    crate::settings_view::Update::Voice(Ok(update))
                }
                Err(reason) => reject(reason),
            };
            return Ok(Step::Settings {
                update,
                policy: None,
            });
        }
        if let SettingsRead::RuntimeStatus {
            identity,
            instance,
            glassbox,
        } = read
        {
            let status = if frame["ok"] != true {
                Err(text(&frame, "error", 4096)?.to_owned())
            } else if identity != self.work_identity()? || instance != self.context_instance()? {
                Err("Runtime changed during inspection. Open status again.".into())
            } else {
                crate::inspection::Snapshot::parse_status(
                    &frame["result"],
                    &identity,
                    &instance,
                    glassbox,
                )
                .map(Box::new)
                .map_err(|error| error.to_string())
            };
            return Ok(Step::Settings {
                update: crate::settings_view::Update::RuntimeStatus(status),
                policy: None,
            });
        }
        if frame["ok"] != true {
            return Ok(Step::Settings {
                update: crate::settings_view::Update::Rejected(text(&frame, "error", 4096)?.into()),
                policy: None,
            });
        }
        let result = &frame["result"];
        let state = Snapshot::parse(result)?;
        let (session, backend, model, profile) = self.expected.as_ref().expect("active");
        let (agent, schedules) = self.browser_authority.as_ref().expect("authority");
        if (&state.session, &state.backend, &state.model, &state.profile)
            != (session, backend, model, profile)
            || state.held_restored_input.is_none()
            || state.schedules_paused != *schedules
            || result["ownership"]["agent_id"] != *agent
        {
            return Err(fail(
                "Settings read changed owned runtime identity or policy",
            ));
        }
        let update = match crate::settings_view::Snapshot::parse(result, session) {
            Ok(snapshot) => crate::settings_view::Update::Snapshot(snapshot),
            Err(error) => crate::settings_view::Update::Rejected(error.to_string()),
        };
        Ok(Step::Settings {
            update,
            policy: Some(result["tool_policy"].clone()),
        })
    }
}

impl Management {
    pub fn request_command(&mut self, intent: crate::command_palette::Intent) -> io::Result<Value> {
        use crate::command_palette::{Intent, LIST, PREPARE, RESOLVE};
        if !self.ready() {
            return Err(fail("Finish current work before opening commands"));
        }
        self.ensure_drained()?;
        if self.browser_authority.is_none() {
            return Err(fail("Runtime omitted owned command authority"));
        }
        let session = &self.expected.as_ref().expect("active").0;
        let mut pending = CommandPending {
            id: None,
            stage: 0,
            raw: None,
            row: None,
            arguments: String::new(),
            skills: false,
            reviewed: None,
        };
        let mut command = match intent {
            Intent::Read { skills } => {
                pending.skills = skills;
                self.command_prepared = None;
                json!({"type":LIST,"version":1,"session_id":session})
            }
            Intent::Raw(raw) => {
                if raw.len() > 65536 || !raw.trim_start().starts_with('/') {
                    return Err(fail("Enter a slash command within 64 KiB"));
                }
                pending.raw = Some(raw);
                self.command_prepared = None;
                json!({"type":LIST,"version":1,"session_id":session})
            }
            Intent::Resolve { snapshot, row } => {
                let c = self
                    .command_catalog
                    .as_ref()
                    .filter(|c| c.snapshot == snapshot)
                    .ok_or_else(|| fail("Refresh command catalog before selecting"))?;
                let entry = c
                    .entries
                    .iter()
                    .find(|e| e.id == row)
                    .ok_or_else(|| fail("Command row is absent"))?;
                if entry.action.is_none() {
                    return Err(fail(&entry.unavailable));
                }
                pending.stage = 1;
                pending.row = Some(row.clone());
                let mut v = c.request();
                v["type"] = json!(RESOLVE);
                v["row_id"] = json!(row);
                v["arguments"] = json!("");
                v
            }
            Intent::Prepare {
                snapshot,
                row,
                arguments,
            } => {
                let c = self
                    .command_catalog
                    .as_ref()
                    .filter(|c| c.snapshot == snapshot)
                    .ok_or_else(|| fail("Refresh skill catalog before preparing"))?;
                if !c.entries.iter().any(|e| e.id == row && e.kind == "skill")
                    || arguments.len() > 65536
                {
                    return Err(fail("Invalid skill row or argument budget"));
                }
                pending.stage = 2;
                pending.row = Some(row.clone());
                pending.arguments = arguments.clone();
                let mut v = c.request();
                v["type"] = json!(PREPARE);
                v["row_id"] = json!(row);
                v["arguments"] = json!(arguments);
                v["backend"] = json!(c.backend);
                v["model"] = json!(c.model);
                v
            }
            Intent::Submit { digest } => {
                let p = self
                    .command_prepared
                    .as_ref()
                    .filter(|p| p.digest == digest)
                    .ok_or_else(|| {
                        fail("Prepare and review this exact skill again before submitting")
                    })?;
                pending.stage = 3;
                pending.row = Some(p.row.clone());
                pending.arguments = p.arguments.clone();
                pending.reviewed = Some(p.clone());
                json!({"type":PREPARE,"version":1,"session_id":p.session,"agent_id":p.agent,"backend":p.backend,"model":p.model,"snapshot":p.snapshot,"row_id":p.row,"arguments":p.arguments})
            }
        };
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve command read"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.command = Some(pending);
        Ok(command)
    }
    fn command_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::command_palette::{Action, Catalog, PREPARE, RESOLVE, Route, Update};
        let publish = |update| Step::Commands {
            update,
            policy: None,
        };
        self.ensure_drained()?;
        let mut pending = self.command.take().expect("command pending");
        if frame["ok"] != true {
            return Ok(publish(Update::Rejected(
                text(&frame, "error", 4096)?.into(),
            )));
        }
        let v = &frame["result"];
        let (session, backend, model, _) = self.expected.as_ref().expect("active");
        let (agent, _) = self.browser_authority.as_ref().expect("authority");
        let mut command = match pending.stage {
            0 => {
                let c = Catalog::parse(v, session, agent, backend, model)?;
                self.command_catalog = Some(c.clone());
                if let Some(raw) = &pending.raw {
                    pending.stage = 1;
                    let mut command = c.request();
                    command["type"] = json!(RESOLVE);
                    command["command"] = json!(raw);
                    command
                } else {
                    return Ok(publish(Update::Catalog {
                        catalog: c,
                        skills: pending.skills,
                    }));
                }
            }
            1 => {
                let c = self.command_catalog.as_ref().expect("catalog");
                let r = Route::parse(
                    v,
                    c,
                    pending.row.as_deref(),
                    pending.raw.as_deref(),
                    &pending.arguments,
                )?;
                if r.action == Action::SkillPrepare {
                    pending.row = Some(r.row.clone());
                    pending.arguments = r.arguments.clone();
                    pending.stage = 2;
                    let mut command = c.request();
                    command["type"] = json!(PREPARE);
                    command["row_id"] = json!(r.row);
                    command["arguments"] = json!(r.arguments);
                    command["backend"] = json!(c.backend);
                    command["model"] = json!(c.model);
                    command
                } else {
                    return Ok(publish(Update::Route {
                        catalog: c.clone(),
                        route: r,
                    }));
                }
            }
            2 | 3 => {
                let c = self.command_catalog.as_ref().expect("catalog");
                let prepared = crate::prepared_skill::Prepared::parse(
                    v,
                    c,
                    pending.row.as_deref().expect("skillrow"),
                    &pending.arguments,
                )?;
                if let Err(error) = crate::live_chat::display_units(&prepared.prompt) {
                    return Ok(publish(Update::Rejected(error.to_string())));
                }
                if pending.stage == 3 && pending.reviewed.as_ref() != Some(&prepared) {
                    return Ok(publish(Update::Rejected("Skill file or prepared payload changed; prepare and review again before submitting".into())));
                }
                pending.stage = if pending.stage == 3 { 5 } else { 4 };
                pending.reviewed = Some(prepared);
                json!({"type":"gui.state","include_conversations":false})
            }
            4 | 5 => {
                let state = Snapshot::parse(v)?;
                let (_, _, _, profile) = self.expected.as_ref().expect("active");
                let (_, schedules) = self.browser_authority.as_ref().expect("authority");
                if (&state.session, &state.backend, &state.model, &state.profile)
                    != (session, backend, model, profile)
                    || state.held_restored_input.is_none()
                    || state.schedules_paused != *schedules
                    || v["ownership"]["agent_id"] != *agent
                {
                    return Err(fail("Prepared skill state readback changed owned runtime"));
                }
                let prepared = pending.reviewed.expect("verified preparation");
                let update = if pending.stage == 5 {
                    self.command_prepared = None;
                    Update::Verified(Box::new(prepared))
                } else {
                    self.command_prepared = Some(prepared.clone());
                    Update::Prepared {
                        catalog: Box::new(self.command_catalog.as_ref().expect("catalog").clone()),
                        prepared: Box::new(prepared),
                    }
                };
                return Ok(Step::Commands {
                    update,
                    policy: Some(v["tool_policy"].clone()),
                });
            }
            _ => return Err(fail("Invalid command operation stage")),
        };
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve command resolution"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.command = Some(pending);
        Ok(Step::Write(command))
    }
}

impl Management {
    fn engine_identity(&self) -> io::Result<crate::engine_controls::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Runtime has not negotiated"))?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Engine controls require owned agent identity"))?;
        let (instance, _, _) = self.engine_instance.as_ref().ok_or_else(|| {
            fail("Engine controls require startup ownership; update source or relaunch after changing backend/conversation")
        })?;
        Ok(crate::engine_controls::Identity {
            session: session.clone(),
            agent: agent.clone(),
            backend: backend.clone(),
            model: model.clone(),
            instance: instance.clone(),
        })
    }
    pub fn engine_timeout(&self) -> Option<std::time::Duration> {
        self.engine
            .as_ref()
            .and_then(|p| p.review.as_ref())
            .map(|r| std::time::Duration::from_millis(r.timeout_ms))
    }
    pub fn request_engine(&mut self, intent: crate::engine_controls::Intent) -> io::Result<Value> {
        use crate::engine_controls::{INSPECT, Intent, OPERATIONS};
        if !self.ready() {
            return Err(fail("Wait for the current runtime operation"));
        }
        self.ensure_drained()?;
        let identity = self.engine_identity()?;
        if !OPERATIONS
            .iter()
            .chain(
                [
                    "gui.state",
                    self.history_operation(),
                    crate::restored_queue::READ,
                ]
                .iter(),
            )
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail("Runtime lacks complete owned engine controls"));
        }
        let mut pending = EnginePending {
            config: None,
            id: None,
            stage: 0,
            action: None,
            snapshot: None,
            review: None,
            receipt: None,
            history: None,
            policy: None,
            state: None,
            mutation: None,
        };
        let mut command = match intent {
            Intent::Config(request) => {
                use crate::engine_config as cfg;
                if cfg::OPERATIONS
                    .iter()
                    .any(|op| !self.operations.iter().any(|cap| cap == op))
                {
                    return Err(fail("This source lacks reviewed engine configuration"));
                }
                let command = match &request {
                    cfg::Intent::Read => {
                        self.config_review = None;
                        self.config_snapshot = None;
                        identity.command(cfg::SNAPSHOT)
                    }
                    cfg::Intent::Review {
                        snapshot,
                        field,
                        value,
                    } => {
                        self.config_review = None;
                        let snap = self
                            .config_snapshot
                            .as_ref()
                            .filter(|s| s.token == *snapshot && s.id == identity)
                            .ok_or_else(|| fail("Refresh the current engine configuration"))?;
                        snap.review_command(field, value)?
                    }
                    cfg::Intent::Save(token) => self
                        .config_review
                        .as_ref()
                        .filter(|r| r.token == *token && r.snap.id == identity)
                        .ok_or_else(|| fail("Review the current engine preference again"))?
                        .save_command(),
                };
                self.engine_review = None;
                pending.config = Some(EngineConfigPending {
                    request,
                    receipt: None,
                });
                command
            }
            Intent::Read => {
                self.config_review = None;
                self.config_snapshot = None;
                self.engine_review = None;
                identity.command(INSPECT)
            }
            Intent::Validate { snapshot, action } => {
                let inspected = self
                    .engine_snapshot
                    .as_ref()
                    .filter(|s| s.token == snapshot)
                    .ok_or_else(|| fail("Refresh engine inspection before selecting"))?
                    .clone();
                inspected.request(action)?;
                pending.stage = 1;
                pending.action = Some(action);
                pending.snapshot = Some(inspected);
                json!({"type":"gui.state","include_conversations":false})
            }
            Intent::Execute { review } => {
                let (reviewed, history) = self
                    .engine_review
                    .as_ref()
                    .filter(|(r, _)| r.token == review)
                    .ok_or_else(|| fail("Review the source engine action again"))?
                    .clone();
                let command = reviewed.request()?;
                pending.stage = 5;
                pending.action = Some(reviewed.action);
                pending.snapshot = Some(reviewed.snapshot.clone());
                pending.review = Some(reviewed);
                pending.history = Some(history);
                self.engine_review = None;
                command
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve engine operation"))?;
        command["id"] = id.to_value();
        pending.id = Some(id.clone());
        if pending.stage == 5 {
            pending.mutation = Some(id);
        }
        self.engine = Some(pending);
        self.menu = None;
        self.queue_menu = None;
        self.browser_page = None;
        self.backend_catalog = None;
        self.command_prepared = None;
        Ok(command)
    }
    fn engine_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::engine_controls::{Receipt, Review, Snapshot as EngineSnapshot, Update};
        self.ensure_drained()?;
        let mut pending = self.engine.take().expect("matched engine");
        if let Some(config) = pending.config.as_mut() {
            use crate::engine_config as cfg;
            let (id, generation, admission) = (
                self.engine_identity()?,
                self.engine_instance.as_ref().expect("engine").1,
                self.engine_instance.as_ref().expect("engine").2,
            );
            let update = if frame["ok"] != true {
                if matches!(&config.request, cfg::Intent::Save(_)) {
                    return Err(fail(
                        "Engine preference publication or readback failed; close and inspect the conversation settings",
                    ));
                }
                self.config_review = None;
                cfg::Update::Rejected(text(&frame, "error", 4096)?.into())
            } else {
                let value = &frame["result"];
                match &config.request {
                    cfg::Intent::Read => {
                        let snap = cfg::Snapshot::parse(value, &id, generation, admission)?;
                        self.config_snapshot = Some(snap.clone());
                        cfg::Update::Snapshot(snap)
                    }
                    cfg::Intent::Review {
                        snapshot,
                        field,
                        value: requested,
                    } => {
                        let snap = self
                            .config_snapshot
                            .as_ref()
                            .filter(|s| s.token == *snapshot && s.id == id)
                            .ok_or_else(|| fail("Engine configuration selection expired"))?;
                        let review = cfg::Review::parse(value, snap, field, requested)?;
                        self.config_review = Some(review.clone());
                        cfg::Update::Review(review)
                    }
                    cfg::Intent::Save(token) => {
                        let review = self
                            .config_review
                            .as_ref()
                            .filter(|r| r.token == *token && r.snap.id == id)
                            .ok_or_else(|| fail("Engine preference review expired"))?;
                        if let Some(receipt) = config.receipt.as_ref() {
                            review.verify(value, receipt)?;
                            let field = review.field.clone();
                            self.config_review = None;
                            self.config_snapshot = None;
                            self.engine_snapshot = None;
                            cfg::Update::Saved(field)
                        } else if value["kind"] == "engine_config_refused" {
                            let message = review.refusal(value, text(&frame, "id", 128)?)?;
                            self.config_review = None;
                            cfg::Update::Rejected(message)
                        } else {
                            let mut cmd = review.receipt(value, text(&frame, "id", 128)?)?;
                            let id = self.wire.register_read().map_err(|_| {
                                fail("Cannot reserve independent engine configuration readback")
                            })?;
                            cmd["id"] = id.to_value();
                            pending.id = Some(id);
                            config.receipt = Some(value.clone());
                            self.engine = Some(pending);
                            return Ok(Step::Write(cmd));
                        }
                    }
                }
            };
            return Ok(Step::Engine {
                update: Update::Config(Box::new(update)),
                policy: None,
                state: None,
            });
        }
        if frame["ok"] != true {
            if pending.stage <= 4 {
                self.engine_snapshot = None;
                self.engine_review = None;
                return Ok(Step::Engine {
                    update: Update::Rejected(text(&frame, "error", 4096)?.into()),
                    policy: None,
                    state: None,
                });
            }
            return Err(fail("Engine action readback failed; outcome is unproven"));
        }
        let result = &frame["result"];
        let identity = self.engine_identity()?;
        let (session, backend, model, profile) = self.expected.as_ref().expect("active").clone();
        let (agent, schedules) = self.browser_authority.as_ref().expect("owned").clone();
        match pending.stage {
            0 => {
                let snapshot = EngineSnapshot::parse(result, &identity)?;
                let (_, generation, admission) = self.engine_instance.as_ref().expect("engine");
                if snapshot.generation != *generation || snapshot.admission != *admission {
                    return Err(fail(
                        "Engine inspection changed admission without an owned action",
                    ));
                }
                if let Some(peer) = &snapshot.local_peer_start
                    && (!self
                        .operations
                        .iter()
                        .any(|op| op == "gui.engine.native_peer_validate")
                        || !self.engine_peer_refusal.as_ref().is_some_and(
                            |(id, generation, selection)| {
                                peer["refused_action_id"] == *id
                                    && snapshot.generation == *generation
                                    && peer["selection_id"] == *selection
                            },
                        ))
                {
                    return Err(fail(
                        "Second-instance override lacks a verified prior canonical refusal",
                    ));
                }
                if snapshot.reconnect.is_some()
                    && !self
                        .operations
                        .iter()
                        .any(|op| op == "gui.engine.native_reconnect_validate")
                {
                    return Err(fail(
                        "Connect-only engine action lacks negotiated source support",
                    ));
                }
                self.engine_snapshot = Some(snapshot.clone());
                return Ok(Step::Engine {
                    policy: Some(snapshot.policy.clone()),
                    update: Update::Snapshot(Box::new(snapshot)),
                    state: None,
                });
            }
            1 | 7 => {
                if pending.stage == 1
                    && (result["busy"] == true
                        || [
                            "active_work",
                            "pending_approvals",
                            "pending_questions",
                            "pending_model_loads",
                        ]
                        .iter()
                        .any(|key| result[key].as_array().is_some_and(|v| !v.is_empty())))
                {
                    return Ok(Step::Engine {
                        update: Update::Rejected(
                            "Finish source work before managing its engine".into(),
                        ),
                        policy: None,
                        state: None,
                    });
                }
                let state = Snapshot::parse(result)?;
                if pending.stage == 1 && state.held_restored_input.is_some_and(|n| n > 0) {
                    return Ok(Step::Engine {
                        update: Update::Rejected(
                            "Resolve held input before managing its engine".into(),
                        ),
                        policy: None,
                        state: None,
                    });
                }

                if state.session != session
                    || state.backend != backend
                    || state.model != model
                    || state.profile != profile
                    || state.schedules_paused != schedules
                    || state.held_restored_input != Some(0)
                    || result["ownership"]["agent_id"] != agent
                {
                    return Err(fail(
                        "Engine state changed owned authority or pending input",
                    ));
                }
                if result["tool_policy"] != pending.snapshot.as_ref().expect("inspected").policy {
                    return Err(fail("Engine state changed tool policy"));
                }
                pending.policy = Some(result["tool_policy"].clone());
                pending.state = Some(state);
            }
            2 => match self.parse_history(result, &session, &backend, &model) {
                Ok(history) => pending.history = Some(history),
                Err(error) => {
                    return Ok(Step::Engine {
                        update: Update::Rejected(format!(
                            "Cannot review engine action with this history: {error}"
                        )),
                        policy: None,
                        state: None,
                    });
                }
            },
            3 | 9 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Engine revision exhausted"))?;
                let queue = crate::restored_queue::Snapshot::parse(result, self.revision)?;
                if queue.session != session
                    || queue.backend != backend
                    || queue.model != model
                    || !queue.entries.is_empty()
                {
                    return Err(fail(
                        "Engine action requires unchanged empty held-input queue",
                    ));
                }
                if pending.stage == 9 {
                    let receipt = pending.receipt.take().expect("receipt");
                    let (_, generation, admission) = self.engine_instance.as_mut().expect("engine");
                    self.engine_peer_refusal = if receipt.status == "refused"
                        && receipt.error_code.as_deref() == Some("resource_confirmation_required")
                        && receipt.review.action == crate::engine_controls::Action::Start
                    {
                        Some((
                            receipt.review.action_id.clone(),
                            receipt.generation,
                            receipt.review.snapshot.launch["selection_id"]
                                .as_str()
                                .expect("validated plan")
                                .to_owned(),
                        ))
                    } else {
                        None
                    };
                    *generation = receipt.generation;
                    *admission = receipt.admission;
                    self.engine_snapshot = None;
                    self.engine_review = None;
                    self.queue_menu = Some(queue);
                    return Ok(Step::Engine {
                        update: Update::Settled {
                            admission: receipt.admission,
                            message: receipt.message(),
                        },
                        policy: pending.policy,
                        state: pending.state,
                    });
                }
            }
            4 => {
                let review = Review::parse(
                    result,
                    pending.snapshot.take().expect("snapshot"),
                    pending.action.expect("action"),
                )?;
                self.engine_review =
                    Some((review.clone(), pending.history.take().expect("history")));
                return Ok(Step::Engine {
                    update: Update::Review(Box::new(review)),
                    policy: pending.policy,
                    state: None,
                });
            }
            5 => {
                pending.receipt = Some(Receipt::parse(
                    result,
                    pending.review.as_ref().expect("review").clone(),
                )?);
            }
            6 => {
                pending
                    .receipt
                    .as_ref()
                    .expect("receipt")
                    .verify_readback(result)?;
            }
            8 => {
                let history = self.parse_history(result, &session, &backend, &model)?;
                if !pending
                    .history
                    .as_ref()
                    .is_some_and(|before| history.preserves_context(before))
                {
                    return Err(fail("Engine lifecycle changed saved conversation"));
                }
            }
            _ => return Err(fail("Unknown engine operation stage")),
        }
        if self.closing {
            return Ok(Step::Engine{update:Update::Rejected("Engine response consumed during shutdown; no action success or prompt was published".into()),policy:None,state:None});
        }
        pending.stage += 1;
        let mut command = match pending.stage {
            2 | 8 => self.history_command(&session, &backend, &model)?,
            3 | 9 => json!({"type":crate::restored_queue::READ,"session_id":session}),
            4 => pending
                .snapshot
                .as_ref()
                .expect("snapshot")
                .request(pending.action.expect("action"))?,
            6 => pending
                .receipt
                .as_ref()
                .expect("receipt")
                .readback_request(),
            7 => json!({"type":"gui.state","include_conversations":false}),
            _ => return Err(fail("Invalid engine continuation")),
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve engine readback"))?;
        command["id"] = id.to_value();
        pending.id = Some(id);
        self.engine = Some(pending);
        Ok(Step::Write(command))
    }
}

impl Management {
    fn conversation_identity(&self) -> io::Result<crate::conversation_actions::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Runtime not connected"))?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Runtime lacks owned conversation authority"))?;
        Ok(crate::conversation_actions::Identity {
            session: session.clone(),
            agent: agent.clone(),
            backend: backend.clone(),
            model: model.clone(),
        })
    }
    fn request_conversation_action(
        &mut self,
        intent: crate::conversation_actions::Intent,
    ) -> io::Result<Value> {
        use crate::conversation_actions::{Intent, SNAPSHOT};
        if !self.ready() {
            return Err(fail(
                "Wait for the current operation before editing conversations",
            ));
        }
        self.ensure_drained()?;
        if !crate::conversation_actions::OPERATIONS
            .iter()
            .chain(
                [
                    "gui.state",
                    self.history_operation(),
                    crate::restored_queue::READ,
                    crate::conversation_browser::PAGE,
                ]
                .iter(),
            )
            .all(|op| self.operations.iter().any(|v| v == op))
        {
            return Err(fail(
                "Source does not support reviewed conversation actions",
            ));
        }
        let identity = self.conversation_identity()?;
        let (stage, action, title, snapshot, review, history_before, policy, state, mut command) =
            match intent {
                Intent::Read { action } => {
                    self.conversation_snapshot = None;
                    self.conversation_review = None;
                    (
                        0,
                        action,
                        String::new(),
                        None,
                        None,
                        None,
                        None,
                        None,
                        identity.command(SNAPSHOT),
                    )
                }
                Intent::Validate {
                    snapshot,
                    action,
                    title,
                } => {
                    let s = self
                        .conversation_snapshot
                        .as_ref()
                        .filter(|s| s.token == snapshot && s.identity == identity)
                        .ok_or_else(|| fail("Conversation selection changed; refresh explicitly"))?
                        .clone();
                    s.request(action, &title)?;
                    self.conversation_review = None;
                    (
                        1,
                        action,
                        title,
                        Some(s),
                        None,
                        None,
                        None,
                        None,
                        json!({"type":"gui.state","include_conversations":false}),
                    )
                }
                Intent::Apply { review } => {
                    let (r, h, p, state) = self
                        .conversation_review
                        .take()
                        .filter(|(r, _, _, _)| r.token == review && r.before.identity == identity)
                        .ok_or_else(|| fail("Review is stale or already consumed; review again"))?;
                    let command = r.request();
                    (
                        5,
                        r.action,
                        String::new(),
                        None,
                        Some(r),
                        Some(h),
                        Some(p),
                        Some(state),
                        command,
                    )
                }
            };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve conversation action"))?;
        command["id"] = id.to_value();
        self.conversation = Some(ConversationPending {
            id: id.clone(),
            stage,
            action,
            title,
            snapshot,
            review,
            history_before,
            history: None,
            policy,
            state,
            current: None,
            queue: None,
            mutation: (stage == 5).then_some(id),
        });
        self.menu = None;
        self.queue_menu = None;
        self.browser_page = None;
        self.command_prepared = None;
        Ok(command)
    }
    fn conversation_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::conversation_actions::{
            Action, Changed, Receipt, Review, SNAPSHOT, Snapshot as Current, Update,
        };
        let mut pending = self
            .conversation
            .take()
            .expect("matched conversation action");
        let identity = self.conversation_identity()?;
        let reject = |s: String| Step::ConversationAction {
            update: Update::Rejected(s),
            policy: None,
        };
        if frame["ok"] != true {
            let error = text(&frame, "error", 4096)?.to_owned();
            if pending.stage <= 4 || self.closing {
                self.conversation_review = None;
                self.conversation_snapshot = None;
                return Ok(reject(error));
            }
            return Err(fail(&format!(
                "Conversation apply/readback failed; session uncertain: {error}"
            )));
        }
        let result = &frame["result"];
        let after = pending.review.as_ref().map(Review::after_identity);
        let selected = if pending.stage >= 6 {
            after.as_ref().expect("review")
        } else {
            &identity
        };
        match pending.stage {
            0 => {
                let current = Current::parse(result, &identity)?;
                if self.closing {
                    return Ok(reject(
                        "Conversation inspection drained during shutdown".into(),
                    ));
                }
                self.conversation_snapshot = Some(current.clone());
                return Ok(Step::ConversationAction {
                    update: Update::Snapshot(Box::new(current)),
                    policy: None,
                });
            }
            1 | 7 => {
                if pending.stage == 1
                    && (result["busy"] == true
                        || [
                            "active_work",
                            "pending_approvals",
                            "pending_questions",
                            "pending_model_loads",
                        ]
                        .iter()
                        .any(|key| result[key].as_array().is_some_and(|v| !v.is_empty()))
                        || result["restored_input"]["held"]
                            .as_u64()
                            .is_some_and(|n| n > 0))
                {
                    return Ok(reject(
                        "Finish owned work or resolve held input before changing conversations"
                            .into(),
                    ));
                }
                let before = pending.state.clone().zip(pending.policy.clone());
                let state = self.verify_conversation_state(
                    result,
                    selected,
                    pending.stage == 1 && pending.snapshot.as_ref().is_some_and(|s| s.pending),
                    if pending.stage == 7 {
                        Some(
                            before
                                .as_ref()
                                .ok_or_else(|| fail("Missing reviewed conversation policy"))?,
                        )
                    } else {
                        None
                    },
                )?;
                pending.policy = Some(result["tool_policy"].clone());
                pending.state = Some(state);
            }
            2 | 8 => {
                if serde_json::to_vec(result)?.len() > 1048576 {
                    return Err(fail(
                        "Conversation history exceeded requested response budget",
                    ));
                }
                let history = if pending.stage == 8 && pending.action == Action::New {
                    self.verify_new_conversation_history(result, selected)?
                } else {
                    match self.parse_history(
                        result,
                        &selected.session,
                        &selected.backend,
                        &selected.model,
                    ) {
                        Ok(h) => h,
                        Err(e) if pending.stage == 2 => {
                            return Ok(reject(format!("Cannot review conversation action: {e}")));
                        }
                        Err(e) => return Err(e),
                    }
                };
                if pending.stage == 2 {
                    pending.history_before = Some(history.clone());
                } else {
                    let review = pending.review.as_ref().unwrap();
                    if pending.action == Action::Rename
                        && (!pending
                            .history_before
                            .as_ref()
                            .is_some_and(|before| history.preserves_context(before))
                            || history.descriptor.as_ref().map_or_else(
                                || result["meta"]["name"] != json!(review.title),
                                |d| Some(&d.title) != review.title.as_ref(),
                            ))
                    {
                        return Err(fail(
                            "Rename changed history/provider references or did not persist its exact title",
                        ));
                    }
                    pending.history = Some(history);
                }
            }
            3 | 9 => {
                let queue = self.verify_empty_conversation_queue(result, selected)?;
                pending.queue = Some(queue);
            }
            4 => {
                let review =
                    Review::parse(result, pending.snapshot.take().unwrap(), pending.action)?;
                if self.closing {
                    return Ok(reject("Conversation review drained during shutdown".into()));
                }
                self.conversation_review = Some((
                    review.clone(),
                    pending.history_before.take().unwrap(),
                    pending.policy.clone().unwrap(),
                    pending.state.take().unwrap(),
                ));
                return Ok(Step::ConversationAction {
                    update: Update::Review(Box::new(review)),
                    policy: pending.policy,
                });
            }
            5 => {
                if self.closing {
                    Receipt::drain(result, pending.review.as_ref().unwrap())?;
                    return Ok(reject(
                        "Conversation result drained during shutdown; effects unverified".into(),
                    ));
                }
                let receipt = Receipt::parse(result, pending.review.as_ref().unwrap().clone())?;
                self.work_transition_receipt(
                    result,
                    !receipt.refused && pending.action == Action::New,
                )?;
                if receipt.refused {
                    self.conversation_snapshot = None;
                    return Ok(reject(receipt.error.unwrap()));
                }
            }
            6 => {
                let current = Current::parse(result, selected)?;
                let review = pending.review.as_ref().unwrap();
                if current.pending
                    || review.action == Action::Rename
                        && Some(current.title.as_str()) != review.title.as_deref()
                {
                    return Err(fail(
                        "Conversation readback contradicts materialization or title",
                    ));
                }
                pending.current = Some(current);
            }
            10 => {
                self.revision = self
                    .revision
                    .checked_add(1)
                    .ok_or_else(|| fail("Conversation revision exhausted"))?;
                let page = crate::conversation_browser::Page::parse(
                    result,
                    self.revision,
                    &selected.session,
                )?;
                let current = pending.current.take().unwrap();
                if page.agent != selected.agent
                    || page
                        .entries
                        .iter()
                        .any(|e| e.current && e.title != current.title)
                {
                    return Err(fail(
                        "Conversation catalog contradicts current ownership/title",
                    ));
                }
                if self.closing {
                    return Ok(reject(
                        "Conversation readback drained during shutdown; effects unverified".into(),
                    ));
                }
                let state = pending.state.take().unwrap();
                let queue = pending.queue.take().unwrap();
                self.install_verified_conversation(&state, &queue, &page);
                return Ok(Step::ConversationChanged(Box::new(Changed {
                    action: pending.action,
                    snapshot: state,
                    history: pending.history.take().unwrap(),
                    queue,
                    policy: pending.policy.take().unwrap(),
                    current,
                    page,
                })));
            }
            _ => return Err(fail("Unknown conversation mutation phase")),
        }
        if self.closing {
            return Ok(reject(
                "Conversation response consumed during shutdown; no completion published".into(),
            ));
        }
        pending.stage += 1;
        let mut command = match pending.stage {
            2 | 8 => self.history_command(&selected.session, &selected.backend, &selected.model)?,
            3 | 9 => json!({"type":crate::restored_queue::READ,"session_id":selected.session}),
            4 => pending
                .snapshot
                .as_ref()
                .unwrap()
                .request(pending.action, &pending.title)?,
            6 => pending
                .review
                .as_ref()
                .unwrap()
                .after_identity()
                .command(SNAPSHOT),
            7 => json!({"type":"gui.state","include_conversations":false}),
            10 => {
                json!({"type":crate::conversation_browser::PAGE,"session_id":selected.session,"cursor":null})
            }
            _ => return Err(fail("Invalid conversation continuation")),
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve conversation verification"))?;
        command["id"] = id.to_value();
        pending.id = id;
        self.conversation = Some(pending);
        Ok(Step::Write(command))
    }
}

struct ThemePending {
    id: CommandId,
    stage: u8,
    change: Option<crate::theme_controls::Change>,
    snapshot: Option<crate::theme_controls::Snapshot>,
    review: Option<crate::theme_controls::Review>,
    outcome: Option<crate::theme_controls::Outcome>,
}
impl Management {
    fn theme_identity(&self) -> io::Result<crate::theme_controls::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Runtime is not initialized"))?;
        let (agent, _) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Runtime omitted owned theme authority"))?;
        Ok(crate::theme_controls::Identity {
            session: session.clone(),
            agent: agent.clone(),
            backend: backend.clone(),
            model: model.clone(),
        })
    }
    fn startup_theme(
        &mut self,
        snapshot: Snapshot,
        history: Option<crate::resume::History>,
    ) -> io::Result<Step> {
        if self
            .operations
            .iter()
            .any(|op| op == crate::theme_controls::SNAPSHOT)
            && self.browser_authority.is_some()
        {
            let command = self.request_theme(crate::theme_controls::Intent::Read)?;
            self.theme_startup = Some((snapshot, history));
            Ok(Step::Write(command))
        } else if let Some(history) = history {
            Ok(Step::Restored { snapshot, history })
        } else {
            Ok(Step::Initialized(snapshot))
        }
    }
    pub fn request_theme(&mut self, intent: crate::theme_controls::Intent) -> io::Result<Value> {
        use crate::theme_controls::{Intent, SAVE, SNAPSHOT, VALIDATE};
        if !self.ready() {
            return Err(fail("Finish current work before using themes"));
        }
        self.ensure_drained()?;
        let identity = self.theme_identity()?;
        let (stage, mut command, change, review) = match intent {
            Intent::Read => {
                self.theme_snapshot = None;
                self.theme_review = None;
                self.theme_state = None;
                (0, identity.command(SNAPSHOT), None, None)
            }
            Intent::Validate { snapshot, change } => {
                self.theme_review = None;
                let current = self
                    .theme_snapshot
                    .as_ref()
                    .filter(|s| s.token == snapshot && s.identity == identity)
                    .ok_or_else(|| fail("Refresh theme authority before validation"))?;
                (2, current.request(&change)?, Some(change), None)
            }
            Intent::Save { review, action_id } => {
                let current = self
                    .theme_review
                    .take()
                    .filter(|r| {
                        r.token == review
                            && r.action_id == action_id
                            && r.snapshot.identity == identity
                    })
                    .ok_or_else(|| fail("Validate the theme again before saving"))?;
                (3, current.request(), None, Some(current))
            }
        };
        let operation = match stage {
            0 => SNAPSHOT,
            2 => VALIDATE,
            _ => SAVE,
        };
        if !self.operations.iter().any(|op| op == operation) {
            return Err(fail(
                "Source theme operation is unavailable; update Python source",
            ));
        }
        let id = if stage == 3 {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve theme operation"))?;
        command["id"] = id.to_value();
        self.theme = Some(ThemePending {
            id,
            stage,
            change,
            snapshot: None,
            review,
            outcome: None,
        });
        Ok(command)
    }
    fn theme_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::theme_controls::{Outcome, Review, SNAPSHOT, Snapshot as ThemeSnapshot, Update};
        let mut p = self.theme.take().expect("theme request");
        self.ensure_drained()?;
        if self.closing {
            // Drain the dispatched request; no late readback, UI adoption or save.
            self.theme_startup = None;
            return Ok(Step::Ignore);
        }
        let publish = |update, policy, startup| Step::Theme {
            update,
            policy,
            startup,
        };
        if frame["ok"] != true {
            if p.stage >= 3 {
                return Err(fail(
                    "Theme save or readback failed without a proven outcome",
                ));
            }
            return Ok(publish(
                Update::Rejected(text(&frame, "error", 4096)?.into()),
                None,
                self.theme_startup.take(),
            ));
        }
        let result = &frame["result"];
        let identity = self.theme_identity()?;
        match p.stage {
            0 => {
                identity.verify(result)?;
                let snapshot = match ThemeSnapshot::parse(result, &identity) {
                    Ok(s) => s,
                    Err(e) => {
                        return Ok(publish(
                            Update::Rejected(e.to_string()),
                            None,
                            self.theme_startup.take(),
                        ));
                    }
                };
                p.snapshot = Some(snapshot);
                p.stage = 1;
            }
            1 | 5 => {
                let state = Snapshot::parse(result)?;
                let (session, backend, model, profile) = self.expected.as_ref().expect("active");
                let (agent, schedules) = self.browser_authority.as_ref().expect("authority");
                if (&state.session, &state.backend, &state.model, &state.profile)
                    != (session, backend, model, profile)
                    || state.held_restored_input.is_none()
                    || state.schedules_paused != *schedules
                    || result["ownership"]["agent_id"] != *agent
                {
                    return Err(fail(
                        "Theme read changed owned runtime identity or authority",
                    ));
                }
                let policy = result["tool_policy"].clone();
                if p.stage == 1 {
                    let snapshot = p.snapshot.take().expect("read snapshot");
                    self.theme_state = Some((state, policy.clone()));
                    self.theme_snapshot = Some(snapshot.clone());
                    return Ok(publish(
                        Update::Snapshot(Box::new(snapshot)),
                        Some(policy),
                        self.theme_startup.take(),
                    ));
                }
                let (before, before_policy) = self
                    .theme_state
                    .as_ref()
                    .ok_or_else(|| fail("Theme preflight state missing"))?;
                if &state != before || &policy != before_policy {
                    return Err(fail("Theme save changed runtime state or tool policy"));
                }
                let snapshot = p.snapshot.take().expect("readback");
                let outcome = p.outcome.take().expect("receipt");
                outcome.verify(&snapshot, p.review.as_ref().expect("review"))?;
                self.theme_snapshot = Some(snapshot.clone());
                return Ok(publish(
                    Update::Applied {
                        snapshot: Box::new(snapshot),
                        outcome: Box::new(outcome),
                    },
                    Some(policy),
                    None,
                ));
            }
            2 => {
                let snapshot = self
                    .theme_snapshot
                    .as_ref()
                    .ok_or_else(|| fail("Missing theme authority"))?;
                let review = Review::parse(
                    result,
                    snapshot,
                    p.change.as_ref().expect("requested change"),
                )?;
                self.theme_review = Some(review.clone());
                return Ok(publish(Update::Review(Box::new(review)), None, None));
            }
            3 => {
                match Outcome::parse(result, p.review.as_ref().expect("confirmed review"))? {
                    Ok(outcome) => p.outcome = Some(outcome),
                    Err(reason) => {
                        self.theme_snapshot = None;
                        self.theme_state = None;
                        return Ok(publish(Update::Rejected(reason), None, None));
                    }
                }
                p.stage = 4;
            }
            4 => {
                p.snapshot = Some(ThemeSnapshot::parse(result, &identity)?);
                p.stage = 5;
            }
            _ => return Err(fail("Unknown theme response phase")),
        }
        let mut command = if p.stage == 4 {
            identity.command(SNAPSHOT)
        } else {
            json!({"type":"gui.state","include_conversations":false})
        };
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve theme readback"))?;
        command["id"] = id.to_value();
        p.id = id;
        self.theme = Some(p);
        Ok(Step::Write(command))
    }
}

impl Management {
    fn history_identity(
        &self,
        session: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<crate::history::Identity> {
        let agent = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("History requires owned-agent authority"))?
            .0
            .clone();
        Ok(crate::history::Identity {
            from_session: session.into(),
            session: session.into(),
            agent,
            backend: backend.into(),
            model: model.into(),
            scope: crate::history::Scope::Current,
            catalog: None,
        })
    }
    fn history_operation(&self) -> &'static str {
        if self.history_projection {
            crate::history::SNAPSHOT
        } else {
            "gui.conversations.read"
        }
    }
    fn history_command(&self, session: &str, backend: &str, model: &str) -> io::Result<Value> {
        if self.history_projection {
            Ok(self
                .history_identity(session, backend, model)?
                .command(crate::history::SNAPSHOT))
        } else {
            Ok(
                json!({"type":"gui.conversations.read","session_id":session,"max_response_bytes":1048576}),
            )
        }
    }
    fn parse_history(
        &self,
        result: &Value,
        session: &str,
        backend: &str,
        model: &str,
    ) -> io::Result<crate::resume::History> {
        if self.history_projection {
            let descriptor = crate::history::Snapshot::parse(
                result,
                &self.history_identity(session, backend, model)?,
            )?;
            if descriptor.read_only {
                return Err(fail(
                    "Current history proof requires writable owned context",
                ));
            }
            Ok(crate::resume::History::projected(descriptor))
        } else {
            crate::backend_controls::history(result, session)
        }
    }
    fn history_active_identity(&self) -> io::Result<crate::history::Identity> {
        let (session, backend, model, _) = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("History requires an active runtime"))?;
        self.history_identity(session, backend, model)
    }
    fn history_target_matches(
        identity: &crate::history::Identity,
        target: &crate::history_view::Target,
        active: &crate::history::Identity,
    ) -> bool {
        use crate::{history::Scope, history_view::Target};
        match target {
            Target::Current => identity == active,
            Target::Saved { session } => {
                identity.scope == Scope::Preview
                    && identity.session == *session
                    && *session != active.session
                    && identity.from_session == active.session
                    && identity.agent == active.agent
                    && identity.backend == active.backend
                    && identity.model == active.model
                    && identity.catalog.is_some()
            }
        }
    }
    pub fn request_history(&mut self, intent: crate::history_view::Intent) -> io::Result<Value> {
        use crate::{
            history::{Anchor, PageRequest},
            history_view::{Intent, Target},
        };
        if !self.ready() || !self.history_projection {
            return Err(fail(
                "Saved History needs an idle source with projection version 1",
            ));
        }
        self.ensure_drained()?;
        let active = self.history_active_identity()?;
        let key = intent.key().clone();
        if key.generation == 0 || key.generation <= self.history_generation {
            return Err(fail("Saved History read generation is stale"));
        }
        let admitted = self
            .history_target
            .as_ref()
            .is_some_and(|(target, owner)| target == &key.target && owner == &active);
        let mutating = matches!(&intent,Intent::Speech{request,..} if matches!(request.action,"start"|"close"));
        let (phase, identity, descriptor, position, prior, mut command) = match intent {
            Intent::Speech { request, .. } => {
                if !self
                    .operations
                    .iter()
                    .any(|op| op == crate::playback::OPERATION)
                {
                    return Err(fail("Source does not support reviewed response playback"));
                }
                let descriptor = self
                    .history_snapshot
                    .as_ref()
                    .filter(|s| {
                        admitted
                            && s.token == request.snapshot
                            && s.materialized
                            && Self::history_target_matches(&s.identity, &key.target, &active)
                    })
                    .ok_or_else(|| fail("Refresh history before reviewing playback"))?
                    .clone();
                if !self.history_page.as_ref().is_some_and(|p| {
                    p.fragments.iter().any(|f| {
                        crate::playback::Selection::from_fragment(f).as_ref()
                            == Some(&request.selected)
                    })
                }) {
                    return Err(fail("Select a complete answer in the current history page"));
                }
                let command = request.command(&descriptor);
                (
                    HistoryPhase::Speech(request),
                    descriptor.identity.clone(),
                    Some(descriptor),
                    PageRequest::Anchor(Anchor::Head),
                    None,
                    command,
                )
            }
            Intent::Inspect {
                revision, session, ..
            } => {
                let catalog = self
                    .browser_page
                    .as_ref()
                    .ok_or_else(|| fail("Refresh the conversation browser before inspecting"))?;
                if catalog.session != active.session || catalog.agent != active.agent {
                    return Err(fail(
                        "Inspection catalogue belongs to another owned runtime",
                    ));
                }
                let selected = catalog.inspection(revision, &session)?;
                let target = if selected.current {
                    Target::Current
                } else {
                    Target::Saved {
                        session: session.clone(),
                    }
                };
                if key.target != target {
                    return Err(fail(
                        "Inspection target differs from the selected conversation",
                    ));
                }
                self.history_refresh_command(&key.target, &active, Anchor::Tail)
            }
            Intent::Refresh { anchor, .. } => {
                if key.target != Target::Current && !admitted {
                    return Err(fail(
                        "Select a saved conversation before refreshing its history",
                    ));
                }
                self.history_refresh_command(&key.target, &active, anchor)
            }
            Intent::Image {
                snapshot, selected, ..
            } => {
                if !self.operations.iter().any(|op| op == crate::history::IMAGE) {
                    return Err(fail("The source does not support saved image preview"));
                }
                let descriptor = self
                    .history_snapshot
                    .as_ref()
                    .filter(|s| {
                        admitted
                            && s.token == snapshot
                            && s.materialized
                            && Self::history_target_matches(&s.identity, &key.target, &active)
                    })
                    .ok_or_else(|| fail("Refresh saved history before opening the image"))?
                    .clone();
                if !self.history_page.as_ref().is_some_and(|page| {
                    page.fragments
                        .iter()
                        .any(|f| f.image().as_ref() == Some(&selected))
                }) {
                    return Err(fail("Select an embedded image in the current history page"));
                }
                let command = selected.command(&descriptor);
                (
                    HistoryPhase::Image(selected),
                    descriptor.identity.clone(),
                    Some(descriptor),
                    PageRequest::Anchor(Anchor::Head),
                    None,
                    command,
                )
            }
            Intent::Page {
                snapshot, position, ..
            } => {
                if key.target != Target::Current && !admitted {
                    return Err(fail("Saved History target changed; inspect it again"));
                }
                let descriptor = self
                    .history_snapshot
                    .as_ref()
                    .filter(|s| {
                        s.token == snapshot
                            && Self::history_target_matches(&s.identity, &key.target, &active)
                    })
                    .ok_or_else(|| fail("Saved History changed; explicitly refresh"))?
                    .clone();
                let prior = self.history_page.clone();
                if let PageRequest::Cursor(cursor) = &position
                    && !prior.as_ref().is_some_and(|p| {
                        p.previous.as_ref() == Some(cursor) || p.next.as_ref() == Some(cursor)
                    })
                {
                    return Err(fail("History cursor was not offered by the visible page"));
                }
                let command = descriptor.page_request(&position)?;
                (
                    HistoryPhase::Page,
                    descriptor.identity.clone(),
                    Some(descriptor),
                    position,
                    prior,
                    command,
                )
            }
        };
        let id = if mutating {
            self.wire.register_request()
        } else {
            self.wire.register_read()
        }
        .map_err(|_| fail("Cannot reserve history operation"))?;
        command["id"] = id.to_value();
        // A read-only inspection cannot retain a previously offered Open review.
        self.browser_preview = None;
        self.history_generation = key.generation;
        self.history_target = Some((key.target.clone(), active.clone()));
        self.saved_history = Some(SavedHistoryPending {
            id,
            key,
            active,
            identity,
            phase,
            descriptor,
            position,
            prior,
        });
        Ok(command)
    }
    fn history_refresh_command(
        &self,
        target: &crate::history_view::Target,
        active: &crate::history::Identity,
        anchor: crate::history::Anchor,
    ) -> (
        HistoryPhase,
        crate::history::Identity,
        Option<crate::history::Snapshot>,
        crate::history::PageRequest,
        Option<crate::history::Page>,
        Value,
    ) {
        use crate::{history::PageRequest, history_view::Target};
        let (phase, command) = match target {
            Target::Current => (
                HistoryPhase::Snapshot,
                active.command(crate::history::SNAPSHOT),
            ),
            Target::Saved { session } => (
                HistoryPhase::Locate,
                json!({"type":crate::conversation_browser::PAGE,"session_id":active.session,"locate_session_id":session}),
            ),
        };
        (
            phase,
            active.clone(),
            None,
            PageRequest::Anchor(anchor),
            None,
            command,
        )
    }
    fn saved_history_response(&mut self, frame: Value) -> io::Result<Step> {
        use crate::{
            history::{Page, PageRequest, Scope, Snapshot as HistorySnapshot},
            history_view::{Target, Update},
        };
        let mut pending = self.saved_history.take().expect("matched history read");
        self.ensure_drained()?;
        if self.closing {
            return Ok(Step::Ignore);
        }
        if self.history_active_identity()? != pending.active {
            return Err(fail("Active runtime changed during history inspection"));
        }
        if frame["ok"] != true {
            // The frontend retains its previous page as stale. No cached cursor
            // may authorize another read until an explicit refresh succeeds.
            if !matches!(pending.phase, HistoryPhase::Speech(_)) {
                self.history_snapshot = None;
                self.history_page = None;
            }
            return Ok(Step::SavedHistory(Update::Rejected {
                key: pending.key,
                reason: text(&frame, "error", 4096)?.into(),
            }));
        }
        let result = &frame["result"];
        let mut command = match pending.phase {
            HistoryPhase::Speech(request) => {
                let snapshot = pending
                    .descriptor
                    .ok_or_else(|| fail("Playback lost its history descriptor"))?;
                let state = request.parse(result, &snapshot, self.playback.as_ref())?;
                self.playback = if state.status == "closed" {
                    None
                } else {
                    Some(state.clone())
                };
                return Ok(Step::SavedHistory(Update::Speech {
                    key: pending.key,
                    state,
                }));
            }
            HistoryPhase::Locate => {
                let Target::Saved { session } = &pending.key.target else {
                    return Err(fail("Current history cannot use a saved locator"));
                };
                if result["located_session_id"].as_str() != Some(session.as_str()) {
                    return Err(fail("Located history changed the selected conversation"));
                }
                let catalog = crate::conversation_browser::Page::parse(
                    result,
                    pending.key.generation,
                    &pending.active.session,
                )?;
                if catalog.agent != pending.active.agent {
                    return Err(fail("Located history belongs to another owned agent"));
                }
                let selected = catalog.inspection(pending.key.generation, session)?;
                if selected.current {
                    return Err(fail(
                        "Saved inspection unexpectedly selected the active conversation",
                    ));
                }
                pending.identity = crate::history::Identity {
                    session: session.clone(),
                    scope: Scope::Preview,
                    catalog: Some(catalog.snapshot),
                    ..pending.active.clone()
                };
                pending.phase = HistoryPhase::Snapshot;
                pending.identity.command(crate::history::SNAPSHOT)
            }
            HistoryPhase::Snapshot => {
                let descriptor = HistorySnapshot::parse(result, &pending.identity)?;
                if !Self::history_target_matches(
                    &descriptor.identity,
                    &pending.key.target,
                    &pending.active,
                ) {
                    return Err(fail("History descriptor changed its inspection target"));
                }
                let command = descriptor.page_request(&pending.position)?;
                pending.descriptor = Some(descriptor);
                pending.phase = HistoryPhase::Page;
                command
            }
            HistoryPhase::Image(selected) => {
                let snapshot = pending
                    .descriptor
                    .ok_or_else(|| fail("Saved image lacks its descriptor"))?;
                let thumbnail = selected.parse(result, &snapshot)?;
                return Ok(Step::SavedHistory(Update::Image {
                    key: pending.key,
                    selected,
                    thumbnail,
                }));
            }
            HistoryPhase::Page => {
                let descriptor = pending
                    .descriptor
                    .ok_or_else(|| fail("History page lacks its inspected descriptor"))?;
                let page = Page::parse(result, &descriptor)?;
                page.accepts(&pending.position)?;
                if let PageRequest::Cursor(cursor) = &pending.position {
                    page.follows(
                        pending
                            .prior
                            .as_ref()
                            .ok_or_else(|| fail("History page lacks prior cursor authority"))?,
                        cursor,
                    )?;
                }
                self.history_snapshot = Some(descriptor.clone());
                self.history_page = Some(page.clone());
                return Ok(Step::SavedHistory(if pending.prior.is_some() {
                    Update::Page {
                        key: pending.key,
                        page: Box::new(page),
                    }
                } else {
                    Update::Loaded {
                        key: pending.key,
                        snapshot: Box::new(descriptor),
                        page: Box::new(page),
                    }
                }));
            }
        };
        let id = self
            .wire
            .register_read()
            .map_err(|_| fail("Cannot reserve bounded history read"))?;
        command["id"] = id.to_value();
        pending.id = id;
        self.saved_history = Some(pending);
        Ok(Step::Write(command))
    }
}
