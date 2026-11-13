//! `/tools`: Python's tool list (src/litetui/tool_list.py ToolListBody), drawn natively.
//!
//! Python opens ToolListBody in its modal host (align center middle, the body full width,
//! height auto): the bold "Tools" title, the global Switch with its label, a rule, the
//! muted note, an 18-row scroll region (padding 0 1) holding a Checkbox per tool and its
//! "authority — description" line (padding 0 0 1 3), the MCP note and a centred row of
//! Close (primary) and Dock to side. Each tick or switch applies at once; there is no Save.
//! This module only lays the body out and maps keys and clicks to what was hit; the view
//! that owns it runs the change through the runtime's own checks.
use crate::{
    picker_box::{button, wrap},
    runtime_controls::Snapshot,
    theme::Theme,
};
use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::{
    buffer::Buffer,
    layout::Rect,
    style::{Color, Modifier, Style},
};
use unicode_width::UnicodeWidthStr;

pub const NOTE: &str = "Unticking a tool hides it from the model and refuses it if called anyway. Changes apply at once — there is no save.";
pub const GLOBAL: &str = "All tools — the same switch as Ctrl+T and Settings → Agent loop";
const SCROLL_ROWS: u16 = 18;

/// What a key or click landed on.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    Switch,
    /// The #tl-scroll VerticalScroll, itself a focus stop in Textual.
    Scroll,
    Tool(usize),
    Close,
    Dock,
}

/// What the owner should do.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Act {
    Toggle(Target),
    Close,
    Nothing,
}

#[derive(Default)]
pub struct List {
    /// Textual's focus chain in DOM order: 0 the Switch, 1 the scroll region, 2..=n+1 the
    /// tools, n+2 Close, n+3 Dock to side. The host focuses the first focusable on mount.
    focus: usize,
    scroll: usize,
    max_scroll: usize,
    tools: usize,
    /// Each tool's first content row in the scroll region.
    offsets: Vec<usize>,
    hits: Vec<(Rect, Target)>,
    region: Option<Rect>,
}

pub fn detail(authority: &str, description: &str, summary: &str) -> String {
    let text = if description.is_empty() {
        summary
    } else {
        description
    };
    if text.is_empty() {
        authority.to_owned()
    } else {
        format!("{authority} — {text}")
    }
}

/// Textual's tall border: ▊ ▔ ▎ / ▊ ▎ / ▊ ▁ ▎ around a filled $surface interior.
fn tall(buf: &mut Buffer, rect: Rect, edge: Color, t: &Theme) {
    let inner = usize::from(rect.width.saturating_sub(2));
    let style = Style::default().fg(edge).bg(t.surface);
    for y in rect.top()..rect.bottom() {
        let fill = if y == rect.top() {
            "▔"
        } else if y + 1 == rect.bottom() {
            "▁"
        } else {
            " "
        };
        buf.set_string(rect.x, y, format!("▊{}▎", fill.repeat(inner)), style);
    }
}

impl List {
    pub fn reset(&mut self) {
        *self = Self::default();
    }
    fn target(&self, index: usize) -> Target {
        match index {
            0 => Target::Switch,
            1 => Target::Scroll,
            i if i <= self.tools + 1 => Target::Tool(i - 2),
            i if i == self.tools + 2 => Target::Close,
            _ => Target::Dock,
        }
    }
    fn index(&self, target: Target) -> usize {
        match target {
            Target::Switch => 0,
            Target::Scroll => 1,
            Target::Tool(i) => i + 2,
            Target::Close => self.tools + 2,
            Target::Dock => self.tools + 3,
        }
    }
    fn reveal(&mut self) {
        if let Target::Tool(i) = self.target(self.focus)
            && let Some(&top) = self.offsets.get(i)
        {
            let rows = usize::from(SCROLL_ROWS);
            if top < self.scroll {
                self.scroll = top;
            } else if top + 3 > self.scroll + rows {
                self.scroll = (top + 3).saturating_sub(rows).min(self.max_scroll);
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Act {
        if key.kind != KeyEventKind::Press {
            return Act::Nothing;
        }
        let count = self.tools + 4;
        // The scroll keys are VerticalScroll's bindings: they reach it from the region or a
        // checkbox inside it, not from the Switch or the buttons.
        let in_scroll = matches!(self.target(self.focus), Target::Scroll | Target::Tool(_));
        match key.code {
            KeyCode::Esc => return Act::Close,
            KeyCode::Tab if key.modifiers.is_empty() => {
                self.focus = (self.focus + 1) % count;
                self.reveal();
            }
            KeyCode::BackTab => {
                self.focus = (self.focus + count - 1) % count;
                self.reveal();
            }
            KeyCode::Enter | KeyCode::Char(' ') if key.modifiers.is_empty() => {
                return match self.target(self.focus) {
                    Target::Close => Act::Close,
                    Target::Dock | Target::Scroll => Act::Nothing,
                    target => Act::Toggle(target),
                };
            }
            KeyCode::Up if in_scroll => self.scroll = self.scroll.saturating_sub(1),
            KeyCode::Down if in_scroll => self.scroll = (self.scroll + 1).min(self.max_scroll),
            KeyCode::PageUp if in_scroll => {
                self.scroll = self.scroll.saturating_sub(usize::from(SCROLL_ROWS))
            }
            KeyCode::PageDown if in_scroll => {
                self.scroll = (self.scroll + usize::from(SCROLL_ROWS)).min(self.max_scroll)
            }
            KeyCode::Home if in_scroll && key.modifiers == KeyModifiers::NONE => self.scroll = 0,
            KeyCode::End if in_scroll && key.modifiers == KeyModifiers::NONE => {
                self.scroll = self.max_scroll
            }
            _ => {}
        }
        Act::Nothing
    }
    pub fn mouse(&mut self, event: MouseEvent) -> Act {
        let at = (event.column, event.row).into();
        match event.kind {
            MouseEventKind::Down(MouseButton::Left) => {
                let Some(&(_, target)) = self.hits.iter().find(|(r, _)| r.contains(at)) else {
                    return Act::Nothing;
                };
                self.focus = self.index(target);
                match target {
                    Target::Close => Act::Close,
                    Target::Dock | Target::Scroll => Act::Nothing,
                    target => Act::Toggle(target),
                }
            }
            MouseEventKind::ScrollDown if self.region.is_some_and(|r| r.contains(at)) => {
                self.scroll = (self.scroll + 2).min(self.max_scroll);
                Act::Nothing
            }
            MouseEventKind::ScrollUp if self.region.is_some_and(|r| r.contains(at)) => {
                self.scroll = self.scroll.saturating_sub(2);
                Act::Nothing
            }
            _ => Act::Nothing,
        }
    }
    pub fn draw(
        &mut self,
        buf: &mut Buffer,
        area: Rect,
        snapshot: &Snapshot,
        notice: &str,
        t: &Theme,
    ) {
        self.hits.clear();
        self.region = None;
        let base = Style::default().fg(t.text()).bg(t.screen());
        buf.set_style(area, base);
        for y in area.top()..area.bottom() {
            buf.set_string(area.x, y, " ".repeat(usize::from(area.width)), base);
        }
        let Some(inventory) = snapshot.tools.as_ref() else {
            return;
        };
        let w = usize::from(area.width);
        if w < 20 {
            return;
        }
        self.tools = inventory.rows.len();
        self.focus = self.focus.min(self.tools + 3);
        // #tl-note, .tl-auth, #tl-mcp: $text-muted over the screen.
        let screen = t.screen();
        let muted = Style::default().fg(t.muted_on(screen)).bg(screen);
        let note = wrap(NOTE, w);
        let mcp = (inventory.dynamic_sources > 0).then(|| {
            wrap(
                &format!(
                    "MCP servers ({}) provide tools too. Those are switched off per SERVER in Settings → Capabilities, not here.",
                    inventory.dynamic_sources
                ),
                w,
            )
        });
        // The scroll region's content: a 3-row Checkbox, the detail lines, a blank row.
        let layout = |content: usize| {
            let mut offsets = Vec::new();
            let mut details = Vec::new();
            let mut y = 0;
            for row in &inventory.rows {
                offsets.push(y);
                let lines = wrap(
                    &detail(&row.authority, &row.description, &row.summary),
                    content.saturating_sub(3).max(1),
                );
                y += 3 + lines.len() + 1;
                details.push(lines);
            }
            (offsets, details, y)
        };
        let (mut offsets, mut details, mut total) = layout(w.saturating_sub(2));
        let scrolling = total > usize::from(SCROLL_ROWS);
        if scrolling {
            (offsets, details, total) = layout(w.saturating_sub(4));
        }
        self.offsets = offsets.clone();
        self.max_scroll = total.saturating_sub(usize::from(SCROLL_ROWS));
        self.scroll = self.scroll.min(self.max_scroll);
        let height = 2
            + 5
            + (note.len() + 2)
            + usize::from(SCROLL_ROWS)
            + mcp.as_ref().map_or(0, |m| 1 + m.len())
            + 4;
        let top = area.y + (usize::from(area.height).saturating_sub(height) / 2) as u16;
        let bottom = area.bottom();
        let put = |buf: &mut Buffer, x: u16, y: u16, text: &str, style: Style| {
            if y < bottom {
                buf.set_string(x, y, text, style);
            }
        };
        let mut y = top;
        put(buf, area.x, y, "Tools", base.add_modifier(Modifier::BOLD));
        y += 2;
        // #tl-global: the Switch (padding 0 2, a 4-cell slider) and its label.
        let focused = |index: usize| self.focus == index;
        // Switch and Checkbox: `tall $border-blurred` on $surface; `:focus` is `tall $border`
        // with `background-tint: $foreground 5%`.
        let styled = !t.css.is_empty();
        let edge = |on: bool| {
            if on {
                t.var("border", t.primary)
            } else {
                t.var("border-blurred", t.thinking_border)
            }
        };
        let inside = |on: bool| if on && styled { t.input() } else { t.surface_bg() };
        let switch = Rect::new(area.x, y, 10, 3);
        if y + 3 <= bottom {
            if styled {
                crate::picker_box::tall_edges(buf, switch, edge(focused(0)), inside(focused(0)), screen, t.text());
            } else {
                tall(buf, switch, edge(focused(0)), t);
            }
            let on = snapshot.facts.tools_enabled;
            // .switch--slider: $panel (on: $success) over $panel-darken-2.
            let track = Style::default().bg(t.var("panel-darken-2", t.panel));
            let thumb = Style::default().bg(if on {
                t.var("success", t.title)
            } else {
                t.var("panel", t.thinking_border)
            });
            buf.set_string(area.x + 3, y + 1, "    ", track);
            buf.set_string(area.x + 3 + if on { 2 } else { 0 }, y + 1, "  ", thumb);
            self.hits.push((switch, Target::Switch));
        }
        put(buf, area.x + 11, y + 1, GLOBAL, base);
        y += 4;
        // #tl-global's `border-bottom: solid $foreground 20%`.
        put(
            buf,
            area.x,
            y,
            &"─".repeat(w),
            Style::default()
                .fg(t.css_alpha("foreground", 20.0, screen, t.thinking_border))
                .bg(screen),
        );
        y += 2;
        for line in &note {
            put(buf, area.x, y, line, muted);
            y += 1;
        }
        y += 1;
        // #tl-scroll.
        let region = Rect::new(
            area.x,
            y,
            area.width,
            SCROLL_ROWS.min(bottom.saturating_sub(y)),
        );
        self.region = Some(region);
        let first = self.scroll;
        let last = self.scroll + usize::from(region.height);
        let row_y = |content: usize| region.y + (content - first) as u16;
        for (index, tool) in inventory.rows.iter().enumerate() {
            let start = offsets[index];
            let label_w = tool.name.width() as u16;
            let checkbox = Rect::new(area.x + 1, 0, label_w + 9, 1);
            for dy in 0..3usize {
                let c = start + dy;
                if c < first || c >= last {
                    continue;
                }
                let yy = row_y(c);
                let fill = match dy {
                    0 => "▔",
                    2 => "▁",
                    _ => " ",
                };
                let is_focused = focused(index + 2);
                let bg = inside(is_focused);
                if styled {
                    let row = Rect::new(checkbox.x, yy, label_w + 9, 1);
                    let border = edge(is_focused);
                    // One row of the tall border (the row's own top/middle/bottom part).
                    let mut part = |glyph: &str, style: Style| {
                        buf.set_string(row.x + 1, yy, glyph.repeat(usize::from(label_w + 7)), style);
                    };
                    part(
                        fill,
                        Style::default()
                            .fg(if dy == 1 { t.text() } else { border })
                            .bg(bg),
                    );
                    buf.set_string(row.x, yy, "▊", Style::default().fg(screen).bg(border));
                    buf.set_string(row.right() - 1, yy, "▎", Style::default().fg(border).bg(screen));
                } else {
                    let style = Style::default().fg(edge(is_focused)).bg(t.surface);
                    buf.set_string(
                        checkbox.x,
                        yy,
                        format!("▊{}▎", fill.repeat(usize::from(label_w + 7))),
                        style,
                    );
                }
                if dy == 1 {
                    // .toggle--button: $panel behind the X, `$text-success` when on and
                    // `$panel-darken-2` when off; the half blocks are the button's colour
                    // on the checkbox's own background.
                    let panel = t.var("panel", t.panel);
                    let side = Style::default().fg(panel).bg(bg);
                    let inner = Style::default()
                        .fg(if tool.enabled {
                            t.css_over("text-success", panel, t.title)
                        } else {
                            t.var("panel-darken-2", t.background)
                        })
                        .bg(panel);
                    buf.set_string(checkbox.x + 2, yy, "▐", side);
                    buf.set_string(checkbox.x + 3, yy, "X", inner);
                    buf.set_string(checkbox.x + 4, yy, "▌", side);
                    let label = if focused(index + 2) {
                        let cursor = t.var("block-cursor-background", t.primary);
                        Style::default()
                            .fg(t.css_over("block-cursor-foreground", cursor, t.background))
                            .bg(cursor)
                    } else {
                        Style::default().fg(t.text()).bg(bg)
                    };
                    buf.set_string(checkbox.x + 6, yy, &tool.name, label);
                }
            }
            let shown_top = start.max(first);
            let shown_bottom = (start + 3).min(last);
            if shown_top < shown_bottom {
                self.hits.push((
                    Rect::new(
                        checkbox.x,
                        row_y(shown_top),
                        checkbox.width,
                        (shown_bottom - shown_top) as u16,
                    ),
                    Target::Tool(index),
                ));
            }
            for (i, line) in details[index].iter().enumerate() {
                let c = start + 3 + i;
                if c >= first && c < last {
                    buf.set_string(area.x + 4, row_y(c), line, muted);
                }
            }
        }
        if scrolling {
            crate::picker_box::scrollbar(
                buf,
                area.right().saturating_sub(3),
                region.y,
                region.height,
                total,
                self.scroll,
                t,
            );
            crate::picker_box::scrollbar(
                buf,
                area.right().saturating_sub(2),
                region.y,
                region.height,
                total,
                self.scroll,
                t,
            );
        }
        y = region.bottom();
        if let Some(mcp) = &mcp {
            y += 1;
            for line in mcp {
                put(buf, area.x, y, line, muted);
                y += 1;
            }
        }
        y += 1;
        // #tl-buttons: Close (primary, margin-right 1) and the inline Dock to side.
        if y + 3 <= bottom {
            let x = area.x + (area.width.saturating_sub(34)) / 2;
            button(buf, x, y, "Close", t, true);
            button(buf, x + 17, y, "Dock to side", t, false);
            self.hits.push((Rect::new(x, y, 16, 3), Target::Close));
            self.hits.push((Rect::new(x + 17, y, 16, 3), Target::Dock));
            y += 3;
        }
        if !notice.is_empty() && y + 1 < bottom {
            let line = crate::live::terminal_text(notice);
            let shown: String = wrap(&line, w).into_iter().next().unwrap_or_default();
            let x = area.x + (w.saturating_sub(shown.width()) / 2) as u16;
            put(
                buf,
                x,
                y + 1,
                &shown,
                Style::default().fg(t.var("error", t.error)).bg(screen),
            );
        }
    }
}
