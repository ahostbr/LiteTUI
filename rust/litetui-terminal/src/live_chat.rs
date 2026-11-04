//! Bounded serialized native chat view. Local row order is not a wire execution ID.
use crate::{
    live::{ContentKind, Observation},
    native_activity::{Activity, Tool},
};
use std::io;
use unicode_width::UnicodeWidthChar;
// Independent display-work budget, not a byte limit. LF pays one full
// minimum-width row, escaped controls pay their expansion, every scalar pays
// at least one unit (including combining marks), and every card pays overhead.
// Reject before allocating/wrapping an oversized view; never silently truncate.
pub(crate) const DISPLAY_UNITS: usize = 32768;
pub(crate) const RETAINED_BYTES: usize = 8 * 1024 * 1024;
pub(crate) fn display_units(text: &str) -> io::Result<usize> {
    text.chars().try_fold(0usize, |total, ch| {
        let cost = if ch == '\n' {
            32
        } else if crate::display_control(ch) {
            12
        } else {
            ch.width().unwrap_or(0).max(1)
        };
        let total = total.saturating_add(cost);
        if total > DISPLAY_UNITS {
            Err(io::Error::other("Live display-line/work limit exceeded"))
        } else {
            Ok(total)
        }
    })
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ReplyState {
    Sending,
    Streaming,
    Cancelling,
    Cancelled,
    Complete,
    ApprovalStopped,
    Incomplete,
    Refused,
    Failed,
    Reconciled,
    Maintenance,
    MaintenanceSettled,
    AwaitingRetirement,
}
#[derive(Default)]
pub struct Card {
    pub answer: String,
    pub reasoning: String,
    answer_units: usize,
    reasoning_units: usize,
}
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Item {
    Card(usize),
    Tool(usize),
    WorkInput(usize),
}
pub struct WorkSource {
    pub source: String,
    pub requested: crate::tool_mode::Profile,
    pub effective: crate::tool_mode::Profile,
}
pub struct WorkInput {
    pub text: String,
    pub source: WorkSource,
    pub delivery: crate::work_lifecycle::Delivery,
}
pub struct CommittedWork {
    pub status: String,
    pub history: crate::work_lifecycle::CommittedHistory,
}
struct PendingWorkSegment {
    operation: String,
    admission: String,
    input: WorkInput,
}
pub struct Row {
    pub source_owned: bool,
    pub source: Option<WorkSource>,
    pub inputs: Vec<WorkInput>,
    pub committed: Option<CommittedWork>,
    pub prompt: String,
    pub cards: Vec<Card>,
    pub tools: Vec<Tool>,
    pub items: Vec<Item>,
    pub state: ReplyState,
    pub stop_reason: Option<String>,
}
struct LaunchReservation {
    action_id: String,
    generation: u64,
    stop_requested: bool,
    stop_sent: bool,
    stop_handed_off: bool,
}
pub struct Chat {
    launch: Option<LaunchReservation>,
    schedule_proposal: Option<String>,
    schedule_reserved: bool,
    schedule_prompt_held: bool,
    pub backend: String,
    pub model: String,
    pub rows: Vec<Row>,
    pub history: Option<crate::resume::History>,
    pub saved_history: crate::history_view::View,
    pub rewrite: crate::conversation_rewrite::View,
    pub compact: crate::conversation_compact::View,
    compact_active: bool,
    pub prior_context_rows: usize,
    rewrite_pending: bool,
    rewrite_old_cost: Option<(usize, usize)>,
    rewrite_start_row: Option<usize>,
    rewrite_stream_row: Option<usize>,
    rewrite_segment: Option<u64>,
    rewrite_rows: std::collections::BTreeMap<String, usize>,
    rewrite_maintenance: std::collections::HashSet<String>,
    operation_rows: std::collections::BTreeMap<String, usize>,
    work_admissions: std::collections::BTreeMap<String, String>,
    work_seen: std::collections::HashSet<String>,
    work_ended: std::collections::BTreeMap<String, crate::live::TerminalOutcome>,
    work_segments: std::collections::BTreeMap<String, PendingWorkSegment>,
    suspended_rows: std::collections::BTreeMap<String, usize>,
    forwarded_row: Option<usize>,
    work_uncertain: std::collections::BTreeMap<String, String>,
    pub ready: bool,
    pub busy: bool,
    pub closed: bool,
    pub quitting: bool,
    pub notice: String,
    pub conversation_exit_notice: Option<String>,
    pub runtime_state: crate::runtime_state::State,
    pub approvals: crate::approvals::View,
    pub questions: crate::questions::View,
    pub runtime_controls: crate::runtime_controls_view::View,
    pub controls: crate::model_controls::View,
    pub queue: crate::restored_queue::View,
    pub engine: crate::engine_controls::View,
    pub engine_admission: Option<crate::engine_controls::Admission>,
    pub commands: crate::command_palette::View,
    pub themes: crate::theme_controls::View,
    pub effective_theme: Option<crate::theme::Theme>,
    pub theme_status: String,
    pub work: crate::work_view::View,
    pub settings: crate::settings_view::View,
    pub backend_controls: crate::backend_controls::View,
    pub browser: crate::conversation_browser::View,
    pending: Option<String>,
    submission_id: Option<String>,
    queue_pending: bool,
    recovered_inputs: std::collections::HashSet<String>,
    stop_requests: usize,
    stops_sent: usize,
    pub stop_confirm: bool,
    pub stop_selected: bool,
    retained_bytes: usize,
    retained_units: usize,
}
impl Chat {
    pub fn new(backend: String, model: String) -> Self {
        Self {
            launch: None,
            schedule_proposal: None,
            schedule_reserved: false,
            schedule_prompt_held: false,
            backend,
            model,
            rows: Vec::new(),
            history: None,
            saved_history: crate::history_view::View::default(),
            rewrite: crate::conversation_rewrite::View::default(),
            compact: crate::conversation_compact::View::default(),
            compact_active: false,
            prior_context_rows: 0,
            rewrite_pending: false,
            rewrite_old_cost: None,
            rewrite_start_row: None,
            rewrite_stream_row: None,
            rewrite_segment: None,
            rewrite_rows: std::collections::BTreeMap::new(),
            rewrite_maintenance: std::collections::HashSet::new(),
            operation_rows: std::collections::BTreeMap::new(),
            work_admissions: std::collections::BTreeMap::new(),
            work_seen: std::collections::HashSet::new(),
            work_ended: std::collections::BTreeMap::new(),
            work_segments: std::collections::BTreeMap::new(),
            suspended_rows: std::collections::BTreeMap::new(),
            forwarded_row: None,
            work_uncertain: std::collections::BTreeMap::new(),
            ready: false,
            busy: false,
            closed: false,
            quitting: false,
            notice: "Connecting real owned Python runtime…".into(),
            conversation_exit_notice: None,
            runtime_state: crate::runtime_state::State::default(),
            approvals: crate::approvals::View::default(),
            questions: crate::questions::View::default(),
            runtime_controls: crate::runtime_controls_view::View::default(),
            controls: crate::model_controls::View::default(),
            queue: crate::restored_queue::View::default(),
            engine: crate::engine_controls::View::default(),
            engine_admission: None,
            commands: crate::command_palette::View::default(),
            themes: crate::theme_controls::View::default(),
            effective_theme: None,
            theme_status: "Theme: local fallback; source pending".into(),
            work: crate::work_view::View::default(),
            settings: crate::settings_view::View::default(),
            backend_controls: crate::backend_controls::View::default(),
            browser: crate::conversation_browser::View::default(),
            pending: None,
            submission_id: None,
            queue_pending: false,
            recovered_inputs: std::collections::HashSet::new(),
            stop_requests: 0,
            stops_sent: 0,
            stop_confirm: false,
            stop_selected: false,
            retained_bytes: 0,
            retained_units: 0,
        }
    }
    pub fn decide_schedule(&mut self, external_idle: bool) -> Option<(String, bool)> {
        let id = self.schedule_proposal.take()?;
        let idle = external_idle
            && self.launch.is_none()
            && self.ready
            && !self.schedule_prompt_held
            && !self.busy
            && !self.closed
            && !self.quitting
            && self.pending.is_none()
            && !self.queue_pending
            && !self.rewrite_pending
            && !self.compact_active
            && self.work_uncertain.is_empty()
            && self.work_admissions.is_empty()
            && self.approvals.is_empty()
            && self.questions.is_empty()
            && !self.stop_confirm
            && !self.runtime_controls.owns_input()
            && !self.runtime_controls.pending()
            && !self.controls.owns_input()
            && !self.queue.owns_input()
            && !self.engine.owns_input()
            && !self.engine.pending()
            && !self.commands.owns_input()
            && !self.themes.owns_input()
            && !self.saved_history.owns_input()
            && !self.work.owns_input()
            && !self.settings.owns_input()
            && !self.backend_controls.owns_input()
            && !self.browser.owns_input()
            && !self.rewrite.owns_input()
            && !self.compact.owns_input();
        self.schedule_reserved = idle;
        if idle {
            self.ready = false;
            self.notice = "Scheduled job is preparing its new conversation; draft kept.".into();
        }
        Some((id, idle))
    }
    pub fn submit(&mut self, prompt: &str) -> io::Result<()> {
        self.reserve_row(prompt, false)
    }
    fn reserve_row(&mut self, prompt: &str, recovered: bool) -> io::Result<()> {
        self.reserve_row_inner(prompt, recovered, false)
    }
    fn reserve_row_inner(
        &mut self,
        prompt: &str,
        recovered: bool,
        rewriting: bool,
    ) -> io::Result<()> {
        if !self.ready
            || self.launch.is_some()
            || self.busy
            || !self.work_uncertain.is_empty()
            || self.closed
            || self.quitting
            || self.runtime_controls.owns_input()
            || self.runtime_controls.pending()
            || self.controls.owns_input()
            || self.queue.owns_input()
            || self.engine.owns_input()
            || self.engine.pending()
            || self.commands.owns_input()
            || self.themes.owns_input()
            || self.saved_history.owns_input()
            || self.work.owns_input()
            || self.settings.owns_input()
            || self.backend_controls.owns_input()
            || self.browser.owns_input()
            || self.queue_pending
            || self.rewrite_pending
            || self.rewrite.blocks_composer() && !rewriting
            || self.compact.blocks_composer()
        {
            return Err(io::Error::other(
                "Wait for the runtime to be ready and the current turn to finish",
            ));
        }
        if prompt.trim().is_empty()
            || prompt.len() > 65536
            || (!recovered && prompt.trim_start().starts_with('/'))
        {
            return Err(io::Error::other(
                "Enter ordinary prompt text; use the source command palette for slash commands (64 KiB maximum)",
            ));
        }
        let units = self.prompt_budget(prompt)?;
        self.saved_history.invalidate();
        self.retained_units += units;
        self.retained_bytes += prompt.len();
        if !rewriting {
            self.rewrite_stream_row = None;
            self.rewrite_start_row = None;
        }
        self.pending = Some(prompt.to_owned());
        self.submission_id = None;
        self.stop_requests = 0;
        self.stops_sent = 0;
        self.rows.push(Row {
            source_owned: false,
            source: None,
            inputs: Vec::new(),
            committed: None,
            prompt: prompt.into(),
            cards: vec![Card::default()],
            tools: Vec::new(),
            items: vec![Item::Card(0)],
            state: ReplyState::Sending,
            stop_reason: None,
        });
        self.busy = true;
        self.notice = "Sending prompt (not yet admitted)…".into();
        Ok(())
    }
    fn prompt_budget(&self, prompt: &str) -> io::Result<usize> {
        if self.rows.len() >= 100
            || self.retained_bytes.saturating_add(prompt.len()) > RETAINED_BYTES
        {
            return Err(io::Error::other(
                "Live transcript limit reached; no prompt was sent",
            ));
        }
        let units = display_units(prompt)?.saturating_add(320);
        if self.retained_units.saturating_add(units) > DISPLAY_UNITS {
            return Err(io::Error::other(
                "Live display-line/work limit reached; no prompt sent",
            ));
        }
        Ok(units)
    }
    pub fn prepare_reconciliation(&self, intent: &crate::restored_queue::Intent) -> io::Result<()> {
        if !self.ready
            || self.busy && !self.uncertainty_only_busy()
            || self.closed
            || self.quitting
            || self.queue_pending
        {
            return Err(io::Error::other(
                "Finish the current operation before checking provider history",
            ));
        }
        self.prompt_budget(self.queue.selection_text(intent)?)?;
        Ok(())
    }
    fn record_reconciled(&mut self, entry: &crate::restored_queue::Entry) -> io::Result<()> {
        if !self.ready
            || self.busy && !self.uncertainty_only_busy()
            || self.closed
            || self.quitting
            || self.queue_pending
            || self.recovered_inputs.contains(&entry.id)
        {
            return Err(io::Error::other("Unexpected or duplicate reconciled input"));
        }
        let units = self.prompt_budget(&entry.content)?;
        self.retained_units += units;
        self.retained_bytes += entry.content.len();
        self.recovered_inputs.insert(entry.id.clone());
        self.rows.push(Row {
            source_owned: false,
            source: None,
            inputs: Vec::new(),
            committed: None,
            prompt: entry.content.clone(),
            cards: Vec::new(),
            tools: Vec::new(),
            items: Vec::new(),
            state: ReplyState::Reconciled,
            stop_reason: None,
        });
        Ok(())
    }
    pub fn queue_pending(&self) -> bool {
        self.queue_pending
    }
    pub fn prepare_continuation(
        &mut self,
        intent: &crate::restored_queue::Intent,
    ) -> io::Result<()> {
        let prompt = self.queue.selection_text(intent)?.to_owned();
        self.queue.finish();
        self.engine.finish();
        self.commands.finish();
        self.themes.finish();
        self.saved_history.finish();
        self.work.finish();
        self.settings.finish();
        self.backend_controls.finish();
        self.browser.finish();
        self.reserve_row(&prompt, true)?;
        self.pending = None; // The queue command carries identity, never a duplicate prompt.
        self.queue_pending = true;
        self.notice = "Continuing saved input; waiting for admission…".into();
        Ok(())
    }
    pub fn prepare_rewrite(
        &mut self,
        intent: &crate::conversation_rewrite::Intent,
    ) -> io::Result<()> {
        let prompt = self.rewrite.prepare_apply(intent)?.text.clone();
        if self.rewrite_pending || self.busy || !self.ready || self.closed || self.quitting {
            return Err(io::Error::other("Finish current work before edit/retry"));
        }
        self.prompt_budget(&prompt)?;
        self.saved_history.finish();
        self.browser.finish();
        self.completion_closed_for_rewrite();
        let old = (self.retained_bytes, self.retained_units);
        self.reserve_row_inner(&prompt, false, true)?;
        self.pending = None;
        self.rewrite_pending = true;
        self.rewrite_old_cost = Some(old);
        self.rewrite_start_row = Some(self.rows.len() - 1);
        self.rewrite_stream_row = self.rewrite_start_row;
        self.rewrite_segment = None;
        self.rewrite_rows.clear();
        self.rewrite_maintenance.clear();
        self.notice =
            "Submitting reviewed edit/retry; admission and saved effects are not yet verified."
                .into();
        Ok(())
    }
    pub fn prepare_compact(
        &mut self,
        intent: &crate::conversation_compact::Intent,
    ) -> io::Result<()> {
        self.compact.prepare_apply(intent)?;
        if self.rewrite_pending || self.busy || !self.ready || self.closed || self.quitting {
            return Err(io::Error::other("Finish current work before compaction"));
        }
        self.prompt_budget("Conversation compaction")?;
        self.saved_history.finish();
        self.browser.finish();
        self.completion_closed_for_rewrite();
        self.pending = None;
        self.submission_id = None;
        self.stop_requests = 0;
        self.stops_sent = 0;
        self.compact_active = true;
        self.rewrite_pending = true;
        self.busy = true;
        self.rewrite_old_cost = Some((self.retained_bytes, self.retained_units));
        self.rewrite_start_row = Some(self.rows.len());
        self.rewrite_stream_row = None;
        self.rewrite_segment = None;
        self.rewrite_rows.clear();
        self.rewrite_maintenance.clear();
        self.notice = "Submitting reviewed compaction; saved effects are not yet verified.".into();
        Ok(())
    }
    fn completion_closed_for_rewrite(&mut self) {
        self.controls.close();
        self.queue.finish();
        self.engine.finish();
        self.commands.finish();
        self.themes.finish();
        self.work.finish();
        self.settings.finish();
        self.backend_controls.finish();
    }
    pub fn rewrite_pending(&self) -> bool {
        self.rewrite_pending || self.rewrite.blocks_composer() || self.compact.blocks_composer()
    }
    pub fn prepare_work(&mut self, intent: &crate::work_view::Intent) -> io::Result<()> {
        let crate::work_view::Request::ExecutionApply(review) = &intent.request else {
            return Ok(());
        };
        if !self.ready
            || self.busy
            || self.closed
            || self.quitting
            || self.launch.is_some()
            || self.pending.is_some()
            || self.queue_pending
            || self.rewrite_pending
            || self.compact_active
            || !self.work_uncertain.is_empty()
            || !self.work_admissions.is_empty()
            || !self.approvals.is_empty()
            || !self.questions.is_empty()
            || self.work.pending_generation() != Some(intent.generation)
        {
            return Err(io::Error::other(
                "Finish current work before starting this invocation",
            ));
        }
        self.prompt_budget(review.wire["payload"]["text"].as_str().ok_or_else(|| {
            io::Error::other("Execution review omitted its exact display payload")
        })?)?;
        self.launch = Some(LaunchReservation {
            action_id: review.action_id.clone(),
            generation: intent.generation,
            stop_requested: false,
            stop_sent: false,
            stop_handed_off: false,
        });
        self.notice =
            "Starting reviewed Work; waiting for its exact launch receipt. Ctrl+Esc stops it."
                .into();
        Ok(())
    }
    pub fn execution_pending(&self) -> bool {
        self.launch.is_some()
    }
    pub fn take_execution_stop(&mut self) -> Option<String> {
        let launch = self.launch.as_mut()?;
        if !launch.stop_requested || launch.stop_sent || self.quitting {
            return None;
        }
        launch.stop_sent = true;
        Some(launch.action_id.clone())
    }
    pub fn take_submission(&mut self) -> Option<String> {
        self.pending.take()
    }
    pub fn cancelling(&self) -> bool {
        self.launch.as_ref().is_some_and(|p| p.stop_requested)
            || self.busy && self.stop_requests > 0
    }
    pub fn open_stop(&mut self) {
        if !self.closed
            && !self.quitting
            && (self.launch.as_ref().is_some_and(|p| !p.stop_requested)
                || self.busy && self.stop_requests < 2 && self.launch.is_none())
        {
            self.stop_confirm = true;
            self.stop_selected = false;
        }
    }
    pub fn request_stop(&mut self) -> io::Result<()> {
        self.stop_confirm = false;
        if !self.closed
            && !self.quitting
            && let Some(launch) = self.launch.as_mut()
        {
            launch.stop_requested = true;
            self.approvals.take_decision();
            self.questions.take_decision();
            self.notice = "Stopping the exact invocation; waiting for verified retirement. Goal Pause is separate.".into();
            return Ok(());
        }
        if !self.busy || self.closed || self.quitting || self.stop_requests >= 2 {
            return Ok(());
        }
        if self.pending.take().is_some() {
            return self.settle(crate::live::TerminalOutcome::Cancelled(
                "Cancelled before sending to the runtime.".into(),
            ));
        }
        self.stop_requests += 1;
        self.approvals.take_decision();
        self.questions.take_decision();
        if self.pending_row().is_ok() {
            let index = self.current_row_index()?;
            self.rows[index].state = ReplyState::Cancelling;
        }
        self.notice =
            "Stopping; waiting for the runtime. Ctrl+Esc can request a forced stop.".into();
        Ok(())
    }
    pub fn take_abort(&mut self) -> Option<String> {
        if self.launch.is_some() || !self.busy || self.stops_sent >= self.stop_requests {
            return None;
        }
        let id = self.submission_id.clone()?;
        self.stops_sent += 1;
        Some(id)
    }
    fn install_conversation(
        &mut self,
        snapshot: crate::management::Snapshot,
        history: crate::resume::History,
        queue: crate::restored_queue::Snapshot,
    ) {
        self.backend = snapshot.backend.clone();
        self.model = snapshot.model.clone();
        self.runtime_state.opened(snapshot);
        self.controls = crate::model_controls::View::default();
        self.queue = crate::restored_queue::View::default();
        self.queue
            .observe(crate::restored_queue::Update::Snapshot(queue));
        self.rows.clear();
        self.operation_rows.clear();
        self.work_admissions.clear();
        self.work_seen.clear();
        self.work_ended.clear();
        self.work_segments.clear();
        self.suspended_rows.clear();
        self.forwarded_row = None;
        self.work_uncertain.clear();
        self.prior_context_rows = 0;
        self.recovered_inputs.clear();
        self.submission_id = None;
        self.stop_requests = 0;
        self.stops_sent = 0;
        self.approvals.clear();
        self.questions.clear();
        self.retained_bytes = history.bytes;
        self.retained_units = history.units;
        self.saved_history.finish();
        if let (Some(descriptor), Some(page)) =
            (history.descriptor.as_deref(), history.page.as_deref())
        {
            self.saved_history
                .install(descriptor.clone(), page.clone(), false)
                .expect("verified history page");
        }
        self.history = Some(history);
    }
    pub fn observe(&mut self, observation: Observation) -> io::Result<()> {
        if self.closed {
            return Err(io::Error::other("Observation after live view closed"));
        }
        match observation {
            Observation::RuntimeControls(update) => {
                self.runtime_controls.apply(update);
                self.notice = self.runtime_controls.notice.clone();
            }
            Observation::RuntimePolicy(policy) => {
                self.runtime_state.install_runtime_policy(policy.facts);
            }
            Observation::WorkLaunchStop {
                action_id,
                generation,
                stopped_operation,
            } => {
                let launch = self
                    .launch
                    .as_mut()
                    .filter(|p| p.action_id == action_id && p.generation == generation)
                    .ok_or_else(|| {
                        io::Error::other("Execution Stop handoff changed its exact reservation")
                    })?;
                if !launch.stop_requested || !launch.stop_sent || launch.stop_handed_off {
                    return Err(io::Error::other(
                        "Execution Stop handoff was unsolicited or repeated",
                    ));
                }
                launch.stop_handed_off = true;
                if let Some(operation) = stopped_operation {
                    if self.submission_id.as_ref() != Some(&operation) || !self.busy {
                        return Err(io::Error::other(
                            "Execution Stop handoff changed actual admitted operation",
                        ));
                    }
                    self.stop_requests += 1;
                    self.stops_sent += 1;
                    if let Ok(index) = self.current_row_index() {
                        self.rows[index].state = ReplyState::Cancelling;
                    }
                }
                self.notice="Stop is verified for the admitted turn; launch publication is still pending. Goal Pause is separate.".into();
            }
            Observation::WorkLaunchPending {
                action_id,
                generation,
                reason,
            } => {
                if !self
                    .launch
                    .as_ref()
                    .is_some_and(|p| p.action_id == action_id && p.generation == generation)
                {
                    return Err(io::Error::other(
                        "Pending launch notice changed its exact reservation",
                    ));
                }
                self.notice = reason.clone();
                self.work.notice = reason;
            }
            Observation::WorkLaunchFinished {
                action_id,
                update,
                stopped_operation,
                stop_verified,
            } => {
                let launch = self
                    .launch
                    .take()
                    .ok_or_else(|| io::Error::other("Unreserved Work launch result"))?;
                if launch.action_id != action_id || launch.generation != update.generation {
                    self.launch = Some(launch);
                    return Err(io::Error::other(
                        "Work launch result changed its local reservation",
                    ));
                }
                if launch.stop_requested && !stop_verified {
                    self.launch = Some(launch);
                    return Ok(());
                }
                self.work.observe(update);
                if let Some(operation) = stopped_operation {
                    if !launch.stop_requested
                        || !launch.stop_sent
                        || launch.stop_handed_off
                        || self.submission_id.as_ref() != Some(&operation)
                        || !self.busy
                    {
                        return Err(io::Error::other(
                            "Work launch Stop changed its admitted operation",
                        ));
                    }
                    self.stop_requests += 1;
                    self.stops_sent += 1;
                    if let Ok(index) = self.current_row_index() {
                        self.rows[index].state = ReplyState::Cancelling;
                    }
                }
                self.notice = self.work.notice.clone();
            }
            Observation::WorkExecution(update) => self.observe_work(*update)?,
            Observation::Compact(update) => {
                if self.compact.pending_key() != Some(update.key()) {
                    return Ok(());
                }
                if matches!(update, crate::conversation_compact::Update::Rejected { .. })
                    && self.compact_active
                {
                    if self.submission_id.is_some() {
                        return Err(io::Error::other(
                            "Dispatched compaction cannot be locally refused",
                        ));
                    }
                    self.compact_active = false;
                    self.rewrite_pending = false;
                    self.rewrite_old_cost = None;
                    self.busy = false;
                }
                if matches!(
                    update,
                    crate::conversation_compact::Update::Submitted { .. }
                ) && (!self.compact_active || !self.rewrite_pending || !self.busy)
                {
                    return Err(io::Error::other(
                        "Compaction dispatch without a reserved action",
                    ));
                }
                self.compact.observe(update)?;
                self.notice = self.compact.notice.clone();
            }
            Observation::CompactSettled(changed) => {
                if !self.compact_active
                    || !self.rewrite_pending
                    || self.compact.pending_key() != Some(&changed.key)
                    || self.submission_id.as_deref()
                        != Some(changed.admission.operation_id.as_str())
                    || !self.rewrite_maintenance.is_empty()
                    || self.pending_row().is_ok()
                    || !self.approvals.is_empty()
                    || !self.questions.is_empty()
                {
                    return Err(io::Error::other(
                        "Compaction readback does not match retired source work",
                    ));
                }
                let start = self
                    .rewrite_start_row
                    .ok_or_else(|| io::Error::other("Missing compaction row boundary"))?;
                if start > self.rows.len() {
                    return Err(io::Error::other("Invalid compaction row boundary"));
                }
                if changed.settlement.outcome == crate::conversation_compact::Outcome::Compacted
                    && changed.review.clear_display()
                {
                    self.rows.drain(..start);
                    self.rebase_operation_rows(start);
                    self.prior_context_rows = 0;
                    self.recovered_inputs.clear();
                    let (bytes, units) = self
                        .rewrite_old_cost
                        .take()
                        .ok_or_else(|| io::Error::other("Missing prior transcript cost"))?;
                    self.retained_bytes = self
                        .retained_bytes
                        .checked_sub(bytes)
                        .ok_or_else(|| io::Error::other("Invalid retained byte count"))?;
                    self.retained_units = self
                        .retained_units
                        .checked_sub(units)
                        .ok_or_else(|| io::Error::other("Invalid retained display count"))?;
                } else if changed.settlement.outcome
                    == crate::conversation_compact::Outcome::Compacted
                {
                    self.prior_context_rows = start;
                }
                self.rewrite_old_cost = None;
                self.runtime_state.opened(changed.snapshot);
                self.queue
                    .observe(crate::restored_queue::Update::Snapshot(changed.queue));
                let descriptor = changed
                    .history
                    .descriptor
                    .as_deref()
                    .ok_or_else(|| io::Error::other("Missing verified compact history"))?;
                let page = changed
                    .history
                    .page
                    .as_deref()
                    .ok_or_else(|| io::Error::other("Missing verified compact page"))?;
                self.saved_history
                    .install(descriptor.clone(), page.clone(), false)?;
                self.history = Some(changed.history);
                self.compact_active = false;
                self.rewrite_pending = false;
                self.busy = false;
                self.submission_id = None;
                self.rewrite_start_row = None;
                self.rewrite_stream_row = None;
                self.rewrite_segment = None;
                self.rewrite_rows.clear();
                self.rewrite_maintenance.clear();
                self.compact
                    .observe(crate::conversation_compact::Update::Settled {
                        key: changed.key,
                        outcome: changed.settlement.outcome,
                        reason: changed.settlement.explanation(),
                    })?;
                self.notice = self.compact.notice.clone();
            }
            Observation::Rewrite(update) => {
                if self.rewrite.pending_key() != Some(update.key()) {
                    return Ok(());
                }
                if matches!(update, crate::conversation_rewrite::Update::Rejected { .. })
                    && self.rewrite_pending
                {
                    if self.submission_id.is_some() {
                        return Err(io::Error::other(
                            "Dispatched edit/retry cannot be locally refused",
                        ));
                    }
                    let crate::conversation_rewrite::Update::Rejected { reason, .. } = &update
                    else {
                        unreachable!()
                    };
                    self.rewrite_pending = false;
                    self.rewrite_old_cost = None;
                    self.settle(crate::live::TerminalOutcome::Refused(reason.clone()))?;
                }
                if matches!(
                    update,
                    crate::conversation_rewrite::Update::Submitted { .. }
                ) && (!self.rewrite_pending || !self.busy)
                {
                    return Err(io::Error::other(
                        "Edit/retry dispatch without a reserved view turn",
                    ));
                }
                self.rewrite.observe(update)?;
                self.notice = self.rewrite.notice.clone();
            }
            Observation::RewriteSettled(changed) => {
                use crate::conversation_rewrite::{AdmissionOutcome, Update};
                if !self.rewrite_pending
                    || self.busy && !self.uncertainty_only_busy()
                    || self.rewrite.pending_key() != Some(&changed.key)
                    || self.submission_id.as_deref()
                        != Some(changed.admission.operation_id.as_str())
                {
                    return Err(io::Error::other(
                        "Edit/retry readback does not match the settled native turn",
                    ));
                }
                if changed.admission.outcome == AdmissionOutcome::Accepted {
                    let start = self
                        .rewrite_start_row
                        .ok_or_else(|| io::Error::other("Missing edit/retry execution rows"))?;
                    if start >= self.rows.len() {
                        return Err(io::Error::other("Missing retained rewrite rows"));
                    }
                    self.rows.drain(..start);
                    self.rebase_operation_rows(start);
                    self.prior_context_rows = 0;
                    self.recovered_inputs.clear();
                    let (bytes, units) = self
                        .rewrite_old_cost
                        .take()
                        .ok_or_else(|| io::Error::other("Missing prior transcript cost"))?;
                    self.retained_bytes = self
                        .retained_bytes
                        .checked_sub(bytes)
                        .ok_or_else(|| io::Error::other("Invalid retained rewrite byte count"))?;
                    self.retained_units =
                        self.retained_units.checked_sub(units).ok_or_else(|| {
                            io::Error::other("Invalid retained rewrite display count")
                        })?;
                } else {
                    self.rewrite_old_cost = None;
                }
                self.runtime_state.opened(changed.snapshot);
                self.queue
                    .observe(crate::restored_queue::Update::Snapshot(changed.queue));
                let descriptor = changed
                    .history
                    .descriptor
                    .as_deref()
                    .ok_or_else(|| io::Error::other("Missing verified rewrite history"))?;
                let page = changed
                    .history
                    .page
                    .as_deref()
                    .ok_or_else(|| io::Error::other("Missing verified rewrite history page"))?;
                self.saved_history
                    .install(descriptor.clone(), page.clone(), false)?;
                self.history = Some(changed.history);
                self.rewrite_pending = false;
                self.submission_id = None;
                self.rewrite_start_row = None;
                self.rewrite_stream_row = None;
                self.rewrite_segment = None;
                self.rewrite_rows.clear();
                self.rewrite_maintenance.clear();
                self.rewrite.observe(Update::Settled {
                    key: changed.key,
                    outcome: changed.admission.outcome,
                    reason: changed.admission.reason,
                })?;
                self.notice = self.rewrite.notice.clone();
            }
            Observation::RewriteSegment {
                operation_id,
                execution_id,
                index,
                previous_outcome,
            } => {
                self.check_rewrite_operation(&operation_id)?;
                if self.rewrite_rows.contains_key(&execution_id)
                    || index != self.rewrite_segment.map_or(0, |n| n + 1)
                {
                    return Err(io::Error::other("Unexpected rewrite display segment"));
                }
                if let Some(outcome) = previous_outcome {
                    self.settle(outcome)?;
                }
                if index == 0 && !self.compact_active {
                    if self.rewrite_segment.is_some() || self.pending_row().is_err() {
                        return Err(io::Error::other("Missing initial rewrite row"));
                    }
                } else {
                    if self.pending_row().is_ok() {
                        return Err(io::Error::other("Continuation before prior segment ended"));
                    }
                    let row = if let Some(segment) = self.work_segments.remove(&execution_id) {
                        if segment.operation != operation_id {
                            return Err(io::Error::other(
                                "Work segment belongs to another operation",
                            ));
                        }
                        self.append_work_row(segment.input)
                    } else {
                        self.append_rewrite_row("Conversation continuation", ReplyState::Sending)?
                    };
                    self.rewrite_stream_row = Some(row);
                }
                self.operation_rows
                    .insert(operation_id.clone(), self.current_row_index()?);
                self.rewrite_segment = Some(index);
                self.rewrite_rows
                    .insert(execution_id, self.current_row_index()?);
                self.busy = true;
                self.notice="Receiving the next source-owned rewrite segment; final saved state is not yet verified.".into();
            }
            Observation::RewriteAftercare {
                operation_id,
                aftercare_id,
                running,
                previous_outcome,
            } => {
                self.check_rewrite_operation(&operation_id)?;
                if running == self.rewrite_rows.contains_key(&aftercare_id)
                    || (!running && !self.rewrite_maintenance.contains(&aftercare_id))
                {
                    return Err(io::Error::other("Unexpected rewrite maintenance boundary"));
                }
                if let Some(outcome) = previous_outcome {
                    self.settle(outcome)?;
                }
                if running {
                    let row = self
                        .append_rewrite_row("Conversation compaction", ReplyState::Maintenance)?;
                    self.rewrite_rows.insert(aftercare_id.clone(), row);
                    self.rewrite_maintenance.insert(aftercare_id);
                } else {
                    let row = self
                        .rows
                        .get_mut(self.rewrite_rows[&aftercare_id])
                        .ok_or_else(|| io::Error::other("Missing maintenance row"))?;
                    row.state = ReplyState::MaintenanceSettled;
                    for tool in &mut row.tools {
                        tool.finish();
                    }
                    self.rewrite_maintenance.remove(&aftercare_id);
                }
                self.refresh_rewrite_busy();
                self.notice =
                    "Source follow-up work changed; waiting for final saved-history verification."
                        .into();
            }
            Observation::RewriteActivity {
                execution_id,
                activity,
            } => {
                let row = self.rewrite_event_row(&execution_id)?;
                self.activity_at(row, activity)?;
            }
            Observation::RewriteStatus { execution_id, text } => {
                let row = self.rewrite_event_row(&execution_id)?;
                if !self.rewrite_maintenance.contains(&execution_id) {
                    return Err(io::Error::other(
                        "Compaction status outside its active worker",
                    ));
                }
                let units = display_units(&text)?;
                self.replace_cost((0, 0), (text.len() + 1, units + 32))?;
                let card = &mut self.rows[row].cards[0];
                card.answer.push_str(&text);
                card.answer.push('\n');
                card.answer_units += units + 32;
                self.notice =
                    "Compaction status received; saved effects await independent verification."
                        .into();
            }
            Observation::SavedHistory(update) => {
                if self.saved_history.pending_key() == Some(update.key()) {
                    self.saved_history.observe(update)?;
                    self.notice = self.saved_history.notice.clone();
                }
            }
            Observation::EngineAdmission(admission) => {
                let first = self.engine_admission.is_none() && !self.ready;
                self.engine_admission = Some(admission);
                self.ready = admission == crate::engine_controls::Admission::PromptReady;
                self.runtime_state.engine_changed();
                self.notice = admission.label().into();
                if first {
                    self.engine.open();
                }
            }
            Observation::Engine(update) => {
                if let crate::engine_controls::Update::Settled { message, .. }
                | crate::engine_controls::Update::Rejected(message) = &update
                {
                    self.notice = message.clone();
                }
                self.engine.observe(update);
            }
            Observation::Commands(update) => {
                use crate::command_palette::Update;
                match update {
                    Update::Prepared { catalog, prepared } => {
                        match self.prompt_budget(&prepared.prompt) {
                            Ok(_) => self
                                .commands
                                .observe(Update::Prepared { catalog, prepared }),
                            Err(error) => {
                                self.commands.observe(Update::Rejected(error.to_string()))
                            }
                        }
                        self.notice = self.commands.notice.clone();
                    }
                    Update::Verified(prepared) => {
                        if !self.commands.accepts_verified(&prepared) {
                            self.commands.settle_cancelled();
                            return Ok(());
                        }
                        self.commands.close();
                        self.commands.waiting = false;
                        if let Err(error) = self.submit(&prepared.prompt) {
                            self.commands.open = true;
                            self.commands.observe(Update::Rejected(error.to_string()));
                            self.notice = self.commands.notice.clone();
                        } else {
                            self.commands.prepared = None;
                        }
                    }
                    other => {
                        self.commands.observe(other);
                        self.notice = self.commands.notice.clone();
                    }
                }
            }
            Observation::Theme(update) => {
                match &update {
                    crate::theme_controls::Update::Snapshot(s)
                    | crate::theme_controls::Update::Applied { snapshot: s, .. } => {
                        if let Some(row) = s.effective() {
                            self.effective_theme = Some(row.palette.theme(&row.name));
                            self.theme_status = format!(
                                "Theme: {} · source verified",
                                crate::live::terminal_text(&s.selection.effective)
                                    .replace('\n', "\\n")
                                    .replace('\t', "\\t")
                            );
                        } else {
                            self.effective_theme = None;
                            self.theme_status = format!(
                                "Theme: local fallback; {} has unsupported colors",
                                crate::live::terminal_text(&s.selection.effective)
                                    .replace('\n', "\\n")
                                    .replace('\t', "\\t")
                            );
                        }
                    }
                    crate::theme_controls::Update::Rejected(_)
                        if self.effective_theme.is_none() =>
                    {
                        self.theme_status = "Theme: local fallback; source unavailable".into();
                    }
                    _ => {}
                }
                self.themes.observe(update);
                self.notice = self.themes.notice.clone();
            }
            Observation::Work(update) => {
                if self.work.pending_generation() == Some(update.generation)
                    && matches!(update.data, crate::work_view::Data::Paused)
                {
                    self.runtime_state.schedules_paused();
                }
                self.work.observe(update);
                self.notice = self.work.notice.clone();
            }
            Observation::Settings(update) => {
                self.settings.observe(update);
                self.notice = self.settings.notice.clone();
            }
            Observation::Backend(update) => {
                self.backend_controls.observe(update)?;
                self.notice = self.backend_controls.notice.clone();
            }
            Observation::BackendChanged(changed) => {
                if !self.ready || self.busy || self.queue_pending || self.pending.is_some() {
                    return Err(io::Error::other("Backend changed while a turn was pending"));
                }
                self.backend_controls
                    .observe(crate::backend_controls::Update::Changed {
                        backend: changed.snapshot.backend.clone(),
                        model: changed.snapshot.model.clone(),
                    })?;
                self.backend = changed.snapshot.backend.clone();
                self.model = changed.snapshot.model.clone();
                self.runtime_state.opened(changed.snapshot);
                self.saved_history.finish();
                self.history = Some(changed.history);
                self.controls = crate::model_controls::View::default();
                self.browser = crate::conversation_browser::View::default();
                self.queue = crate::restored_queue::View::default();
                self.queue
                    .observe(crate::restored_queue::Update::Snapshot(changed.queue));
                self.notice =
                    "Backend and model verified; conversation and draft preserved.".into();
            }
            Observation::Browser(update) => {
                self.browser.observe(update)?;
                self.notice = self.browser.notice.clone();
            }
            Observation::ScheduledProposal(id) => {
                if self.schedule_proposal.is_some() || self.schedule_reserved {
                    return Err(io::Error::other("Repeated scheduled frontend proposal"));
                }
                self.schedule_proposal = Some(id);
            }
            Observation::ScheduledLocalNotSent { prompt, reason } => {
                let index = self.rows.len().checked_sub(1).ok_or_else(|| {
                    io::Error::other("Scheduled refusal has no forwarded local prompt")
                })?;
                let row = &self.rows[index];
                if !self.busy
                    || self.closed
                    || self.schedule_reserved
                    || self.schedule_prompt_held
                    || self.pending.is_some()
                    || self.submission_id.is_some()
                    || self.queue_pending
                    || self.rewrite_pending
                    || self.compact_active
                    || self.forwarded_row.is_some()
                    || !self.work_admissions.is_empty()
                    || !self.work_uncertain.is_empty()
                    || self.operation_rows.values().any(|owned| *owned == index)
                    || row.source_owned
                    || row.source.is_some()
                    || !row.inputs.is_empty()
                    || row.committed.is_some()
                    || self.stops_sent != 0
                    || !(row.state == ReplyState::Sending
                        || row.state == ReplyState::Cancelling && self.stop_requests > 0)
                    || row.prompt != prompt
                {
                    return Err(io::Error::other(
                        "Scheduled refusal changed its exact unsubmitted local prompt",
                    ));
                }
                self.settle(crate::live::TerminalOutcome::Refused(reason))?;
                // Proposal and rejection can drain in one UI batch. Keep this
                // firing declined until its exact Held readback, even though
                // the unsent local row is no longer busy. Never replay it.
                self.schedule_prompt_held = true;
                self.ready = false;
            }
            Observation::ScheduledHeld(reason) => {
                if (self.schedule_reserved || self.schedule_prompt_held)
                    && !self.quitting
                    && !self.closed
                {
                    self.ready = true;
                }
                self.schedule_reserved = false;
                self.schedule_prompt_held = false;
                self.schedule_proposal = None;
                self.notice = reason;
            }
            Observation::ScheduledConversationChanged(changed) => {
                if !self.schedule_reserved
                    || self.schedule_prompt_held
                    || self.busy
                    || self.queue_pending
                    || self.pending.is_some()
                {
                    return Err(io::Error::other(
                        "Scheduled conversation changed without an idle frontend reservation",
                    ));
                }
                self.schedule_reserved = false;
                self.ready = true;
                self.install_conversation(changed.snapshot, changed.history, changed.queue);
                self.browser = crate::conversation_browser::View::default();
                self.work.finish();
                self.settings.finish();
                self.commands.finish();
                self.backend_controls.finish();
                self.notice = "Scheduled conversation verified; waiting for the captured job input. Composer kept.".into();
            }
            Observation::ConversationChanged(changed) => {
                if !self.ready || self.busy || self.queue_pending || self.pending.is_some() {
                    return Err(io::Error::other(
                        "Conversation mutated while a turn was pending",
                    ));
                }
                self.browser.action_completed(&changed)?;
                if changed.action == crate::conversation_actions::Action::New {
                    self.install_conversation(changed.snapshot, changed.history, changed.queue);
                    self.notice =
                        "New conversation verified. Composer kept; no prompt sent.".into();
                } else {
                    self.queue
                        .observe(crate::restored_queue::Update::Snapshot(changed.queue));
                    self.notice = format!("Conversation renamed: {}", changed.current.title);
                }
            }
            Observation::Opened(opened) => {
                if !self.ready || self.busy || self.queue_pending || self.pending.is_some() {
                    return Err(io::Error::other(
                        "Conversation opened while a turn was pending",
                    ));
                }
                self.browser
                    .observe(crate::conversation_browser::Update::Opened {
                        session: opened.snapshot.session.clone(),
                    })?;
                self.install_conversation(opened.snapshot, opened.history, opened.queue);
                self.notice =
                    "Conversation opened and held input verified. Nothing replayed.".into();
            }
            Observation::History(history) => {
                if self.ready || self.busy || !self.rows.is_empty() || self.history.is_some() {
                    return Err(io::Error::other("Saved history arrived outside startup"));
                }
                self.retained_bytes = history.bytes;
                self.retained_units = history.units;
                if let (Some(descriptor), Some(page)) =
                    (history.descriptor.as_deref(), history.page.as_deref())
                {
                    self.saved_history
                        .install(descriptor.clone(), page.clone(), false)?;
                    self.notice = format!(
                        "Saved context verified: {} messages. F6 opens Saved History.",
                        descriptor.counts.messages
                    );
                } else {
                    self.notice = format!("Restored {} saved messages.", history.messages.len());
                }
                self.history = Some(history);
            }
            Observation::Submission { id } => {
                if !self.compact_active {
                    self.pending_row()?;
                }
                if self.submission_id.is_some() || self.operation_rows.contains_key(&id) {
                    return Err(io::Error::other("Duplicate local prompt reservation"));
                }
                if !self.compact_active {
                    self.operation_rows
                        .insert(id.clone(), self.current_row_index()?);
                }
                self.submission_id = Some(id);
            }
            Observation::ApprovalSettled { event, outcome } => {
                self.approvals.observe(event)?;
                self.settle(outcome)?;
            }
            Observation::State(update) => self.runtime_state.apply(update),
            Observation::Queue(update) => {
                if let crate::restored_queue::Update::Snapshot(snapshot) = &update
                    && let Some(entry) = &snapshot.recovered
                {
                    self.record_reconciled(entry)?;
                }
                if let crate::restored_queue::Update::ContinuationRejected(reason) = &update {
                    if !self.queue_pending || !self.busy || self.submission_id.is_some() {
                        return Err(io::Error::other("Unexpected local continuation refusal"));
                    }
                    self.settle(crate::live::TerminalOutcome::Refused(reason.clone()))?;
                    self.queue_pending = false;
                } else if matches!(update, crate::restored_queue::Update::Snapshot(_)) && !self.busy
                {
                    self.queue_pending = false;
                }
                self.queue.observe(update);
                self.notice = self.queue.notice.clone();
            }
            Observation::Controls(update) => {
                if let crate::model_controls::Update::Choices(menu) = &update {
                    self.model = menu.model.clone();
                }
                self.controls.observe(update);
                self.notice = self.controls.notice.clone();
            }
            Observation::Approval(event) => {
                if matches!(event, crate::approvals::Event::Requested(_))
                    && self.pending_row().is_err()
                    && self.rewrite_maintenance.is_empty()
                {
                    return Err(io::Error::other("Approval without active source work"));
                }
                self.notice = self.approvals.observe(event)?;
                self.refresh_rewrite_busy();
            }
            Observation::Question { event, settled } => {
                if matches!(event, crate::questions::Event::Requested(_))
                    && self.pending_row().is_err()
                    && self.rewrite_maintenance.is_empty()
                {
                    return Err(io::Error::other("Question without active source work"));
                }
                self.notice = self.questions.observe(event)?;
                self.refresh_rewrite_busy();
                if let Some(outcome) = settled {
                    self.settle(outcome)?;
                }
            }
            Observation::Ready => {
                self.ready = true;
                self.notice = format!(
                    "Connected. Enter sends; Ctrl+Q quits. {}",
                    self.runtime_state
                        .tool_description()
                        .unwrap_or_else(|| "Tool policy unavailable.".into())
                );
                if let Some(held) = self
                    .runtime_state
                    .startup()
                    .and_then(|s| s.held_restored_input)
                    && held > 0
                {
                    self.notice
                        .push_str(&format!(" Restored input held: {held}. Nothing replayed."));
                }
            }
            Observation::Content { card, kind, text } => self.content(Some(card), kind, text)?,
            Observation::Text(text) => self.content(None, ContentKind::AnswerDelta, text)?,
            Observation::Snapshot(text) => self.content(None, ContentKind::AnswerSnapshot, text)?,
            Observation::Reasoning(text) => {
                self.content(None, ContentKind::ReasoningDelta, text)?
            }
            Observation::ReasoningSnapshot(text) => {
                self.content(None, ContentKind::ReasoningSnapshot, text)?
            }
            Observation::Activity(activity) => self.activity(activity)?,
            Observation::ConversationUnverified(info) => {
                self.conversation_exit_notice = Some(info.clone());
                self.notice = info;
            }
            Observation::Information(info) => {
                self.notice = info;
            }
            Observation::ApprovalStopped(reason) => {
                self.settle(crate::live::TerminalOutcome::ApprovalStopped(reason))?
            }
            Observation::Cancelled(reason) => {
                self.settle(crate::live::TerminalOutcome::Cancelled(reason))?
            }
            Observation::Refused(reason) => {
                self.settle(crate::live::TerminalOutcome::Refused(reason))?
            }
            Observation::Failed(reason) => {
                self.settle(crate::live::TerminalOutcome::Failed(reason))?
            }
            Observation::Complete => self.settle(crate::live::TerminalOutcome::Complete)?,
        }
        if let Some(reason) = self.work_uncertain.values().next() {
            self.busy = true;
            if !self.quitting {
                self.notice = reason.clone();
            }
        }
        Ok(())
    }
    fn rebase_operation_rows(&mut self, start: usize) {
        self.operation_rows.retain(|_, row| {
            if *row < start {
                false
            } else {
                *row -= start;
                true
            }
        });
    }
    fn retain_work_busy(&mut self) {
        self.busy |= !self.work_uncertain.is_empty()
            || !self.work_admissions.is_empty()
            || !self.work_ended.is_empty()
            || !self.suspended_rows.is_empty()
            || self.forwarded_row.is_some();
    }
    pub(crate) fn uncertainty_only_busy(&self) -> bool {
        !self.work_uncertain.is_empty()
            && self.pending_row().is_err()
            && !self.compact_active
            && self.rewrite_maintenance.is_empty()
            && self.work_admissions.is_empty()
            && self.work_segments.is_empty()
            && self.work_ended.is_empty()
            && self.suspended_rows.is_empty()
            && self.forwarded_row.is_none()
            && self.approvals.is_empty()
            && self.questions.is_empty()
    }
    fn work_row(&self, operation: &str) -> io::Result<usize> {
        self.operation_rows
            .get(operation)
            .copied()
            .filter(|i| *i < self.rows.len())
            .ok_or_else(|| io::Error::other("Work receipt has no exact operation row"))
    }
    fn append_work_row(&mut self, input: WorkInput) -> usize {
        let index = self.rows.len();
        let (prompt, committed) = match input.delivery {
            crate::work_lifecycle::Delivery::Append => (input.text, None),
            crate::work_lifecycle::Delivery::CommittedHistory(history) => (
                String::new(),
                Some(CommittedWork {
                    status: input.text,
                    history,
                }),
            ),
        };
        self.rows.push(Row {
            source_owned: true,
            source: Some(input.source),
            inputs: Vec::new(),
            committed,
            prompt,
            cards: vec![Card::default()],
            tools: Vec::new(),
            items: vec![Item::Card(0)],
            state: ReplyState::Sending,
            stop_reason: None,
        });
        index
    }
    fn finish_work_row(
        &mut self,
        index: usize,
        outcome: crate::live::TerminalOutcome,
        paid: bool,
    ) -> io::Result<()> {
        use crate::live::TerminalOutcome;
        if !matches!(
            self.rows[index].state,
            ReplyState::Sending
                | ReplyState::Streaming
                | ReplyState::Cancelling
                | ReplyState::AwaitingRetirement
        ) {
            return Err(io::Error::other(
                "Work receipt repeated an already settled row",
            ));
        }
        let (state, reason) = match outcome {
            TerminalOutcome::Complete => (ReplyState::Complete, None),
            TerminalOutcome::Refused(s) => (ReplyState::Refused, Some(s)),
            TerminalOutcome::Failed(s) => (ReplyState::Failed, Some(s)),
            TerminalOutcome::Cancelled(s) => (ReplyState::Cancelled, Some(s)),
            TerminalOutcome::ApprovalStopped(s) => (ReplyState::ApprovalStopped, Some(s)),
        };
        if !paid {
            let reason = reason.as_deref().unwrap_or("");
            self.replace_cost((0, 0), (reason.len(), display_units(reason)?))?;
        }
        let row = &mut self.rows[index];
        row.state = state;
        row.stop_reason = reason;
        for tool in &mut row.tools {
            tool.finish();
        }
        Ok(())
    }
    fn observe_work(&mut self, update: crate::live::WorkExecution) -> io::Result<()> {
        use crate::{
            live::{TerminalOutcome, WorkExecution},
            work_lifecycle::Mode,
        };
        match update {
            WorkExecution::Admitted {
                input,
                admission,
                payload,
                suspended_submission,
            } => {
                if !self.ready
                    || !self.work_uncertain.is_empty()
                    || self.quitting
                    || input.id != admission.input
                    || input.source != payload.source
                    || input.requested != payload.profile
                    || admission.requested != payload.profile
                    || admission.operation != admission.origin.operation
                    || self.work_seen.contains(&admission.id)
                    || self.work_seen.len() >= 4096
                    || self.work_admissions.len() >= 256
                    || [
                        &input.id,
                        &admission.id,
                        &admission.operation,
                        &admission.origin.execution,
                        &payload.source,
                    ]
                    .iter()
                    .any(|s| s.is_empty() || s.len() > 128)
                {
                    return Err(io::Error::other(
                        "Unexpected or repeated Work display admission",
                    ));
                }
                let (reference_bytes, reference_units) = match &payload.delivery {
                    crate::work_lifecycle::Delivery::Append => (0, 0),
                    crate::work_lifecycle::Delivery::CommittedHistory(history) => {
                        if admission.mode != Mode::NewTurn
                            || payload.source != "child-result"
                            || !payload.content.is_empty()
                            || history.receipt_ids.is_empty()
                            || history.receipt_ids.len() > 256
                        {
                            return Err(io::Error::other(
                                "Committed Work history cannot authorize a user append",
                            ));
                        }
                        let mut bytes = history.session.len()
                            + history.transcript_sha256.len()
                            + history.context_sha256.len();
                        let mut units = 96usize;
                        for id in &history.receipt_ids {
                            bytes = bytes.saturating_add(id.len());
                            units = units.saturating_add(display_units(id)?).saturating_add(64);
                        }
                        (bytes, units)
                    }
                };
                let text = payload.display_text();
                let units = display_units(text)?
                    .saturating_add(display_units(&payload.source)?)
                    .saturating_add(reference_units)
                    .saturating_add(320);
                // Only the exact display text is retained. Hidden content is not duplicated.
                // Fixed metadata allowance covers all bounded identity keys held by this admission.
                let bytes = text
                    .len()
                    .saturating_add(payload.source.len())
                    .saturating_add(reference_bytes)
                    .saturating_add(1536);
                let current = self.current_row_index().ok();
                match admission.mode {
                    Mode::NewTurn => {
                        if self.rewrite_pending
                            || self.rows.len() >= 100
                            || self.operation_rows.contains_key(&admission.operation)
                            || !self.approvals.is_empty()
                            || !self.questions.is_empty()
                        {
                            return Err(io::Error::other(
                                "Work NewTurn conflicts with current display ownership",
                            ));
                        }
                        if let Some(id) = suspended_submission.as_deref() {
                            let row = self.work_row(id)?;
                            if self.submission_id.as_deref() != Some(id)
                                || self.rows[row].source_owned
                                || self.rows[row].state != ReplyState::Sending
                                || !self.suspended_rows.is_empty()
                            {
                                return Err(io::Error::other(
                                    "Work displaced a different local prompt",
                                ));
                            }
                        } else if let Some(row) = current
                            && (matches!(
                                self.rows[row].state,
                                ReplyState::Streaming | ReplyState::Cancelling
                            ) || self.rows[row].state == ReplyState::Sending
                                && (self.rows[row].source_owned
                                    || self.submission_id.is_some()
                                    || self.forwarded_row.is_some()))
                        {
                            return Err(io::Error::other(
                                "Work NewTurn arrived before the current stream ended",
                            ));
                        }
                    }
                    Mode::InTurn => {
                        let row = self.work_row(&admission.operation)?;
                        if self.submission_id.as_deref() != Some(&admission.operation)
                            || current != Some(row)
                            || self.pending_row().is_err()
                            || self.rows[row].inputs.len() >= 100
                            || suspended_submission.is_some()
                        {
                            return Err(io::Error::other("Work input has no active operation row"));
                        }
                    }
                    Mode::ActionSegment => {
                        self.check_rewrite_operation(&admission.operation)?;
                        if self.work_segments.contains_key(&admission.origin.execution)
                            || self.rewrite_rows.contains_key(&admission.origin.execution)
                            || self.rows.len() + self.work_segments.len() >= 100
                            || suspended_submission.is_some()
                        {
                            return Err(io::Error::other(
                                "Work action segment has no new execution",
                            ));
                        }
                    }
                }
                self.replace_cost((0, 0), (bytes, units))?;
                let display = WorkInput {
                    text: text.into(),
                    source: WorkSource {
                        source: payload.source,
                        requested: payload.profile,
                        effective: admission.effective,
                    },
                    delivery: payload.delivery,
                };
                match admission.mode {
                    Mode::NewTurn => {
                        if let Some(id) = suspended_submission {
                            self.suspended_rows.insert(id.clone(), self.work_row(&id)?);
                        } else if let Some(row) = current
                            && self.rows[row].state == ReplyState::Sending
                        {
                            if self.pending.is_some() {
                                self.finish_work_row(row, TerminalOutcome::Refused("Not sent: source work arrived before local submission. Submit again explicitly if still wanted.".into()), false)?;
                            } else {
                                self.forwarded_row = Some(row);
                            }
                        }
                        self.pending = None;
                        let row = self.append_work_row(display);
                        self.operation_rows.insert(admission.operation.clone(), row);
                        self.submission_id = Some(admission.operation.clone());
                        self.rewrite_stream_row = None;
                        self.stop_requests = 0;
                        self.stops_sent = 0;
                        self.stop_confirm = false;
                        self.busy = true;
                        self.notice = "Source work admitted; receiving its exact input.".into();
                    }
                    Mode::InTurn => {
                        let row = &mut self.rows[current.unwrap()];
                        row.items.push(Item::WorkInput(row.inputs.len()));
                        row.inputs.push(display);
                        self.notice = "Source input admitted into the current turn.".into();
                    }
                    Mode::ActionSegment => {
                        self.work_segments.insert(
                            admission.origin.execution,
                            PendingWorkSegment {
                                operation: admission.operation.clone(),
                                admission: admission.id.clone(),
                                input: display,
                            },
                        );
                    }
                }
                self.work_seen.insert(admission.id.clone());
                self.work_admissions
                    .insert(admission.id, admission.operation);
            }
            WorkExecution::Ended { operation, outcome } => {
                let row = self.work_row(&operation)?;
                if self.work_ended.contains_key(&operation)
                    || !matches!(
                        self.rows[row].state,
                        ReplyState::Sending | ReplyState::Streaming | ReplyState::Cancelling
                    )
                {
                    return Err(io::Error::other("Unexpected Work stream ending"));
                }
                let reason = match &outcome {
                    TerminalOutcome::Complete => "",
                    TerminalOutcome::Refused(s)
                    | TerminalOutcome::Failed(s)
                    | TerminalOutcome::Cancelled(s)
                    | TerminalOutcome::ApprovalStopped(s) => s,
                };
                self.replace_cost((0, 0), (reason.len(), display_units(reason)?))?;
                self.rows[row].state = ReplyState::AwaitingRetirement;
                self.work_ended.insert(operation, outcome);
                self.notice = "Stream ended; awaiting independent source worker retirement.".into();
            }
            WorkExecution::Retired { admission_id } => {
                if self.work_admissions.remove(&admission_id).is_none() {
                    return Err(io::Error::other("Unknown or repeated Work retirement"));
                }
            }
            WorkExecution::Settled { operation, outcome } => {
                let row = self.work_row(&operation)?;
                if self.work_admissions.values().any(|op| op == &operation)
                    || self.work_ended.get(&operation) != Some(&outcome)
                    || self.submission_id.as_deref() == Some(&operation)
                        && (!self.approvals.is_empty() || !self.questions.is_empty())
                {
                    return Err(io::Error::other(
                        "Work settlement differs from exact retired operation",
                    ));
                }
                self.finish_work_row(row, outcome, true)?;
                self.work_ended.remove(&operation);
                if self.submission_id.as_deref() == Some(&operation) {
                    self.submission_id = None;
                    self.stop_confirm = false;
                    self.busy = false;
                    self.notice = "Source turn and worker retirement verified.".into();
                }
            }
            WorkExecution::NotStarted {
                operation,
                execution,
                outcome,
            } => {
                self.check_rewrite_operation(&operation)?;
                let segment = self.work_segments.get(&execution).ok_or_else(|| {
                    io::Error::other("Not-started Work has no exact held execution")
                })?;
                if segment.operation != operation
                    || self.work_admissions.contains_key(&segment.admission)
                    || self.work_uncertain.is_empty()
                    || !matches!(
                        outcome,
                        TerminalOutcome::Cancelled(_) | TerminalOutcome::Failed(_)
                    )
                {
                    return Err(io::Error::other(
                        "Not-started Work lacks verified unsuccessful retirement",
                    ));
                }
                let segment = self
                    .work_segments
                    .remove(&execution)
                    .expect("verified held input");
                let row = self.append_work_row(segment.input);
                self.rows[row].cards.clear();
                self.rows[row].items.clear();
                self.finish_work_row(row, outcome, false)?;
            }
            WorkExecution::LocalRefused { operation, reason } => {
                let row = self
                    .suspended_rows
                    .get(&operation)
                    .copied()
                    .ok_or_else(|| {
                        io::Error::other("Local refusal has no exact displaced prompt")
                    })?;
                self.finish_work_row(row, TerminalOutcome::Refused(reason), false)?;
                self.suspended_rows.remove(&operation);
            }
            WorkExecution::LocalNotSent { reason } => {
                let row = self
                    .forwarded_row
                    .ok_or_else(|| io::Error::other("Local refusal has no parked unsent prompt"))?;
                self.finish_work_row(row, TerminalOutcome::Refused(reason), false)?;
                self.forwarded_row = None;
            }
            WorkExecution::Reconciled {
                input_id,
                recovered,
            } => {
                if (!recovered && !self.work_uncertain.contains_key(&input_id))
                    || (self.busy && !self.uncertainty_only_busy())
                {
                    return Err(io::Error::other(
                        "History reconciliation cannot release active work",
                    ));
                }
                if let Some(reason) = self.work_uncertain.remove(&input_id) {
                    self.replace_cost(
                        (
                            reason.len().saturating_mul(2) + input_id.len(),
                            display_units(&reason)?,
                        ),
                        (0, 0),
                    )?;
                }
                self.notice =
                    format!("Provider history confirmed input {input_id}; nothing resent.");
            }
            WorkExecution::Uncertain { input_id, reason } => {
                if input_id.is_empty() || input_id.len() > 128 {
                    return Err(io::Error::other("Invalid uncertain input identity"));
                }
                // An uncertain input can win the same local-dispatch race as an
                // accepted NewTurn, without creating any new operation or row.
                if self.submission_id.is_none()
                    && let Some(row) = self.rows.len().checked_sub(1)
                    && !self.rows[row].source_owned
                    && self.rows[row].state == ReplyState::Sending
                {
                    if self.pending.is_some() {
                        self.finish_work_row(row, TerminalOutcome::Refused("Not sent: source input delivery became uncertain before local submission. Inspect Work before continuing.".into()), false)?;
                        self.pending = None;
                    } else if self.forwarded_row.is_none() {
                        self.forwarded_row = Some(row);
                    }
                }
                let notice = format!(
                    "Source input {input_id}: {reason} · delivery uncertain; F1 Work, F7 held input, and Ctrl+Q remain available."
                );
                if input_id.len() > 128
                    || self.work_uncertain.len() >= 256
                    || self.work_uncertain.contains_key(&input_id)
                {
                    return Err(io::Error::other(
                        "Duplicate or excessive Work input uncertainty",
                    ));
                }
                self.replace_cost(
                    (0, 0),
                    (
                        notice.len().saturating_mul(2) + input_id.len(),
                        display_units(&notice)?,
                    ),
                )?;
                self.work_uncertain.insert(input_id, notice.clone());
                self.notice = notice;
            }
        }
        // A receipt for an older operation must never release the current stream.
        self.busy = self.pending_row().is_ok()
            || self.compact_active
            || !self.rewrite_maintenance.is_empty();
        self.retain_work_busy();
        Ok(())
    }
    fn check_rewrite_operation(&self, operation: &str) -> io::Result<()> {
        if !self.rewrite_pending || self.submission_id.as_deref() != Some(operation) || self.closed
        {
            Err(io::Error::other(
                "Rewrite display event belongs to another operation",
            ))
        } else {
            Ok(())
        }
    }
    fn append_rewrite_row(&mut self, prompt: &str, state: ReplyState) -> io::Result<usize> {
        let units = self.prompt_budget(prompt)?;
        self.retained_units += units;
        self.retained_bytes += prompt.len();
        let index = self.rows.len();
        self.rows.push(Row {
            source_owned: true,
            source: None,
            inputs: Vec::new(),
            committed: None,
            prompt: prompt.into(),
            cards: vec![Card::default()],
            tools: Vec::new(),
            items: vec![Item::Card(0)],
            state,
            stop_reason: None,
        });
        Ok(index)
    }
    fn rewrite_event_row(&self, execution: &str) -> io::Result<usize> {
        let row = self
            .rewrite_rows
            .get(execution)
            .copied()
            .filter(|i| *i < self.rows.len())
            .ok_or_else(|| io::Error::other("Rewrite observation has no retained execution row"))?;
        if !self.rewrite_pending
            || !(self.rewrite_maintenance.contains(execution)
                || self.rewrite_stream_row == Some(row) && self.pending_row().is_ok())
        {
            return Err(io::Error::other(
                "Rewrite observation arrived after worker settlement",
            ));
        }
        Ok(row)
    }
    fn refresh_rewrite_busy(&mut self) {
        if self.rewrite_pending {
            self.busy = self.compact_active
                || self.pending_row().is_ok()
                || !self.rewrite_maintenance.is_empty()
                || !self.approvals.is_empty()
                || !self.questions.is_empty();
        }
    }
    fn current_row_index(&self) -> io::Result<usize> {
        if self.compact_active && self.rewrite_stream_row.is_none() {
            return Err(io::Error::other("Compaction has no user stream row"));
        }
        self.rewrite_stream_row
            .or_else(|| {
                self.submission_id
                    .as_ref()
                    .and_then(|id| self.operation_rows.get(id))
                    .copied()
            })
            .or_else(|| self.rows.len().checked_sub(1))
            .filter(|i| *i < self.rows.len())
            .ok_or_else(|| io::Error::other("Missing live row"))
    }
    fn settle(&mut self, outcome: crate::live::TerminalOutcome) -> io::Result<()> {
        // Retirement may be verified before the current question/approval/abort
        // receipt. Its later ordinary terminal still binds the stored operation.
        if let Some(operation) = self.submission_id.as_ref()
            && self.work_ended.contains_key(operation)
        {
            return self.observe_work(crate::live::WorkExecution::Settled {
                operation: operation.clone(),
                outcome,
            });
        }
        let (state, stop_reason) = match outcome {
            crate::live::TerminalOutcome::Complete => (ReplyState::Complete, None),
            crate::live::TerminalOutcome::Refused(reason) => (ReplyState::Refused, Some(reason)),
            crate::live::TerminalOutcome::Failed(reason) => (ReplyState::Failed, Some(reason)),
            crate::live::TerminalOutcome::ApprovalStopped(reason) => {
                (ReplyState::ApprovalStopped, Some(reason))
            }
            crate::live::TerminalOutcome::Cancelled(reason) => {
                (ReplyState::Cancelled, Some(reason))
            }
        };
        if !self.approvals.is_empty() || !self.questions.is_empty() {
            return Err(io::Error::other("Turn settlement with pending input"));
        }
        self.pending_row()?;
        if !self.rewrite_pending
            && self
                .submission_id
                .as_ref()
                .is_some_and(|id| self.work_admissions.values().any(|op| op == id))
        {
            return Err(io::Error::other(
                "Work turn settlement preceded worker retirement",
            ));
        }
        let reason = stop_reason.as_deref().unwrap_or("");
        let units = display_units(reason)?;
        if self.retained_units.saturating_add(units) > DISPLAY_UNITS
            || self.retained_bytes.saturating_add(reason.len()) > RETAINED_BYTES
        {
            return Err(io::Error::other("Live transcript/display limit exceeded"));
        }
        self.retained_units += units;
        self.retained_bytes += reason.len();
        let index = self.current_row_index()?;
        let row = &mut self.rows[index];
        row.state = state;
        row.stop_reason = stop_reason;
        for tool in &mut row.tools {
            tool.finish();
        }
        self.busy = false;
        self.stop_confirm = false;
        // Retain the exact operation until the independent rewrite readback.
        if !self.rewrite_pending {
            self.submission_id = None;
        }
        self.notice = if self.rewrite_pending {
            "Turn settled; verifying edit/retry history and worker retirement."
        } else if self.queue_pending {
            "Turn ended. Checking saved input before another prompt."
        } else if row.state == ReplyState::Refused {
            "Input was not started. Submit again explicitly to continue; Ctrl+Q quits."
        } else if row.state == ReplyState::Failed {
            "Turn failed. Partial output retained. Check recovery before retrying; Ctrl+Q quits."
        } else if row.state == ReplyState::Cancelled {
            "Turn cancelled. Partial output retained. Type another prompt; Ctrl+Q quits."
        } else if row.state == ReplyState::ApprovalStopped {
            "Turn stopped for approval. Type another prompt; Ctrl+Q quits."
        } else {
            "Reply completed. Type another prompt; Ctrl+Q quits."
        }
        .into();
        self.refresh_rewrite_busy();
        self.retain_work_busy();
        Ok(())
    }
    fn pending_row(&self) -> io::Result<&Row> {
        self.rows
            .get(self.current_row_index()?)
            .filter(|row| {
                matches!(
                    row.state,
                    ReplyState::Sending | ReplyState::Streaming | ReplyState::Cancelling
                )
            })
            .ok_or_else(|| io::Error::other("Content without a pending live row"))
    }
    fn replace_cost(&mut self, old: (usize, usize), new: (usize, usize)) -> io::Result<()> {
        let bytes = self
            .retained_bytes
            .saturating_sub(old.0)
            .saturating_add(new.0);
        let units = self
            .retained_units
            .saturating_sub(old.1)
            .saturating_add(new.1);
        if bytes > RETAINED_BYTES {
            return Err(io::Error::other("Live transcript text limit exceeded"));
        }
        if units > DISPLAY_UNITS {
            return Err(io::Error::other("Live display-line/work limit exceeded"));
        }
        self.retained_bytes = bytes;
        self.retained_units = units;
        Ok(())
    }
    fn content(&mut self, index: Option<usize>, kind: ContentKind, text: String) -> io::Result<()> {
        let units = display_units(&text)?;
        let row = self.pending_row()?;
        if index.is_none() && !row.tools.is_empty() && !self.rewrite_pending {
            return Err(io::Error::other(
                "Content without native display ownership after tools",
            ));
        }
        let index = index.unwrap_or(0);
        if index > row.cards.len() || index >= 100 {
            return Err(io::Error::other("Invalid native card sequence"));
        }
        let new_card = index == row.cards.len();
        let old = if kind.replaces() {
            row.cards
                .get(index)
                .map(|card| {
                    if kind.reasoning() {
                        (card.reasoning.len(), card.reasoning_units)
                    } else {
                        (card.answer.len(), card.answer_units)
                    }
                })
                .unwrap_or_default()
        } else {
            (0, 0)
        };
        self.replace_cost(old, (text.len(), units + if new_card { 320 } else { 0 }))?;
        let row_index = self.current_row_index()?;
        let row = &mut self.rows[row_index];
        if new_card {
            row.cards.push(Card::default());
            row.items.push(Item::Card(index));
        }
        let card = &mut row.cards[index];
        let (body, work) = if kind.reasoning() {
            (&mut card.reasoning, &mut card.reasoning_units)
        } else {
            (&mut card.answer, &mut card.answer_units)
        };
        if kind.replaces() {
            *body = text;
            *work = units;
        } else {
            body.push_str(&text);
            *work += units;
        }
        if row.state != ReplyState::Cancelling {
            row.state = ReplyState::Streaming;
        }
        self.notice = "Receiving real runtime content…".into();
        Ok(())
    }
    fn activity(&mut self, activity: Activity) -> io::Result<()> {
        self.pending_row()?;
        self.activity_at(self.current_row_index()?, activity)
    }
    fn activity_at(&mut self, index: usize, activity: Activity) -> io::Result<()> {
        let row = &self.rows[index];
        let existing = row.tools.iter().position(|tool| tool.id == activity.id);
        // A first observation mounts after the current card. Later results keep
        // their original position, even when other cards have since arrived.
        if activity.card >= row.cards.len()
            || (existing.is_none() && activity.card + 1 != row.cards.len())
        {
            return Err(io::Error::other(
                "Native tool has invalid display ownership",
            ));
        }
        if existing.is_none() && row.tools.len() >= 100 {
            return Err(io::Error::other("Native tool count limit exceeded"));
        }
        let old = existing.map(|i| &row.tools[i]);
        let cost = |tool: &Tool| -> io::Result<(usize, usize)> {
            tool.texts()
                .iter()
                .try_fold((0, 320), |(bytes, units), text| {
                    Ok((bytes + text.len(), units + display_units(text)?))
                })
        };
        let old_cost = old.map(cost).transpose()?.unwrap_or_default();
        let tool = Tool::updated(old, activity)?;
        let new_cost = cost(&tool)?;
        self.replace_cost(old_cost, new_cost)?;
        let row = &mut self.rows[index];
        if let Some(index) = existing {
            row.tools[index] = tool;
        } else {
            row.items.push(Item::Tool(row.tools.len()));
            row.tools.push(tool);
        }
        if !matches!(row.state, ReplyState::Cancelling | ReplyState::Maintenance) {
            row.state = ReplyState::Streaming;
        }
        self.notice = "Native tool observation (not an approval or dispatch)".into();
        Ok(())
    }
    pub fn begin_quit(&mut self) {
        self.runtime_controls.close();
        self.controls.finish();
        self.queue.finish();
        self.engine.finish();
        self.commands.finish();
        self.themes.finish();
        self.saved_history.finish();
        self.work.finish();
        self.settings.finish();
        self.backend_controls.finish();
        self.browser.finish();
        self.quitting = true;
        self.notice = "Closing Python runtime and its owned process tree…".into();
    }
    pub fn finish(&mut self, failure: Option<String>) {
        self.runtime_controls.close();
        self.controls.finish();
        self.queue.finish();
        self.engine.finish();
        self.commands.finish();
        self.themes.finish();
        self.saved_history.finish();
        self.work.finish();
        self.settings.finish();
        self.backend_controls.finish();
        self.browser.finish();
        self.queue_pending = false;
        self.rewrite_pending = false;
        self.rewrite_old_cost = None;
        self.rewrite.finish();
        self.compact.finish();
        self.compact_active = false;
        self.rewrite_start_row = None;
        self.rewrite_stream_row = None;
        self.rewrite_segment = None;
        self.rewrite_rows.clear();
        self.rewrite_maintenance.clear();
        self.closed = true;
        self.stop_confirm = false;
        self.ready = false;
        self.busy = false;
        self.pending = None;
        self.approvals.clear();
        self.questions.clear();
        for row in &mut self.rows {
            for tool in &mut row.tools {
                tool.finish();
            }
            if !matches!(
                row.state,
                ReplyState::Complete
                    | ReplyState::ApprovalStopped
                    | ReplyState::Cancelled
                    | ReplyState::Refused
                    | ReplyState::Failed
                    | ReplyState::Reconciled
                    | ReplyState::MaintenanceSettled
            ) {
                row.state = ReplyState::Incomplete;
            }
        }
        self.notice =
            failure.unwrap_or_else(|| "Python runtime and contained process tree exited.".into());
    }
}
