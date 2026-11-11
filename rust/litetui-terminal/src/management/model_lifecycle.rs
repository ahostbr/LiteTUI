//! Only the captured lifecycle operation may finish across its retired epoch.
use super::{Management, PreferencesRequest, SettingsRead, Snapshot, Step, fail};
use crate::work_protocol::Identity;
use serde_json::Value;
use std::io;

impl Management {
    pub(super) fn model_lifecycle_event(&self, frame: &Value, event: &Value) -> bool {
        matches!(&self.settings, Some((id, SettingsRead::Preferences(_, _, PreferencesRequest::ModelLifecycle(r))))
            if matches!(**r, crate::model_lifecycle::Request::Apply(_))
                && r.transition_target() == event["model"].as_str()
                && frame["operation_id"] == id.to_value()
                && matches!(frame.get("execution_id"), None | Some(Value::Null)))
    }

    pub(super) fn model_lifecycle_response_owner(
        &self,
        request: &PreferencesRequest,
        identity: &Identity,
        instance: &str,
    ) -> io::Result<bool> {
        let PreferencesRequest::ModelLifecycle(request) = request else {
            return Ok(false);
        };
        if request.transition_target().is_none() {
            return Ok(false);
        }
        if identity != &self.work_identity()?
            || self
                .execution_ledger()
                .is_none_or(|ledger| ledger.capture().instance != instance)
        {
            return Err(fail("Model transition response lost its captured origin"));
        }
        if request.retained_readback() {
            self.require_work_identity_rebind()?;
            if self.work_lifecycle_active() {
                return Err(fail("Model readback reused an active old epoch"));
            }
        }
        Ok(true)
    }

    pub(super) fn model_lifecycle_target_response(
        &mut self,
        frame: Value,
        update: Box<crate::model_lifecycle::Update>,
    ) -> io::Result<Step> {
        if frame["ok"] != true {
            return Err(fail("Cannot independently verify the loaded model policy"));
        }
        self.require_work_identity_rebind()?;
        let result = &frame["result"];
        let state = Snapshot::parse(result)?;
        let target = update
            .selected_model()
            .ok_or_else(|| fail("Model transition omitted settled target"))?;
        let (agent, schedules) = self
            .browser_authority
            .as_ref()
            .ok_or_else(|| fail("Model transition lacks ownership"))?;
        let expected = self
            .expected
            .as_ref()
            .ok_or_else(|| fail("Model transition lacks runtime"))?;
        if state.session != expected.0
            || state.backend != expected.1
            || state.model != target
            || state.schedules_paused != *schedules
            || state.held_restored_input.is_none()
            || result["ownership"]["agent_id"] != *agent
        {
            return Err(fail("Loaded model policy differs from the reviewed target"));
        }
        self.verify_target_profile(&state, &result["tool_policy"], &expected.3)?;
        if self.model_target_state.is_some() {
            return Err(fail("Earlier model readback still awaits capture"));
        }
        let expected = self.expected.as_mut().expect("checked");
        expected.2 = state.model.clone();
        expected.3 = state.profile.clone();
        self.model_target_state = Some((state.clone(), result["tool_policy"].clone()));
        self.origin = None;
        self.prompt = None;
        self.menu = None;
        Ok(Step::ModelLifecycleChanged {
            update,
            state,
            policy: result["tool_policy"].clone(),
        })
    }

    pub(super) fn model_lifecycle_identity_pending(&self) -> bool {
        matches!(&self.settings, Some((_, SettingsRead::Preferences(_, _, PreferencesRequest::ModelLifecycle(r))))
            if r.transition_target().is_some())
            || matches!(
                &self.settings,
                Some((_, SettingsRead::ModelLifecycleTarget(_)))
            )
    }
}
