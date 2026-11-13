//! Exact automatic startup observations for the two opt-in paid Claude tests.
//! Management validates wire identities, policy, idle state and Work activation before
//! publishing Theme -> ready facts -> startup state -> current History -> Ready.
//! Optional captured RuntimePolicy and informational notices precede that sequence.
use litetui_terminal::{
    footer::FieldKey, history, live::Observation, resume, runtime_state, theme_controls,
};
use std::io;

const EMPTY_REPLAY: &str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Phase {
    Theme,
    Facts,
    State,
    History,
    Ready,
    Active,
}

pub struct Startup {
    backend: &'static str,
    model: &'static str,
    phase: Phase,
    owner: Option<theme_controls::Identity>,
    policy_owner: Option<theme_controls::Identity>,
}

impl Startup {
    pub fn new(backend: &'static str, model: &'static str) -> Self {
        Self {
            backend,
            model,
            phase: Phase::Theme,
            owner: None,
            policy_owner: None,
        }
    }

    pub fn observe(&mut self, event: &Observation) -> io::Result<()> {
        // Neither paid run requests another Theme/History or a second startup.
        // SavedHistory and compaction have separate, correlated observation variants.
        if self.phase == Phase::Active {
            return if matches!(
                event,
                Observation::Theme(_) | Observation::History(_) | Observation::Ready
            ) {
                Err(self.unexpected(event))
            } else {
                Ok(())
            };
        }
        match event {
            Observation::Information(_) if self.phase == Phase::Theme => Ok(()),
            Observation::RuntimePolicy(policy)
                if self.phase == Phase::Theme && self.policy_owner.is_none() =>
            {
                let identity = &policy.binding.identity;
                if policy.generation != 0
                    || identity.backend != self.backend
                    || identity.model != self.model
                {
                    return Err(self.unexpected(event));
                }
                self.policy_owner = Some(theme_controls::Identity {
                    session: identity.session.clone(),
                    agent: identity.agent.clone(),
                    backend: identity.backend.clone(),
                    model: identity.model.clone(),
                });
                Ok(())
            }
            Observation::Theme(theme_controls::Update::Snapshot(snapshot))
                if self.phase == Phase::Theme
                    && snapshot.identity.backend == self.backend
                    && snapshot.identity.model == self.model
                    && self
                        .policy_owner
                        .as_ref()
                        .is_none_or(|owner| owner == &snapshot.identity) =>
            {
                resume::conversation_id(&snapshot.identity.session)?;
                resume::conversation_id(&snapshot.identity.agent)?;
                self.owner = Some(snapshot.identity.clone());
                self.phase = Phase::Facts;
                Ok(())
            }
            Observation::State(update) if matches!(self.phase, Phase::Facts | Phase::State) => {
                // Update's kind is private. Apply each independently: only ready facts
                // produce a model footer, and only Startup supplies startup(). The manager
                // already binds the opaque Ready conversation to this exact owned session.
                let mut facts = runtime_state::State::default();
                facts.apply(update.clone());
                let valid = if self.phase == Phase::Facts {
                    facts.startup().is_none()
                        && facts
                            .footer_fields()
                            .iter()
                            .any(|field| field.key == FieldKey::Model && field.text == self.model)
                } else {
                    facts.startup().is_some_and(|snapshot| {
                        snapshot.session
                            == self.owner.as_ref().expect("captured theme owner").session
                            && snapshot.backend == self.backend
                            && snapshot.model == self.model
                            && snapshot.profile == "strict"
                            && snapshot.mode == "normal"
                            && snapshot.held_restored_input == Some(0)
                            && !snapshot.schedules_paused
                            && snapshot.conversations == 0
                            && snapshot.jobs == 0
                            && snapshot.tasks == 0
                    })
                };
                if !valid {
                    return Err(self.unexpected(event));
                }
                self.phase = if self.phase == Phase::Facts {
                    Phase::State
                } else {
                    Phase::History
                };
                Ok(())
            }
            Observation::History(history)
                if self.phase == Phase::History && self.fresh_empty(history) =>
            {
                self.phase = Phase::Ready;
                Ok(())
            }
            Observation::Ready if self.phase == Phase::Ready => {
                self.phase = Phase::Active;
                Ok(())
            }
            _ => Err(self.unexpected(event)),
        }
    }

    fn fresh_empty(&self, observed: &resume::History) -> bool {
        let (Some(snapshot), Some(page), Some(owner)) = (
            observed.descriptor.as_deref(),
            observed.page.as_deref(),
            self.owner.as_ref(),
        ) else {
            return false;
        };
        let identity = &snapshot.identity;
        if identity.from_session != owner.session
            || identity.session != owner.session
            || identity.agent != owner.agent
            || identity.backend != owner.backend
            || identity.model != owner.model
            || identity.scope != history::Scope::Current
            || identity.catalog.is_some()
            || snapshot.open_effects.is_some()
            || snapshot.open_action_id.is_some()
            || snapshot.read_only
            || !snapshot.pending
            || snapshot.materialized
            || snapshot.transcript_revision.is_some()
            || !snapshot.fresh_system_context
            || snapshot.counts.non_system != 0
            || snapshot.counts.messages != snapshot.counts.system
            || snapshot.counts.blocks != 0
            || snapshot.replay_sha256 != EMPTY_REPLAY
            || page.identity != *identity
            || page.snapshot != snapshot.token
            || page.context_sha256 != snapshot.context_sha256
            || page.replay_sha256 != snapshot.replay_sha256
            || page.counts != snapshot.counts
            || !page.fragments.is_empty()
            || page.previous.is_some()
            || page.next.is_some()
            || !page.at_start
            || !page.at_end
            || page.bytes != 0
            || page.units != 0
        {
            return false;
        }
        // Full equality also checks the private legacy bytes/units counters, the exact
        // wrapper session, empty messages and the hidden system count. No system text
        // is fabricated or printed: its source context hash remains bound to the page.
        let mut expected = resume::History::projected(snapshot.clone());
        expected.page = Some(Box::new(page.clone()));
        observed == &expected
    }

    fn unexpected(&self, event: &Observation) -> io::Error {
        io::Error::other(format!(
            "Unexpected startup observation (phase={:?}): {}",
            self.phase,
            describe(event)
        ))
    }
}

/// Keep the complete variant name and bounded escaped payload in failure logs.
pub fn describe(event: &Observation) -> String {
    let rendered = format!("{event:?}");
    let mut text: String = rendered.chars().take(2048).collect();
    if rendered.chars().count() > 2048 {
        text.push_str(" [truncated]");
    }
    text
}

#[cfg(test)]
mod tests {
    use super::*;
    use litetui_terminal::{management, runtime_controls, work_lifecycle};
    use serde_json::json;
    use std::collections::BTreeMap;

    const SESSION: &str = "12345678-1234-4123-8123-123456789abc";
    const AGENT: &str = "22345678-1234-4123-8123-123456789abc";
    const OTHER: &str = "32345678-1234-4123-8123-123456789abc";

    fn theme(backend: &str, model: &str) -> Observation {
        Observation::Theme(theme_controls::Update::Snapshot(Box::new(
            theme_controls::Snapshot {
                identity: theme_controls::Identity {
                    session: SESSION.into(),
                    agent: AGENT.into(),
                    backend: backend.into(),
                    model: model.into(),
                },
                token: "a".repeat(64),
                revisions: json!({}),
                selection: theme_controls::Selection {
                    saved: String::new(),
                    saved_definition: None,
                    requested: String::new(),
                    runtime: String::new(),
                    effective: "controlled fixture".into(),
                    row: None,
                    effective_reason: Some("controlled fixture".into()),
                    definition: "b".repeat(64),
                },
                eligible: false,
                reason: Some("controlled fixture".into()),
                rows: Vec::new(),
                registered_definitions: BTreeMap::new(),
                invalid: Vec::new(),
                css: BTreeMap::new(),
            },
        )))
    }
    fn facts(model: &str) -> Observation {
        Observation::State(
            runtime_state::Update::from_event(
                &json!({"type":"ready", "model":model, "conversation_id":SESSION}),
            )
            .unwrap()
            .unwrap(),
        )
    }
    fn state() -> management::Snapshot {
        management::Snapshot {
            session: SESSION.into(),
            model: "model".into(),
            backend: "claude".into(),
            profile: "strict".into(),
            mode: "normal".into(),
            writable: false,
            held_restored_input: Some(0),
            schedules_paused: false,
            conversations: 0,
            jobs: 0,
            tasks: 0,
            presentation: Default::default(),
            title: None,
        }
    }
    fn state_event(snapshot: management::Snapshot) -> Observation {
        Observation::State(runtime_state::Update::from_management(snapshot))
    }
    fn before_history() -> Startup {
        let mut startup = Startup::new("claude", "model");
        startup
            .observe(&Observation::Information("Checking runtime state…".into()))
            .unwrap();
        startup.observe(&theme("claude", "model")).unwrap();
        startup.observe(&facts("model")).unwrap();
        startup.observe(&state_event(state())).unwrap();
        startup
    }
    fn empty_history(system: u64) -> resume::History {
        let snapshot = history::Snapshot {
            open_effects: None,
            open_action_id: None,
            read_only: false,
            identity: history::Identity {
                from_session: SESSION.into(),
                session: SESSION.into(),
                agent: AGENT.into(),
                backend: "claude".into(),
                model: "model".into(),
                scope: history::Scope::Current,
                catalog: None,
            },
            token: "a".repeat(64),
            transcript_revision: None,
            context_sha256: "b".repeat(64),
            replay_sha256: EMPTY_REPLAY.into(),
            pending: true,
            materialized: false,
            fresh_system_context: true,
            title: "(no user message)".into(),
            counts: history::Counts {
                messages: system,
                non_system: 0,
                system,
                blocks: 0,
            },
        };
        let page = history::Page {
            identity: snapshot.identity.clone(),
            snapshot: snapshot.token.clone(),
            context_sha256: snapshot.context_sha256.clone(),
            replay_sha256: snapshot.replay_sha256.clone(),
            counts: snapshot.counts.clone(),
            fragments: Vec::new(),
            previous: None,
            next: None,
            at_start: true,
            at_end: true,
            bytes: 0,
            units: 0,
        };
        let mut result = resume::History::projected(snapshot);
        result.page = Some(Box::new(page));
        result
    }
    fn policy() -> runtime_controls::Policy {
        let provider = work_lifecycle::Provider::Host;
        runtime_controls::Policy {
            binding: runtime_controls::Binding { mcp_lifecycle: false, identity: work_lifecycle::Identity { session: SESSION.into(), agent: AGENT.into(), backend: "claude".into(), model: "model".into() }, work_instance: "c".repeat(64), provider, tool_selection: false },
            generation: 0,
            facts: runtime_controls::Facts::parse(&json!({"tools_enabled":false,"settings_profile":"strict","chosen_profile":"strict","seat_profile":"strict","effective_profile":"strict","requested_cli_profile":"strict","plan_mode":false,"mcp_enabled":false,"approval_route":"host","cwd":std::env::current_dir().unwrap().to_string_lossy(),"provider":"host","inventory":"host_request"}), provider).unwrap(),
        }
    }

    #[test]
    fn startup_accepts_complete_fresh_sequence_with_hidden_system_context() {
        for system in [0, 1, 2] {
            let mut startup = before_history();
            startup
                .observe(&Observation::History(empty_history(system)))
                .unwrap();
            startup.observe(&Observation::Ready).unwrap();
            startup.observe(&Observation::Complete).unwrap();
        }
    }
    #[test]
    fn startup_captured_policy_must_precede_and_match_theme_owner() {
        let mut startup = Startup::new("claude", "model");
        startup
            .observe(&Observation::RuntimePolicy(policy()))
            .unwrap();
        assert!(
            startup
                .observe(&Observation::RuntimePolicy(policy()))
                .is_err()
        );
        startup.observe(&theme("claude", "model")).unwrap();
        startup.observe(&facts("model")).unwrap();
        startup.observe(&state_event(state())).unwrap();
        startup
            .observe(&Observation::History(empty_history(1)))
            .unwrap();
        startup.observe(&Observation::Ready).unwrap();
        let mut changed = policy();
        changed.binding.identity.agent = OTHER.into();
        let mut startup = Startup::new("claude", "model");
        startup
            .observe(&Observation::RuntimePolicy(changed))
            .unwrap();
        assert!(startup.observe(&theme("claude", "model")).is_err());
        let mut changed = policy();
        changed.generation = 1;
        assert!(
            Startup::new("claude", "model")
                .observe(&Observation::RuntimePolicy(changed))
                .is_err()
        );
        let mut changed = policy();
        changed.binding.identity.model = "other".into();
        assert!(
            Startup::new("claude", "model")
                .observe(&Observation::RuntimePolicy(changed))
                .is_err()
        );
    }
    #[test]
    fn startup_rejects_missing_duplicate_foreign_rejected_and_late_theme() {
        let mut startup = Startup::new("claude", "model");
        assert!(startup.observe(&Observation::Ready).is_err());
        assert!(startup.observe(&theme("custom", "model")).is_err());
        assert!(startup.observe(&theme("claude", "other")).is_err());
        assert!(
            startup
                .observe(&Observation::Theme(theme_controls::Update::Rejected(
                    "fixture rejection".into()
                )))
                .is_err()
        );
        startup.observe(&theme("claude", "model")).unwrap();
        assert!(startup.observe(&theme("claude", "model")).is_err());
        assert!(startup.observe(&Observation::Ready).is_err());
        let mut startup = before_history();
        startup
            .observe(&Observation::History(empty_history(1)))
            .unwrap();
        startup.observe(&Observation::Ready).unwrap();
        assert!(startup.observe(&theme("claude", "model")).is_err());
        assert!(startup.observe(&Observation::Ready).is_err());
    }
    #[test]
    fn startup_requires_both_ordered_matching_state_observations() {
        let mut startup = Startup::new("claude", "model");
        assert!(startup.observe(&facts("model")).is_err());
        startup.observe(&theme("claude", "model")).unwrap();
        assert!(startup.observe(&facts("other")).is_err());
        assert!(startup.observe(&state_event(state())).is_err());
        startup.observe(&facts("model")).unwrap();
        assert!(startup.observe(&facts("model")).is_err());
        let changes: &[fn(&mut management::Snapshot)] = &[
            |s| s.session = OTHER.into(),
            |s| s.model = "other".into(),
            |s| s.backend = "other".into(),
            |s| s.profile = "autonomous".into(),
            |s| s.mode = "plan".into(),
            |s| s.held_restored_input = None,
            |s| s.held_restored_input = Some(1),
            |s| s.schedules_paused = true,
            |s| s.conversations = 1,
            |s| s.jobs = 1,
            |s| s.tasks = 1,
        ];
        for change in changes {
            let mut s = state();
            change(&mut s);
            assert!(startup.observe(&state_event(s)).is_err());
        }
        startup.observe(&state_event(state())).unwrap();
        assert!(startup.observe(&state_event(state())).is_err());
        assert!(startup.observe(&Observation::Ready).is_err());
    }
    #[test]
    fn startup_rejects_early_duplicate_and_late_history_or_turn_observations() {
        let mut startup = Startup::new("claude", "model");
        assert!(
            startup
                .observe(&Observation::History(empty_history(1)))
                .is_err()
        );
        for event in [
            Observation::Submission {
                id: "unrequested".into(),
            },
            Observation::Complete,
            Observation::Text("unrequested".into()),
        ] {
            assert!(startup.observe(&event).is_err());
        }
        let mut startup = before_history();
        startup
            .observe(&Observation::History(empty_history(1)))
            .unwrap();
        assert!(
            startup
                .observe(&Observation::History(empty_history(1)))
                .is_err()
        );
        startup.observe(&Observation::Ready).unwrap();
        assert!(
            startup
                .observe(&Observation::History(empty_history(1)))
                .is_err()
        );
    }
    #[test]
    fn startup_rejects_changed_history_identity_and_legacy_projection() {
        let changes: &[fn(&mut resume::History)] = &[
            |h| h.session = OTHER.into(),
            |h| h.descriptor.as_mut().unwrap().identity.from_session = OTHER.into(),
            |h| h.descriptor.as_mut().unwrap().identity.session = OTHER.into(),
            |h| h.descriptor.as_mut().unwrap().identity.agent = OTHER.into(),
            |h| h.descriptor.as_mut().unwrap().identity.backend = "other".into(),
            |h| h.descriptor.as_mut().unwrap().identity.model = "other".into(),
            |h| h.descriptor.as_mut().unwrap().identity.scope = history::Scope::Preview,
            |h| h.descriptor.as_mut().unwrap().identity.catalog = Some("c".repeat(64)),
            |h| h.descriptor = None,
        ];
        for (index, change) in changes.iter().enumerate() {
            let mut h = empty_history(1);
            change(&mut h);
            assert!(
                before_history().observe(&Observation::History(h)).is_err(),
                "identity case {index}"
            );
        }
        let legacy = resume::History::parse(&json!({"session_id":SESSION,"read_only":false,"meta":{"id":SESSION},"messages":[{"role":"system","content":"fixture"}]}), SESSION).unwrap();
        assert!(
            before_history()
                .observe(&Observation::History(legacy))
                .is_err()
        );
    }
    #[test]
    fn startup_rejects_restored_visible_materialized_or_inconsistent_history() {
        let changes: &[fn(&mut resume::History)] = &[
            |h| h.descriptor.as_mut().unwrap().pending = false,
            |h| h.descriptor.as_mut().unwrap().materialized = true,
            |h| h.descriptor.as_mut().unwrap().transcript_revision = Some("c".repeat(64)),
            |h| h.descriptor.as_mut().unwrap().fresh_system_context = false,
            |h| h.descriptor.as_mut().unwrap().counts.non_system = 1,
            |h| h.descriptor.as_mut().unwrap().counts.messages = 2,
            |h| h.descriptor.as_mut().unwrap().counts.blocks = 1,
            |h| h.hidden_system_messages = 0,
            |h| {
                h.messages.push(resume::Message {
                    role: "user".into(),
                    text: "prior turn".into(),
                    details: String::new(),
                })
            },
            |h| h.descriptor.as_mut().unwrap().read_only = true,
            |h| h.descriptor.as_mut().unwrap().open_action_id = Some(OTHER.into()),
            |h| {
                h.descriptor.as_mut().unwrap().open_effects = Some(history::OpenEffects {
                    lifecycle_hooks: false,
                    provider_cleanup: false,
                })
            },
            |h| {
                h.descriptor.as_mut().unwrap().replay_sha256 = "c".repeat(64);
                h.page.as_mut().unwrap().replay_sha256 = "c".repeat(64);
            },
        ];
        for (index, change) in changes.iter().enumerate() {
            let mut h = empty_history(1);
            change(&mut h);
            assert!(
                before_history().observe(&Observation::History(h)).is_err(),
                "content case {index}"
            );
        }
    }
    #[test]
    fn startup_requires_exact_complete_empty_page_readback() {
        let changes: &[fn(&mut resume::History)] = &[
            |h| h.page = None,
            |h| h.page.as_mut().unwrap().identity.agent = OTHER.into(),
            |h| h.page.as_mut().unwrap().snapshot = "c".repeat(64),
            |h| h.page.as_mut().unwrap().context_sha256 = "c".repeat(64),
            |h| h.page.as_mut().unwrap().replay_sha256 = "c".repeat(64),
            |h| h.page.as_mut().unwrap().counts.system = 0,
            |h| h.page.as_mut().unwrap().previous = Some("cursor".into()),
            |h| h.page.as_mut().unwrap().next = Some("cursor".into()),
            |h| h.page.as_mut().unwrap().at_start = false,
            |h| h.page.as_mut().unwrap().at_end = false,
            |h| h.page.as_mut().unwrap().bytes = 1,
            |h| h.page.as_mut().unwrap().units = 1,
        ];
        for (index, change) in changes.iter().enumerate() {
            let mut h = empty_history(1);
            change(&mut h);
            assert!(
                before_history().observe(&Observation::History(h)).is_err(),
                "page case {index}"
            );
        }
    }
    #[test]
    fn unexpected_observation_diagnostics_keep_variant_and_bound_payload() {
        let event = Observation::Theme(theme_controls::Update::Rejected("x".repeat(4096)));
        let message = describe(&event);
        assert!(message.starts_with("Theme(Rejected("));
        assert!(message.ends_with(" [truncated]"));
        assert_eq!(message.chars().count(), 2048 + " [truncated]".len());
    }
}
