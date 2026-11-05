//! Native rewrite segments and maintenance retain independent display ownership.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_rewrite as r, history as h, history_view as hv,
    live::{Observation, Turn},
    live_chat::{Chat, ReplyState},
    native_activity::ResultState,
    theme::builtin_themes,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
const FIRST: &str = "42345678-1234-4123-8123-123456789abc";
const SECOND: &str = "52345678-1234-4123-8123-123456789abc";
const CARE: &str = "62345678-1234-4123-8123-123456789abc";
const TOOL: &str = "72345678-1234-4123-8123-123456789abc";
fn review() -> r::Review {
    let selection = r::Selection {
        history_key: hv::ReadKey {
            target: hv::Target::Current,
            generation: 1,
        },
        identity: h::Identity {
            scope: h::Scope::Current,
            from_session: SESSION.into(),
            session: SESSION.into(),
            agent: "22345678-1234-4123-8123-123456789abc".into(),
            backend: "custom".into(),
            model: "test".into(),
            catalog: None,
        },
        history_snapshot: "a".repeat(64),
        context_sha256: "b".repeat(64),
        messages: 3,
        message_index: 1,
        message_sha256: "c".repeat(64),
    };
    r::Review {
        before: r::Snapshot {
            selection,
            token: "d".repeat(64),
            original_text: "original".into(),
            prefix_context_sha256: "e".repeat(64),
            discarded_messages: 1,
            edit: r::Eligibility {
                eligible: true,
                reason: None,
            },
            retry: r::Eligibility {
                eligible: true,
                reason: None,
            },
        },
        action: r::Action::Edit,
        token: "f".repeat(64),
        action_id: ACTION.into(),
        text: "original".into(),
        text_sha256: format!("{:x}", Sha256::digest(b"original")),
        prepared_context_sha256: "1".repeat(64),
        prepared_messages: 2,
        prompt_hooks: false,
        native_rewind: None,
    }
}
fn ready() -> (Turn, String) {
    let mut turn = Turn::default();
    turn.accept(&json!({"type":"ready","protocol_version":1,"launch_status":"ready","backend":"custom","model":"test"}),"custom","test").unwrap();
    let op = turn.submit_rewrite(&review()).unwrap();
    (turn, op)
}
fn event(op: &str, execution: &str, mut value: Value) -> Value {
    value["rewrite_origin"] =
        json!({"session_id":SESSION,"operation_id":op,"source":"rpc","execution_id":execution});
    value
}
fn start(op: &str, execution: &str, index: u64, previous: Option<&str>) -> Value {
    event(
        op,
        execution,
        json!({"type":"turn_start","model":"test","rewrite_segment":{"action_id":ACTION,"index":index,"previous_execution_id":previous}}),
    )
}
fn care(op: &str, phase: &str) -> Value {
    event(
        op,
        CARE,
        json!({"type":"rewrite_aftercare","action_id":ACTION,"aftercare_id":CARE,"phase":phase,"kind":"compaction","after_execution_id":FIRST}),
    )
}
fn ack(op: &str) -> Value {
    let mut receipt = review().request();
    for(k,v) in json!({"kind":"conversation_rewrite_apply","operation_id":op,"turn":"accepted","mutated":true,"close_required":false,"reason":null,
        "persistence":{"snapshot_written":true,"readback_verified":true},"refusal_history":null}).as_object().unwrap(){receipt[k]=v.clone();}
    json!({"type":"response","id":op,"ok":true,"result":receipt})
}
fn accept(turn: &mut Turn, value: Value) -> Observation {
    turn.accept(&value, "custom", "test").unwrap()
}
fn finish_segment(turn: &mut Turn, op: &str, execution: &str, text: &str) -> Observation {
    accept(
        turn,
        event(op, execution, json!({"type":"text_delta","text":text})),
    );
    accept(
        turn,
        event(
            op,
            execution,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
        ),
    )
}
#[test]
fn successive_segments_keep_one_admission_and_support_early_terminal_before_ack() {
    for early in [true, false] {
        let (mut t, op) = ready();
        if early {
            accept(&mut t, ack(&op));
        }
        assert!(matches!(
            accept(&mut t, start(&op, FIRST, 0, None)),
            Observation::RewriteSegment {
                index: 0,
                previous_outcome: None,
                ..
            }
        ));
        let end = finish_segment(&mut t, &op, FIRST, "first answer");
        assert_eq!(matches!(end, Observation::Complete), early);
        let next = accept(&mut t, start(&op, SECOND, 1, Some(FIRST)));
        assert!(matches!(next, Observation::RewriteSegment { index: 1, .. }));
        if let Observation::RewriteSegment {
            previous_outcome, ..
        } = next
        {
            assert_eq!(previous_outcome.is_some(), !early);
        }
        if !early {
            assert!(matches!(
                accept(&mut t, ack(&op)),
                Observation::Information(_)
            ));
        }
        assert!(matches!(
            finish_segment(&mut t, &op, SECOND, "second answer"),
            Observation::Complete
        ));
        assert!(t.submit("too early before verified readback").is_err());
        assert!(t.accept(&ack(&op), "custom", "test").is_err());
    }
}
#[test]
fn segment_metadata_cannot_rebind_action_execution_predecessor_or_operation() {
    for fault in [
        "action",
        "index",
        "previous",
        "reuse",
        "operation",
        "session",
        "source",
        "missing",
        "unfinished",
    ] {
        let (mut t, op) = ready();
        accept(&mut t, ack(&op));
        accept(&mut t, start(&op, FIRST, 0, None));
        if fault != "unfinished" {
            finish_segment(&mut t, &op, FIRST, "answer");
        }
        let mut next = start(&op, SECOND, 1, Some(FIRST));
        match fault {
            "action" => next["rewrite_segment"]["action_id"] = json!(CARE),
            "index" => next["rewrite_segment"]["index"] = json!(3),
            "previous" => next["rewrite_segment"]["previous_execution_id"] = json!(CARE),
            "reuse" => next["rewrite_origin"]["execution_id"] = json!(FIRST),
            "operation" => next["rewrite_origin"]["operation_id"] = json!("another"),
            "session" => next["rewrite_origin"]["session_id"] = json!(CARE),
            "source" => next["rewrite_origin"]["source"] = json!("human"),
            "missing" => {
                next.as_object_mut().unwrap().remove("rewrite_segment");
            }
            _ => {}
        }
        assert!(t.accept(&next, "custom", "test").is_err(), "{fault}");
    }
}
#[test]
fn maintenance_is_exactly_bound_and_can_surround_an_inline_segment() {
    let (mut t, op) = ready();
    accept(&mut t, start(&op, FIRST, 0, None));
    finish_segment(&mut t, &op, FIRST, "first");
    assert!(matches!(
        accept(&mut t, care(&op, "start")),
        Observation::RewriteAftercare {
            running: true,
            previous_outcome: Some(_),
            ..
        }
    ));
    accept(&mut t, ack(&op));
    assert!(t.accept(&care(&op, "start"), "custom", "test").is_err());
    accept(&mut t, start(&op, SECOND, 1, Some(FIRST)));
    finish_segment(&mut t, &op, SECOND, "second");
    let mut wrong = care(&op, "end");
    wrong["after_execution_id"] = json!(SECOND);
    assert!(t.accept(&wrong, "custom", "test").is_err());
    assert!(matches!(
        accept(&mut t, care(&op, "end")),
        Observation::RewriteAftercare { running: false, .. }
    ));
    assert!(t.accept(&care(&op, "end"), "custom", "test").is_err());
}
#[test]
fn initial_start_can_race_stop_but_no_new_descendant_is_admitted_after_stop() {
    let (mut t, op) = ready();
    let abort = t.abort(&op).unwrap().unwrap();
    // A local Stop may race a start already emitted by the source.
    accept(&mut t, start(&op, FIRST, 0, None));
    accept(&mut t, ack(&op));
    accept(
        &mut t,
        event(
            &op,
            FIRST,
            json!({"type":"turn_end","stopReason":"cancelled","error":null}),
        ),
    );
    accept(
        &mut t,
        json!({"type":"response","id":abort["id"],"ok":true,"result":{"stopped":true,"cancelled_asks":[]}}),
    );
    assert!(
        t.accept(&start(&op, SECOND, 1, Some(FIRST)), "custom", "test")
            .is_err()
    );
}
#[test]
fn buffered_rewrite_work_is_consumed_until_first_stop_receipt() {
    for maintenance in [false, true] {
        for stopped in [false, true] {
            let (mut t, op) = ready();
            accept(&mut t, ack(&op));
            accept(&mut t, start(&op, FIRST, 0, None));
            let abort = t.abort(&op).unwrap().unwrap();
            finish_segment(&mut t, &op, FIRST, "already emitted answer");
            // Source output is ordered: these starts may precede its receipt of Stop.
            if maintenance {
                accept(&mut t, care(&op, "start"));
            } else {
                accept(&mut t, start(&op, SECOND, 1, Some(FIRST)));
            }
            // A pending second Stop must not reopen the first acknowledged barrier.
            let second_abort = t.abort(&op).unwrap().unwrap();
            accept(
                &mut t,
                json!({"type":"response","id":abort["id"],"ok":true,
                "result":{"stopped":stopped,"cancelled_asks":[]}}),
            );
            if maintenance {
                assert!(
                    t.accept(&start(&op, SECOND, 1, Some(FIRST)), "custom", "test")
                        .is_err()
                );
                accept(&mut t, care(&op, "end"));
            } else {
                finish_segment(&mut t, &op, SECOND, "already running continuation");
                let mut late = care(&op, "start");
                late["after_execution_id"] = json!(SECOND);
                assert!(t.accept(&late, "custom", "test").is_err());
            }
            // Both receipts remain owned and must be consumed independently.
            accept(
                &mut t,
                json!({"type":"response","id":second_abort["id"],"ok":true,
                "result":{"stopped":false,"cancelled_asks":[]}}),
            );
        }
    }
}
#[test]
fn initial_scheduled_worker_can_report_cancelled_after_stop_receipt() {
    let (mut t, op) = ready();
    let abort = t.abort(&op).unwrap().unwrap();
    accept(
        &mut t,
        json!({"type":"response","id":abort["id"],"ok":true,
        "result":{"stopped":true,"cancelled_asks":[]}}),
    );
    accept(&mut t, ack(&op));
    accept(&mut t, start(&op, FIRST, 0, None));
    accept(
        &mut t,
        event(
            &op,
            FIRST,
            json!({"type":"turn_end","stopReason":"cancelled","error":null}),
        ),
    );
    assert!(t.accept(&care(&op, "start"), "custom", "test").is_err());
    assert!(
        t.accept(&start(&op, SECOND, 1, Some(FIRST)), "custom", "test")
            .is_err()
    );
}
fn draw(chat: &mut Chat) {
    Terminal::new(TestBackend::new(100, 40))
        .unwrap()
        .draw(|f| chat.rewrite.draw(f, f.area(), &builtin_themes().remove(0)))
        .unwrap();
}
fn prepared_chat(op: &str) -> Chat {
    let mut chat = Chat::new("custom".into(), "test".into());
    chat.observe(Observation::Ready).unwrap();
    chat.submit("older live prompt").unwrap();
    chat.take_submission();
    chat.observe(Observation::Text("older live answer".into()))
        .unwrap();
    chat.observe(Observation::Complete).unwrap();
    let review = review();
    chat.rewrite
        .open(r::Action::Edit, review.before.selection.clone())
        .unwrap();
    let read = chat.rewrite.take_intent().unwrap();
    chat.observe(Observation::Rewrite(r::Update::Snapshot {
        key: read.key().clone(),
        snapshot: Box::new(review.before.clone()),
    }))
    .unwrap();
    draw(&mut chat);
    chat.rewrite
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let validate = chat.rewrite.take_intent().unwrap();
    chat.observe(Observation::Rewrite(r::Update::Review {
        key: validate.key().clone(),
        review: Box::new(review),
    }))
    .unwrap();
    draw(&mut chat);
    chat.rewrite
        .key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE));
    let apply = chat.rewrite.take_intent().unwrap();
    chat.prepare_rewrite(&apply).unwrap();
    chat.observe(Observation::Submission { id: op.into() })
        .unwrap();
    chat.observe(Observation::Rewrite(r::Update::Submitted {
        key: apply.key().clone(),
    }))
    .unwrap();
    chat
}
fn relay(turn: &mut Turn, chat: &mut Chat, value: Value) {
    let observation = accept(turn, value);
    chat.observe(observation).unwrap();
}
#[test]
fn buffered_start_retires_prior_display_row_without_retiring_stop_receipts() {
    for maintenance in [false, true] {
        let (mut t, op) = ready();
        let mut chat = prepared_chat(&op);
        relay(&mut t, &mut chat, ack(&op));
        relay(&mut t, &mut chat, start(&op, FIRST, 0, None));
        relay(
            &mut t,
            &mut chat,
            event(
                &op,
                FIRST,
                json!({"type":"text_delta","text":"finished before Stop"}),
            ),
        );
        chat.request_stop().unwrap();
        assert_eq!(chat.take_abort().as_deref(), Some(op.as_str()));
        let abort = t.abort(&op).unwrap().unwrap();
        relay(
            &mut t,
            &mut chat,
            event(
                &op,
                FIRST,
                json!({"type":"turn_end","stopReason":"stop","error":null}),
            ),
        );
        assert_eq!(chat.rows[1].state, ReplyState::Cancelling);
        relay(
            &mut t,
            &mut chat,
            if maintenance {
                care(&op, "start")
            } else {
                start(&op, SECOND, 1, Some(FIRST))
            },
        );
        assert_eq!(chat.rows[1].state, ReplyState::Complete);
        assert_eq!(chat.rows.len(), 3);
        assert!(chat.rewrite_pending());
        assert!(chat.submit("still blocked").is_err());
        relay(
            &mut t,
            &mut chat,
            json!({"type":"response","id":abort["id"],"ok":true,
            "result":{"stopped":true,"cancelled_asks":[]}}),
        );
        assert!(chat.rewrite_pending());
        if maintenance {
            relay(&mut t, &mut chat, care(&op, "end"));
            assert_eq!(chat.rows[2].state, ReplyState::MaintenanceSettled);
        } else {
            relay(
                &mut t,
                &mut chat,
                event(
                    &op,
                    SECOND,
                    json!({"type":"turn_end","stopReason":"cancelled","error":null}),
                ),
            );
            assert_eq!(chat.rows[2].state, ReplyState::Cancelled);
        }
        assert!(chat.rewrite_pending());
        assert!(chat.submit("readback still pending").is_err());
    }
}
#[test]
fn inline_stream_and_compaction_keep_distinct_rows_and_source_status() {
    let (mut t, op) = ready();
    let mut chat = prepared_chat(&op);
    relay(&mut t, &mut chat, start(&op, FIRST, 0, None));
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            FIRST,
            json!({"type":"text_delta","text":"first answer"}),
        ),
    );
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            FIRST,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
        ),
    );
    relay(&mut t, &mut chat, care(&op, "start"));
    relay(&mut t, &mut chat, ack(&op));
    assert!(chat.busy);
    assert!(chat.rewrite_pending());
    assert!(!chat.rewrite.owns_input());
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            CARE,
            json!({"type":"tool_call","name":"read","args":{"path":"observed-only"}}),
        ),
    );
    relay(&mut t, &mut chat, start(&op, SECOND, 1, Some(FIRST)));
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            SECOND,
            json!({"type":"text_delta","text":"second answer"}),
        ),
    );
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            SECOND,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
        ),
    );
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            CARE,
            json!({"type":"compaction","reason":"compacted","tokens_before":100,"tokens_after":40,
        "tokens_before_exact":true,"tokens_after_exact":false,"messages_dropped":2,"messages_summarised":3,"kept_recent":4}),
        ),
    );
    relay(&mut t, &mut chat, care(&op, "end"));
    assert_eq!(chat.rows.len(), 4);
    assert_eq!(chat.rows[0].cards[0].answer, "older live answer");
    assert_eq!(chat.rows[1].cards[0].answer, "first answer");
    assert_eq!(chat.rows[3].cards[0].answer, "second answer");
    assert_eq!(chat.rows[2].state, ReplyState::MaintenanceSettled);
    assert!(
        chat.rows[2].cards[0]
            .answer
            .contains("after: 40 (exact: false)")
    );
    assert_eq!(chat.rows[2].tools[0].name.as_deref(), Some("read"));
    assert_eq!(chat.rows[2].tools[0].result, ResultState::Incomplete);
    assert!(!chat.busy);
    assert!(chat.rewrite_pending());
    assert!(chat.submit("still forbidden").is_err());
}
#[test]
fn maintenance_approvals_and_stop_keep_exact_apply_ownership() {
    let (mut t, op) = ready();
    let mut chat = prepared_chat(&op);
    relay(&mut t, &mut chat, ack(&op));
    relay(&mut t, &mut chat, start(&op, FIRST, 0, None));
    relay(
        &mut t,
        &mut chat,
        event(&op, FIRST, json!({"type":"text_delta","text":"answer"})),
    );
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            FIRST,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
        ),
    );
    relay(&mut t, &mut chat, care(&op, "start"));
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            CARE,
            json!({"type":"tool_approval_requested","id":"compact-approval","tool":"write","input":{"path":"not-executed"},"profile":"strict","why":"write","timeout_s":30.0}),
        ),
    );
    assert!(!chat.approvals.is_empty());
    chat.request_stop().unwrap();
    assert_eq!(chat.take_abort().as_deref(), Some(op.as_str()));
    assert_eq!(chat.rows[1].state, ReplyState::Complete);
    assert_eq!(chat.rows[2].state, ReplyState::Maintenance);
    assert!(t.abort(&op).unwrap().is_some());
    assert!(t.abort("another operation").unwrap().is_none());
    assert!(
        t.accept(&start(&op, SECOND, 1, Some(FIRST)), "custom", "test")
            .is_err()
    );
}
#[test]
fn generic_tools_require_pairs_and_cannot_jump_between_worker_rows() {
    let (mut t, op) = ready();
    accept(&mut t, ack(&op));
    accept(&mut t, start(&op, FIRST, 0, None));
    finish_segment(&mut t, &op, FIRST, "answer");
    accept(&mut t, care(&op, "start"));
    let request = event(
        &op,
        CARE,
        json!({"type":"generic_tool_requested","observation_id":TOOL,"round":1,"slot":0,"provider_call_id":"p","name":"read"}),
    );
    assert!(matches!(
        accept(&mut t, request.clone()),
        Observation::RewriteActivity { .. }
    ));
    assert!(t.accept(&request, "custom", "test").is_err());
    let returned = event(
        &op,
        CARE,
        json!({"type":"generic_tool_returned","observation_id":TOOL,"ok":false,"result":"denied"}),
    );
    let mut wrong = returned.clone();
    wrong["rewrite_origin"]["execution_id"] = json!(FIRST);
    assert!(t.accept(&wrong, "custom", "test").is_err());
    assert!(matches!(
        accept(&mut t, returned.clone()),
        Observation::RewriteActivity { .. }
    ));
    assert!(t.accept(&returned, "custom", "test").is_err());
}
#[test]
fn malformed_compaction_status_does_not_become_a_success_notice() {
    for (field, bad) in [
        ("tokens_after", json!(-1)),
        ("tokens_before_exact", Value::Null),
        ("messages_dropped", json!(1.2)),
        ("reason", json!("unknown")),
    ] {
        let (mut t, op) = ready();
        accept(&mut t, ack(&op));
        accept(&mut t, start(&op, FIRST, 0, None));
        finish_segment(&mut t, &op, FIRST, "answer");
        accept(&mut t, care(&op, "start"));
        let mut status = event(
            &op,
            CARE,
            json!({"type":"compaction","reason":"compacted","tokens_before":100,"tokens_after":50,"tokens_before_exact":true,"tokens_after_exact":false,"messages_dropped":2,"messages_summarised":3,"kept_recent":4}),
        );
        status[field] = bad;
        assert!(t.accept(&status, "custom", "test").is_err(), "{field}");
    }
}
#[test]
fn cumulative_display_budget_spans_all_followup_rows() {
    let (mut t, op) = ready();
    let mut chat = prepared_chat(&op);
    relay(&mut t, &mut chat, ack(&op));
    relay(&mut t, &mut chat, start(&op, FIRST, 0, None));
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            FIRST,
            json!({"type":"text_delta","text":"x".repeat(20_000)}),
        ),
    );
    relay(
        &mut t,
        &mut chat,
        event(
            &op,
            FIRST,
            json!({"type":"turn_end","stopReason":"stop","error":null}),
        ),
    );
    relay(&mut t, &mut chat, start(&op, SECOND, 1, Some(FIRST)));
    let next = accept(
        &mut t,
        event(
            &op,
            SECOND,
            json!({"type":"text_delta","text":"y".repeat(20_000)}),
        ),
    );
    assert!(chat.observe(next).is_err());
    assert_eq!(chat.rows[1].cards[0].answer.len(), 20_000);
    assert!(chat.rows[2].cards[0].answer.is_empty());
}

#[test]
fn deferred_compaction_notice_binds_last_stream_without_creating_worker_authority() {
    let (mut t, op) = ready();
    accept(&mut t, ack(&op));
    accept(&mut t, start(&op, FIRST, 0, None));
    finish_segment(&mut t, &op, FIRST, "answer");
    let mut status = event(
        &op,
        FIRST,
        json!({"type":"compaction","reason":"deferred_busy","tokens_before":null,"tokens_after":null,"tokens_before_exact":false,"tokens_after_exact":false,"messages_dropped":0,"messages_summarised":0,"kept_recent":0}),
    );
    assert!(matches!(
        accept(&mut t, status.clone()),
        Observation::Information(_)
    ));
    status["reason"] = json!("compacted");
    assert!(t.accept(&status, "custom", "test").is_err());
    status["reason"] = json!("deferred_busy");
    status["rewrite_origin"]["execution_id"] = json!(SECOND);
    assert!(t.accept(&status, "custom", "test").is_err());
    assert!(t.abort(&op).unwrap().is_none());
}

#[test]
fn cancelled_compaction_is_a_retained_status_without_success_or_new_stream() {
    let (mut t, op) = ready();
    accept(&mut t, ack(&op));
    accept(&mut t, start(&op, FIRST, 0, None));
    finish_segment(&mut t, &op, FIRST, "answer");
    accept(&mut t, care(&op, "start"));
    let status = event(
        &op,
        CARE,
        json!({"type":"compaction","reason":"cancelled","tokens_before":100,"tokens_after":null,"tokens_before_exact":true,"tokens_after_exact":false,"messages_dropped":0,"messages_summarised":0,"kept_recent":0}),
    );
    let Observation::RewriteStatus { execution_id, text } = accept(&mut t, status) else {
        panic!("Expected retained maintenance status")
    };
    assert_eq!(execution_id, CARE);
    assert!(text.contains("Compaction: cancelled."));
    assert!(text.contains("dropped: 0"));
}
