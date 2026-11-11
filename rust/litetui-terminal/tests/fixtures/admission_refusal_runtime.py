"""Real floor/hooks/Work/RPC, with only Codex transport and hook effects controlled."""

from __future__ import annotations

import asyncio
import copy
import importlib.util
import json
import os
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    "_refusal_common", Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
)
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
from litetui import background_input, fleet_policy, hook_host, lifecycle_hooks, rpc
from litetui.codex_app_server import AppServer

root = Path(os.environ["LITETUI_DATA_ROOT"])
report = {"provider_requests": [], "refusals": [], "hook_calls": []}
original_seed = common.seed
original_serve = common.serve
original_settle = common.history.settle


def save():
    (root / "admission-observations.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


def seed(app):
    original_seed(app)
    assert app.backend.name == "codex" and app.thinking_level == "low"
    report["initial_history"] = copy.deepcopy(app.conversation)
    policy = copy.deepcopy(fleet_policy.DEFAULT_POLICY)
    policy["floors"]["codex"]["min_thinking_level"] = "medium"
    path = root / "fixture-fleet-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    os.environ[fleet_policy.POLICY_ENV] = str(path)
    os.environ.pop("LITETUI_HOOKS", None)
    hook = lifecycle_hooks.Hook(
        "controlled-denial", ("prompt_before",), "unused-controlled-hook", mode="gate"
    )
    (root / "hooks.json").write_text(
        json.dumps({"version": 1, "hooks": [hook.document()]}), encoding="utf-8"
    )
    assert hook_host.snapshot(app).hooks
    save()


async def request(server, method, params):
    if method == "account/read":
        return {"account": {"type": "chatgpt"}}
    if method == "model/list":
        return {
            "data": [
                {
                    "model": "gpt-6.1-sol",
                    "displayName": "Controlled Codex",
                    "defaultReasoningEffort": "medium",
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": level}
                        for level in ("low", "medium", "high")
                    ],
                }
            ]
        }
    if method == "thread/start":
        assert not report["provider_requests"]
        return {"thread": {"id": "controlled-refusal-thread"}}
    if method == "thread/loaded/list":
        return {"data": [], "nextCursor": None}
    if method == "turn/start":
        assert (
            params["threadId"] == "controlled-refusal-thread"
            and params["effort"] == "medium"
        )
        assert any(
            item.get("text") == "explicit valid recovery" for item in params["input"]
        )
        report["provider_requests"].append(copy.deepcopy(params))
        save()
        turn = "controlled-recovery-turn"
        for event in [
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": params["threadId"],
                    "turnId": turn,
                    "itemId": "controlled-answer",
                    "delta": "CONTROLLED CODEX RECOVERY",
                },
            },
            {
                "method": "turn/completed",
                "params": {
                    "threadId": params["threadId"],
                    "turn": {"id": turn, "status": "completed"},
                },
            },
        ]:
            await server.events.put(event)
        return {"turn": {"id": turn}}
    raise AssertionError("Uncontrolled Codex method: " + method)


async def invoke(app, hook, document, profile, **kwargs):
    assert hook.id == "controlled-denial" and document["event"] == "prompt_before"
    report["hook_calls"].append(copy.deepcopy(document))
    save()
    await asyncio.sleep(0.01)
    denied = document["data"]["prompt"] == "deny this hook"
    return lifecycle_hooks.HookResult(
        not denied, "Controlled asynchronous hook denial" if denied else ""
    )


async def settle(app):
    await original_settle(app)
    # Textual can cancel a superseded startup task before _run enters its
    # cancellation handler. Join it explicitly; a stale RUNNING enum is not
    # evidence that a cancelled task is still alive (notably Python 3.14).
    from textual.worker import WorkerCancelled

    for worker in common.workers:
        if worker.group == "ctx" and worker.is_cancelled and not worker.is_finished:
            task = worker._task
            assert task is not None and task.done() and task.cancelled()
            try:
                await asyncio.wait_for(worker.wait(), 2)
            except WorkerCancelled:
                pass
            assert worker.is_finished and worker._task.done()
            report.setdefault("startup_cancelled_context_joined", []).append(
                worker.name
            )
            save()


async def serve(app):
    original_emit = rpc.rpc_emit

    def emit(row):
        if (
            row.get("type") == "gui.event"
            and row.get("event", {}).get("phase") == "pre_admission"
        ):
            bridge = background_input.bridge(app)
            report["refusals"].append(
                {
                    "frame": copy.deepcopy(row),
                    "history": copy.deepcopy(app.conversation),
                    "executions": list(bridge.executions),
                    "inputs": list(bridge.inputs),
                }
            )
            save()
        original_emit(row)

    with (
        patch.object(rpc, "rpc_emit", emit),
        patch.object(hook_host, "_invoke", invoke),
    ):
        await original_serve(app)


common.seed = seed
common.history.settle = settle
common.history.serve = serve
if __name__ == "__main__":
    with (
        patch.object(AppServer, "start", common.history.codex_start),
        patch.object(AppServer, "close", common.history.codex_close),
        patch.object(AppServer, "request", request),
    ):
        common.history.main()
