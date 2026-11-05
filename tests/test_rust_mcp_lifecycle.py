"""Owned native MCP lifecycle, using private declarations and controlled transports."""

from __future__ import annotations

import asyncio
import copy
import json
import subprocess
import threading

import pytest
import pytest_asyncio
from test_rust_background_input import app as owned_app
from test_rust_background_input import command
from test_rust_background_input import switch_app as owned_switch_app
from test_rust_scheduled_publication import published_rpc

from litetui import background_input as work
from litetui import gui_rpc, native_mcp_shutdown, rpc
from litetui import native_mcp_lifecycle as mcp
from litetui import native_runtime_controls as controls

REAL_POPEN = subprocess.Popen
app, switch_app = owned_app, owned_switch_app
pytestmark = [pytest.mark.asyncio, pytest.mark.real_mcp_load]


class Server:
    def __init__(self, name, config):
        self.name, self.cfg = name, config
        self.tools = [
            {
                "name": "private",
                "description": "controlled",
                "inputSchema": {"type": "object"},
            }
        ]
        self.initialized = False
        self.stops = self.calls = 0
        self.fail_stop = self.fail_start = False
        self.stopped = False

    def start(self):
        self.initialized = True
        if self.fail_start:
            raise RuntimeError("SECRET start failure")

    def stop(self):
        self.stops += 1
        if self.fail_stop:
            self.fail_stop = False
            raise RuntimeError("SECRET stop failure")
        self.stopped = True

    def call(self, tool, args):
        self.calls += 1
        return f"private:{tool}:{args['value']}"


@pytest_asyncio.fixture
async def armed(app, monkeypatch, request, tmp_path):
    from litetui import paths

    monkeypatch.setattr(paths, "PLAN_PROMPT_FILE", tmp_path / "plan-mode.md")
    provider = getattr(request, "param", None)
    if provider == "codex":
        monkeypatch.setattr(app.backend, "name", "codex")
        from litetui.codex_app_server import AppServer

        monkeypatch.setattr(app.backend, "app_server", AppServer(), raising=False)
        app._agent_session.update_execution(
            backend="codex", model=app.model_id, thinking_level="default"
        )
    elif provider == "claude":
        from litetui.claude_backend import ClaudeBackend

        app.backend = ClaudeBackend(app.settings)
        app._agent_session.update_execution(
            backend="claude", model=app.model_id, thinking_level="default"
        )
    if provider:
        from litetui import seat_authority

        monkeypatch.setattr(seat_authority, "floor_refusal", lambda *_: None)
    app._launch_options.work_lifecycle_version = 1
    app._launch_options.mcp_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app.settings.mcp_enabled = False
    app._cli_tool_profile = app._active_tool_profile = "strict"
    app._invocation_saved_values["mcp_enabled"] = True
    app._launch_overrides["mcp_enabled"] = False
    config = {
        "mcpServers": {
            "private": {
                "command": "private-fixture",
                "args": ["SECRET-ARG"],
                "env": {"SECRET_ENV": "SECRET-VALUE"},
            },
            "web": {
                "url": "https://user:SECRET-PASS@example.invalid:443/SECRET-PATH?SECRET-QUERY",
                "headers": {"Authorization": "SECRET-HEADER"},
            },
        }
    }
    path = app.mcp.root / ".mcp.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    created = []

    def build(name, cfg):
        server = Server(name, cfg)
        created.append(server)
        return server

    monkeypatch.setattr(app.mcp, "_build", build)
    app._mcp_test_created, app._mcp_test_config = created, path
    capture = await published_rpc(
        app,
        monkeypatch,
        command(
            app,
            type="gui.work.native_capture",
            runtime_controls_version=1,
            mcp_lifecycle_version=1,
        ),
    )
    active = await published_rpc(
        app,
        monkeypatch,
        command(
            app,
            type="gui.work.native_activate",
            instance=capture["instance"],
            activation_token=capture["activation_token"],
            runtime_controls_version=1,
            mcp_lifecycle_version=1,
        ),
    )
    assert capture["mcp_lifecycle_version"] == active["mcp_lifecycle_version"] == 1
    yield app
    await native_mcp_shutdown.settle_before_shutdown(app)


def cmd(app, op, **fields):
    return {
        "version": 1,
        "id": "mcp-" + op,
        "type": mcp.PREFIX + op,
        **work.identity(app),
        "work_instance": work.bridge(app).instance,
        **fields,
    }


async def call(app, monkeypatch, op, **fields):
    return await published_rpc(app, monkeypatch, cmd(app, op, **fields))


async def review(app, monkeypatch, action, target=None):
    snap = await call(app, monkeypatch, "snapshot")
    return await call(
        app,
        monkeypatch,
        "review",
        revision=snap["revision"],
        action=action,
        target=target,
    )


async def change(app, monkeypatch, action, target=None):
    checked = await review(app, monkeypatch, action, target)
    applied = await call(app, monkeypatch, "apply", token=checked["token"])
    assert mcp.blocking(app)
    assert controls._state(app).generation == applied["policy_before"]["generation"]
    settled = await call(
        app,
        monkeypatch,
        "readback",
        token=checked["token"],
        apply_id=applied["apply_id"],
    )
    assert not mcp.blocking(app)
    assert controls._state(app).generation == settled["policy_after"]["generation"]
    return settled


async def test_reviewed_enable_no_transport_then_each_owned_connection_and_disable(
    armed, monkeypatch
):
    app = armed
    before_history = copy.deepcopy(app.conversation)
    before_global = (app.mcp.root / "settings.json").read_bytes()
    before_decl = app._mcp_test_config.read_bytes()
    initial = await call(app, monkeypatch, "snapshot")
    assert (
        not initial["enabled"]
        and initial["overridden"]
        and initial["dispatch"]["count"] == 0
    )
    assert "SECRET" not in json.dumps(initial) and "user:" not in json.dumps(initial)
    assert not app._mcp_test_created
    enabled = await change(app, monkeypatch, "enable")
    assert enabled["state"]["enabled"] and enabled["state"]["saved_enabled"]
    assert (
        not enabled["state"]["overridden"]
        and not app._mcp_test_created
        and not app.mcp.lazy
    )
    assert (await call(app, monkeypatch, "snapshot"))["revision"] == enabled["state"][
        "revision"
    ]
    connected = await change(app, monkeypatch, "connect", "private")
    assert (
        connected["state"]["dispatch"]["count"] == 1 and len(app._mcp_test_created) == 1
    )
    assert not connected["state"]["tools_enabled"]
    assert (
        app.mcp_dispatch["mcp__private__private"]({"value": "proof"})
        == "private:private:proof"
    )
    await change(app, monkeypatch, "reconnect", "private")
    assert len(app._mcp_test_created) == 2 and app._mcp_test_created[0].stopped
    await change(app, monkeypatch, "reload")
    assert len(app._mcp_test_created) == 2 and len(app.mcp.servers) == 1
    await change(app, monkeypatch, "disconnect", "private")
    await change(app, monkeypatch, "connect", "private")
    disabled = await change(app, monkeypatch, "disable")
    assert not disabled["state"]["enabled"] and not disabled["state"]["saved_enabled"]
    assert not app.mcp.servers and not app.mcp._closing and not app.mcp_dispatch
    await change(app, monkeypatch, "enable")
    assert len(app._mcp_test_created) == 3
    assert (
        app.conversation == before_history
        and app._mcp_test_config.read_bytes() == before_decl
    )
    assert (app.mcp.root / "settings.json").read_bytes() == before_global


async def test_failed_cleanup_is_redacted_quarantined_and_retryable(armed, monkeypatch):
    app = armed
    await change(app, monkeypatch, "enable")
    await change(app, monkeypatch, "connect", "private")
    server = app.mcp.servers["private"]
    old_dispatch = app.mcp_dispatch["mcp__private__private"]
    server.fail_stop = True
    failed = await change(app, monkeypatch, "disable")
    assert failed["status"] == "failed" and failed["state"]["enabled"]
    assert (
        failed["state"]["rows"][0]["state"] == "failed"
        and failed["state"]["dispatch"]["count"] == 0
    )
    assert "SECRET" not in json.dumps(failed) and app.mcp.servers["private"] is server
    assert (
        old_dispatch({"value": "forbidden"}).startswith("[error]") and server.calls == 0
    )
    assert not app.mcp.dispatch_for("mcp__private__private")
    retried = await change(app, monkeypatch, "disconnect", "private")
    assert retried["status"] == "completed" and server.stopped and server.stops == 2
    assert not app.mcp._stop_failed
    await change(app, monkeypatch, "disable")


async def test_exact_publication_holds_new_authorization_and_generation(
    armed, monkeypatch
):
    app = armed
    checked = await review(app, monkeypatch, "enable")
    applied = await call(app, monkeypatch, "apply", token=checked["token"])
    admitted = []

    async def authorize():
        async with controls.authorization(app) as ticket:
            admitted.append(ticket.generation)

    waiter = asyncio.create_task(authorize())
    await asyncio.sleep(0)
    assert not admitted and not waiter.done()
    read = cmd(app, "readback", token=checked["token"], apply_id=applied["apply_id"])
    result = await gui_rpc.async_dispatch(app, read)
    assert mcp.blocking(app) and not admitted
    rpc.publish_management_result(app, read, result)
    await asyncio.wait_for(waiter, 2)
    assert admitted == [1]
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, read)


@pytest.mark.parametrize("phase", ["apply", "readback"])
async def test_failed_exact_emission_retains_hold(armed, monkeypatch, phase):
    app = armed
    checked = await review(app, monkeypatch, "enable")
    apply = cmd(app, "apply", token=checked["token"])
    result = await gui_rpc.async_dispatch(app, apply)
    current = apply
    if phase == "readback":
        rpc.publish_management_result(app, apply, result)
        current = cmd(
            app, "readback", token=checked["token"], apply_id=result["apply_id"]
        )
        result = await gui_rpc.async_dispatch(app, current)

    def fail(_row):
        raise OSError("controlled response failure")

    monkeypatch.setattr(rpc, "rpc_emit", fail)
    with pytest.raises(OSError):
        rpc.publish_management_result(app, current, result)
    assert mcp.blocking(app) and controls._state(app).generation == 0


@pytest.mark.parametrize("drift", ["file", "settings", "dispatch", "manager"])
async def test_review_drift_refuses_before_transport_start(armed, monkeypatch, drift):
    app = armed
    await change(app, monkeypatch, "enable")
    checked = await review(app, monkeypatch, "connect", "private")
    if drift == "file":
        app._mcp_test_config.write_text('{"mcpServers":{}}', encoding="utf-8")
    elif drift == "settings":
        app.settings.max_tokens_tools += 1
    elif drift == "dispatch":
        app.mcp_dispatch["foreign"] = lambda _: "bad"
    else:
        monkeypatch.setattr(app, "mcp", copy.copy(app.mcp))
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, cmd(app, "apply", token=checked["token"]))
    assert not app._mcp_test_created
    if drift == "manager":
        app.mcp = app._owned_mcp_manager


async def test_old_policy_and_legacy_mutations_are_rejected_after_enable(
    armed, monkeypatch
):
    app = armed
    await change(app, monkeypatch, "enable")
    old = {
        "type": "prompt",
        "id": "old",
        "message": "keep draft",
        "runtime_policy": {
            "version": 1,
            "work_instance": work.bridge(app).instance,
            "generation": 0,
        },
    }
    with pytest.raises(ValueError, match="generation"):
        controls.operation_allowed(app, old)
    for request in (
        {"type": "gui.mcp.reload"},
        {"type": "gui.settings.apply", "settings": {"mcp_enabled": False}},
    ):
        with pytest.raises(ValueError, match="reviewed"):
            controls.operation_allowed(app, request)
    controls.operation_allowed(
        app, {**old, "runtime_policy": {**old["runtime_policy"], "generation": 1}}
    )


async def test_cancelled_connection_joins_thread_and_shutdown_reaps_exact_handle(
    armed, monkeypatch
):
    app = armed
    await change(app, monkeypatch, "enable")
    checked = await review(app, monkeypatch, "connect", "private")
    entered, release = threading.Event(), threading.Event()
    original = Server.start

    def start(server):
        entered.set()
        assert release.wait(5)
        original(server)

    monkeypatch.setattr(Server, "start", start)
    task = asyncio.create_task(
        gui_rpc.async_dispatch(app, cmd(app, "apply", token=checked["token"]))
    )
    assert await asyncio.to_thread(entered.wait, 3)
    closing = asyncio.create_task(mcp.settle_before_shutdown(app))
    await asyncio.sleep(0)
    assert not task.done() and not closing.done()
    release.set()
    await asyncio.wait_for(closing, 3)
    assert task.cancelled() and app.mcp.servers and mcp.blocking(app)
    await native_mcp_shutdown.settle_before_shutdown(app)
    assert app._mcp_test_created[0].stopped and not app.mcp.servers


async def test_capability_is_explicit_and_old_runtime_controls_keep_mcp_off(
    app, monkeypatch
):
    app._launch_options.work_lifecycle_version = 1
    app._launch_options.mcp_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app._cli_tool_profile = app._active_tool_profile = "strict"
    for fields in (
        {"mcp_lifecycle_version": 1},
        {"runtime_controls_version": 1, "mcp_lifecycle_version": True},
        {"runtime_controls_version": 1, "mcp_lifecycle_version": 2},
    ):
        with pytest.raises(ValueError, match="MCP lifecycle"):
            work.capture(app, command(app, type="gui.work.native_capture", **fields))
    capture = await published_rpc(
        app,
        monkeypatch,
        command(app, type="gui.work.native_capture", runtime_controls_version=1),
    )
    await published_rpc(
        app,
        monkeypatch,
        command(
            app,
            type="gui.work.native_activate",
            instance=capture["instance"],
            activation_token=capture["activation_token"],
            runtime_controls_version=1,
        ),
    )
    assert not mcp.enabled(app)
    with pytest.raises(ValueError, match="Negotiate"):
        await gui_rpc.async_dispatch(app, cmd(app, "snapshot"))
    app.settings.mcp_enabled = True
    with pytest.raises(ValueError, match="policy"):
        controls._facts(app)


async def test_enable_requires_explicit_opt_in_and_capture_starts_off(app):
    app._launch_options.work_lifecycle_version = 1
    app._launch_options.mcp_lifecycle_version = 1
    app._rpc = app._gui_rpc_enabled = app._approval_host = True
    app._cli_tool_profile = app._active_tool_profile = "strict"
    app.settings.mcp_enabled = True
    with pytest.raises(ValueError, match="startup-off"):
        work.capture(
            app,
            command(
                app,
                type="gui.work.native_capture",
                runtime_controls_version=1,
                mcp_lifecycle_version=1,
            ),
        )
    app.settings.mcp_enabled = False
    capture = work.capture(
        app,
        command(
            app,
            type="gui.work.native_capture",
            runtime_controls_version=1,
            mcp_lifecycle_version=1,
        ),
    )
    with pytest.raises(ValueError, match="negotiation"):
        work.activate(
            app,
            command(
                app,
                type="gui.work.native_activate",
                instance=capture["instance"],
                activation_token=capture["activation_token"],
                runtime_controls_version=1,
            ),
        )


async def test_new_conversation_preflight_and_target_saved_true_overlay(
    armed, monkeypatch
):
    from litetui import conversation_transition

    app = armed
    await change(app, monkeypatch, "enable")
    snapshot = conversation_transition.snapshot(
        app, {"version": 1, "id": "new-snapshot", **work.identity(app)}
    )
    assert (
        not snapshot["actions"]["new"]["eligible"]
        and snapshot["actions"]["rename"]["eligible"]
    )
    with pytest.raises(ValueError, match="Disable"):
        await work.before_identity_transition(app)
    assert not work.bridge(app).closing
    await change(app, monkeypatch, "disable")
    old = work.bridge(app)
    proof = await work.before_identity_transition(app)
    assert old.closing
    app.settings.mcp_enabled = True  # controlled target's saved preference loaded by canonical settings adoption
    mcp.apply_startup_overlay(app)
    assert (
        app.settings.mcp_enabled is False
        and app._invocation_saved_values["mcp_enabled"] is True
    )
    # Repeated canonical adoption and retirement must retain the target's saved value.
    path = app.convo_dir / "settings.json"
    saved = json.loads(path.read_text())
    saved["execution"]["mcp_enabled"] = True
    path.write_text(json.dumps(saved), encoding="utf8")
    app._adopt_convo_settings(False)
    app._adopt_convo_settings(False)
    assert app._invocation_saved_values["mcp_enabled"] is True
    assert mcp._saved(app)[2].saved.mcp_enabled is True
    work.after_identity_transition(app, proof)
    assert app._invocation_saved_values["mcp_enabled"] is True
    assert json.loads(path.read_text())["execution"]["mcp_enabled"] is True
    captured = work.capture(
        app,
        command(
            app,
            type="gui.work.native_capture",
            runtime_controls_version=1,
            mcp_lifecycle_version=1,
        ),
    )
    assert (
        captured["runtime_policy"]["facts"]["mcp_enabled"] is False
        and captured["instance"] != old.instance
    )


@pytest.mark.parametrize(
    "fault", ["result-copy", "result-change", "foreign-task", "dispatch-change"]
)
async def test_exact_publication_rejects_substitution_and_retains_hold(
    armed, monkeypatch, fault
):
    app = armed
    checked = await review(app, monkeypatch, "enable")
    request = cmd(app, "apply", token=checked["token"])
    result = await gui_rpc.async_dispatch(app, request)
    if fault == "result-copy":
        result = copy.deepcopy(result)
    elif fault == "result-change":
        result["policy_after"]["generation"] += 1
    elif fault == "dispatch-change":
        app.mcp_dispatch["foreign"] = lambda _: "bad"
    if fault == "foreign-task":

        async def foreign():
            return mcp.prepare_publication(app, request, result)

        with pytest.raises(ValueError):
            await asyncio.create_task(foreign())
    else:
        with pytest.raises(ValueError):
            mcp.prepare_publication(app, request, result)
    assert mcp.blocking(app) and controls._state(app).generation == 0


async def test_saved_failure_is_held_and_never_starts_transport(armed, monkeypatch):
    from litetui.settings_service import SettingsService

    app = armed
    checked = await review(app, monkeypatch, "enable")

    def failure(*_args, **_kwargs):
        raise OSError("PRIVATE write failure")

    monkeypatch.setattr(SettingsService, "save_patch", failure)
    with pytest.raises(ValueError, match="uncertain"):
        await gui_rpc.async_dispatch(app, cmd(app, "apply", token=checked["token"]))
    assert not app._mcp_test_created and mcp.blocking(app)


async def test_disposable_stdio_transport_and_actual_authorized_host_dispatch(
    armed, monkeypatch, tmp_path
):
    import os
    import sys
    from pathlib import Path

    from test_rust_runtime_controls import cmd as runtime_cmd
    from test_rust_runtime_controls import snapshot_review

    from litetui import mcp_client, tool_approval, ttyguard

    app = armed
    root = app.mcp.root
    script = root / "controlled-stdio.py"
    script.write_text(
        "import json,sys\n"
        "for line in sys.stdin:\n"
        " m=json.loads(line)\n"
        " if 'id' not in m: continue\n"
        " method=m['method']\n"
        " assert method in ('initialize','tools/list','tools/call')\n"
        " result={'tools':[{'name':'private','inputSchema':{'type':'object','properties':{'value':{'type':'string'}}}}]} if method=='tools/list' else {'capabilities':{}}\n"
        " if method=='tools/call': result={'content':[{'type':'text','text':'CONTROLLED:'+m['params']['arguments']['value']}]}\n"
        " print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)\n",
        encoding="utf-8",
    )
    app._mcp_test_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "private": {"command": sys.executable, "args": ["-u", str(script)]}
                }
            }
        ),
        encoding="utf-8",
    )
    processes = []

    def spawn(argv, **kwargs):
        assert (
            argv == [sys.executable, "-u", str(script)] and Path(kwargs["cwd"]) == root
        )
        kwargs.update(text=True, encoding="utf-8", errors="replace")
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
        process = REAL_POPEN(argv, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(ttyguard, "popen", spawn)
    monkeypatch.setattr(
        app.mcp,
        "_build",
        lambda name, cfg: mcp_client.MCPManager._build(app.mcp, name, cfg),
    )
    await change(app, monkeypatch, "enable")
    await change(app, monkeypatch, "connect", "private")
    server = app.mcp.servers["private"]
    reader = server._reader
    assert len(processes) == 1 and processes[0].poll() is None and reader.is_alive()
    denied = await app._execute_tool(
        "mcp__private__private", {"value": "denied"}, hooks_enabled=False
    )
    assert not denied[1] and "disabled" in denied[0].lower()
    for field, value in (("tools_enabled", True), ("tool_profile", "interactive")):
        _, checked = await snapshot_review(app, monkeypatch, field, value)
        applied = await published_rpc(
            app,
            monkeypatch,
            {
                "id": "runtime-apply",
                "type": controls.PREFIX + "apply",
                "review": checked,
            },
        )
        await published_rpc(
            app,
            monkeypatch,
            runtime_cmd(
                app,
                "readback",
                action_id=checked["action_id"],
                review_token=checked["review_token"],
                apply_operation_id=applied["apply_operation_id"],
                readback_token=applied["readback_token"],
                result_sha256=work.digest(work.encoded(applied)),
            ),
        )
    approvals = []

    async def approve(owner, name, args, decision):
        approvals.append((owner, name, args, decision.action))
        return tool_approval.ONCE

    monkeypatch.setattr(tool_approval, "approve_over_rpc", approve)
    result = await app._execute_tool(
        "mcp__private__private", {"value": "proof"}, hooks_enabled=False
    )
    assert result[1] and "CONTROLLED:proof" in result[0] and len(approvals) == 1
    await change(app, monkeypatch, "disconnect", "private")
    assert (
        processes[0].poll() is not None
        and not reader.is_alive()
        and not app.mcp.servers
    )


@pytest.mark.parametrize(
    "fault",
    [
        "declaration",
        "settings-document",
        "conversation",
        "other-server",
        "effective-profile",
    ],
)
async def test_thread_effect_drift_keeps_exact_owned_cleanup_and_publication_hold(
    armed, monkeypatch, fault
):
    app = armed
    await change(app, monkeypatch, "enable")
    checked = await review(app, monkeypatch, "connect", "private")
    original = Server.start

    def changed(server):
        original(server)
        if fault == "declaration":
            raw = json.loads(app._mcp_test_config.read_text())
            raw["mcpServers"]["private"]["args"].append("changed during connect")
            app._mcp_test_config.write_text(json.dumps(raw), encoding="utf8")
        elif fault == "settings-document":
            path = app.mcp.root / "settings.json"
            raw = json.loads(path.read_text())
            raw["max_tokens_tools"] = 125
            path.write_text(json.dumps(raw), encoding="utf8")
        elif fault == "effective-profile":
            app._active_tool_profile = "autonomous"
        elif fault == "conversation":
            app.conversation.append({"role": "user", "content": "outside operation"})
        else:
            app.mcp.servers["unexpected"] = Server(
                "unexpected", {"command": "never-run"}
            )

    monkeypatch.setattr(Server, "start", changed)
    with pytest.raises(ValueError, match="uncertain"):
        await gui_rpc.async_dispatch(app, cmd(app, "apply", token=checked["token"]))
    assert app.mcp.servers["private"] is app._mcp_test_created[0]
    assert mcp.blocking(app) and controls._state(app).generation == 1


async def test_independent_dispatch_detects_same_name_foreign_callable(
    armed, monkeypatch
):
    app = armed
    await change(app, monkeypatch, "enable")
    await change(app, monkeypatch, "connect", "private")
    app.mcp_dispatch["mcp__private__private"] = lambda _: "foreign"
    with pytest.raises(ValueError, match="dispatch"):
        mcp.require_inventory(app)
    assert app._mcp_test_created[0].calls == 0


async def test_explicit_source_seat_subset_is_preserved(armed, monkeypatch):
    app = armed
    await change(app, monkeypatch, "enable")
    # A changed selection is itself unreviewed and cannot widen a captured seat.
    app.mcp.seat_servers = frozenset({"web"})
    with pytest.raises(ValueError, match="inventory"):
        await gui_rpc.async_dispatch(app, cmd(app, "snapshot"))
    assert not app._mcp_test_created


async def test_backend_transition_preserves_mcp_launch_capability(switch_app):
    from test_rust_backend_transition import command as backend_command

    from litetui.launch_options import LaunchOptions

    app = switch_app
    app._launch_options = LaunchOptions(
        work_lifecycle_version=1, mcp_lifecycle_version=1
    )
    result = await gui_rpc.async_dispatch(app, backend_command(app))
    assert result["status"] == "connected", result
    assert (
        app._launch_options.work_lifecycle_version
        == app._launch_options.mcp_lifecycle_version
        == 1
    )
    assert (
        app._launch_options.overrides(app.settings, app.backend.name, app.model_id)[
            "mcp_enabled"
        ]
        is False
    )


@pytest.mark.parametrize("armed", ["codex", "claude"], indirect=True)
async def test_provider_inventory_scope_and_native_codex_prohibition(
    armed, monkeypatch
):
    app = armed
    snap = await call(app, monkeypatch, "snapshot")
    provider = snap["provider"]
    assert snap["model_inventory"] == provider + "_fixed"
    if provider == "codex":
        for action in ("enable", "disable", "reload"):
            assert not snap["actions"][action]["eligible"]
            with pytest.raises(ValueError, match="Codex"):
                await gui_rpc.async_dispatch(
                    app,
                    cmd(
                        app,
                        "review",
                        revision=snap["revision"],
                        action=action,
                        target=None,
                    ),
                )
        assert all(
            not choice["eligible"]
            for row in snap["rows"]
            for choice in row["actions"].values()
        )
        assert not app._mcp_test_created and controls._state(app).generation == 0
    else:
        await change(app, monkeypatch, "enable")
        await change(app, monkeypatch, "connect", "private")
        snap = await call(app, monkeypatch, "snapshot")
        assert (
            snap["model_inventory"] == "claude_fixed" and snap["dispatch"]["count"] == 1
        )
        assert controls._facts(app)["inventory"] == "claude_session"


@pytest.mark.parametrize("fault", ["tools", "config", "state"])
async def test_unrelated_server_drift_during_connect_never_publishes(
    armed, monkeypatch, fault
):
    app = armed
    await change(app, monkeypatch, "enable")
    await change(app, monkeypatch, "connect", "web")
    other = app.mcp.servers["web"]
    checked = await review(app, monkeypatch, "connect", "private")
    start = Server.start

    def changed(server):
        start(server)
        if fault == "tools":
            other.tools.append({"name": "foreign", "inputSchema": {"type": "object"}})
        elif fault == "config":
            other.cfg = {**other.cfg, "headers": {"New": "private"}}
        else:
            other.error = "private failure"

    monkeypatch.setattr(Server, "start", changed)
    with pytest.raises(ValueError, match="uncertain"):
        await gui_rpc.async_dispatch(app, cmd(app, "apply", token=checked["token"]))
    assert app.mcp.servers["web"] is other and mcp.blocking(app)
    assert controls._state(app).generation == 2


@pytest.mark.parametrize("refused", [False, True])
async def test_mcp_retires_only_verified_finished_prior_runtime_control(
    armed, monkeypatch, refused
):
    from test_rust_runtime_controls import cmd as runtime_cmd
    from test_rust_runtime_controls import snapshot_review

    app = armed
    _, checked = await snapshot_review(
        app, monkeypatch, "plan_mode" if refused else "tools_enabled", True
    )
    if refused:
        from litetui import paths

        paths.PLAN_PROMPT_FILE.write_text("Changed after exact review", encoding="utf8")
    applied = await published_rpc(
        app,
        monkeypatch,
        {"id": "prior-apply", "type": controls.PREFIX + "apply", "review": checked},
    )
    await published_rpc(
        app,
        monkeypatch,
        runtime_cmd(
            app,
            "readback",
            action_id=checked["action_id"],
            review_token=checked["review_token"],
            apply_operation_id=applied["apply_operation_id"],
            readback_token=applied["readback_token"],
            result_sha256=work.digest(work.encoded(applied)),
        ),
    )
    prior = controls._state(app).attempt
    assert prior.verified and prior.readback_published and prior.worker.is_finished
    original_task = prior.readback_task
    owner = asyncio.create_task(asyncio.Event().wait())
    prior.readback_task = owner
    snap = await call(app, monkeypatch, "snapshot")
    with pytest.raises(ValueError, match="prior runtime control"):
        await gui_rpc.async_dispatch(
            app,
            cmd(app, "review", revision=snap["revision"], action="enable", target=None),
        )
    assert app.settings.mcp_enabled is False and controls._state(app).attempt is prior
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    prior.readback_task = original_task
    await change(app, monkeypatch, "enable")
    assert (
        prior.closed
        and controls._state(app).attempt is None
        and controls._state(app).generation == (1 if refused else 2)
    )
