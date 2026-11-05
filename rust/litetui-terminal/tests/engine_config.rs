use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use litetui_terminal::{
    engine_config as cfg,
    engine_controls::{Admission, Identity, Intent, Update, View},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

fn owner() -> Identity {
    Identity {
        session: "00000000-0000-4000-8000-000000000001".into(),
        agent: "11111111-1111-4111-8111-111111111111".into(),
        backend: "strata".into(),
        model: "engine-model".into(),
        instance: "1".repeat(64),
    }
}
fn wire(kind: &str, fields: Value) -> Value {
    let mut v = owner().command("ignored");
    v.as_object_mut().unwrap().remove("type");
    v["kind"] = json!(kind);
    v.as_object_mut()
        .unwrap()
        .extend(fields.as_object().unwrap().clone());
    v
}
fn state() -> Value {
    let mut fields = json!({});
    for name in [
        "strata_host",
        "strata_root",
        "strata_config",
        "strata_max_context",
    ] {
        let value = if name == "strata_max_context" {
            Value::Null
        } else {
            json!("")
        };
        fields[name] = json!({"saved":value,"effective":value,"timing":if name=="strata_host"{"reconnect"}else{"restart"},"locked":null});
    }
    wire(
        "engine_config_snapshot",
        json!({"scope":"conversation","generation":0,"admission":"management_only","fields":fields,"revisions":{"global":"absent","agent":"absent","conversation":"absent"},"destination":"owned/conversation/settings.json","snapshot":"a".repeat(64)}),
    )
}
fn parse(v: &Value) -> std::io::Result<cfg::Snapshot> {
    cfg::Snapshot::parse(v, &owner(), 0, Admission::ManagementOnly)
}
fn reviewed(s: &cfg::Snapshot, field: &str, value: &Value) -> (Value, cfg::Review) {
    let mut v = s.wire.clone();
    v.as_object_mut().unwrap().remove("fields");
    let row = &s.wire["fields"][field];
    v["kind"] = json!("engine_config_review");
    v["field"] = json!(field);
    v["value"] = value.clone();
    v["saved_before"] = row["saved"].clone();
    v["effective_before"] = row["effective"].clone();
    v["timing"] = row["timing"].clone();
    v["review"] = json!("b".repeat(64));
    let r = cfg::Review::parse(&v, s, field, value).unwrap();
    (v, r)
}
#[test]
fn preference_review_refusal_save_and_readback_bind_only_the_reviewed_destination() {
    let valid = state();
    for (field, value) in [
        ("scope", json!("device")),
        ("management_instance", json!("2".repeat(64))),
        ("generation", json!(1)),
        ("snapshot", json!("bad")),
        ("admission", json!("prompt_ready")),
    ] {
        let mut bad = valid.clone();
        bad[field] = value;
        assert!(parse(&bad).is_err(), "{field}");
    }
    let mut locked = valid.clone();
    locked["fields"]["strata_config"]["locked"] = json!("Launch-owned");
    assert!(
        parse(&locked)
            .unwrap()
            .review_command("strata_config", &json!("new.json"))
            .is_err()
    );
    assert!(cfg::input("ninfer_max_concurrency", "9").is_err());
    assert!(cfg::input("strata_root", &"é".repeat(2049)).is_err());
    assert_eq!(cfg::input("strata_max_context", "").unwrap(), Value::Null);
    let s = parse(&valid).unwrap();
    let (v, r) = reviewed(&s, "strata_config", &json!("my.json"));
    let mut wrong = v;
    wrong["destination"] = json!("other/settings.json");
    assert!(cfg::Review::parse(&wrong, &s, "strata_config", &json!("my.json")).is_err());
    let mut refusal = wire(
        "engine_config_refused",
        json!({"review":r.token,"field":r.field,"save_id":"g1-c9","mutated":false,"reason":"Configuration changed"}),
    );
    assert_eq!(
        r.refusal(&refusal, "g1-c9").unwrap(),
        "Configuration changed"
    );
    refusal["mutated"] = json!(true);
    assert!(r.refusal(&refusal, "g1-c9").is_err());
    let mut saved = wire(
        "engine_config_save",
        json!({"review":r.token,"field":r.field,"save_id":"g1-c9","saved":r.value,"effective":"","timing":"restart","revisions":{"global":"absent","agent":"absent","conversation":"f".repeat(64)},"generation":0,"admission":"management_only"}),
    );
    assert_eq!(r.receipt(&saved, "g1-c9").unwrap()["type"], cfg::READBACK);
    let mut read = saved.clone();
    read["kind"] = json!("engine_config_readback");
    r.verify(&read, &saved).unwrap();
    read["effective"] = json!("my.json");
    assert!(r.verify(&read, &saved).is_err());
    saved["revisions"]["agent"] = json!("a".repeat(64));
    assert!(r.receipt(&saved, "g1-c9").is_err());
}
fn draw(view: &mut View, w: u16, h: u16) -> String {
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
    terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn key(view: &mut View, code: KeyCode) {
    view.key(KeyEvent::new(code, KeyModifiers::NONE));
}
#[test]
fn configuration_requires_all_visible_pages_after_resize_and_a_fresh_confirmation() {
    let mut view = View::default();
    view.open();
    view.take_intent();
    view.observe(Update::Rejected("Inspecting".into()));
    draw(&mut view, 72, 24);
    key(&mut view, KeyCode::Char('c'));
    assert!(matches!(
        view.take_intent(),
        Some(Intent::Config(cfg::Intent::Read))
    ));
    let s = parse(&state()).unwrap();
    view.observe(Update::Config(Box::new(cfg::Update::Snapshot(s.clone()))));
    draw(&mut view, 72, 24);
    key(&mut view, KeyCode::Down);
    key(&mut view, KeyCode::Enter);
    draw(&mut view, 72, 24);
    view.config_paste(&"x".repeat(4000));
    view.config_paste(&"z".repeat(100));
    key(&mut view, KeyCode::Enter);
    let Some(Intent::Config(cfg::Intent::Review { field, value, .. })) = view.take_intent() else {
        panic!("Missing review")
    };
    assert_eq!(field, "strata_root");
    assert_eq!(value.as_str().unwrap().len(), 4000);
    let (_, r) = reviewed(&s, &field, &value);
    view.observe(Update::Config(Box::new(cfg::Update::Review(r))));
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    draw(&mut view, 50, 18);
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    for _ in 0..24 {
        key(&mut view, KeyCode::PageDown);
        draw(&mut view, 50, 18);
    }
    draw(&mut view, 52, 20);
    key(&mut view, KeyCode::Enter);
    assert!(view.take_intent().is_none());
    for _ in 0..24 {
        key(&mut view, KeyCode::PageDown);
        draw(&mut view, 52, 20);
    }
    view.key(KeyEvent::new_with_kind(
        KeyCode::Enter,
        KeyModifiers::NONE,
        KeyEventKind::Repeat,
    ));
    assert!(view.take_intent().is_none());
    key(&mut view, KeyCode::Enter);
    assert!(matches!(
        view.take_intent(),
        Some(Intent::Config(cfg::Intent::Save(_)))
    ));
}
