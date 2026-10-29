//! Explicit launch choice. This module cannot start a runtime or write settings.
use crate::tool_mode::Profile;
use crossterm::event::{Event, KeyCode, KeyEventKind, KeyModifiers, MouseButton, MouseEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::{Color, Modifier, Style},
    text::Line,
    widgets::{Block, Paragraph},
};

const PROFILES: [Profile; 3] = [Profile::Strict, Profile::Interactive, Profile::Autonomous];
#[derive(Clone, Copy, Debug, PartialEq, Eq, Default)]
pub enum Selection {
    #[default]
    Pending,
    Selected(Profile),
    Cancelled,
}
#[derive(Default)]
pub struct Picker {
    cursor: usize,
    result: Selection,
    visible: bool,
    geometry: Option<Rect>,
    hits: Vec<Rect>,
    pressed: Option<usize>,
}
impl Picker {
    pub fn selection(&self) -> Selection {
        self.result
    }
    pub fn handle(&mut self, event: Event) -> Selection {
        if self.result != Selection::Pending {
            return self.result;
        }
        match event {
            Event::Resize(..) => {
                self.visible = false;
                self.hits.clear();
                self.pressed = None;
            }
            Event::Key(key) if key.kind == KeyEventKind::Press => {
                self.pressed = None;
                if (key.code == KeyCode::Esc && key.modifiers.is_empty())
                    || (matches!(key.code, KeyCode::Char('q' | 'Q'))
                        && key.modifiers == KeyModifiers::CONTROL)
                {
                    self.result = Selection::Cancelled;
                } else if self.visible && key.modifiers.is_empty() {
                    match key.code {
                        KeyCode::Down | KeyCode::Tab => {
                            self.cursor = (self.cursor + 1) % PROFILES.len()
                        }
                        KeyCode::Up | KeyCode::BackTab => {
                            self.cursor = (self.cursor + PROFILES.len() - 1) % PROFILES.len()
                        }
                        KeyCode::Enter => self.result = Selection::Selected(PROFILES[self.cursor]),
                        _ => {}
                    }
                } else if self.visible
                    && key.code == KeyCode::BackTab
                    && key.modifiers == KeyModifiers::SHIFT
                {
                    self.cursor = (self.cursor + PROFILES.len() - 1) % PROFILES.len();
                }
            }
            Event::Mouse(mouse) if self.visible => {
                let hit = self
                    .hits
                    .iter()
                    .position(|rect| rect.contains((mouse.column, mouse.row).into()));
                match mouse.kind {
                    MouseEventKind::Down(MouseButton::Left) => self.pressed = hit,
                    MouseEventKind::Up(MouseButton::Left) => {
                        if self
                            .pressed
                            .take()
                            .is_some_and(|pressed| Some(pressed) == hit)
                        {
                            self.cursor = hit.expect("matched press");
                        }
                    }
                    _ => {}
                }
            }
            // Paste, focus, repeat and release events never constitute launch intent.
            _ => {}
        }
        self.result
    }
    pub fn draw(&mut self, frame: &mut Frame, tools_enabled: bool) {
        let area = frame.area();
        let bg = Color::Rgb(11, 13, 17);
        let fg = Color::Rgb(220, 224, 231);
        let muted = Color::Rgb(139, 149, 167);
        frame.render_widget(Block::default().style(Style::default().bg(bg).fg(fg)), area);
        if self.geometry != Some(area) {
            self.pressed = None;
            self.geometry = Some(area);
        }
        self.hits.clear();
        self.visible = area.width >= 36 && area.height >= 14 && self.result == Selection::Pending;
        if !self.visible {
            frame.render_widget(Paragraph::new("Choose tool profile\nPython has not started.\nResize to 36 x 14.\nEsc or Ctrl+Q cancels.").style(Style::default().bg(bg).fg(fg)),area);
            return;
        }
        let width = area.width.saturating_sub(2).min(70);
        let inner = Rect::new(
            area.x + (area.width - width) / 2,
            area.y + (area.height - 14) / 2,
            width,
            14,
        );
        let labels = [
            "Ask for commands and writes.",
            "Ask: destructive/rare commands.",
            "No profile confirmations.",
        ];
        let mut lines = vec![
            Line::styled(
                "Choose tool profile",
                Style::default().add_modifier(Modifier::BOLD),
            ),
            Line::raw("Python has not started."),
            Line::raw(if tools_enabled {
                "Tools: on"
            } else {
                "Tools: off"
            }),
            Line::raw(""),
        ];
        for (i, profile) in PROFILES.iter().enumerate() {
            let selected = i == self.cursor;
            lines.push(Line::styled(
                format!("{} {}", if selected { ">" } else { " " }, profile.as_str()),
                Style::default()
                    .fg(if selected {
                        Color::Rgb(139, 183, 255)
                    } else {
                        fg
                    })
                    .add_modifier(if selected {
                        Modifier::BOLD
                    } else {
                        Modifier::empty()
                    }),
            ));
            lines.push(Line::styled(
                format!("  {}", labels[i]),
                Style::default().fg(muted),
            ));
            self.hits.push(Rect::new(
                inner.x,
                inner.y + 4 + (i as u16) * 2,
                inner.width,
                2,
            ));
        }
        lines.push(Line::styled(
            "Source restrictions still apply.",
            Style::default().fg(muted),
        ));
        lines.push(Line::raw("Up/Down select | Enter starts"));
        lines.push(Line::raw("Esc cancels | Ctrl+Q quits"));
        frame.render_widget(
            Paragraph::new(lines).style(Style::default().bg(bg).fg(fg)),
            inner,
        );
    }
}
