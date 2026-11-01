//! Inspection cannot adopt saved context or reuse Open authority.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_browser as browser,
    history::{self, Anchor, PageRequest},
    history_view::{Intent, ReadKey, Target, Update, View},
    management::{Management, Step},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const CURRENT: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const SAVED: &str = "32345678-1234-4123-8123-123456789abc";
const OTHER: &str = "42345678-1234-4123-8123-123456789abc";
fn reply(command: &Value, result: Value) -> Value {
    json!({"type":"response","id":command["id"],"ok":true,"result":result})
}
fn write(step: Step) -> Value {
    match step {
        Step::Write(command) => command,
        other => panic!("Expected read, got {other:?}"),
    }
}
fn descriptor(saved: bool, readonly: bool, catalog: &str) -> Value {
    json!({"version":1,"kind":"history_snapshot","projection_version":1,
        "scope":if saved {"preview"} else {"current"},"from_session_id":CURRENT,
        "session_id":if saved {SAVED} else {CURRENT},"agent_id":AGENT,"backend":"custom","model":"active-model",
        "catalog_snapshot":if saved {json!(catalog)} else {Value::Null},"snapshot":if saved {"d".repeat(64)} else {"a".repeat(64)},
        "transcript_revision":"b".repeat(64),"context_sha256":if saved {"e".repeat(64)} else {"c".repeat(64)},
        "replay_sha256":if saved {"f".repeat(64)} else {"d".repeat(64)},"pending":false,"materialized":true,
        "title":if saved {"Other saved conversation"} else {"Active conversation"},"read_only":readonly,
        "open_effects":if saved {json!({"lifecycle_hooks":true,"provider_cleanup":true,"materialize":false})} else {Value::Null},
        "open_action_id":if saved {json!(OTHER)} else {Value::Null},"fresh_system_context":true,
        "counts":{"messages":0,"non_system":0,"system":0,"blocks":0}})
}
fn page(desc: &Value) -> Value {
    let mut value = json!({"version":1,"kind":"history_page","projection_version":1,"fragments":[],
        "previous_cursor":null,"next_cursor":null,"at_start":true,"at_end":true});
    for key in [
        "scope",
        "from_session_id",
        "session_id",
        "agent_id",
        "backend",
        "model",
        "catalog_snapshot",
        "snapshot",
        "context_sha256",
        "replay_sha256",
        "counts",
    ] {
        value[key] = desc[key].clone();
    }
    value
}
fn catalog(token: &str, located: bool, can_open: bool) -> Value {
    let mut value = json!({"session_id":CURRENT,"agent_id":AGENT,"snapshot":token,"offset":0,"total":2,
        "previous_cursor":null,"next_cursor":null,"entries":[
        {"session_id":CURRENT,"agent_id":AGENT,"agent_name":"Owner","title":"Active","message_count":0,
            "updated_at":2.0,"current":true,"can_open":false,"reason":"Already open"},
        {"session_id":SAVED,"agent_id":AGENT,"agent_name":"Owner","title":"Saved","message_count":0,
            "updated_at":1.0,"current":false,"can_open":can_open,"reason":if can_open {""} else {"Incompatible settings"}}
    ]});
    if located {
        value["located_session_id"] = json!(SAVED);
    }
    value
}
fn connected() -> Management {
    let mut manager = Management::new(None);
    let hello = manager
        .begin(&json!({"type":"ready","history_projection_version":1,
        "management_protocols":[1],"capabilities":["hello","state"],"conversation_id":CURRENT,
        "backend":"custom","model":"active-model","tool_profile":"strict"}))
        .unwrap();
    let state=write(manager.accept(reply(&hello,json!({"protocol_version":1,"data_version":1,
        "capabilities":["hello","state"],"operations":["gui.hello","gui.state",history::SNAPSHOT,
        history::PAGE,history::OPEN,browser::PAGE,browser::OPEN,browser::PREVIEW,litetui_terminal::restored_queue::READ]}))).unwrap());
    let snapshot=write(manager.accept(reply(&state,json!({"session_id":CURRENT,"busy":false,
        "mode":"normal","tool_profile":"strict","model":{"model":"active-model","backend":"custom"},
        "ownership":{"writable":true,"data_version":1,"agent_id":AGENT},"schedules_paused":false,
        "restored_input":{"policy":"hold","held":0},"conversations":[],"jobs":[],"tasks":[],"active_work":[],
        "pending_approvals":[],"pending_questions":[],"pending_model_loads":[]}))).unwrap());
    let desc = descriptor(false, false, "");
    let command = write(manager.accept(reply(&snapshot, desc.clone())).unwrap());
    assert!(matches!(
        manager.accept(reply(&command, page(&desc))).unwrap(),
        Step::Restored { .. }
    ));
    assert!(manager.ready());
    manager
}
fn browser_page(manager: &mut Management, can_open: bool) -> browser::Page {
    let command = manager.request_browser(browser::Intent::Refresh).unwrap();
    let Step::Browser(browser::Update::Page(page)) = manager
        .accept(reply(&command, catalog(&"a".repeat(64), false, can_open)))
        .unwrap()
    else {
        panic!()
    };
    page
}
fn key(generation: u64, saved: bool) -> ReadKey {
    ReadKey {
        generation,
        target: if saved {
            Target::Saved {
                session: SAVED.into(),
            }
        } else {
            Target::Current
        },
    }
}
fn inspect(manager: &mut Management, generation: u64, can_open: bool) -> Value {
    let page = browser_page(manager, can_open);
    manager
        .request_history(Intent::Inspect {
            key: key(generation, true),
            revision: page.revision,
            session: SAVED.into(),
        })
        .unwrap()
}
fn advance(manager: &mut Management, command: Value, phase: usize) -> (Value, Value) {
    let desc = descriptor(true, true, &"b".repeat(64));
    let command = if phase > 0 {
        assert_eq!(command["type"], browser::PAGE);
        assert_eq!(command["locate_session_id"], SAVED);
        assert!(command.get("cursor").is_none());
        write(
            manager
                .accept(reply(&command, catalog(&"b".repeat(64), true, false)))
                .unwrap(),
        )
    } else {
        command
    };
    let command = if phase > 1 {
        assert_eq!(command["type"], history::SNAPSHOT);
        assert_eq!(command["scope"], "preview");
        assert_eq!(command["session_id"], SAVED);
        assert_eq!(command["from_session_id"], CURRENT);
        assert_eq!(command["model"], "active-model");
        write(manager.accept(reply(&command, desc.clone())).unwrap())
    } else {
        command
    };
    (command, desc)
}
fn complete(manager: &mut Management, command: Value) -> Update {
    let (command, desc) = advance(manager, command, 2);
    assert_eq!(command["type"], history::PAGE);
    let Step::SavedHistory(update) = manager.accept(reply(&command, page(&desc))).unwrap() else {
        panic!()
    };
    update
}
#[test]
fn saved_readonly_and_incompatible_rows_use_reads_without_adopting_context_or_open_authority() {
    for can_open in [false, true] {
        let mut manager = connected();
        let command = inspect(&mut manager, 1, can_open);
        assert!(manager.prompt("while-reading").is_err());
        let Update::Loaded {
            key: observed,
            snapshot,
            ..
        } = complete(&mut manager, command)
        else {
            panic!()
        };
        assert_eq!(observed, key(1, true));
        assert!(snapshot.read_only);
        assert_eq!(snapshot.identity.session, SAVED);
        assert!(
            manager
                .request_browser(browser::Intent::ConfirmOpen {
                    snapshot: snapshot.token,
                    action_id: OTHER.into()
                })
                .is_err()
        );
        let read = manager
            .request_history(Intent::Refresh {
                key: key(2, false),
                anchor: Anchor::Tail,
            })
            .unwrap();
        assert_eq!(read["scope"], "current");
        assert_eq!(read["session_id"], CURRENT);
        assert_eq!(read["from_session_id"], CURRENT);
        assert!(read["catalog_snapshot"].is_null());
    }
}
#[test]
fn current_row_inspection_uses_current_scope_without_locator() {
    let mut manager = connected();
    let offered = browser_page(&mut manager, false);
    let read = manager
        .request_history(Intent::Inspect {
            key: key(1, false),
            revision: offered.revision,
            session: CURRENT.into(),
        })
        .unwrap();
    assert_eq!(read["type"], history::SNAPSHOT);
    assert_eq!(read["scope"], "current");
    assert_eq!(read["session_id"], CURRENT);
    assert!(read.get("locate_session_id").is_none());
}
#[test]
fn selection_and_generation_forgery_cannot_create_a_read_or_open_route() {
    let mut manager = connected();
    let offered = browser_page(&mut manager, false);
    assert!(
        manager
            .request_history(Intent::Refresh {
                key: key(1, true),
                anchor: Anchor::Tail
            })
            .is_err()
    );
    for (read_key, revision, session) in [
        (key(0, true), offered.revision, SAVED),
        (key(1, true), offered.revision + 1, SAVED),
        (key(1, true), offered.revision, OTHER),
        (key(1, false), offered.revision, SAVED),
    ] {
        assert!(
            manager
                .request_history(Intent::Inspect {
                    key: read_key,
                    revision,
                    session: session.into()
                })
                .is_err()
        );
        assert!(manager.ready());
    }
    assert!(
        manager
            .request_browser(browser::Intent::Inspect {
                revision: offered.revision,
                session: SAVED.into()
            })
            .is_err()
    );
    let command = manager
        .request_history(Intent::Inspect {
            key: key(1, true),
            revision: offered.revision,
            session: SAVED.into(),
        })
        .unwrap();
    complete(&mut manager, command);
    assert!(
        manager
            .request_history(Intent::Refresh {
                key: key(1, true),
                anchor: Anchor::Tail
            })
            .is_err()
    );
    let wrong = ReadKey {
        generation: 2,
        target: Target::Saved {
            session: OTHER.into(),
        },
    };
    assert!(
        manager
            .request_history(Intent::Refresh {
                key: wrong,
                anchor: Anchor::Tail
            })
            .is_err()
    );
}
#[test]
fn saved_refresh_relocates_same_uuid_and_uses_only_new_catalog_token() {
    let mut manager = connected();
    let command = inspect(&mut manager, 1, false);
    complete(&mut manager, command);
    let locate = manager
        .request_history(Intent::Refresh {
            key: key(2, true),
            anchor: Anchor::Head,
        })
        .unwrap();
    assert_eq!(locate["locate_session_id"], SAVED);
    assert_eq!(locate["session_id"], CURRENT);
    let mut reordered = catalog(&"c".repeat(64), true, false);
    reordered["entries"].as_array_mut().unwrap().reverse();
    let snapshot = write(manager.accept(reply(&locate, reordered)).unwrap());
    assert_eq!(snapshot["catalog_snapshot"], "c".repeat(64));
    assert_eq!(snapshot["session_id"], SAVED);
    let desc = descriptor(true, true, &"c".repeat(64));
    let read = write(manager.accept(reply(&snapshot, desc.clone())).unwrap());
    assert_eq!(read["anchor"], "head");
    assert!(
        matches!(manager.accept(reply(&read,page(&desc))).unwrap(),Step::SavedHistory(Update::Loaded{key:observed,..}) if observed==key(2,true))
    );
}
#[test]
fn malformed_locator_cannot_select_a_neighbor_or_foreign_owner() {
    for fault in ["echo", "agent", "active", "missing-row"] {
        let mut manager = connected();
        let command = inspect(&mut manager, 1, false);
        let mut result = catalog(&"b".repeat(64), true, false);
        match fault {
            "echo" => result["located_session_id"] = json!(OTHER),
            "agent" => {
                result["agent_id"] = json!(OTHER);
                for row in result["entries"].as_array_mut().unwrap() {
                    row["agent_id"] = json!(OTHER);
                }
            }
            "active" => result["session_id"] = json!(OTHER),
            "missing-row" => {
                result["entries"].as_array_mut().unwrap().pop();
                result["total"] = json!(1);
            }
            _ => unreachable!(),
        }
        assert!(manager.accept(reply(&command, result)).is_err(), "{fault}");
        assert!(!manager.ready());
        assert!(manager.prompt("must-not-run").is_err());
    }
}
#[test]
fn descriptor_and_page_identity_swaps_fault_before_publication() {
    for phase in [1, 2] {
        for field in [
            "session_id",
            "from_session_id",
            "agent_id",
            "backend",
            "model",
            "scope",
            "catalog_snapshot",
        ] {
            let mut manager = connected();
            let command = inspect(&mut manager, 1, false);
            let (command, desc) = advance(&mut manager, command, phase);
            let mut result = if phase == 1 { desc } else { page(&desc) };
            result[field] = match field {
                "scope" => json!("current"),
                "backend" => json!("claude"),
                "model" => json!("different-model"),
                "catalog_snapshot" => json!("9".repeat(64)),
                _ => json!(OTHER),
            };
            assert!(
                manager.accept(reply(&command, result)).is_err(),
                "{phase}/{field}"
            );
            assert!(!manager.ready());
        }
    }
}
fn draw(view: &mut View) {
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    let mut terminal = Terminal::new(TestBackend::new(80, 24)).unwrap();
    terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
}
#[test]
fn refusal_at_each_stage_retains_stale_view_but_retires_cursor_authority() {
    for phase in 0..3 {
        let mut manager = connected();
        let offered = browser_page(&mut manager, false);
        let mut view = View::default();
        view.inspect(offered.revision, SAVED.into(), false);
        let command = manager
            .request_history(view.take_intent().unwrap())
            .unwrap();
        view.observe(complete(&mut manager, command)).unwrap();
        let old_page = view.page.clone();
        draw(&mut view);
        view.key(KeyEvent::new(KeyCode::F(5), KeyModifiers::NONE));
        let intent = view.take_intent().unwrap();
        let pending = intent.key().clone();
        let command = manager.request_history(intent).unwrap();
        let (command, _) = advance(&mut manager, command, phase);
        let Step::SavedHistory(update) = manager
            .accept(
                json!({"type":"response","id":command["id"],"ok":false,"error":"Target changed"}),
            )
            .unwrap()
        else {
            panic!()
        };
        assert_eq!(update.key(), &pending);
        view.observe(update).unwrap();
        assert_eq!(view.page, old_page);
        assert!(view.is_stale());
        assert!(manager.ready());
        assert!(
            manager
                .request_history(Intent::Page {
                    key: key(3, true),
                    snapshot: "d".repeat(64),
                    position: PageRequest::Anchor(Anchor::Tail)
                })
                .is_err()
        );
        assert_eq!(
            manager
                .request_history(Intent::Refresh {
                    key: key(4, true),
                    anchor: Anchor::Tail
                })
                .unwrap()["type"],
            browser::PAGE
        );
    }
}
#[test]
fn closing_at_each_stage_never_issues_a_followup_or_admits_prompt() {
    for phase in 0..3 {
        let mut manager = connected();
        let command = inspect(&mut manager, 1, false);
        let (command, desc) = advance(&mut manager, command, phase);
        let result = match phase {
            0 => catalog(&"b".repeat(64), true, false),
            1 => desc,
            _ => page(&desc),
        };
        manager.begin_close();
        assert!(matches!(
            manager.accept(reply(&command, result)).unwrap(),
            Step::Ignore
        ));
        assert!(!manager.ready());
        assert!(manager.prompt("after-close").is_err());
    }
}
#[test]
fn full_original_envelopes_enforce_locate_and_history_limits_including_errors() {
    for phase in 0..3 {
        for error in [false, true] {
            for over in [false, true] {
                let mut manager = connected();
                let command = inspect(&mut manager, 1, false);
                let (command, desc) = advance(&mut manager, command, phase);
                let response = if error {
                    json!({"type":"response","id":command["id"],"ok":false,"error":"refused"})
                } else {
                    reply(
                        &command,
                        match phase {
                            0 => catalog(&"b".repeat(64), true, false),
                            1 => desc,
                            _ => page(&desc),
                        },
                    )
                };
                let maximum = if phase == 0 {
                    browser::PAGE_BYTES
                } else {
                    history::MAX_RESPONSE_BYTES
                };
                let mut raw = serde_json::to_vec(&response).unwrap();
                raw.resize(maximum + usize::from(over), b' ');
                let result = manager.accept_wire(serde_json::from_slice(&raw).unwrap(), raw.len());
                assert_eq!(
                    result.is_err(),
                    over,
                    "phase={phase} error={error} over={over}"
                );
            }
        }
    }
}
#[test]
fn admitted_inspection_retires_previously_offered_open_confirmation() {
    let mut manager = connected();
    let offered = browser_page(&mut manager, true);
    let preview = manager
        .request_browser(browser::Intent::Open {
            revision: offered.revision,
            session: SAVED.into(),
        })
        .unwrap();
    let desc = descriptor(true, false, &"a".repeat(64));
    let read = write(manager.accept(reply(&preview, desc.clone())).unwrap());
    assert!(matches!(
        manager.accept(reply(&read, page(&desc))).unwrap(),
        Step::Browser(browser::Update::Preview(_))
    ));
    let command = manager
        .request_history(Intent::Inspect {
            key: key(1, true),
            revision: offered.revision,
            session: SAVED.into(),
        })
        .unwrap();
    complete(&mut manager, command);
    assert!(
        manager
            .request_browser(browser::Intent::ConfirmOpen {
                snapshot: "d".repeat(64),
                action_id: OTHER.into()
            })
            .is_err()
    );
}

fn finish_with_page(manager: &mut Management, locate: Value, desc: &Value, first: Value) -> Update {
    let mut located = catalog(desc["catalog_snapshot"].as_str().unwrap(), true, true);
    located["entries"][1]["message_count"] = desc["counts"]["messages"].clone();
    let snapshot = write(manager.accept(reply(&locate, located)).unwrap());
    let read = write(manager.accept(reply(&snapshot, desc.clone())).unwrap());
    let Step::SavedHistory(update) = manager.accept(reply(&read, first)).unwrap() else {
        panic!()
    };
    assert!(manager.ready());
    update
}
#[test]
fn writable_inspection_descriptor_cannot_authorize_open_even_when_its_action_is_valid() {
    let mut manager = connected();
    let locate = inspect(&mut manager, 1, true);
    let desc = descriptor(true, false, &"b".repeat(64));
    let Update::Loaded {
        key: observed,
        snapshot,
        ..
    } = finish_with_page(&mut manager, locate, &desc, page(&desc))
    else {
        panic!()
    };
    assert_eq!(observed, key(1, true));
    assert!(!snapshot.read_only);
    // The action is independently valid: refusal must be from absent browser
    // review authority, not read-only history or a malformed action.
    let action = snapshot.open_request().unwrap();
    assert_eq!(action["type"], history::OPEN);
    assert_eq!(action["session_id"], SAVED);
    assert_eq!(action["history_snapshot"], snapshot.token);
    assert_eq!(
        action["action_id"],
        snapshot.open_action_id.as_ref().unwrap().as_str()
    );
    let refusal = manager
        .request_browser(browser::Intent::ConfirmOpen {
            snapshot: snapshot.token,
            action_id: snapshot.open_action_id.unwrap(),
        })
        .unwrap_err();
    assert!(refusal.to_string().contains("preview is stale"));
    assert!(manager.ready());
}
const SAVED_TEXT: &str = "abcdefghijkl";
fn paged_descriptor(saved: bool, catalog: &str) -> Value {
    let mut desc = descriptor(saved, false, catalog);
    desc["fresh_system_context"] = json!(false);
    desc["counts"] = json!({"messages":1,"non_system":1,"system":0,"blocks":1});
    desc
}
fn fragment_page(desc: &Value, start: usize, end: usize) -> Value {
    let sha = |text: &str| format!("{:x}", Sha256::digest(text.as_bytes()));
    let mut result = page(desc);
    result["fragments"] = json!([{
        "block_index":0,"block_id":"1".repeat(64),"message_index":0,
        "message_sha256":"2".repeat(64),"kind":"assistant","role":"assistant",
        "label":"Saved answer","state":"complete",
        "metadata":{"provider":null,"thread_id":null,"turn_id":null,"item_id":null,
            "tool_call_id":null,"ok":null,"duration_ms":null,"attachment_kind":null},
        "block_sha256":sha(SAVED_TEXT),
        "part":{"utf8_start":start,"utf8_end":end,"utf8_total":SAVED_TEXT.len(),
            "sha256":sha(&SAVED_TEXT[start..end])},"text":&SAVED_TEXT[start..end]
    }]);
    result["at_start"] = json!(start == 0);
    result["at_end"] = json!(end == SAVED_TEXT.len());
    result["previous_cursor"] = if start == 0 {
        Value::Null
    } else {
        json!(format!("before-{start}"))
    };
    result["next_cursor"] = if end == SAVED_TEXT.len() {
        Value::Null
    } else {
        json!(format!("after-{end}"))
    };
    result
}
fn paged_saved() -> (Management, Value, Box<history::Snapshot>) {
    let mut manager = connected();
    let locate = inspect(&mut manager, 1, true);
    let desc = paged_descriptor(true, &"b".repeat(64));
    let Update::Loaded { snapshot, .. } =
        finish_with_page(&mut manager, locate, &desc, fragment_page(&desc, 8, 12))
    else {
        panic!()
    };
    (manager, desc, snapshot)
}
fn cursor_intent(generation: u64, saved: bool, desc: &Value, cursor: &str) -> Intent {
    Intent::Page {
        key: key(generation, saved),
        snapshot: desc["snapshot"].as_str().unwrap().into(),
        position: PageRequest::Cursor(cursor.into()),
    }
}
fn accept_page(manager: &mut Management, read: &Value, value: Value, expected: ReadKey) {
    let Step::SavedHistory(Update::Page {
        key: observed,
        page,
    }) = manager.accept(reply(read, value.clone())).unwrap()
    else {
        panic!()
    };
    assert_eq!(observed, expected);
    assert_eq!(
        page.fragments[0].start,
        value["fragments"][0]["part"]["utf8_start"]
            .as_u64()
            .unwrap()
    );
    assert_eq!(
        page.fragments[0].end,
        value["fragments"][0]["part"]["utf8_end"].as_u64().unwrap()
    );
    assert_eq!(page.fragments[0].text, value["fragments"][0]["text"]);
    assert!(manager.ready());
}
#[test]
fn only_offered_cursors_can_page_back_and_forward_through_exact_fragments() {
    let (mut manager, desc, _) = paged_saved();
    assert!(
        manager
            .request_history(cursor_intent(2, true, &desc, "unoffered"))
            .is_err()
    );
    assert!(manager.ready());
    for (generation, cursor, start, end) in [
        (2, "before-8", 4, 8),
        (3, "before-4", 0, 4),
        (4, "after-4", 4, 8),
        (5, "after-8", 8, 12),
    ] {
        let read = manager
            .request_history(cursor_intent(generation, true, &desc, cursor))
            .unwrap();
        assert_eq!(read["type"], history::PAGE);
        assert_eq!(read["session_id"], SAVED);
        assert_eq!(read["from_session_id"], CURRENT);
        assert_eq!(read["snapshot"], desc["snapshot"]);
        assert_eq!(read["cursor"], cursor);
        assert!(read.get("anchor").is_none());
        accept_page(
            &mut manager,
            &read,
            fragment_page(&desc, start, end),
            key(generation, true),
        );
    }
    // A cursor from an older visible page is no longer authority.
    assert!(
        manager
            .request_history(cursor_intent(6, true, &desc, "after-4"))
            .is_err()
    );
    assert!(manager.ready());
}
#[test]
fn refreshed_snapshot_and_current_target_reject_prior_saved_cursor_authority() {
    let (mut manager, old, _) = paged_saved();
    let locate = manager
        .request_history(Intent::Refresh {
            key: key(2, true),
            anchor: Anchor::Tail,
        })
        .unwrap();
    let mut fresh = paged_descriptor(true, &"c".repeat(64));
    fresh["snapshot"] = json!("9".repeat(64));
    let mut fresh_tail = fragment_page(&fresh, 8, 12);
    fresh_tail["previous_cursor"] = json!("fresh-before-8");
    finish_with_page(&mut manager, locate, &fresh, fresh_tail);
    for intent in [
        cursor_intent(3, true, &old, "before-8"),
        cursor_intent(3, true, &fresh, "before-8"),
    ] {
        assert!(manager.request_history(intent).is_err());
        assert!(manager.ready());
    }
    let read = manager
        .request_history(cursor_intent(3, true, &fresh, "fresh-before-8"))
        .unwrap();
    accept_page(
        &mut manager,
        &read,
        fragment_page(&fresh, 4, 8),
        key(3, true),
    );
    let snapshot = manager
        .request_history(Intent::Refresh {
            key: key(4, false),
            anchor: Anchor::Tail,
        })
        .unwrap();
    let current = paged_descriptor(false, "");
    let read = write(manager.accept(reply(&snapshot, current.clone())).unwrap());
    let mut current_tail = fragment_page(&current, 8, 12);
    current_tail["previous_cursor"] = json!("current-before-8");
    assert!(
        matches!(manager.accept(reply(&read, current_tail)).unwrap(), Step::SavedHistory(Update::Loaded { key: observed, .. }) if observed == key(4, false))
    );
    for intent in [
        cursor_intent(5, true, &fresh, "before-4"),
        cursor_intent(5, false, &fresh, "before-4"),
        cursor_intent(5, false, &current, "before-4"),
    ] {
        assert!(manager.request_history(intent).is_err());
        assert!(manager.ready());
    }
    let read = manager
        .request_history(cursor_intent(5, false, &current, "current-before-8"))
        .unwrap();
    assert_eq!(read["scope"], "current");
    assert_eq!(read["session_id"], CURRENT);
    accept_page(
        &mut manager,
        &read,
        fragment_page(&current, 4, 8),
        key(5, false),
    );
}
#[test]
fn offered_cursor_replies_cannot_publish_gaps_overlaps_or_changed_block_identity() {
    for forward in [false, true] {
        for fault in ["gap", "overlap", "changed-block"] {
            let (mut manager, desc, snapshot) = paged_saved();
            let generation = if forward {
                let read = manager
                    .request_history(Intent::Page {
                        key: key(2, true),
                        snapshot: desc["snapshot"].as_str().unwrap().into(),
                        position: PageRequest::Anchor(Anchor::Head),
                    })
                    .unwrap();
                accept_page(
                    &mut manager,
                    &read,
                    fragment_page(&desc, 0, 4),
                    key(2, true),
                );
                3
            } else {
                2
            };
            let cursor = if forward { "after-4" } else { "before-8" };
            let read = manager
                .request_history(cursor_intent(generation, true, &desc, cursor))
                .unwrap();
            let (start, end) = match (forward, fault) {
                (true, "gap") => (8, 12),
                (false, "gap") => (0, 4),
                (true, "overlap") => (2, 6),
                (false, "overlap") => (6, 10),
                _ => (4, 8),
            };
            let mut wrong = fragment_page(&desc, start, end);
            if fault == "changed-block" {
                wrong["fragments"][0]["block_id"] = json!("3".repeat(64));
            }
            // Each response is valid independently: only cross-page continuity fails.
            history::Page::parse(&wrong, &snapshot).unwrap();
            let refusal = manager.accept(reply(&read, wrong)).unwrap_err();
            assert!(
                refusal.to_string().contains("exact offered neighbor"),
                "{forward}/{fault}: {refusal}"
            );
            assert!(!manager.ready());
            assert!(manager.prompt("cannot-use-invalid-page").is_err());
        }
    }
}
