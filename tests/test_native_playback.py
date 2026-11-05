"""Review authority and no-replay receipts without any audio or remote service."""
from __future__ import annotations

import copy

import pytest
from test_rust_native_history import common, save_history
from test_rust_runtime_controls import app as _app
from test_rust_runtime_controls import armed as _armed
from test_rust_runtime_controls import switch_app as _switch_app

from litetui import background_input as work
from litetui import native_history, voice_backend
from litetui import native_playback as playback

app, armed, switch_app = _app, _armed, _switch_app
pytestmark = pytest.mark.asyncio


def review_command(runtime):
    tick = chr(96)
    save_history(runtime, [
        {"role": "system", "content": "Private playback fixture"},
        {"role": "assistant", "content": "First answer"},
        {"role": "assistant", "content": f"Second answer {tick*3}secret code{tick*3} with {tick}inline{tick} end."},
    ])
    command = common(runtime, id=work.ident())
    descriptor, _, index = native_history._collect(runtime, command)
    block = [block for block in index.view.blocks if block.kind == "assistant"][-1]
    return {**command, "type": playback.OPERATION, "snapshot": descriptor["snapshot"],
            "block_index": block.index, "block_id": block.block_id,
            "message_index": block.message_index, "message_sha256": block.message_sha256,
            "action": "review", "playback": None, "text_sha256": None}


def follow(command, receipt, action):
    return {**command, "id": work.ident(), "action": action,
            "playback": receipt["playback"], "text_sha256": receipt["text_sha256"]}


async def test_selected_response_and_settings_are_reviewed_without_execution(armed, monkeypatch):
    runtime, _ = armed
    runtime.settings.tts_enabled = True
    command = review_command(runtime)
    monkeypatch.setattr(playback, "_command", lambda *_: pytest.fail("review launched playback"))
    before = copy.deepcopy(runtime.conversation)
    review = await playback.dispatch(runtime, command)
    assert review["text"] == "Second answer with end."
    assert runtime._native_playback.task is None and runtime.conversation == before
    for field, value in [("block_id", "0" * 64), ("message_index", 1),
                         ("snapshot", "0" * 64), ("text_sha256", "0" * 64),
                         ("from_session_id", work.ident())]:
        changed = follow(command, review, "start")
        changed[field] = value
        with pytest.raises(ValueError):
            await playback.dispatch(runtime, changed)
        assert runtime._native_playback.task is None
    runtime.settings.tts_voice = "Changed fixture voice"
    with pytest.raises(ValueError, match="changed"):
        await playback.dispatch(runtime, follow(command, review, "start"))
    close_command = follow(command, review, "close")
    closed = await playback.dispatch(runtime, close_command)
    assert playback.blocking(runtime)
    work.finish_publication(runtime, work.prepare_publication(runtime, close_command, closed))
    assert closed["status"] == "closed" and closed["outcome"] == "review"
    assert runtime._native_playback is None and runtime.conversation == before
    with pytest.raises(ValueError, match="stale"):
        await playback.dispatch(runtime, follow(command, review, "start"))


async def test_disabled_and_stale_history_refuse_before_dispatch(armed):
    runtime, _ = armed
    runtime.settings.tts_enabled = False
    command = review_command(runtime)
    with pytest.raises(ValueError, match="Enable response"):
        await playback.dispatch(runtime, command)
    assert not playback.blocking(runtime)
    runtime.settings.tts_enabled = True
    command = review_command(runtime)
    review = await playback.dispatch(runtime, command)
    runtime.conversation[-1]["content"] = "Changed selected answer"
    runtime._snapshot("private changed answer")
    with pytest.raises(ValueError, match="changed"):
        await playback.dispatch(runtime, follow(command, review, "start"))
    assert runtime._native_playback.task is None
    close_command = follow(command, review, "close")
    closed = await playback.dispatch(runtime, close_command)
    work.finish_publication(runtime, work.prepare_publication(runtime, close_command, closed))


@pytest.mark.parametrize("ack,route", [(True, "queued"), (False, "uncertain")])
async def test_canonical_dispatch_receipt_preserves_remote_ownership(monkeypatch, ack, route):
    calls = []
    monkeypatch.setattr(voice_backend.litesuite_tts, "speak", lambda text: calls.append(text) or ack)
    monkeypatch.setattr(voice_backend.optional_python, "resolve", lambda *_: pytest.fail("remote request replayed locally"))
    receipt = voice_backend.dispatch_speech("Exact reviewed response")
    assert receipt.route == route and receipt.completed is None and receipt.process is None
    assert calls == ["Exact reviewed response"]


@pytest.mark.parametrize("change", ["user", "huge", "block_identity", "timeout"])
async def test_unsupported_response_and_budget_shapes_refuse_before_capture(armed, change):
    runtime, _ = armed
    runtime.settings.tts_enabled = True
    command = review_command(runtime)
    if change == "timeout":
        runtime.settings.tts_timeout = 3601
        command = review_command(runtime)
    else:
        if change == "user":
            runtime.conversation[-1]["role"] = "user"
        elif change == "huge":
            runtime.conversation[-1]["content"] = "x" * 16385
        else:
            command["block_id"] = "0" * 64
        if change != "block_identity":
            runtime._snapshot("private unsupported response")
            descriptor, _, index = native_history._collect(runtime, command)
            block = index.view.blocks[-1]
            command.update(snapshot=descriptor["snapshot"], block_index=block.index,
                block_id=block.block_id, message_index=block.message_index, message_sha256=block.message_sha256)
    with pytest.raises(ValueError):
        await playback.dispatch(runtime, command)
    assert not playback.blocking(runtime)


@pytest.mark.parametrize("status", ["local", "queued"])
@pytest.mark.parametrize("publication_fails", [False, True])
async def test_playback_holds_automatic_work_through_exact_close_publication(armed, monkeypatch, status, publication_fails):
    import asyncio
    from types import SimpleNamespace

    from test_rust_background_input import settle_inputs

    from litetui import native_runtime_controls as controls
    from litetui import rpc

    runtime, _ = armed
    runtime.settings.tts_enabled = True
    runtime.settings.tts_litesuite_first = status == "queued"
    monkeypatch.setattr(runtime, "_user_bubble", lambda *_args, **_kwargs: SimpleNamespace(border_title="You"))
    monkeypatch.setattr(runtime, "_chat_running", lambda: any(w.group == "chat" and not w.is_finished for w in runtime.workers))
    wakeups = []
    def schedule(callback, *args):
        wakeups.append(callback)
        asyncio.get_running_loop().call_soon(callback, *args)
    monkeypatch.setattr(runtime, "call_later", schedule)
    async def private_dispatch(owner):
        (owner.directory / "route").write_text(status, encoding="ascii")
        if status == "queued":
            owner.status = status
            return
        while not owner.cancel.is_set():
            await asyncio.sleep(0.01)
        owner.status = "stopped"
    monkeypatch.setattr(playback, "_run", private_dispatch)
    command = review_command(runtime)
    review = await playback.dispatch(runtime, command)
    assert controls.mutation_hold(runtime)
    await playback.dispatch(runtime, follow(command, review, "start"))
    async def output():
        return "Automatic output held behind response playback"
    before = copy.deepcopy(runtime.conversation)
    runtime._start_background("bash", {"command": "controlled coroutine only"}, output())
    for _ in range(300):
        if work.bridge(runtime).inputs:
            break
        await asyncio.sleep(0.01)
    entry = next(iter(work.bridge(runtime).inputs.values()))
    await asyncio.sleep(0.05)
    assert runtime._pending_input == [entry.item] and entry.admission is None and entry.claim_task is None
    runtime._flush_pending_input()
    assert runtime.conversation == before and not runtime._rewrite_test_requests
    close_command = follow(command, review, "close")
    result = await playback.dispatch(runtime, close_command)
    assert result["outcome"] == ("stopped" if status == "local" else "queued")
    assert controls.mutation_hold(runtime) and not wakeups
    with pytest.raises(ValueError, match="publication"):
        work.prepare_publication(runtime, close_command, dict(result))
    def emit(row):
        if row.get("ok"):
            assert controls.mutation_hold(runtime) and entry.admission is None
            if publication_fails:
                raise OSError("Controlled playback response failure")
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    if publication_fails:
        with pytest.raises(OSError, match="response failure"):
            rpc.publish_management_result(runtime, close_command, result)
        assert controls.mutation_hold(runtime) and not wakeups
        runtime._flush_pending_input()
        assert entry.admission is None and not runtime._rewrite_test_requests
        await work.settle_before_shutdown(runtime)
        assert runtime._native_playback is None
    else:
        rpc.publish_management_result(runtime, close_command, result)
        assert not controls.mutation_hold(runtime) and wakeups == [runtime._flush_pending_input]
        await settle_inputs(runtime, 1)
        assert entry.state == "settled" and len(runtime._rewrite_test_requests) == 1
        with pytest.raises(ValueError, match="stale"):
            await playback.dispatch(runtime, close_command)


@pytest.mark.parametrize("phase", ["review", "start"])
@pytest.mark.parametrize("owner", ["runtime", "model_hold", "model_error"])
async def test_playback_refuses_unpublished_sibling_mutations(armed, monkeypatch, phase, owner):
    from test_rust_runtime_controls import snapshot_review

    from litetui import native_runtime_controls as controls

    runtime, _ = armed
    runtime.settings.tts_enabled = True
    command = review_command(runtime)
    _, control_review = await snapshot_review(runtime, monkeypatch, "plan_mode", True)
    receipt = await playback.dispatch(runtime, command) if phase == "start" else None
    attempt = None
    if owner == "runtime":
        attempt = controls.reserve_command(runtime, {"type": controls.PREFIX + "apply",
            "id": work.ident(), "review": control_review})
    elif owner == "model_hold":
        runtime._native_model_lifecycle_hold = True
    else:
        runtime._backend_transition_error = "Controlled model transition needs shutdown"
    before = copy.deepcopy(runtime.conversation)
    with pytest.raises(ValueError, match="runtime control|owned model|model transition"):
        await playback.dispatch(runtime, command if receipt is None else follow(command, receipt, "start"))
    assert runtime.conversation == before
    if receipt is None:
        assert not playback.blocking(runtime)
    else:
        assert runtime._native_playback.task is None
        close_command = follow(command, receipt, "close")
        if owner == "model_error":
            # Fatal canonical owner state requires joined shutdown.
            with pytest.raises(ValueError, match="model transition"):
                await playback.dispatch(runtime, close_command)
            await work.settle_before_shutdown(runtime)
        else:
            # Exact Close remains available while a sibling holds admission.
            closed = await playback.dispatch(runtime, close_command)
            work.finish_publication(runtime, work.prepare_publication(runtime, close_command, closed))
        assert not playback.blocking(runtime)
    if attempt is not None:
        assert controls._state(runtime).attempt is attempt and not attempt.effect_attempted
        controls._state(runtime).attempt = None
    runtime._native_model_lifecycle_hold = False
    runtime._backend_transition_error = None
