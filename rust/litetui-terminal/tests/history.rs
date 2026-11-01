use litetui_terminal::history::{
    self, Anchor, Identity, OpenResult, Page, PageRequest, Scope, Snapshot,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const TARGET: &str = "32345678-1234-4123-8123-123456789abc";
fn sha(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
fn id() -> Identity {
    Identity {
        from_session: SESSION.into(),
        session: SESSION.into(),
        agent: AGENT.into(),
        backend: "custom".into(),
        model: "test".into(),
        scope: Scope::Current,
        catalog: None,
    }
}
fn descriptor() -> Value {
    json!({"version":1,"kind":"history_snapshot","projection_version":1,"scope":"current","from_session_id":SESSION,"session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","catalog_snapshot":null,"snapshot":"a".repeat(64),"transcript_revision":"b".repeat(64),"context_sha256":"c".repeat(64),"replay_sha256":"d".repeat(64),"pending":false,"materialized":true,"title":"Saved test","read_only":false,"open_effects":null,"open_action_id":null,"fresh_system_context":false,"counts":{"messages":1001,"non_system":101,"system":900,"blocks":2}})
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&descriptor(), &id()).unwrap()
}
fn fragment(index: u64, full: &str, start: usize, end: usize) -> Value {
    json!({"block_index":index,"block_id":format!("{:064x}",index+1),"message_index":900+index,"message_sha256":"e".repeat(64),"kind":"assistant","role":"assistant","label":"Answer","state":"complete","metadata":{"provider":null,"thread_id":null,"turn_id":null,"item_id":null,"tool_call_id":null,"ok":null,"duration_ms":null,"attachment_kind":null},"block_sha256":sha(full),"part":{"utf8_start":start,"utf8_end":end,"utf8_total":full.len(),"sha256":sha(&full[start..end])},"text":&full[start..end]})
}
fn page(fragments: Vec<Value>, start: bool, end: bool) -> Value {
    let d = descriptor();
    json!({"version":1,"kind":"history_page","projection_version":1,"scope":"current","from_session_id":SESSION,"session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","catalog_snapshot":null,"snapshot":d["snapshot"],"context_sha256":d["context_sha256"],"replay_sha256":d["replay_sha256"],"counts":d["counts"],"fragments":fragments,"previous_cursor":if start{Value::Null}else{json!("previous")},"next_cursor":if end{Value::Null}else{json!("next")},"at_start":start,"at_end":end})
}
#[test]
fn whole_context_is_independent_of_resident_page_and_title_revision() {
    let before = snapshot();
    let mut d = descriptor();
    d["title"] = json!("Renamed");
    d["transcript_revision"] = json!("f".repeat(64));
    assert!(
        Snapshot::parse(&d, &id())
            .unwrap()
            .preserves_context(&before)
    );
    d["context_sha256"] = json!("f".repeat(64));
    assert!(
        !Snapshot::parse(&d, &id())
            .unwrap()
            .preserves_context(&before)
    );
    let p = Page::parse(&page(vec![fragment(1, "last", 0, 4)], false, true), &before).unwrap();
    assert_eq!(p.fragments.len(), 1);
    assert_eq!(p.counts.messages, 1001);
    assert_eq!(p.counts.non_system, 101);
}
#[test]
fn descriptor_rejects_foreign_or_contradictory_full_context_proofs() {
    for fault in [
        "agent",
        "session",
        "scope",
        "catalog",
        "version",
        "count",
        "fresh",
        "revision",
        "pending",
        "materialized",
        "hash",
        "missing-readonly",
    ] {
        let mut d = descriptor();
        match fault {
            "agent" => d["agent_id"] = json!(SESSION),
            "session" => d["session_id"] = json!(TARGET),
            "scope" => d["scope"] = json!("preview"),
            "catalog" => d["catalog_snapshot"] = json!("a".repeat(64)),
            "version" => d["projection_version"] = json!(2),
            "count" => d["counts"]["system"] = json!(0),
            "fresh" => d["fresh_system_context"] = json!(true),
            "revision" => d["transcript_revision"] = Value::Null,
            "pending" => d["pending"] = json!(true),
            "materialized" => d["materialized"] = json!(false),
            "hash" => d["context_sha256"] = json!("C".repeat(64)),
            "missing-readonly" => {
                d.as_object_mut().unwrap().remove("read_only");
            }
            _ => unreachable!(),
        }
        assert!(Snapshot::parse(&d, &id()).is_err(), "{fault}");
    }
}
#[test]
fn pending_empty_context_is_valid_without_materialization_or_provider_references() {
    let mut d = descriptor();
    d["pending"] = json!(true);
    d["materialized"] = json!(false);
    d["transcript_revision"] = Value::Null;
    d["counts"] = json!({"messages":0,"non_system":0,"system":0,"blocks":0});
    d["fresh_system_context"] = json!(true);
    let s = Snapshot::parse(&d, &id()).unwrap();
    let mut p = page(vec![], true, true);
    p["counts"] = d["counts"].clone();
    assert!(Page::parse(&p, &s).is_ok());
    d["fresh_system_context"] = json!(false);
    assert!(Snapshot::parse(&d, &id()).is_err());
}
#[test]
fn read_only_preview_is_inspectable_but_cannot_open() {
    let mut expected = id();
    expected.scope = Scope::Preview;
    expected.session = TARGET.into();
    expected.catalog = Some("f".repeat(64));
    let mut d = descriptor();
    d["scope"] = json!("preview");
    d["session_id"] = json!(TARGET);
    d["catalog_snapshot"] = json!("f".repeat(64));
    d["open_effects"] = json!({"lifecycle_hooks":true,"provider_cleanup":true,"materialize":false});
    d["open_action_id"] = json!(AGENT);
    d["read_only"] = json!(true);
    let s = Snapshot::parse(&d, &expected).unwrap();
    assert!(s.read_only);
    assert!(s.open_request().is_err());
    assert!(s.page_request(&PageRequest::Anchor(Anchor::Tail)).is_ok());
    d["read_only"] = json!(false);
    let s = Snapshot::parse(&d, &expected).unwrap();
    assert_eq!(s.open_request().unwrap()["type"], history::OPEN);
}
#[test]
fn utf8_fragments_traverse_both_directions_without_gaps_duplicates_or_text_dedup() {
    let s = snapshot();
    let full = "same 😀 same";
    let first = Page::parse(&page(vec![fragment(0, full, 0, 5)], true, false), &s).unwrap();
    let middle = Page::parse(
        &page(vec![fragment(0, full, 5, full.len())], false, false),
        &s,
    )
    .unwrap();
    middle.follows(&first, "next").unwrap();
    first.follows(&middle, "previous").unwrap();
    let last = Page::parse(
        &page(vec![fragment(1, full, 0, full.len())], false, true),
        &s,
    )
    .unwrap();
    last.follows(&middle, "next").unwrap();
    assert_eq!(
        middle.fragments[0].block_sha256,
        last.fragments[0].block_sha256
    );
    assert_ne!(middle.fragments[0].block_id, last.fragments[0].block_id);
    assert!(last.follows(&first, "next").is_err());
    assert!(middle.follows(&first, "foreign-cursor").is_err());
}
#[test]
fn page_rejects_tampered_digest_ranges_order_metadata_and_snapshot() {
    for fault in [
        "sha",
        "end",
        "total",
        "block-sha",
        "order",
        "duplicate",
        "boundary",
        "cursor",
        "snapshot",
        "counts",
        "kind",
        "state",
        "metadata",
        "budget",
    ] {
        let mut p = page(
            vec![fragment(0, "one", 0, 3), fragment(1, "two", 0, 3)],
            true,
            true,
        );
        match fault {
            "sha" => p["fragments"][0]["part"]["sha256"] = json!("f".repeat(64)),
            "end" => p["fragments"][0]["part"]["utf8_end"] = json!(4),
            "total" => p["fragments"][0]["part"]["utf8_total"] = json!(2),
            "block-sha" => p["fragments"][0]["block_sha256"] = json!("f".repeat(64)),
            "order" => p["fragments"].as_array_mut().unwrap().reverse(),
            "duplicate" => p["fragments"][1] = p["fragments"][0].clone(),
            "boundary" => p["at_end"] = json!(false),
            "cursor" => p["next_cursor"] = json!("forged"),
            "snapshot" => p["snapshot"] = json!("f".repeat(64)),
            "counts" => p["counts"]["blocks"] = json!(3),
            "kind" => p["fragments"][0]["kind"] = json!("approval"),
            "state" => p["fragments"][0]["state"] = json!("running"),
            "metadata" => p["fragments"][0]["metadata"]["command"] = json!("never"),
            "budget" => p["fragments"][0] = fragment(0, &"\n".repeat(1100), 0, 1100),
            _ => unreachable!(),
        }
        assert!(Page::parse(&p, &snapshot()).is_err(), "{fault}");
    }
}
#[test]
fn compact_open_binds_preview_then_requires_settled_current_descriptor() {
    let mut identity = id();
    identity.scope = Scope::Preview;
    identity.session = TARGET.into();
    identity.catalog = Some("f".repeat(64));
    let mut preview = descriptor();
    preview["session_id"] = json!(TARGET);
    preview["scope"] = json!("preview");
    preview["catalog_snapshot"] = json!("f".repeat(64));
    preview["open_effects"] =
        json!({"lifecycle_hooks":true,"provider_cleanup":true,"materialize":false});
    preview["open_action_id"] = json!(AGENT);
    let s = Snapshot::parse(&preview, &identity).unwrap();
    let mut current = descriptor();
    current["from_session_id"] = json!(TARGET);
    current["session_id"] = json!(TARGET);
    let mut receipt = json!({"version":1,"kind":"conversation_open","from_session_id":SESSION,"session_id":TARGET,"target_session_id":TARGET,"agent_id":AGENT,"backend":"custom","model":"test","catalog_snapshot":"f".repeat(64),"history_snapshot":s.token,"action_id":AGENT,"status":"connected","mutated":true,"close_required":false,"error":null,"worker_settled":true,"connection_settled":true,"history_settled":true,"history":current});
    assert!(matches!(
        OpenResult::parse(&receipt, &s).unwrap(),
        OpenResult::Connected(_)
    ));
    receipt["model"] = json!("saved-model");
    receipt["history"]["model"] = json!("saved-model");
    assert!(OpenResult::parse(&receipt, &s).is_err());
    receipt["model"] = json!("test");
    receipt["history"]["model"] = json!("test");
    receipt["history_settled"] = json!(false);
    assert!(OpenResult::parse(&receipt, &s).is_err());
    receipt["status"] = json!("refused");
    receipt["mutated"] = json!(false);
    receipt["session_id"] = json!(SESSION);
    receipt["history"] = Value::Null;
    receipt["error"] = json!("stale preview");
    assert!(matches!(
        OpenResult::parse(&receipt, &s).unwrap(),
        OpenResult::Refused(_)
    ));
    receipt["target_session_id"] = json!(SESSION);
    assert!(OpenResult::parse(&receipt, &s).is_err());
}

mod viewer {
    use super::*;
    use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
    use litetui_terminal::history_view::{Intent, ReadKey, Target, Update, View};
    use ratatui::{Terminal, backend::TestBackend};
    fn draw(v: &mut View, w: u16, h: u16) -> String {
        let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
        let theme = litetui_terminal::theme::builtin_themes().remove(0);
        t.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        t.backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect()
    }
    fn key(v: &mut View, k: KeyCode) {
        v.key(KeyEvent::new(k, KeyModifiers::NONE));
    }
    fn loaded() -> View {
        let mut v = View::default();
        v.open();
        let key = v.take_intent().unwrap().key().clone();
        let p = Page::parse(
            &page(vec![fragment(1, "saved answer", 0, 12)], false, true),
            &snapshot(),
        )
        .unwrap();
        v.observe(Update::Loaded {
            key,
            snapshot: Box::new(snapshot()),
            page: Box::new(p),
        })
        .unwrap();
        v
    }
    #[test]
    fn exclusive_history_paging_uses_offered_cursor_without_execution_intent() {
        let mut v = loaded();
        assert!(draw(&mut v, 80, 24).contains("Saved History"));
        key(&mut v, KeyCode::Left);
        assert!(
            matches!(v.take_intent(),Some(Intent::Page{position:PageRequest::Cursor(s),..}) if s=="previous")
        );
        key(&mut v, KeyCode::Enter);
        assert!(v.take_intent().is_none());
    }
    #[test]
    fn cancelled_late_read_never_reopens_and_stale_page_is_retained_until_refresh() {
        let mut v = loaded();
        draw(&mut v, 80, 24);
        key(&mut v, KeyCode::Left);
        let pending = v.take_intent().unwrap().key().clone();
        key(&mut v, KeyCode::Esc);
        assert!(v.owns_input());
        v.observe(Update::Rejected {
            key: pending,
            reason: "late".into(),
        })
        .unwrap();
        assert!(!v.owns_input());
        let mut v = loaded();
        let original = v.page.clone();
        draw(&mut v, 80, 24);
        key(&mut v, KeyCode::Left);
        let pending = v.take_intent().unwrap().key().clone();
        v.observe(Update::Rejected {
            key: pending,
            reason: "source snapshot changed".into(),
        })
        .unwrap();
        assert_eq!(v.page, original);
        assert!(v.is_stale());
        draw(&mut v, 80, 24);
        key(&mut v, KeyCode::Left);
        assert!(v.take_intent().is_none());
        key(&mut v, KeyCode::F(5));
        assert!(matches!(v.take_intent(), Some(Intent::Refresh { .. })));
    }
    fn rejected(key: ReadKey, reason: &str) -> Update {
        Update::Rejected {
            key,
            reason: reason.into(),
        }
    }
    fn saved_loaded(key: ReadKey, read_only: bool) -> Update {
        let mut identity = id();
        identity.scope = Scope::Preview;
        identity.session = TARGET.into();
        identity.catalog = Some("f".repeat(64));
        let mut raw = descriptor();
        raw["session_id"] = json!(TARGET);
        raw["scope"] = json!("preview");
        raw["catalog_snapshot"] = json!("f".repeat(64));
        raw["read_only"] = json!(read_only);
        raw["open_effects"] =
            json!({"lifecycle_hooks":true,"provider_cleanup":true,"materialize":false});
        raw["open_action_id"] = json!(AGENT);
        let snapshot = Snapshot::parse(&raw, &identity).unwrap();
        let mut raw_page = page(vec![fragment(1, "saved answer", 0, 12)], false, true);
        raw_page["session_id"] = json!(TARGET);
        raw_page["scope"] = json!("preview");
        raw_page["catalog_snapshot"] = json!("f".repeat(64));
        let page = Page::parse(&raw_page, &snapshot).unwrap();
        Update::Loaded {
            key,
            snapshot: Box::new(snapshot),
            page: Box::new(page),
        }
    }
    #[test]
    fn saved_inspection_binds_revision_session_and_read_only_display_without_open_authority() {
        for read_only in [false, true] {
            let mut view = View::default();
            view.inspect(17, TARGET.into(), false);
            let intent = view.take_intent().unwrap();
            let pending = intent.key().clone();
            assert_eq!(
                intent,
                Intent::Inspect {
                    key: pending.clone(),
                    revision: 17,
                    session: TARGET.into()
                }
            );
            assert_eq!(
                pending.target,
                Target::Saved {
                    session: TARGET.into()
                }
            );
            view.observe(saved_loaded(pending, read_only)).unwrap();
            let rendered = draw(&mut view, 80, 24);
            assert!(rendered.contains("saved answer"));
            assert!(!rendered.contains("Enter: open"));
            key(&mut view, KeyCode::Enter);
            assert!(view.take_intent().is_none());
            key(&mut view, KeyCode::F(5));
            let refresh = view.take_intent().unwrap();
            assert!(matches!(refresh, Intent::Refresh { .. }));
            assert_eq!(
                refresh.key().target,
                Target::Saved {
                    session: TARGET.into()
                }
            );
        }
    }
    #[test]
    fn generations_survive_finish_and_stale_updates_do_not_retire_newer_reads() {
        let mut view = View::default();
        view.inspect(3, TARGET.into(), false);
        let old = view.take_intent().unwrap().key().clone();
        view.finish();
        view.inspect(4, TARGET.into(), false);
        let next = view.take_intent().unwrap().key().clone();
        assert!(next.generation > old.generation);
        let notice = view.notice.clone();
        view.observe(rejected(old.clone(), "old refusal")).unwrap();
        view.observe(saved_loaded(old, false)).unwrap();
        assert!(view.waiting());
        assert_eq!(view.pending_key(), Some(&next));
        assert_eq!(view.notice, notice);
        assert!(view.page.is_none());
        let mut wrong_target = next.clone();
        wrong_target.target = Target::Current;
        view.observe(rejected(wrong_target, "wrong target"))
            .unwrap();
        assert_eq!(view.pending_key(), Some(&next));
        view.observe(saved_loaded(next.clone(), true)).unwrap();
        assert!(!view.waiting());
        let retained = view.page.clone();
        view.observe(rejected(next, "duplicate refusal")).unwrap();
        assert_eq!(view.page, retained);
        assert!(!view.is_stale());
    }
    #[test]
    fn current_inspect_and_f6_after_saved_inspection_have_distinct_monotonic_reads() {
        let mut view = View::default();
        view.inspect(3, SESSION.into(), true);
        let initial = view.take_intent().unwrap();
        assert_eq!(initial.key().target, Target::Current);
        assert!(
            matches!(initial, Intent::Inspect { revision: 3, ref session, .. } if session == SESSION)
        );
        view.finish();
        view.inspect(4, TARGET.into(), false);
        let saved = view.take_intent().unwrap().key().clone();
        view.observe(saved_loaded(saved.clone(), false)).unwrap();
        view.close();
        view.open();
        let current = view.take_intent().unwrap();
        assert_eq!(current.key().target, Target::Current);
        assert!(current.key().generation > saved.generation);
        assert!(view.page.is_none() && view.snapshot.is_none());
        view.observe(saved_loaded(saved, false)).unwrap();
        assert!(view.page.is_none() && view.waiting());
    }
    #[test]
    fn close_before_send_retires_only_unsent_key_and_close_after_send_holds_input() {
        let mut view = View::default();
        view.inspect(1, TARGET.into(), false);
        let unsent = view.pending_key().unwrap().clone();
        view.close();
        assert!(!view.owns_input() && view.pending_key().is_none());
        view.inspect(2, TARGET.into(), false);
        let sent = view.take_intent().unwrap().key().clone();
        view.close();
        view.observe(rejected(unsent, "late unsent")).unwrap();
        assert!(view.owns_input() && !view.is_open());
        assert_eq!(view.pending_key(), Some(&sent));
        view.observe(saved_loaded(sent, true)).unwrap();
        assert!(!view.owns_input() && view.page.is_none());
    }
    #[test]
    fn matching_key_cannot_adopt_a_different_saved_target() {
        let mut view = View::default();
        view.inspect(1, TARGET.into(), false);
        let pending = view.take_intent().unwrap().key().clone();
        let mut wrong = saved_loaded(pending.clone(), false);
        if let Update::Loaded { snapshot, page, .. } = &mut wrong {
            snapshot.identity.session = SESSION.into();
            page.identity.session = SESSION.into();
        }
        assert!(view.observe(wrong).is_err());
        assert_eq!(view.pending_key(), Some(&pending));
        assert!(view.page.is_none() && view.waiting());
    }
    #[test]
    fn each_page_and_refresh_has_one_new_key_and_repeated_keys_cannot_release_input() {
        let mut view = loaded();
        draw(&mut view, 80, 24);
        key(&mut view, KeyCode::Left);
        let page_read = view.take_intent().unwrap();
        assert!(matches!(page_read, Intent::Page { .. }));
        let page_key = page_read.key().clone();
        draw(&mut view, 80, 24);
        for _ in 0..3 {
            key(&mut view, KeyCode::F(5));
        }
        assert!(view.take_intent().is_none());
        assert_eq!(view.pending_key(), Some(&page_key));
        view.observe(rejected(page_key.clone(), "source changed"))
            .unwrap();
        let retained = view.page.clone();
        draw(&mut view, 80, 24);
        key(&mut view, KeyCode::F(5));
        let refresh = view.take_intent().unwrap();
        assert!(matches!(refresh, Intent::Refresh { .. }));
        assert!(refresh.key().generation > page_key.generation);
        view.observe(Update::Page {
            key: page_key,
            page: Box::new(retained.clone().unwrap()),
        })
        .unwrap();
        assert_eq!(view.pending_key(), Some(refresh.key()));
        assert!(view.waiting() && view.is_stale());
        assert_eq!(view.page, retained);
        assert!(draw(&mut view, 36, 14).contains("Esc"));
        key(&mut view, KeyCode::Esc);
        assert!(view.owns_input() && !view.is_open());
        view.observe(rejected(refresh.key().clone(), "settled after close"))
            .unwrap();
        assert!(!view.owns_input());
    }
    #[test]
    fn compact_resize_retains_visible_modal_escape_and_inert_controls() {
        let mut v = loaded();
        for (w, h) in [(36, 14), (41, 18), (42, 17)] {
            assert!(draw(&mut v, w, h).contains("Esc"));
            key(&mut v, KeyCode::Enter);
            assert!(v.take_intent().is_none());
        }
        assert!(draw(&mut v, 42, 18).contains("Saved History"));
        key(&mut v, KeyCode::Esc);
        assert!(!v.is_open());
    }
}

#[test]
fn actual_native_launch_requires_the_requested_projection_version() {
    history::require_projection(&json!({"history_projection_version":1})).unwrap();
    for value in [
        Value::Null,
        json!(false),
        json!(true),
        json!("1"),
        json!(0),
        json!(2),
    ] {
        assert!(history::require_projection(&json!({"history_projection_version":value})).is_err());
    }
    assert!(history::require_projection(&json!({})).is_err());
}
#[test]
fn captured_open_shutdown_identity_uses_the_departing_session() {
    let command = json!({"type":history::OPEN,"action_id":TARGET,"from_session_id":SESSION,
        "session_id":TARGET,"agent_id":AGENT,"backend":"custom","model":"test"});
    let captured = history::shutdown_identity(&command).unwrap();
    assert_eq!(captured["action"], "open");
    assert_eq!(captured["session_id"], SESSION);
    assert_eq!(captured["target_session_id"], TARGET);
    let proof = json!({"action":"open","action_id":TARGET,"from_session_id":SESSION,
        "target_session_id":TARGET,"current_session_id":TARGET,"agent_id":AGENT,"backend":"custom","model":"test",
        "version":1,"worker_settled":true,"lifecycle_settled":true,"native_cleanup_settled":true,
        "effects_verified":false,"pending_at_close":true});
    litetui_terminal::conversation_actions::verify_shutdown(
        &json!({"conversation_shutdown":proof}),
        Some(&captured),
    )
    .unwrap();
    let mut wrong = proof;
    wrong["action_id"] = json!(SESSION);
    assert!(
        litetui_terminal::conversation_actions::verify_shutdown(
            &json!({"conversation_shutdown":wrong}),
            Some(&captured)
        )
        .is_err()
    );
}

#[test]
fn projected_startup_requires_only_bounded_operations_and_keeps_full_context_separate() {
    use litetui_terminal::management::{Management, Step};
    for read_only in [false, true] {
        let mut manager = Management::new(None);
        let hello = manager
            .begin(&json!({"type":"ready","history_projection_version":1,
            "management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,
            "backend":"custom","model":"test","tool_profile":"strict"}))
            .unwrap();
        let Step::Write(state) = manager
            .accept(
                json!({"type":"response","id":hello["id"],"ok":true,"result":{
            "protocol_version":1,"data_version":1,"capabilities":["hello","state"],
            "operations":["gui.hello","gui.state",history::SNAPSHOT,history::PAGE,history::OPEN]}}),
            )
            .unwrap()
        else {
            panic!()
        };
        let Step::Write(read)=manager.accept(json!({"type":"response","id":state["id"],"ok":true,"result":{
            "session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict","model":{"model":"test","backend":"custom"},
            "ownership":{"writable":true,"data_version":1,"agent_id":AGENT},"schedules_paused":false,
            "restored_input":{"policy":"hold","held":0},"conversations":[],"jobs":[],"tasks":[],
            "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[]}})).unwrap() else {panic!()};
        assert_eq!(read["type"], history::SNAPSHOT);
        let mut desc = descriptor();
        desc["read_only"] = json!(read_only);
        let result =
            manager.accept(json!({"type":"response","id":read["id"],"ok":true,"result":desc}));
        if read_only {
            assert!(result.is_err());
            assert!(!manager.ready());
            continue;
        }
        let Step::Write(read) = result.unwrap() else {
            panic!()
        };
        assert_eq!(read["type"], history::PAGE);
        assert_eq!(read["anchor"], "tail");
        let Step::Restored{history,..}=manager.accept(json!({"type":"response","id":read["id"],"ok":true,"result":page(vec![fragment(1,"tail",0,4)],false,true)})).unwrap() else {panic!()};
        assert!(history.messages.is_empty());
        assert_eq!(history.descriptor.unwrap().counts.messages, 1001);
        assert_eq!(history.page.unwrap().fragments.len(), 1);
        assert!(manager.ready());
    }
}

#[test]
fn raw_response_size_excludes_framing_but_not_padding_or_escape_expansion() {
    for ending in [b"\n".as_slice(), b"\r\n".as_slice()] {
        let mut exact = vec![b' '; history::MAX_RESPONSE_BYTES];
        exact.extend_from_slice(ending);
        assert_eq!(
            history::wire_response_bytes(&exact),
            history::MAX_RESPONSE_BYTES
        );
        exact.insert(0, b' ');
        assert_eq!(
            history::wire_response_bytes(&exact),
            history::MAX_RESPONSE_BYTES + 1
        );
    }
    assert_eq!(history::wire_response_bytes(br#"{"text":"\u96ea"}"#), 17);
}
