//! Create, edit and delete lifecycle hooks: list, bounded form, painted review, explicit confirmation.
use crate::{
    hook_edit::{
        Action, Effective, Hook, RepairReview, Reply, Request, Review, Snapshot, file_label,
    },
    hook_edit_form::Form,
    hook_repair_editor::Editor,
    hook_toggle::Scope,
    live::terminal_text,
    theme::Theme,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};

#[derive(Default)]
pub struct View {
    pub open: bool,
    scope: Scope,
    snapshot: Option<Snapshot>,
    selected: usize,
    form: Option<Form>,
    review: Option<Review>,
    editor: Option<Editor>,
    repair: Option<RepairReview>,
    page: usize,
    seen: Vec<bool>,
    bounds: Option<Rect>,
    visible: bool,
    changed: bool,
    closed: Option<bool>,
    pub notice: String,
}

/// The exact change, one labelled field per line. Private values are never present here.
pub fn review_lines(r: &Review) -> Vec<String> {
    let verb = match r.action {
        Action::Create => "Create",
        Action::Update => "Edit",
        Action::Delete => "Delete",
    };
    let other = file_label(r.scope.other());
    let mut text = vec![
        format!("{verb} {} hook {}", r.scope.key(), r.hook_id),
        format!(
            "Scope file that changes: {}{}",
            r.file,
            if r.created_file { " (new file)" } else { "" }
        ),
        format!("Not touched: {other}"),
        "Document diff:".into(),
    ];
    let after = r.after.as_ref().map(Hook::lines).unwrap_or_default();
    let before = r.before.as_ref().map(Hook::lines).unwrap_or_default();
    match (&r.before, &r.after) {
        (None, _) => text.extend(after.iter().map(|(k, v)| format!("+ {k}: {v}"))),
        (_, None) => text.extend(before.iter().map(|(k, v)| format!("- {k}: {v}"))),
        _ => {
            for ((key, old), (_, new)) in before.iter().zip(&after) {
                if old == new {
                    text.push(format!("  {key}: {new}"));
                } else {
                    text.push(format!("- {key}: {old}"));
                    text.push(format!("+ {key}: {new}"));
                }
            }
        }
    }
    text.push(format!(
        "{} other hook(s) in this file keep their stored values; the file is rewritten in the source's canonical format.",
        r.others
    ));
    text.push(format!(
        "Effective before: {}",
        Effective::describe(&r.before_effective, r.hooks_off)
    ));
    text.push(format!(
        "Effective after: {}",
        Effective::describe(&r.after_effective, r.hooks_off)
    ));
    text.push(format!(
        "File SHA-256 before: {}",
        r.before_hash.as_deref().unwrap_or("(file does not exist)")
    ));
    text.push(format!("File SHA-256 after: {}", r.after_hash));
    text.push("The source validated this change with its own hook validator and writes it under the scope locks, then reads it back. Authoring never runs the hook.".into());
    text
}

/// The unreadable document, the corrected document the source will write in its place, and what is lost.
pub fn repair_lines(r: &RepairReview) -> Vec<String> {
    let mut text = vec![
        format!("Repair {} hook document", r.scope.key()),
        format!("Scope file that changes: {}", r.file),
        format!("Not touched: {}", file_label(r.scope.other())),
        format!(
            "Replaces {} unreadable byte(s), SHA-256 {}, with the canonical document below ({} hook(s)).",
            r.before_bytes,
            r.before_hash,
            r.rows.len()
        ),
        "The source's hook validator accepted your corrected text. The unreadable text is not kept, so copy it elsewhere first if you need it.".into(),
        "--- document that will be written ---".into(),
    ];
    text.extend(r.after_text.lines().map(str::to_owned));
    text.push("--- end of document ---".into());
    text.push(format!("File SHA-256 after: {}", r.after_hash));
    text.push(
        "The source writes it under the scope lock only if the file still has the unreadable bytes shown, then reads it back. Repair never runs a hook."
            .into(),
    );
    text
}

impl View {
    pub fn open(&mut self, scope: Scope) -> Request {
        *self = Self {
            open: true,
            scope,
            ..Self::default()
        };
        Request::Snapshot(scope)
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn close(&mut self) {
        *self = Self::default();
    }
    /// Set once when the user leaves; true when a verified change was saved while open.
    pub fn take_closed(&mut self) -> Option<bool> {
        self.closed.take()
    }
    pub fn accepts_paste(&self) -> bool {
        self.open
            && self.visible
            && self.review.is_none()
            && self.repair.is_none()
            && (self.editor.is_some() || self.form.as_ref().is_some_and(Form::accepts_paste))
    }
    pub fn paste(&mut self, text: &str) {
        if self.review.is_some() || self.repair.is_some() {
            return;
        }
        if let Some(editor) = self.editor.as_mut() {
            if let Err(error) = editor.insert(text) {
                self.notice = error;
            }
        } else if let Some(form) = self.form.as_mut()
            && let Err(error) = form.paste(text)
        {
            self.notice = error.to_string();
        }
    }
    pub fn reject(&mut self, reason: String, request: &Request) {
        self.review = None;
        self.repair = None;
        self.seen.clear();
        self.notice = reason;
        match request {
            Request::Snapshot(_) => {
                self.snapshot = None;
                self.form = None;
                self.editor = None;
            }
            Request::Review { .. } | Request::Repair { .. } => {}
            Request::Apply(_) | Request::RepairApply(_) => {
                // Publication may have happened before the failure: nothing here may look saved.
                self.snapshot = None;
                self.form = None;
                self.editor = None;
                self.changed = true;
            }
        }
    }
    pub fn reply(&mut self, reply: Reply) {
        if !self.open {
            return;
        }
        match reply {
            Reply::Snapshot(snapshot) => {
                self.scope = snapshot.scope;
                self.selected = self.selected.min(snapshot.rows.len().saturating_sub(1));
                self.snapshot = Some(*snapshot);
                self.notice.clear();
            }
            Reply::Review(review) => {
                self.review = Some(*review);
                self.page = 0;
                self.seen.clear();
                self.notice.clear();
            }
            Reply::RepairReview(review) => {
                self.repair = Some(*review);
                self.page = 0;
                self.seen.clear();
                self.notice.clear();
            }
            Reply::RepairSaved(saved) => {
                self.repair = None;
                self.editor = None;
                self.snapshot = None;
                self.changed = true;
                self.seen.clear();
                self.notice = format!(
                    "Verified repair of {}: {} hook(s) read back. F5 reloads this scope.",
                    saved.file,
                    saved.rows.len()
                );
            }
            Reply::Saved(saved) => {
                self.review = None;
                self.form = None;
                self.snapshot = None;
                self.changed = true;
                self.seen.clear();
                self.notice = format!(
                    "Verified {} of {} in {}: saved and effective state read back. F5 reloads this scope.",
                    match saved.action {
                        Action::Create => "create",
                        Action::Update => "edit",
                        Action::Delete => "delete",
                    },
                    saved.hook_id,
                    saved.file
                );
            }
        }
    }
    fn review_request(&mut self, action: Action) -> Option<Request> {
        let Some(snapshot) = self.snapshot.as_ref() else {
            self.notice = "Reload the scope with Esc then F5 before reviewing.".into();
            return None;
        };
        let (before, hook) = match action {
            Action::Delete => (snapshot.rows.get(self.selected).cloned(), None),
            _ => {
                let form = self.form.as_ref()?;
                match form.hook() {
                    Ok(hook) => (form.original().cloned(), Some(hook)),
                    Err(error) => {
                        self.notice = error.to_string();
                        return None;
                    }
                }
            }
        };
        if action != Action::Create && before.is_none() {
            self.notice = "No hook is selected.".into();
            return None;
        }
        Some(Request::Review {
            scope: snapshot.scope,
            revision: snapshot.revision.clone(),
            rows: snapshot.rows.len(),
            catalog: snapshot.catalog.clone(),
            action,
            before: before.map(Box::new),
            hook: hook.map(Box::new),
        })
    }
    fn repair_request(&mut self) -> Option<Request> {
        let snapshot = self.snapshot.as_ref()?;
        Some(Request::Repair {
            scope: snapshot.scope,
            revision: snapshot.revision.clone(),
            catalog: snapshot.catalog.clone(),
            text: self.editor.as_ref()?.text().to_owned(),
        })
    }
    pub fn key(&mut self, e: KeyEvent, waiting: bool) -> Option<Request> {
        if waiting || e.kind != KeyEventKind::Press {
            return None;
        }
        if e.code == KeyCode::Esc && e.modifiers.is_empty() {
            if self.review.take().is_some() || self.repair.take().is_some() {
                self.notice = "Review discarded; nothing was written.".into();
            } else if self.editor.take().is_some() {
                self.notice = "Repair draft discarded; nothing was written.".into();
            } else if self.form.take().is_some() {
                self.notice = "Draft discarded; nothing was written.".into();
            } else {
                let changed = self.changed;
                self.close();
                self.closed = Some(changed);
            }
            self.seen.clear();
            return None;
        }
        if !self.visible {
            return None;
        }
        if self.review.is_some() || self.repair.is_some() {
            if e.modifiers.is_empty() {
                match e.code {
                    KeyCode::PageDown | KeyCode::Down => {
                        self.page = (self.page + 1).min(self.seen.len().saturating_sub(1))
                    }
                    KeyCode::PageUp | KeyCode::Up => self.page = self.page.saturating_sub(1),
                    KeyCode::Enter if !self.seen.is_empty() && self.seen.iter().all(|b| *b) => {
                        return match (&self.review, &self.repair) {
                            (Some(review), _) => Some(Request::Apply(Box::new(review.clone()))),
                            (_, Some(repair)) => {
                                Some(Request::RepairApply(Box::new(repair.clone())))
                            }
                            _ => None,
                        };
                    }
                    _ => {}
                }
            }
            return None;
        }
        if let Some(editor) = self.editor.as_mut() {
            return match editor.key(e) {
                Ok(true) => self.repair_request(),
                Ok(false) => None,
                Err(error) => {
                    self.notice = error;
                    None
                }
            };
        }
        if let Some(form) = self.form.as_mut() {
            return match form.key(e) {
                Ok(true) => {
                    let action = form.action;
                    self.review_request(action)
                }
                Ok(false) => None,
                Err(error) => {
                    self.notice = error.to_string();
                    None
                }
            };
        }
        if !e.modifiers.is_empty() && e.modifiers != KeyModifiers::SHIFT {
            return None;
        }
        let rows = self.snapshot.as_ref().map_or(0, |s| s.rows.len());
        match e.code {
            KeyCode::Tab | KeyCode::BackTab => {
                self.scope = self.scope.other();
                self.selected = 0;
                self.snapshot = None;
                self.notice.clear();
                return Some(Request::Snapshot(self.scope));
            }
            KeyCode::F(5) => {
                self.snapshot = None;
                self.notice.clear();
                return Some(Request::Snapshot(self.scope));
            }
            KeyCode::Up => self.selected = self.selected.saturating_sub(1),
            KeyCode::Down => self.selected = (self.selected + 1).min(rows.saturating_sub(1)),
            KeyCode::Char('r' | 'R') => {
                match self.snapshot.as_ref().and_then(|s| s.unreadable.as_ref()) {
                    Some(unreadable) => {
                        self.editor = Some(Editor::new(&unreadable.text));
                        self.notice.clear();
                    }
                    None => {
                        self.notice =
                            "This scope's document is readable; nothing needs repair.".into()
                    }
                }
            }
            KeyCode::Char('n' | 'N' | 'e' | 'E' | 'd' | 'D') | KeyCode::Enter
                if self
                    .snapshot
                    .as_ref()
                    .is_some_and(|s| s.unreadable.is_some()) =>
            {
                self.notice = "The document cannot be read. Press R to repair it first.".into();
            }
            KeyCode::Char('n' | 'N') => match self.snapshot.as_ref() {
                Some(s) if s.rows.len() < crate::hook_edit::MAX_HOOKS => {
                    self.form = Some(Form::create(&s.catalog));
                    self.notice.clear();
                }
                Some(_) => self.notice = "A scope holds at most 64 hooks.".into(),
                None => self.notice = "Reload the scope with F5 first.".into(),
            },
            KeyCode::Char('e' | 'E') | KeyCode::Enter => {
                if let Some(s) = self.snapshot.as_ref() {
                    if let Some(hook) = s.rows.get(self.selected) {
                        self.form = Some(Form::edit(&s.catalog, hook));
                        self.notice.clear();
                    } else {
                        self.notice = "There is no hook to edit; press N to create one.".into();
                    }
                }
            }
            KeyCode::Char('d' | 'D') => {
                self.notice.clear();
                return self.review_request(Action::Delete);
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme, waiting: bool) {
        let w = area.width.min(100);
        let h = area.height.min(36);
        let rect = Rect::new(
            area.x + (area.width - w) / 2,
            area.y + (area.height - h) / 2,
            w,
            h,
        );
        frame.render_widget(Clear, rect);
        let block = Block::bordered()
            .title(format!(" Author lifecycle hooks · {} ", self.scope.key()))
            .style(Style::default().fg(theme.text()).bg(theme.screen()));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        self.visible = inner.width >= 25 && inner.height >= 6;
        if !self.visible {
            return;
        }
        let rows = usize::from(inner.height) - 3;
        let width = usize::from(inner.width);
        let pager = self
            .review
            .as_ref()
            .map(review_lines)
            .or_else(|| self.repair.as_ref().map(repair_lines));
        let (lines, keys) = if let Some(text) = pager {
            let text = text
                .iter()
                .map(|line| terminal_text(line))
                .collect::<Vec<_>>()
                .join("\n");
            let lines = crate::ui::wrap(&text, width);
            let count = lines.len().div_ceil(rows).max(1);
            if self.seen.len() != count || self.bounds != Some(rect) {
                self.seen = vec![false; count];
                self.page = 0;
            }
            self.page = self.page.min(count - 1);
            self.seen[self.page] = true;
            (
                lines
                    .into_iter()
                    .skip(self.page * rows)
                    .take(rows)
                    .collect::<Vec<_>>(),
                format!(
                    "Page {}/{} · ↑ ↓ read · Enter writes this change after all pages · Esc goes back",
                    self.page + 1,
                    count
                ),
            )
        } else if let Some(editor) = &self.editor {
            let mut lines = vec![format!(
                "Repair {}: edit the text until the source accepts it.",
                self.snapshot.as_ref().map_or("", |s| s.file.as_str())
            )];
            lines.extend(editor.lines(width, rows.saturating_sub(1)));
            (
                lines,
                "Type to edit · ← → ↑ ↓ Home End · Enter new line · F6 reviews the repair · Esc discards".into(),
            )
        } else if let Some(form) = &self.form {
            (
                form.visible_lines(width, rows),
                "Tab / Shift+Tab field · Space selects · Enter reviews · Ctrl+U clears · Esc discards".into(),
            )
        } else {
            let mut lines = Vec::new();
            if let Some(snapshot) = &self.snapshot {
                if let Some(unreadable) = &snapshot.unreadable {
                    lines.push(format!("{} cannot be read:", snapshot.file));
                    lines.push(terminal_text(&unreadable.error));
                    lines.push(
                        "Press R to edit the stored text and repair it. Nothing else can be edited until then."
                            .into(),
                    );
                    lines.push(String::new());
                    lines.extend(
                        unreadable
                            .text
                            .lines()
                            .take(rows.saturating_sub(5))
                            .map(terminal_text),
                    );
                } else {
                    lines.push(format!(
                        "{} · {} hook(s)",
                        snapshot.file,
                        snapshot.rows.len()
                    ));
                }
                if snapshot.hooks_off {
                    lines.push("Lifecycle hooks are switched off for this runtime; saved changes will not run.".into());
                }
                if snapshot.rows.is_empty() && snapshot.unreadable.is_none() {
                    lines.push("No hooks in this scope. Press N to create one.".into());
                }
                let room = rows.saturating_sub(lines.len());
                let start = self.selected.saturating_add(1).saturating_sub(room);
                for (i, hook) in snapshot.rows.iter().enumerate().skip(start).take(room) {
                    lines.push(terminal_text(&format!(
                        "{} {}",
                        if i == self.selected { "›" } else { " " },
                        hook.summary()
                    )));
                }
            }
            (
                lines,
                "Tab scope · ↑ ↓ select · N new · E / Enter edit · D delete · R repair · F5 reload · Esc back"
                    .into(),
            )
        };
        self.bounds = Some(rect);
        frame.render_widget(Paragraph::new(lines.join("\n")), inner);
        frame.render_widget(
            Paragraph::new(if waiting {
                "Waiting for source…".into()
            } else {
                terminal_text(&self.notice)
            }),
            Rect::new(inner.x, inner.bottom() - 3, inner.width, 1),
        );
        frame.render_widget(
            Paragraph::new(crate::ui::wrap(&keys, inner.width.into()).join("\n")),
            Rect::new(inner.x, inner.bottom() - 2, inner.width, 2),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh(executable: &str) -> Hook {
        Hook {
            env_count: 0,
            ..hook(executable, 3.0)
        }
    }
    fn hook(executable: &str, timeout: f64) -> Hook {
        Hook {
            id: "audit".into(),
            events: vec!["tool_before".into()],
            executable: executable.into(),
            argv: vec!["two words".into()],
            enabled: true,
            mode: "observe".into(),
            timeout,
            sources: vec![],
            tools: vec![],
            env_count: 1,
            custom_cwd: false,
        }
    }
    fn review(action: Action, before: Option<Hook>, after: Option<Hook>) -> Review {
        let json = serde_json::json!({"kind":"hook_edit_review","version":1,"session_id":"s","agent_id":"a",
            "backend":"b","model":"m","work_instance":"w","scope":"project"});
        let catalog = crate::hook_edit::Catalog {
            events: vec!["tool_before".into()],
            gate_events: vec![],
            sources: vec![],
        };
        let wire = |h: &Option<Hook>| {
            h.as_ref().map_or(serde_json::Value::Null, |h| {
                serde_json::json!({"id":h.id,"events":h.events,"executable":h.executable,"argv":h.argv,
                    "enabled":h.enabled,"mode":h.mode,"timeout":h.timeout,"sources":h.sources,"tools":h.tools,
                    "env_count":h.env_count,"custom_cwd":h.custom_cwd})
            })
        };
        let effective = |on: bool| serde_json::json!({"scope":"project","enabled":on,"command_id":"a".repeat(24)});
        let mut wire_review = json;
        for (key, value) in [
            ("revision", serde_json::json!("1".repeat(64))),
            ("token", serde_json::json!("2".repeat(64))),
            ("action", serde_json::json!(action.key())),
            ("hook_id", serde_json::json!("audit")),
            ("file", serde_json::json!(file_label(Scope::Project))),
            ("created_file", serde_json::json!(false)),
            ("hooks_off", serde_json::json!(false)),
            ("before", wire(&before)),
            ("after", wire(&after)),
            (
                "before_effective",
                if before.is_some() {
                    effective(true)
                } else {
                    serde_json::Value::Null
                },
            ),
            (
                "after_effective",
                if after.is_some() {
                    effective(true)
                } else {
                    serde_json::Value::Null
                },
            ),
            ("others", serde_json::json!(2)),
            ("before_sha256", serde_json::json!("3".repeat(64))),
            ("after_sha256", serde_json::json!("4".repeat(64))),
            ("after_revision", serde_json::json!("5".repeat(64))),
        ] {
            wire_review[key] = value;
        }
        let request = Request::Review {
            scope: Scope::Project,
            revision: "1".repeat(64),
            rows: 2 + usize::from(before.is_some()),
            catalog,
            action,
            before: before.map(Box::new),
            hook: after.map(Box::new),
        };
        let id = crate::work_protocol::Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "b".into(),
            model: "m".into(),
        };
        match request.reply(&wire_review, &id, "w", None).unwrap() {
            Reply::Review(review) => *review,
            _ => unreachable!(),
        }
    }

    #[test]
    fn review_names_the_scope_file_field_diff_effects_and_hashes_without_private_values() {
        let update = review_lines(&review(
            Action::Update,
            Some(hook("old.exe", 5.0)),
            Some(hook("new.exe", 5.0)),
        ))
        .join("\n");
        assert!(update.contains("Scope file that changes: project .litetui/hooks.json"));
        assert!(update.contains("Not touched: global hooks.json"));
        assert!(
            update.contains("- command: old.exe \"two words\"\n+ command: new.exe \"two words\"")
        );
        assert!(
            update.contains("  timeout: 5 s")
                && update.contains("1 environment value(s) kept in the source")
        );
        assert!(update.contains("2 other hook(s)") && update.contains("File SHA-256 after: 4444"));
        assert!(update.contains("Effective after: project hook runs"));
        let create = review_lines(&review(Action::Create, None, Some(fresh("x.exe")))).join("\n");
        assert!(create.contains("+ command: x.exe") && !create.contains("\n- "));
        let delete =
            review_lines(&review(Action::Delete, Some(hook("x.exe", 3.0)), None)).join("\n");
        assert!(
            delete.contains("- command: x.exe")
                && delete.contains("Effective after: no hook with this ID runs")
        );
    }

    #[test]
    fn apply_requires_every_review_page_to_have_been_painted() {
        let mut view = View {
            open: true,
            visible: true,
            review: Some(review(Action::Create, None, Some(fresh("x.exe")))),
            seen: vec![true, false],
            ..View::default()
        };
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        assert!(
            view.key(enter, false).is_none(),
            "an unseen page blocks Apply"
        );
        assert!(view.key(enter, true).is_none(), "waiting blocks Apply");
        view.seen = vec![true, true];
        assert!(matches!(view.key(enter, false), Some(Request::Apply(_))));
        view.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE), false);
        assert!(view.review.is_none() && view.take_closed().is_none());
    }

    #[test]
    fn failed_publication_never_looks_saved_and_leaving_reports_a_change() {
        let apply = Request::Apply(Box::new(review(Action::Create, None, Some(fresh("x.exe")))));
        let mut view = View {
            open: true,
            visible: true,
            form: Some(Form::create(&crate::hook_edit::Catalog {
                events: vec!["tool_before".into()],
                gate_events: vec![],
                sources: vec![],
            })),
            ..View::default()
        };
        view.reject("readback failed".into(), &apply);
        assert!(view.form.is_none() && view.snapshot.is_none() && view.notice == "readback failed");
        view.visible = true;
        view.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE), false);
        assert_eq!(view.take_closed(), Some(true));
        assert!(!view.open);
    }
}
