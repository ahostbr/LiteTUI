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
    let (left, outer_width) = card_geometry(width);
    let inner_width = outer_width.saturating_sub(6);
    lines.push(card_top(t, title, left, outer_width));
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
    if let [folded] = reasoning {
        lines.push(outer_row(vec![Span::styled(
            padded(&clip_label(folded, inner_width), inner_width),
            style(t.thinking_text, t.surface),
        )]));
    } else if !reasoning.is_empty() {
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
    lines.push(card_bottom(t, left, outer_width));
    lines.push(Line::raw(""));
}
/// Left margin and outer width of an assistant card at a transcript width.
fn card_geometry(width: usize) -> (usize, usize) {
    let narrow = width < 78;
    let left = if narrow { 0 } else { 2 };
    let right = if narrow { 0 } else { 8 };
    (left, width.saturating_sub(left + right).max(12))
}
fn card_top(
    t: &crate::theme::Theme,
    title: &str,
    left: usize,
    outer_width: usize,
) -> Line<'static> {
    let title = clip_label(title, outer_width.saturating_sub(5));
    Line::from(vec![
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
    ])
}
fn card_bottom(t: &crate::theme::Theme, left: usize, outer_width: usize) -> Line<'static> {
    Line::styled(
        format!(
            "{}╰{}╯",
            " ".repeat(left),
            "─".repeat(outer_width.saturating_sub(2))
        ),
        style(t.assistant_border, t.surface),
    )
}
/// A folded assistant card is its border and title row only (source: collapsed `AssistantMessage`).
fn folded_card(t: &crate::theme::Theme, lines: &mut Vec<Line<'static>>, title: &str, width: usize) {
    let (left, outer_width) = card_geometry(width);
    lines.push(card_top(t, title, left, outer_width));
    lines.push(card_bottom(t, left, outer_width));
    lines.push(Line::raw(""));
}

// Retain original wire text in the projection; terminal escapes and bidi
// controls are printable escapes ONLY at this presentation boundary.
pub(crate) fn display_safe(text: &str) -> String {
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
    tool_body(
        t,
        lines,
        &format!("{status} · {}", tool.timing.label()),
        t.title,
        width,
    );
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

fn work_source(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    source: &crate::live_chat::WorkSource,
    width: usize,
) {
    tool_body(
        t,
        lines,
        &format!(
            "Source: {} · requested {} · effective {}",
            display_safe(&source.source),
            source.requested.as_str(),
            source.effective.as_str()
        ),
        t.tool_text,
        width,
    );
}

fn native_tool(
    t: &crate::theme::Theme,
    lines: &mut Vec<Line<'static>>,
    row: &crate::live_chat::Row,
    index: usize,
    width: usize,
    compact: bool,
) {
    use crate::native_activity::ResultState;
    let tool = &row.tools[index];
    let expanded = row.tool_expanded(index);
    let selected = row.tool_view.selected == Some(index);
    let metadata = |text: &str| display_safe(text).replace('\n', "\\u{a}");
    let header = format!(
        "{} {} · {}",
        if expanded { "▾" } else { "▸" },
        row.tool_provider(),
        metadata(tool.name.as_deref().unwrap_or(&tool.id))
    );
    let header_color = if selected { t.title } else { t.tool_text };
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
    if compact && !expanded {
        let marker = if selected { "›" } else { " " };
        let label = clip_label(
            &format!("{marker} {header} · {} · {status}", tool.timing.label()),
            width.saturating_sub(2),
        );
        lines.push(Line::styled(
            format!("  {label}"),
            style(color, t.tool_background),
        ));
        return;
    }
    for fragment in wrap(&header, width.saturating_sub(8)) {
        lines.push(Line::styled(
            format!("   {} {fragment}", if selected { "›" } else { " " }),
            style(header_color, t.tool_background).add_modifier(Modifier::BOLD),
        ));
    }
    tool_body(
        t,
        lines,
        &format!("{status} · {}", tool.timing.label()),
        color,
        width,
    );
    if !expanded {
        lines.push(Line::raw(""));
        return;
    }
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

type Headers = Vec<crate::compact_display::Header>;
/// One foldable transcript element (a prompt or an assistant card) and where its lines end.
struct FoldSpan {
    row: usize,
    target: crate::compact_display::Target,
    end: usize,
}
struct Transcript {
    lines: Vec<Line<'static>>,
    selected: (usize, usize),
    headers: Headers,
    folds: Vec<FoldSpan>,
    #[cfg(test)]
    rendered_rows: usize,
}
/// The settled line below a turn's last card (source `format_turn_stop_line`).
fn stop_line(
    row: &crate::live_chat::Row,
    presentation: &crate::presentation::Presentation,
) -> Option<String> {
    use crate::{live_chat::ReplyState, presentation::Ending};
    let ending = match row.state {
        ReplyState::Complete => Ending::Completed,
        ReplyState::Cancelled | ReplyState::ApprovalStopped | ReplyState::Failed => Ending::Stopped,
        _ => return None,
    };
    row.timing.line(ending, presentation)
}
fn transcript(app: &Demo, width: usize) -> Transcript {
    use crate::compact_display::{Header, HeaderHit, Target};
    let mut headers: Headers = Vec::new();
    let mut folds = Vec::new();
    let t = app.palette();
    let presentation = app.presentation();
    if let Some(chat) = &app.live {
        let mut lines = Vec::new();
        let mut selected = (0, 0);
        // Choose the bounded absolute row window before any body is escaped or wrapped.
        let visible = chat.visible_row_range();
        #[cfg(test)]
        let mut rendered_rows = 0;
        if chat.has_older_live_page() || chat.has_newer_live_page() {
            let location = if visible.is_empty() && chat.show_live_history() {
                "Saved startup history".to_owned()
            } else {
                format!(
                    "Live rows {}–{} of {}",
                    visible.start + 1,
                    visible.end,
                    chat.rows.len()
                )
            };
            tool_body(
                t,
                &mut lines,
                &format!(
                    "{location} · {}{}PgUp/PgDn at page edges · Ctrl+Home/End first/latest",
                    if chat.has_older_live_page() {
                        "older available · "
                    } else {
                        ""
                    },
                    if chat.has_newer_live_page() {
                        "newer available · "
                    } else {
                        ""
                    }
                ),
                t.thinking_text,
                width,
            );
            lines.push(Line::raw(""));
        }
        if let Some(descriptor) = chat.history.as_ref().and_then(|h| h.descriptor.as_ref()) {
            tool_body(
                t,
                &mut lines,
                &format!(
                    "Saved context: {} messages · Saved history in Ctrl+\\ (separate from live activity)",
                    descriptor.counts.messages
                ),
                t.thinking_text,
                width,
            );
        }
        if let Some(history) = &chat.history
            && history.descriptor.is_none()
            && chat.show_live_history()
            && chat.screen_cleared_history.as_deref() != Some(history.session.as_str())
        {
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
        for row_index in visible.clone() {
            let row = &chat.rows[row_index];
            #[cfg(test)]
            {
                rendered_rows += 1;
            }
            if row_index == visible.start && row_index < chat.prior_context_rows {
                lines.push(Line::styled(
                    "  Retained scrollback from before compaction · F6 shows current context",
                    style(t.tool_text, t.background),
                ));
            }
            if row_index == chat.prior_context_rows && chat.prior_context_rows > 0 {
                lines.push(Line::styled(
                    "  Source work since compaction",
                    style(t.tool_text, t.background),
                ));
            }
            if let Some(committed) = &row.committed {
                tool_body(
                    t,
                    &mut lines,
                    &format!("Source continuation: {}", display_safe(&committed.status)),
                    t.foreground,
                    width,
                );
                tool_body(
                    t,
                    &mut lines,
                    "Already committed history references:",
                    t.tool_text,
                    width,
                );
                for receipt in &committed.history.receipt_ids {
                    tool_body(
                        t,
                        &mut lines,
                        &format!("Receipt: {}", display_safe(receipt)),
                        t.tool_text,
                        width,
                    );
                }
            } else {
                let label = if row.source_owned { "Source" } else { "You" };
                headers.push(Header {
                    line: lines.len(),
                    span: None,
                    hit: HeaderHit {
                        row: row_index,
                        target: Target::Prompt,
                    },
                });
                if row.prompt_fold.folded {
                    lines.push(Line::styled(
                        clip_label(
                            &format!(
                                "▸ {label} · {}",
                                crate::compact_display::summary(&row.prompt, 80)
                            ),
                            width.saturating_sub(2),
                        ),
                        style(t.foreground, t.background),
                    ));
                } else {
                    for (n, text) in wrap(
                        &format!("{label}: {}", display_safe(&row.prompt)),
                        width.saturating_sub(4),
                    )
                    .into_iter()
                    .enumerate()
                    {
                        lines.push(Line::styled(
                            format!("{}{text}", if n == 0 { "▾ " } else { "  " }),
                            style(t.foreground, t.background),
                        ));
                    }
                }
                if let Some(name) = row.image_name.as_ref().filter(|_| !row.prompt_fold.folded) {
                    headers.push(Header {
                        line: lines.len(),
                        span: None,
                        hit: HeaderHit {
                            row: row_index,
                            target: Target::Image,
                        },
                    });
                    tool_body(
                        t,
                        &mut lines,
                        &format!(
                            "[Image: {}] · click opens Saved History",
                            display_safe(name)
                        ),
                        t.tool_text,
                        width,
                    );
                }
                folds.push(FoldSpan {
                    row: row_index,
                    target: Target::Prompt,
                    end: lines.len(),
                });
            }
            if let Some(source) = &row.source {
                work_source(t, &mut lines, source, width);
            }
            lines.push(Line::raw(""));
            let state = match row.state {
                crate::live_chat::ReplyState::Sending => "Sending · not yet completed",
                crate::live_chat::ReplyState::Streaming => "Streaming",
                crate::live_chat::ReplyState::AwaitingRetirement => {
                    "Stream ended · awaiting worker retirement"
                }
                crate::live_chat::ReplyState::Maintenance => "Compacting · source work",
                crate::live_chat::ReplyState::MaintenanceSettled => {
                    "Compaction worker settled · see source status"
                }
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
            let settled = stop_line(row, &presentation);
            let state_text = match &settled {
                Some(line) => format!("{state}\n{line}"),
                None => state.to_owned(),
            };
            let last_card = row.items.iter().rev().find_map(|item| match item {
                crate::live_chat::Item::Card(index) => Some(*index),
                _ => None,
            });
            if row.cards.is_empty() {
                tool_body(t, &mut lines, &state_text, t.tool_text, width);
                lines.push(Line::raw(""));
            }
            for item in &row.items {
                match *item {
                    crate::live_chat::Item::WorkInput(index) => {
                        let input = &row.inputs[index];
                        work_source(t, &mut lines, &input.source, width);
                        tool_body(
                            t,
                            &mut lines,
                            &display_safe(&input.text),
                            t.foreground,
                            width,
                        );
                        lines.push(Line::raw(""));
                    }
                    crate::live_chat::Item::Tool(index) => {
                        let start = lines.len();
                        native_tool(
                            t,
                            &mut lines,
                            row,
                            index,
                            width,
                            app.compact_display.active(),
                        );
                        if row.tool_view.selected == Some(index) {
                            selected = (start, lines.len());
                        }
                    }
                    crate::live_chat::Item::Card(index) => {
                        let card = &row.cards[index];
                        let card_hit = HeaderHit {
                            row: row_index,
                            target: Target::Card(index),
                        };
                        if card.fold.folded {
                            // Speak is offered once the turn has settled with an answer; it
                            // only opens the reviewed saved history.
                            let speak = row.state == crate::live_chat::ReplyState::Complete
                                && !card.answer.is_empty();
                            let preview = crate::compact_display::summary(&card.answer, 80);
                            let title = format!(
                                "▸ {}Assistant · {}{}",
                                if speak { "♫ Speak · " } else { "" },
                                display_safe(&chat.model),
                                if preview.is_empty() {
                                    String::new()
                                } else {
                                    format!(" · {preview}")
                                }
                            );
                            let (left, outer) = card_geometry(width);
                            if speak {
                                let from = left + 5;
                                let to = (from + rendered_width("♫ Speak"))
                                    .min(left + 3 + outer.saturating_sub(5));
                                headers.push(Header {
                                    line: lines.len(),
                                    span: Some((from as u16, to as u16)),
                                    hit: HeaderHit {
                                        row: row_index,
                                        target: Target::Speak,
                                    },
                                });
                            }
                            headers.push(Header {
                                line: lines.len(),
                                span: None,
                                hit: card_hit,
                            });
                            folded_card(t, &mut lines, &title, width);
                            folds.push(FoldSpan {
                                row: row_index,
                                target: Target::Card(index),
                                end: lines.len(),
                            });
                            continue;
                        }
                        let expanded = card
                            .reasoning_expanded
                            .unwrap_or(!app.compact_display.active());
                        let thinking = presentation.show_thinking && !card.reasoning.is_empty();
                        headers.push(Header {
                            line: lines.len(),
                            span: None,
                            hit: card_hit,
                        });
                        if thinking {
                            headers.push(Header {
                                line: lines.len() + if expanded { 3 } else { 2 },
                                span: None,
                                hit: HeaderHit {
                                    row: row_index,
                                    target: Target::Reasoning(index),
                                },
                            });
                        }
                        assistant_card(
                            t,
                            &mut lines,
                            &format!("▾ Assistant · {}", display_safe(&chat.model)),
                            &if !thinking {
                                Vec::new()
                            } else if expanded {
                                vec!["▾ Thinking".to_owned(), display_safe(&card.reasoning)]
                            } else {
                                let text = card
                                    .reasoning
                                    .trim_end()
                                    .rsplit('\n')
                                    .next()
                                    .unwrap_or("")
                                    .trim_start_matches([' ', '#', '*', '\t']);
                                vec![format!(
                                    "▸ Thinking · {}",
                                    crate::compact_display::summary(text, 65)
                                )]
                            },
                            &display_safe(&card.answer),
                            if last_card == Some(index) {
                                &state_text
                            } else {
                                state
                            },
                            width,
                        );
                        folds.push(FoldSpan {
                            row: row_index,
                            target: Target::Card(index),
                            end: lines.len(),
                        });
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
        return Transcript {
            lines,
            selected,
            headers,
            folds,
            #[cfg(test)]
            rendered_rows,
        };
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
    Transcript {
        lines,
        selected: selected_range,
        headers,
        folds,
        #[cfg(test)]
        rendered_rows: 0,
    }
}

/// Fold settled prompts and cards whose lines all end at or above `top`. The newest foldable is
/// never folded, and each card folds at most once (`Fold::autofold`). True when anything folded.
fn fold_off_screen(app: &mut Demo, folds: &[FoldSpan], top: usize) -> bool {
    use crate::{compact_display::Target, live_chat::ReplyState};
    let Some(chat) = app.live.as_mut() else {
        return false;
    };
    let mut folded = false;
    for span in folds.split_last().map_or(&[][..], |(_, older)| older) {
        if span.end > top {
            continue;
        }
        let Some(row) = chat.rows.get_mut(span.row) else {
            continue;
        };
        let settled = !matches!(
            row.state,
            ReplyState::Sending
                | ReplyState::Streaming
                | ReplyState::Cancelling
                | ReplyState::Maintenance
                | ReplyState::AwaitingRetirement
        );
        folded |= match span.target {
            Target::Prompt => row.prompt_fold.autofold(),
            Target::Card(index) => {
                settled && row.cards.get_mut(index).is_some_and(|c| c.fold.autofold())
            }
            _ => false,
        };
    }
    folded
}

pub fn draw(frame: &mut Frame, app: &mut Demo) {
    let t = app.palette().clone();
    let area = frame.area();
    if app.live.is_some() {
        app.compact_display.resize(area.width);
    }
    let compact = app.live.is_some() && app.compact_display.active();
    let presentation = app.presentation();
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
    let selectable = !(app.viewport_too_small
        || app.approval_open
        || app.themes_open
        || app.has_live_input()
        || app.completion.is_open());
    if !selectable {
        app.compact_display.clear_hits();
        app.transcript_selection.hide();
    }
    if app.viewport_too_small {
        if let Some(chat) = app.live.as_mut() {
            chat.runtime_controls.hide();
            chat.controls.hide();
            chat.queue.hide();
            chat.engine.invalidate();
            chat.commands.hide();
            chat.themes.hide();
            chat.saved_history.hide();
            chat.rewrite.hide();
            chat.compact.hide();
            chat.work.hide();
            chat.settings.occlude();
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
        Constraint::Length(if compact { 3 } else { 1 }),
        Constraint::Min(3),
        Constraint::Length(1),
        Constraint::Length(
            if compact
                && app.composer.text().is_empty()
                && (app.footer.selected().is_some() || app.has_live_input())
            {
                3
            } else {
                5
            },
        ),
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
        Paragraph::new(
            if let Some(chat) = &app.live
                && compact
            {
                app.compact_display
                    .now(chat)
                    .iter()
                    .map(|line| clip_label(line, usize::from(areas[1].width)))
                    .collect::<Vec<_>>()
                    .join("\n")
            } else if let Some(chat) = &app.live {
                format!(
                    " LIVE · {} · Ctrl+\\ commands · Ctrl+G view · Ctrl+Q quits",
                    chat.theme_status
                )
            } else {
                " LOCAL PREVIEW · No model connected · all calls, timings and counts are fixtures"
                    .into()
            },
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
    // A sidebar dialog takes its edge and the chat reflows narrower beside it instead of
    // being covered. Too narrow a terminal falls back to the modal placement.
    let dock = (presentation.sidebar_dialogs
        && (app.has_live_approval() || app.has_live_question())
        && area.width >= 80)
        .then(|| {
            let width = (area.width / 3).clamp(36, 52);
            let x = if presentation.dialogs_left {
                area.x
            } else {
                area.right() - width
            };
            Rect::new(x, areas[1].y, width, areas[1].height + areas[2].height)
        });
    let mut body = areas[2];
    if let Some(dock) = dock {
        body.width -= dock.width;
        if presentation.dialogs_left {
            body.x += dock.width;
        }
    }
    let viewport = Rect::new(
        body.x + 1,
        body.y,
        body.width.saturating_sub(2),
        body.height.saturating_sub(picker_height),
    );
    let page = app.live.as_ref().map(|chat| {
        let rows = chat.visible_row_range();
        (rows.start, rows.end, chat.show_live_history())
    });
    if app.transcript_rows != page {
        // Identical geometry/local scroll can now name different absolute rows.
        // A press or captured cell selection from the old page must not transfer.
        app.compact_display.clear_hits();
        app.transcript_selection.clear();
        app.transcript_rows = page;
    }
    let mut view = transcript(app, viewport.width as usize);
    let height = viewport.height as usize;
    // Source `_autocollapse_offscreen`: while the follow lock holds, fold settled cards that end
    // above the line the view is about to settle on, in the same frame as the move.
    if presentation.autoscroll
        && app
            .live
            .as_ref()
            .is_none_or(|chat| chat.is_latest_live_page())
        && app.scroll >= view.lines.len().saturating_sub(height)
        && fold_off_screen(app, &view.folds, view.lines.len().saturating_sub(height))
    {
        app.transcript_selection.clear();
        view = transcript(app, viewport.width as usize);
    }
    let Transcript {
        lines,
        selected,
        headers,
        ..
    } = view;
    app.max_scroll = lines.len().saturating_sub(height);
    let size = (viewport.width, viewport.height);
    let resized = app.transcript_viewport.replace(size) != Some(size);
    if app.follow_selection || (app.live.is_some() && selected.1 > selected.0 && resized) {
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
    if selectable {
        app.compact_display.set_hits(viewport, app.scroll, &headers);
        app.transcript_selection
            .draw(frame.buffer_mut(), viewport, app.scroll);
    }
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
            " {}{} · scroll {}/{}",
            app.live
                .as_ref()
                .and_then(|c| c.work.image.attached.as_ref())
                .map(|r| format!(
                    "[Image: {} · Ctrl+O review · Ctrl+X clear] ",
                    display_safe(r.name())
                ))
                .unwrap_or_default(),
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
                    " Ctrl+\\ commands · Esc stops · Ctrl+Q "
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
    let hint = if compact && presentation.key_hints {
        "^G view · ^B select · ^E open"
    } else {
        ""
    };
    let footer_width = usize::from(areas[5].width.saturating_sub(1));
    let status_width = footer_width.saturating_sub(hint.width() + usize::from(!hint.is_empty()));
    let footer = crate::footer::FooterLayout::with_status_width(
        &app.footer_fields,
        status_width,
        footer_width,
    );
    app.footer.update_visible(&footer.actions);
    let footer_style = style(t.footer_foreground, t.footer_background);
    let row = |row: &crate::footer::FooterRow| {
        // Python's footer starts at column 0 (its one cell of padding is on the right).
        let mut spans = vec![];
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
    // Python's second row: "|| strict on  ·  plan:off  meters:off" with the version at the right.
    let mut permission = row(&footer.permission);
    let version = app
        .live
        .as_ref()
        .and_then(|chat| chat.runtime_state.version())
        .map(|v| format!("v{v} "));
    if app.live.is_some() && !footer.permission.fields.is_empty() {
        permission.spans.push(Span::raw("  "));
        permission.spans.push(Span::styled(
            if presentation.task_manager {
                "meters:on"
            } else {
                "meters:off"
            },
            footer_style,
        ));
    }
    frame.render_widget(
        Paragraph::new(vec![row(&footer.status), permission]).style(footer_style),
        areas[5],
    );
    if let Some(version) = version
        && areas[5].height >= 2
        && usize::from(areas[5].width) > version.width() + 40
    {
        let w = version.width() as u16;
        frame.render_widget(
            Paragraph::new(version).style(footer_style),
            Rect::new(areas[5].right() - w, areas[5].y + 1, w, 1),
        );
    }
    if !hint.is_empty() {
        let w = hint.width().min(usize::from(areas[5].width)) as u16;
        frame.render_widget(
            Paragraph::new(hint).style(footer_style),
            Rect::new(areas[5].right() - w, areas[5].y, w, 1),
        );
    }
    if app.approval_open {
        approval(frame, app, area);
    }
    if app.has_live_approval() {
        live_approval(frame, app, dock.unwrap_or(area), dock.is_some());
    } else if app.has_live_question() {
        live_question(frame, app, dock.unwrap_or(area), dock.is_some());
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
    // Current-conversation Work can start after an idle management panel opens.
    // Source prompts own the frame; late management replies retain their owner
    // but must not collect visible-review evidence beneath an overlay.
    let lower_panels_visible = !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.live.as_ref().is_some_and(|chat| chat.cancelling());
    if app.has_live_engine() && lower_panels_visible {
        app.live
            .as_mut()
            .expect("engine")
            .engine
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.engine.hide();
    }
    if app.has_live_commands() && lower_panels_visible {
        app.live
            .as_mut()
            .expect("commands")
            .commands
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.commands.hide();
    }
    if app.help.open && !app.stop_open() {
        app.help.draw(frame, area, &t);
    }
    if app.has_live_history()
        && !app.has_live_rewrite()
        && !app.has_live_compact()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.stop_open()
        && !app.viewport_too_small
    {
        let theme = app.palette().clone();
        app.live
            .as_mut()
            .expect("live")
            .saved_history
            .draw(frame, area, &theme);
    } else if let Some(chat) = app.live.as_mut() {
        chat.saved_history.hide();
    }
    if app.has_live_rewrite()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.stop_open()
        && !app.viewport_too_small
    {
        app.live
            .as_mut()
            .expect("rewrite control")
            .rewrite
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.rewrite.hide();
    }
    if app.has_live_compact()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.stop_open()
        && !app.viewport_too_small
    {
        app.live
            .as_mut()
            .expect("compact control")
            .compact
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.compact.hide();
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
    if app.has_live_runtime_controls()
        && !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
    {
        app.live
            .as_mut()
            .expect("runtime controls")
            .runtime_controls
            .draw(frame, frame.area(), &t);
    }
    if app.has_live_work()
        && !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.viewport_too_small
        && !app.live.as_ref().is_some_and(|chat| chat.cancelling())
    {
        app.live.as_mut().expect("Work").work.draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.work.hide();
    }
    if app.has_live_settings()
        && !app.stop_open()
        && !app.has_live_approval()
        && !app.has_live_question()
        && !app.live.as_ref().is_some_and(|chat| chat.cancelling())
    {
        app.live
            .as_mut()
            .expect("settings view")
            .settings
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.settings.occlude();
    }
    if app.has_live_backend() && lower_panels_visible {
        app.live
            .as_mut()
            .expect("backend selector")
            .backend_controls
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.backend_controls.hide();
    }
    if app.has_live_browser()
        && lower_panels_visible
        && !app.has_live_history()
        && !app.has_live_rewrite()
        && !app.has_live_compact()
    {
        app.live
            .as_mut()
            .expect("conversation browser")
            .browser
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.browser.hide();
    }
    if app.has_live_controls() && lower_panels_visible {
        app.live
            .as_mut()
            .expect("live controls")
            .controls
            .draw(frame, area, &t);
    } else if let Some(chat) = app.live.as_mut() {
        chat.controls.hide();
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
    let title = if chat.execution_pending() {
        " Stop this work? "
    } else if chat.cancelling() {
        " Force stop? "
    } else {
        " Stop this turn? "
    };
    let text = vec![
        Line::raw(if chat.execution_pending() {
            "Any saved run or goal is kept."
        } else {
            "Partial output stays visible."
        }),
        Line::raw(if chat.execution_pending() {
            "Goal Pause is a separate action."
        } else {
            "Tool effects are not undone."
        }),
        Line::raw(if chat.execution_pending() {
            "Wait for verified retirement."
        } else if chat.cancelling() {
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
            if chat.execution_pending() {
                "[> Stop work <]"
            } else {
                "[> Stop turn <]"
            }
        } else if chat.execution_pending() {
            "[  Stop work  ]"
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

/// Modal dialogs are centered; a docked (sidebar) dialog fills the rectangle it was given.
fn dialog_rect(area: Rect, width: u16, height: u16, docked: bool) -> Rect {
    if docked {
        return area;
    }
    Rect::new(
        area.x + (area.width - width) / 2,
        area.y + (area.height - height) / 2,
        width,
        height,
    )
}

fn live_approval(frame: &mut Frame, app: &mut Demo, area: Rect, docked: bool) {
    let t = app.palette().clone();
    let chat = app.live.as_mut().expect("live approval");
    let count = chat.approvals.len();
    let current = chat.approvals.current_mut().expect("visible approval");
    let width = if docked {
        area.width
    } else {
        area.width.saturating_sub(4).min(78)
    };
    let lines = wrap(
        &display_safe(&current.request.display_text()),
        width.saturating_sub(4) as usize,
    );
    let height = (lines.len() + 8).min(usize::from(area.height) * 85 / 100) as u16;
    let rect = dialog_rect(area, width, height, docked);
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
    let layout = (parts[0].width, parts[0].height);
    if current.layout != Some(layout) {
        current.layout = Some(layout);
        current.painted_lines.clear();
        current.reviewed = false;
        current.confirming = false;
        current.confirmation_shown = false;
        current.pressed = None;
    }
    current
        .painted_lines
        .extend(current.scroll..(current.scroll + parts[0].height as usize).min(lines.len()));
    current.reviewed = current.painted_lines.len() == lines.len();
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
    let choices = if current.request.remember_scope.is_some() {
        3
    } else {
        2
    };
    let selected = if current.remember {
        2
    } else {
        u8::from(current.allow)
    };
    let mut action = String::from(" ");
    let mut hits = Vec::new();
    if current.waiting {
        action.push_str("Decision sent; waiting for source closure.");
    } else if current.confirming {
        action.push_str("[ Confirm always (Enter) ]");
        if current.reviewed && parts[1].width >= 28 && parts[1].height >= 3 {
            current.confirmation_shown = true;
            hits.push((Rect::new(parts[1].x + 1, parts[1].y + 2, 25, 1), 3));
        }
    } else {
        for (index, label) in ["Deny", "Allow once", "Always allow"]
            .iter()
            .enumerate()
            .take(choices)
        {
            let text = if selected == index as u8 {
                format!("[> {label} <] ")
            } else {
                format!("[ {label} ] ")
            };
            let x = action.len() as u16;
            if x + text.len() as u16 <= parts[1].width && parts[1].height >= 3 {
                hits.push((
                    Rect::new(parts[1].x + x, parts[1].y + 2, text.len() as u16 - 1, 1),
                    index as u8,
                ));
            }
            action.push_str(&text);
        }
    }
    if current.hits != hits {
        current.pressed = None;
    }
    current.hits = hits;
    let note = if current.confirming {
        " Confirm exact tool + authority for this conversation."
    } else if current.remember && !current.reviewed {
        " Read every request page before confirming Always allow."
    } else if current.request.remember_scope.is_some() {
        " Always allow needs separate confirmation and saved readback."
    } else {
        " This request supports Deny or Allow once only."
    };
    let controls = vec![
        Line::raw(format!(
            " Source wait budget: {budget}; turn deadline still applies."
        )),
        Line::raw(note),
        Line::styled(
            action,
            style(t.tool_text, t.surface).add_modifier(Modifier::BOLD),
        ),
        Line::raw(" Tab/click selects; Enter confirms; Esc denies; Ctrl+Q quits."),
        Line::raw(" Up/Down/PgUp/PgDn scroll full request; no auto-answer."),
    ];
    frame.render_widget(
        Paragraph::new(controls).style(style(t.foreground, t.surface)),
        parts[1],
    );
}

fn live_question(frame: &mut Frame, app: &mut Demo, area: Rect, docked: bool) {
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
    let rect = dialog_rect(area, width, height, docked);
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

#[cfg(test)]
mod live_paging_render_work_tests {
    use super::*;
    use crate::{live::Observation, live_chat::Chat};

    #[test]
    fn ten_thousand_local_rows_materialize_only_the_selected_bounded_window() {
        let mut chat = Chat::new("custom".into(), "fixture".into());
        chat.observe(Observation::Ready).unwrap();
        for index in 0..10_000 {
            chat.submit(&format!("prompt-{index}")).unwrap();
            chat.observe(Observation::Text(format!("answer-{index}")))
                .unwrap();
            chat.observe(Observation::Complete).unwrap();
        }
        let mut app = Demo::live("custom".into(), "fixture".into());
        app.live = Some(chat);
        for first in [false, true, false] {
            let chat = app.live.as_mut().unwrap();
            if first {
                chat.first_live_page();
            } else {
                chat.latest_live_page();
            }
            let range = chat.visible_row_range();
            assert!(range.len() <= 64);
            assert!(
                chat.rows[range.clone()]
                    .iter()
                    .map(|row| row.render_units())
                    .sum::<usize>()
                    <= crate::live_chat::DISPLAY_UNITS
            );
            if first {
                assert_eq!(range.start, 0);
            } else {
                assert_eq!(range.end, 10_000);
            }
            for width in [36, 120, 240] {
                let view = transcript(&app, width);
                assert_eq!(view.rendered_rows, range.len());
                assert!(
                    view.headers
                        .iter()
                        .all(|header| range.contains(&header.hit.row))
                );
                assert!(view.folds.iter().all(|fold| range.contains(&fold.row)));
                assert!(
                    view.lines.len() < 64 * 64,
                    "only short fixture bodies in one page are materialized"
                );
            }
        }
        let chat = app.live.as_ref().unwrap();
        assert_eq!(chat.rows.len(), 10_000);
        assert_eq!(chat.rows[0].prompt, "prompt-0");
        assert_eq!(chat.rows[9_999].cards[0].answer, "answer-9999");
    }
}
