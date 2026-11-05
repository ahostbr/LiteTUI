//! Source-owned Work admission remains distinct from stream endings and retirement receipts.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_rewrite as r,
    demo::Demo,
    history as h, history_view as hv,
    live::{Observation, TerminalOutcome, WorkExecution},
    live_chat::{Chat, Item, ReplyState},
    theme::builtin_themes,
    tool_mode::Profile,
    ui,
    work_lifecycle::{Admission, Input, Mode, Origin, Payload, Provider},
};
use ratatui::{Terminal, backend::TestBackend};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
fn ready() -> Chat {
    let mut chat = Chat::new("custom".into(), "test".into());
    chat.observe(Observation::Ready).unwrap();
    chat
}
fn work(update: WorkExecution) -> Observation {
    Observation::WorkExecution(Box::new(update))
}
fn admitted(n: usize, operation: &str, mode: Mode, text: &str) -> WorkExecution {
    WorkExecution::Admitted {
        input: Input {
            id: format!("input-{n}"),
            producer: format!("producer-{n}"),
            payload_sha256: "a".repeat(64),
            source: "task_result".into(),
            requested: Profile::Autonomous,
            session: SESSION.into(),
            revision: n as u64,
        },
        admission: Admission {
            id: format!("admission-{n}"),
            input: format!("input-{n}"),
            mode,
            operation: operation.into(),
            origin: Origin {
                session: SESSION.into(),
                operation: operation.into(),
                hook: None,
                source: "task_result".into(),
                execution: format!("execution-{n}"),
            },
            parent: (mode != Mode::NewTurn).then(|| "parent".into()),
            boundary: u64::from(mode == Mode::InTurn),
            requested: Profile::Autonomous,
            effective: Profile::Strict,
            seat: Profile::Strict,
            provider: Provider::Host,
            delivery: None,
        },
        payload: Payload {
            delivery: litetui_terminal::work_lifecycle::Delivery::Append,
            content: "hidden provider context is not the display text".into(),
            text: Some(text.into()),
            source: "task_result".into(),
            profile: Profile::Autonomous,
        },
        suspended_submission: None,
    }
}
fn end(chat: &mut Chat, operation: &str) {
    chat.observe(work(WorkExecution::Ended {
        operation: operation.into(),
        outcome: TerminalOutcome::Complete,
    }))
    .unwrap();
}
fn retire(chat: &mut Chat, n: usize) {
    chat.observe(work(WorkExecution::Retired {
        admission_id: format!("admission-{n}"),
    }))
    .unwrap();
}
fn settle(chat: &mut Chat, operation: &str) {
    chat.observe(work(WorkExecution::Settled {
        operation: operation.into(),
        outcome: TerminalOutcome::Complete,
    }))
    .unwrap();
}
fn screen(app: &mut Demo) -> String {
    let mut terminal = Terminal::new(TestBackend::new(140, 65)).unwrap();
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
fn exact_new_turn_text_profile_and_source_are_visible_without_touching_draft() {
    let mut app = Demo::live("custom".into(), "test".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("my unfinished draft");
    let text = "exact source text\nsecond line \0\u{1b}\u{202e}";
    app.observe_live(work(admitted(1, "work-1", Mode::NewTurn, text)))
        .unwrap();
    assert_eq!(app.composer.text(), "my unfinished draft");
    let chat = app.live.as_ref().unwrap();
    assert_eq!(chat.rows[0].prompt, text);
    assert!(chat.rows[0].source_owned);
    assert_eq!(
        chat.rows[0].source.as_ref().unwrap().effective,
        Profile::Strict
    );
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    let rendered = screen(&mut app);
    for expected in [
        "exact source text",
        "task_result",
        "requested autonomous",
        "effective strict",
    ] {
        assert!(rendered.contains(expected), "{expected}");
    }
    assert!(!rendered.contains("hidden provider context"));
    assert!(!rendered.contains('\0'));
    assert!(!rendered.contains('\u{1b}'));
    assert!(!rendered.contains('\u{202e}'));
}
#[test]
fn source_new_turn_preserves_open_work_and_other_modal_state() {
    for work_open in [false, true] {
        let mut app = Demo::live("custom".into(), "test".into());
        app.observe_live(Observation::Ready).unwrap();
        app.insert_text("draft");
        if work_open {
            app.open_work();
        } else {
            app.live.as_mut().unwrap().themes.open();
        }
        let generation = app.live.as_ref().unwrap().work.pending_generation();
        app.observe_live(work(admitted(1, "work", Mode::NewTurn, "background")))
            .unwrap();
        let chat = app.live.as_ref().unwrap();
        assert_eq!(chat.work.pending_generation(), generation);
        assert_eq!(chat.work.owns_input(), work_open);
        assert_eq!(chat.themes.owns_input(), !work_open);
        assert_eq!(app.composer.text(), "draft");
    }
}
#[test]
fn ending_waits_for_all_exact_retirements_and_old_receipts_do_not_settle_new_turn() {
    let mut chat = ready();
    chat.observe(work(admitted(1, "old", Mode::NewTurn, "first")))
        .unwrap();
    chat.observe(Observation::Text("old answer".into()))
        .unwrap();
    chat.observe(work(admitted(2, "old", Mode::InTurn, "in-turn")))
        .unwrap();
    end(&mut chat, "old");
    assert_eq!(chat.rows[0].state, ReplyState::AwaitingRetirement);
    assert!(chat.submit("too early").is_err());
    assert!(chat.observe(Observation::Text("late".into())).is_err());
    chat.observe(work(admitted(3, "new", Mode::NewTurn, "second")))
        .unwrap();
    chat.observe(Observation::Text("new answer".into()))
        .unwrap();
    retire(&mut chat, 2);
    assert!(
        chat.observe(work(WorkExecution::Settled {
            operation: "old".into(),
            outcome: TerminalOutcome::Complete
        }))
        .is_err()
    );
    assert_eq!(chat.rows[1].state, ReplyState::Streaming);
    retire(&mut chat, 1);
    settle(&mut chat, "old");
    assert_eq!(chat.rows[0].state, ReplyState::Complete);
    assert_eq!(chat.rows[1].state, ReplyState::Streaming);
    assert!(chat.busy);
    chat.observe(Observation::Text(" continued".into()))
        .unwrap();
    assert_eq!(chat.rows[1].cards[0].answer, "new answer continued");
    end(&mut chat, "new");
    retire(&mut chat, 3);
    settle(&mut chat, "new");
    assert!(!chat.busy);
    assert_eq!(chat.rows[1].state, ReplyState::Complete);
    chat.submit("next user prompt").unwrap();
}
#[test]
fn current_settles_before_old_receipt_without_unblocking_user_submission() {
    let mut chat = ready();
    chat.observe(work(admitted(1, "old", Mode::NewTurn, "old")))
        .unwrap();
    end(&mut chat, "old");
    chat.observe(work(admitted(2, "new", Mode::NewTurn, "new")))
        .unwrap();
    end(&mut chat, "new");
    retire(&mut chat, 2);
    settle(&mut chat, "new");
    assert!(chat.busy);
    assert!(chat.submit("still old retirement").is_err());
    retire(&mut chat, 1);
    settle(&mut chat, "old");
    assert!(!chat.busy);
}
#[test]
fn in_turn_input_is_a_distinct_exact_display_item_on_current_operation() {
    let mut app = Demo::live("custom".into(), "test".into());
    app.observe_live(Observation::Ready).unwrap();
    app.live.as_mut().unwrap().submit("local question").unwrap();
    app.live.as_mut().unwrap().take_submission();
    app.observe_live(Observation::Submission { id: "local".into() })
        .unwrap();
    app.observe_live(Observation::Text("before input".into()))
        .unwrap();
    app.observe_live(work(admitted(
        1,
        "local",
        Mode::InTurn,
        "source boundary input",
    )))
    .unwrap();
    let chat = app.live.as_ref().unwrap();
    assert_eq!(chat.rows.len(), 1);
    assert_eq!(chat.rows[0].items, [Item::Card(0), Item::WorkInput(0)]);
    assert_eq!(chat.rows[0].inputs[0].text, "source boundary input");
    let rendered = screen(&mut app);
    assert!(rendered.contains("source boundary input"));
    assert!(rendered.contains("task_result"));
    assert!(
        app.observe_live(work(admitted(2, "wrong", Mode::InTurn, "bad")))
            .is_err()
    );
    let chat = app.live.as_mut().unwrap();
    assert!(chat.observe(Observation::Complete).is_err());
    retire(chat, 1); // independent readback may precede the stream terminal
    chat.observe(Observation::Complete).unwrap();
    assert_eq!(chat.rows[0].state, ReplyState::Complete);
}
#[test]
fn unknown_duplicate_and_mismatched_work_receipts_fail_without_row_fallback() {
    let mut chat = ready();
    chat.observe(work(admitted(1, "one", Mode::NewTurn, "one")))
        .unwrap();
    assert!(
        chat.observe(work(admitted(1, "one", Mode::InTurn, "duplicate")))
            .is_err()
    );
    assert!(
        chat.observe(work(WorkExecution::Retired {
            admission_id: "unknown".into()
        }))
        .is_err()
    );
    assert!(
        chat.observe(work(WorkExecution::Settled {
            operation: "unknown".into(),
            outcome: TerminalOutcome::Complete
        }))
        .is_err()
    );
    end(&mut chat, "one");
    retire(&mut chat, 1);
    assert!(
        chat.observe(work(WorkExecution::Retired {
            admission_id: "admission-1".into()
        }))
        .is_err()
    );
    assert!(
        chat.observe(work(WorkExecution::Settled {
            operation: "one".into(),
            outcome: TerminalOutcome::Failed("different".into())
        }))
        .is_err()
    );
    settle(&mut chat, "one");
    assert!(
        chat.observe(work(WorkExecution::Settled {
            operation: "one".into(),
            outcome: TerminalOutcome::Complete
        }))
        .is_err()
    );
    assert_eq!(chat.rows[0].state, ReplyState::Complete);
}
#[test]
fn local_prompt_race_preserves_exact_parked_row_in_all_three_dispatch_phases() {
    for phase in ["unsent", "forwarded", "reserved"] {
        let mut chat = ready();
        chat.submit("losing local prompt").unwrap();
        if phase != "unsent" {
            chat.take_submission();
        }
        if phase == "reserved" {
            chat.observe(Observation::Submission { id: "local".into() })
                .unwrap();
        }
        let mut update = admitted(1, "source", Mode::NewTurn, "winning source input");
        if phase == "reserved"
            && let WorkExecution::Admitted {
                suspended_submission,
                ..
            } = &mut update
        {
            *suspended_submission = Some("local".into());
        }
        chat.observe(work(update)).unwrap();
        assert!(chat.take_submission().is_none());
        chat.observe(Observation::Text("source reply".into()))
            .unwrap();
        if phase == "forwarded" {
            chat.observe(work(WorkExecution::LocalNotSent {
                reason: "not sent by supervisor".into(),
            }))
            .unwrap();
        }
        if phase == "reserved" {
            assert!(
                chat.observe(work(WorkExecution::LocalRefused {
                    operation: "other".into(),
                    reason: "wrong".into()
                }))
                .is_err()
            );
            chat.observe(work(WorkExecution::LocalRefused {
                operation: "local".into(),
                reason: "source busy".into(),
            }))
            .unwrap();
        }
        assert_eq!(chat.rows[0].prompt, "losing local prompt");
        assert_eq!(chat.rows[0].state, ReplyState::Refused);
        assert_eq!(chat.rows[1].state, ReplyState::Streaming);
        assert_eq!(chat.rows[1].cards[0].answer, "source reply");
        assert!(chat.busy);
    }
}
#[test]
fn uncertainty_blocks_new_submission_but_work_and_quit_remain_reachable() {
    let mut app = Demo::live("custom".into(), "test".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("keep draft");
    app.observe_live(work(WorkExecution::Uncertain {
        input_id: "uncertain-input".into(),
        reason: "delivery not proven".into(),
    }))
    .unwrap();
    app.submit();
    assert_eq!(app.composer.text(), "keep draft");
    assert!(app.live.as_ref().unwrap().rows.is_empty());
    app.open_work();
    assert!(app.live.as_ref().unwrap().work.owns_input());
    app.live.as_mut().unwrap().begin_quit();
    assert!(app.live.as_ref().unwrap().quitting);
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
}
#[test]
fn exact_empty_display_and_absent_display_are_not_replaced_with_invented_text() {
    let mut chat = ready();
    chat.observe(work(admitted(1, "empty", Mode::NewTurn, "")))
        .unwrap();
    assert_eq!(chat.rows[0].prompt, "");
    end(&mut chat, "empty");
    retire(&mut chat, 1);
    settle(&mut chat, "empty");
    let mut update = admitted(2, "content", Mode::NewTurn, "unused");
    if let WorkExecution::Admitted { payload, .. } = &mut update {
        payload.text = None;
        payload.content = "exact fallback content".into();
    }
    chat.observe(work(update)).unwrap();
    assert_eq!(chat.rows[1].prompt, "exact fallback content");
}
#[test]
fn source_display_and_held_action_payloads_pay_same_bounded_budget() {
    for text in [
        "x".repeat(32769),
        "\n".repeat(1100),
        "\u{202e}".repeat(3000),
    ] {
        let mut chat = ready();
        assert!(
            chat.observe(work(admitted(1, "too-large", Mode::NewTurn, &text)))
                .is_err()
        );
        assert!(chat.rows.is_empty());
    }
    let mut chat = ready();
    chat.observe(work(admitted(1, "many", Mode::NewTurn, "start")))
        .unwrap();
    let mut rejected = false;
    for n in 2..=102 {
        if chat
            .observe(work(admitted(n, "many", Mode::InTurn, &"x".repeat(500))))
            .is_err()
        {
            rejected = true;
            break;
        }
    }
    assert!(rejected);
    assert!(chat.rows[0].inputs.len() < 100);
    let mut chat = prepared_chat("action");
    assert!(
        chat.observe(work(admitted(
            1,
            "action",
            Mode::ActionSegment,
            &"\n".repeat(1100)
        )))
        .is_err()
    );
    assert_eq!(chat.rows.len(), 2);
}
#[test]
fn action_segment_consumes_only_matching_execution_payload() {
    let mut chat = prepared_chat("action");
    chat.observe(Observation::RewriteSegment {
        operation_id: "action".into(),
        execution_id: "initial".into(),
        index: 0,
        previous_outcome: None,
    })
    .unwrap();
    chat.observe(Observation::Text("first answer".into()))
        .unwrap();
    chat.observe(Observation::Complete).unwrap();
    chat.observe(work(admitted(
        1,
        "action",
        Mode::ActionSegment,
        "exact continuation from source",
    )))
    .unwrap();
    assert_eq!(chat.rows.len(), 2);
    chat.observe(Observation::RewriteSegment {
        operation_id: "action".into(),
        execution_id: "execution-1".into(),
        index: 1,
        previous_outcome: None,
    })
    .unwrap();
    assert_eq!(chat.rows.len(), 3);
    assert_eq!(chat.rows[2].prompt, "exact continuation from source");
    assert_eq!(chat.rows[2].source.as_ref().unwrap().source, "task_result");
    assert_eq!(chat.rows[1].prompt, "original");
    chat.observe(Observation::Text("continuation answer".into()))
        .unwrap();
    assert_eq!(chat.rows[2].cards[0].answer, "continuation answer");
    assert!(
        chat.observe(work(admitted(
            2,
            "action",
            Mode::ActionSegment,
            "wrong repeated execution"
        )))
        .is_ok()
    );
    assert_eq!(chat.rows.len(), 3); // another admitted payload is held, never replayed into current row
}

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

#[test]
fn ordinary_operation_ending_before_source_new_turn_keeps_exact_old_row() {
    let mut chat = ready();
    chat.submit("ordinary local input").unwrap();
    chat.take_submission();
    chat.observe(Observation::Submission {
        id: "ordinary".into(),
    })
    .unwrap();
    chat.observe(Observation::Text("ordinary reply".into()))
        .unwrap();
    end(&mut chat, "ordinary");
    assert_eq!(chat.rows[0].state, ReplyState::AwaitingRetirement);
    chat.observe(work(admitted(
        1,
        "source",
        Mode::NewTurn,
        "new source input",
    )))
    .unwrap();
    chat.observe(Observation::Text("new source reply".into()))
        .unwrap();
    settle(&mut chat, "ordinary");
    assert_eq!(chat.rows[0].state, ReplyState::Complete);
    assert_eq!(chat.rows[1].state, ReplyState::Streaming);
    assert_eq!(chat.rows[1].cards[0].answer, "new source reply");
    assert!(chat.busy);
}
#[test]
fn late_refusal_and_close_do_not_relabel_a_prompt_as_executed() {
    let mut chat = ready();
    chat.submit("not submitted").unwrap();
    chat.observe(work(admitted(1, "source", Mode::NewTurn, "source")))
        .unwrap();
    chat.finish(None);
    assert_eq!(chat.rows[0].state, ReplyState::Refused);
    assert_eq!(chat.rows[1].state, ReplyState::Incomplete);
}
#[test]
fn uncertain_delivery_survives_late_terminal_and_information_notices() {
    let mut chat = ready();
    chat.submit("ordinary").unwrap();
    chat.take_submission();
    chat.observe(Observation::Submission {
        id: "ordinary".into(),
    })
    .unwrap();
    chat.observe(work(WorkExecution::Uncertain {
        input_id: "unproven".into(),
        reason: "source delivery not proven".into(),
    }))
    .unwrap();
    chat.observe(Observation::Complete).unwrap();
    chat.observe(Observation::Information("a later source notice".into()))
        .unwrap();
    assert!(chat.busy);
    assert!(chat.notice.contains("source delivery not proven"));
    assert!(chat.submit("do not send").is_err());
}

#[test]
fn independently_retired_current_work_accepts_later_exact_ordinary_terminal() {
    for failure in [false, true] {
        let mut chat = ready();
        chat.observe(work(admitted(1, "current", Mode::NewTurn, "source")))
            .unwrap();
        let outcome = if failure {
            TerminalOutcome::Failed("source failure".into())
        } else {
            TerminalOutcome::Complete
        };
        chat.observe(work(WorkExecution::Ended {
            operation: "current".into(),
            outcome,
        }))
        .unwrap();
        assert!(chat.observe(Observation::Complete).is_err());
        retire(&mut chat, 1);
        assert_eq!(chat.rows[0].state, ReplyState::AwaitingRetirement);
        assert!(chat.busy);
        if failure {
            assert!(chat.observe(Observation::Complete).is_err());
            chat.observe(Observation::Failed("source failure".into()))
                .unwrap();
            assert_eq!(chat.rows[0].state, ReplyState::Failed);
        } else {
            chat.observe(Observation::Complete).unwrap();
            assert_eq!(chat.rows[0].state, ReplyState::Complete);
        }
        assert!(!chat.busy);
        chat.submit("next local prompt").unwrap();
    }
}

#[test]
fn uncertain_input_parks_only_the_exact_unsent_or_forwarded_local_prompt() {
    for phase in ["unsent", "forwarded", "reserved"] {
        let mut chat = ready();
        chat.submit("losing local prompt").unwrap();
        if phase != "unsent" {
            chat.take_submission();
        }
        if phase == "reserved" {
            chat.observe(Observation::Submission { id: "local".into() })
                .unwrap();
        }
        chat.observe(work(WorkExecution::Uncertain {
            input_id: "unproven".into(),
            reason: "cannot prove delivery".into(),
        }))
        .unwrap();
        assert!(chat.take_submission().is_none());
        if phase == "forwarded" {
            chat.observe(work(WorkExecution::LocalNotSent {
                reason: "not sent by supervisor".into(),
            }))
            .unwrap();
        } else if phase == "reserved" {
            // The already reserved operation uses its ordinary correlated refusal.
            assert!(
                chat.observe(work(WorkExecution::LocalNotSent {
                    reason: "wrong unreserved refusal".into()
                }))
                .is_err()
            );
            chat.observe(Observation::Refused("source busy".into()))
                .unwrap();
        }
        assert_eq!(chat.rows.len(), 1);
        assert_eq!(chat.rows[0].prompt, "losing local prompt");
        assert_eq!(chat.rows[0].state, ReplyState::Refused);
        assert!(chat.busy);
        assert!(chat.notice.contains("cannot prove delivery"));
        assert!(chat.submit("no retry").is_err());
        chat.work.open();
        assert!(chat.work.owns_input());
        chat.begin_quit();
        assert!(chat.quitting);
    }
}

#[test]
fn failed_action_before_start_displays_exact_payload_only_after_verified_retirement() {
    for failed in [false, true] {
        let mut chat = prepared_chat("action");
        chat.observe(Observation::RewriteSegment {
            operation_id: "action".into(),
            execution_id: "initial".into(),
            index: 0,
            previous_outcome: None,
        })
        .unwrap();
        chat.observe(Observation::Text("real first answer".into()))
            .unwrap();
        chat.observe(Observation::Complete).unwrap();
        chat.observe(work(admitted(
            1,
            "action",
            Mode::ActionSegment,
            "exact input that never streamed",
        )))
        .unwrap();
        chat.observe(work(WorkExecution::Uncertain {
            input_id: "input-1".into(),
            reason: "worker never started".into(),
        }))
        .unwrap();
        let outcome = if failed {
            TerminalOutcome::Failed("source worker failed before start".into())
        } else {
            TerminalOutcome::Cancelled("source worker cancelled before start".into())
        };
        let not_started = || {
            work(WorkExecution::NotStarted {
                operation: "action".into(),
                execution: "execution-1".into(),
                outcome: outcome.clone(),
            })
        };
        assert!(chat.observe(not_started()).is_err());
        assert_eq!(chat.rows.len(), 2);
        retire(&mut chat, 1);
        assert!(
            chat.observe(work(WorkExecution::NotStarted {
                operation: "action".into(),
                execution: "wrong".into(),
                outcome: outcome.clone()
            }))
            .is_err()
        );
        assert!(
            chat.observe(work(WorkExecution::NotStarted {
                operation: "wrong".into(),
                execution: "execution-1".into(),
                outcome: outcome.clone()
            }))
            .is_err()
        );
        assert!(
            chat.observe(work(WorkExecution::NotStarted {
                operation: "action".into(),
                execution: "execution-1".into(),
                outcome: TerminalOutcome::Complete
            }))
            .is_err()
        );
        chat.observe(not_started()).unwrap();
        assert_eq!(chat.rows[2].prompt, "exact input that never streamed");
        assert_eq!(
            chat.rows[2].state,
            if failed {
                ReplyState::Failed
            } else {
                ReplyState::Cancelled
            }
        );
        assert!(chat.rows[2].cards.is_empty());
        assert!(chat.rows[2].items.is_empty());
        assert_eq!(chat.rows[1].cards[0].answer, "real first answer");
        assert!(chat.busy);
        assert!(chat.observe(not_started()).is_err());
        let mut app = Demo::live("custom".into(), "test".into());
        app.live = Some(chat);
        let rendered = screen(&mut app);
        assert!(rendered.contains("exact input that never streamed"));
        assert!(rendered.contains(if failed {
            "source worker failed"
        } else {
            "source worker cancelled"
        }));
    }
}

fn changed_action(chat: &Chat) -> litetui_terminal::management::RewriteChanged {
    let review = review();
    let snapshot = h::Snapshot {
        open_effects: None,
        open_action_id: None,
        read_only: false,
        identity: review.before.selection.identity.clone(),
        token: "4".repeat(64),
        transcript_revision: Some("5".repeat(64)),
        context_sha256: "6".repeat(64),
        replay_sha256: "7".repeat(64),
        pending: false,
        materialized: true,
        fresh_system_context: false,
        title: "verified context".into(),
        counts: h::Counts {
            messages: 1,
            system: 1,
            non_system: 0,
            blocks: 0,
        },
    };
    let page = h::Page {
        identity: snapshot.identity.clone(),
        snapshot: snapshot.token.clone(),
        context_sha256: snapshot.context_sha256.clone(),
        replay_sha256: snapshot.replay_sha256.clone(),
        counts: snapshot.counts.clone(),
        fragments: vec![],
        previous: None,
        next: None,
        at_start: true,
        at_end: true,
        bytes: 0,
        units: 0,
    };
    let mut history = litetui_terminal::resume::History::projected(snapshot);
    history.page = Some(Box::new(page));
    litetui_terminal::management::RewriteChanged {
        key: chat.rewrite.pending_key().unwrap().clone(),
        admission: r::Admission {
            outcome: r::AdmissionOutcome::Accepted,
            operation_id: "action".into(),
            action_id: ACTION.into(),
            mutated: true,
            close_required: false,
            reason: None,
            prepared_context_sha256: review.prepared_context_sha256,
            prepared_messages: review.prepared_messages,
            snapshot_written: true,
            readback_verified: true,
            refusal_history: None,
        },
        snapshot: litetui_terminal::management::Snapshot {
            session: SESSION.into(),
            model: "test".into(),
            backend: "custom".into(),
            profile: "strict".into(),
            mode: "normal".into(),
            writable: true,
            held_restored_input: Some(0),
            schedules_paused: false,
            conversations: 0,
            jobs: 0,
            tasks: 0,
            presentation: Default::default(),
        },
        history,
        queue: litetui_terminal::restored_queue::Snapshot {
            revision: 1,
            token: "8".repeat(64),
            session: SESSION.into(),
            backend: "custom".into(),
            model: "test".into(),
            entries: vec![],
            message: None,
            recovered: None,
        },
        policy: serde_json::json!({}),
    }
}
#[test]
fn final_verified_action_readback_releases_only_action_while_uncertainty_stays() {
    for pending_stream in [false, true] {
        let mut chat = prepared_chat("action");
        if !pending_stream {
            chat.observe(Observation::Text("real answer".into()))
                .unwrap();
            chat.observe(Observation::Complete).unwrap();
        }
        chat.observe(work(WorkExecution::Uncertain {
            input_id: "uncertain".into(),
            reason: "delivery unresolved".into(),
        }))
        .unwrap();
        let changed = changed_action(&chat);
        let result = chat.observe(Observation::RewriteSettled(Box::new(changed)));
        if pending_stream {
            assert!(result.is_err());
            assert!(chat.rewrite_pending());
        } else {
            result.unwrap();
            assert!(!chat.rewrite_pending());
            assert_eq!(chat.rows.len(), 1);
            assert!(chat.busy);
            assert!(chat.submit("do not execute").is_err());
            assert!(chat.notice.contains("delivery unresolved"));
            let mut app = Demo::live("custom".into(), "test".into());
            app.live = Some(chat);
            app.open_work();
            assert!(app.live.as_ref().unwrap().work.owns_input());
        }
    }
}

fn committed_admission(mode: Mode) -> WorkExecution {
    let mut update = admitted(
        1,
        "committed",
        mode,
        "Child result is already in canonical history",
    );
    if let WorkExecution::Admitted { input, payload, .. } = &mut update {
        input.source = "child-result".into();
        payload.source = input.source.clone();
        payload.content.clear();
        payload.delivery = litetui_terminal::work_lifecycle::Delivery::CommittedHistory(
            litetui_terminal::work_lifecycle::CommittedHistory {
                session: SESSION.into(),
                transcript_sha256: "a".repeat(64),
                context_sha256: "b".repeat(64),
                receipt_ids: vec![
                    "original-committed-receipt-one".into(),
                    "original-committed-receipt-two".into(),
                ],
            },
        );
    }
    update
}
#[test]
fn committed_history_continuation_renders_status_and_original_receipts_without_user_prompt() {
    let mut app = Demo::live("custom".into(), "test".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("untouched local draft");
    app.observe_live(work(committed_admission(Mode::NewTurn)))
        .unwrap();
    let chat = app.live.as_mut().unwrap();
    assert!(chat.rows[0].prompt.is_empty());
    assert_eq!(
        chat.rows[0].committed.as_ref().unwrap().status,
        "Child result is already in canonical history"
    );
    assert!(chat.take_submission().is_none());
    assert_eq!(app.composer.text(), "untouched local draft");
    let rendered = screen(&mut app);
    for expected in [
        "Source continuation:",
        "Already committed history references",
        "original-committed-receipt-one",
        "original-committed-receipt-two",
        "child-result",
    ] {
        assert!(rendered.contains(expected), "{expected}");
    }
    assert!(!rendered.contains("You: Child result"));
    assert!(!rendered.contains("Source: Child result"));
}
#[test]
fn committed_history_cannot_be_reused_as_inline_append_and_receipts_are_bounded() {
    for mode in [Mode::InTurn, Mode::ActionSegment] {
        let mut chat = ready();
        assert!(chat.observe(work(committed_admission(mode))).is_err());
        assert!(chat.rows.is_empty());
    }
    let mut update = committed_admission(Mode::NewTurn);
    if let WorkExecution::Admitted { payload, .. } = &mut update
        && let litetui_terminal::work_lifecycle::Delivery::CommittedHistory(history) =
            &mut payload.delivery
    {
        history.receipt_ids = (0..256)
            .map(|i| format!("{i}-{}", "\n".repeat(10)))
            .collect();
    }
    let mut chat = ready();
    assert!(chat.observe(work(update)).is_err());
    assert!(chat.rows.is_empty());
}

#[test]
fn reconciliation_releases_only_the_exact_uncertain_input() {
    let mut chat = ready();
    for input_id in ["uncertain-one", "uncertain-two"] {
        chat.observe(work(WorkExecution::Uncertain {
            input_id: input_id.into(),
            reason: "Provider send result unknown".into(),
        }))
        .unwrap();
    }
    chat.observe(work(WorkExecution::Reconciled {
        input_id: "uncertain-one".into(),
        recovered: false,
    }))
    .unwrap();
    assert!(chat.busy);
    assert!(chat.submit("still blocked").is_err());
    assert!(chat.notice.contains("uncertain-two"));
    chat.observe(work(WorkExecution::Reconciled {
        input_id: "uncertain-two".into(),
        recovered: false,
    }))
    .unwrap();
    assert!(!chat.busy);
    assert!(chat.rows.is_empty());
    assert!(
        chat.observe(work(WorkExecution::Reconciled {
            input_id: "uncertain-two".into(),
            recovered: false
        }))
        .is_err()
    );
    chat.submit("explicit later prompt").unwrap();
}
#[test]
fn held_input_history_recovery_is_reachable_under_idle_work_uncertainty() {
    let mut app = Demo::live("codex".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.observe_live(work(WorkExecution::Uncertain {
        input_id: "held".into(),
        reason: "Unknown send".into(),
    }))
    .unwrap();
    app.open_queue();
    assert!(app.has_live_queue());
    let menu=litetui_terminal::restored_queue::Snapshot::parse(&serde_json::json!({"session_id":SESSION,"backend":"codex","model":"model","policy":"hold","held":1,"snapshot":"a".repeat(64),"entries":[{"id":"delivery","provider":"codex","state":"uncertain","revision":1,"content":"Already accepted user input","source":"harness","tool_profile":"strict","thread_id":"thread","turn_id":"turn","can_cancel":false,"can_continue":false,"can_reconcile":true}]}),1).unwrap();
    app.observe_live(Observation::Queue(
        litetui_terminal::restored_queue::Update::Snapshot(menu.clone()),
    ))
    .unwrap();
    app.live
        .as_ref()
        .unwrap()
        .prepare_reconciliation(&litetui_terminal::restored_queue::Intent::Reconcile {
            revision: menu.revision,
            entry: "delivery".into(),
        })
        .unwrap();
    let mut checked = menu;
    checked.recovered = Some(Box::new(checked.entries.remove(0)));
    app.observe_live(Observation::Queue(
        litetui_terminal::restored_queue::Update::Snapshot(checked),
    ))
    .unwrap();
    assert!(app.live.as_ref().unwrap().busy);
    app.observe_live(work(WorkExecution::Reconciled {
        input_id: "held".into(),
        recovered: false,
    }))
    .unwrap();
    let chat = app.live.as_ref().unwrap();
    assert!(!chat.busy);
    assert_eq!(chat.rows.len(), 1);
    assert_eq!(chat.rows[0].state, ReplyState::Reconciled);
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
}
#[test]
fn recovered_history_receipt_cannot_release_an_active_work_row() {
    let mut chat = ready();
    chat.observe(work(admitted(1, "source-turn", Mode::NewTurn, "Work")))
        .unwrap();
    assert!(
        chat.observe(work(WorkExecution::Reconciled {
            input_id: "historical-input".into(),
            recovered: true
        }))
        .is_err()
    );
    assert!(chat.busy);
}
