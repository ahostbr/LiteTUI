//! Read-only facts reported by the real legacy runtime, not requested settings.
//! A ready context length is only a ceiling. Only usage.max_context_tokens is
//! a confirmed denominator (app.py::_rpc_emit_usage withholds it unless loaded).
use crate::{
    footer::{FieldKey as K, FooterField as F},
    live::terminal_text,
};
use serde_json::Value;
use std::io;

#[derive(Clone, Debug, PartialEq)]
struct ReadyFacts {
    model: String,
    conversation: Option<String>,
    thinking: Option<String>,
    context_ceiling: Option<u64>,
    /// ready.context_loaded: ctx_max is the loaded window, not a ceiling ("max").
    context_loaded: bool,
    tools: Option<crate::tool_mode::Facts>,
    /// ready.seat_name: the registered seat the status row names (absent when unregistered).
    seat: Option<String>,
    /// ready.authority_glyphs: each profile's footer glyph, from tool_policy.stops_you.
    glyphs: std::collections::BTreeMap<String, String>,
    /// ready.version: LiteTUI's version, right-aligned on the second footer row.
    version: Option<String>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Update(UpdateKind);
#[derive(Clone, Debug, PartialEq)]
enum UpdateKind {
    Ready(ReadyFacts),
    Choices {
        model: String,
        thinking: Option<String>,
    },
    Startup(crate::management::Snapshot),
    Held(usize),
    Usage {
        used: u64,
        maximum: Option<u64>,
    },
    TurnStarted {
        reset_rate: bool,
    },
    SettledRate(Option<f64>),
}
fn optional_text(event: &Value, key: &str, limit: usize) -> io::Result<Option<String>> {
    match event.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(value)) if value.len() <= limit => {
            Ok((!value.is_empty()).then(|| value.clone()))
        }
        _ => Err(io::Error::other(format!(
            "Invalid or oversized runtime {key}"
        ))),
    }
}
fn optional_count(event: &Value, key: &str) -> io::Result<Option<u64>> {
    match event.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value.as_u64().map(Some).ok_or_else(|| {
            io::Error::other(format!("Runtime {key} must be a nonnegative integer"))
        }),
    }
}
impl Update {
    pub fn from_queue(snapshot: &crate::restored_queue::Snapshot) -> Self {
        Self(UpdateKind::Held(snapshot.entries.len()))
    }
    pub fn from_choices(menu: &crate::model_controls::Choices) -> Self {
        Self(UpdateKind::Choices {
            model: menu.model.clone(),
            thinking: (menu.kind == crate::model_controls::Kind::Thinking)
                .then(|| menu.current.clone()),
        })
    }
    pub fn from_management(snapshot: crate::management::Snapshot) -> Self {
        Self(UpdateKind::Startup(snapshot))
    }
    /// Extract validated facts; callers check protocol identity/lifecycle separately.
    /// Opaque provider usage keys are not reinterpreted as context/output tokens.
    pub fn from_event(event: &Value) -> io::Result<Option<Self>> {
        Ok(match event.get("type").and_then(Value::as_str) {
            Some("ready") => Some(UpdateKind::Ready(ReadyFacts {
                tools: crate::tool_mode::Facts::from_ready(event)?,
                model: optional_text(event, "model", 1024)?
                    .ok_or_else(|| io::Error::other("Ready model missing"))?,
                conversation: optional_text(event, "conversation_id", 128)?,
                thinking: optional_text(event, "thinking_level", 64)?,
                context_ceiling: optional_count(event, "context_length")?.filter(|n| *n > 0),
                context_loaded: event
                    .get("context_loaded")
                    .and_then(Value::as_bool)
                    .unwrap_or(false),
                seat: optional_text(event, "seat_name", 256)?,
                version: optional_text(event, "version", 64)?,
                glyphs: match event.get("authority_glyphs") {
                    None | Some(Value::Null) => Default::default(),
                    Some(Value::Object(map)) => map
                        .iter()
                        .map(|(k, v)| match v.as_str() {
                            Some(g) if k.len() <= 64 && g.chars().count() <= 4 => {
                                Ok((k.clone(), g.to_owned()))
                            }
                            _ => Err(io::Error::other("Invalid runtime authority glyph")),
                        })
                        .collect::<io::Result<_>>()?,
                    Some(_) => return Err(io::Error::other("Invalid runtime authority glyphs")),
                },
            })),
            Some("usage") => {
                let usage = event
                    .get("usage")
                    .filter(|value| value.is_object())
                    .ok_or_else(|| io::Error::other("Runtime usage needs an object"))?;
                let used = optional_count(usage, "context_tokens")?
                    .ok_or_else(|| io::Error::other("Runtime context_tokens missing"))?;
                let maximum = optional_count(usage, "max_context_tokens")?;
                if maximum == Some(0) {
                    return Err(io::Error::other(
                        "Runtime context denominator must be positive",
                    ));
                }
                Some(UpdateKind::Usage { used, maximum })
            }
            Some("turn_start") => Some(UpdateKind::TurnStarted {
                // Claude clears app.tps at entry (claude_turn.py); the
                // generic turn explicitly retains its last rate (app.py).
                reset_rate: event.get("provider").and_then(Value::as_str) == Some("claude"),
            }),
            Some("turn_end") => {
                let rate = match event.get("tps") {
                    None | Some(Value::Null) => None,
                    Some(value) => Some(
                        value
                            .as_f64()
                            .filter(|rate| rate.is_finite() && *rate >= 0.0)
                            .ok_or_else(|| {
                                io::Error::other(
                                    "Runtime tps must be a finite nonnegative number or null",
                                )
                            })?,
                    ),
                };
                Some(UpdateKind::SettledRate(rate))
            }
            _ => None,
        }
        .map(Self))
    }
}

#[derive(Default)]
pub struct State {
    runtime_policy: Option<crate::runtime_controls::Facts>,
    startup: Option<crate::management::Snapshot>,
    ready: Option<ReadyFacts>,
    usage: Option<(u64, Option<u64>)>,
    rate: Option<f64>,
    presentation: crate::presentation::Presentation,
}
impl State {
    pub fn install_runtime_policy(&mut self, facts: crate::runtime_controls::Facts) {
        self.runtime_policy = Some(facts);
    }
    pub fn engine_changed(&mut self) {
        self.ready = None;
        self.usage = None;
        self.rate = None;
    }
    /// Called only after the source open, effective policy, state and held-input barriers.
    pub fn opened(&mut self, snapshot: crate::management::Snapshot) {
        if let Some(ready) = self.ready.as_mut() {
            ready.model = snapshot.model.clone();
            ready.conversation = Some(snapshot.session.clone());
            ready.context_ceiling = None;
            ready.thinking = None;
        }
        self.presentation = (*snapshot.presentation).clone();
        self.startup = Some(snapshot);
        self.usage = None;
        self.rate = None;
    }
    pub fn presentation(&self) -> &crate::presentation::Presentation {
        &self.presentation
    }
    /// Adopt verified interface preferences from the F10 editor.
    pub fn set_presentation(&mut self, presentation: crate::presentation::Presentation) {
        self.presentation = presentation;
    }
    /// The last settled generation rate, for the turn stop line.
    pub fn rate(&self) -> Option<f64> {
        self.rate
    }

    pub fn apply(&mut self, update: Update) {
        match update.0 {
            UpdateKind::Choices { model, thinking } => {
                if let Some(ready) = self.ready.as_mut() {
                    if ready.model != model {
                        ready.model = model;
                        ready.context_ceiling = None;
                        ready.thinking = None;
                        self.usage = None;
                        self.rate = None;
                    }
                    if let Some(level) = thinking {
                        ready.thinking = Some(level);
                    }
                }
            }
            UpdateKind::Startup(snapshot) => {
                self.presentation = (*snapshot.presentation).clone();
                self.startup = Some(snapshot);
            }
            UpdateKind::Held(count) => {
                if let Some(snapshot) = &mut self.startup {
                    snapshot.held_restored_input = Some(count);
                }
            }
            UpdateKind::Ready(ready) => {
                self.ready = Some(ready);
                self.usage = None;
                self.rate = None;
            }
            UpdateKind::Usage { used, maximum } => self.usage = Some((used, maximum)),
            UpdateKind::TurnStarted { reset_rate } => {
                if reset_rate {
                    self.rate = None;
                }
            }
            UpdateKind::SettledRate(rate) => self.rate = rate,
        }
    }
    /// Initial read-only facts, never a substitute for current turn settlement.
    pub fn schedules_paused(&mut self) {
        if let Some(snapshot) = self.startup.as_mut() {
            snapshot.schedules_paused = true;
        }
    }
    pub fn startup(&self) -> Option<&crate::management::Snapshot> {
        self.startup.as_ref()
    }
    pub fn tool_description(&self) -> Option<String> {
        if let Some(policy) = self.runtime_policy.as_ref() {
            return Some(format!(
                "Tools {} · {} · approvals: {}",
                if policy.tools_enabled { "on" } else { "off" },
                policy.effective_profile.as_str(),
                policy.approval_route
            ));
        }
        self.ready
            .as_ref()?
            .tools
            .as_ref()
            .map(|facts| facts.description())
    }
    /// The source's version for the footer, once ready reported it.
    pub fn version(&self) -> Option<&str> {
        self.ready.as_ref().and_then(|r| r.version.as_deref())
    }
    pub fn footer_fields(&self) -> Vec<F> {
        let Some(ready) = &self.ready else {
            return Vec::new();
        };
        let mut fields = Vec::new();
        // Facts are collected in source default order, then the effective device switches and
        // footer order arrange them. Missing facts do not become fabricated zeroes or authority.
        if let Some((used, Some(maximum))) = self.usage {
            fields.push(F::new(
                K::Percent,
                format!("{:.0}%", (used as f64 / maximum as f64) * 100.0),
            ));
        }
        let context = match self.usage {
            Some((used, Some(maximum))) => format!("ctx {} / {}", count(used), count(maximum)),
            Some((used, None)) => format!("ctx {} / ?", count(used)),
            None => match ready.context_ceiling {
                // Python prints "max" only when the number is a ceiling, not the loaded window.
                Some(ceiling) if ready.context_loaded => format!("ctx — / {}", count(ceiling)),
                Some(ceiling) => format!("ctx — / {} max", count(ceiling)),
                None => "ctx —".into(),
            },
        };
        fields.push(F::new(K::Context, context));
        if let Some(seat) = &ready.seat {
            fields.push(F::new(K::Seat, label(seat)));
        }
        if let Some(conversation) = &ready.conversation {
            fields.push(F::new(
                K::Conversation,
                label(&conversation.chars().take(8).collect::<String>()),
            ));
        }
        if let Some(held) = self.startup.as_ref().and_then(|s| s.held_restored_input)
            && held > 0
        {
            fields.push(F::new(K::RestoredInput, format!("held:{held}")));
        }
        fields.push(F::new(K::Model, label(&ready.model)));
        if let Some(thinking) = &ready.thinking {
            fields.push(F::new(K::Thinking, format!("think:{}", label(thinking))));
        }
        if let Some(rate) = self.rate {
            fields.push(F::new(K::Throughput, format!("{rate:.1} tok/s")));
        }
        // Python's authority field: "<glyph> <profile> on" (textfmt.profile_text); tools on/off
        // is the title bar's, not the footer's.
        let authority = |profile: &str| match ready.glyphs.get(profile) {
            Some(glyph) => format!("{glyph} {profile} on"),
            None => profile.to_owned(),
        };
        if let Some(policy) = &self.runtime_policy {
            fields.push(F::new(
                K::Authority,
                authority(policy.effective_profile.as_str()),
            ));
            fields.push(F::new(
                K::Plan,
                if policy.plan_mode {
                    "plan:on"
                } else {
                    "plan:off"
                },
            ));
        } else if let Some(tools) = &ready.tools {
            fields.push(F::new(K::Authority, authority(tools.profile.as_str())));
        }
        // No seat registration, plan, cache or background facts
        // are available in these events. Never infer them from launch arguments.
        self.presentation.arrange(fields)
    }
}
fn label(value: &str) -> String {
    terminal_text(value)
        .replace('\n', "\\n")
        .replace('\t', "\\t")
}
fn count(value: u64) -> String {
    let digits = value.to_string();
    let mut result = String::with_capacity(digits.len() + 6);
    for (index, ch) in digits.chars().enumerate() {
        if index > 0 && (digits.len() - index).is_multiple_of(3) {
            result.push(',');
        }
        result.push(ch);
    }
    result
}
