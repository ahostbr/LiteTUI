"""Portable adapter contract: controlled external receipts, NOT paired OSS proof."""

import asyncio
import json
import os
import subprocess
from types import SimpleNamespace

import pytest

from litetui import app as app_module
from litetui import cli, harness, image_viewer, runtime_log, settings
from litetui.agent_launch_context import acquire
from litetui.agent_ownership import OwnershipError
from litetui.llm_backend import BackendError
from litetui.seat_authority import AGENT_SHELL_MARKERS

PRIVATE = "FORBIDDEN_EXTERNAL_TRACEBACK_PAYLOAD"


@pytest.mark.parametrize("receipt_shape", ["exact", "wrong-id", "duplicate", "refused"])
def test_plain_cli_adapter_requires_exact_owned_receipt_before_chat_readiness(
    tmp_path, monkeypatch, receipt_shape
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
    cfg.default_model = "offline-fixture-model"
    cfg.user_name_asked = True
    cfg.user_name = "Offline fixture"
    monkeypatch.setattr(settings, "load", lambda: cfg)
    monkeypatch.setattr(image_viewer, "init_image_backend", lambda: None)
    monkeypatch.setattr(cli.sys, "argv", ["litetui"])
    monkeypatch.setattr(harness, "harness_disabled", lambda: False)
    monkeypatch.setattr(harness, "_liteharness_exe", lambda: "fixture-liteharness")
    commands = []
    sessions = []
    agent_ids = []
    recorders = []
    readiness = []

    def fixture_transport(command, *, timeout):
        """Do not launch a child or import optional OSS in the portable suite."""
        assert command[:2] == ["fixture-liteharness", "register"]
        assert timeout == 30
        assert "--strict-identity" in command and "--takeover" not in command
        commands.append(list(command))

        def value(flag):
            return command[command.index(flag) + 1]

        receipt = {
            "agent_id": value("--agent-id"),
            "name": value("--name"),
            "backend": value("--backend"),
            "model": value("--model"),
            "thinking_level": value("--thinking-level"),
            "session_pid": int(value("--session-pid")),
        }
        assert receipt["session_pid"] == os.getpid()
        assert receipt["name"] == "LiteTUI"
        assert receipt["model"] == cfg.default_model
        if receipt_shape == "wrong-id":
            receipt["agent_id"] = "22222222-2222-4222-8222-222222222222"
        stdout = "Folder-owned identity: " + json.dumps(receipt) + "\n"
        if receipt_shape == "duplicate":
            stdout += stdout
        stderr = (
            "Traceback (most recent call last):\n"
            f'  File "{PRIVATE}", line 1, in {PRIVATE}\n'
            f"PermissionError: {PRIVATE}\n"
        )
        return subprocess.CompletedProcess(
            command, 1 if receipt_shape == "refused" else 0, stdout, stderr
        )

    monkeypatch.setattr(harness.ttyguard, "run", fixture_transport)

    class ReadyBackend:
        def __init__(self, name):
            self.name = name

        def base_url(self):
            return "https://offline.invalid/v1"

        async def ensure_chat_ready(self, model):
            readiness.append(model)

    def deny_provider(*args, **kwargs):
        raise AssertionError(
            "Offline adapter must not reach a provider or model operation"
        )

    monkeypatch.setattr(
        app_module.llm_backend,
        "make_backend",
        lambda config: ReadyBackend(config.backend),
    )
    monkeypatch.setattr(app_module.model_transport, "for_app", deny_provider)
    monkeypatch.setattr(
        app_module,
        "AsyncOpenAI",
        lambda **kwargs: SimpleNamespace(
            base_url=kwargs["base_url"],
            chat=SimpleNamespace(completions=SimpleNamespace(create=deny_provider)),
        ),
    )

    class OfflineApp(app_module.LiteTUI):
        def run(self, **kwargs):
            # Real ordinary CLI/session/app/Seat/adapter; replace terminal pump
            # and backend readiness only. No provider load or request occurs.
            recorder = runtime_log.RuntimeRecorder(
                tmp_path / "captured" / "runtime.jsonl"
            )
            monkeypatch.setattr(runtime_log, "_ACTIVE", recorder)
            recorders.append(recorder)
            session = self._agent_session
            sessions.append(session)
            authority = session.authority
            agent_ids.append(authority.agent_id)
            before = (session.memory_root / "settings.json").read_bytes()
            self.backend = ReadyBackend(authority.backend)
            self.model_id = authority.model
            self._thinking_level = authority.thinking_level
            self.model_rows = {}
            self._effective_request_overrides = dict
            succeeded = receipt_shape == "exact"
            assert asyncio.run(self._register_owned_startup()) is succeeded
            assert self.seat.registered is succeeded
            assert (
                commands[0][commands[0].index("--agent-id") + 1] == authority.agent_id
            )
            self._new_convo()
            assert self.store.convo_dir == session.conversation_directory(self.convo_id)
            assert not self.store.convo_dir.exists()
            if succeeded:
                asyncio.run(self._ensure_chat_ready())
                assert readiness == [authority.model]
                assert self._owned_launch_error is None and self.seat.error is None
            else:
                with pytest.raises(
                    BackendError, match="Owned agent registration blocked"
                ):
                    asyncio.run(self._ensure_chat_ready())
                assert readiness == []
                assert PRIVATE not in self.seat.error + self._owned_launch_error
            assert asyncio.run(self._register_owned_startup()) is succeeded
            assert len(commands) == 1  # no suffix, takeover or whole-registration retry
            assert session.authority.agent_id == authority.agent_id
            assert (session.memory_root / "settings.json").read_bytes() == before

    monkeypatch.setattr(app_module, "LiteTUI", OfflineApp)
    try:
        cli.main()  # no --agent, --model, --rpc or spawn marker: plain handlaunch
        recorder = recorders[0]
        for path in (recorder.path, recorder.errors_path):
            if path.exists():
                assert PRIVATE not in path.read_text(encoding="utf-8")
    finally:
        for recorder in recorders:
            recorder.close()
    with pytest.raises(OwnershipError, match="not owned"):
        _ = sessions[0].authority
    with acquire(tmp_path, "LiteTUI") as reopened:
        assert reopened.authority.agent_id == agent_ids[0]
