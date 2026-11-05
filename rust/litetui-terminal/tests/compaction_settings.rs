use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use litetui_terminal::compaction_settings::{
    self as edit, Intent, Outcome, Review, Snapshot, Update, View,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
fn data() -> Value {
    let mut fields = serde_json::Map::new();
    let values = [
        json!(4),
        json!(12288),
        json!(8),
        json!("low"),
        json!(false),
        json!(true),
        json!(true),
        json!(80),
    ];
    for (i, name) in edit::FIELDS.iter().enumerate() {
        let (minimum, maximum) = match i {
            0 => (json!(0), json!(1_000_000)),
            1 => (json!(256), json!(2_147_483_647)),
            2 => (json!(1), json!(512)),
            7 => (json!(1), json!(99)),
            _ => (Value::Null, Value::Null),
        };
        fields.insert((*name).into(),json!({"type":if i<3||i==7{"integer"}else if i==3{"choice"}else{"boolean"},"scope":if i==5{"device"}else{"conversation"},"apply_timing":if i==5{"immediate"}else{"next_turn"},"destination":if i==5{"global"}else{"conversation"},"minimum":minimum,"maximum":maximum,"choices":if i==3{json!(edit::LEVELS)}else{json!([])},"saved":values[i],"effective":values[i],"editable":true,"reason":null}));
    }
    json!({"version":1,"kind":"compaction_settings_snapshot","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":"a".repeat(64),"revisions":{"global":"a".repeat(64),"conversation":"absent","agent":"b".repeat(64)},"destinations":{"global":"C:/owned/settings.json","conversation":format!("C:/owned/agents/{AGENT}/conversations/{SESSION}/settings.json")},"fields":fields,"tools":{"effective":false,"editable":false,"reason":"Tools are shared turn authority; this editor does not change them."},"reasoning":{"available":true,"reason":null,"source":"canonical compaction request adapter","supported_levels":["none","minimal","low","medium","high","xhigh","max"],"saved":"low","effective":"low"}})
}
fn snapshot(v: &Value) -> Snapshot {
    Snapshot::parse(v, SESSION, AGENT, "custom", "test").unwrap()
}
fn review_data(s: &Snapshot, field: &str, value: &Value) -> Value {
    let f = s.field(field).unwrap();
    let mut v = s.request(field, value).unwrap();
    for (key, value) in [
        ("kind", json!("compaction_settings_review")),
        ("scope", json!(f.scope)),
        ("apply_timing", json!(f.apply_timing)),
        ("destination", s.destinations[&f.destination].clone()),
        ("saved_before", f.saved.clone()),
        ("effective_before", f.effective.clone()),
        ("review", json!("c".repeat(64))),
        ("action_id", json!(ACTION)),
        (
            "resolved_value",
            if field == "compact_thinking_level" {
                json!(if value == "ultra" {
                    "max"
                } else {
                    value.as_str().unwrap()
                })
            } else {
                Value::Null
            },
        ),
    ] {
        v[key] = value;
    }
    v
}
fn review(field: &str, value: Value) -> Review {
    let s = snapshot(&data());
    Review::parse(&review_data(&s, field, &value), &s, field, &value).unwrap()
}
fn receipt(r: &Review, partial: bool) -> Value {
    let f = r.snapshot.field(&r.field).unwrap();
    let effective = if partial {
        f.effective.clone()
    } else {
        r.value.clone()
    };
    let mut rev = r.snapshot.revisions.clone();
    rev[&f.destination] = json!("d".repeat(64));
    json!({"kind":"compaction_settings_save","version":1,"session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"test","snapshot":r.snapshot.token,"review":r.token,"action_id":r.action_id,"field":r.field,"value":r.value,"scope":f.scope,"apply_timing":f.apply_timing,"destination":r.destination,"status":if partial{"partial"}else{"saved"},"mutated":true,"close_required":false,"error":null,"persistence":[{"destination":r.destination,"scope":f.scope,"saved":true,"error":null,"revision":"d".repeat(64),"fields":[r.field]}],"runtime":[{"field":r.field,"scope":f.scope,"status":if partial{"failed"}else{"applied"},"action":if partial{"retry"}else{"none"},"requested":r.value,"effective":effective,"reason":if partial{json!("Controlled apply failure")}else{Value::Null}}],"saved":r.value,"effective":effective,"revisions":rev})
}
fn after(r: &Review, partial: bool) -> Value {
    let mut v = data();
    v["snapshot"] = json!("e".repeat(64));
    v["revisions"] = receipt(r, partial)["revisions"].clone();
    v["fields"][&r.field]["saved"] = r.value.clone();
    if !partial {
        v["fields"][&r.field]["effective"] = r.value.clone();
    }
    if r.field == "compact_thinking_level" {
        v["reasoning"]["saved"] = json!(r.resolved_value);
        if !partial {
            v["reasoning"]["effective"] = json!(r.resolved_value);
        }
    }
    v
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
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
fn read(v: &mut View) -> u64 {
    let Some(Intent::Read { generation }) = v.take_intent() else {
        panic!("Expected read")
    };
    generation
}
fn view() -> View {
    let mut v = View::default();
    v.open();
    let generation = read(&mut v);
    v.observe(Update::Snapshot {
        generation,
        snapshot: Box::new(snapshot(&data())),
    });
    draw(&mut v, 90, 30);
    v
}
fn replace(v: &mut View, text: &str) {
    v.key(key(KeyCode::Home));
    for _ in 0..10 {
        v.key(key(KeyCode::Delete));
    }
    v.paste(text);
}
fn validate(v: &mut View) -> (u64, Review) {
    v.key(key(KeyCode::Enter));
    let Some(Intent::Validate {
        generation,
        snapshot: token,
        field,
        value,
    }) = v.take_intent()
    else {
        panic!("Expected validation")
    };
    let s = v.snapshot.as_ref().unwrap();
    assert_eq!(token, s.token);
    let r = Review::parse(&review_data(s, &field, &value), s, &field, &value).unwrap();
    (generation, r)
}
fn confirm(v: &mut View) -> (u64, Review) {
    let (generation, r) = validate(v);
    v.observe(Update::Review {
        generation,
        review: Box::new(r.clone()),
    });
    for _ in 0..30 {
        draw(v, 70, 24);
        v.key(key(KeyCode::Enter));
        if let Some(Intent::Save { generation, review }) = v.take_intent() {
            return (generation, *review);
        }
        draw(v, 70, 24);
        v.key(key(KeyCode::PageDown));
    }
    panic!("Review did not become confirmable")
}
#[test]
fn all_eight_fields_preserve_exact_scope_types_bounds_and_saved_reasoning_preference() {
    let s = snapshot(&data());
    assert_eq!(s.fields.len(), 8);
    assert_eq!(s.effective("clear_screen_after_compact").unwrap(), true);
    assert_eq!(s.fields[5].scope, "device");
    assert_eq!(s.fields[5].apply_timing, "immediate");
    for (field, valid, invalid) in [
        (
            edit::FIELDS[0],
            vec![json!(0), json!(1_000_000)],
            vec![json!(-1), json!(1_000_001), json!(true), json!(1.0)],
        ),
        (
            edit::FIELDS[1],
            vec![json!(256), json!(2_147_483_647u64)],
            vec![json!(255), json!(2_147_483_648u64), json!("256")],
        ),
        (
            edit::FIELDS[2],
            vec![json!(1), json!(512)],
            vec![json!(0), json!(513)],
        ),
        (
            edit::FIELDS[3],
            edit::LEVELS.into_iter().map(|s| json!(s)).collect(),
            vec![json!("none"), json!(0)],
        ),
        (
            edit::FIELDS[4],
            vec![json!(true), json!(false)],
            vec![json!(0), json!(1), json!("true")],
        ),
        (
            edit::FIELDS[5],
            vec![json!(true), json!(false)],
            vec![Value::Null],
        ),
        (
            edit::FIELDS[6],
            vec![json!(true), json!(false)],
            vec![json!(0), json!(1), json!("false"), Value::Null],
        ),
        (
            edit::FIELDS[7],
            vec![json!(1), json!(80), json!(99)],
            vec![
                json!(0),
                json!(100),
                json!(-1),
                json!(true),
                json!(80.5),
                json!("80"),
                Value::Null,
            ],
        ),
    ] {
        for value in valid {
            assert!(s.request(field, &value).is_ok(), "{field} {value}");
        }
        for value in invalid {
            assert!(s.request(field, &value).is_err(), "{field} {value}");
        }
    }
    let r = review(edit::FIELDS[3], json!("ultra"));
    assert_eq!(r.value, "ultra");
    assert_eq!(r.resolved_value.as_deref(), Some("max"));
}
#[test]
fn snapshot_rejects_identity_metadata_ownership_and_resolution_drift() {
    for fault in [
        "session",
        "agent",
        "backend",
        "model",
        "version",
        "token",
        "revision",
        "field",
        "extra",
        "scope",
        "timing",
        "destination",
        "bounds",
        "choices",
        "authority",
        "reason",
        "tools",
        "reasoning",
        "resolved",
        "duplicate",
        "absolute",
        "traversal",
        "bytes",
    ] {
        let mut v = data();
        match fault {
            "session" => v["session_id"] = json!(AGENT),
            "agent" => v["agent_id"] = json!(SESSION),
            "backend" => v["backend"] = json!("other"),
            "model" => v["model"] = json!("other"),
            "version" => v["version"] = json!(2),
            "token" => v["snapshot"] = json!("X".repeat(64)),
            "revision" => v["revisions"]["other"] = json!("absent"),
            "field" => {
                v["fields"].as_object_mut().unwrap().remove(edit::FIELDS[2]);
            }
            "extra" => v["fields"]["temperature"] = json!({}),
            "scope" => v["fields"][edit::FIELDS[5]]["scope"] = json!("conversation"),
            "timing" => v["fields"][edit::FIELDS[0]]["apply_timing"] = json!("immediate"),
            "destination" => v["fields"][edit::FIELDS[0]]["destination"] = json!("global"),
            "bounds" => v["fields"][edit::FIELDS[0]]["minimum"] = json!(1),
            "choices" => v["fields"][edit::FIELDS[3]]["choices"] = json!(["low"]),
            "authority" => v["fields"][edit::FIELDS[0]]["editable"] = json!(false),
            "reason" => v["fields"][edit::FIELDS[0]]["reason"] = json!("locked"),
            "tools" => v["tools"]["editable"] = json!(true),
            "reasoning" => v["reasoning"]["available"] = json!(false),
            "resolved" => v["reasoning"]["effective"] = json!("ultra"),
            "duplicate" => v["reasoning"]["supported_levels"] = json!(["low", "low"]),
            "absolute" => v["destinations"]["global"] = json!("settings.json"),
            "traversal" => {
                v["destinations"]["conversation"] =
                    json!(format!("C:/other/../conversations/{SESSION}/settings.json"))
            }
            "bytes" => v["excess"] = json!("x".repeat(edit::MAX_BYTES)),
            _ => unreachable!(),
        }
        assert!(
            Snapshot::parse(&v, SESSION, AGENT, "custom", "test").is_err(),
            "{fault}"
        );
    }
}
#[test]
fn source_readonly_capability_is_visible_and_never_borrowed_from_chat_ownership() {
    let mut v = data();
    for field in &edit::FIELDS[..4] {
        v["fields"][field]["editable"] = json!(false);
        v["fields"][field]["reason"] = json!("Canonical provider capability owns this preference");
    }
    v["reasoning"]["available"] = json!(false);
    v["reasoning"]["reason"] = json!("Engine-owned");
    v["reasoning"]["supported_levels"] = json!([]);
    v["reasoning"]["saved"] = Value::Null;
    v["reasoning"]["effective"] = Value::Null;
    let s = snapshot(&v);
    assert!(s.request(edit::FIELDS[3], &json!("high")).is_err());
    assert!(s.request(edit::FIELDS[4], &json!(true)).is_ok());
    let mut view = View::default();
    view.open();
    let generation = read(&mut view);
    view.observe(Update::Snapshot {
        generation,
        snapshot: Box::new(s),
    });
    assert!(draw(&mut view, 100, 32).contains("Canonical provider"));
    assert!(!view.accepts_paste());
    view.paste("123");
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
}
#[test]
fn review_echoes_exact_source_identity_destination_and_resolution() {
    let s = snapshot(&data());
    let value = json!("ultra");
    let original = review_data(&s, edit::FIELDS[3], &value);
    for fault in [
        "snapshot",
        "revisions",
        "field",
        "value",
        "scope",
        "apply_timing",
        "destination",
        "saved_before",
        "effective_before",
        "review",
        "action_id",
        "resolved_value",
    ] {
        let mut v = original.clone();
        v[fault] = json!("wrong");
        assert!(
            Review::parse(&v, &s, edit::FIELDS[3], &value).is_err(),
            "{fault}"
        );
    }
    let r = Review::parse(&original, &s, edit::FIELDS[3], &value).unwrap();
    let mut expected = original;
    expected.as_object_mut().unwrap().remove("kind");
    assert_eq!(r.request().unwrap(), expected);
    let mut unchanged = review_data(&s, edit::FIELDS[3], &json!("low"));
    unchanged["resolved_value"] = json!("high");
    assert!(Review::parse(&unchanged, &s, edit::FIELDS[3], &json!("low")).is_err());
}
#[test]
fn every_field_saved_and_partial_receipt_requires_exact_independent_readback() {
    for (field, value) in edit::FIELDS.iter().zip([
        json!(0),
        json!(256),
        json!(512),
        json!("ultra"),
        json!(true),
        json!(false),
        json!(false),
        json!(99),
    ]) {
        let r = review(field, value);
        for partial in [false, true] {
            let o = Outcome::parse(&receipt(&r, partial), &r).unwrap().unwrap();
            o.verify(&snapshot(&after(&r, partial)), &r).unwrap();
            for fault in [
                "other_field",
                "tools",
                "reasoning_source",
                "other_revision",
                "destination",
            ] {
                let mut v = after(&r, partial);
                match fault {
                    "other_field" => {
                        let other = if *field == edit::FIELDS[0] {
                            edit::FIELDS[2]
                        } else {
                            edit::FIELDS[0]
                        };
                        v["fields"][other]["effective"] = json!(7);
                    }
                    "tools" => v["tools"]["effective"] = json!(true),
                    "reasoning_source" => v["reasoning"]["source"] = json!("changed adapter"),
                    "other_revision" => v["revisions"]["agent"] = json!("f".repeat(64)),
                    "destination" => v["destinations"]["global"] = json!("C:/other/settings.json"),
                    _ => unreachable!(),
                };
                assert!(
                    o.verify(&snapshot(&v), &r).is_err(),
                    "{field} {partial} {fault}"
                );
            }
        }
    }
}
#[test]
fn receipts_cannot_claim_success_from_wrong_path_type_status_or_unrelated_revision() {
    let r = review(edit::FIELDS[4], json!(true));
    for fault in [
        "value",
        "action_id",
        "destination",
        "scope",
        "timing",
        "saved",
        "effective",
        "third_effective",
        "runtime",
        "action",
        "reason",
        "mutated",
        "close",
        "persistence",
        "fields",
        "revision",
        "global",
    ] {
        let mut v = receipt(&r, false);
        match fault {
            "value" => v["value"] = json!(1),
            "action_id" => v["action_id"] = json!(AGENT),
            "destination" => v["destination"] = json!("C:/other/settings.json"),
            "scope" => v["scope"] = json!("device"),
            "timing" => v["apply_timing"] = json!("immediate"),
            "saved" => v["saved"] = json!(false),
            "effective" => v["effective"] = json!(false),
            "third_effective" => v["effective"] = json!(1),
            "runtime" => v["runtime"][0]["status"] = json!("pending"),
            "action" => v["runtime"][0]["action"] = json!("retry"),
            "reason" => v["runtime"][0]["reason"] = json!("failed"),
            "mutated" => v["mutated"] = json!(false),
            "close" => v["close_required"] = json!(true),
            "persistence" => v["persistence"][0]["destination"] = json!("C:/other/settings.json"),
            "fields" => v["persistence"][0]["fields"] = json!([edit::FIELDS[5]]),
            "revision" => v["persistence"][0]["revision"] = json!("z".repeat(64)),
            "global" => v["revisions"]["global"] = json!("f".repeat(64)),
            _ => unreachable!(),
        };
        assert!(Outcome::parse(&v, &r).is_err(), "{fault}");
    }
    let r = review(edit::FIELDS[0], json!(8));
    let mut v = receipt(&r, true);
    v["effective"] = json!(7);
    v["runtime"][0]["effective"] = json!(7);
    assert!(Outcome::parse(&v, &r).is_err());
}
#[test]
fn preflight_and_verified_nonwriting_refusals_are_distinct_from_uncertain_failure() {
    let r = review(edit::FIELDS[0], json!(8));
    let mut v = receipt(&r, false);
    for (k, value) in [
        ("status", json!("refused")),
        ("mutated", json!(false)),
        ("error", json!("Stale review")),
        ("persistence", json!([])),
        ("runtime", json!([])),
        ("saved", Value::Null),
        ("effective", Value::Null),
        ("revisions", Value::Null),
    ] {
        v[k] = value;
    }
    assert_eq!(Outcome::parse(&v, &r).unwrap(), Err("Stale review".into()));
    v["saved"] = json!(4);
    assert!(Outcome::parse(&v, &r).is_err());
    v["effective"] = json!(4);
    v["revisions"] = r.snapshot.revisions.clone();
    v["persistence"] = json!([{"destination":r.destination,"scope":"conversation","saved":false,"error":"Not writable","fields":[r.field]}]);
    assert!(Outcome::parse(&v, &r).unwrap().is_err());
    v["status"] = json!("failed");
    v["mutated"] = json!(true);
    v["close_required"] = json!(true);
    assert!(Outcome::parse(&v, &r).is_err());
}
#[test]
fn numeric_draft_is_lossless_and_survives_field_navigation_and_explicit_refresh() {
    let mut v = view();
    replace(&mut v, "00042");
    assert_eq!(v.draft(), "00042");
    v.key(key(KeyCode::Left));
    v.key(key(KeyCode::Backspace));
    assert_eq!(v.draft(), "0002");
    v.key(key(KeyCode::Down));
    assert_eq!(v.selected_field(), edit::FIELDS[1]);
    v.key(key(KeyCode::Up));
    assert_eq!(v.draft(), "0002");
    v.key(key(KeyCode::F(5)));
    let g = read(&mut v);
    v.observe(Update::Snapshot {
        generation: g,
        snapshot: Box::new(snapshot(&data())),
    });
    draw(&mut v, 90, 30);
    assert_eq!(v.draft(), "0002");
    v.paste(" 7");
    v.paste("12345678901");
    assert_eq!(v.draft(), "0002");
    let (_, r) = validate(&mut v);
    assert_eq!(r.value, 2);
}
#[test]
fn all_boolean_and_choice_preferences_are_editable_without_coercing_saved_values() {
    let mut v = view();
    for _ in 0..3 {
        v.key(key(KeyCode::Down));
    }
    assert_eq!(v.draft(), "low");
    assert!(!v.accepts_paste());
    v.paste("high");
    assert_eq!(v.draft(), "low");
    v.key(key(KeyCode::Right));
    assert_eq!(v.draft(), "medium");
    v.key(key(KeyCode::Down));
    assert_eq!(v.draft(), "false");
    v.key(key(KeyCode::Char(' ')));
    assert_eq!(v.draft(), "true");
    v.key(key(KeyCode::Down));
    assert_eq!(v.draft(), "true");
    v.key(key(KeyCode::Left));
    assert_eq!(v.draft(), "false");
    let (_, r) = validate(&mut v);
    assert_eq!(r.field, edit::FIELDS[5]);
    assert_eq!(r.value, false);
}
#[test]
fn every_review_page_requires_a_fresh_draw_and_exposes_effect_disclosures() {
    let mut v = view();
    replace(&mut v, "8");
    let (g, r) = validate(&mut v);
    v.observe(Update::Review {
        generation: g,
        review: Box::new(r),
    });
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    let mut seen = String::new();
    for _ in 0..20 {
        seen.push_str(&draw(&mut v, 48, 20));
        v.key(key(KeyCode::Enter));
        if let Some(Intent::Save { .. }) = v.take_intent() {
            break;
        }
        draw(&mut v, 48, 20);
        v.key(key(KeyCode::PageDown));
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none());
    }
    assert!(v.waiting);
    for text in [
        "CONFIRM PREFERENCE SAVE",
        "safe-tail",
        "goal evaluation",
        "subagent requests",
        "Tools remain",
    ] {
        let painted: String = seen
            .chars()
            .filter(|c| !c.is_whitespace() && !matches!(c, '│' | '┌' | '┐' | '└' | '┘' | '─'))
            .collect();
        let expected: String = text.chars().filter(|c| !c.is_whitespace()).collect();
        assert!(painted.contains(&expected), "missing {text}");
    }
    v.key(KeyEvent {
        code: KeyCode::Enter,
        modifiers: KeyModifiers::NONE,
        kind: KeyEventKind::Repeat,
        state: crossterm::event::KeyEventState::NONE,
    });
    assert!(v.take_intent().is_none());
}
#[test]
fn wrong_generations_phases_and_late_closed_reads_cannot_reopen_or_replace_drafts() {
    let mut v = View::default();
    v.open();
    let g = read(&mut v);
    v.observe(Update::Snapshot {
        generation: g + 1,
        snapshot: Box::new(snapshot(&data())),
    });
    assert!(v.waiting);
    assert!(v.snapshot.is_none());
    v.observe(Update::Review {
        generation: g,
        review: Box::new(review(edit::FIELDS[0], json!(8))),
    });
    assert!(v.waiting);
    v.close();
    v.observe(Update::Snapshot {
        generation: g,
        snapshot: Box::new(snapshot(&data())),
    });
    assert!(!v.owns_input());
    assert!(v.snapshot.is_none());
    v.finish();
    v.open();
    assert!(read(&mut v) > g);
}
#[test]
fn dispatched_save_keeps_ownership_until_correlated_readback_even_after_escape() {
    let mut v = view();
    replace(&mut v, "8");
    let (g, r) = confirm(&mut v);
    v.key(key(KeyCode::Esc));
    assert!(v.waiting);
    assert!(!v.open);
    assert!(v.owns_input());
    let outcome = Outcome::parse(&receipt(&r, false), &r).unwrap().unwrap();
    let s = snapshot(&after(&r, false));
    outcome.verify(&s, &r).unwrap();
    v.observe(Update::Saved {
        generation: g,
        outcome: Box::new(outcome),
        snapshot: Box::new(s),
    });
    assert!(!v.owns_input());
    assert!(!v.open);
    assert!(v.notice.contains("Saved 8"));
    assert!(v.take_intent().is_none());
}
#[test]
fn rejected_review_keeps_the_draft_but_requires_explicit_fresh_authority() {
    let mut v = view();
    replace(&mut v, "00008");
    let (g, _) = validate(&mut v);
    v.observe(Update::Rejected {
        generation: g,
        reason: "Revision changed".into(),
    });
    draw(&mut v, 90, 30);
    assert_eq!(v.draft(), "00008");
    assert!(!v.editing());
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::F(5)));
    let next = read(&mut v);
    assert!(next > g);
    v.observe(Update::Snapshot {
        generation: next,
        snapshot: Box::new(snapshot(&data())),
    });
    draw(&mut v, 90, 30);
    assert_eq!(v.draft(), "00008");
    assert!(v.editing());
}
#[test]
fn tiny_viewport_escape_and_mouse_or_modifier_input_never_confirm_hidden_review() {
    let mut v = view();
    replace(&mut v, "8");
    let (g, r) = validate(&mut v);
    v.observe(Update::Review {
        generation: g,
        review: Box::new(r),
    });
    draw(&mut v, 100, 32);
    v.mouse(MouseEvent {
        kind: MouseEventKind::Moved,
        column: 1,
        row: 1,
        modifiers: KeyModifiers::NONE,
    });
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 100, 32);
    v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 20, 5);
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Esc));
    assert!(v.open);
    assert_eq!(v.draft(), "8");
    v.key(key(KeyCode::Esc));
    assert!(!v.owns_input());
}

#[test]
fn empty_adapter_vocabulary_preserves_canonical_resolved_effort_through_readback() {
    for (backend, old_effort, preference, resolved) in [
        ("llamacpp", Some("low"), "high", Some("high")),
        ("lmstudio", None, "off", Some("none")),
    ] {
        let mut original = data();
        original["backend"] = json!(backend);
        original["reasoning"]["supported_levels"] = json!([]);
        original["reasoning"]["saved"] = json!(old_effort);
        original["reasoning"]["effective"] = json!(old_effort);
        let s = Snapshot::parse(&original, SESSION, AGENT, backend, "test").unwrap();
        let mut reviewed = review_data(&s, edit::FIELDS[3], &json!(preference));
        reviewed["resolved_value"] = json!(resolved);
        let r = Review::parse(&reviewed, &s, edit::FIELDS[3], &json!(preference)).unwrap();
        let mut saved = receipt(&r, false);
        saved["backend"] = json!(backend);
        let outcome = Outcome::parse(&saved, &r).unwrap().unwrap();
        let mut after = original;
        after["snapshot"] = json!("e".repeat(64));
        after["revisions"] = saved["revisions"].clone();
        after["fields"][edit::FIELDS[3]]["saved"] = json!(preference);
        after["fields"][edit::FIELDS[3]]["effective"] = json!(preference);
        after["reasoning"]["saved"] = json!(resolved);
        after["reasoning"]["effective"] = json!(resolved);
        let after = Snapshot::parse(&after, SESSION, AGENT, backend, "test").unwrap();
        outcome.verify(&after, &r).unwrap();
    }
}

#[test]
fn resolved_effort_has_an_exact_utf8_byte_budget_even_without_adapter_vocabulary() {
    let effort = format!("{}x", "雪".repeat(21));
    assert_eq!(effort.len(), 64);
    let mut v = data();
    v["reasoning"]["supported_levels"] = json!([]);
    v["reasoning"]["saved"] = json!(effort);
    v["reasoning"]["effective"] = json!(effort);
    let s = snapshot(&v);
    let mut reviewed = review_data(&s, edit::FIELDS[3], &json!("high"));
    reviewed["resolved_value"] = json!(effort);
    Review::parse(&reviewed, &s, edit::FIELDS[3], &json!("high")).unwrap();
    reviewed["resolved_value"] = json!(format!("{effort}x"));
    assert!(Review::parse(&reviewed, &s, edit::FIELDS[3], &json!("high")).is_err());
    v["reasoning"]["effective"] = json!(format!("{effort}x"));
    assert!(Snapshot::parse(&v, SESSION, AGENT, "custom", "test").is_err());
}

#[test]
fn narrow_draft_pages_expose_complete_readonly_reasons_without_editing() {
    let reason = format!(
        "{} FINAL_REASON_END",
        (0..80)
            .map(|i| format!("DETAIL_{i:03} "))
            .collect::<String>()
    );
    for field in edit::FIELDS {
        let mut raw = data();
        raw["fields"][field]["editable"] = json!(false);
        raw["fields"][field]["reason"] = json!(reason);
        if field == "compact_thinking_level" {
            raw["reasoning"]["available"] = json!(false);
            raw["reasoning"]["reason"] = json!("Engine-owned");
            raw["reasoning"]["supported_levels"] = json!([]);
            raw["reasoning"]["saved"] = Value::Null;
            raw["reasoning"]["effective"] = Value::Null;
        }
        let mut view = View::default();
        view.open_field(field);
        let generation = read(&mut view);
        view.observe(Update::Snapshot {
            generation,
            snapshot: Box::new(snapshot(&raw)),
        });
        let original = view.draft().to_owned();
        let first = draw(&mut view, 48, 20);
        assert!(
            first.contains("Read-only"),
            "{field} must visibly explain disabled editing"
        );
        let mut seen = first;
        for _ in 0..32 {
            assert!(!view.accepts_paste());
            view.paste("123");
            view.key(key(KeyCode::Enter));
            assert!(view.take_intent().is_none());
            view.key(key(KeyCode::PageDown));
            seen.push_str(&draw(&mut view, 48, 20));
        }
        let compact: String = seen
            .chars()
            .filter(|c| !c.is_whitespace() && *c != '│')
            .collect();
        for i in 0..80 {
            assert!(
                compact.contains(&format!("DETAIL_{i:03}")),
                "missing detail {i} for {field}"
            );
        }
        assert!(
            compact.contains("FINAL_REASON_END"),
            "Complete selected reason must be reachable for {field}"
        );
        assert_eq!(view.draft(), original);
        view.key(key(KeyCode::Tab));
        draw(&mut view, 48, 20);
        assert_ne!(view.selected_field(), field);
    }
}

#[test]
fn draft_detail_pages_preserve_numeric_drafts_and_require_visible_editing() {
    let mut raw = data();
    raw["reasoning"]["supported_levels"] = json!([]);
    raw["reasoning"]["saved"] = json!("adapter-effort-".repeat(4));
    raw["reasoning"]["effective"] = raw["reasoning"]["saved"].clone();
    let mut view = View::default();
    view.open();
    let generation = read(&mut view);
    view.observe(Update::Snapshot {
        generation,
        snapshot: Box::new(snapshot(&raw)),
    });
    draw(&mut view, 48, 20);
    replace(&mut view, "37");
    draw(&mut view, 48, 20);
    view.key(key(KeyCode::PageDown));
    draw(&mut view, 48, 20);
    assert!(
        !view.accepts_paste(),
        "Offscreen draft must not accept edits"
    );
    view.paste("999");
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
    assert_eq!(view.draft(), "37");
    view.key(key(KeyCode::PageUp));
    draw(&mut view, 48, 20);
    assert!(view.accepts_paste());
    assert_eq!(view.draft(), "37");
}

#[test]
fn partial_receipts_require_canonical_runtime_action_pairs() {
    let r = review("compact_max_tokens", json!(768));
    for runtime in ["failed", "pending"] {
        for action in ["none", "retry", "restart", "reconnect", "reload"] {
            let mut value = receipt(&r, true);
            value["runtime"][0]["status"] = json!(runtime);
            value["runtime"][0]["action"] = json!(action);
            let expected = match runtime {
                "failed" => action == "retry",
                "pending" => matches!(action, "restart" | "reconnect" | "reload"),
                _ => unreachable!(),
            };
            assert_eq!(
                Outcome::parse(&value, &r).is_ok(),
                expected,
                "{runtime}/{action}"
            );
        }
    }
}
fn trigger_view(field: &str) -> View {
    let mut v = View::default();
    v.open_field(field);
    let generation = read(&mut v);
    v.observe(Update::Snapshot {
        generation,
        snapshot: Box::new(snapshot(&data())),
    });
    draw(&mut v, 90, 30);
    assert_eq!(v.selected_field(), field);
    v
}
fn review_pages(v: &mut View, field: &str, value: &Value) -> String {
    let (generation, review) = validate(v);
    assert_eq!((review.field.as_str(), &review.value), (field, value));
    v.observe(Update::Review {
        generation,
        review: Box::new(review),
    });
    let mut seen = String::new();
    for _ in 0..20 {
        seen.push_str(&draw(v, 48, 20));
        v.key(key(KeyCode::PageDown));
    }
    seen.chars().filter(|c| c.is_alphanumeric()).collect()
}
#[test]
fn autocompact_percent_outside_one_through_ninety_nine_never_leaves_the_editor() {
    let mut v = trigger_view("autocompact_at_percent");
    assert_eq!(v.draft(), "80");
    assert!(v.accepts_paste());
    for bad in ["0", "00", "100", "9999999999"] {
        replace(&mut v, bad);
        draw(&mut v, 90, 30);
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none(), "{bad} must be refused locally");
        assert!(!v.waiting, "{bad}");
        assert!(draw(&mut v, 90, 30).contains("invalid scalar type or value"));
        assert_eq!(v.draft(), bad);
    }
    for (good, expected) in [("1", 1), ("99", 99), ("0080", 80)] {
        let mut v = trigger_view("autocompact_at_percent");
        replace(&mut v, good);
        let (_, r) = validate(&mut v);
        assert_eq!(r.value, json!(expected));
    }
}
#[test]
fn autocompact_toggle_is_a_boolean_and_its_review_states_the_real_threshold_check() {
    let mut v = trigger_view("autocompact_enabled");
    assert_eq!(v.draft(), "true");
    assert!(!v.accepts_paste());
    v.key(key(KeyCode::Right));
    assert_eq!(v.draft(), "false");
    let painted = review_pages(&mut v, "autocompact_enabled", &json!(false));
    for text in [
        "applies: next_turn",
        "tested after a turn and between tool rounds",
        "saving never starts one",
        "Codex engine owns its own context",
    ] {
        let expected: String = text.chars().filter(|c| c.is_alphanumeric()).collect();
        assert!(painted.contains(&expected), "missing {text}");
    }
}
#[test]
fn only_the_autocompact_reviews_carry_the_threshold_disclosure() {
    let mut v = trigger_view("autocompact_at_percent");
    replace(&mut v, "65");
    let painted = review_pages(&mut v, "autocompact_at_percent", &json!(65));
    assert!(painted.contains("Autocompactistestedafteraturn"));
    let mut other = view();
    replace(&mut other, "8");
    let painted = review_pages(&mut other, "compact_keep_recent", &json!(8));
    assert!(!painted.contains("Autocompactistested"));
}
