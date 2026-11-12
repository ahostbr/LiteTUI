//! Fresh-root provenance guard and bounded saved transcript projection.
//! The marker is not authority: Python still acquires its agent/conversation locks.
use serde_json::{Value, json};
use std::{
    fs,
    io::{self, Read},
    path::Path,
};

pub const MARKER: &str = "rust-frontend.json";
const FORMAT: &str = "litetui-rust-owned-root";
/// What a persistent root copies from the Python data root on its first run: settings only
/// (global settings with themes, per-model choices and keys; MCP server declarations).
pub const SEEDED: [&str; 3] = ["settings.json", "mcp.json", ".mcp.json"];

pub fn conversation_id(value: &str) -> io::Result<()> {
    crate::stream::required_uuid4(&json!({"id": value}), "id")
        .map(|_| ())
        .map_err(|_| io::Error::other("Resume requires an exact canonical conversation UUID"))
}

fn read_json(path: &Path, maximum: u64) -> io::Result<Value> {
    let meta = fs::symlink_metadata(path)?;
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return Err(io::Error::other("Resume refuses linked state files"));
        }
    }
    if !meta.is_file() || meta.file_type().is_symlink() {
        return Err(io::Error::other("Resume requires regular state files"));
    }
    let mut bytes = Vec::new();
    fs::File::open(path)?
        .take(maximum + 1)
        .read_to_end(&mut bytes)?;
    if bytes.len() as u64 > maximum {
        return Err(io::Error::other("Resume state file exceeds its size limit"));
    }
    serde_json::from_slice(&bytes).map_err(io::Error::other)
}

pub fn mark_fresh(root: &Path) -> io::Result<()> {
    use std::io::Write;
    let mut file = fs::OpenOptions::new()
        .create_new(true)
        .write(true)
        .open(root.join(MARKER))?;
    file.write_all(&serde_json::to_vec(&json!({"format":FORMAT,"version":1}))?)
}

/// The root carries this frontend's marker (it made the root).
pub fn validate_marker(root: &Path) -> io::Result<()> {
    let marker = read_json(&root.join(MARKER), 1024).map_err(|e| {
        io::Error::other(format!(
            "This data root was not created by the Rust frontend: {e}"
        ))
    })?;
    if marker["format"] != FORMAT || marker["version"].as_u64() != Some(1) {
        return Err(io::Error::other("Unsupported Rust frontend root marker"));
    }
    Ok(())
}

/// The root's settings.json, bounded and regular.
pub fn read_settings(root: &Path) -> io::Result<Value> {
    read_json(&root.join("settings.json"), 1024 * 1024)
}

/// Read-only preflight. Never create, migrate or overwrite an existing root.
pub fn validate_root(root: &Path, tools: bool) -> io::Result<()> {
    validate_marker(root)?;
    let settings = read_json(&root.join("settings.json"), 256 * 1024)?;
    // The explicit lifecycle launch capability overlays MCP off before Python
    // constructs any manager, even when the saved preference is enabled.
    if settings["tools_enabled"].as_bool() != Some(tools)
        || settings["mcp_enabled"].as_bool().is_none()
    {
        return Err(io::Error::other(
            "Resume tool mode must match the saved root; MCP preference must be boolean",
        ));
    }
    Ok(())
}

#[derive(Clone, Debug, PartialEq)]
pub struct Message {
    pub role: String,
    pub text: String,
    pub details: String,
}
#[derive(Clone, Debug, PartialEq)]
pub struct History {
    pub descriptor: Option<Box<crate::history::Snapshot>>,
    pub page: Option<Box<crate::history::Page>>,
    pub session: String,
    pub messages: Vec<Message>,
    pub hidden_system_messages: usize,
    pub(crate) bytes: usize,
    pub(crate) units: usize,
}
impl History {
    pub fn projected(descriptor: crate::history::Snapshot) -> Self {
        Self {
            session: descriptor.identity.session.clone(),
            hidden_system_messages: descriptor.counts.system as usize,
            descriptor: Some(Box::new(descriptor)),
            page: None,
            messages: Vec::new(),
            bytes: 0,
            units: 0,
        }
    }
    pub fn preserves_context(&self, prior: &Self) -> bool {
        match (&self.descriptor, &prior.descriptor) {
            (Some(after), Some(before)) => after.preserves_context(before),
            (None, None) => self == prior,
            _ => false,
        }
    }
    pub fn parse(value: &Value, session: &str) -> io::Result<Self> {
        if value["session_id"] != session
            || value["read_only"] != false
            || value.get("meta").and_then(Value::as_object).is_none()
            || value["meta"].get("id").is_some_and(|id| id != session)
        {
            return Err(io::Error::other(
                "Saved transcript identity/ownership does not match the resumed conversation",
            ));
        }
        let source = value["messages"]
            .as_array()
            .filter(|v| !v.is_empty() && v.len() <= 1000)
            .ok_or_else(|| io::Error::other("Saved transcript requires 1..1000 messages"))?;
        let mut history = Self {
            descriptor: None,
            page: None,
            session: session.into(),
            messages: Vec::new(),
            hidden_system_messages: 0,
            bytes: 0,
            units: 0,
        };
        for message in source {
            let object = message
                .as_object()
                .ok_or_else(|| io::Error::other("Saved message must be an object"))?;
            let role = object
                .get("role")
                .and_then(Value::as_str)
                .filter(|s| !s.is_empty() && s.len() <= 32)
                .ok_or_else(|| io::Error::other("Saved message lacks a valid role"))?;
            // Source UI also omits system prompts. They remain in Python context.
            if role == "system" {
                history.hidden_system_messages += 1;
                continue;
            }
            if history.messages.len() >= 100 {
                return Err(io::Error::other(
                    "Saved visible-message limit exceeded; no prompt sent",
                ));
            }
            let text = match object.get("content") {
                Some(Value::String(s)) => s.clone(),
                None | Some(Value::Null) => String::new(),
                Some(v) => serde_json::to_string(v).map_err(io::Error::other)?,
            };
            let extras: serde_json::Map<String, Value> = object
                .iter()
                .filter(|(key, _)| !matches!(key.as_str(), "role" | "content"))
                .map(|(key, value)| (key.clone(), value.clone()))
                .collect();
            let details = if extras.is_empty() {
                String::new()
            } else {
                serde_json::to_string(&extras).map_err(io::Error::other)?
            };
            history.bytes = history
                .bytes
                .saturating_add(role.len() + text.len() + details.len());
            history.units = history
                .units
                .saturating_add(crate::live_chat::display_units(&text)?)
                .saturating_add(crate::live_chat::display_units(&details)?)
                .saturating_add(crate::live_chat::display_units(role)?)
                .saturating_add(320);
            if history.bytes > crate::live_chat::RETAINED_BYTES
                || history.units > crate::live_chat::DISPLAY_UNITS
            {
                return Err(io::Error::other(
                    "Saved transcript display budget exceeded; no prompt sent",
                ));
            }
            history.messages.push(Message {
                role: role.into(),
                text,
                details,
            });
        }
        Ok(history)
    }
}
