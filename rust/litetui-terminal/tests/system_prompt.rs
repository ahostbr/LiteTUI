//! System prompt editing: strict reply parsing, `/system` prefill, review gating and routing.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    command_palette::{Action, Catalog, Route, Update},
    context::{Document, MAX_DOCUMENT_BYTES, PAGE_BYTES, Page, Request, Status},
    context_edit::{Base, Reply, Request as Edit, Review, SYSTEM_OPERATIONS, SystemFacts},
    context_view::View,
    demo::Demo,
    live::Observation,
    theme::builtin_themes,
    work_protocol::Identity,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const INSTANCE: &str = "ab34fe8d-ae45-4191-bdd8-53e82c2a2454";
const EFFECT: &str = "Saved now and sent with your next request in this conversation.";
fn identity() -> Identity {
    Identity {
        session: "48732232-6a68-4c03-bf1f-50b6fd19c775".into(),
        agent: "39fa047c-c3db-4297-9498-569d11e7a3af".into(),
        backend: "host".into(),
        model: "test".into(),
    }
}
fn digest(s: &str) -> String {
    format!("{:x}", Sha256::digest(s.as_bytes()))
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn draw(view: &mut View, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    let theme = builtin_themes().remove(0);
    terminal.draw(|f| view.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(width as usize)
        .map(|line| line.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn page(req: &Request, doc: &str) -> Page {
    let mut v = req.command(&identity(), INSTANCE);
    v.as_object_mut().unwrap().remove("type");
    v["kind"] = json!("context_page");
    v["status"] = json!(if doc.is_empty() { "empty" } else { "page" });
    v["revision"] = json!(digest(doc));
    v["text"] = json!(doc);
    v["total_bytes"] = json!(doc.len());
    v["next_offset"] = Value::Null;
    Page::parse(&v, &identity(), INSTANCE, req).unwrap()
}
fn facts(stored: &str) -> SystemFacts {
    SystemFacts {
        effect: EFFECT.into(),
        stored: stored.into(),
    }
}
fn base(text: &str) -> Base {
    Base {
        document: Document::System,
        revision: Some(digest(text)),
        text: text.into(),
        system: Some(facts("saved")),
    }
}
fn reply_for(req: &Edit, kind: &str, fields: Value) -> Value {
    let mut v = req.command(&identity(), INSTANCE).unwrap();
    v.as_object_mut().unwrap().remove("type");
    v["kind"] = json!(kind);
    for key in ["revision", "text", "token", "apply_id", "work_instance"] {
        if fields.get(key).is_none() && key != "work_instance" {
            v.as_object_mut().unwrap().remove(key);
        }
    }
    for (k, val) in fields.as_object().unwrap() {
        v[k] = val.clone();
    }
    v
}

#[test]
fn system_uses_its_own_operations_and_bounds_every_draft() {
    let id = identity();
    let open = Edit::Open(Document::System, Some(digest("old")));
    assert_eq!(open.operation(), SYSTEM_OPERATIONS[0]);
    assert_eq!(open.command(&id, INSTANCE).unwrap()["document"], "system");
    let review = Edit::Review(base("old"), "new".into());
    assert_eq!(review.operation(), SYSTEM_OPERATIONS[1]);
    for draft in ["", " \r\n\t ", "a\0b", &"x".repeat(MAX_DOCUMENT_BYTES + 1)] {
        assert!(
            Edit::Review(base("old"), draft.into())
                .command(&id, INSTANCE)
                .is_err(),
            "{draft:?}"
        );
    }
    let exact = "x".repeat(MAX_DOCUMENT_BYTES);
    let command = Edit::Review(base("old"), exact.clone())
        .command(&id, INSTANCE)
        .unwrap();
    assert_eq!(command["text"].as_str().unwrap().len(), MAX_DOCUMENT_BYTES);
    let exact_text = "A\r\n\tB\n";
    let command = Edit::Review(base("old"), exact_text.into())
        .command(&id, INSTANCE)
        .unwrap();
    assert_eq!(command["text"], exact_text);
    // Memory documents keep their own operations.
    assert_eq!(
        Edit::Open(Document::Memory, None).operation(),
        "gui.context.native_edit"
    );
}

#[test]
fn replies_require_exact_fields_matching_effect_and_a_representable_persistence_state() {
    let id = identity();
    let open = Edit::Open(Document::System, Some(digest("old")));
    let good = reply_for(
        &open,
        "context_edit",
        json!({"revision": digest("old"), "text": "old", "effect": EFFECT, "stored": "diverged"}),
    );
    let Reply::Opened(opened) = open.reply(&good, &id, INSTANCE, None).unwrap() else {
        panic!("opened")
    };
    assert_eq!(opened.system.unwrap().stored, "diverged");
    for (field, invalid) in [
        ("effect", json!("")),
        ("effect", json!("x".repeat(1025))),
        ("effect", Value::Null),
        ("stored", json!("unknown")),
        ("stored", Value::Null),
        ("revision", json!(digest("other"))),
        ("text", json!("changed")),
    ] {
        let mut bad = good.clone();
        bad[field] = invalid;
        assert!(open.reply(&bad, &id, INSTANCE, None).is_err(), "{field}");
    }
    let mut extra = good.clone();
    extra["path"] = json!("x");
    assert!(open.reply(&extra, &id, INSTANCE, None).is_err());
    let mut missing = good.clone();
    missing.as_object_mut().unwrap().remove("effect");
    assert!(open.reply(&missing, &id, INSTANCE, None).is_err());

    let draft = "new\r\n\t雪";
    let review_req = Edit::Review(base("old"), draft.into());
    let review_reply = reply_for(
        &review_req,
        "context_review",
        json!({"token": "f".repeat(64), "before_revision": digest("old"), "before_bytes": 3,
               "after_revision": digest(draft), "after_bytes": draft.len(), "effect": EFFECT}),
    );
    assert!(matches!(
        review_req
            .reply(&review_reply, &id, INSTANCE, None)
            .unwrap(),
        Reply::Reviewed(_)
    ));
    let mut changed = review_reply.clone();
    changed["effect"] = json!("A different consumer.");
    assert!(review_req.reply(&changed, &id, INSTANCE, None).is_err());
    let mut stale = review_reply.clone();
    stale["after_revision"] = json!(digest("different"));
    assert!(review_req.reply(&stale, &id, INSTANCE, None).is_err());

    let review = Review {
        base: base("old"),
        draft: draft.into(),
        token: "f".repeat(64),
    };
    let apply = Edit::Apply(review.clone());
    let applied = json!({"token": review.token, "apply_id": "c1", "revision": digest(draft),
        "bytes": draft.len(), "stored": "session_only"});
    let readback = reply_for(&apply, "context_apply", applied.clone());
    let command = apply.readback(&readback, &id, INSTANCE, "c1").unwrap();
    assert_eq!(command["type"], SYSTEM_OPERATIONS[3]);
    assert_eq!(command["apply_id"], "c1");
    for stored in ["diverged", "unknown"] {
        let mut bad = readback.clone();
        bad["stored"] = json!(stored);
        assert!(
            apply.readback(&bad, &id, INSTANCE, "c1").is_err(),
            "{stored}"
        );
    }
    let mut saved = applied.clone();
    saved["effect"] = json!(EFFECT);
    let saved = reply_for(&apply, "context_saved", saved);
    let Reply::Saved(done) = apply.reply(&saved, &id, INSTANCE, Some("c1")).unwrap() else {
        panic!("saved")
    };
    assert_eq!(done.text, draft);
    assert_eq!(done.system.unwrap().stored, "session_only");
    let mut diverged = saved.clone();
    diverged["stored"] = json!("diverged");
    assert!(apply.reply(&diverged, &id, INSTANCE, Some("c1")).is_err());
    let mut wrong = saved.clone();
    wrong["revision"] = json!(digest("not the reviewed draft"));
    assert!(apply.reply(&wrong, &id, INSTANCE, Some("c1")).is_err());
}

fn open_editor(view: &mut View, current: &str, prefill: Option<&str>) {
    let request = match prefill {
        Some(text) => view.open_system_editor(text),
        None => view.open(),
    };
    assert_eq!(request.document, Document::System);
    view.apply(page(&request, current));
    draw(view, 100, 24);
    let open = match prefill {
        Some(_) => view
            .continue_system_editor()
            .expect("automatic editor open"),
        None => view.edit_key(key(KeyCode::Char('e'))).expect("manual open"),
    };
    let Edit::Open(Document::System, revision) = open else {
        panic!("system open")
    };
    assert_eq!(revision, Some(digest(current)));
    view.edit_reply(Reply::Opened(base(current)));
    assert!(draw(view, 100, 24).contains("Edit System prompt"));
}

#[test]
fn slash_system_text_prefills_the_editor_and_still_requires_review_apply_and_a_reread() {
    let mut view = View::default();
    let prefill = "Be terse.\r\n\tIndent 雪\n";
    open_editor(&mut view, "Old prompt", Some(prefill));
    let screen = draw(&mut view, 100, 24);
    assert!(screen.contains("Prefilled from /system"), "{screen}");
    assert!(screen.contains("Be terse."), "{screen}");
    // Nothing was requested for the mutation by opening the editor.
    assert!(view.continue_system_editor().is_none());
    let Some(Edit::Review(reviewed_base, draft)) = view.edit_key(key(KeyCode::F(6))) else {
        panic!("review")
    };
    assert_eq!(draft, prefill, "CR, LF and TAB are kept byte for byte");
    assert_eq!(reviewed_base.text, "Old prompt");
    let review = Review {
        base: reviewed_base,
        draft: draft.clone(),
        token: "a".repeat(64),
    };
    // Apply is unavailable until the review itself has been painted completely.
    view.edit_reply(Reply::Reviewed(review.clone()));
    assert!(view.edit_key(key(KeyCode::F(6))).is_none());
    let screen = draw(&mut view, 100, 24);
    assert!(screen.contains("Review System prompt"), "{screen}");
    assert!(screen.contains("Takes effect:"), "{screen}");
    assert!(
        screen.contains("Old prompt") && screen.contains("Be terse."),
        "{screen}"
    );
    let Some(Edit::Apply(applied)) = view.edit_key(key(KeyCode::F(6))) else {
        panic!("apply after the review was shown")
    };
    assert_eq!(applied, review);
    view.edit_reply(Reply::Saved(Base {
        document: Document::System,
        revision: Some(digest(&draft)),
        text: draft.clone(),
        system: Some(facts("saved")),
    }));
    assert!(draw(&mut view, 100, 24).contains("Saved to the transcript and verified"));
}

#[test]
fn bare_system_opens_the_current_text_and_a_session_only_prompt_says_so() {
    let mut view = View::default();
    open_editor(&mut view, "Current text", None);
    assert!(draw(&mut view, 100, 24).contains("Current text"));
    view.edit_key(key(KeyCode::Esc));
    let mut view = View::default();
    let request = view.open_system_editor("");
    view.apply(page(&request, "Held in memory"));
    draw(&mut view, 100, 24);
    view.continue_system_editor().unwrap();
    view.edit_reply(Reply::Opened(Base {
        system: Some(facts("session_only")),
        ..base("Held in memory")
    }));
    let screen = draw(&mut view, 100, 24);
    assert!(
        screen.contains("Held in memory") && !screen.contains("Prefilled from /system"),
        "bare /system edits the current text: {screen}"
    );
    assert!(
        screen.contains("Not in a conversation file yet"),
        "{screen}"
    );
}

#[test]
fn rejection_after_prefill_keeps_the_draft_and_unavailable_prompts_do_not_open_an_editor() {
    let mut view = View::default();
    open_editor(&mut view, "Old", Some("Draft to keep"));
    let Some(Edit::Review(..)) = view.edit_key(key(KeyCode::F(6))) else {
        panic!("review")
    };
    view.edit_rejected(
        "An operation is active; cancel or wait for it before changing this resource",
    );
    let screen = draw(&mut view, 100, 24);
    assert!(
        screen.contains("Draft kept.") && screen.contains("Draft to keep"),
        "{screen}"
    );
    let mut view = View::default();
    let request = view.open_system_editor("Draft");
    let mut value = request.command(&identity(), INSTANCE);
    value.as_object_mut().unwrap().remove("type");
    value["kind"] = json!("context_page");
    value["status"] = json!("oversize");
    for key in ["revision", "next_offset", "total_bytes"] {
        value[key] = Value::Null;
    }
    value["text"] = json!("");
    view.apply(Page::parse(&value, &identity(), INSTANCE, &request).unwrap());
    assert!(view.continue_system_editor().is_none());
    assert!(draw(&mut view, 100, 24).contains("cannot be edited right now"));
    assert!(view.continue_system_editor().is_none());
}

#[test]
fn missing_prompt_opens_an_empty_base_with_no_revision() {
    let mut view = View::default();
    let request = view.open_system_editor("Fresh");
    let mut value = request.command(&identity(), INSTANCE);
    value.as_object_mut().unwrap().remove("type");
    value["kind"] = json!("context_page");
    value["status"] = json!("missing");
    for key in ["revision", "next_offset", "total_bytes"] {
        value[key] = Value::Null;
    }
    value["text"] = json!("");
    let missing = Page::parse(&value, &identity(), INSTANCE, &request).unwrap();
    assert_eq!(missing.status, Status::Missing);
    view.apply(missing);
    draw(&mut view, 100, 24);
    assert_eq!(
        view.continue_system_editor(),
        Some(Edit::Open(Document::System, None))
    );
    let _ = PAGE_BYTES;
}

fn catalog() -> Catalog {
    let session = "12345678-1234-4123-8123-123456789abc";
    let agent = "22345678-1234-4123-8123-123456789abc";
    let entry = json!({"id": "1".repeat(64), "kind": "command", "owner": "convo", "title": "System",
        "help": "Set the system prompt", "group": "convo", "group_label": "Conversation", "order": 1,
        "tokens": ["/system"], "palette": false, "palette_order": null, "completion_order": 0,
        "skill_order": null, "native_action": "system_prompt", "accepts_arguments": true,
        "unavailable_reason": "", "source": null});
    let data = json!({"version": 1, "kind": "command_catalog", "session_id": session, "agent_id": agent,
        "backend": "custom", "model": "model", "snapshot": "a".repeat(64), "entries": [entry]});
    Catalog::parse(&data, session, agent, "custom", "model").unwrap()
}

fn route(c: &Catalog, raw: &str, arguments: &str) -> std::io::Result<Route> {
    let v = json!({"version": 1, "kind": "command_route", "session_id": "12345678-1234-4123-8123-123456789abc",
        "agent_id": "22345678-1234-4123-8123-123456789abc", "backend": "custom", "model": "model",
        "snapshot": c.snapshot, "row_id": c.entries[0].id, "native_action": "system_prompt",
        "arguments": arguments, "command": raw});
    Route::parse(&v, c, None, Some(raw), "")
}

#[test]
fn system_route_keeps_exact_arguments_bounds_them_and_never_applies() {
    let c = catalog();
    assert_eq!(c.entries[0].action, Some(Action::SystemPrompt));
    let exact = "  keep\r\n\tthis 雪  ";
    let routed = route(&c, "/system  keep\r\n\tthis 雪  ", exact).unwrap();
    assert_eq!(
        (routed.action, routed.arguments.as_str()),
        (Action::SystemPrompt, exact)
    );
    assert!(route(&c, "/system a\0b", "a\0b").is_err());
    let huge = "x".repeat(MAX_DOCUMENT_BYTES + 1);
    assert!(route(&c, "/system x", &huge).is_err());

    let routed = route(&c, "/system Be terse.", "Be terse.").unwrap();
    let mut app = Demo::live("custom".into(), "model".into());
    app.live.as_mut().unwrap().ready = true;
    app.insert_text("/system Be terse.");
    app.live.as_mut().unwrap().commands.raw("/system Be terse.");
    app.live.as_mut().unwrap().commands.take_intent();
    app.observe_live(Observation::Commands(Update::Route {
        catalog: c,
        route: routed,
    }))
    .unwrap();
    assert_eq!(app.composer.text(), "/system Be terse.");
    assert!(
        app.live.as_ref().unwrap().work.open,
        "opens Work, which reads before it edits"
    );
    assert!(app.live.as_ref().unwrap().rows.is_empty());
}
