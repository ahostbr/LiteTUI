//! Reviewed composer text captured by the current host stream and durable Work ledger.
use crate::{
    tool_mode::Profile,
    work_lifecycle::{Input, Origin, Producer, ProducerKind},
    work_protocol::Identity,
};
use crossterm::event::{KeyCode, KeyEvent};
use ratatui::{
    Frame,
    layout::{Margin, Rect},
    widgets::{Block, Clear, Paragraph},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::io;

pub const REVIEW: &str = "gui.prompt.native_queue_review";
pub const APPLY: &str = "gui.prompt.native_queue_apply";
pub const SEND_REVIEW: &str = "gui.prompt.native_send_review";
pub const SEND_APPLY: &str = "gui.prompt.native_send_apply";
pub const MAX_TEXT: usize = 16384;
#[derive(Clone, Debug, PartialEq)]
pub struct Draft {
    pub text: String,
    pub alternate: bool,
    pub operation: String,
}
impl Draft {
    fn chord(&self) -> &'static str {
        if self.alternate { "alternate" } else { "enter" }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub struct Review {
    pub draft: Draft,
    pub origin: Origin,
    pub profile: Profile,
    pub generation: u64,
    pub id: String,
    pub payload_hash: String,
    pub prior_inputs: Option<Vec<String>>,
    native: Option<Value>,
    wire: Value,
}
#[derive(Clone, Debug, PartialEq)]
pub struct Receipt {
    pub draft: Draft,
    pub producer: Producer,
    pub input: Input,
}
#[derive(Clone, Debug, PartialEq)]
pub enum Request {
    Review(Draft),
    Apply(Box<Review>),
}
#[derive(Clone, Debug, PartialEq)]
pub enum Reply {
    Review(Box<Review>),
    Queued(Box<Receipt>),
}
impl Reply {
    pub(crate) fn interrupted(&self) -> Option<&str> {
        match self {
            Self::Queued(r) if r.producer.kind == ProducerKind::UserInterrupt => {
                Some(&r.draft.operation)
            }
            _ => None,
        }
    }
}
fn bad() -> io::Error {
    io::Error::other("Queued input differs from the reviewed foreground turn, text or policy")
}
impl Request {
    pub fn operation(&self) -> &'static str {
        match self {
            Self::Review(_) => REVIEW,
            Self::Apply(_) => APPLY,
        }
    }
    pub fn send_operation(&self) -> &'static str {
        match self {
            Self::Review(_) => SEND_REVIEW,
            Self::Apply(_) => SEND_APPLY,
        }
    }
    pub fn interrupts(&self) -> bool {
        matches!(self,Self::Apply(r) if r.prior_inputs.is_some())
    }
    pub fn draft(&self) -> &Draft {
        match self {
            Self::Review(d) => d,
            Self::Apply(r) => &r.draft,
        }
    }
    pub fn command(
        &self,
        identity: &Identity,
        instance: &str,
        origin: &Origin,
    ) -> io::Result<Value> {
        let d = self.draft();
        if origin.operation != d.operation
            || origin.session != identity.session
            || crate::stream::required_uuid4(&origin.value(), "hook_id").is_err()
            || origin.source != "rpc"
            || d.text.len() > MAX_TEXT
            || d.text.contains('\0')
            || d.text.trim().is_empty()
            || d.text.trim_start().starts_with('/')
        {
            return Err(bad());
        }
        let mut v = json!({"version":1,"type":self.operation(),"session_id":identity.session,
            "agent_id":identity.agent,"backend":identity.backend,"model":identity.model,"work_instance":instance,
            "origin":origin.value(),"chord":d.chord(),"text":d.text});
        if let Self::Apply(r) = self {
            if &r.origin != origin {
                return Err(bad());
            }
            v["review"] = json!(r.id);
            if let Some(native) = &r.native {
                v["native"] = native.clone();
            }
        }
        Ok(v)
    }
    pub fn parse(
        &self,
        identity: &Identity,
        instance: &str,
        origin: &Origin,
        id: &str,
        verified_policy: (u64, Profile),
        v: &Value,
    ) -> io::Result<Reply> {
        let profile = Profile::parse(v["profile"].as_str().ok_or_else(bad)?)?;
        let draft = self.draft();
        let mut expected = self.command(identity, instance, origin)?;
        expected.as_object_mut().unwrap().remove("type");
        let payload = json!({"content":draft.text,"text":draft.text,"source":"rpc","tool_profile":profile.as_str()});
        let payload_hash = format!("{:x}", Sha256::digest(serde_json::to_vec(&payload)?));
        let review_id = crate::stream::required_uuid4(v, "review")
            .map_err(|_| bad())?
            .to_owned();
        let generation = v["policy_epoch"]
            .as_u64()
            .filter(|g| *g > 0)
            .ok_or_else(bad)?;
        if (generation, profile) != verified_policy {
            return Err(bad());
        }
        expected["review"] = json!(review_id);
        expected["kind"] = json!("midturn_review");
        let native = if identity.backend == "claude" {
            let n = &v["native"];
            let valid_hex = |v: &Value| {
                v.as_str().is_some_and(|s| {
                    s.len() == 32
                        && s.bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                })
            };
            if n.as_object().map(|o| o.len()) != Some(5)
                || n["provider"] != "claude"
                || n["generation"]
                    .as_u64()
                    .filter(|n| *n > 0 && *n <= 9007199254740991)
                    .is_none()
                || crate::stream::required_uuid4(n, "native_session").is_err()
                || !valid_hex(&n["segment"])
                || !valid_hex(&n["active_input"])
                || v["behavior"] != "queue"
            {
                return Err(bad());
            }
            expected["native"] = n.clone();
            Some(n.clone())
        } else {
            None
        };
        let prior_inputs = match v["behavior"].as_str() {
            Some("queue") => None,
            Some("interrupt") => {
                let prior = v["prior_inputs"]
                    .as_array()
                    .filter(|items| items.len() <= 256)
                    .ok_or_else(bad)?;
                let mut ids = Vec::with_capacity(prior.len());
                for id in prior {
                    let id = crate::work_lifecycle::uuid(&json!({"id":id}), "id")?;
                    if ids.contains(&id) {
                        return Err(bad());
                    }
                    ids.push(id);
                }
                expected["prior_inputs"] = json!(ids);
                Some(ids)
            }
            _ => return Err(bad()),
        };
        expected["behavior"] = json!(if prior_inputs.is_some() {
            "interrupt"
        } else {
            "queue"
        });
        expected["enter_interrupts"] = json!(draft.alternate != prior_inputs.is_some());
        expected["profile"] = json!(profile.as_str());
        expected["policy_epoch"] = json!(generation);
        expected["payload_sha256"] = json!(payload_hash);
        match self {
            Self::Review(d) if expected == *v => Ok(Reply::Review(Box::new(Review {
                draft: d.clone(),
                origin: origin.clone(),
                profile,
                generation,
                id: review_id,
                payload_hash,
                prior_inputs,
                native,
                wire: expected,
            }))),
            Self::Apply(r) if r.wire == expected => {
                let producer = Producer::parse(&v["producer"])?;
                let input = Input::parse(&v["input"])?;
                expected["kind"] = json!("midturn_queued");
                expected["operation_id"] = json!(id);
                expected["producer"] = v["producer"].clone();
                expected["input"] = v["input"].clone();
                if let Some(native) = &r.native {
                    let d = &v["native_delivery"];
                    let ident = d["id"].as_str().ok_or_else(bad)?;
                    if ident.len() != 32
                        || !ident
                            .bytes()
                            .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
                        || d != &json!({"id":ident,"segment_id":native["segment"],"state":"prepared","operation_id":r.id})
                    {
                        return Err(bad());
                    }
                    expected["native_delivery"] = d.clone();
                }

                if expected != *v
                    || producer.kind
                        != if r.prior_inputs.is_some() {
                            ProducerKind::UserInterrupt
                        } else {
                            ProducerKind::UserQueue
                        }
                    || producer.object != r.id
                    || producer.owner != instance
                    || producer.session != identity.session
                    || producer.generation != 1
                    || producer.owner_created.is_some()
                    || producer.action.is_some()
                    || producer.operation.as_ref() != Some(&origin.operation)
                    || producer.execution.as_ref() != Some(&origin.execution)
                    || input.producer != producer.id
                    || input.session != identity.session
                    || input.payload_sha256 != r.payload_hash
                    || input.source != "rpc"
                    || input.requested != r.profile
                {
                    return Err(bad());
                }
                Ok(Reply::Queued(Box::new(Receipt {
                    draft: r.draft.clone(),
                    producer,
                    input,
                })))
            }
            _ => Err(bad()),
        }
    }
}
#[derive(Default)]
pub struct View {
    pub open: bool,
    review: Option<Review>,
    notice: String,
    page: usize,
    pages: usize,
    seen: Vec<bool>,
    pub queued: Option<Draft>,
    geometry: Option<(u16, u16)>,
}
impl View {
    pub fn begin(&mut self) {
        *self = Self {
            open: true,
            notice: "Checking the current turn and send key…".into(),
            ..Self::default()
        };
    }
    pub fn reply(&mut self, reply: Reply) {
        match reply {
            Reply::Review(r) => {
                self.review = Some(*r);
                self.page = 0;
                self.seen.clear();
            }
            Reply::Queued(r) => {
                self.queued = Some(r.draft);
                self.open = false;
                self.review = None;
            }
        }
    }
    pub fn reject(&mut self, reason: String) {
        self.notice = reason;
        self.review = None;
    }
    pub fn key(&mut self, key: KeyEvent, waiting: bool) -> Option<Request> {
        if waiting || !key.modifiers.is_empty() {
            return None;
        }
        match key.code {
            KeyCode::Esc => {
                self.open = false;
                self.review = None;
            }
            KeyCode::PageDown | KeyCode::Right => {
                self.page = (self.page + 1).min(self.pages.saturating_sub(1))
            }
            KeyCode::PageUp | KeyCode::Left => self.page = self.page.saturating_sub(1),
            KeyCode::Enter | KeyCode::Char('i' | 'I')
                if !self.seen.is_empty()
                    && self.seen.iter().all(|v| *v)
                    && self.review.as_ref().is_some_and(|r| {
                        r.prior_inputs.is_some() == matches!(key.code, KeyCode::Char('i' | 'I'))
                    }) =>
            {
                return self.review.clone().map(|r| Request::Apply(Box::new(r)));
            }
            _ => {}
        }
        None
    }
    pub fn draw(&mut self, f: &mut Frame, area: Rect, waiting: bool) {
        f.render_widget(Clear, area);
        let body = area.inner(Margin::new(1, 1));
        if body.width < 12 || body.height < 4 {
            self.seen.clear();
            return;
        }
        let content = if let Some(r) = &self.review {
            let effect = if let Some(prior) = &r.prior_inputs {
                format!(
                    "Interrupt the current reply. Keep its partial output and earlier tool effects. Send this draft first after the worker stops, ahead of {} queued message(s).",
                    prior.len()
                )
            } else if let Some(native) = &r.native {
                format!(
                    "Queue this message for the next Claude turn, after the current reply finishes. This does not interrupt or steer the running reply.\nClaude session: {}\nSegment: {}",
                    native["native_session"].as_str().unwrap_or_default(),
                    native["segment"].as_str().unwrap_or_default()
                )
            } else {
                "Queue this message at the next safe boundary of the running reply, or as the next turn.".into()
            };
            format!(
                "{effect}\nTool profile: {}\n\n{}",
                r.profile.as_str(),
                crate::live::terminal_text(&r.draft.text)
            )
        } else {
            crate::live::terminal_text(&self.notice)
        };
        let lines = crate::ui::wrap(&content, body.width as usize);
        let height = usize::from(body.height - 1);
        self.pages = lines.len().div_ceil(height).max(1);
        let geometry = (body.width, body.height);
        if self.geometry != Some(geometry) || self.seen.len() != self.pages {
            self.geometry = Some(geometry);
            self.seen = vec![false; self.pages];
        }
        self.page = self.page.min(self.pages - 1);
        self.seen[self.page] = true;
        let footer = if waiting {
            "Waiting for source confirmation".to_owned()
        } else if let Some(r) = &self.review {
            let confirm = if r.prior_inputs.is_some() {
                "I interrupt and send next"
            } else {
                "Enter queue"
            };
            format!(
                "PgUp/PgDn  {}/{}   {confirm}   Esc cancel",
                self.page + 1,
                self.pages
            )
        } else {
            "Esc return to draft".into()
        };
        f.render_widget(
            Block::bordered().title(
                if self
                    .review
                    .as_ref()
                    .is_some_and(|r| r.prior_inputs.is_some())
                {
                    " Interrupt reply "
                } else {
                    " Queue composer message "
                },
            ),
            area,
        );
        f.render_widget(
            Paragraph::new(
                lines
                    .iter()
                    .skip(self.page * height)
                    .take(height)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join("\n"),
            ),
            body,
        );
        f.render_widget(
            Paragraph::new(footer),
            Rect::new(body.x, body.bottom() - 1, body.width, 1),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crossterm::event::KeyModifiers;
    use ratatui::{Terminal, backend::TestBackend};
    const HOOK: &str = "3aab1c4f-4e72-422c-88f2-d770df1eb65f";
    #[test]
    fn claude_queue_requires_native_owner_and_prepared_delivery_and_never_interrupts() {
        let identity = Identity {
            session: HOOK.into(),
            agent: "agent".into(),
            backend: "claude".into(),
            model: "fixture".into(),
        };
        let origin = Origin {
            session: HOOK.into(),
            operation: "op".into(),
            hook: Some(HOOK.into()),
            source: "rpc".into(),
            execution: HOOK.into(),
        };
        let draft = Draft {
            text: "Next Claude input 雪".into(),
            alternate: false,
            operation: "op".into(),
        };
        let request = Request::Review(draft.clone());
        let payload =
            json!({"content":draft.text,"text":draft.text,"source":"rpc","tool_profile":"strict"});
        let native = json!({"provider":"claude","generation":1,"native_session":HOOK,"segment":"a".repeat(32),"active_input":"b".repeat(32)});
        let good = json!({"version":1,"kind":"midturn_review","session_id":HOOK,"agent_id":"agent","backend":"claude","model":"fixture","work_instance":&"a".repeat(64),
            "origin":origin.value(),"chord":"enter","text":draft.text,"behavior":"queue","enter_interrupts":false,
            "profile":"strict","policy_epoch":1,"payload_sha256":format!("{:x}",Sha256::digest(serde_json::to_vec(&payload).unwrap())),"review":HOOK,"native":native});
        let parse = |v: &Value| {
            request.parse(
                &identity,
                &"a".repeat(64),
                &origin,
                "review",
                (1, Profile::Strict),
                v,
            )
        };
        for (key, value) in [
            ("provider", json!("codex")),
            ("generation", json!(0)),
            ("native_session", json!("foreign")),
            ("segment", json!("wrong")),
            ("active_input", Value::Null),
        ] {
            let mut v = good.clone();
            v["native"][key] = value;
            assert!(parse(&v).is_err(), "{key}");
        }
        let mut interrupt = good.clone();
        interrupt["behavior"] = json!("interrupt");
        interrupt["prior_inputs"] = json!([]);
        assert!(parse(&interrupt).is_err());
        let Reply::Review(review) = parse(&good).unwrap() else {
            panic!("review")
        };
        let apply = Request::Apply(review.clone());
        assert_eq!(
            apply.command(&identity, &"a".repeat(64), &origin).unwrap()["native"],
            native
        );
        let mut queued = good.clone();
        queued["kind"] = json!("midturn_queued");
        queued["operation_id"] = json!("apply");
        queued["producer"] = json!({"producer_id":HOOK,"kind":"user_queue","object_id":HOOK,"producer_session_id":HOOK,"owner_instance":&"a".repeat(64),"owner_created":null,"generation":1,"causal_action_id":null,"causal_operation_id":"op","causal_execution_id":HOOK});
        queued["input"] = json!({"input_id":HOOK,"producer_id":HOOK,"payload_sha256":good["payload_sha256"],"source":"rpc","requested_profile":"strict","delivery_session_id":HOOK,"queue_revision":1});
        queued["native_delivery"] = json!({"id":"c".repeat(32),"segment_id":native["segment"],"state":"prepared","operation_id":HOOK});
        let accepted = |v: &Value| {
            apply.parse(
                &identity,
                &"a".repeat(64),
                &origin,
                "apply",
                (1, Profile::Strict),
                v,
            )
        };
        assert!(accepted(&queued).is_ok());
        for (key, value) in [
            ("state", json!("submitted")),
            ("operation_id", json!("foreign")),
            ("segment_id", json!("d".repeat(32))),
            ("id", json!("not-native")),
        ] {
            let mut v = queued.clone();
            v["native_delivery"][key] = value;
            assert!(accepted(&v).is_err(), "{key}");
        }
        let mut view = View::default();
        view.begin();
        view.reply(Reply::Review(review));
        let mut terminal = Terminal::new(TestBackend::new(120, 30)).unwrap();
        terminal.draw(|f| view.draw(f, f.area(), false)).unwrap();
        let text = terminal
            .backend()
            .buffer()
            .content
            .iter()
            .map(|c| c.symbol())
            .collect::<String>();
        assert!(text.contains("next Claude turn") && text.contains("does not interrupt"));
        assert!(
            view.key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE), false)
                .is_none()
        );
        assert!(
            view.key(KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE), false)
                .is_some()
        );
    }

    #[test]
    fn review_rejects_changed_owner_chord_text_scope_and_policy_and_requires_every_painted_page() {
        let identity = Identity {
            session: "session".into(),
            agent: "agent".into(),
            backend: "custom".into(),
            model: "fixture".into(),
        };
        let origin = Origin {
            session: "session".into(),
            operation: "op".into(),
            hook: Some(HOOK.into()),
            source: "rpc".into(),
            execution: "execution".into(),
        };
        let draft = Draft {
            text: "line of text\n".repeat(15),
            alternate: true,
            operation: "op".into(),
        };
        let request = Request::Review(draft.clone());
        let payload =
            json!({"content":draft.text,"text":draft.text,"source":"rpc","tool_profile":"strict"});
        let sha = format!(
            "{:x}",
            Sha256::digest(serde_json::to_vec(&payload).unwrap())
        );
        let good = json!({"version":1,"kind":"midturn_review","session_id":"session","agent_id":"agent","backend":"custom","model":"fixture","work_instance":"instance",
            "origin":origin.value(),"chord":"alternate","text":draft.text,"behavior":"queue","enter_interrupts":true,
            "profile":"strict","policy_epoch":1,"payload_sha256":sha,"review":HOOK});
        let parse = |v: &Value| {
            request.parse(
                &identity,
                "instance",
                &origin,
                "op",
                (1, Profile::Strict),
                v,
            )
        };
        for (key, value) in [
            ("text", json!("different")),
            ("chord", json!("enter")),
            ("origin", Value::Null),
            ("work_instance", json!("other")),
            ("profile", json!("autonomous")),
            ("policy_epoch", json!(2)),
            ("behavior", json!("interrupt")),
            ("enter_interrupts", json!(false)),
            ("payload_sha256", json!("wrong")),
        ] {
            let mut forged = good.clone();
            forged[key] = value;
            assert!(parse(&forged).is_err(), "{key}");
        }
        let mut interrupt = good.clone();
        interrupt["behavior"] = json!("interrupt");
        interrupt["enter_interrupts"] = json!(false);
        interrupt["prior_inputs"] = json!([HOOK]);
        assert!(parse(&interrupt).is_ok());
        for prior in [Value::Null, json!(["invalid"]), json!([HOOK, HOOK])] {
            let mut changed = interrupt.clone();
            changed["prior_inputs"] = prior;
            assert!(parse(&changed).is_err());
        }
        let mut view = View::default();
        view.begin();
        view.reply(parse(&good).unwrap());
        let enter = KeyEvent::new(KeyCode::Enter, KeyModifiers::NONE);
        let down = KeyEvent::new(KeyCode::PageDown, KeyModifiers::NONE);
        let paint = |view: &mut View, width| {
            Terminal::new(TestBackend::new(width, 9))
                .unwrap()
                .draw(|f| view.draw(f, f.area(), false))
                .unwrap();
        };
        assert!(view.key(enter, false).is_none());
        paint(&mut view, 42);
        assert!(view.pages > 1);
        assert!(view.key(enter, false).is_none());
        for _ in 1..view.pages {
            view.key(down, false);
            paint(&mut view, 42);
        }
        assert!(
            view.key(
                KeyEvent::new(KeyCode::Enter, KeyModifiers::CONTROL | KeyModifiers::SHIFT),
                false
            )
            .is_none()
        );
        assert!(view.key(enter, true).is_none());
        assert!(matches!(view.key(enter, false), Some(Request::Apply(_))));
        paint(&mut view, 41);
        assert!(view.key(enter, false).is_none());
        view.begin();
        view.reply(parse(&interrupt).unwrap());
        paint(&mut view, 42);
        for _ in 1..view.pages {
            view.key(down, false);
            paint(&mut view, 42);
        }
        assert!(view.key(enter, false).is_none());
        let confirmed = view
            .key(KeyEvent::new(KeyCode::Char('i'), KeyModifiers::NONE), false)
            .unwrap();
        assert!(confirmed.interrupts());
    }
}
