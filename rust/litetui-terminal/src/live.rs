//! Owned Python RPC runtime for one-shot and interactive frontends.
//! Versioned management startup, selected envelopes and serial prompt ownership.
//! No automatic retries, backend fallback, automatic approvals or local model launches.
use crate::{
    protocol::JsonlDecoder,
    transport::{Inbound, IngressLimits},
};
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read, Write},
    path::PathBuf,
    process::{Child, ChildStdin, Command, Stdio},
    sync::mpsc,
    thread,
    time::{Duration, Instant},
};

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
    pub launch: crate::runtime_launch::LaunchOptions,
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
    Engine(crate::engine_controls::Update),
    EngineAdmission(crate::engine_controls::Admission),
    State(crate::runtime_state::Update),
    History(crate::resume::History),
    Controls(crate::model_controls::Update),
    Queue(crate::restored_queue::Update),
    Commands(crate::command_palette::Update),
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
    ready: bool,
    engine_admission: Option<crate::engine_controls::Admission>,
    allow_engine_management: bool,
    engine_shutdown: Option<crate::engine_controls::ShutdownIdentity>,
    shutdown_acknowledged: bool,
    sent: bool,
    closing: bool,
    command_id: String,
    continued_input: Option<(String, String)>,
    visible_answers: std::collections::BTreeSet<usize>,
    tools_seen: bool,
    card_count: usize,
    started: bool,
    acknowledged: bool,
    admission_refused: bool,
    settled: bool,
    terminal: Option<TerminalOutcome>,
    content_bytes: usize,
    approvals: crate::approvals::Ledger,
    questions: crate::questions::Ledger,
    policy: Option<crate::tool_mode::Expected>,
    aborts: crate::cancellation::Ledger,
    stop_requests: usize,
}
impl Turn {
    fn can_manage(&self) -> bool {
        self.can_engine()
            && self
                .engine_admission
                .is_none_or(|a| a == crate::engine_controls::Admission::PromptReady)
    }
    fn can_engine(&self) -> bool {
        self.ready && !self.closing && (!self.sent || self.settled)
    }
    pub fn with_policy(policy: crate::tool_mode::Expected) -> Self {
        Self {
            policy: Some(policy),
            ..Self::default()
        }
    }
    /// Reserve a new command ID exactly once. Neither a write nor admission.
    pub fn submit(&mut self, prompt: &str) -> io::Result<Value> {
        if !self.ready
            || self
                .engine_admission
                .is_some_and(|a| a != crate::engine_controls::Admission::PromptReady)
            || self.closing
            || !self.approvals.settled()
            || !self.questions.settled()
            || (self.sent && !self.settled)
            || prompt.trim().is_empty()
            || prompt.len() > 65536
        {
            return Err(io::Error::other(
                "Runtime is not idle, or prompt is empty/too large",
            ));
        }
        // Provider journals outlive this process. An ordinal, timestamp or PID
        // can alias a saved operation after restart; reserve fresh OS randomness.
        self.command_id = fresh_prompt_id()?;
        self.continued_input = None;
        self.sent = true;
        self.started = false;
        self.acknowledged = false;
        self.admission_refused = false;
        self.settled = false;
        self.terminal = None;
        self.stop_requests = 0;
        self.visible_answers.clear();
        self.tools_seen = false;
        self.card_count = 1;
        self.content_bytes = 0;
        Ok(
            json!({"type":"prompt","id":self.command_id,"message":prompt,"tool_profile":self.policy.as_ref().map_or("strict",|p|p.profile.as_str())}),
        )
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
    fn settle(&mut self) -> Observation {
        self.settled = true;
        match self
            .terminal
            .as_ref()
            .expect("terminal outcome before settlement")
        {
            TerminalOutcome::Complete => Observation::Complete,
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
            || self.settled
            || self.terminal.is_some()
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
                self.shutdown_acknowledged = true;
                Ok(Observation::Information(
                    if self.engine_shutdown.is_some() {
                        "shutdown acknowledged; resource release unverified".into()
                    } else {
                        "shutdown acknowledged".into()
                    },
                ))
            }
            "response" => {
                if let Some(stopped) = self.aborts.response(event)? {
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
                if event["id"] != self.command_id
                    || !self.sent
                    || self.acknowledged
                    || event["ok"] != true
                    || event["result"]["turn"] != "accepted"
                {
                    return Err(fail("Unexpected/refused prompt response"));
                }
                self.acknowledged = true;
                Ok(if self.can_settle() {
                    self.settle()
                } else {
                    Observation::Information("prompt acknowledged (not completion)".into())
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
                } else {
                    Observation::Information(
                        "Input not started; waiting for admission and input/stop receipts.".into(),
                    )
                })
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
                let card = crate::native_activity::card_index(event)?;
                if card.is_none() && self.tools_seen {
                    return Err(fail("Content without native display ownership after tools"));
                }
                if card.is_some_and(|index| index > self.card_count) {
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
                if self.started && self.terminal.is_none() && !self.closing =>
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
            "user_input_requested" if self.started && self.terminal.is_none() && !self.closing => {
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
                    return Ok(Observation::Information(
                        "turn interrupted while quitting; not completed".into(),
                    ));
                }
                if !self.acknowledged
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
                if self.can_settle() {
                    Ok(self.settle())
                } else {
                    Ok(Observation::Information(
                        "Turn ended; waiting for input closures and command responses".into(),
                    ))
                }
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
        .args([
            "-m",
            "litetui.cli",
            "--rpc",
            "--hold-restored-input",
            "--backend",
            &options.backend,
            "--model",
            &options.model,
            "--tool-profile",
            profile.as_str(),
            "--mcp-servers",
            "none",
        ])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(id) = &options.resume {
        command.args(["--convo", id]);
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
    let mut tail = Vec::new();
    let mut deadline = Some(Instant::now() + options.timeout);
    let result = (|| -> io::Result<()> {
        loop {
            check_deadline(deadline)?;
            check_mirror_deadline(mirror_deadline)?;
            if !turn.closing
                && let Some(command) = frontend.command()?
            {
                match command {
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
                    Input::Settings(intent) => {
                        let editing = matches!(intent, crate::settings_view::Intent::Editor(_));
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
                            Err(error) => frontend.observe(Observation::Settings(if editing {
                                crate::settings_view::Update::Editor(
                                    crate::settings_editor::Update::Rejected(error.to_string()),
                                )
                            } else {
                                crate::settings_view::Update::Rejected(error.to_string())
                            }))?,
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
                        let command = if turn.can_manage() {
                            management.request_browser(intent)
                        } else {
                            Err(io::Error::other(
                                "Finish the current turn before browsing conversations",
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
                            Err(error) => frontend.observe(Observation::Browser(
                                crate::conversation_browser::Update::Rejected(error.to_string()),
                            ))?,
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
                        let command = if turn.can_engine() {
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
                    Input::Prompt(prompt) => {
                        if !management.ready() {
                            return Err(io::Error::other("Management startup is not complete"));
                        }
                        let value = turn.submit(&prompt)?;
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
                    Input::Abort { prompt_id } => {
                        if let Some(value) = turn.abort(&prompt_id)? {
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
                        turn.closing = true;
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
                        if turn.engine_shutdown.is_some() && !turn.shutdown_acknowledged {
                            Err(io::Error::other(
                                "Engine runtime exited without verified shutdown acknowledgement",
                            ))
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
                match management.accept(event)? {
                    Step::Bootstrap(ready) => {
                        turn.accept(&ready, &options.backend, &options.model)?;
                        startup_facts = if turn.can_manage() {
                            crate::runtime_state::Update::from_event(&ready)?
                        } else {
                            None
                        };
                        if !turn.closing {
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
                                turn.policy
                                    .as_ref()
                                    .expect("launch policy")
                                    .validate(&policy)?;
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
                                turn.policy
                                    .as_ref()
                                    .expect("launch policy")
                                    .validate(&policy)?;
                            }
                            frontend.observe(Observation::Commands(update))?;
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Settings { update, policy } => {
                        if !turn.closing {
                            if let Some(policy) = policy {
                                turn.policy
                                    .as_ref()
                                    .expect("launch policy")
                                    .validate(&policy)?;
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
                        if !turn.closing {
                            turn.policy
                                .as_ref()
                                .expect("launch policy")
                                .validate(&opened.policy)?;
                            frontend.observe(Observation::BackendChanged(opened))?;
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
                        if !turn.closing {
                            turn.policy
                                .as_ref()
                                .expect("launch policy")
                                .validate(&opened.policy)?;
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
                            check_deadline(deadline)?;
                            deadline = None;
                        }
                    }
                    Step::Event(event) => publish_event(
                        &mut turn,
                        &event,
                        management.backend().unwrap_or(&options.backend),
                        management.model().unwrap_or(&options.model),
                        &mut deadline,
                        frontend,
                    )?,
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
    Engine(crate::engine_controls::Intent),
    Control(crate::model_controls::Intent),
    Queue(crate::restored_queue::Intent),
    Commands(crate::command_palette::Intent),
    Settings(crate::settings_view::Intent),
    Backend(crate::backend_controls::Intent),
    Browser(crate::conversation_browser::Intent),
    Prompt(String),
    Abort { prompt_id: String },
    Approve(crate::approvals::Decision),
    Answer(crate::questions::Decision),
    Quit,
}
trait Frontend {
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
            Observation::Engine(_)
            | Observation::EngineAdmission(_)
            | Observation::State(_)
            | Observation::Commands(_)
            | Observation::Settings(_)
            | Observation::Backend(_)
            | Observation::BackendChanged(_)
            | Observation::Browser(_)
            | Observation::Opened(_)
            | Observation::Controls(_)
            | Observation::Queue(_)
            | Observation::Submission { .. } => {} // Read-only footer facts; no one-shot action.
            Observation::History(history) => {
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
            Observation::Information(kind) => {
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
            Observation::Refused(reason) => {
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
) -> io::Result<()> {
    check_deadline(*deadline)?;
    let observation = turn.accept(event, backend, model)?;
    if let Some(update) = crate::runtime_state::Update::from_event(event)? {
        frontend.observe(Observation::State(update))?;
    }
    check_deadline(*deadline)?;
    if !turn.closing
        && matches!(
            observation,
            Observation::Ready
                | Observation::Complete
                | Observation::ApprovalStopped(_)
                | Observation::Cancelled(_)
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
    frontend.observe(observation)
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
}
