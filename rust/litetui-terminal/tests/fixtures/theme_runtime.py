"""Actual owned theme RPC with real registration, color resolution and persistence.

Only external provider/OS boundaries are synthetic. Theme/save faults wrap the
actual I/O/reactive boundary; snapshots, reviews, authority and readback are source.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import importlib.util
import json
import os
import platform
import socket
import subprocess
import sys
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any
from unittest.mock import patch

source = Path(os.environ["RUST_THEME_SOURCE"]).resolve()
root = Path(os.environ["LITETUI_DATA_ROOT"])
os.environ["RUST_BACKEND_SWITCH_SOURCE"] = str(source)
os.environ["RUST_BACKEND_SWITCH_CASE"] = "success"
spec = importlib.util.spec_from_file_location(
    "_theme_external_boundaries", Path(__file__).with_name("backend_switch_runtime.py"))
assert spec and spec.loader
boundary: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(boundary)
case = os.environ.get("RUST_THEME_CASE", "select")
driver = os.environ.get("RUST_THEME_DRIVER", "self")
phase = os.environ.get("RUST_THEME_PHASE", "fresh")
name = os.environ.get("RUST_THEME_NAME", "  Theme 雪  ")
SNAPSHOT = "gui.themes.native_snapshot"
VALIDATE = "gui.themes.native_validate"
SAVE = "gui.themes.native_save"
CORE = {
    "primary": "#123456", "secondary": "#456789", "accent": "#789abc",
    "success": "#92a394", "warning": "#c6b083", "error": "#a65f62",
    "background": "#0a0b0c", "surface": "#161718", "panel": "#242526",
    "foreground": "#d6d7d8",
}
TOKENS = {**CORE, "thinking-text": "#b1c2d3", "thinking-box": "#456789",
          "tool-text": "#d4b291", "footer-foreground": "#91a2b3", "footer-background": "#101213"}
requests: list[dict] = []
events: list[dict] = []
writes: list[dict] = []
commands: list[dict] = []
results: list[dict] = []


def prepare_settings():
    """Fixture inputs before actual CLI construction, never a theme runtime mock."""
    if phase != "fresh":
        return
    path = root / "settings.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved.setdefault("theme_name", "textual-dark")
    custom = saved.setdefault("custom_themes", {})
    if case in {"startup-custom", "overwrite", "refresh-failed"}:
        custom["Theme 雪"] = {**TOKENS, "primary": "#654321", "tool-text": "#654321"}
        saved["theme_name"] = "Theme 雪"
    if case in {"alias-lose", "alias-win"}:
        aliases = [("Theme 雪", {**TOKENS, "primary": "#654321"}),
                   (" Theme 雪 ", {**TOKENS, "primary": "#abcdef"})]
        if case == "alias-win":
            aliases.reverse()
        custom.update(aliases)
        saved["theme_name"] = "Theme 雪"
    if case.startswith("shadow-"):
        target = case.removeprefix("shadow-")
        custom[target] = dict(TOKENS)
        saved["theme_name"] = target
    if case in {"empty-selection", "startup-light"}:
        saved["theme_name"] = "" if case == "empty-selection" else "textual-light"
    if case == "startup-unknown":
        saved["theme_name"] = "missing-theme"
    if case == "startup-invalid":
        custom["broken-theme"] = {"primary": "not-a-color"}
        saved["theme_name"] = "broken-theme"
    if case == "startup-case":
        custom["Case Theme"] = dict(TOKENS)
        custom["case theme"] = {**TOKENS, "primary": "#abcdef"}
        saved["theme_name"] = "case theme"
    if case == "unselectable-name":
        custom["comma,name"] = dict(TOKENS)
    path.write_text(json.dumps(saved, ensure_ascii=False), encoding="utf-8")


async def inference(transport, *, purpose="turn", **request):
    if purpose == "card-summary":
        return NS(choices=[NS(message=NS(content="Controlled theme summary"))])
    app = boundary.app_instance
    assert purpose == "turn" and app is not None
    assert transport.backend is app.backend
    authority = app._agent_session.authority
    assert transport.backend.name == authority.backend and request["model"] == authority.model
    requests.append({"session_id": app.convo_id, "agent_id": authority.agent_id,
                     "backend": app.backend.name, "model": request["model"],
                     "tools_enabled": app.tools_enabled, "profile": app._active_tool_profile,
                     "messages": copy.deepcopy(request["messages"]),
                     "effective_theme": app.current_theme.name})
    stream = boundary.Stream()
    boundary.streams.append(stream)
    return stream


def disk(app):
    paths = [root / "settings.json", app._agent_session.memory_root / "settings.json",
             app.convo_dir / "settings.json", app.convo_path]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
            for path in paths}


def actual_palette(app):
    """Independent observation through the mounted app's actual CSS variables."""
    from textual.color import Color
    variables = app.get_css_variables()
    mapping = {
        "background": "background", "surface": "surface", "primary": "primary",
        "panel": "panel", "tool_background": "surface-darken-2", "foreground": "foreground",
        "title": "success", "assistant_border": "success-darken-2",
        "thinking_text": "thinking-text", "thinking_border": "thinking-box",
        "tool_text": "tool-text", "error": "error",
        "footer_foreground": "footer-foreground", "footer_background": "footer-background",
    }
    colors = {key: Color.parse(variables.get(variable) or variables[
        "foreground" if key == "footer_foreground" else "background"])
              for key, variable in mapping.items()}
    if any(color.a != 1 or color.ansi is not None or color.auto for color in colors.values()):
        return None  # actual source has no exact fourteen-opaque-RGB representation
    return {key: list(color.rgb) for key, color in colors.items()}


def identity(app):
    return {**boundary.identity(app), "tools_enabled": app.tools_enabled,
            "profile": app._active_tool_profile, "history": copy.deepcopy(app.conversation),
            "pending": app.store.pending, "queue": copy.deepcopy(app._pending_input)}


def theme_state(app):
    return {"requested_name": app.settings.theme_name, "runtime_name": app.theme,
            "effective_name": app.current_theme.name, "palette": actual_palette(app),
            "registered": {key: asdict(value) for key, value in app.available_themes.items()},
            "custom_themes": copy.deepcopy(app.settings.custom_themes)}


async def settle(app):
    from textual.worker import WorkerCancelled

    from litetui import hook_host
    await hook_host.drain_lifecycle(app)
    async def retire():
        for _ in range(6):
            workers = list(app.workers)
            outcomes = await asyncio.gather(*(worker.wait() for worker in workers), return_exceptions=True)
            for worker, outcome in zip(workers, outcomes, strict=True):
                if isinstance(outcome, WorkerCancelled) and worker.name != "_stream":
                    continue
                if isinstance(outcome, BaseException):
                    raise outcome
            await asyncio.sleep(0)
            if not list(app.workers):
                return
        raise AssertionError("Controlled owned workers did not settle")
    await asyncio.wait_for(retire(), 8)


def custom_tokens():
    tokens = json.loads(os.environ.get("RUST_THEME_TOKENS", json.dumps(TOKENS)))
    if case == "optional-missing":
        tokens = dict(CORE)
    elif case == "optional-invalid":
        tokens = {**CORE, "thinking-text": "invalid", "thinking-box": "",
                  "tool-text": "invalid", "footer-foreground": "invalid", "footer-background": ""}
    elif case == "invalid-core":
        tokens["primary"] = "red"
    elif case == "unknown-token":
        tokens["not-a-theme-token"] = "#123456"
    return tokens


async def serve(app):
    from textual.app import App
    from textual.theme import Theme

    from litetui import gui_rpc, rpc, settings, settings_service, store_identity

    boundary.ready = asyncio.Event()
    report: dict[str, Any] = {}
    in_save = False
    saved = False
    stale_used = False
    target_name = name.strip()
    actual_write = settings_service._write
    actual_legacy_write = settings._save_locked
    actual_register = type(app).register_theme
    actual_validate = type(app)._validate_theme
    actual_mutate = type(app).mutate_reactive
    actual_footer = app._refresh_ctx_label

    def write(path, raw):
        writes.append({"path": str(path), "in_save": in_save, "kind": "service",
                       "fields": sorted(raw), "theme_name": raw.get("theme_name")})
        if in_save and Path(path) == root / "settings.json":
            if case == "write-before":
                raise OSError("Controlled theme write refused before replacement")
            if case in {"write-after", "write-corrupt", "partial-palette-drift"}:
                payload = {**raw, "theme_name": "uncertain-theme"} if case == "write-corrupt" else raw
                actual_write(path, payload)
                raise OSError("Controlled theme write acknowledgement lost")
        return actual_write(path, raw)

    def legacy_write(st, settings_root=None):
        writes.append({"path": str(settings.settings_path(settings_root)), "in_save": in_save,
                       "kind": "legacy", "theme_name": st.theme_name})
        return actual_legacy_write(st, settings_root)

    def register(host, theme):
        events.append({"effect": "register", "name": theme.name, "in_save": in_save})
        if in_save and case == "register-failed" and theme.name == target_name:
            raise RuntimeError("Controlled theme registration failure")
        return actual_register(host, theme)

    def validate(host, theme_name):
        if in_save and case == "select-failed" and theme_name == target_name:
            raise RuntimeError("Controlled theme selection failure")
        return actual_validate(host, theme_name)

    def mutate(host, reactive):
        events.append({"effect": "refresh", "theme": host.theme, "in_save": in_save})
        if in_save and case == "refresh-failed" and reactive is App.theme:
            raise RuntimeError("Controlled theme refresh failure")
        return actual_mutate(host, reactive)

    def footer(*args, **kwargs):
        events.append({"effect": "footer", "theme": app.theme, "in_save": in_save})
        return actual_footer(*args, **kwargs)

    def mutate_global():
        path = root / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["fixture_foreign_device_edit"] = int(data.get("fixture_foreign_device_edit", 0)) + 1
        path.write_text(json.dumps(data), encoding="utf-8")

    async def management(command):
        nonlocal saved, stale_used, in_save, target_name
        operation = command["type"]
        commands.append(copy.deepcopy(command))
        if operation == SNAPSHOT and case == "unsupported-catalog":
            raise ValueError("Controlled unsupported source theme operation")
        if operation == SAVE:
            if case in {"stale", "one-shot-stale"} and (case == "stale" or not stale_used):
                mutate_global()
                stale_used = True
            if case == "registry-drift":
                app.available_themes["matrix"].variables["thinking-text"] = "#010203"
        in_save = operation == SAVE
        try:
            result = await gui_rpc.async_dispatch(app, command)
        finally:
            in_save = False
        if case == "control-routes" and driver == "rpc" and operation in {
            "gui.commands.resolve", "gui.conversations.native_snapshot"
        }:
            await asyncio.sleep(0.3)
        if operation == VALIDATE:
            target_name = result["target"]["name"]
            if case == "cancel-validate" and driver == "rpc":
                await asyncio.sleep(0.3)
        if operation == SAVE:
            saved = result.get("status") in {"saved", "partial"}
            if case == "generic-save-error":
                raise RuntimeError("Controlled response loss after actual theme save")
        if saved and operation == SNAPSHOT:
            if case == "definition-drift":
                result["selection"]["effective_definition"] = "0" * 64
            if case in {"palette-drift", "partial-palette-drift"}:
                row = next(row for row in result["rows"]
                           if row["row_id"] == result["selection"]["effective_row_id"])
                row["palette"]["primary"] = [(int(row["palette"]["primary"][0]) + 1) % 256, 2, 3]
        if saved and operation == "gui.state" and case == "policy-drift":
            result["tool_policy"]["tools_enabled"] = not result["tool_policy"]["tools_enabled"]
        results.append({"type": operation, "result": copy.deepcopy(result)})
        return result

    def common():
        authority = app._agent_session.authority
        return {"version": 1, "session_id": app.convo_id, "agent_id": authority.agent_id,
                "backend": app.backend.name, "model": app.model_id}

    async def prompt(text):
        command = {"type": "prompt", "id": "theme-explicit-" + phase + "-" + str(len(requests)),
                   "message": text, "tool_profile": "strict"}
        commands.append(command)
        rpc._dispatch(app, command)
        await settle(app)

    with ExitStack() as stack:
        for obj, key in ((socket, "create_connection"), (socket, "getaddrinfo"),
                         (socket.socket, "connect"), (socket.socket, "connect_ex")):
            stack.enter_context(patch.object(obj, key, boundary.forbidden))
        try:
            async with asyncio.timeout(55), app.run_test(size=(100, 32)) as pilot:
                await asyncio.wait_for(boundary.ready.wait(), 15)
                await settle(app)
                assert next(row for row in boundary.records if row.get("type") == "ready")["launch_status"] == "ready"
                assert app.plugins.status.get("themes") == "active"
                if phase == "fresh" and case != "empty":
                    app.conversation = [{"role": "system", "content": "Theme fixture system"},
                                        {"role": "user", "content": "Preserved theme history"},
                                        {"role": "assistant", "content": "Retained theme answer"}]
                    app._prompt_store_identity = store_identity.capture(app)
                    app._store_injected = True
                    app._materialise_convo()
                    app.store.record_snapshot(app.conversation, store_identity=app._prompt_store_identity,
                                              store_injected=True)
                    await settle(app)
                if case == "disk-only":
                    path = root / "settings.json"
                    document = json.loads(path.read_text(encoding="utf-8"))
                    document.setdefault("custom_themes", {})["other-instance"] = {**CORE, "primary": "#abcdef"}
                    document["custom_themes"]["invalid-preserved"] = {"primary": "broken"}
                    path.write_text(json.dumps(document), encoding="utf-8")
                    assert "other-instance" not in app.settings.custom_themes
                if case == "startup-registered":
                    app.register_theme(Theme(name="registered-color", primary="#123456",
                                             variables={"tool-text": "red", "thinking-text": "blue"}))
                    app.theme = "registered-color"
                    await pilot.pause()
                if case in {"startup-alpha", "startup-ansi", "active-alpha", "repair-alpha"}:
                    raw_color = "ansi_default" if case == "startup-ansi" else "#ff000080"
                    app.register_theme(Theme(name="unsupported-color", primary="#123456",
                                             variables={"thinking-text": raw_color}))
                    if case in {"active-alpha", "repair-alpha"}:
                        app.theme = "unsupported-color"
                        await pilot.pause()
                if case == "oversized-catalog":
                    for index in range(257):
                        app.register_theme(Theme(name=f"controlled-extra-{index}", primary="#123456"))
                await gui_rpc.async_dispatch(app, {"type": "gui.hello", "id": "theme-hello", "protocol_version": 1})
                before, theme_before, initial_disk = identity(app), theme_state(app), disk(app)
                css_before = app._css_update_count
                stack.enter_context(patch.object(settings_service, "_write", write))
                stack.enter_context(patch.object(settings, "_save_locked", legacy_write))
                stack.enter_context(patch.object(type(app), "register_theme", register))
                stack.enter_context(patch.object(type(app), "_validate_theme", validate))
                stack.enter_context(patch.object(type(app), "mutate_reactive", mutate))
                stack.enter_context(patch.object(app, "_refresh_ctx_label", footer))
                if driver == "rpc":
                    while line := await asyncio.to_thread(sys.stdin.readline):
                        command = json.loads(line)
                        if command["type"].startswith("gui."):
                            try:
                                result = await management(command)
                                rpc.publish_management_result(app, command, result)
                            except Exception as exc:  # noqa: BLE001 - real RPC response boundary
                                rpc._respond(command.get("id"), ok=False, error=str(exc))
                        else:
                            commands.append(copy.deepcopy(command))
                            rpc._dispatch(app, command)
                            if command["type"] == "shutdown":
                                break
                            await settle(app)
                else:
                    try:
                        snapshot = await management({"type": SNAPSHOT, "id": "theme-snapshot", **common()})
                    except ValueError as exc:
                        report["snapshot_error"] = str(exc)
                    else:
                        report["snapshot"] = snapshot
                        report["snapshot_unchanged"] = disk(app) == initial_disk and theme_state(app) == theme_before
                        startup = case.startswith(("startup-", "shadow-")) or case in {"unselectable-name"}
                        if phase == "resume":
                            await prompt("Explicit prompt after theme restart")
                        elif not startup:
                            selection = case in {"select", "select-current", "active-alpha", "empty-selection", "validate-only", "cancel-validate", "stale",
                                                 "one-shot-stale", "registry-drift", "foreign", "replay"}
                            command = {"type": VALIDATE, "id": "theme-validate", **common(),
                                       "snapshot": snapshot["snapshot"], "revisions": snapshot["revisions"]}
                            if selection:
                                row = next(row for row in snapshot["rows"] if row["name"] == ("textual-dark" if case == "select-current" else "matrix"))
                                command.update(action="select", row_id=row["row_id"])
                            else:
                                command.update(action="custom", name=("comma,name" if case == "invalid-name" else
                                                                    "unsupported-color" if case == "repair-alpha" else name),
                                               tokens=custom_tokens())
                            if case == "foreign":
                                command["agent_id"] = "c87c8777-a42a-4b03-907a-c9e0cfda2b5d"
                            try:
                                review = await management(command)
                            except (ValueError, TypeError) as exc:
                                report["validation_error"] = str(exc)
                            else:
                                report["review"] = review
                                report["validate_unchanged"] = disk(app) == initial_disk and theme_state(app) == theme_before
                                if case not in {"validate-only", "cancel-validate"}:
                                    apply = {"type": SAVE, "id": "theme-save", **common(),
                                             "snapshot": review["snapshot"], "revisions": review["revisions"],
                                             "review": review["review"], "action_id": review["action_id"]}
                                    report["result"] = await management(apply)
                                    await pilot.pause()
                                    report["after_save"] = theme_state(app)
                                    report["after_save_identity"] = identity(app)
                                    report["after_save_disk"] = disk(app)
                                    report["css_after_save"] = app._css_update_count
                                    report["writes_at_save"] = copy.deepcopy(writes)
                                    report["events_at_save"] = copy.deepcopy(events)
                                    if report["result"]["status"] in {"saved", "partial"}:
                                        report["readback"] = await management(
                                            {"type": SNAPSHOT, "id": "theme-readback", **common()})
                                        report["state"] = await management(
                                            {"type": "gui.state", "id": "theme-state", "include_conversations": False})
                                    if case == "replay":
                                        try:
                                            report["replay"] = await management(apply)
                                        except ValueError as exc:
                                            report["replay_error"] = str(exc)
                                    if case == "later-watch-save" and report["result"]["status"] == "saved":
                                        path = root / "settings.json"
                                        document = json.loads(path.read_text(encoding="utf-8"))
                                        document["custom_themes"]["external-after-save"] = {**CORE, "primary": "#fedcba"}
                                        path.write_text(json.dumps(document), encoding="utf-8")
                                        events.append({"effect": "explicit-later-theme-selection", "name": "matrix"})
                                        app.theme = "matrix"
                                        await pilot.pause()
                                        report["later_saved_document"] = json.loads(path.read_text(encoding="utf-8"))
                                    if report["result"]["status"] == "saved":
                                        assert not requests
                                        await prompt("Explicit prompt after verified theme save")
                                    elif report["result"]["status"] == "partial" and not report["result"]["close_required"]:
                                        assert not requests
                                        await prompt("Explicit prompt after known theme-only partial result")
                await pilot.pause()
                report.update(before=before, theme_before=theme_before, initial_disk=initial_disk,
                              after=identity(app), theme_after=theme_state(app), final_disk=disk(app),
                              css_before=css_before, css_after=app._css_update_count,
                              saved_document=json.loads((root / "settings.json").read_text(encoding="utf-8")))
        finally:
            await app.client.close()
    from litetui import app as app_module
    assert Path(app_module.__file__).resolve() == source / "src/litetui/app.py"
    report.update(source_app=app_module.__file__, requests=requests, commands=commands, results=results,
                  calls=boundary.calls, denied=boundary.denied, writes=writes, events=events)
    (root / ("theme-" + phase + "-report.json")).write_text(json.dumps(report), encoding="utf-8")
    (root / "theme-commands.jsonl").write_text(
        "".join(json.dumps(command) + "\n" for command in commands), encoding="utf-8")


def main():
    """Reuse external seams without the old fixture's no-global-write assertion."""
    import httpx

    from litetui import (
        gpu_gate,
        harness,
        image_viewer,
        llm_backend,
        mcp_client,
        model_transport,
        plugins,
        rpc,
        thinking_probe,
        ttyguard,
        update_check,
    )
    from litetui.app import LiteTUI
    from litetui.custom_backend import CustomBackend

    prepare_settings()
    original_init, original_emit = LiteTUI.__init__, rpc.rpc_emit

    def constructed(app, *args, **kwargs):
        boundary.app_instance = app
        original_init(app, *args, **kwargs)

    def emit(row):
        boundary.records.append(row)
        original_emit(row)
        if row.get("type") == "ready" and boundary.ready is not None:
            boundary.ready.set()

    def registered(seat):
        seat.registered, seat.error = True, ""
        return True

    def run(app, **kwargs):
        assert kwargs == {"headless": True}
        asyncio.run(serve(app))

    with ExitStack() as stack:
        for obj, key, replacement in (
            (platform, "node", lambda: "controlled-host"), (subprocess, "Popen", boundary.forbidden),
            (httpx.Client, "send", boundary.forbidden), (httpx.AsyncClient, "send", boundary.forbidden_async),
            (ttyguard, "run", boundary.command), (ttyguard, "popen", boundary.forbidden),
            (gpu_gate, "nvidia_smi", lambda: "fixture-nvidia-smi"),
            (plugins, "PLUGIN_LOAD_ORDER", ("litetui.plugins.core_tools", "litetui.plugins.self_compact",
                                           "litetui.plugins.themes_plugin", "litetui.plugins.settings_ui",
                                           "litetui.plugins.convo")),
            (rpc, "start_rpc_reader", lambda _app: None), (rpc, "rpc_emit", emit),
            (harness.Seat, "register", registered), (update_check, "start_background_check", lambda _app: None),
            (image_viewer, "init_image_backend", lambda: None),
            (LiteTUI, "__init__", constructed), (LiteTUI, "run", run),
            (model_transport.OpenAITransport, "create", inference),
            (CustomBackend, "_get", boundary.custom_get), (llm_backend, "_http_json", boundary.forbidden),
            (thinking_probe, "get_effective_levels", lambda *_args, **_kwargs: None),
        ):
            stack.enter_context(patch.object(obj, key, replacement))
        for method in ("load", "connect", "reload_configs"):
            stack.enter_context(patch.object(mcp_client.MCPManager, method, boundary.forbidden))
        gpu_gate.is_rtx_5090.cache_clear()
        try:
            module_spec = importlib.util.spec_from_file_location("litetui._actual_theme_cli", source / "src/litetui/cli.py")
            assert module_spec and module_spec.loader
            cli = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(cli)
            cli.main()
        finally:
            gpu_gate.is_rtx_5090.cache_clear()
        assert not boundary.denied, boundary.denied
        assert all(stream.closed for stream in boundary.streams)


if __name__ == "__main__":
    main()
