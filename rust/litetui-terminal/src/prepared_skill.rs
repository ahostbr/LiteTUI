//! Lossless staged prompt storage. Rendering escapes text; storage never normalizes it.
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::io;
pub const MAX_BYTES: usize = 65536;
fn fail(s: &str) -> io::Error {
    io::Error::other(s.to_owned())
}
pub fn digest(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}
#[derive(Clone, Debug, PartialEq)]
pub struct Prepared {
    pub session: String,
    pub agent: String,
    pub backend: String,
    pub model: String,
    pub snapshot: String,
    pub row: String,
    pub arguments: String,
    pub body: String,
    pub prompt: String,
    pub digest: String,
    pub name: String,
    pub source: String,
    pub path: String,
    pub file: Value,
}
impl Prepared {
    pub fn parse(
        v: &Value,
        catalog: &crate::command_palette::Catalog,
        row: &str,
        arguments: &str,
    ) -> io::Result<Self> {
        let text = |v: &Value, key: &str, max: usize| -> io::Result<String> {
            v[key]
                .as_str()
                .filter(|s| s.len() <= max)
                .map(str::to_owned)
                .ok_or_else(|| fail("Invalid prepared skill text"))
        };
        if serde_json::to_vec(v)?.len() > 1024 * 1024
            || v["version"] != 1
            || v["kind"] != "skill_prompt"
            || v["session_id"] != catalog.session
            || v["agent_id"] != catalog.agent
            || v["backend"] != catalog.backend
            || v["model"] != catalog.model
            || v["snapshot"] != catalog.snapshot
            || v["row_id"] != row
            || v["arguments"] != arguments
        {
            return Err(fail("Prepared skill identity or request changed"));
        }
        let entry = catalog
            .entries
            .iter()
            .find(|e| e.id == row && e.kind == "skill")
            .ok_or_else(|| fail("Prepared skill is not the selected source row"))?;
        let body = text(v, "body", MAX_BYTES)?;
        let prompt = text(v, "prompt", MAX_BYTES)?;
        if body.is_empty()
            || prompt.trim().is_empty()
            || prompt
                != if arguments.is_empty() {
                    body.clone()
                } else {
                    format!("{body}\n\n---\n\n{arguments}")
                }
        {
            return Err(fail(
                "Prepared skill payload does not preserve exact body and arguments",
            ));
        }
        let hash = digest(&prompt);
        if v["prompt_sha256"] != hash {
            return Err(fail("Prepared skill prompt digest mismatch"));
        }
        let file = &v["file"];
        crate::command_palette::token(file, "sha256")?;
        if file["bytes"].as_u64().is_none_or(|n| n > MAX_BYTES as u64)
            || !(file["mtime_ns"].is_null() || file["mtime_ns"].as_i64().is_some())
            || ["device", "inode"]
                .iter()
                .any(|k| !(file[*k].is_null() || file[*k].as_u64().is_some()))
        {
            return Err(fail("Invalid prepared skill file provenance"));
        }
        text(file, "resolved_path", 4096)?;
        let name = text(&v["skill"], "name", 128)?;
        let source = text(&v["skill"], "source", 256)?;
        let path = text(&v["skill"], "path", 4096)?;
        if name != entry.title || entry.source.as_ref() != Some(&source) || path.is_empty() {
            return Err(fail("Prepared skill metadata differs from selected row"));
        }
        Ok(Self {
            session: catalog.session.clone(),
            agent: catalog.agent.clone(),
            backend: catalog.backend.clone(),
            model: catalog.model.clone(),
            snapshot: catalog.snapshot.clone(),
            row: row.into(),
            arguments: arguments.into(),
            body,
            prompt,
            digest: hash,
            name,
            source,
            path,
            file: file.clone(),
        })
    }
}
pub type Draft = BoundedDraft<MAX_BYTES>;
#[derive(Default)]
pub struct BoundedDraft<const LIMIT: usize> {
    text: String,
    cursor: usize,
}
impl<const LIMIT: usize> BoundedDraft<LIMIT> {
    pub fn text(&self) -> &str {
        &self.text
    }
    pub fn set(&mut self, text: &str) -> io::Result<()> {
        if text.len() > LIMIT {
            return Err(fail("Document exceeds the editor limit; draft kept unchanged"));
        }
        self.text = text.into();
        self.cursor = self.text.len();
        Ok(())
    }
    pub fn insert(&mut self, text: &str) -> io::Result<()> {
        if self.text.len().saturating_add(text.len()) > LIMIT {
            return Err(fail("Document exceeds the editor limit; draft kept unchanged"));
        }
        self.text.insert_str(self.cursor, text);
        self.cursor += text.len();
        Ok(())
    }
    pub fn key(&mut self, key: crossterm::event::KeyCode) {
        use crossterm::event::KeyCode;
        match key {
            KeyCode::Left => {
                self.cursor = self.text[..self.cursor]
                    .char_indices()
                    .next_back()
                    .map_or(0, |(i, _)| i)
            }
            KeyCode::Right => {
                self.cursor += self.text[self.cursor..]
                    .chars()
                    .next()
                    .map_or(0, char::len_utf8)
            }
            KeyCode::Home => self.cursor = 0,
            KeyCode::End => self.cursor = self.text.len(),
            KeyCode::Backspace if self.cursor > 0 => {
                let before = self.cursor;
                self.key(KeyCode::Left);
                self.text.drain(self.cursor..before);
            }
            KeyCode::Delete if self.cursor < self.text.len() => {
                let after =
                    self.cursor + self.text[self.cursor..].chars().next().unwrap().len_utf8();
                self.text.drain(self.cursor..after);
            }
            _ => {}
        }
    }
    pub fn cursor(&self) -> usize {
        self.cursor
    }
}
