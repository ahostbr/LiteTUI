//! The transcript draws an answer from Python's Markdown lines (native_markdown.py) once they
//! come back for the card's width, and asks again when the answer or the width changes.
use litetui_terminal::{
    demo::Demo,
    live::Observation,
    markdown::{Lines, Run},
    ui,
};
use ratatui::{Terminal, backend::TestBackend};

fn screen(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(110)
        .map(|row| row.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}

fn lines(text: &str) -> Lines {
    vec![
        vec![Run {
            text: text.into(),
            ..Default::default()
        }],
        Vec::new(),
    ]
}

#[test]
fn an_answer_is_drawn_from_pythons_lines_for_its_width_and_asked_again_when_it_changes() {
    let mut app = Demo::live("custom".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("question");
    app.submit();
    app.live.as_mut().unwrap().take_submission();
    app.observe_live(Observation::Text("raw *answer*\nsecond line".into()))
        .unwrap();
    let before = screen(&mut app);
    assert!(before.contains("raw *answer*"), "{before}");
    let chat = app.live.as_mut().unwrap();
    let (key, text, width) = chat.take_markdown_request().expect("asked for");
    assert_eq!(text, "raw *answer*\nsecond line");
    // One read at a time.
    assert!(chat.take_markdown_request().is_none());
    app.observe_live(Observation::Markdown {
        key,
        width,
        lines: Some(lines("raw answer second line")),
    })
    .unwrap();
    let after = screen(&mut app);
    assert!(after.contains("raw answer second line"), "{after}");
    assert!(!after.contains("raw *answer*"), "{after}");
    // The settled render stands; a refusal leaves the raw text and waits before asking again.
    let chat = app.live.as_mut().unwrap();
    assert!(chat.take_markdown_request().is_none());
    app.observe_live(Observation::Text(" more".into())).unwrap();
    let chat = app.live.as_mut().unwrap();
    let (key, _, width) = chat.take_markdown_request().expect("asked again");
    app.observe_live(Observation::Markdown {
        key,
        width,
        lines: None,
    })
    .unwrap();
    assert!(app.live.as_mut().unwrap().take_markdown_request().is_none());
}
