"""T1082 D2: a scheduled prompt that starts with "/loop" must NOT fire Claude Code's
NATIVE /loop when LiteTUI's Claude backend delivers it (clause (d): "if a scheduled
prompt has a /loop command in it so be it ... it runs at the scheduled level").

LiteTUI delivers a scheduled turn's text unchanged (claude_turn: session.query(entry_id,
item["content"])). Three guards in ClaudeBackend._options should keep it text:
verbatim_prompts (client_composed), --disable-slash-commands, and skills=[] + tools=[]
(no CronCreate / ScheduleWakeup / Skill).

CONTROL (init only, no user turn): the same options with the slash-command and skill
guards removed must LIST "loop". Otherwise the probe cannot see a reachable /loop, and
the main run's silence proves nothing.

MAIN (one haiku turn): LiteTUI's own options plus the ClaudeTools bridge. Send the
prompt, read to the result, then listen 75 s: a 1-minute loop would fire in that window.
PASS = all of:
  (a) init lists no "loop" command and none of CronCreate / ScheduleWakeup / Skill;
  (b) the replayed user message is the prompt text verbatim;
  (c) no tool_use named CronCreate / ScheduleWakeup / Skill;
  (d) no message at all in the 75 s after the result;
  (e) no .claude/scheduled_tasks.json under the temp cwd.
Remote only (Claude), CPU only, no model load. setting_sources=[]: Ryan's user hooks
never run, so no seat registers.

    LITETUI_CLAUDE_LIVE=1 python e2e/claude_loop_probe.py [artifact.json]
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

PROMPT = "/loop 1m reply exactly: loop-probe"
LOOP_TOOLS = {"CronCreate", "ScheduleWakeup", "Skill"}
LISTEN_S = 75


def _names(items) -> list[str]:
    return [i.get("name") if isinstance(i, dict) else str(i) for i in (items or [])]


async def control(backend, root: Path) -> dict:
    from claude_agent_sdk import ClaudeSDKClient
    opts = await backend._options(cwd=root, model="haiku")
    # None = the CLI's own defaults (bundled skills such as /loop), NOT "all", which
    # would also turn setting sources on and run Ryan's user hooks.
    opts.skills = None
    opts.extra_args = {k: v for k, v in opts.extra_args.items() if k != "disable-slash-commands"}
    async with ClaudeSDKClient(opts) as client:
        info = await client.get_server_info() or {}
    return {"commands": _names(info.get("commands"))}


async def main_run(backend, bridge, root: Path) -> dict:
    from claude_agent_sdk import ClaudeSDKClient
    opts = await backend._options(cwd=root, model="haiku", **bridge.sdk_options())
    out: dict = {"messages": [], "tool_uses": [], "late": []}
    async with ClaudeSDKClient(opts) as client:
        info = await client.get_server_info() or {}
        out["commands"] = _names(info.get("commands"))
        await client.query(PROMPT)
        async for message in client.receive_response():
            kind = type(message).__name__
            data = getattr(message, "data", None)
            if kind == "SystemMessage" and getattr(message, "subtype", None) == "init" and isinstance(data, dict):
                out["init_tools"] = data.get("tools", [])
                out["init_slash_commands"] = data.get("slash_commands", [])
                out["init_skills"] = data.get("skills", [])
            for block in getattr(message, "content", None) or []:
                name = type(block).__name__
                if name == "ToolUseBlock":
                    out["tool_uses"].append(block.name)
                elif kind == "UserMessage" and name == "TextBlock":
                    out.setdefault("replayed", []).append(block.text)
                elif kind == "AssistantMessage" and name == "TextBlock":
                    out.setdefault("assistant_text", []).append(block.text)
            if kind == "UserMessage" and isinstance(getattr(message, "content", None), str):
                out.setdefault("replayed", []).append(message.content)
            out["messages"].append(kind)
        started = time.monotonic()
        try:
            async with asyncio.timeout(LISTEN_S):
                async for message in client.receive_messages():
                    out["late"].append(type(message).__name__)
        except TimeoutError:
            pass
        out["listened_s"] = round(time.monotonic() - started, 1)
    out["scheduled_tasks_file"] = (root / ".claude" / "scheduled_tasks.json").exists()
    return out


def verdict(ctrl: dict, run: dict) -> dict:
    listed = set(run.get("commands", [])) | set(_names(run.get("init_slash_commands")))
    return {
        "control_discriminates": "loop" in ctrl["commands"],
        "a_no_loop_command_or_tool": "loop" not in listed and not (
            LOOP_TOOLS & set(_names(run.get("init_tools")))) and "loop" not in _names(run.get("init_skills")),
        "b_replayed_verbatim": PROMPT in run.get("replayed", []),
        "c_no_loop_tool_use": not (LOOP_TOOLS & set(run["tool_uses"])),
        "d_nothing_after_result": run["late"] == [],
        "e_no_scheduled_tasks_file": not run["scheduled_tasks_file"],
    }


async def main(artifact: Path) -> int:
    from litetui.claude_backend import ClaudeBackend
    from litetui.claude_tools import ClaudeTools
    from litetui.settings import Settings
    with tempfile.TemporaryDirectory(prefix="litetui-claude-loop-") as directory:
        root = Path(directory)
        cfg = Settings(backend="claude")
        backend = ClaudeBackend(cfg)
        backend.segment_id = "loop-probe"
        app = SimpleNamespace(backend=backend, convo_id="loop-probe", tools_enabled=False,
                              _stop_requested=False, settings=cfg, _active_tool_profile="interactive",
                              _pending_tool_images=[], _all_tools=lambda: [])
        bridge = ClaudeTools(app, backend, "loop-probe", workspace=root)
        ctrl = await control(backend, root)
        run = await main_run(backend, bridge, root)
        result = {"prompt": PROMPT, "control": ctrl, "run": run, "verdict": verdict(ctrl, run)}
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(result, indent=2), encoding="utf-8")
    ok = all(result["verdict"].values())
    print(("PASS " if ok else "FAIL ") + json.dumps(result["verdict"]) + f" -> {artifact}")
    return 0 if ok else 1


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1")
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("artifacts/claude-loop-probe.json")
    raise SystemExit(asyncio.run(asyncio.wait_for(main(target), 240)))
