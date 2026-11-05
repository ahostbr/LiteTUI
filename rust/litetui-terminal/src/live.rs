//! Owned Python RPC runtime for one-shot and interactive frontends.
//! Versioned management startup, selected envelopes and serial prompt ownership.
//! No automatic retries, backend fallback, automatic approvals or local model launches.
mod work;
use crate::{
    protocol::JsonlDecoder,
    transport::{Inbound, IngressLimits},
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read, Write},
    path::{Path, PathBuf},
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};
pub use work::Update as WorkExecution;

const FRAME_BYTES: usize = 2 * 1024 * 1024;
const PROMPT_ID: &str = "rust-live-prompt";
const SHUTDOWN_ID: &str = "rust-live-shutdown";

fn fresh_prompt_id() -> io::Result<String> {
    let mut bytes = [0u8; 16];
    #[cfg(windows)]
    {
        use windows_sys::Win32::Security::Cryptography::{
            BCRYPT_USE_SYSTEM_PREFERRED_RNG, BCryptGenRandom,
        };
        // The provider chooses its system RNG when the handle is null; the
        // buffer is valid for the full length and remains owned by this call.
        let status = unsafe {
            BCryptGenRandom(
                std::ptr::null_mut(),
                bytes.as_mut_ptr(),
                bytes.len() as u32,
                BCRYPT_USE_SYSTEM_PREFERRED_RNG,
            )
        };
        if status != 0 {
            return Err(io::Error::other(format!(
                "Cannot reserve prompt identity: {status}"
            )));
        }
    }
    #[cfg(unix)]
    fs::File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    #[cfg(not(any(windows, unix)))]
    return Err(io::Error::other(
        "No supported source of fresh prompt identities",
    ));
    Ok(format!("{PROMPT_ID}-{:032x}", u128::from_be_bytes(bytes)))
}

#[derive(Debug)]
pub struct Options {
    pub python: PathBuf,
    pub source: PathBuf,
    pub data_root: PathBuf,
    pub backend: String,
    pub model: String,
    pub prompt: String,
    pub timeout: Duration,
    pub tools_enabled: bool,
    pub tool_profile: Option<crate::tool_mode::Profile>,
    pub workspace: Option<PathBuf>,
    pub resume: Option<String>,
    pub owner: Option<OwnerStore>,
    pub launch: crate::runtime_launch::LaunchOptions,
}
#[derive(Debug)]
pub struct OwnerStore {
    name: String,
    directory: PathBuf,
}

impl OwnerStore {
    fn from_flags(name: Option<String>, directory: Option<String>) -> io::Result<Option<Self>> {
        match (name, directory) {
            (None, None) => Ok(None),
            (Some(name), Some(directory))
                if !name.trim().is_empty()
                    && name.len() <= 1024
                    && !name.chars().any(char::is_control)
                    && Path::new(&directory).is_absolute()
                    && !directory.chars().any(char::is_control) =>
            {
                Ok(Some(Self {
                    name: name.trim().to_owned(),
                    directory: directory.into(),
                }))
            }
            _ => Err(io::Error::other(
                "--owner needs a name and --memory an absolute folder, together",
            )),
        }
    }

    fn verify(expected: Option<&Self>, ready: &Value, child: &OwnedChild) -> io::Result<()> {
        match (expected, ready.get("owner_store")) {
            (None, None) => Ok(()),
            (Some(store), Some(value))
                if value.as_object().is_some_and(|obj| obj.len() == 4)
                    && ready["pid"]
                        .as_u64()
                        .and_then(|pid| u32::try_from(pid).ok())
                        .is_some_and(|pid| {
                            pid == child.child.id() || child.job.contains_pid(pid).unwrap_or(false)
                        })
                    && value["version"].as_u64() == Some(1)
                    && value["name"] == store.name
                    && value["agent_id"]
                        .as_str()
                        .is_some_and(|id| !id.is_empty() && id.len() <= 128)
                    && value["directory"]
                        .as_str()
                        .is_some_and(|s| Path::new(s) == store.directory) =>
            {
                Ok(())
            }
            _ => Err(io::Error::other(
                "Runtime did not verify the selected owner memory lease",
            )),
        }
    }
}

impl Options {
    /// Required at the runtime boundary, before filesystem writes or child creation.
    pub fn selected_profile(&self) -> io::Result<crate::tool_mode::Profile> {
        self.tool_profile.ok_or_else(|| io::Error::other(
            "Select a tool profile before starting Python; use --tool-profile strict|interactive|autonomous"))
    }
    pub fn parse(args: &[String]) -> io::Result<Self> {
        let (launch, args) = crate::runtime_launch::LaunchOptions::extract(args)?;
        let mut fields = std::collections::HashMap::new();
        for pair in args.chunks(2) {
            if pair.len() != 2
                || ![
                    "--python",
                    "--source",
                    "--data-root",
                    "--backend",
                    "--model",
                    "--prompt",
                    "--timeout",
                    "--tools",
                    "--tool-profile",
                    "--workspace",
                    "--resume",
                    "--owner",
                    "--memory",
                ]
                .contains(&pair[0].as_str())
                || fields.insert(pair[0].as_str(), pair[1].clone()).is_some()
            {
                return Err(io::Error::other(
                    "Expected unique --live option/value pairs; see --help",
                ));
            }
        }
        let mut required = |name| {
            fields
                .remove(name)
                .filter(|v| !v.is_empty())
                .ok_or_else(|| io::Error::other(format!("Missing {name}")))
        };
        let python = required("--python")?.into();
        let source = required("--source")?.into();
        let data_root = required("--data-root")?.into();
        let backend = required("--backend")?;
        let model = required("--model")?;
        let prompt_provided = fields.contains_key("--prompt");
        let prompt = fields.remove("--prompt").unwrap_or_default();
        if launch.engine_management && prompt_provided {
            return Err(io::Error::other(
                "Engine management cannot queue a launch prompt",
            ));
        }
        let owner = OwnerStore::from_flags(fields.remove("--owner"), fields.remove("--memory"))?;
        let resume = fields.remove("--resume");
        if let Some(id) = &resume {
            crate::resume::conversation_id(id)?;
        }
        let tools_enabled = match fields.remove("--tools").as_deref().unwrap_or("off") {
            "on" => true,
            "off" => false,
            _ => return Err(io::Error::other("--tools must be on or off")),
        };
        let tool_profile = fields
            .remove("--tool-profile")
            .as_deref()
            .map(crate::tool_mode::Profile::parse)
            .transpose()?;
        let workspace = fields.remove("--workspace").map(PathBuf::from);
        if tools_enabled && workspace.as_ref().is_none_or(|p| !p.is_absolute()) {
            return Err(io::Error::other(
                "Tools on requires an explicit absolute --workspace",
            ));
        }
        let seconds: u64 = fields
            .remove("--timeout")
            .unwrap_or_else(|| "180".into())
            .parse()
            .map_err(io::Error::other)?;
        if prompt.len() > 65536 {
            return Err(io::Error::other("Prompt exceeds 64 KiB live limit"));
        }
        let selected_backend = crate::runtime_launch::Backend::parse(&backend)?;
        launch.validate(selected_backend, &model)?;
        if !(1..=600).contains(&seconds) {
            return Err(io::Error::other("Live timeout must be 1..600 seconds"));
        }
        Ok(Self {
            python,
            source,
            data_root,
            backend,
            model,
            prompt,
            timeout: Duration::from_secs(seconds),
            tools_enabled,
            tool_profile,
            workspace,
            owner,
            resume,
            launch,
        })
    }
}

/// Events are observations of a single isolated legacy turn, NOT invented IDs
/// or an admission receipt. The prompt ACK alone never completes this state.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ContentKind {
    AnswerDelta,
    AnswerSnapshot,
    ReasoningDelta,
    ReasoningSnapshot,
}
impl ContentKind {
    pub fn replaces(self) -> bool {
        matches!(self, Self::AnswerSnapshot | Self::ReasoningSnapshot)
    }
    pub fn reasoning(self) -> bool {
        matches!(self, Self::ReasoningDelta | Self::ReasoningSnapshot)
    }
}
#[derive(Debug, PartialEq)]
pub enum Observation {
    RuntimeControls(crate::runtime_controls_view::Update),
    RuntimePolicy(crate::runtime_controls::Policy),
    WorkLaunchStop {
        action_id: String,
        generation: u64,
        stopped_operation: Option<String>,
    },
    WorkLaunchPending {
        action_id: String,
        generation: u64,
        reason: String,
    },
    ScheduledProposal(String),
    ScheduledHeld(String),
    ScheduledLocalNotSent {
        prompt: String,
        reason: String,
    },
    ScheduledConversationChanged(Box<crate::conversation_actions::Changed>),
    WorkExecution(Box<WorkExecution>),
    WorkLaunchFinished {
        action_id: String,
        update: crate::work_view::Update,
        stopped_operation: Option<String>,
        stop_verified: bool,
    },
    Compact(crate::conversation_compact::Update),
    CompactSettled(Box<crate::management::CompactChanged>),
    Rewrite(crate::conversation_rewrite::Update),
    RewriteSettled(Box<crate::management::RewriteChanged>),
    RewriteSegment {
        operation_id: String,
        execution_id: String,
        index: u64,
        previous_outcome: Option<TerminalOutcome>,
    },
    RewriteAftercare {
        operation_id: String,
        aftercare_id: String,
        running: bool,
        previous_outcome: Option<TerminalOutcome>,
    },
    RewriteActivity {
        execution_id: String,
        activity: crate::native_activity::Activity,
    },
    RewriteStatus {
        execution_id: String,
        text: String,
    },
    SavedHistory(crate::history_view::Update),
    Theme(crate::theme_controls::Update),
    ConversationChanged(Box<crate::conversation_actions::Changed>),
    ConversationUnverified(String),
    Engine(crate::engine_controls::Update),
    EngineAdmission(crate::engine_controls::Admission),
    State(crate::runtime_state::Update),
    History(crate::resume::History),
    Controls(crate::model_controls::Update),
    Queue(crate::restored_queue::Update),
    Commands(crate::command_palette::Update),
    Work(crate::work_view::Update),
    Settings(crate::settings_view::Update),
    Backend(crate::backend_controls::Update),
    BackendChanged(Box<crate::backend_controls::Changed>),
    Browser(crate::conversation_browser::Update),
    Opened(Box<crate::conversation_browser::Opened>),
    Ready,
    /// Local command reservation; neither source admission nor completion.
    Submission {
        id: String,
    },
    Content {
        card: usize,
        kind: ContentKind,
        text: String,
    },
    Activity(crate::native_activity::Activity),
    Approval(crate::approvals::Event),
    ApprovalSettled {
        event: crate::approvals::Event,
        outcome: TerminalOutcome,
    },
    Question {
        event: crate::questions::Event,
        settled: Option<TerminalOutcome>,
    },
    Text(String),
    Snapshot(String),
    Reasoning(String),
    ReasoningSnapshot(String),
    Information(String),
    /// Source-authoritative approval refusal; settled, but never successful.
    ApprovalStopped(String),
    Cancelled(String),
    Refused(String),
    ImageRefused {
        reason: String,
        review: crate::image::Review,
        prompt: String,
    },
    Failed(String),
    Complete,
}
#[derive(Clone, Debug, PartialEq)]
pub enum TerminalOutcome {
    Complete,
    ApprovalStopped(String),
    Cancelled(String),
    Refused(String),
    Failed(String),
}
#[derive(Default)]
pub struct Turn {
    work: work::State,
    ready: bool,
    engine_admission: Option<crate::engine_controls::Admission>,
    allow_engine_management: bool,
    engine_shutdown: Option<crate::engine_controls::ShutdownIdentity>,
    shutdown_acknowledged: bool,
    conversation_shutdown: Option<Value>,
    conversation_completed_shutdown: Option<Value>,
    sent: bool,
    closing: bool,
    command_id: String,
    continued_input: Option<(String, String)>,
    compact_input: Option<crate::conversation_compact::Review>,
    compact_admission: Option<crate::conversation_compact::Admission>,
    compact_shutdown: Option<Value>,
    compact_current_session: Option<String>,
    compact_root_started: bool,
    compact_root_ended: bool,
    rewrite_input: Option<crate::conversation_rewrite::Review>,
    rewrite_admission: Option<crate::conversation_rewrite::Admission>,
    rewrite_shutdown: Option<Value>,
    rewrite_current_session: Option<String>,
    rewrite_active: bool,
    rewrite_first_terminal: bool,
    rewrite_segments: Vec<String>,
    rewrite_aftercare: std::collections::BTreeMap<String, Option<String>>,
    rewrite_executions: std::collections::HashSet<String>,
    // Host observation IDs are scoped to the operation; returns update the request's card.
    observed_tools: std::collections::BTreeMap<String, (String, usize, bool)>,
    host_content_follows_tool: bool,
    visible_answers: std::collections::BTreeSet<usize>,
    tools_seen: bool,
    card_count: usize,
    started: bool,
    native_turns: std::collections::BTreeSet<(String, String)>,
    native_turn_bytes: usize,
    acknowledged: bool,
    admission_refused: bool,
    settled: bool,
    /// A Quit drained this turn's stream: its turn_end was seen but, by design, never
    /// recorded as a terminal outcome and never settled. The source owner is retired
    /// all the same.
    quit_ended: bool,
    terminal: Option<TerminalOutcome>,
    content_bytes: usize,
    approvals: crate::approvals::Ledger,
    questions: crate::questions::Ledger,
    policy: Option<crate::tool_mode::Expected>,
    runtime_policy: Option<crate::runtime_controls::Policy>,
    ordinary_runtime_prompt: bool,
    image_prompt: Option<(crate::image::Review, String)>,
    image_retained: bool,
    aborts: crate::cancellation::Ledger,
    stop_requests: usize,
    stop_acknowledged: bool,
}
fn require_work_lifecycle(ready: &Value) -> io::Result<()> {
    if ready["work_lifecycle_version"].as_u64() != Some(1) {
        return Err(io::Error::other(
            "Runtime did not acknowledge requested Work lifecycle version 1",
        ));
    }
    Ok(())
}
impl Turn {
    /// Stop admitting user work while the source drains its owned shutdown.
    pub fn begin_close(&mut self) {
        self.closing = true;
    }
    fn retire_scheduled_refusal(&mut self, identity: Option<&Value>) {
        // An automatically declined proposal may race a human New. It never
        // owned that human transition's outstanding shutdown obligation.
        if identity.is_some() && self.conversation_shutdown.as_ref() == identity {
            self.conversation_shutdown = self.conversation_completed_shutdown.clone();
        }
    }
    fn can_runtime_controls(&self) -> bool {
        self.ready
            && !self.closing
            && !self.rewrite_active
            && !self.work.uncertain
            && self
                .engine_admission
                .is_none_or(|a| a == crate::engine_controls::Admission::PromptReady)
            && self.approvals.settled()
            && self.questions.settled()
            && (!self.sent || self.started || self.settled)
    }
    fn validate_current_policy(&self, value: &Value) -> io::Result<()> {
        let expected = self.policy.as_ref().expect("launch policy");
        let mode = match self.runtime_policy.as_ref() {
            Some(policy) => crate::tool_mode::ValidationMode::Current(policy),
            None => crate::tool_mode::ValidationMode::Launch,
        };
        expected.validate_mode(value, mode)
    }
    fn install_runtime_policy(
        &mut self,
        policy: crate::runtime_controls::Policy,
    ) -> io::Result<()> {
        let expected = self.policy.as_ref().expect("launch policy");
        let mode = match self.runtime_policy.as_ref() {
            None => crate::tool_mode::ValidationMode::Launch,
            Some(old) if old.binding == policy.binding => {
                crate::tool_mode::ValidationMode::Current(&policy)
            }
            Some(_) => crate::tool_mode::ValidationMode::Target,
        };
        expected.validate_facts(&policy.facts, mode)?;
        if let Some(old) = self.runtime_policy.as_ref() {
            if old.binding.work_instance == policy.binding.work_instance
                && old.binding != policy.binding
            {
                return Err(io::Error::other(
                    "Runtime capability or identity changed within one Work epoch",
                ));
            }
            if old.binding == policy.binding && policy.generation != old.generation + 1 {
                return Err(io::Error::other(
                    "Runtime control policy generation was repeated or skipped",
                ));
            }
            if old.binding != policy.binding && policy.generation != 0 {
                return Err(io::Error::other(
                    "New runtime control epoch did not start at generation zero",
                ));
            }
        } else if policy.generation != 0 {
            return Err(io::Error::other(
                "Initial runtime control policy did not start at generation zero",
            ));
        }
        self.runtime_policy = Some(policy);
        Ok(())
    }
    fn verify_runtime_owner(
        &self,
        owner: &crate::runtime_controls::Owner,
        known: Option<crate::work_lifecycle::Origin>,
        current: bool,
    ) -> io::Result<()> {
        use crate::runtime_controls::OwnerState;
        let valid = match owner.state {
            // A readback's owner is read at publication. After a Quit the source may
            // publish it once the stream has retired (idle or terminal), and this side
            // never settles a quit-drained turn: its observed end is the retirement evidence.
            OwnerState::Idle => {
                (!self.sent || self.settled || self.quit_ended) && !self.work.owns_work()
            }
            OwnerState::Active | OwnerState::Terminal => {
                let origin = owner.origin.as_ref().expect("strict owner parser");
                self.started
                    && origin.operation == self.command_id
                    && known.as_ref() == Some(origin)
                    && match owner.state {
                        OwnerState::Active => !current || self.terminal.is_none() && !self.settled,
                        OwnerState::Terminal => self.terminal.is_some() || self.quit_ended,
                        _ => false,
                    }
            }
        };
        if !valid {
            return Err(io::Error::other(
                "Runtime control owner differs from the exact admitted stream",
            ));
        }
        Ok(())
    }
    fn verify_runtime_readback(
        &self,
        value: &crate::runtime_controls::Readback,
        receipt: Option<&crate::runtime_controls::Receipt>,
        known: Option<crate::work_lifecycle::Origin>,
    ) -> io::Result<()> {
        use crate::runtime_controls::{EffectiveTransition, OwnerState, Release};
        let receipt =
            receipt.ok_or_else(|| io::Error::other("Runtime readback lacks its exact result"))?;
        if value.release == Release::Close {
            return Ok(());
        }
        if let Some(owner) = value.owner.as_ref() {
            self.verify_runtime_owner(owner, known.clone(), true)?;
            if value.release == Release::Install
                && owner.state == OwnerState::Idle
                && let Some(previous) = receipt.owner_after.as_ref().and_then(|o| o.origin.as_ref())
                && (known.as_ref() != Some(previous)
                    || self.command_id != previous.operation
                    || !(self.settled || self.quit_ended))
            {
                return Err(io::Error::other(
                    "Runtime readback lost the exact settled predecessor",
                ));
            }
        }
        // The current source has no ordinary turn-finish seat restoration. A
        // terminal event alone is not evidence of such a policy assignment.
        if value.effective_transition == Some(EffectiveTransition::RestoreSeat) {
            return Err(io::Error::other(
                "Runtime effective-profile restoration has no canonical source transition",
            ));
        }
        Ok(())
    }
    fn can_manage(&self) -> bool {
        self.can_engine()
            && self
                .engine_admission
                .is_none_or(|a| a == crate::engine_controls::Admission::PromptReady)
    }
    fn can_engine(&self) -> bool {
        self.ready
            && !self.closing
            && !self.rewrite_active
            && !self.work.uncertain
            && !self.work.owns_work()
            && (!self.sent || self.settled)
    }
    fn can_reconcile_work(&self) -> bool {
        self.ready
            && !self.closing
            && !self.rewrite_active
            && !self.work.owns_work()
            && self.work.ended.is_empty()
            && (!self.sent || self.settled)
            && self.approvals.settled()
            && self.questions.settled()
            && self.aborts.settled()
    }
    pub fn with_policy(policy: crate::tool_mode::Expected) -> Self {
        Self {
            policy: Some(policy),
            ..Self::default()
        }
    }
    /// Reserve a new command ID exactly once. Neither a write nor admission.
    pub fn submit(&mut self, prompt: &str) -> io::Result<Value> {
        if prompt.trim().is_empty() || prompt.len() > 65536 {
            return Err(io::Error::other("Prompt is empty or exceeds 64 KiB"));
        }
        let id = self.reserve_operation()?;
        if let Some(policy) = &self.runtime_policy {
            self.ordinary_runtime_prompt = true;
            Ok(
                json!({"type":"prompt","id":id,"message":prompt,"runtime_policy":policy.prompt_binding()}),
            )
        } else {
            Ok(
                json!({"type":"prompt","id":id,"message":prompt,"tool_profile":self.policy.as_ref().map_or("strict",|p|p.profile.as_str())}),
            )
        }
    }
    fn reserve_operation(&mut self) -> io::Result<String> {
        if !self.ready
            || self
                .engine_admission
                .is_some_and(|a| a != crate::engine_controls::Admission::PromptReady)
            || self.closing
            || self.work.uncertain
            || self.work.owns_work()
            || self.rewrite_active
            || !self.approvals.settled()
            || !self.questions.settled()
            || (self.sent && !self.settled)
        {
            return Err(io::Error::other(
                "Runtime is not idle, or prompt is empty/too large",
            ));
        }
        // Provider journals outlive this process. An ordinal, timestamp or PID
        // can alias a saved operation after restart; reserve fresh OS randomness.
        self.reset_operation(fresh_prompt_id()?)?;
        Ok(self.command_id.clone())
    }
    fn reset_operation(&mut self, operation: String) -> io::Result<()> {
        self.work.reserve_operation(&operation)?;
        self.command_id = operation;
        self.image_prompt = None;
        self.image_retained = false;
        self.ordinary_runtime_prompt = false;
        self.continued_input = None;
        self.rewrite_input = None;
        self.compact_input = None;
        self.compact_admission = None;
        self.compact_root_started = false;
        self.compact_root_ended = false;
        self.rewrite_admission = None;
        self.rewrite_first_terminal = false;
        self.rewrite_segments.clear();
        self.rewrite_aftercare.clear();
        self.rewrite_executions.clear();
        self.observed_tools.clear();
        self.host_content_follows_tool = false;
        self.sent = true;
        self.started = false;
        self.native_turns.clear();
        self.native_turn_bytes = 0;
        self.acknowledged = false;
        self.admission_refused = false;
        self.settled = false;
        self.terminal = None;
        self.stop_requests = 0;
        self.stop_acknowledged = false;
        self.visible_answers.clear();
        self.tools_seen = false;
        self.card_count = 1;
        self.content_bytes = 0;
        Ok(())
    }
    pub fn submit_continuation(
        &mut self,
        prompt: &str,
        session: &str,
        entry: &str,
    ) -> io::Result<String> {
        let command = self.submit(prompt)?;
        self.continued_input = Some((session.to_owned(), entry.to_owned()));
        Ok(command["id"].as_str().expect("reserved").to_owned())
    }
    pub fn submit_rewrite(
        &mut self,
        review: &crate::conversation_rewrite::Review,
    ) -> io::Result<String> {
        let command = self.submit(&review.text)?;
        self.rewrite_input = Some(review.clone());
        self.rewrite_active = true;
        Ok(command["id"]
            .as_str()
            .expect("reserved execution ID")
            .to_owned())
    }
    pub fn submit_compact(
        &mut self,
        review: &crate::conversation_compact::Review,
    ) -> io::Result<String> {
        let operation = self.reserve_operation()?;
        self.compact_input = Some(review.clone());
        self.rewrite_active = true;
        Ok(operation)
    }
    fn action_identity(&self) -> io::Result<(&str, &str)> {
        if !self.rewrite_active {
            return Err(io::Error::other("No active reviewed conversation action"));
        }
        if let Some(review) = &self.compact_input {
            Ok((&review.action_id, &review.before.selection.identity.session))
        } else if let Some(review) = &self.rewrite_input {
            Ok((&review.action_id, &review.before.selection.identity.session))
        } else {
            Err(io::Error::other("Missing reviewed action identity"))
        }
    }
    fn rewrite_origin(&self, event: &Value) -> io::Result<String> {
        let (_, session) = self.action_identity()?;
        let origin = &event[if self.compact_input.is_some() {
            "compact_origin"
        } else {
            "rewrite_origin"
        }];
        if origin["session_id"] != session
            || origin["operation_id"] != self.command_id
            || origin["source"] != "rpc"
        {
            return Err(io::Error::other(
                "Conversation action changed its captured operation origin",
            ));
        }
        crate::stream::required_uuid4(origin, "execution_id")
            .map(str::to_owned)
            .map_err(|_| io::Error::other("Invalid conversation action execution identity"))
    }
    fn transfer_rewrite_terminal(&mut self) -> Option<TerminalOutcome> {
        if !self.settled
            && self.terminal.is_some()
            && self.approvals.settled()
            && self.questions.settled()
        {
            // Transfer a finished display row independently of action-wide Stop
            // receipts. Rewrite readback still waits for every receipt to settle.
            self.settled = true;
            self.terminal.clone()
        } else {
            None
        }
    }
    fn rewrite_readback_ready(&self) -> bool {
        self.rewrite_active
            && !self.work.owns_work()
            && self.rewrite_first_terminal
            && self.acknowledged
            && self.aborts.settled()
            && self.approvals.settled()
            && self.questions.settled()
    }
    fn finish_rewrite(&mut self) -> io::Result<()> {
        if !self.rewrite_readback_ready()
            || (!self.settled && (self.compact_input.is_none() || self.started))
            || !self.rewrite_aftercare.is_empty()
        {
            return Err(io::Error::other(
                "Rewrite readback preceded stream, maintenance or input settlement",
            ));
        }
        self.rewrite_active = false;
        self.settled = true;
        Ok(())
    }
    fn rewrite_segment(&mut self, event: &Value, model: &str) -> io::Result<Observation> {
        let execution = self.rewrite_origin(event)?;
        let marker = &event[if self.compact_input.is_some() {
            "compact_segment"
        } else {
            "rewrite_segment"
        }];
        let (action_id, _) = self.action_identity()?;
        let index = self.rewrite_segments.len();
        let prior = self.rewrite_segments.last();
        if event["model"] != model
            || marker["action_id"] != action_id
            || marker["index"].as_u64() != Some(index as u64)
            || marker.get("previous_execution_id") != Some(&prior.map_or(Value::Null, |s| json!(s)))
            || index >= 100
            || self.rewrite_executions.contains(&execution)
            || ((index > 0 || self.compact_input.is_some()) && self.stop_acknowledged)
            || (self.compact_input.is_some() && !self.compact_root_ended)
            || self
                .compact_input
                .as_ref()
                .is_some_and(|review| !review.before.config.wake_after_compact)
            || self.closing
            || !self.approvals.settled()
            || !self.questions.settled()
            || (index == 0 && (self.started || self.terminal.is_some() || self.settled))
            || (index > 0 && self.terminal.is_none())
        {
            return Err(io::Error::other(
                "Invalid or out-of-order rewrite stream segment",
            ));
        }
        let previous_outcome = self.transfer_rewrite_terminal();
        self.rewrite_executions.insert(execution.clone());
        self.rewrite_segments.push(execution.clone());
        self.started = true;
        self.terminal = None;
        self.settled = false;
        self.visible_answers.clear();
        self.tools_seen = false;
        self.card_count = 1;
        Ok(Observation::RewriteSegment {
            operation_id: self.command_id.clone(),
            execution_id: execution,
            index: index as u64,
            previous_outcome,
        })
    }
    fn rewrite_aftercare_event(&mut self, event: &Value) -> io::Result<Observation> {
        let execution = self.rewrite_origin(event)?;
        if self.compact_input.is_some() && event["root"] == true {
            return self.compact_root_event(event, execution);
        }
        if self.compact_input.is_some() && event["root"] != false {
            return Err(io::Error::other("Missing compact maintenance role"));
        }
        let (action_id, _) = self.action_identity()?;
        let after = crate::stream::required_uuid4(event, "after_execution_id")
            .map_err(|_| io::Error::other("Invalid maintenance predecessor"))?;
        if event["action_id"] != action_id
            || event["aftercare_id"] != execution
            || event["kind"] != "compaction"
        {
            return Err(io::Error::other(
                "Rewrite maintenance changed its action or worker identity",
            ));
        }
        let running = match event["phase"].as_str() {
            Some("start")
                if !self.closing
                    && !self.stop_acknowledged
                    && self.terminal.is_some()
                    && self.approvals.settled()
                    && self.questions.settled()
                    && self.rewrite_segments.last().map(String::as_str) == Some(after)
                    && self.rewrite_executions.len() < 256
                    && !self.rewrite_executions.contains(&execution) =>
            {
                true
            }
            Some("end")
                if self
                    .rewrite_aftercare
                    .get(&execution)
                    .and_then(|v| v.as_deref())
                    == Some(after) =>
            {
                false
            }
            _ => {
                return Err(io::Error::other(
                    "Invalid or out-of-order rewrite maintenance boundary",
                ));
            }
        };
        if running {
            self.rewrite_executions.insert(execution.clone());
            self.rewrite_aftercare
                .insert(execution.clone(), Some(after.to_owned()));
        } else {
            self.rewrite_aftercare.remove(&execution);
        }
        let previous_outcome = self.transfer_rewrite_terminal();
        Ok(Observation::RewriteAftercare {
            operation_id: self.command_id.clone(),
            aftercare_id: execution,
            running,
            previous_outcome,
        })
    }
    fn compact_root_event(&mut self, event: &Value, execution: String) -> io::Result<Observation> {
        let review = self
            .compact_input
            .as_ref()
            .ok_or_else(|| io::Error::other("Root maintenance without compact review"))?;
        if event["action_id"] != review.action_id
            || execution != review.root_execution_id
            || event["aftercare_id"] != execution
            || event["kind"] != "compaction"
            || event.get("after_execution_id") != Some(&Value::Null)
        {
            return Err(io::Error::other(
                "Compact root changed its reviewed authority",
            ));
        }
        let running = match event["phase"].as_str() {
            Some("start")
                if !self.closing
                    && !self.compact_root_started
                    && !self.started
                    && self.rewrite_aftercare.is_empty()
                    && self.approvals.settled()
                    && self.questions.settled()
                    && self.rewrite_executions.insert(execution.clone()) =>
            {
                true
            }
            Some("end")
                if self.compact_root_started
                    && !self.compact_root_ended
                    && self.rewrite_aftercare.get(&execution) == Some(&None) =>
            {
                false
            }
            _ => {
                return Err(io::Error::other(
                    "Unexpected/replayed compact root boundary",
                ));
            }
        };
        if running {
            self.compact_root_started = true;
            self.rewrite_aftercare.insert(execution.clone(), None);
        } else {
            self.compact_root_ended = true;
            self.rewrite_first_terminal = true;
            self.rewrite_aftercare.remove(&execution);
        }
        Ok(Observation::RewriteAftercare {
            operation_id: self.command_id.clone(),
            aftercare_id: execution,
            running,
            previous_outcome: None,
        })
    }
    fn rewrite_interactive(&self, event: &Value) -> bool {
        self.rewrite_origin(event)
            .is_ok_and(|execution| self.rewrite_aftercare.contains_key(&execution))
    }
    fn rewrite_tool(&mut self, event: &Value, kind: &str) -> io::Result<Observation> {
        let execution = self.rewrite_origin(event)?;
        let maintenance = self.rewrite_aftercare.contains_key(&execution);
        if !(maintenance
            || self.terminal.is_none() && self.rewrite_segments.last() == Some(&execution))
        {
            return Err(io::Error::other(
                "Tool observation has no active rewrite worker",
            ));
        }
        let activity = self.tool_activity(event, kind, &execution, 0)?;
        Ok(Observation::RewriteActivity {
            execution_id: execution,
            activity,
        })
    }

    fn tool_activity(
        &mut self,
        event: &Value,
        kind: &str,
        execution: &str,
        mut card: usize,
    ) -> io::Result<crate::native_activity::Activity> {
        use crate::native_activity::{Activity, Detail};
        let (id, name, detail) = if kind == "generic_tool_returned" {
            let id = crate::stream::required_uuid4(event, "observation_id")
                .map_err(|_| io::Error::other("Invalid tool observation identity"))?
                .to_owned();
            let (_, request_card, _) = self
                .observed_tools
                .get(&id)
                .filter(|(owner, _, returned)| owner == execution && !returned)
                .ok_or_else(|| io::Error::other("Missing or already returned tool observation"))?;
            card = *request_card;
            let result = event["result"]
                .as_str()
                .filter(|s| s.len() <= FRAME_BYTES)
                .ok_or_else(|| io::Error::other("Invalid tool result"))?
                .to_owned();
            let ok = event["ok"]
                .as_bool()
                .ok_or_else(|| io::Error::other("Invalid tool return flag"))?;
            (
                id,
                None,
                Detail::Result {
                    text: result,
                    is_error: Some(!ok),
                },
            )
        } else {
            if self.observed_tools.len() >= 100 {
                return Err(io::Error::other("Tool observation limit exceeded"));
            }
            let name = event["name"]
                .as_str()
                .filter(|s| !s.is_empty() && s.len() <= 512)
                .ok_or_else(|| io::Error::other("Invalid tool observation name"))?
                .to_owned();
            if kind == "generic_tool_requested" {
                let id = crate::stream::required_uuid4(event, "observation_id")
                    .map_err(|_| io::Error::other("Invalid tool observation identity"))?
                    .to_owned();
                if self.observed_tools.contains_key(&id)
                    || event["round"].as_u64().is_none()
                    || event["slot"].as_u64().is_none()
                    || !event["provider_call_id"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 512)
                {
                    return Err(io::Error::other(
                        "Invalid or duplicate generic tool observation",
                    ));
                }
                let text = format!(
                    "Host invocation observed; round {}, slot {}, provider call {}",
                    event["round"], event["slot"], event["provider_call_id"]
                );
                (
                    id,
                    Some(name),
                    Detail::Input {
                        text: Some(text),
                        complete: true,
                    },
                )
            } else {
                if !event["args"].is_object() {
                    return Err(io::Error::other("Tool arguments must be an object"));
                }
                (
                    format!("display-tool-{}", self.observed_tools.len()),
                    Some(name),
                    Detail::Input {
                        text: Some(event["args"].to_string()),
                        complete: true,
                    },
                )
            }
        };
        let activity = Activity {
            card,
            id: id.clone(),
            name,
            detail,
        };
        self.content_bytes = self
            .content_bytes
            .checked_add(activity.emitted_bytes())
            .filter(|n| *n <= 4 * FRAME_BYTES)
            .ok_or_else(|| io::Error::other("Live content budget exceeded"))?;
        if kind == "generic_tool_returned" {
            self.observed_tools.get_mut(&id).expect("validated").2 = true;
        } else {
            self.observed_tools
                .insert(id, (execution.to_owned(), card, false));
        }
        Ok(activity)
    }
    fn rewrite_compaction(&mut self, event: &Value) -> io::Result<Observation> {
        let execution = self.rewrite_origin(event)?;
        let deferred =
            event["reason"] == "deferred_busy" && self.rewrite_segments.last() == Some(&execution);
        if (!self.rewrite_aftercare.contains_key(&execution) && !deferred)
            || !matches!(
                event["reason"].as_str(),
                Some(
                    "compacted"
                        | "failed"
                        | "cancelled"
                        | "too_short"
                        | "already_recent"
                        | "deferred_busy"
                )
            )
            || ["tokens_before_exact", "tokens_after_exact"]
                .iter()
                .any(|k| !event[k].is_boolean())
            || ["messages_dropped", "messages_summarised", "kept_recent"]
                .iter()
                .any(|k| event[k].as_u64().is_none_or(|n| n > 1_000_000_000))
            || ["tokens_before", "tokens_after"].iter().any(|k| {
                !matches!(event.get(*k), Some(Value::Null))
                    && !event[k]
                        .as_f64()
                        .is_some_and(|n| n.is_finite() && (0.0..=1e12).contains(&n))
            })
        {
            return Err(io::Error::other("Malformed or unowned compaction status"));
        }
        let text = format!(
            "Compaction: {}. Messages summarised: {}; dropped: {}; kept recent: {}. Tokens before: {} (exact: {}); after: {} (exact: {}).",
            event["reason"].as_str().unwrap(),
            event["messages_summarised"],
            event["messages_dropped"],
            event["kept_recent"],
            event["tokens_before"],
            event["tokens_before_exact"],
            event["tokens_after"],
            event["tokens_after_exact"]
        );
        self.content_bytes = self
            .content_bytes
            .checked_add(text.len())
            .filter(|n| *n <= 4 * FRAME_BYTES)
            .ok_or_else(|| io::Error::other("Live content budget exceeded"))?;
        if deferred {
            return Ok(Observation::Information(text));
        }
        Ok(Observation::RewriteStatus {
            execution_id: execution,
            text,
        })
    }
    fn settle(&mut self) -> Observation {
        self.settled = true;
        self.work.ended.remove(&self.command_id);
        match self
            .terminal
            .as_ref()
            .expect("terminal outcome before settlement")
        {
            TerminalOutcome::Complete => Observation::Complete,
            TerminalOutcome::Refused(reason) if self.image_retained => {
                let (review, prompt) = self
                    .image_prompt
                    .as_ref()
                    .expect("retained submitted image");
                Observation::ImageRefused {
                    reason: reason.clone(),
                    review: review.clone(),
                    prompt: prompt.clone(),
                }
            }
            TerminalOutcome::Refused(reason) => Observation::Refused(reason.clone()),
            TerminalOutcome::Failed(reason) => Observation::Failed(reason.clone()),
            TerminalOutcome::Cancelled(reason) => Observation::Cancelled(reason.clone()),
            TerminalOutcome::ApprovalStopped(reason) => {
                Observation::ApprovalStopped(reason.clone())
            }
        }
    }
    fn can_settle(&self) -> bool {
        self.terminal.is_some()
            && (self.rewrite_active || !self.work.pending_operation(&self.command_id))
            && self.acknowledged
            && !self.settled
            && self.aborts.settled()
            && self.approvals.settled()
            && self.questions.settled()
    }
    /// A stale UI stop must never be applied to a later prompt.
    pub fn abort(&mut self, prompt_id: &str) -> io::Result<Option<Value>> {
        if self.closing
            || !self.sent
            || ((self.compact_input.is_none() || !self.rewrite_active)
                && (self.settled || self.terminal.is_some())
                && self.rewrite_aftercare.is_empty())
            || prompt_id != self.command_id
            || self.stop_requests >= 2
        {
            return Ok(None);
        }
        let command = self.aborts.request()?;
        self.stop_requests += 1;
        Ok(Some(command))
    }
    pub fn answer(&mut self, decision: crate::questions::Decision) -> io::Result<Option<Value>> {
        if self.closing || self.stop_requests > 0 {
            return Ok(None);
        }
        self.questions.decide(decision)
    }
    fn question_observation(&mut self, event: crate::questions::Event) -> Observation {
        let settled = if self.can_settle() {
            self.settled = true;
            self.work.ended.remove(&self.command_id);
            self.terminal.clone()
        } else {
            None
        };
        Observation::Question { event, settled }
    }
    pub fn approve(&mut self, decision: crate::approvals::Decision) -> io::Result<Option<Value>> {
        if self.closing || self.stop_requests > 0 {
            return Ok(None);
        }
        self.approvals.decide(decision)
    }
    pub fn accept(&mut self, event: &Value, backend: &str, model: &str) -> io::Result<Observation> {
        let fail = |message: &str| io::Error::other(message.to_owned());
        let kind = event
            .get("type")
            .and_then(Value::as_str)
            .ok_or_else(|| fail("Missing event type"))?;
        let text = || {
            event
                .get("text")
                .and_then(Value::as_str)
                .ok_or_else(|| fail("Missing event text"))
        };
        match kind {
            "ready" => {
                if self.ready
                    || event["protocol_version"] != 1
                    || !(event["launch_status"] == "ready"
                        || (self.allow_engine_management
                            && matches!(backend, "ninfer" | "strata")
                            && event["launch_status"] == "management"
                            && event["admission"] == "management_only"
                            && event["effective_model"].is_null()
                            && matches!(
                                event["management_reason"].as_str(),
                                Some("engine_unavailable" | "engine_unloaded")
                            )))
                    || event["backend"] != backend
                    || event["model"] != model
                {
                    return Err(fail(&format!("Unexpected/blocked runtime ready: {event}")));
                }
                if let Some(policy) = &self.policy {
                    policy.validate(event)?;
                }
                if event.get("management_instance").is_some() {
                    if !matches!(backend, "ninfer" | "strata") {
                        return Err(fail("Engine startup metadata belongs to another backend"));
                    }
                    let admission = crate::engine_controls::Admission::parse(&event["admission"])?;
                    if event["desired_model"] != model
                        || (admission == crate::engine_controls::Admission::PromptReady
                            && event["effective_model"] != model)
                        || (event["launch_status"] == "ready"
                            && admission != crate::engine_controls::Admission::PromptReady)
                    {
                        return Err(fail("Contradictory engine startup admission"));
                    }
                    self.engine_shutdown = Some(crate::engine_controls::ShutdownIdentity {
                        instance: event["management_instance"]
                            .as_str()
                            .ok_or_else(|| fail("Missing engine instance"))?
                            .into(),
                        session: event["conversation_id"]
                            .as_str()
                            .ok_or_else(|| fail("Missing engine session"))?
                            .into(),
                        backend: backend.into(),
                    });
                    self.engine_admission = Some(admission);
                } else if event["launch_status"] == "management" {
                    return Err(fail("Missing explicit engine management identity"));
                }
                self.ready = true;
                Ok(
                    if self.engine_admission
                        == Some(crate::engine_controls::Admission::ManagementOnly)
                    {
                        Observation::EngineAdmission(
                            crate::engine_controls::Admission::ManagementOnly,
                        )
                    } else {
                        Observation::Ready
                    },
                )
            }
            "response"
                if self.closing
                    && event["id"] == SHUTDOWN_ID
                    && event["ok"] == true
                    && event["result"]["ok"] == true =>
            {
                crate::engine_controls::verify_shutdown(
                    &event["result"],
                    self.engine_shutdown.as_ref(),
                )?;
                crate::conversation_actions::verify_shutdown(
                    &event["result"],
                    self.conversation_shutdown.as_ref(),
                )
                .or_else(|error| {
                    if event["result"]["conversation_shutdown"]["pending_at_close"] == false
                        && self.conversation_completed_shutdown.is_some()
                    {
                        crate::conversation_actions::verify_shutdown(
                            &event["result"],
                            self.conversation_completed_shutdown.as_ref(),
                        )
                    } else {
                        Err(error)
                    }
                })?;
                let rewrite_interrupted = if let Some(session) = &self.rewrite_current_session {
                    crate::conversation_rewrite::verify_shutdown(
                        &event["result"],
                        self.rewrite_shutdown.as_ref(),
                        session,
                    )?
                } else {
                    false
                };
                let compact_interrupted = if let Some(session) = &self.compact_current_session {
                    crate::conversation_compact::verify_shutdown(
                        &event["result"],
                        self.compact_shutdown.as_ref(),
                        session,
                    )?
                } else {
                    false
                };
                self.shutdown_acknowledged = true;
                Ok(if compact_interrupted {
                    Observation::ConversationUnverified("Shutdown joined the interrupted compaction; saved and provider effects remain unverified. Reopen to inspect.".into())
                } else if rewrite_interrupted {
                    Observation::ConversationUnverified("Shutdown joined the interrupted edit/retry; saved and provider effects remain unverified. Reopen to inspect.".into())
                } else if self.conversation_shutdown.is_some()
                    && self.conversation_shutdown != self.conversation_completed_shutdown
                {
                    Observation::ConversationUnverified(format!(
                        "Shutdown joined the interrupted {} action; saved effects remain unverified. Reopen the conversation to inspect.",
                        self.conversation_shutdown.as_ref().unwrap()["action"]
                            .as_str()
                            .unwrap_or("conversation")
                    ))
                } else {
                    Observation::Information(if self.engine_shutdown.is_some() {
                        "shutdown acknowledged; resource release unverified".into()
                    } else {
                        "shutdown acknowledged".into()
                    })
                })
            }
            // A failed or refused joined shutdown is the source's own verdict on Quit, not a
            // stray prompt acknowledgement; say what the source said.
            "response" if self.closing && event["id"] == SHUTDOWN_ID => {
                let reason = event["error"]
                    .as_str()
                    .map(str::trim)
                    .filter(|text| !text.is_empty() && text.len() <= 512);
                Err(io::Error::other(match reason {
                    Some(text) => format!(
                        "Runtime shutdown failed: {}",
                        terminal_text(text).replace(['\n', '\t'], " ")
                    ),
                    None => "Runtime shutdown was not acknowledged".to_owned(),
                }))
            }
            "response" => {
                if let Some(update) = self.work_local_response(event)? {
                    return Ok(update);
                }
                if let Some(stopped) = self.aborts.response(event)? {
                    // A local Stop can cross already emitted source starts. Its first
                    // receipt orders the barrier; a second pending Stop cannot reopen it.
                    self.stop_acknowledged = true;
                    return Ok(if self.can_settle() {
                        self.settle()
                    } else {
                        Observation::Information(
                            if stopped {
                                "Stop requested; waiting for source turn settlement."
                            } else {
                                "Source was idle at stop request; waiting for turn settlement."
                            }
                            .into(),
                        )
                    });
                }
                if let Some(event) = self.questions.response(event)? {
                    return Ok(self.question_observation(event));
                }
                if let Some(event) = self.approvals.response(event)? {
                    if self.can_settle() {
                        return Ok(self.settle());
                    }
                    return Ok(Observation::Approval(event));
                }
                if self.image_prompt.is_some()
                    && event["id"] == self.command_id
                    && self.sent
                    && !self.acknowledged
                    && event["ok"] == true
                    && event["result"]["turn"] == "image_refused"
                {
                    let retained = &event["result"]["retained"];
                    if !retained.is_null() {
                        if retained != self.image_prompt.as_ref().expect("image").0.wire() {
                            return Err(fail("Source retained a different image"));
                        }
                        self.image_retained = true;
                    }
                    if self.started || self.terminal.is_some() {
                        return Err(fail("Image refused after execution started"));
                    }
                    let reason = event["result"]["reason"]
                        .as_str()
                        .filter(|s| !s.is_empty() && s.len() <= 4096)
                        .ok_or_else(|| fail("Missing image refusal reason"))?
                        .to_owned();
                    self.acknowledged = true;
                    self.admission_refused = true;
                    self.terminal = Some(TerminalOutcome::Refused(reason));
                    return Ok(if self.can_settle() {
                        self.settle()
                    } else {
                        Observation::Information("Image refused; waiting for stop receipts.".into())
                    });
                }
                if let Some(review) = &self.compact_input
                    && event["id"] == self.command_id
                    && self.sent
                    && !self.acknowledged
                    && event["ok"] == true
                {
                    use crate::conversation_compact::{Admission, AdmissionStatus};
                    let admission = Admission::parse(&event["result"], review, &self.command_id)?;
                    if admission.status == AdmissionStatus::Failed || admission.close_required {
                        if self.closing {
                            self.acknowledged = true;
                            self.compact_admission = Some(admission);
                            return Ok(Observation::Information("Compaction interrupted during shutdown; waiting for owned cleanup. Effects remain unverified.".into()));
                        }
                        return Err(fail("Compaction admission is uncertain; reopen to inspect"));
                    }
                    if admission.status == AdmissionStatus::Refused {
                        if self.compact_root_started || self.started || self.terminal.is_some() {
                            return Err(fail("Compaction refusal after execution started"));
                        }
                        self.admission_refused = true;
                        self.rewrite_first_terminal = true;
                    }
                    self.acknowledged = true;
                    self.compact_admission = Some(admission);
                    return Ok(if self.can_settle() {
                        self.settle()
                    } else {
                        Observation::Information("Compaction admission received; waiting for source work and saved-history verification.".into())
                    });
                }
                if let Some(review) = &self.rewrite_input
                    && event["id"] == self.command_id
                    && self.sent
                    && !self.acknowledged
                    && event["ok"] == true
                {
                    use crate::conversation_rewrite::{Admission, AdmissionOutcome};
                    let admission = Admission::parse(&event["result"], review, &self.command_id)?;
                    if admission.outcome == AdmissionOutcome::Failed || admission.close_required {
                        if self.closing {
                            self.acknowledged = true;
                            self.rewrite_admission = Some(admission);
                            return Ok(Observation::Information(
                                "Edit/retry interrupted during shutdown; waiting for owned cleanup. Effects remain unverified.".into(),
                            ));
                        }
                        return Err(fail(
                            "Edit/retry publication or execution is uncertain; reopen to inspect",
                        ));
                    }
                    let refused = admission.outcome == AdmissionOutcome::Refused;
                    if refused && (self.started || self.terminal.is_some()) {
                        return Err(fail("Rewrite refusal after execution started"));
                    }
                    let reason = admission.reason.clone();
                    self.rewrite_admission = Some(admission);
                    if refused {
                        self.acknowledged = true;
                        self.admission_refused = true;
                        self.rewrite_first_terminal = true;
                        self.terminal =
                            Some(TerminalOutcome::Refused(reason.expect("verified refusal")));
                        return Ok(if self.can_settle() {
                            self.settle()
                        } else {
                            Observation::Information(
                                "Edit/retry refused; waiting for pending input/stop receipts."
                                    .into(),
                            )
                        });
                    }
                }
                if let Some((session, entry)) = &self.continued_input
                    && event["id"] == self.command_id
                    && self.sent
                    && !self.acknowledged
                    && event["ok"] == true
                {
                    let result = &event["result"];
                    if result["session_id"] != *session
                        || result["entry_id"] != *entry
                        || result["held"].as_u64().is_none_or(|n| n > 1000)
                    {
                        return Err(fail("Continuation receipt identity/count is invalid"));
                    }
                    if result["turn"] == "refused" {
                        if self.started || self.terminal.is_some() {
                            return Err(fail("Refusal after execution started"));
                        }
                        let reason = result["reason"]
                            .as_str()
                            .filter(|s| !s.is_empty() && s.len() <= 4096)
                            .ok_or_else(|| fail("Missing continuation refusal reason"))?
                            .to_owned();
                        self.acknowledged = true;
                        self.admission_refused = true;
                        self.terminal = Some(TerminalOutcome::Refused(reason));
                        return Ok(if self.can_settle() {
                            self.settle()
                        } else {
                            Observation::Information(
                                "Input refused; waiting for input/stop receipts.".into(),
                            )
                        });
                    }
                }
                if event["id"] != self.command_id || !self.sent || self.acknowledged {
                    return Err(fail("Unexpected prompt response"));
                }
                if event["ok"] == false && !self.started && self.terminal.is_none() {
                    let reason = event["error"].as_str().map(str::trim)
                        .filter(|text| !text.is_empty() && text.len() <= 512);
                    return Err(match reason {
                        Some(text) => io::Error::other(format!(
                            "Runtime refused the prompt: {}", terminal_text(text).replace(['\n', '\t'], " ")
                        )),
                        None => fail("Runtime refused the prompt"),
                    });
                }
                if event["ok"] != true || event["result"]["turn"] != "accepted" {
                    return Err(fail("Invalid prompt acknowledgement"));
                }
                self.acknowledged = true;
                Ok(if self.can_settle() {
                    self.settle()
                } else {
                    Observation::Information("prompt acknowledged (not completion)".into())
                })
            }
            "submit_refused"
                if self.image_prompt.is_some()
                    && self.sent
                    && !self.started
                    && self.terminal.is_none() =>
            {
                let reason = event["detail"]
                    .as_str()
                    .filter(|s| !s.is_empty() && s.len() <= 4096)
                    .ok_or_else(|| fail("Missing source image refusal detail"))?
                    .to_owned();
                self.admission_refused = true;
                self.terminal = Some(TerminalOutcome::Refused(reason));
                Ok(if self.can_settle() {
                    self.settle()
                } else {
                    Observation::Information(
                        "Source refused the image; waiting for its command receipt.".into(),
                    )
                })
            }
            "cache_warning" | "turn_refused"
                if backend == "claude"
                    && self.sent
                    && !self.started
                    && self.terminal.is_none()
                    && !self.settled =>
            {
                if event["provider"] != "claude" {
                    return Err(fail("Pre-start observation belongs to another provider"));
                }
                let reason = event["reason"]
                    .as_str()
                    .filter(|s| !s.trim().is_empty() && s.len() <= 4096)
                    .ok_or_else(|| fail("Invalid pre-start refusal/warning reason"))?
                    .to_owned();
                self.content_bytes = self
                    .content_bytes
                    .checked_add(reason.len())
                    .filter(|size| *size <= 4 * FRAME_BYTES)
                    .ok_or_else(|| fail("Live content budget exceeded"))?;
                if kind == "cache_warning" {
                    return Ok(Observation::Information(format!(
                        "Claude cache warning: {reason}"
                    )));
                }
                self.terminal = Some(TerminalOutcome::Refused(reason));
                Ok(if self.can_settle() {
                    self.settle()
                } else if let Some(update) = self.work_terminal() {
                    update
                } else {
                    Observation::Information(
                        "Input not started; waiting for admission and input/stop receipts.".into(),
                    )
                })
            }
            "turn_start" if self.rewrite_active => self.rewrite_segment(event, model),
            "rewrite_aftercare" if self.rewrite_active && self.compact_input.is_none() => {
                self.rewrite_aftercare_event(event)
            }
            "compact_maintenance" if self.rewrite_active && self.compact_input.is_some() => {
                self.rewrite_aftercare_event(event)
            }
            "compaction" if self.rewrite_active => self.rewrite_compaction(event),
            "tool_call" | "generic_tool_requested" | "generic_tool_returned"
                if self.rewrite_active =>
            {
                self.rewrite_tool(event, kind)
            }
            "generic_tool_requested" | "generic_tool_returned"
                if self.started
                    && self.terminal.is_none()
                    && !matches!(backend, "claude" | "codex") =>
            {
                let operation = self.command_id.clone();
                let activity =
                    self.tool_activity(event, kind, &operation, self.card_count.saturating_sub(1))?;
                if kind == "generic_tool_requested" {
                    self.host_content_follows_tool = true;
                }
                Ok(Observation::Activity(activity))
            }
            // The separate tool_call diagnostic has no stable invocation ID. It cannot
            // stand in for a generic request, a result, or a permission decision.
            "tool_call" if self.started && self.terminal.is_none() => {
                let name = event["name"]
                    .as_str()
                    .filter(|name| !name.is_empty() && name.len() <= 512)
                    .ok_or_else(|| fail("Invalid tool diagnostic name"))?;
                if !event["args"].is_object() {
                    return Err(fail("Invalid tool diagnostic arguments"));
                }
                Ok(Observation::Information(format!(
                    "Tool call observed: {name}"
                )))
            }
            "turn_start"
                if self.sent && !self.started && self.terminal.is_none() && !self.settled =>
            {
                if event["model"] != model {
                    return Err(fail("Turn model differs from requested model"));
                }
                self.started = true;
                Ok(Observation::Information("turn started".into()))
            }
            "text_delta" | "native_text_snapshot" | "reasoning_delta"
                if self.started && self.terminal.is_none() =>
            {
                let mut card = crate::native_activity::card_index(event)?;
                if !self.rewrite_active
                    && !self.observed_tools.is_empty()
                    && !matches!(backend, "claude" | "codex")
                {
                    // The generic source streams each round without a card index. Place
                    // the first later text after the tools while retaining earlier cards.
                    if card.is_none() {
                        card = Some(if self.host_content_follows_tool {
                            self.card_count
                        } else {
                            self.card_count.saturating_sub(1)
                        });
                    }
                    self.host_content_follows_tool = false;
                }
                if card.is_none() && self.tools_seen {
                    return Err(fail("Content without native display ownership after tools"));
                }
                if card.is_some_and(|index| index >= 100 || index > self.card_count) {
                    return Err(fail("Invalid native card sequence"));
                }
                let reasoning = kind == "reasoning_delta";
                let replace = if reasoning {
                    match event.get("reconcile").and_then(Value::as_str) {
                        Some("append") => false,
                        Some("replace") => true,
                        None if backend != "claude" && event.get("reconcile").is_none() => false,
                        _ => {
                            return Err(fail(
                                "Reasoning needs explicit append/replace reconciliation for native Claude",
                            ));
                        }
                    }
                } else {
                    kind == "native_text_snapshot"
                };
                let text = text()?.to_owned();
                // Answer and reasoning share the aggregate emitted-text budget,
                // including replacements. A trace is never an answer receipt.
                self.content_bytes = self
                    .content_bytes
                    .checked_add(text.len())
                    .filter(|size| *size <= 4 * FRAME_BYTES)
                    .ok_or_else(|| fail("Live content budget exceeded"))?;
                if let Some(index) = card {
                    self.card_count = self.card_count.max(index + 1);
                }
                if !reasoning {
                    if !text.is_empty() {
                        self.visible_answers.insert(card.unwrap_or(0));
                    } else if replace {
                        self.visible_answers.remove(&card.unwrap_or(0));
                    }
                }
                let content_kind = match (reasoning, replace) {
                    (true, true) => ContentKind::ReasoningSnapshot,
                    (true, false) => ContentKind::ReasoningDelta,
                    (false, true) => ContentKind::AnswerSnapshot,
                    (false, false) => ContentKind::AnswerDelta,
                };
                Ok(if let Some(card) = card {
                    Observation::Content {
                        card,
                        kind: content_kind,
                        text,
                    }
                } else {
                    match content_kind {
                        ContentKind::ReasoningSnapshot => Observation::ReasoningSnapshot(text),
                        ContentKind::ReasoningDelta => Observation::Reasoning(text),
                        ContentKind::AnswerSnapshot => Observation::Snapshot(text),
                        ContentKind::AnswerDelta => Observation::Text(text),
                    }
                })
            }
            "native_activity" if self.started && self.terminal.is_none() && backend == "claude" => {
                let activity = crate::native_activity::Activity::from_event(event)?;
                if activity.card >= self.card_count {
                    return Err(fail("Native tool has invalid display ownership"));
                }
                self.content_bytes = self
                    .content_bytes
                    .checked_add(activity.emitted_bytes())
                    .filter(|n| *n <= 4 * FRAME_BYTES)
                    .ok_or_else(|| fail("Live content budget exceeded"))?;
                self.tools_seen = true;
                Ok(Observation::Activity(activity))
            }
            "tool_approval_requested"
                if ((self.started && self.terminal.is_none())
                    || self.rewrite_interactive(event)
                    || !self.started && !self.acknowledged && self.rewrite_input.as_ref().is_some_and(|review| {
                        review.prompt_hooks && event.get("rewrite_prewrite").is_some_and(|marker| {
                            self.rewrite_origin(event).is_ok_and(|execution| {
                                *marker == json!({"session_id":review.before.selection.identity.session,
                                    "action_id":review.action_id,"operation_id":self.command_id,"execution_id":execution})
                            })
                        })
                    }))
                    && !self.closing =>
            {
                let event = self.approvals.request(event)?;
                if let crate::approvals::Event::Requested(request) = &event {
                    self.content_bytes = self
                        .content_bytes
                        .checked_add(request.text_bytes())
                        .filter(|n| *n <= 4 * FRAME_BYTES)
                        .ok_or_else(|| fail("Live content budget exceeded"))?;
                }
                Ok(Observation::Approval(event))
            }
            "user_input_requested"
                if ((self.started && self.terminal.is_none())
                    || self.rewrite_interactive(event))
                    && !self.closing =>
            {
                let event = self.questions.request(event)?;
                if let crate::questions::Event::Requested(request) = &event {
                    self.content_bytes = self
                        .content_bytes
                        .checked_add(request.text_bytes())
                        .filter(|size| *size <= 4 * FRAME_BYTES)
                        .ok_or_else(|| fail("Live content budget exceeded"))?;
                }
                Ok(self.question_observation(event))
            }
            "user_input_resolved" => {
                let event = self.questions.resolved(event)?;
                Ok(self.question_observation(event))
            }
            "tool_approval_closed" => {
                let event = self.approvals.close(event)?;
                Ok(if self.can_settle() {
                    self.settled = true;
                    self.work.ended.remove(&self.command_id);
                    Observation::ApprovalSettled {
                        event,
                        outcome: self.terminal.clone().expect("terminal"),
                    }
                } else {
                    Observation::Approval(event)
                })
            }
            "turn_end" if self.started && self.terminal.is_none() => {
                if self.closing && event["stopReason"] != "stop" {
                    self.quit_ended = true;
                    return Ok(Observation::Information(
                        "turn interrupted while quitting; not completed".into(),
                    ));
                }
                if (!self.acknowledged
                    && self.rewrite_input.is_none()
                    && self.compact_input.is_none())
                    || (event["stopReason"] != "cancelled"
                        && (!self.approvals.all_closed() || !self.questions.accounted_for()))
                {
                    return Err(fail(
                        "Turn ended before prompt admission or input settlement",
                    ));
                }
                let outcome = match event["stopReason"].as_str() {
                    Some("approval" | "cancelled") => {
                        // The source can refuse before asking this frontend (for
                        // example, no approval route). No answer or local denial
                        // is required, and a closure alone never implies this stop.
                        let reason = match event.get("error") {
                            None | Some(Value::Null) => "",
                            Some(Value::String(reason)) if reason.len() <= 16384 => reason,
                            _ => return Err(fail("Invalid or oversized turn-stop reason")),
                        };
                        let reason = if reason.is_empty() {
                            if event["stopReason"] == "cancelled" {
                                "Runtime cancelled the turn. Earlier tool effects are retained."
                            } else {
                                "Runtime stopped the turn for approval."
                            }
                        } else {
                            reason
                        };
                        self.content_bytes = self
                            .content_bytes
                            .checked_add(reason.len())
                            .filter(|size| *size <= 4 * FRAME_BYTES)
                            .ok_or_else(|| fail("Live content budget exceeded"))?;
                        if event["stopReason"] == "cancelled" {
                            TerminalOutcome::Cancelled(reason.to_owned())
                        } else {
                            TerminalOutcome::ApprovalStopped(reason.to_owned())
                        }
                    }
                    Some("error") => {
                        let reason = event["error"]
                            .as_str()
                            .filter(|s| !s.trim().is_empty() && s.len() <= 16384)
                            .ok_or_else(|| fail("Missing or oversized runtime failure reason"))?;
                        self.content_bytes = self
                            .content_bytes
                            .checked_add(reason.len())
                            .filter(|size| *size <= 4 * FRAME_BYTES)
                            .ok_or_else(|| fail("Live content budget exceeded"))?;
                        TerminalOutcome::Failed(reason.to_owned())
                    }
                    Some("stop")
                        if !self.visible_answers.is_empty()
                            && !event.get("error").is_some_and(|v| !v.is_null() && v != "") =>
                    {
                        TerminalOutcome::Complete
                    }
                    _ => {
                        return Err(fail(&format!(
                            "Turn did not complete successfully: {event}"
                        )));
                    }
                };
                self.terminal = Some(outcome);
                self.work_stream_ended();
                if self.rewrite_active {
                    self.rewrite_first_terminal = true;
                }
                if self.can_settle() {
                    Ok(self.settle())
                } else if let Some(update) = self.work_terminal() {
                    Ok(update)
                } else {
                    Ok(Observation::Information(
                        "Turn ended; waiting for input closures and command responses".into(),
                    ))
                }
            }
            "native_turn_started"
                if backend == "codex" && self.started && self.terminal.is_none() =>
            {
                if event["eventVersion"] != 1
                    || event["provider"] != "codex"
                    || ["threadId", "turnId"].iter().any(|key| {
                        !event[key].as_str().is_some_and(|s| {
                            !s.is_empty() && s.len() <= 4096 && !s.chars().any(char::is_control)
                        })
                    })
                {
                    return Err(fail("Malformed native Codex turn-start metadata"));
                }
                let pair = (
                    event["threadId"].as_str().unwrap().to_owned(),
                    event["turnId"].as_str().unwrap().to_owned(),
                );
                let bytes = pair.0.len() + pair.1.len();
                if self.native_turns.len() >= 1024
                    || self.native_turn_bytes.saturating_add(bytes) > 65536
                    || !self.native_turns.insert(pair)
                {
                    return Err(fail(
                        "Duplicate or excessive native Codex turn-start metadata",
                    ));
                }
                self.native_turn_bytes += bytes;
                Ok(Observation::Information("Codex native turn started".into()))
            }
            // These source-defined observations have no authority in this frontend.
            // Print their kind, rather than silently discarding unsupported state.
            "model_state" | "usage" | "native_usage" | "native_event" => {
                Ok(Observation::Information(kind.into()))
            }
            // Unknown input contracts remain unsupported; never auto-answer.
            _ => Err(fail(&format!(
                "Unsupported or out-of-order live event: {kind}"
            ))),
        }
    }
}

/// Never allow model/provider output to execute terminal control sequences.
pub fn terminal_text(text: &str) -> String {
    text.chars()
        .flat_map(|ch| {
            if (ch.is_control() && !matches!(ch, '\n' | '\t'))
                || matches!(ch, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
            {
                format!("\\u{{{:x}}}", ch as u32)
                    .chars()
                    .collect::<Vec<_>>()
            } else {
                vec![ch]
            }
        })
        .collect()
}

fn send(stdin: &mut ChildStdin, value: Value) -> io::Result<()> {
    serde_json::to_writer(&mut *stdin, &value)?;
    stdin.write_all(b"\n")?;
    stdin.flush()
}

struct OwnedChild {
    child: Child,
    job: crate::live_process::Job,
    stdin: Option<mpsc::SyncSender<Value>>,
    reaped: bool,
}
impl OwnedChild {
    fn request_shutdown(&mut self) -> io::Result<()> {
        if let Some(stdin) = self.stdin.take() {
            stdin
                .try_send(json!({"type":"shutdown","id":SHUTDOWN_ID}))
                .map_err(io::Error::other)?;
        }
        Ok(())
    }
    fn close(&mut self) -> io::Result<()> {
        if self.reaped {
            return Ok(());
        }
        let deadline = Instant::now() + Duration::from_secs(20);
        let write = self.request_shutdown(); // bounded queue, never a blocking pipe write
        let mut status = None;
        while Instant::now() < deadline {
            if status.is_none() {
                status = self.child.try_wait()?;
            }
            if let Some(status) = status
                && self.job.active()? == 0
            {
                self.reaped = true;
                if !status.success() {
                    return Err(io::Error::other(format!("Python exited with {status}")));
                }
                return write;
            }
            thread::sleep(Duration::from_millis(50));
        }
        self.job.terminate()?;
        self.child.wait()?;
        self.reaped = true;
        Err(io::Error::other(
            "Runtime job shutdown timed out; forced termination, provider cleanup unproven",
        ))
    }
}
impl Drop for OwnedChild {
    fn drop(&mut self) {
        // The job's kill-on-close remains armed even if graceful cleanup fails.
        if !self.reaped {
            let _ = self.close();
        }
    }
}

// Blocking output lives off the supervisor thread. Fail-stop on a full bounded
// queue; a stalled terminal cannot prevent child shutdown or deadline checks.
struct Output {
    tx: Option<mpsc::SyncSender<Vec<u8>>>,
    done: mpsc::Receiver<io::Result<()>>,
}
impl Output {
    fn new() -> Self {
        let (tx, rx) = mpsc::sync_channel::<Vec<u8>>(64);
        let (done_tx, done) = mpsc::channel();
        thread::spawn(move || {
            let result = (|| -> io::Result<()> {
                let mut output = io::stdout().lock();
                while let Ok(bytes) = rx.recv() {
                    output.write_all(&bytes)?;
                    output.flush()?;
                }
                Ok(())
            })();
            let _ = done_tx.send(result);
        });
        Self { tx: Some(tx), done }
    }
    fn finish(mut self) -> io::Result<()> {
        self.tx.take();
        self.done
            .recv_timeout(Duration::from_secs(2))
            .map_err(io::Error::other)?
    }
}
impl Write for Output {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        self.tx
            .as_ref()
            .expect("open output")
            .try_send(bytes.to_vec())
            .map_err(io::Error::other)?;
        Ok(bytes.len())
    }
    fn flush(&mut self) -> io::Result<()> {
        Ok(())
    }
}

enum Pipe {
    Stdout(Vec<u8>),
    Stderr(Vec<u8>),
    End(bool),
    Error(io::Error),
}
fn reader(mut input: impl Read + Send + 'static, stdout: bool, tx: mpsc::SyncSender<Pipe>) {
    thread::spawn(move || {
        let mut buffer = [0; 8192];
        loop {
            let message = match input.read(&mut buffer) {
                Ok(0) => {
                    let _ = tx.send(Pipe::End(stdout));
                    return;
                }
                Ok(n) if stdout => Pipe::Stdout(buffer[..n].to_vec()),
                Ok(n) => Pipe::Stderr(buffer[..n].to_vec()),
                Err(error) => {
                    let _ = tx.send(Pipe::Error(error));
                    return;
                }
            };
            if tx.send(message).is_err() {
                return;
            }
        }
    });
}

fn drive(options: Options, frontend: &mut impl Frontend) -> io::Result<()> {
    if options.launch.engine_management && !options.prompt.is_empty() {
        return Err(io::Error::other(
            "Engine management cannot queue a first prompt",
        ));
    }
    // Validate direct Options construction as well as CLI parsing, before any
    // data root, settings, executable lookup or child process side effects.
    let backend = crate::runtime_launch::Backend::parse(&options.backend)?;
    let launch_args = options.launch.python_args(backend, &options.model)?;
    let thinking_args = options
        .launch
        .thinking_args(backend, options.resume.is_some());
    let profile = options.selected_profile()?;
    let source = options.source.canonicalize()?;
    let python = options.python.canonicalize()?;
    if !source.join("src/litetui/cli.py").is_file()
        || !python.is_file()
        || !options.data_root.is_absolute()
    {
        return Err(io::Error::other(
            "Expected Python executable, LiteTUI source tree, and absolute data root (new unless --resume)",
        ));
    }
    let workspace = match &options.workspace {
        Some(path) if path.is_absolute() && path.is_dir() => path.canonicalize()?,
        Some(_) => {
            return Err(io::Error::other(
                "Workspace must be an existing absolute directory",
            ));
        }
        None if !options.tools_enabled => source.clone(),
        None => {
            return Err(io::Error::other(
                "Tools on requires an explicit absolute --workspace",
            ));
        }
    };
    let root = if let Some(id) = &options.resume {
        crate::resume::conversation_id(id)?;
        let root = options.data_root.canonicalize()?;
        crate::resume::validate_root(&root, options.tools_enabled)?;
        root
    } else {
        // Fresh launch never adopts an existing seat or rewrites its settings.
        if let Some(parent) = options.data_root.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::create_dir(&options.data_root)?;
        let root = options.data_root.canonicalize()?;
        fs::write(
            root.join("settings.json"),
            serde_json::to_vec_pretty(&json!({
                "backend": options.backend, "default_model": options.model,
                "backend_chosen": true, "user_name_asked": true,
                "tools_enabled": options.tools_enabled, "mcp_enabled": false, "seat_name": "RustLive",
                "tool_policy_profile": profile.as_str()
            }))?,
        )?;
        crate::resume::mark_fresh(&root)?;
        root
    };
    let mut command = Command::new(&python);
    command
        .current_dir(&workspace)
        .env("LITETUI_DATA_ROOT", &root)
        .env("LITETUI_APPROVAL_HOST", "1")
        // A fresh owned child has its own identity and this frontend's approval route.
        .env_remove("LITETUI_OWNER")
        .env_remove("LITETUI_SPAWN_IDENTITY")
        .env_remove("LITEHARNESS_SPAWNED_BY")
        .env_remove("LITEHARNESS_AGENT_ID")
        .env_remove("LITEHARNESS_AGENT_NAME")
        .env_remove("LITETUI_SEAT_NAME")
        .env("PYTHONPATH", source.join("src"))
        .env("PYTHONUTF8", "1")
        .env("PYTHONUNBUFFERED", "1")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .args([
            "-m",
            "litetui.cli",
            "--rpc",
            "--hold-restored-input",
            "--work-lifecycle-version",
            "1",
            "--history-projection-version",
            "1",
            "--backend",
            &options.backend,
            "--model",
            &options.model,
            "--tool-profile",
            profile.as_str(),
            "--mcp-lifecycle-version",
            "1",
            "--mcp-servers",
            "all",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(id) = &options.resume {
        command.args(["--convo", id]);
    }
    if let Some(store) = &options.owner {
        command
            .arg("--owner")
            .arg(&store.name)
            .arg("--memory")
            .arg(&store.directory);
    }
    command.args(launch_args).args(thinking_args);
    let (job, mut child) = crate::live_process::Job::spawn(&mut command)?;
    let mut stdin = child.stdin.take().expect("piped stdin");
    let stdout = child.stdout.take().expect("piped stdout");
    let stderr = child.stderr.take().expect("piped stderr");
    let (input_tx, input_rx) = mpsc::sync_channel(2);
    let mut owned = OwnedChild {
        child,
        job,
        stdin: Some(input_tx),
        reaped: false,
    };
    let (tx, rx) = mpsc::sync_channel(8); // bounded borrowed chunks in addition to ingress budget
    let errors = tx.clone();
    thread::spawn(move || {
        while let Ok(command) = input_rx.recv() {
            if let Err(error) = send(&mut stdin, command) {
                let _ = errors.send(Pipe::Error(error));
                break;
            }
        }
    });
    reader(stdout, true, tx.clone());
    reader(stderr, false, tx);
    let mut ingress = Inbound::new(
        1,
        IngressLimits {
            frame_bytes: FRAME_BYTES,
            queued_frames: 256,
            queued_bytes: 4 * FRAME_BYTES,
        },
    )
    .map_err(|e| io::Error::other(format!("{e:?}")))?;
    let mut decoder =
        JsonlDecoder::new(FRAME_BYTES).map_err(|e| io::Error::other(format!("{e:?}")))?;
    let mut turn = Turn::with_policy(crate::tool_mode::Expected {
        enabled: options.tools_enabled,
        profile,
        workspace,
    });
    turn.allow_engine_management = options.launch.engine_management;
    let mut management = crate::management::Management::new(options.resume.clone());
    let mut startup_facts = None;
    let mut mirror_deadline = None;
    let mut work_deadline = None;
    let mut execution_deadline = None;
    let mut execution_stop_deadline = None;
    let mut runtime_controls_deadline = None;
    let mut runtime_control_failure = None;
    let mut schedule_deadline = None;
    let mut management_deadline = None;
    let mut work_retirement_deadlines = std::collections::HashMap::<String, Instant>::new();
    let mut tail = Vec::new();
    let mut deadline = Some(Instant::now() + options.timeout);
    let result = (|| -> io::Result<()> {
        loop {
            check_deadline(deadline)?;
            if turn.closing {
                schedule_deadline = None;
            } else if management.schedule_pending() {
                schedule_deadline.get_or_insert_with(|| Instant::now() + options.timeout);
            }
            check_deadline(schedule_deadline)?;
            if management.work_pending() {
                check_deadline(work_deadline)?;
            } else {
                work_deadline = None;
            }
            if turn.closing || !management.execution_launch_pending() {
                execution_deadline = None;
            }
            if turn.closing || !management.execution_stop_pending() {
                execution_stop_deadline = None;
            }
            check_deadline(execution_deadline)?;
            check_deadline(execution_stop_deadline)?;
            if turn.closing || !management.runtime_controls_pending() {
                runtime_controls_deadline = None;
            }
            check_deadline(runtime_controls_deadline)?;
            check_mirror_deadline(mirror_deadline)?;
            // An autonomous source turn can overlap an already-dispatched read.
            // Neither completion may erase the other's timeout. Reviewed action
            // workers retain their separately renewed foreground deadline.
            if turn.closing || turn.rewrite_active || management.ready() {
                management_deadline = None;
            } else {
                let until = management_deadline.get_or_insert_with(|| {
                    Instant::now() + management.engine_timeout().unwrap_or(options.timeout)
                });
                check_deadline(Some(*until))?;
            }
            if !turn.closing {
                let waiting = turn.work_waits();
                work_retirement_deadlines.retain(|id, _| waiting.contains(id));
                for id in waiting {
                    work_retirement_deadlines
                        .entry(id)
                        .or_insert_with(|| Instant::now() + options.timeout);
                }
                for deadline in work_retirement_deadlines.values() {
                    check_deadline(Some(*deadline))?;
                }
            }
            if !turn.closing
                && let Some(command) = frontend.command()?
            {
                match command {
                    Input::ScheduleDecision {
                        proposal_id,
                        accept,
                    } => {
                        let value =
                            management.decide_schedule(&proposal_id, accept, turn.can_manage())?;
                        owned
                            .stdin
                            .as_ref()
                            .expect("owned stdin")
                            .try_send(value)
                            .map_err(io::Error::other)?;
                    }
                    Input::RuntimeControls(intent) => {
                        let command = if turn.can_runtime_controls() {
                            management.request_runtime_controls(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the pending approval or owned action before changing runtime controls",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                runtime_controls_deadline
                                    .get_or_insert_with(|| Instant::now() + options.timeout);
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                            }
                            Err(error) => frontend.observe(Observation::RuntimeControls(
                                crate::runtime_controls_view::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    Input::Engine(intent) => {
                        let command = if turn.can_engine() {
                            management.request_engine(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish current work before engine management",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(
                                    Instant::now()
                                        + management.engine_timeout().unwrap_or(options.timeout),
                                );
                            }
                            Err(error) => frontend.observe(Observation::Engine(
                                crate::engine_controls::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    Input::Commands(intent) => {
                        let command = if turn.can_manage() {
                            management.request_command(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before using commands",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Commands(
                                crate::command_palette::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    Input::Rewrite(intent) => {
                        let intent = *intent;
                        let key = intent.key().clone();
                        if matches!(intent, crate::conversation_rewrite::Intent::Apply { .. }) {
                            let prepared = if turn.can_manage() {
                                management.prepare_rewrite(&intent)
                            } else {
                                Err(io::Error::other(
                                    "Finish the current turn before edit/retry",
                                ))
                            };
                            match prepared {
                                Ok(prepared) => {
                                    let operation = turn.submit_rewrite(&prepared.review)?;
                                    let command = management.begin_rewrite(prepared, &operation)?;
                                    turn.rewrite_shutdown = Some(command.clone());
                                    turn.rewrite_current_session =
                                        Some(key.selection.identity.session.clone());
                                    owned
                                        .stdin
                                        .as_ref()
                                        .expect("owned stdin")
                                        .try_send(command)
                                        .map_err(io::Error::other)?;
                                    frontend.observe(Observation::Rewrite(
                                        crate::conversation_rewrite::Update::Submitted { key },
                                    ))?;
                                    frontend.observe(Observation::Submission { id: operation })?;
                                    deadline = Some(Instant::now() + options.timeout);
                                }
                                Err(error) => frontend.observe(Observation::Rewrite(
                                    crate::conversation_rewrite::Update::Rejected {
                                        key,
                                        reason: error.to_string(),
                                    },
                                ))?,
                            }
                        } else {
                            let command = if turn.can_manage() {
                                management.request_rewrite(intent)
                            } else {
                                Err(io::Error::other(
                                    "Finish current work before reviewing edit/retry",
                                ))
                            };
                            match command {
                                Ok(command) => {
                                    turn.rewrite_current_session =
                                        Some(key.selection.identity.session.clone());
                                    owned
                                        .stdin
                                        .as_ref()
                                        .expect("owned stdin")
                                        .try_send(command)
                                        .map_err(io::Error::other)?;
                                    deadline = Some(Instant::now() + options.timeout);
                                }
                                Err(error) => frontend.observe(Observation::Rewrite(
                                    crate::conversation_rewrite::Update::Rejected {
                                        key,
                                        reason: error.to_string(),
                                    },
                                ))?,
                            }
                        }
                    }
                    Input::Compact(intent) => {
                        let intent = *intent;
                        let key = intent.key().clone();
                        if matches!(intent, crate::conversation_compact::Intent::Apply { .. }) {
                            let prepared = if turn.can_manage() {
                                management.prepare_compact(&intent)
                            } else {
                                Err(io::Error::other(
                                    "Finish the current turn before compaction",
                                ))
                            };
                            match prepared {
                                Ok(prepared) => {
                                    let operation = turn.submit_compact(&prepared.review)?;
                                    let command = management.begin_compact(prepared, &operation)?;
                                    turn.compact_shutdown = Some(command.clone());
                                    turn.compact_current_session =
                                        Some(key.selection.identity.session.clone());
                                    owned
                                        .stdin
                                        .as_ref()
                                        .expect("owned stdin")
                                        .try_send(command)
                                        .map_err(io::Error::other)?;
                                    frontend.observe(Observation::Compact(
                                        crate::conversation_compact::Update::Submitted { key },
                                    ))?;
                                    frontend.observe(Observation::Submission { id: operation })?;
                                    deadline = Some(Instant::now() + options.timeout);
                                }
                                Err(error) => frontend.observe(Observation::Compact(
                                    crate::conversation_compact::Update::Rejected {
                                        key,
                                        reason: error.to_string(),
                                    },
                                ))?,
                            }
                        } else {
                            let command = if turn.can_manage() {
                                management.request_compact(intent)
                            } else {
                                Err(io::Error::other(
                                    "Finish current work before reviewing compaction",
                                ))
                            };
                            match command {
                                Ok(command) => {
                                    turn.compact_current_session =
                                        Some(key.selection.identity.session.clone());
                                    owned
                                        .stdin
                                        .as_ref()
                                        .expect("owned stdin")
                                        .try_send(command)
                                        .map_err(io::Error::other)?;
                                    deadline = Some(Instant::now() + options.timeout);
                                }
                                Err(error) => frontend.observe(Observation::Compact(
                                    crate::conversation_compact::Update::Rejected {
                                        key,
                                        reason: error.to_string(),
                                    },
                                ))?,
                            }
                        }
                    }
                    Input::History(intent) => {
                        let key = intent.key().clone();
                        let command = if turn.can_engine() {
                            management.request_history(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish current work before reading Saved History",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::SavedHistory(
                                crate::history_view::Update::Rejected {
                                    key,
                                    reason: error.to_string(),
                                },
                            ))?,
                        }
                    }
                    Input::Theme(intent) => {
                        let reading = matches!(intent, crate::theme_controls::Intent::Read);
                        let command = if turn.can_manage() || reading && turn.can_engine() {
                            management.request_theme(intent)
                        } else {
                            Err(io::Error::other("Finish current work before using themes"))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Theme(
                                crate::theme_controls::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    Input::Work(intent) => {
                        let generation = intent.generation;
                        let launch_action = match &intent.request {
                            crate::work_view::Request::ExecutionApply(review) => {
                                Some(review.action_id.clone())
                            }
                            _ => None,
                        };
                        let permitted = match &intent.request {
                            crate::work_view::Request::Midturn(r) => {
                                turn.ordinary_runtime_prompt
                                    && turn.started
                                    && !turn.settled
                                    && !turn.closing
                                    && turn.terminal.is_none()
                                    && turn.questions.settled()
                                    && turn.approvals.settled()
                                    && turn.aborts.settled()
                                    && turn.stop_requests == 0
                                    && turn.command_id == r.draft().operation
                            }
                            crate::work_view::Request::ManualReview { .. }
                            | crate::work_view::Request::ManualDecision { accept: true, .. } => {
                                turn.can_manage()
                            }
                            crate::work_view::Request::Dictation(_) => turn.can_manage(),
                            crate::work_view::Request::Hook(_)
                            | crate::work_view::Request::HookEdit(_)
                            | crate::work_view::Request::Plugin(_) => turn.can_manage(),
                            crate::work_view::Request::ExecutionValidate(_)
                            | crate::work_view::Request::ExecutionApply(_) => turn.can_manage(),
                            crate::work_view::Request::ControlValidate(_)
                            | crate::work_view::Request::ControlApply(_) => turn.can_manage(),
                            crate::work_view::Request::DeferredValidate
                            | crate::work_view::Request::DeferredApply(_) => {
                                turn.can_reconcile_work()
                            }
                            _ => true,
                        };
                        let queueing =
                            matches!(&intent.request, crate::work_view::Request::Midturn(_));
                        let request = if permitted && queueing {
                            turn.runtime_policy
                                .as_ref()
                                .ok_or_else(|| {
                                    io::Error::other(
                                        "Queued input lacks its captured runtime policy",
                                    )
                                })
                                .and_then(|policy| management.bind_queue_policy(policy))
                                .and_then(|()| management.request_work(intent))
                        } else if permitted {
                            management.request_work(intent)
                        } else if queueing {
                            Err(io::Error::other(
                                "The foreground turn changed or needs attention. The composer draft is still available.",
                            ))
                        } else {
                            Err(io::Error::other(
                                "Finish owned execution before changing durable Work",
                            ))
                        };
                        match request {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                if launch_action.is_some() {
                                    execution_deadline = Some(Instant::now() + options.timeout);
                                } else {
                                    work_deadline = Some(Instant::now() + options.timeout);
                                }
                            }
                            Err(error) => {
                                let update = crate::work_view::Update {
                                    generation,
                                    data: crate::work_view::Data::Rejected(error.to_string()),
                                };
                                if let Some(action_id) = launch_action {
                                    frontend.observe(Observation::WorkLaunchFinished {
                                        action_id,
                                        update,
                                        stopped_operation: None,
                                        stop_verified: true,
                                    })?;
                                } else {
                                    frontend.observe(Observation::Work(update))?;
                                }
                            }
                        }
                    }
                    Input::ExecutionStop(action_id) => {
                        let command = management.request_execution_stop(&action_id)?;
                        owned
                            .stdin
                            .as_ref()
                            .expect("owned stdin")
                            .try_send(command)
                            .map_err(io::Error::other)?;
                        execution_stop_deadline = Some(Instant::now() + Duration::from_secs(20));
                    }
                    Input::Settings(intent) => {
                        let voice = matches!(intent, crate::settings_view::Intent::Voice(_));
                        let presentation =
                            matches!(intent, crate::settings_view::Intent::Presentation(_));
                        let connection =
                            matches!(intent, crate::settings_view::Intent::Connection(_));
                        let agent = matches!(intent, crate::settings_view::Intent::Agent(_));
                        let model_preferences =
                            matches!(intent, crate::settings_view::Intent::ModelPreferences(_));
                        let model_lifecycle =
                            matches!(intent, crate::settings_view::Intent::ModelLifecycle(_));
                        let mcp = matches!(intent, crate::settings_view::Intent::Mcp(_));
                        let runtime_status =
                            matches!(intent, crate::settings_view::Intent::RuntimeStatus);
                        let editing = matches!(intent, crate::settings_view::Intent::Editor(_));
                        let compact_generation = match &intent {
                            crate::settings_view::Intent::Compaction(
                                crate::compaction_settings::Intent::Read { generation }
                                | crate::compaction_settings::Intent::Validate { generation, .. }
                                | crate::compaction_settings::Intent::Save { generation, .. },
                            ) => Some(*generation),
                            _ => None,
                        };
                        let command = if turn.can_manage() {
                            management.request_settings(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before reading settings",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => {
                                frontend.observe(Observation::Settings(if model_lifecycle {
                                    crate::settings_view::Update::ModelLifecycle(Err(
                                        error.to_string()
                                    ))
                                } else if model_preferences {
                                    crate::settings_view::Update::ModelPreferences(Err(
                                        error.to_string()
                                    ))
                                } else if voice {
                                    crate::settings_view::Update::Voice(Err(error.to_string()))
                                } else if presentation {
                                    crate::settings_view::Update::Presentation(Err(
                                        error.to_string()
                                    ))
                                } else if connection {
                                    crate::settings_view::Update::Connection(Err(error.to_string()))
                                } else if agent {
                                    crate::settings_view::Update::Agent(Err(error.to_string()))
                                } else if mcp {
                                    crate::settings_view::Update::Mcp(Err(error.to_string()))
                                } else if runtime_status {
                                    crate::settings_view::Update::RuntimeStatus(Err(
                                        error.to_string()
                                    ))
                                } else if let Some(generation) = compact_generation {
                                    crate::settings_view::Update::Compaction(
                                        crate::compaction_settings::Update::Rejected {
                                            generation,
                                            reason: error.to_string(),
                                        },
                                    )
                                } else if editing {
                                    crate::settings_view::Update::Editor(
                                        crate::settings_editor::Update::Rejected(error.to_string()),
                                    )
                                } else {
                                    crate::settings_view::Update::Rejected(error.to_string())
                                }))?
                            }
                        }
                    }
                    Input::Backend(intent) => {
                        let command = if turn.can_manage() {
                            management.request_backend(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before changing backend",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Backend(
                                crate::backend_controls::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    Input::Browser(intent) => {
                        let action =
                            matches!(intent, crate::conversation_browser::Intent::Action(_));
                        let command = if turn.can_manage() {
                            management.request_browser(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before browsing conversations",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                if value["type"] == crate::conversation_actions::APPLY {
                                    turn.conversation_shutdown = Some(value.clone());
                                } else if value["type"] == crate::history::OPEN {
                                    turn.conversation_shutdown =
                                        Some(crate::history::shutdown_identity(&value)?);
                                }
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Browser(if action {
                                crate::conversation_browser::Update::Action(
                                    crate::conversation_actions::Update::Rejected(
                                        error.to_string(),
                                    ),
                                )
                            } else {
                                crate::conversation_browser::Update::Rejected(error.to_string())
                            }))?,
                        }
                    }
                    Input::Control(intent) => {
                        let command = if turn.can_manage() {
                            management.request_control(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before changing model controls",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Controls(
                                crate::model_controls::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    Input::Queue(crate::restored_queue::Intent::Continue { revision, entry }) => {
                        let prepared = if turn.can_manage() {
                            management.prepare_continuation(revision, &entry)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before continuing held input",
                            ))
                        };
                        match prepared {
                            Ok(prepared) => {
                                let id = turn.submit_continuation(
                                    &prepared.prompt,
                                    &prepared.session,
                                    &prepared.entry,
                                )?;
                                let command = management.begin_continuation(prepared, &id)?;
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(command)
                                    .map_err(io::Error::other)?;
                                frontend.observe(Observation::Submission { id })?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Queue(
                                crate::restored_queue::Update::ContinuationRejected(
                                    error.to_string(),
                                ),
                            ))?,
                        }
                    }
                    Input::Queue(intent) => {
                        // Read/cancel/resolve held input is recovery, not prompt admission.
                        // Continue is handled separately and still requires prompt readiness.
                        let recovery = matches!(
                            intent,
                            crate::restored_queue::Intent::Open
                                | crate::restored_queue::Intent::Reconcile { .. }
                        );
                        let command = if turn.can_engine() || recovery && turn.can_reconcile_work()
                        {
                            management.request_queue(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before changing held input",
                            ))
                        };
                        match command {
                            Ok(value) => {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(value)
                                    .map_err(io::Error::other)?;
                                deadline = Some(Instant::now() + options.timeout);
                            }
                            Err(error) => frontend.observe(Observation::Queue(
                                crate::restored_queue::Update::Rejected(error.to_string()),
                            ))?,
                        }
                    }
                    input @ (Input::Prompt(_) | Input::ImagePrompt(_, _)) => {
                        let (prompt, image) = match input {
                            Input::Prompt(prompt) => (prompt, None),
                            Input::ImagePrompt(prompt, image) => (prompt, Some(image)),
                            _ => unreachable!(),
                        };
                        if management.schedule_pending()
                            || (schedule_deadline.is_some() && !management.ready())
                        {
                            frontend.observe(Observation::ScheduledLocalNotSent {
                                prompt,
                                reason: "Prompt was not sent while a scheduled handoff was being resolved. Its text remains in the conversation; submit it again after the handoff settles.".into(),
                            })?;
                            continue;
                        }
                        if !management.ready() {
                            return Err(io::Error::other("Management startup is not complete"));
                        }
                        if management.work_lifecycle_active()
                            && (turn.work.owns_work() || turn.work.uncertain)
                        {
                            // Source admission may win after the UI queues a draft but
                            // before the supervisor reserves its command identity.
                            // Keep that draft visible and never silently replay it.
                            frontend.observe(Observation::WorkExecution(Box::new(
                                WorkExecution::LocalNotSent {
                                    reason: "Background work acquired the runtime before this prompt was sent. Submit it again after that work settles.".into(),
                                },
                            )))?;
                        } else {
                            let mut value = turn.submit(&prompt)?;
                            if let Some(image) = image {
                                if !turn.ordinary_runtime_prompt {
                                    return Err(io::Error::other(
                                        "Image requires a published runtime policy",
                                    ));
                                }
                                value["native_image"] = image.wire().clone();
                                turn.image_prompt = Some((image, prompt.clone()));
                            }
                            let id = value["id"].as_str().expect("reserved ID").to_owned();
                            management.prompt(&id)?;
                            owned
                                .stdin
                                .as_ref()
                                .expect("owned stdin")
                                .try_send(value)
                                .map_err(io::Error::other)?;
                            frontend.observe(Observation::Submission { id })?;
                            deadline = Some(Instant::now() + options.timeout);
                        }
                    }
                    Input::Abort { prompt_id } => {
                        if management.interrupt_apply_pending() {
                            frontend.observe(Observation::Information(
                                "Waiting for the reviewed interrupt receipt before another Stop"
                                    .into(),
                            ))?;
                        } else if let Some(value) = turn.abort(&prompt_id)? {
                            owned
                                .stdin
                                .as_ref()
                                .expect("owned stdin")
                                .try_send(value)
                                .map_err(io::Error::other)?;
                            let stop_deadline = Instant::now() + Duration::from_secs(20);
                            deadline =
                                Some(deadline.map_or(stop_deadline, |d| d.min(stop_deadline)));
                        }
                    }
                    Input::Approve(decision) => {
                        if let Some(value) = turn.approve(decision)? {
                            owned
                                .stdin
                                .as_ref()
                                .expect("owned stdin")
                                .try_send(value)
                                .map_err(io::Error::other)?;
                        }
                        // No deadline extension, no implicit denial on expiry.
                    }
                    Input::Answer(decision) => {
                        if let Some(value) = turn.answer(decision)? {
                            owned
                                .stdin
                                .as_ref()
                                .expect("owned stdin")
                                .try_send(value)
                                .map_err(io::Error::other)?;
                        }
                    }
                    Input::Quit => {
                        management.begin_close();
                        turn.begin_close();
                        deadline = Some(Instant::now() + Duration::from_secs(20));
                        owned.request_shutdown()?;
                    }
                }
            }
            let chunk = match rx.recv_timeout(Duration::from_millis(25)) {
                Ok(chunk) => chunk,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(error) => return Err(io::Error::other(error)),
            };
            check_deadline(deadline)?;
            check_mirror_deadline(mirror_deadline)?;
            let ingress_result = match chunk {
                Pipe::Stdout(bytes) => ingress.push(1, &bytes),
                Pipe::Stderr(bytes) => {
                    tail.extend(bytes);
                    if tail.len() > 65536 {
                        tail.drain(..tail.len() - 65536);
                    }
                    continue;
                }
                Pipe::End(false) => continue,
                Pipe::End(true) => {
                    management.ensure_drained()?;
                    ingress
                        .finish(1)
                        .map_err(|e| io::Error::other(format!("{e:?}")))?;
                    decoder
                        .finish()
                        .map_err(|e| io::Error::other(format!("{e:?}")))?;
                    return if turn.closing {
                        if (turn.engine_shutdown.is_some()
                            || turn.conversation_shutdown.is_some()
                            || turn.rewrite_current_session.is_some()
                            || turn.compact_current_session.is_some()
                            || management.work_lifecycle_active())
                            && !turn.shutdown_acknowledged
                        {
                            Err(io::Error::other(
                                "Owned runtime exited without verified shutdown acknowledgement",
                            ))
                        } else if let Some(failure) = runtime_control_failure.take() {
                            Err(io::Error::other(failure))
                        } else {
                            Ok(())
                        }
                    } else {
                        Err(io::Error::other("Runtime stdout closed unexpectedly"))
                    };
                }
                Pipe::Error(error) => return Err(error),
            };
            while let Some(frame) = ingress.pop_frame() {
                let mut value = None;
                decoder
                    .push(&frame, |v| value = Some(v))
                    .map_err(|e| io::Error::other(format!("{e:?}")))?;
                let event = value.expect("complete nonblank frame");
                check_mirror_deadline(mirror_deadline)?;
                use crate::management::Step;
                let authority = turn.work_current(management.known_work_origin());
                let step = management.accept_wire_with_work(
                    event,
                    crate::history::wire_response_bytes(&frame),
                    authority.as_ref(),
                )?;
                if let Some(policy) = management.take_runtime_policy() {
                    turn.install_runtime_policy(policy.clone())?;
                    frontend.observe(Observation::RuntimePolicy(policy))?;
                }
                match step {
                    Step::RuntimeControls(update) => {
                        check_deadline(runtime_controls_deadline)?;
                        match &update {
                            crate::runtime_controls_view::Update::Snapshot { snapshot, .. } => {
                                turn.verify_runtime_owner(
                                    &snapshot.owner,
                                    management.known_work_origin(),
                                    false,
                                )?;
                            }
                            crate::runtime_controls_view::Update::Review(review) => {
                                turn.verify_runtime_owner(
                                    &review.owner_before,
                                    management.known_work_origin(),
                                    false,
                                )?;
                            }
                            crate::runtime_controls_view::Update::Settled(readback) => {
                                let verified = turn
                                    .verify_runtime_readback(
                                        readback,
                                        management.runtime_control_receipt(readback),
                                        management.known_work_origin(),
                                    )
                                    .and_then(|()| management.install_runtime_readback(readback));
                                if let Err(error) = verified {
                                    runtime_control_failure = Some(error.to_string());
                                    if !turn.closing {
                                        frontend.observe(Observation::RuntimeControls(crate::runtime_controls_view::Update::Rejected(format!("Could not verify the change: {error}. Closing this runtime."))))?;
                                        management.begin_close();
                                        turn.begin_close();
                                        deadline = Some(Instant::now() + Duration::from_secs(20));
                                        owned.request_shutdown()?;
                                    }
                                    continue;
                                }
                                if let Some(command) = management.take_deferred_work_activation()? {
                                    owned
                                        .stdin
                                        .as_ref()
                                        .expect("owned stdin")
                                        .try_send(command)
                                        .map_err(io::Error::other)?;
                                }
                                if let Some(policy) = management.take_runtime_policy() {
                                    turn.install_runtime_policy(policy.clone())?;
                                    frontend.observe(Observation::RuntimePolicy(policy))?;
                                }
                            }
                            _ => {}
                        }
                        runtime_controls_deadline = None;
                        if !turn.closing {
                            frontend.observe(Observation::RuntimeControls(update))?;
                        }
                    }
                    Step::SavedHistory(update) => {
                        if !turn.closing {
                            frontend.observe(Observation::SavedHistory(update))?;
                            deadline = None;
                        }
                    }
                    Step::Bootstrap(ready) => {
                        OwnerStore::verify(options.owner.as_ref(), &ready, &owned)?;
                        require_work_lifecycle(&ready)?;
                        turn.accept(&ready, &options.backend, &options.model)?;
                        crate::history::require_projection(&ready)?;
                        startup_facts = if turn.can_manage() {
                            crate::runtime_state::Update::from_event(&ready)?
                        } else {
                            None
                        };
                        if !turn.closing {
                            management.enable_work_lifecycle(profile)?;
                            let hello = management.begin(&ready)?;
                            owned
                                .stdin
                                .as_ref()
                                .expect("owned stdin")
                                .try_send(hello)
                                .map_err(io::Error::other)?;
                            frontend.observe(Observation::Information(
                                "Checking runtime state…".into(),
                            ))?;
                        }
                    }
                    Step::Write(command) => {
                        if !turn.closing {
                            if let Some(identity) = management.scheduled_shutdown_identity(&command)
                            {
                                turn.conversation_shutdown = Some(identity);
                            }
                            owned
                                .stdin
                                .as_ref()
                                .expect("owned stdin")
                                .try_send(command)
                                .map_err(io::Error::other)?;
                        }
                    }
                    Step::Initialized(snapshot) => {
                        if !turn.closing {
                            frontend.observe(Observation::Theme(
                                crate::theme_controls::Update::Rejected(
                                    "Source theme catalog unavailable; using local fallback."
                                        .into(),
                                ),
                            ))?;
                            publish_startup(
                                frontend,
                                &mut startup_facts,
                                snapshot,
                                None,
                                &mut deadline,
                                turn.engine_admission,
                            )?;
                        }
                    }
                    Step::Restored { snapshot, history } => {
                        if !turn.closing {
                            frontend.observe(Observation::Theme(
                                crate::theme_controls::Update::Rejected(
                                    "Source theme catalog unavailable; using local fallback."
                                        .into(),
                                ),
                            ))?;
                            publish_startup(
                                frontend,
                                &mut startup_facts,
                                snapshot,
                                Some(history),
                                &mut deadline,
                                turn.engine_admission,
                            )?;
                        }
                    }
                    Step::Engine {
                        update,
                        policy,
                        state,
                    } => {
                        if !turn.closing {
                            if let Some(policy) = policy {
                                turn.validate_current_policy(&policy)?;
                            }
                            if let crate::engine_controls::Update::Settled { admission, .. } =
                                &update
                            {
                                turn.engine_admission = Some(*admission);
                                frontend.observe(Observation::EngineAdmission(*admission))?;
                            }
                            if let Some(state) = state {
                                frontend.observe(Observation::State(
                                    crate::runtime_state::Update::from_management(state),
                                ))?;
                            }
                            frontend.observe(Observation::Engine(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Commands { update, policy } => {
                        if !turn.closing {
                            if let Some(policy) = policy {
                                turn.validate_current_policy(&policy)?;
                            }
                            frontend.observe(Observation::Commands(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Theme {
                        update,
                        policy,
                        startup,
                    } => {
                        if !turn.closing {
                            if let Some(policy) = policy {
                                turn.validate_current_policy(&policy)?;
                            }
                            frontend.observe(Observation::Theme(update))?;
                            if let Some((snapshot, history)) = startup {
                                publish_startup(
                                    frontend,
                                    &mut startup_facts,
                                    snapshot,
                                    history,
                                    &mut deadline,
                                    turn.engine_admission,
                                )?;
                            } else {
                                check_deadline(deadline)?;
                                deadline = None;
                            }
                        }
                    }
                    Step::ScheduledProposal(proposal_id) => {
                        if !turn.closing {
                            schedule_deadline = Some(Instant::now() + options.timeout);
                            if frontend.scheduled_handoff() {
                                frontend.observe(Observation::ScheduledProposal(proposal_id))?;
                            } else {
                                let command = management.decide_schedule(
                                    &proposal_id,
                                    false,
                                    turn.can_manage(),
                                )?;
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(command)
                                    .map_err(io::Error::other)?;
                            }
                        }
                    }
                    Step::ScheduledHeld {
                        reason,
                        shutdown_identity,
                    } => {
                        schedule_deadline = None;
                        turn.retire_scheduled_refusal(shutdown_identity.as_ref());
                        if !turn.closing {
                            frontend.observe(Observation::ScheduledHeld(reason))?;
                        }
                    }
                    Step::ScheduledConversationChanged(changed) => {
                        schedule_deadline = None;
                        if turn.rewrite_current_session.is_some() {
                            turn.rewrite_current_session = Some(changed.snapshot.session.clone());
                        }
                        if turn.compact_current_session.is_some() {
                            turn.compact_current_session = Some(changed.snapshot.session.clone());
                        }
                        if !turn.closing {
                            turn.validate_current_policy(&changed.policy)?;
                            turn.conversation_completed_shutdown =
                                turn.conversation_shutdown.clone();
                            frontend.observe(Observation::ScheduledConversationChanged(changed))?;
                        }
                    }
                    Step::WorkLifecycle(effect) => {
                        let starts = matches!(effect.as_ref(),crate::work_lifecycle::Effect::Admission{admission,..}
                            if admission.mode==crate::work_lifecycle::Mode::NewTurn);
                        check_deadline(deadline)?;
                        let effective = match effect.as_ref() {
                            crate::work_lifecycle::Effect::Admission {
                                admission,
                                drain_only: false,
                                ..
                            } => Some(admission.effective),
                            _ => None,
                        };
                        let updates = turn.accept_work(*effect)?;
                        if let Some(policy) =
                            effective.and_then(|profile| management.runtime_effective(profile))
                        {
                            turn.runtime_policy = Some(policy.clone());
                            if !turn.closing {
                                frontend.observe(Observation::RuntimePolicy(policy))?;
                            }
                        }
                        turn.work_observed_deadline(&updates, &mut deadline);
                        for update in updates {
                            frontend.observe(update)?;
                        }
                        match management.execution_after_work()? {
                            Step::WorkLaunch {
                                action_id,
                                update,
                                stop: None,
                                stop_verified: false,
                            } if !turn.closing => {
                                check_deadline(execution_deadline)?;
                                execution_deadline = None;
                                frontend.observe(Observation::WorkLaunchFinished {
                                    action_id,
                                    update,
                                    stopped_operation: None,
                                    stop_verified: false,
                                })?;
                            }
                            Step::Ignore => {}
                            _ => {
                                return Err(io::Error::other(
                                    "Unexpected execution completion during Work event",
                                ));
                            }
                        }
                        if starts && !turn.closing {
                            deadline = Some(Instant::now() + options.timeout);
                        }
                    }
                    Step::WorkLifecycleVerified { admission_id } => {
                        check_deadline(deadline)?;
                        let updates = turn.verify_work(&admission_id)?;
                        turn.work_observed_deadline(&updates, &mut deadline);
                        for update in updates {
                            if !turn.closing {
                                frontend.observe(update)?;
                            }
                        }
                    }
                    Step::WorkLaunchStop {
                        action_id,
                        generation,
                        admission,
                    } => {
                        check_deadline(execution_stop_deadline)?;
                        if turn.closing || !management.runtime_controls_pending() {
                            runtime_controls_deadline = None;
                        }
                        check_deadline(runtime_controls_deadline)?;
                        execution_stop_deadline = None;
                        if !turn.closing {
                            let mut stopped_operation = None;
                            if let Some(command) = turn.abort(&admission.operation)? {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(command)
                                    .map_err(io::Error::other)?;
                                stopped_operation = Some(admission.operation.clone());
                                let until = Instant::now() + Duration::from_secs(20);
                                deadline = Some(deadline.map_or(until, |d| d.min(until)));
                            }
                            frontend.observe(Observation::WorkLaunchStop {
                                action_id,
                                generation,
                                stopped_operation,
                            })?;
                        }
                    }
                    Step::WorkLaunchPending {
                        action_id,
                        generation,
                        reason,
                    } => {
                        if !turn.closing {
                            frontend.observe(Observation::WorkLaunchPending {
                                action_id,
                                generation,
                                reason,
                            })?;
                        }
                    }
                    Step::WorkLaunch {
                        action_id,
                        update,
                        stop,
                        stop_verified,
                    } => {
                        check_deadline(execution_deadline)?;
                        check_deadline(execution_stop_deadline)?;
                        if turn.closing || !management.runtime_controls_pending() {
                            runtime_controls_deadline = None;
                        }
                        check_deadline(runtime_controls_deadline)?;
                        execution_deadline = None;
                        execution_stop_deadline = None;
                        if !turn.closing {
                            let mut stopped_operation = None;
                            if let Some(admission) = stop
                                && let Some(command) = turn.abort(&admission.operation)?
                            {
                                owned
                                    .stdin
                                    .as_ref()
                                    .expect("owned stdin")
                                    .try_send(command)
                                    .map_err(io::Error::other)?;
                                stopped_operation = Some(admission.operation);
                                let until = Instant::now() + Duration::from_secs(20);
                                deadline = Some(deadline.map_or(until, |d| d.min(until)));
                            }
                            frontend.observe(Observation::WorkLaunchFinished {
                                action_id,
                                update,
                                stopped_operation,
                                stop_verified,
                            })?;
                        }
                    }
                    Step::Work(update) => {
                        if update.interrupted().is_some_and(|id| id == turn.command_id)
                            && turn.terminal.is_none()
                            && !turn.settled
                        {
                            turn.stop_requests = turn.stop_requests.max(1);
                            turn.stop_acknowledged = true;
                        }
                        check_deadline(work_deadline)?;
                        work_deadline = None;
                        if !turn.closing {
                            frontend.observe(Observation::Work(update))?;
                        }
                    }
                    Step::Settings { update, policy } => {
                        if !turn.closing {
                            if let Some(policy) = policy {
                                turn.validate_current_policy(&policy)?;
                            }
                            frontend.observe(Observation::Settings(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Backend(update) => {
                        if !turn.closing {
                            frontend.observe(Observation::Backend(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::BackendChanged(opened) => {
                        if turn.rewrite_current_session.is_some() {
                            turn.rewrite_current_session = Some(opened.snapshot.session.clone());
                        }
                        if turn.compact_current_session.is_some() {
                            turn.compact_current_session = Some(opened.snapshot.session.clone());
                        }
                        if !turn.closing {
                            turn.validate_current_policy(&opened.policy)?;
                            frontend.observe(Observation::BackendChanged(opened))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::ConversationAction { update, policy } => {
                        if !turn.closing {
                            if let Some(policy) = policy {
                                turn.validate_current_policy(&policy)?;
                            }
                            frontend.observe(Observation::Browser(
                                crate::conversation_browser::Update::Action(update),
                            ))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::ConversationChanged(changed) => {
                        if turn.rewrite_current_session.is_some() {
                            turn.rewrite_current_session = Some(changed.snapshot.session.clone());
                        }
                        if turn.compact_current_session.is_some() {
                            turn.compact_current_session = Some(changed.snapshot.session.clone());
                        }
                        if !turn.closing {
                            turn.conversation_completed_shutdown =
                                turn.conversation_shutdown.clone();
                            turn.validate_current_policy(&changed.policy)?;
                            frontend.observe(Observation::ConversationChanged(changed))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Browser(update) => {
                        if !turn.closing {
                            frontend.observe(Observation::Browser(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Opened(opened) => {
                        if turn.rewrite_current_session.is_some() {
                            turn.rewrite_current_session = Some(opened.snapshot.session.clone());
                        }
                        if turn.compact_current_session.is_some() {
                            turn.compact_current_session = Some(opened.snapshot.session.clone());
                        }
                        if !turn.closing {
                            turn.conversation_completed_shutdown =
                                turn.conversation_shutdown.clone();
                            turn.validate_current_policy(&opened.policy)?;
                            frontend.observe(Observation::Opened(opened))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Control(update) => {
                        if !turn.closing {
                            if let crate::model_controls::Update::Choices(menu) = &update {
                                frontend.observe(Observation::State(
                                    crate::runtime_state::Update::from_choices(menu),
                                ))?;
                            }
                            frontend.observe(Observation::Controls(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Queue(update) => {
                        if !turn.closing {
                            if let crate::restored_queue::Update::Snapshot(menu) = &update {
                                frontend.observe(Observation::State(
                                    crate::runtime_state::Update::from_queue(menu),
                                ))?;
                            }
                            frontend.observe(Observation::Queue(update))?;
                            if let Some(effect) = management.take_verified_work_reconciliation() {
                                for observation in turn.accept_work(effect)? {
                                    frontend.observe(observation)?;
                                }
                            }
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Rewrite(update) => {
                        if !turn.closing {
                            frontend.observe(Observation::Rewrite(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::RewriteSettled(changed) => {
                        if !turn.closing {
                            turn.validate_current_policy(&changed.policy)?;
                            turn.finish_rewrite()?;
                            frontend.observe(Observation::RewriteSettled(changed))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Compact(update) => {
                        if !turn.closing {
                            frontend.observe(Observation::Compact(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::CompactSettled(changed) => {
                        if !turn.closing {
                            turn.validate_current_policy(&changed.policy)?;
                            turn.finish_rewrite()?;
                            frontend.observe(Observation::CompactSettled(changed))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Event(event) => {
                        if turn.closing
                            && event["type"] == "response"
                            && event["id"] == SHUTDOWN_ID
                            && event["ok"] == true
                        {
                            management.verify_work_shutdown(&event["result"]["work_shutdown"])?;
                            management.verify_execution_shutdown(
                                &event["result"]["work_execution_shutdown"],
                            )?;
                            management.verify_runtime_shutdown(&event["result"])?;
                        }
                        let action_segment = turn.rewrite_active && event["type"] == "turn_start";
                        if let Some(id) = publish_event(
                            &mut turn,
                            &event,
                            management.backend().unwrap_or(&options.backend),
                            management.model().unwrap_or(&options.model),
                            &mut deadline,
                            frontend,
                        )? {
                            management.acknowledge_rewrite_question(&id);
                            management.acknowledge_compact_question(&id);
                        }
                        // A negotiated attended prompt supplies no profile override. Its
                        // actual ordinary stream start is the canonical admission at seat.
                        if !turn.closing
                            && !action_segment
                            && turn.ordinary_runtime_prompt
                            && event["type"] == "turn_start"
                            && let Some(seat) =
                                turn.runtime_policy.as_ref().map(|p| p.facts.seat_profile)
                            && let Some(policy) = management.runtime_effective(seat)
                        {
                            turn.runtime_policy = Some(policy.clone());
                            frontend.observe(Observation::RuntimePolicy(policy))?;
                        }
                        if action_segment && !turn.closing {
                            deadline = Some(Instant::now() + options.timeout);
                        }
                    }
                    Step::Ignore => {}
                }
                if !turn.closing
                    && turn.settled
                    && !management.has_unpaired()
                    && management.needs_continuation_readback()
                {
                    // A scheduled worker may refuse before starting after its
                    // admission was accepted. Preserve the admission receipt.
                    let command = management.read_continuation(turn.admission_refused)?;
                    owned
                        .stdin
                        .as_ref()
                        .expect("owned stdin")
                        .try_send(command)
                        .map_err(io::Error::other)?;
                    deadline = Some(Instant::now() + options.timeout);
                }
                if !turn.closing
                    && turn.rewrite_readback_ready()
                    && !management.has_unpaired()
                    && management.needs_rewrite_readback()
                {
                    let admission = turn
                        .rewrite_admission
                        .clone()
                        .ok_or_else(|| io::Error::other("Missing edit/retry admission receipt"))?;
                    let command = management.read_rewrite(admission)?;
                    owned
                        .stdin
                        .as_ref()
                        .expect("owned stdin")
                        .try_send(command)
                        .map_err(io::Error::other)?;
                    deadline = Some(Instant::now() + options.timeout);
                }
                if !turn.closing
                    && turn.rewrite_readback_ready()
                    && !management.has_unpaired()
                    && management.needs_compact_readback()
                {
                    let admission = turn
                        .compact_admission
                        .clone()
                        .ok_or_else(|| io::Error::other("Missing compaction admission receipt"))?;
                    let command = management.read_compact(admission)?;
                    owned
                        .stdin
                        .as_ref()
                        .expect("owned stdin")
                        .try_send(command)
                        .map_err(io::Error::other)?;
                    deadline = Some(Instant::now() + options.timeout);
                }
                if !turn.closing
                    && !management.has_unpaired()
                    && !management.work_lifecycle_pending()
                    && let Some(admission) = turn.take_work_receipt()
                {
                    let command = management.request_work_settled(&admission)?;
                    owned
                        .stdin
                        .as_ref()
                        .expect("owned stdin")
                        .try_send(command)
                        .map_err(io::Error::other)?;
                }
                if management.has_unpaired() {
                    mirror_deadline.get_or_insert_with(|| {
                        Instant::now() + options.timeout.min(Duration::from_secs(20))
                    });
                } else {
                    mirror_deadline = None;
                }
            }
            // Never admit another command after a fault, including when an
            // accepted prefix contains Ready or Complete. Drain prefix first.
            ingress_result.map_err(|e| io::Error::other(format!("{e:?}")))?;
        }
    })();
    // Failure cleanup still drains both pipes to avoid deadlocking shutdown.
    // Success reaches here only after clean, fully validated stdout EOF.
    let (tail_tx, tail_rx) = mpsc::channel();
    thread::spawn(move || {
        while let Ok(chunk) = rx.recv() {
            if let Pipe::Stderr(bytes) = chunk {
                tail.extend(bytes);
                if tail.len() > 65536 {
                    tail.drain(..tail.len() - 65536);
                }
            }
        }
        let _ = tail_tx.send(tail);
    });
    let cleanup = owned.close();
    let tail = tail_rx
        .recv_timeout(Duration::from_secs(2))
        .map_err(|error| {
            io::Error::other(format!(
                "live={result:?}; cleanup={cleanup:?}; runtime pipes did not settle: {error}"
            ))
        })?;
    match (result, cleanup) {
        (Ok(()), Ok(())) => Ok(()),
        (result, cleanup) => Err(io::Error::other(format!(
            "live={result:?}; cleanup={cleanup:?}\n{}",
            terminal_text(&String::from_utf8_lossy(&tail))
        ))),
    }
}

pub enum Input {
    RuntimeControls(crate::runtime_controls_view::Intent),
    ScheduleDecision { proposal_id: String, accept: bool },
    Compact(Box<crate::conversation_compact::Intent>),
    Rewrite(Box<crate::conversation_rewrite::Intent>),
    History(crate::history_view::Intent),
    Theme(crate::theme_controls::Intent),
    Engine(crate::engine_controls::Intent),
    Control(crate::model_controls::Intent),
    Queue(crate::restored_queue::Intent),
    Commands(crate::command_palette::Intent),
    Work(crate::work_view::Intent),
    ExecutionStop(String),
    Settings(crate::settings_view::Intent),
    Backend(crate::backend_controls::Intent),
    Browser(crate::conversation_browser::Intent),
    ImagePrompt(String, crate::image::Review),
    Prompt(String),
    Abort { prompt_id: String },
    Approve(crate::approvals::Decision),
    Answer(crate::questions::Decision),
    Quit,
}
trait Frontend {
    fn scheduled_handoff(&self) -> bool {
        false
    }
    fn command(&mut self) -> io::Result<Option<Input>>;
    fn observe(&mut self, observation: Observation) -> io::Result<()>;
}
struct OneShot {
    output: Output,
    prompt: Option<String>,
    next: Option<Input>,
}
impl Frontend for OneShot {
    fn command(&mut self) -> io::Result<Option<Input>> {
        Ok(self.next.take())
    }
    fn observe(&mut self, observation: Observation) -> io::Result<()> {
        match observation {
            Observation::RuntimeControls(_)
            | Observation::RuntimePolicy(_)
            | Observation::ScheduledProposal(_)
            | Observation::ScheduledHeld(_)
            | Observation::ScheduledLocalNotSent { .. }
            | Observation::ScheduledConversationChanged(_)
            | Observation::Compact(_)
            | Observation::CompactSettled(_)
            | Observation::Rewrite(_)
            | Observation::RewriteSettled(_)
            | Observation::RewriteSegment { .. }
            | Observation::RewriteAftercare { .. }
            | Observation::RewriteActivity { .. }
            | Observation::RewriteStatus { .. }
            | Observation::Engine(_)
            | Observation::EngineAdmission(_)
            | Observation::State(_)
            | Observation::Commands(_)
            | Observation::Theme(_)
            | Observation::SavedHistory(_)
            | Observation::WorkExecution(_)
            | Observation::WorkLaunchStop { .. }
            | Observation::WorkLaunchPending { .. }
            | Observation::WorkLaunchFinished { .. }
            | Observation::Work(_)
            | Observation::Settings(_)
            | Observation::Backend(_)
            | Observation::BackendChanged(_)
            | Observation::Browser(_)
            | Observation::Opened(_)
            | Observation::ConversationChanged(_)
            | Observation::Controls(_)
            | Observation::Queue(_)
            | Observation::Submission { .. } => {} // Read-only footer facts; no one-shot action.
            Observation::History(history) => {
                if let Some(descriptor) = &history.descriptor {
                    writeln!(
                        self.output,
                        "Saved context verified: {} messages; bounded display available in interactive Saved History.",
                        descriptor.counts.messages
                    )?;
                }
                writeln!(
                    self.output,
                    "Restored conversation {}",
                    terminal_text(&history.session)
                )?;
                for message in history.messages {
                    writeln!(
                        self.output,
                        "Saved {}: {}",
                        terminal_text(&message.role),
                        terminal_text(&message.text)
                    )?;
                    if !message.details.is_empty() {
                        writeln!(
                            self.output,
                            "Saved details: {}",
                            terminal_text(&message.details)
                        )?;
                    }
                }
            }
            Observation::Approval(_)
            | Observation::ApprovalSettled { .. }
            | Observation::Question { .. } => {
                return Err(io::Error::other(
                    "One-shot frontend cannot answer approval or question; use interactive (no answer sent)",
                ));
            }
            Observation::Content { card, kind, text } => writeln!(
                self.output,
                "\n[Card {card} {kind:?}] {}",
                terminal_text(&text)
            )?,
            Observation::Activity(activity) => writeln!(
                self.output,
                "\n[Native display activity] {}",
                terminal_text(&format!("{activity:?}"))
            )?,
            Observation::Ready => {
                let prompt = self
                    .prompt
                    .take()
                    .ok_or_else(|| io::Error::other("Repeated ready"))?;
                writeln!(self.output, "Ready. Prompt: {}", terminal_text(&prompt))?;
                self.next = Some(Input::Prompt(prompt));
            }
            Observation::Text(text) => write!(self.output, "{}", terminal_text(&text))?,
            Observation::Reasoning(text) => {
                writeln!(self.output, "\n[Thinking] {}", terminal_text(&text))?
            }
            Observation::ReasoningSnapshot(text) => writeln!(
                self.output,
                "\n[Thinking snapshot replaces current trace]\n{}",
                terminal_text(&text)
            )?,
            Observation::Snapshot(text) => writeln!(
                self.output,
                "\n[Answer snapshot replaces current answer]\n{}",
                terminal_text(&text)
            )?,
            Observation::Information(kind) | Observation::ConversationUnverified(kind) => {
                writeln!(self.output, "\n[Runtime: {}]", terminal_text(&kind))?
            }
            Observation::Cancelled(reason) => {
                return Err(io::Error::other(format!(
                    "Turn cancelled: {}",
                    terminal_text(&reason)
                )));
            }
            Observation::ApprovalStopped(reason) => {
                // A settled refusal keeps interactive chat alive, but a one-shot
                // command must still return failure rather than claim success.
                return Err(io::Error::other(format!(
                    "Turn stopped for approval: {}",
                    terminal_text(&reason)
                )));
            }
            Observation::Failed(reason) => {
                return Err(io::Error::other(format!(
                    "Turn failed: {}",
                    terminal_text(&reason)
                )));
            }
            Observation::ImageRefused { reason, .. } | Observation::Refused(reason) => {
                return Err(io::Error::other(format!(
                    "Input not started: {}",
                    terminal_text(&reason)
                )));
            }
            Observation::Complete => {
                writeln!(self.output, "\n[Turn ended: stop]")?;
                self.next = Some(Input::Quit);
            }
        }
        Ok(())
    }
}
pub fn run(options: Options) -> io::Result<()> {
    let profile = options.selected_profile()?;
    if options.prompt.trim().is_empty() {
        return Err(io::Error::other("--live requires --prompt"));
    }
    if options.launch.engine_management {
        return Err(io::Error::other("Engine management requires --interactive"));
    }
    let mut frontend = OneShot {
        output: Output::new(),
        prompt: Some(options.prompt.clone()),
        next: None,
    };
    writeln!(
        frontend.output,
        "LiteTUI Rust LIVE — one prompt, legacy RPC\nBackend: {} / {}\nData: {}",
        terminal_text(&options.backend),
        terminal_text(&options.model),
        terminal_text(&options.data_root.to_string_lossy())
    )?;
    writeln!(
        frontend.output,
        "Requested tools: {} · profile: {}",
        if options.tools_enabled { "on" } else { "off" },
        profile.as_str()
    )?;
    drive(options, &mut frontend)?;
    writeln!(
        frontend.output,
        "[Python child and contained process tree exited successfully]"
    )?;
    frontend.output.finish()
}

/// UI owns the receivers, worker owns the real child. Queues are bounded and
/// nonblocking; saturation fails visibly rather than dropping stream deltas.
pub struct Interactive {
    commands: mpsc::SyncSender<Input>,
    quitting: std::sync::Arc<std::sync::atomic::AtomicBool>,
    pub events: mpsc::Receiver<Observation>,
    done: mpsc::Receiver<io::Result<()>>,
    finished: Option<Result<(), String>>,
}
struct Bridge {
    commands: mpsc::Receiver<Input>,
    quitting: std::sync::Arc<std::sync::atomic::AtomicBool>,
    events: mpsc::SyncSender<Observation>,
}
impl Frontend for Bridge {
    fn scheduled_handoff(&self) -> bool {
        true
    }
    fn command(&mut self) -> io::Result<Option<Input>> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Ok(Some(Input::Quit));
        }
        match self.commands.try_recv() {
            Ok(input) => Ok(Some(input)),
            Err(mpsc::TryRecvError::Empty) => Ok(None),
            Err(mpsc::TryRecvError::Disconnected) => Ok(Some(Input::Quit)),
        }
    }
    fn observe(&mut self, observation: Observation) -> io::Result<()> {
        self.events.try_send(observation).map_err(io::Error::other)
    }
}
impl Interactive {
    pub fn scheduled_decision(&self, proposal_id: String, accept: bool) -> io::Result<()> {
        self.commands
            .try_send(Input::ScheduleDecision {
                proposal_id,
                accept,
            })
            .map_err(io::Error::other)
    }

    pub fn start(options: Options) -> Self {
        let (commands, input) = mpsc::sync_channel(2);
        let (events, output) = mpsc::sync_channel(64);
        let (finished, done) = mpsc::channel();
        let quitting = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let worker_quitting = quitting.clone();
        thread::spawn(move || {
            let mut frontend = Bridge {
                commands: input,
                quitting: worker_quitting,
                events,
            };
            let result = drive(options, &mut frontend);
            let _ = finished.send(result);
        });
        Self {
            commands,
            quitting,
            events: output,
            done,
            finished: None,
        }
    }
    /// Cache cleanup outcome so UI polling cannot consume ownership evidence.
    pub fn poll_done(&mut self) -> Option<io::Result<()>> {
        if self.finished.is_none() {
            self.finished = match self.done.try_recv() {
                Ok(result) => Some(result.map_err(|error| error.to_string())),
                Err(mpsc::TryRecvError::Empty) => None,
                Err(mpsc::TryRecvError::Disconnected) => {
                    Some(Err("Runtime worker exited without a cleanup result".into()))
                }
            };
        }
        self.finished
            .clone()
            .map(|result| result.map_err(io::Error::other))
    }
    /// Close even after draw/input failure; keep draining to avoid blocking the
    /// worker's bounded observation queue while awaiting whole-job cleanup.
    pub fn shutdown(&mut self) -> io::Result<()> {
        if let Some(result) = self.poll_done() {
            return result;
        }
        self.quit()?;
        let deadline = Instant::now() + Duration::from_secs(45);
        loop {
            for _ in self.events.try_iter() {}
            if let Some(result) = self.poll_done() {
                return result;
            }
            if Instant::now() >= deadline {
                return Err(io::Error::other(
                    "Runtime cleanup result timed out; cleanup unproven",
                ));
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
    /// Unconditional epilogue for the terminal frontend, including fallible
    /// draw/poll/read paths. Preserve both UI and cleanup errors.
    pub fn with_cleanup(
        mut self,
        operation: impl FnOnce(&mut Self) -> io::Result<()>,
    ) -> io::Result<()> {
        let result = operation(&mut self);
        let cleanup = self.shutdown();
        match (result, cleanup) {
            (Ok(()), result) | (result, Ok(())) => result,
            (Err(ui), Err(cleanup)) => Err(io::Error::other(format!(
                "UI failure: {ui}; runtime cleanup: {cleanup}"
            ))),
        }
    }
    pub fn submit(&self, prompt: String) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Prompt(prompt))
            .map_err(io::Error::other)
    }
    pub fn submit_image(&self, prompt: String, image: crate::image::Review) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::ImagePrompt(prompt, image))
            .map_err(io::Error::other)
    }
    pub fn approve(&self, decision: crate::approvals::Decision) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Approve(decision))
            .map_err(io::Error::other)
    }
    pub fn control(&self, intent: crate::model_controls::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Control(intent))
            .map_err(io::Error::other)
    }
    pub fn runtime_controls(&self, intent: crate::runtime_controls_view::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::RuntimeControls(intent))
            .map_err(io::Error::other)
    }
    pub fn engine(&self, intent: crate::engine_controls::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Engine(intent))
            .map_err(io::Error::other)
    }
    pub fn commands(&self, intent: crate::command_palette::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Commands(intent))
            .map_err(io::Error::other)
    }
    pub fn rewrite(&self, intent: crate::conversation_rewrite::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Rewrite(Box::new(intent)))
            .map_err(io::Error::other)
    }
    pub fn compact(&self, intent: crate::conversation_compact::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Compact(Box::new(intent)))
            .map_err(io::Error::other)
    }
    pub fn history(&self, intent: crate::history_view::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::History(intent))
            .map_err(io::Error::other)
    }
    pub fn theme(&self, intent: crate::theme_controls::Intent) -> io::Result<()> {
        self.commands
            .try_send(Input::Theme(intent))
            .map_err(|e| io::Error::other(e.to_string()))
    }
    pub fn execution_stop(&self, action_id: String) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::ExecutionStop(action_id))
            .map_err(io::Error::other)
    }
    pub fn work(&self, intent: crate::work_view::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Work(intent))
            .map_err(io::Error::other)
    }
    pub fn settings(&self, intent: crate::settings_view::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Settings(intent))
            .map_err(io::Error::other)
    }
    pub fn backend(&self, intent: crate::backend_controls::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Backend(intent))
            .map_err(io::Error::other)
    }
    pub fn browser(&self, intent: crate::conversation_browser::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Browser(intent))
            .map_err(io::Error::other)
    }
    pub fn queue(&self, intent: crate::restored_queue::Intent) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Queue(intent))
            .map_err(io::Error::other)
    }
    pub fn answer(&self, decision: crate::questions::Decision) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Answer(decision))
            .map_err(io::Error::other)
    }
    pub fn abort(&self, prompt_id: String) -> io::Result<()> {
        if self.quitting.load(std::sync::atomic::Ordering::Acquire) {
            return Err(io::Error::other("Runtime is quitting"));
        }
        self.commands
            .try_send(Input::Abort { prompt_id })
            .map_err(io::Error::other)
    }
    pub fn quit(&self) -> io::Result<()> {
        // Cancellation is an out-of-band control, not another queue item.
        self.quitting
            .store(true, std::sync::atomic::Ordering::Release);
        Ok(())
    }
}

fn check_mirror_deadline(deadline: Option<Instant>) -> io::Result<()> {
    if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
        Err(io::Error::other("Runtime GUI envelope deadline elapsed"))
    } else {
        Ok(())
    }
}
fn check_deadline(deadline: Option<Instant>) -> io::Result<()> {
    if deadline.is_some_and(|deadline| Instant::now() >= deadline) {
        return Err(io::Error::other("Live deadline elapsed; no prompt retry"));
    }
    Ok(())
}
fn publish_startup(
    frontend: &mut impl Frontend,
    facts: &mut Option<crate::runtime_state::Update>,
    snapshot: crate::management::Snapshot,
    history: Option<crate::resume::History>,
    deadline: &mut Option<Instant>,
    engine_admission: Option<crate::engine_controls::Admission>,
) -> io::Result<()> {
    check_deadline(*deadline)?;
    if let Some(update) = facts.take() {
        frontend.observe(Observation::State(update))?;
    }
    let held = snapshot.held_restored_input;
    frontend.observe(Observation::State(
        crate::runtime_state::Update::from_management(snapshot),
    ))?;
    if let Some(held) = held
        && held > 0
    {
        frontend.observe(Observation::Information(format!(
            "Restored input held: {held}. Nothing replayed."
        )))?;
    }
    if let Some(history) = history {
        frontend.observe(Observation::History(history))?;
    }
    check_deadline(*deadline)?;
    *deadline = None;
    if let Some(admission) = engine_admission
        && admission != crate::engine_controls::Admission::PromptReady
    {
        frontend.observe(Observation::EngineAdmission(admission))
    } else {
        frontend.observe(Observation::Ready)
    }
}
fn publish_event(
    turn: &mut Turn,
    event: &Value,
    backend: &str,
    model: &str,
    deadline: &mut Option<Instant>,
    frontend: &mut impl Frontend,
) -> io::Result<Option<String>> {
    check_deadline(*deadline)?;
    let observation = turn.accept(event, backend, model)?;
    if let Some(update) = crate::runtime_state::Update::from_event(event)? {
        frontend.observe(Observation::State(update))?;
    }
    check_deadline(*deadline)?;
    if !turn.closing
        && matches!(&observation, Observation::WorkExecution(update) if matches!(update.as_ref(),WorkExecution::Ended{..}))
    {
        *deadline = None;
    }
    if !turn.closing
        && !turn.rewrite_active
        && matches!(
            observation,
            Observation::Ready
                | Observation::Complete
                | Observation::ApprovalStopped(_)
                | Observation::Cancelled(_)
                | Observation::ImageRefused { .. }
                | Observation::Refused(_)
                | Observation::Failed(_)
                | Observation::ApprovalSettled { .. }
                | Observation::Question {
                    settled: Some(_),
                    ..
                }
        )
    {
        *deadline = None;
    }
    // Successful question answers close on their correlated ACK, unlike host
    // cancellation events. The shared ledger has validated that ACK above.
    let answered = match &observation {
        Observation::Question {
            event: crate::questions::Event::Acknowledged { id, accepted: true },
            ..
        } => Some(id.clone()),
        _ => None,
    };
    frontend.observe(observation)?;
    Ok(answered)
}
#[cfg(test)]
mod deadline_tests {
    use super::*;
    struct SlowView {
        complete: bool,
    }
    impl Frontend for SlowView {
        fn command(&mut self) -> io::Result<Option<Input>> {
            Ok(None)
        }
        fn observe(&mut self, observation: Observation) -> io::Result<()> {
            match observation {
                Observation::State(_) => thread::sleep(Duration::from_millis(25)),
                Observation::Complete => self.complete = true,
                _ => {}
            }
            Ok(())
        }
    }
    #[test]
    fn publishing_runtime_state_cannot_erase_an_expired_turn_deadline() {
        let mut turn = Turn::default();
        turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"fixture"}),"claude","fixture").unwrap();
        let command = turn.submit("fixture").unwrap();
        for event in [
            json!({"type":"response","id":command["id"],"ok":true,"result":{"turn":"accepted"}}),
            json!({"type":"turn_start","model":"fixture"}),
            json!({"type":"text_delta","text":"done"}),
        ] {
            turn.accept(&event, "claude", "fixture").unwrap();
        }
        let mut deadline = Some(Instant::now() + Duration::from_millis(10));
        let mut frontend = SlowView { complete: false };
        let result = publish_event(
            &mut turn,
            &json!({"type":"turn_end","stopReason":"stop","tps":1.0}),
            "claude",
            "fixture",
            &mut deadline,
            &mut frontend,
        );
        assert!(
            result.is_err(),
            "expired deadline must not be cleared by completion"
        );
        assert!(!frontend.complete);
        assert!(deadline.is_some());
    }
}
#[cfg(test)]
mod approval_shutdown_tests {
    use super::*;
    #[test]
    fn quit_is_not_lost_behind_a_full_decision_queue() {
        let (commands, _input) = mpsc::sync_channel(2);
        let (_events, output) = mpsc::sync_channel(64);
        let (_finished, done) = mpsc::channel();
        let quitting = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
        let mut bridge = Bridge {
            commands: _input,
            events: _events,
            quitting: quitting.clone(),
        };
        let runtime = Interactive {
            commands,
            quitting,
            events: output,
            done,
            finished: None,
        };
        runtime
            .approve(crate::approvals::Decision {
                id: "a".into(),
                allow: false,
            })
            .unwrap();
        runtime
            .approve(crate::approvals::Decision {
                id: "b".into(),
                allow: false,
            })
            .unwrap();
        assert!(
            runtime.quit().is_ok(),
            "quit must have a control path independent of a full decision queue"
        );
        assert!(matches!(bridge.command().unwrap(), Some(Input::Quit)));
        assert!(
            runtime
                .approve(crate::approvals::Decision {
                    id: "c".into(),
                    allow: true
                })
                .is_err()
        );
    }
}

#[cfg(test)]
mod engine_admission_tests {
    use super::*;
    fn ready() -> Value {
        json!({"type":"ready","protocol_version":1,"launch_status":"management","management_reason":"engine_unavailable","conversation_id":"00000000-0000-4000-8000-000000000001","backend":"strata","model":"chosen","desired_model":"chosen","effective_model":null,"admission":"management_only","management_instance":"a".repeat(64)})
    }
    #[test]
    fn stopped_engine_requires_explicit_management_startup_and_never_ready() {
        let mut normal = Turn::default();
        assert!(normal.accept(&ready(), "strata", "chosen").is_err());
        let mut managed = Turn {
            allow_engine_management: true,
            ..Turn::default()
        };
        assert_eq!(
            managed.accept(&ready(), "strata", "chosen").unwrap(),
            Observation::EngineAdmission(crate::engine_controls::Admission::ManagementOnly)
        );
        assert!(managed.submit("never queued").is_err());
        assert!(managed.can_engine());
        assert!(!managed.can_manage());
    }
    #[test]
    fn management_opt_in_never_accepts_generic_blocked_or_unknown_reason() {
        for (key, value) in [
            ("launch_status", json!("blocked")),
            ("management_reason", json!("bad_policy")),
            ("desired_model", json!("other")),
        ] {
            let mut event = ready();
            event[key] = value;
            let mut turn = Turn {
                allow_engine_management: true,
                ..Turn::default()
            };
            assert!(turn.accept(&event, "strata", "chosen").is_err());
        }
    }
    #[test]
    fn explicit_work_launch_requires_exact_acknowledgement() {
        for value in [
            Value::Null,
            json!(true),
            json!(false),
            json!(0),
            json!(2),
            json!("1"),
            json!(1.0),
        ] {
            assert!(require_work_lifecycle(&json!({"work_lifecycle_version":value})).is_err());
        }
        assert!(require_work_lifecycle(&json!({})).is_err());
        assert!(require_work_lifecycle(&json!({"work_lifecycle_version":1})).is_ok());
    }
}

#[cfg(test)]
mod scheduled_shutdown_tests {
    use super::*;
    #[test]
    fn declined_schedule_cannot_retire_a_competing_human_transition() {
        let human = json!({"action_id":"human-new"});
        let scheduled = json!({"action_id":"scheduled-new"});
        let completed = json!({"action_id":"previous-completed"});
        let mut turn = Turn {
            conversation_shutdown: Some(human.clone()),
            conversation_completed_shutdown: Some(completed.clone()),
            ..Turn::default()
        };
        turn.retire_scheduled_refusal(None);
        assert_eq!(turn.conversation_shutdown, Some(human.clone()));
        turn.retire_scheduled_refusal(Some(&scheduled));
        assert_eq!(turn.conversation_shutdown, Some(human));
        turn.conversation_shutdown = Some(scheduled.clone());
        turn.retire_scheduled_refusal(Some(&scheduled));
        assert_eq!(turn.conversation_shutdown, Some(completed));
    }
}

#[cfg(test)]
mod shutdown_refusal_tests {
    use super::*;
    #[test]
    fn a_failed_shutdown_is_reported_as_the_source_refusal_not_as_a_prompt_response() {
        let mut turn = Turn::default();
        turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"fixture"}),"claude","fixture").unwrap();
        turn.begin_close();
        let error = turn
            .accept(
                &json!({"type":"response","id":SHUTDOWN_ID,"ok":false,"error":"[WinError 32] directory in use"}),
                "claude",
                "fixture",
            )
            .unwrap_err()
            .to_string();
        assert!(
            error.contains("shutdown") && error.contains("directory in use"),
            "{error}"
        );
        assert!(!error.contains("prompt"), "{error}");
    }
}

#[cfg(test)]
mod quit_interruption_tests {
    use super::*;
    use crate::runtime_controls::{Owner, OwnerState};
    fn owner(state: OwnerState, origin: Option<crate::work_lifecycle::Origin>) -> Owner {
        Owner {
            state,
            origin,
            wire: Value::Null,
        }
    }
    /// A started host turn, then a user Quit. `ended` delivers the interrupted turn_end
    /// that the source emits while it drains the exact stream.
    fn quitting(ended: bool) -> (Turn, crate::work_lifecycle::Origin) {
        let mut turn = Turn::default();
        turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"fixture"}),"claude","fixture").unwrap();
        let command = turn.submit("fixture").unwrap();
        for event in [
            json!({"type":"response","id":command["id"],"ok":true,"result":{"turn":"accepted"}}),
            json!({"type":"turn_start","model":"fixture"}),
        ] {
            turn.accept(&event, "claude", "fixture").unwrap();
        }
        turn.begin_close();
        if ended {
            turn.accept(
                &json!({"type":"turn_end","stopReason":"error","error":"Something went wrong talking to the model server."}),
                "claude",
                "fixture",
            )
            .unwrap();
        }
        let origin = crate::work_lifecycle::Origin {
            session: "s".into(),
            operation: turn.command_id.clone(),
            hook: None,
            source: "rpc".into(),
            execution: "e".into(),
        };
        (turn, origin)
    }
    #[test]
    fn a_readback_published_after_the_quit_interrupted_stream_may_name_it_retired() {
        // Source order under load: the interrupted turn_end, then the held readback,
        // whose owner is read at publication and is already past Active.
        let (turn, origin) = quitting(true);
        turn.verify_runtime_owner(&owner(OwnerState::Idle, None), Some(origin.clone()), true)
            .expect("an idle owner after the observed interrupted end");
        turn.verify_runtime_owner(
            &owner(OwnerState::Terminal, Some(origin.clone())),
            Some(origin),
            true,
        )
        .expect("a terminal owner after the observed interrupted end");
    }
    #[test]
    fn a_readback_cannot_retire_a_stream_whose_end_was_never_observed() {
        let (turn, origin) = quitting(false);
        assert!(
            turn.verify_runtime_owner(&owner(OwnerState::Idle, None), Some(origin.clone()), true)
                .is_err(),
            "idle with no stream end observed"
        );
        assert!(
            turn.verify_runtime_owner(
                &owner(OwnerState::Terminal, Some(origin.clone())),
                Some(origin.clone()),
                true
            )
            .is_err(),
            "terminal with no stream end observed"
        );
        turn.verify_runtime_owner(
            &owner(OwnerState::Active, Some(origin.clone())),
            Some(origin),
            true,
        )
        .expect("the early order: the owner is still active before the end");
    }
    #[test]
    fn a_foreign_origin_is_still_refused_after_the_interrupted_end() {
        let (turn, origin) = quitting(true);
        let mut other = origin.clone();
        other.execution = "other".into();
        assert!(
            turn.verify_runtime_owner(
                &owner(OwnerState::Terminal, Some(other)),
                Some(origin),
                true
            )
            .is_err()
        );
    }
}
