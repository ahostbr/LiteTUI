use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseEvent, MouseEventKind};
use litetui_terminal::{
    context::{Document, MAX_DOCUMENT_BYTES, OPERATION, PAGE_BYTES, Page, Request, Status},
    context_view::View,
    theme::builtin_themes,
    work_protocol::Identity,
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

const INSTANCE: &str = "ab34fe8d-ae45-4191-bdd8-53e82c2a2454";
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

fn wire(req: &Request, doc: &str) -> Value {
    let mut response = req.command(&identity(), INSTANCE);
    assert_eq!(
        response.as_object_mut().unwrap().remove("type"),
        Some(json!(OPERATION))
    );
    response["kind"] = json!("context_page");
    response["status"] = json!(if doc.is_empty() { "empty" } else { "page" });
    response["revision"] = json!(digest(doc));
    let mut end = (req.offset + PAGE_BYTES).min(doc.len());
    while !doc.is_char_boundary(end) {
        end -= 1;
    }
    response["text"] = json!(&doc[req.offset..end]);
    response["total_bytes"] = json!(doc.len());
    response["next_offset"] = if end == doc.len() {
        Value::Null
    } else {
        json!(end)
    };
    response
}
fn parsed(req: &Request, doc: &str) -> Page {
    Page::parse(&wire(req, doc), &identity(), INSTANCE, req).unwrap()
}
fn unavailable(req: &Request, status: &str) -> Value {
    let mut value = req.command(&identity(), INSTANCE);
    value.as_object_mut().unwrap().remove("type");
    value["kind"] = json!("context_page");
    value["status"] = json!(status);
    for key in ["revision", "next_offset", "total_bytes"] {
        value[key] = Value::Null;
    }
    value["text"] = json!("");
    value
}
fn draw(v: &mut View, width: u16, height: u16) -> String {
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    let theme = builtin_themes().remove(0);
    terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(width as usize)
        .map(|line| line.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}

#[test]
fn exact_identity_document_fields_and_byte_counts_are_required() {
    let req = Request::first(Document::Memory);
    let doc = "é".repeat(3500);
    let good = wire(&req, &doc);
    assert_eq!(parsed(&req, &doc).next_offset, Some(PAGE_BYTES));
    for (field, invalid) in [
        ("version", json!(true)),
        ("kind", json!("context")),
        ("session_id", json!("another")),
        ("agent_id", json!("another")),
        ("backend", json!("another")),
        ("model", json!("another")),
        ("work_instance", json!("another")),
        ("document", json!("soul.md")),
        ("offset", json!(1)),
        ("offset", json!(0.0)),
        ("total_bytes", json!(-1)),
        ("total_bytes", json!(MAX_DOCUMENT_BYTES + 1)),
        ("next_offset", json!(4095)),
        ("next_offset", Value::Null),
        ("text", json!("x".repeat(4097))),
        ("status", json!("empty")),
        ("revision", json!("F".repeat(64))),
        ("text", json!("x")),
        ("unknown", json!(false)),
    ] {
        let mut bad = good.clone();
        bad[field] = invalid;
        assert!(
            Page::parse(&bad, &identity(), INSTANCE, &req).is_err(),
            "{field}"
        );
    }
    let mut bad_final = wire(&req, "all text");
    bad_final["revision"] = json!(digest("forged"));
    assert!(Page::parse(&bad_final, &identity(), INSTANCE, &req).is_err());
    let mut bad_split = good;
    bad_split["total_bytes"] = json!(4096);
    bad_split["text"] = json!("x".repeat(4093));
    bad_split["next_offset"] = json!(4093);
    assert!(Page::parse(&bad_split, &identity(), INSTANCE, &req).is_err());
}

#[test]
fn absent_empty_oversize_and_changed_have_disjoint_evidence() {
    let req = Request::first(Document::Soul);
    let revision = digest("");
    let empty = parsed(&req, "");
    assert_eq!(empty.status, Status::Empty);
    assert_eq!(empty.revision.as_deref(), Some(revision.as_str()));
    for status in ["missing", "oversize"] {
        let mut absent = unavailable(&req, status);
        assert!(Page::parse(&absent, &identity(), INSTANCE, &req).is_ok());
        absent["text"] = json!("previous contents");
        assert!(Page::parse(&absent, &identity(), INSTANCE, &req).is_err());
    }
    assert!(Page::parse(&unavailable(&req, "changed"), &identity(), INSTANCE, &req).is_err());
    let pinned = Request {
        offset: 4096,
        revision: Some(revision),
        ..req
    };
    assert!(
        Page::parse(
            &unavailable(&pinned, "missing"),
            &identity(),
            INSTANCE,
            &pinned
        )
        .is_err()
    );
    let page = Page::parse(
        &unavailable(&pinned, "changed"),
        &identity(),
        INSTANCE,
        &pinned,
    )
    .unwrap();
    assert_eq!(page.status, Status::Changed);
}

#[test]
fn preserves_utf8_boundaries_and_revalidates_backward_and_changed_reads() {
    let doc = format!("{}🌼{}", "x".repeat(PAGE_BYTES - 3), "y".repeat(PAGE_BYTES));
    let mut view = View::default();
    let first = view.open();
    view.apply(parsed(&first, &doc));
    draw(&mut view, 76, 16);
    let second = view.key(key(KeyCode::Char(']'))).unwrap();
    assert_eq!(second.offset, PAGE_BYTES - 3);
    assert_eq!(second.revision.as_deref(), Some(digest(&doc).as_str()));
    assert!(view.key(key(KeyCode::Right)).is_none());
    view.apply(parsed(&second, &doc));
    let third = view.key(key(KeyCode::Char(']'))).unwrap();
    view.apply(parsed(&third, &doc));
    assert!(view.key(key(KeyCode::Char(']'))).is_none());
    let back = view.key(key(KeyCode::Char('['))).unwrap();
    assert_eq!(back, second);
    view.apply(parsed(&back, &doc));
    let start = view.key(key(KeyCode::Char('['))).unwrap();
    assert_eq!(start.offset, 0);
    assert_eq!(start.revision, second.revision);
    view.apply(
        Page::parse(
            &unavailable(&start, "changed"),
            &identity(),
            INSTANCE,
            &start,
        )
        .unwrap(),
    );
    assert!(draw(&mut view, 76, 16).contains("Document changed"));
    assert!(view.page().unwrap().text.is_empty());
    assert!(view.key(key(KeyCode::Char(']'))).is_none());
    let refreshed = view.key(key(KeyCode::F(5))).unwrap();
    assert_eq!(refreshed, first);
}

#[test]
fn changed_bytes_under_the_same_claimed_revision_are_refused() {
    let contents = "a".repeat(PAGE_BYTES + 5);
    let mut view = View::default();
    let first = view.open();
    view.apply(parsed(&first, &contents));
    draw(&mut view, 60, 12);
    let next = view.key(key(KeyCode::Char(']'))).unwrap();
    let mut substituted = wire(&next, &contents);
    substituted["text"] = json!("wrong");
    view.apply(Page::parse(&substituted, &identity(), INSTANCE, &next).unwrap());
    assert!(view.notice().contains("disagree"));
    assert!(view.page().is_none());
}

#[test]
fn reader_escapes_terminal_controls_scrolls_and_ignores_hidden_input() {
    let mut view = View::default();
    let first = view.open();
    let contents = format!("first\x1b[31m\u{202e}\r\n{}\nLAST", "x\n".repeat(20));
    view.apply(parsed(&first, &contents));
    let screen = draw(&mut view, 70, 14);
    assert!(screen.contains("first\\u{1b}[31m\\u{202e}"));
    assert!(!screen.contains("LAST") && !screen.contains("\\u{d}"));
    view.key(key(KeyCode::End));
    assert!(draw(&mut view, 70, 14).contains("LAST"));
    view.hide();
    assert!(view.key(key(KeyCode::Tab)).is_none());
    assert!(
        view.mouse(MouseEvent {
            kind: MouseEventKind::ScrollUp,
            column: 7,
            row: 5,
            modifiers: KeyModifiers::NONE
        })
        .is_none()
    );
    draw(&mut view, 70, 14);
    assert!(
        view.key(KeyEvent::new_with_kind(
            KeyCode::Right,
            KeyModifiers::NONE,
            KeyEventKind::Repeat
        ))
        .is_none()
    );
    let next = view.key(key(KeyCode::Tab)).unwrap();
    assert_eq!(next.document, Document::Memory);
    assert_eq!(next.offset, 0);
    assert!(next.revision.is_none());
    view.key(key(KeyCode::Esc));
    assert!(!view.is_open());
    view.apply(parsed(&next, "late private text"));
    assert!(view.page().is_none() && !draw(&mut view, 70, 14).contains("late private text"));
}

#[test]
fn memory_review_requires_all_rendered_lines_after_resize_and_fresh_visible_press() {
    use litetui_terminal::context_edit::{Base, Reply, Request as Edit, Review};
    let mut view = View::default();
    let request = view.open();
    view.apply(parsed(&request, "system"));
    draw(&mut view, 80, 20);
    let request = view.key(key(KeyCode::Right)).unwrap();
    view.apply(parsed(&request, "original"));
    draw(&mut view, 80, 20);
    assert!(matches!(
        view.edit_key(key(KeyCode::Char('e'))),
        Some(Edit::Open(Document::Memory, _))
    ));
    let base = Base {
        document: Document::Memory,
        revision: Some(digest("original")),
        text: "original".into(),
    };
    view.edit_reply(Reply::Opened(base.clone()));
    draw(&mut view, 80, 20);
    view.paste(&"\nline".repeat(54));
    let Some(Edit::Review(_, draft)) = view.edit_key(key(KeyCode::F(6))) else {
        panic!("expected review")
    };
    let mut review = Review {
        base: base.clone(),
        token: "a".repeat(64),
        draft: draft.clone(),
    };
    review.draft.push('!');
    view.edit_reply(Reply::Reviewed(review.clone()));
    assert!(draw(&mut view, 80, 20).contains("Edit memory.md"));
    review.draft = draft;
    view.edit_reply(Reply::Reviewed(review));
    draw(&mut view, 80, 20);
    assert!(view.edit_key(key(KeyCode::F(6))).is_none());
    view.edit_key(key(KeyCode::End));
    draw(&mut view, 80, 20);
    assert!(
        view.edit_key(key(KeyCode::F(6))).is_none(),
        "Skipping the middle never authorizes"
    );
    view.edit_key(key(KeyCode::Home));
    for _ in 0..10 {
        draw(&mut view, 80, 20);
        view.edit_key(key(KeyCode::PageDown));
    }
    assert!(matches!(
        view.edit_key(key(KeyCode::F(6))),
        Some(Edit::Apply(_))
    ));
    draw(&mut view, 50, 15);
    assert!(view.edit_key(key(KeyCode::F(6))).is_none());
    for _ in 0..15 {
        draw(&mut view, 50, 15);
        view.edit_key(key(KeyCode::PageDown));
    }
    let repeat = KeyEvent::new_with_kind(KeyCode::F(6), KeyModifiers::NONE, KeyEventKind::Repeat);
    assert!(view.edit_key(repeat).is_none());
    view.hide();
    assert!(view.edit_key(key(KeyCode::F(6))).is_none());
    draw(&mut view, 20, 8);
    assert!(view.edit_key(key(KeyCode::F(6))).is_none());
    draw(&mut view, 50, 15);
    assert!(
        view.edit_key(key(KeyCode::F(6))).is_none(),
        "Tiny layout invalidated previous review coverage"
    );
}

#[test]
fn memory_parser_checks_full_identity_revision_bytes_and_readback_correlation() {
    use litetui_terminal::context_edit::{Base, Request as Edit, Review};
    let id = identity();
    let base = Base {
        document: Document::Memory,
        revision: None,
        text: String::new(),
    };
    let draft = "🌱\r\n";
    let review = Review {
        base: base.clone(),
        draft: draft.into(),
        token: "f".repeat(64),
    };
    let req = Edit::Apply(review.clone());
    let mut v = id.request();
    for (k, val) in [
        ("kind", json!("context_apply")),
        ("work_instance", json!(INSTANCE)),
        ("document", json!("memory.md")),
        ("token", json!(review.token)),
        ("revision", json!(digest(draft))),
        ("bytes", json!(draft.len())),
        ("apply_id", json!("c1")),
    ] {
        v[k] = val;
    }
    req.readback(&v, &id, INSTANCE, "c1").unwrap();
    assert!(req.reply(&v, &id, INSTANCE, Some("c1")).is_err());
    v["kind"] = json!("context_saved");
    req.reply(&v, &id, INSTANCE, Some("c1")).unwrap();
    for (k, val) in [
        ("revision", json!(digest(""))),
        ("apply_id", json!("c2")),
        ("document", json!("handoff.md")),
        ("work_instance", json!("foreign")),
        ("backend", json!("foreign")),
        ("bytes", json!(1)),
        ("extra", Value::Null),
    ] {
        let mut changed = v.clone();
        changed[k] = val;
        assert!(
            req.reply(&changed, &id, INSTANCE, Some("c1")).is_err(),
            "{k}"
        );
    }
    assert!(
        Edit::Review(base, "🌱".repeat(65537))
            .command(&id, INSTANCE)
            .is_err()
    );
    for doc in [Document::System, Document::Hooks] {
        assert!(Edit::Open(doc, None).command(&id, INSTANCE).is_err());
    }
}
