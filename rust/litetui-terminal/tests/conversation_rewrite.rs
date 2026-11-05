use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use litetui_terminal::{
    conversation_rewrite::*,
    history::{self, Identity, Scope},
    history_view::{ReadKey, Target},
    theme::builtin_themes,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION_ID: &str = "32345678-1234-4123-8123-123456789abc";
const OP: &str = "rewrite-test-operation";
fn sha(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
fn selection() -> Selection {
    Selection {
        history_key: ReadKey {
            target: Target::Current,
            generation: 7,
        },
        identity: Identity {
            from_session: SESSION.into(),
            session: SESSION.into(),
            agent: AGENT.into(),
            backend: "custom".into(),
            model: "test".into(),
            scope: Scope::Current,
            catalog: None,
        },
        history_snapshot: "a".repeat(64),
        context_sha256: "b".repeat(64),
        messages: 5,
        message_index: 2,
        message_sha256: "c".repeat(64),
    }
}
fn snapshot_value(original: &str) -> Value {
    let mut v = selection().command(SNAPSHOT);
    v["kind"] = json!("conversation_rewrite_snapshot");
    v["snapshot"] = json!("d".repeat(64));
    v["original_text"] = json!(original);
    v["original_bytes"] = json!(original.len());
    v["prefix_messages"] = json!(2);
    v["prefix_context_sha256"] = json!("e".repeat(64));
    v["discarded_messages"] = json!(2);
    v["actions"] =
        json!({"edit":{"eligible":true,"reason":null},"retry":{"eligible":true,"reason":null}});
    v
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&snapshot_value("original user"), &selection()).unwrap()
}
fn review_value(before: &Snapshot, action: Action, text: &str) -> Value {
    let mut v = before.request(action, text);
    v["kind"] = json!("conversation_rewrite_review");
    v["review"] = json!("f".repeat(64));
    v["action_id"] = json!(ACTION_ID);
    v["text"] = json!(text);
    v["text_sha256"] = json!(sha(text));
    v["prepared_context_sha256"] = json!("1".repeat(64));
    v["prepared_messages"] = json!(3);
    v["prefix_context_sha256"] = json!(before.prefix_context_sha256);
    v["prefix_messages"] = json!(2);
    v["discarded_messages"] = json!(2);
    v["effects"] = json!({"prompt_hooks":true,"provider_execution":true,"provider_rewind":false});
    v
}
fn review() -> Review {
    let before = snapshot();
    Review::parse(
        &review_value(&before, Action::Edit, "replacement"),
        before,
        Action::Edit,
        "replacement",
    )
    .unwrap()
}
fn admission_value(review: &Review, outcome: AdmissionOutcome) -> Value {
    let mut v = review.request();
    let accepted = outcome == AdmissionOutcome::Accepted;
    v["kind"] = json!("conversation_rewrite_apply");
    v["operation_id"] = json!(OP);
    v["turn"] = json!(outcome.as_str());
    v["mutated"] = json!(accepted);
    v["close_required"] = json!(outcome == AdmissionOutcome::Failed);
    v["reason"] = if accepted {
        Value::Null
    } else {
        json!("not admitted")
    };
    v["persistence"] = json!({"snapshot_written":accepted,"readback_verified":accepted});
    v["refusal_history"] = if outcome == AdmissionOutcome::Refused {
        history_value(true)
    } else {
        Value::Null
    };
    v
}
fn history_value(refused: bool) -> Value {
    let mut v = selection().identity.command(history::SNAPSHOT);
    for (key,value) in json!({"kind":"history_snapshot","projection_version":1,"snapshot":"2".repeat(64),"transcript_revision":"3".repeat(64),
        "context_sha256":if refused {"b".repeat(64)}else{"4".repeat(64)},"replay_sha256":"5".repeat(64),"pending":false,"materialized":true,
        "title":"Current","read_only":false,"open_effects":null,"open_action_id":null,"fresh_system_context":false,
        "counts":{"messages":if refused{5}else{4},"system":1,"non_system":if refused{4}else{3},"blocks":if refused{4}else{3}}}).as_object().unwrap() {v[key]=value.clone();}
    v
}
fn settled_value(review: &Review, admission: &Admission) -> Value {
    let mut v = admission_value(review, admission.outcome);
    v["kind"] = json!("conversation_rewrite_settled");
    v["worker_settled"] = json!(true);
    v["execution"] = if admission.outcome == AdmissionOutcome::Refused {
        json!({"segments":0,"last_execution_id":null})
    } else {
        json!({"segments":1,"last_execution_id":"42345678-1234-4123-8123-123456789abc"})
    };
    v["history"] = history_value(admission.outcome == AdmissionOutcome::Refused);
    v
}
#[test]
fn codex_review_and_receipt_require_the_exact_suffix_thread_and_fresh_user_item() {
    let mut before = snapshot();
    before.selection.identity.backend = "codex".into();
    let mut review = review_value(&before, Action::Retry, &before.original_text);
    review["effects"]["provider_rewind"] = json!(true);
    review["native_rewind"] = json!({"thread_id":"thread","before_sha256":"a".repeat(64),"prefix_sha256":"b".repeat(64),"retained_turns":1,
        "suffix":[{"turn_id":"dropped","user_item_id":"old-user","text":before.original_text,
            "items":[{"id":"old-user","kind":"userMessage","details":""},{"id":"tool","kind":"commandExecution","details":"touch example"}]}]});
    let parse =
        |wire: &Value| Review::parse(wire, before.clone(), Action::Retry, &before.original_text);
    for (pointer, value) in [
        ("/native_rewind/suffix/0/user_item_id", json!("other")),
        ("/native_rewind/suffix/0/text", json!("changed")),
        ("/native_rewind/suffix/0/items/1/id", json!("old-user")),
        ("/effects/provider_rewind", json!(false)),
        ("/native_rewind/retained_turns", json!(100)),
    ] {
        let mut bad = review.clone();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(parse(&bad).is_err(), "{pointer}");
    }
    let r = parse(&review).unwrap();
    assert_eq!(r.request()["native_rewind"], review["native_rewind"]);
    let admission =
        Admission::parse(&admission_value(&r, AdmissionOutcome::Accepted), &r, OP).unwrap();
    let mut settled = settled_value(&r, &admission);
    settled["history"]["backend"] = json!("codex");
    settled["native_rewind"] = json!({"thread_id":"thread","prefix_sha256":"b".repeat(64),
        "history_sha256":"c".repeat(64),"turn_id":"replacement","user_item_id":"new-user"});
    assert_eq!(
        Settlement::parse(&settled, &admission, &r, OP)
            .unwrap()
            .native_turn
            .as_deref(),
        Some("replacement")
    );
    for (key, bad) in [
        ("thread_id", "foreign"),
        ("user_item_id", "tool"),
        ("turn_id", "dropped"),
        ("prefix_sha256", "bad"),
    ] {
        let mut changed = settled.clone();
        changed["native_rewind"][key] = json!(bad);
        assert!(
            Settlement::parse(&changed, &admission, &r, OP).is_err(),
            "{key}"
        );
    }
}
#[test]
fn claude_fork_requires_native_checkpoint_distinct_segment_and_observed_result() {
    let mut before = snapshot();
    before.selection.identity.backend = "claude".into();
    let mut value = review_value(&before, Action::Edit, "replacement");
    value["effects"]["provider_fork"] = json!(true);
    value["native_fork"] = json!({"thread_id":SESSION,"before_sha256":"a".repeat(64),"prefix_sha256":"b".repeat(64),"retained_turns":1,
        "fork":{"old_segment":"1".repeat(32),"new_segment":"2".repeat(32),"delivery":"3".repeat(32),"checkpoint":"99345678-1234-4123-8123-123456789abc","ledger_before":"c".repeat(64)},
        "suffix":[{"turn_id":AGENT,"user_item_id":ACTION_ID,"text":"original user","items":[{"id":ACTION_ID,"kind":"userMessage","details":""},
            {"id":AGENT,"kind":"agentMessage","details":"former reply"}]}]});
    let parse = |wire: &Value| Review::parse(wire, before.clone(), Action::Edit, "replacement");
    for (path, bad) in [
        ("/native_fork/fork/checkpoint", json!("unknown")),
        ("/native_fork/fork/new_segment", json!("1".repeat(32))),
        ("/native_fork/retained_turns", json!(0)),
        ("/native_fork/suffix/0/items/1/kind", json!("tool")),
        ("/effects/provider_rewind", json!(true)),
    ] {
        let mut changed = value.clone();
        *changed.pointer_mut(path).unwrap() = bad;
        assert!(parse(&changed).is_err(), "{path}");
    }
    let review = parse(&value).unwrap();
    assert_eq!(review.request()["native_fork"], value["native_fork"]);
    let admission = Admission::parse(
        &admission_value(&review, AdmissionOutcome::Accepted),
        &review,
        OP,
    )
    .unwrap();
    let mut settled = settled_value(&review, &admission);
    settled["history"]["backend"] = json!("claude");
    let new = "55345678-1234-4123-8123-123456789abc";
    let result = "66345678-1234-4123-8123-123456789abc";
    settled["native_fork"] = json!({"thread_id":SESSION,"session_id":new,"result_id":result,"prefix_sha256":"b".repeat(64),
        "history_sha256":"d".repeat(64),"ledger_after":"e".repeat(64),"turn_id":"77345678-1234-4123-8123-123456789abc",
        "user_item_id":"88345678-1234-4123-8123-123456789abc"});
    let parsed = Settlement::parse(&settled, &admission, &review, OP).unwrap();
    assert_eq!(parsed.fork_result, Some((new.into(), result.into())));
    for (key, bad) in [
        ("session_id", json!(SESSION)),
        ("ledger_after", json!("c".repeat(64))),
        ("result_id", json!("bad")),
    ] {
        let mut changed = settled.clone();
        changed["native_fork"][key] = bad;
        assert!(
            Settlement::parse(&changed, &admission, &review, OP).is_err(),
            "{key}"
        );
    }
}

fn draw(view: &mut View, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| view.draw(frame, frame.area(), &builtin_themes().remove(0)))
        .unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|cell| cell.symbol())
        .collect()
}
fn key(view: &mut View, code: KeyCode) {
    view.key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn loaded(action: Action) -> View {
    let mut view = View::default();
    view.open(action, selection()).unwrap();
    let read = view.take_intent().unwrap();
    view.observe(Update::Snapshot {
        key: read.key().clone(),
        snapshot: Box::new(snapshot()),
    })
    .unwrap();
    view
}
fn reviewed() -> View {
    let mut view = loaded(Action::Edit);
    draw(&mut view, 100, 40);
    key(&mut view, KeyCode::Enter);
    let Intent::Validate {
        key,
        action,
        message,
        ..
    } = view.take_intent().unwrap()
    else {
        panic!()
    };
    let before = snapshot();
    let review = Review::parse(
        &review_value(&before, action, &message),
        before,
        action,
        &message,
    )
    .unwrap();
    view.observe(Update::Review {
        key,
        review: Box::new(review),
    })
    .unwrap();
    view
}
#[test]
fn snapshot_binds_full_selection_bytes_counts_and_eligibility() {
    for fault in [
        "session",
        "index",
        "message_hash",
        "history",
        "context",
        "bytes",
        "prefix",
        "discarded",
        "reason",
        "missing_reason",
        "type",
    ] {
        let mut value = snapshot_value("原文🦀");
        match fault {
            "session" => value["session_id"] = json!(AGENT),
            "index" => value["message_index"] = json!(1),
            "message_hash" => value["message_sha256"] = json!("9".repeat(64)),
            "history" => value["history_snapshot"] = json!("9".repeat(64)),
            "context" => value["context_sha256"] = json!("9".repeat(64)),
            "bytes" => value["original_bytes"] = json!(3),
            "prefix" => value["prefix_messages"] = json!(1),
            "discarded" => value["discarded_messages"] = json!(3),
            "reason" => value["actions"]["edit"]["reason"] = json!("cannot"),
            "missing_reason" => {
                value["actions"]["retry"]
                    .as_object_mut()
                    .unwrap()
                    .remove("reason");
            }
            "type" => value["actions"]["retry"]["eligible"] = json!(1),
            _ => unreachable!(),
        }
        assert!(Snapshot::parse(&value, &selection()).is_err(), "{fault}");
    }
    let value = snapshot_value("原文🦀");
    assert_eq!(
        Snapshot::parse(&value, &selection()).unwrap().original_text,
        "原文🦀"
    );
}
#[test]
fn current_generation_is_local_and_preview_is_never_rewrite_authority() {
    let selected = selection();
    assert!(selected.command(SNAPSHOT).get("generation").is_none());
    assert!(selected.command(SNAPSHOT).get("history_key").is_none());
    for fault in ["target", "scope", "readonly_scope", "zero", "count"] {
        let mut selected = selection();
        match fault {
            "target" => {
                selected.history_key.target = Target::Saved {
                    session: AGENT.into(),
                }
            }
            "scope" => selected.identity.scope = Scope::Preview,
            "readonly_scope" => selected.identity.catalog = Some("6".repeat(64)),
            "zero" => selected.history_key.generation = 0,
            "count" => selected.messages = 2,
            _ => unreachable!(),
        }
        assert!(selected.verify().is_err(), "{fault}");
    }
}
#[test]
fn original_text_bounds_are_utf8_bytes_and_never_truncated() {
    let text = "é".repeat(TEXT_BYTES / 2);
    let value = snapshot_value(&text);
    assert_eq!(
        Snapshot::parse(&value, &selection()).unwrap().original_text,
        text
    );
    assert!(Snapshot::parse(&snapshot_value(&(text + "x")), &selection()).is_err());
    assert!(validate_text(" \n\t").is_err());
    let mut value = snapshot_value("small");
    value["extra"] = json!("x".repeat(RESPONSE_BYTES));
    assert!(Snapshot::parse(&value, &selection()).is_err());
}
#[test]
fn retry_omits_replacement_and_review_verifies_exact_text_hash_and_effects() {
    let before = snapshot();
    assert!(
        before
            .request(Action::Retry, "ignored")
            .get("text")
            .is_none()
    );
    assert_eq!(before.request(Action::Edit, "keep  \n")["text"], "keep  \n");
    for fault in [
        "text",
        "hash",
        "prefix",
        "count",
        "dropped",
        "rewind",
        "execution",
        "action",
        "uuid",
    ] {
        let mut value = review_value(&before, Action::Retry, &before.original_text);
        match fault {
            "text" => value["text"] = json!("different"),
            "hash" => value["text_sha256"] = json!("0".repeat(64)),
            "prefix" => value["prefix_context_sha256"] = json!("0".repeat(64)),
            "count" => value["prepared_messages"] = json!(4),
            "dropped" => value["discarded_messages"] = json!(3),
            "rewind" => value["effects"]["provider_rewind"] = json!(true),
            "execution" => value["effects"]["provider_execution"] = json!(false),
            "action" => value["action"] = json!("edit"),
            "uuid" => value["action_id"] = json!("32345678-1234-1123-8123-123456789abc"),
            _ => unreachable!(),
        }
        assert!(
            Review::parse(&value, before.clone(), Action::Retry, &before.original_text).is_err(),
            "{fault}"
        );
    }
    let review = Review::parse(
        &review_value(&before, Action::Retry, &before.original_text),
        before.clone(),
        Action::Retry,
        &before.original_text,
    )
    .unwrap();
    assert_eq!(review.request()["text"], before.original_text);
    assert!(review.request().get("effects").is_none());
}
#[test]
fn admission_distinguishes_refusal_execution_and_uncertain_publication() {
    let review = review();
    for outcome in [
        AdmissionOutcome::Accepted,
        AdmissionOutcome::Refused,
        AdmissionOutcome::Failed,
    ] {
        let value = admission_value(&review, outcome);
        assert_eq!(
            Admission::parse(&value, &review, OP).unwrap().outcome,
            outcome
        );
        assert!(Admission::parse(&value, &review, "other").is_err());
    }
    for fault in [
        "mutated", "close", "reason", "written", "verified", "hash", "action",
    ] {
        let mut value = admission_value(&review, AdmissionOutcome::Accepted);
        match fault {
            "mutated" => value["mutated"] = json!(false),
            "close" => value["close_required"] = json!(true),
            "reason" => value["reason"] = json!("failure"),
            "written" => value["persistence"]["snapshot_written"] = json!(false),
            "verified" => value["persistence"]["readback_verified"] = json!(false),
            "hash" => value["prepared_context_sha256"] = json!("0".repeat(64)),
            "action" => value["action_id"] = json!(AGENT),
            _ => unreachable!(),
        }
        assert!(Admission::parse(&value, &review, OP).is_err(), "{fault}");
    }
    let mut refusal = admission_value(&review, AdmissionOutcome::Refused);
    refusal["mutated"] = json!(true);
    assert!(Admission::parse(&refusal, &review, OP).is_err());
}
#[test]
fn settlement_requires_exact_worker_action_and_refusal_preserves_context() {
    let review = review();
    for outcome in [AdmissionOutcome::Accepted, AdmissionOutcome::Refused] {
        let admission = Admission::parse(&admission_value(&review, outcome), &review, OP).unwrap();
        let value = settled_value(&review, &admission);
        assert_eq!(
            Settlement::parse(&value, &admission, &review, OP)
                .unwrap()
                .outcome,
            outcome
        );
        for field in ["worker_settled", "close_required", "mutated"] {
            let mut bad = value.clone();
            bad[field] = json!(!value[field].as_bool().unwrap());
            assert!(
                Settlement::parse(&bad, &admission, &review, OP).is_err(),
                "{field}"
            );
        }
        let mut bad = value.clone();
        bad["operation_id"] = json!("foreign");
        assert!(Settlement::parse(&bad, &admission, &review, OP).is_err());
        let mut bad = value.clone();
        bad["history"]["read_only"] = json!(true);
        assert!(Settlement::parse(&bad, &admission, &review, OP).is_err());
        if outcome == AdmissionOutcome::Refused {
            let mut bad = value.clone();
            bad["history"]["context_sha256"] = json!("0".repeat(64));
            assert!(Settlement::parse(&bad, &admission, &review, OP).is_err());
        }
    }
}
#[test]
fn generations_advance_each_phase_and_survive_finish_with_stale_updates_ignored() {
    let mut view = View::default();
    view.open(Action::Edit, selection()).unwrap();
    let first = view.take_intent().unwrap().key().clone();
    view.finish();
    view.open(Action::Edit, selection()).unwrap();
    let second = view.take_intent().unwrap().key().clone();
    assert!(second.generation > first.generation);
    view.observe(Update::Rejected {
        key: first,
        reason: "late".into(),
    })
    .unwrap();
    assert_eq!(view.pending_key(), Some(&second));
    view.observe(Update::Snapshot {
        key: second.clone(),
        snapshot: Box::new(snapshot()),
    })
    .unwrap();
    draw(&mut view, 100, 40);
    key(&mut view, KeyCode::Enter);
    let third = view.take_intent().unwrap().key().clone();
    assert!(third.generation > second.generation);
    view.observe(Update::Rejected {
        key: second,
        reason: "duplicate".into(),
    })
    .unwrap();
    assert_eq!(view.pending_key(), Some(&third));
}
#[test]
fn closing_unsent_and_sent_reads_has_distinct_pending_ownership() {
    let mut view = View::default();
    view.open(Action::Edit, selection()).unwrap();
    view.close();
    assert!(!view.blocks_composer());
    assert!(view.take_intent().is_none());
    view.open(Action::Edit, selection()).unwrap();
    let pending = view.take_intent().unwrap().key().clone();
    view.close();
    assert!(view.owns_input());
    view.observe(Update::Snapshot {
        key: pending,
        snapshot: Box::new(snapshot()),
    })
    .unwrap();
    assert!(!view.owns_input());
    assert!(!view.is_open());
}
#[test]
fn review_is_not_confirmation_until_fresh_draw_and_press() {
    let mut view = reviewed();
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    draw(&mut view, 100, 40);
    view.key(KeyEvent::new_with_kind(
        KeyCode::Enter,
        KeyModifiers::NONE,
        KeyEventKind::Repeat,
    ));
    assert!(view.take_intent().is_none());
    draw(&mut view, 100, 40);
    key(&mut view, KeyCode::Enter);
    let apply = view.take_intent().unwrap();
    assert!(view.prepare_apply(&apply).is_ok());
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
}
#[test]
fn submitted_hands_live_input_back_but_blocks_composer_until_exact_settlement() {
    let mut view = reviewed();
    draw(&mut view, 100, 40);
    key(&mut view, KeyCode::Enter);
    let apply = view.take_intent().unwrap();
    let pending = apply.key().clone();
    view.observe(Update::Submitted {
        key: pending.clone(),
    })
    .unwrap();
    assert!(!view.owns_input());
    assert!(!view.is_open());
    assert!(view.blocks_composer());
    assert!(view.prepare_apply(&apply).is_err());
    let mut late = pending.clone();
    late.generation -= 1;
    view.observe(Update::Settled {
        key: late,
        outcome: AdmissionOutcome::Accepted,
        reason: None,
    })
    .unwrap();
    assert!(view.blocks_composer());
    assert!(
        view.observe(Update::Rejected {
            key: pending.clone(),
            reason: "late failure".into()
        })
        .is_err()
    );
    assert!(view.blocks_composer());
    view.observe(Update::Settled {
        key: pending,
        outcome: AdmissionOutcome::Accepted,
        reason: None,
    })
    .unwrap();
    assert!(!view.blocks_composer());
    assert!(!view.is_open());
}
#[test]
fn esc_cancels_review_and_keeps_edit_without_scheduling_or_stopping() {
    let mut view = loaded(Action::Edit);
    draw(&mut view, 100, 40);
    view.paste(" edited");
    let original = view.draft().to_owned();
    key(&mut view, KeyCode::Enter);
    let Intent::Validate {
        key: pending,
        action,
        message,
        ..
    } = view.take_intent().unwrap()
    else {
        panic!()
    };
    let before = snapshot();
    let review = Review::parse(
        &review_value(&before, action, &message),
        before,
        action,
        &message,
    )
    .unwrap();
    view.observe(Update::Review {
        key: pending,
        review: Box::new(review),
    })
    .unwrap();
    key(&mut view, KeyCode::Esc);
    assert_eq!(view.draft(), original);
    assert!(view.review().is_none());
    assert!(view.take_intent().is_none());
    assert!(view.is_open());
}
#[test]
fn readonly_eligibility_displays_reason_without_allowing_edits_or_validation() {
    let mut value = snapshot_value("original user");
    value["actions"] = json!({"edit":{"eligible":false,"reason":"Native provider history cannot rewind"},"retry":{"eligible":false,"reason":"Native provider history cannot rewind"}});
    let mut view = View::default();
    view.open(Action::Edit, selection()).unwrap();
    let pending = view.take_intent().unwrap().key().clone();
    view.observe(Update::Snapshot {
        key: pending,
        snapshot: Box::new(Snapshot::parse(&value, &selection()).unwrap()),
    })
    .unwrap();
    assert!(draw(&mut view, 100, 40).contains("Native provider history cannot rewind"));
    view.paste("changed");
    key(&mut view, KeyCode::Enter);
    assert_eq!(view.draft(), "original user");
    assert!(view.take_intent().is_none());
}
#[test]
fn unicode_editing_and_shift_enter_preserve_bytes_and_overflow_keeps_draft() {
    let mut view = loaded(Action::Edit);
    draw(&mut view, 100, 40);
    view.paste("🦀");
    view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::SHIFT));
    assert!(view.draft().ends_with("🦀\n"));
    key(&mut view, KeyCode::Backspace);
    key(&mut view, KeyCode::Backspace);
    assert_eq!(view.draft(), "original user");
    let before = view.draft().to_owned();
    view.paste(&"x".repeat(TEXT_BYTES));
    assert_eq!(view.draft(), before);
}
#[test]
fn resize_disarms_review_and_small_view_can_only_escape() {
    let mut view = reviewed();
    draw(&mut view, 100, 40);
    draw(&mut view, 20, 8);
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    key(&mut view, KeyCode::Esc);
    assert!(view.review().is_none());
    assert!(view.take_intent().is_none());
}
#[test]
fn long_review_requires_every_page_and_resize_returns_to_first_page() {
    let mut view = loaded(Action::Edit);
    draw(&mut view, 60, 20);
    view.paste(&" long text".repeat(100));
    key(&mut view, KeyCode::Enter);
    let Intent::Validate {
        key: pending,
        action,
        message,
        ..
    } = view.take_intent().unwrap()
    else {
        panic!()
    };
    let before = snapshot();
    let review = Review::parse(
        &review_value(&before, action, &message),
        before,
        action,
        &message,
    )
    .unwrap();
    view.observe(Update::Review {
        key: pending,
        review: Box::new(review),
    })
    .unwrap();
    draw(&mut view, 60, 20);
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    draw(&mut view, 60, 20);
    key(&mut view, KeyCode::PageDown);
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    assert!(draw(&mut view, 80, 24).contains("Page 1/"));
    for _ in 0..20 {
        draw(&mut view, 80, 24);
        key(&mut view, KeyCode::Enter);
        if let Some(intent) = view.take_intent() {
            assert!(matches!(intent, Intent::Apply { .. }));
            return;
        }
        draw(&mut view, 80, 24);
        key(&mut view, KeyCode::PageDown);
    }
    panic!("review never became confirmable");
}

fn shutdown_value(command: Option<&Value>, current: &str) -> Value {
    let mut proof = json!({"version":1,"worker_settled":true,"admission_settled":true,"hooks_settled":true,
        "effects_verified":false,"pending_at_close":command.is_some(),"mutated":false,
        "action_id":null,"action":null,"operation_id":null,"session_id":null,"agent_id":null,"backend":null,"model":null,"current_session_id":current});
    if let Some(command) = command {
        for field in [
            "action_id",
            "action",
            "session_id",
            "agent_id",
            "backend",
            "model",
        ] {
            proof[field] = command[field].clone();
        }
        proof["operation_id"] = command["id"].clone();
    }
    json!({"conversation_rewrite_shutdown":proof})
}
#[test]
fn shutdown_requires_exact_action_and_all_cleanup_without_claiming_effects() {
    let mut command = review().request();
    command["id"] = json!(OP);
    let result = shutdown_value(Some(&command), SESSION);
    assert!(verify_shutdown(&result, Some(&command), SESSION).unwrap());
    for field in [
        "worker_settled",
        "admission_settled",
        "hooks_settled",
        "effects_verified",
    ] {
        let mut bad = result.clone();
        bad["conversation_rewrite_shutdown"][field] = json!(
            !result["conversation_rewrite_shutdown"][field]
                .as_bool()
                .unwrap()
        );
        assert!(
            verify_shutdown(&bad, Some(&command), SESSION).is_err(),
            "{field}"
        );
    }
    for field in [
        "action_id",
        "action",
        "operation_id",
        "session_id",
        "agent_id",
        "backend",
        "model",
        "current_session_id",
    ] {
        let mut bad = result.clone();
        bad["conversation_rewrite_shutdown"][field] = json!("foreign");
        assert!(
            verify_shutdown(&bad, Some(&command), SESSION).is_err(),
            "{field}"
        );
    }
    let mut bad = result.clone();
    bad["conversation_rewrite_shutdown"]["mutated"] = json!("false");
    assert!(verify_shutdown(&bad, Some(&command), SESSION).is_err());
}
#[test]
fn read_only_shutdown_has_null_action_and_completed_action_may_precede_new_session() {
    let result = shutdown_value(None, SESSION);
    assert!(!verify_shutdown(&result, None, SESSION).unwrap());
    let mut bad = result.clone();
    bad["conversation_rewrite_shutdown"]
        .as_object_mut()
        .unwrap()
        .remove("action_id");
    assert!(verify_shutdown(&bad, None, SESSION).is_err());
    let mut command = review().request();
    command["id"] = json!(OP);
    assert!(verify_shutdown(&shutdown_value(Some(&command), SESSION), None, SESSION).is_err());
    let mut later = shutdown_value(Some(&command), AGENT);
    assert!(verify_shutdown(&later, Some(&command), AGENT).is_err());
    later["conversation_rewrite_shutdown"]["pending_at_close"] = json!(false);
    later["conversation_rewrite_shutdown"]["mutated"] = json!(true);
    assert!(!verify_shutdown(&later, Some(&command), AGENT).unwrap());
}

#[test]
fn same_key_wrong_phase_cannot_replace_a_pending_validation() {
    let mut view = loaded(Action::Edit);
    draw(&mut view, 100, 40);
    key(&mut view, KeyCode::Enter);
    let pending = view.take_intent().unwrap().key().clone();
    assert!(
        view.observe(Update::Snapshot {
            key: pending.clone(),
            snapshot: Box::new(snapshot())
        })
        .is_err()
    );
    assert_eq!(view.pending_key(), Some(&pending));
    assert!(view.waiting());
}
#[test]
fn literal_caret_glyph_in_user_text_does_not_move_the_editor_away_from_real_caret() {
    let original = format!("▏ first\n{}tail", "middle\n".repeat(40));
    let mut view = View::default();
    view.open(Action::Edit, selection()).unwrap();
    let pending = view.take_intent().unwrap().key().clone();
    view.observe(Update::Snapshot {
        key: pending,
        snapshot: Box::new(Snapshot::parse(&snapshot_value(&original), &selection()).unwrap()),
    })
    .unwrap();
    let shown = draw(&mut view, 60, 20);
    assert!(shown.contains("tail▏"));
    assert!(view.accepts_paste());
}

#[test]
fn stale_review_refusal_binds_a_fresh_same_identity_checkpoint_without_adopting_old_context() {
    let review = review();
    let mut receipt = admission_value(&review, AdmissionOutcome::Refused);
    receipt["refusal_history"]["snapshot"] = json!("6".repeat(64));
    receipt["refusal_history"]["context_sha256"] = json!("7".repeat(64));
    receipt["refusal_history"]["counts"] =
        json!({"messages":6,"system":1,"non_system":5,"blocks":5});
    let admission = Admission::parse(&receipt, &review, OP).unwrap();
    assert_ne!(
        admission.refusal_history.as_ref().unwrap().context_sha256,
        review.before.selection.context_sha256
    );
    let mut settled = settled_value(&review, &admission);
    settled["history"] = receipt["refusal_history"].clone();
    assert!(Settlement::parse(&settled, &admission, &review, OP).is_ok());
    settled["history"] = history_value(true);
    assert!(Settlement::parse(&settled, &admission, &review, OP).is_err());
}
#[test]
fn refusal_checkpoint_requires_owned_materialized_descriptor_and_nonrefusals_explicit_null() {
    let review = review();
    for fault in ["missing", "null", "session", "readonly", "pending", "hash"] {
        let mut receipt = admission_value(&review, AdmissionOutcome::Refused);
        match fault {
            "missing" => {
                receipt.as_object_mut().unwrap().remove("refusal_history");
            }
            "null" => receipt["refusal_history"] = Value::Null,
            "session" => receipt["refusal_history"]["session_id"] = json!(AGENT),
            "readonly" => receipt["refusal_history"]["read_only"] = json!(true),
            "pending" => receipt["refusal_history"]["pending"] = json!(true),
            "hash" => receipt["refusal_history"]["context_sha256"] = json!("forged"),
            _ => unreachable!(),
        }
        assert!(Admission::parse(&receipt, &review, OP).is_err(), "{fault}");
    }
    for outcome in [AdmissionOutcome::Accepted, AdmissionOutcome::Failed] {
        let mut receipt = admission_value(&review, outcome);
        assert!(Admission::parse(&receipt, &review, OP).is_ok());
        receipt["refusal_history"] = history_value(true);
        assert!(Admission::parse(&receipt, &review, OP).is_err());
        receipt.as_object_mut().unwrap().remove("refusal_history");
        assert!(Admission::parse(&receipt, &review, OP).is_err());
    }
}

#[test]
fn review_explains_the_one_shot_prewrite_hook_decision() {
    let mut view = reviewed();
    let text = draw(&mut view, 140, 50);
    assert!(text.contains("Hooks that need approval will ask before saving."));
    assert!(text.contains("Each approval applies once to this edit/retry."));
}
