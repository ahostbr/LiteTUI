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
}
pub struct Row {
    pub prompt: String,
    pub cards: Vec<Card>,
    pub tools: Vec<Tool>,
    pub items: Vec<Item>,
    pub state: ReplyState,
    pub stop_reason: Option<String>,
}
pub struct Chat {
    pub backend: String,
    pub model: String,
    pub rows: Vec<Row>,
    pub history: Option<crate::resume::History>,
    pub ready: bool,
    pub busy: bool,
    pub closed: bool,
    pub quitting: bool,
    pub notice: String,
    pub runtime_state: crate::runtime_state::State,
    pub approvals: crate::approvals::View,
    pub questions: crate::questions::View,
    pub controls: crate::model_controls::View,
    pub queue: crate::restored_queue::View,
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
            backend,
            model,
            rows: Vec::new(),
            history: None,
            ready: false,
            busy: false,
            closed: false,
            quitting: false,
            notice: "Connecting real owned Python runtime…".into(),
            runtime_state: crate::runtime_state::State::default(),
            approvals: crate::approvals::View::default(),
            questions: crate::questions::View::default(),
            controls: crate::model_controls::View::default(),
            queue: crate::restored_queue::View::default(),
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
    pub fn submit(&mut self, prompt: &str) -> io::Result<()> {
        self.reserve_row(prompt, false)
    }
    fn reserve_row(&mut self, prompt: &str, recovered: bool) -> io::Result<()> {
        if !self.ready
            || self.busy
            || self.closed
            || self.quitting
            || self.controls.owns_input()
            || self.queue.owns_input()
            || self.queue_pending
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
                "Enter ordinary prompt text; slash commands are not wired yet (64 KiB maximum)",
            ));
        }
        let units = self.prompt_budget(prompt)?;
        self.retained_units += units;
        self.retained_bytes += prompt.len();
        self.pending = Some(prompt.to_owned());
        self.submission_id = None;
        self.stop_requests = 0;
        self.stops_sent = 0;
        self.rows.push(Row {
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
        if !self.ready || self.busy || self.closed || self.quitting || self.queue_pending {
            return Err(io::Error::other(
                "Finish the current operation before checking provider history",
            ));
        }
        self.prompt_budget(self.queue.selection_text(intent)?)?;
        Ok(())
    }
    fn record_reconciled(&mut self, entry: &crate::restored_queue::Entry) -> io::Result<()> {
        if !self.ready
            || self.busy
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
        self.reserve_row(&prompt, true)?;
        self.pending = None; // The queue command carries identity, never a duplicate prompt.
        self.queue_pending = true;
        self.notice = "Continuing saved input; waiting for admission…".into();
        Ok(())
    }
    pub fn take_submission(&mut self) -> Option<String> {
        self.pending.take()
    }
    pub fn cancelling(&self) -> bool {
        self.busy && self.stop_requests > 0
    }
    pub fn open_stop(&mut self) {
        if self.busy && !self.closed && !self.quitting && self.stop_requests < 2 {
            self.stop_confirm = true;
            self.stop_selected = false;
        }
    }
    pub fn request_stop(&mut self) -> io::Result<()> {
        self.stop_confirm = false;
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
        self.rows.last_mut().expect("busy row").state = ReplyState::Cancelling;
        self.notice =
            "Stopping; waiting for the runtime. Ctrl+Esc can request a forced stop.".into();
        Ok(())
    }
    pub fn take_abort(&mut self) -> Option<String> {
        if !self.busy || self.stops_sent >= self.stop_requests {
            return None;
        }
        let id = self.submission_id.clone()?;
        self.stops_sent += 1;
        Some(id)
    }
    pub fn observe(&mut self, observation: Observation) -> io::Result<()> {
        if self.closed {
            return Err(io::Error::other("Observation after live view closed"));
        }
        match observation {
            Observation::History(history) => {
                if self.ready || self.busy || !self.rows.is_empty() || self.history.is_some() {
                    return Err(io::Error::other("Saved history arrived outside startup"));
                }
                self.retained_bytes = history.bytes;
                self.retained_units = history.units;
                self.notice = format!("Restored {} saved messages.", history.messages.len());
                self.history = Some(history);
            }
            Observation::Submission { id } => {
                self.pending_row()?;
                if self.submission_id.replace(id).is_some() {
                    return Err(io::Error::other("Duplicate local prompt reservation"));
                }
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
                if matches!(event, crate::approvals::Event::Requested(_)) {
                    self.pending_row()?;
                }
                self.notice = self.approvals.observe(event)?;
            }
            Observation::Question { event, settled } => {
                if matches!(event, crate::questions::Event::Requested(_)) {
                    self.pending_row()?;
                }
                self.notice = self.questions.observe(event)?;
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
        Ok(())
    }
    fn settle(&mut self, outcome: crate::live::TerminalOutcome) -> io::Result<()> {
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
        let reason = stop_reason.as_deref().unwrap_or("");
        let units = display_units(reason)?;
        if self.retained_units.saturating_add(units) > DISPLAY_UNITS
            || self.retained_bytes.saturating_add(reason.len()) > RETAINED_BYTES
        {
            return Err(io::Error::other("Live transcript/display limit exceeded"));
        }
        self.retained_units += units;
        self.retained_bytes += reason.len();
        let row = self.rows.last_mut().expect("validated pending row");
        row.state = state;
        row.stop_reason = stop_reason;
        for tool in &mut row.tools {
            tool.finish();
        }
        self.busy = false;
        self.stop_confirm = false;
        self.submission_id = None;
        self.notice = if self.queue_pending {
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
        Ok(())
    }
    fn pending_row(&self) -> io::Result<&Row> {
        self.rows
            .last()
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
        if index.is_none() && !row.tools.is_empty() {
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
        let row = self.rows.last_mut().expect("validated");
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
        let row = self.pending_row()?;
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
        let row = self.rows.last_mut().expect("validated");
        if let Some(index) = existing {
            row.tools[index] = tool;
        } else {
            row.items.push(Item::Tool(row.tools.len()));
            row.tools.push(tool);
        }
        if row.state != ReplyState::Cancelling {
            row.state = ReplyState::Streaming;
        }
        self.notice = "Native tool observation (not an approval or dispatch)".into();
        Ok(())
    }
    pub fn begin_quit(&mut self) {
        self.controls.finish();
        self.queue.finish();
        self.quitting = true;
        self.notice = "Closing Python runtime and its owned process tree…".into();
    }
    pub fn finish(&mut self, failure: Option<String>) {
        self.controls.finish();
        self.queue.finish();
        self.queue_pending = false;
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
                ReplyState::Complete | ReplyState::ApprovalStopped | ReplyState::Cancelled
            ) {
                row.state = ReplyState::Incomplete;
            }
        }
        self.notice =
            failure.unwrap_or_else(|| "Python runtime and contained process tree exited.".into());
    }
}
