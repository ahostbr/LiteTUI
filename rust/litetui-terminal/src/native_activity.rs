//! Native display records. Provider IDs are scoped to one serialized turn;
//! observing a request/result never dispatches a tool or grants permission.
use serde_json::Value;
use std::io;

pub(crate) fn card_index(event: &Value) -> io::Result<Option<usize>> {
    event
        .get("card_index")
        .map(|v| {
            v.as_u64()
                .filter(|n| *n < 100)
                .map(|n| n as usize)
                .ok_or_else(|| io::Error::other("Invalid native display card_index"))
        })
        .transpose()
}
fn label(event: &Value, key: &str, required: bool) -> io::Result<Option<String>> {
    match event.get(key) {
        None | Some(Value::Null) if !required => Ok(None),
        Some(Value::String(s)) if !s.is_empty() && s.len() <= 512 => Ok(Some(s.clone())),
        _ => Err(io::Error::other(format!("Invalid native tool {key}"))),
    }
}
fn display(value: &Value) -> String {
    value
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string())
}
#[derive(Debug, PartialEq)]
pub struct Activity {
    pub(crate) card: usize,
    pub(crate) id: String,
    pub(crate) name: Option<String>,
    pub(crate) detail: Detail,
}
#[derive(Debug, PartialEq)]
pub(crate) enum Detail {
    Input {
        text: Option<String>,
        complete: bool,
    },
    Result {
        text: String,
        is_error: Option<bool>,
    },
}
impl Activity {
    pub fn from_event(event: &Value) -> io::Result<Self> {
        if event["type"] != "native_activity" || event["provider"] != "claude" {
            return Err(io::Error::other(
                "Unsupported native activity provider/type",
            ));
        }
        let card = card_index(event)?
            .ok_or_else(|| io::Error::other("Native activity needs display ownership"))?;
        let complete = event
            .get("complete")
            .and_then(Value::as_bool)
            .ok_or_else(|| io::Error::other("Native activity needs boolean complete"))?;
        let detail = match event.get("kind").and_then(Value::as_str) {
            Some("tool_use") => Detail::Input {
                text: match event.get("input") {
                    None | Some(Value::Null) => None,
                    Some(v @ Value::Object(_)) => Some(v.to_string()),
                    _ => {
                        return Err(io::Error::other(
                            "Native tool input must be an object or null",
                        ));
                    }
                },
                complete,
            },
            Some("tool_result") if complete => Detail::Result {
                text: display(
                    event
                        .get("result")
                        .ok_or_else(|| io::Error::other("Native result missing"))?,
                ),
                is_error: match event.get("is_error") {
                    Some(Value::Null) => None,
                    Some(Value::Bool(value)) => Some(*value),
                    _ => {
                        return Err(io::Error::other(
                            "Native result needs explicit nullable is_error",
                        ));
                    }
                },
            },
            _ => {
                return Err(io::Error::other(
                    "Unsupported or incomplete native tool record",
                ));
            }
        };
        Ok(Self {
            card,
            id: label(event, "id", true)?.expect("required"),
            name: label(event, "name", false)?,
            detail,
        })
    }
    pub(crate) fn emitted_bytes(&self) -> usize {
        self.id.len()
            + self.name.as_ref().map_or(0, String::len)
            + match &self.detail {
                Detail::Input { text, .. } => text.as_ref().map_or(0, String::len),
                Detail::Result { text, .. } => text.len(),
            }
    }
}
#[derive(Clone, Debug, PartialEq)]
pub enum ResultState {
    Pending,
    Returned {
        text: String,
        is_error: Option<bool>,
    },
    Incomplete,
}
#[derive(Clone, Debug)]
pub struct Tool {
    pub card: usize,
    pub id: String,
    pub name: Option<String>,
    pub input: Option<String>,
    pub input_complete: bool,
    pub request_seen: bool,
    pub result: ResultState,
}
impl Tool {
    pub(crate) fn updated(old: Option<&Self>, event: Activity) -> io::Result<Self> {
        let mut tool = old.cloned().unwrap_or_else(|| Self {
            card: event.card,
            id: event.id.clone(),
            name: None,
            input: None,
            input_complete: false,
            request_seen: false,
            result: ResultState::Pending,
        });
        if tool.card != event.card
            || (tool.name.is_some() && event.name.is_some() && tool.name != event.name)
        {
            return Err(io::Error::other("Native tool identity changed within turn"));
        }
        if event.name.is_some() {
            tool.name = event.name;
        }
        match event.detail {
            Detail::Input { text, complete } => {
                if let Some(text) = text {
                    tool.input = Some(text);
                }
                tool.input_complete = complete;
                tool.request_seen = true;
            }
            Detail::Result { text, is_error } => {
                tool.result = ResultState::Returned { text, is_error }
            }
        }
        Ok(tool)
    }
    pub(crate) fn finish(&mut self) {
        if self.result == ResultState::Pending {
            self.result = ResultState::Incomplete;
        }
    }
    pub(crate) fn texts(&self) -> Vec<&str> {
        let mut texts = vec![self.id.as_str()];
        if let Some(name) = &self.name {
            texts.push(name);
        }
        if let Some(input) = &self.input {
            texts.push(input);
        }
        if let ResultState::Returned { text, .. } = &self.result {
            texts.push(text);
        }
        texts
    }
}
