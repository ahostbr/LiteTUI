"""Opt-in live disk/native-process restart probe; evidence contains only counts/flags."""

import argparse
import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS

from litetui.codex_app_server import AppServer, AppServerTransport
from litetui.codex_trace import records
from litetui.conversation import ConversationRepository
from litetui.model_transport import collect


async def run(output):
    from litetui.model_transport import credential_path

    with tempfile.TemporaryDirectory(prefix="litetui-history-restart-") as folder:
        root = Path(folder)
        shutil.copy2(credential_path("codex"), root / "auth.json")
        transcript = root / "convo.jsonl"
        calls = []
        tools = [{"type": "function", "function": {
            "name": "echo", "description": "Return a synthetic test marker.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }}]
        messages = [
            {"role": "system", "content": "Synthetic test. Call litetui_echo exactly once, then reply OK. Do not use any other tools."},
            {"role": "user", "content": "Call litetui_echo once then reply OK."},
        ]

        def snapshot(conversation):
            transcript.write_text(json.dumps({"type": "snapshot", "messages": conversation}) + "\n",
                                  encoding="utf-8")

        async def execute(name, arguments):
            calls.append(name)
            return "ECHO_OK", True

        def make_app(conversation):
            app = NS(conversation=conversation, backend=NS(models={}), tools_enabled=True,
                     _active_tool_profile="scheduled", _rpc=True, _stop_requested=False,
                     _rpc_emit=lambda event: None, _execute_tool=execute,
                     plugins=NS(tool_specs=lambda: tools, deferred_specs=list))

            def edit(index, reason):
                with transcript.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "edit", "index": index,
                                             "message": app.conversation[index]}) + "\n")
            app._edit = edit
            return app

        class ObservedServer(AppServer):
            def __init__(self):
                super().__init__()
                self.environment["CODEX_HOME"] = str(root)
                self.methods = []

            async def request(self, method, params):
                self.methods.append(method)
                return await super().request(method, params)

        first = ObservedServer()
        snapshot(messages)
        try:
            stream = await AppServerTransport(first, make_app(messages)).create(
                model="gpt-6-astra", messages=messages, tools=tools, stream=True,
                extra_body={"reasoning_effort": "medium"})
            await collect(stream)
            original = records(messages[-1].get("provider_metadata"))
            expected = {(r.get("turnId"), r["id"]) for r in original}
            assert expected and calls == ["echo"], "synthetic turn did not produce the expected trace"
            process = first.process
        finally:
            await first.close()
        first_closed = process.returncode is not None
        # Simulate display persistence lag, retaining only the accepted thread/turn
        # reference. This is deliberate test-fixture damage, not an application edit.
        _, saved = ConversationRepository.read(transcript)
        for message in saved:
            message.get("provider_metadata", {}).pop("display_trace", None)
        snapshot(saved)
        _, reopened = ConversationRepository.read(transcript)
        second = ObservedServer()
        try:
            app = make_app(reopened)
            transport = AppServerTransport(second, app)
            changed = await transport.refresh_history()
            recovered = records(reopened[-1].get("provider_metadata"))
            identities = {(r.get("turnId"), r["id"]) for r in recovered}
            assert changed > 0 and expected <= identities, "native read did not recover the saved trace"
            assert len(identities) == len(recovered), "recovery duplicated items"
            assert any(r.get("kind") == "dynamicToolCall" and "ECHO_OK" in r.get("result", "")
                       and r.get("ok") is True for r in recovered), "tool result was not recovered"
            assert any(r.get("kind") == "agentMessage" and "OK" in r.get("result", "")
                       and r.get("state") == "completed" for r in recovered), "answer was not recovered"
            _, reloaded = ConversationRepository.read(transcript)
            assert records(reloaded[-1].get("provider_metadata")) == recovered
            assert await transport.refresh_history() == 0, "repeat read was not idempotent"
            assert "turn/start" not in second.methods and "thread/resume" not in second.methods
            assert calls == ["echo"], "history replay executed a tool"
            second_process = second.process
        finally:
            await second.close()
        evidence = {"initial_turn_completed": True, "initial_host_calls": len(calls),
                    "first_process_closed": first_closed,
                    "second_process_closed": second_process.returncode is not None,
                    "recovered_items": len(recovered), "native_identities_preserved": True,
                    "tool_result_recovered": True, "assistant_answer_recovered": True,
                    "disk_reload_matches": True, "repeat_read_idempotent": True,
                    "recovery_turn_start_count": second.methods.count("turn/start"),
                    "recovery_thread_resume_count": second.methods.count("thread/resume"),
                    "recovery_read_count": second.methods.count("thread/read")}
    evidence["temporary_home_removed"] = not root.exists()
    output.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(evidence))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.output))
