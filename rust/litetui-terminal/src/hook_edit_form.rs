//! The bounded hook form. It holds typed drafts only; the source's validator decides what is acceptable.
use crate::hook_edit::{
    Action, Catalog, Hook, MAX_ARGUMENT, MAX_ARGV, MAX_EXECUTABLE, MAX_ID, MAX_TOOL, MAX_TOOLS,
};
use crate::{live::terminal_text, prepared_skill::BoundedDraft};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use std::io;

fn refuse(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum Field {
    Id,
    Events,
    Executable,
    Argv,
    Mode,
    Timeout,
    Enabled,
    Sources,
    Tools,
}
const ORDER: [Field; 9] = [
    Field::Id,
    Field::Events,
    Field::Executable,
    Field::Argv,
    Field::Mode,
    Field::Timeout,
    Field::Enabled,
    Field::Sources,
    Field::Tools,
];
impl Field {
    fn label(self) -> &'static str {
        match self {
            Self::Id => "Hook ID",
            Self::Events => "Events (Space selects)",
            Self::Executable => "Executable",
            Self::Argv => "Arguments (JSON array of strings)",
            Self::Mode => "Mode (Space switches)",
            Self::Timeout => "Timeout in seconds",
            Self::Enabled => "Enabled (Space switches)",
            Self::Sources => "Prompt sources (none selected = all)",
            Self::Tools => "Tool filters (JSON array, * and ? wildcards)",
        }
    }
    fn limit(self) -> usize {
        match self {
            Self::Id => MAX_ID,
            Self::Executable => MAX_EXECUTABLE,
            Self::Argv => MAX_ARGV * (MAX_ARGUMENT + 8) + 2,
            Self::Timeout => 16,
            Self::Tools => MAX_TOOLS * (MAX_TOOL + 8) + 2,
            _ => 0,
        }
    }
}

#[derive(Default)]
struct Line(BoundedDraft<70_000>);
impl Line {
    fn with(text: &str) -> Self {
        let mut line = Self::default();
        line.0.set(text).expect("bounded");
        line
    }
    fn text(&self) -> &str {
        self.0.text()
    }
}

pub struct Form {
    catalog: Catalog,
    pub action: Action,
    original: Option<Hook>,
    id: Line,
    executable: Line,
    argv: Line,
    timeout: Line,
    tools: Line,
    events: Vec<bool>,
    sources: Vec<bool>,
    gate: bool,
    enabled: bool,
    cursor: [usize; 2],
    selected: usize,
}

impl Form {
    pub fn create(catalog: &Catalog) -> Self {
        Self {
            events: vec![false; catalog.events.len()],
            sources: vec![false; catalog.sources.len()],
            catalog: catalog.clone(),
            action: Action::Create,
            original: None,
            id: Line::default(),
            executable: Line::default(),
            argv: Line::with("[]"),
            timeout: Line::with("10"),
            tools: Line::with("[]"),
            gate: false,
            enabled: true,
            cursor: [0, 0],
            selected: 0,
        }
    }
    pub fn edit(catalog: &Catalog, hook: &Hook) -> Self {
        let json = |items: &[String]| serde_json::to_string(items).expect("strings");
        let picked = |all: &[String], chosen: &[String]| {
            all.iter().map(|v| chosen.contains(v)).collect::<Vec<_>>()
        };
        Self {
            events: picked(&catalog.events, &hook.events),
            sources: picked(&catalog.sources, &hook.sources),
            catalog: catalog.clone(),
            action: Action::Update,
            original: Some(hook.clone()),
            id: Line::with(&hook.id),
            executable: Line::with(&hook.executable),
            argv: Line::with(&json(&hook.argv)),
            timeout: Line::with(&hook.timeout.to_string()),
            tools: Line::with(&json(&hook.tools)),
            gate: hook.mode == "gate",
            enabled: hook.enabled,
            cursor: [0, 0],
            selected: 2,
        }
    }
    fn step(&mut self, forward: bool) {
        self.selected = if forward {
            (self.selected + 1) % ORDER.len()
        } else {
            (self.selected + ORDER.len() - 1) % ORDER.len()
        };
    }
    pub fn field(&self) -> Field {
        ORDER[self.selected]
    }
    fn text_mut(&mut self) -> Option<&mut Line> {
        match self.field() {
            Field::Id if self.action == Action::Create => Some(&mut self.id),
            Field::Executable => Some(&mut self.executable),
            Field::Argv => Some(&mut self.argv),
            Field::Timeout => Some(&mut self.timeout),
            Field::Tools => Some(&mut self.tools),
            _ => None,
        }
    }
    pub fn accepts_paste(&self) -> bool {
        !(matches!(
            self.field(),
            Field::Events | Field::Mode | Field::Enabled | Field::Sources
        ) || self.field() == Field::Id && self.action == Action::Update)
    }
    pub fn paste(&mut self, text: &str) -> io::Result<()> {
        let text = text.trim_end_matches(['\r', '\n']);
        if text.chars().any(char::is_control) {
            return Err(refuse("This field takes a single line of plain text"));
        }
        let limit = self.field().limit();
        let Some(line) = self.text_mut() else {
            return Err(refuse("Space switches this field"));
        };
        if line.text().len() + text.len() > limit {
            return Err(refuse("This field is at its size limit"));
        }
        line.0.insert(text)
    }
    fn choice(&mut self) -> Option<(&mut Vec<bool>, &mut usize)> {
        match self.field() {
            Field::Events => Some((&mut self.events, &mut self.cursor[0])),
            Field::Sources => Some((&mut self.sources, &mut self.cursor[1])),
            _ => None,
        }
    }
    /// Returns true when Enter asks for the source's review.
    pub fn key(&mut self, key: KeyEvent) -> io::Result<bool> {
        if key.kind != KeyEventKind::Press {
            return Ok(false);
        }
        let plain = key.modifiers.is_empty() || key.modifiers == KeyModifiers::SHIFT;
        match key.code {
            KeyCode::Enter if key.modifiers.is_empty() => return Ok(true),
            KeyCode::Tab if key.modifiers.is_empty() => {
                self.step(true);
                return Ok(false);
            }
            KeyCode::BackTab => {
                self.step(false);
                return Ok(false);
            }
            _ => {}
        }
        let field = self.field();
        if let Some((flags, cursor)) = self.choice() {
            if key.modifiers.is_empty() {
                match key.code {
                    KeyCode::Up | KeyCode::Left => *cursor = cursor.saturating_sub(1),
                    KeyCode::Down | KeyCode::Right => {
                        *cursor = (*cursor + 1).min(flags.len().saturating_sub(1))
                    }
                    KeyCode::Char(' ') => {
                        if let Some(flag) = flags.get_mut(*cursor) {
                            *flag = !*flag;
                        }
                    }
                    _ => {}
                }
            }
            return Ok(false);
        }
        if matches!(field, Field::Mode | Field::Enabled) {
            if key.modifiers.is_empty()
                && matches!(
                    key.code,
                    KeyCode::Char(' ') | KeyCode::Left | KeyCode::Right
                )
            {
                if field == Field::Mode {
                    self.gate = !self.gate;
                } else {
                    self.enabled = !self.enabled;
                }
            } else if key.modifiers.is_empty() && matches!(key.code, KeyCode::Up | KeyCode::Down) {
                self.step(key.code == KeyCode::Down);
            }
            return Ok(false);
        }
        if key.modifiers.is_empty() && matches!(key.code, KeyCode::Up | KeyCode::Down) {
            self.step(key.code == KeyCode::Down);
            return Ok(false);
        }
        let writable = self.accepts_paste();
        if key.code == KeyCode::Char('u') && key.modifiers == KeyModifiers::CONTROL {
            if let (true, Some(line)) = (writable, self.text_mut()) {
                line.0.set("")?;
            }
            return Ok(false);
        }
        if !plain || !writable {
            return Ok(false);
        }
        match key.code {
            KeyCode::Char(ch) => self.paste(&ch.to_string())?,
            code => {
                if let Some(line) = self.text_mut() {
                    line.0.key(code);
                }
            }
        }
        Ok(false)
    }
    /// The typed draft as a hook. Only bounds are enforced here; the source validates meaning.
    pub fn hook(&self) -> io::Result<Hook> {
        let list =
            |line: &Line, count: usize, each: usize, what: &str| -> io::Result<Vec<String>> {
                let text = line.text().trim();
                if text.is_empty() {
                    return Ok(Vec::new());
                }
                let items: Vec<String> = serde_json::from_str(text).map_err(|_| {
                refuse(&format!(
                    "{what} must be a JSON array of strings, for example [\"--flag\", \"value\"]"
                ))
            })?;
                if items.len() > count || items.iter().any(|s| s.len() > each || s.contains('\0')) {
                    return Err(refuse(&format!("{what} exceed the editing limits")));
                }
                Ok(items)
            };
        let timeout = self
            .timeout
            .text()
            .trim()
            .parse::<f64>()
            .ok()
            .filter(|t| t.is_finite())
            .ok_or_else(|| refuse("Timeout must be a number of seconds"))?;
        let picked = |all: &[String], flags: &[bool]| {
            all.iter()
                .zip(flags)
                .filter(|(_, on)| **on)
                .map(|(v, _)| v.clone())
                .collect::<Vec<_>>()
        };
        for (text, max, what) in [
            (self.id.text(), MAX_ID, "Hook ID"),
            (self.executable.text(), MAX_EXECUTABLE, "Executable"),
        ] {
            if text.len() > max || text.contains('\0') {
                return Err(refuse(&format!("{what} exceeds the editing limits")));
            }
        }
        Ok(Hook {
            id: self.id.text().to_owned(),
            events: picked(&self.catalog.events, &self.events),
            executable: self.executable.text().to_owned(),
            argv: list(&self.argv, MAX_ARGV, MAX_ARGUMENT, "Arguments")?,
            enabled: self.enabled,
            mode: if self.gate { "gate" } else { "observe" }.into(),
            timeout,
            sources: picked(&self.catalog.sources, &self.sources),
            tools: list(&self.tools, MAX_TOOLS, MAX_TOOL, "Tool filters")?,
            env_count: self.original.as_ref().map_or(0, |h| h.env_count),
            custom_cwd: self.original.as_ref().is_some_and(|h| h.custom_cwd),
        })
    }
    pub fn original(&self) -> Option<&Hook> {
        self.original.as_ref()
    }
    fn summary(&self, field: Field) -> String {
        let flags = |all: &[String], on: &[bool], none: &str| {
            let chosen: Vec<_> = all
                .iter()
                .zip(on)
                .filter(|(_, on)| **on)
                .map(|(v, _)| v.as_str())
                .collect();
            if chosen.is_empty() {
                none.to_owned()
            } else {
                chosen.join(", ")
            }
        };
        match field {
            Field::Id => self.id.text().to_owned(),
            Field::Events => flags(&self.catalog.events, &self.events, "(none selected)"),
            Field::Executable => self.executable.text().to_owned(),
            Field::Argv => self.argv.text().to_owned(),
            Field::Mode => if self.gate { "gate" } else { "observe" }.into(),
            Field::Timeout => self.timeout.text().to_owned(),
            Field::Enabled => self.enabled.to_string(),
            Field::Sources => flags(&self.catalog.sources, &self.sources, "(all)"),
            Field::Tools => self.tools.text().to_owned(),
        }
    }
    pub fn visible_lines(&self, width: usize, height: usize) -> Vec<String> {
        let width = width.max(8);
        let field = self.field();
        let clip = |text: String| {
            let text = terminal_text(&text);
            if text.chars().count() > width {
                text.chars()
                    .take(width.saturating_sub(1))
                    .collect::<String>()
                    + "…"
            } else {
                text
            }
        };
        let head = format!(
            "{} hook · field {}/{}: {}",
            if self.action == Action::Create {
                "New"
            } else {
                "Edit"
            },
            self.selected + 1,
            ORDER.len(),
            field.label()
        );
        let mut detail = Vec::new();
        if let Some(choices) = match field {
            Field::Events => Some((&self.catalog.events, &self.events, self.cursor[0])),
            Field::Sources => Some((&self.catalog.sources, &self.sources, self.cursor[1])),
            _ => None,
        } {
            for (i, name) in choices.0.iter().enumerate() {
                let gate = field == Field::Events && self.catalog.gate_events.contains(name);
                detail.push(format!(
                    "{} [{}] {}{}",
                    if i == choices.2 { "›" } else { " " },
                    if choices.1[i] { "x" } else { " " },
                    name,
                    if gate { "  (can gate)" } else { "" }
                ));
            }
        } else if self.action == Action::Update && field == Field::Id {
            detail.push("The ID of an existing hook is fixed. Create a new hook and delete this one to rename.".into());
        } else if matches!(field, Field::Mode | Field::Enabled) {
            detail.push(format!("Now: {}", self.summary(field)));
        } else if let Some(line) = match field {
            Field::Id => Some(&self.id),
            Field::Executable => Some(&self.executable),
            Field::Argv => Some(&self.argv),
            Field::Timeout => Some(&self.timeout),
            _ => Some(&self.tools),
        } {
            let (before, after) = line.text().split_at(line.0.cursor());
            let text = format!("{}│{}", terminal_text(before), terminal_text(after));
            let wrapped = crate::ui::wrap(&text, width);
            let cursor = crate::ui::wrap(&terminal_text(before), width)
                .len()
                .saturating_sub(1);
            let room = height.saturating_sub(2).max(1);
            let start = cursor.saturating_sub(room - 1);
            detail.extend(wrapped.into_iter().skip(start).take(room));
        }
        let mut out = vec![head];
        if height >= ORDER.len() + 3 + detail.len().min(10) {
            for (i, f) in ORDER.iter().enumerate() {
                out.push(clip(format!(
                    "{} {}: {}",
                    if i == self.selected { "›" } else { " " },
                    f.label().split(" (").next().unwrap_or(""),
                    self.summary(*f)
                )));
            }
            out.push(String::new());
        }
        let room = height.saturating_sub(out.len());
        let focus = match field {
            Field::Events => self.cursor[0],
            Field::Sources => self.cursor[1],
            _ => 0,
        };
        let start = if matches!(field, Field::Events | Field::Sources) {
            (focus + 1).saturating_sub(room.max(1))
        } else {
            0
        };
        out.extend(detail.into_iter().skip(start).take(room));
        out.truncate(height);
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn catalog() -> Catalog {
        Catalog {
            events: vec!["app_start".into(), "tool_before".into()],
            gate_events: vec!["tool_before".into()],
            sources: vec!["typed".into(), "rpc".into()],
        }
    }
    fn press(form: &mut Form, code: KeyCode) -> bool {
        form.key(KeyEvent::new(code, KeyModifiers::NONE)).unwrap()
    }
    fn type_text(form: &mut Form, text: &str) {
        for ch in text.chars() {
            press(form, KeyCode::Char(ch));
        }
    }

    #[test]
    fn typed_create_becomes_the_exact_bounded_hook() {
        let mut form = Form::create(&catalog());
        type_text(&mut form, "audit");
        press(&mut form, KeyCode::Tab);
        press(&mut form, KeyCode::Down);
        press(&mut form, KeyCode::Char(' '));
        press(&mut form, KeyCode::Tab);
        type_text(&mut form, "audit.exe");
        press(&mut form, KeyCode::Tab);
        form.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL))
            .unwrap();
        type_text(&mut form, r#"["--quiet","two words"]"#);
        press(&mut form, KeyCode::Tab);
        press(&mut form, KeyCode::Char(' '));
        press(&mut form, KeyCode::Tab);
        for _ in 0..2 {
            press(&mut form, KeyCode::Backspace);
        }
        type_text(&mut form, "2.5");
        press(&mut form, KeyCode::Tab);
        press(&mut form, KeyCode::Char(' '));
        press(&mut form, KeyCode::Tab);
        press(&mut form, KeyCode::Down);
        press(&mut form, KeyCode::Char(' '));
        press(&mut form, KeyCode::Tab);
        assert!(press(&mut form, KeyCode::Enter));
        let hook = form.hook().unwrap();
        assert_eq!(hook.id, "audit");
        assert_eq!(hook.events, ["tool_before"]);
        assert_eq!(hook.argv, ["--quiet", "two words"]);
        assert_eq!(hook.mode, "gate");
        assert_eq!(hook.timeout, 2.5);
        assert!(!hook.enabled);
        assert_eq!(hook.sources, ["rpc"]);
    }

    #[test]
    fn edit_prefills_and_never_changes_the_fixed_id() {
        let mut hook = Hook {
            id: "keep".into(),
            events: vec!["app_start".into()],
            executable: "run.exe".into(),
            argv: vec!["a b".into()],
            enabled: false,
            mode: "observe".into(),
            timeout: 7.0,
            sources: vec![],
            tools: vec!["w*".into()],
            env_count: 2,
            custom_cwd: true,
        };
        let mut form = Form::edit(&catalog(), &hook);
        assert_eq!(form.hook().unwrap(), hook);
        assert_eq!(form.field(), Field::Executable);
        press(&mut form, KeyCode::BackTab);
        press(&mut form, KeyCode::BackTab);
        assert_eq!(form.field(), Field::Id);
        assert!(!form.accepts_paste());
        type_text(&mut form, "renamed");
        press(&mut form, KeyCode::Backspace);
        assert_eq!(form.hook().unwrap().id, "keep");
        assert!(form.paste("x").is_err());
        hook.timeout = 7.0;
        assert_eq!(form.original(), Some(&hook));
    }

    #[test]
    fn rejects_unbounded_or_malformed_input_without_judging_meaning() {
        let mut form = Form::create(&catalog());
        for _ in 0..3 {
            press(&mut form, KeyCode::Tab);
        }
        form.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL))
            .unwrap();
        type_text(&mut form, "not json");
        assert!(form.hook().unwrap_err().to_string().contains("JSON array"));
        form.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL))
            .unwrap();
        form.paste(&format!("[{}]", vec!["\"x\""; MAX_ARGV + 1].join(",")))
            .unwrap();
        assert!(form.hook().unwrap_err().to_string().contains("limits"));
        assert!(form.paste("two\nlines").is_err());
        assert!(form.paste("\u{1b}[31m").is_err());
        form.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL))
            .unwrap();
        form.paste(&"x".repeat(Field::Argv.limit())).unwrap();
        assert!(form.paste("y").is_err());
        // Meaning is left to the source: an empty ID, no event and zero timeout still build.
        let mut blank = Form::create(&catalog());
        press(&mut blank, KeyCode::Tab);
        press(&mut blank, KeyCode::Tab);
        press(&mut blank, KeyCode::Tab);
        press(&mut blank, KeyCode::Tab);
        press(&mut blank, KeyCode::Tab);
        blank
            .key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL))
            .unwrap();
        type_text(&mut blank, "0");
        let hook = blank.hook().unwrap();
        assert!(hook.id.is_empty() && hook.events.is_empty() && hook.timeout == 0.0);
        type_text(&mut blank, "x");
        assert!(blank.hook().is_err(), "a non-number is not sent");
    }

    #[test]
    fn small_terminals_still_show_the_focused_field() {
        let form = Form::create(&catalog());
        let lines = form.visible_lines(40, 4);
        assert!(lines.len() <= 4 && lines.len() >= 2);
        assert!(lines[0].contains("field 1/9"));
        let tall = form.visible_lines(60, 20);
        assert!(tall.iter().any(|l| l.contains("› Hook ID")));
        assert!(tall.iter().any(|l| l.contains("Timeout in seconds: 10")));
    }
}
