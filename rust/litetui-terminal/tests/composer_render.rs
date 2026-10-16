use litetui_terminal::{composer::Motion, demo::Demo, ui};
use ratatui::{
    Terminal,
    backend::{Backend, TestBackend},
    layout::Rect,
    style::Modifier,
};

#[test]
fn oversized_grapheme_uses_a_visible_placeholder_without_losing_text() {
    let original = format!("{}Z", "\u{1100}".repeat(20));
    let mut app = Demo::default();
    app.insert_text(&original);
    app.composer.move_cursor(Motion::Home, false);
    let mut terminal = Terminal::new(TestBackend::new(40, 24)).unwrap();
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    assert_eq!(terminal.backend().buffer()[cursor].symbol(), "�");
    app.composer.move_cursor(Motion::Right, true);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    assert_eq!(terminal.backend().buffer()[cursor].symbol(), "Z");
    assert!(
        terminal.backend().buffer()[(1, cursor.y)]
            .modifier
            .contains(Modifier::UNDERLINED)
    );
    assert_eq!(app.composer.text(), original);
    terminal.backend_mut().resize(80, 24);
    terminal.resize(Rect::new(0, 0, 80, 24)).unwrap();
    app.composer.move_cursor(Motion::Home, false);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    assert_eq!(
        terminal.backend().buffer()[cursor].symbol(),
        "\u{1100}".repeat(20)
    );
}

#[test]
fn scalar_positions_inside_a_grapheme_snap_visually_to_its_end() {
    for glyph in ["❤️", "1️⃣", "👩‍👩‍👧‍👦"] {
        let mut app = Demo::default();
        app.insert_text(&format!("{glyph}Z"));
        app.composer.move_cursor(Motion::Home, false);
        let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
        for _ in glyph.chars() {
            app.composer.move_cursor(Motion::Right, false);
            terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
            let cursor = terminal.backend_mut().get_cursor_position().unwrap();
            assert_eq!(
                terminal.backend().buffer()[cursor].symbol(),
                "Z",
                "glyph {glyph}, cursor {cursor:?}"
            );
        }
    }
}

#[test]
fn wide_character_under_cursor_is_not_clipped_at_the_right_edge() {
    for width in [40, 80, 120] {
        let mut app = Demo::default();
        app.insert_text(&format!("{}雪Z", "a".repeat(width as usize - 3)));
        app.composer.move_cursor(Motion::Left, false);
        app.composer.move_cursor(Motion::Left, false);
        let mut terminal = Terminal::new(TestBackend::new(width, 24)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let cursor = terminal.backend_mut().get_cursor_position().unwrap();
        assert_eq!(
            terminal.backend().buffer()[cursor].symbol(),
            "雪",
            "width {width}"
        );
        assert!(cursor.x + 1 < width - 1);
    }
}

#[test]
fn cursor_tracks_insertion_position_and_selection_not_just_the_tail() {
    let mut app = Demo::default();
    app.insert_text("abc雪Z");
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    app.composer.move_cursor(Motion::Left, true);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    let cell = &terminal.backend().buffer()[cursor];
    assert_eq!(cell.symbol(), "Z");
    assert!(cell.modifier.contains(Modifier::UNDERLINED));
    app.composer.move_cursor(Motion::Left, false); // Collapse selection, no extra step.
    app.composer.move_cursor(Motion::Left, false);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    assert_eq!(terminal.backend().buffer()[cursor].symbol(), "雪");
    app.composer.move_cursor(Motion::Home, false);
    terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
    let cursor = terminal.backend_mut().get_cursor_position().unwrap();
    assert_eq!(terminal.backend().buffer()[cursor].symbol(), "a");
}

#[test]
fn long_wide_input_keeps_cursor_inside_the_composer_after_resize() {
    let mut app = Demo::default();
    app.insert_text(&format!("{}雪Z", "🙂".repeat(100)));
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    for width in [80, 40, 120] {
        terminal.backend_mut().resize(width, 24);
        terminal.resize(Rect::new(0, 0, width, 24)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let cursor = terminal.backend_mut().get_cursor_position().unwrap();
        assert!(cursor.x > 0 && cursor.x < width - 1);
        assert!(cursor.y > 0 && cursor.y < 23);
        assert_eq!(terminal.backend().buffer()[cursor].symbol(), " ");
        app.composer.move_cursor(Motion::Left, false);
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let cursor = terminal.backend_mut().get_cursor_position().unwrap();
        assert_eq!(
            terminal.backend().buffer()[cursor].symbol(),
            "Z",
            "width {width}, cursor {cursor:?}, nonempty row {:?}",
            (0..width)
                .filter_map(|x| {
                    let s = terminal.backend().buffer()[(x, cursor.y)].symbol();
                    (s != " ").then_some((x, s.to_owned()))
                })
                .collect::<Vec<_>>()
        );
        app.composer.move_cursor(Motion::End, false);
    }
}
