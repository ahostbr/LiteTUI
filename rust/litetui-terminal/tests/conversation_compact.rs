use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    conversation_compact::*,
    history::{Identity, Scope},
    history_view::{ReadKey, Target},
    theme::builtin_themes,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
const ROOT: &str = "42345678-1234-4123-8123-123456789abc";
fn sha(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
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
        messages: 7,
    }
}
fn snapshot_value() -> Value {
    let config = json!({"keep_recent":2,"max_tokens":1024,"max_tool_iters":4,"thinking_level":"default","tools_enabled":true,"wake_after_compact":false,"clear_screen_after_compact":true});
    let mut v = selection().command(SNAPSHOT);
    for(k,x)in json!({"kind":"conversation_compact_snapshot","snapshot":"c".repeat(64),"eligible":true,"reason":null,"config":config,"plan":{"messages":7,"system_messages":1,"head_messages":4,"tail_messages":2,"head_context_sha256":"d".repeat(64),"tail_context_sha256":"e".repeat(64),"prompt_sha256":"f".repeat(64),"config_sha256":sha(&serde_json::to_string(&config).unwrap())}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn snapshot() -> Snapshot {
    Snapshot::parse(&snapshot_value(), &selection()).unwrap()
}
fn review_value(hint: &str) -> Value {
    let before = snapshot_value();
    let mut v = snapshot().request(hint);
    for(k,x)in json!({"kind":"conversation_compact_review","plan":before["plan"],"config":before["config"],"review":"1".repeat(64),"action_id":ACTION,"action":"compact","root_execution_id":ROOT,"hint_sha256":sha(hint),"request_prompt_sha256":if hint.is_empty(){"f".repeat(64)}else{"2".repeat(64)},"effects":{"provider_execution":true,"provider_rewind":false,"tools_enabled":true,"wake_after_compact":false,"clear_screen_after_compact":true}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn review() -> Review {
    Review::parse(&review_value("retain 雪"), snapshot(), "retain 雪").unwrap()
}
fn history(context: &str) -> Value {
    json!({"version":1,"kind":"history_snapshot","projection_version":1,"scope":"current","from_session_id":SESSION,"session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","catalog_snapshot":null,"snapshot":"3".repeat(64),"transcript_revision":"4".repeat(64),"context_sha256":context,"replay_sha256":"5".repeat(64),"pending":false,"materialized":true,"title":"Current","read_only":false,"open_effects":null,"open_action_id":null,"fresh_system_context":false,"counts":{"messages":3,"system":1,"non_system":2,"blocks":2}})
}
fn admission_value(status: &str) -> Value {
    let mut v = review().request();
    for(k,x)in json!({"kind":"conversation_compact_apply","operation_id":"op","status":status,"reason":if status=="accepted"{Value::Null}else{json!("refused or uncertain")},"close_required":status=="failed","refusal_history":if status=="refused"{history(&"8".repeat(64))}else{Value::Null}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
fn admitted() -> Admission {
    Admission::parse(&admission_value("accepted"), &review(), "op").unwrap()
}
fn settled_value() -> Value {
    let mut v = review().request();
    for(k,x)in json!({"kind":"conversation_compact_settled","operation_id":"op","status":"accepted","worker_settled":true,"close_required":false,"outcome":"compacted","reason_code":"compacted","reason":null,"history_changed":true,"history":history(&"9".repeat(64)),"root_history":history(&"9".repeat(64)),"witness":{"root_started":true,"input_context_sha256":"b".repeat(64),"root_context_sha256":"9".repeat(64),"publication_attempted":true,"publication_verified":true},"execution":{"segments":0,"last_execution_id":null}}).as_object().unwrap(){v[k]=x.clone();}
    v
}
#[test]
fn selection_only_accepts_fresh_current_owned_coordinates() {
    selection().verify().unwrap();
    for fault in 0..5 {
        let mut s = selection();
        match fault {
            0 => {
                s.history_key.target = Target::Saved {
                    session: SESSION.into(),
                }
            }
            1 => s.history_key.generation = 0,
            2 => s.identity.scope = Scope::Preview,
            3 => s.identity.from_session = AGENT.into(),
            _ => s.context_sha256 = "not-a-hash".into(),
        };
        assert!(s.verify().is_err());
    }
}
#[test]
fn plan_counts_and_config_are_independently_bound_without_clamping() {
    assert!(matches!(snapshot().plan, Plan::Host(plan) if plan.head_messages == 4));
    for path in [
        "/plan/messages",
        "/plan/system_messages",
        "/plan/head_messages",
        "/config/keep_recent",
        "/config/max_tokens",
        "/config/max_tool_iters",
    ] {
        let mut v = snapshot_value();
        *v.pointer_mut(path).unwrap() = json!(true);
        assert!(Snapshot::parse(&v, &selection()).is_err(), "{path}");
    }
    for (path, value) in [
        ("/config/max_tokens", json!(255)),
        ("/config/max_tool_iters", json!(513)),
        ("/config/thinking_level", json!("invented")),
        ("/plan/config_sha256", json!("0".repeat(64))),
        ("/plan/tail_messages", json!(7)),
    ] {
        let mut v = snapshot_value();
        *v.pointer_mut(path).unwrap() = value;
        assert!(Snapshot::parse(&v, &selection()).is_err(), "{path}");
    }
    let mut v = snapshot_value();
    v["eligible"] = json!(false);
    v["reason"] = json!("Native provider owns context");
    assert!(!Snapshot::parse(&v, &selection()).unwrap().eligible);
}
#[test]
fn exact_unicode_hint_and_typed_effects_survive_review_and_apply() {
    for hint in ["", "  ", "/literal guidance", "雪\nα"] {
        let r = Review::parse(&review_value(hint), snapshot(), hint).unwrap();
        assert_eq!(r.request()["hint"], hint);
        assert_eq!(r.request()["type"], APPLY);
        assert_eq!(r.request()["effects"]["tools_enabled"], true);
        assert!(r.clear_display());
    }
    let limit = "雪".repeat(21845) + "a";
    assert_eq!(limit.len(), 65536);
    assert!(validate_hint(&limit).is_ok());
    assert!(validate_hint(&(limit + "x")).is_err());
    assert!(validate_hint("a\0b").is_err());
    for (path, value) in [
        ("/hint_sha256", json!("0".repeat(64))),
        ("/effects/provider_rewind", json!(true)),
        ("/effects/tools_enabled", json!(false)),
        ("/root_execution_id", json!(SESSION.replace("4123", "1123"))),
        ("/hint", json!("other")),
    ] {
        let mut v = review_value("retain 雪");
        *v.pointer_mut(path).unwrap() = value;
        assert!(
            Review::parse(&v, snapshot(), "retain 雪").is_err(),
            "{path}"
        );
    }
}
#[test]
fn admission_is_not_a_publication_receipt_and_refusals_need_exact_current_history() {
    let a = admitted();
    assert_eq!(a.status, AdmissionStatus::Accepted);
    assert_eq!(a.root_execution_id.as_deref(), Some(ROOT));
    for status in ["accepted", "refused", "failed"] {
        let v = admission_value(status);
        assert!(Admission::parse(&v, &review(), "op").is_ok());
        let mut v = v;
        v["root_execution_id"] = json!(AGENT);
        assert!(Admission::parse(&v, &review(), "op").is_err());
    }
    for status in ["accepted", "failed"] {
        let mut v = admission_value(status);
        v["refusal_history"] = history(&"8".repeat(64));
        assert!(Admission::parse(&v, &review(), "op").is_err());
    }
    let mut v = admission_value("refused");
    v["refusal_history"] = Value::Null;
    assert!(Admission::parse(&v, &review(), "op").is_err());
}
#[test]
fn zero_stream_success_requires_exact_publication_and_independent_root_context() {
    let result = Settlement::parse(&settled_value(), &admitted(), &review(), "op").unwrap();
    assert_eq!(result.outcome, Outcome::Compacted);
    assert_eq!(result.execution_segments, 0);
    assert_eq!(result.last_execution_id, None);
    for (path, value) in [
        ("/witness/publication_verified", json!(false)),
        ("/witness/root_started", json!(false)),
        ("/witness/root_context_sha256", json!("0".repeat(64))),
        ("/history_changed", json!(false)),
        ("/execution/last_execution_id", json!(ROOT)),
        ("/root_history/read_only", json!(true)),
    ] {
        let mut v = settled_value();
        *v.pointer_mut(path).unwrap() = value;
        assert!(
            Settlement::parse(&v, &admitted(), &review(), "op").is_err(),
            "{path}"
        );
    }
}
#[test]
fn codex_compaction_binds_native_identity_without_host_writes_or_wakes() {
    let mut selected = selection();
    selected.identity.backend = "codex".into();
    let mut snapshot = snapshot_value();
    snapshot["backend"] = json!("codex");
    snapshot["plan"] = json!({"kind": "codex", "thread_id": "codex-thread", "messages": 7});
    snapshot["config"]["tools_enabled"] = json!(false);
    snapshot["config"]["clear_screen_after_compact"] = json!(false);
    let before = Snapshot::parse(&snapshot, &selected).unwrap();
    let mut review = review_value("");
    review["backend"] = json!("codex");
    review["plan"] = snapshot["plan"].clone();
    review["config"] = snapshot["config"].clone();
    review["request_prompt_sha256"] = Value::Null;
    review["effects"]["tools_enabled"] = json!(false);
    review["effects"]["clear_screen_after_compact"] = json!(false);
    let proof = Review::parse(&review, before.clone(), "").unwrap();
    let mut hinted = review.clone();
    hinted["request_prompt_sha256"] = json!(sha(""));
    assert!(Review::parse(&hinted, before, "").is_err());
    let mut admission = admission_value("accepted");
    admission["backend"] = json!("codex");
    let admitted = Admission::parse(&admission, &proof, "op").unwrap();
    let mut result = settled_value();
    result["backend"] = json!("codex");
    result["history_changed"] = json!(false);
    for path in ["history", "root_history"] {
        result[path]["backend"] = json!("codex");
        result[path]["snapshot"] = json!(selected.history_snapshot);
        result[path]["context_sha256"] = json!(selected.context_sha256);
    }
    result["witness"] = json!({"root_started":true, "input_context_sha256":selected.context_sha256,
        "root_context_sha256":selected.context_sha256, "publication_attempted":false,
        "publication_verified":false, "native_compaction":{"thread_id":"codex-thread","turn_id":"native-turn","item_id":"native-item"}});
    let verified = Settlement::parse(&result, &admitted, &proof, "op").unwrap();
    assert_eq!(verified.native_compaction.unwrap().item_id, "native-item");
    for (pointer, value) in [
        ("/witness/native_compaction", Value::Null),
        ("/witness/native_compaction/thread_id", json!("foreign")),
        (
            "/witness/native_compaction/item_id",
            json!("compact:native-turn"),
        ),
        ("/witness/publication_attempted", json!(true)),
        ("/root_history/snapshot", json!("0".repeat(64))),
        ("/execution/segments", json!(1)),
    ] {
        let mut bad = result.clone();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(
            Settlement::parse(&bad, &admitted, &proof, "op").is_err(),
            "{pointer}"
        );
    }
}

#[test]
fn claude_settlement_binds_two_segments_the_sdk_result_and_local_publication() {
    let mut selected = selection();
    selected.identity.backend = "claude".into();
    let mut snap = snapshot_value();
    snap["backend"] = json!("claude");
    snap["config"]["tools_enabled"] = json!(false);
    snap["plan"] = json!({"kind":"claude","messages":7,"segment":"a".repeat(32),"native_session":SESSION,
        "ledger_before":"1".repeat(64),"prompt_sha256":sha("Claude summary"),"tools_sha256":"7".repeat(64),"host_tools":[]});
    let before = Snapshot::parse(&snap, &selected).unwrap();
    let mut review = review_value("");
    review["backend"] = json!("claude");
    review["config"] = snap["config"].clone();
    review["plan"] = snap["plan"].clone();
    review["request_prompt_sha256"] = snap["plan"]["prompt_sha256"].clone();
    review["effects"]["tools_enabled"] = json!(false);
    let proof = Review::parse(&review, before, "").unwrap();
    let mut admitted = admission_value("accepted");
    admitted["backend"] = json!("claude");
    let admitted = Admission::parse(&admitted, &proof, "op").unwrap();
    let mut result = settled_value();
    result["backend"] = json!("claude");
    for key in ["history", "root_history"] {
        result[key]["backend"] = json!("claude");
    }
    result["witness"]["claude_compaction"] = json!({"old_segment":"a".repeat(32),"native_session":SESSION,
        "result_id":AGENT,"new_segment":"b".repeat(32),"seed_sha256":"2".repeat(64),
        "ledger_before":"1".repeat(64),"ledger_after":"3".repeat(64),"tool_calls":0,"store_files":[]});
    assert_eq!(
        Settlement::parse(&result, &admitted, &proof, "op")
            .unwrap()
            .claude_receipt
            .unwrap()
            .result_id,
        AGENT
    );
    for (pointer, value) in [
        ("/witness/claude_compaction", Value::Null),
        (
            "/witness/claude_compaction/new_segment",
            json!("a".repeat(32)),
        ),
        (
            "/witness/claude_compaction/old_segment",
            json!("c".repeat(32)),
        ),
        ("/witness/claude_compaction/native_session", json!(ROOT)),
        ("/witness/claude_compaction/result_id", json!("synthetic")),
        (
            "/witness/claude_compaction/ledger_after",
            json!("1".repeat(64)),
        ),
        ("/witness/claude_compaction/seed_sha256", json!("bad")),
        ("/witness/publication_verified", json!(false)),
        ("/root_history/snapshot", json!("0".repeat(64))),
    ] {
        let mut bad = result.clone();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(
            Settlement::parse(&bad, &admitted, &proof, "op").is_err(),
            "{pointer}"
        );
    }
}

fn claude_with_effects(tools: bool, wake: bool) -> (Review, Admission, Value) {
    let mut selected = selection();
    selected.identity.backend = "claude".into();
    let mut snap = snapshot_value();
    snap["backend"] = json!("claude");
    snap["config"]["tools_enabled"] = json!(tools);
    snap["config"]["wake_after_compact"] = json!(wake);
    snap["plan"] = json!({"kind":"claude","messages":7,"segment":"a".repeat(32),"native_session":SESSION,
        "ledger_before":"1".repeat(64),"prompt_sha256":sha("Claude summary"),"tools_sha256":"7".repeat(64),"host_tools":[]});
    let before =
        Snapshot::parse(&snap, &selected).expect("tools and wake are reviewable for Claude");
    let mut review = review_value("");
    review["backend"] = json!("claude");
    review["config"] = snap["config"].clone();
    review["plan"] = snap["plan"].clone();
    review["request_prompt_sha256"] = snap["plan"]["prompt_sha256"].clone();
    review["effects"]["tools_enabled"] = json!(tools);
    review["effects"]["wake_after_compact"] = json!(wake);
    let proof = Review::parse(&review, before, "").unwrap();
    let mut admitted = admission_value("accepted");
    admitted["backend"] = json!("claude");
    let admitted = Admission::parse(&admitted, &proof, "op").unwrap();
    let mut result = settled_value();
    result["backend"] = json!("claude");
    for key in ["history", "root_history"] {
        result[key]["backend"] = json!("claude");
    }
    result["witness"]["claude_compaction"] = json!({"old_segment":"a".repeat(32),"native_session":SESSION,
        "result_id":AGENT,"new_segment":"b".repeat(32),"seed_sha256":"2".repeat(64),
        "ledger_before":"1".repeat(64),"ledger_after":"3".repeat(64),"tool_calls":0,"store_files":[]});
    (proof, admitted, result)
}
fn grown(mut result: Value) -> Value {
    // The wake appended its own user and assistant rows after the root's summary pair.
    result["history"] = history(&"8".repeat(64));
    result["history"]["backend"] = json!("claude");
    result["history"]["counts"] = json!({"messages":5,"system":1,"non_system":4,"blocks":4});
    result["execution"] = json!({"segments":1,"last_execution_id":ACTION});
    result
}
#[test]
fn claude_review_binds_tools_and_wake_and_the_receipt_names_the_tool_work() {
    let (proof, admitted, mut result) = claude_with_effects(true, false);
    result["witness"]["claude_compaction"]["tool_calls"] = json!(2);
    result["witness"]["claude_compaction"]["store_files"] = json!(["C:/store/memory.md"]);
    let settled = Settlement::parse(&result, &admitted, &proof, "op").unwrap();
    let receipt = settled.claude_receipt.clone().unwrap();
    assert_eq!(
        (receipt.tool_calls, receipt.store_files.clone()),
        (2, vec!["C:/store/memory.md".to_owned()])
    );
    let text = settled.explanation().unwrap();
    assert!(
        text.contains("2 tool call(s)") && text.contains("memory.md"),
        "{text}"
    );
    for (pointer, value) in [
        ("/witness/claude_compaction/tool_calls", json!(65)),
        ("/witness/claude_compaction/tool_calls", json!("2")),
        (
            "/witness/claude_compaction/store_files",
            json!(["a", "b", "c"]),
        ),
        ("/witness/claude_compaction/store_files", json!([""])),
        ("/witness/claude_compaction/store_files", json!([7])),
        ("/witness/claude_compaction/store_files", Value::Null),
    ] {
        let mut bad = result.clone();
        *bad.pointer_mut(pointer).unwrap() = value;
        assert!(
            Settlement::parse(&bad, &admitted, &proof, "op").is_err(),
            "{pointer}"
        );
    }
    // A review that disclosed tools off cannot settle with tool work.
    let (off_proof, off_admitted, mut off) = claude_with_effects(false, false);
    off["witness"]["claude_compaction"]["tool_calls"] = json!(1);
    assert!(Settlement::parse(&off, &off_admitted, &off_proof, "op").is_err());
}
#[test]
fn claude_wake_settles_only_when_the_history_grew_past_the_published_root() {
    let (proof, admitted, result) = claude_with_effects(false, true);
    let woken = grown(result.clone());
    let settled = Settlement::parse(&woken, &admitted, &proof, "op").unwrap();
    assert_eq!(settled.execution_segments, 1);
    assert!(settled.explanation().unwrap().contains("wake turn"));
    // The root's own publication must still be exactly the summary pair.
    let mut wrong_root = woken.clone();
    wrong_root["root_history"]["counts"] =
        json!({"messages":4,"system":1,"non_system":3,"blocks":3});
    assert!(Settlement::parse(&wrong_root, &admitted, &proof, "op").is_err());
    // A wake that left the history exactly at the root is a wake that never persisted.
    let mut same = woken.clone();
    same["history"] = same["root_history"].clone();
    assert!(Settlement::parse(&same, &admitted, &proof, "op").is_err());
    // Without a wake the settled history must still equal the root's.
    let mut drift = result.clone();
    drift["history"] = woken["history"].clone();
    assert!(Settlement::parse(&drift, &admitted, &proof, "op").is_err());
    // A wake the review did not allow is never admitted.
    let (off_proof, off_admitted, off) = claude_with_effects(false, false);
    assert!(Settlement::parse(&grown(off), &off_admitted, &off_proof, "op").is_err());
}
fn claude_snapshot_with(tools: bool, host_tools: Value) -> Value {
    let mut snap = snapshot_value();
    snap["backend"] = json!("claude");
    snap["config"]["tools_enabled"] = json!(tools);
    snap["plan"] = json!({"kind":"claude","messages":7,"segment":"a".repeat(32),"native_session":SESSION,
        "ledger_before":"1".repeat(64),"prompt_sha256":sha("Claude summary"),"tools_sha256":"7".repeat(64),
        "host_tools":host_tools});
    snap
}
#[test]
fn claude_plan_discloses_host_tools_only_as_a_short_sorted_unique_list_of_names_with_tools_on() {
    let mut selected = selection();
    selected.identity.backend = "claude".into();
    let parsed = Snapshot::parse(
        &claude_snapshot_with(true, json!(["convo_search", "view_image"])),
        &selected,
    )
    .expect("a disclosed host tool list is reviewable");
    match parsed.plan {
        Plan::Claude(Some(plan)) => assert_eq!(plan.host_tools, ["convo_search", "view_image"]),
        other => panic!("{other:?}"),
    }
    let sixteen: Vec<String> = (b'a'..b'a' + 16)
        .map(|c| format!("tool_{}", c as char))
        .collect();
    assert!(Snapshot::parse(&claude_snapshot_with(true, json!(sixteen)), &selected).is_ok());
    let seventeen: Vec<String> = (b'a'..b'a' + 17)
        .map(|c| format!("tool_{}", c as char))
        .collect();
    for (tools, bad) in [
        (true, json!(["view_image", "convo_search"])),
        (true, json!(["convo_search", "convo_search"])),
        (true, json!(["Convo_Search"])),
        (true, json!(["convo search"])),
        (true, json!([""])),
        (true, json!(["a".repeat(33)])),
        (true, json!([7])),
        (true, json!("convo_search")),
        (true, json!(seventeen)),
        (false, json!(["convo_search"])),
    ] {
        assert!(
            Snapshot::parse(&claude_snapshot_with(tools, bad.clone()), &selected).is_err(),
            "{tools} {bad}"
        );
    }
}
#[test]
fn claude_plan_without_a_host_tool_list_or_with_an_extra_field_is_refused() {
    let mut selected = selection();
    selected.identity.backend = "claude".into();
    let mut snap = claude_snapshot_with(true, json!([]));
    snap["plan"].as_object_mut().unwrap().remove("host_tools");
    assert!(Snapshot::parse(&snap, &selected).is_err());
    let mut snap = claude_snapshot_with(true, json!([]));
    snap["plan"]["host_tool"] = json!([]);
    assert!(Snapshot::parse(&snap, &selected).is_err());
}
#[test]
fn claude_plan_requires_the_tool_fingerprint() {
    let mut selected = selection();
    selected.identity.backend = "claude".into();
    let mut snap = snapshot_value();
    snap["backend"] = json!("claude");
    snap["plan"] = json!({"kind":"claude","messages":7,"segment":"a".repeat(32),"native_session":SESSION,
        "ledger_before":"1".repeat(64),"prompt_sha256":sha("Claude summary"),"host_tools":[]});
    assert!(Snapshot::parse(&snap, &selected).is_err());
    snap["plan"]["tools_sha256"] = json!("not-a-hash");
    assert!(Snapshot::parse(&snap, &selected).is_err());
}
#[test]
fn coherent_refusal_drift_is_bound_to_refusal_descriptor_not_old_context() {
    let admission = Admission::parse(&admission_value("refused"), &review(), "op").unwrap();
    let mut v = settled_value();
    v["status"] = json!("refused");
    v["outcome"] = json!("no_change");
    v["reason_code"] = json!("admission_refused");
    v["history"] = history(&"8".repeat(64));
    v["root_history"] = Value::Null;
    v["witness"] = json!({"root_started":false,"input_context_sha256":"b".repeat(64),"root_context_sha256":"8".repeat(64),"publication_attempted":false,"publication_verified":false});
    assert!(Settlement::parse(&v, &admission, &review(), "op").is_ok());
    v["history"] = history(&"7".repeat(64));
    assert!(Settlement::parse(&v, &admission, &review(), "op").is_err());
}
#[test]
fn cancelled_and_failed_tool_work_may_change_history_but_cannot_claim_publication_or_wake() {
    for (outcome, reason) in [
        ("cancelled", "cancelled"),
        ("failed", "no_summary"),
        ("failed", "provider_failed"),
        ("no_change", "too_short"),
        ("no_change", "already_recent"),
    ] {
        let mut v = settled_value();
        v["outcome"] = json!(outcome);
        v["reason_code"] = json!(reason);
        v["witness"]["publication_attempted"] = json!(false);
        v["witness"]["publication_verified"] = json!(false);
        assert!(
            Settlement::parse(&v, &admitted(), &review(), "op").is_ok(),
            "{outcome}"
        );
        v["execution"] = json!({"segments":1,"last_execution_id":ROOT});
        assert!(Settlement::parse(&v, &admitted(), &review(), "op").is_err());
    }
}
fn key(view: &mut View, code: KeyCode) {
    view.key(KeyEvent::new(code, KeyModifiers::NONE));
}
fn draw(view: &mut View) {
    let mut terminal = Terminal::new(TestBackend::new(100, 30)).unwrap();
    terminal
        .draw(|f| view.draw(f, f.area(), &builtin_themes()[0]))
        .unwrap();
}
fn loaded() -> View {
    let mut v = View::default();
    v.open(selection()).unwrap();
    let k = v.take_intent().unwrap().key().clone();
    v.observe(Update::Snapshot {
        key: k,
        snapshot: Box::new(snapshot()),
    })
    .unwrap();
    v
}
#[test]
fn view_requires_visible_exact_review_and_releases_input_only_after_submission() {
    let mut v = loaded();
    draw(&mut v);
    v.paste("retain 雪");
    key(&mut v, KeyCode::Enter);
    let k = v.take_intent().unwrap().key().clone();
    v.observe(Update::Review {
        key: k,
        review: Box::new(review()),
    })
    .unwrap();
    key(&mut v, KeyCode::Enter);
    assert!(v.take_intent().is_none());
    let intent = loop {
        draw(&mut v);
        key(&mut v, KeyCode::Enter);
        if let Some(i) = v.take_intent() {
            break i;
        }
        draw(&mut v);
        key(&mut v, KeyCode::PageDown);
    };
    assert!(v.prepare_apply(&intent).is_ok());
    let k = intent.key().clone();
    v.observe(Update::Submitted { key: k.clone() }).unwrap();
    assert!(!v.owns_input() && v.blocks_composer());
    assert!(!v.accepts_paste());
    v.paste("late");
    assert_eq!(v.draft(), "retain 雪");
    v.observe(Update::Settled {
        key: k,
        outcome: Outcome::Failed,
        reason: Some("no summary".into()),
    })
    .unwrap();
    assert!(!v.blocks_composer());
    assert!(v.notice.contains("failed"));
}
#[test]
fn cancelled_reads_never_reopen_or_reuse_request_generation() {
    let mut v = View::default();
    v.open(selection()).unwrap();
    let k = v.take_intent().unwrap().key().clone();
    v.close();
    v.observe(Update::Snapshot {
        key: k.clone(),
        snapshot: Box::new(snapshot()),
    })
    .unwrap();
    assert!(!v.is_open());
    v.finish();
    v.open(selection()).unwrap();
    assert!(v.take_intent().unwrap().key().generation > k.generation);
}
#[test]
fn shutdown_proves_exact_retirement_not_success() {
    let mut command = review().request();
    command["id"] = json!("op");
    let mut proof = json!({"version":1,"worker_settled":true,"admission_settled":true,"hooks_settled":true,"effects_verified":false,"pending_at_close":true,"mutated":true,"action_id":ACTION,"action":"compact","operation_id":"op","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","current_session_id":SESSION});
    assert!(
        verify_shutdown(
            &json!({"conversation_compact_shutdown":proof}),
            Some(&command),
            SESSION
        )
        .unwrap()
    );
    proof["effects_verified"] = json!(true);
    assert!(
        verify_shutdown(
            &json!({"conversation_compact_shutdown":proof}),
            Some(&command),
            SESSION
        )
        .is_err()
    );
}

#[test]
fn wake_off_settlement_cannot_claim_an_unreviewed_stream() {
    let mut v = settled_value();
    v["execution"] = json!({"segments":1,"last_execution_id":ROOT});
    assert!(Settlement::parse(&v, &admitted(), &review(), "op").is_err());
}

#[test]
fn settlement_explains_every_validated_reason_code_without_inventing_success() {
    for (code, outcome, explanation) in [
        ("compacted", "compacted", None),
        (
            "too_short",
            "no_change",
            Some("Too few messages to compact."),
        ),
        (
            "already_recent",
            "no_change",
            Some("All messages are kept as recent context."),
        ),
        ("no_summary", "failed", Some("No summary was produced.")),
        (
            "provider_failed",
            "failed",
            Some("The provider could not complete compaction."),
        ),
        ("cancelled", "cancelled", Some("Compaction was cancelled.")),
        (
            "admission_refused",
            "no_change",
            Some("The source refused compaction admission."),
        ),
    ] {
        let mut value = settled_value();
        value["outcome"] = json!(outcome);
        value["reason_code"] = json!(code);
        value["witness"]["publication_attempted"] = json!(code == "compacted");
        value["witness"]["publication_verified"] = json!(code == "compacted");
        let admission = if code == "admission_refused" {
            value["status"] = json!("refused");
            value["history"] = history(&"8".repeat(64));
            value["root_history"] = Value::Null;
            value["witness"]["root_started"] = json!(false);
            value["witness"]["root_context_sha256"] = json!("8".repeat(64));
            Admission::parse(&admission_value("refused"), &review(), "op").unwrap()
        } else {
            admitted()
        };
        let result = Settlement::parse(&value, &admission, &review(), "op").unwrap();
        assert_eq!(result.explanation().as_deref(), explanation, "{code}");
        value["reason"] = json!("Exact source detail 雪");
        let result = Settlement::parse(&value, &admission, &review(), "op").unwrap();
        assert_eq!(
            result.explanation().as_deref(),
            Some("Exact source detail 雪"),
            "{code}"
        );
    }
}

#[test]
fn settlement_source_explanation_preserves_its_utf8_bound() {
    let detail = "雪".repeat(1365) + "a";
    assert_eq!(detail.len(), 4096);
    let mut value = settled_value();
    value["reason"] = json!(detail);
    let result = Settlement::parse(&value, &admitted(), &review(), "op").unwrap();
    assert_eq!(result.explanation(), Some(detail.clone()));
    value["reason"] = json!(detail + "x");
    assert!(Settlement::parse(&value, &admitted(), &review(), "op").is_err());
}
