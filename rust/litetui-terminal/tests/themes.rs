use litetui_terminal::{demo::Demo, theme, ui};
use ratatui::{Terminal, backend::TestBackend, style::Color};

#[test]
fn catalog_has_public_dark_themes_not_a_single_fixed_palette() {
    let themes = theme::builtin_themes();
    assert_eq!(themes.len(), 36);
    for name in ["ash", "matrix", "dracula", "nord", "lite-suite"] {
        assert!(themes.iter().any(|theme| theme.name == name));
    }
    assert!(!themes.iter().any(|theme| theme.name == "textual-light"));
}

#[test]
fn theme_picker_filters_selects_and_cancels_without_persistence() {
    let mut demo = Demo::default();
    assert_eq!(demo.palette().name, "ash");
    demo.open_themes();
    demo.theme_query = "matrix".into();
    assert_eq!(demo.filtered_themes().len(), 1);
    demo.choose_theme();
    assert_eq!(demo.palette().name, "matrix");
    assert!(!demo.themes_open);
    demo.reset();
    assert_eq!(
        demo.palette().name,
        "matrix",
        "replaying fixtures must not reset theme"
    );
    demo.open_themes();
    demo.theme_query = "not-a-theme".into();
    demo.choose_theme();
    assert_eq!(demo.palette().name, "matrix");
    assert!(
        demo.themes_open,
        "an empty match must not silently choose a theme"
    );
}

#[test]
fn every_public_theme_renders_dark_and_preserves_tool_structure() {
    let mut demo = Demo::default();
    for index in 0..demo.themes.len() {
        demo.theme_index = index;
        let mut terminal = Terminal::new(TestBackend::new(120, 50)).unwrap();
        let frame = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
        for cell in &frame.buffer.content {
            match cell.bg {
                Color::Rgb(r, g, b) => assert!(
                    u32::from(r) + u32::from(g) + u32::from(b) < 400,
                    "light surface in {}",
                    demo.palette().name
                ),
                other => panic!(
                    "unresolved background in {}: {other:?}",
                    demo.palette().name
                ),
            }
        }
        let text: String = frame
            .buffer
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        assert!(text.contains("Thinking:"));
        assert!(text.contains("Arguments"));
    }
}

#[test]
fn custom_resolved_colors_are_consumed_by_the_renderer() {
    let mut demo = Demo::default();
    let mut custom = demo.palette().clone();
    custom.name = "fixture-custom".into();
    custom.tool_text = Color::Rgb(255, 0, 255);
    custom.thinking_border = Color::Rgb(0, 255, 0);
    custom.thinking_text = Color::Rgb(100, 200, 255);
    custom.footer_foreground = Color::Rgb(255, 200, 100);
    demo.themes.push(custom);
    demo.theme_index = demo.themes.len() - 1;
    let mut terminal = Terminal::new(TestBackend::new(120, 50)).unwrap();
    let frame = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
    for color in [
        Color::Rgb(255, 0, 255),
        Color::Rgb(0, 255, 0),
        Color::Rgb(100, 200, 255),
        Color::Rgb(255, 200, 100),
    ] {
        assert!(frame.buffer.content.iter().any(|cell| cell.fg == color));
    }
}

#[test]
fn compact_picker_keeps_search_tail_selection_and_help_visible() {
    let mut demo = Demo::default();
    demo.open_themes();
    demo.search_themes(&("雪".repeat(40) + "tail"));
    let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
    let frame = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
    let text: String = frame
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains("tail"));
    assert!(text.contains("No matching themes"));
    assert!(text.contains("Enter apply"));
    assert!(text.contains("Esc"));
    demo.theme_query.clear();
    demo.theme_cursor = demo.themes.len() - 1;
    let frame = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
    let text: String = frame
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    assert!(text.contains(&format!("> {}", demo.themes.last().unwrap().name)));
}

#[test]
fn compact_footer_keeps_theme_approval_and_quit_discoverable() {
    let mut demo = Demo::default();
    let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
    let frame = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
    let text: String = frame
        .buffer
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect();
    for label in ["F4", "F6", "Ctrl+Q"] {
        assert!(text.contains(label), "missing {label}");
    }
}

#[test]
fn search_is_bounded_sanitized_and_navigation_wraps() {
    let mut demo = Demo::default();
    demo.open_themes();
    demo.theme_cursor = 0;
    demo.move_theme(false);
    assert_eq!(demo.theme_cursor, demo.themes.len() - 1);
    demo.move_theme(true);
    assert_eq!(demo.theme_cursor, 0);
    demo.search_themes("\n\r\t\u{1b}");
    assert!(demo.theme_query.is_empty());
    demo.search_themes(&"x".repeat(200));
    assert_eq!(demo.theme_query.chars().count(), 80);
    demo.move_theme(true);
    demo.choose_theme();
    assert!(demo.themes_open);
    assert_eq!(demo.palette().name, "ash");
}

#[test]
fn compact_tool_header_prioritizes_state_over_metadata() {
    let mut demo = Demo::default();
    for (index, expected) in [(3, "waiting approval"), (4, "error")] {
        demo.selected_tool = index;
        demo.follow_selection = true;
        let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
        let frame = terminal.draw(|frame| ui::draw(frame, &mut demo)).unwrap();
        let rows: Vec<String> = frame
            .buffer
            .content
            .chunks(36)
            .map(|row| row.iter().map(|cell| cell.symbol()).collect())
            .collect();
        assert!(
            rows.iter()
                .any(|row| row.contains(demo.tools[index].name) && row.contains(expected))
        );
    }
}
