//! Read-only `/help` and `/keys` overlay: the live command catalog already
//! delivered by the source, and the keys this frontend binds. Inert: no
//! intent, no mutation, nothing is sent; Esc closes it.
use crate::{command_palette::Catalog, live::terminal_text, theme::Theme};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Borders, Clear, Paragraph},
};

/// Every binding below is read from `main.rs` key routing for the live frontend.
pub const KEYS: &[(&str, &str)] = &[
    (
        "Enter",
        "send the draft (while a turn runs: review a mid-turn message)",
    ),
    ("Ctrl+J / Ctrl+Shift+Enter", "alternate send"),
    (
        "Up / Down",
        "composer history; Down at the end moves onto the footer chips",
    ),
    ("Tab", "accept the highlighted slash completion"),
    (
        "Esc",
        "stop the running turn (asks first); closes any open control",
    ),
    ("Ctrl+Esc", "open the stop dialog"),
    ("Ctrl+Q", "quit and close the owned runtime"),
    (
        "Ctrl+Space",
        "dictate into the composer (Voice sets the key)",
    ),
    (
        "/tasks /job /cal",
        "Work: tasks, jobs, loops, goal, calendar, plugins",
    ),
    ("/model", "models"),
    ("/resume", "conversations"),
    ("/backend", "backend"),
    ("/settings", "settings"),
    (
        "Ctrl+\\",
        "command palette: themes, saved history, held input, skills (Tab)",
    ),
    ("/engine", "engine management"),
    ("Ctrl+T", "tool selection"),
    ("Ctrl+P", "plan mode"),
    ("Alt+P", "chat pause"),
    ("Shift+Tab", "authority profile"),
    ("Ctrl+L", "new conversation (reviewed)"),
    ("Ctrl+O", "review an image (reads the clipboard image)"),
    ("Ctrl+X", "clear the attached image"),
    ("Ctrl+G", "toggle compact / full transcript view"),
    (
        "Ctrl+B / Ctrl+E",
        "select the previous tool card / open or fold the recent one",
    ),
    ("PgUp / PgDn", "scroll the transcript"),
    (
        "Alt+Up / Alt+Down",
        "scroll the transcript by one line (Up / Down keep prompt history)",
    ),
    (
        "Alt+S",
        "scroll lock: opens the reviewed Follow new replies setting (/settings, U) when idle",
    ),
    (
        "click a card title",
        "fold or unfold a prompt or answer; a folded answer's Speak and an image line open Saved History",
    ),
    (
        "Ctrl+Home / Ctrl+End",
        "jump to the top / bottom of the transcript",
    ),
    ("Ctrl+V", "paste from the clipboard"),
    (
        "Ctrl+C / Ctrl+Shift+C",
        "copy the selection / always the transcript selection",
    ),
    ("Ctrl+A or Home / End", "start / end of the line"),
    (
        "Ctrl+Left / Ctrl+Right",
        "move by word (add Shift to select)",
    ),
    (
        "Ctrl+W / Ctrl+F",
        "delete the word before / after the cursor",
    ),
    ("Ctrl+U / Ctrl+\\", "delete to the start / end of the line"),
    ("Ctrl+D", "delete the character under the cursor"),
    ("Ctrl+Shift+A", "select the whole draft"),
];

#[derive(Default)]
pub struct View {
    pub open: bool,
    lines: Vec<String>,
    scroll: usize,
    max_scroll: usize,
    page: usize,
    /// Python's /help text: drawn as its HelpScreen instead of this frontend's own list.
    python: Option<String>,
    close_hit: Option<Rect>,
}
fn display(text: &str) -> String {
    terminal_text(text).replace(['\n', '\t'], " ")
}
impl View {
    /// `keys_first` only decides which section the reader lands on.
    pub fn open(&mut self, catalog: Option<&Catalog>, keys_first: bool) {
        // /help is Python's HelpScreen when the source sends its text.
        self.python = (!keys_first)
            .then(|| catalog.and_then(|c| c.help_text.clone()))
            .flatten();
        let mut commands = vec!["Commands (live source registry)".to_owned()];
        match catalog {
            Some(catalog) => {
                let mut rows: Vec<_> = catalog
                    .entries
                    .iter()
                    .filter(|e| e.kind == "command" && e.completion_order.is_some())
                    .collect();
                rows.sort_by_key(|e| e.completion_order);
                for entry in rows {
                    commands.push(format!("  {}", display(&entry.tokens.join(", "))));
                    if !entry.help.is_empty() {
                        commands.push(format!("      {}", display(&entry.help)));
                    }
                    if entry.action.is_none() {
                        commands.push(format!(
                            "      Not available in this frontend: {}",
                            display(&entry.unavailable)
                        ));
                    }
                }
                let skills = catalog.entries.iter().filter(|e| e.kind == "skill").count();
                commands.push(format!(
                    "  {skills} skills are listed under Tab in the Ctrl+\\ palette; /<skill name> prepares one for review."
                ));
            }
            None => {
                commands.push("  The command catalog has not been read yet; press Ctrl+\\.".into())
            }
        }
        let mut keys = vec!["Keys (this frontend)".to_owned()];
        keys.extend(KEYS.iter().map(|(key, what)| format!("  {key}: {what}")));
        let (first, second) = if keys_first {
            (keys, commands)
        } else {
            (commands, keys)
        };
        self.lines = first;
        self.lines.push(String::new());
        self.lines.extend(second);
        self.scroll = 0;
        self.max_scroll = 0;
        self.page = 1;
        self.open = true;
    }
    pub fn close(&mut self) {
        self.open = false;
        self.lines.clear();
        self.python = None;
        self.close_hit = None;
    }
    /// A click on Close closes (HelpBody's #help-close).
    pub fn mouse(&mut self, event: crossterm::event::MouseEvent) {
        use crossterm::event::{MouseButton, MouseEventKind};
        match event.kind {
            MouseEventKind::Down(MouseButton::Left)
                if self
                    .close_hit
                    .is_some_and(|r| r.contains((event.column, event.row).into())) =>
            {
                self.close()
            }
            MouseEventKind::ScrollDown => {
                self.scroll = self.scroll.saturating_add(2).min(self.max_scroll)
            }
            MouseEventKind::ScrollUp => self.scroll = self.scroll.saturating_sub(2),
            _ => {}
        }
    }
    pub fn key(&mut self, key: KeyEvent) {
        if key.kind == KeyEventKind::Release || !key.modifiers.is_empty() {
            return;
        }
        match key.code {
            KeyCode::Esc if key.kind == KeyEventKind::Press => self.close(),
            // HelpScreen binds q to close, and Enter presses the focused Close.
            KeyCode::Char('q') | KeyCode::Enter
                if self.python.is_some() && key.kind == KeyEventKind::Press =>
            {
                self.close()
            }
            KeyCode::Up => self.scroll = self.scroll.saturating_sub(1),
            KeyCode::Down => self.scroll = self.scroll.saturating_add(1).min(self.max_scroll),
            KeyCode::PageUp => self.scroll = self.scroll.saturating_sub(self.page),
            KeyCode::PageDown => {
                self.scroll = self.scroll.saturating_add(self.page).min(self.max_scroll)
            }
            KeyCode::Home => self.scroll = 0,
            KeyCode::End => self.scroll = self.max_scroll,
            _ => {}
        }
    }
    /// Python's HelpScreen: #help-box (88 wide, the 80% body's height, thick $primary border,
    /// padding 1 2) with the bold title, the text in a scroll region (1-column scrollbar) and the
    /// Close + Dock to side row, centred, below it.
    fn draw_python(&mut self, frame: &mut Frame, area: Rect, theme: &Theme, text: &str) {
        frame.render_widget(Clear, area);
        frame.buffer_mut().set_style(
            area,
            Style::default().fg(theme.text()).bg(theme.screen()),
        );
        let bw = area.width.min(88);
        let body = (u32::from(area.height) * 80 / 100) as u16;
        if bw < 14 || body < 11 {
            return;
        }
        let rect = Rect::new(
            area.x + (area.width - bw) / 2,
            area.y + (area.height - body) / 2,
            bw,
            body,
        );
        let buf = frame.buffer_mut();
        crate::picker_box::thick_box(buf, rect, theme);
        let cx = rect.x + 3;
        let cw = bw - 6;
        buf.set_string(
            cx,
            rect.y + 2,
            "Commands & keys",
            // #help-title: bold $primary-lighten-2.
            Style::default()
                .fg(theme.var("primary-lighten-2", theme.title))
                .bg(theme.surface_bg())
                .add_modifier(ratatui::style::Modifier::BOLD),
        );
        // Scroll region: title 2 rows down, the button row (padding-top 1, 3 rows) and the
        // box's padding and border below.
        let top = rect.y + 4;
        let height = body.saturating_sub(10);
        let width = usize::from(cw.saturating_sub(1)).max(1);
        let lines: Vec<String> = text
            .split('\n')
            .flat_map(|line| crate::picker_box::wrap(&terminal_text(line), width))
            .collect();
        self.page = usize::from(height).max(1);
        self.max_scroll = lines.len().saturating_sub(usize::from(height));
        self.scroll = self.scroll.min(self.max_scroll);
        let style = Style::default().fg(theme.foreground).bg(theme.surface);
        for (dy, line) in lines
            .iter()
            .skip(self.scroll)
            .take(usize::from(height))
            .enumerate()
        {
            buf.set_string(cx, top + dy as u16, line, style);
        }
        crate::picker_box::scrollbar(
            buf,
            cx + cw - 1,
            top,
            height,
            lines.len(),
            self.scroll,
            theme,
        );
        // #help-buttons: Close (primary) and the inline Dock to side (margin-right 1), centred.
        let buttons = top + height + 1;
        let close_w = 16u16;
        let dock_w = 16u16;
        let total = close_w + dock_w + 1;
        let x = cx + cw.saturating_sub(total) / 2;
        crate::picker_box::button(buf, x, buttons, "Close", theme, true);
        crate::picker_box::button(buf, x + close_w, buttons, "Dock to side", theme, false);
        self.close_hit = Some(Rect::new(x, buttons, close_w, 3));
    }
    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        if !self.open {
            return;
        }
        if let Some(text) = self.python.clone() {
            return self.draw_python(frame, area, theme, &text);
        }
        if area.width < 36 || area.height < 14 {
            return;
        }
        let width = area.width.saturating_sub(2).min(100);
        let height = area.height.saturating_sub(2).min(34);
        let rect = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - height) / 2,
            width,
            height,
        );
        frame.render_widget(Clear, rect);
        let block = Block::default()
            .borders(Borders::ALL)
            .title(" Help · commands and keys · read only ")
            .style(Style::default().fg(theme.text()).bg(theme.screen()))
            .border_style(Style::default().fg(theme.title));
        let inner = block.inner(rect);
        frame.render_widget(block, rect);
        let body = Rect::new(
            inner.x,
            inner.y,
            inner.width,
            inner.height.saturating_sub(1),
        );
        let wrapped: Vec<String> = self
            .lines
            .iter()
            .flat_map(|line| {
                if line.is_empty() {
                    vec![String::new()]
                } else {
                    crate::ui::wrap(line, usize::from(body.width).max(1))
                }
            })
            .collect();
        let rows = usize::from(body.height).max(1);
        self.page = rows;
        self.max_scroll = wrapped.len().saturating_sub(rows);
        self.scroll = self.scroll.min(self.max_scroll);
        frame.render_widget(
            Paragraph::new(
                wrapped
                    .iter()
                    .skip(self.scroll)
                    .take(rows)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            body,
        );
        frame.render_widget(
            Paragraph::new(format!(
                "↑↓ PgUp PgDn Home End scroll · {}/{} · Esc closes",
                self.scroll + 1,
                self.max_scroll + 1
            )),
            Rect::new(inner.x, inner.bottom().saturating_sub(1), inner.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }
    fn screen(view: &mut View, width: u16, height: u16) -> String {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        let theme = crate::theme::builtin_themes().remove(0);
        terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect()
    }
    #[test]
    fn keys_section_leads_for_keys_and_follows_commands_for_help() {
        let mut view = View::default();
        view.open(None, true);
        assert!(view.lines[0].starts_with("Keys"));
        view.open(None, false);
        assert!(view.lines[0].starts_with("Commands"));
        assert!(view.lines.iter().any(|l| l.contains("press Ctrl+\\")));
        assert!(
            view.lines
                .iter()
                .any(|l| l.contains("Ctrl+Q: quit and close the owned runtime"))
        );
    }
    #[test]
    fn transcript_navigation_keys_are_listed() {
        let mut view = View::default();
        view.open(None, true);
        for key in ["Alt+Up / Alt+Down", "Alt+S", "click a card title"] {
            assert!(
                view.lines.iter().any(|l| l.trim_start().starts_with(key)),
                "{key}"
            );
        }
    }
    #[test]
    fn scrolls_within_bounds_and_only_escape_closes() {
        let mut view = View::default();
        view.open(None, false);
        let first = screen(&mut view, 60, 16);
        assert!(first.contains("Commands (live source registry)"));
        for _ in 0..200 {
            view.key(key(KeyCode::PageDown));
            screen(&mut view, 60, 16);
        }
        assert_eq!(view.scroll, view.max_scroll);
        assert!(screen(&mut view, 60, 16).contains("Ctrl+Shift+A"));
        for other in [KeyCode::Enter, KeyCode::Char('q'), KeyCode::Tab] {
            view.key(key(other));
            assert!(view.open);
        }
        view.key(KeyEvent::new(KeyCode::Esc, KeyModifiers::CONTROL));
        assert!(view.open);
        view.key(key(KeyCode::Esc));
        assert!(!view.open);
        assert!(screen(&mut view, 60, 16).trim().is_empty());
    }
}
