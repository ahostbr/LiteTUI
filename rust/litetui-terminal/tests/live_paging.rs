use litetui_terminal::{
    demo::Demo,
    live::{ContentKind, Observation},
    live_chat::{Chat, DISPLAY_UNITS, MAX_VISIBLE_ROWS, ReplyState},
    native_activity::Activity,
};
use serde_json::json;

fn finish(chat: &mut Chat, n: usize, tools: bool) {
    chat.submit(&format!("prompt {n}")).unwrap();
    assert_eq!(chat.take_submission().unwrap(), format!("prompt {n}"));
    chat.observe(Observation::Submission {
        id: format!("operation-{n}"),
    })
    .unwrap();
    if tools {
        for event in [
            json!({"type":"native_activity","provider":"claude","kind":"tool_use",
                "id":"same-provider-id","name":"Read","card_index":0,"input":{"path":format!("file-{n}")},
                "complete":true,"result":null,"is_error":null}),
            json!({"type":"native_activity","provider":"claude","kind":"tool_result",
                "id":"same-provider-id","name":null,"card_index":0,"input":null,
                "complete":true,"result":format!("result {n}"),"is_error":false}),
        ] {
            chat.observe(Observation::Activity(Activity::from_event(&event).unwrap()))
                .unwrap();
        }
    }
    chat.observe(Observation::Content {
        card: 0,
        kind: ContentKind::AnswerDelta,
        text: format!("answer {n}"),
    })
    .unwrap();
    chat.observe(Observation::Complete).unwrap();
}
fn bounded(chat: &Chat) {
    let range = chat.visible_row_range();
    assert!(range.len() <= MAX_VISIBLE_ROWS);
    assert!(
        chat.rows[range]
            .iter()
            .map(|row| row.render_units())
            .sum::<usize>()
            <= DISPLAY_UNITS
    );
}

#[test]
fn ten_thousand_ordinary_turns_keep_text_absolute_ids_and_bounded_pages() {
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    for n in 0..10_000 {
        finish(&mut chat, n, false);
        bounded(&chat);
    }
    assert_eq!(chat.rows.len(), 10_000);
    assert_eq!(chat.rows[0].prompt, "prompt 0");
    assert_eq!(chat.rows[0].cards[0].answer, "answer 0");
    assert_eq!(chat.rows[9_999].cards[0].answer, "answer 9999");
    assert!(chat.is_latest_live_page());
    assert!(chat.first_live_page());
    assert_eq!(chat.visible_row_range().start, 0);
    assert!(chat.has_newer_live_page());
    let mut last = 0;
    while chat.newer_live_page() {
        bounded(&chat);
        assert!(chat.visible_row_range().start > last);
        last = chat.visible_row_range().start;
    }
    assert!(chat.is_latest_live_page());
    chat.submit("keep duplicate operation protection").unwrap();
    chat.take_submission().unwrap();
    assert!(
        chat.observe(Observation::Submission {
            id: "operation-0".into()
        })
        .is_err()
    );
    chat.observe(Observation::Submission {
        id: "operation-final".into(),
    })
    .unwrap();
    chat.observe(Observation::Text("active identity survives paging".into()))
        .unwrap();
    chat.first_live_page();
    chat.observe(Observation::Complete).unwrap();
    assert_eq!(chat.rows[10_000].state, ReplyState::Complete);
    assert_eq!(chat.rows[0].prompt, "prompt 0");
}

#[test]
fn hundreds_of_tool_turns_keep_per_turn_ids_and_reveal_selected_old_tool() {
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    for n in 0..256 {
        finish(&mut chat, n, true);
    }
    assert_eq!(chat.rows.len(), 256);
    assert_eq!(chat.rows[0].tools[0].id, chat.rows[255].tools[0].id);
    assert_eq!(chat.rows[0].cards[0].answer, "answer 0");
    bounded(&chat);
    for _ in 0..200 {
        assert!(chat.select_previous_tool());
    }
    let selected = 256 - 200;
    assert!(chat.visible_row_range().contains(&selected));
    bounded(&chat);
    assert!(!chat.is_latest_live_page());
}

#[test]
fn deep_browsing_preserves_draft_and_never_follows_new_output_from_old_page() {
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    for n in 0..150 {
        finish(app.live.as_mut().unwrap(), n, false);
    }
    app.insert_text("retained draft");
    app.transcript_home();
    let range = app.live.as_ref().unwrap().visible_row_range();
    app.max_scroll = 5;
    app.scroll = 5;
    app.observe_live(Observation::Information("background status".into()))
        .unwrap();
    assert_eq!(app.scroll, 5);
    assert_eq!(app.live.as_ref().unwrap().visible_row_range(), range);
    assert_eq!(app.composer.text(), "retained draft");
    app.scroll_down(1);
    assert!(app.live.as_ref().unwrap().visible_row_range().start >= range.end);
    assert_eq!(app.scroll, 0);
    app.transcript_end();
    assert!(app.live.as_ref().unwrap().is_latest_live_page());
    assert_eq!(app.composer.text(), "retained draft");
}

#[test]
fn backward_pages_stop_before_the_page_being_left_with_uneven_row_costs() {
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    for n in 0..6 {
        chat.submit(&format!("row {n}")).unwrap();
        chat.take_submission().unwrap();
        chat.observe(Observation::Text("x".repeat(if n % 2 == 0 {
            15000
        } else {
            2000
        })))
        .unwrap();
        chat.observe(Observation::Complete).unwrap();
    }
    let latest = chat.visible_row_range();
    assert!(chat.older_live_page());
    assert_eq!(chat.visible_row_range().end, latest.start);
    bounded(&chat);
    assert!(chat.newer_live_page());
    assert_eq!(chat.visible_row_range(), latest);
}

#[test]
fn scrolling_inside_full_latest_page_pins_it_before_new_rows_or_cost_growth() {
    let mut app = Demo::live("claude".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    for n in 0..64 {
        finish(app.live.as_mut().unwrap(), n, false);
    }
    app.max_scroll = 100;
    app.scroll = 20;
    let first = app.live.as_ref().unwrap().visible_row_range().start;
    app.observe_live(Observation::Information(
        "pin before subsequent source input".into(),
    ))
    .unwrap();
    finish(app.live.as_mut().unwrap(), 64, false);
    app.observe_live(Observation::Information("later status".into()))
        .unwrap();
    assert_eq!(app.live.as_ref().unwrap().visible_row_range().start, first);
    assert_eq!(app.scroll, 20);
    assert!(app.live.as_ref().unwrap().has_newer_live_page());
}

#[test]
fn active_row_budget_rejects_overflow_without_changing_text_or_previous_rows() {
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    for n in 0..128 {
        finish(&mut chat, n, false);
    }
    chat.submit("q").unwrap();
    let body = "x".repeat(DISPLAY_UNITS - 321);
    chat.observe(Observation::Text(body.clone())).unwrap();
    let cost = chat.rows.last().unwrap().render_units();
    assert_eq!(cost, DISPLAY_UNITS);
    let failure = chat
        .observe(Observation::Text("y".into()))
        .unwrap_err()
        .to_string();
    assert!(failure.contains("row display-work"), "{failure}");
    assert_eq!(chat.rows.last().unwrap().cards[0].answer, body);
    assert_eq!(chat.rows.last().unwrap().render_units(), cost);
    assert_eq!(chat.rows[0].cards[0].answer, "answer 0");
    bounded(&chat);
}

#[test]
fn cleared_rows_stay_hidden_when_selecting_or_revealing_old_tools() {
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    finish(&mut chat, 0, true);
    assert!(chat.select_previous_tool());
    chat.clear_screen().unwrap();
    assert!(!chat.select_previous_tool());
    assert_eq!(chat.visible_row_range(), 1..1);
    finish(&mut chat, 1, true);
    assert!(chat.select_previous_tool());
    assert_eq!(chat.visible_row_range(), 1..2);
    assert!(chat.select_previous_tool());
    chat.reveal_live_row(0);
    assert_eq!(chat.visible_row_range(), 1..2);
    assert_eq!(chat.rows[0].cards[0].answer, "answer 0");
}
