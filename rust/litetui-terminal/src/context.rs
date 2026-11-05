//! Pages from the current source-owned system prompt and agent documents.
use crate::{work_lifecycle as work, work_protocol::Identity};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const OPERATION: &str = "gui.context.native_page";
pub const TOPIC_PAGE: &str = "gui.context.native_topic_page";
pub const PAGE_BYTES: usize = 4096;
pub const MAX_DOCUMENT_BYTES: usize = 262_144;

#[derive(Clone, Debug, Default, PartialEq, Eq)]
pub enum Document {
    #[default]
    System,
    Memory,
    Soul,
    Handoff,
    Hooks,
    Plugins,
    Topics,
    Topic(String),
}

impl Document {
    pub const ALL: [Self; 7] = [
        Self::System,
        Self::Memory,
        Self::Soul,
        Self::Handoff,
        Self::Hooks,
        Self::Plugins,
        Self::Topics,
    ];

    pub fn topic(name: &str) -> io::Result<Self> {
        let parts: Vec<_> = name.split('/').collect();
        if name.len() > 512
            || !(2..=17).contains(&parts.len())
            || parts[0] != "memories"
            || !name.ends_with(".md")
            || parts[1..].iter().any(|s| {
                let stem = s.split('.').next().unwrap_or("").to_uppercase();
                s.is_empty()
                    || s.trim() != *s
                    || s.ends_with('.')
                    || s.encode_utf16().count() > 255
                    || s.chars()
                        .any(|c| c.is_control() || r#"<>:"\|?*"#.contains(c))
                    || matches!(stem.as_str(), "CON" | "PRN" | "AUX" | "NUL")
                    || ["COM", "LPT"].iter().any(|prefix| {
                        stem.strip_prefix(prefix).is_some_and(|n| {
                            matches!(
                                n,
                                "1" | "2"
                                    | "3"
                                    | "4"
                                    | "5"
                                    | "6"
                                    | "7"
                                    | "8"
                                    | "9"
                                    | "¹"
                                    | "²"
                                    | "³"
                            )
                        })
                    })
            })
        {
            return Err(work::bad(
                "Use a portable relative Markdown topic under memories/",
            ));
        }
        Ok(Self::Topic(name.into()))
    }
    pub fn key(&self) -> &str {
        match self {
            Self::System => "system",
            Self::Topics => "topics",
            Self::Topic(name) => name,
            Self::Memory => "memory.md",
            Self::Soul => "soul.md",
            Self::Handoff => "handoff.md",
            Self::Hooks => "hooks",
            Self::Plugins => "plugins",
        }
    }

    pub fn label(&self) -> &str {
        match self {
            Self::System => "System prompt",
            Self::Hooks => "Hooks",
            Self::Plugins => "Plugins",
            Self::Topics => "Topics",
            other => other.key(),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Request {
    pub document: Document,
    pub offset: usize,
    pub revision: Option<String>,
}

impl Request {
    pub fn operation(&self) -> &'static str {
        if matches!(self.document, Document::Topics | Document::Topic(_)) {
            TOPIC_PAGE
        } else {
            OPERATION
        }
    }
    pub fn first(document: Document) -> Self {
        Self {
            document,
            offset: 0,
            revision: None,
        }
    }

    pub fn command(&self, identity: &Identity, work_instance: &str) -> Value {
        let mut command = identity.request();
        command["type"] = json!(self.operation());
        command["work_instance"] = json!(work_instance);
        command["document"] = json!(self.document.key());
        command["offset"] = json!(self.offset);
        command["revision"] = json!(self.revision);
        command
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Status {
    Page,
    Empty,
    Missing,
    Oversize,
    Changed,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Page {
    pub document: Document,
    pub status: Status,
    pub revision: Option<String>,
    pub total_bytes: Option<usize>,
    pub offset: usize,
    pub next_offset: Option<usize>,
    pub text: String,
}

impl Page {
    pub fn parse(
        value: &Value,
        identity: &Identity,
        work_instance: &str,
        request: &Request,
    ) -> io::Result<Self> {
        work::keys(
            value,
            &[
                "version",
                "kind",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "document",
                "status",
                "revision",
                "total_bytes",
                "offset",
                "next_offset",
                "text",
            ],
        )?;
        if value["version"].as_u64() != Some(1)
            || value["kind"] != "context_page"
            || value["session_id"] != identity.session
            || value["agent_id"] != identity.agent
            || value["backend"] != identity.backend
            || value["model"] != identity.model
            || value["work_instance"] != work_instance
            || value["document"] != request.document.key()
            || value["offset"].as_u64() != Some(request.offset as u64)
            || request.offset > MAX_DOCUMENT_BYTES
            || request.offset != 0 && request.revision.is_none()
        {
            return Err(work::bad(
                "Context reply belongs to a different request or Work instance",
            ));
        }
        let status = match value["status"].as_str() {
            Some("page") => Status::Page,
            Some("empty") => Status::Empty,
            Some("missing") => Status::Missing,
            Some("oversize") => Status::Oversize,
            Some("changed") => Status::Changed,
            _ => return Err(work::bad("Unknown context page status")),
        };
        let text = value["text"]
            .as_str()
            .filter(|text| text.len() <= PAGE_BYTES)
            .ok_or_else(|| work::bad("Context page exceeds its UTF-8 byte budget or lacks text"))?;
        let mut page = Self {
            document: request.document.clone(),
            status,
            revision: None,
            total_bytes: None,
            offset: request.offset,
            next_offset: None,
            text: text.to_owned(),
        };

        if matches!(status, Status::Missing | Status::Oversize | Status::Changed) {
            if !(value["revision"].is_null()
                && value["total_bytes"].is_null()
                && value["next_offset"].is_null()
                && text.is_empty())
                || (status == Status::Changed) != request.revision.is_some()
            {
                return Err(work::bad("Inconsistent context document availability"));
            }
            return Ok(page);
        }

        let revision = work::hash(value, "revision")?;
        let total = value["total_bytes"]
            .as_u64()
            .filter(|&n| n <= MAX_DOCUMENT_BYTES as u64)
            .ok_or_else(|| work::bad("Invalid context document size"))?
            as usize;
        let end = request.offset + text.len();
        if request
            .revision
            .as_ref()
            .is_some_and(|expected| expected != &revision)
            || end > total
            || (status == Status::Empty) != (total == 0)
            || (status == Status::Empty) != text.is_empty()
        {
            return Err(work::bad(
                "Context document revision, size or content disagrees",
            ));
        }
        if end < total {
            if total - request.offset <= PAGE_BYTES
                || text.len() < PAGE_BYTES - 3
                || value["next_offset"].as_u64() != Some(end as u64)
            {
                return Err(work::bad(
                    "Context page has a gap or invalid UTF-8 boundary",
                ));
            }
            page.next_offset = Some(end);
        } else if !value["next_offset"].is_null() {
            return Err(work::bad("Final context page indicates more text"));
        } else if request.offset == 0
            && format!("{:x}", Sha256::digest(text.as_bytes())) != revision
        {
            return Err(work::bad(
                "Context document hash disagrees with its complete contents",
            ));
        }
        page.revision = Some(revision);
        page.total_bytes = Some(total);
        Ok(page)
    }
}
