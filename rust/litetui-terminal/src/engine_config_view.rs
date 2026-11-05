use crate::{
    engine_config::{self as cfg, Intent, Review, Snapshot, Update},
    prepared_skill::BoundedDraft,
    theme::Theme,
    ui,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{Frame, layout::Rect, style::Style, widgets::Paragraph};
use serde_json::Value;

pub(super) enum Action {
    Stay,
    Back,
    Send(Intent),
}
#[derive(Default)]
pub(super) struct View {
    snapshot: Option<Snapshot>,
    selected: usize,
    draft: Option<BoundedDraft<4096>>,
    replace: bool,
    discard: bool,
    review: Option<Review>,
    seen: Vec<bool>,
    page: usize,
    geometry: Option<Rect>,
    pub(super) notice: String,
}
fn display(v: &Value) -> String {
    match v {
        Value::Null => "(default)".into(),
        Value::String(s) if s.is_empty() => "(blank)".into(),
        Value::String(s) => ui::display_safe(s),
        v => v.to_string(),
    }
}
fn edit_value(v: &Value) -> String {
    match v {
        Value::Null => String::new(),
        Value::String(s) => s.clone(),
        v => v.to_string(),
    }
}
impl View {
    pub(super) fn clear_layout(&mut self) {
        self.seen.clear();
        self.page = 0;
        self.geometry = None;
    }
    fn selected(&self) -> Option<(&str, &Value)> {
        let snap = self.snapshot.as_ref()?;
        let key = snap.names.get(self.selected)?.as_str();
        Some((key, &snap.wire["fields"][key]))
    }
    pub(super) fn observe(&mut self, update: Update) -> bool {
        self.clear_layout();
        match update {
            Update::Snapshot(s) => {
                self.selected = self.selected.min(s.names.len() - 1);
                self.snapshot = Some(s);
                self.review = None;
            }
            Update::Review(r) => {
                self.notice.clear();
                self.review = Some(r);
            }
            Update::Rejected(message) => {
                self.review = None;
                self.notice = if self.draft.is_some() {
                    format!("Draft kept. {message}")
                } else {
                    message
                };
            }
            Update::Saved(field) => {
                self.review = None;
                self.draft = None;
                self.notice =
                    format!("{field} saved and verified. Reopen to use the saved configuration.");
                return true;
            }
        }
        false
    }
    pub(super) fn accepts_paste(&self) -> bool {
        self.draft.is_some() && self.review.is_none() && !self.discard
    }
    pub(super) fn paste(&mut self, s: &str) {
        if !self.accepts_paste() {
            return;
        }
        if s.len() > 4096 || s.chars().any(char::is_control) {
            self.notice = "Use a single line of at most 4096 UTF-8 bytes.".into();
            return;
        }
        if let Some(draft) = self.draft.as_mut() {
            let result = if self.replace {
                draft.set(s)
            } else {
                draft.insert(s)
            };
            if result.is_ok() {
                self.replace = false;
                self.notice.clear()
            } else if let Err(e) = result {
                self.notice = e.to_string();
            }
        }
    }
    pub(super) fn key(&mut self, key: KeyEvent) -> Action {
        if key.kind != KeyEventKind::Press {
            return Action::Stay;
        }
        let plain = key.modifiers.is_empty();
        if self.discard {
            if plain {
                match key.code {
                    KeyCode::Enter => {
                        self.draft = None;
                        self.discard = false;
                        self.notice.clear();
                    }
                    KeyCode::Esc => self.discard = false,
                    _ => {}
                }
            }
            return Action::Stay;
        }
        if let Some(r) = &self.review {
            if plain {
                match key.code {
                    KeyCode::Esc => {
                        self.review = None;
                        self.clear_layout();
                    }
                    KeyCode::PageDown => {
                        self.page = (self.page + 1).min(self.seen.len().saturating_sub(1))
                    }
                    KeyCode::PageUp => self.page = self.page.saturating_sub(1),
                    KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|v| *v) => {
                        return Action::Send(Intent::Save(r.token.clone()));
                    }
                    _ => {}
                }
            }
            return Action::Stay;
        }
        if plain && key.code == KeyCode::F(5) {
            return Action::Send(Intent::Read);
        }
        if self.draft.is_some() {
            if plain && key.code == KeyCode::Esc {
                if self.selected().is_some_and(|(_, r)| {
                    self.draft.as_ref().unwrap().text() == edit_value(&r["saved"])
                }) {
                    self.draft = None;
                } else {
                    self.discard = true;
                }
            } else if plain && key.code == KeyCode::Enter {
                let Some((name, _)) = self.selected() else {
                    return Action::Stay;
                };
                let snap = self.snapshot.as_ref().expect("selected field");
                match cfg::input(name, self.draft.as_ref().unwrap().text())
                    .and_then(|v| snap.review_command(name, &v).map(|_| v))
                {
                    Ok(value) => {
                        return Action::Send(Intent::Review {
                            snapshot: snap.token.clone(),
                            field: name.into(),
                            value,
                        });
                    }
                    Err(e) => self.notice = e.to_string(),
                }
            } else if key.modifiers == KeyModifiers::CONTROL && key.code == KeyCode::Char('a') {
                self.replace = true
            } else if let KeyCode::Char(c) = key.code
                && matches!(key.modifiers, KeyModifiers::NONE | KeyModifiers::SHIFT)
            {
                self.paste(c.encode_utf8(&mut [0; 4]));
            } else if plain {
                if self.replace && matches!(key.code, KeyCode::Backspace | KeyCode::Delete) {
                    self.paste("");
                } else if let Some(d) = self.draft.as_mut() {
                    self.replace = false;
                    d.key(key.code);
                }
            }
        } else if plain {
            if key.code == KeyCode::Esc {
                return Action::Back;
            }
            if let Some(s) = self.snapshot.as_ref() {
                match key.code {
                    KeyCode::Down | KeyCode::Tab => {
                        self.selected = (self.selected + 1) % s.names.len()
                    }
                    KeyCode::Up => {
                        self.selected = (self.selected + s.names.len() - 1) % s.names.len()
                    }
                    KeyCode::Enter => {
                        if let Some((_, r)) = self.selected() {
                            if let Some(reason) = r["locked"].as_str() {
                                self.notice = reason.into();
                            } else {
                                let mut draft = BoundedDraft::default();
                                draft
                                    .set(&edit_value(&r["saved"]))
                                    .expect("bounded saved value");
                                self.draft = Some(draft);
                                self.replace = true;
                                self.notice.clear();
                            }
                        }
                    }
                    _ => {}
                }
            }
        }
        Action::Stay
    }
    pub(super) fn draw(&mut self, f: &mut Frame, area: Rect, theme: &Theme, waiting: bool) {
        let inner = area.inner(ratatui::layout::Margin::new(2, 1));
        let width = usize::from(inner.width);
        let body = Rect::new(inner.x, inner.y + 1, inner.width, inner.height - 3);
        if self.geometry != Some(body) {
            self.geometry = Some(body);
            self.page = 0;
            self.seen.clear();
        }
        let mut cursor_line = None;
        let (title, content, mut help) = if self.discard {
            (
                "Discard unsaved field?".into(),
                String::new(),
                "Enter discard · Esc keep editing",
            )
        } else if let Some(r) = &self.review {
            let row = &r.snap.wire["fields"][&r.field];
            (
                "Review engine preference".into(),
                format!(
                    "{}\nConversation {}\nDestination {}\n\nSaved before: {}\nProposed: {}\nEffective now: {}\n\nSaving will defer this change until {}. Reopen before using the saved configuration.",
                    r.field,
                    r.snap.id.session,
                    ui::display_safe(r.snap.wire["destination"].as_str().unwrap()),
                    display(&row["saved"]),
                    display(&r.value),
                    display(&row["effective"]),
                    row["timing"].as_str().unwrap()
                ),
                "PgUp/PgDn review every page · Esc edit",
            )
        } else if let Some(d) = self.draft.as_ref() {
            let (name, row) = self.selected().expect("selected preference");
            let (left, right) = d.text().split_at(d.cursor());
            let hint = match name {
                "ninfer_kv_dtype" => "Choose fp8, bf16, int8, nvfp4 or k8v4.\n",
                "ninfer_max_concurrency" => "Enter 1 to 8 requests.\n",
                "ninfer_kv_capacity" => "Enter auto, a positive token count, or leave blank.\n",
                "ninfer_host_kv_mib" | "strata_max_context" => {
                    "Leave blank to use the engine default.\n"
                }
                _ => "",
            };
            let prefix = format!(
                "Saved: {}\nEffective: {}\n{hint}\n{}",
                display(&row["saved"]),
                display(&row["effective"]),
                ui::display_safe(left)
            );
            cursor_line = Some(ui::wrap(&format!("{prefix}▏"), width).len() - 1);
            (
                format!("Edit {name} · conversation"),
                format!(
                    "{prefix}{}{}",
                    if self.replace { " [replace] " } else { "▏" },
                    ui::display_safe(right)
                ),
                "Enter review · Ctrl+A replace · F5 refresh, keep draft · Esc",
            )
        } else if let Some(s) = self.snapshot.as_ref() {
            let lines = s
                .names
                .iter()
                .enumerate()
                .map(|(i, k)| {
                    format!(
                        "{} {}  {}{}",
                        if self.selected == i { "›" } else { " " },
                        k,
                        display(&s.wire["fields"][k]["saved"]),
                        if s.wire["fields"][k]["locked"].is_null() {
                            ""
                        } else {
                            "  [locked]"
                        }
                    )
                })
                .map(|line| {
                    ui::wrap(&line, width)
                        .into_iter()
                        .next()
                        .unwrap_or_default()
                })
                .collect::<Vec<_>>();
            (
                "Engine preferences · current conversation".into(),
                lines.join("\n"),
                "↑ ↓ select · Enter edit · F5 refresh · Esc engine",
            )
        } else {
            (
                "Engine preferences".into(),
                "Reading current conversation settings…".to_owned(),
                "Esc engine",
            )
        };
        let lines = ui::wrap(&content, width);
        let height = usize::from(body.height).max(1);
        let pages = lines.len().div_ceil(height).max(1);
        if self.review.is_some() {
            if self.seen.len() != pages {
                self.seen = vec![false; pages];
            }
            self.page = self.page.min(pages - 1);
            if !waiting {
                self.seen[self.page] = true;
            }
            if self.seen.iter().all(|p| *p) {
                help = "Enter save reviewed field · PgUp/PgDn · Esc edit";
            }
        }
        let page = if self.review.is_some() {
            self.page
        } else {
            cursor_line.unwrap_or(0) / height
        };
        let visible = lines
            .iter()
            .skip(page * height)
            .take(height)
            .map(String::as_str)
            .collect::<Vec<_>>()
            .join("\n");
        let title = if self.review.is_some() && pages > 1 {
            format!("{title} · {}/{}", page + 1, pages)
        } else {
            title
        };
        f.render_widget(
            Paragraph::new(title).style(Style::default().fg(theme.primary)),
            Rect::new(inner.x, inner.y, inner.width, 1),
        );
        f.render_widget(Paragraph::new(visible), body);
        f.render_widget(
            Paragraph::new(ui::display_safe(&self.notice)),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 1),
        );
        f.render_widget(
            Paragraph::new(if waiting {
                "Waiting for the source…"
            } else {
                help
            }),
            Rect::new(inner.x, inner.bottom() - 1, inner.width, 1),
        );
    }
}
