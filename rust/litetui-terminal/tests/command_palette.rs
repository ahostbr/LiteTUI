use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use litetui_terminal::{
    command_palette::{self as commands, Action, Catalog, Intent, Route, Update, View},
    demo::Demo,
    live::Observation,
    management::{Management, Step},
    prepared_skill::{Draft, Prepared, digest},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::{Value, json};
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
fn data() -> Value {
    let entries=[("command","Settings","settings",json!(["/settings","/config"]),json!(0),json!(0)),("command","Unsafe plugin","",json!(["/unsafe"]),json!(1),json!(1)),("skill","ExactSkill","skill_prepare",json!(["/ExactSkill"]),Value::Null,json!(2)),("skill","settings","skill_prepare",json!(["/settings"]),Value::Null,Value::Null)].into_iter().enumerate().map(|(i,(kind,title,action,tokens,palette,completion))|json!({"id":format!("{:064x}",i+1),"kind":kind,"owner":if kind=="skill"{"skills"}else{"plugin"},"title":title,"help":"Source description","group":"app","group_label":"Application","order":100,"tokens":tokens,"palette":!palette.is_null(),"palette_order":palette,"completion_order":completion,"skill_order":if kind=="skill"{json!(i-2)}else{Value::Null},"native_action":if action.is_empty(){Value::Null}else{json!(action)},"accepts_arguments":kind=="skill","unavailable_reason":if action.is_empty(){"No native adapter"}else{""},"source":if kind=="skill"{json!("test")}else{Value::Null}})).collect::<Vec<_>>();
    json!({"version":1,"kind":"command_catalog","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"model","snapshot":"a".repeat(64),"entries":entries})
}
fn catalog() -> Catalog {
    Catalog::parse(&data(), SESSION, AGENT, "custom", "model").unwrap()
}
fn prepared_data(c: &Catalog, args: &str) -> Value {
    let body = "# Skill: ExactSkill\n---\r\nname: ExactSkill\r\n---\r\n  α body\t\r\n\n";
    let prompt = if args.is_empty() {
        body.into()
    } else {
        format!("{body}\n\n---\n\n{args}")
    };
    json!({"version":1,"kind":"skill_prompt","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"model","snapshot":c.snapshot,"row_id":c.entries[2].id,"arguments":args,"skill":{"name":"ExactSkill","source":"test","path":"C:/skills/exact/SKILL.md"},"file":{"sha256":"b".repeat(64),"bytes":45,"mtime_ns":1234,"device":0,"inode":55,"resolved_path":"C:/skills/exact/SKILL.md"},"body":body,"prompt_sha256":digest(&prompt),"prompt":prompt})
}
fn prepared(args: &str) -> Prepared {
    let c = catalog();
    Prepared::parse(&prepared_data(&c, args), &c, &c.entries[2].id, args).unwrap()
}
fn route_data(c: &Catalog, raw: Option<&str>) -> Value {
    let mut v = json!({"version":1,"kind":"command_route","session_id":SESSION,"agent_id":AGENT,"backend":"custom","model":"model","snapshot":c.snapshot,"row_id":c.entries[0].id,"native_action":"settings","arguments":""});
    if let Some(raw) = raw {
        v["command"] = json!(raw);
    }
    v
}
fn key(k: KeyCode) -> KeyEvent {
    KeyEvent::new(k, KeyModifiers::NONE)
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
fn view(skills: bool) -> View {
    let mut v = View::default();
    v.open(skills);
    v.take_intent();
    v.observe(Update::Catalog {
        catalog: catalog(),
        skills,
    });
    v
}
#[test]
fn catalog_bounds_foreign_duplicates_aliases_and_source_ranks_are_enforced() {
    for fault in [
        "session",
        "agent",
        "backend",
        "model",
        "token",
        "duplicate",
        "alias",
        "rank",
        "gap",
        "order",
        "action",
        "args",
        "reason",
        "rows",
        "bytes",
    ] {
        let mut v = data();
        match fault {
            "session" => v["session_id"] = json!(AGENT),
            "agent" => v["agent_id"] = json!(SESSION),
            "backend" => v["backend"] = json!("claude"),
            "model" => v["model"] = json!("other"),
            "token" => v["snapshot"] = json!("x".repeat(64)),
            "duplicate" => v["entries"][1]["id"] = v["entries"][0]["id"].clone(),
            "alias" => v["entries"][1]["tokens"] = json!(["/config"]),
            "rank" => v["entries"][1]["palette_order"] = json!(0),
            "gap" => v["entries"][1]["completion_order"] = json!(4),
            "order" => v["entries"][0]["order"] = json!(true),
            "action" => v["entries"][0]["native_action"] = json!("eval"),
            "args" => v["entries"][0]["accepts_arguments"] = json!(true),
            "reason" => v["entries"][0]["unavailable_reason"] = json!("locked"),
            "rows" => v["entries"] = json!(vec![v["entries"][0].clone(); 1025]),
            "bytes" => v["extra"] = json!("x".repeat(512 * 1024)),
            _ => {}
        }
        assert!(
            Catalog::parse(&v, SESSION, AGENT, "custom", "model").is_err(),
            "{fault}"
        );
    }
    let c = catalog();
    assert_eq!(c.entries.len(), 4);
    assert_eq!(c.completions().len(), 3);
    assert_eq!(c.entries[3].completion_order, None);
}
#[test]
fn route_validates_exact_raw_echo_or_selected_row_without_inventing_aliases() {
    let c = catalog();
    let raw = "  /CONFIG   ";
    let v = route_data(&c, Some(raw));
    assert_eq!(
        Route::parse(&v, &c, None, Some(raw), "").unwrap().action,
        Action::Settings
    );
    for fault in [
        "command",
        "row_id",
        "native_action",
        "arguments",
        "agent_id",
    ] {
        let mut v = v.clone();
        v[fault] = json!("wrong");
        assert!(Route::parse(&v, &c, None, Some(raw), "").is_err());
    }
    assert!(Route::parse(&v, &c, Some(&c.entries[0].id), None, "").is_err());
}
#[test]
fn prepared_bytes_frontmatter_crlf_unicode_and_trailing_arguments_are_lossless() {
    let args = "  argument α\r\nsecond\n\n  ";
    let p = prepared(args);
    assert_eq!(p.arguments, args);
    assert!(p.body.contains("---\r\nname: ExactSkill\r\n---"));
    assert!(p.prompt.ends_with(args));
    assert_eq!(digest(&p.prompt), p.digest);
    let mut d = Draft::default();
    d.set(args).unwrap();
    d.key(KeyCode::Home);
    d.insert("prefix\n").unwrap();
    assert_eq!(d.text(), format!("prefix\n{args}"));
    let before = d.text().to_owned();
    assert!(d.insert(&"x".repeat(65536)).is_err());
    assert_eq!(d.text(), before);
}
#[test]
fn forged_prepared_payload_or_provenance_cannot_stage() {
    let c = catalog();
    for fault in [
        "identity",
        "arguments",
        "prompt",
        "digest",
        "row",
        "name",
        "source",
        "metadata",
        "bytes",
    ] {
        let mut v = prepared_data(&c, "arg ");
        match fault {
            "identity" => v["session_id"] = json!(AGENT),
            "arguments" => v["arguments"] = json!("arg"),
            "prompt" => v["prompt"] = json!("truncated"),
            "digest" => v["prompt_sha256"] = json!("c".repeat(64)),
            "row" => v["row_id"] = c.entries[3].id.clone().into(),
            "name" => v["skill"]["name"] = json!("settings"),
            "source" => v["skill"]["source"] = json!("other"),
            "metadata" => v["file"]["inode"] = json!(-1),
            "bytes" => v["file"]["bytes"] = json!(65537),
            _ => {}
        }
        assert!(
            Prepared::parse(&v, &c, &c.entries[2].id, "arg ").is_err(),
            "{fault}"
        );
    }
}
#[test]
fn unavailable_entries_are_visible_and_cannot_resolve() {
    let mut v = view(false);
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Down));
    assert!(draw(&mut v, 80, 28).contains("No native adapter"));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Home));
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Resolve { .. })));
}
#[test]
fn skill_selection_arguments_and_full_preview_require_fresh_explicit_send() {
    let mut v = view(true);
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::End));
    v.key(key(KeyCode::Home)); // exact source order may place collision first
    v.paste("ExactSkill");
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    draw(&mut v, 80, 28);
    v.paste("arg\r\n  ");
    assert_eq!(v.arguments(), "arg\r\n  ");
    v.key(key(KeyCode::Enter));
    assert!(
        matches!(v.take_intent(),Some(Intent::Prepare{arguments,..}) if arguments=="arg\r\n  ")
    );
    v.observe(Update::Prepared {
        catalog: Box::new(catalog()),
        prepared: Box::new(prepared("arg\r\n  ")),
    });
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    draw(&mut v, 42, 18);
    for _ in 0..50 {
        v.key(key(KeyCode::PageDown));
    }
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    for _ in 0..50 {
        draw(&mut v, 42, 18);
        v.key(key(KeyCode::PageDown));
    }
    draw(&mut v, 42, 18);
    let mut repeat = key(KeyCode::Enter);
    repeat.kind = KeyEventKind::Repeat;
    v.key(repeat);
    assert!(v.take_intent().is_none());
    draw(&mut v, 42, 18);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Submit { .. })));
}
#[test]
fn dirty_arguments_require_prepare_and_stale_refresh_preserves_exact_draft() {
    let mut v = view(true);
    v.observe(Update::Prepared {
        catalog: Box::new(catalog()),
        prepared: Box::new(prepared("original\n")),
    });
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Tab));
    draw(&mut v, 80, 28);
    v.paste("new\r\n  ");
    let expected = v.arguments().to_owned();
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(), Some(Intent::Prepare { .. })));
    v.observe(Update::Rejected("stale".into()));
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::F(5)));
    assert_eq!(v.take_intent(), Some(Intent::Read { skills: true }));
    v.observe(Update::Catalog {
        catalog: catalog(),
        skills: true,
    });
    assert_eq!(v.arguments(), expected);
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    assert!(matches!(v.take_intent(),Some(Intent::Prepare{arguments,..}) if arguments==expected));
}
#[test]
fn catalog_pointer_pairs_and_small_geometry_never_activate() {
    let mut v = view(false);
    draw(&mut v, 80, 28);
    let mouse = |kind| MouseEvent {
        kind,
        column: 3,
        row: 4,
        modifiers: KeyModifiers::NONE,
    };
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left)));
    v.mouse(mouse(MouseEventKind::Down(MouseButton::Left)));
    draw(&mut v, 42, 18);
    v.mouse(mouse(MouseEventKind::Up(MouseButton::Left)));
    assert!(v.take_intent().is_none());
    assert!(draw(&mut v, 36, 14).contains("42×18"));
    v.key(key(KeyCode::Enter));
    assert!(v.take_intent().is_none());
    v.key(key(KeyCode::Esc));
    assert!(!v.owns_input());
}
fn state() -> Value {
    json!({"session_id":SESSION,"busy":false,"mode":"normal","tool_profile":"strict","model":{"backend":"custom","model":"model"},"ownership":{"data_version":1,"writable":true,"agent_id":AGENT},"restored_input":{"policy":"hold","held":0},"schedules_paused":false,"conversations":[],"jobs":[],"tasks":[],"active_work":[],"pending_approvals":[],"pending_questions":[],"pending_model_loads":[],"tool_policy":{"driver":"checks expected policy"}})
}
fn reply(m: &mut Management, c: &Value, v: Value) -> std::io::Result<Step> {
    m.accept(json!({"type":"response","id":c["id"],"ok":true,"result":v}))
}
fn write(s: Step) -> Value {
    let Step::Write(c) = s else { panic!("{s:?}") };
    c
}
fn connected() -> Management {
    let mut m = Management::default();
    let h=m.begin(&json!({"management_protocols":[1],"capabilities":["hello","state"],"conversation_id":SESSION,"backend":"custom","model":"model","tool_profile":"strict"})).unwrap();
    let c=write(reply(&mut m,&h,json!({"protocol_version":1,"data_version":1,"capabilities":["hello","state"],"operations":["gui.hello","gui.state",commands::LIST,commands::RESOLVE,commands::PREPARE]})).unwrap());
    reply(&mut m, &c, state()).unwrap();
    m
}
fn prepare(m: &mut Management) -> Prepared {
    let read = m.request_command(Intent::Read { skills: true }).unwrap();
    reply(m, &read, data()).unwrap();
    let c = catalog();
    let command = m
        .request_command(Intent::Prepare {
            snapshot: c.snapshot.clone(),
            row: c.entries[2].id.clone(),
            arguments: "arg\r\n  ".into(),
        })
        .unwrap();
    let state_read = write(reply(m, &command, prepared_data(&c, "arg\r\n  ")).unwrap());
    assert_eq!(state_read["type"], "gui.state");
    assert_eq!(state_read["include_conversations"], false);
    let Step::Commands {
        update: Update::Prepared { prepared, .. },
        policy: Some(_),
    } = reply(m, &state_read, state()).unwrap()
    else {
        panic!()
    };
    *prepared
}
#[test]
fn reducer_reprepares_same_payload_and_checks_policy_before_explicit_submission() {
    let mut m = connected();
    let p = prepare(&mut m);
    let command = m
        .request_command(Intent::Submit {
            digest: p.digest.clone(),
        })
        .unwrap();
    assert_eq!(command["type"], commands::PREPARE);
    let state_read =
        write(reply(&mut m, &command, prepared_data(&catalog(), &p.arguments)).unwrap());
    assert!(!m.ready());
    assert!(matches!(
        reply(&mut m, &state_read, state()).unwrap(),
        Step::Commands {
            update: Update::Verified(_),
            policy: Some(_)
        }
    ));
    assert!(m.ready());
}
#[test]
fn reducer_changed_skill_or_display_budget_refuses_without_replaying() {
    let mut m = connected();
    let p = prepare(&mut m);
    let command = m
        .request_command(Intent::Submit {
            digest: p.digest.clone(),
        })
        .unwrap();
    let mut changed = prepared_data(&catalog(), &p.arguments);
    changed["file"]["mtime_ns"] = json!(9999);
    assert!(matches!(
        reply(&mut m, &command, changed).unwrap(),
        Step::Commands {
            update: Update::Rejected(_),
            ..
        }
    ));
    assert!(m.ready());
    let c = catalog();
    let command = m
        .request_command(Intent::Prepare {
            snapshot: c.snapshot.clone(),
            row: c.entries[2].id.clone(),
            arguments: "".into(),
        })
        .unwrap();
    let mut large = prepared_data(&c, "");
    large["body"] = json!("\u{202e}".repeat(7000));
    large["prompt"] = large["body"].clone();
    large["prompt_sha256"] = json!(digest(large["prompt"].as_str().unwrap()));
    assert!(matches!(
        reply(&mut m, &command, large).unwrap(),
        Step::Commands {
            update: Update::Rejected(_),
            ..
        }
    ));
    assert!(m.ready());
}
#[test]
fn raw_slash_draft_cannot_fall_back_to_provider_and_verified_skill_keeps_composer() {
    let mut app = Demo::live("custom".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("/UNKNOWN exact arguments");
    app.submit();
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    assert_eq!(app.composer.text(), "/UNKNOWN exact arguments");
    assert_eq!(
        app.live.as_mut().unwrap().commands.take_intent(),
        Some(Intent::Raw("/UNKNOWN exact arguments".into()))
    );
    app.observe_live(Observation::Commands(Update::Rejected("unknown".into())))
        .unwrap();
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    let p = prepared("trailing\n  ");
    app.observe_live(Observation::Commands(Update::Prepared {
        catalog: Box::new(catalog()),
        prepared: Box::new(p.clone()),
    }))
    .unwrap();
    for _ in 0..50 {
        draw(&mut app.live.as_mut().unwrap().commands, 80, 28);
        app.live
            .as_mut()
            .unwrap()
            .commands
            .key(key(KeyCode::PageDown));
    }
    draw(&mut app.live.as_mut().unwrap().commands, 80, 28);
    app.live.as_mut().unwrap().commands.key(key(KeyCode::Enter));
    app.live.as_mut().unwrap().commands.take_intent();
    app.observe_live(Observation::Commands(Update::Verified(Box::new(p.clone()))))
        .unwrap();
    assert_eq!(app.live.as_mut().unwrap().take_submission(), Some(p.prompt));
    assert_eq!(app.composer.text(), "/UNKNOWN exact arguments");
}

#[test]
fn cancelled_route_or_submit_readback_settles_without_action() {
    let mut v = view(false);
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Enter));
    v.take_intent();
    v.key(key(KeyCode::Esc));
    v.observe(Update::Route {
        catalog: catalog(),
        route: Route {
            row: catalog().entries[0].id.clone(),
            action: Action::Quit,
            arguments: String::new(),
        },
    });
    assert!(v.take_route().is_none());
    assert!(!v.owns_input());
    let mut app = Demo::live("custom".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("original draft");
    app.open_commands();
    app.live.as_mut().unwrap().commands.take_intent();
    let p = prepared("arg");
    app.observe_live(Observation::Commands(Update::Prepared {
        catalog: Box::new(catalog()),
        prepared: Box::new(p.clone()),
    }))
    .unwrap();
    for _ in 0..50 {
        draw(&mut app.live.as_mut().unwrap().commands, 80, 28);
        app.live
            .as_mut()
            .unwrap()
            .commands
            .key(key(KeyCode::PageDown));
    }
    draw(&mut app.live.as_mut().unwrap().commands, 80, 28);
    app.live.as_mut().unwrap().commands.key(key(KeyCode::Enter));
    app.live.as_mut().unwrap().commands.take_intent();
    app.live.as_mut().unwrap().commands.key(key(KeyCode::Esc));
    app.observe_live(Observation::Commands(Update::Verified(Box::new(p))))
        .unwrap();
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
    assert!(!app.has_live_commands());
    assert_eq!(app.composer.text(), "original draft");
}

#[test]
fn staging_preflights_remaining_history_budget_without_replacing_draft() {
    let mut app = Demo::live("custom".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    // Fill the retained metadata budget with individually renderable rows.
    // Paging permits another row after one large prior prompt; the lifetime
    // retention ceiling must still protect command staging and the main draft.
    let prompt = "x".repeat(litetui_terminal::live_chat::DISPLAY_UNITS - 320);
    for _ in
        0..litetui_terminal::live_chat::RETAINED_UNITS / litetui_terminal::live_chat::DISPLAY_UNITS
    {
        app.live.as_mut().unwrap().submit(&prompt).unwrap();
        app.live.as_mut().unwrap().take_submission();
        app.observe_live(Observation::Complete).unwrap();
    }
    app.insert_text("original main draft");
    app.open_commands();
    app.live.as_mut().unwrap().commands.take_intent();
    let p = prepared(&"y".repeat(1000));
    app.observe_live(Observation::Commands(Update::Prepared {
        catalog: Box::new(catalog()),
        prepared: Box::new(p),
    }))
    .unwrap();
    assert!(app.live.as_ref().unwrap().commands.prepared.is_none());
    assert!(app.live.as_ref().unwrap().commands.notice.contains("limit"));
    assert_eq!(app.composer.text(), "original main draft");
    assert!(app.live.as_mut().unwrap().take_submission().is_none());
}
#[test]
fn bounded_completion_retains_all_rows_and_source_skill_rank_including_shadowed() {
    let c = catalog();
    assert_eq!(c.entries[3].skill_order, Some(1));
    let mut picker = litetui_terminal::completion::CompletionPicker::default();
    let candidates = (0..1024)
        .map(|i| litetui_terminal::completion::Completion {
            name: format!("command-{i}"),
            description: String::new(),
            kind: litetui_terminal::completion::CompletionKind::Command,
        })
        .collect::<Vec<_>>();
    picker.refilter(&candidates, "/");
    assert_eq!(picker.matches().len(), 1024);
    picker.move_by(1023);
    assert_eq!(picker.current().unwrap().name, "command-1023");
}
#[test]
fn all_themes_and_minimum_geometry_preserve_preview_and_escape_metadata() {
    for theme in litetui_terminal::theme::builtin_themes() {
        let mut c = catalog();
        c.entries[0].title = "Title\u{202e}\nforged".into();
        let mut v = View::default();
        v.open(false);
        v.take_intent();
        v.observe(Update::Catalog {
            catalog: c,
            skills: false,
        });
        let mut terminal = Terminal::new(TestBackend::new(42, 18)).unwrap();
        terminal.draw(|f| v.draw(f, f.area(), &theme)).unwrap();
        let screen: String = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect();
        assert!(screen.contains("\\u{202e}\\nforged"));
        assert!(!screen.contains('\u{202e}'));
        assert_eq!(terminal.backend().buffer()[(2, 2)].bg, theme.background);
    }
}

#[test]
fn shifted_printable_search_and_arguments_are_exact_but_chords_do_not_type() {
    let mut v = view(true);
    draw(&mut v, 80, 28);
    v.key(KeyEvent::new(KeyCode::Char('E'), KeyModifiers::SHIFT));
    assert!(draw(&mut v, 80, 28).contains("Search: E"));
    v.key(key(KeyCode::Enter));
    draw(&mut v, 80, 28);
    for c in ['A', ':', 'Δ'] {
        v.key(KeyEvent::new(KeyCode::Char(c), KeyModifiers::SHIFT));
    }
    v.key(KeyEvent::new(KeyCode::Char('x'), KeyModifiers::CONTROL));
    v.key(KeyEvent::new(KeyCode::Char('y'), KeyModifiers::ALT));
    assert_eq!(v.arguments(), "A:Δ");
}

#[test]
fn raw_route_echo_budget_allows_a_full_composer_of_astral_text() {
    let c = catalog();
    let args = "😀".repeat(16372);
    let raw = format!("/ExactSkill {args}");
    let mut v = route_data(&c, Some(&raw));
    v["row_id"] = json!(c.entries[2].id);
    v["native_action"] = json!("skill_prepare");
    v["arguments"] = json!(args);
    assert!(serde_json::to_vec(&v).unwrap().len() > 128 * 1024);
    assert_eq!(
        Route::parse(&v, &c, None, Some(&raw), "").unwrap().action,
        Action::SkillPrepare
    );
}

#[test]
fn source_session_and_theme_routes_open_review_views_without_mutation_or_draft_loss() {
    for (name, action, arguments) in [
        ("themes", Action::Themes, ""),
        ("conversation_new", Action::ConversationNew, ""),
        (
            "conversation_rename",
            Action::ConversationRename,
            "  Exact 雪 title  ",
        ),
    ] {
        let mut d = data();
        d["entries"][0]["native_action"] = json!(name);
        d["entries"][0]["accepts_arguments"] = json!(action == Action::ConversationRename);
        let c = Catalog::parse(&d, SESSION, AGENT, "custom", "model").unwrap();
        let mut rv = route_data(&c, Some("/source resolved exact command"));
        rv["native_action"] = json!(name);
        rv["arguments"] = json!(arguments);
        let route =
            Route::parse(&rv, &c, None, Some("/source resolved exact command"), "").unwrap();
        let mut app = Demo::live("custom".into(), "model".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("PRESERVE COMPOSER");
        app.live
            .as_mut()
            .unwrap()
            .commands
            .raw("/source resolved exact command");
        app.live.as_mut().unwrap().commands.take_intent();
        app.observe_live(Observation::Commands(Update::Route { catalog: c, route }))
            .unwrap();
        assert_eq!(app.composer.text(), "PRESERVE COMPOSER");
        assert!(app.live.as_ref().unwrap().rows.is_empty());
        if action == Action::Themes {
            assert_eq!(
                app.live.as_mut().unwrap().themes.take_intent(),
                Some(litetui_terminal::theme_controls::Intent::Read)
            );
        } else {
            use litetui_terminal::{conversation_actions as a, conversation_browser as b};
            let expected = if action == Action::ConversationNew {
                a::Action::New
            } else {
                a::Action::Rename
            };
            assert_eq!(
                app.live.as_mut().unwrap().browser.take_intent(),
                Some(b::Intent::Action(a::Intent::Read { action: expected }))
            );
            if expected == a::Action::Rename {
                assert_eq!(
                    app.live.as_ref().unwrap().browser.actions.draft(),
                    arguments
                );
            }
        }
    }
}
#[test]
fn rename_route_bound_and_command_specific_argument_policy_remain_strict() {
    let mut d = data();
    d["entries"][0]["native_action"] = json!("conversation_rename");
    d["entries"][0]["accepts_arguments"] = json!(true);
    let c = Catalog::parse(&d, SESSION, AGENT, "custom", "model").unwrap();
    for argument in ["a".repeat(4097), "bad\0name".into()] {
        let mut v = route_data(&c, Some("/rename exact"));
        v["native_action"] = json!("conversation_rename");
        v["arguments"] = json!(argument);
        assert!(Route::parse(&v, &c, None, Some("/rename exact"), "").is_err());
    }
    d["entries"][0]["native_action"] = json!("themes");
    assert!(Catalog::parse(&d, SESSION, AGENT, "custom", "model").is_err());
}

#[test]
fn glassbox_registered_routes_prefill_review_only_and_preserve_draft() {
    use litetui_terminal::{glassbox_controls::Request, settings_view};
    for (arguments, expected) in [
        ("", Request::Snapshot),
        ("status", Request::Snapshot),
        ("start", Request::Review("start".into())),
        (" ON ", Request::Review("start".into())),
        ("stop", Request::Review("stop".into())),
        ("off", Request::Review("stop".into())),
    ] {
        let mut d = data();
        d["entries"][0]["native_action"] = json!("glassbox");
        d["entries"][0]["accepts_arguments"] = json!(true);
        d["entries"][0]["tokens"] = json!(["glassbox", "/glassbox"]);
        let c = Catalog::parse(&d, SESSION, AGENT, "custom", "model").unwrap();
        let raw = format!("/glassbox {arguments}");
        let mut rv = route_data(&c, Some(&raw));
        rv["native_action"] = json!("glassbox");
        rv["arguments"] = json!(arguments);
        let route = Route::parse(&rv, &c, None, Some(&raw), "").unwrap();
        assert_eq!(route.action, Action::Glassbox);
        let mut app = Demo::live("custom".into(), "model".into());
        app.live.as_mut().unwrap().ready = true;
        app.insert_text("Exact draft 雪 ");
        app.composer
            .move_cursor(litetui_terminal::composer::Motion::WordLeft, true);
        let caret = (
            app.composer.before_cursor().to_owned(),
            app.composer.selected_text().to_owned(),
        );
        app.live.as_mut().unwrap().commands.raw(&raw);
        app.live.as_mut().unwrap().commands.take_intent();
        app.observe_live(Observation::Commands(Update::Route {
            catalog: c.clone(),
            route,
        }))
        .unwrap();
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(settings_view::Intent::Glassbox(Box::new(expected)))
        );
        app.live.as_mut().unwrap().settings.key(key(KeyCode::Enter));
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            None,
            "no hidden apply before review result"
        );
        assert_eq!(app.composer.text(), "Exact draft 雪 ");
        assert_eq!(
            caret,
            (
                app.composer.before_cursor().to_owned(),
                app.composer.selected_text().to_owned()
            )
        );
        for invalid in ["reload", "start now", "--port 9999", "status stop"] {
            rv["arguments"] = json!(invalid);
            assert!(Route::parse(&rv, &c, None, Some(&raw), "").is_err());
        }
    }
}

#[test]
fn control_routes_keep_exact_arguments_and_reject_unrepresentable_intent() {
    for (action, accepts, arguments, bad) in [
        (
            "conversation_compact",
            true,
            "Keep  guidance 雪\n trailing  ",
            "bad\0hint",
        ),
        ("model_load", true, "different-model", "bad\0model"),
        ("model_unload", true, "different-model", "bad\0model"),
        ("model_reconnect", false, "", "extra"),
        ("engine", true, "STOP", "start 4"),
        ("plan", true, "off", "maybe"),
        ("plugin_reload", true, "exact_tool", "two tools"),
        ("hooks", false, "", "extra"),
        ("work_new_job", false, "", "every minute"),
    ] {
        let mut data = data();
        data["entries"][0]["native_action"] = json!(action);
        data["entries"][0]["accepts_arguments"] = json!(accepts);
        let c = Catalog::parse(&data, SESSION, AGENT, "custom", "model").unwrap();
        let mut wire = route_data(&c, None);
        wire["native_action"] = json!(action);
        wire["arguments"] = json!(arguments);
        let route = Route::parse(&wire, &c, Some(&c.entries[0].id), None, arguments).unwrap();
        assert_eq!(route.arguments, arguments);
        wire["arguments"] = json!(bad);
        assert!(
            Route::parse(&wire, &c, Some(&c.entries[0].id), None, bad).is_err(),
            "{action}"
        );
    }
}

fn routed(action: &str, accepts: bool, backend: &str) -> Catalog {
    let mut d = data();
    d["backend"] = json!(backend);
    d["entries"][0]["native_action"] = json!(action);
    d["entries"][0]["accepts_arguments"] = json!(accepts);
    Catalog::parse(&d, SESSION, AGENT, backend, "model").unwrap()
}
fn route_of(c: &Catalog, action: &str, backend: &str, arguments: &str) -> std::io::Result<Route> {
    let mut v = route_data(c, Some("/c"));
    v["backend"] = json!(backend);
    v["native_action"] = json!(action);
    v["arguments"] = json!(arguments);
    Route::parse(&v, c, None, Some("/c"), "")
}
/// A live frontend that has just resolved `/c <arguments>` into `action`.
fn routed_app(action: &str, accepts: bool, backend: &str, arguments: &str) -> Demo {
    let c = routed(action, accepts, backend);
    let route = route_of(&c, action, backend, arguments).unwrap();
    let mut app = Demo::live(backend.into(), "model".into());
    app.live.as_mut().unwrap().ready = true;
    app.insert_text("PRESERVE COMPOSER");
    let commands = &mut app.live.as_mut().unwrap().commands;
    commands.raw("/c");
    commands.take_intent();
    app.observe_live(Observation::Commands(Update::Route { catalog: c, route }))
        .unwrap();
    assert_eq!(app.composer.text(), "PRESERVE COMPOSER");
    app
}
fn paint(app: &mut Demo, w: u16, h: u16) -> String {
    let mut t = Terminal::new(TestBackend::new(w, h)).unwrap();
    t.draw(|f| litetui_terminal::ui::draw(f, app)).unwrap();
    t.backend()
        .buffer()
        .content
        .chunks(w as usize)
        .map(|r| r.iter().map(|c| c.symbol()).collect::<String>())
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn argument_routes_accept_only_shapes_a_reviewed_control_can_preselect() {
    let long = "a".repeat(37);
    for (action, backend, good, bad) in [
        (
            "models",
            "custom",
            vec!["", "2", "model-B", " model-B "],
            vec!["0", "bad\0name"],
        ),
        (
            "thinking",
            "custom",
            vec!["high", "UNSET", ""],
            vec!["very high", "bad\nlevel"],
        ),
        (
            "backend",
            "custom",
            vec!["codex", ""],
            vec!["--default", "codex lmstudio"],
        ),
        (
            "conversation_resume",
            "custom",
            vec!["", "0000a", "abc-def0", "00000000-"],
            vec!["12", "ABC", "xyz", long.as_str()],
        ),
        (
            "claude",
            "claude",
            vec!["", "STATUS", "resolve", " continue ", "new"],
            vec!["replay", "resolve now"],
        ),
        (
            "mcp",
            "custom",
            vec!["", "list", "LS", "status"],
            vec!["add x", "reload", "reconnect x"],
        ),
    ] {
        let c = routed(action, true, backend);
        for argument in good {
            assert!(
                route_of(&c, action, backend, argument).is_ok(),
                "{action} {argument:?}"
            );
        }
        for argument in bad {
            assert!(
                route_of(&c, action, backend, argument).is_err(),
                "{action} {argument:?}"
            );
        }
    }
    // A Claude session command is never offered on another backend.
    let c = routed("claude", true, "custom");
    assert!(route_of(&c, "claude", "custom", "").is_err());
    // Commands whose source ignores arguments still refuse them natively.
    for action in [
        "tool_list",
        "model_reconnect",
        "settings",
        "help",
        "keys",
        "clear_screen",
    ] {
        let c = routed(action, false, "custom");
        assert!(route_of(&c, action, "custom", "").is_ok(), "{action}");
        assert!(route_of(&c, action, "custom", "extra").is_err(), "{action}");
        let mut d = data();
        d["entries"][0]["native_action"] = json!(action);
        d["entries"][0]["accepts_arguments"] = json!(true);
        assert!(
            Catalog::parse(&d, SESSION, AGENT, "custom", "model").is_err(),
            "{action}"
        );
    }
}

fn choices(
    kind: litetui_terminal::model_controls::Kind,
) -> litetui_terminal::model_controls::Choices {
    use litetui_terminal::model_controls::{Choices, Kind};
    let value = if kind == Kind::Model {
        json!({"backend":"custom","model":"model-A","models":[
            {"slug":"model-A","name":"Model A","current":true,"loaded":null},
            {"slug":"model-B","name":"Model B","current":false,"loaded":false}]})
    } else {
        json!({"backend":"custom","model":"model-A","thinking":{"level":null,
            "levels":["default","off","low","high"],"source":"backend metadata","note":"n"}})
    };
    Choices::parse(kind, &value, 3, "session").unwrap()
}
fn pending_select(app: &mut Demo) -> Option<String> {
    use litetui_terminal::model_controls::Intent;
    paint(app, 100, 30);
    let controls = &mut app.live.as_mut().unwrap().controls;
    assert_eq!(
        controls.take_intent(),
        None,
        "an argument must not apply by itself"
    );
    controls.key(key(KeyCode::Enter));
    match controls.take_intent() {
        Some(Intent::Select { value, .. }) => Some(value),
        other => {
            assert_eq!(other, None);
            None
        }
    }
}

#[test]
fn model_and_thinking_arguments_move_the_cursor_and_wait_for_explicit_enter() {
    use litetui_terminal::model_controls::{Intent, Kind, Update as Controls};
    for (action, kind, argument, expected, notice) in [
        (
            "models",
            Kind::Model,
            "model-B",
            "model-B",
            "selected Model B",
        ),
        ("models", Kind::Model, "2", "model-B", "selected Model B"),
        ("models", Kind::Model, "9", "model-A", "not offered"),
        ("models", Kind::Model, "missing", "model-A", "not offered"),
        ("thinking", Kind::Thinking, "HIGH", "high", "selected high"),
        (
            "thinking",
            Kind::Thinking,
            "unset",
            "default",
            "selected default",
        ),
        (
            "thinking",
            Kind::Thinking,
            "xhigh",
            "default",
            "not offered",
        ),
    ] {
        let mut app = routed_app(action, true, "custom", argument);
        let controls = &mut app.live.as_mut().unwrap().controls;
        assert_eq!(controls.take_intent(), Some(Intent::Open(kind)));
        app.observe_live(Observation::Controls(Controls::Choices(choices(kind))))
            .unwrap();
        assert!(
            app.live.as_ref().unwrap().controls.notice.contains(notice),
            "{argument}: {}",
            app.live.as_ref().unwrap().controls.notice
        );
        assert_eq!(
            pending_select(&mut app).as_deref(),
            Some(expected),
            "{argument}"
        );
        assert_eq!(app.composer.text(), "PRESERVE COMPOSER");
    }
}

#[test]
fn backend_and_resume_arguments_preselect_rows_without_switching_or_opening() {
    use litetui_terminal::{backend_controls, conversation_browser as browser};
    let mut app = routed_app("backend", true, "custom", "CODEX");
    assert_eq!(
        app.live.as_mut().unwrap().backend_controls.take_intent(),
        Some(backend_controls::Intent::Read)
    );
    let catalog = backend_controls::Catalog::parse(
        &json!({"version":1,"kind":"support_metadata","readiness_checked":false,"session_id":SESSION,
            "agent_id":AGENT,"agent_name":"Owned agent","snapshot":"a".repeat(64),
            "selected_backend":"custom","configured_backend":"custom","selected_model":"old",
            "codex_engine":{"requested_native":true,"effective":null},
            "entries":[{"name":"custom","label":"Custom","selected":true,"owned_engine_controls":false},
                {"name":"codex","label":"Codex","selected":false,"owned_engine_controls":false}]}),
        7,
        SESSION,
        AGENT,
    )
    .unwrap();
    app.observe_live(Observation::Backend(backend_controls::Update::Catalog(
        catalog,
    )))
    .unwrap();
    let backend = &mut app.live.as_mut().unwrap().backend_controls;
    assert!(
        backend.notice.contains("selected codex"),
        "{}",
        backend.notice
    );
    assert_eq!(backend.take_intent(), None);
    let shown = paint(&mut app, 100, 30);
    assert!(shown.contains(">  Codex [codex]"), "{shown}");
    assert!(
        shown.contains("● Custom [custom]"),
        "current backend stays marked"
    );
    let backend = &mut app.live.as_mut().unwrap().backend_controls;
    backend.key(key(KeyCode::Enter));
    assert_eq!(
        backend.take_intent(),
        None,
        "Enter only starts typing the model"
    );

    let page = |offset: usize| {
        let entries: Vec<Value> = (0..3)
            .map(|i| {
                let session = if i == 0 {
                    SESSION.to_owned()
                } else {
                    format!("00000000-0000-4000-8000-{:012x}", i + 1)
                };
                json!({"session_id":session,"agent_id":AGENT,"agent_name":"Owned agent",
                    "title":format!("Saved {i}"),"message_count":2,"updated_at":1.0,
                    "current":session == SESSION,"can_open":session != SESSION,
                    "reason":if session == SESSION {"Already open"} else {""}})
            })
            .collect();
        browser::Page::parse(
            &json!({"session_id":SESSION,"agent_id":AGENT,"snapshot":"a".repeat(64),"offset":offset,
                "total":3,"entries":entries,"previous_cursor":null,"next_cursor":null}),
            1,
            SESSION,
        )
        .unwrap()
    };
    for (argument, notice) in [
        (
            "00000000-0000-4000-8000-000000000003",
            "selected 00000000-0000-4000-8000-000000000003",
        ),
        ("00000000-", "matches 2 rows"),
        ("ffff", "is not on this first page"),
        (SESSION, "current conversation"),
    ] {
        let mut app = routed_app("conversation_resume", true, "custom", argument);
        assert_eq!(
            app.live.as_mut().unwrap().browser.take_intent(),
            Some(browser::Intent::Refresh)
        );
        app.observe_live(Observation::Browser(browser::Update::Page(page(0))))
            .unwrap();
        let view = &mut app.live.as_mut().unwrap().browser;
        assert!(view.notice.contains(notice), "{argument}: {}", view.notice);
        assert!(
            view.notice.contains("Nothing has been opened"),
            "{}",
            view.notice
        );
        paint(&mut app, 100, 30);
        let view = &mut app.live.as_mut().unwrap().browser;
        view.key(key(KeyCode::Enter));
        assert_eq!(
            view.take_intent(),
            None,
            "{argument}: Enter without O must not open"
        );
    }
}

#[test]
fn claude_routes_open_only_existing_reviewed_controls() {
    use litetui_terminal::{claude_reset, restored_queue, settings_view};
    for verb in ["resolve", "CONTINUE"] {
        let mut app = routed_app("claude", true, "claude", verb);
        assert!(app.has_live_queue(), "{verb}");
        assert_eq!(
            app.live.as_mut().unwrap().queue.take_intent(),
            Some(restored_queue::Intent::Open)
        );
        assert!(app.notice.contains("changed nothing"), "{}", app.notice);
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
    }
    let mut app = routed_app("claude", true, "claude", "new");
    assert_eq!(
        app.live.as_mut().unwrap().settings.take_intent(),
        Some(settings_view::Intent::ClaudeReset(Box::new(
            claude_reset::Request::Review
        )))
    );
    assert_eq!(app.live.as_mut().unwrap().browser.take_intent(), None);
    assert_eq!(app.composer.text(), "PRESERVE COMPOSER");
    let chat = app.live.as_mut().unwrap();
    assert_eq!(chat.backend, "claude");
    assert_eq!(chat.model, "model");
    assert!(chat.history.is_none());
    assert!(chat.rows.is_empty());
    assert!(chat.take_submission().is_none());
    let visible = paint(&mut app, 100, 30);
    assert!(
        visible.contains("Reading the exact Claude session"),
        "{visible}"
    );
    for verb in ["", "status"] {
        let mut app = routed_app("claude", true, "claude", verb);
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(settings_view::Intent::RuntimeStatus)
        );
    }
}

#[test]
fn mcp_routes_reuse_the_settings_panel_and_list_is_inspection_only() {
    use litetui_terminal::{mcp_config, mcp_lifecycle, settings_view};
    for (verb, read_only) in [("", false), ("list", true), ("LS", true), ("status", true)] {
        let mut app = routed_app("mcp", true, "custom", verb);
        assert_eq!(
            app.live.as_mut().unwrap().settings.take_intent(),
            Some(settings_view::Intent::Mcp(Box::new(
                mcp_config::Request::List
            )))
        );
        app.observe_live(Observation::Settings(settings_view::Update::Mcp(Err(
            "fixture".into(),
        ))))
        .unwrap();
        let settings = &mut app.live.as_mut().unwrap().settings;
        settings.key(key(KeyCode::Tab));
        let switched = settings.take_intent();
        if read_only {
            assert_eq!(
                switched, None,
                "{verb}: list must not reach the runtime tab"
            );
            settings.key(key(KeyCode::Char('e')));
            assert_eq!(settings.take_intent(), None);
            settings.key(key(KeyCode::Esc));
        } else {
            assert_eq!(
                switched,
                Some(settings_view::Intent::McpLifecycle(Box::new(
                    mcp_lifecycle::Request::List
                )))
            );
        }
    }
}

#[test]
fn help_and_keys_overlay_list_the_live_catalog_and_native_keys_read_only() {
    for (action, first) in [
        ("help", "Commands (live source registry)"),
        ("keys", "Keys (this frontend)"),
    ] {
        let mut app = routed_app(action, false, "custom", "");
        assert!(app.help.open && app.has_live_input(), "{action}");
        let head = paint(&mut app, 110, 40);
        assert!(head.contains(first), "{action}");
        app.help.key(key(KeyCode::End));
        let tail = paint(&mut app, 110, 40);
        app.help.key(key(KeyCode::Home));
        let (commands, keys) = if action == "help" {
            (&head, &tail)
        } else {
            (&tail, &head)
        };
        for text in [
            "/settings, /config",
            "Source description",
            "/unsafe",
            "Not available in this frontend: No native adapter",
        ] {
            assert!(commands.contains(text), "{action} {text}");
        }
        for text in [
            "Ctrl+Q: quit and close the owned runtime",
            "F11: command palette and skills",
        ] {
            assert!(keys.contains(text) || action == "help", "{action} {text}");
        }
        assert!(
            tail.contains("Ctrl+Shift+A") || action == "keys",
            "{action}"
        );
        // Dark theme: the overlay paints the theme background, never terminal default.
        let mut t = Terminal::new(TestBackend::new(110, 40)).unwrap();
        t.draw(|f| litetui_terminal::ui::draw(f, &mut app)).unwrap();
        assert_eq!(t.backend().buffer()[(6, 6)].bg, app.palette().background);
        // Inert: scrolling keys change nothing outside the overlay; Esc closes it.
        for code in [
            KeyCode::Down,
            KeyCode::PageDown,
            KeyCode::End,
            KeyCode::Enter,
            KeyCode::Char('x'),
        ] {
            app.help.key(key(code));
        }
        assert!(app.help.open);
        assert!(app.live.as_mut().unwrap().commands.take_intent().is_none());
        assert!(app.live.as_mut().unwrap().take_submission().is_none());
        app.help.key(key(KeyCode::Esc));
        assert!(!app.help.open && !app.has_live_input());
        assert_eq!(app.composer.text(), "PRESERVE COMPOSER");
    }
}

#[test]
fn clear_screen_hides_the_transcript_display_only_and_new_work_still_shows() {
    let mut app = Demo::live("custom".into(), "model".into());
    app.observe_live(Observation::Ready).unwrap();
    app.live.as_mut().unwrap().submit("FIRST-PROMPT").unwrap();
    app.live.as_mut().unwrap().take_submission();
    app.observe_live(Observation::Text("FIRST-ANSWER".into()))
        .unwrap();
    // A turn that has not settled is never hidden under the user's feet.
    let c = routed("clear_screen", false, "custom");
    let route = |app: &mut Demo| {
        let route = route_of(&c, "clear_screen", "custom", "").unwrap();
        app.live.as_mut().unwrap().commands.raw("/c");
        app.live.as_mut().unwrap().commands.take_intent();
        app.insert_text("DRAFT");
        app.observe_live(Observation::Commands(Update::Route {
            catalog: c.clone(),
            route,
        }))
        .unwrap();
    };
    route(&mut app);
    assert_eq!(app.live.as_ref().unwrap().screen_cleared_rows, 0);
    assert!(app.notice.contains("Nothing was cleared"), "{}", app.notice);
    assert!(paint(&mut app, 100, 30).contains("FIRST-PROMPT"));
    app.observe_live(Observation::Complete).unwrap();
    assert!(paint(&mut app, 100, 30).contains("FIRST-ANSWER"));
    let rows = app.live.as_ref().unwrap().rows.len();
    route(&mut app);
    let chat = app.live.as_ref().unwrap();
    assert_eq!((chat.screen_cleared_rows, chat.rows.len()), (rows, rows));
    assert!(
        app.notice.contains("The conversation is unchanged"),
        "{}",
        app.notice
    );
    assert_eq!(app.composer.text(), "DRAFTDRAFT");
    let shown = paint(&mut app, 100, 30);
    assert!(!shown.contains("FIRST-PROMPT") && !shown.contains("FIRST-ANSWER"));
    app.live.as_mut().unwrap().submit("SECOND-PROMPT").unwrap();
    assert!(paint(&mut app, 100, 30).contains("SECOND-PROMPT"));
    assert!(!paint(&mut app, 100, 30).contains("FIRST-PROMPT"));
}

#[test]
fn unavailable_commands_show_their_own_reason_in_completion_and_palette() {
    let mut d = data();
    d["entries"][1]["unavailable_reason"] = json!(
        "Screenshot is a Textual window action; the terminal frontend has no window to capture."
    );
    let c = Catalog::parse(&d, SESSION, AGENT, "custom", "model").unwrap();
    assert!(
        c.completions()
            .iter()
            .any(|r| r.description.contains("Textual window action"))
    );
    let mut v = View::default();
    v.open(false);
    v.take_intent();
    v.observe(Update::Catalog {
        catalog: c,
        skills: false,
    });
    draw(&mut v, 80, 28);
    v.key(key(KeyCode::Down));
    assert!(draw(&mut v, 80, 28).contains("Textual window action"));
}
