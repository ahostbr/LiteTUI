//! Work inspection stays separate from the ordinary composer and execution authority.
use crate::{live::terminal_text, theme::Theme, work_protocol as protocol};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, MouseEvent, MouseEventKind};
#[path = "work_form.rs"]
mod form;
use crate::work_controls as controls;
use crate::work_execution_controls as execution;
use protocol::{Calendar, Page, PauseReview, Row, Section, Snapshot, StopReview, Tail};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Destination {
    Tasks = 0,
    Jobs = 2,
    Loops = 3,
    Goal = 4,
    Calendar = 5,
    Plugins,
    Hooks,
    NewJob,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    ManualReview {
        section: Section,
        row: Box<Row>,
    },
    ManualDecision {
        review: Box<crate::scheduled_transition::ManualReview>,
        accept: bool,
    },
    Context(crate::context::Request),
    ContextEdit(Box<crate::context_edit::Request>),
    Hook(Box<crate::hook_toggle::Request>),
    Plugin(Box<crate::plugin_reload::Request>),
    Image(crate::image::Request),
    Midturn(Box<crate::midturn::Request>),
    ChatPause(Box<crate::chat_pause::Request>),
    Dictation(crate::dictation::Request),
    ExecutionValidate(Box<execution::Edit>),
    ExecutionApply(Box<execution::Review>),
    ControlValidate(Box<controls::Edit>),
    ControlApply(Box<controls::Review>),
    DeferredValidate,
    DeferredApply(Box<crate::deferred_control::Review>),
    Snapshot,
    Page {
        section: Section,
        offset: usize,
    },
    Tail(Row),
    Calendar {
        month: String,
        date: Option<String>,
        offset: usize,
    },
    PauseValidate,
    PauseApply(Box<PauseReview>),
    StopValidate(Row),
    StopApply(Box<StopReview>),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Intent {
    pub generation: u64,
    pub request: Request,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Data {
    ManualReview(Box<crate::scheduled_transition::ManualReview>),
    Context(Box<crate::context::Page>),
    ContextEdit(Box<crate::context_edit::Reply>),
    Hook(Box<crate::hook_toggle::Reply>),
    Plugin(Box<crate::plugin_reload::Reply>),
    Image(Option<crate::image::Review>),
    Midturn(Box<crate::midturn::Reply>),
    ChatPause(Box<crate::chat_pause::Reply>),
    Dictation(crate::dictation::State),
    ImagePreview(crate::image::Thumbnail),
    ExecutionReview(Box<execution::Review>),
    ExecutionVerified {
        action: execution::Action,
        status: String,
        reason: Option<String>,
    },
    ControlReview(Box<controls::Review>),
    ControlSaved {
        action: controls::Action,
        row: serde_json::Value,
    },
    DeferredReview(Box<crate::deferred_control::Review>),
    DeferredDiscarded(Box<Snapshot>),
    Snapshot(Box<Snapshot>),
    Page(Page),
    Tail(Tail),
    Calendar(Calendar),
    Review(Box<PauseReview>),
    Paused,
    StopReview(Box<StopReview>),
    Stopped(Row),
    Rejected(String),
}
#[derive(Clone, Debug, PartialEq)]
pub struct Update {
    pub generation: u64,
    pub data: Data,
}
impl Update {
    pub(crate) fn interrupted(&self) -> Option<&str> {
        match &self.data {
            Data::Midturn(reply) => reply.interrupted(),
            _ => None,
        }
    }
}
#[derive(Clone)]
enum Review {
    Manual(Box<crate::scheduled_transition::ManualReview>),
    Execution(Box<execution::Review>),
    Control(Box<controls::Review>),
    Deferred(Box<crate::deferred_control::Review>),
    Pause(Box<PauseReview>),
    Stop(Box<StopReview>),
}
impl Review {
    fn request(&self) -> Request {
        match self {
            Self::Manual(review) => Request::ManualDecision {
                review: review.clone(),
                accept: true,
            },
            Self::Execution(review) => Request::ExecutionApply(review.clone()),
            Self::Control(review) => Request::ControlApply(review.clone()),
            Self::Deferred(review) => Request::DeferredApply(review.clone()),
            Self::Pause(review) => Request::PauseApply(review.clone()),
            Self::Stop(review) => Request::StopApply(review.clone()),
        }
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    pub waiting: bool,
    pub notice: String,
    pub snapshot: Option<Snapshot>,
    pub page: Option<Page>,
    pub calendar: Option<Calendar>,
    pub tail: Option<Tail>,
    context: crate::context_view::View,
    context_after_snapshot: Option<crate::context::Document>,
    command_after_snapshot: Option<Destination>,
    plugin_after_snapshot: Option<String>,
    system_after_snapshot: Option<String>,
    hooks: crate::hook_toggle_view::View,
    plugin_reload: crate::plugin_reload::View,
    pub image: crate::image::Picker,
    pub midturn: crate::midturn::View,
    pub chat_pause: crate::chat_pause::View,
    pub dictation: crate::dictation::View,
    section: usize,
    selected: usize,
    page_back: Vec<usize>,
    calendar_back: Vec<usize>,
    details: bool,
    error: Option<String>,
    scroll: usize,
    max_scroll: usize,
    month: String,
    review: Option<Review>,
    form: Option<form::Form>,
    saved_control: Option<(controls::Action, serde_json::Value)>,
    execution_result: Option<(execution::Action, String, Option<String>)>,
    stopped: Option<Row>,
    review_page: usize,
    seen: Vec<bool>,
    visible: bool,
    geometry: Option<Rect>,
    generation: u64,
    pending: Option<Request>,
    intent: Option<Intent>,
}
impl View {
    pub fn manual_decision_pending(&self) -> bool {
        matches!(self.pending, Some(Request::ManualDecision { .. }))
    }
    pub fn expire_manual(&mut self) {
        if matches!(self.review, Some(Review::Manual(_))) && !self.waiting {
            self.review = None;
            self.seen.clear();
            self.hide();
            self.notice="A newer scheduled proposal replaced this review. Refresh Work before running a job.".into();
        }
    }
    pub fn clear_attached_image(&mut self) {
        if self.image.open
            && !self.waiting
            && let Some(review) = &self.image.attached
        {
            self.request(Request::Image(crate::image::Request::Clear(
                review.token().into(),
            )));
        }
    }
    pub fn read_clipboard_image(&mut self) {
        if self.image.open && !self.waiting {
            self.request(Request::Image(self.image.clipboard_request()));
        }
    }
    pub fn review_chat_pause(&mut self, active: bool) {
        if !self.owns_input() {
            self.open = true;
            self.chat_pause.begin(active);
            self.request(Request::ChatPause(Box::new(
                crate::chat_pause::Request::Review { active },
            )));
        }
    }
    pub fn dictate(&mut self, draft: crate::dictation::Draft) {
        if !self.owns_input() {
            self.open = true;
            let request = self.dictation.begin(draft);
            self.request(Request::Dictation(request));
        }
    }
    pub fn review_queued_text(&mut self, draft: crate::midturn::Draft) {
        if !self.owns_input() {
            self.open = true;
            self.midturn.begin();
            self.request(Request::Midturn(Box::new(crate::midturn::Request::Review(
                draft,
            ))));
        }
    }
    pub fn open_image(&mut self) {
        if !self.owns_input() {
            self.open = true;
            self.image.open();
        }
    }
    pub fn context_open(&self) -> bool {
        self.context.is_open()
    }
    pub fn interrupt_apply_pending(&self) -> bool {
        self.waiting && matches!(&self.pending,Some(Request::Midturn(r)) if r.interrupts())
    }
    pub fn owns_input(&self) -> bool {
        self.open || self.waiting
    }
    pub fn open_at(&mut self, destination: Destination) {
        if self.waiting {
            return;
        }
        self.open();
        match destination {
            Destination::Plugins => {
                self.context_after_snapshot = Some(crate::context::Document::Plugins)
            }
            Destination::Hooks | Destination::NewJob => {
                self.command_after_snapshot = Some(destination)
            }
            _ => self.section = destination as usize,
        }
    }
    pub(crate) fn open_plugin_reload(&mut self, target: &str) {
        // Preserve the fresh Work identity read before opening its child control.
        if self.open && matches!(self.pending, Some(Request::Snapshot)) && self.intent.is_some() {
            self.plugin_after_snapshot = Some(target.to_owned());
        }
    }
    /// `/system [text]`: after the fresh Work snapshot, read the prompt and open its editor.
    pub(crate) fn open_system_prompt(&mut self, prefill: &str) {
        if self.open && matches!(self.pending, Some(Request::Snapshot)) && self.intent.is_some() {
            self.system_after_snapshot = Some(prefill.to_owned());
        }
    }
    pub fn open(&mut self) {
        if self.waiting {
            return;
        }
        self.open = true;
        self.refresh();
    }
    pub fn hide(&mut self) {
        self.chat_pause.hide();
        self.image.hide();
        self.hooks.hide();
        self.plugin_reload.hide();
        self.context.hide();
        self.visible = false;
    }
    pub fn close(&mut self) {
        self.context_after_snapshot = None;
        self.command_after_snapshot = None;
        self.plugin_after_snapshot = None;
        self.system_after_snapshot = None;
        self.image.close();
        self.midturn.open = false;
        self.chat_pause.open = false;
        self.hooks.close();
        self.plugin_reload.close();
        self.context.close();
        self.open = false;
        self.hide();
        // A sent read still owns its correlated reply. Retain input ownership
        // until it drains, so closing the panel cannot race a fresh prompt.
        let dispatched = self.pending.is_some() && self.intent.is_none();
        if !dispatched {
            self.pending = None;
            self.intent = None;
            self.waiting = false;
            self.review = None;
            self.seen.clear();
        }
    }
    pub fn finish(&mut self) {
        let generation = self.generation;
        let attached = self.image.attached.take();
        *self = Self::default();
        self.image.attached = attached;
        self.generation = generation;
    }
    pub fn take_intent(&mut self) -> Option<Intent> {
        if let Some(request) = self.dictation.poll(self.waiting) {
            self.request(Request::Dictation(request));
        }
        self.intent.take()
    }
    pub fn pending_generation(&self) -> Option<u64> {
        self.pending.as_ref().map(|_| self.generation)
    }
    fn request(&mut self, request: Request) {
        let Some(generation) = self.generation.checked_add(1) else {
            self.notice = "Work request limit reached; reopen the runtime.".into();
            return;
        };
        self.generation = generation;
        self.error = None;
        self.pending = Some(request.clone());
        self.intent = Some(Intent {
            generation,
            request,
        });
        self.waiting = true;
        self.hide();
    }
    fn refresh(&mut self) {
        self.context_after_snapshot = None;
        self.command_after_snapshot = None;
        self.plugin_after_snapshot = None;
        self.system_after_snapshot = None;
        self.hooks.close();
        self.plugin_reload.close();
        self.context.close();
        self.review = None;
        self.form = None;
        self.saved_control = None;
        self.execution_result = None;
        self.stopped = None;
        self.tail = None;
        self.page = None;
        self.calendar = None;
        self.details = false;
        self.scroll = 0;
        self.notice = "Reading Work…".into();
        self.request(Request::Snapshot);
    }
    fn section_request(&mut self) {
        self.page_back.clear();
        self.calendar_back.clear();
        self.selected = 0;
        self.scroll = 0;
        self.details = false;
        self.tail = None;
        self.page = None;
        self.calendar = None;
        let Some(snapshot) = &self.snapshot else {
            return;
        };
        if self.section < 4 {
            let section = Section::ALL[self.section];
            if !snapshot.source(section).available {
                self.notice = snapshot.source(section).reason.clone().unwrap_or_default();
                self.hide();
                return;
            }
            self.request(Request::Page { section, offset: 0 });
        } else if self.section == 5 {
            if !snapshot.source(Section::Jobs).available {
                self.notice = snapshot
                    .source(Section::Jobs)
                    .reason
                    .clone()
                    .unwrap_or_default();
                self.hide();
                return;
            }
            self.request(Request::Calendar {
                month: self.month.clone(),
                date: None,
                offset: 0,
            });
        } else {
            self.notice = if self.section == 6 {
                "Staged invocation recovery · D reviews discard · F5 refresh"
            } else {
                "Current conversation goal · F5 refresh"
            }
            .into();
            self.hide();
        }
    }
    pub fn observe(&mut self, update: Update) {
        if self.pending.is_none() || update.generation != self.generation {
            return;
        }
        let pending = self.pending.take().expect("pending");
        let compatible = matches!(
            (&pending, &update.data),
            (Request::ManualReview { .. }, Data::ManualReview(_))
                | (Request::ExecutionValidate(_), Data::ExecutionReview(_))
                | (Request::ExecutionApply(_), Data::ExecutionVerified { .. })
                | (Request::ControlValidate(_), Data::ControlReview(_))
                | (Request::ControlApply(_), Data::ControlSaved { .. })
                | (Request::DeferredValidate, Data::DeferredReview(_))
                | (Request::DeferredApply(_), Data::DeferredDiscarded(_))
                | (Request::Midturn(_), Data::Midturn(_))
                | (Request::ChatPause(_), Data::ChatPause(_))
                | (Request::Dictation(_), Data::Dictation(_))
                | (Request::Image(_), Data::Image(_))
                | (
                    Request::Image(crate::image::Request::Preview(_)),
                    Data::ImagePreview(_)
                )
                | (Request::Context(_), Data::Context(_))
                | (Request::ContextEdit(_), Data::ContextEdit(_))
                | (Request::Hook(_), Data::Hook(_))
                | (Request::Plugin(_), Data::Plugin(_))
                | (Request::Snapshot, Data::Snapshot(_))
                | (Request::Page { .. }, Data::Page(_))
                | (Request::Tail(_), Data::Tail(_))
                | (Request::Calendar { .. }, Data::Calendar(_))
                | (Request::PauseValidate, Data::Review(_))
                | (Request::PauseApply(_), Data::Paused)
                | (Request::StopValidate(_), Data::StopReview(_))
                | (Request::StopApply(_), Data::Stopped(_))
                | (_, Data::Rejected(_))
        );
        if !compatible {
            self.pending = Some(pending);
            return;
        }
        self.waiting = false;
        self.intent = None;
        self.hide();
        self.scroll = 0;
        match update.data {
            Data::ChatPause(reply) => {
                self.chat_pause.reply(*reply);
                self.open = self.chat_pause.open;
            }
            Data::Rejected(reason) if matches!(pending, Request::ChatPause(_)) => {
                self.chat_pause.reject(reason)
            }
            Data::Dictation(state) => {
                self.dictation.reply(state);
                self.open = self.dictation.open;
                if !self.open {
                    self.notice = "Dictation finished. Review your composer before sending.".into();
                }
            }
            Data::Rejected(reason) if matches!(pending, Request::Dictation(_)) => {
                self.dictation.reject(reason)
            }
            Data::Midturn(reply) => {
                let interrupted = reply.interrupted().is_some();
                self.midturn.reply(*reply);
                self.open = self.midturn.open;
                if !self.open {
                    self.notice = if interrupted {
                        "Interrupt accepted. Your message will run when the current worker stops."
                    } else {
                        "Message queued in the current conversation."
                    }
                    .into();
                }
            }
            Data::Rejected(reason) if matches!(pending, Request::Midturn(_)) => {
                self.midturn.reject(reason)
            }
            Data::Plugin(reply) => self.plugin_reload.reply(*reply),
            Data::Rejected(reason) if matches!(pending, Request::Plugin(_)) => {
                self.plugin_reload.reject(reason)
            }
            Data::Hook(reply) => self.hooks.reply(*reply),
            Data::Rejected(reason) if matches!(pending, Request::Hook(_)) => {
                self.hooks.reject(reason)
            }
            Data::ContextEdit(reply) => self.context.edit_reply(*reply),
            Data::Rejected(reason) if matches!(pending, Request::ContextEdit(_)) => {
                self.context.edit_rejected(&reason)
            }
            Data::ImagePreview(thumbnail) => self.image.thumbnail = Some(thumbnail),
            Data::Image(value) => {
                let Request::Image(request) = &pending else {
                    unreachable!()
                };
                self.image.result(request, value);
                self.open = self.image.open;
                self.notice = if self.open {
                    "Review the inspected image."
                } else if self.image.attached.is_some() {
                    "Image attached. Draft kept."
                } else {
                    "Image cleared. Draft kept."
                }
                .into();
            }
            Data::Rejected(reason) if matches!(pending, Request::Image(_)) => {
                self.image.notice = reason.clone();
                self.notice = reason;
            }
            Data::Context(page) => {
                if self.context.is_open() {
                    self.context.apply(*page);
                    if let Some(request) = self.context.continue_topics() {
                        self.request(Request::Context(request));
                    } else if let Some(request) = self.context.continue_draft_reload() {
                        self.request(Request::ContextEdit(Box::new(request)));
                    } else if let Some(request) = self.context.continue_system_editor() {
                        self.request(Request::ContextEdit(Box::new(request)));
                    }
                }
            }
            Data::Rejected(reason) if matches!(pending, Request::Context(_)) => {
                if self.context.is_open() {
                    if self.context.editing() {
                        self.context.edit_rejected(&reason);
                    } else {
                        self.context.reject(&reason);
                    }
                }
            }
            Data::ManualReview(review) => {
                if self.open {
                    self.review = Some(Review::Manual(review));
                    self.review_page = 0;
                    self.seen.clear();
                    self.notice =
                        "Read every page before running this job in a new conversation.".into();
                }
            }
            Data::ExecutionReview(review) => {
                self.review = Some(Review::Execution(review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page before starting this Work.".into();
            }
            Data::ExecutionVerified {
                action,
                status,
                reason,
            } => {
                self.review = None;
                if status == "published" {
                    self.form = None;
                }
                self.snapshot = None;
                self.page = None;
                self.calendar = None;
                self.tail = None;
                self.details = true;
                self.notice = if status == "published" {
                    "Launch independently verified. Work activity shows execution; Stop does not pause a goal."
                } else {
                    "Work was not started. The refusal was independently verified; F5 refreshes Work."
                }.into();
                if let Some(detail) = &reason {
                    self.notice = format!("{detail} {}", self.notice);
                }
                self.execution_result = Some((action, status, reason));
            }
            Data::ControlReview(review) => {
                self.review = Some(Review::Control(review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page before saving this Work change.".into();
            }
            Data::ControlSaved { action, row } => {
                self.review = None;
                self.form = None;
                self.snapshot = None;
                self.page = None;
                self.calendar = None;
                self.tail = None;
                self.saved_control = Some((action, row));
                self.details = true;
                self.notice="Saved configuration independently verified. F5 refreshes Work before another action.".into();
            }

            Data::DeferredReview(review) => {
                self.review = Some(Review::Deferred(review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page before discarding this staged invocation.".into();
            }
            Data::DeferredDiscarded(snapshot) => {
                self.snapshot = Some(*snapshot);
                self.review = None;
                self.page = None;
                self.details = false;
                self.notice="Staged invocation discarded and read back. Nothing replayed; existing input uncertainty is unchanged.".into();
            }

            Data::Snapshot(snapshot) => {
                self.month = snapshot.calendar_month.clone();
                self.snapshot = Some(*snapshot);
                self.notice = "Captured Work facts · Tab categories · Enter details".into();
                if self.open {
                    if let Some(target) = self.plugin_after_snapshot.take() {
                        let request = self.plugin_reload.open_target(&target);
                        self.request(Request::Plugin(Box::new(request)));
                    } else if let Some(prefill) = self.system_after_snapshot.take() {
                        let request = self.context.open_system_editor(&prefill);
                        self.request(Request::Context(request));
                    } else if let Some(destination) = self.command_after_snapshot.take() {
                        match destination {
                            Destination::Hooks => {
                                let request = self.hooks.open();
                                self.request(Request::Hook(Box::new(request)));
                            }
                            Destination::NewJob => {
                                self.section = 2;
                                match form::Form::new_job() {
                                    Ok(form) => self.form = Some(form),
                                    Err(error) => self.notice = error.to_string(),
                                }
                            }
                            _ => unreachable!("only command destinations are deferred"),
                        }
                    } else if let Some(document) = self.context_after_snapshot.clone() {
                        let request = self.context.open_document(document);
                        self.request(Request::Context(request));
                    } else {
                        self.section_request();
                    }
                }
            }
            Data::Page(page) => {
                if let Some(old) = &self.page {
                    if old.section == page.section {
                        update_back(&mut self.page_back, old.offset, page.offset);
                    } else {
                        self.page_back.clear();
                    }
                } else {
                    self.page_back.clear();
                }
                self.page = Some(page);
                self.selected = 0;
                self.details = false;
                self.notice =
                    "Captured rows · Enter details · N/PgDn next page · B/PgUp back".into();
            }
            Data::Tail(tail) => {
                self.tail = Some(tail);
                self.details = true;
                self.notice = "Task output · PgUp/PgDn scroll · Esc returns".into();
            }
            Data::Calendar(calendar) => {
                if let Some(old) = &self.calendar {
                    if old.month == calendar.month && old.date == calendar.date {
                        update_back(&mut self.calendar_back, old.offset, calendar.offset);
                    } else {
                        self.calendar_back.clear();
                    }
                } else {
                    self.calendar_back.clear();
                }
                self.calendar = Some(calendar);
                self.selected = 0;
                self.notice =
                    "Calendar · Enter opens the selected day · Left/Right change month".into();
            }
            Data::Review(review) => {
                self.review = Some(Review::Pause(review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page; confirm only on the last visible page.".into();
            }
            Data::Paused => {
                if let Some(s) = self.snapshot.as_mut() {
                    s.paused = true;
                    s.can_pause = false;
                    s.pause_reason = Some("Future scheduled prompts are already paused.".into());
                }
                self.review = None;
                self.notice =
                    "Future scheduled prompts paused. Active work and saved jobs are unchanged."
                        .into();
            }
            Data::StopReview(review) => {
                self.review = Some(Review::Stop(review));
                self.review_page = 0;
                self.seen.clear();
                self.notice = "Read every page before confirming this task Stop.".into();
            }
            Data::Stopped(row) => {
                self.notice = format!(
                    "Task Stop verified · final state {} · F5 refresh Work",
                    row.status
                );
                self.stopped = Some(row);
                self.review = None;
                self.snapshot = None;
                self.page = None;
                self.tail = None;
                self.calendar = None;
                self.details = true;
            }
            Data::Rejected(reason) => {
                self.review = None;
                self.error = Some(reason);
                self.details = true;
                self.notice = "Work request failed · PgUp/PgDn details · F5 refresh".into();
            }
        }
    }
    fn count(&self) -> usize {
        if let Some(c) = &self.calendar {
            if c.date.is_some() {
                c.rows.len()
            } else {
                c.days.iter().filter(|d| d.date.is_some()).count()
            }
        } else {
            self.page.as_ref().map_or(0, |p| p.rows.len())
        }
    }
    fn current_row(&self) -> Option<&Row> {
        self.page.as_ref().and_then(|p| p.rows.get(self.selected))
    }
    fn page_by(&mut self, forward: bool) {
        if let Some(c) = &self.calendar
            && let Some(date) = &c.date
        {
            let offset = if forward {
                c.next
            } else {
                self.calendar_back.last().copied()
            };
            if let Some(offset) = offset.filter(|o| *o != c.offset) {
                self.request(Request::Calendar {
                    month: c.month.clone(),
                    date: Some(date.clone()),
                    offset,
                });
            }
            return;
        }
        if let Some(p) = &self.page {
            let offset = if forward {
                p.next
            } else {
                self.page_back.last().copied()
            };
            if let Some(offset) = offset.filter(|o| *o != p.offset) {
                self.request(Request::Page {
                    section: p.section,
                    offset,
                });
            }
        }
    }
    fn execution(&mut self, action: execution::Action, row: Option<Row>, draft: serde_json::Value) {
        let result = self
            .snapshot
            .as_ref()
            .ok_or_else(|| std::io::Error::other("Refresh Work before reviewing execution"))
            .and_then(|snapshot| execution::Edit::new(snapshot, action, row.as_ref(), draft));
        match result {
            Ok(edit) => self.request(Request::ExecutionValidate(Box::new(edit))),
            Err(error) => self.notice = error.to_string(),
        }
    }
    fn control(&mut self, action: controls::Action, row: Option<Row>, draft: serde_json::Value) {
        let result = self
            .snapshot
            .as_ref()
            .ok_or_else(|| std::io::Error::other("Refresh Work before editing"))
            .and_then(|snapshot| controls::Edit::new(snapshot, action, row.as_ref(), draft));
        match result {
            Ok(edit) => self.request(Request::ControlValidate(Box::new(edit))),
            Err(error) => self.notice = error.to_string(),
        }
    }
    pub fn accepts_paste(&self) -> bool {
        if self.hooks.open || self.plugin_reload.open {
            return false;
        }
        if self.context.is_open() {
            return !self.waiting && self.context.accepts_paste();
        }
        if self.image.open {
            return !self.waiting && self.image.editing();
        }
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && self.form.as_ref().is_some_and(form::Form::accepts_paste)
    }
    pub fn clipboard_error(&mut self, error: String) {
        self.notice = error;
    }
    pub fn paste(&mut self, text: &str) {
        if self.dictation.open
            || self.chat_pause.open
            || self.midturn.open
            || self.hooks.open
            || self.plugin_reload.open
        {
            return;
        }
        if self.context.is_open() {
            if !self.waiting {
                self.context.paste(text);
            }
            return;
        }
        if self.image.open {
            if !self.waiting {
                self.image.paste(text);
            }
            return;
        }
        if self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && let Some(form) = self.form.as_mut()
            && let Err(error) = form.paste(text)
        {
            self.notice = error.to_string();
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if self.dictation.open {
            if let Some(r) = self.dictation.key(key, self.waiting) {
                self.request(Request::Dictation(r));
            }
            self.open = self.dictation.open;
            return;
        }
        if self.chat_pause.open {
            if let Some(req) = self.chat_pause.key(key, self.waiting) {
                self.request(Request::ChatPause(Box::new(req)));
            }
            self.open = self.chat_pause.open;
            return;
        }
        if self.midturn.open {
            if key.kind == KeyEventKind::Press {
                if let Some(req) = self.midturn.key(key, self.waiting) {
                    self.request(Request::Midturn(Box::new(req)));
                }
                self.open = self.midturn.open;
            }
            return;
        }
        if self.plugin_reload.open {
            if let Some(req) = self.plugin_reload.key(key, self.waiting) {
                self.request(Request::Plugin(Box::new(req)));
            }
            return;
        }
        if self.context.is_open()
            && self.context.document() == crate::context::Document::Plugins
            && self.context.page().is_some()
            && !self.waiting
            && key.kind == KeyEventKind::Press
            && key.modifiers.is_empty()
            && key.code == KeyCode::Char('r')
        {
            let req = self.plugin_reload.open();
            self.request(Request::Plugin(Box::new(req)));
            return;
        }
        if self.hooks.open {
            if let Some(request) = self.hooks.key(key, self.waiting) {
                self.request(Request::Hook(Box::new(request)));
            }
            return;
        }
        if self.context.is_open()
            && self.context.document() == crate::context::Document::Hooks
            && self.context.page().is_some()
            && !self.waiting
            && key.kind == KeyEventKind::Press
            && key.modifiers.is_empty()
            && key.code == KeyCode::Char('e')
        {
            let req = self.hooks.open();
            self.request(Request::Hook(Box::new(req)));
            return;
        }
        if self.image.open {
            if let Some(request) = self.image.key(key, self.waiting) {
                self.request(Request::Image(request));
            }
            if !self.image.open {
                self.open = false;
            }
            return;
        }
        if self.context.is_open() {
            let editing = self.context.editing();
            if !self.waiting
                && let Some(request) = self.context.reload_draft(key)
            {
                self.request(Request::Context(request));
            } else if !self.waiting
                && let Some(request) = self.context.edit_key(key)
            {
                self.request(Request::ContextEdit(Box::new(request)));
            } else if editing && !self.context.editing() {
                let request = self.context.reload();
                self.request(Request::Context(request));
            } else if !editing
                && !matches!(self.pending, Some(Request::ContextEdit(_)))
                && let Some(request) = self.context.key(key)
            {
                self.request(Request::Context(request));
            }
            if !self.context.is_open() && self.context_after_snapshot.is_some() {
                self.close();
            }
            return;
        }
        if self.form.is_some() && self.review.is_none() && !self.waiting {
            if key.kind == KeyEventKind::Release {
                return;
            }
            if key.code == KeyCode::Esc
                && key.modifiers.is_empty()
                && key.kind == KeyEventKind::Press
            {
                self.form = None;
                self.hide();
                return;
            }
            if !self.open || !self.visible {
                return;
            }
            if key.code == KeyCode::Enter && key.kind != KeyEventKind::Press {
                return;
            }
            let form = self.form.as_mut().unwrap();
            match form.key(key) {
                Ok(true) => match form.draft() {
                    Ok(draft) => {
                        if form.is_goal() {
                            self.execution(execution::Action::GoalStart, None, draft);
                            return;
                        }
                        let row = form.row.clone();
                        let action = if row.is_some() {
                            controls::Action::JobUpdate
                        } else {
                            controls::Action::JobCreate
                        };
                        self.control(action, row, draft);
                    }
                    Err(error) => self.notice = error.to_string(),
                },
                Ok(false) => {}
                Err(error) => self.notice = error.to_string(),
            }
            return;
        }

        let back_tab =
            key.code == KeyCode::BackTab && key.modifiers == crossterm::event::KeyModifiers::SHIFT;
        if key.kind == KeyEventKind::Release || (!key.modifiers.is_empty() && !back_tab) {
            return;
        }
        if key.code == KeyCode::Esc && key.kind == KeyEventKind::Press {
            if let Some(Review::Manual(review)) = self.review.as_ref()
                && !self.waiting
            {
                self.request(Request::ManualDecision {
                    review: review.clone(),
                    accept: false,
                });
                return;
            }
            if self.review.is_some() && !self.waiting {
                self.review = None;
                self.seen.clear();
                self.hide();
            } else if self.tail.is_some() {
                self.tail = None;
                self.details = false;
                self.scroll = 0;
                self.hide();
            } else if self.details {
                self.details = false;
                self.scroll = 0;
                self.hide();
            } else if self.calendar.as_ref().is_some_and(|c| c.date.is_some()) && !self.waiting {
                self.request(Request::Calendar {
                    month: self.month.clone(),
                    date: None,
                    offset: 0,
                });
            } else {
                self.close();
            }
            return;
        }
        if !self.open || !self.visible || self.waiting {
            return;
        }
        if let Some(review) = &self.review {
            if key.kind != KeyEventKind::Press {
                return;
            }
            match key.code {
                KeyCode::PageUp | KeyCode::Left => {
                    self.review_page = self.review_page.saturating_sub(1);
                    self.hide();
                }
                KeyCode::PageDown | KeyCode::Right => {
                    self.review_page =
                        (self.review_page + 1).min(self.seen.len().saturating_sub(1));
                    self.hide();
                }
                KeyCode::Enter => {
                    if self.review_page + 1 < self.seen.len() {
                        self.review_page += 1;
                        self.hide();
                    } else if !self.seen.is_empty() && self.seen.iter().all(|seen| *seen) {
                        self.request(review.request());
                    }
                }
                _ => {}
            }
            return;
        }
        if key.kind == KeyEventKind::Press {
            match key.code {
                KeyCode::Char('i') if !self.details && self.snapshot.is_some() => {
                    let request = self.context.open();
                    self.request(Request::Context(request));
                    return;
                }
                KeyCode::F(5) => {
                    self.refresh();
                    return;
                }
                KeyCode::Tab => {
                    self.section = (self.section + 1) % 7;
                    self.section_request();
                    return;
                }
                KeyCode::BackTab => {
                    self.section = (self.section + 6) % 7;
                    self.section_request();
                    return;
                }
                KeyCode::Char('n') if self.section == 4 => {
                    if self.snapshot.is_none() {
                        self.notice = "Refresh Work before starting a goal.".into();
                    } else {
                        match form::Form::goal() {
                            Ok(form) => {
                                self.form = Some(form);
                                self.hide();
                            }
                            Err(error) => self.notice = error.to_string(),
                        }
                    }
                    return;
                }
                KeyCode::Char('r') if matches!(self.section, 2 | 3) => {
                    if let Some(row) = self.current_row().cloned() {
                        if row.raw["new_conversation"] == true {
                            let section = if self.section == 2 {
                                Section::Jobs
                            } else {
                                Section::Loops
                            };
                            self.request(Request::ManualReview {
                                section,
                                row: Box::new(row),
                            });
                        } else {
                            self.execution(
                                execution::Action::JobRun,
                                Some(row),
                                serde_json::json!({}),
                            );
                        }
                    } else {
                        self.notice = "Select the exact job to review Run.".into();
                    }
                    return;
                }
                KeyCode::Char('r') if self.section == 4 => {
                    self.execution(execution::Action::GoalResume, None, serde_json::json!({}));
                    return;
                }
                KeyCode::Char('c') if matches!(self.section, 2 | 3) => {
                    match form::Form::new(if self.section == 2 { "cron" } else { "loop" }, None) {
                        Ok(form) => {
                            self.form = Some(form);
                            self.hide();
                        }
                        Err(error) => self.notice = error.to_string(),
                    }
                    return;
                }
                KeyCode::Char('e') if matches!(self.section, 2 | 3) => {
                    if let Some(row) = self.current_row().cloned() {
                        match form::Form::new(&row.kind, Some(row.clone())) {
                            Ok(form) => {
                                self.form = Some(form);
                                self.hide();
                            }
                            Err(error) => self.notice = error.to_string(),
                        }
                    }
                    return;
                }
                KeyCode::Char('d') if matches!(self.section, 2 | 3) => {
                    if let Some(row) = self.current_row().cloned() {
                        self.control(
                            controls::Action::JobDelete,
                            Some(row),
                            serde_json::json!({}),
                        );
                    }
                    return;
                }
                KeyCode::Char(' ') if self.section == 3 => {
                    if let Some(row) = self.current_row().cloned() {
                        let enabled = row.raw["enabled"] != true;
                        self.control(
                            controls::Action::LoopEnable,
                            Some(row),
                            serde_json::json!({"enabled":enabled}),
                        );
                    }
                    return;
                }
                KeyCode::Char('p') if self.section == 4 => {
                    self.control(controls::Action::GoalPause, None, serde_json::json!({}));
                    return;
                }
                KeyCode::Char('d') if self.section == 4 => {
                    self.control(controls::Action::GoalClear, None, serde_json::json!({}));
                    return;
                }
                KeyCode::Char('d') if self.section == 6 => {
                    if self
                        .snapshot
                        .as_ref()
                        .and_then(|s| s.deferred_job.as_ref())
                        .is_some_and(|d| d.eligible)
                    {
                        self.request(Request::DeferredValidate);
                    } else {
                        self.notice="No eligible staged invocation to discard; refresh Work for current evidence.".into();
                    }
                    return;
                }
                KeyCode::Char('k') => {
                    if let Some(row) = self.current_row().filter(|row| row.can_stop())
                        && self.snapshot.as_ref().is_some_and(|s| !s.quitting)
                    {
                        self.request(Request::StopValidate(row.clone()));
                    } else {
                        self.notice =
                            "Select a current owned running host task to review Stop.".into();
                    }
                    return;
                }
                KeyCode::Char('p') => {
                    if let Some(snapshot) = &self.snapshot {
                        if snapshot.can_pause {
                            self.request(Request::PauseValidate);
                        } else {
                            self.notice = snapshot.pause_reason.clone().unwrap_or_default();
                        }
                    }
                    return;
                }
                _ => {}
            }
        }
        if self.details || self.section == 4 || self.section == 6 {
            match key.code {
                KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
                KeyCode::Down => self.scroll = (self.scroll + 1).min(self.max_scroll),
                KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(8),
                KeyCode::PageDown => self.scroll = (self.scroll + 8).min(self.max_scroll),
                _ => {}
            }
            return;
        }
        match key.code {
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => {
                self.selected = (self.selected + 1).min(self.count().saturating_sub(1))
            }
            KeyCode::Home => self.selected = 0,
            KeyCode::End => self.selected = self.count().saturating_sub(1),
            KeyCode::PageDown | KeyCode::Char('n') => self.page_by(true),
            KeyCode::PageUp | KeyCode::Char('b') => self.page_by(false),
            KeyCode::Left | KeyCode::Right
                if self.section == 5 && key.kind == KeyEventKind::Press =>
            {
                self.month = protocol::shift_month(&self.month, key.code == KeyCode::Right);
                self.request(Request::Calendar {
                    month: self.month.clone(),
                    date: None,
                    offset: 0,
                });
            }
            KeyCode::Char('t') if key.kind == KeyEventKind::Press => {
                if let Some(row) = self.current_row() {
                    if row.tail_available {
                        self.request(Request::Tail(row.clone()));
                    } else {
                        self.notice = "Output is unavailable for this selected row.".into();
                    }
                }
            }
            KeyCode::Enter if key.kind == KeyEventKind::Press => {
                if let Some(c) = &self.calendar {
                    if c.date.is_none() {
                        if let Some(day) = c
                            .days
                            .iter()
                            .filter_map(|d| d.date.as_ref())
                            .nth(self.selected)
                        {
                            self.request(Request::Calendar {
                                month: self.month.clone(),
                                date: Some(day.clone()),
                                offset: 0,
                            });
                        }
                    } else {
                        self.details = true;
                        self.scroll = 0;
                        self.hide();
                    }
                } else if self.current_row().is_some()
                    || self.section < 4
                        && self
                            .snapshot
                            .as_ref()
                            .is_some_and(|s| !s.source(Section::ALL[self.section]).available)
                {
                    self.error = None;
                    self.details = true;
                    self.scroll = 0;
                    self.hide();
                }
            }
            _ => {}
        }
    }
    pub fn mouse(&mut self, event: MouseEvent) {
        if self.dictation.open
            || self.chat_pause.open
            || self.midturn.open
            || self.hooks.open
            || self.plugin_reload.open
        {
            return;
        }
        if self.image.open {
            return;
        }
        if self.context.is_open() {
            if !self.context.editing()
                && !self.waiting
                && let Some(request) = self.context.mouse(event)
            {
                self.request(Request::Context(request));
            }
            return;
        }
        if !self.open || !self.visible || self.waiting || !event.modifiers.is_empty() {
            return;
        }
        if self.review.is_some() {
            self.seen.fill(false);
            self.hide();
            return;
        }
        match event.kind {
            MouseEventKind::ScrollUp => {
                if self.details || self.section == 4 || self.section == 6 {
                    self.scroll = self.scroll.saturating_sub(3);
                } else {
                    self.selected = self.selected.saturating_sub(1);
                }
            }
            MouseEventKind::ScrollDown => {
                if self.details || self.section == 4 || self.section == 6 {
                    self.scroll = (self.scroll + 3).min(self.max_scroll);
                } else {
                    self.selected = (self.selected + 1).min(self.count().saturating_sub(1));
                }
            }
            _ => {}
        }
    }
    fn review_lines(&self) -> Vec<String> {
        let r = match self.review.as_ref().unwrap() {
            Review::Manual(review) => return review.details.clone(),
            Review::Execution(review) => return review.detail_lines(),
            Review::Control(review) => return vec![
                review.edit.action.label().into(),review.consequence.clone(),
                format!("Conversation: {}",review.edit.snapshot.identity.session),
                format!("Scope: {}",review.wire["scope"]),
                "Before:".into(),serde_json::to_string_pretty(&review.before).expect("validated control before"),
                "After:".into(),serde_json::to_string_pretty(&review.after).expect("validated control after"),
                review.rearm_minutes.map_or_else(||"Run counters are preserved. This save does not run a job.".into(),|minutes|format!("Next run is rearmed at save time plus {minutes} minutes. This save does not run the job.")),
                "Enter on the last visible page confirms once. Esc cancels; local draft is kept.".into(),
            ],
            Review::Deferred(review) => return vec![
                "Discard this staged invocation".into(),crate::deferred_control::CONSEQUENCE.into(),
                format!("Conversation: {}",review.identity.session),format!("Agent: {}",review.identity.agent),
                "Exact retained job and delivery evidence:".into(),
                serde_json::to_string_pretty(&review.selected.wire).expect("validated deferred evidence"),
                "The stamped run is retained. No job is run, no input is marked complete, and no effect is undone.".into(),
                "Enter on the last visible page confirms once. Esc cancels.".into(),
            ],
            Review::Pause(review) => review,
            Review::Stop(review) => {
                let mut lines = vec![
                    "Stop this owned background task".into(),
                    protocol::STOP_CONSEQUENCE.into(),
                    format!("Conversation: {}", review.snapshot.identity.session),
                    format!("Agent: {}", review.snapshot.identity.agent),
                    format!("Backend: {}", review.snapshot.identity.backend),
                    format!("Model: {}", review.snapshot.identity.model),
                    "Exact selected task:".into(),
                    serde_json::to_string_pretty(&review.row.raw).expect("validated task row"),
                    "Source Stop authority:".into(),
                ];
                for key in [
                    "stop_mode",
                    "process_id",
                    "owner_pid",
                    "owner_instance",
                    "owner_created",
                    "producer_id",
                    "worker_id",
                    "lifecycle_instance",
                    "action_id",
                ] {
                    lines.push(format!("{key}: {}", review.wire[key]));
                }
                lines.push("Enter on the last visible page confirms once. Esc cancels.".into());
                return lines;
            }
        };
        vec![
            "Pause future scheduled prompts".into(),
            protocol::CONSEQUENCE.into(),
            format!("Conversation: {}", r.snapshot.identity.session),
            format!("Agent: {}", r.snapshot.identity.agent),
            format!("Backend: {}", r.snapshot.identity.backend),
            format!("Model: {}", r.snapshot.identity.model),
            "Applies only to this runtime. Saved schedules remain enabled.".into(),
            "This does not stop a task, pause a goal, clear input, or close the runtime.".into(),
            "Enter on the last visible page confirms once. Esc cancels.".into(),
        ]
    }
    fn detail_lines(&self) -> Vec<String> {
        let mut lines = Vec::new();
        if let Some(notice) = self
            .snapshot
            .as_ref()
            .and_then(|s| s.scheduler_notice.as_ref())
        {
            lines.push(format!("Schedule held: {notice}"));
            lines.push(String::new());
        }
        lines.extend(self.detail_content());
        lines
    }
    fn detail_content(&self) -> Vec<String> {
        if let Some((action, status, reason)) = &self.execution_result {
            return vec![action.label().into(), format!("Launch result: {status}"),
                reason.clone().unwrap_or_else(|| "The source launch receipt and Work ledger were independently read back.".into()),
                "A recorded launch is not model completion. Follow the actual Work activity.".into(),
                "Stop cancels the current invocation or turn. Goal Pause separately prevents future goal work.".into()];
        }
        if let Some((action, row)) = &self.saved_control {
            return vec![
                format!("{} verified", action.label()),
                serde_json::to_string_pretty(row).expect("validated saved control"),
                "F5 refreshes Work. No prompt was dispatched by this save.".into(),
            ];
        }

        if let Some(row) = &self.stopped {
            return vec![
                "Task Stop independently verified. Workers, callbacks and process retired.".into(),
                serde_json::to_string_pretty(&row.raw).expect("validated task row"),
                "F5 refreshes the Work capture before another task action.".into(),
            ];
        }
        if let Some(error) = &self.error {
            return vec![error.clone()];
        }
        if self.section < 4
            && let Some(reason) = self
                .snapshot
                .as_ref()
                .and_then(|s| s.source(Section::ALL[self.section]).reason.as_ref())
        {
            return vec![reason.clone()];
        }
        if let Some(t) = &self.tail {
            let mut rows = vec![
                format!("Task {} · {}", t.task_id, t.status),
                format!("Output: {}", t.path),
                format!(
                    "Bytes {}..{}{}",
                    t.offset,
                    t.total,
                    if t.lossy {
                        " · invalid UTF-8 replaced"
                    } else {
                        ""
                    }
                ),
            ];
            if let Some(r) = &t.reason {
                rows.push(r.clone());
            }
            rows.extend(t.text.lines().map(str::to_owned));
            return rows;
        }
        if self.section == 6 {
            let mut lines = self
                .snapshot
                .as_ref()
                .and_then(|s| s.deferred_job.as_ref())
                .map_or_else(
                    || vec!["No retained staged invocation.".into()],
                    |d| {
                        vec![
                            "Staged invocation recovery".into(),
                            crate::deferred_control::CONSEQUENCE.into(),
                            serde_json::to_string_pretty(&d.wire)
                                .expect("validated deferred evidence"),
                            d.reason.clone().unwrap_or_else(|| {
                                "D reviews discard. Nothing is replayed.".into()
                            }),
                        ]
                    },
                );
            if let Some(receipt) = self
                .snapshot
                .as_ref()
                .and_then(|s| s.execution_receipt.as_ref())
            {
                lines.push("Reviewed Work launch history".into());
                lines.push(
                    "This receipt records a past launch. It cannot start, resume or cancel work."
                        .into(),
                );
                lines.push(format!("Availability: {}", receipt.status));
                if let Some(reason) = &receipt.reason {
                    lines.push(reason.clone());
                }
                if let Some(document) = &receipt.document {
                    lines.push(
                        serde_json::to_string_pretty(&document.wire)
                            .expect("validated historical launch"),
                    );
                }
            }
            return lines;
        }
        if self.section == 4 {
            let Some(s) = &self.snapshot else {
                return vec![];
            };
            let g = &s.goal;
            if g["status"] != "present" {
                return vec![
                    g["reason"]
                        .as_str()
                        .unwrap_or("No saved goal for this conversation.")
                        .into(),
                ];
            }
            let g = &g["value"];
            let mut rows = Vec::new();
            for (key, label) in [
                ("objective", "Objective"),
                ("status", "Status"),
                ("evaluated_turns", "Evaluated turns"),
                ("max_turns", "Maximum turns (0 means unlimited)"),
                ("last_verdict", "Last verdict"),
                ("last_reason", "Last reason"),
                ("no_progress_count", "No-progress turns"),
                ("tool_profile", "Recorded tool profile"),
                ("started_by", "Started by"),
                ("created", "Created"),
                ("updated", "Updated"),
            ] {
                let v = &g[key];
                rows.push(format!(
                    "{label}: {}",
                    v.as_str()
                        .map(str::to_owned)
                        .unwrap_or_else(|| v.to_string())
                ));
            }
            if let Some(evidence) = g["missing_evidence"].as_array() {
                for e in evidence {
                    rows.push(format!(
                        "Missing evidence: {}",
                        e.as_str().unwrap_or_default()
                    ));
                }
            }
            return rows;
        }
        if let Some(c) = &self.calendar
            && let Some(row) = c.rows.get(self.selected)
        {
            return vec![
                row.summary(),
                format!("Job: {}", row.job),
                format!("Firings on this day: {}", row.total),
            ];
        }
        self.current_row().map_or_else(Vec::new, Row::details)
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if self.dictation.open {
            self.dictation.draw(frame, area, self.waiting);
            return;
        }
        if self.chat_pause.open {
            self.chat_pause.draw(frame, area, self.waiting);
            return;
        }
        if self.midturn.open {
            self.midturn.draw(frame, area, self.waiting);
            return;
        }
        if self.plugin_reload.open {
            self.plugin_reload.draw(frame, area, theme, self.waiting);
            return;
        }
        if self.hooks.open {
            self.hooks.draw(frame, area, theme, self.waiting);
            return;
        }
        if self.image.open {
            self.image.draw(frame, area, theme, self.waiting);
            return;
        }
        self.visible = false;
        if !self.owns_input() {
            return;
        }
        if self.context.is_open() {
            self.context.draw(frame, area, theme);
            return;
        }
        if area.width < 48 || area.height < 18 {
            self.seen.clear();
            self.review_page = 0;
            frame.render_widget(Clear, area);
            frame.render_widget(
                Paragraph::new("Work needs 48×18. Resize, or Esc to close.")
                    .block(Block::bordered().title(" Work ")),
                area,
            );
            return;
        }
        let width = area.width.saturating_sub(2).min(110);
        let height = area.height.saturating_sub(2).min(36);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        if self.geometry != Some(rect) {
            self.geometry = Some(rect);
            self.seen.clear();
            self.review_page = 0;
        }
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(" Work ")
            .style(Style::default().fg(theme.foreground).bg(theme.background))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let notice_lines = self
            .snapshot
            .as_ref()
            .and_then(|s| s.scheduler_notice.as_ref())
            .map(|s| {
                crate::ui::wrap(
                    &terminal_text(&format!("Schedule held: {s}")),
                    inner.width as usize,
                )
            })
            .unwrap_or_default();
        let notice_height = notice_lines.len().min(4) as u16;
        if notice_height > 0 {
            frame.render_widget(
                Paragraph::new(
                    notice_lines
                        .into_iter()
                        .take(4)
                        .collect::<Vec<_>>()
                        .join("\n"),
                ),
                Rect::new(inner.x, inner.y + 1, inner.width, notice_height),
            );
        }
        let body = Rect::new(
            inner.x,
            inner.y + 2 + notice_height,
            inner.width,
            inner.height.saturating_sub(5 + notice_height),
        );
        let label = if self.section < 4 {
            Section::ALL[self.section].label()
        } else if self.section == 6 {
            "Recovery"
        } else if self.section == 4 {
            "Goal"
        } else {
            "Calendar"
        };
        let header = format!(
            "{} · {}{}",
            label,
            self.snapshot.as_ref().map_or("Reading", |s| if s.paused {
                "Schedules paused"
            } else {
                "Schedules active"
            }),
            if self.snapshot.as_ref().is_some_and(|s| s.busy) {
                " · Turn active"
            } else {
                ""
            }
        );
        frame.render_widget(
            Paragraph::new(terminal_text(&header)),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        let wrap = |lines: Vec<String>| -> Vec<String> {
            lines
                .into_iter()
                .flat_map(|line| crate::ui::wrap(&terminal_text(&line), body.width as usize))
                .collect()
        };
        let lines = if self.form.is_some() && self.review.is_none() {
            self.form
                .as_ref()
                .unwrap()
                .visible_lines(body.width as usize, body.height as usize)
        } else if self.review.is_some() {
            let lines = wrap(self.review_lines());
            let per = body.height.max(1) as usize;
            let pages = lines.len().div_ceil(per).max(1);
            if self.seen.len() != pages {
                self.seen = vec![false; pages];
            }
            self.review_page = self.review_page.min(pages - 1);
            self.seen[self.review_page] = true;
            self.notice = format!(
                "Review {}/{} · PgUp/PgDn · Enter {} · Esc cancel",
                self.review_page + 1,
                pages,
                if self.review_page + 1 == pages {
                    "confirms"
                } else {
                    "next page"
                }
            );
            lines
                .into_iter()
                .skip(self.review_page * per)
                .take(per)
                .collect::<Vec<_>>()
        } else if self.details || self.section == 4 || self.section == 6 {
            let lines = wrap(self.detail_lines());
            self.max_scroll = lines.len().saturating_sub(body.height as usize);
            self.scroll = self.scroll.min(self.max_scroll);
            lines
                .into_iter()
                .skip(self.scroll)
                .take(body.height as usize)
                .collect()
        } else {
            let mut labels = Vec::new();
            if let Some(c) = &self.calendar {
                if c.date.is_some() {
                    labels.extend(c.rows.iter().map(|entry| entry.summary()));
                } else {
                    labels.extend(c.days.iter().filter(|d| d.date.is_some()).map(|d| {
                        format!(
                            "{} · {} job{}{}",
                            d.date.as_deref().unwrap(),
                            d.total,
                            if d.total == 1 { "" } else { "s" },
                            d.entries
                                .first()
                                .map(|e| format!(" · {}", e.summary()))
                                .unwrap_or_default()
                        )
                    }));
                }
            } else if let Some(p) = &self.page {
                labels.extend(
                    p.rows
                        .iter()
                        .map(|r| format!("{} · {} · {}", r.label, r.status, r.id)),
                );
            }
            if labels.is_empty() {
                vec![if self.waiting {
                    "Reading…".into()
                } else if self.section < 4 {
                    if self
                        .snapshot
                        .as_ref()
                        .is_some_and(|s| !s.source(Section::ALL[self.section]).available)
                    {
                        "Source unavailable · Enter for complete details".into()
                    } else {
                        "No rows in this captured section.".into()
                    }
                } else {
                    "No calendar entries.".into()
                }]
            } else {
                let page_height = body.height.max(1) as usize;
                let start = self.selected / page_height * page_height;
                labels
                    .into_iter()
                    .enumerate()
                    .skip(start)
                    .take(page_height)
                    .map(|(i, label)| {
                        let escaped = terminal_text(&label);
                        let chunks =
                            crate::ui::wrap(&escaped, body.width.saturating_sub(4) as usize);
                        format!(
                            "{} {}{}",
                            if i == self.selected { "▶" } else { " " },
                            chunks.first().cloned().unwrap_or_default(),
                            if chunks.len() > 1 { "…" } else { "" }
                        )
                    })
                    .collect()
            }
        };
        frame.render_widget(Paragraph::new(lines.join("\n")), body);
        let footer = if self.form.is_some() && self.review.is_none() {
            "Tab field | Space toggles | Ctrl+U clears\nEnter reviews | Shift+Enter newline | Esc cancel".into()
        } else if self.review.is_some() {
            self.notice.clone()
        } else {
            format!(
                "I Context · Tab categories · F5 refresh · {}\n{}",
                match self.section {
                    2 => "R run · C create · E edit · D delete",
                    3 => "R run · C create · E edit · D delete · Space enable/pause",
                    4 => "N new goal · R resume · P pause · D clear",
                    6 => "D discard staged invocation",
                    _ => "P pause schedules · T output · K Stop task",
                },
                self.notice
            )
        };
        let footer = crate::ui::wrap(&terminal_text(&footer), inner.width as usize)
            .into_iter()
            .take(3)
            .collect::<Vec<_>>()
            .join("\n");
        frame.render_widget(
            Paragraph::new(footer),
            Rect::new(inner.x, body.bottom(), inner.width, 3),
        );
        self.visible = true;
    }
}

// The source may shrink pages to fit its byte budget; offsets are not fixed strides.
fn update_back(back: &mut Vec<usize>, old: usize, new: usize) {
    if new > old {
        back.push(old);
    } else if new < old {
        if let Some(index) = back.iter().position(|offset| *offset == new) {
            back.truncate(index);
        } else {
            back.clear();
        }
    }
}
