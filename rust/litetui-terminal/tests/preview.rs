use litetui_terminal::{
    demo::{Demo, ToolState},
    ui,
};
use ratatui::{Terminal, backend::TestBackend, style::Color};

fn screen(app: &mut Demo, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}

#[test]
fn preview_is_explicitly_disconnected_and_shows_tool_arguments() {
    let text = screen(&mut Demo::default(), 120, 44);
    assert!(text.contains("LOCAL PREVIEW"));
    assert!(text.contains("No model connected"));
    assert!(text.contains("read"));
    assert!(text.contains("Arguments"));
    assert!(text.contains("Result"));
}

#[test]
fn every_cell_has_a_dark_background_even_after_resize() {
    let mut demo = Demo::default();
    for (width, height) in [(1, 1), (20, 6), (45, 16), (80, 24), (120, 44), (160, 50)] {
        let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
        // Inspect the completed frame, including wide-glyph continuation cells.
        // The diff sent to TestBackend intentionally omits those covered cells.
        let rendered = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
        for cell in &rendered.buffer.content {
            match cell.bg {
                Color::Rgb(r, g, b) => assert!(r < 50 && g < 50 && b < 60),
                other => panic!("not an explicit dark background: {other:?}"),
            }
        }
    }
}

#[test]
fn tool_expansion_and_selection_are_real_presentation_state() {
    let mut demo = Demo {
        selected_tool: 0,
        ..Demo::default()
    };
    assert!(!demo.tools[0].expanded);
    demo.toggle_tool();
    assert!(demo.tools[0].expanded);
    demo.next_tool();
    assert_eq!(demo.selected_tool, 1);
    demo.previous_tool();
    assert_eq!(demo.selected_tool, 0);
}

#[test]
fn mock_approval_defaults_to_deny_and_cannot_run_a_command() {
    let mut demo = Demo::default();
    demo.open_approval();
    assert!(demo.approval_open);
    assert!(!demo.approval_allow);
    demo.resolve_approval(false);
    assert_eq!(demo.tools[3].state, ToolState::Denied);
    assert!(demo.tools[3].result.contains("No file was written"));
    assert!(!demo.approval_open);
    demo.open_approval();
    assert!(!demo.approval_open);
}

#[test]
fn allowing_a_mock_approval_only_changes_fixture_text() {
    let mut demo = Demo::default();
    demo.open_approval();
    demo.resolve_approval(true);
    assert_eq!(demo.tools[3].state, ToolState::Success);
    assert!(demo.tools[3].result.contains("simulated"));
    assert!(demo.tools[3].result.contains("No file was written"));
}

#[test]
fn replay_and_mock_progress_are_deterministic() {
    let mut demo = Demo::default();
    assert_eq!(demo.tools[2].state, ToolState::Running);
    demo.tick(100);
    assert_eq!(demo.tools[2].state, ToolState::Success);
    demo.reset();
    assert_eq!(demo.tools[2].state, ToolState::Running);
    assert_eq!(demo.tools[3].state, ToolState::Awaiting);
}

#[test]
fn input_is_bounded_sanitized_and_only_adds_a_mock_turn() {
    let mut demo = Demo::default();
    demo.insert_text("hello 雪\u{1b}[31m\r\nworld");
    assert!(!demo.input.contains('\u{1b}'));
    assert!(!demo.input.contains('\r'));
    assert!(!demo.input.contains('\n'));
    demo.submit();
    assert_eq!(demo.turns.len(), 1);
    assert!(demo.turns[0].contains("hello 雪"));
    assert!(demo.input.is_empty());
    for _ in 0..12 {
        demo.insert_text(&"a".repeat(800));
        assert!(demo.input.chars().count() <= 500);
        demo.submit();
    }
    assert!(demo.turns.len() <= 6);
}

#[test]
fn modal_is_labeled_and_small_terminals_do_not_panic() {
    let mut demo = Demo::default();
    demo.open_approval();
    let text = screen(&mut demo, 120, 44);
    assert!(text.contains("MOCK APPROVAL"));
    assert!(text.contains("No tool will execute"));
    for (width, height) in [(1, 1), (20, 6), (45, 16)] {
        screen(&mut demo, width, height);
    }
}

#[test]
fn scroll_is_clamped_to_the_rendered_document() {
    let mut demo = Demo {
        scroll: usize::MAX,
        ..Demo::default()
    };
    screen(&mut demo, 80, 24);
    assert!(demo.scroll <= demo.max_scroll);
    demo.scroll_up(usize::MAX);
    assert_eq!(demo.scroll, 0);
    demo.scroll_down(usize::MAX);
    assert_eq!(demo.scroll, demo.max_scroll);
}

#[test]
fn small_modal_keeps_both_decisions_and_help_visible() {
    let mut demo = Demo::default();
    demo.open_approval();
    let text = screen(&mut demo, 36, 14);
    assert!(text.contains("Deny mock"));
    assert!(text.contains("Allow mock"));
    assert!(text.contains("Enter"));
    assert!(text.contains("Esc"));
}

#[test]
fn resize_on_the_same_terminal_preserves_dark_rendering() {
    let mut demo = Demo::default();
    demo.open_approval();
    let mut terminal = Terminal::new(TestBackend::new(120, 44)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
    for (width, height) in [(36, 14), (20, 6), (120, 44)] {
        terminal.backend_mut().resize(width, height);
        terminal
            .resize(ratatui::layout::Rect::new(0, 0, width, height))
            .unwrap();
        let rendered = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
        assert!(
            rendered
                .buffer
                .content
                .iter()
                .all(|cell| matches!(cell.bg, Color::Rgb(r, g, b) if r < 50 && g < 50 && b < 60))
        );
    }
}

#[test]
fn ash_tool_rows_and_nested_thinking_match_the_existing_visual_grammar() {
    let mut demo = Demo::default();
    let mut terminal = Terminal::new(TestBackend::new(120, 50)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
    let buffer = terminal.backend().buffer();
    let rows: Vec<String> = buffer
        .content
        .chunks(120)
        .map(|row| row.iter().map(|cell| cell.symbol()).collect())
        .collect();
    let tool_row = rows
        .iter()
        .position(|row| row.contains("grep") && row.contains("done"))
        .unwrap();
    assert!(rows[tool_row].contains("pattern"));
    assert!(rows[tool_row].contains("chars"));
    assert!(!rows[tool_row].contains('╭'));
    assert!(!rows[tool_row].contains('╮'));
    assert!(
        buffer.content[tool_row * 120..(tool_row + 1) * 120]
            .iter()
            .any(|cell| cell.fg == Color::Rgb(232, 163, 61))
    );
    assert!(
        rows.iter()
            .any(|row| row.contains("Thinking:") && row.contains('╭'))
    );
    assert!(rows.iter().any(|row| row.contains("┏╍")));
    assert!(rows.iter().any(|row| row.contains("╏ ▾ Thinking")));
    assert!(
        buffer
            .content
            .iter()
            .any(|cell| cell.bg == Color::Rgb(21, 21, 20))
    );
}
