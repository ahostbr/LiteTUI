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
    tools: Option<crate::tool_mode::Facts>,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Update(UpdateKind);
#[derive(Clone, Debug, PartialEq)]
enum UpdateKind {
    Ready(ReadyFacts),
    Usage { used: u64, maximum: Option<u64> },
    TurnStarted { reset_rate: bool },
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
    ready: Option<ReadyFacts>,
    usage: Option<(u64, Option<u64>)>,
    rate: Option<f64>,
}
impl State {
    pub fn apply(&mut self, update: Update) {
        match update.0 {
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
    pub fn tool_description(&self) -> Option<String> {
        self.ready
            .as_ref()?
            .tools
            .as_ref()
            .map(|facts| facts.description())
    }
    pub fn footer_fields(&self) -> Vec<F> {
        let Some(ready) = &self.ready else {
            return Vec::new();
        };
        let mut fields = Vec::new();
        // Source default order; dynamic preference/visibility transport is still
        // pending. Missing facts do not become fabricated zeroes or authority.
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
                Some(ceiling) => format!("ctx — / {} max", count(ceiling)),
                None => "ctx —".into(),
            },
        };
        fields.push(F::new(K::Context, context));
        if let Some(conversation) = &ready.conversation {
            fields.push(F::new(
                K::Conversation,
                label(&conversation.chars().take(8).collect::<String>()),
            ));
        }
        fields.push(F::new(K::Model, label(&ready.model)));
        if let Some(thinking) = &ready.thinking {
            fields.push(F::new(K::Thinking, format!("think:{}", label(thinking))));
        }
        if let Some(rate) = self.rate {
            fields.push(F::new(K::Throughput, format!("{rate:.1} tok/s")));
        }
        if let Some(tools) = &ready.tools {
            fields.push(F::new(
                K::Authority,
                format!(
                    "tools:{} · {}",
                    if tools.enabled { "on" } else { "off" },
                    tools.profile.as_str()
                ),
            ));
        }
        // No seat registration, plan, cache or background facts
        // are available in these events. Never infer them from launch arguments.
        fields
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
