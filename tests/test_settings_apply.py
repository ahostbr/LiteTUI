from litetui.settings_apply import (
    PersistenceDestinationResult,
    RuntimeSettingStatus,
    SettingsSaveResult,
)


def test_save_result_distinguishes_partial_persistence_and_runtime_pending():
    result = SettingsSaveResult(
        persistence=(
            PersistenceDestinationResult(
                "defaults", "defaults", True, revision="rev-defaults",
                fields=("temperature",),
            ),
            PersistenceDestinationResult(
                "conversation", "conversation", False, "locked",
                revision="rev-conversation", fields=("backend",),
            ),
        ),
        runtime=(
            RuntimeSettingStatus(
                "backend", "conversation", "pending", "reconnect",
                requested="lmstudio", effective="codex",
                reason="Reconnect required",
            ),
        ),
    )

    assert not result.fully_saved
    assert result.has_pending_runtime
    assert not result.has_runtime_failures
    assert result.persistence[0].saved is True
    assert result.persistence[0].revision == "rev-defaults"
    assert result.persistence[0].fields == ("temperature",)
    assert result.persistence[1].error == "locked"


def test_runtime_failure_retains_requested_and_effective_values():
    status = RuntimeSettingStatus(
        "thinking_level", "conversation", "failed", "retry",
        requested="xhigh", effective="medium", reason="Model rejected level",
    )
    result = SettingsSaveResult(runtime=(status,))

    assert result.has_runtime_failures
    assert not result.has_pending_runtime
    assert status.requested == "xhigh"
    assert status.effective == "medium"
    assert status.action == "retry"
