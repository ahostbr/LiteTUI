//! Structured source questions. Answer ACKs resolve input, never prove tool execution.
use crate::composer::{Composer, Delete, Motion};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use serde_json::{Value, json};
use std::{
    collections::{BTreeSet, VecDeque},
    fmt, io,
};

const MAX_TEXT: usize = 16384;
const MAX_PENDING: usize = 16;
const MAX_REQUESTS: usize = 100;

#[derive(Clone, Debug, PartialEq)]
pub struct OptionRow {
    pub title: String,
    pub description: String,
}
#[derive(Clone, PartialEq)]
pub struct Question {
    pub label: String,
    pub text: String,
    pub options: Vec<OptionRow>,
    pub multi_select: bool,
    pub secret: bool,
    pub selected: BTreeSet<usize>,
    pub note: String,
}
impl fmt::Debug for Question {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Question")
            .field("label", &self.label)
            .field("secret", &self.secret)
            .finish_non_exhaustive()
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Request {
    pub id: String,
    pub questions: Vec<Question>,
}
#[derive(Debug, PartialEq)]
pub enum Event {
    Requested(Request),
    Resolved { id: String, cancelled: bool },
    Acknowledged { id: String, accepted: bool },
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Action {
    Submit,
    Chat,
    Cancel,
}
impl Action {
    fn wire(self) -> &'static str {
        match self {
            Self::Submit => "submit",
            Self::Chat => "chat",
            Self::Cancel => "cancel",
        }
    }
}
#[derive(Clone, PartialEq)]
pub struct Answer {
    pub selected: BTreeSet<usize>,
    pub note: String,
}
#[derive(Clone, PartialEq)]
pub struct Decision {
    pub id: String,
    pub action: Action,
    pub answers: Vec<Answer>,
}
// Notes may be secrets. Never include answer values in debug/error logs.
impl fmt::Debug for Decision {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("QuestionDecision")
            .field("id", &self.id)
            .field("action", &self.action)
            .finish_non_exhaustive()
    }
}
fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text(value: &Value, key: &str, nonempty: bool) -> io::Result<String> {
    value[key]
        .as_str()
        .filter(|s| s.len() <= MAX_TEXT && (!nonempty || !s.is_empty()))
        .map(str::to_owned)
        .ok_or_else(|| fail("Invalid question text"))
}
fn id(value: &Value) -> io::Result<String> {
    let id = text(value, "id", true)?;
    if id.len() > 128
        || id
            .chars()
            .any(|c| c.is_whitespace() || crate::display_control(c))
    {
        return Err(fail("Invalid question ID"));
    }
    Ok(id)
}
impl Request {
    pub fn text_bytes(&self) -> usize {
        self.id.len()
            + self
                .questions
                .iter()
                .map(|q| {
                    q.label.len()
                        + q.text.len()
                        + q.note.len()
                        + q.options
                            .iter()
                            .map(|o| o.title.len() + o.description.len())
                            .sum::<usize>()
                })
                .sum::<usize>()
    }
    fn parse(value: &Value) -> io::Result<Self> {
        let raw = value["questions"]
            .as_array()
            .filter(|qs| !qs.is_empty() && qs.len() <= 8)
            .ok_or_else(|| fail("Question request needs 1–8 questions"))?;
        let mut request = Self {
            id: id(value)?,
            questions: Vec::new(),
        };
        for q in raw {
            let options = q["options"]
                .as_array()
                .filter(|opts| opts.len() <= 10)
                .ok_or_else(|| fail("Question needs at most 10 options"))?
                .iter()
                .map(|o| {
                    Ok(OptionRow {
                        title: text(o, "title", true)?,
                        description: text(o, "description", false)?,
                    })
                })
                .collect::<io::Result<Vec<_>>>()?;
            let multi_select = q["multiSelect"]
                .as_bool()
                .ok_or_else(|| fail("Missing question multiSelect"))?;
            let secret = match q.get("isSecret") {
                None => false,
                Some(value) => value.as_bool().ok_or_else(|| fail("Invalid isSecret"))?,
            };
            let selected = q["selected"]
                .as_array()
                .filter(|v| v.len() <= options.len())
                .ok_or_else(|| fail("Invalid initial question selection"))?
                .iter()
                .map(|i| {
                    i.as_u64()
                        .and_then(|n| usize::try_from(n).ok())
                        .filter(|n| *n < options.len())
                        .ok_or_else(|| fail("Invalid selected option"))
                })
                .collect::<io::Result<BTreeSet<_>>>()?;
            let note = text(q, "note", false)?;
            if (!multi_select && selected.len() > 1) || note.chars().count() > 1024 {
                return Err(fail("Invalid initial question answer"));
            }
            if q["answered"].as_bool() != Some(!selected.is_empty() || !note.trim().is_empty()) {
                return Err(fail("Question answered flag disagrees with content"));
            }
            request.questions.push(Question {
                label: text(q, "label", true)?,
                text: text(q, "question", true)?,
                options,
                multi_select,
                secret,
                selected,
                note,
            });
            if request.text_bytes() > MAX_TEXT {
                return Err(fail("Question display text limit exceeded"));
            }
        }
        // Bound escaping/wrapping before rendering; metadata and secret notes count too.
        for q in &request.questions {
            crate::live_chat::display_units(&q.text)?;
            crate::live_chat::display_units(&q.note)?;
        }
        let combined = request
            .questions
            .iter()
            .flat_map(|q| {
                std::iter::once(q.label.as_str())
                    .chain(std::iter::once(q.text.as_str()))
                    .chain(std::iter::once(q.note.as_str()))
                    .chain(
                        q.options
                            .iter()
                            .flat_map(|o| [o.title.as_str(), o.description.as_str()]),
                    )
            })
            .collect::<Vec<_>>()
            .join("\n");
        crate::live_chat::display_units(&combined)?;
        Ok(request)
    }
}
struct Record {
    id: String,
    shape: Vec<(usize, bool)>,
    command: Option<String>,
    closed: bool,
    acknowledged: bool,
}
#[derive(Default)]
pub struct Ledger {
    records: Vec<Record>,
    sequence: u64,
}
impl Ledger {
    pub fn request(&mut self, value: &Value) -> io::Result<Event> {
        let request = Request::parse(value)?;
        if self.records.len() >= MAX_REQUESTS
            || self.records.iter().filter(|r| !r.closed).count() >= MAX_PENDING
            || self.records.iter().any(|r| r.id == request.id)
        {
            return Err(fail("Question limit or reused ID"));
        }
        self.records.push(Record {
            id: request.id.clone(),
            shape: request
                .questions
                .iter()
                .map(|q| (q.options.len(), q.multi_select))
                .collect(),
            command: None,
            closed: false,
            acknowledged: false,
        });
        Ok(Event::Requested(request))
    }
    pub fn decide(&mut self, decision: Decision) -> io::Result<Option<Value>> {
        let index = self
            .records
            .iter()
            .position(|r| r.id == decision.id)
            .ok_or_else(|| fail("Answer for unknown question"))?;
        let record = &self.records[index];
        if record.closed {
            return Ok(None);
        } // Source closure wins over unsent input.
        if record.command.is_some() {
            return Err(fail("Question was already answered"));
        }
        if decision.action != Action::Cancel {
            if decision.answers.len() != record.shape.len() {
                return Err(fail("Wrong question answer count"));
            }
            let mut bytes = 0usize;
            for (answer, &(count, multi)) in decision.answers.iter().zip(&record.shape) {
                bytes = bytes.saturating_add(answer.note.len());
                if answer.selected.iter().any(|n| *n >= count)
                    || (!multi && answer.selected.len() > 1)
                    || answer.note.chars().count() > 1024
                    || bytes > MAX_TEXT
                    || (decision.action == Action::Submit
                        && answer.selected.is_empty()
                        && answer.note.trim().is_empty())
                {
                    return Err(fail("Incomplete or invalid question answer"));
                }
            }
        }
        let command_id = loop {
            let id = format!("rust-question-answer-{}", self.sequence);
            self.sequence = self
                .sequence
                .checked_add(1)
                .ok_or_else(|| fail("Question command ID exhausted"))?;
            if !self.records.iter().any(|r| r.id == id) {
                break id;
            }
        };
        let answers = if decision.action == Action::Cancel {
            Vec::new()
        } else {
            decision
                .answers
                .iter()
                .map(|a| json!({"selected":a.selected, "note":a.note}))
                .collect()
        };
        self.records[index].command = Some(command_id.clone());
        Ok(Some(
            json!({"type":"answer","id":command_id,"ask_id":decision.id,
            "action":decision.action.wire(),"answers":answers}),
        ))
    }
    pub fn resolved(&mut self, value: &Value) -> io::Result<Event> {
        let id = id(value)?;
        // Current source emits this only for host/lifetime cancellation.
        if value["cancelled"].as_bool() != Some(true) {
            return Err(fail("Unknown question resolution"));
        }
        let record = self
            .records
            .iter_mut()
            .find(|r| r.id == id)
            .ok_or_else(|| fail("Resolution for unknown question"))?;
        if record.closed {
            return Err(fail("Duplicate question resolution"));
        }
        record.closed = true;
        Ok(Event::Resolved {
            id,
            cancelled: true,
        })
    }
    pub fn response(&mut self, value: &Value) -> io::Result<Option<Event>> {
        let Some(record) = self
            .records
            .iter_mut()
            .find(|r| r.command.as_deref() == value["id"].as_str() && r.command.is_some())
        else {
            return Ok(None);
        };
        if record.acknowledged {
            return Err(fail("Duplicate question response"));
        }
        let accepted = match value["ok"].as_bool() {
            Some(true)
                if value["result"]["answered"] == record.id
                    && value["result"]["id_key"] == "ask_id" =>
            {
                true
            }
            Some(false)
                if record.closed
                    && value["error"]
                        .as_str()
                        .is_some_and(|s| !s.is_empty() && s.len() <= 4096) =>
            {
                false
            }
            _ => {
                return Err(fail(
                    "Rejected or malformed question response; no automatic retry",
                ));
            }
        };
        record.acknowledged = true;
        record.closed = true;
        Ok(Some(Event::Acknowledged {
            id: record.id.clone(),
            accepted,
        }))
    }
    pub fn accounted_for(&self) -> bool {
        self.records.iter().all(|r| r.closed || r.command.is_some())
    }
    pub fn settled(&self) -> bool {
        self.records
            .iter()
            .all(|r| r.closed && (r.command.is_none() || r.acknowledged))
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum HitTarget {
    Question(usize),
    Control(usize),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Hit {
    pub rect: ratatui::layout::Rect,
    pub target: HitTarget,
}
pub struct Pending {
    pub request: Request,
    pub answers: Vec<BTreeSet<usize>>,
    pub notes: Vec<Composer>,
    pub active: usize,
    pub focus: usize,
    pub scroll: usize,
    pub follow_focus: bool,
    pub waiting: bool,
    pub notice: String,
    pub hits: Vec<Hit>,
    pressed: Option<HitTarget>,
}
impl Pending {
    fn new(request: Request) -> Self {
        let notes = request
            .questions
            .iter()
            .map(|q| {
                let mut editor = Composer::new(1024, 0);
                editor.insert(&q.note);
                editor
            })
            .collect();
        let answers = request
            .questions
            .iter()
            .map(|q| q.selected.clone())
            .collect();
        Self {
            request,
            answers,
            notes,
            active: 0,
            focus: 0,
            scroll: 0,
            follow_focus: true,
            waiting: false,
            notice: String::new(),
            hits: Vec::new(),
            pressed: None,
        }
    }
    pub fn invalidate_hits(&mut self) {
        self.hits.clear();
        self.pressed = None;
    }
    pub fn set_hits(&mut self, hits: Vec<Hit>) {
        if self.hits != hits {
            self.pressed = None;
        }
        self.hits = hits;
    }
    pub fn question(&self) -> &Question {
        &self.request.questions[self.active]
    }
    pub fn answered(&self, index: usize) -> bool {
        !self.answers[index].is_empty() || !self.notes[index].text().trim().is_empty()
    }
    pub fn note_focused(&self) -> bool {
        self.focus == self.question().options.len() + 1
    }
    pub fn jump(&mut self, index: usize) {
        if !self.waiting && index < self.request.questions.len() {
            self.invalidate_hits();
            self.active = index;
            self.focus = 0;
            self.scroll = 0;
            self.follow_focus = true;
        }
    }
    pub fn paste(&mut self, text: &str, clipboard: bool) {
        if !self.waiting && self.note_focused() {
            if clipboard {
                self.notes[self.active].paste_clipboard(text);
            } else {
                self.notes[self.active].paste_bracketed(text);
            }
            self.follow_focus = true;
        }
    }
    fn decision(&mut self, action: Action) -> Option<Decision> {
        if self.waiting {
            return None;
        }
        if action == Action::Submit
            && let Some(i) = (0..self.request.questions.len()).find(|i| !self.answered(*i))
        {
            self.jump(i);
            self.notice =
                "Choose an option or add a note; Chat about this permits partial answers.".into();
            return None;
        }
        let answers: Vec<Answer> = if action == Action::Cancel {
            Vec::new()
        } else {
            self.answers
                .iter()
                .zip(&self.notes)
                .map(|(selected, note)| Answer {
                    selected: selected.clone(),
                    note: note.text().into(),
                })
                .collect()
        };
        if answers.iter().map(|a| a.note.len()).sum::<usize>() > MAX_TEXT {
            self.notice = "Notes exceed 16 KiB; shorten them before sending.".into();
            return None;
        }
        self.waiting = true;
        Some(Decision {
            id: self.request.id.clone(),
            action,
            answers,
        })
    }
    pub fn activate(&mut self) -> Option<Decision> {
        if self.waiting {
            return None;
        }
        let count = self.question().options.len();
        if (1..=count).contains(&self.focus) {
            let index = self.focus - 1;
            if !self.answers[self.active].remove(&index) {
                if !self.question().multi_select {
                    self.answers[self.active].clear();
                }
                self.answers[self.active].insert(index);
            }
        } else if self.focus == count + 2 {
            self.answers[self.active].clear();
            self.notes[self.active].clear();
        } else if self.focus == count + 3 {
            if self.active + 1 < self.request.questions.len() {
                self.jump(self.active + 1);
            } else {
                return self.decision(Action::Submit);
            }
        } else if self.focus == count + 4 {
            return self.decision(Action::Chat);
        } else if self.focus == count + 5 {
            return self.decision(Action::Cancel);
        }
        None
    }
    pub fn key(&mut self, event: KeyEvent) -> Option<Decision> {
        if self.waiting || event.kind != KeyEventKind::Press {
            return None;
        }
        self.pressed = None;
        let control = event.modifiers == KeyModifiers::CONTROL;
        let shift = event.modifiers == KeyModifiers::SHIFT;
        if event.modifiers.is_empty() {
            match event.code {
                KeyCode::Esc => return self.decision(Action::Cancel),
                KeyCode::Tab | KeyCode::Down => {
                    self.focus = (self.focus + 1) % (self.question().options.len() + 6);
                    self.follow_focus = true;
                    return None;
                }
                KeyCode::Up => {
                    self.focus = (self.focus + self.question().options.len() + 5)
                        % (self.question().options.len() + 6);
                    self.follow_focus = true;
                    return None;
                }
                KeyCode::PageDown => {
                    self.scroll = self.scroll.saturating_add(10);
                    self.follow_focus = false;
                    return None;
                }
                KeyCode::PageUp => {
                    self.scroll = self.scroll.saturating_sub(10);
                    self.follow_focus = false;
                    return None;
                }
                KeyCode::Left | KeyCode::Right if self.focus == 0 => {
                    let count = self.request.questions.len();
                    let next = if event.code == KeyCode::Right {
                        (self.active + 1) % count
                    } else {
                        (self.active + count - 1) % count
                    };
                    self.jump(next);
                    return None;
                }
                KeyCode::Enter => return self.activate(),
                KeyCode::Char(' ') if !self.note_focused() => return self.activate(),
                _ => {}
            }
        }
        if shift && event.code == KeyCode::BackTab {
            self.focus = (self.focus + self.question().options.len() + 5)
                % (self.question().options.len() + 6);
            self.follow_focus = true;
            return None;
        }
        if !self.note_focused() {
            return None;
        }
        let note = &mut self.notes[self.active];
        match event.code {
            KeyCode::Left if !control => note.move_cursor(Motion::Left, shift),
            KeyCode::Right if !control => note.move_cursor(Motion::Right, shift),
            KeyCode::Home => note.move_cursor(Motion::Home, shift),
            KeyCode::End => note.move_cursor(Motion::End, shift),
            KeyCode::Backspace if event.modifiers.is_empty() => note.delete(Delete::Left),
            KeyCode::Delete if event.modifiers.is_empty() => note.delete(Delete::Right),
            KeyCode::Char('a' | 'A') if control => note.select_all(),
            KeyCode::Char(c) if event.modifiers.is_empty() || shift => note.insert(&c.to_string()),
            _ => {}
        }
        self.follow_focus = true;
        None
    }
}
#[derive(Default)]
pub struct View {
    pending: VecDeque<Pending>,
    decision: Option<Decision>,
}
impl View {
    pub fn current(&self) -> Option<&Pending> {
        self.pending.front()
    }
    pub fn current_mut(&mut self) -> Option<&mut Pending> {
        self.pending.front_mut()
    }
    pub fn is_empty(&self) -> bool {
        self.pending.is_empty()
    }
    pub fn observe(&mut self, event: Event) -> io::Result<String> {
        match event {
            Event::Requested(request) => {
                if self.pending.len() >= MAX_PENDING
                    || self.pending.iter().any(|r| r.request.id == request.id)
                {
                    return Err(fail("Question view limit or duplicate"));
                }
                self.pending.push_back(Pending::new(request));
                Ok("Question requested. No automatic answer.".into())
            }
            Event::Resolved { id, .. } => {
                self.close(&id)?;
                Ok("Question cancelled by the runtime.".into())
            }
            Event::Acknowledged { id, accepted } => {
                if self.pending.iter().any(|r| r.request.id == id) {
                    self.close(&id)?;
                }
                Ok(if accepted {
                    "Question answer accepted; turn still running."
                } else {
                    "Question was already cancelled; answer not applied."
                }
                .into())
            }
        }
    }
    fn close(&mut self, id: &str) -> io::Result<()> {
        let index = self
            .pending
            .iter()
            .position(|r| r.request.id == id)
            .ok_or_else(|| fail("Question closure without visible request"))?;
        self.pending.remove(index);
        if self.decision.as_ref().is_some_and(|d| d.id == id) {
            self.decision = None;
        }
        Ok(())
    }
    pub fn mouse(&mut self, event: crossterm::event::MouseEvent) {
        use crossterm::event::{MouseButton, MouseEventKind};
        let Some(pending) = self.pending.front_mut().filter(|p| !p.waiting) else {
            return;
        };
        let hit = pending
            .hits
            .iter()
            .find(|hit| {
                hit.rect
                    .contains(ratatui::layout::Position::new(event.column, event.row))
            })
            .map(|hit| hit.target);
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => pending.pressed = hit,
            MouseEventKind::Up(MouseButton::Left) => {
                if pending
                    .pressed
                    .take()
                    .is_some_and(|pressed| Some(pressed) == hit)
                {
                    match hit {
                        Some(HitTarget::Question(index)) => pending.jump(index),
                        Some(HitTarget::Control(focus)) => {
                            pending.focus = focus;
                            pending.follow_focus = true;
                            if let Some(decision) = pending.activate() {
                                self.decision = Some(decision);
                            }
                        }
                        None => {}
                    }
                }
            }
            MouseEventKind::ScrollUp => {
                pending.invalidate_hits();
                pending.scroll = pending.scroll.saturating_sub(3);
                pending.follow_focus = false;
            }
            MouseEventKind::ScrollDown => {
                pending.invalidate_hits();
                pending.scroll = pending.scroll.saturating_add(3);
                pending.follow_focus = false;
            }
            _ => {}
        }
    }
    pub fn key(&mut self, event: KeyEvent) {
        if let Some(pending) = self.pending.front_mut()
            && let Some(decision) = pending.key(event)
        {
            self.decision = Some(decision);
        }
    }
    pub fn activate(&mut self) {
        if let Some(pending) = self.pending.front_mut()
            && let Some(decision) = pending.activate()
        {
            self.decision = Some(decision);
        }
    }
    pub fn take_decision(&mut self) -> Option<Decision> {
        self.decision.take()
    }
    pub fn clear(&mut self) {
        self.pending.clear();
        self.decision = None;
    }
}
