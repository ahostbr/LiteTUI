//! Exact captured execution and input ownership shared by reviewed actions.
use super::*;

#[derive(Clone, Copy, PartialEq, Eq)]
pub(super) enum Kind {
    Rewrite,
    Compact,
}

pub(super) struct Chain {
    kind: Kind,
    pub(super) root_execution: Option<String>,
    pub(super) root_finished: bool,
    expected_root: Option<String>,
    root_bound: bool,
    root_origin: Option<Origin>,
    wake_allowed: bool,
    pub(super) rewind_thread: Option<String>,
    pub(super) rewind_turn: Option<String>,
    pub(super) action: String,
    pub(super) operation: String,
    pub(super) segments: u64,
    pub(super) last_execution: Option<String>,
    pub(super) ended: bool,
    pub(super) maintenance: std::collections::HashMap<String, (Option<String>, bool, Origin)>,
    pub(super) inputs: std::collections::HashMap<(String, String), (ExecutionId, Origin)>,
}

impl Chain {
    pub(super) fn new(kind: Kind, action: String, operation: String) -> Self {
        Self {
            kind,
            action,
            operation,
            root_execution: None,
            root_finished: false,
            expected_root: None,
            root_bound: false,
            root_origin: None,
            wake_allowed: false,
            rewind_thread: None,
            rewind_turn: None,
            segments: 0,
            last_execution: None,
            ended: true,
            maintenance: Default::default(),
            inputs: Default::default(),
        }
    }
    pub(super) fn work_predecessor(&self) -> Option<(&str, &Origin)> {
        if self.kind != Kind::Compact
            || !self.root_bound
            || !self.root_finished
            || !self.wake_allowed
            || self.segments != 0
            || !self.maintenance.is_empty()
        {
            return None;
        }
        let execution = self.root_execution.as_deref()?;
        if self.expected_root.as_deref() != Some(execution) {
            return None;
        }
        Some((execution, self.root_origin.as_ref()?))
    }
    pub(super) fn permit_wake(&mut self, allowed: bool) {
        self.wake_allowed = allowed;
    }
    pub(super) fn bind_root(&mut self, root: Option<&str>) -> io::Result<()> {
        if self.kind != Kind::Compact
            || self.root_bound
            || self
                .root_execution
                .as_deref()
                .is_some_and(|seen| Some(seen) != root)
            || root.is_none() && (self.segments != 0 || !self.maintenance.is_empty())
        {
            return Err(fail(
                "Compaction admission changed its captured root worker",
            ));
        }
        self.root_bound = true;
        self.expected_root = root.map(str::to_owned);
        Ok(())
    }
    pub(super) fn acknowledge_question(&mut self, id: &str) {
        self.inputs
            .remove(&("user_input_resolved".to_owned(), id.to_owned()));
    }
    pub(super) fn verify(
        &mut self,
        frame: &Value,
        event: &Value,
        executions: &mut HashSet<String>,
        stream_origin: &mut Option<(ExecutionId, Origin)>,
    ) -> io::Result<()> {
        let chain = self;
        let (execution, origin, _) = crate::stream::origin(frame, 4096)
            .map_err(|_| fail("Rewrite event lacks captured execution origin"))?;
        if origin.operation.as_ref().and_then(Value::as_str) != Some(chain.operation.as_str())
            || origin.source.as_deref() != Some("rpc")
        {
            return Err(fail("Rewrite event changed its admitted operation origin"));
        }
        let kind = event["type"].as_str().expect("wire event");
        let compact = chain.kind == Kind::Compact;
        let maintenance_kind = if compact {
            "compact_maintenance"
        } else {
            "rewrite_aftercare"
        };
        let segment_kind = if compact {
            "compact_segment"
        } else {
            "rewrite_segment"
        };
        if (kind == "rewrite_aftercare" && compact)
            || (kind == "compact_maintenance" && !compact)
            || event
                .get(if compact {
                    "rewrite_segment"
                } else {
                    "compact_segment"
                })
                .is_some()
        {
            return Err(fail(
                "Execution metadata belongs to another reviewed action family",
            ));
        }
        if kind == maintenance_kind {
            let id = crate::stream::required_uuid4(event, "aftercare_id")
                .map_err(|_| fail("Invalid maintenance identity"))?;
            let root = if compact {
                event["root"]
                    .as_bool()
                    .ok_or_else(|| fail("Missing compaction root flag"))?
            } else {
                false
            };
            let parent = if root {
                if event.get("after_execution_id") != Some(&Value::Null) {
                    return Err(fail("Root compaction cannot invent a preceding stream"));
                }
                None
            } else {
                Some(
                    crate::stream::required_uuid4(event, "after_execution_id")
                        .map_err(|_| fail("Invalid maintenance predecessor"))?
                        .to_owned(),
                )
            };
            if id != execution.as_str()
                || event["action_id"] != chain.action
                || event["kind"] != "compaction"
            {
                return Err(fail("Maintenance changed its captured reviewed authority"));
            }
            match event["phase"].as_str() {
                Some("start")
                    if chain.ended
                        && chain.maintenance.len() < 64
                        && executions.len() < 1000
                        && (if root {
                            chain.root_execution.is_none()
                                && chain.segments == 0
                                && (!chain.root_bound || chain.expected_root.as_deref() == Some(id))
                        } else {
                            parent.is_some() && chain.last_execution == parent
                        })
                        && executions.insert(id.to_owned()) =>
                {
                    if root {
                        chain.root_execution = Some(id.to_owned());
                        chain.root_origin = Some(origin.clone());
                    }
                    chain
                        .maintenance
                        .insert(id.to_owned(), (parent, root, origin.clone()));
                }
                Some("end")
                    if chain.ended
                        && chain.maintenance.get(id).is_some_and(|captured| {
                            captured == &(parent.clone(), root, origin.clone())
                        }) =>
                {
                    chain.maintenance.remove(id);
                    if root {
                        chain.root_finished = true;
                    }
                }
                _ => return Err(fail("Unexpected or replayed reviewed maintenance boundary")),
            }
            return Ok(());
        }
        if kind == "turn_start" {
            let segment = &event[segment_kind];
            let previous = chain
                .last_execution
                .as_ref()
                .map_or(Value::Null, |s| json!(s));
            if !chain.ended
                || compact && (!chain.root_finished || !chain.wake_allowed)
                || segment["action_id"] != chain.action
                || segment["index"].as_u64() != Some(chain.segments)
                || segment.get("previous_execution_id") != Some(&previous)
                || executions.len() >= 1000
                || !executions.insert(execution.as_str().to_owned())
            {
                return Err(fail("Rewrite segment is out of order, replayed or foreign"));
            }
            chain.segments += 1;
            chain.last_execution = Some(execution.as_str().to_owned());
            chain.ended = false;
            *stream_origin = Some((execution, origin));
            return Ok(());
        }
        if kind == "turn_refused" || event.get(segment_kind).is_some() {
            return Err(fail("Unexpected rewrite execution start/refusal"));
        }
        // A waiting input callback may retire after its worker boundary on Quit.
        // Only the exact previously observed request can close under that origin.
        if matches!(kind, "user_input_resolved" | "tool_approval_closed") {
            let id = text(event, "id", 256)?.to_owned();
            let key = (kind.to_owned(), id);
            if chain.inputs.get(&key) != Some(&(execution, origin)) {
                return Err(fail(
                    "Rewrite input closure changed its captured request origin",
                ));
            }
            chain.inputs.remove(&key);
            return Ok(());
        }
        if let Some((_, _, captured_origin)) = chain.maintenance.get(execution.as_str()) {
            if captured_origin != &origin {
                return Err(fail("Rewrite maintenance changed its captured hook origin"));
            }
            if matches!(
                kind,
                "turn_end" | "text_delta" | "reasoning_delta" | "native_text_snapshot"
            ) {
                return Err(fail("Maintenance cannot impersonate a stream segment"));
            }
        } else {
            if stream_origin.as_ref() != Some(&(execution.clone(), origin.clone())) {
                return Err(fail(
                    "Rewrite event changed its captured stream or maintenance worker",
                ));
            }
            if kind == "turn_end" {
                if chain.ended {
                    return Err(fail("Duplicate rewrite segment end"));
                }
                chain.ended = true;
            } else if chain.ended && kind != "compaction" {
                return Err(fail("Rewrite stream content after its terminal boundary"));
            }
        }
        if kind == "native_turn_started"
            && let Some(thread) = &chain.rewind_thread
        {
            let turn = text(event, "turnId", 128)?;
            if event["provider"] != "codex"
                || event["eventVersion"] != 1
                || event["threadId"] != *thread
                || chain.segments != 1
                || chain.ended
                || chain.last_execution.as_deref() != Some(execution.as_str())
                || chain.rewind_turn.is_some()
            {
                return Err(fail(
                    "Codex replacement emitted an unrelated or repeated native turn",
                ));
            }
            chain.rewind_turn = Some(turn.to_owned());
        }
        let closure = match kind {
            "user_input_requested" => Some("user_input_resolved"),
            "tool_approval_requested" => Some("tool_approval_closed"),
            _ => None,
        };
        if let Some(closure) = closure {
            let id = text(event, "id", 256)?.to_owned();
            let key = (closure.to_owned(), id);
            if chain.inputs.len() >= 200 || chain.inputs.contains_key(&key) {
                return Err(fail("Duplicate or excessive pending rewrite inputs"));
            }
            chain.inputs.insert(key, (execution, origin));
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    const ROOT: &str = "12345678-1234-4123-8123-123456789abc";
    const STREAM: &str = "22345678-1234-4123-8123-123456789abc";
    const OTHER: &str = "32345678-1234-4123-8123-123456789abc";
    fn frame(id: &str) -> Value {
        json!({"execution_id":id,"operation_id":"op","session_id":"session","hook_id":null,"source":"rpc"})
    }
    fn root(phase: &str) -> Value {
        json!({"type":"compact_maintenance","action_id":"action","aftercare_id":ROOT,
            "phase":phase,"kind":"compaction","root":true,"after_execution_id":null})
    }
    fn compact() -> Chain {
        let mut c = Chain::new(Kind::Compact, "action".into(), "op".into());
        c.bind_root(Some(ROOT)).unwrap();
        c.permit_wake(true);
        c
    }
    fn accept(c: &mut Chain, event: Value) -> io::Result<()> {
        c.verify(&frame(ROOT), &event, &mut HashSet::new(), &mut None)
    }
    #[test]
    fn manual_root_finishes_without_a_fabricated_stream() {
        let mut c = compact();
        accept(&mut c, root("start")).unwrap();
        accept(&mut c, root("end")).unwrap();
        assert!(c.root_finished && c.maintenance.is_empty() && c.ended);
        assert_eq!(c.root_execution.as_deref(), Some(ROOT));
        assert_eq!(c.segments, 0);
        assert_eq!(c.last_execution, None);
    }
    #[test]
    fn manual_root_is_exactly_reviewed_once_with_no_predecessor() {
        for fault in ["foreign", "parent", "not-root", "repeat"] {
            let mut c = compact();
            let mut event = root("start");
            let mut envelope = frame(ROOT);
            match fault {
                "foreign" => {
                    event["aftercare_id"] = json!(OTHER);
                    envelope = frame(OTHER);
                }
                "parent" => event["after_execution_id"] = json!(STREAM),
                "not-root" => event["root"] = json!(false),
                _ => {
                    accept(&mut c, root("start")).unwrap();
                    accept(&mut c, root("end")).unwrap();
                }
            }
            assert!(
                c.verify(&envelope, &event, &mut HashSet::new(), &mut None)
                    .is_err(),
                "{fault}"
            );
        }
    }
    #[test]
    fn manual_wake_requires_root_retirement_and_keeps_its_real_origin() {
        let start = json!({"type":"turn_start","compact_segment":{"action_id":"action","index":0,"previous_execution_id":null}});
        let mut c = compact();
        let mut seen = HashSet::new();
        let mut origin = None;
        assert!(
            c.verify(&frame(STREAM), &start, &mut seen, &mut origin)
                .is_err()
        );
        c.verify(&frame(ROOT), &root("start"), &mut seen, &mut origin)
            .unwrap();
        assert!(
            c.verify(&frame(STREAM), &start, &mut seen, &mut origin)
                .is_err()
        );
        c.verify(&frame(ROOT), &root("end"), &mut seen, &mut origin)
            .unwrap();
        c.verify(&frame(STREAM), &start, &mut seen, &mut origin)
            .unwrap();
        assert_eq!(c.segments, 1);
        assert_eq!(c.last_execution.as_deref(), Some(STREAM));
        let mut changed = frame(STREAM);
        changed["hook_id"] = json!(OTHER);
        assert!(
            c.verify(
                &changed,
                &json!({"type":"text_delta","text":"foreign"}),
                &mut seen,
                &mut origin
            )
            .is_err()
        );
        c.verify(
            &frame(STREAM),
            &json!({"type":"turn_end"}),
            &mut seen,
            &mut origin,
        )
        .unwrap();
    }
    #[test]
    fn reviewed_wake_off_cannot_admit_a_stream() {
        let mut c = compact();
        c.permit_wake(false);
        accept(&mut c, root("start")).unwrap();
        accept(&mut c, root("end")).unwrap();
        let event = json!({"type":"turn_start","compact_segment":{"action_id":"action","index":0,"previous_execution_id":null}});
        assert!(
            c.verify(&frame(STREAM), &event, &mut HashSet::new(), &mut None)
                .is_err()
        );
    }
    #[test]
    fn retired_root_only_accepts_the_exact_captured_input_closure_once() {
        let mut c = compact();
        accept(&mut c, root("start")).unwrap();
        accept(&mut c, json!({"type":"user_input_requested","id":"ask"})).unwrap();
        accept(&mut c, root("end")).unwrap();
        assert_eq!(c.inputs.len(), 1);
        let close = json!({"type":"user_input_resolved","id":"ask","cancelled":true});
        let mut drift = frame(ROOT);
        drift["hook_id"] = json!(OTHER);
        assert!(
            c.verify(&drift, &close, &mut HashSet::new(), &mut None)
                .is_err()
        );
        accept(&mut c, close.clone()).unwrap();
        assert!(c.inputs.is_empty());
        assert!(accept(&mut c, close).is_err());
    }
    #[test]
    fn correlated_answer_ack_retires_only_the_known_question() {
        let mut c = compact();
        accept(&mut c, root("start")).unwrap();
        accept(&mut c, json!({"type":"user_input_requested","id":"ask"})).unwrap();
        accept(&mut c, root("end")).unwrap();
        c.acknowledge_question("other");
        assert_eq!(c.inputs.len(), 1);
        c.acknowledge_question("ask");
        assert!(c.inputs.is_empty());
    }
    #[test]
    fn action_families_cannot_borrow_each_others_markers() {
        let mut rewrite = Chain::new(Kind::Rewrite, "action".into(), "op".into());
        assert!(accept(&mut rewrite, root("start")).is_err());
        let mut c = compact();
        let mut legacy = root("start");
        legacy["type"] = json!("rewrite_aftercare");
        assert!(accept(&mut c, legacy.clone()).is_err());
        assert!(accept(&mut rewrite, legacy).is_err());
        assert!(c.bind_root(Some(ROOT)).is_err());
    }
}

#[cfg(test)]
mod work_root_tests {
    use super::*;
    const ROOT: &str = "42345678-1234-4123-8123-123456789abc";
    const ACTION: &str = "32345678-1234-4123-8123-123456789abc";
    fn boundary(
        chain: &mut Chain,
        executions: &mut HashSet<String>,
        stream: &mut Option<(ExecutionId, Origin)>,
        phase: &str,
    ) {
        let event = json!({"type":"compact_maintenance","action_id":ACTION,"aftercare_id":ROOT,"phase":phase,"kind":"compaction","root":true,"after_execution_id":null});
        let frame = json!({"type":"gui.event","event_id":"62345678-1234-4123-8123-123456789abc","session_id":"12345678-1234-4123-8123-123456789abc","operation_id":"reviewed-operation","execution_id":ROOT,"hook_id":null,"source":"rpc","event":event});
        chain.verify(&frame, &event, executions, stream).unwrap();
    }
    #[test]
    fn only_finished_reviewed_wake_root_can_precede_first_work_segment() {
        for allowed in [false, true] {
            let mut chain = Chain::new(Kind::Compact, ACTION.into(), "reviewed-operation".into());
            chain.bind_root(Some(ROOT)).unwrap();
            chain.permit_wake(allowed);
            let mut executions = HashSet::new();
            let mut stream = None;
            assert!(chain.work_predecessor().is_none());
            boundary(&mut chain, &mut executions, &mut stream, "start");
            assert!(chain.work_predecessor().is_none());
            boundary(&mut chain, &mut executions, &mut stream, "end");
            assert_eq!(chain.work_predecessor().is_some(), allowed);
            assert!(stream.is_none(), "maintenance never invents a stream");
            if allowed {
                let (id, origin) = chain.work_predecessor().unwrap();
                assert_eq!(id, ROOT);
                assert_eq!(origin.operation, Some(json!("reviewed-operation")));
                chain.segments = 1;
                assert!(chain.work_predecessor().is_none());
            }
        }
    }
}
