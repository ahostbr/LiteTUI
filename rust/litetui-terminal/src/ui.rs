use crate::demo::{Demo, ToolState};
use ratatui::{
    Frame,
    layout::{Alignment, Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Clear, Paragraph},
};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

// Ash: themes.py:228; derived CSS colors from Textual's color system.
const BG: Color = Color::Rgb(13, 13, 12);
const PANEL: Color = Color::Rgb(21, 21, 20);
const TOOL_BG: Color = Color::Rgb(0, 0, 0);
const TEXT: Color = Color::Rgb(208, 206, 200);
const MUTED: Color = Color::Rgb(139, 149, 167);
const TITLE: Color = Color::Rgb(116, 113, 106);
const BORDER: Color = Color::Rgb(79, 76, 70);
const THINK_BORDER: Color = Color::Rgb(186, 182, 172);
const CYAN: Color = Color::Rgb(208, 206, 200);
const AMBER: Color = Color::Rgb(232, 163, 61);
const RED: Color = Color::Rgb(229, 83, 75);

fn style(fg: Color, bg: Color) -> Style {
    Style::default().fg(fg).bg(bg)
}
fn clip(text: &str, width: usize) -> String {
    let mut used = 0;
    text.chars()
        .take_while(|ch| {
            used += ch.width().unwrap_or(0);
            used <= width
        })
        .collect()
}
fn padded(text: &str, width: usize) -> String {
    let value = clip(text, width);
    format!(
        "{}{}",
        value,
        " ".repeat(width.saturating_sub(value.width()))
    )
}
fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut result = Vec::new();
    for source in text.split('\n') {
        let mut line = String::new();
        let mut used = 0;
        for ch in source.chars() {
            let cells = ch.width().unwrap_or(0);
            if used + cells > width && !line.is_empty() {
                result.push(std::mem::take(&mut line));
                used = 0;
            }
            if cells <= width {
                line.push(ch);
                used += cells;
            }
        }
        result.push(line);
    }
    result
}

// ToolMessage is NOT a bordered card. Its header is a single amber tool name,
// gray argument summary, then subdued completion/duration/character metadata.
fn tool_header(app: &Demo, index: usize, width: usize) -> Line<'static> {
    let tool = &app.tools[index];
    let marker = if tool.expanded { "▾" } else { "▸" };
    let leading = format!("   {marker} 🔧 {}", tool.name);
    let status = match tool.state {
        ToolState::Success => format!(
            " · done · {} · {} chars",
            if index == 2 { "1.3s" } else { "0.4s" },
            tool.result.chars().count()
        ),
        ToolState::Running => format!(" · {:.1}s …", app.ticks as f64 / 10.0),
        ToolState::Awaiting => " · waiting for approval".into(),
        ToolState::Denied => " · denied · no action".into(),
        ToolState::Error => " · error · 2.0s".into(),
    };
    let available = width.saturating_sub(leading.width() + status.width() + 3);
    let summary = clip(tool.arguments, available.min(110));
    let selected = index == app.selected_tool;
    Line::from(vec![
        Span::styled(
            leading,
            style(AMBER, TOOL_BG).add_modifier(if selected {
                Modifier::BOLD | Modifier::UNDERLINED
            } else {
                Modifier::BOLD
            }),
        ),
        Span::styled(format!(" · {summary}"), style(MUTED, TOOL_BG)),
        Span::styled(
            status,
            style(
                if tool.state == ToolState::Error {
                    RED
                } else {
                    TITLE
                },
                TOOL_BG,
            ),
        ),
    ])
}

// AssistantMessage margins: left 2 / right 8; padding: 1 row / 2 columns.
// ThinkingBlock uses Textual's actual dashed glyphs: ┏╍┓ / ╏ / ┗╍┛.
fn thinking_card(lines: &mut Vec<Line<'static>>, index: usize, width: usize) {
    let narrow = width < 78;
    let left = if narrow { 0 } else { 2 };
    let right = if narrow { 0 } else { 8 };
    let outer_width = width.saturating_sub(left + right).max(12);
    let inner_width = outer_width.saturating_sub(6);
    let summaries = [
        "Inspecting the tool-call layout",
        "Checking arguments and results",
        "Verifying the focused checks",
        "Asking before a write",
        "Keeping a failed call visible",
    ];
    let summary = summaries[index];
    let title = clip(
        &format!("▾ Thinking: **{summary}** - mock-model"),
        outer_width.saturating_sub(5),
    );
    lines.push(Line::from(vec![
        Span::styled(" ".repeat(left), style(TEXT, BG)),
        Span::styled("╭─ ", style(BORDER, PANEL)),
        Span::styled(title.clone(), style(TITLE, PANEL)),
        Span::styled(
            format!(
                "{}╮",
                "─".repeat(outer_width.saturating_sub(title.width() + 4))
            ),
            style(BORDER, PANEL),
        ),
    ]));
    let outer_row = |inside: Vec<Span<'static>>| {
        let mut row = vec![
            Span::styled(" ".repeat(left), style(TEXT, BG)),
            Span::styled("│  ", style(BORDER, PANEL)),
        ];
        row.extend(inside);
        row.push(Span::styled("  │", style(BORDER, PANEL)));
        Line::from(row)
    };
    lines.push(outer_row(vec![Span::styled(
        " ".repeat(inner_width),
        style(TEXT, PANEL),
    )]));
    lines.push(outer_row(vec![Span::styled(
        format!("┏{}┓", "╍".repeat(inner_width.saturating_sub(2))),
        style(THINK_BORDER, PANEL),
    )]));
    let mut body = vec![
        "▾ Thinking · 0.4s · 16 tok · 40.9 tok/s".to_string(),
        summary.to_string(),
    ];
    if index == 1 {
        body.push("Matching the existing tool presentation".into());
    }
    for text in body {
        for row in wrap(&text, inner_width.saturating_sub(4)) {
            lines.push(outer_row(vec![
                Span::styled("╏ ", style(THINK_BORDER, PANEL)),
                Span::styled(
                    padded(&row, inner_width.saturating_sub(4)),
                    style(TEXT, PANEL).add_modifier(Modifier::BOLD),
                ),
                Span::styled(" ╏", style(THINK_BORDER, PANEL)),
            ]));
        }
    }
    lines.push(outer_row(vec![Span::styled(
        format!("┗{}┛", "╍".repeat(inner_width.saturating_sub(2))),
        style(THINK_BORDER, PANEL),
    )]));
    lines.push(outer_row(vec![Span::styled(
        " ".repeat(inner_width),
        style(TEXT, PANEL),
    )]));
    let speech = "♫ Speak [mock]";
    lines.push(outer_row(vec![Span::styled(
        format!(
            "{}{speech}",
            " ".repeat(inner_width.saturating_sub(speech.width()))
        ),
        style(TITLE, PANEL),
    )]));
    lines.push(Line::styled(
        format!(
            "{}╰{}╯",
            " ".repeat(left),
            "─".repeat(outer_width.saturating_sub(2))
        ),
        style(BORDER, PANEL),
    ));
    lines.push(Line::raw(""));
}

fn transcript(app: &Demo, width: usize) -> (Vec<Line<'static>>, (usize, usize)) {
    let mut lines = vec![
        Line::styled(
            "  ▸ You · Show me the tool calls and their results.",
            style(TEXT, BG),
        ),
        Line::raw(""),
    ];
    let mut selected_range = (0, 0);
    for (index, tool) in app.tools.iter().enumerate() {
        thinking_card(&mut lines, index, width);
        let start = lines.len();
        lines.push(tool_header(app, index, width));
        if tool.expanded {
            let fields = [
                ("Arguments", MUTED),
                (tool.arguments, TEXT),
                ("", MUTED),
                (
                    if tool.state == ToolState::Running {
                        "Output (running)"
                    } else {
                        "Result"
                    },
                    MUTED,
                ),
                (
                    &tool.result,
                    if tool.state == ToolState::Error {
                        RED
                    } else {
                        MUTED
                    },
                ),
            ];
            for (text, color) in fields {
                for row in wrap(text, width.saturating_sub(8)) {
                    lines.push(Line::styled(format!("     {row}"), style(color, TOOL_BG)));
                }
            }
            lines.push(Line::styled(" ".repeat(width), style(TEXT, TOOL_BG)));
        }
        lines.push(Line::raw(""));
        if index == app.selected_tool {
            selected_range = (start, lines.len());
        }
    }
    for user in &app.turns {
        for row in wrap(&format!("You: {user}"), width.saturating_sub(4)) {
            lines.push(Line::styled(format!("  {row}"), style(TEXT, PANEL)));
        }
        for row in wrap(
            "Preview reply: received locally. No real model or tools are connected.",
            width.saturating_sub(4),
        ) {
            lines.push(Line::styled(format!("  {row}"), style(TEXT, PANEL)));
        }
        lines.push(Line::raw(""));
    }
    (lines, selected_range)
}

pub fn draw(frame: &mut Frame, app: &mut Demo) {
    let area = frame.area();
    frame.render_widget(Block::default().style(style(TEXT, BG)), area);
    app.viewport_too_small = area.width < 36 || area.height < 14;
    if app.viewport_too_small {
        frame.render_widget(Paragraph::new(if app.approval_open {
            "LOCAL PREVIEW\nAPPROVAL PAUSED\nResize to 36 x 14.\nEsc closes dialog.\nCtrl+Q exits."
        } else { "LOCAL PREVIEW\nResize to 36 x 14.\nCtrl+Q exits.\nNo tools execute." }).style(style(TEXT, BG)), area);
        return;
    }
    let areas = Layout::vertical([
        Constraint::Length(1),
        Constraint::Length(1),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(5),
        Constraint::Length(2),
    ])
    .split(area);
    frame.render_widget(
        Paragraph::new("LiteTUI — Rust preview · mock-model · tools:5 · think:high")
            .alignment(Alignment::Center)
            .style(style(TITLE, PANEL)),
        areas[0],
    );
    frame.render_widget(
        Paragraph::new(
            " LOCAL PREVIEW · No model connected · all calls, timings and counts are fixtures",
        )
        .style(style(AMBER, BG)),
        areas[1],
    );
    let viewport = Rect::new(
        areas[2].x + 1,
        areas[2].y,
        areas[2].width.saturating_sub(2),
        areas[2].height,
    );
    let (lines, selected) = transcript(app, viewport.width as usize);
    let height = viewport.height as usize;
    app.max_scroll = lines.len().saturating_sub(height);
    if app.follow_selection {
        if selected.0 < app.scroll || selected.1.saturating_sub(selected.0) >= height {
            app.scroll = selected.0;
        } else if selected.1 > app.scroll.saturating_add(height) {
            app.scroll = selected.1.saturating_sub(height);
        }
        app.follow_selection = false;
    }
    app.scroll = app.scroll.min(app.max_scroll);
    frame.render_widget(
        Paragraph::new(
            lines
                .into_iter()
                .skip(app.scroll)
                .take(height)
                .collect::<Vec<_>>(),
        )
        .style(style(TEXT, BG)),
        viewport,
    );
    frame.render_widget(
        Paragraph::new(format!(
            " {} · scroll {}/{}",
            app.notice, app.scroll, app.max_scroll
        ))
        .style(style(TITLE, BG)),
        areas[3],
    );
    let input_width = areas[4].width.saturating_sub(5) as usize;
    let mut tail = String::new();
    for ch in app.input.chars().rev() {
        if tail.width() + ch.width().unwrap_or(0) > input_width {
            break;
        }
        tail.insert(0, ch);
    }
    let composer = Paragraph::new(if app.input.is_empty() {
        "Message... (preview only; Enter adds a local mock turn)"
    } else {
        &tail
    })
    .style(style(
        if app.input.is_empty() { TITLE } else { TEXT },
        PANEL,
    ))
    .block(
        Block::default()
            .borders(Borders::ALL)
            .border_style(style(THINK_BORDER, PANEL)),
    );
    frame.render_widget(composer, areas[4]);
    if !app.approval_open {
        frame.set_cursor_position((areas[4].x + 1 + tail.width() as u16, areas[4].y + 1));
    }
    frame.render_widget(
        Paragraph::new(vec![
            Line::styled(
                "Tab next tool · Ctrl+E/F2 expand · F6 mock approval · F5 replay · Ctrl+Q quit",
                style(MUTED, BG),
            ),
            Line::styled(
                "↑↓ / PgUp PgDn / mouse wheel scroll · Ash theme · offline fixture",
                style(TITLE, BG),
            ),
        ]),
        areas[5],
    );
    if app.approval_open {
        approval(frame, app, area);
    }
    // Wide glyph continuation cells can be reset by Paragraph; never let them
    // inherit a terminal's potentially light default background.
    for cell in &mut frame.buffer_mut().content {
        if cell.bg == Color::Reset {
            cell.set_bg(BG);
        }
    }
}

fn approval(frame: &mut Frame, app: &Demo, area: Rect) {
    let width = area.width.saturating_sub(4).min(76);
    let height = area.height.saturating_sub(2).min(15);
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    let mut lines = vec![
        Line::styled(
            "No tool will execute. This dialog changes fixture state only.",
            style(AMBER, PANEL),
        ),
        Line::raw(""),
        Line::styled("Tool    write", style(TEXT, PANEL)),
        Line::styled("Target  preview-notes.md", style(TEXT, PANEL)),
        Line::styled(
            "Change  Save the tool-card layout notes",
            style(TEXT, PANEL),
        ),
        Line::raw(""),
        Line::styled(
            "A real runtime would enforce policy and match the approval ID.",
            style(MUTED, PANEL),
        ),
        Line::styled(
            "This preview has no runtime and cannot grant real permissions.",
            style(MUTED, PANEL),
        ),
        Line::raw(""),
    ];
    lines.push(Line::from(vec![
        Span::styled(
            if app.approval_allow {
                "  [  Deny mock  ]  "
            } else {
                "  [> Deny mock <]  "
            },
            style(if app.approval_allow { MUTED } else { AMBER }, PANEL),
        ),
        Span::styled(
            if app.approval_allow {
                "  [> Allow mock <]"
            } else {
                "  [  Allow mock  ]"
            },
            style(if app.approval_allow { CYAN } else { MUTED }, PANEL),
        ),
    ]));
    lines.push(Line::styled(
        "Tab selects · Enter confirms · A allow · D deny · Esc closes",
        style(MUTED, PANEL),
    ));
    if width < 68 || height < 15 {
        lines = vec![
            Line::styled("No tool will execute.", style(AMBER, PANEL)),
            Line::styled("write preview-notes.md", style(TEXT, PANEL)),
            Line::styled("No file will be written.", style(MUTED, PANEL)),
            Line::raw(""),
            Line::styled(
                if app.approval_allow {
                    "[  Deny mock  ]"
                } else {
                    "[> Deny mock <]"
                },
                style(AMBER, PANEL),
            ),
            Line::styled(
                if app.approval_allow {
                    "[> Allow mock <]"
                } else {
                    "[  Allow mock  ]"
                },
                style(CYAN, PANEL),
            ),
            Line::styled("Tab select | Enter confirm", style(MUTED, PANEL)),
            Line::styled("A allow | D deny | Esc close", style(MUTED, PANEL)),
        ];
    }
    frame.render_widget(
        Paragraph::new(lines).style(style(TEXT, PANEL)).block(
            Block::default()
                .title(" MOCK APPROVAL · write ")
                .borders(Borders::ALL)
                .border_type(BorderType::Rounded)
                .border_style(style(AMBER, PANEL)),
        ),
        rect,
    );
}
