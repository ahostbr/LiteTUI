"""Plain CLI registration failure preserves live UI ownership, not fleet readiness."""

import asyncio

import pytest

from litetui import app as app_module
from litetui import cli, harness, image_viewer, runtime_log, settings
from litetui.agent_launch_context import acquire
from litetui.agent_ownership import OwnershipError
from litetui.llm_backend import BackendError
from litetui.seat_authority import AGENT_SHELL_MARKERS


def test_plain_cli_failed_registration_can_stage_until_normal_shutdown(
    tmp_path, monkeypatch
):
    for name in (
        *AGENT_SHELL_MARKERS,
        harness.SPAWN_IDENTITY_MARKER,
        "LITESUITE_CANVAS_AGENT",
        "LITEHARNESS_TIER",
        "LITEHARNESS_AGENT_NAME",
        "LITEHARNESS_SPAWNED_BY",
    ):
        monkeypatch.delenv(name, raising=False)
    cfg = settings.Settings()
    cfg.default_model = None
    cfg.user_name_asked = True
    cfg.user_name = "Offline fixture"
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    monkeypatch.setattr(cli.sys, "argv", ["litetui"])
    calls = []
    sessions = []
    agent_ids = []
    private = "FORBIDDEN_OWNED_FAILURE_PAYLOAD"
    captured_logs = []
    messages = []
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(app_module, "_INBOX_SETTLE_S", 0)

    def failed_registration(seat):
        calls.append("register")
        seat.error = private
        return False

    monkeypatch.setattr(harness.Seat, "register", failed_registration)

    class OfflineApp(app_module.LiteTUI):
        def run(self, **kwargs):
            # Only the terminal pump is replaced. CLI ordinary ownership,
            # construction, registration and conversation staging are real.
            self._system = messages.append
            self._cli_args_done = None
            recorder = runtime_log.RuntimeRecorder(
                tmp_path / "captured" / "runtime.jsonl"
            )
            monkeypatch.setattr(runtime_log, "_ACTIVE", recorder)
            captured_logs.append(recorder)
            session = self._agent_session
            sessions.append(session)
            agent_ids.append(session.authority.agent_id)
            assert session.authority.name == "LiteTUI"
            before = (session.memory_root / "settings.json").read_bytes()
            assert asyncio.run(self._register_owned_startup()) is False
            assert session.authority.name == "LiteTUI"
            assert self._cli_launch_error == (
                "Owned agent registration blocked: owned-registration-failed; see runtime-errors.log"
            )
            self._new_convo()
            assert self.store.convo_dir == session.conversation_directory(self.convo_id)
            assert not self.store.convo_dir.exists()  # staging is not a disk write
            assert not self.seat.registered
            assert asyncio.run(self._register_owned_startup()) is False
            assert calls == ["register"]
            asyncio.run(app_module.LiteTUI._inbox_monitor.__wrapped__(self))
            assert self._seat_started
            assert len(messages) == 1 and "sending is disabled" in messages[0]
            assert private not in self.seat.error
            assert private not in self._owned_launch_error
            assert private not in self._cli_launch_error
            assert private not in messages[0]
            with pytest.raises(BackendError, match="Choose a model"):
                asyncio.run(self._ensure_chat_ready())
            assert (session.memory_root / "settings.json").read_bytes() == before

    monkeypatch.setattr(app_module, "LiteTUI", OfflineApp)
    try:
        cli.main()
        recorder = captured_logs[0]
        metadata = recorder.path.read_text(encoding="utf-8")
        errors = recorder.errors_path.read_text(encoding="utf-8")
        assert "harness_registration_failed" in metadata  # old producer was reached
        assert "harness_registration_diagnostic" in errors
        assert "owned-startup" in errors
        assert "event=harness_registration_failed" not in errors  # raw path suppressed
        assert private not in metadata and private not in errors
    finally:
        for recorder in captured_logs:
            recorder.close()
    with pytest.raises(OwnershipError, match="not owned"):
        _ = sessions[0].authority
    # The real CLI finally releases; another owner can reopen normally.
    with acquire(tmp_path, "LiteTUI") as reopened:
        assert reopened.authority.agent_id == agent_ids[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("rpc,spawned", [(False, False), (True, False), (False, True)])
async def test_failed_registration_retains_only_ordinary_interactive_session(
    tmp_path, rpc, spawned
):
    from types import SimpleNamespace

    from litetui.agent_launch_context import ordinary

    cfg = settings.Settings()
    cfg.default_model = "fixture-model"
    session = ordinary(tmp_path, cfg)
    calls = []
    seat = SimpleNamespace(registered=False, error="fixture refusal")
    seat.register = lambda: calls.append("register") or False
    app = SimpleNamespace(
        _agent_session=session,
        seat=seat,
        _rpc=rpc,
        _spawned_marker=spawned,
        _owned_registration_lock=asyncio.Lock(),
        _owned_launch_error=None,
        _cli_launch_error=None,
        store=SimpleNamespace(release=lambda: calls.append("conversation-release")),
    )
    try:
        assert not await app_module.LiteTUI._register_owned_startup(app)
        assert not await app_module.LiteTUI._register_owned_startup(app)
        if rpc or spawned:
            assert calls == ["register", "conversation-release"]
            with pytest.raises(OwnershipError, match="not owned"):
                _ = session.authority
        else:
            assert calls == ["register"]
            assert session.authority.name == "LiteTUI"
            app._register_owned_startup = lambda: (
                app_module.LiteTUI._register_owned_startup(app)
            )
            with pytest.raises(BackendError, match="Owned agent registration blocked"):
                await app_module.LiteTUI._ensure_chat_ready(app)
            assert calls == ["register"]
        assert (
            app._owned_launch_error
            == "Owned agent registration blocked: owned-registration-failed; see runtime-errors.log"
        )
    finally:
        session.release()
