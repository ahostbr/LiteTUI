//! Bounded serialized native chat view. Local row order is not a wire execution ID.
use crate::live::Observation;
use std::io;
use unicode_width::UnicodeWidthChar;
// Independent display-work budget, not a byte limit. LF pays one full
// minimum-width row, escaped controls pay their expansion, every scalar pays
// at least one unit (including combining marks), and every card pays overhead.
// Reject before allocating/wrapping an oversized view; never silently truncate.
const DISPLAY_UNITS: usize = 32768;
fn display_units(text: &str) -> io::Result<usize> {
    text.chars().try_fold(0usize, |total, ch| {
        let cost = if ch == '\n' {
            32
        } else if ch.is_control() || matches!(ch, '\u{202a}'..='\u{202e}' | '\u{2066}'..='\u{2069}')
        {
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
    Complete,
    Incomplete,
}
pub struct Row {
    pub prompt: String,
    pub answer: String,
    pub reasoning: String,
    pub state: ReplyState,
    answer_units: usize,
    reasoning_units: usize,
}
pub struct Chat {
    pub backend: String,
    pub model: String,
    pub rows: Vec<Row>,
    pub ready: bool,
    pub busy: bool,
    pub closed: bool,
    pub quitting: bool,
    pub notice: String,
    pub runtime_state: crate::runtime_state::State,
    pending: Option<String>,
    retained_bytes: usize,
    retained_units: usize,
}
impl Chat {
    pub fn new(backend: String, model: String) -> Self {
        Self {
            backend,
            model,
            rows: Vec::new(),
            ready: false,
            busy: false,
            closed: false,
            quitting: false,
            notice: "Connecting real owned Python runtime…".into(),
            runtime_state: crate::runtime_state::State::default(),
            pending: None,
            retained_bytes: 0,
            retained_units: 0,
        }
    }
    pub fn submit(&mut self, prompt: &str) -> io::Result<()> {
        if !self.ready || self.busy || self.closed || self.quitting {
            return Err(io::Error::other(
                "Wait for the runtime to be ready and the current turn to finish",
            ));
        }
        if prompt.trim().is_empty() || prompt.len() > 65536 || prompt.trim_start().starts_with('/')
        {
            return Err(io::Error::other(
                "Enter ordinary prompt text; slash commands are not wired yet (64 KiB maximum)",
            ));
        }
        if self.rows.len() >= 100
            || self.retained_bytes.saturating_add(prompt.len()) > 8 * 1024 * 1024
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
        self.retained_units += units;
        self.retained_bytes += prompt.len();
        self.pending = Some(prompt.to_owned());
        self.rows.push(Row {
            prompt: prompt.into(),
            answer: String::new(),
            reasoning: String::new(),
            state: ReplyState::Sending,
            answer_units: 0,
            reasoning_units: 0,
        });
        self.busy = true;
        self.notice = "Sending prompt (not yet admitted)…".into();
        Ok(())
    }
    pub fn take_submission(&mut self) -> Option<String> {
        self.pending.take()
    }
    pub fn observe(&mut self, observation: Observation) -> io::Result<()> {
        if self.closed {
            return Err(io::Error::other("Observation after live view closed"));
        }
        let replace = matches!(
            &observation,
            Observation::Snapshot(_) | Observation::ReasoningSnapshot(_)
        );
        let reasoning = matches!(
            &observation,
            Observation::Reasoning(_) | Observation::ReasoningSnapshot(_)
        );
        match observation {
            Observation::State(update) => self.runtime_state.apply(update),
            Observation::Ready => {
                self.ready = true;
                self.notice = "Connected. Enter sends; Ctrl+Q quits. Tools are disabled.".into();
            }
            Observation::Text(text)
            | Observation::Snapshot(text)
            | Observation::Reasoning(text)
            | Observation::ReasoningSnapshot(text) => {
                let units = display_units(&text)?;
                let row = self
                    .rows
                    .last_mut()
                    .filter(|row| matches!(row.state, ReplyState::Sending | ReplyState::Streaming))
                    .ok_or_else(|| io::Error::other("Content without a pending live row"))?;
                let (content, content_units) = if reasoning {
                    (&mut row.reasoning, &mut row.reasoning_units)
                } else {
                    (&mut row.answer, &mut row.answer_units)
                };
                let retained = self
                    .retained_bytes
                    .saturating_sub(if replace { content.len() } else { 0 })
                    .saturating_add(text.len());
                if retained > 8 * 1024 * 1024 {
                    return Err(io::Error::other("Live transcript text limit exceeded"));
                }
                let retained_units = self
                    .retained_units
                    .saturating_sub(if replace { *content_units } else { 0 })
                    .saturating_add(units);
                if retained_units > DISPLAY_UNITS {
                    return Err(io::Error::other("Live display-line/work limit exceeded"));
                }
                self.retained_units = retained_units;
                *content_units = if replace {
                    units
                } else {
                    content_units.saturating_add(units)
                };
                self.retained_bytes = retained;
                if replace {
                    *content = text;
                } else {
                    content.push_str(&text);
                }
                row.state = ReplyState::Streaming;
                self.notice = if reasoning {
                    "Receiving reported reasoning…"
                } else {
                    "Receiving real model reply…"
                }
                .into();
            }
            Observation::Information(info) => {
                self.notice = info;
            }
            Observation::Complete => {
                let row = self
                    .rows
                    .last_mut()
                    .filter(|row| matches!(row.state, ReplyState::Sending | ReplyState::Streaming))
                    .ok_or_else(|| io::Error::other("Completion without a pending live row"))?;
                row.state = ReplyState::Complete;
                self.busy = false;
                self.notice = "Reply completed. Type another prompt; Ctrl+Q quits.".into();
            }
        }
        Ok(())
    }
    pub fn begin_quit(&mut self) {
        self.quitting = true;
        self.notice = "Closing Python runtime and its owned process tree…".into();
    }
    pub fn finish(&mut self, failure: Option<String>) {
        self.closed = true;
        self.ready = false;
        self.busy = false;
        self.pending = None;
        for row in &mut self.rows {
            if row.state != ReplyState::Complete {
                row.state = ReplyState::Incomplete;
            }
        }
        self.notice =
            failure.unwrap_or_else(|| "Python runtime and contained process tree exited.".into());
    }
}
