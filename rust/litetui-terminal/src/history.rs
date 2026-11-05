//! Whole-context preservation proof and inert, bounded saved-history projection.
//! A page is presentation only. It never supplies active execution or approval events.
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{collections::HashSet, io};

pub const SNAPSHOT: &str = "gui.history.native_snapshot";
pub const IMAGE: &str = "gui.history.native_image";
pub const PAGE: &str = "gui.history.native_page";
pub const OPEN: &str = "gui.conversations.native_open";
pub const OPERATIONS: [&str; 3] = [SNAPSHOT, PAGE, OPEN];
pub const PROJECTION_VERSION: u64 = 1;
pub const MAX_RESPONSE_BYTES: usize = 1024 * 1024;
/// Original JSON envelope bytes, excluding only its LF or CRLF framing.
/// Whitespace and JSON escapes still count; decoding must not shrink this evidence.
pub fn wire_response_bytes(frame: &[u8]) -> usize {
    match frame.strip_suffix(b"\n") {
        Some(json) => json.strip_suffix(b"\r").unwrap_or(json).len(),
        None => frame.len(),
    }
}
pub const MAX_PROJECTION_BYTES: u64 = 16 * 1024 * 1024;
pub const MAX_COUNT: u64 = 1_000_000;
pub const MAX_FRAGMENTS: usize = 50;
pub const MAX_CURSOR_BYTES: usize = 2048;

/// Every actual native launch requests v1 before source startup. A missing
/// marker must never fall back to the legacy full-transcript protocol.
pub fn require_projection(ready: &Value) -> io::Result<()> {
    if ready["history_projection_version"].as_u64() != Some(PROJECTION_VERSION) {
        return Err(fail(
            "Runtime did not enable requested history projection version 1",
        ));
    }
    Ok(())
}

/// Normalize compact Open into the same immutable shutdown identity used by
/// the source conversation lifecycle owner. This is evidence, never a new RPC.
pub fn shutdown_identity(command: &Value) -> io::Result<Value> {
    if command["type"] != OPEN {
        return Err(fail("Expected a dispatched native conversation open"));
    }
    Ok(
        json!({"action":"open", "action_id":storage_id(command,"action_id")?,
        "session_id":storage_id(command,"from_session_id")?,
        "target_session_id":storage_id(command,"session_id")?,
        "agent_id":storage_id(command,"agent_id")?,
        "backend":text(command,"backend",128,false)?,
        "model":text(command,"model",1024,false)?}),
    )
}

fn fail(message: &str) -> io::Error {
    io::Error::other(message.to_owned())
}
fn text(value: &Value, key: &str, maximum: usize, empty: bool) -> io::Result<String> {
    value[key]
        .as_str()
        .filter(|s| s.len() <= maximum && (empty || !s.is_empty()))
        .map(str::to_owned)
        .ok_or_else(|| fail(&format!("Invalid saved-history {key}")))
}
fn boolean(value: &Value, key: &str) -> io::Result<bool> {
    value[key]
        .as_bool()
        .ok_or_else(|| fail(&format!("Invalid saved-history {key}")))
}
fn number(value: &Value, key: &str, maximum: u64) -> io::Result<u64> {
    value[key]
        .as_u64()
        .filter(|n| *n <= maximum)
        .ok_or_else(|| fail(&format!("Invalid saved-history {key}")))
}
fn hash(value: &Value, key: &str) -> io::Result<String> {
    let s = text(value, key, 64, false)?;
    if s.len() != 64
        || !s
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    {
        return Err(fail("Invalid saved-history digest or token"));
    }
    Ok(s)
}
fn nullable_hash(value: &Value, key: &str) -> io::Result<Option<String>> {
    if value.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        hash(value, key).map(Some)
    }
}
fn nullable_text(value: &Value, key: &str, maximum: usize) -> io::Result<Option<String>> {
    if value.get(key) == Some(&Value::Null) {
        Ok(None)
    } else {
        text(value, key, maximum, false).map(Some)
    }
}
fn storage_id(value: &Value, key: &str) -> io::Result<String> {
    let s = text(value, key, 36, false)?;
    if s.len() != 36
        || !s.bytes().enumerate().all(|(i, b)| {
            if [8, 13, 18, 23].contains(&i) {
                b == b'-'
            } else {
                b.is_ascii_digit() || (b'a'..=b'f').contains(&b)
            }
        })
    {
        return Err(fail("Saved history has a noncanonical storage identity"));
    }
    Ok(s)
}
fn shape(value: &Value, kind: &str) -> io::Result<()> {
    if value["version"].as_u64() != Some(1)
        || value["kind"] != kind
        || serde_json::to_vec(value)?.len() > MAX_RESPONSE_BYTES
    {
        return Err(fail("Unsupported or oversized saved-history response"));
    }
    Ok(())
}
fn exact_keys(value: &Value, expected: &[&str]) -> io::Result<()> {
    if !value
        .as_object()
        .is_some_and(|o| o.len() == expected.len() && expected.iter().all(|k| o.contains_key(*k)))
    {
        return Err(fail("Saved-history metadata fields changed"));
    }
    Ok(())
}
fn digest(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Scope {
    Current,
    Preview,
}
impl Scope {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Current => "current",
            Self::Preview => "preview",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub from_session: String,
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
    pub scope: Scope,
    pub catalog: Option<String>,
}
impl Identity {
    pub fn command(&self, operation: &str) -> Value {
        json!({"type":operation,"version":1,"from_session_id":self.from_session,
            "session_id":self.session,"agent_id":self.agent,"backend":self.backend,
            "model":self.model,"scope":self.scope.as_str(),"catalog_snapshot":self.catalog})
    }
    pub fn current(&self) -> Self {
        Self {
            from_session: self.session.clone(),
            scope: Scope::Current,
            catalog: None,
            ..self.clone()
        }
    }
    pub fn verify(&self, value: &Value) -> io::Result<()> {
        if storage_id(value, "from_session_id")? != self.from_session
            || storage_id(value, "session_id")? != self.session
            || storage_id(value, "agent_id")? != self.agent
            || text(value, "backend", 128, false)? != self.backend
            || text(value, "model", 1024, false)? != self.model
            || value["scope"] != self.scope.as_str()
            || nullable_hash(value, "catalog_snapshot")? != self.catalog
            || (self.scope == Scope::Current
                && (self.from_session != self.session || self.catalog.is_some()))
            || (self.scope == Scope::Preview
                && (self.from_session == self.session || self.catalog.is_none()))
        {
            return Err(fail("Saved history changed its owned source identity"));
        }
        Ok(())
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Counts {
    pub messages: u64,
    pub non_system: u64,
    pub system: u64,
    pub blocks: u64,
}
impl Counts {
    fn parse(value: &Value) -> io::Result<Self> {
        exact_keys(value, &["messages", "non_system", "system", "blocks"])?;
        let counts = Self {
            messages: number(value, "messages", MAX_COUNT)?,
            non_system: number(value, "non_system", MAX_COUNT)?,
            system: number(value, "system", MAX_COUNT)?,
            blocks: number(value, "blocks", MAX_COUNT)?,
        };
        if counts.non_system.checked_add(counts.system) != Some(counts.messages)
            || (counts.messages == 0 && counts.blocks != 0)
        {
            return Err(fail("Saved-history counts contradict the full context"));
        }
        Ok(counts)
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct OpenEffects {
    pub lifecycle_hooks: bool,
    pub provider_cleanup: bool,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    pub open_effects: Option<OpenEffects>,
    pub open_action_id: Option<String>,
    pub read_only: bool,
    pub identity: Identity,
    pub token: String,
    pub transcript_revision: Option<String>,
    pub context_sha256: String,
    pub replay_sha256: String,
    pub pending: bool,
    pub materialized: bool,
    pub fresh_system_context: bool,
    pub title: String,
    pub counts: Counts,
}
impl Snapshot {
    pub fn parse(value: &Value, expected: &Identity) -> io::Result<Self> {
        shape(value, "history_snapshot")?;
        expected.verify(value)?;
        if value["projection_version"].as_u64() != Some(PROJECTION_VERSION) {
            return Err(fail(
                "Saved history lacks supported owned projection authority",
            ));
        }
        let (open_effects, open_action_id) = if expected.scope == Scope::Preview {
            exact_keys(
                &value["open_effects"],
                &["lifecycle_hooks", "provider_cleanup", "materialize"],
            )?;
            if value["open_effects"]["materialize"] != false {
                return Err(fail("Opening preview would materialize pending input"));
            }
            (
                Some(OpenEffects {
                    lifecycle_hooks: boolean(&value["open_effects"], "lifecycle_hooks")?,
                    provider_cleanup: boolean(&value["open_effects"], "provider_cleanup")?,
                }),
                Some(storage_id(value, "open_action_id")?),
            )
        } else {
            if value.get("open_effects") != Some(&Value::Null)
                || value.get("open_action_id") != Some(&Value::Null)
            {
                return Err(fail("Current history unexpectedly carries an open action"));
            }
            (None, None)
        };
        let result = Self {
            open_effects,
            open_action_id,
            read_only: boolean(value, "read_only")?,
            identity: expected.clone(),
            token: hash(value, "snapshot")?,
            transcript_revision: nullable_hash(value, "transcript_revision")?,
            context_sha256: hash(value, "context_sha256")?,
            replay_sha256: hash(value, "replay_sha256")?,
            pending: boolean(value, "pending")?,
            materialized: boolean(value, "materialized")?,
            fresh_system_context: boolean(value, "fresh_system_context")?,
            title: text(value, "title", 4096, true)?,
            counts: Counts::parse(&value["counts"])?,
        };
        if result.pending == result.materialized
            || result.pending != result.transcript_revision.is_none()
            || (expected.scope == Scope::Preview && result.pending)
            || (result.fresh_system_context && result.counts.non_system != 0)
            || (result.counts.messages == 0 && !result.fresh_system_context)
        {
            return Err(fail(
                "Saved-history storage or fresh-context proof is contradictory",
            ));
        }
        Ok(result)
    }
    /// Context preservation includes hidden system contents and provider references.
    /// Titles, file revisions and display summaries are separate facts.
    pub fn preserves_context(&self, prior: &Self) -> bool {
        self.identity.session == prior.identity.session
            && self.identity.agent == prior.identity.agent
            && self.context_sha256 == prior.context_sha256
            && self.counts.messages == prior.counts.messages
            && self.counts.non_system == prior.counts.non_system
            && self.counts.system == prior.counts.system
            && self.fresh_system_context == prior.fresh_system_context
    }
    pub fn page_request(&self, position: &PageRequest) -> io::Result<Value> {
        let mut value = self.identity.command(PAGE);
        value["snapshot"] = json!(self.token);
        match position {
            PageRequest::Anchor(anchor) => {
                value["cursor"] = Value::Null;
                value["anchor"] = json!(anchor.as_str());
            }
            PageRequest::Cursor(cursor) => {
                if cursor.is_empty() || cursor.len() > MAX_CURSOR_BYTES {
                    return Err(fail("History cursor exceeds its source bound"));
                }
                value["cursor"] = json!(cursor);
            }
        }
        Ok(value)
    }
    pub fn open_request(&self) -> io::Result<Value> {
        if self.identity.scope != Scope::Preview || self.read_only {
            return Err(fail("Open requires a reviewed preview snapshot"));
        }
        let mut value = self.identity.command(OPEN);
        value
            .as_object_mut()
            .expect("command object")
            .remove("scope");
        value["history_snapshot"] = json!(self.token);
        value["action_id"] = json!(self.open_action_id);
        Ok(value)
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Anchor {
    Head,
    Tail,
}
impl Anchor {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Head => "head",
            Self::Tail => "tail",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum PageRequest {
    Anchor(Anchor),
    Cursor(String),
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Kind {
    User,
    Assistant,
    Reasoning,
    Tool,
    Plan,
    Summary,
    Attachment,
    Notice,
    Raw,
}
impl Kind {
    fn parse(value: &Value) -> io::Result<Self> {
        match value.as_str() {
            Some("user") => Ok(Self::User),
            Some("assistant") => Ok(Self::Assistant),
            Some("reasoning") => Ok(Self::Reasoning),
            Some("tool") => Ok(Self::Tool),
            Some("plan") => Ok(Self::Plan),
            Some("summary") => Ok(Self::Summary),
            Some("attachment") => Ok(Self::Attachment),
            Some("notice") => Ok(Self::Notice),
            Some("raw") => Ok(Self::Raw),
            _ => Err(fail("Unknown inert saved-history block kind")),
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::User => "User",
            Self::Assistant => "Assistant",
            Self::Reasoning => "Reasoning",
            Self::Tool => "Tool",
            Self::Plan => "Plan",
            Self::Summary => "Summary",
            Self::Attachment => "Attachment",
            Self::Notice => "Notice",
            Self::Raw => "Saved record",
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SavedState {
    Complete,
    Failed,
    Cancelled,
    Incomplete,
    Unknown,
}
impl SavedState {
    fn parse(value: &Value) -> io::Result<Option<Self>> {
        match value.as_str() {
            Some("complete") => Ok(Some(Self::Complete)),
            Some("failed") => Ok(Some(Self::Failed)),
            Some("cancelled") => Ok(Some(Self::Cancelled)),
            Some("incomplete") => Ok(Some(Self::Incomplete)),
            Some("unknown") => Ok(Some(Self::Unknown)),
            _ if value.is_null() => Ok(None),
            _ => Err(fail("Unknown saved block outcome")),
        }
    }
    pub fn label(self) -> &'static str {
        match self {
            Self::Complete => "saved complete",
            Self::Failed => "saved failure",
            Self::Cancelled => "saved cancellation",
            Self::Incomplete => "incomplete saved result",
            Self::Unknown => "saved outcome unknown",
        }
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Metadata {
    pub provider: Option<String>,
    pub thread_id: Option<String>,
    pub turn_id: Option<String>,
    pub item_id: Option<String>,
    pub tool_call_id: Option<String>,
    pub ok: Option<bool>,
    pub duration_ms: Option<u64>,
    pub attachment_kind: Option<String>,
}
impl Metadata {
    fn parse(value: &Value) -> io::Result<Self> {
        exact_keys(
            value,
            &[
                "provider",
                "thread_id",
                "turn_id",
                "item_id",
                "tool_call_id",
                "ok",
                "duration_ms",
                "attachment_kind",
            ],
        )?;
        let provider = nullable_text(value, "provider", 16)?;
        let attachment_kind = nullable_text(value, "attachment_kind", 16)?;
        if provider
            .as_deref()
            .is_some_and(|s| !matches!(s, "claude" | "codex"))
            || attachment_kind
                .as_deref()
                .is_some_and(|s| !matches!(s, "embedded" | "remote" | "unknown"))
        {
            return Err(fail("Unknown saved block provider or attachment type"));
        }
        let id = |key: &str| {
            if value.get(key) == Some(&Value::Null) {
                Ok(None)
            } else {
                text(value, key, 1024, true).map(Some)
            }
        };
        let ok = if value["ok"].is_null() {
            None
        } else {
            Some(boolean(value, "ok")?)
        };
        let duration_ms = if value["duration_ms"].is_null() {
            None
        } else {
            Some(number(value, "duration_ms", 2147483647)?)
        };
        Ok(Self {
            provider,
            thread_id: id("thread_id")?,
            turn_id: id("turn_id")?,
            item_id: id("item_id")?,
            tool_call_id: id("tool_call_id")?,
            ok,
            duration_ms,
            attachment_kind,
        })
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fragment {
    pub block_index: u64,
    pub block_id: String,
    pub message_index: u64,
    pub message_sha256: String,
    pub kind: Kind,
    pub role: String,
    pub label: String,
    pub state: Option<SavedState>,
    pub metadata: Metadata,
    pub block_sha256: String,
    pub start: u64,
    pub end: u64,
    pub total: u64,
    pub text: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ImageSelection {
    pub block: u64,
    pub block_id: String,
    pub message: u64,
    pub message_hash: String,
}
impl ImageSelection {
    pub fn command(&self, snapshot: &Snapshot) -> Value {
        let mut v = snapshot.identity.command(IMAGE);
        v["snapshot"] = json!(snapshot.token);
        v["block_index"] = json!(self.block);
        v["block_id"] = json!(self.block_id);
        v["message_index"] = json!(self.message);
        v["message_sha256"] = json!(self.message_hash);
        v
    }
    pub fn parse(&self, v: &Value, snapshot: &Snapshot) -> io::Result<crate::image::Thumbnail> {
        crate::work_lifecycle::keys(
            v,
            &[
                "kind",
                "version",
                "from_session_id",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "scope",
                "catalog_snapshot",
                "snapshot",
                "block_index",
                "block_id",
                "message_index",
                "message_sha256",
                "width",
                "height",
                "rgba",
            ],
        )?;
        snapshot.identity.verify(v)?;
        if !snapshot.materialized
            || v["kind"] != "history_image"
            || v["version"].as_u64() != Some(1)
            || v["snapshot"] != snapshot.token
            || v["block_id"] != self.block_id
            || v["block_index"].as_u64() != Some(self.block)
            || v["message_sha256"] != self.message_hash
            || v["message_index"].as_u64() != Some(self.message)
        {
            return Err(fail(
                "Saved image belongs to another history block or snapshot",
            ));
        }
        crate::image::Thumbnail::pixels(v)
    }
}
impl Fragment {
    pub fn embedded_image(&self) -> bool {
        self.kind == Kind::Attachment
            && self.metadata.attachment_kind.as_deref() == Some("embedded")
    }
    pub fn image(&self) -> Option<ImageSelection> {
        self.embedded_image().then(|| ImageSelection {
            block: self.block_index,
            block_id: self.block_id.clone(),
            message: self.message_index,
            message_hash: self.message_sha256.clone(),
        })
    }
    fn parse(value: &Value, counts: &Counts) -> io::Result<Self> {
        let part = &value["part"];
        exact_keys(part, &["utf8_start", "utf8_end", "utf8_total", "sha256"])?;
        let result = Self {
            block_index: number(value, "block_index", MAX_COUNT)?,
            block_id: hash(value, "block_id")?,
            message_index: number(value, "message_index", MAX_COUNT)?,
            message_sha256: hash(value, "message_sha256")?,
            kind: Kind::parse(&value["kind"])?,
            role: text(value, "role", 32, true)?,
            label: text(value, "label", 1024, true)?,
            state: SavedState::parse(
                value
                    .get("state")
                    .ok_or_else(|| fail("Missing saved block state"))?,
            )?,
            metadata: Metadata::parse(&value["metadata"])?,
            block_sha256: hash(value, "block_sha256")?,
            start: number(part, "utf8_start", MAX_PROJECTION_BYTES)?,
            end: number(part, "utf8_end", MAX_PROJECTION_BYTES)?,
            total: number(part, "utf8_total", MAX_PROJECTION_BYTES)?,
            text: text(value, "text", MAX_RESPONSE_BYTES, true)?,
        };
        if result.block_index >= counts.blocks
            || result.message_index >= counts.messages
            || result.start > result.end
            || result.end > result.total
            || result.end - result.start != result.text.len() as u64
            || (result.start == result.end && result.total != 0)
            || digest(&result.text) != hash(part, "sha256")?
            || (result.start == 0
                && result.end == result.total
                && digest(&result.text) != result.block_sha256)
        {
            return Err(fail(
                "Saved-history fragment span or digest is contradictory",
            ));
        }
        Ok(result)
    }
    fn same_block(&self, other: &Self) -> bool {
        self.block_index == other.block_index
            && self.block_id == other.block_id
            && self.message_index == other.message_index
            && self.message_sha256 == other.message_sha256
            && self.kind == other.kind
            && self.role == other.role
            && self.label == other.label
            && self.state == other.state
            && self.metadata == other.metadata
            && self.block_sha256 == other.block_sha256
            && self.total == other.total
    }
    fn follows(&self, prior: &Self) -> bool {
        if self.block_index == prior.block_index {
            self.same_block(prior) && prior.end < prior.total && self.start == prior.end
        } else {
            self.block_index == prior.block_index + 1
                && prior.end == prior.total
                && self.start == 0
                && self.message_index >= prior.message_index
        }
    }
    pub fn heading(&self) -> String {
        let label = if self.label.is_empty() {
            self.kind.label()
        } else {
            &self.label
        };
        let state = self
            .state
            .map(|s| format!(" · {}", s.label()))
            .unwrap_or_default();
        let continued = if self.start > 0 { " · continued" } else { "" };
        format!(
            "Saved {} · {}{state}{continued}",
            self.kind.label(),
            crate::live::terminal_text(label)
        )
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Page {
    pub identity: Identity,
    pub snapshot: String,
    pub context_sha256: String,
    pub replay_sha256: String,
    pub counts: Counts,
    pub fragments: Vec<Fragment>,
    pub previous: Option<String>,
    pub next: Option<String>,
    pub at_start: bool,
    pub at_end: bool,
    pub bytes: usize,
    pub units: usize,
}
impl Page {
    pub fn parse(value: &Value, snapshot: &Snapshot) -> io::Result<Self> {
        shape(value, "history_page")?;
        snapshot.identity.verify(value)?;
        let counts = Counts::parse(&value["counts"])?;
        if value["projection_version"].as_u64() != Some(PROJECTION_VERSION)
            || hash(value, "snapshot")? != snapshot.token
            || hash(value, "context_sha256")? != snapshot.context_sha256
            || hash(value, "replay_sha256")? != snapshot.replay_sha256
            || counts != snapshot.counts
        {
            return Err(fail("Saved-history page changed its full-context snapshot"));
        }
        let values = value["fragments"]
            .as_array()
            .filter(|v| v.len() <= MAX_FRAGMENTS)
            .ok_or_else(|| fail("Saved-history fragment count exceeds budget"))?;
        let mut fragments = Vec::<Fragment>::new();
        let mut blocks = HashSet::new();
        let mut units = 0usize;
        let mut bytes = 0usize;
        for value in values {
            let fragment = Fragment::parse(value, &counts)?;
            if let Some(prior) = fragments.last()
                && !fragment.follows(prior)
            {
                return Err(fail(
                    "Saved-history fragment order has a gap, overlap or changed block",
                ));
            }
            if fragments
                .last()
                .is_none_or(|prior| prior.block_index != fragment.block_index)
                && !blocks.insert(fragment.block_id.clone())
            {
                return Err(fail("Saved-history block identity is duplicated"));
            }
            for text in [&fragment.text, &fragment.label, &fragment.role] {
                units = units.saturating_add(crate::live_chat::display_units(text)?);
                bytes = bytes.saturating_add(text.len());
            }
            units = units.saturating_add(320);
            if units > crate::live_chat::DISPLAY_UNITS || bytes > crate::live_chat::RETAINED_BYTES {
                return Err(fail("Saved-history page exceeds native display budget"));
            }
            fragments.push(fragment);
        }
        let previous = nullable_text(value, "previous_cursor", MAX_CURSOR_BYTES)?;
        let next = nullable_text(value, "next_cursor", MAX_CURSOR_BYTES)?;
        let at_start = boolean(value, "at_start")?;
        let at_end = boolean(value, "at_end")?;
        let expected_start = fragments
            .first()
            .is_none_or(|f| f.block_index == 0 && f.start == 0);
        let expected_end = fragments
            .last()
            .is_none_or(|f| f.block_index + 1 == counts.blocks && f.end == f.total);
        if fragments.is_empty() != (counts.blocks == 0)
            || at_start != expected_start
            || at_end != expected_end
            || previous.is_some() == at_start
            || next.is_some() == at_end
            || previous.as_ref().is_some_and(|p| Some(p) == next.as_ref())
        {
            return Err(fail(
                "Saved-history page navigation contradicts its fragment boundaries",
            ));
        }
        Ok(Self {
            identity: snapshot.identity.clone(),
            snapshot: snapshot.token.clone(),
            context_sha256: snapshot.context_sha256.clone(),
            replay_sha256: snapshot.replay_sha256.clone(),
            counts,
            fragments,
            previous,
            next,
            at_start,
            at_end,
            bytes,
            units,
        })
    }
    pub fn accepts(&self, request: &PageRequest) -> io::Result<()> {
        match request {
            PageRequest::Anchor(Anchor::Head) if self.at_start => Ok(()),
            PageRequest::Anchor(Anchor::Tail) if self.at_end => Ok(()),
            PageRequest::Cursor(_) => Ok(()),
            _ => Err(fail(
                "Saved-history page did not honor its requested anchor",
            )),
        }
    }
    pub fn follows(&self, prior: &Self, requested: &str) -> io::Result<()> {
        if self.identity != prior.identity
            || self.snapshot != prior.snapshot
            || self.context_sha256 != prior.context_sha256
            || self.replay_sha256 != prior.replay_sha256
            || self.counts != prior.counts
        {
            return Err(fail("Saved-history paging crossed a snapshot boundary"));
        }
        let adjacent = if prior.next.as_deref() == Some(requested) {
            self.fragments
                .first()
                .zip(prior.fragments.last())
                .is_some_and(|(next, last)| next.follows(last))
        } else if prior.previous.as_deref() == Some(requested) {
            prior
                .fragments
                .first()
                .zip(self.fragments.last())
                .is_some_and(|(next, last)| next.follows(last))
        } else {
            false
        };
        if !adjacent {
            return Err(fail(
                "Saved-history page was not the exact offered neighbor",
            ));
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum OpenResult {
    Refused(String),
    Connected(Box<Snapshot>),
}
impl OpenResult {
    pub fn parse(value: &Value, preview: &Snapshot) -> io::Result<Self> {
        shape(value, "conversation_open")?;
        let identity = &preview.identity;
        if identity.scope != Scope::Preview
            || storage_id(value, "from_session_id")? != identity.from_session
            || storage_id(value, "target_session_id")? != identity.session
            || storage_id(value, "agent_id")? != identity.agent
            || text(value, "backend", 128, false)? != identity.backend
            || text(value, "model", 1024, false)? != identity.model
            || nullable_hash(value, "catalog_snapshot")? != identity.catalog
            || hash(value, "history_snapshot")? != preview.token
            || value["action_id"] != json!(preview.open_action_id)
        {
            return Err(fail(
                "Conversation open changed its reviewed history selection",
            ));
        }
        let actual = storage_id(value, "session_id")?;
        let status = text(value, "status", 16, false)?;
        let mutated = boolean(value, "mutated")?;
        let close = boolean(value, "close_required")?;
        let error = nullable_text(value, "error", 4096)?;
        let worker = boolean(value, "worker_settled")?;
        let connection = boolean(value, "connection_settled")?;
        let history = boolean(value, "history_settled")?;
        if close || status == "failed" {
            return Err(fail(
                error
                    .as_deref()
                    .unwrap_or("Conversation open outcome is uncertain"),
            ));
        }
        if status == "refused" {
            if mutated
                || actual != identity.from_session
                || !value["history"].is_null()
                || value["model"] != identity.model
            {
                return Err(fail("Refused conversation open reports changed state"));
            }
            return Ok(Self::Refused(
                error.ok_or_else(|| fail("Conversation refusal lacks reason"))?,
            ));
        }
        if status != "connected"
            || !mutated
            || actual != identity.session
            || error.is_some()
            || !worker
            || !connection
            || !history
        {
            return Err(fail(
                "Conversation open lacks settled connection and history proof",
            ));
        }
        let current = identity.current();
        Ok(Self::Connected(Box::new(Snapshot::parse(
            &value["history"],
            &current,
        )?)))
    }
}
