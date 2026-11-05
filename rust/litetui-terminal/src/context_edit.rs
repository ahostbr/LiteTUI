//! Bound review, one publication, and an independent source readback for current-agent memory.
use crate::{
    context::{Document, MAX_DOCUMENT_BYTES},
    work_lifecycle as life,
    work_protocol::Identity,
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const OPEN: &str = "gui.context.native_edit";
pub const REVIEW: &str = "gui.context.native_review";
pub const APPLY: &str = "gui.context.native_apply";
pub const READBACK: &str = "gui.context.native_readback";
pub const OPERATIONS: [&str; 4] = [OPEN, REVIEW, APPLY, READBACK];
pub const TOPIC_OPERATIONS: [&str; 4] = [
    "gui.context.native_topic_edit",
    "gui.context.native_topic_review",
    "gui.context.native_topic_apply",
    "gui.context.native_topic_readback",
];

pub const SYSTEM_OPERATIONS: [&str; 4] = [
    "gui.context.native_system_edit",
    "gui.context.native_system_review",
    "gui.context.native_system_apply",
    "gui.context.native_system_readback",
];
const MAX_EFFECT_BYTES: usize = 1024;

/// What the source says about the system prompt: when the saved text reaches the
/// model (per backend) and where the live text is kept.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SystemFacts {
    pub effect: String,
    /// `saved`, `session_only` (no conversation file yet) or `diverged` (file differs).
    pub stored: String,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Base {
    pub document: Document,
    pub revision: Option<String>,
    pub text: String,
    pub system: Option<SystemFacts>,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    pub base: Base,
    pub draft: String,
    pub token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    Open(Document, Option<String>),
    Review(Base, String),
    Apply(Review),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Reply {
    Opened(Base),
    Reviewed(Review),
    Saved(Base),
}

pub fn editable(document: &Document) -> bool {
    matches!(
        document,
        Document::System
            | Document::Memory
            | Document::Soul
            | Document::Handoff
            | Document::Topic(_)
    )
}
fn digest(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

impl Request {
    pub fn document(&self) -> &Document {
        match self {
            Self::Open(doc, _) => doc,
            Self::Review(base, _) => &base.document,
            Self::Apply(r) => &r.base.document,
        }
    }
    pub fn operation(&self) -> &'static str {
        let ops = self.operations();
        match self {
            Self::Open(..) => ops[0],
            Self::Review(..) => ops[1],
            Self::Apply(_) => ops[2],
        }
    }
    pub fn operations(&self) -> &'static [&'static str; 4] {
        if matches!(self.document(), Document::Topic(_)) {
            &TOPIC_OPERATIONS
        } else if *self.document() == Document::System {
            &SYSTEM_OPERATIONS
        } else {
            &OPERATIONS
        }
    }
    pub fn command(&self, identity: &Identity, instance: &str) -> io::Result<Value> {
        if !editable(self.document()) {
            return Err(life::bad(
                "Choose the system prompt, memory, soul or handoff to edit",
            ));
        }
        let mut v = identity.request();
        v["type"] = json!(self.operation());
        v["work_instance"] = json!(instance);
        v["document"] = json!(self.document().key());
        match self {
            Self::Open(_, revision) => v["revision"] = json!(revision),
            Self::Review(base, draft) => {
                if draft.len() > MAX_DOCUMENT_BYTES {
                    return Err(life::bad("Draft exceeds 256 KiB"));
                }
                if *self.document() == Document::System
                    && (draft.trim().is_empty() || draft.contains('\0'))
                {
                    return Err(life::bad("System prompt must be nonempty text without NUL"));
                }
                v["revision"] = json!(base.revision);
                v["text"] = json!(draft);
            }
            Self::Apply(r) => v["token"] = json!(r.token),
        }
        life::bounded(&v, life::REPLY_BYTES - 512)?;
        Ok(v)
    }
    fn binding(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        kind: &str,
        extra: &[&str],
    ) -> io::Result<()> {
        let common = [
            "version",
            "kind",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "document",
        ];
        life::keys(v, &[common.as_slice(), extra].concat())?;
        if v["kind"] != kind
            || v["version"].as_u64() != Some(1)
            || v["session_id"] != id.session
            || v["agent_id"] != id.agent
            || v["backend"] != id.backend
            || v["model"] != id.model
            || v["work_instance"] != instance
            || v["document"] != self.document().key()
        {
            return Err(life::bad("Memory edit reply belongs to another request"));
        }
        Ok(())
    }
    fn system(&self) -> bool {
        *self.document() == Document::System
    }
    fn stored(v: &Value, allow_diverged: bool) -> io::Result<String> {
        match v["stored"].as_str() {
            Some(s @ ("saved" | "session_only")) => Ok(s.into()),
            Some("diverged") if allow_diverged => Ok("diverged".into()),
            _ => Err(life::bad("Invalid system prompt persistence state")),
        }
    }
    fn facts(v: &Value, allow_diverged: bool) -> io::Result<SystemFacts> {
        let effect = v["effect"]
            .as_str()
            .filter(|s| !s.is_empty() && s.len() <= MAX_EFFECT_BYTES)
            .ok_or_else(|| life::bad("Invalid system prompt effect text"))?;
        Ok(SystemFacts {
            effect: effect.into(),
            stored: Self::stored(v, allow_diverged)?,
        })
    }
    fn publication(&self, v: &Value, apply_id: &str) -> io::Result<&Review> {
        let Self::Apply(r) = self else {
            return Err(life::bad("Memory application requires a review"));
        };
        if v["token"] != r.token
            || v["apply_id"] != apply_id
            || v["revision"] != digest(&r.draft)
            || v["bytes"].as_u64() != Some(r.draft.len() as u64)
        {
            return Err(life::bad(
                "Publication differs from the reviewed memory draft",
            ));
        }
        Ok(r)
    }
    pub fn reply(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        apply_id: Option<&str>,
    ) -> io::Result<Reply> {
        let (kind, mut extra): (_, Vec<&str>) = match self {
            Self::Open(..) => ("context_edit", vec!["revision", "text"]),
            Self::Review(..) => (
                "context_review",
                vec![
                    "token",
                    "before_revision",
                    "before_bytes",
                    "after_revision",
                    "after_bytes",
                ],
            ),
            Self::Apply(_) => (
                "context_saved",
                vec!["token", "apply_id", "revision", "bytes"],
            ),
        };
        if self.system() {
            extra.push("effect");
            if !matches!(self, Self::Review(..)) {
                extra.push("stored");
            }
        }
        self.binding(v, id, instance, kind, &extra)?;
        match self {
            Self::Open(document, revision) => {
                let text = v["text"]
                    .as_str()
                    .filter(|s| s.len() <= MAX_DOCUMENT_BYTES)
                    .ok_or_else(|| life::bad("Invalid memory draft text"))?;
                if v["revision"] != json!(revision)
                    || revision
                        .as_ref()
                        .map_or(!text.is_empty(), |r| *r != digest(text))
                {
                    return Err(life::bad(
                        "Opened text differs from the selected memory revision",
                    ));
                }
                Ok(Reply::Opened(Base {
                    document: document.clone(),
                    revision: revision.clone(),
                    text: text.into(),
                    system: if self.system() {
                        Some(Self::facts(v, true)?)
                    } else {
                        None
                    },
                }))
            }
            Self::Review(base, draft) => {
                if v["before_revision"] != json!(base.revision)
                    || v["before_bytes"] != json!(base.revision.as_ref().map(|_| base.text.len()))
                    || v["after_revision"] != digest(draft)
                    || v["after_bytes"].as_u64() != Some(draft.len() as u64)
                {
                    return Err(life::bad(
                        "Review changed the original document or the proposed text",
                    ));
                }
                if base
                    .system
                    .as_ref()
                    .is_some_and(|facts| v["effect"] != facts.effect)
                {
                    return Err(life::bad(
                        "The system prompt effect changed since it was opened",
                    ));
                }
                Ok(Reply::Reviewed(Review {
                    base: base.clone(),
                    draft: draft.clone(),
                    token: life::hash(v, "token")?,
                }))
            }
            Self::Apply(_) => {
                let r = self.publication(
                    v,
                    apply_id.ok_or_else(|| life::bad("Missing memory application ID"))?,
                )?;
                Ok(Reply::Saved(Base {
                    document: r.base.document.clone(),
                    revision: Some(digest(&r.draft)),
                    text: r.draft.clone(),
                    system: if self.system() {
                        Some(Self::facts(v, false)?)
                    } else {
                        None
                    },
                }))
            }
        }
    }
    pub fn readback(
        &self,
        v: &Value,
        id: &Identity,
        instance: &str,
        apply_id: &str,
    ) -> io::Result<Value> {
        let mut extra = vec!["token", "apply_id", "revision", "bytes"];
        if self.system() {
            extra.push("stored");
        }
        self.binding(v, id, instance, "context_apply", &extra)?;
        if self.system() {
            Self::stored(v, false)?;
        }
        self.publication(v, apply_id)?;
        let mut command = self.command(id, instance)?;
        command["type"] = json!(self.operations()[3]);
        command["apply_id"] = json!(apply_id);
        Ok(command)
    }
}
