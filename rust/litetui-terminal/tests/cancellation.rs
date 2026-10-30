use litetui_terminal::live::Turn;
use serde_json::{Value, json};
fn accept(turn: &mut Turn, event: Value) -> std::io::Result<litetui_terminal::live::Observation> {
    turn.accept(&event, "claude", "fixture")
}
fn started() -> (Turn, String) {
    let mut turn = Turn::default();
    accept(&mut turn,json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"claude","model":"fixture"})).unwrap();
    let prompt = turn.submit("fixture").unwrap();
    accept(
        &mut turn,
        json!({"type":"response","id":prompt["id"],"ok":true,"result":{"turn":"accepted"}}),
    )
    .unwrap();
    accept(&mut turn, json!({"type":"turn_start","model":"fixture"})).unwrap();
    (turn, prompt["id"].as_str().unwrap().to_owned())
}
#[test]
fn source_cancellation_does_not_destroy_the_session_or_require_answer_text() {
    let (mut turn, _prompt_id) = started();
    assert!(
        accept(
            &mut turn,
            json!({"type":"turn_end","stopReason":"cancelled","error":null})
        )
        .is_ok()
    );
    assert!(turn.submit("explicit follow-up").is_ok());
}

fn ack(command: &Value, stopped: bool) -> Value {
    json!({"type":"response","id":command["id"],"ok":true,"result":{"stopped":stopped,"cancelled_asks":[]}})
}
fn end() -> Value {
    json!({"type":"turn_end","stopReason":"cancelled","error":null})
}
#[test]
fn abort_ack_and_terminal_both_required_in_either_order() {
    use litetui_terminal::live::Observation;
    for terminal_first in [false, true] {
        let (mut turn, _prompt_id) = started();
        let command = turn.abort(&_prompt_id).unwrap().unwrap();
        let (first, last) = if terminal_first {
            (end(), ack(&command, false))
        } else {
            (ack(&command, true), end())
        };
        assert!(matches!(
            accept(&mut turn, first).unwrap(),
            Observation::Information(_)
        ));
        assert!(turn.submit("must wait").is_err());
        assert!(matches!(
            accept(&mut turn, last).unwrap(),
            Observation::Cancelled(_)
        ));
        assert!(turn.submit("explicit next prompt").is_ok());
        assert!(
            turn.abort(&_prompt_id).unwrap().is_none(),
            "stale intent cannot stop new prompt"
        );
        assert!(
            accept(&mut turn, ack(&command, true)).is_err(),
            "old receipt cannot settle a new turn"
        );
    }
}
#[test]
fn force_requires_a_second_request_and_all_receipts_remain_accounted_for() {
    let (mut turn, _prompt_id) = started();
    assert!(turn.abort("wrong-target").unwrap().is_none());
    let first = turn.abort(&_prompt_id).unwrap().unwrap();
    let second = turn.abort(&_prompt_id).unwrap().unwrap();
    assert_ne!(first["id"], second["id"]);
    assert!(turn.abort(&_prompt_id).unwrap().is_none());
    accept(&mut turn, ack(&second, true)).unwrap();
    accept(&mut turn, end()).unwrap();
    assert!(turn.submit("wait").is_err());
    assert!(matches!(
        accept(&mut turn, ack(&first, true)).unwrap(),
        litetui_terminal::live::Observation::Cancelled(_)
    ));
}
#[test]
fn malformed_stop_receipts_do_not_settle_or_extend_authority() {
    for response in [
        json!({"stopped":1,"cancelled_asks":[]}),
        json!({"stopped":true}),
        json!({"stopped":true,"cancelled_asks":[false]}),
    ] {
        let (mut turn, _prompt_id) = started();
        let command = turn.abort(&_prompt_id).unwrap().unwrap();
        let mut event = ack(&command, true);
        event["result"] = response;
        assert!(accept(&mut turn, event).is_err());
        accept(&mut turn, end()).unwrap();
        assert!(turn.submit("wait").is_err());
    }
}
#[test]
fn cancellation_can_precede_input_closure_but_cannot_release_the_view_early() {
    use litetui_terminal::{
        live::{Observation, TerminalOutcome},
        live_chat::Chat,
    };
    for question in [true, false] {
        let (mut turn, _prompt_id) = started();
        let mut chat = Chat::new("claude".into(), "fixture".into());
        chat.observe(Observation::Ready).unwrap();
        chat.submit("fixture").unwrap();
        chat.take_submission();
        let request = if question {
            serde_json::from_str(include_str!("fixtures/question_request.json")).unwrap()
        } else {
            json!({"type":"tool_approval_requested","id":"tool","tool":"write","input":{},"profile":"strict","why":"fixture","timeout_s":null})
        };
        let id = request["id"].clone();
        chat.observe(accept(&mut turn, request).unwrap()).unwrap();
        let command = turn.abort(&_prompt_id).unwrap().unwrap();
        chat.observe(accept(&mut turn, ack(&command, true)).unwrap())
            .unwrap();
        chat.observe(accept(&mut turn, end()).unwrap()).unwrap();
        assert!(chat.busy && turn.submit("wait").is_err());
        let closure = if question {
            json!({"type":"user_input_resolved","id":id,"cancelled":true,"reason":"abort"})
        } else {
            json!({"type":"tool_approval_closed","id":id,"outcome":"denied","reason":"cancelled"})
        };
        let observation = accept(&mut turn, closure).unwrap();
        assert!(matches!(
            &observation,
            Observation::Question {
                settled: Some(TerminalOutcome::Cancelled(_)),
                ..
            } | Observation::ApprovalSettled {
                outcome: TerminalOutcome::Cancelled(_),
                ..
            }
        ));
        chat.observe(observation).unwrap();
        assert!(!chat.busy);
        assert!(turn.submit("explicit follow-up").is_ok());
        chat.submit("explicit follow-up").unwrap();
    }
}
#[test]
fn cancellation_retains_partial_text_waits_for_reservation_and_never_replays() {
    use litetui_terminal::{
        live::Observation,
        live_chat::{Chat, ReplyState},
    };
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("first").unwrap();
    chat.take_submission();
    chat.request_stop().unwrap();
    assert!(chat.cancelling() && chat.take_abort().is_none());
    chat.observe(Observation::Submission {
        id: "reserved-first".into(),
    })
    .unwrap();
    assert_eq!(chat.take_abort().as_deref(), Some("reserved-first"));
    assert!(chat.take_abort().is_none());
    chat.observe(Observation::Text("partial".into())).unwrap();
    assert_eq!(chat.rows[0].state, ReplyState::Cancelling);
    chat.observe(Observation::Cancelled("CancelledError".into()))
        .unwrap();
    assert_eq!(chat.rows[0].cards[0].answer, "partial");
    assert_eq!(chat.rows[0].state, ReplyState::Cancelled);
    assert!(chat.take_abort().is_none() && chat.take_submission().is_none());
    chat.submit("user follow-up").unwrap();
    assert_eq!(chat.take_submission().as_deref(), Some("user follow-up"));
    assert!(chat.take_abort().is_none());
    chat.finish(None);
    assert_eq!(chat.rows[0].state, ReplyState::Cancelled);
}
#[test]
fn unsent_prompt_cancels_locally_without_abort_or_prompt_io() {
    use litetui_terminal::{
        live::Observation,
        live_chat::{Chat, ReplyState},
    };
    let mut chat = Chat::new("claude".into(), "fixture".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("never sent").unwrap();
    chat.request_stop().unwrap();
    assert_eq!(chat.rows[0].state, ReplyState::Cancelled);
    assert!(chat.take_submission().is_none() && chat.take_abort().is_none());
    chat.submit("explicit next").unwrap();
}
#[test]
fn stop_confirmation_is_visible_at_minimum_viewport_and_removed_on_settlement() {
    use litetui_terminal::{demo::Demo, live::Observation, ui};
    use ratatui::{Terminal, backend::TestBackend};
    for (w, h) in [(36, 14), (80, 24)] {
        let mut app = Demo::live("claude".into(), "fixture".into());
        app.observe_live(Observation::Ready).unwrap();
        app.live.as_mut().unwrap().submit("hello").unwrap();
        app.live.as_mut().unwrap().take_submission();
        app.open_stop();
        let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
        terminal.draw(|frame| ui::draw(frame, &mut app)).unwrap();
        let rendered: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect();
        for label in [
            "Stop this turn?",
            "Keep going",
            "Stop turn",
            "Enter confirms",
            "Ctrl+Q quits",
        ] {
            assert!(rendered.contains(label), "{w}x{h}: missing {label}");
        }
        app.observe_live(Observation::Complete).unwrap();
        assert!(!app.stop_open());
    }
}

#[test]
fn natural_completion_winning_a_stop_race_is_still_completion() {
    use litetui_terminal::live::Observation;
    let (mut turn, _prompt_id) = started();
    let command = turn.abort(&_prompt_id).unwrap().unwrap();
    accept(
        &mut turn,
        json!({"type":"text_delta","text":"complete answer"}),
    )
    .unwrap();
    accept(
        &mut turn,
        json!({"type":"turn_end","stopReason":"stop","error":null}),
    )
    .unwrap();
    assert!(turn.submit("wait").is_err());
    assert_eq!(
        accept(&mut turn, ack(&command, false)).unwrap(),
        Observation::Complete
    );
}
#[test]
fn stop_waits_for_the_answer_command_already_sent_before_cancellation() {
    use litetui_terminal::{
        live::{Observation, TerminalOutcome},
        questions::{Action, Decision},
    };
    let (mut turn, _prompt_id) = started();
    let request: Value =
        serde_json::from_str(include_str!("fixtures/question_request.json")).unwrap();
    let id = request["id"].as_str().unwrap().to_owned();
    accept(&mut turn, request).unwrap();
    let answer = turn
        .answer(Decision {
            id: id.clone(),
            action: Action::Cancel,
            answers: vec![],
        })
        .unwrap()
        .unwrap();
    let abort = turn.abort(&_prompt_id).unwrap().unwrap();
    accept(
        &mut turn,
        json!({"type":"user_input_resolved","id":id,"cancelled":true,"reason":"abort"}),
    )
    .unwrap();
    accept(&mut turn, ack(&abort, true)).unwrap();
    accept(&mut turn, end()).unwrap();
    assert!(turn.submit("wait").is_err());
    assert!(matches!(
        accept(
            &mut turn,
            json!({"type":"response","id":answer["id"],"ok":false,"error":"Unknown ask"})
        )
        .unwrap(),
        Observation::Question {
            settled: Some(TerminalOutcome::Cancelled(_)),
            ..
        }
    ));
}
