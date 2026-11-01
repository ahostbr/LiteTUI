use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::conversation_browser::{Intent, Page, Update, View, connected_history};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};

const CURRENT: &str = "00000000-0000-4000-8000-000000000001";
const AGENT: &str = "11111111-1111-4111-8111-111111111111";
fn id(index: usize) -> String {
    format!("00000000-0000-4000-8000-{index:012x}")
}
fn data(offset: usize, total: usize) -> Value {
    let entries: Vec<Value> = (offset..(offset + 50).min(total))
        .map(|index| {
            let session = id(index + 1);
            let current = session == CURRENT;
            json!({"session_id":session,"agent_id":AGENT,"agent_name":"Owned agent",
            "title":format!("Saved conversation {}", index + 1),"message_count":index + 2,
            "updated_at":1234.5,"current":current,"can_open":!current,
            "reason":if current {"Already open"} else {""}})
        })
        .collect();
    json!({"session_id":CURRENT,"agent_id":AGENT,"snapshot":"a".repeat(64),"offset":offset,
        "total":total,"entries":entries,
        "previous_cursor":if offset>0 {json!(format!("page-{}",offset-50))} else {Value::Null},
        "next_cursor":if offset+50<total {json!(format!("page-{}",offset+50))} else {Value::Null}})
}
fn page() -> Page {
    Page::parse(&data(0, 3), 7, CURRENT).unwrap()
}
fn key(code: KeyCode) -> KeyEvent {
    KeyEvent::new(code, KeyModifiers::NONE)
}
fn mouse(kind: MouseEventKind, x: u16, y: u16) -> MouseEvent {
    MouseEvent {
        kind,
        column: x,
        row: y,
        modifiers: KeyModifiers::NONE,
    }
}
fn view() -> View {
    let mut view = View::default();
    view.open();
    assert_eq!(view.take_intent(), Some(Intent::Refresh));
    view.observe(Update::Page(page())).unwrap();
    view
}
fn draw(view: &mut View, width: u16, height: u16) -> String {
    let theme = litetui_terminal::theme::builtin_themes().remove(0);
    let mut terminal = Terminal::new(TestBackend::new(width, height)).unwrap();
    terminal
        .draw(|frame| view.draw(frame, frame.area(), &theme))
        .unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .chunks(width as usize)
        .map(|line| line.iter().map(|cell| cell.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}
fn select_second(view: &mut View) {
    draw(view, 80, 30);
    view.key(key(KeyCode::Down));
}
fn arm(view: &mut View) {
    view.key(key(KeyCode::Char('o')));
    draw(view, 80, 30);
}

#[test]
fn catalogue_page_keeps_source_identity_counts_and_explicit_eligibility() {
    let page = page();
    assert_eq!(page.session, CURRENT);
    assert_eq!(page.agent, AGENT);
    assert_eq!(page.total, 3);
    assert!(!page.entries[0].can_open);
    assert_eq!(page.entries[0].reason, "Already open");
    assert_eq!(page.entries[1].updated_at, 1234.5);
    assert!(page.selection(7, &id(2)).is_ok());
    for (revision, session) in [(8, id(2)), (7, CURRENT.into()), (7, id(100))] {
        assert!(page.selection(revision, &session).is_err());
    }
    let empty = Page::parse(&data(0, 0), 1, CURRENT).unwrap();
    assert!(empty.entries.is_empty() && empty.next.is_none() && empty.previous.is_none());
    let mut other_version = data(0, 3);
    other_version["entries"][1]["session_id"] = json!("00000000-0000-7000-8000-000000000002");
    assert!(
        Page::parse(&other_version, 1, CURRENT).is_ok(),
        "storage UUIDs are not restricted to emission UUID4"
    );
}
#[test]
fn malformed_or_unbounded_catalogue_is_never_presented_as_an_empty_list() {
    for fault in [
        "scope",
        "agent",
        "uuid",
        "duplicate",
        "owner",
        "current",
        "can-open",
        "reason",
        "count",
        "time",
        "time-negative",
        "title",
        "name",
        "cursor",
        "missing-cursor",
        "snapshot",
        "offset",
        "total",
        "short-page",
        "byte-budget",
    ] {
        let mut value = data(0, 3);
        match fault {
            "scope" => value["session_id"] = json!(id(99)),
            "agent" => value["agent_id"] = json!("bad"),
            "uuid" => value["entries"][1]["session_id"] = json!("../../other"),
            "duplicate" => value["entries"][1] = value["entries"][0].clone(),
            "owner" => value["entries"][1]["agent_id"] = json!(id(100)),
            "current" => value["entries"][1]["current"] = json!(true),
            "can-open" => value["entries"][0]["can_open"] = json!(true),
            "reason" => value["entries"][0]["reason"] = json!(""),
            "count" => value["entries"][1]["message_count"] = json!(true),
            "time" => value["entries"][1]["updated_at"] = json!("now"),
            "time-negative" => value["entries"][1]["updated_at"] = json!(-1),
            "title" => value["entries"][1]["title"] = json!("x".repeat(4097)),
            "name" => value["entries"][1]["agent_name"] = json!("x".repeat(1025)),
            "cursor" => value["next_cursor"] = json!("unexpected"),
            "missing-cursor" => {
                value.as_object_mut().unwrap().remove("next_cursor");
            }
            "snapshot" => value["snapshot"] = json!("A".repeat(64)),
            "offset" => value["offset"] = json!(1),
            "total" => value["total"] = json!(10_001),
            "short-page" => value["total"] = json!(4),
            "byte-budget" => value["extra"] = json!("x".repeat(256 * 1024)),
            _ => unreachable!(),
        }
        assert!(Page::parse(&value, 1, CURRENT).is_err(), "{fault}");
    }
}
#[test]
fn paging_requires_source_offered_cursor_and_same_catalogue_snapshot() {
    let first = Page::parse(&data(0, 70), 1, CURRENT).unwrap();
    let second = Page::parse(&data(50, 70), 2, CURRENT).unwrap();
    second.follows(&first, "page-50").unwrap();
    first.follows(&second, "page-0").unwrap();
    assert!(second.follows(&first, "invented").is_err());
    for fault in ["snapshot", "total", "offset", "agent", "session"] {
        let mut changed = second.clone();
        match fault {
            "snapshot" => changed.snapshot = "b".repeat(64),
            "total" => changed.total = 71,
            "offset" => changed.offset = 0,
            "agent" => changed.agent = id(100),
            "session" => changed.session = id(100),
            _ => unreachable!(),
        }
        assert!(changed.follows(&first, "page-50").is_err(), "{fault}");
    }
}
#[test]
fn connected_history_is_a_strict_barrier_and_does_not_settle_browser_open() {
    let value = json!({"session_id":id(2),"connected":true,"read_only":false,"meta":{"id":id(2)},
        "messages":[{"role":"user","content":"saved input"}]});
    let history = connected_history(&value, &id(2)).unwrap();
    assert_eq!(history.messages[0].text, "saved input");
    for fault in ["connection", "ownership", "identity", "messages"] {
        let mut invalid = value.clone();
        match fault {
            "connection" => invalid["connected"] = json!(false),
            "ownership" => invalid["read_only"] = json!(true),
            "identity" => invalid["session_id"] = json!(id(3)),
            "messages" => invalid["messages"] = json!(null),
            _ => unreachable!(),
        }
        assert!(connected_history(&invalid, &id(2)).is_err(), "{fault}");
    }
}
#[test]
fn opening_requires_an_offered_selection_drawn_confirmation_and_fresh_enter() {
    for (width, height) in [(36, 14), (80, 30), (140, 45)] {
        let mut v = view();
        v.key(key(KeyCode::Down));
        v.key(key(KeyCode::Char('o')));
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none());
        draw(&mut v, width, height);
        v.key(key(KeyCode::Char('o'))); // current row is not openable
        draw(&mut v, width, height);
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none());
        v.key(key(KeyCode::Down));
        v.key(key(KeyCode::Char('o')));
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none(), "confirmation was not visible");
        let visible = draw(&mut v, width, height);
        assert!(visible.contains("Open this conversation?"), "{visible}");
        assert!(visible.contains("Enter: open | Esc: cancel"), "{visible}");
        let mut held = key(KeyCode::Enter);
        held.kind = KeyEventKind::Repeat;
        v.key(held);
        assert!(v.take_intent().is_none());
        v.key(key(KeyCode::Enter));
        assert_eq!(
            v.take_intent(),
            Some(Intent::Open {
                revision: 7,
                session: id(2)
            })
        );
        assert!(v.waiting);
        v.close();
        assert!(
            v.owns_input(),
            "closing cannot release an already sent operation"
        );
        assert!(v.observe(Update::Opened { session: id(3) }).is_err());
        assert!(v.waiting);
        assert!(
            v.observe(Update::Page(page())).is_err(),
            "catalogue ACK is not completed open"
        );
        assert!(v.waiting);
        v.observe(Update::Opened { session: id(2) }).unwrap();
        assert!(!v.owns_input() && v.page.is_none());
    }
}
#[test]
fn geometry_focus_selection_pointer_modifier_and_source_changes_disarm_open() {
    for change in [
        "resize",
        "hidden",
        "hide",
        "selection",
        "pointer",
        "inspect",
        "source",
        "modifier",
        "escape",
        "paging",
    ] {
        let mut v = view();
        select_second(&mut v);
        arm(&mut v);
        match change {
            "resize" => {
                draw(&mut v, 90, 30);
            }
            "hidden" => {
                draw(&mut v, 35, 13);
            }
            "hide" => v.hide(),
            "selection" => v.key(key(KeyCode::Down)),
            "pointer" => v.mouse(mouse(MouseEventKind::Moved, 0, 0)),
            "inspect" => v.key(key(KeyCode::Right)),
            "source" => v.observe(Update::Page(page())).unwrap(),
            "modifier" => v.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL)),
            "escape" => v.key(key(KeyCode::Esc)),
            "paging" => v.key(key(KeyCode::PageUp)),
            _ => unreachable!(),
        }
        draw(&mut v, 80, 30);
        v.key(key(KeyCode::Enter));
        assert!(v.take_intent().is_none(), "{change}");
    }
}
#[test]
fn pointer_requires_a_paired_visible_row_and_never_opens_it() {
    for paired in [false, true] {
        let mut v = view();
        draw(&mut v, 80, 30);
        if paired {
            v.mouse(mouse(MouseEventKind::Down(MouseButton::Left), 5, 5));
        }
        v.mouse(mouse(MouseEventKind::Up(MouseButton::Left), 5, 5));
        assert!(v.take_intent().is_none());
        v.key(key(KeyCode::Char('o')));
        draw(&mut v, 80, 30);
        v.key(key(KeyCode::Enter));
        assert_eq!(
            v.take_intent(),
            if paired {
                Some(Intent::Open {
                    revision: 7,
                    session: id(2),
                })
            } else {
                None
            }
        );
    }
}
#[test]
fn paging_requests_only_offered_cursors_and_refresh_invalidates_confirmation() {
    let mut v = view();
    v.observe(Update::Page(
        Page::parse(&data(0, 70), 11, CURRENT).unwrap(),
    ))
    .unwrap();
    draw(&mut v, 80, 30);
    v.key(key(KeyCode::PageUp));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::PageDown));
    assert_eq!(
        v.take_intent(),
        Some(Intent::Page {
            revision: 11,
            cursor: "page-50".into()
        })
    );
    assert!(v.waiting && v.owns_input());
    v.close();
    assert!(v.owns_input());
    v.observe(Update::Page(
        Page::parse(&data(50, 70), 12, CURRENT).unwrap(),
    ))
    .unwrap();
    assert!(!v.owns_input());
    v.open();
    assert_eq!(v.take_intent(), Some(Intent::Refresh));
    v.observe(Update::Page(page())).unwrap();
    select_second(&mut v);
    arm(&mut v);
    v.key(key(KeyCode::Char('r')));
    assert_eq!(v.take_intent(), Some(Intent::Refresh));
    assert!(v.waiting && v.page.is_none());
}
#[test]
fn closing_unsent_intent_cancels_only_local_action() {
    let mut v = View::default();
    v.open();
    v.close();
    assert!(!v.owns_input() && v.take_intent().is_none());
    let mut v = view();
    select_second(&mut v);
    arm(&mut v);
    v.key(key(KeyCode::Enter));
    v.close();
    assert!(!v.owns_input() && v.take_intent().is_none());
    assert!(v.observe(Update::Opened { session: id(2) }).is_err());
}
#[test]
fn source_diagnostics_and_titles_remain_inspectable_and_terminal_controls_are_literal() {
    let mut value = data(0, 3);
    value["entries"][1]["title"] = json!("literal\n\t\u{1b}[31m title");
    let mut v = view();
    v.observe(Update::Page(Page::parse(&value, 8, CURRENT).unwrap()))
        .unwrap();
    select_second(&mut v);
    let visible = draw(&mut v, 80, 30);
    assert!(visible.contains("literal\\n\\t\\u{1b}[31m"), "{visible}");
    assert!(!visible.contains('\u{1b}'));
    v.observe(Update::Rejected(format!(
        "Source cannot list: {} DIAGNOSTIC-END",
        "x".repeat(200)
    )))
    .unwrap();
    let mut found = false;
    for _ in 0..30 {
        let text = draw(&mut v, 80, 30);
        found |= text.contains("DIAGNOSTIC-END");
        v.key(key(KeyCode::Right));
    }
    assert!(found);
    v.key(key(KeyCode::Char('o')));
    draw(&mut v, 80, 30);
    v.key(key(KeyCode::Enter));
    assert!(
        v.take_intent().is_none(),
        "a source failure is not an empty successful catalogue"
    );
}
#[test]
fn browser_uses_each_theme_and_preserves_minimum_geometry_confirmation() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut v = view();
        let mut terminal = Terminal::new(TestBackend::new(36, 14)).unwrap();
        terminal
            .draw(|frame| v.draw(frame, frame.area(), &theme))
            .unwrap();
        v.key(key(KeyCode::Down));
        v.key(key(KeyCode::Char('o')));
        terminal
            .draw(|frame| v.draw(frame, frame.area(), &theme))
            .unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|cell| cell.symbol())
            .collect::<String>();
        assert!(text.contains("Open this conversation?"), "{}", theme.name);
        v.key(key(KeyCode::Enter));
        assert_eq!(
            v.take_intent(),
            Some(Intent::Open {
                revision: 7,
                session: id(2)
            })
        );
        assert!(
            terminal
                .backend()
                .buffer()
                .content
                .iter()
                .any(|cell| cell.bg == theme.background)
        );
    }
}

fn state(session: &str) -> Value {
    json!({"session_id":session,"busy":false,"mode":"normal","tool_profile":"strict",
        "model":{"model":"model-A","backend":"claude"},"ownership":{"writable":true,"data_version":1,"agent_id":AGENT},
        "schedules_paused":true,"restored_input":{"policy":"hold","held":0},"conversations":[],"jobs":[],"tasks":[],
        "active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],
        "tool_policy":{"tools_enabled":false,"mcp_enabled":false,"tool_profile":"strict","tool_profile_requested":"strict","approval_route":"host","cwd":"C:/fixture"}})
}
fn reply(
    client: &mut litetui_terminal::management::Management,
    command: &Value,
    result: Value,
) -> std::io::Result<litetui_terminal::management::Step> {
    client.accept(json!({"type":"response","id":command["id"],"ok":true,"result":result}))
}
fn connected() -> litetui_terminal::management::Management {
    use litetui_terminal::management::{Management, Step};
    let mut client = Management::default();
    let ready = json!({"type":"ready","management_protocols":[1],"capabilities":["hello","state"],"conversation_id":CURRENT,"backend":"claude","model":"model-A","tool_profile":"strict"});
    client.accept(ready.clone()).unwrap();
    let command = client.begin(&ready).unwrap();
    let Step::Write(command) = reply(&mut client,&command,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],
        "operations":["gui.hello","gui.state","gui.conversations.page","gui.conversations.preview","gui.conversations.open","gui.restored_input.list"]})).unwrap() else { panic!() };
    assert_eq!(command["include_conversations"], false);
    assert!(matches!(
        reply(&mut client, &command, state(CURRENT)).unwrap(),
        Step::Initialized(_)
    ));
    client
}
fn pending_preview() -> (litetui_terminal::management::Management, Value) {
    use litetui_terminal::management::Step;
    let mut client = connected();
    let command = client.request_browser(Intent::Refresh).unwrap();
    let Step::Browser(Update::Page(page)) = reply(&mut client, &command, data(0, 3)).unwrap()
    else {
        panic!()
    };
    assert!(
        client
            .request_browser(Intent::Open {
                revision: page.revision,
                session: CURRENT.into()
            })
            .is_err()
    );
    let command = client
        .request_browser(Intent::Open {
            revision: page.revision,
            session: id(2),
        })
        .unwrap();
    assert_eq!(command["from_session_id"], CURRENT);
    assert_eq!(command["snapshot"], page.snapshot);
    assert!(!client.ready());
    (client, command)
}
fn preview() -> Value {
    let mut value = history();
    value.as_object_mut().unwrap().remove("connected");
    value["from_session_id"] = json!(CURRENT);
    value["agent_id"] = json!(AGENT);
    value["snapshot"] = json!("a".repeat(64));
    value
}
fn pending_open() -> (litetui_terminal::management::Management, Value) {
    use litetui_terminal::management::Step;
    let (mut client, command) = pending_preview();
    assert_eq!(command["type"], "gui.conversations.preview");
    let Step::Write(open) = reply(&mut client, &command, preview()).unwrap() else {
        panic!()
    };
    assert_eq!(open["type"], "gui.conversations.open");
    assert_ne!(open["id"], command["id"]);
    (client, open)
}
fn history() -> Value {
    json!({"connected":true,"session_id":id(2),"read_only":false,"meta":{"id":id(2)},"messages":[{"role":"user","content":"Saved target"}]})
}
fn queue() -> Value {
    json!({"session_id":id(2),"backend":"claude","model":"model-A","policy":"hold","snapshot":"b".repeat(64),"held":0,"entries":[]})
}
#[test]
fn open_commits_only_after_correlated_connection_state_and_held_readback() {
    use litetui_terminal::management::Step;
    let (mut client, command) = pending_open();
    assert!(client.prompt("early").is_err());
    let Step::Write(read) = reply(&mut client, &command, history()).unwrap() else {
        panic!()
    };
    assert_eq!(read["type"], "gui.state");
    assert_eq!(read["include_conversations"], false);
    assert!(!client.ready());
    let Step::Write(held) = reply(&mut client, &read, state(&id(2))).unwrap() else {
        panic!()
    };
    assert_eq!(held["type"], "gui.restored_input.list");
    assert_eq!(held["session_id"], id(2));
    assert!(!client.ready());
    let Step::Opened(opened) = reply(&mut client, &held, queue()).unwrap() else {
        panic!()
    };
    assert_eq!(opened.history.messages[0].text, "Saved target");
    assert_eq!(opened.snapshot.session, id(2));
    assert!(client.ready());
    let fresh = client.request_browser(Intent::Refresh).unwrap();
    assert_eq!(fresh["session_id"], id(2));
}
#[test]
fn opening_failure_and_inconsistent_readbacks_leave_connection_uncertain() {
    use litetui_terminal::management::Step;
    for phase in 0..3 {
        let (mut client, mut command) = pending_open();
        if phase > 0 {
            let Step::Write(next) = reply(&mut client, &command, history()).unwrap() else {
                panic!()
            };
            command = next;
        }
        if phase > 1 {
            let Step::Write(next) = reply(&mut client, &command, state(&id(2))).unwrap() else {
                panic!()
            };
            command = next;
        }
        assert!(client.accept(json!({"type":"response","id":command["id"],"ok":false,"error":"controlled failure"})).is_err());
        assert!(!client.ready());
        assert!(client.prompt("must not send").is_err());
    }
    for field in [
        "session", "backend", "policy", "owner", "agent", "held", "schedule", "model",
    ] {
        let (mut client, command) = pending_open();
        let Step::Write(read) = reply(&mut client, &command, history()).unwrap() else {
            panic!()
        };
        let mut value = state(&id(2));
        match field {
            "session" => value["session_id"] = json!(CURRENT),
            "backend" => value["model"]["backend"] = json!("codex"),
            "policy" => value["tool_profile"] = json!("autonomous"),
            "owner" => value["ownership"]["writable"] = json!(false),
            "agent" => value["ownership"]["agent_id"] = json!(id(9)),
            "held" => value["restored_input"]["policy"] = json!("automatic"),
            "schedule" => value["schedules_paused"] = json!(false),
            "model" => value["model"]["model"] = json!("unobserved"),
            _ => unreachable!(),
        };
        assert!(reply(&mut client, &read, value).is_err(), "{field}");
        assert!(!client.ready());
    }
    let (mut client, command) = pending_open();
    let Step::Write(read) = reply(&mut client, &command, history()).unwrap() else {
        panic!()
    };
    let Step::Write(read) = reply(&mut client, &read, state(&id(2))).unwrap() else {
        panic!()
    };
    let mut value = queue();
    value["session_id"] = json!(CURRENT);
    assert!(reply(&mut client, &read, value).is_err());
    assert!(!client.ready());
}
#[test]
fn only_captured_open_operation_can_emit_bounded_transition_observations() {
    use litetui_terminal::management::Step;
    for fault in [
        "",
        "foreign-session",
        "foreign-operation",
        "execution",
        "turn",
        "backend",
    ] {
        let (mut client, command) = pending_open();
        let mut event = json!({"type":"model_state","backend":"claude","model":"model-B"});
        if fault == "turn" {
            event = json!({"type":"turn_start","model":"model-A"});
        }
        if fault == "backend" {
            event["backend"] = json!("codex");
        }
        client.accept(event.clone()).unwrap();
        let mut envelope = json!({"type":"gui.event","event_id":"abcdefab-1234-4123-8123-123456789abc","session_id":id(2),"operation_id":command["id"],"event":event});
        match fault {
            "foreign-session" => envelope["session_id"] = json!(id(8)),
            "foreign-operation" => envelope["operation_id"] = json!("unrelated"),
            "execution" => envelope["execution_id"] = json!(id(9)),
            _ => {}
        }
        let result = client.accept(envelope);
        if fault.is_empty() {
            assert!(matches!(result.unwrap(), Step::Ignore));
            let Step::Write(read) = reply(&mut client, &command, history()).unwrap() else {
                panic!()
            };
            let mut value = state(&id(2));
            value["model"]["model"] = json!("model-B");
            assert!(matches!(
                reply(&mut client, &read, value).unwrap(),
                Step::Write(_)
            ));
        } else {
            assert!(result.is_err(), "{fault}");
        }
    }
}
#[test]
fn verified_open_replaces_history_clears_usage_and_preserves_composer_draft() {
    use litetui_terminal::{
        demo::Demo, live::Observation, management::Step, runtime_state::Update as Facts,
    };
    let (mut client, command) = pending_open();
    let Step::Write(read) = reply(&mut client, &command, history()).unwrap() else {
        panic!()
    };
    let Step::Write(read) = reply(&mut client, &read, state(&id(2))).unwrap() else {
        panic!()
    };
    let Step::Opened(opened) = reply(&mut client, &read, queue()).unwrap() else {
        panic!()
    };
    let mut app = Demo::live("claude".into(), "model-A".into());
    app.observe_live(Observation::State(Facts::from_event(&json!({"type":"ready","model":"old-model","conversation_id":CURRENT,"thinking_level":"high","context_length":999})).unwrap().unwrap())).unwrap();
    app.observe_live(Observation::State(
        Facts::from_event(&json!({"type":"turn_end","tps":12.5}))
            .unwrap()
            .unwrap(),
    ))
    .unwrap();
    app.observe_live(Observation::Ready).unwrap();
    app.composer.insert("UNSENT HUMAN DRAFT");
    app.observe_live(Observation::State(
        Facts::from_event(
            &json!({"type":"usage","usage":{"context_tokens":123,"max_context_tokens":999}}),
        )
        .unwrap()
        .unwrap(),
    ))
    .unwrap();
    app.open_browser();
    app.live.as_mut().unwrap().browser.take_intent();
    app.observe_live(Observation::Browser(Update::Page(page())))
        .unwrap();
    let browser = &mut app.live.as_mut().unwrap().browser;
    select_second(browser);
    arm(browser);
    browser.key(key(KeyCode::Enter));
    browser.take_intent();
    app.submit();
    assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
    app.observe_live(Observation::Opened(opened)).unwrap();
    assert_eq!(app.composer.text(), "UNSENT HUMAN DRAFT");
    let chat = app.live.as_ref().unwrap();
    assert_eq!(chat.history.as_ref().unwrap().session, id(2));
    assert!(!chat.browser.owns_input());
    assert_eq!(chat.runtime_state.startup().unwrap().session, id(2));
    assert!(chat.rows.is_empty());
    let fields = chat.runtime_state.footer_fields();
    use litetui_terminal::footer::FieldKey as K;
    assert!(
        fields
            .iter()
            .any(|field| field.key == K::Context && field.text == "ctx —")
    );
    assert!(
        fields
            .iter()
            .any(|field| field.key == K::Model && field.text == "model-A")
    );
    assert!(
        !fields
            .iter()
            .any(|field| matches!(field.key, K::Percent | K::Throughput | K::Thinking))
    );
    app.submit();
    assert_eq!(
        app.live.as_mut().unwrap().take_submission().as_deref(),
        Some("UNSENT HUMAN DRAFT")
    );
}

#[test]
fn catalogue_scope_is_captured_at_startup_and_old_session_events_fail_after_open() {
    use litetui_terminal::management::Step;
    let mut client = connected();
    let command = client.request_browser(Intent::Refresh).unwrap();
    let mut foreign = data(0, 3);
    foreign["agent_id"] = json!(id(9));
    for row in foreign["entries"].as_array_mut().unwrap() {
        row["agent_id"] = json!(id(9));
    }
    assert!(reply(&mut client, &command, foreign).is_err());
    assert!(!client.ready());
    let (mut client, command) = pending_open();
    let Step::Write(read) = reply(&mut client, &command, history()).unwrap() else {
        panic!()
    };
    let Step::Write(read) = reply(&mut client, &read, state(&id(2))).unwrap() else {
        panic!()
    };
    assert!(matches!(
        reply(&mut client, &read, queue()).unwrap(),
        Step::Opened(_)
    ));
    let event = json!({"type":"usage","usage":{"context_tokens":999}});
    client.accept(event.clone()).unwrap();
    assert!(
        client
            .accept(
                json!({"type":"gui.event","event_id":"abcdefab-1234-4123-8123-123456789abc",
        "session_id":CURRENT,"operation_id":command["id"],"event":event})
            )
            .is_err()
    );
    assert!(!client.ready());
}

#[test]
fn preview_uses_exact_native_display_budget_before_any_open_mutation() {
    use litetui_terminal::management::Step;
    for (length, offered) in [(32444, true), (32445, false), (40000, false)] {
        let (mut client, command) = pending_preview();
        let mut value = preview();
        value["messages"] = json!([{"role":"user","content":"x".repeat(length)}]);
        let result = reply(&mut client, &command, value).unwrap();
        if offered {
            let Step::Write(open) = result else { panic!() };
            assert_eq!(open["type"], "gui.conversations.open");
            assert!(!client.ready());
        } else {
            let Step::Browser(Update::Rejected(reason)) = result else {
                panic!()
            };
            assert!(reason.contains("Current conversation remains open"));
            assert!(client.ready());
            client.prompt("explicit-original-session-prompt").unwrap();
        }
    }
    let (mut client, command) = pending_preview();
    let Step::Browser(Update::Rejected(reason))=client.accept(json!({"type":"response","id":command["id"],"ok":false,"error":"Preview exceeds response budget"})).unwrap() else {panic!()};
    assert!(reason.contains("response budget"));
    assert!(client.ready());
}
#[test]
fn preview_must_echo_the_exact_selection_and_source_authority() {
    for field in [
        "session_id",
        "from_session_id",
        "agent_id",
        "snapshot",
        "read_only",
    ] {
        let (mut client, command) = pending_preview();
        let mut value = preview();
        value[field] = json!("foreign");
        assert!(reply(&mut client, &command, value).is_err(), "{field}");
        assert!(!client.ready());
    }
}

fn native_preview(read_only: bool) -> Box<litetui_terminal::history::Snapshot> {
    use litetui_terminal::history::{Identity, Scope, Snapshot};
    let identity = Identity {
        from_session: CURRENT.into(),
        session: id(2),
        agent: AGENT.into(),
        backend: "custom".into(),
        model: "test".into(),
        scope: Scope::Preview,
        catalog: Some("a".repeat(64)),
    };
    Box::new(Snapshot::parse(&json!({"version":1,"kind":"history_snapshot","projection_version":1,"scope":"preview","from_session_id":CURRENT,"session_id":id(2),"agent_id":AGENT,"backend":"custom","model":"test","catalog_snapshot":"a".repeat(64),"snapshot":"b".repeat(64),"transcript_revision":"c".repeat(64),"context_sha256":"d".repeat(64),"replay_sha256":"e".repeat(64),"pending":false,"materialized":true,"title":"Reviewed saved conversation","read_only":read_only,"open_effects":{"lifecycle_hooks":true,"provider_cleanup":true,"materialize":false},"open_action_id":AGENT,"fresh_system_context":false,"counts":{"messages":1203,"non_system":1202,"system":1,"blocks":1202}}),&identity).unwrap())
}
fn requested_native_preview() -> View {
    let mut view = view();
    select_second(&mut view);
    arm(&mut view);
    view.key(key(KeyCode::Enter));
    assert!(matches!(view.take_intent(), Some(Intent::Open { .. })));
    view
}
#[test]
fn native_open_requires_new_visible_review_after_preview_and_never_auto_opens() {
    let mut view = requested_native_preview();
    view.observe(Update::Preview(native_preview(false)))
        .unwrap();
    view.key(key(KeyCode::Enter));
    assert!(view.take_intent().is_none());
    for _ in 0..20 {
        let screen = draw(&mut view, 42, 18);
        if screen.contains("Enter: open once") {
            view.key(key(KeyCode::Enter));
            assert!(matches!(
                view.take_intent(),
                Some(Intent::ConfirmOpen { .. })
            ));
            return;
        }
        view.key(key(KeyCode::PageDown));
    }
    panic!("Review never became fully visible");
}
#[test]
fn read_only_native_preview_stays_non_actionable_and_late_preview_never_reopens() {
    let mut view = requested_native_preview();
    view.observe(Update::Preview(native_preview(true))).unwrap();
    for _ in 0..10 {
        let screen = draw(&mut view, 80, 30);
        assert!(screen.contains("read-only"));
        assert!(!screen.contains("Enter: open once"));
        view.key(key(KeyCode::Enter));
        assert!(view.take_intent().is_none());
        view.key(key(KeyCode::PageDown));
    }
    view.key(key(KeyCode::Esc));
    assert!(view.open);
    let mut view = requested_native_preview();
    view.key(key(KeyCode::Esc));
    assert!(view.owns_input());
    view.observe(Update::Preview(native_preview(false)))
        .unwrap();
    assert!(!view.owns_input());
}
