//! Local typed Work drafts. Only source review can authorize a change or execution.
use crate::{prepared_skill::Draft, work_protocol::Row};
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use serde_json::{Value, json};
use std::io;
pub(super) struct Form {
    pub row: Option<Row>,
    kind: String,
    fields: Vec<Field>,
    selected: usize,
}
struct Field {
    key: &'static str,
    label: &'static str,
    kind: Kind,
    draft: Draft,
}
#[derive(Clone, Copy)]
enum Kind {
    Text,
    Number,
    Bool,
}
impl Field {
    fn new(key: &'static str, label: &'static str, kind: Kind, value: &Value) -> io::Result<Self> {
        let mut draft = Draft::default();
        draft.set(
            &value
                .as_str()
                .map(str::to_owned)
                .unwrap_or_else(|| value.to_string()),
        )?;
        Ok(Self {
            key,
            label,
            kind,
            draft,
        })
    }
    fn value(&self) -> io::Result<Value> {
        Ok(match self.kind {
            Kind::Text => json!(self.draft.text()),
            Kind::Number => json!(
                self.draft
                    .text()
                    .parse::<u32>()
                    .ok()
                    .filter(|v| *v > 0)
                    .ok_or_else(|| io::Error::other(
                        "Interval must be a positive whole number of minutes"
                    ))?
            ),
            Kind::Bool => json!(self.draft.text() == "true"),
        })
    }
}
impl Form {
    pub fn goal() -> io::Result<Self> {
        Ok(Self {
            row: None,
            kind: "goal".into(),
            fields: vec![Field::new(
                "objective",
                "Objective",
                Kind::Text,
                &json!(""),
            )?],
            selected: 0,
        })
    }
    pub fn is_goal(&self) -> bool {
        self.kind == "goal"
    }
    pub fn new(kind: &str, row: Option<Row>) -> io::Result<Self> {
        if !matches!(kind, "cron" | "loop") {
            return Err(io::Error::other("Select Jobs or Loops to create a job"));
        }
        let mut fields = Vec::new();
        let base=row.as_ref().map(|r|r.raw.clone()).unwrap_or_else(||json!({"prompt":"","label":"","schedule":"","interval_minutes":15,"enabled":false,"new_conversation":false}));
        for (key, label, field_kind) in [
            ("prompt", "Prompt", Kind::Text),
            ("label", "Label", Kind::Text),
        ] {
            fields.push(Field::new(key, label, field_kind, &base[key])?);
        }
        if kind == "cron" {
            for (key, label, field_kind) in [
                ("schedule", "Cron schedule", Kind::Text),
                ("enabled", "Enabled", Kind::Bool),
                (
                    "new_conversation",
                    "Start in a new conversation",
                    Kind::Bool,
                ),
            ] {
                fields.push(Field::new(key, label, field_kind, &base[key])?);
            }
        } else if row.is_none() {
            fields.push(Field::new(
                "interval_minutes",
                "Interval in minutes",
                Kind::Number,
                &base["interval_minutes"],
            )?);
            fields.push(Field::new(
                "enabled",
                "Enabled",
                Kind::Bool,
                &base["enabled"],
            )?);
        }
        Ok(Self {
            row,
            kind: kind.into(),
            fields,
            selected: 0,
        })
    }
    pub fn draft(&self) -> io::Result<Value> {
        let mut value = serde_json::Map::new();
        if self.row.is_none() && !self.is_goal() {
            value.insert("kind".into(), json!(self.kind));
        }
        for field in &self.fields {
            let v = field.value()?;
            if self.row.as_ref().is_none_or(|row| row.raw[field.key] != v) {
                value.insert(field.key.into(), v);
            }
        }
        if value.is_empty() {
            return Err(io::Error::other("Change at least one field before review"));
        }
        Ok(Value::Object(value))
    }
    pub fn accepts_paste(&self) -> bool {
        !matches!(self.fields[self.selected].kind, Kind::Bool)
    }
    pub fn paste(&mut self, text: &str) -> io::Result<()> {
        let field = &mut self.fields[self.selected];
        if matches!(field.kind, Kind::Bool) {
            return Err(io::Error::other("Space toggles this field"));
        }
        if !matches!(field.kind, Kind::Text) && !text.chars().all(|c| c.is_ascii_digit()) {
            return Err(io::Error::other("Enter whole-number minutes"));
        }
        field.draft.insert(text)
    }
    pub fn key(&mut self, key: KeyEvent) -> io::Result<bool> {
        if key.code == KeyCode::Enter && key.modifiers.is_empty() {
            return Ok(true);
        }
        if key.code == KeyCode::Tab && key.modifiers.is_empty() {
            self.selected = (self.selected + 1) % self.fields.len();
            return Ok(false);
        }
        if key.code == KeyCode::BackTab && key.modifiers == KeyModifiers::SHIFT {
            self.selected = (self.selected + self.fields.len() - 1) % self.fields.len();
            return Ok(false);
        }
        let field = &mut self.fields[self.selected];
        if matches!(field.kind, Kind::Bool) {
            if key.code == KeyCode::Char(' ') && key.modifiers.is_empty() {
                let new = if field.draft.text() == "true" {
                    "false"
                } else {
                    "true"
                };
                field.draft.set(new)?;
            }
            return Ok(false);
        }
        if key.code == KeyCode::Char('u') && key.modifiers == KeyModifiers::CONTROL {
            field.draft.set("")?;
            return Ok(false);
        }
        if key.code == KeyCode::Enter && key.modifiers == KeyModifiers::SHIFT {
            self.paste("\n")?;
            return Ok(false);
        }
        if key.modifiers.is_empty()
            || key.modifiers == KeyModifiers::SHIFT && matches!(key.code, KeyCode::Char(_))
        {
            if let KeyCode::Char(ch) = key.code {
                self.paste(&ch.to_string())?;
            } else {
                field.draft.key(key.code);
            }
        }
        Ok(false)
    }
    pub fn visible_lines(&self, width: usize, height: usize) -> Vec<String> {
        if height == 0 {
            return Vec::new();
        }
        let field = &self.fields[self.selected];
        let mut out = vec![
            if self.is_goal() {
                "Start a new goal".into()
            } else {
                format!(
                    "{} {} job",
                    if self.row.is_some() { "Edit" } else { "Create" },
                    self.kind
                )
            },
            format!(
                "Field {}/{}: {}",
                self.selected + 1,
                self.fields.len(),
                field.label
            ),
        ];
        let (before, after) = field.draft.text().split_at(field.draft.cursor());
        let escaped_before = crate::live::terminal_text(before);
        let text = format!("{}│{}", escaped_before, crate::live::terminal_text(after));
        let lines = crate::ui::wrap(&text, width.max(1));
        let cursor = crate::ui::wrap(&escaped_before, width.max(1))
            .len()
            .saturating_sub(1);
        out.truncate(height.saturating_sub(1));
        let available = height - out.len();
        let start = cursor.saturating_sub(available.saturating_sub(1));
        out.extend(lines.into_iter().skip(start).take(available));
        out
    }
}
