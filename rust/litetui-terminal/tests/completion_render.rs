use litetui_terminal::{
    completion::{Completion, CompletionKind},
    demo::Demo,
    ui,
};
use ratatui::{
    Terminal,
    backend::{Backend, TestBackend},
    layout::Rect,
    style::Color,
};
fn screen(terminal: &Terminal<TestBackend>) -> String {
    let buffer = terminal.backend().buffer();
    (0..buffer.area.height)
        .map(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, y)].symbol())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}
#[test]
fn completion_selection_and_kind_remain_visible_after_resize_in_every_dark_theme() {
    let mut app = Demo::default();
    app.insert_text("/");
    app.completion.move_by(2);
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    for theme in 0..app.themes.len() {
        app.theme_index = theme;
        for (width, height) in [(80, 24), (36, 14), (120, 30)] {
            terminal.backend_mut().resize(width, height);
            terminal.resize(Rect::new(0, 0, width, height)).unwrap();
            terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
            let text = screen(&terminal);
            assert!(
                text.lines()
                    .any(|line| line.contains("preview-skill") && line.contains("[skill]")),
                "{} at {width}x{height}: {text}",
                app.palette().name
            );
            assert_eq!(app.composer.text(), "/");
            let cursor = terminal.backend_mut().get_cursor_position().unwrap();
            assert!(cursor.x > 0 && cursor.x < width - 1);
            assert!(
                !text
                    .lines()
                    .nth(cursor.y as usize)
                    .unwrap()
                    .contains("[skill]")
            );
        }
    }
}
#[test]
fn emoji_names_are_clipped_at_whole_graphemes_without_hiding_the_kind() {
    let mut app = Demo {
        completion_candidates: vec![Completion {
            name: "❤️".repeat(19),
            description: String::new(),
            kind: CompletionKind::Command,
        }],
        ..Demo::default()
    };
    app.insert_text("/");
    let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let text = screen(&terminal);
    assert!(
        text.lines()
            .any(|line| line.contains("❤️") && line.contains("[command]")),
        "{text}"
    );
}

#[test]
fn large_catalog_scrolls_selected_row_into_view_and_modal_hides_the_list() {
    let mut app = Demo::default();
    app.themes[app.theme_index].primary = Color::Rgb(12, 34, 56);
    app.completion_candidates = (0..240)
        .map(|n| Completion {
            name: format!("item-{n:03}"),
            description: "line one\n\tline two".into(),
            kind: CompletionKind::Command,
        })
        .collect();
    app.insert_text("/");
    app.completion.move_by(239);
    let mut terminal = Terminal::new(TestBackend::new(40, 14)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert!(screen(&terminal).contains("item-199"));
    assert!(
        terminal
            .backend()
            .buffer()
            .content
            .iter()
            .any(|cell| cell.symbol() == "╭" && cell.fg == Color::Rgb(12, 34, 56))
    );
    app.open_approval();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    assert!(!screen(&terminal).contains("item-199"));
    assert!(app.completion.is_open());
    assert_eq!(app.composer.text(), "/");
}
