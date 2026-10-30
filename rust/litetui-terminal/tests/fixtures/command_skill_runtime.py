"""Owned command/skill RPC process with real source registry and attended turns.

Only external provider/hardware boundaries reuse the committed switch harness.
Assertions watch registered callbacks; preparation is never execution.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import socket
import sys
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

source = Path(os.environ["RUST_COMMAND_SKILL_SOURCE"])
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ["RUST_BACKEND_SWITCH_SOURCE"] = str(source)
os.environ["RUST_BACKEND_SWITCH_CASE"] = "success"
spec = importlib.util.spec_from_file_location(
    "_command_external_boundaries", Path(__file__).with_name("backend_switch_runtime.py"))
assert spec and spec.loader
boundary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
case = os.environ.get("RUST_COMMAND_SKILL_CASE", "success")
driver = os.environ.get("RUST_COMMAND_SKILL_DRIVER", "self")
arguments = os.environ.get("RUST_COMMAND_SKILL_ARGS", "Use Δ example.\r\nKeep argument line two\tand trailing spaces  ")
requests: list[dict] = []
callbacks: list[str] = []
files: dict[str, Path] = {}
original_bytes: dict[str, bytes] = {}


def create_skills():
    entries = [("exact", "ExactSkill", "EXACT SELECTED BODY"),
               ("a-case", "caseskill", "WRONG CASE BODY"),
               ("b-case", "CaseSkill", "EXACT CASE BODY"),
               ("FolderTarget", "DifferentName", "WRONG FOLDER BODY"),
               ("selected-folder", "FolderTarget", "EXACT FOLDER BODY"),
               ("shadowed", "settings", "EXACT SHADOWED BODY")]
    for folder, name, marker in entries:
        path = root / "skills" / folder / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = (f"---\r\nname: {name}\r\ndescription: Controlled skill selection\r\n---\r\n"
                f"# {marker}\r\n\r\n```python\r\nprint('Δ Ω')  \r\n```\r\n"
                "Resource: ${CLAUDE_SKILL_DIR}/resource.txt\r\nTrailing body whitespace  \r\n")
        path.write_bytes(text.encode("utf-8"))
        files[name] = path
        original_bytes[name] = text.encode("utf-8")


async def inference(transport, *, purpose="turn", **request):
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled skill summary"))])
    assert purpose == "turn" and boundary.app_instance is not None
    app = boundary.app_instance
    assert transport.backend.name == app._agent_session.authority.backend
    assert request["model"] == app._agent_session.authority.model
    requests.append({"backend": transport.backend.name, "model": request["model"],
                     "messages": copy.deepcopy(request["messages"]),
                     "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled})
    stream = boundary.Stream()
    boundary.streams.append(stream)
    return stream


def disk(app):
    paths = [root / "settings.json", app._agent_session.memory_root / "settings.json",
             app.convo_dir / "settings.json", app.convo_path]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for path in paths}


def state(app):
    return {**boundary.identity(app), "history": copy.deepcopy(app.conversation),
            "profile": app._active_tool_profile, "tools_enabled": app.tools_enabled}


async def serve(app):
    from litetui import gui_rpc, hook_host, plugins, rpc, skills, store_identity

    boundary.__dict__["ready"] = asyncio.Event()
    report = {}
    initial_disk = None
    stale_used = False
    prepared = False
    command_log: list[dict] = []
    prepare_attempts = 0
    actual_fdopen = os.fdopen
    drifted = False
    selected_name = {"case-ambiguity": "CaseSkill", "folder-ambiguity": "FolderTarget",
                     "shadowed-skill": "settings"}.get(case, "ExactSkill")
    selected_path = files[selected_name]

    class DriftingRead:
        def __init__(self, stream):
            self.stream = stream
        def __getattr__(self, key):
            return getattr(self.stream, key)
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            nonlocal drifted
            result = self.stream.__exit__(*args)
            if not drifted:
                drifted = True
                selected_path.write_bytes(selected_path.read_bytes() + b"\nChanged during read\n")
            return result

    def fdopen(descriptor, *args, **kwargs):
        stream = actual_fdopen(descriptor, *args, **kwargs)
        if case == "file-drift" and not drifted and os.fstat(descriptor).st_ino == selected_path.stat().st_ino:
            return DriftingRead(stream)
        return stream

    async def management(command):
        nonlocal stale_used, prepared, prepare_attempts
        operation = command["type"]
        if operation in {"gui.commands.resolve", "gui.skills.native_prepare"}:
            if case in {"stale-generation", "one-shot-stale"} and (case == "stale-generation" or not stale_used):
                app.plugins.add_command("controlled-late", ("/late-" + str(len(command_log)),), boundary.forbidden)
                stale_used = True
            if case == "stale-policy" and not stale_used:
                app.tools_enabled = not app.tools_enabled
                stale_used = True
        if operation == "gui.skills.native_prepare":
            prepare_attempts += 1
            if case == "invalid-utf8":
                selected_path.write_bytes(b"invalid\xffUTF8")
            elif case == "oversized":
                selected_path.write_bytes(b"x" * 65537)
            elif case == "binding-expansion":
                selected_path.write_bytes(b"${CLAUDE_SKILL_DIR}" * 2000)
            elif case == "encoded-budget":
                selected_path.write_bytes(b"\x00" * 60000)
            elif case == "display-budget":
                selected_path.write_text("\u202e" * 7000, encoding="utf-8")
            elif case == "file-updated":
                selected_path.write_bytes(original_bytes[selected_name] + b"\nFresh file revision\n")
            with patch.object(os, "fdopen", fdopen):
                result = await gui_rpc.async_dispatch(app, command)
            prepared = True
            if case == "cancel-verify" and prepare_attempts == 2:
                await asyncio.sleep(0.3)
            if case == "foreign-prepared":
                result["agent_id"] = "00000000-0000-4000-8000-000000000000"
            elif case == "digest-drift":
                result["prompt_sha256"] = "0" * 64
            elif case == "prepared-snapshot-drift":
                result["snapshot"] = "0" * 64
        else:
            result = await gui_rpc.async_dispatch(app, command)
            if case == "cancel-route" and operation == "gui.commands.resolve":
                await asyncio.sleep(0.3)
        if prepared and case == "policy-drift" and operation == "gui.state":
            result["tool_policy"]["tools_enabled"] = not app.tools_enabled
        return result

    async def dispatch(command):
        command_log.append(copy.deepcopy(command))
        with (root / "command-skill-commands.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(command) + "\n")
        if command["type"].startswith("gui."):
            return await management(command)
        rpc._dispatch(app, command)
        await hook_host.drain_lifecycle(app)
        await asyncio.wait_for(boundary.settle_workers(app), 8)
        return None

    with ExitStack() as stack:
        for obj, name in ((socket, "create_connection"), (socket, "getaddrinfo"),
                          (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, name, boundary.forbidden))
        previous_profile = sys.getprofile()
        try:
            async with asyncio.timeout(45), app.run_test(size=(100, 32)):
                await asyncio.wait_for(boundary.ready.wait(), 12)
                await hook_host.drain_lifecycle(app)
                await asyncio.wait_for(boundary.settle_workers(app), 5)
                ready = next(row for row in boundary.records if row.get("type") == "ready")
                assert ready["launch_status"] == "ready", ready
                manifests = plugins.register_plugins(app, app.plugins, order=(
                    "litetui.plugins.misc", "litetui.plugins.convo", "litetui.plugins.model_switch",
                    "litetui.plugins.settings_ui", "litetui.plugins.skills_plugin"))
                assert {manifest.id for manifest in manifests} == {"misc", "convo", "model-switch", "settings-ui", "skills"}
                assert {skill.name for skill in app.skills} >= set(files), "Actual startup must discover fixture skills"
                watched = {getattr(entry.handler, "__code__", None) for entry in app.plugins.commands.values()}
                watched |= {getattr(entry.run, "__code__", None) for entry in app.plugins.palette_rows}
                watched.discard(None)
                def observe_callback(frame, event, _arg):
                    if event == "call" and frame.f_code in watched:
                        callbacks.append(frame.f_code.co_name)
                        if case != "padded-slash":
                            raise AssertionError("Command preparation executed a registered callback")
                sys.setprofile(observe_callback)
                app.conversation = [{"role": "system", "content": "Command fixture system"},
                                    {"role": "user", "content": "Preserved command history"},
                                    {"role": "assistant", "content": "Retained answer"}]
                app._prompt_store_identity = store_identity.capture(app)
                app._store_injected = True
                app._materialise_convo()
                app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity, store_injected=True)
                await hook_host.drain_lifecycle(app)
                await asyncio.wait_for(boundary.settle_workers(app), 5)
                initial_disk = disk(app)
                before = state(app)
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "command-hello", "protocol_version": 1})
                if driver == "rpc":
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        command = json.loads(line)
                        if command["type"].startswith("gui."):
                            try:
                                result = await dispatch(command)
                                rpc._respond(command.get("id"), ok=True, result=result)
                            except Exception as exc:  # noqa: BLE001 - actual RPC error boundary
                                rpc._respond(command.get("id"), ok=False, error=str(exc))
                        else:
                            await dispatch(command)
                            if command["type"] == "shutdown":
                                break
                    report = {"before": before}
                else:
                    catalog = await dispatch({"type": "gui.commands.list", "id": "command-list", "session_id": app.convo_id})
                    base = {"version": 1, "session_id": app.convo_id, "agent_id": before["agent_id"], "snapshot": catalog["snapshot"]}
                    report = {"before": before, "catalog": catalog, "catalog_unchanged": disk(app) == initial_disk and state(app) == before}
                    if case in {"routes", "unknown", "unsupported", "control-args", "mixed", "raw-ambiguous", "stale-generation", "stale-policy", "foreign-agent", "foreign-session"}:
                        commands = ["/settings", "/config", "/set", "/exit", "/thinking", "/conversations", "/skill"] if case == "routes" else [{"unknown": "/not-a-command", "unsupported": "/hooks", "control-args": "/settings extra", "raw-ambiguous": "/caseskill"}.get(case, "/settings")]
                        report["routes"] = []
                        for index, text in enumerate(commands):
                            command = {**base, "type": "gui.commands.resolve", "id": "resolve-" + str(index), "command": text}
                            if case == "mixed":
                                command["row_id"] = catalog["entries"][0]["id"]
                            if case == "foreign-agent":
                                command["agent_id"] = "00000000-0000-4000-8000-000000000000"
                            if case == "foreign-session":
                                command["session_id"] = "00000000-0000-4000-8000-000000000000"
                            try:
                                report["routes"].append(await dispatch(command))
                            except Exception as exc:  # noqa: BLE001 - refusal is the observed result
                                report["error"] = str(exc)
                    elif case in {"whitespace-only", "padded-slash"}:
                        await dispatch({"type": "prompt", "id": "command-explicit-0", "message": " \r\n\t " if case == "whitespace-only" else " \t/plugins \r\n ", "tool_profile": "strict"})
                    else:
                        row = next(row for row in catalog["entries"] if row["kind"] == "skill" and row["title"] == selected_name)
                        requested_args = "x" * 65537 if case == "args-oversized" else arguments
                        if case in {"raw-skill", "shadowed-skill"}:
                            raw = ("/skills settings " if case == "shadowed-skill" else "/ExactSkill ") + arguments
                            route = await dispatch({**base, "type": "gui.commands.resolve", "id": "skill-resolve", "command": raw})
                            report["route"] = route
                            assert route["row_id"] == row["id"]
                            requested_args = route["arguments"]
                        command = {**base, "backend": before["backend"], "model": before["model"],
                                   "type": "gui.skills.native_prepare", "id": "prepare-0", "row_id": row["id"], "arguments": requested_args}
                        try:
                            result = await dispatch(command)
                        except Exception as exc:  # noqa: BLE001 - refusal is the observed result
                            report["error"] = str(exc)
                        else:
                            report["prepared"] = result
                            report["prepare_unchanged"] = disk(app) == initial_disk and state(app) == before and not requests
                            text = selected_path.read_bytes().decode("utf-8")
                            body = "# Skill: " + selected_name + "\n\n" + skills.bind_skill_dir(text, selected_path.parent)
                            report["expected_prompt"] = body + ("\n\n---\n\n" + requested_args if requested_args else "")
                            if case not in {"prepare-only", "cancel", "display-budget", "encoded-budget"}:
                                await dispatch({"type": "prompt", "id": "command-explicit-0", "message": result["prompt"], "tool_profile": "strict"})
                report.update(after=state(app), final_disk=disk(app), initial_disk=initial_disk)
        finally:
            sys.setprofile(previous_profile)
            await app.client.close()
    from litetui import app as app_module
    assert Path(app_module.__file__).resolve() == source.resolve() / "src/litetui/app.py"
    report.update(requests=requests, calls=boundary.calls, denied=boundary.denied, callbacks=callbacks,
                  command_log=command_log, prepare_attempts=prepare_attempts, source_app=app_module.__file__,
                  events=[row for row in boundary.records if row.get("type") in {"gui.event", "submit_refused", "turn_refused", "turn_start", "turn_end"}])
    (root / "command-skill-report.json").write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    create_skills()
    with patch.object(boundary, "serve", serve), patch.object(boundary, "inference", inference):
        boundary.main()
