//! Inspect configured voice devices and routes without probing audio or running a model.
use crate::{
    theme::Theme,
    work_lifecycle::{hash, keys},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind};
use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use std::{collections::BTreeMap, io};

pub const OPERATIONS: [&str; 4] = [
    "gui.voice.native_preferences",
    "gui.voice.native_review",
    "gui.voice.native_apply",
    "gui.voice.native_readback",
];
const FIELDS: [&str; 9] = [
    "stt_model",
    "stt_mic",
    "stt_hotkey",
    "tts_enabled",
    "tts_litesuite_first",
    "tts_engine",
    "tts_voice",
    "tts_edge_voice",
    "tts_timeout",
];
const EDITABLE: [&str; 3] = ["stt_model", "tts_engine", "tts_litesuite_first"];
fn bad() -> io::Error {
    io::Error::other("Voice preference response differs from its reviewed device settings")
}
fn choices(key: &str) -> Vec<Value> {
    match key {
        "stt_model" => vec![json!("tiny.en"), json!("base.en"), json!("small.en")],
        "tts_engine" => vec![json!("pyttsx3"), json!("edge")],
        "tts_litesuite_first" => vec![json!(false), json!(true)],
        _ => vec![],
    }
}
fn label(key: &str) -> &'static str {
    match key {
        "stt_model" => "Dictation model",
        "tts_engine" => "Fallback speech engine",
        "tts_litesuite_first" => "Prefer LiteSuite speech",
        _ => "Voice preference",
    }
}
fn verify_binding(v: &Value, id: &Identity, instance: &str, kind: &str) -> io::Result<()> {
    if v["kind"] != kind
        || v["scope"] != "device"
        || v["work_instance"] != instance
        || !id
            .request()
            .as_object()
            .expect("identity")
            .iter()
            .all(|(k, val)| v[k] == *val)
    {
        return Err(bad());
    }
    Ok(())
}
fn display(v: &Value, empty: &str) -> String {
    match v {
        Value::String(s) if s.is_empty() => empty.into(),
        Value::String(s) => s.clone(),
        Value::Bool(true) => "on".into(),
        Value::Bool(false) => "off".into(),
        _ => v.to_string(),
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
struct Field {
    saved: Value,
    effective: Value,
    editable: bool,
    overridden: bool,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Snapshot {
    fields: BTreeMap<String, Field>,
    revision: String,
    listen: bool,
}
impl Snapshot {
    fn parse(v: &Value, id: &Identity, instance: &str) -> io::Result<Self> {
        verify_binding(v, id, instance, "voice_preferences")?;
        keys(
            v,
            &[
                "version",
                "scope",
                "kind",
                "session_id",
                "agent_id",
                "backend",
                "model",
                "work_instance",
                "fields",
                "revision",
                "listen_registered",
            ],
        )?;
        keys(&v["fields"], &FIELDS)?;
        let mut fields = BTreeMap::new();
        for key in FIELDS {
            let raw = &v["fields"][key];
            keys(raw, &["saved", "effective", "editable", "overridden"])?;
            let saved = raw["saved"].clone();
            let effective = raw["effective"].clone();
            for value in [&saved, &effective] {
                let valid = match key {
                    "tts_enabled" | "tts_litesuite_first" => value.is_boolean(),
                    "tts_timeout" => value
                        .as_u64()
                        .is_some_and(|n| (1..=2_147_483_647).contains(&n)),
                    _ => value
                        .as_str()
                        .is_some_and(|s| s.len() <= 512 && !s.chars().any(crate::display_control)),
                };
                if !valid {
                    return Err(bad());
                }
            }
            let editable = raw["editable"].as_bool().ok_or_else(bad)?;
            let overridden = raw["overridden"].as_bool().ok_or_else(bad)?;
            if editable != (!overridden && saved == effective && choices(key).contains(&effective))
            {
                return Err(bad());
            }
            fields.insert(
                key.into(),
                Field {
                    saved,
                    effective,
                    editable,
                    overridden,
                },
            );
        }
        Ok(Self {
            fields,
            revision: hash(v, "revision")?,
            listen: v["listen_registered"].as_bool().ok_or_else(bad)?,
        })
    }
    fn shown(&self, key: &str, empty: &str) -> String {
        let field = &self.fields[key];
        let mut s = display(&field.effective, empty);
        if field.saved != field.effective {
            s.push_str(&format!(" (saved {})", display(&field.saved, empty)));
        }
        if field.overridden {
            s.push_str(" (override)");
        }
        s
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Edit {
    before: Snapshot,
    key: String,
    value: Value,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Review {
    edit: Edit,
    token: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Request {
    List,
    Review(Edit),
    Apply(Review),
    Readback(Review, String, String),
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Update {
    List(Snapshot),
    Review(Review),
    Saved(Snapshot),
}
pub enum Reply {
    Update(Update),
    Next(Request),
}
impl Request {
    pub fn command(&self, id: &Identity, instance: &str) -> io::Result<Value> {
        let mut cmd = id.request();
        cmd["work_instance"] = json!(instance);
        cmd["type"] = json!(match self {
            Self::List => OPERATIONS[0],
            Self::Review(e) => {
                let r = e
                    .before
                    .fields
                    .get(&e.key)
                    .filter(|r| r.editable)
                    .ok_or_else(bad)?;
                if r.effective == e.value || !choices(&e.key).contains(&e.value) {
                    return Err(bad());
                }
                cmd["revision"] = json!(e.before.revision);
                cmd["field"] = json!(e.key);
                cmd["value"] = e.value.clone();
                OPERATIONS[1]
            }
            Self::Apply(r) => {
                cmd["token"] = json!(r.token);
                OPERATIONS[2]
            }
            Self::Readback(r, id, _) => {
                cmd["token"] = json!(r.token);
                cmd["apply_id"] = json!(id);
                OPERATIONS[3]
            }
        });
        Ok(cmd)
    }
    pub fn response(
        self,
        v: &Value,
        id: &Identity,
        instance: &str,
        cmd_id: &str,
    ) -> io::Result<Reply> {
        let (kind, e, token, applied) = match &self {
            Self::List => {
                return Ok(Reply::Update(Update::List(Snapshot::parse(
                    v, id, instance,
                )?)));
            }
            Self::Review(e) => ("voice_review", e, None, None),
            Self::Apply(r) => ("voice_applied", &r.edit, Some(&r.token), Some(cmd_id)),
            Self::Readback(r, apply, _) => {
                ("voice_saved", &r.edit, Some(&r.token), Some(apply.as_str()))
            }
        };
        verify_binding(v, id, instance, kind)?;
        let mut members = vec![
            "kind",
            "version",
            "scope",
            "session_id",
            "agent_id",
            "backend",
            "model",
            "work_instance",
            "token",
            "field",
            "value",
            "before",
            "after",
            "apply_id",
        ];
        if kind == "voice_saved" {
            members.push("preferences")
        }
        keys(v, &members)?;
        let actual = hash(v, "token")?;
        if v["field"] != e.key
            || v["value"] != e.value
            || v["before"] != e.before.revision
            || token.is_some_and(|t| *t != actual)
            || v["apply_id"] != json!(applied)
        {
            return Err(bad());
        }
        match self {
            Self::List => unreachable!(),
            Self::Review(edit) if v["after"].is_null() => {
                Ok(Reply::Update(Update::Review(Review {
                    edit,
                    token: actual,
                })))
            }
            Self::Apply(review) => {
                let after = hash(v, "after")?;
                if after == review.edit.before.revision {
                    return Err(bad());
                }
                Ok(Reply::Next(Self::Readback(review, cmd_id.into(), after)))
            }
            Self::Readback(review, _, after) => {
                let actual = Snapshot::parse(&v["preferences"], id, instance)?;
                let edit = review.edit;
                let mut expected = edit.before;
                expected.revision = after.clone();
                let field = expected.fields.get_mut(&edit.key).ok_or_else(bad)?;
                field.saved = edit.value.clone();
                field.effective = edit.value;
                if actual != expected || v["after"] != after {
                    return Err(bad());
                }
                Ok(Reply::Update(Update::Saved(actual)))
            }
            _ => Err(bad()),
        }
    }
}

#[derive(Default)]
pub struct View {
    pub open: bool,
    waiting: bool,
    visible: bool,
    data: Option<Snapshot>,
    selected: usize,
    draft: Option<Value>,
    review: Option<Review>,
    note: String,
    scroll: usize,
    height: usize,
    lines: usize,
}
impl View {
    pub fn dictation_key(&self, key: KeyEvent) -> bool {
        self.open
            && self.visible
            && !self.waiting
            && self.review.is_none()
            && self.draft.is_none()
            && key.kind == KeyEventKind::Press
            && key.modifiers.is_empty()
            && matches!(key.code, KeyCode::Char('d' | 'D'))
    }
    pub fn open(&mut self) {
        *self = Self {
            open: true,
            waiting: true,
            ..Self::default()
        }
    }
    pub fn hide(&mut self) {
        self.visible = false;
    }
    pub fn update(&mut self, r: Result<Update, String>) {
        if !self.open || !self.waiting {
            return;
        }
        self.waiting = false;
        self.hide();
        self.scroll = 0;
        self.note.clear();
        match r {
            Ok(Update::List(s)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
            }
            Ok(Update::Review(r)) => self.review = Some(r),
            Ok(Update::Saved(s)) => {
                self.data = Some(s);
                self.review = None;
                self.draft = None;
                self.note = "Saved and verified on disk.".into();
            }
            Err(reason) => {
                self.data = None;
                self.review = None;
                self.draft = None;
                self.note = crate::live::terminal_text(&reason);
            }
        }
    }
    pub fn key(&mut self, key: KeyEvent) -> Option<Request> {
        if key.kind != KeyEventKind::Press || !key.modifiers.is_empty() || self.waiting {
            return None;
        }
        if key.code == KeyCode::Esc {
            if self.review.take().is_some() || self.draft.take().is_some() {
                self.draft = None;
                self.scroll = 0;
                self.hide()
            } else {
                *self = Self::default()
            }
            return None;
        }
        if matches!(key.code, KeyCode::PageDown | KeyCode::PageUp) {
            self.scroll = if key.code == KeyCode::PageDown {
                self.scroll
                    .saturating_add(self.height)
                    .min(self.lines.saturating_sub(self.height))
            } else {
                self.scroll.saturating_sub(self.height)
            };
            self.hide();
            return None;
        }
        let field = EDITABLE[self.selected];
        let req = match key.code {
            KeyCode::F(5) if self.review.is_none() => Request::List,
            KeyCode::Up | KeyCode::Down if self.review.is_none() => {
                self.selected = if key.code == KeyCode::Up {
                    self.selected.saturating_sub(1)
                } else {
                    (self.selected + 1).min(EDITABLE.len() - 1)
                };
                self.draft = None;
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Left | KeyCode::Right if self.review.is_none() => {
                let row = self.data.as_ref()?.fields.get(field)?;
                if !row.editable {
                    return None;
                }
                let options = choices(field);
                let old = self.draft.as_ref().unwrap_or(&row.effective);
                let pos = options.iter().position(|v| v == old)?;
                let step = if key.code == KeyCode::Right {
                    1
                } else {
                    options.len() - 1
                };
                self.draft = Some(options[(pos + step) % options.len()].clone());
                self.scroll = 0;
                self.hide();
                return None;
            }
            KeyCode::Enter if self.visible && self.review.is_some() => {
                Request::Apply(self.review.take()?)
            }
            KeyCode::Enter if self.visible => {
                let before = self.data.clone()?;
                let value = self.draft.clone()?;
                if value == before.fields[field].effective {
                    return None;
                }
                Request::Review(Edit {
                    before,
                    key: field.into(),
                    value,
                })
            }
            _ => return None,
        };
        self.waiting = true;
        self.scroll = 0;
        self.hide();
        Some(req)
    }

    pub fn draw(&mut self, frame: &mut Frame, area: Rect, theme: &Theme) {
        let mut lines = vec!["Voice preferences on this device".to_owned()];
        if let Some(review) = &self.review {
            lines.extend([
                format!("Save {}?", label(&review.edit.key)),
                format!(
                    "Current: {}",
                    review.edit.before.shown(&review.edit.key, "default")
                ),
                format!("Requested: {}", display(&review.edit.value, "default")),
                "Saving sets the preference for the next explicit voice operation.".into(),
                "Enter save   Esc cancel".into(),
            ]);
        } else if let Some(s) = &self.data {
            lines.extend([format!("Dictation (faster-whisper)   Model: {}",s.shown("stt_model","default")),
                format!("Microphone: {}   Python shortcut: {}",s.shown("stt_mic","first available"),s.shown("stt_hotkey","default")),
                format!("Python reply Speak button: {}   Prefer LiteSuite: {}",s.shown("tts_enabled","default"),s.shown("tts_litesuite_first","default")),
                format!("Fallback engine: {}   Local timeout: {} s",s.shown("tts_engine","default"),s.shown("tts_timeout","default")),
                format!("Windows voice: {}   Edge voice: {}",s.shown("tts_voice","Windows default"),s.shown("tts_edge_voice","default")),
                format!("Qwen2-Audio listen tool registered: {}. It handles audio separately from dictation.",s.listen),
                String::new()]);
            lines.push("D dictate to composer".into());
            let key = EDITABLE[self.selected];
            let row = &s.fields[key];
            lines.push(format!(
                "{}: {}",
                label(key),
                self.draft
                    .as_ref()
                    .map(|v| display(v, "default"))
                    .unwrap_or_else(|| s.shown(key, "default"))
            ));
            lines.push(if row.editable {
                "↑/↓ field   ←/→ choose   Enter review   F5 refresh   Esc back".into()
            } else {
                "Current field is read-only   ↑/↓ field   F5 refresh   Esc back".into()
            });
        }
        if !self.note.is_empty() {
            lines.push(self.note.clone())
        }
        if self.waiting {
            lines.push("Waiting for the source…".into())
        }
        if self.data.is_none() && !self.waiting && self.review.is_none() {
            lines.push("F5 refresh   Esc back".into())
        }
        let wrapped = crate::ui::wrap(&lines.join("\n"), usize::from(area.width.saturating_sub(2)));
        self.height = usize::from(area.height.saturating_sub(2));
        self.lines = wrapped.len();
        self.scroll = self.scroll.min(self.lines.saturating_sub(self.height));
        self.visible = area.width >= 36
            && self.height >= 5
            && (self.review.is_none() || self.lines <= self.height);
        let text = if area.width < 36 || self.height < 5 || self.review.is_some() && !self.visible {
            "Resize to see the full review. Esc back.".into()
        } else {
            wrapped
                .iter()
                .skip(self.scroll)
                .take(self.height)
                .cloned()
                .collect::<Vec<_>>()
                .join("\n")
        };
        frame.render_widget(Clear, area);
        frame.render_widget(
            Paragraph::new(text)
                .style(Style::default().fg(theme.foreground))
                .block(Block::bordered().title(" Voice ")),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (Identity, Value) {
        let id = Identity {
            session: "s".into(),
            agent: "a".into(),
            backend: "custom".into(),
            model: "m".into(),
        };
        let mut root = id.request();
        for (key, value) in [
            ("kind", json!("voice_preferences")),
            ("scope", json!("device")),
            ("work_instance", json!("i")),
            ("revision", json!("1".repeat(64))),
            ("listen_registered", json!(true)),
        ] {
            root[key] = value;
        }
        for (key, val) in [
            ("stt_model", json!("base.en")),
            ("stt_mic", json!("")),
            ("stt_hotkey", json!("ctrl+u")),
            ("tts_enabled", json!(false)),
            ("tts_litesuite_first", json!(true)),
            ("tts_engine", json!("pyttsx3")),
            ("tts_voice", json!("")),
            ("tts_edge_voice", json!("default")),
            ("tts_timeout", json!(300)),
        ] {
            root["fields"][key] = json!({"effective":val,"saved":val,"editable":EDITABLE.contains(&key),"overridden":false});
        }
        (id, root)
    }

    #[test]
    fn snapshot_checks_binding_edit_eligibility_and_renderable_values() {
        let (id, doc) = fixture();
        assert!(Snapshot::parse(&doc, &id, "i").is_ok());
        for (path, value) in [
            ("/work_instance", json!("old")),
            ("/version", json!(2)),
            ("/fields/stt_model/editable", json!(false)),
            ("/fields/tts_litesuite_first/saved", json!(1)),
            ("/fields/tts_voice/effective", json!("bad\u{202e}text")),
            ("/fields/tts_timeout/saved", json!(0)),
        ] {
            let mut drift = doc.clone();
            *drift.pointer_mut(path).unwrap() = value;
            assert!(Snapshot::parse(&drift, &id, "i").is_err(), "{path}");
        }
    }

    #[test]
    fn readback_rejects_substituted_setting_and_unrelated_effective_drift() {
        let (id, doc) = fixture();
        let before = Snapshot::parse(&doc, &id, "i").unwrap();
        let edit = Edit {
            before,
            key: "stt_model".into(),
            value: json!("small.en"),
        };
        let review = Review {
            edit,
            token: "a".repeat(64),
        };
        let after = "b".repeat(64);
        let mut saved = doc.clone();
        saved["revision"] = json!(after);
        saved["fields"]["stt_model"]["effective"] = json!("small.en");
        saved["fields"]["stt_model"]["saved"] = json!("small.en");
        let mut response = id.request();
        for (key, val) in [
            ("kind", json!("voice_saved")),
            ("scope", json!("device")),
            ("work_instance", json!("i")),
            ("token", json!(review.token)),
            ("field", json!("stt_model")),
            ("value", json!("small.en")),
            ("before", json!(review.edit.before.revision)),
            ("after", json!(after)),
            ("apply_id", json!("apply")),
            ("preferences", saved),
        ] {
            response[key] = val;
        }
        let request = Request::Readback(review, "apply".into(), after);
        assert!(matches!(
            request.clone().response(&response, &id, "i", "readback"),
            Ok(Reply::Update(Update::Saved(_)))
        ));
        for (path, val) in [
            ("/field", json!("tts_engine")),
            ("/apply_id", json!("replay")),
            (
                "/preferences/fields/tts_voice/effective",
                json!("different voice"),
            ),
        ] {
            let mut drift = response.clone();
            *drift.pointer_mut(path).unwrap() = val;
            assert!(
                request
                    .clone()
                    .response(&drift, &id, "i", "readback")
                    .is_err(),
                "{path}"
            );
        }
    }
}
