use crate::{
    completion::CompletionKind,
    demo::{Demo, ToolState},
    rendered_width,
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
    text.graphemes(true)
        .take_while(|glyph| {
            used += glyph.width();
            used <= width
        })
        .collect()
}
fn clip_label(text: &str, width: usize) -> String {
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
        " ".repeat(width.saturating_sub(rendered_width(&value)))
    )
}
pub(crate) fn wrap(text: &str, width: usize) -> Vec<String> {
    let width = width.max(1);
    let mut result = Vec::new();
    for source in text.split('\n') {
        let mut line = String::new();
        let mut used = 0;
        for glyph in source.graphemes(true) {
            let cells = glyph.width();
            if used + cells > width && !line.is_empty() {
                result.push(std::mem::take(&mut line));
                used = 0;
            }
            if cells <= width {
                line.push_str(glyph);
                used += cells;
            } else {
                // Display fallback only: retained transcript text is untouched.
                line.push('�');
                used += 1;
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
    let summaries = [
        "Inspecting the tool-call layout",
        "Checking arguments and results",
        "Verifying the focused checks",
        "Asking before a write",
        "Keeping a failed call visible",
    ];
    let summary = summaries[index];
    let mut body = vec![
        "▾ Thinking · 0.4s · 16 tok · 40.9 tok/s".to_string(),
        summary.to_string(),
    ];
    if index == 1 {
        body.push("Matching the existing tool presentation".into());
    }
    assistant_card(
        t,
        lines,
        &format!("▾ Thinking: **{summary}** - mock-model"),
        &body,
        "",
        "♫ Speak [mock]",
        width,
    );
}

// Shared AssistantMessage / ThinkingBlock geometry. Runtime projection supplies
// its own content and settlement label; fixture timing/speech stays in the demo.
fn assistant_card(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    title: &str,
    reasoning: &[String],
    answer: &str,
    footer: &str,
    width: usize,
) {
    let narrow = width < 78;
    let left = if narrow { 0 } else { 2 };
    let right = if narrow { 0 } else { 8 };
    let outer_width = width.saturating_sub(left + right).max(12);
    let inner_width = outer_width.saturating_sub(6);
    let title = clip_label(title, outer_width.saturating_sub(5));
    lines.push(Line::from(vec![
        Span::styled(" ".repeat(left), style(t.foreground, t.background)),
        Span::styled("╭─ ", style(t.assistant_border, t.surface)),
        Span::styled(title.clone(), style(t.title, t.surface)),
        Span::styled(
            format!(
                "{}╮",
                "─".repeat(outer_width.saturating_sub(rendered_width(&title) + 4))
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
    if !reasoning.is_empty() {
        lines.push(outer_row(vec![Span::styled(
            format!("┏{}┓", "╍".repeat(inner_width.saturating_sub(2))),
            style(t.thinking_border, t.surface),
        )]));
        for text in reasoning {
            for row in wrap(text, inner_width.saturating_sub(4)) {
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
    }
    if !answer.is_empty() {
        for row in wrap(answer, inner_width) {
            lines.push(outer_row(vec![Span::styled(
                padded(&row, inner_width),
                style(t.foreground, t.surface),
            )]));
        }
    }
    lines.push(outer_row(vec![Span::styled(
        " ".repeat(inner_width),
        style(t.foreground, t.surface),
    )]));
    for row in wrap(footer, inner_width) {
        lines.push(outer_row(vec![Span::styled(
            format!(
                "{}{row}",
                " ".repeat(inner_width.saturating_sub(rendered_width(&row)))
            ),
            style(t.title, t.surface),
        )]));
    }
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

// Retain original wire text in the projection; terminal escapes and bidi
// controls are printable escapes ONLY at this presentation boundary.
fn display_safe(text: &str) -> String {
    let mut safe = String::new();
    for ch in text.chars() {
        match ch {
            '\n' => safe.push('\n'),
            '\t' => safe.push_str("    "),
            ch if crate::display_control(ch) => {
                safe.push_str(&format!("\\u{{{:x}}}", ch as u32));
            }
            ch => safe.push(ch),
        }
    }
    safe
}

/// Render identified generic streams and grouped host observations; no demo
/// content, completion inference, commands or persistence. `scroll` is a visual line
/// offset, clamped after wrapping so narrow resize never loses retained text.
pub fn draw_stream_projection(
    frame: &mut Frame,
    area: Rect,
    projection: &crate::stream::Projection,
    t: &crate::theme::Theme,
    scroll: usize,
) {
    use crate::stream::StreamState;
    let area = area.intersection(frame.area());
    frame.render_widget(
        Block::default().style(style(t.foreground, t.background)),
        area,
    );
    if area.width < 12 || area.height < 3 {
        frame.render_widget(
            Paragraph::new("Resize to view streams").style(style(t.foreground, t.background)),
            area,
        );
        return;
    }
    let header = if let Some(fault) = projection.fault() {
        format!("STREAM BLOCKED · {fault:?} · settlement unknown")
    } else {
        "STREAM PROJECTION · persistence unverified".to_owned()
    };
    frame.render_widget(
        Paragraph::new(header).style(style(
            if projection.fault().is_some() {
                t.error
            } else {
                t.title
            },
            t.background,
        )),
        Rect::new(area.x, area.y, area.width, 1),
    );
    let mut lines = Vec::new();
    for row in projection.rows() {
        let title = format!(
            "Assistant · {} · {}",
            display_safe(row.model.as_deref().unwrap_or("unknown model")),
            row.execution.as_str()
        );
        let reasoning = if row.reasoning.is_empty() {
            Vec::new()
        } else {
            vec!["▾ Thinking".to_owned(), display_safe(&row.reasoning)]
        };
        let footer = match &row.state {
            StreamState::Streaming => "Streaming · settlement unknown".to_owned(),
            StreamState::Incomplete => "Incomplete · settlement unknown".to_owned(),
            StreamState::Ended(terminal) => {
                let mut label = format!("Stream ended · {}", display_safe(&terminal.reason));
                if let Some(error) = &terminal.error {
                    label.push_str(&format!("\n{}", display_safe(error)));
                }
                label
            }
        };
        assistant_card(
            t,
            &mut lines,
            &title,
            &reasoning,
            &display_safe(&row.answer),
            &footer,
            area.width as usize,
        );
        if !row.tools.is_empty() {
            for line in wrap(
                "Tools grouped by execution (not transcript order)",
                area.width as usize,
            ) {
                lines.push(Line::styled(line, style(t.title, t.background)));
            }
            for tool in &row.tools {
                observed_tool(t, &mut lines, tool, area.width as usize);
            }
        }
    }
    if lines.is_empty() {
        lines.push(Line::raw("No identified streams"));
    }
    let height = area.height as usize - 1;
    let offset = scroll.min(lines.len().saturating_sub(height));
    frame.render_widget(
        Paragraph::new(
            lines
                .into_iter()
                .skip(offset)
                .take(height)
                .collect::<Vec<_>>(),
        )
        .style(style(t.foreground, t.background)),
        Rect::new(area.x, area.y + 1, area.width, area.height - 1),
    );
}

// Shared unbordered ToolMessage body geometry. No fixture execution metadata.
fn tool_body(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    text: &str,
    color: Color,
    width: usize,
) {
    for row in wrap(text, width.saturating_sub(8)) {
        lines.push(Line::styled(
            format!("     {row}"),
            style(color, t.tool_background),
        ));
    }
}

fn observed_tool(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    tool: &crate::stream::ToolObservation,
    width: usize,
) {
    use crate::stream::ToolObservationState;
    // Wrap instead of truncating identity/status on narrow terminals. These
    // rows have no collapse/approval controls until a real view controller owns them.
    // Metadata newlines are visible escapes; only result bodies keep multiline
    // formatting. A supplied name cannot inject a new status-looking line.
    let metadata = |text: &str| display_safe(text).replace('\n', "\\u{a}");
    for row in wrap(
        &format!("Tool: {}", metadata(&tool.name)),
        width.saturating_sub(3),
    ) {
        lines.push(Line::styled(
            format!("   {row}"),
            style(t.tool_text, t.tool_background).add_modifier(Modifier::BOLD),
        ));
    }
    let status = match &tool.state {
        ToolObservationState::Requested => "Requested · dispatch unknown",
        ToolObservationState::Returned { ok: true, .. } => "Returned · host ok=true",
        ToolObservationState::Returned { ok: false, .. } => "Returned · host ok=false",
        ToolObservationState::Incomplete => "Incomplete · return unavailable",
    };
    tool_body(t, lines, status, t.title, width);
    tool_body(
        t,
        lines,
        &format!("Observation {}", tool.id.as_str()),
        MUTED,
        width,
    );
    let coordinates = format!(
        "Round {} · slot {} · provider {}",
        tool.round,
        tool.slot,
        metadata(&tool.provider_call_id)
    );
    tool_body(t, lines, &coordinates, MUTED, width);
    if let ToolObservationState::Returned { result, .. } = &tool.state {
        tool_body(t, lines, "Host result (not task completion)", MUTED, width);
        tool_body(t, lines, &display_safe(result), MUTED, width);
    }
    lines.push(Line::raw(""));
}

fn native_tool(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    tool: &crate::native_activity::Tool,
    width: usize,
) {
    use crate::native_activity::ResultState;
    let metadata = |text: &str| display_safe(text).replace('\n', "\\u{a}");
    tool_body(
        t,
        lines,
        &format!(
            "Claude · {}",
            metadata(tool.name.as_deref().unwrap_or(&tool.id))
        ),
        t.tool_text,
        width,
    );
    let (status, color) = match &tool.result {
        ResultState::Pending => ("Request observed · result pending", t.title),
        ResultState::Incomplete => ("Incomplete · result unavailable", t.error),
        ResultState::Returned {
            is_error: Some(true),
            ..
        } => ("Result observed · reported failure", t.error),
        ResultState::Returned {
            is_error: Some(false),
            ..
        } => ("Result observed · is_error=false", t.title),
        ResultState::Returned { is_error: None, .. } => {
            ("Result observed · outcome unknown", t.title)
        }
    };
    tool_body(t, lines, status, color, width);
    tool_body(
        t,
        lines,
        &format!("Native tool {}", metadata(&tool.id)),
        MUTED,
        width,
    );
    if !tool.request_seen {
        tool_body(t, lines, "Request unavailable", MUTED, width);
    }
    if let Some(input) = &tool.input {
        tool_body(
            t,
            lines,
            if tool.input_complete {
                "Arguments"
            } else {
                "Arguments · still streaming"
            },
            MUTED,
            width,
        );
        tool_body(t, lines, &display_safe(input), t.tool_text, width);
    }
    if let ResultState::Returned { text, .. } = &tool.result {
        tool_body(
            t,
            lines,
            "Native result (not task completion)",
            MUTED,
            width,
        );
        tool_body(t, lines, &display_safe(text), color, width);
    }
    lines.push(Line::raw(""));
}

fn transcript(app: &Demo, width: usize) -> (Vec<Line<'static>>, (usize, usize)) {
    let t = app.palette();
    if let Some(chat) = &app.live {
        let mut lines = Vec::new();
        if let Some(history) = &chat.history {
            tool_body(
                t,
                &mut lines,
                &format!("Saved conversation · {}", display_safe(&history.session)),
                t.title,
                width,
            );
            for message in &history.messages {
                let label = format!("Saved {}", display_safe(&message.role));
                if message.role == "assistant" {
                    assistant_card(
                        t,
                        &mut lines,
                        &label,
                        &[],
                        &display_safe(&message.text),
                        "Saved transcript · outcome not inferred",
                        width,
                    );
                } else {
                    tool_body(
                        t,
                        &mut lines,
                        &format!("{label}: {}", display_safe(&message.text)),
                        t.foreground,
                        width,
                    );
                }
                if !message.details.is_empty() {
                    tool_body(
                        t,
                        &mut lines,
                        &format!("Saved details: {}", display_safe(&message.details)),
                        t.tool_text,
                        width,
                    );
                }
                lines.push(Line::raw(""));
            }
        }
        for row in &chat.rows {
            for text in wrap(
                &format!("You: {}", display_safe(&row.prompt)),
                width.saturating_sub(4),
            ) {
                lines.push(Line::styled(
                    format!("  {text}"),
                    style(t.foreground, t.background),
                ));
            }
            lines.push(Line::raw(""));
            let state = match row.state {
                crate::live_chat::ReplyState::Sending => "Sending · not yet completed",
                crate::live_chat::ReplyState::Streaming => "Streaming",
                crate::live_chat::ReplyState::Complete => "Completed · stop",
                crate::live_chat::ReplyState::Cancelling => "Stopping · awaiting runtime",
                crate::live_chat::ReplyState::Cancelled => "Cancelled · partial output retained",
                crate::live_chat::ReplyState::ApprovalStopped => "Stopped · approval",
                crate::live_chat::ReplyState::Incomplete => "Incomplete · runtime closed or failed",
                crate::live_chat::ReplyState::Refused => "Not started · source refused",
                crate::live_chat::ReplyState::Failed => "Failed · partial output retained",
                crate::live_chat::ReplyState::Reconciled => {
                    "Previously accepted · verified in provider history · not resent"
                }
            };
            if row.state == crate::live_chat::ReplyState::Reconciled {
                tool_body(t, &mut lines, state, t.tool_text, width);
                lines.push(Line::raw(""));
            }
            for item in &row.items {
                match *item {
                    crate::live_chat::Item::Tool(index) => {
                        native_tool(t, &mut lines, &row.tools[index], width)
                    }
                    crate::live_chat::Item::Card(index) => {
                        let card = &row.cards[index];
                        assistant_card(
                            t,
                            &mut lines,
                            &format!("Assistant · {}", display_safe(&chat.model)),
                            &if card.reasoning.is_empty() {
                                Vec::new()
                            } else {
                                vec!["▾ Thinking".to_owned(), display_safe(&card.reasoning)]
                            },
                            &display_safe(&card.answer),
                            state,
                            width,
                        );
                    }
                }
            }
            if let Some(reason) = &row.stop_reason {
                tool_body(
                    t,
                    &mut lines,
                    &format!(
                        "{}: {}",
                        if row.state == crate::live_chat::ReplyState::Refused {
                            "Input not started"
                        } else if row.state == crate::live_chat::ReplyState::Cancelled {
                            "Turn cancelled"
                        } else {
                            "Approval stop"
                        },
                        display_safe(reason)
                    ),
                    t.error,
                    width,
                );
                lines.push(Line::raw(""));
            }
        }
        return (lines, (0, 0));
    }
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
                tool_body(t, &mut lines, text, color, width);
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
    if (app.viewport_too_small || app.has_live_approval() || app.stop_open())
        && let Some(chat) = app.live.as_mut()
        && let Some(pending) = chat.questions.current_mut()
    {
        pending.invalidate_hits();
    }
    if app.viewport_too_small {
        if let Some(chat) = app.live.as_mut() {
            chat.controls.hide();
            chat.queue.hide();
            chat.engine.invalidate();
            chat.commands.hide();
            chat.themes.hide();
            chat.settings.hide();
            chat.backend_controls.hide();
            chat.browser.hide();
        }
        app.footer.update_visible(&[]);
        app.completion.hide();
        frame.render_widget(Paragraph::new(if app.live.is_some() { "LIVE RUNTIME\nResize to 36 x 14.\nCtrl+Q exits cleanly.\nInput paused until resized." } else if app.themes_open {
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
        Paragraph::new(if let Some(chat) = &app.live {
            format!(
                "LiteTUI — Rust LIVE · {} · {}",
                display_safe(&chat.backend),
                display_safe(&chat.model)
            )
        } else {
            "LiteTUI — Rust preview · mock-model · tools:5 · think:high".into()
        })
        .alignment(Alignment::Center)
        .style(style(t.title, t.surface)),
        areas[0],
    );
    frame.render_widget(
        Paragraph::new(if let Some(chat) = &app.live {
            format!(" LIVE · {} · F4 themes · Ctrl+Q quits", chat.theme_status)
        } else {
            " LOCAL PREVIEW · No model connected · all calls, timings and counts are fixtures"
                .into()
        })
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
                let name = clip_label(&crate::live::terminal_text(&candidate.name), name_width);
                let tag = match candidate.kind {
                    CompletionKind::Command => "[command]",
                    CompletionKind::Skill => "[skill]",
                };
                let safe_description: String = crate::live::terminal_text(&candidate.description)
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
            display_safe(&app.notice),
            app.scroll,
            app.max_scroll
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
            if app.live.is_some() {
                "Message… (Enter sends to the real model; Ctrl+Q quits)"
            } else {
                "Message... (preview only; Enter adds a local mock turn)"
            },
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
                .title_bottom(if app.live.is_some() {
                    " F3 models · F7 held · F8 conversations · F4 themes · Esc stops · Ctrl+Q "
                } else {
                    " F4 themes · F6 approval · Ctrl+Q "
                })
                .border_style(style(t.thinking_border, t.surface)),
        );
    frame.render_widget(composer, areas[4]);
    if !app.approval_open && !app.has_live_input() && !app.themes_open {
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
    if app.has_live_approval() {
        live_approval(frame, app, area);
    } else if app.has_live_question() {
        live_question(frame, app, area);
    }
    if app.themes_open {
        theme_picker(frame, app, area);
    }
    if app.has_live_queue()
        && !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.live.as_ref().is_some_and(|chat| chat.cancelling())
    {
        app.live
            .as_mut()
            .expect("held input")
            .queue
            .draw(frame, area, &t);
    } else if let Some(chat) = &mut app.live {
        chat.queue.hide();
    }
    if app.has_live_engine() {
        app.live
            .as_mut()
            .expect("engine")
            .engine
            .draw(frame, area, &t);
    }
    if app.has_live_commands() {
        app.live
            .as_mut()
            .expect("commands")
            .commands
            .draw(frame, area, &t);
    }
    if app.has_live_themes()
        && !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.live.as_ref().is_some_and(|chat| chat.cancelling())
    {
        app.live
            .as_mut()
            .expect("theme view")
            .themes
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.themes.hide();
    }
    if app.has_live_settings() {
        app.live
            .as_mut()
            .expect("settings view")
            .settings
            .draw(frame, area, &t);
    }
    if app.has_live_backend() {
        app.live
            .as_mut()
            .expect("backend selector")
            .backend_controls
            .draw(frame, area, &t);
    }
    if app.has_live_browser() {
        app.live
            .as_mut()
            .expect("conversation browser")
            .browser
            .draw(frame, area, &t);
    }
    if app.has_live_controls() {
        app.live
            .as_mut()
            .expect("live controls")
            .controls
            .draw(frame, area, &t);
    }
    if app.has_live_input() && app.live.as_ref().is_some_and(|chat| chat.cancelling()) {
        stopping_dialog(frame, app, area);
    }
    if app.stop_open() {
        stop_dialog(frame, app, area);
    }
    // Wide glyph continuation cells can be reset by Paragraph; never let them
    // inherit a terminal's potentially light default background.
    for cell in &mut frame.buffer_mut().content {
        if cell.bg == Color::Reset {
            cell.set_bg(t.background);
        }
    }
}

fn stopping_dialog(frame: &mut Frame, app: &Demo, area: Rect) {
    let t = app.palette();
    let width = area.width.saturating_sub(2).min(54);
    let height = 8;
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    frame.render_widget(Paragraph::new("Waiting for runtime settlement.\nPartial output is retained.\nInput decisions are paused.\nCtrl+Esc requests forced stop.\nCtrl+Q closes the session.")
        .style(style(t.foreground,t.surface))
        .wrap(ratatui::widgets::Wrap { trim:false })
        .block(Block::default().title(" Stopping turn ").borders(Borders::ALL)),rect);
}

fn stop_dialog(frame: &mut Frame, app: &Demo, area: Rect) {
    let t = app.palette();
    let chat = app.live.as_ref().expect("stop dialog");
    let width = area.width.saturating_sub(2).min(64);
    let height = area.height.saturating_sub(2).min(12);
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    let title = if chat.cancelling() {
        " Force stop? "
    } else {
        " Stop this turn? "
    };
    let text = vec![
        Line::raw("Partial output stays visible."),
        Line::raw("Tool effects are not undone."),
        Line::raw(if chat.cancelling() {
            "Second stop forces interruption."
        } else {
            "Wait for runtime settlement."
        }),
        Line::raw(""),
        Line::raw(if chat.stop_selected {
            "[  Keep going  ]"
        } else {
            "[> Keep going <]"
        }),
        Line::raw(if chat.stop_selected {
            "[> Stop turn <]"
        } else {
            "[  Stop turn  ]"
        }),
        Line::raw("Tab selects | Enter confirms"),
        Line::raw("Esc closes | Ctrl+Q quits"),
    ];
    frame.render_widget(
        Paragraph::new(text)
            .style(style(t.foreground, t.surface))
            .wrap(ratatui::widgets::Wrap { trim: false })
            .block(Block::default().title(title).borders(Borders::ALL)),
        rect,
    );
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

fn live_approval(frame: &mut Frame, app: &mut Demo, area: Rect) {
    let t = app.palette().clone();
    let chat = app.live.as_mut().expect("live approval");
    let count = chat.approvals.len();
    let current = chat.approvals.current_mut().expect("visible approval");
    let width = area.width.saturating_sub(4).min(78);
    let lines = wrap(
        &display_safe(&current.request.display_text()),
        width.saturating_sub(4) as usize,
    );
    let height = (lines.len() + 8).min(usize::from(area.height) * 85 / 100) as u16;
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    frame.render_widget(Clear, rect);
    let block = Block::default()
        .borders(Borders::ALL)
        .border_type(BorderType::Rounded)
        .border_style(style(t.tool_text, t.surface))
        .title(format!(" Tool approval ({count} pending) "));
    let inner = block.inner(rect);
    frame.render_widget(block.style(style(t.foreground, t.surface)), rect);
    let parts = Layout::vertical([Constraint::Min(1), Constraint::Length(6)]).split(inner);
    current.scroll = current
        .scroll
        .min(lines.len().saturating_sub(parts[0].height as usize));
    frame.render_widget(
        Paragraph::new(
            lines
                .into_iter()
                .skip(current.scroll)
                .map(|line| Line::raw(format!(" {line}")))
                .collect::<Vec<_>>(),
        )
        .style(style(t.foreground, t.surface)),
        parts[0],
    );
    let budget = current
        .request
        .timeout_s
        .map_or("unbounded".into(), |n| format!("{n}s"));
    let action: String = if current.waiting {
        "Decision sent; waiting for source closure.".into()
    } else if current.allow {
        "  [ Deny ]   [> Allow once <]".into()
    } else {
        "  [> Deny <]   [ Allow once ]".into()
    };
    let controls = vec![
        Line::raw(format!(
            " Source wait budget: {budget}; turn deadline still applies."
        )),
        Line::raw(" No execution receipt. Always-allow is not offered yet."),
        Line::styled(
            action,
            style(t.tool_text, t.surface).add_modifier(Modifier::BOLD),
        ),
        Line::raw(" Tab selects; Enter decides; Esc denies; Ctrl+Q quits."),
        Line::raw(" Up/Down/PgUp/PgDn scroll full request; no auto-answer."),
    ];
    frame.render_widget(
        Paragraph::new(controls).style(style(t.foreground, t.surface)),
        parts[1],
    );
}

fn live_question(frame: &mut Frame, app: &mut Demo, area: Rect) {
    use crate::questions::{Hit, HitTarget};
    let t = app.palette().clone();
    let pending = app
        .live
        .as_mut()
        .expect("live question")
        .questions
        .current_mut()
        .expect("visible question");
    let width = area.width.saturating_sub(4).min(88);
    let height = area.height.saturating_sub(2).min(32);
    let rect = Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    );
    let block = Block::bordered()
        .border_type(BorderType::Rounded)
        .title(" Question ")
        .style(style(t.foreground, t.surface))
        .border_style(style(t.tool_text, t.surface));
    let inner = block.inner(rect);
    frame.render_widget(Clear, rect);
    frame.render_widget(block, rect);
    let parts = Layout::vertical([
        Constraint::Length(2),
        Constraint::Min(1),
        Constraint::Length(3),
    ])
    .split(inner);
    let mut hits = Vec::new();
    let mut tabs = Vec::new();
    let mut x = parts[0].x;
    let tab_count = (parts[0].width as usize / 5).max(1);
    let first_tab = pending.active.saturating_sub(tab_count - 1);
    for i in first_tab..pending.request.questions.len() {
        let text = format!(
            "[{}{}] ",
            i + 1,
            if pending.answered(i) { "*" } else { " " }
        );
        let len = text.len() as u16;
        if x.saturating_add(len) > parts[0].right() {
            break;
        }
        tabs.push(Span::styled(
            text,
            style(
                if i == pending.active {
                    t.tool_text
                } else {
                    MUTED
                },
                t.surface,
            )
            .add_modifier(if i == pending.active {
                Modifier::BOLD
            } else {
                Modifier::empty()
            }),
        ));
        hits.push(Hit {
            rect: Rect::new(x, parts[0].y, len, 1),
            target: HitTarget::Question(i),
        });
        x += len;
    }
    frame.render_widget(
        Paragraph::new(vec![
            Line::from(tabs),
            Line::raw(if pending.focus == 0 {
                "> Left/Right: change question"
            } else {
                "  Tab/Up/Down: move focus"
            }),
        ]),
        parts[0],
    );
    let question = pending.question();
    let count = question.options.len();
    let mut body: Vec<(String, Option<usize>)> = Vec::new();
    let body_width = parts[1].width.saturating_sub(2) as usize;
    let mut add = |text: String, focus: Option<usize>| {
        let start = body.len();
        for line in wrap(&text, body_width) {
            body.push((line, focus));
        }
        start
    };
    add(
        format!(
            "{}: {}",
            display_safe(&question.label),
            display_safe(&question.text)
        ),
        None,
    );
    add(
        if question.multi_select {
            "Select any options, or enter a note."
        } else {
            "Select one option, or enter a note."
        }
        .into(),
        None,
    );
    for (i, option) in question.options.iter().enumerate() {
        add(
            format!(
                "{} [{}] {}{}",
                if pending.focus == i + 1 { ">" } else { " " },
                if pending.answers[pending.active].contains(&i) {
                    "x"
                } else {
                    " "
                },
                display_safe(&option.title),
                if option.description.is_empty() {
                    String::new()
                } else {
                    format!(" — {}", display_safe(&option.description))
                }
            ),
            Some(i + 1),
        );
    }
    let note = &pending.notes[pending.active];
    let before = note.before_cursor();
    let after = &note.text()[before.len()..];
    let note_label = format!(
        "{} {}: ",
        if pending.note_focused() { ">" } else { " " },
        if question.secret {
            "Secret note (hidden)"
        } else {
            "Type something"
        }
    );
    let before_display = if question.secret {
        "*".repeat(before.chars().count())
    } else {
        display_safe(before)
    };
    let after_display = if question.secret {
        "*".repeat(after.chars().count())
    } else {
        display_safe(after)
    };
    let prefix = format!(
        "{note_label}{before_display}{}",
        if pending.note_focused() { "│" } else { "" }
    );
    let note_start = add(format!("{prefix}{after_display}"), Some(count + 1));
    let caret_row = note_start + wrap(&prefix, body_width).len() - 1;
    for (focus, label) in [
        (count + 2, "Clear this answer"),
        (
            count + 3,
            if pending.active + 1 < pending.request.questions.len() {
                "Next question"
            } else {
                "Submit answers"
            },
        ),
        (count + 4, "Chat about this"),
        (count + 5, "Cancel without answers"),
    ] {
        add(
            format!(
                "{} [ {} ]",
                if pending.focus == focus { ">" } else { " " },
                label
            ),
            Some(focus),
        );
    }
    let visible = parts[1].height as usize;
    if pending.follow_focus {
        let focus_row = if pending.note_focused() {
            Some(caret_row)
        } else {
            body.iter()
                .position(|(_, focus)| *focus == Some(pending.focus))
        };
        if let Some(index) = focus_row {
            if index < pending.scroll {
                pending.scroll = index;
            } else if index >= pending.scroll + visible {
                pending.scroll = index.saturating_sub(visible.saturating_sub(1));
            }
        } else if pending.focus == 0 {
            pending.scroll = 0;
        }
        pending.follow_focus = false;
    }
    pending.scroll = pending.scroll.min(body.len().saturating_sub(visible));
    let mut lines = Vec::new();
    for (offset, (line, focus)) in body.iter().skip(pending.scroll).take(visible).enumerate() {
        if let Some(focus) = focus {
            hits.push(Hit {
                rect: Rect::new(parts[1].x, parts[1].y + offset as u16, parts[1].width, 1),
                target: HitTarget::Control(*focus),
            });
        }
        lines.push(Line::styled(
            format!(" {line}"),
            style(
                if *focus == Some(pending.focus) {
                    t.tool_text
                } else {
                    t.foreground
                },
                t.surface,
            ),
        ));
    }
    pending.set_hits(hits);
    frame.render_widget(Paragraph::new(lines), parts[1]);
    let footer = if pending.waiting {
        "Answer sent; waiting for source response."
    } else if !pending.notice.is_empty() {
        pending.notice.as_str()
    } else {
        "Enter/Space selects · Esc cancels · Ctrl+Q quits"
    };
    frame.render_widget(
        Paragraph::new(vec![
            Line::raw(clip_label(footer, parts[2].width as usize)),
            Line::raw("PgUp/PgDn scroll · * answered · notes may be partial"),
            Line::raw("Submit requires every answer; Chat allows discussion."),
        ])
        .style(style(t.foreground, t.surface)),
        parts[2],
    );
}
