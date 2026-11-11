//! Local TestBackend paging coverage. No runtime, Work, persistence or provider proof.
use crossterm::event::{KeyModifiers, MouseButton, MouseEvent, MouseEventKind};
use litetui_terminal::{
    compact_display::Target, demo::Demo, live::Observation, live_chat::Chat, ui,
};
use ratatui::{Terminal, backend::TestBackend, buffer::Buffer};

fn fixture(count: usize, same: bool, long: bool) -> Demo {
    let mut chat = Chat::new("custom".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    for index in 0..count {
        chat.submit(&if same {
            "SAME PROMPT".into()
        } else {
            format!("ROW{index:05}")
        })
        .unwrap();
        let answer = if long {
            format!("{}TAIL{index:05}", "body\n".repeat(700))
        } else if same {
            "same answer".into()
        } else {
            format!("ANSWER{index:05}")
        };
        chat.observe(Observation::Text(answer)).unwrap();
        chat.observe(Observation::Complete).unwrap();
    }
    let mut app = Demo::live("custom".into(), "fixture".into());
    app.live = Some(chat);
    app.insert_text("unsent draft");
    app
}
fn draw(app: &mut Demo, width: u16) -> Buffer {
    let mut terminal = Terminal::new(TestBackend::new(width, 40)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal.backend().buffer().clone()
}
fn text(buffer: &Buffer) -> String {
    buffer.content.iter().map(|cell| cell.symbol()).collect()
}
fn line_with(buffer: &Buffer, needle: &str) -> u16 {
    (0..buffer.area.height)
        .find(|y| {
            (0..buffer.area.width)
                .map(|x| buffer[(x, *y)].symbol())
                .collect::<String>()
                .contains(needle)
        })
        .expect(needle)
}
fn mouse(kind: MouseEventKind, x: u16, y: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    }
}

#[test]
fn paging_draws_absolute_rows_and_keeps_all_old_text_and_the_composer() {
    let mut app = fixture(160, false, false);
    let latest = app.live.as_ref().unwrap().visible_row_range();
    assert_eq!(latest.end, 160);
    let buffer = draw(&mut app, 140);
    let first = format!("ROW{:05}", latest.start);
    assert!(text(&buffer).contains(&first));
    let y = line_with(&buffer, &first);
    assert!(
        app.compact_display
            .click(mouse(MouseEventKind::Down(MouseButton::Left), 4, y))
            .is_none()
    );
    let hit = app
        .compact_display
        .click(mouse(MouseEventKind::Up(MouseButton::Left), 4, y))
        .unwrap();
    assert_eq!(hit.row, latest.start);
    assert_eq!(hit.target, Target::Prompt);
    assert!(app.live.as_mut().unwrap().first_live_page());
    app.scroll = 0;
    let oldest = app.live.as_ref().unwrap().visible_row_range();
    assert_eq!(oldest.start, 0);
    assert!(text(&draw(&mut app, 140)).contains("ROW00000"));
    assert!(app.live.as_mut().unwrap().newer_live_page());
    assert_eq!(
        app.live.as_ref().unwrap().visible_row_range().start,
        oldest.end
    );
    draw(&mut app, 36);
    draw(&mut app, 140);
    assert_eq!(app.composer.text(), "unsent draft");
    assert_eq!(app.live.as_ref().unwrap().rows.len(), 160);
    assert_eq!(
        app.live.as_ref().unwrap().rows[0].cards[0].answer,
        "ANSWER00000"
    );
}

#[test]
fn page_change_invalidates_identical_cells_and_old_header_press() {
    let mut app = fixture(160, true, false);
    let old = draw(&mut app, 140);
    let y = line_with(&old, "SAME PROMPT");
    app.compact_display
        .click(mouse(MouseEventKind::Down(MouseButton::Left), 4, y));
    assert!(
        app.transcript_selection
            .mouse(mouse(MouseEventKind::Down(MouseButton::Left), 4, y), &old)
    );
    app.transcript_selection
        .mouse(mouse(MouseEventKind::Drag(MouseButton::Left), 8, y), &old);
    app.transcript_selection
        .mouse(mouse(MouseEventKind::Up(MouseButton::Left), 8, y), &old);
    assert!(app.transcript_selection.active());
    assert!(app.live.as_mut().unwrap().older_live_page());
    app.scroll = 0;
    draw(&mut app, 140);
    assert!(
        !app.transcript_selection.active(),
        "identical cells from a different absolute page cannot inherit selection"
    );
    assert!(
        app.compact_display
            .click(mouse(MouseEventKind::Up(MouseButton::Left), 4, y))
            .is_none()
    );
}

#[test]
fn older_page_follow_does_not_autofold_retained_rows() {
    let mut app = fixture(160, false, false);
    assert!(app.live.as_mut().unwrap().first_live_page());
    app.scroll = usize::MAX;
    draw(&mut app, 140);
    let chat = app.live.as_ref().unwrap();
    assert!(
        chat.rows
            .iter()
            .all(|row| !row.prompt_fold.folded && !row.cards[0].fold.folded)
    );
}

#[test]
fn one_large_row_remains_line_scrollable_to_its_last_byte() {
    let mut app = fixture(3, false, true);
    assert_eq!(app.live.as_ref().unwrap().visible_row_range().len(), 1);
    app.scroll = usize::MAX;
    assert!(text(&draw(&mut app, 140)).contains("TAIL00002"));
    assert!(app.live.as_mut().unwrap().older_live_page());
    app.scroll = usize::MAX;
    assert!(text(&draw(&mut app, 140)).contains("TAIL00001"));
    assert_eq!(app.live.as_ref().unwrap().rows.len(), 3);
}

#[test]
fn new_stream_chunks_do_not_move_a_reader_on_an_older_page() {
    let mut app = fixture(160, false, false);
    app.live.as_mut().unwrap().submit("active turn").unwrap();
    assert!(app.live.as_mut().unwrap().first_live_page());
    app.scroll = 0;
    draw(&mut app, 140);
    let before = app.live.as_ref().unwrap().visible_row_range();
    app.observe_live(Observation::Text("arriving answer".into()))
        .unwrap();
    assert_eq!(app.live.as_ref().unwrap().visible_row_range(), before);
    assert_eq!(app.scroll, 0);
    assert!(text(&draw(&mut app, 140)).contains("ROW00000"));
    draw(&mut app, 80);
    assert_eq!(app.live.as_ref().unwrap().visible_row_range(), before);
    assert_eq!(app.composer.text(), "unsent draft");
}
