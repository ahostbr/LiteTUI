"""Reviewed conversation actions against real owned stores; external work forbidden."""
from __future__ import annotations

import asyncio
import copy
import uuid
from dataclasses import replace

import pytest
import pytest_asyncio
from test_rust_backend_transition import disk, forbidden
from test_rust_backend_transition import switch_app as _controlled_app

from litetui import conversation_transition as transition
from litetui import gui_rpc, hook_host, lifecycle_hooks
from litetui.conversation import ConversationRepository
from litetui.plugins.convo import normalize_title

switch_app = _controlled_app
ENTER, LEAVE = hook_host.enter_conversation, hook_host.leave_conversation


@pytest_asyncio.fixture
async def app(switch_app, monkeypatch):
    instance = switch_app
    monkeypatch.setattr(hook_host, "enter_conversation", ENTER)
    monkeypatch.setattr(hook_host, "leave_conversation", LEAVE)
    instance._hook_conversation_id = instance.convo_id
    instance._hook_lifecycle = []
    instance._hook_source = "typed"
    instance._bind_task_store_original = instance._bind_task_store
    yield instance


def common(app):
    return {"version": 1, "session_id": app.convo_id,
            "agent_id": app._agent_session.authority.agent_id,
            "backend": app.backend.name, "model": app.model_id}


def reviewed(app, action="rename", title="  New\n title  "):
    cmd = common(app)
    state = transition.snapshot(app, cmd)
    return transition.validate(app, {**cmd, "snapshot": state["snapshot"], "action": action,
                                    **({"title": title} if action == "rename" else {})})


def pending(app):
    app._stage_new_convo(str(uuid.uuid4()))
    app.conversation = [{"role": "system", "content": "Pending system"}]
    app._hook_conversation_id = None


@pytest.mark.asyncio
@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("action", ["rename", "new"])
async def test_review_is_pure_and_apply_has_independent_durable_readback(app, staged, action):
    if staged:
        pending(app)
    old_id, old_path = app.convo_id, app.convo_path
    before, history, policy = disk(app), copy.deepcopy(app.conversation), gui_rpc._tool_policy(app)
    review = reviewed(app, action)
    assert disk(app) == before and app.conversation == history
    assert review["effects"]["materialize"] is (staged or action == "new")
    result = await gui_rpc.async_dispatch(app, {**review, "type": "gui.conversations.native_apply"})
    assert result["status"] == ("renamed" if action == "rename" else "created"), result
    assert result["mutated"] and not result["close_required"]
    assert result["persistence"] == {"transcript": True, "settings": True}
    assert all(result["lifecycle"].values())
    assert gui_rpc._tool_policy(app) == policy
    current = transition.snapshot(app, common(app))
    assert current["current"]["materialized"] and not current["current"]["pending"]
    meta, messages = ConversationRepository.read(app.convo_path)
    assert meta["id"] == result["session_id"] == review["target_session_id"]
    assert messages == app.conversation
    if action == "rename":
        assert current["current"]["title"] == meta["name"] == "New title"
        assert messages == history and app.convo_id == old_id
    else:
        assert all(message["role"] == "system" for message in messages)
        assert app.convo_id != old_id and all("provider_metadata" not in message for message in messages)
        assert (old_path.read_bytes() if old_path.exists() else None) == before.get(str(old_path))


@pytest.mark.parametrize("raw,expected", [("  a\n b\t c ", "a b c"), ("x" * 60, "x" * 60),
                                           ("x" * 61, "x" * 60 + "\u2026"), ("\r\n", "")])
def test_title_normalization_is_canonical(raw, expected):
    assert normalize_title(raw) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [None, True, 123, "", " \t\r\n", "a\0b", "a" * 4097, "\u754c" * 1400])
async def test_invalid_title_never_writes(app, raw):
    before = disk(app)
    state = transition.snapshot(app, common(app))
    with pytest.raises(ValueError):
        transition.validate(app, {**common(app), "snapshot": state["snapshot"], "action": "rename", "title": raw})
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["session_id", "agent_id", "backend", "model", "snapshot", "review",
                                   "action_id", "action", "target_session_id", "title", "version"])
async def test_changed_or_foreign_apply_consumes_review_without_writes(app, field):
    review = reviewed(app)
    before = disk(app)
    result = await transition.apply(app, {**review, field: "changed"})
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"], result
    assert disk(app) == before
    replay = await transition.apply(app, review)
    assert replay["status"] == "refused" and disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["history", "settings", "pending", "busy", "registry", "disk", "target"])
async def test_drift_refuses_before_effects(app, monkeypatch, fault):
    review = reviewed(app, "new")
    if fault == "history":
        app.conversation.append({"role": "user", "content": "Changed"})
    elif fault == "settings":
        app.settings.max_tokens_chat += 1
    elif fault == "pending":
        app._pending_input.append({"content": "retained"})
    elif fault == "busy":
        monkeypatch.setattr(app, "_chat_running", lambda: True)
    elif fault == "registry":
        app.plugins.prompt_sections = []
    elif fault == "disk":
        with app.convo_path.open("ab") as out:
            out.write(b'{"type":"rename","name":"external"}\n')
    else:
        app._agent_session.conversation_directory(review["target_session_id"]).mkdir()
    before = disk(app)
    result = await transition.apply(app, review)
    assert result["status"] == "refused" and not result["mutated"] and not result["close_required"], result
    assert disk(app) == before


@pytest.mark.asyncio
async def test_mutable_registered_new_and_rename_handlers_never_run(app):
    for key in ("/new", "/rename"):
        entry = app.plugins.commands.get(key)
        if entry:
            app.plugins.commands[key] = replace(entry, handler=forbidden)
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == "created", result


@pytest.mark.asyncio
async def test_saved_rename_replay_is_not_another_append(app):
    review = reviewed(app)
    result = await transition.apply(app, review)
    before = disk(app)
    assert result["status"] == "renamed"
    assert (await transition.apply(app, review))["status"] == "refused"
    assert disk(app) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["materialize", "write", "settings", "task_bind"])
async def test_publication_failure_closes_all_prompt_admission(app, monkeypatch, fault):
    review = reviewed(app, "new")
    if fault == "materialize":
        monkeypatch.setattr(app, "_materialise_convo", lambda: None)
    elif fault == "write":
        monkeypatch.setattr(app.store, "write_record", lambda *_: app.store.note_error(OSError("controlled write")))
    elif fault == "settings":
        monkeypatch.setattr("litetui.convo_settings.save", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("controlled settings")))
    else:
        monkeypatch.setattr(app, "_bind_task_store", lambda: app._task_store_reported.add("controlled task failure"))
    result = await transition.apply(app, review)
    assert result["status"] == "failed" and result["close_required"] and result["mutated"], result
    history = copy.deepcopy(app.conversation)
    item = {"source": "rpc", "content": "must not enter"}
    assert await hook_host.admit_prompt_origin(app, item) is None
    assert hook_host.accept_prompt_origin(app, item) is None
    assert app.conversation == history
    with pytest.raises(ValueError, match="uncertain"):
        app._validate_owned_execution()


@pytest.mark.asyncio
@pytest.mark.parametrize("allowed", [True, False])
async def test_actual_hook_dispatch_is_recorded_and_never_prompts_for_approval(app, monkeypatch, allowed):
    config = app.hook_config
    config.atomic_write(config.path("global"), [lifecycle_hooks.Hook("transition", ("conversation_leave", "conversation_start"), "controlled")])
    calls = []

    async def authorize(*_args, **kwargs):
        assert kwargs["allow_prompt"] is False
        calls.append("authorize")

    async def run(hook, document, workspace, **kwargs):
        calls.append(document["event"])
        item = {"source": "rpc", "content": "injected by hook"}
        assert await hook_host.admit_prompt_origin(app, item) is None
        result = lifecycle_hooks.HookResult(allowed, "controlled failure" if not allowed else "", exit_code=0 if allowed else 1)
        kwargs["on_settled"](result, {"process_started": True, "process_exited": True,
                                      "pipes_settled": True, "kill_requested": False, "kill_confirmed": None})
        return result

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(lifecycle_hooks, "run_hook", run)
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == ("created" if allowed else "failed"), result
    assert result["close_required"] is (not allowed)
    assert calls[:2] == ["authorize", "conversation_leave"]


@pytest.mark.asyncio
async def test_apply_cancellation_during_lifecycle_latches_and_preserves_old_history(app, monkeypatch):
    config = app.hook_config
    config.atomic_write(config.path("global"), [lifecycle_hooks.Hook("slow", ("conversation_leave",), "controlled")])
    entered = asyncio.Event()

    async def authorize(*_args, **_kwargs):
        return None

    async def run(*_args, **_kwargs):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(lifecycle_hooks, "run_hook", run)
    old_id, before = app.convo_id, app.convo_path.read_bytes()
    worker = asyncio.create_task(transition.apply(app, reviewed(app, "new")))
    await asyncio.wait_for(entered.wait(), 2)
    worker.cancel()
    result = await worker
    assert result["status"] == "failed" and result["close_required"]
    assert app.convo_id == old_id and app.convo_path.read_bytes() == before

@pytest.mark.asyncio
async def test_hook_approval_needed_is_failed_without_opening_an_approval(app, monkeypatch):
    config = app.hook_config
    config.atomic_write(config.path("global"), [lifecycle_hooks.Hook("approval", ("conversation_leave",), "controlled")])
    calls = []

    async def authorize(*_args, **kwargs):
        calls.append(kwargs["allow_prompt"])
        return ("Independent approval is required", False)

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(lifecycle_hooks, "run_hook", forbidden)
    old_id, before = app.convo_id, app.convo_path.read_bytes()
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == "failed" and result["close_required"], result
    assert calls == [False] and app.convo_id == old_id and app.convo_path.read_bytes() == before
    assert not getattr(app, "_approval_waiters", {})


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["failure", "cancel", "double_cancel", "drift"])
async def test_native_cleanup_is_joined_before_history_or_storage_moves(app, monkeypatch, fault):
    entered, release = asyncio.Event(), asyncio.Event()
    settled = []
    app.backend.owns_native_turns = True

    async def close():
        entered.set()
        await release.wait()
        settled.append(True)
        if fault == "failure":
            raise OSError("controlled native close failure")

    cleanup = None

    def shutdown():
        nonlocal cleanup
        cleanup = asyncio.create_task(close())
        return cleanup

    app.backend.shutdown = shutdown
    review = reviewed(app, "new")
    old_id, before = app.convo_id, app.convo_path.read_bytes()
    task = asyncio.create_task(transition.apply(app, review))
    await asyncio.wait_for(entered.wait(), 2)
    assert app.convo_id == old_id and app.convo_path.read_bytes() == before
    if fault in ("cancel", "double_cancel"):
        task.cancel()
        await asyncio.sleep(0)
        if fault == "double_cancel":
            task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and app._conversation_transition_pending
    elif fault == "drift":
        app.settings.max_tokens_chat += 1
    release.set()
    result = await task
    assert settled and cleanup.done() and not cleanup.cancelled()
    assert result["status"] == "failed" and result["close_required"], result
    assert app.convo_id == old_id and app.convo_path.read_bytes() == before
    assert not app._conversation_transition_pending


@pytest.mark.asyncio
async def test_gui_and_direct_prompt_paths_refuse_while_apply_owns_lifecycle(app, monkeypatch):
    review = reviewed(app, "new")
    original = hook_host.leave_conversation
    seen = []

    def leave(instance):
        with pytest.raises(ValueError, match="transition is active"):
            gui_rpc.dispatch(instance, {"type": "gui.thinking.set", "level": "high"})
        with pytest.raises(ValueError, match="transition is active"):
            instance._validate_owned_execution()
        assert hook_host.accept_prompt_origin(instance, {"source": "rpc", "content": "not admitted"}) is None
        seen.append(True)
        original(instance)

    monkeypatch.setattr(hook_host, "leave_conversation", leave)
    result = await transition.apply(app, review)
    assert result["status"] == "created" and seen, result


@pytest.mark.asyncio
async def test_shutdown_requested_after_review_refuses_before_effects(app):
    review = reviewed(app, "new")
    before = disk(app)
    app._conversation_transition_shutdown_requested = True
    result = await transition.apply(app, review)
    assert result["status"] == "refused" and not result["mutated"]
    assert disk(app) == before


@pytest.mark.asyncio
async def test_hook_source_change_invalidates_review(app):
    review = reviewed(app, "new")
    app._hook_source = "scheduled"
    before = disk(app)
    result = await transition.apply(app, review)
    assert result["status"] == "refused" and not result["mutated"]
    assert disk(app) == before

@pytest.mark.asyncio
async def test_unverified_executed_hook_retains_cleanup_failure(app, monkeypatch):
    config = app.hook_config
    config.atomic_write(config.path("global"), [lifecycle_hooks.Hook("unknown-exit", ("conversation_leave",), "controlled")])

    async def authorize(*_args, **_kwargs):
        return None

    async def run(*_args, **_kwargs):
        return lifecycle_hooks.HookResult(False, "hook cancelled", exit_code=None)

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(lifecycle_hooks, "run_hook", run)
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == "failed" and result["close_required"]
    assert "termination is unverified" in app._conversation_transition_cleanup_error

@pytest.mark.asyncio
async def test_historical_task_store_diagnostic_does_not_poison_fresh_new(app):
    app._task_store_reported.add("old unrelated failure")
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == "created" and not result["close_required"], result
    assert app._task_store_reported == {"old unrelated failure"}


@pytest.mark.asyncio
async def test_current_deduplicated_task_bind_failure_is_still_fatal(app, monkeypatch):
    def bind(*_args, **_kwargs):
        raise OSError("same earlier failure")

    app._task_store_reported.add("old unrelated failure")
    monkeypatch.setattr("litetui.app.tasks_mod.bind", bind)
    monkeypatch.setattr(app, "_report_task_store_error", lambda _error: None)
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == "failed" and result["close_required"], result

@pytest.mark.asyncio
@pytest.mark.parametrize("kill_confirmed", [True, False])
async def test_canonical_cancel_exposes_real_worker_terminal_proof(app, monkeypatch, kill_confirmed):
    import io
    from types import SimpleNamespace

    from litetui import conversation_shutdown, ttyguard

    config = app.hook_config
    config.atomic_write(config.path("global"), [lifecycle_hooks.Hook("cancelled", ("conversation_leave",), "controlled")])
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()
    running = [True]
    proc = SimpleNamespace(pid=123456, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(),
                           poll=lambda: None if running[0] else 0, _litetui_job=None)

    def spawn(*_args, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        return proc

    def kill(_pid, actual):
        assert actual is proc
        if kill_confirmed:
            running[0] = False
        return kill_confirmed

    async def authorize(*_args, **kwargs):
        assert kwargs["allow_prompt"] is False

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(ttyguard, "popen", spawn)
    monkeypatch.setattr(ttyguard, "kill_tree", kill)
    worker = asyncio.create_task(transition.apply(app, reviewed(app, "new")))
    await asyncio.wait_for(entered.wait(), 2)
    worker.cancel()
    result = await worker
    assert result["status"] == "failed" and result["close_required"]
    if kill_confirmed:
        assert not getattr(app, "_conversation_transition_cleanup_error", None)
        proof = await conversation_shutdown.settle_before_shutdown(app)
        assert proof["worker_settled"] and proof["native_cleanup_settled"]
    else:
        assert "cleanup is unverified" in app._conversation_transition_cleanup_error
        with pytest.raises(RuntimeError, match="unproven process cleanup"):
            await conversation_shutdown.settle_before_shutdown(app)


@pytest.mark.asyncio
async def test_explicit_hook_isolation_preserves_canonical_disabled_behavior(app, monkeypatch):
    app.hook_config.path("global").write_bytes(b"malformed ignored hooks")
    monkeypatch.setenv("LITETUI_HOOKS", "off")
    review = reviewed(app, "new")
    assert not review["effects"]["lifecycle_hooks"]
    result = await transition.apply(app, review)
    assert result["status"] == "created", result

@pytest.mark.asyncio
async def test_unexpected_error_after_hook_spawn_cannot_claim_shutdown_cleanup(app, monkeypatch):
    import io
    from types import SimpleNamespace

    from litetui import conversation_shutdown, ttyguard

    app.hook_config.atomic_write(app.hook_config.path("global"),
        [lifecycle_hooks.Hook("start-error", ("conversation_leave",), "controlled")])
    proc = SimpleNamespace(pid=123457, stdin=io.StringIO(), stdout=io.StringIO(), stderr=io.StringIO(),
                           poll=lambda: None, _litetui_job=None)
    original_start = lifecycle_hooks.threading.Thread.start

    def start(thread):
        if getattr(thread._target, "__name__", "") == "drain":
            raise RuntimeError("controlled pipe-thread start failure")
        return original_start(thread)

    async def authorize(*_args, **_kwargs):
        return None

    monkeypatch.setattr(app, "_authorize_action", authorize)
    monkeypatch.setattr(ttyguard, "popen", lambda *_args, **_kwargs: proc)
    monkeypatch.setattr(lifecycle_hooks.threading.Thread, "start", start)
    result = await transition.apply(app, reviewed(app, "new"))
    assert result["status"] == "failed" and result["close_required"]
    assert "did not report terminal cleanup proof" in app._conversation_transition_cleanup_error
    with pytest.raises(RuntimeError, match="unproven process cleanup"):
        await conversation_shutdown.settle_before_shutdown(app)
