use crate::{
    completion::CompletionKind,
    demo::{Demo, ToolState},
};
use ratatui::{
    Frame,
    layout::{Alignment, Constraint, Layout, Rect},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, BorderType, Borders, Clear, List, ListItem, ListState, Paragraph},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

// Legacy ToolMessage argument/result gray is fixed in the Python widget too.
const MUTED: Color = Color::Rgb(139, 149, 167);

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
fn clip_completion_name(text: &str, width: usize) -> String {
    let safe: String = text.chars().filter(|ch| !ch.is_control()).collect();
    let mut used = 0;
    safe.graphemes(true)
        .take_while(|glyph| {
            used += glyph.width();
            used <= width
        })
        .collect()
}
fn tail(text: &str, width: usize) -> String {
    let mut value = String::new();
    let mut used = 0;
    for ch in text.chars().rev() {
        let cells = ch.width().unwrap_or(0);
        if used + cells > width {
            break;
        }
        value.insert(0, ch);
        used += cells;
    }
    value
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
    let t = app.palette();
    let tool = &app.tools[index];
    let marker = if tool.expanded { "▾" } else { "▸" };
    if width < 60 {
        let status = match tool.state {
            ToolState::Success => "done",
            ToolState::Running => "running",
            ToolState::Awaiting => "waiting approval",
            ToolState::Denied => "denied",
            ToolState::Error => "error",
        };
        return Line::from(vec![
            Span::styled(
                format!(" {marker} {}", tool.name),
                style(t.tool_text, t.tool_background).add_modifier(if index == app.selected_tool {
                    Modifier::BOLD | Modifier::UNDERLINED
                } else {
                    Modifier::BOLD
                }),
            ),
            Span::styled(
                format!(" · {status}"),
                style(
                    if tool.state == ToolState::Error {
                        t.error
                    } else {
                        t.title
                    },
                    t.tool_background,
                ),
            ),
        ]);
    }
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
            style(t.tool_text, t.tool_background).add_modifier(if selected {
                Modifier::BOLD | Modifier::UNDERLINED
            } else {
                Modifier::BOLD
            }),
        ),
        Span::styled(format!(" · {summary}"), style(MUTED, t.tool_background)),
        Span::styled(
            status,
            style(
                if tool.state == ToolState::Error {
                    t.error
                } else {
                    t.title
                },
                t.tool_background,
            ),
        ),
    ])
}

// AssistantMessage margins: left 2 / right 8; padding: 1 row / 2 columns.
// ThinkingBlock uses Textual's actual dashed glyphs: ┏╍┓ / ╏ / ┗╍┛.
fn thinking_card(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    index: usize,
    width: usize,
) {
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
        Span::styled(" ".repeat(left), style(t.foreground, t.background)),
        Span::styled("╭─ ", style(t.assistant_border, t.surface)),
        Span::styled(title.clone(), style(t.title, t.surface)),
        Span::styled(
            format!(
                "{}╮",
                "─".repeat(outer_width.saturating_sub(title.width() + 4))
            ),
            style(t.assistant_border, t.surface),
        ),
    ]));
    let outer_row = |inside: Vec<Span<'static>>| {
        let mut row = vec![
            Span::styled(" ".repeat(left), style(t.foreground, t.background)),
            Span::styled("│  ", style(t.assistant_border, t.surface)),
        ];
        row.extend(inside);
        row.push(Span::styled("  │", style(t.assistant_border, t.surface)));
        Line::from(row)
    };
    lines.push(outer_row(vec![Span::styled(
        " ".repeat(inner_width),
        style(t.foreground, t.surface),
    )]));
    lines.push(outer_row(vec![Span::styled(
        format!("┏{}┓", "╍".repeat(inner_width.saturating_sub(2))),
        style(t.thinking_border, t.surface),
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
                Span::styled("╏ ", style(t.thinking_border, t.surface)),
                Span::styled(
                    padded(&row, inner_width.saturating_sub(4)),
                    style(t.thinking_text, t.surface).add_modifier(Modifier::BOLD),
                ),
                Span::styled(" ╏", style(t.thinking_border, t.surface)),
            ]));
        }
    }
    lines.push(outer_row(vec![Span::styled(
        format!("┗{}┛", "╍".repeat(inner_width.saturating_sub(2))),
        style(t.thinking_border, t.surface),
    )]));
    lines.push(outer_row(vec![Span::styled(
        " ".repeat(inner_width),
        style(t.foreground, t.surface),
    )]));
    let speech = "♫ Speak [mock]";
    lines.push(outer_row(vec![Span::styled(
        format!(
            "{}{speech}",
            " ".repeat(inner_width.saturating_sub(speech.width()))
        ),
        style(t.title, t.surface),
    )]));
    lines.push(Line::styled(
        format!(
            "{}╰{}╯",
            " ".repeat(left),
            "─".repeat(outer_width.saturating_sub(2))
        ),
        style(t.assistant_border, t.surface),
    ));
    lines.push(Line::raw(""));
}

fn transcript(app: &Demo, width: usize) -> (Vec<Line<'static>>, (usize, usize)) {
    let t = app.palette();
    let mut lines = vec![
        Line::styled(
            "  ▸ You · Show me the tool calls and their results.",
            style(t.foreground, t.background),
        ),
        Line::raw(""),
    ];
    let mut selected_range = (0, 0);
    for (index, tool) in app.tools.iter().enumerate() {
        thinking_card(t, &mut lines, index, width);
        let start = lines.len();
        lines.push(tool_header(app, index, width));
        if tool.expanded {
            let fields = [
                ("Arguments", MUTED),
                (tool.arguments, t.foreground),
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
                        t.error
                    } else {
                        MUTED
                    },
                ),
            ];
            for (text, color) in fields {
                for row in wrap(text, width.saturating_sub(8)) {
                    lines.push(Line::styled(
                        format!("     {row}"),
                        style(color, t.tool_background),
                    ));
                }
            }
            lines.push(Line::styled(
                " ".repeat(width),
                style(t.foreground, t.tool_background),
            ));
        }
        lines.push(Line::raw(""));
        if index == app.selected_tool {
            selected_range = (start, lines.len());
        }
    }
    for user in &app.turns {
        for row in wrap(&format!("You: {user}"), width.saturating_sub(4)) {
            lines.push(Line::styled(
                format!("  {row}"),
                style(t.foreground, t.surface),
            ));
        }
        for row in wrap(
            "Preview reply: received locally. No real model or tools are connected.",
            width.saturating_sub(4),
        ) {
            lines.push(Line::styled(
                format!("  {row}"),
                style(t.foreground, t.surface),
            ));
        }
        lines.push(Line::raw(""));
    }
    (lines, selected_range)
}

pub fn draw(frame: &mut Frame, app: &mut Demo) {
    let t = app.palette().clone();
    let area = frame.area();
    frame.render_widget(
        Block::default().style(style(t.foreground, t.background)),
        area,
    );
    app.viewport_too_small = area.width < 36 || area.height < 14;
    if app.viewport_too_small {
        app.footer.update_visible(&[]);
        app.completion.hide();
        frame.render_widget(Paragraph::new(if app.themes_open {
            "LOCAL PREVIEW\nTHEME PICKER PAUSED\nResize to 36 x 14.\nEsc closes dialog.\nCtrl+Q exits."
        } else if app.approval_open {
            "LOCAL PREVIEW\nAPPROVAL PAUSED\nResize to 36 x 14.\nEsc closes dialog.\nCtrl+Q exits."
        } else { "LOCAL PREVIEW\nResize to 36 x 14.\nCtrl+Q exits.\nNo tools execute." }).style(style(t.foreground, t.background)), area);
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
            .style(style(t.title, t.surface)),
        areas[0],
    );
    frame.render_widget(
        Paragraph::new(
            " LOCAL PREVIEW · No model connected · all calls, timings and counts are fixtures",
        )
        .style(style(t.tool_text, t.background)),
        areas[1],
    );
    let picker_height = if app.completion.is_open() && !app.themes_open && !app.approval_open {
        (app.completion.matches().len() + 2)
            .min(12)
            .min(areas[2].height as usize) as u16
    } else {
        0
    };
    let viewport = Rect::new(
        areas[2].x + 1,
        areas[2].y,
        areas[2].width.saturating_sub(2),
        areas[2].height.saturating_sub(picker_height),
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
        .style(style(t.foreground, t.background)),
        viewport,
    );
    if picker_height >= 3 {
        let picker_area = Rect::new(
            areas[2].x + 2,
            areas[2].bottom() - picker_height,
            areas[2].width.saturating_sub(4),
            picker_height,
        );
        let name_width = (picker_area.width.saturating_sub(2 + 11) as usize).min(26);
        let rows: Vec<_> = app
            .completion
            .matches()
            .iter()
            .map(|candidate| {
                let name = clip_completion_name(&candidate.name, name_width);
                let tag = match candidate.kind {
                    CompletionKind::Command => "[command]",
                    CompletionKind::Skill => "[skill]",
                };
                let safe_description: String = candidate
                    .description
                    .chars()
                    .map(|ch| if ch.is_control() { ' ' } else { ch })
                    .collect();
                let description: String = safe_description
                    .split_whitespace()
                    .collect::<Vec<_>>()
                    .join(" ")
                    .chars()
                    .take(40)
                    .collect();
                ListItem::new(format!(
                    "{name}{}{tag:<11}{description}",
                    " ".repeat(name_width.saturating_sub(name.width()))
                ))
            })
            .collect();
        let list = List::new(rows)
            .style(style(t.foreground, t.surface))
            .highlight_style(style(t.title, t.panel).add_modifier(Modifier::BOLD))
            .block(
                Block::default()
                    .borders(Borders::ALL)
                    .border_type(BorderType::Rounded)
                    .border_style(style(t.primary, t.surface)),
            );
        let mut selection = ListState::default().with_selected(Some(app.completion.selected()));
        frame.render_stateful_widget(list, picker_area, &mut selection);
        app.completion.record_view(
            Rect::new(
                picker_area.x + 1,
                picker_area.y + 1,
                picker_area.width.saturating_sub(2),
                picker_area.height.saturating_sub(2),
            ),
            selection.offset(),
        );
    } else {
        app.completion.hide();
    }
    frame.render_widget(
        Paragraph::new(format!(
            " {} · scroll {}/{}",
            app.notice, app.scroll, app.max_scroll
        ))
        .style(style(t.title, t.background)),
        areas[3],
    );
    let input_width = areas[4].width.saturating_sub(2).max(1) as usize;
    // Some clusters (e.g. repeated Hangul leading jamo) exceed an entire row.
    // Keep the logical text intact but draw one replacement cell until it fits.
    let graphemes: Vec<_> = app
        .composer
        .text()
        .grapheme_indices(true)
        .map(|(index, text)| {
            (
                index,
                text,
                if text.width() > input_width {
                    "�"
                } else {
                    text
                },
            )
        })
        .collect();
    let cursor_byte = app.composer.before_cursor().len();
    // Match the renderer's grapheme cell widths, not byte/scalar counts. Cursor
    // movement remains scalar-based; terminals cannot split a drawn grapheme.
    let cursor_cells: usize = graphemes
        .iter()
        .filter(|(index, _, _)| *index < cursor_byte)
        // A scalar boundary inside a grapheme has no separate terminal cell.
        // Snap the visual caret to its trailing edge without changing editing.
        .map(|(_, _, display)| display.width())
        .sum();
    let caret_width = graphemes
        .iter()
        .find(|(index, _, _)| *index >= cursor_byte)
        .map_or(1, |(_, _, display)| display.width().max(1));
    let desired_scroll = (cursor_cells + caret_width).saturating_sub(input_width);
    let mut input_scroll = 0;
    for (_, _, text) in &graphemes {
        if input_scroll >= desired_scroll {
            break;
        }
        input_scroll += text.width();
    }
    // Round scrolling UP to a whole glyph. Paragraph rounds a partial wide
    // glyph down; using that unadjusted offset displaced the caret and hid Z.
    let (before, selected, _) = app.composer.selection_parts();
    let selected_start = before.len();
    let selected_end = selected_start + selected.len();
    let input_line = if app.composer.text().is_empty() {
        Line::styled(
            "Message... (preview only; Enter adds a local mock turn)",
            style(t.title, t.surface),
        )
    } else {
        Line::from(
            graphemes
                .into_iter()
                .map(|(index, text, display)| {
                    if !selected.is_empty()
                        && index < selected_end
                        && index + text.len() > selected_start
                    {
                        Span::styled(
                            display,
                            style(t.title, t.panel)
                                .add_modifier(Modifier::BOLD | Modifier::UNDERLINED),
                        )
                    } else {
                        Span::raw(display)
                    }
                })
                .collect::<Vec<_>>(),
        )
    };
    let composer = Paragraph::new(input_line)
        .style(style(t.foreground, t.surface))
        .scroll((0, input_scroll as u16))
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title_bottom(" F4 themes · F6 approval · Ctrl+Q ")
                .border_style(style(t.thinking_border, t.surface)),
        );
    frame.render_widget(composer, areas[4]);
    if !app.approval_open && !app.themes_open {
        frame.set_cursor_position((
            areas[4].x + 1 + (cursor_cells - input_scroll) as u16,
            areas[4].y + 1,
        ));
    }
    let footer = crate::footer::FooterLayout::new(
        &app.footer_fields,
        areas[5].width.saturating_sub(1) as usize,
    );
    app.footer.update_visible(&footer.actions);
    let footer_style = style(t.footer_foreground, t.footer_background);
    let row = |row: &crate::footer::FooterRow| {
        let mut spans = vec![Span::raw(" ")];
        if row.overflow {
            spans.push(Span::raw("…"));
        }
        for (index, field) in row.fields.iter().enumerate() {
            if index > 0 {
                spans.push(Span::raw(row.separator));
            }
            let selected = field
                .key
                .action()
                .is_some_and(|action| app.footer.selected() == Some(action));
            spans.push(Span::styled(
                field.text.clone(),
                if selected {
                    footer_style.add_modifier(Modifier::REVERSED | Modifier::BOLD)
                } else {
                    footer_style
                },
            ));
        }
        Line::from(spans)
    };
    frame.render_widget(
        Paragraph::new(vec![row(&footer.status), row(&footer.permission)]).style(footer_style),
        areas[5],
    );
    if app.approval_open {
        approval(frame, app, area);
    }
    if app.themes_open {
        theme_picker(frame, app, area);
    }
    // Wide glyph continuation cells can be reset by Paragraph; never let them
    // inherit a terminal's potentially light default background.
    for cell in &mut frame.buffer_mut().content {
        if cell.bg == Color::Reset {
            cell.set_bg(t.background);
        }
    }
}

fn approval(frame: &mut Frame, app: &Demo, area: Rect) {
    let t = app.palette();
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
            style(t.tool_text, t.surface),
        ),
        Line::raw(""),
        Line::styled("Tool    write", style(t.foreground, t.surface)),
        Line::styled("Target  preview-notes.md", style(t.foreground, t.surface)),
        Line::styled(
            "Change  Save the tool-card layout notes",
            style(t.foreground, t.surface),
        ),
        Line::raw(""),
        Line::styled(
            "A real runtime would enforce policy and match the approval ID.",
            style(MUTED, t.surface),
        ),
        Line::styled(
            "This preview has no runtime and cannot grant real permissions.",
            style(MUTED, t.surface),
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
            style(
                if app.approval_allow {
                    MUTED
                } else {
                    t.tool_text
                },
                t.surface,
            ),
        ),
        Span::styled(
            if app.approval_allow {
                "  [> Allow mock <]"
            } else {
                "  [  Allow mock  ]"
            },
            style(
                if app.approval_allow {
                    t.foreground
                } else {
                    MUTED
                },
                t.surface,
            ),
        ),
    ]));
    lines.push(Line::styled(
        "Tab selects · Enter confirms · A allow · D deny · Esc closes",
        style(MUTED, t.surface),
    ));
    if width < 68 || height < 15 {
        lines = vec![
            Line::styled("No tool will execute.", style(t.tool_text, t.surface)),
            Line::styled("write preview-notes.md", style(t.foreground, t.surface)),
            Line::styled("No file will be written.", style(MUTED, t.surface)),
            Line::raw(""),
            Line::styled(
                if app.approval_allow {
                    "[  Deny mock  ]"
                } else {
                    "[> Deny mock <]"
                },
                style(t.tool_text, t.surface),
            ),
            Line::styled(
                if app.approval_allow {
                    "[> Allow mock <]"
                } else {
                    "[  Allow mock  ]"
                },
                style(t.foreground, t.surface),
            ),
            Line::styled("Tab select | Enter confirm", style(MUTED, t.surface)),
            Line::styled("A allow | D deny | Esc close", style(MUTED, t.surface)),
        ];
    }
    frame.render_widget(
        Paragraph::new(lines)
            .style(style(t.foreground, t.surface))
            .block(
                Block::default()
                    .title(" MOCK APPROVAL · write ")
                    .borders(Borders::ALL)
                    .border_type(BorderType::Rounded)
                    .border_style(style(t.tool_text, t.surface)),
            ),
        rect,
    );
}

fn theme_picker(frame: &mut Frame, app: &Demo, area: Rect) {
    let t = app.palette();
    let width = area.width.saturating_sub(4).min(64);
    let height = area.height.saturating_sub(2).min(24);
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    let inner = Rect::new(rect.x + 1, rect.y + 1, width - 2, height - 2);
    frame.render_widget(Clear, rect);
    frame.render_widget(
        Block::bordered()
            .title(" Themes · preview only ")
            .border_type(BorderType::Rounded)
            .style(style(t.foreground, t.panel))
            .border_style(style(t.thinking_border, t.panel)),
        rect,
    );
    let matches = app.filtered_themes();
    let cursor = app.theme_cursor.min(matches.len().saturating_sub(1));
    let visible = inner.height.saturating_sub(3) as usize;
    let start = cursor.saturating_sub(visible.saturating_sub(1));
    let mut lines = vec![
        Line::raw(format!(
            "Search: {}",
            tail(&app.theme_query, inner.width.saturating_sub(8) as usize)
        )),
        Line::raw(""),
    ];
    if matches.is_empty() {
        lines.push(Line::raw("No matching themes"));
    }
    for (row, &index) in matches.iter().enumerate().skip(start).take(visible) {
        let name = format!(
            "{} {}{}",
            if row == cursor { ">" } else { " " },
            app.themes[index].name,
            if index == app.theme_index {
                " (current)"
            } else {
                ""
            }
        );
        lines.push(Line::styled(
            name,
            style(
                if row == cursor {
                    t.tool_text
                } else {
                    t.foreground
                },
                t.panel,
            )
            .add_modifier(if row == cursor {
                Modifier::BOLD
            } else {
                Modifier::empty()
            }),
        ));
    }
    frame.render_widget(
        Paragraph::new(lines).style(style(t.foreground, t.panel)),
        inner,
    );
    frame.render_widget(
        Paragraph::new("↑↓ select · Enter apply · Esc").style(style(t.foreground, t.panel)),
        Rect::new(inner.x, inner.y + inner.height - 1, inner.width, 1),
    );
}
