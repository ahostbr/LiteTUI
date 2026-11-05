"""Actual SDK transcript forks and a controlled, model-free SDK client."""

import asyncio
import json
import os
from uuid import uuid4

from claude_agent_sdk._internal.sessions import _canonicalize_path, _get_project_dir
from claude_agent_sdk.types import AssistantMessage, ResultMessage, TextBlock

OLD = "12345678-1111-4111-8111-123456789abc"
report = {}


def _path(workspace, session):
    directory = _get_project_dir(_canonicalize_path(str(workspace)))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (session + ".jsonl")


class Client:
    def __init__(self, options):
        self.options = options
        self.frames = asyncio.Queue()

    async def connect(self):
        pass

    async def get_server_info(self):
        return {}

    async def get_context_usage(self):
        return {"maxTokens": 200000, "totalTokens": 120}

    async def query(self, prompt):
        native = self.options.resume
        bootstrap = prompt == "fixture-bootstrap"
        if not bootstrap:
            assert native and native != OLD
            path = _path(self.options.cwd, native)
            rows = [json.loads(r) for r in path.read_bytes().splitlines()]
            last = [r for r in rows if r["type"] == "assistant"][-1]
            ident, answer = str(uuid4()), str(uuid4())
            message_id = "msg_" + uuid4().hex
            response = "Controlled Claude fork response"
            pair = [
                {
                    "type": "user",
                    "sessionId": native,
                    "uuid": ident,
                    "parentUuid": last["uuid"],
                    "message": {"role": "user", "content": prompt},
                },
                {
                    "type": "assistant",
                    "sessionId": native,
                    "uuid": answer,
                    "parentUuid": ident,
                    "message": {
                        "role": "assistant",
                        "id": message_id,
                        "content": [{"type": "text", "text": response}],
                    },
                },
            ]
            if os.environ["RUST_REWRITE_CASE"] == "claude-settled-drift":
                pair[-1]["message"]["id"] = "unrelated-native-message"
            with path.open("ab") as out:
                for row in pair:
                    out.write((json.dumps(row) + "\n").encode())
            await self.frames.put(
                AssistantMessage(
                    content=[TextBlock(response)],
                    model=self.options.model,
                    message_id=message_id,
                    uuid=answer,
                )
            )
            report.setdefault("queries", []).append({"session": native, "message": prompt, "native": pair})
        result = str(uuid4())
        if not bootstrap:
            report["result"] = result
        await self.frames.put(
            ResultMessage(
                subtype="success",
                duration_ms=1,
                duration_api_ms=1,
                is_error=False,
                num_turns=1,
                session_id=native,
                uuid=result,
                terminal_reason="completed",
                result="complete",
            )
        )

    async def receive_messages(self):
        while True:
            yield await self.frames.get()

    async def disconnect(self):
        pass


async def metadata(backend):
    backend.models = {"claude-fixture": {"value": "claude-fixture"}}
    return "ok"


def seed(app, case, root):
    from litetui import store_identity
    from litetui.claude_turn import launch_workspace, ledger_for

    os.environ["CLAUDE_CONFIG_DIR"] = str(root / "isolated-claude-sdk")
    app.conversation = [
        app.conversation[0],
        {"role": "system", "content": "Keep the system prompt"},
        {"role": "system", "content": "Keep this other saved system context"},
        {"role": "user", "content": "PREFIX USER: keep this earlier request"},
        {"role": "assistant", "content": "PREFIX FINAL ANSWER"},
        *app.conversation[5:],
    ]
    ledger = ledger_for(app)
    segment = ledger.select_segment(launch_workspace(app))
    ledger.fix_system_prompt(
        segment["id"],
        "Controlled immutable Claude instructions",
        store_identity=store_identity.capture(app),
        store_injected=True,
    )
    ledger.bind_session(segment["id"], OLD)
    native = []
    for i in [3, 5, 7]:
        msg, reply = app.conversation[i : i + 2]
        e = ledger.prepare(segment["id"], msg["content"], "strict", "rpc")
        ledger.update_delivery(e["id"], "submitted")
        ledger.update_delivery(e["id"], "terminal", stop_reason="stop")
        msg["claude_delivery"] = {"id": e["id"], "segment_id": segment["id"], "state": "prepared"}
        user, assistant = str(uuid4()), str(uuid4())
        model_id = "msg_" + uuid4().hex
        native.extend(
            [
                {
                    "type": "user",
                    "sessionId": OLD,
                    "uuid": user,
                    "parentUuid": native[-1]["uuid"] if native else None,
                    "message": {"role": "user", "content": msg["content"]},
                },
                {
                    "type": "assistant",
                    "sessionId": OLD,
                    "uuid": assistant,
                    "parentUuid": user,
                    "message": {
                        "role": "assistant",
                        "id": model_id,
                        "content": [{"type": "text", "text": reply["content"]}],
                    },
                },
            ]
        )
        reply["claude_native"] = {
            "segment_id": segment["id"],
            "session_id": OLD,
            "delivery_id": e["id"],
            "activities": [],
            "message_ids": [model_id],
        }
    path = _path(segment["workspace"], OLD)
    path.write_bytes(b"".join((json.dumps(v) + "\n").encode() for v in native))
    report.update(original=path.read_bytes().hex(), path=str(path), segment=segment["id"])
    if case == "claude-partial":
        save = ledger._save

        def fail_selection():
            if ledger.selected["id"] != segment["id"]:
                raise OSError("controlled failure while saving the selected fork segment")
            save()

        ledger._save = fail_selection
    if case == "claude-forged":
        app.conversation[6]["claude_native"]["message_ids"] = ["another-model-id"]


async def start(app):
    from litetui.claude_turn import effort_for, ledger_for, session_for

    session, _, _ = await session_for(app, app.backend, ledger_for(app).selected, effort_for(app))
    await session.query("bootstrap", "fixture-bootstrap")
    async for _ in session.events():
        pass
