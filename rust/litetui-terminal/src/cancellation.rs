//! Stop command receipts are not turn completion and never resolve input locally.
use serde_json::{Value, json};
use std::{collections::BTreeMap, io};

#[derive(Default)]
pub struct Ledger {
    receipts: BTreeMap<String, bool>,
}
impl Ledger {
    pub fn request(&mut self) -> io::Result<Value> {
        if self.receipts.len() >= 200 {
            return Err(io::Error::other("Stop command limit reached"));
        }
        let id = format!("rust-abort-{}", self.receipts.len());
        self.receipts.insert(id.clone(), false);
        Ok(json!({"type":"abort","id":id}))
    }
    pub fn response(&mut self, event: &Value) -> io::Result<Option<bool>> {
        let Some(received) = event["id"]
            .as_str()
            .and_then(|id| self.receipts.get_mut(id))
        else {
            return Ok(None);
        };
        if *received {
            return Err(io::Error::other("Duplicate stop response"));
        }
        let stopped = event["result"]["stopped"].as_bool();
        let asks = event["result"]["cancelled_asks"].as_array();
        if event["ok"] != true
            || stopped.is_none()
            || asks.is_none_or(|asks| {
                asks.len() > 100
                    || asks
                        .iter()
                        .any(|id| id.as_str().is_none_or(|id| id.is_empty() || id.len() > 256))
            })
        {
            return Err(io::Error::other("Malformed stop response"));
        }
        *received = true;
        Ok(stopped)
    }
    pub fn settled(&self) -> bool {
        self.receipts.values().all(|received| *received)
    }
}
