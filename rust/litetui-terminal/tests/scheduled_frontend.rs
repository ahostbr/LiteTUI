use litetui_terminal::{demo::Demo, live::Observation};
fn ready() -> Demo {
    let mut app = Demo::live("custom".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app
}
fn proposed(app: &mut Demo) {
    app.observe_live(Observation::ScheduledProposal("exact-proposal".into()))
        .unwrap();
}
#[test]
fn idle_proposal_reserves_ui_without_consuming_or_sending_draft() {
    let mut app = ready();
    app.composer.paste_bracketed("draft Δ kept");
    proposed(&mut app);
    assert_eq!(
        app.take_schedule_decision(),
        Some(("exact-proposal".into(), true))
    );
    assert!(app.take_schedule_decision().is_none());
    assert_eq!(app.composer.text(), "draft Δ kept");
    let chat = app.live.as_mut().unwrap();
    assert!(!chat.ready);
    assert!(chat.rows.is_empty());
    assert!(chat.submit("later").is_err());
    app.open_settings();
    assert!(!app.live.as_ref().unwrap().settings.owns_input());
    app.observe_live(Observation::ScheduledHeld("slot changed".into()))
        .unwrap();
    assert!(app.live.as_ref().unwrap().ready);
    assert_eq!(app.composer.text(), "draft Δ kept");
}
#[test]
fn every_open_editor_or_review_declines_without_replacing_it() {
    for panel in 0..6 {
        let mut app = ready();
        match panel {
            0 => app.live.as_mut().unwrap().settings.open(),
            1 => app.live.as_mut().unwrap().browser.open(),
            2 => app.live.as_mut().unwrap().work.open(),
            3 => app.themes_open = true,
            4 => app.approval_open = true,
            _ => app.live.as_mut().unwrap().stop_confirm = true,
        }
        proposed(&mut app);
        assert_eq!(
            app.take_schedule_decision(),
            Some(("exact-proposal".into(), false)),
            "panel {panel}"
        );
        assert!(app.live.as_ref().unwrap().ready);
    }
}
#[test]
fn queued_local_submission_wins_and_is_never_replayed_by_handoff() {
    let mut app = ready();
    app.live.as_mut().unwrap().submit("already queued").unwrap();
    proposed(&mut app);
    assert_eq!(
        app.take_schedule_decision(),
        Some(("exact-proposal".into(), false))
    );
    app.observe_live(Observation::ScheduledHeld(
        "local submission owns runtime".into(),
    ))
    .unwrap();
    let chat = app.live.as_ref().unwrap();
    assert!(chat.busy);
    assert_eq!(chat.rows.len(), 1);
    assert_eq!(chat.rows[0].prompt, "already queued");
}
#[test]
fn hidden_dispatched_read_still_declines_until_exact_reply() {
    let mut app = ready();
    let work = &mut app.live.as_mut().unwrap().work;
    work.open();
    assert!(work.take_intent().is_some());
    work.close();
    assert!(work.owns_input());
    proposed(&mut app);
    assert_eq!(
        app.take_schedule_decision(),
        Some(("exact-proposal".into(), false))
    );
}
#[test]
fn quit_or_duplicate_proposals_cannot_open_new_reservation() {
    let mut app = ready();
    proposed(&mut app);
    assert!(
        app.observe_live(Observation::ScheduledProposal("another".into()))
            .is_err()
    );
    app.live.as_mut().unwrap().begin_quit();
    assert_eq!(
        app.take_schedule_decision(),
        Some(("exact-proposal".into(), false))
    );
}

fn unsent(prompt: &str) -> Observation {
    Observation::ScheduledLocalNotSent {
        prompt: prompt.into(),
        reason: "Not sent; explicit resubmission required".into(),
    }
}
#[test]
fn forwarded_local_prompt_refusal_declines_both_ui_batch_orders_without_replay() {
    use litetui_terminal::{composer::Motion, live_chat::ReplyState};
    for decide_before_refusal in [false, true] {
        let mut app = ready();
        let original = "Exact queued prompt Δ  kept verbatim";
        app.composer.paste_clipboard(original);
        app.submit();
        assert_eq!(
            app.live.as_mut().unwrap().take_submission().as_deref(),
            Some(original)
        );
        app.insert_text("Newer draft must survive");
        app.composer.move_cursor(Motion::WordLeft, true);
        let newer = (
            app.composer.text().to_owned(),
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        proposed(&mut app);
        if decide_before_refusal {
            assert_eq!(
                app.take_schedule_decision(),
                Some(("exact-proposal".into(), false))
            );
        }
        app.observe_live(unsent(original)).unwrap();
        let chat = app.live.as_ref().unwrap();
        assert!(!chat.busy && !chat.ready);
        assert_eq!(chat.rows.len(), 1);
        assert_eq!(chat.rows[0].state, ReplyState::Refused);
        assert_eq!(chat.rows[0].prompt, original);
        assert!(!chat.rows[0].source_owned);
        if !decide_before_refusal {
            assert_eq!(
                app.take_schedule_decision(),
                Some(("exact-proposal".into(), false))
            );
        }
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        assert!(app.live.as_mut().unwrap().submit(original).is_err());
        app.observe_live(Observation::ScheduledHeld("Decline read back".into()))
            .unwrap();
        assert!(app.live.as_ref().unwrap().ready);
        assert_eq!(
            newer,
            (
                app.composer.text().to_owned(),
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            )
        );
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        app.live.as_mut().unwrap().submit(original).unwrap();
        assert_eq!(
            app.live.as_mut().unwrap().take_submission().as_deref(),
            Some(original)
        );
        assert_eq!(app.live.as_ref().unwrap().rows.len(), 2);
    }
}
#[test]
fn scheduled_local_refusal_rejects_unrelated_unforwarded_admitted_and_repeated_rows() {
    use litetui_terminal::live_chat::ReplyState;
    for fault in [
        "empty",
        "unforwarded",
        "wrong-text",
        "submitted",
        "source-owned",
        "streaming",
        "repeated",
    ] {
        let mut app = ready();
        if fault != "empty" {
            app.live.as_mut().unwrap().submit("exact local").unwrap();
            if fault != "unforwarded" {
                app.live.as_mut().unwrap().take_submission().unwrap();
            }
            match fault {
                "submitted" => app
                    .observe_live(Observation::Submission {
                        id: "already-owned".into(),
                    })
                    .unwrap(),
                "source-owned" => app.live.as_mut().unwrap().rows[0].source_owned = true,
                "streaming" => app.live.as_mut().unwrap().rows[0].state = ReplyState::Streaming,
                "repeated" => app.observe_live(unsent("exact local")).unwrap(),
                _ => {}
            }
        }
        let prompt = if fault == "wrong-text" {
            "changed local"
        } else {
            "exact local"
        };
        assert!(app.observe_live(unsent(prompt)).is_err(), "{fault}");
    }
}
#[test]
fn scheduled_refusal_and_late_hold_do_not_reopen_a_quitting_view() {
    let mut app = ready();
    app.live.as_mut().unwrap().submit("exact local").unwrap();
    app.live.as_mut().unwrap().take_submission().unwrap();
    proposed(&mut app);
    app.live.as_mut().unwrap().begin_quit();
    app.observe_live(unsent("exact local")).unwrap();
    assert_eq!(
        app.take_schedule_decision(),
        Some(("exact-proposal".into(), false))
    );
    app.observe_live(Observation::ScheduledHeld("Decline read back".into()))
        .unwrap();
    assert!(!app.live.as_ref().unwrap().ready);
    assert!(app.live.as_mut().unwrap().submit("later").is_err());
}

#[test]
fn exact_unadmitted_local_cancellation_drains_scheduled_refusal_without_abort() {
    use litetui_terminal::live_chat::ReplyState;
    let mut app = ready();
    app.live.as_mut().unwrap().submit("exact local").unwrap();
    app.live.as_mut().unwrap().take_submission().unwrap();
    proposed(&mut app);
    let chat = app.live.as_mut().unwrap();
    chat.request_stop().unwrap();
    assert_eq!(chat.rows[0].state, ReplyState::Cancelling);
    assert!(chat.take_abort().is_none());
    app.observe_live(unsent("exact local")).unwrap();
    assert_eq!(
        app.take_schedule_decision(),
        Some(("exact-proposal".into(), false))
    );
    let chat = app.live.as_mut().unwrap();
    assert!(!chat.busy && !chat.ready);
    assert_eq!(chat.rows[0].state, ReplyState::Refused);
    assert!(chat.take_abort().is_none());
    app.observe_live(Observation::ScheduledHeld("Decline read back".into()))
        .unwrap();
    assert!(app.live.as_ref().unwrap().ready);

    let mut admitted = ready();
    admitted
        .live
        .as_mut()
        .unwrap()
        .submit("exact local")
        .unwrap();
    admitted.live.as_mut().unwrap().take_submission().unwrap();
    admitted
        .observe_live(Observation::Submission {
            id: "owned-operation".into(),
        })
        .unwrap();
    admitted.live.as_mut().unwrap().request_stop().unwrap();
    assert_eq!(
        admitted.live.as_mut().unwrap().take_abort().as_deref(),
        Some("owned-operation")
    );
    assert!(admitted.observe_live(unsent("exact local")).is_err());
}
