//! A reply card is titled "<summary> - <model>" when Python's card summary lands for its
//! answer (native_card_summary.py), as AssistantMessage._header_text titles it.
use litetui_terminal::{demo::Demo, live::Observation, ui};
use ratatui::{Terminal, backend::TestBackend};
use sha2::{Digest, Sha256};

fn screen(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(110, 35)).unwrap();
    terminal.draw(|frame| ui::draw(frame, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}

fn sha(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.trim().as_bytes()))
}

#[test]
fn a_summary_titles_the_card_whose_answer_it_names() {
    let mut app = Demo::live("custom".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("question");
    app.submit();
    app.live.as_mut().unwrap().take_submission();
    app.observe_live(Observation::Text("The answer.\n".into()))
        .unwrap();
    app.observe_live(Observation::Complete).unwrap();
    assert!(screen(&mut app).contains("╭─ ▾ fixture "));
    // A summary for another answer titles nothing.
    app.observe_live(Observation::CardSummary {
        sha256: sha("Another answer."),
        thinking: false,
        summary: "Wrong card".into(),
    })
    .unwrap();
    assert!(!screen(&mut app).contains("Wrong card"));
    app.observe_live(Observation::CardSummary {
        sha256: sha("  The answer."),
        thinking: false,
        summary: "Explains the answer".into(),
    })
    .unwrap();
    let shown = screen(&mut app);
    assert!(
        shown.contains("╭─ ▾ Explains the answer - fixture "),
        "{shown}"
    );
}
