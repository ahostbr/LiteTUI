"""Reviewed model lifecycle: owned identity and canonical controls behind private substitutes."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import asdict

import pytest
from test_rust_context_reader import app, armed, switch_app

from litetui import background_input as work
from litetui import (
    convo_settings,
    gui_rpc,
    llm_backend,
    model_transport,
    store_identity,
)

_CANONICAL_LM_LOAD = llm_backend.LMStudioBackend.load
from litetui import native_model_lifecycle as models

__all__ = ["app", "armed", "switch_app"]
pytestmark = pytest.mark.asyncio


def command(owner, operation, **fields):
    return {
        "type": models.PREFIX + operation,
        "id": "lifecycle-" + operation,
        "version": 1,
        **work.identity(owner),
        "work_instance": work.bridge(owner).instance,
        **fields,
    }


async def call(owner, operation, **fields):
    from litetui import rpc

    cmd = command(owner, operation, **fields)
    result = await gui_rpc.async_dispatch(owner, cmd)
    rpc.publish_management_result(owner, cmd, result)
    return result


def prepare(owner):
    from litetui.app import LiteTUI
    from litetui import effort_projection

    effort_projection.install(owner)

    owner.connect = lambda: owner.run_worker(
        inspect.unwrap(LiteTUI.connect)(owner), group="init", exclusive=True
    )
    owner._fetch_ctx_window = lambda: owner.run_worker(
        inspect.unwrap(LiteTUI.fetch_context_window)(owner), group="ctx", exclusive=True
    )
    owner._launch_prepared = True
    owner._launch_options.server_mode = "connect"
    owner._connect_settled = True
    owner._gui_connection_success = True
    owner._client_binding = model_transport.bind_client(owner.client, owner.backend)


async def review(owner, action="reconnect"):
    snap = await call(owner, "snapshot")
    return await call(owner, "review", revision=snap["revision"], action=action)


async def test_custom_inspection_refuses_load_and_does_not_claim_residency(armed):
    prepare(armed)
    result = await call(armed, "snapshot")
    assert result["reported_loaded"] is None
    assert result["actions"]["reconnect"]["eligible"]
    assert not result["actions"]["load"]["eligible"]
    assert not result["actions"]["unload"]["eligible"]
    with pytest.raises(ValueError, match="no standard"):
        await call(armed, "review", revision=result["revision"], action="load")


async def test_reconnect_requires_connect_only_prepared_runtime(armed):
    for mode, prepared in (("auto", True), ("connect", False)):
        armed._launch_options.server_mode = mode
        armed._launch_prepared = prepared
        result = await call(armed, "snapshot")
        assert not any(row["eligible"] for row in result["actions"].values())


async def test_reviewed_reconnect_joins_canonical_connect_and_independent_readback(
    armed,
):
    prepare(armed)
    before = armed.convo_path.read_bytes()
    saved = asdict(convo_settings.load(armed.convo_dir))
    backend, client, bridge = armed.backend, armed.client, work.bridge(armed)
    r = await review(armed)
    a = await call(armed, "apply", token=r["token"])
    assert armed._backend_transition_inflight
    assert (
        work.prompt_refusal(armed)
        == "A reviewed model operation owns admission until its independent readback"
    )
    with pytest.raises(ValueError, match="exact model operation readback"):
        await call(armed, "apply", token=r["token"])
    value = await call(armed, "readback", token=r["token"], apply_id=a["apply_id"])
    assert value["kind"] == "model_lifecycle_settled"
    assert value["state"]["connected"]
    assert not armed._backend_transition_inflight
    assert (
        armed.backend is backend
        and armed.client is client
        and work.bridge(armed) is bridge
    )
    assert armed.convo_path.read_bytes() == before
    assert asdict(convo_settings.load(armed.convo_dir)) == saved
    with pytest.raises(ValueError, match="again"):
        await call(armed, "readback", token=r["token"], apply_id=a["apply_id"])


async def test_stale_review_consumed_without_reconnect(armed, monkeypatch):
    prepare(armed)
    r = await review(armed)
    armed.settings.llama_load_settings[armed.model_id] = {"ctx": 4096}
    monkeypatch.setattr(armed, "connect", lambda: pytest.fail("Stale review connected"))
    with pytest.raises(ValueError, match="changed after review"):
        await call(armed, "apply", token=r["token"])
    with pytest.raises(ValueError, match="again"):
        await call(armed, "apply", token=r["token"])


async def test_cancelled_model_read_joins_real_async_boundary(armed, monkeypatch):
    prepare(armed)
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = armed.backend.list_models

    async def held():
        entered.set()
        await release.wait()
        try:
            return await original()
        finally:
            finished.set()

    monkeypatch.setattr(armed.backend, "list_models", held)
    task = asyncio.create_task(call(armed, "snapshot"))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()


async def test_post_apply_readback_failure_closes_admission_and_never_replays(armed):
    prepare(armed)
    r = await review(armed)
    a = await call(armed, "apply", token=r["token"])
    armed.settings.llama_load_settings[armed.model_id] = {"ctx": 4096}
    with pytest.raises(ValueError, match="changed after the operation"):
        await call(armed, "readback", token=r["token"], apply_id=a["apply_id"])
    assert armed._backend_transition_error == models.CLOSE_REQUIRED


def lm_runtime(owner, monkeypatch):
    owner.settings.backend = "lmstudio"
    owner._agent_session.update_execution(
        backend="lmstudio", model=owner.model_id, thinking_level="default"
    )
    owner._cli_initial_backend = "lmstudio"
    owner._launch_backend = "lmstudio"
    owner.backend = llm_backend.LMStudioBackend(owner.settings)
    owner.client.base_url = owner.backend.base_url()
    owner._prompt_store_identity = store_identity.capture(owner)
    state = {"loaded": False, "calls": [], "gates": 0}
    monkeypatch.setattr(llm_backend.LMStudioBackend, "load", _CANONICAL_LM_LOAD)

    def native_rows():
        return [
            {
                "id": owner.model_id,
                "type": "llm",
                "max_context_length": 8192,
                "loaded_context_length": 4096 if state["loaded"] else None,
            }
        ]

    monkeypatch.setattr(owner.backend, "_native_models", native_rows)

    # Undo the fixture's listing replacement; exercise the canonical projection.
    async def listing():
        return await asyncio.to_thread(owner.backend._list_sync)

    monkeypatch.setattr(owner.backend, "list_models", listing)

    class Sdk:
        def load(self, key, config=None):
            assert key == owner.model_id
            state["calls"].append(("load", config))
            state["loaded"] = True

        def unload(self, key):
            assert key == owner.model_id
            state["calls"].append(("unload", None))
            state["loaded"] = False

    monkeypatch.setattr(owner.backend, "_sdk", Sdk)

    async def allow(key):
        assert key == owner.model_id
        state["gates"] += 1
        return True

    owner.backend.vram_gate = allow
    owner.backend.on_load_settings = owner.remember_load_settings
    prepare(owner)
    owner._launch_options.work_lifecycle_version = 1
    c = {
        "type": "gui.work.native_capture",
        "id": "capture",
        "version": 1,
        **work.identity(owner),
    }
    captured = work.capture(owner, c)
    work.activate(
        owner,
        {
            **c,
            "type": "gui.work.native_activate",
            "instance": captured["instance"],
            "activation_token": captured["activation_token"],
        },
    )
    return state


async def test_lmstudio_canonical_load_unload_and_gate_with_real_connection(
    app, monkeypatch
):
    state = lm_runtime(app, monkeypatch)
    original = app.convo_path.read_bytes()
    for action, loaded in (("load", True), ("unload", False)):
        r = await review(app, action)
        a = await call(app, "apply", token=r["token"])
        result = await call(app, "readback", token=r["token"], apply_id=a["apply_id"])
        assert result["state"]["reported_loaded"] is loaded
        assert result["state"]["connected"] and app.ctx_loaded is loaded
    assert state["calls"] == [("load", None), ("unload", None)]
    assert state["gates"] == 1
    assert app.convo_path.read_bytes() == original


async def test_lmstudio_gate_refusal_never_reaches_sdk_or_replays(app, monkeypatch):
    state = lm_runtime(app, monkeypatch)

    async def refuse(_key):
        return False

    app.backend.vram_gate = refuse
    r = await review(app, "load")
    with pytest.raises(llm_backend.VramRefused):
        await call(app, "apply", token=r["token"])
    assert state["calls"] == []
    assert not getattr(app, "_backend_transition_error", None)
    assert not app._backend_transition_inflight


async def test_lmstudio_partial_load_failure_is_not_rolled_back(app, monkeypatch):
    state = lm_runtime(app, monkeypatch)

    class Partial:
        def load(self, key, config=None):
            state["calls"].append(("load", config))
            state["loaded"] = True
            raise OSError("controlled lost SDK reply")

    monkeypatch.setattr(app.backend, "_sdk", Partial)
    r = await review(app, "load")
    with pytest.raises(ValueError, match="uncertain"):
        await call(app, "apply", token=r["token"])
    assert state["loaded"] and state["calls"] == [("load", None)]
    assert app._backend_transition_error == models.CLOSE_REQUIRED
    with pytest.raises(ValueError, match="again"):
        await call(app, "apply", token=r["token"])


async def test_cancelled_load_joins_sdk_thread_before_close(app, monkeypatch):
    import threading

    state = lm_runtime(app, monkeypatch)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Held:
        def load(self, key, config=None):
            entered.set()
            assert release.wait(5), "test release was not supplied"
            state["loaded"] = True
            finished.set()

    monkeypatch.setattr(app.backend, "_sdk", Held)
    r = await review(app, "load")
    task = asyncio.create_task(call(app, "apply", token=r["token"]))
    while not entered.is_set():
        await asyncio.sleep(0.001)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not finished.is_set()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set() and state["loaded"]
    assert app._backend_transition_error == models.CLOSE_REQUIRED


async def test_exact_readback_publication_owns_hold_and_one_wake(armed, monkeypatch):
    prepare(armed)
    r = await review(armed)
    a = await call(armed, "apply", token=r["token"])
    cmd = command(armed, "readback", token=r["token"], apply_id=a["apply_id"])
    # New Work may enqueue while held; its arrival does not change model authority.
    armed._pending_input.append(
        {
            "content": "queued during lifecycle",
            "source": "harness",
            "tool_profile": "strict",
        }
    )
    result = await gui_rpc.async_dispatch(armed, cmd)
    assert armed._native_model_lifecycle_hold
    assert work.prompt_refusal(armed)
    ticket = work.prepare_publication(armed, cmd, result)
    assert armed._native_model_lifecycle_hold
    wakes = []
    monkeypatch.setattr(armed, "call_later", lambda callback: wakes.append(callback))
    work.finish_publication(armed, ticket)
    assert not armed._native_model_lifecycle_hold
    assert wakes == [armed._flush_pending_input]
    with pytest.raises(ValueError, match="one-use"):
        work.finish_publication(armed, ticket)
    assert wakes == [armed._flush_pending_input]
    armed._pending_input.clear()


async def test_foreign_or_changed_publication_retains_hold(armed):
    import copy

    prepare(armed)
    r = await review(armed)
    a = await call(armed, "apply", token=r["token"])
    cmd = command(armed, "readback", token=r["token"], apply_id=a["apply_id"])
    result = await gui_rpc.async_dispatch(armed, cmd)
    with pytest.raises(ValueError, match="foreign"):
        work.prepare_publication(armed, cmd, copy.deepcopy(result))
    assert armed._native_model_lifecycle_hold
    assert armed._backend_transition_error == models.CLOSE_REQUIRED


async def test_saved_context_is_requested_and_independently_verified(app, monkeypatch):
    state = lm_runtime(app, monkeypatch)
    app.settings.default_context_length = 4096
    app.settings.llama_load_settings[app.model_id] = {"ctx": 1024}
    r = await review(app, "load")
    a = await call(app, "apply", token=r["token"])
    result = await call(app, "readback", token=r["token"], apply_id=a["apply_id"])
    assert state["calls"] == [("load", {"contextLength": 4096})]
    assert result["state"]["context_length"] == 4096
    assert convo_settings.load(app.convo_dir).lmstudio_load == {"ctx": 4096}


async def test_llama_connect_only_cooperating_router_uses_canonical_routes(
    app, monkeypatch
):
    from pathlib import Path
    from types import SimpleNamespace

    from litetui import router_record

    state = {"loaded": False, "requests": []}
    app.settings.backend = "llamacpp"
    app.settings.llama_host = "http://127.0.0.1:45125"
    app._agent_session.update_execution(
        backend="llamacpp", model=app.model_id, thinking_level="default"
    )
    app._cli_initial_backend = "llamacpp"
    app._launch_backend = "llamacpp"
    backend = llm_backend.LlamaCppBackend(app.settings)
    app.backend = backend
    app.client.base_url = backend.base_url()
    app._prompt_store_identity = store_identity.capture(app)
    backend.require_existing_server()
    backend._attached_host = backend._host
    backend._attached_owner = "controlled-router"
    monkeypatch.setattr(llm_backend, "scan_models", lambda _: [])
    monkeypatch.setattr(
        llm_backend, "llama_executable", lambda _: Path("unused-no-spawn")
    )
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: host == backend._host)
    record = SimpleNamespace(
        is_mine=False, pid=7, port=45125, owner="controlled-router"
    )
    monkeypatch.setattr(router_record, "read", lambda: record)
    monkeypatch.setattr(router_record, "is_live", lambda value: value is record)

    def request(url, body=None, **_kwargs):
        assert url.startswith(backend._host + "/")
        route = url.removeprefix(backend._host)
        state["requests"].append((route, body))
        if route == "/props" and body is None:
            return {"role": "router"}
        if route == "/models" and body is None:
            return {
                "data": [
                    {
                        "id": app.model_id,
                        "status": {
                            "value": "loaded" if state["loaded"] else "unloaded"
                        },
                        "args": ["--ctx-size", "4096"],
                    }
                ]
            }
        assert body == {"model": app.model_id}
        if route == "/models/load":
            state["loaded"] = True
        elif route == "/models/unload":
            state["loaded"] = False
        else:
            pytest.fail("Unreviewed router route")
        return {"success": True}

    monkeypatch.setattr(llm_backend, "_http_json", request)

    async def allowed(_key):
        return True

    backend.vram_gate = allowed
    prepare(app)
    app._launch_options.work_lifecycle_version = 1
    c = {
        "type": "gui.work.native_capture",
        "id": "capture",
        "version": 1,
        **work.identity(app),
    }
    captured = work.capture(app, c)
    work.activate(
        app,
        {
            **c,
            "type": "gui.work.native_activate",
            "instance": captured["instance"],
            "activation_token": captured["activation_token"],
        },
    )
    app.settings.default_context_length = 3072
    app.settings.llama_load_settings[app.model_id] = {"ctx": 1024}
    snap = await call(app, "snapshot")
    assert not snap["actions"]["load"]["eligible"]
    assert "owns its presets" in snap["actions"]["load"]["reason"]
    explicit = await call(app, "snapshot", target=app.model_id, context_mode="default", context_value=None)
    assert explicit["actions"]["load"]["eligible"] and explicit["load_context"] is None
    assert explicit["preset_context"] == 1024
    assert not any(body is not None for _, body in state["requests"])
    app.settings.default_context_length = None
    for action in ("load", "unload", "reconnect"):
        r = await review(app, action)
        a = await call(app, "apply", token=r["token"])
        settled = await call(app, "readback", token=r["token"], apply_id=a["apply_id"])
        assert settled["state"]["connected"] and backend._owned is None
    assert [route for route, body in state["requests"] if body is not None] == [
        "/models/load",
        "/models/unload",
    ]
    backend._attached_owner = None
    snap = await call(app, "snapshot")
    assert not snap["actions"]["load"]["eligible"]
    assert snap["actions"]["reconnect"]["eligible"]


@pytest.mark.parametrize("persistent", [False, True])
async def test_replaced_client_close_failure_is_retained_for_joined_shutdown(
    armed, monkeypatch, persistent
):
    from litetui import conversation_action
    from litetui.app import LiteTUI

    prepare(armed)
    old = armed.client
    old.api_key = "obsolete-private-binding"
    close = old.close
    attempts = []

    async def failing_close():
        attempts.append("close")
        if persistent or len(attempts) == 1:
            raise OSError("controlled client cleanup failure")
        await close()

    monkeypatch.setattr(old, "close", failing_close)
    receipt = await review(armed)
    try:
        with pytest.raises(ValueError, match="uncertain"):
            await call(armed, "apply", token=receipt["token"])
        assert armed.client is not old and not old.is_closed()
        assert len(attempts) == 1
        assert armed._native_model_lifecycle_clients[0].client is old
        assert armed._native_model_lifecycle_action is None
        assert armed._backend_transition_error == models.CLOSE_REQUIRED
        assert armed._native_model_lifecycle_hold
        assert armed._native_model_lifecycle_review is None

        responses, exits = [], []
        armed._conversation_rewrite_shutdown_callbacks = [
            lambda **value: responses.append(value)
        ]
        monkeypatch.setattr(armed, "exit", lambda: exits.append(True))
        await conversation_action._finish_exit(armed)
        assert len(attempts) == 2
        assert responses[0]["ok"] is (not persistent)
        assert bool(exits) is (not persistent)
        assert old.is_closed() is (not persistent)
        # Cleanup cannot retroactively verify a model effect or reopen admission.
        assert armed._backend_transition_error == models.CLOSE_REQUIRED
        assert armed._native_model_lifecycle_hold
        assert armed._backend_transition_inflight
        if persistent:
            assert armed._native_model_lifecycle_clients[0].client is old
            released = []

            async def release():
                released.append(True)

            monkeypatch.setattr(armed, "_release_owned_storage", release)
            with pytest.raises(OSError, match="controlled client cleanup"):
                await LiteTUI._shutdown(armed)
            assert len(attempts) == 3 and not released and not old.is_closed()
            assert armed._native_model_lifecycle_clients[0].client is old
        else:
            assert armed._native_model_lifecycle_clients == []
            await models.settle_before_shutdown(armed)
            assert len(attempts) == 2
    finally:
        await close()
        # This is private test cleanup, never a product recovery exemption.
        armed._native_model_lifecycle_clients.clear()


async def test_shutdown_joins_inflight_replacement_close_without_duplicate_attempt(
    armed, monkeypatch
):
    from litetui import conversation_action

    prepare(armed)
    old = armed.client
    old.api_key = "obsolete-private-binding"
    close = old.close
    entered, release, retired = asyncio.Event(), asyncio.Event(), asyncio.Event()
    attempts = []

    async def held_close():
        attempts.append("close")
        entered.set()
        await release.wait()
        await close()
        retired.set()

    monkeypatch.setattr(old, "close", held_close)
    receipt = await review(armed)
    action = asyncio.create_task(call(armed, "apply", token=receipt["token"]))
    exits, responses = [], []
    shutdown = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert armed._native_model_lifecycle_clients[0].client is old
        assert armed._native_model_lifecycle_action is action
        armed._conversation_rewrite_shutdown_callbacks = [
            lambda **value: responses.append(value)
        ]
        monkeypatch.setattr(armed, "exit", lambda: exits.append(True))
        shutdown = asyncio.create_task(conversation_action._finish_exit(armed))
        # Wait for the actual shutdown hook to cancel and join the admitted owner.
        for _ in range(100):
            if action.cancelling():
                break
            await asyncio.sleep(0.01)
        assert action.cancelling()
        assert not action.done() and not shutdown.done()
        assert not responses and not exits and not old.is_closed()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await action
        await shutdown
        assert retired.is_set() and old.is_closed() and len(attempts) == 1
        assert responses[0]["ok"] and exits == [True]
        assert armed._native_model_lifecycle_clients == []
        assert armed._native_model_lifecycle_action is None
        assert armed._backend_transition_error == models.CLOSE_REQUIRED
        assert armed._native_model_lifecycle_hold
    finally:
        release.set()
        await asyncio.gather(action, return_exceptions=True)
        if shutdown is not None:
            await shutdown
        await close()


async def test_successful_close_return_without_closed_client_cannot_acknowledge_shutdown(
    armed, monkeypatch
):
    from litetui import conversation_action

    prepare(armed)
    old = armed.client
    old.api_key = "obsolete-private-binding"
    close = old.close
    attempts = []

    async def unchanged():
        attempts.append("close")

    monkeypatch.setattr(old, "close", unchanged)
    receipt = await review(armed)
    try:
        with pytest.raises(ValueError, match="uncertain"):
            await call(armed, "apply", token=receipt["token"])
        responses, exits = [], []
        armed._conversation_rewrite_shutdown_callbacks = [
            lambda **value: responses.append(value)
        ]
        monkeypatch.setattr(armed, "exit", lambda: exits.append(True))
        await conversation_action._finish_exit(armed)
        assert len(attempts) == 2 and not old.is_closed()
        assert responses[0]["ok"] is False and not exits
        assert armed._native_model_lifecycle_clients[0].client is old
    finally:
        await close()
        armed._native_model_lifecycle_clients.clear()


async def test_shutdown_waiter_cancellation_does_not_abandon_shared_close_retry(
    armed, monkeypatch
):
    prepare(armed)
    old = armed.client
    old.api_key = "obsolete-private-binding"
    close = old.close
    entered, release = asyncio.Event(), asyncio.Event()
    attempts = []

    async def retry_close():
        attempts.append("close")
        if len(attempts) == 1:
            raise OSError("controlled first close failure")
        entered.set()
        await release.wait()
        await close()

    monkeypatch.setattr(old, "close", retry_close)
    receipt = await review(armed)
    waiters = []
    try:
        with pytest.raises(ValueError, match="uncertain"):
            await call(armed, "apply", token=receipt["token"])
        waiters.append(asyncio.create_task(models.settle_before_shutdown(armed)))
        await asyncio.wait_for(entered.wait(), 2)
        waiters.append(asyncio.create_task(models.settle_before_shutdown(armed)))
        await asyncio.sleep(0)
        waiters[0].cancel()
        await asyncio.sleep(0)
        assert not any(task.done() for task in waiters)
        assert len(attempts) == 2 and not old.is_closed()
        assert armed._native_model_lifecycle_clients[0].client is old
        release.set()
        await asyncio.gather(*waiters)
        assert len(attempts) == 2 and old.is_closed()
        assert armed._native_model_lifecycle_clients == []
        assert armed._backend_transition_error == models.CLOSE_REQUIRED
        assert armed._native_model_lifecycle_hold
    finally:
        release.set()
        await asyncio.gather(*waiters, return_exceptions=True)
        await close()
        armed._native_model_lifecycle_clients.clear()
