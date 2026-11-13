//! One bounded runtime choice, ordered by its exact result/readback publication.
use super::{CommandId, Management, Step, fail, text};
use crate::{runtime_controls as controls, runtime_controls_view as view, work_lifecycle as life};
use serde_json::Value;
use std::io;

#[derive(Default)]
pub(super) struct Control {
    capabilities: Option<controls::Capabilities>,
    mcp_lifecycle: bool,
    captured: Option<controls::Policy>,
    policy: Option<controls::Policy>,
    policy_update: Option<controls::Policy>,
    snapshot: Option<controls::Snapshot>,
    review: Option<controls::Review>,
    pending: Option<Pending>,
    invocation: Option<Invocation>,
    retired_shutdown: Option<Value>,
    retired_policy: Option<controls::Policy>,
    activation_sent: bool,
    retired_review: Option<controls::Review>,
    historical: Option<Invocation>,
}
struct Pending {
    id: CommandId,
    kind: Read,
}
enum Read {
    Snapshot(controls::Field),
    Review(Box<controls::Snapshot>, controls::Change),
    Apply { historical: bool },
    Readback { historical: bool },
}
struct Invocation {
    review: controls::Review,
    apply_id: String,
    readback_id: Option<String>,
    receipt: Option<controls::Receipt>,
    readback: Option<controls::Readback>,
    released: bool,
}
fn rpc_id(id: &CommandId) -> io::Result<String> {
    id.to_value()
        .as_str()
        .map(str::to_owned)
        .ok_or_else(|| fail("Runtime control RPC id must be text"))
}
impl Management {
    pub(super) fn register_sidecar_runtime(
        &mut self,
        value: &Value,
        outer_id: &str,
    ) -> io::Result<()> {
        let policy = self
            .runtime_policy()
            .ok_or_else(|| fail("Sidecar runtime policy is unavailable"))?;
        let review = crate::sidecar_controls::runtime_review(value, policy)?;
        if self
            .runtime_controls
            .invocation
            .as_ref()
            .is_some_and(|a| !a.released)
        {
            return Err(fail("Another runtime choice still owns its readback"));
        }
        self.runtime_controls.invocation = Some(Invocation {
            review,
            apply_id: format!("{outer_id}/apply"),
            readback_id: None,
            receipt: None,
            readback: None,
            released: false,
        });
        Ok(())
    }
    pub(super) fn observe_sidecar_runtime_apply(
        &mut self,
        review: &Value,
        applied: &Value,
    ) -> io::Result<()> {
        let current = self
            .runtime_controls
            .invocation
            .as_mut()
            .ok_or_else(|| fail("Sidecar runtime has no retained invocation"))?;
        if current.review.wire != review["review"]
            || applied["command"]["id"] != current.apply_id
            || current.receipt.is_some()
        {
            return Err(fail(
                "Sidecar runtime application changed its exact invocation",
            ));
        }
        current.receipt = Some(controls::Receipt::parse(
            &applied["result"],
            &current.review,
            &current.apply_id,
        )?);
        Ok(())
    }
    pub(super) fn install_sidecar_runtime(&mut self, value: &Value) -> io::Result<()> {
        let invocation = self
            .runtime_controls
            .invocation
            .as_mut()
            .ok_or_else(|| fail("Sidecar runtime has no application"))?;
        if invocation.released || invocation.readback.is_some() {
            return Err(fail("Sidecar runtime readback was consumed"));
        }
        let receipt = invocation
            .receipt
            .as_ref()
            .ok_or_else(|| fail("Sidecar runtime receipt is missing"))?;
        let readback = controls::Readback::parse(&value["result"], receipt)?;
        let operation = text(&value["command"], "id", 128)?.to_owned();
        if value["result"]["readback_operation_id"] != operation {
            return Err(fail("Sidecar runtime readback changed its actual command"));
        }
        invocation.readback_id = Some(operation);
        invocation.readback = Some(readback.clone());
        self.install_runtime_readback(&readback)
    }
    pub(super) fn configure_runtime_controls(&mut self, operations: &[String]) -> io::Result<()> {
        self.runtime_controls.capabilities = controls::Capabilities::negotiate(operations)?;
        self.runtime_controls.mcp_lifecycle = crate::mcp_lifecycle::negotiate(
            operations,
            self.runtime_controls.capabilities.is_some(),
        )?;
        Ok(())
    }
    pub fn runtime_controls_pending(&self) -> bool {
        self.runtime_controls.pending.is_some()
            || self
                .runtime_controls
                .invocation
                .as_ref()
                .is_some_and(|p| !p.released)
            || self
                .runtime_controls
                .historical
                .as_ref()
                .is_some_and(|p| !p.released)
    }
    pub(super) fn retired_runtime_policy(&self) -> Option<&controls::Policy> {
        self.runtime_controls.retired_policy.as_ref()
    }
    pub(super) fn historical_runtime_binding(&self) -> Option<&controls::Binding> {
        self.runtime_controls
            .historical
            .as_ref()
            .map(|p| &p.review.binding)
    }
    pub(super) fn verify_target_runtime_capture(
        &self,
        state: &super::Snapshot,
        reported: &Value,
    ) -> io::Result<()> {
        let captured = self
            .runtime_controls
            .captured
            .as_ref()
            .ok_or_else(|| fail("Target capture lost runtime policy"))?;
        let previous = self
            .retired_runtime_policy()
            .ok_or_else(|| fail("Target capture lost predecessor policy"))?;
        let id = &captured.binding.identity;
        if state.session != id.session
            || state.backend != id.backend
            || state.model != id.model
            || state.profile != captured.facts.effective_profile.as_str()
            || (state.mode == "plan") != captured.facts.plan_mode
            || previous.facts.requested_cli_profile != captured.facts.requested_cli_profile
        {
            return Err(fail(
                "Target runtime capture disagrees with independently observed conversation state",
            ));
        }
        let expected = crate::tool_mode::Expected {
            enabled: previous.facts.tools_enabled,
            profile: previous.facts.seat_profile,
            workspace: previous.facts.cwd.clone().into(),
        };
        expected.validate_facts(&captured.facts, crate::tool_mode::ValidationMode::Target)?;
        expected.validate_mode(
            reported,
            crate::tool_mode::ValidationMode::Current(captured),
        )
    }
    pub(super) fn runtime_gate_held(&self) -> bool {
        self.runtime_controls
            .invocation
            .as_ref()
            .is_some_and(|attempt| !attempt.released && attempt.receipt.is_some())
    }
    pub fn runtime_effective(
        &mut self,
        profile: crate::tool_mode::Profile,
    ) -> Option<controls::Policy> {
        let policy = self.runtime_controls.policy.as_mut()?;
        if policy.facts.effective_profile == profile {
            return None;
        }
        policy.facts.effective_profile = profile;
        policy.facts.wire["effective_profile"] = Value::from(profile.as_str());
        if let Some(expected) = self.expected.as_mut() {
            expected.3 = profile.as_str().to_owned();
        }
        Some(policy.clone())
    }
    pub fn runtime_policy(&self) -> Option<&controls::Policy> {
        self.runtime_controls.policy.as_ref()
    }
    pub(super) fn install_mcp_policy(
        &mut self,
        before: &controls::Policy,
        after: controls::Policy,
    ) -> io::Result<()> {
        let current = self
            .runtime_controls
            .policy
            .as_ref()
            .ok_or_else(|| fail("MCP policy has no active owner"))?;
        if current != before {
            return Err(fail("MCP receipt changed the exact published policy"));
        }
        crate::mcp_lifecycle::verify_policy_transition(before, &after)?;
        if self
            .runtime_controls
            .invocation
            .as_ref()
            .is_some_and(|attempt| {
                !attempt.released
                    || !attempt.readback.as_ref().is_some_and(|readback| {
                        readback.verified
                            && matches!(
                                readback.release,
                                controls::Release::Install | controls::Release::Unchanged
                            )
                    })
            })
        {
            return Err(fail(
                "MCP policy cannot retire an unfinished runtime control",
            ));
        }
        // The verified MCP transition supersedes a completed generic control,
        // just as a new generic Apply does. Keep strict idle-generation shutdown.
        self.runtime_controls.invocation = None;
        self.runtime_controls.policy = Some(after.clone());
        self.runtime_controls.policy_update = Some(after);
        self.runtime_controls.snapshot = None;
        self.runtime_controls.review = None;
        Ok(())
    }
    pub fn take_runtime_policy(&mut self) -> Option<controls::Policy> {
        self.runtime_controls.policy_update.take()
    }
    pub(super) fn runtime_capture_request(&self, command: &mut Value) {
        if self.runtime_controls.capabilities.is_some() {
            command["runtime_controls_version"] = Value::from(1);
            if self
                .runtime_controls
                .capabilities
                .as_ref()
                .is_some_and(|caps| caps.tool_selection)
            {
                command["tool_selection_version"] = Value::from(1);
            }
            if self.runtime_controls.mcp_lifecycle {
                command["mcp_lifecycle_version"] = Value::from(1);
            }
        }
    }
    /// Validate only negotiated extension fields, then let the old strict parser
    /// validate every remaining key. No unknown field is silently discarded.
    pub(super) fn runtime_capture(
        &mut self,
        value: &Value,
        identity: &life::Identity,
    ) -> io::Result<Value> {
        let Some(capabilities) = self.runtime_controls.capabilities.as_ref() else {
            return Ok(value.clone());
        };
        if self.runtime_controls.policy.is_some()
            || self
                .runtime_controls
                .invocation
                .as_ref()
                .is_some_and(|p| !p.released)
        {
            return Err(fail(
                "Runtime controls recapture lacks completed retirement",
            ));
        }
        crate::mcp_lifecycle::verify_capability(value, self.runtime_controls.mcp_lifecycle)?;
        let binding = controls::Binding {
            mcp_lifecycle: self.runtime_controls.mcp_lifecycle,
            identity: identity.clone(),
            work_instance: value["instance"]
                .as_str()
                .ok_or_else(|| fail("Runtime controls capture lacks Work instance"))?
                .to_owned(),
            provider: life::Provider::parse(value, "provider")?,
            tool_selection: capabilities.tool_selection,
        };
        let policy = capabilities.parse_capture(value, &binding)?;
        self.runtime_controls.captured = Some(policy);
        let mut legacy = value.clone();
        let object = legacy
            .as_object_mut()
            .ok_or_else(|| fail("Runtime capture is not an object"))?;
        for key in [
            "runtime_controls_version",
            "runtime_policy",
            "runtime_controls_limits",
            "tool_selection_version",
        ] {
            object.remove(key);
        }
        if self.runtime_controls.mcp_lifecycle {
            object.remove("mcp_lifecycle_version");
        }
        Ok(legacy)
    }
    pub(super) fn runtime_capture_seat(&self) -> Option<crate::tool_mode::Profile> {
        self.runtime_controls
            .captured
            .as_ref()
            .map(|p| p.facts.seat_profile)
    }
    pub(super) fn runtime_activation_request(&mut self, command: &mut Value) {
        if self.runtime_controls.captured.is_some() {
            command["runtime_controls_version"] = Value::from(1);
            if self
                .runtime_controls
                .captured
                .as_ref()
                .is_some_and(|policy| policy.binding.tool_selection)
            {
                command["tool_selection_version"] = Value::from(1);
            }
            if self.runtime_controls.mcp_lifecycle {
                command["mcp_lifecycle_version"] = Value::from(1);
            }
            self.runtime_controls.activation_sent = true;
        }
    }
    pub(super) fn historical_runtime_pending(&self) -> bool {
        self.runtime_controls
            .historical
            .as_ref()
            .is_some_and(|attempt| !attempt.released)
    }
    fn runtime_attempt_mut(&mut self, historical: bool) -> Option<&mut Invocation> {
        if historical {
            self.runtime_controls.historical.as_mut()
        } else {
            self.runtime_controls.invocation.as_mut()
        }
    }
    fn register_runtime_apply(
        &mut self,
        review: Box<controls::Review>,
        historical: bool,
    ) -> io::Result<Value> {
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve runtime controls Apply"))?;
        let mut command = review.request();
        command["id"] = id.to_value();
        let attempt = Invocation {
            review: *review,
            apply_id: rpc_id(&id)?,
            readback_id: None,
            receipt: None,
            readback: None,
            released: false,
        };
        if historical {
            self.runtime_controls.retired_review = None;
            self.runtime_controls.historical = Some(attempt);
        } else {
            self.runtime_controls.invocation = Some(attempt);
            self.runtime_controls.review = None;
            self.runtime_controls.snapshot = None;
        }
        self.runtime_controls.pending = Some(Pending {
            id,
            kind: Read::Apply { historical },
        });
        Ok(command)
    }
    pub(super) fn runtime_activation(&mut self, value: &Value) -> io::Result<Value> {
        let Some(captured) = self.runtime_controls.captured.as_ref() else {
            return Ok(value.clone());
        };
        if self
            .runtime_controls
            .historical
            .as_ref()
            .is_some_and(|p| !p.released)
        {
            return Err(fail(
                "Runtime activation overtook a historical control publication",
            ));
        }
        crate::mcp_lifecycle::verify_capability(value, captured.binding.mcp_lifecycle)?;
        let policy = self
            .runtime_controls
            .capabilities
            .as_ref()
            .expect("negotiated")
            .parse_activation(value, &captured.binding, captured)?;
        let mut legacy = value.clone();
        let object = legacy
            .as_object_mut()
            .ok_or_else(|| fail("Runtime activation is not an object"))?;
        if captured.binding.mcp_lifecycle {
            object.remove("mcp_lifecycle_version");
        }
        for key in [
            "runtime_controls_version",
            "runtime_policy",
            "tool_selection_version",
        ] {
            object.remove(key);
        }
        if let Some(expected) = self.expected.as_mut() {
            expected.3 = policy.facts.effective_profile.as_str().to_owned();
        }
        self.runtime_controls.policy = Some(policy.clone());
        self.runtime_controls.policy_update = Some(policy);
        self.runtime_controls.retired_shutdown = None;
        self.runtime_controls.retired_policy = None;
        self.runtime_controls.retired_review = None;
        self.runtime_controls.snapshot = None;
        self.runtime_controls.review = None;
        self.runtime_controls.invocation = None;
        Ok(legacy)
    }
    pub fn request_runtime_controls(&mut self, intent: view::Intent) -> io::Result<Value> {
        if let view::Intent::Apply(review) = &intent
            && self.runtime_controls.retired_review.as_ref() == Some(review.as_ref())
        {
            if self.closing
                || self.failed
                || self.runtime_controls_pending()
                || self.runtime_controls.activation_sent
            {
                return Err(fail(
                    "The old Review expired before dispatch. Review the current conversation.",
                ));
            }
            self.ensure_drained()?;
            return self.register_runtime_apply(review.clone(), true);
        }
        if !self.ready() {
            return Err(fail(
                "Finish the pending management action before changing runtime controls",
            ));
        }
        self.ensure_drained()?;
        let policy = self.runtime_controls.policy.as_ref().ok_or_else(|| {
            fail("Runtime does not support activated Tools, Authority, and Plan controls")
        })?;
        let (mut command, kind) = match intent {
            view::Intent::Read(field) => {
                if field == controls::Field::ToolSelection && !policy.binding.tool_selection {
                    return Err(fail("Runtime did not negotiate individual tool controls"));
                }
                (
                    policy
                        .binding
                        .request(if field == controls::Field::ToolSelection {
                            controls::TOOL_LIST
                        } else {
                            controls::SNAPSHOT
                        }),
                    Read::Snapshot(field),
                )
            }
            view::Intent::Review {
                snapshot,
                change,
                source,
            } => {
                if self.runtime_controls.snapshot.as_ref() != Some(snapshot.as_ref()) {
                    return Err(fail("Runtime choice differs from the retained snapshot"));
                }
                policy.verify_stable(&snapshot.binding, snapshot.generation, &snapshot.facts)?;
                let mut command = snapshot.validate_request(&change)?;
                if let Some(source) = source {
                    command["source"] = serde_json::json!(source);
                }
                (command, Read::Review(snapshot, change))
            }
            view::Intent::Apply(review) => {
                if self.runtime_controls.review.as_ref() != Some(review.as_ref()) {
                    return Err(fail("Review runtime controls again before confirming"));
                }
                policy.verify_stable(&review.binding, review.generation, &review.before)?;
                return self.register_runtime_apply(review, false);
            }
        };
        let id = self
            .wire
            .register_request()
            .map_err(|_| fail("Cannot reserve runtime control request"))?;
        command["id"] = id.to_value();
        self.runtime_controls.review = None;
        self.runtime_controls.pending = Some(Pending { id, kind });
        Ok(command)
    }
    pub(super) fn runtime_response_pending(&self, frame: &Value) -> bool {
        matches!(frame["type"].as_str(), Some("response" | "error"))
            && self
                .runtime_controls
                .pending
                .as_ref()
                .is_some_and(|p| frame["id"] == p.id.to_value())
    }
    pub(super) fn runtime_response(&mut self, frame: Value) -> io::Result<Step> {
        let pending = self
            .runtime_controls
            .pending
            .take()
            .expect("runtime control response");
        if frame["ok"] != true {
            return if matches!(pending.kind, Read::Snapshot(_) | Read::Review(..)) {
                self.runtime_controls.snapshot = None;
                self.runtime_controls.review = None;
                Ok(Step::RuntimeControls(view::Update::Rejected(
                    "Runtime refused this review; no choice was applied.".into(),
                )))
            } else {
                Err(fail(
                    "Runtime control action lost its exact typed result/readback",
                ))
            };
        }
        let value = &frame["result"];
        match pending.kind {
            Read::Snapshot(field) => {
                let policy = self
                    .runtime_controls
                    .policy
                    .as_ref()
                    .expect("active control policy");
                let snapshot = if field == controls::Field::ToolSelection {
                    controls::Snapshot::parse_tool_list(value, &policy.binding)?
                } else {
                    controls::Snapshot::parse(value, &policy.binding)?
                };
                policy.verify_stable(&snapshot.binding, snapshot.generation, &snapshot.facts)?;
                self.runtime_controls.snapshot = Some(snapshot.clone());
                Ok(Step::RuntimeControls(view::Update::Snapshot {
                    field,
                    snapshot: Box::new(snapshot),
                }))
            }
            Read::Review(snapshot, change) => {
                let review = controls::Review::parse(value, &snapshot, &change)?;
                self.runtime_controls.review = Some(review.clone());
                Ok(Step::RuntimeControls(view::Update::Review(Box::new(
                    review,
                ))))
            }
            Read::Apply { historical } => {
                let invocation = self
                    .runtime_attempt_mut(historical)
                    .expect("consumed control");
                let receipt =
                    controls::Receipt::parse(value, &invocation.review, &invocation.apply_id)?;
                if historical && receipt.status != controls::Status::Refused {
                    return Err(fail("Retired Review claimed runtime effects"));
                }
                if self.closing {
                    self.runtime_attempt_mut(historical)
                        .expect("consumed control")
                        .receipt = Some(receipt);
                    return Ok(Step::Ignore);
                }
                let mut command = receipt.readback_request()?;
                let id = self
                    .wire
                    .register_request()
                    .map_err(|_| fail("Cannot reserve runtime control readback"))?;
                command["id"] = id.to_value();
                let invocation = self
                    .runtime_attempt_mut(historical)
                    .expect("consumed control");
                invocation.readback_id = Some(rpc_id(&id)?);
                invocation.receipt = Some(receipt);
                self.runtime_controls.pending = Some(Pending {
                    id,
                    kind: Read::Readback { historical },
                });
                Ok(Step::Write(command))
            }
            Read::Readback { historical } => {
                let invocation = self
                    .runtime_attempt_mut(historical)
                    .expect("consumed control");
                let readback = controls::Readback::parse(
                    value,
                    invocation
                        .receipt
                        .as_ref()
                        .expect("published control result"),
                )?;
                if value["readback_operation_id"] != pending.id.to_value() {
                    return Err(fail("Runtime control readback changed its actual command"));
                }
                if historical
                    && (readback.release == controls::Release::Install
                        || readback.current.is_some()
                        || readback.owner.is_some())
                {
                    return Err(fail(
                        "Historical refusal supplied current policy or installation authority",
                    ));
                }
                invocation.readback = Some(readback.clone());
                // Supervisor checks actual Turn ownership before future-seat installation.
                Ok(Step::RuntimeControls(view::Update::Settled(Box::new(
                    readback,
                ))))
            }
        }
    }
    pub fn runtime_control_receipt(
        &self,
        readback: &controls::Readback,
    ) -> Option<&controls::Receipt> {
        [
            &self.runtime_controls.invocation,
            &self.runtime_controls.historical,
        ]
        .into_iter()
        .flatten()
        .find(|attempt| attempt.readback.as_ref() == Some(readback))?
        .receipt
        .as_ref()
    }
    pub fn install_runtime_readback(&mut self, readback: &controls::Readback) -> io::Result<()> {
        if let Some(attempt) = self.runtime_controls.historical.as_mut()
            && attempt.readback.as_ref() == Some(readback)
        {
            if attempt.released
                || readback.release != controls::Release::Unchanged
                || readback.status != controls::Status::Refused
            {
                return Err(fail(
                    "Historical runtime choice lacks a single verified no-effect refusal",
                ));
            }
            attempt.released = true;
            return Ok(());
        }
        let invocation = self
            .runtime_controls
            .invocation
            .as_ref()
            .ok_or_else(|| fail("Unsolicited runtime control installation"))?;
        if invocation.released || invocation.readback.as_ref() != Some(readback) {
            return Err(fail(
                "Runtime control installation changed its exact readback",
            ));
        }
        match readback.release {
            controls::Release::Install => {
                let policy = readback
                    .policy()
                    .ok_or_else(|| fail("Unverified runtime policy installation"))?;
                if !self.closing {
                    self.install_future_work_seat(&policy)?;
                }
                if let Some(expected) = self.expected.as_mut() {
                    expected.3 = policy.facts.effective_profile.as_str().to_owned();
                }
                self.runtime_controls.policy = Some(policy.clone());
                self.runtime_controls.policy_update = Some(policy);
            }
            controls::Release::Unchanged => {
                let current = readback
                    .current
                    .as_ref()
                    .ok_or_else(|| fail("Current control refusal omitted observed policy"))?;
                let policy = self
                    .runtime_controls
                    .policy
                    .as_ref()
                    .expect("published policy");
                policy.verify_stable(&readback.binding, policy.generation, current)?;
            }
            controls::Release::Close => {
                return Err(fail(
                    "Runtime choice could not be independently verified; close is required",
                ));
            }
        }
        self.runtime_controls
            .invocation
            .as_mut()
            .expect("consumed control")
            .released = true;
        Ok(())
    }
    pub fn verify_runtime_shutdown(&mut self, envelope: &Value) -> io::Result<()> {
        let value = envelope.get("runtime_controls_shutdown");
        let historical = envelope.get("runtime_controls_historical_shutdown");
        let Some(policy) = self.runtime_controls.policy.as_ref() else {
            if let Some(previous) = self.runtime_controls.retired_shutdown.as_ref() {
                if value != Some(previous) {
                    return Err(fail(
                        "Runtime close omitted exact predecessor control proof",
                    ));
                }
            } else if value.is_some() || historical.is_some() {
                return Err(fail(
                    "Unactivated runtime supplied control shutdown authority",
                ));
            } else {
                return Ok(());
            }
            return self.verify_historical_runtime_shutdown(historical);
        };
        let value =
            value.ok_or_else(|| fail("Activated runtime omitted control shutdown proof"))?;
        let proof = controls::Shutdown::parse(value, &policy.binding)?;
        if let Some(p) = self.runtime_controls.invocation.as_ref() {
            proof.verify_invocation(
                &p.review,
                &p.apply_id,
                p.receipt.as_ref(),
                p.readback.as_ref(),
                p.readback_id.as_deref(),
                policy.generation,
            )?;
        } else {
            proof.verify_idle(policy.generation)?;
        }
        self.verify_historical_runtime_shutdown(historical)?;
        self.runtime_controls.retired_shutdown = Some(value.clone());
        self.runtime_controls.retired_review = self.runtime_controls.review.take();
        self.runtime_controls.activation_sent = false;
        self.runtime_controls.retired_policy = self.runtime_controls.policy.take();
        self.runtime_controls.policy_update = None;
        Ok(())
    }
    fn verify_historical_runtime_shutdown(&self, value: Option<&Value>) -> io::Result<()> {
        let value =
            value.ok_or_else(|| fail("Control shutdown omitted paired historical proof"))?;
        match self.runtime_controls.historical.as_ref() {
            None if value.is_null() => Ok(()),
            Some(p) => controls::HistoricalShutdown::parse(value, &p.review.binding)?
                .verify_invocation(
                    &p.review,
                    &p.apply_id,
                    p.receipt.as_ref(),
                    p.readback.as_ref(),
                    p.readback_id.as_deref(),
                ),
            _ => Err(fail("Unknown historical control shutdown obligation")),
        }
    }
}
