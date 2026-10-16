//! Offline wire primitives; no process, provider, authority or command replay.
//!
//! Limits must be chosen explicitly by a caller. These primitives do not select
//! a production frame limit or establish the separate bounded-queue contract.
use serde_json::Value;
use std::{error::Error, fmt};

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum DecodeError {
    InvalidLimit,
    FrameTooLarge { limit: usize },
    InvalidUtf8,
    InvalidJson(String),
    NotObject,
    TruncatedFrame,
    Stopped,
}

impl fmt::Display for DecodeError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::InvalidLimit => write!(f, "JSONL frame limit must be positive"),
            Self::FrameTooLarge { limit } => write!(f, "JSONL frame exceeds {limit} bytes"),
            Self::InvalidUtf8 => write!(f, "JSONL frame is not valid UTF-8"),
            Self::InvalidJson(error) => write!(f, "invalid JSONL frame: {error}"),
            Self::NotObject => write!(f, "protocol frame must be a JSON object"),
            Self::TruncatedFrame => write!(f, "EOF before the final frame's newline"),
            Self::Stopped => write!(
                f,
                "decoder is stopped; a new connection needs a new decoder"
            ),
        }
    }
}
impl Error for DecodeError {}

/// Bounded partial-line storage and synchronous frame delivery, not an event queue.
///
/// A valid prefix is delivered even if a later frame in the same input is bad.
/// After any framing fault, remaining input is ignored and the decoder stops;
/// callers must not retry the chunk or reuse this instance for a new connection.
/// The byte limit excludes LF but includes CR in a CRLF line, even on blank lines.
pub struct JsonlDecoder {
    pending: Vec<u8>,
    limit: usize,
    stopped: bool,
}

impl JsonlDecoder {
    pub fn new(max_frame_bytes: usize) -> Result<Self, DecodeError> {
        if max_frame_bytes == 0 {
            return Err(DecodeError::InvalidLimit);
        }
        Ok(Self {
            pending: Vec::new(),
            limit: max_frame_bytes,
            stopped: false,
        })
    }

    pub fn push(&mut self, input: &[u8], mut emit: impl FnMut(Value)) -> Result<(), DecodeError> {
        if self.stopped {
            return Err(DecodeError::Stopped);
        }
        for &byte in input {
            if byte == b'\n' {
                let result = self.line();
                self.pending.clear();
                match result {
                    Ok(Some(value)) => emit(value),
                    Ok(None) => {}
                    Err(error) => {
                        self.stopped = true;
                        return Err(error);
                    }
                }
            } else {
                if self.pending.len() == self.limit {
                    self.pending.clear();
                    self.stopped = true;
                    return Err(DecodeError::FrameTooLarge { limit: self.limit });
                }
                self.pending.push(byte);
            }
        }
        Ok(())
    }

    fn line(&self) -> Result<Option<Value>, DecodeError> {
        let text = std::str::from_utf8(&self.pending).map_err(|_| DecodeError::InvalidUtf8)?;
        if text
            .trim_matches(|ch: char| ch.is_ascii_whitespace())
            .is_empty()
        {
            return Ok(None);
        }
        let value = parse_wire_value(text, 128)?;
        if !value.is_object() {
            return Err(DecodeError::NotObject);
        }
        Ok(Some(value))
    }

    /// Close at EOF. An unterminated object is not silently accepted or retried.
    pub fn finish(&mut self) -> Result<(), DecodeError> {
        if self.stopped {
            return Err(DecodeError::Stopped);
        }
        self.stopped = true;
        if self.pending.is_empty() {
            Ok(())
        } else {
            self.pending.clear();
            Err(DecodeError::TruncatedFrame)
        }
    }
}

// Deserialize containers through raw children, not Value's magic-map visitor:
// arbitrary_precision otherwise converts a literal object with the key
// "$serde_json::private::Number" into a number, corrupting payloads and allowing
// object-valued identities/versions through integer checks. Actual primitive
// numbers still use serde's exact-number parser. Keep a structural depth bound
// as raw child scanning does not enforce Value's usual recursive visitor limit.
fn parse_wire_value(text: &str, depth_left: usize) -> Result<Value, DecodeError> {
    use serde_json::value::RawValue;
    let invalid = |error: serde_json::Error| DecodeError::InvalidJson(error.to_string());
    if depth_left == 0 {
        return Err(DecodeError::InvalidJson(
            "JSON nesting exceeds 128 values".into(),
        ));
    }
    match text.trim_start().as_bytes().first() {
        Some(b'{') => {
            struct WireObject(usize);
            impl<'de> serde::de::Visitor<'de> for WireObject {
                type Value = Value;
                fn expecting(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
                    formatter.write_str("a JSON object")
                }
                fn visit_map<A: serde::de::MapAccess<'de>>(
                    self,
                    mut entries: A,
                ) -> Result<Value, A::Error> {
                    let mut object = serde_json::Map::new();
                    while let Some((key, raw)) = entries.next_entry::<String, &RawValue>()? {
                        // Validate every occurrence BEFORE last-key-wins insertion:
                        // overwritten children must not bypass the depth bound.
                        let value = parse_wire_value(raw.get(), self.0)
                            .map_err(serde::de::Error::custom)?;
                        object.insert(key, value);
                    }
                    Ok(Value::Object(object))
                }
            }
            let mut decoder = serde_json::Deserializer::from_str(text);
            let value =
                serde::Deserializer::deserialize_map(&mut decoder, WireObject(depth_left - 1))
                    .map_err(invalid)?;
            decoder.end().map_err(invalid)?;
            Ok(value)
        }
        Some(b'[') => {
            let entries: Vec<&RawValue> = serde_json::from_str(text).map_err(invalid)?;
            entries
                .into_iter()
                .map(|raw| parse_wire_value(raw.get(), depth_left - 1))
                .collect::<Result<Vec<Value>, _>>()
                .map(Value::Array)
        }
        _ => serde_json::from_str(text).map_err(invalid),
    }
}

/// Wire correlation only. This is NOT a turn, session, approval or operation ID.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct CommandId(IdValue);

// Private so callers cannot construct two unequal identities with the same wire
// representation (e.g. Signed(7) vs Unsigned(7)). Only negative values use Signed.
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum IdValue {
    Text(String),
    Unsigned(u64),
    Signed(i64),
}
impl CommandId {
    pub fn to_value(&self) -> Value {
        match &self.0 {
            IdValue::Text(value) => Value::String(value.clone()),
            IdValue::Unsigned(value) => (*value).into(),
            IdValue::Signed(value) => (*value).into(),
        }
    }
}
impl TryFrom<&Value> for CommandId {
    type Error = ClientFault;
    fn try_from(value: &Value) -> Result<Self, Self::Error> {
        if let Some(text) = value.as_str() {
            return Ok(Self(IdValue::Text(text.to_owned())));
        }
        if let Some(number) = value.as_u64() {
            return Ok(Self(IdValue::Unsigned(number)));
        }
        if let Some(number) = value.as_i64() {
            return Ok(Self(if number < 0 {
                IdValue::Signed(number)
            } else {
                IdValue::Unsigned(number as u64)
            }));
        }
        Err(ClientFault::Malformed(
            "correlation ID must be a string or integer in i64/u64 range",
        ))
    }
}

/// Source-reviewed delivery classification, not an event-payload schema.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Delivery {
    Mirrored,
    DirectOnly,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ClientError {
    InvalidLimit,
    InvalidPolicy,
    HelloAlreadyStarted,
    NotReady,
    PendingLimit,
    IdExhausted,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum ClientFault {
    Malformed(&'static str),
    UnknownEvent(String),
    ActivationUncertain,
    IncompatibleHello,
    HelloRejected,
    Disconnected,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Notice {
    Response {
        id: Option<CommandId>,
        matched: bool,
        frame: Value,
    },
    DirectError(Value),
    Hello(Value),
    /// Provisional legacy observation, not proof of reconciled application state.
    LegacyEvent(Value),
    /// Selected representation only; a future reducer must validate its payload.
    Event {
        frame: Value,
        event: Value,
    },
    IgnoredMirror(Value),
    Quarantined(Value),
    Fault {
        reason: ClientFault,
        frame: Value,
    },
    StaleGeneration,
}

#[derive(Clone, Copy)]
enum Pending {
    Hello,
    Read,
}

/// Offline protocol bookkeeping. No writer, child, retry timer or state reducer.
///
/// A matching hello establishes version compatibility, NOT authority or a proven
/// snapshot/event barrier. State events during negotiation latch uncertainty.
/// A fault never clears automatically, although already-pending responses drain.
/// Generated string IDs never repeat within a generation; replacing a connection
/// requires a fresh caller-owned generation and a fresh client/decoder.
///
/// Delivery policy must come from a reviewed producer inventory. Unknown events
/// block; this slice provides no speculative diagnostic exemptions. Per-event
/// payload schemas, state reconciliation and mutation submission remain unwired.
pub struct WireClient {
    generation: u64,
    next_id: u64,
    max_pending: usize,
    pending: std::collections::HashMap<CommandId, Pending>,
    delivery: std::collections::HashMap<String, Delivery>,
    hello_started: bool,
    compatible: bool,
    closed: bool,
    fault: Option<ClientFault>,
}

impl WireClient {
    pub fn new(
        generation: u64,
        max_pending: usize,
        policy: impl IntoIterator<Item = (String, Delivery)>,
    ) -> Result<Self, ClientError> {
        if max_pending == 0 {
            return Err(ClientError::InvalidLimit);
        }
        let mut delivery = std::collections::HashMap::new();
        for (name, kind) in policy {
            if name.is_empty()
                || matches!(name.as_str(), "response" | "error" | "gui.event")
                || delivery.insert(name, kind).is_some()
            {
                return Err(ClientError::InvalidPolicy);
            }
        }
        Ok(Self {
            generation,
            next_id: 0,
            max_pending,
            pending: std::collections::HashMap::new(),
            delivery,
            hello_started: false,
            compatible: false,
            closed: false,
            fault: None,
        })
    }

    fn allocate(&mut self, kind: Pending) -> Result<CommandId, ClientError> {
        if self.pending.len() >= self.max_pending {
            return Err(ClientError::PendingLimit);
        }
        let next = self
            .next_id
            .checked_add(1)
            .ok_or(ClientError::IdExhausted)?;
        let id = CommandId(IdValue::Text(format!(
            "g{}-c{}",
            self.generation, self.next_id
        )));
        self.next_id = next;
        self.pending.insert(id.clone(), kind);
        Ok(id)
    }

    /// Register before returning the command; the future owner may write it ONCE.
    pub fn begin_hello(&mut self) -> Result<Value, ClientError> {
        if self.hello_started {
            return Err(ClientError::HelloAlreadyStarted);
        }
        if self.closed || self.fault.is_some() {
            return Err(ClientError::NotReady);
        }
        let id = self.allocate(Pending::Hello)?;
        self.hello_started = true;
        Ok(serde_json::json!({"type":"gui.hello", "id":id.to_value(), "protocol_version":1}))
    }

    /// Correlate a future read request. This does not send or authorize anything.
    pub fn register_read(&mut self) -> Result<CommandId, ClientError> {
        if !self.ready_for_reads() {
            return Err(ClientError::NotReady);
        }
        self.allocate(Pending::Read)
    }
    pub fn compatible(&self) -> bool {
        self.compatible
    }
    pub fn ready_for_reads(&self) -> bool {
        self.compatible && !self.closed && self.fault.is_none()
    }
    pub fn fault(&self) -> Option<&ClientFault> {
        self.fault.as_ref()
    }
    pub fn pending_count(&self) -> usize {
        self.pending.len()
    }

    pub fn disconnect(&mut self) {
        self.closed = true;
        self.fault.get_or_insert(ClientFault::Disconnected);
        // Pending requests remain uncertain, not completed and not resubmitted.
    }

    fn fail(&mut self, reason: ClientFault, frame: Value) -> Notice {
        self.fault.get_or_insert(reason.clone());
        Notice::Fault { reason, frame }
    }

    pub fn ingest(&mut self, generation: u64, frame: Value) -> Notice {
        if generation != self.generation {
            return Notice::StaleGeneration;
        }
        if self.closed {
            return Notice::Quarantined(frame);
        }
        let Some(kind) = frame.get("type").and_then(Value::as_str) else {
            return self.fail(ClientFault::Malformed("frame type must be a string"), frame);
        };
        if kind == "response" {
            return self.response(frame);
        }
        if kind == "error" {
            return if frame.get("error").is_some_and(Value::is_string) {
                Notice::DirectError(frame)
            } else {
                self.fail(ClientFault::Malformed("direct error needs text"), frame)
            };
        }
        if self.fault.is_some() {
            return Notice::Quarantined(frame);
        }
        if kind == "gui.event" {
            return self.envelope(frame);
        }
        let Some(delivery) = self.delivery.get(kind).copied() else {
            return self.fail(ClientFault::UnknownEvent(kind.to_owned()), frame);
        };
        if !self.compatible {
            return if self.hello_started {
                self.fail(ClientFault::ActivationUncertain, frame)
            } else {
                Notice::LegacyEvent(frame)
            };
        }
        match delivery {
            Delivery::Mirrored => Notice::IgnoredMirror(frame),
            Delivery::DirectOnly => Notice::Event {
                event: frame.clone(),
                frame,
            },
        }
    }

    fn response(&mut self, frame: Value) -> Notice {
        let id = match frame.get("id").map(CommandId::try_from).transpose() {
            Ok(id) => id,
            Err(reason) => return self.fail(reason, frame),
        };
        let Some(ok) = frame.get("ok").and_then(Value::as_bool) else {
            return self.fail(ClientFault::Malformed("response ok must be boolean"), frame);
        };
        if (ok && frame.get("result").is_none())
            || (!ok && !frame.get("error").is_some_and(Value::is_string))
        {
            return self.fail(
                ClientFault::Malformed("response needs result or error"),
                frame,
            );
        }
        let pending = id.as_ref().and_then(|id| self.pending.remove(id));
        if matches!(pending, Some(Pending::Hello)) {
            if !ok {
                return self.fail(ClientFault::HelloRejected, frame);
            }
            let result = &frame["result"];
            let strings = |key| {
                result
                    .get(key)
                    .and_then(Value::as_array)
                    .is_some_and(|items| items.iter().all(Value::is_string))
            };
            if result.get("protocol_version").and_then(Value::as_u64) != Some(1)
                || result.get("data_version").and_then(Value::as_u64) != Some(1)
                || !strings("capabilities")
                || !strings("operations")
            {
                return self.fail(ClientFault::IncompatibleHello, frame);
            }
            self.compatible = true;
            return Notice::Hello(frame);
        }
        Notice::Response {
            id,
            matched: pending.is_some(),
            frame,
        }
    }

    fn envelope(&mut self, frame: Value) -> Notice {
        let nullable_session = frame
            .get("session_id")
            .is_some_and(|value| value.is_null() || value.is_string());
        let nullable_operation = frame
            .get("operation_id")
            .is_some_and(|value| value.is_null() || CommandId::try_from(value).is_ok());
        let Some(event) = frame.get("event").filter(|event| event.is_object()) else {
            return self.fail(
                ClientFault::Malformed("GUI envelope needs an event object"),
                frame,
            );
        };
        let Some(kind) = event.get("type").and_then(Value::as_str) else {
            return self.fail(
                ClientFault::Malformed("GUI event type must be a string"),
                frame,
            );
        };
        if !nullable_session || !nullable_operation {
            return self.fail(
                ClientFault::Malformed(
                    "GUI identity fields must be present with valid nullable values",
                ),
                frame,
            );
        }
        if !self.compatible {
            return self.fail(ClientFault::ActivationUncertain, frame);
        }
        match self.delivery.get(kind) {
            Some(Delivery::Mirrored) => Notice::Event {
                event: event.clone(),
                frame,
            },
            Some(Delivery::DirectOnly) => self.fail(
                ClientFault::Malformed("direct-only event unexpectedly wrapped"),
                frame,
            ),
            None => self.fail(ClientFault::UnknownEvent(kind.to_owned()), frame),
        }
    }
}
