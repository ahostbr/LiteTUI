//! Bounded projection of explicitly attributed GENERIC stream/tool observations.
//!
//! This is presentation state, not operation success, persistence or authority.
//! Caller owns one projection per connection generation and feeds selected wire
//! notices. Other runtime producers are deliberately unsupported, not discarded.
use crate::protocol::Notice;
use serde_json::Value;

#[derive(Clone, Copy, Debug)]
pub struct Limits {
    pub rows: usize,
    /// Aggregate retained tool observations, including returned observations.
    pub tool_calls: usize,
    /// Total retained UTF-8 bytes across answer, reasoning AND tool results.
    pub text_bytes: usize,
    /// Retained identity/model/terminal/tool strings; integers cost 8 bytes each.
    pub metadata_bytes: usize,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ExecutionId(String);
impl ExecutionId {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

/// A host invocation observation, NOT a provider call, execution or task ID.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ObservationId(String);
impl ObservationId {
    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Origin {
    pub session: String,
    pub operation: Option<Value>,
    pub hook: Option<String>,
    pub source: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Terminal {
    pub reason: String,
    pub tps: Option<f64>,
    pub tps_source: Option<String>,
    pub error: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum StreamState {
    Streaming,
    /// Only an observed end record. Never evidence of successful operation.
    Ended(Terminal),
    Incomplete,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ToolObservationState {
    /// Request record observed, not evidence of authorization or dispatch.
    Requested,
    /// Literal host return flag/result, not success or background completion.
    Returned { result: String, ok: bool },
    /// No retained return before stream end or loss of certainty.
    Incomplete,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ToolObservation {
    pub id: ObservationId,
    pub round: u64,
    pub slot: u64,
    pub provider_call_id: String,
    pub name: String,
    pub state: ToolObservationState,
    pub timing: crate::presentation::ToolTiming,
}

#[derive(Clone, Debug, PartialEq)]
pub struct Row {
    pub execution: ExecutionId,
    pub origin: Origin,
    pub model: Option<String>,
    pub thinking_level: Option<String>,
    pub answer: String,
    pub reasoning: String,
    /// Request-arrival order within this execution, not a full transcript order.
    pub tools: Vec<ToolObservation>,
    pub state: StreamState,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ProjectionFault {
    InvalidLimit,
    NotNegotiated,
    UnexpectedHello,
    Malformed(&'static str),
    UnsupportedEvent,
    OriginChanged,
    MissingStart,
    DuplicateStart,
    DuplicateToolRequest,
    MissingToolRequest,
    DuplicateToolReturn,
    AfterEnd,
    Capacity,
    TransportUncertain,
    UncorrelatedLegacy,
    Disconnected,
}

pub struct Projection {
    generation: u64,
    limits: Limits,
    rows: Vec<Row>,
    tool_calls: usize,
    text_bytes: usize,
    metadata_bytes: usize,
    negotiated: bool,
    fault: Option<ProjectionFault>,
}

impl Projection {
    pub fn new(generation: u64, limits: Limits) -> Result<Self, ProjectionFault> {
        if limits.rows == 0
            || limits.tool_calls == 0
            || limits.text_bytes == 0
            || limits.metadata_bytes == 0
        {
            return Err(ProjectionFault::InvalidLimit);
        }
        Ok(Self {
            generation,
            limits,
            rows: Vec::new(),
            tool_calls: 0,
            text_bytes: 0,
            metadata_bytes: 0,
            negotiated: false,
            fault: None,
        })
    }
    pub fn rows(&self) -> &[Row] {
        &self.rows
    }
    pub fn fault(&self) -> Option<&ProjectionFault> {
        self.fault.as_ref()
    }
    pub fn generation(&self) -> u64 {
        self.generation
    }

    fn block(&mut self, reason: ProjectionFault) -> ProjectionFault {
        let reason = self.fault.get_or_insert(reason).clone();
        for row in &mut self.rows {
            if row.state == StreamState::Streaming {
                row.state = StreamState::Incomplete;
            }
            incomplete_tools(row);
        }
        reason
    }
    pub fn disconnect(&mut self) {
        self.block(ProjectionFault::Disconnected);
    }

    /// Stale generations and raw mirrors never mutate this generation's rows.
    /// A fault latches; no later hello/end silently restores certainty.
    pub fn apply(&mut self, generation: u64, notice: &Notice) -> Result<(), ProjectionFault> {
        if generation != self.generation || matches!(notice, Notice::StaleGeneration) {
            return Ok(());
        }
        if let Some(reason) = &self.fault {
            return Err(reason.clone());
        }
        let result = match notice {
            Notice::Hello(_) if !self.negotiated => {
                self.negotiated = true;
                Ok(())
            }
            Notice::Hello(_) => Err(ProjectionFault::UnexpectedHello),
            Notice::Event { frame, event } if self.negotiated => self.event(frame, event),
            Notice::Event { .. } => Err(ProjectionFault::NotNegotiated),
            Notice::LegacyEvent(_) => Err(ProjectionFault::UncorrelatedLegacy),
            Notice::Fault { .. } | Notice::Quarantined(_) | Notice::DirectError(_) => {
                Err(ProjectionFault::TransportUncertain)
            }
            Notice::IgnoredMirror(_) | Notice::Response { .. } | Notice::StaleGeneration => Ok(()),
        };
        result.map_err(|reason| self.block(reason))
    }

    fn event(&mut self, frame: &Value, event: &Value) -> Result<(), ProjectionFault> {
        if frame.get("type").and_then(Value::as_str) != Some("gui.event")
            || frame.get("event") != Some(event)
        {
            return Err(ProjectionFault::Malformed(
                "selected event/envelope mismatch",
            ));
        }
        let kind = required_text(event, "type")?;
        if !matches!(
            kind,
            "turn_start"
                | "text_delta"
                | "reasoning_delta"
                | "turn_end"
                | "generic_tool_requested"
                | "generic_tool_returned"
        ) {
            return Err(ProjectionFault::UnsupportedEvent);
        }
        let (execution, origin, origin_bytes) = origin(frame, self.limits.metadata_bytes)?;
        let index = self.rows.iter().position(|row| row.execution == execution);
        if kind == "turn_start" {
            if index.is_some() {
                return Err(ProjectionFault::DuplicateStart);
            }
            let model = nullable_text(event, "model")?;
            let thinking = nullable_text(event, "thinking_level")?;
            let bytes = origin_bytes
                .saturating_add(text_size(model))
                .saturating_add(text_size(thinking));
            if self.rows.len() >= self.limits.rows {
                return Err(ProjectionFault::Capacity);
            }
            self.check_metadata(bytes)?;
            self.rows.push(Row {
                execution,
                origin,
                model: model.map(str::to_owned),
                thinking_level: thinking.map(str::to_owned),
                answer: String::new(),
                reasoning: String::new(),
                tools: Vec::new(),
                state: StreamState::Streaming,
            });
            self.metadata_bytes += bytes;
            return Ok(());
        }
        let index = index.ok_or(ProjectionFault::MissingStart)?;
        if self.rows[index].origin != origin {
            return Err(ProjectionFault::OriginChanged);
        }
        if self.rows[index].state != StreamState::Streaming {
            return Err(ProjectionFault::AfterEnd);
        }
        match kind {
            "text_delta" | "reasoning_delta" => {
                let text = required_text(event, "text")?;
                self.check_text(text.len())?;
                let row = &mut self.rows[index];
                if kind == "text_delta" {
                    row.answer.push_str(text);
                } else {
                    row.reasoning.push_str(text);
                }
                self.text_bytes += text.len();
            }
            "generic_tool_requested" => self.tool_request(index, event)?,
            "generic_tool_returned" => self.tool_return(index, event)?,
            "turn_end" => {
                let reason = required_text(event, "stopReason")?;
                if reason.is_empty() {
                    return Err(ProjectionFault::Malformed("empty stopReason"));
                }
                let tps = match event.get("tps") {
                    Some(Value::Null) => None,
                    Some(value) => Some(
                        value
                            .as_f64()
                            .filter(|v| v.is_finite() && *v >= 0.0)
                            .ok_or(ProjectionFault::Malformed(
                                "tps must be null or a finite nonnegative number",
                            ))?,
                    ),
                    None => return Err(ProjectionFault::Malformed("missing tps")),
                };
                let tps_source = nullable_text(event, "tpsSource")?;
                let error = if event.get("error").is_some() {
                    nullable_text(event, "error")?
                } else {
                    None
                };
                let bytes = reason
                    .len()
                    .saturating_add(text_size(tps_source))
                    .saturating_add(text_size(error));
                self.check_metadata(bytes)?;
                self.rows[index].state = StreamState::Ended(Terminal {
                    reason: reason.to_owned(),
                    tps,
                    tps_source: tps_source.map(str::to_owned),
                    error: error.map(str::to_owned),
                });
                incomplete_tools(&mut self.rows[index]);
                self.metadata_bytes += bytes;
            }
            _ => unreachable!("event kinds checked above"),
        }
        Ok(())
    }

    fn tool_request(&mut self, index: usize, event: &Value) -> Result<(), ProjectionFault> {
        let id = required_uuid4(event, "observation_id")?;
        // Global within this projection generation, including returned calls.
        if self
            .rows
            .iter()
            .flat_map(|row| &row.tools)
            .any(|tool| tool.id.as_str() == id)
        {
            return Err(ProjectionFault::DuplicateToolRequest);
        }
        let round = required_u64(event, "round")?;
        let slot = required_u64(event, "slot")?;
        let provider = required_text(event, "provider_call_id")?;
        let name = required_text(event, "name")?;
        let bytes = id
            .len()
            .saturating_add(provider.len())
            .saturating_add(name.len())
            .saturating_add(16);
        if self.tool_calls >= self.limits.tool_calls {
            return Err(ProjectionFault::Capacity);
        }
        self.check_metadata(bytes)?;
        self.rows[index].tools.push(ToolObservation {
            id: ObservationId(id.to_owned()),
            round,
            slot,
            provider_call_id: provider.to_owned(),
            name: name.to_owned(),
            state: ToolObservationState::Requested,
            timing: crate::presentation::ToolTiming::started(),
        });
        self.tool_calls += 1;
        self.metadata_bytes += bytes;
        Ok(())
    }

    fn tool_return(&mut self, index: usize, event: &Value) -> Result<(), ProjectionFault> {
        let id = required_uuid4(event, "observation_id")?;
        let tool_index = self.rows[index]
            .tools
            .iter()
            .position(|tool| tool.id.as_str() == id)
            .ok_or(ProjectionFault::MissingToolRequest)?;
        if self.rows[index].tools[tool_index].state != ToolObservationState::Requested {
            return Err(ProjectionFault::DuplicateToolReturn);
        }
        let result = required_text(event, "result")?;
        let ok = event
            .get("ok")
            .and_then(Value::as_bool)
            .ok_or(ProjectionFault::Malformed("ok"))?;
        self.check_text(result.len())?;
        self.rows[index].tools[tool_index].state = ToolObservationState::Returned {
            result: result.to_owned(),
            ok,
        };
        self.rows[index].tools[tool_index].timing.finish();
        self.text_bytes += result.len();
        Ok(())
    }

    fn check_text(&self, additional: usize) -> Result<(), ProjectionFault> {
        if additional > self.limits.text_bytes.saturating_sub(self.text_bytes) {
            Err(ProjectionFault::Capacity)
        } else {
            Ok(())
        }
    }
    fn check_metadata(&self, additional: usize) -> Result<(), ProjectionFault> {
        if additional
            > self
                .limits
                .metadata_bytes
                .saturating_sub(self.metadata_bytes)
        {
            Err(ProjectionFault::Capacity)
        } else {
            Ok(())
        }
    }
}

fn incomplete_tools(row: &mut Row) {
    for tool in &mut row.tools {
        if tool.state == ToolObservationState::Requested {
            tool.state = ToolObservationState::Incomplete;
            tool.timing.finish();
        }
    }
}
fn required_text<'a>(value: &'a Value, key: &'static str) -> Result<&'a str, ProjectionFault> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or(ProjectionFault::Malformed(key))
}
fn required_u64(value: &Value, key: &'static str) -> Result<u64, ProjectionFault> {
    value
        .get(key)
        .and_then(Value::as_u64)
        .ok_or(ProjectionFault::Malformed(key))
}
fn nullable_text<'a>(
    value: &'a Value,
    key: &'static str,
) -> Result<Option<&'a str>, ProjectionFault> {
    match value.get(key) {
        Some(Value::Null) => Ok(None),
        Some(Value::String(text)) => Ok(Some(text)),
        _ => Err(ProjectionFault::Malformed(key)),
    }
}
fn text_size(text: Option<&str>) -> usize {
    text.map_or(0, str::len)
}

pub(crate) fn required_uuid4<'a>(
    value: &'a Value,
    key: &'static str,
) -> Result<&'a str, ProjectionFault> {
    let id = required_text(value, key)?;
    // Python uuid4 canonical lowercase spelling; ID domains remain separate.
    let bytes = id.as_bytes();
    let valid = bytes.len() == 36
        && bytes[14] == b'4'
        && matches!(bytes[19], b'8' | b'9' | b'a' | b'b')
        && bytes.iter().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                *b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(b)
            }
        });
    if valid {
        Ok(id)
    } else {
        Err(ProjectionFault::Malformed(key))
    }
}

pub(crate) fn origin(
    frame: &Value,
    limit: usize,
) -> Result<(ExecutionId, Origin, usize), ProjectionFault> {
    let execution = required_uuid4(frame, "execution_id")?;
    let session = required_text(frame, "session_id")?;
    if session.is_empty() {
        return Err(ProjectionFault::Malformed("empty session_id"));
    }
    let operation = match frame.get("operation_id") {
        Some(Value::Null) => None,
        Some(value) if value.is_string() || value.is_i64() || value.is_u64() => Some(value),
        _ => return Err(ProjectionFault::Malformed("operation_id")),
    };
    let hook = nullable_text(frame, "hook_id")?;
    let source = nullable_text(frame, "source")?;
    let size = execution
        .len()
        .saturating_add(session.len())
        .saturating_add(operation.map_or(0, |v| v.as_str().map_or(8, str::len)))
        .saturating_add(text_size(hook))
        .saturating_add(text_size(source));
    if size > limit {
        return Err(ProjectionFault::Capacity);
    }
    Ok((
        ExecutionId(execution.to_owned()),
        Origin {
            session: session.to_owned(),
            operation: operation.cloned(),
            hook: hook.map(str::to_owned),
            source: source.map(str::to_owned),
        },
        size,
    ))
}
