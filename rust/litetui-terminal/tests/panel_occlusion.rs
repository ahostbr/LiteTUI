//! Actual Work NewTurn observations preempt panels opened while idle. No processes or host I/O.
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use litetui_terminal::{
    approvals, backend_controls as b, command_palette as c, conversation_browser as v,
    demo::Demo,
    engine_controls as e,
    live::{Observation, TerminalOutcome, WorkExecution},
    model_controls as m,
    prepared_skill::Prepared,
    questions,
    tool_mode::Profile,
    ui,
    work_lifecycle::{Admission, Delivery, Input, Mode, Origin, Payload, Provider},
};
use ratatui::{Terminal, backend::TestBackend};
use serde_json::json;
const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
#[derive(Clone, Copy, Debug)]
enum Panel {
    Engine,
    Commands,
    Backend,
    Browser,
    Models,
}
const PANELS: [Panel; 5] = [
    Panel::Engine,
    Panel::Commands,
    Panel::Backend,
    Panel::Browser,
    Panel::Models,
];
impl Panel {
    fn open(self, app: &mut Demo) {
        match self {
            Self::Engine => app.open_engine(),
            Self::Commands => app.open_commands(),
            Self::Backend => app.open_backend(),
            Self::Browser => app.open_browser(),
            Self::Models => app.open_models(m::Kind::Model),
        }
        assert!(self.owns(app));
        assert!(
            self.take(app),
            "public open must create a real pending read"
        );
        // Reach Review/Prepare through their normal UI sequence before Work starts.
        match self {
            Self::Engine => {
                app.observe_live(Observation::Engine(e::Update::Snapshot(Box::new(
                    engine_snapshot(),
                ))))
                .unwrap();
                draw(app, false);
                self.key(app, KeyCode::Down);
                draw(app, false);
                self.key(app, KeyCode::Enter);
                assert!(matches!(
                    app.live.as_mut().unwrap().engine.take_intent(),
                    Some(e::Intent::Validate {
                        action: e::Action::Stop,
                        ..
                    })
                ));
            }
            Self::Commands => {
                app.observe_live(Observation::Commands(c::Update::Catalog {
                    catalog: catalog(),
                    skills: false,
                }))
                .unwrap();
                draw(app, false);
                self.key(app, KeyCode::Enter);
                draw(app, false);
                app.live.as_mut().unwrap().commands.paste("kept arguments");
                self.key(app, KeyCode::Enter);
                assert!(matches!(
                    app.live.as_mut().unwrap().commands.take_intent(),
                    Some(c::Intent::Prepare { .. })
                ));
            }
            _ => {}
        }
    }
    fn owns(self, app: &Demo) -> bool {
        match self {
            Self::Engine => app.has_live_engine(),
            Self::Commands => app.has_live_commands(),
            Self::Backend => app.has_live_backend(),
            Self::Browser => app.has_live_browser(),
            Self::Models => app.has_live_controls(),
        }
    }
    fn take(self, app: &mut Demo) -> bool {
        let chat = app.live.as_mut().unwrap();
        match self {
            Self::Engine => chat.engine.take_intent().is_some(),
            Self::Commands => chat.commands.take_intent().is_some(),
            Self::Backend => chat.backend_controls.take_intent().is_some(),
            Self::Browser => chat.browser.take_intent().is_some(),
            Self::Models => chat.controls.take_intent().is_some(),
        }
    }
    fn key(self, app: &mut Demo, code: KeyCode) {
        let key = KeyEvent::new(code, KeyModifiers::NONE);
        let chat = app.live.as_mut().unwrap();
        match self {
            Self::Engine => chat.engine.key(key),
            Self::Commands => chat.commands.key(key),
            Self::Backend => chat.backend_controls.key(key),
            Self::Browser => chat.browser.key(key),
            Self::Models => chat.controls.key(key),
        }
    }
    fn title(self) -> &'static str {
        match self {
            Self::Engine => "Owned engine",
            Self::Commands => "Prepared skill",
            Self::Backend => "Select the engine for this conversation",
            Self::Browser => "Conversations",
            Self::Models => "Select a model",
        }
    }
    fn late_reply(self, app: &mut Demo) {
        let observation = match self {
            Self::Models => Observation::Controls(m::Update::Choices(m::Choices {
                revision: 1,
                kind: m::Kind::Model,
                session: SESSION.into(),
                backend: "strata".into(),
                model: "fixture".into(),
                current: "fixture".into(),
                rows: vec![
                    m::Choice {
                        value: "fixture".into(),
                        label: "Fixture".into(),
                    },
                    m::Choice {
                        value: "other".into(),
                        label: "Other".into(),
                    },
                ],
                note: "Source choices".into(),
                message: None,
                context: None,
            })),
            Self::Backend => Observation::Backend(b::Update::Catalog(b::Catalog {
                revision: 1,
                session: SESSION.into(),
                agent: AGENT.into(),
                agent_name: "Fixture".into(),
                snapshot: "a".repeat(64),
                selected: "strata".into(),
                configured: "strata".into(),
                model: "fixture".into(),
                requested_native: false,
                effective_engine: None,
                entries: vec![
                    b::Entry {
                        name: "strata".into(),
                        label: "Strata".into(),
                        selected: true,
                        owned_engine_controls: true,
                        mark: None,
                    },
                    b::Entry {
                        name: "custom".into(),
                        label: "Custom".into(),
                        selected: false,
                        owned_engine_controls: false,
                        mark: None,
                    },
                ],
            })),
            Self::Browser => Observation::Browser(v::Update::Page(v::Page {
                revision: 1,
                session: SESSION.into(),
                agent: AGENT.into(),
                snapshot: "a".repeat(64),
                offset: 0,
                total: 1,
                entries: vec![v::Entry {
                    session: "32345678-1234-4123-8123-123456789abc".into(),
                    agent: AGENT.into(),
                    agent_name: "Fixture".into(),
                    title: "Other conversation".into(),
                    message_count: 1,
                    updated_at: 1234.0,
                    current: false,
                    can_open: true,
                    reason: String::new(),
                }],
                previous: None,
                next: None,
            })),
            Self::Commands => {
                let catalog = catalog();
                let prepared = Prepared {
                    session: SESSION.into(),
                    agent: AGENT.into(),
                    backend: "strata".into(),
                    model: "fixture".into(),
                    snapshot: catalog.snapshot.clone(),
                    row: "b".repeat(64),
                    arguments: "kept arguments".into(),
                    body: "Controlled source skill".into(),
                    prompt: "Controlled source skill\nkept arguments".into(),
                    digest: "c".repeat(64),
                    name: "Fixture".into(),
                    source: "fixture".into(),
                    path: "C:/fixture/SKILL.md".into(),
                    file: json!({}),
                };
                Observation::Commands(c::Update::Prepared {
                    catalog: Box::new(catalog),
                    prepared: Box::new(prepared),
                })
            }
            Self::Engine => {
                let snapshot = engine_snapshot();
                let summary = snapshot.ownership.clone();
                Observation::Engine(e::Update::Review(Box::new(e::Review {
                    snapshot,
                    action: e::Action::Stop,
                    token: "d".repeat(64),
                    action_id: "42345678-1234-4123-8123-123456789abc".into(),
                    timeout_ms: 120000,
                    summary,
                })))
            }
        };
        app.observe_live(observation).unwrap();
    }
}
fn app() -> Demo {
    let mut app = Demo::live("strata".into(), "fixture".into());
    app.observe_live(Observation::Ready).unwrap();
    app.insert_text("unsent selected draft");
    app.composer.select_all();
    app
}
fn admitted() -> Observation {
    Observation::WorkExecution(Box::new(WorkExecution::Admitted {
        input: Input {
            id: "input-1".into(),
            producer: "producer-1".into(),
            payload_sha256: "a".repeat(64),
            source: "scheduled".into(),
            requested: Profile::Strict,
            session: SESSION.into(),
            revision: 1,
        },
        admission: Admission {
            id: "admission-1".into(),
            input: "input-1".into(),
            mode: Mode::NewTurn,
            operation: "scheduled-operation".into(),
            origin: Origin {
                session: SESSION.into(),
                operation: "scheduled-operation".into(),
                hook: None,
                source: "scheduled".into(),
                execution: "execution-1".into(),
            },
            parent: None,
            boundary: 0,
            requested: Profile::Strict,
            effective: Profile::Strict,
            seat: Profile::Strict,
            provider: Provider::Host,
            delivery: None,
        },
        payload: Payload {
            delivery: Delivery::Append,
            content: "scheduled source prompt".into(),
            text: Some("scheduled source prompt".into()),
            source: "scheduled".into(),
            profile: Profile::Strict,
        },
        suspended_submission: None,
    }))
}
fn overlay(app: &mut Demo, which: &str) {
    match which {
        "approval" => {
            let event=approvals::Ledger::default().request(&json!({"type":"tool_approval_requested","id":"approval","tool":"write","input":{"path":"fixture"},"profile":"strict","why":"writes","timeout_s":300.0})).unwrap();
            app.observe_live(Observation::Approval(event)).unwrap();
        }
        "question" => {
            let raw = serde_json::from_str(include_str!("fixtures/question_request.json")).unwrap();
            let event = questions::Ledger::default().request(&raw).unwrap();
            app.observe_live(Observation::Question {
                event,
                settled: None,
            })
            .unwrap();
        }
        "stop" => {
            app.open_stop();
            assert!(app.stop_open());
        }
        "cancel" => {
            app.live.as_mut().unwrap().request_stop().unwrap();
            assert!(app.live.as_ref().unwrap().cancelling());
        }
        "tiny" => {}
        _ => unreachable!(),
    }
}
fn draw(app: &mut Demo, tiny: bool) -> String {
    let mut terminal = Terminal::new(TestBackend::new(if tiny { 20 } else { 140 }, 60)).unwrap();
    terminal.draw(|f| ui::draw(f, app)).unwrap();
    terminal
        .backend()
        .buffer()
        .content
        .iter()
        .map(|c| c.symbol())
        .collect()
}
fn settled(app: &mut Demo, which: &str) {
    match which {
        "approval" => app
            .observe_live(Observation::Approval(approvals::Event::Closed {
                id: "approval".into(),
                outcome: Some("denied".into()),
                reason: "answered".into(),
            }))
            .unwrap(),
        "question" => app
            .observe_live(Observation::Question {
                event: questions::Event::Resolved {
                    id: "ask-000000000000".into(),
                    cancelled: true,
                },
                settled: None,
            })
            .unwrap(),
        _ => {}
    }
    for update in [
        WorkExecution::Ended {
            operation: "scheduled-operation".into(),
            outcome: TerminalOutcome::Cancelled("fixture complete".into()),
        },
        WorkExecution::Retired {
            admission_id: "admission-1".into(),
        },
        WorkExecution::Settled {
            operation: "scheduled-operation".into(),
            outcome: TerminalOutcome::Cancelled("fixture complete".into()),
        },
    ] {
        app.observe_live(Observation::WorkExecution(Box::new(update)))
            .unwrap();
    }
    // Equivalent to dismissing the non-mutating Keep-going dialog after settlement.
    app.live.as_mut().unwrap().stop_confirm = false;
}
#[test]
fn current_conversation_work_preempts_every_idle_panel_and_retains_late_reply() {
    for panel in PANELS {
        for which in ["approval", "question", "stop", "cancel", "tiny"] {
            let mut app = app();
            panel.open(&mut app);
            app.observe_live(admitted()).unwrap();
            assert!(app.live.as_ref().unwrap().busy);
            assert!(
                panel.owns(&app),
                "admission must not silently release panel ownership"
            );
            overlay(&mut app, which);
            draw(&mut app, which == "tiny");
            panel.late_reply(&mut app);
            let screen = draw(&mut app, which == "tiny");
            assert!(
                !screen.contains(panel.title()),
                "{panel:?}/{which} covered source prompt"
            );
            if which == "approval" {
                assert!(screen.contains("Tool approval"));
            }
            if which == "question" {
                assert!(screen.contains("Which files?"));
            }
            assert!(panel.owns(&app));
            for code in [
                KeyCode::Down,
                KeyCode::Enter,
                KeyCode::Char('o'),
                KeyCode::Enter,
            ] {
                panel.key(&mut app, code);
            }
            assert!(
                !panel.take(&mut app),
                "hidden reply gained painted intent: {panel:?}/{which}"
            );
            settled(&mut app, which);
            let screen = draw(&mut app, false);
            assert!(
                screen.contains(panel.title()),
                "{panel:?}/{which} lost retained panel"
            );
            assert_eq!(app.composer.text(), "unsent selected draft");
            assert_eq!(app.composer.selected_text(), "unsent selected draft");
            assert_eq!(app.live.as_ref().unwrap().rows.len(), 1);
        }
    }
}
#[test]
fn help_is_closed_by_real_work_prompt_observation() {
    for which in ["approval", "question"] {
        let mut app = app();
        app.help.open(None, false);
        app.observe_live(admitted()).unwrap();
        overlay(&mut app, which);
        assert!(!app.help.open);
        let screen = draw(&mut app, false);
        assert!(screen.contains(if which == "approval" {
            "Tool approval"
        } else {
            "Which files?"
        }));
    }
}

fn engine_snapshot() -> e::Snapshot {
    e::Snapshot {
        identity: e::Identity {
            session: SESSION.into(),
            agent: AGENT.into(),
            backend: "strata".into(),
            model: "fixture".into(),
            instance: "a".repeat(64),
        },
        token: "b".repeat(64),
        generation: 1,
        admission: e::Admission::PromptReady,
        effective: Some("fixture".into()),
        ownership: json!({"kind":"retained_handle","handle_token":"c".repeat(64),"host":"http://127.0.0.1:45120","pid":1234}),
        launch: serde_json::Value::Null,
        policy: json!({}),
        start: false,
        stop: true,
        start_reason: "Already running".into(),
        stop_reason: String::new(),
        reconnect: None,
        local_peer_start: None,
    }
}

fn catalog() -> c::Catalog {
    c::Catalog {
        session: SESSION.into(),
        agent: AGENT.into(),
        backend: "strata".into(),
        model: "fixture".into(),
        snapshot: "a".repeat(64),
        entries: vec![c::Entry {
            id: "b".repeat(64),
            kind: "skill".into(),
            owner: "skills".into(),
            title: "Fixture".into(),
            help: "Controlled source skill".into(),
            group: "app".into(),
            group_label: "Application".into(),
            tokens: vec!["/Fixture".into()],
            palette: true,
            action: Some(c::Action::SkillPrepare),
            arguments: true,
            unavailable: String::new(),
            palette_order: Some(0),
            completion_order: Some(0),
            skill_order: Some(0),
            source: Some("fixture".into()),
        }],
    }
}
