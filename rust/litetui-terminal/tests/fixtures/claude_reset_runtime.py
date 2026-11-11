"""Real source and pinned SDK message types with a private in-memory SDK transport."""
from __future__ import annotations

import copy
import importlib.util
import os
from pathlib import Path
from unittest.mock import patch

import claude_agent_sdk

spec = importlib.util.spec_from_file_location("_reset_claude", Path(__file__).with_name("claude_queue_runtime.py"))
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
controls = fixture.controls
report = controls.report


class Client(fixture.Client):
    async def query(self, prompt):
        (controls.root / "release-stream").touch()
        report.setdefault("reset_sdk_options", []).append({"resume": self.options.resume, "prompt": prompt})
        await super().query(prompt)
    async def disconnect(self):
        await super().disconnect()
        report["reset_sdk_closes"] = report.get("reset_sdk_closes", 0) + 1


if __name__ == "__main__":
    from litetui import native_claude_reset as reset
    from litetui import plugins
    from litetui.claude_backend import ClaudeBackend
    from litetui.conversation import ConversationRepository

    original_register = plugins.register_plugins
    def registered(app, registry, order=None, disabled=frozenset()):
        return original_register(app, registry, order=("litetui.plugins.core_tools", "litetui.plugins.convo",
            "litetui.plugins.self_compact", "litetui.plugins.misc"), disabled=disabled)

    actual_dispatch = reset.dispatch
    async def audited(app, cmd):
        phase = cmd["type"].removeprefix(reset.PREFIX)
        if phase == "review" and "reset_before" not in report:
            ledger = app._claude_ledger
            ledger.prepare(ledger.selected["id"], "Retained delivery from old segment", "strict", "typed")
            report["reset_before"] = {"identity": app.convo_id, "history": copy.deepcopy(app.conversation),
                "ledger": ledger.document, "transcript": app.convo_path.read_text(encoding="utf-8"),
                "owner": id(app.backend.session._owner), "session": id(app.backend.session)}
        result = await actual_dispatch(app, cmd)
        report.setdefault("reset_phases", []).append(phase)
        if phase == "review":
            assert report.get("reset_sdk_closes", 0) == 0
            assert app._claude_ledger.document == report["reset_before"]["ledger"]
        if phase == "readback":
            attempt = app._native_claude_reset
            assert reset.blocking(app) and attempt.session._owner.done()
            report["reset_after"] = {"identity": app.convo_id, "history": copy.deepcopy(app.conversation),
                "ledger": app._claude_ledger.document, "transcript": app.convo_path.read_text(encoding="utf-8"),
                "queries": len(report["claude_queries"]), "session": id(attempt.session),
                "owner": id(attempt.session._owner), "retired": attempt.session._owner.done()}
        return result

    original_release = ConversationRepository.release
    def release(repository, *args, **kwargs):
        app = controls.history.boundary.app_instance
        if app is not None and repository is app.store and repository.owned:
            report["reset_clear_on_release"] = getattr(app, "_native_claude_reset", None) is None
        return original_release(repository, *args, **kwargs)
    os.environ["CLAUDE_CONFIG_DIR"] = str(controls.root / "private-claude")
    with patch.object(ClaudeBackend, "ensure_running", fixture.metadata), patch.object(claude_agent_sdk, "ClaudeSDKClient", Client), patch.object(plugins, "register_plugins", registered), patch.object(reset, "dispatch", audited), patch.object(ConversationRepository, "release", release):
        controls.history.main()
