//! Bounded local draft of one new MCP declaration. Environment and header values are masked on
//! screen and never leave the draft except in the single review request.
use crate::{live::terminal_text, prepared_skill::BoundedDraft};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{Map, Value, json};

type Draft = BoundedDraft<8192>;
pub const NAME_MAX: usize = 96;
const COMMAND_MAX: usize = 1024;
const ARGS_MAX: usize = 4096;
const URL_MAX: usize = 2048;
const PAIRS_MAX: usize = 32;
const ARG_MAX: usize = 1024;
const ARGS_COUNT_MAX: usize = 64;
const SECRET_MAX: usize = 8192;
const MASK: &str = "••••";

fn safe_name_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || matches!(c, '_' | '.' | '-')
}
fn env_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '_'
}
fn header_char(c: char) -> bool {
    c.is_ascii_alphanumeric() || c == '-'
}

#[derive(Clone, PartialEq, Eq)]
pub enum Kind {
    Stdio {
        command: String,
        args: Vec<String>,
        env: Vec<(String, String)>,
    },
    Http {
        url: String,
        headers: Vec<(String, String)>,
    },
}
/// What the user typed, ready for the source to validate. Values are secrets: `Debug` omits them.
#[derive(Clone, PartialEq, Eq)]
pub struct Spec {
    pub name: String,
    pub kind: Kind,
}
impl std::fmt::Debug for Spec {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Spec")
            .field("name", &self.name)
            .field("transport", &self.transport())
            .field("args", &self.arg_count())
            .field("env", &self.env_names())
            .field("headers", &self.header_names())
            .finish_non_exhaustive()
    }
}
impl Spec {
    pub fn transport(&self) -> &'static str {
        match self.kind {
            Kind::Stdio { .. } => "stdio",
            Kind::Http { .. } => "http",
        }
    }
    pub fn arg_count(&self) -> usize {
        match &self.kind {
            Kind::Stdio { args, .. } => args.len(),
            Kind::Http { .. } => 0,
        }
    }
    pub fn env_names(&self) -> Vec<String> {
        match &self.kind {
            Kind::Stdio { env, .. } => env.iter().map(|(k, _)| k.clone()).collect(),
            Kind::Http { .. } => Vec::new(),
        }
    }
    pub fn header_names(&self) -> Vec<String> {
        match &self.kind {
            Kind::Http { headers, .. } => headers.iter().map(|(k, _)| k.clone()).collect(),
            Kind::Stdio { .. } => Vec::new(),
        }
    }
    pub fn wire(&self) -> Value {
        let object = |pairs: &[(String, String)]| {
            Value::Object(
                pairs
                    .iter()
                    .map(|(k, v)| (k.clone(), json!(v)))
                    .collect::<Map<_, _>>(),
            )
        };
        match &self.kind {
            Kind::Stdio { command, args, env } => json!({
                "transport": "stdio", "name": self.name, "command": command,
                "args": args, "env": object(env),
            }),
            Kind::Http { url, headers } => json!({
                "transport": "http", "name": self.name, "url": url, "headers": object(headers),
            }),
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Field {
    Name,
    Transport,
    Command,
    Args,
    Env,
    Url,
    Headers,
}
impl Field {
    fn label(self) -> &'static str {
        match self {
            Self::Name => "Server name",
            Self::Transport => "Transport",
            Self::Command => "Command",
            Self::Args => "Arguments (separated by spaces)",
            Self::Env => "Environment (NAME=value, one per line)",
            Self::Url => "URL (http:// or https://)",
            Self::Headers => "Headers (Name: value, one per line)",
        }
    }
    fn secret(self) -> bool {
        matches!(self, Self::Env | Self::Headers)
    }
    fn limit(self) -> usize {
        match self {
            Self::Name => NAME_MAX,
            Self::Command => COMMAND_MAX,
            Self::Args => ARGS_MAX,
            Self::Url => URL_MAX,
            Self::Env | Self::Headers => SECRET_MAX,
            Self::Transport => 0,
        }
    }
}
const STDIO: [Field; 5] = [
    Field::Name,
    Field::Transport,
    Field::Command,
    Field::Args,
    Field::Env,
];
const HTTP: [Field; 4] = [Field::Name, Field::Transport, Field::Url, Field::Headers];

#[derive(Default)]
pub struct Form {
    name: Draft,
    command: Draft,
    args: Draft,
    env: Draft,
    url: Draft,
    headers: Draft,
    http: bool,
    selected: usize,
    pub note: String,
}

fn parse_pairs(
    text: &str,
    delimiter: char,
    valid: fn(char) -> bool,
    label: &str,
) -> Result<Vec<(String, String)>, String> {
    let mut pairs: Vec<(String, String)> = Vec::new();
    for (number, line) in text.split('\n').enumerate() {
        if line.trim().is_empty() {
            continue;
        }
        let Some((name, value)) = line.split_once(delimiter) else {
            return Err(format!(
                "{label} line {} needs {}",
                number + 1,
                if delimiter == '=' {
                    "NAME=value"
                } else {
                    "Name: value"
                }
            ));
        };
        let value = value.trim();
        if name.is_empty()
            || name.len() > NAME_MAX
            || !name.chars().all(valid)
            || (delimiter == '=' && name.starts_with(|c: char| c.is_ascii_digit()))
        {
            return Err(format!("{label} line {} has an invalid name", number + 1));
        }
        if value.is_empty() {
            return Err(format!("{label} line {} needs a value", number + 1));
        }
        if pairs.iter().any(|(existing, _)| existing == name) {
            return Err(format!("{label} line {} repeats a name", number + 1));
        }
        pairs.push((name.to_owned(), value.to_owned()));
    }
    if pairs.len() > PAIRS_MAX {
        return Err(format!("{label} accepts at most {PAIRS_MAX} entries"));
    }
    Ok(pairs)
}

fn mask_line(line: &str, delimiter: char, valid: fn(char) -> bool) -> String {
    let (name, value) = match line.split_once(delimiter) {
        Some((name, value)) => (name, Some(value)),
        None => (line, None),
    };
    // Text before the delimiter that is not a legal name could be a value typed in the wrong
    // place, so only legal name characters are ever shown.
    let mut shown: String = name
        .chars()
        .map(|c| if valid(c) { c } else { '•' })
        .collect();
    if let Some(value) = value {
        shown.push(delimiter);
        if !value.trim().is_empty() {
            shown.push_str(MASK);
        }
    }
    shown
}

fn shorten(text: &str, max: usize) -> String {
    let chars: Vec<char> = text.chars().collect();
    if chars.len() <= max {
        text.to_owned()
    } else {
        format!("{}…", chars[..max].iter().collect::<String>())
    }
}

impl Form {
    fn fields(&self) -> &'static [Field] {
        if self.http { &HTTP } else { &STDIO }
    }
    fn current(&self) -> Field {
        self.fields()[self.selected.min(self.fields().len() - 1)]
    }
    fn draft(&self, field: Field) -> Option<&Draft> {
        Some(match field {
            Field::Name => &self.name,
            Field::Command => &self.command,
            Field::Args => &self.args,
            Field::Env => &self.env,
            Field::Url => &self.url,
            Field::Headers => &self.headers,
            Field::Transport => return None,
        })
    }
    fn draft_mut(&mut self, field: Field) -> Option<&mut Draft> {
        Some(match field {
            Field::Name => &mut self.name,
            Field::Command => &mut self.command,
            Field::Args => &mut self.args,
            Field::Env => &mut self.env,
            Field::Url => &mut self.url,
            Field::Headers => &mut self.headers,
            Field::Transport => return None,
        })
    }
    pub fn accepts_paste(&self) -> bool {
        self.current() != Field::Transport
    }
    pub fn paste(&mut self, text: &str) {
        let field = self.current();
        if field == Field::Transport {
            self.note = "Space switches between stdio and http.".into();
            return;
        }
        let text = if field.secret() {
            text.replace("\r\n", "\n").replace('\r', "\n")
        } else {
            text.to_owned()
        };
        if text
            .chars()
            .any(|c| c.is_control() && !(field.secret() && c == '\n'))
        {
            self.note = "That text contains line breaks or control characters.".into();
            return;
        }
        let draft = self.draft_mut(field).expect("text field");
        if draft.text().len() + text.len() > field.limit() {
            self.note = "That would exceed the size limit for this field.".into();
            return;
        }
        let _ = draft.insert(&text);
    }
    fn step(&mut self, forward: bool) {
        let length = self.fields().len();
        self.selected = if forward {
            (self.selected + 1) % length
        } else {
            (self.selected + length - 1) % length
        };
    }
    /// True when Enter asks to review. Everything else edits the draft in place.
    pub fn key(&mut self, key: KeyEvent) -> bool {
        self.note.clear();
        let plain = key.modifiers.is_empty();
        let shift = key.modifiers == KeyModifiers::SHIFT;
        let control = key.modifiers == KeyModifiers::CONTROL;
        let field = self.current();
        match key.code {
            KeyCode::Enter if plain => return true,
            KeyCode::Enter if shift || key.modifiers == KeyModifiers::ALT => self.newline(),
            KeyCode::Char('n') if control => self.newline(),
            KeyCode::Tab | KeyCode::Down if plain => self.step(true),
            KeyCode::BackTab => self.step(false),
            KeyCode::Up if plain => self.step(false),
            KeyCode::Char('u') if control => {
                if let Some(draft) = self.draft_mut(field) {
                    let _ = draft.set("");
                }
            }
            KeyCode::Char(' ') | KeyCode::Left | KeyCode::Right
                if plain && field == Field::Transport =>
            {
                self.http = !self.http;
                self.selected = 1;
            }
            KeyCode::Char(c) if (plain || shift) && !c.is_control() => {
                self.paste(&c.to_string());
            }
            KeyCode::Backspace
            | KeyCode::Delete
            | KeyCode::Left
            | KeyCode::Right
            | KeyCode::Home
            | KeyCode::End
                if plain =>
            {
                // Secret fields only ever grow or shrink at the end, so what is masked is
                // what is edited.
                if (!field.secret() || key.code == KeyCode::Backspace)
                    && let Some(draft) = self.draft_mut(field)
                {
                    draft.key(key.code);
                }
            }
            _ => {}
        }
        false
    }
    fn newline(&mut self) {
        if self.current().secret() {
            self.paste("\n");
        } else {
            self.note = "Only the environment and header fields take several lines.".into();
        }
    }
    pub fn spec(&self) -> Result<Spec, String> {
        let name = self.name.text().trim();
        if name.is_empty() || name.len() > NAME_MAX || !name.chars().all(safe_name_char) {
            return Err(
                "A server name needs 1-96 letters, digits, underscore, dot or hyphen.".into(),
            );
        }
        let kind = if self.http {
            let url = self.url.text().trim();
            if !(url.starts_with("http://") || url.starts_with("https://"))
                || url.chars().any(|c| c.is_whitespace() || c.is_control())
            {
                return Err(
                    "A URL must start with http:// or https:// and contain no spaces.".into(),
                );
            }
            Kind::Http {
                url: url.to_owned(),
                headers: parse_pairs(self.headers.text(), ':', header_char, "Headers")?,
            }
        } else {
            let command = self.command.text().trim();
            if command.is_empty() {
                return Err("A stdio server needs a command.".into());
            }
            let args: Vec<String> = self
                .args
                .text()
                .split_whitespace()
                .map(str::to_owned)
                .collect();
            if args.len() > ARGS_COUNT_MAX || args.iter().any(|a| a.len() > ARG_MAX) {
                return Err("Use at most 64 arguments of at most 1024 bytes each.".into());
            }
            Kind::Stdio {
                command: command.to_owned(),
                args,
                env: parse_pairs(self.env.text(), '=', env_char, "Environment")?,
            }
        };
        Ok(Spec {
            name: name.to_owned(),
            kind,
        })
    }

    fn summary(&self, field: Field) -> String {
        match field {
            Field::Transport => (if self.http { "http" } else { "stdio" }).into(),
            Field::Env | Field::Headers => {
                let (delimiter, valid): (char, fn(char) -> bool) = if field == Field::Env {
                    ('=', env_char)
                } else {
                    (':', header_char)
                };
                let text = self.draft(field).expect("text").text();
                let names: Vec<String> = text
                    .split('\n')
                    .filter(|l| !l.trim().is_empty())
                    .map(|l| mask_line(l, delimiter, valid))
                    .collect();
                if names.is_empty() {
                    "none".into()
                } else {
                    format!("{} (values hidden)", shorten(&names.join(", "), 60))
                }
            }
            _ => {
                let text = self.draft(field).expect("text").text();
                if text.is_empty() {
                    "—".into()
                } else {
                    shorten(&terminal_text(text), 60)
                }
            }
        }
    }

    pub fn lines(&self, width: usize) -> Vec<String> {
        let mut out = Vec::new();
        for (position, field) in self.fields().iter().copied().enumerate() {
            let active = position == self.selected.min(self.fields().len() - 1);
            let marker = if active { "> " } else { "  " };
            if !active || field == Field::Transport {
                let hint = if active { "   (Space switches)" } else { "" };
                out.push(format!(
                    "{marker}{}: {}{hint}",
                    field.label(),
                    self.summary(field)
                ));
                continue;
            }
            out.push(format!("{marker}{}:", field.label()));
            let draft = self.draft(field).expect("text");
            if field.secret() {
                let (delimiter, valid): (char, fn(char) -> bool) = if field == Field::Env {
                    ('=', env_char)
                } else {
                    (':', header_char)
                };
                let masked: Vec<String> = draft
                    .text()
                    .split('\n')
                    .map(|l| mask_line(l, delimiter, valid))
                    .collect();
                let skip = masked.len().saturating_sub(4);
                let last = masked.len() - 1;
                for (i, line) in masked.into_iter().enumerate().skip(skip) {
                    out.push(format!("    {line}{}", if i == last { "│" } else { "" }));
                }
            } else {
                let (before, after) = draft.text().split_at(draft.cursor());
                let room = width.saturating_sub(12).max(8);
                let tail: Vec<char> = terminal_text(before).chars().collect();
                let head: String = if tail.len() > room {
                    format!("…{}", tail[tail.len() - room..].iter().collect::<String>())
                } else {
                    tail.iter().collect()
                };
                out.push(format!("    {head}│{}", shorten(&terminal_text(after), 12)));
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn press(form: &mut Form, code: KeyCode) -> bool {
        form.key(KeyEvent::new(code, KeyModifiers::NONE))
    }
    fn type_text(form: &mut Form, text: &str) {
        for c in text.chars() {
            if c == '\n' {
                form.key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::CONTROL));
            } else {
                press(form, KeyCode::Char(c));
            }
        }
    }
    fn stdio_form() -> Form {
        let mut form = Form::default();
        type_text(&mut form, "files");
        press(&mut form, KeyCode::Tab);
        press(&mut form, KeyCode::Tab);
        type_text(&mut form, "npx");
        press(&mut form, KeyCode::Tab);
        type_text(&mut form, "-y  @scope/pkg");
        press(&mut form, KeyCode::Tab);
        type_text(&mut form, "TOKEN=hunter2\nREGION=us east");
        form
    }

    #[test]
    fn stdio_draft_becomes_the_exact_source_shape() {
        let spec = stdio_form().spec().unwrap();
        assert_eq!(
            spec.wire(),
            json!({"transport":"stdio","name":"files","command":"npx",
                "args":["-y","@scope/pkg"],"env":{"TOKEN":"hunter2","REGION":"us east"}})
        );
        assert_eq!(spec.env_names(), ["TOKEN", "REGION"]);
        assert_eq!((spec.transport(), spec.arg_count()), ("stdio", 2));
    }
    #[test]
    fn http_draft_uses_headers_and_ignores_stdio_fields() {
        let mut form = stdio_form();
        press(&mut form, KeyCode::BackTab);
        press(&mut form, KeyCode::BackTab);
        press(&mut form, KeyCode::BackTab);
        press(&mut form, KeyCode::BackTab);
        assert_eq!(form.current(), Field::Name);
        press(&mut form, KeyCode::Tab);
        press(&mut form, KeyCode::Char(' '));
        assert!(form.http);
        press(&mut form, KeyCode::Tab);
        type_text(&mut form, "https://api.example.com/mcp?k=v");
        press(&mut form, KeyCode::Tab);
        type_text(&mut form, "Authorization: Bearer abc");
        let spec = form.spec().unwrap();
        assert_eq!(
            spec.wire(),
            json!({"transport":"http","name":"files","url":"https://api.example.com/mcp?k=v",
                "headers":{"Authorization":"Bearer abc"}})
        );
        assert_eq!(spec.header_names(), ["Authorization"]);
    }
    #[test]
    fn secret_values_are_never_painted_or_debug_printed() {
        let mut form = stdio_form();
        let painted = form.lines(100).join("\n");
        assert!(painted.contains("TOKEN=••••") && painted.contains("REGION=••••│"));
        assert!(!painted.contains("hunter2") && !painted.contains("us east"));
        press(&mut form, KeyCode::BackTab);
        let summary = form.lines(100).join("\n");
        assert!(summary.contains("TOKEN=••••, REGION=••••") && !summary.contains("hunter2"));
        let spec = form.spec().unwrap();
        let debug = format!("{spec:?}");
        assert!(
            !debug.contains("hunter2") && !debug.contains("us east") && debug.contains("TOKEN")
        );
        // A value typed before any '=' is masked as soon as it stops looking like a name.
        let mut wrong = Form::default();
        press(&mut wrong, KeyCode::Tab);
        press(&mut wrong, KeyCode::Tab);
        press(&mut wrong, KeyCode::Tab);
        press(&mut wrong, KeyCode::Tab);
        type_text(&mut wrong, "my secret!");
        assert!(wrong.lines(100).join("\n").contains("my•secret•"));
    }
    #[test]
    fn invalid_drafts_are_refused_locally_without_echoing_values() {
        let refuse = |edit: &dyn Fn(&mut Form)| {
            let mut form = stdio_form();
            edit(&mut form);
            let error = form.spec().unwrap_err();
            assert!(!error.contains("hunter2"), "{error}");
        };
        refuse(&|f| f.name.set("bad name").unwrap());
        refuse(&|f| f.name.set("").unwrap());
        refuse(&|f| f.command.set("  ").unwrap());
        refuse(&|f| f.env.set("TOKEN hunter2").unwrap());
        refuse(&|f| f.env.set("1BAD=hunter2").unwrap());
        refuse(&|f| f.env.set("A=hunter2\nA=hunter2").unwrap());
        refuse(&|f| f.env.set("A=").unwrap());
        refuse(&|f| f.args.set(&"x ".repeat(65)).unwrap());
        refuse(&|f| {
            f.env
                .set(&(0..33).map(|i| format!("V{i}=x\n")).collect::<String>())
                .unwrap()
        });
        let mut http = stdio_form();
        http.http = true;
        http.url.set("ftp://example.com").unwrap();
        assert!(http.spec().is_err());
        http.url.set("https://exa mple.com").unwrap();
        assert!(http.spec().is_err());
        http.url.set("https://example.com").unwrap();
        http.headers.set("Bad Name: x").unwrap();
        assert!(http.spec().is_err());
    }
    #[test]
    fn typing_is_bounded_and_control_text_is_rejected() {
        let mut form = Form::default();
        form.paste(&"a".repeat(97));
        assert!(form.name.text().is_empty() && !form.note.is_empty());
        form.paste("two\nlines");
        assert!(form.name.text().is_empty());
        form.paste("fine");
        assert_eq!(form.name.text(), "fine");
        press(&mut form, KeyCode::Backspace);
        assert_eq!(form.name.text(), "fin");
        assert!(!form.key(KeyEvent::new(KeyCode::Char('n'), KeyModifiers::CONTROL)));
        assert!(!form.note.is_empty());
        form.key(KeyEvent::new(KeyCode::Char('u'), KeyModifiers::CONTROL));
        assert!(form.name.text().is_empty());
        assert!(press(&mut form, KeyCode::Enter));
    }
    #[test]
    fn secret_fields_ignore_cursor_moves_so_masking_matches_editing() {
        let mut form = stdio_form();
        let before = form.env.text().to_owned();
        for code in [KeyCode::Left, KeyCode::Home, KeyCode::Delete] {
            press(&mut form, code);
        }
        assert_eq!(form.env.text(), before);
        form.paste("a\r\nB=c\rD=e");
        assert!(form.env.text().ends_with("a\nB=c\nD=e"));
    }
}
