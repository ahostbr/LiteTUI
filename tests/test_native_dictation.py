"""Private contained helpers exercise the source dictation contract without devices."""
from __future__ import annotations

import asyncio
import hashlib
import subprocess
import sys

import pytest
from test_rust_runtime_controls import app as _app
from test_rust_runtime_controls import armed as _armed
from test_rust_runtime_controls import switch_app as _switch_app

app, armed, switch_app = _app, _armed, _switch_app

from litetui import background_input as work
from litetui import native_dictation as voice

REAL_POPEN = subprocess.Popen

pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(sys.platform != "win32", reason="owned Windows helper")]


def command(app, action, previous=None):
    bridge = work.bridge(app)
    return {"type": voice.OPERATION, "id": work.ident(), "version": 1, **bridge.identity,
            "work_instance": bridge.instance, "action": action,
            "capture": previous["capture"] if previous else None,
            "draft_sha256": hashlib.sha256(b"Existing draft").hexdigest(),
            "text_sha256": previous["text_sha256"] if action == "apply" else None}


async def wait_result(app, result):
    for _ in range(300):
        result = await voice.dispatch(app, command(app, "status", result))
        if result["status"] in {"ready", "empty", "failed"}:
            return result
        await asyncio.sleep(0.025)
    raise AssertionError("Bounded private helper did not finish")


async def test_dictation_exact_capture_apply_stale_preferences_and_cleanup(armed, tmp_path, monkeypatch):
    runtime, _ = armed
    runtime.settings.stt_model = "base.en"
    runtime.settings.stt_mic = "Private fixture microphone"
    script = tmp_path / "device_free.py"
    script.write_text(
        "import pathlib,sys,time,json\n"
        "p=pathlib.Path(sys.argv[1]);(p/'recording').touch()\n"
        "while not (p/'finish').exists():time.sleep(0.02)\n"
        "(p/'transcribing').touch()\n"
        "(p/'result.json').write_text(json.dumps({'status':'ready','text':'Fixture words'}),encoding='utf8')\n",
        encoding="utf-8")
    expected = lambda directory: [sys.executable, "-B", str(script), str(directory)]
    monkeypatch.setattr(voice, "_command", expected)
    def start(command, **kwargs):
        assert command == expected(runtime._native_dictation.directory)
        return REAL_POPEN(command, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", start)
    before = list(runtime.conversation)
    review = await voice.dispatch(runtime, command(runtime, "review"))
    assert review["status"] == "review" and runtime._native_dictation.task is None
    runtime.settings.stt_mic = "Changed microphone"
    with pytest.raises(ValueError, match="preferences changed"):
        await voice.dispatch(runtime, command(runtime, "start", review))
    runtime.settings.stt_mic = "Private fixture microphone"
    recording = await voice.dispatch(runtime, command(runtime, "start", review))
    directory = runtime._native_dictation.directory
    with pytest.raises(ValueError, match="already started"):
        await voice.dispatch(runtime, command(runtime, "start", recording))
    await voice.dispatch(runtime, command(runtime, "finish", recording))
    result = await wait_result(runtime, recording)
    assert result["text"] == "Fixture words" and result["status"] == "ready", runtime._native_dictation.failure
    assert runtime._native_dictation.task.done() and runtime.conversation == before and not runtime._pending_input
    bad = command(runtime, "apply", result)
    bad["text_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="Review the completed"):
        await voice.dispatch(runtime, bad)
    apply_command = command(runtime, "apply", result)
    accepted = await voice.dispatch(runtime, apply_command)
    assert voice.blocking(runtime)
    work.finish_publication(runtime, work.prepare_publication(runtime, apply_command, accepted))
    assert accepted["status"] == "applied" and accepted["text"] == result["text"]
    assert runtime._native_dictation is None and not directory.exists() and runtime.conversation == before
    with pytest.raises(ValueError, match="stale"):
        await voice.dispatch(runtime, command(runtime, "apply", result))


async def test_dictation_cancel_joins_private_child_and_disposes_capture(armed, tmp_path, monkeypatch):
    runtime, _ = armed
    runtime.settings.stt_model = "base.en"
    child = tmp_path / "grandchild.py"
    marker, late = tmp_path / "started", tmp_path / "late-write"
    child.write_text("import pathlib,sys,time\n"
                     "pathlib.Path(sys.argv[1]).touch();time.sleep(2);pathlib.Path(sys.argv[2]).touch()\n",
                     encoding="utf-8")
    script = tmp_path / "waiting.py"
    script.write_text("import subprocess,sys,time\n"
                      f"subprocess.Popen([sys.executable,'-B',{str(child)!r},{str(marker)!r},{str(late)!r}])\n"
                      "time.sleep(15)\n", encoding="utf-8")
    expected = lambda directory: [sys.executable, "-B", str(script), str(directory)]
    monkeypatch.setattr(voice, "_command", expected)
    def start(command, **kwargs):
        assert command == expected(runtime._native_dictation.directory)
        return REAL_POPEN(command, **kwargs)
    monkeypatch.setattr(subprocess, "Popen", start)
    review = await voice.dispatch(runtime, command(runtime, "review"))
    await voice.dispatch(runtime, command(runtime, "start", review))
    capture = runtime._native_dictation
    for _ in range(200):
        if marker.exists():
            break
        assert not capture.task.done(), capture.failure
        await asyncio.sleep(0.025)
    assert marker.exists()
    cancel_command = command(runtime, "cancel", review)
    cancelled = await voice.dispatch(runtime, cancel_command)
    assert voice.blocking(runtime)
    work.finish_publication(runtime, work.prepare_publication(runtime, cancel_command, cancelled))
    assert cancelled["status"] == "cancelled" and cancelled["text"] == ""
    assert capture.task.done() and not capture.unsettled and not capture.directory.exists()
    assert runtime._native_dictation is None
    await asyncio.sleep(2.1)
    assert not late.exists()


async def test_review_and_start_refuse_foreign_binding_and_draft(armed):
    runtime, _ = armed
    review = await voice.dispatch(runtime, command(runtime, "review"))
    for field, value in [("work_instance", work.ident()), ("agent_id", work.ident()),
                         ("capture", work.ident()), ("draft_sha256", "0" * 64)]:
        changed = command(runtime, "start", review)
        changed[field] = value
        with pytest.raises(ValueError):
            await voice.dispatch(runtime, changed)
        assert runtime._native_dictation.task is None
    assert (await voice.dispatch(runtime, command(runtime, "status", review)))["status"] == "review"
    cancel_command = command(runtime, "cancel", review)
    cancelled = await voice.dispatch(runtime, cancel_command)
    work.finish_publication(runtime, work.prepare_publication(runtime, cancel_command, cancelled))
    assert runtime._native_dictation is None


@pytest.mark.parametrize("action", ["apply", "cancel"])
@pytest.mark.parametrize("publication_fails", [False, True])
async def test_dictation_holds_captured_work_until_exact_release_publication(armed, monkeypatch, action, publication_fails):
    from test_rust_background_input import settle_inputs

    from litetui import native_runtime_controls as controls
    from litetui import rpc
    from litetui.event_origin import EventOrigin

    runtime, _ = armed
    runtime.settings.stt_model = "base.en"
    from types import SimpleNamespace
    monkeypatch.setattr(runtime, "_user_bubble", lambda *_args, **_kwargs: SimpleNamespace(border_title="You"))
    monkeypatch.setattr(runtime, "_chat_running", lambda: any(w.group == "chat" and not w.is_finished for w in runtime.workers))
    wakeups = []
    def schedule(callback, *args):
        wakeups.append(callback)
        asyncio.get_running_loop().call_soon(callback, *args)
    monkeypatch.setattr(runtime, "call_later", schedule)
    finished = asyncio.Event()
    async def capture_result(capture):
        await finished.wait()
        capture.status, capture.transcript = "ready", "Reviewed fixture transcript"
    monkeypatch.setattr(voice, "_run", capture_result)
    review = await voice.dispatch(runtime, command(runtime, "review"))
    await voice.dispatch(runtime, command(runtime, "start", review))
    assert controls.mutation_hold(runtime)

    async def output():
        return "Captured background completion held during dictation"
    before = list(runtime.conversation)
    runtime._start_background("bash", {"command": "controlled coroutine only"}, output())
    for _ in range(300):
        if work.bridge(runtime).inputs:
            break
        await asyncio.sleep(0.01)
    assert len(work.bridge(runtime).inputs) == 1
    entry = next(iter(work.bridge(runtime).inputs.values()))
    await asyncio.sleep(0.05)
    assert entry.state == "queued" and entry.claim_task is None and entry.admission is None
    assert runtime._pending_input == [entry.item] and runtime.conversation == before
    runtime._flush_pending_input()
    with pytest.raises(ValueError, match="Dictation"):
        work._begin(runtime, entry, False)
    with pytest.raises(ValueError, match="Dictation"):
        work.before_stream(runtime, EventOrigin(session_id=runtime.convo_id, operation_id="private-dictation-hold", source="harness"))
    assert entry.claim_task is None and not runtime._rewrite_test_requests

    finished.set()
    await runtime._native_dictation.task
    ready = await voice.dispatch(runtime, command(runtime, "status", review))
    assert ready["status"] == "ready" and controls.mutation_hold(runtime)
    runtime._flush_pending_input()
    assert entry.state == "queued" and entry.admission is None
    release_command = command(runtime, action, ready)
    result = await voice.dispatch(runtime, release_command)
    assert controls.mutation_hold(runtime) and runtime._native_dictation.temporary is None
    assert not wakeups
    for changed_command, changed_result in (
        ({**release_command, "id": work.ident()}, result),
        (release_command, dict(result)),
        (release_command, {**result, "text": "substituted"}),
    ):
        with pytest.raises(ValueError, match="publication"):
            work.prepare_publication(runtime, changed_command, changed_result)
    emitted = []
    def emit(row):
        emitted.append(row)
        if row.get("ok"):
            assert controls.mutation_hold(runtime) and entry.admission is None
            assert entry.claim_task is None and runtime.conversation == before
            if publication_fails:
                raise OSError("Controlled release response write failure")
    monkeypatch.setattr(rpc, "rpc_emit", emit)
    if publication_fails:
        with pytest.raises(OSError, match="response write failure"):
            rpc.publish_management_result(runtime, release_command, result)
        assert controls.mutation_hold(runtime) and not wakeups
        runtime._flush_pending_input()
        with pytest.raises(ValueError, match="publication"):
            await voice.dispatch(runtime, command(runtime, "cancel", ready))
        rpc.publish_management_result(runtime, release_command, result)
        assert emitted[-1]["ok"] is False
        assert entry.admission is None and not runtime._rewrite_test_requests
        proof = await work.settle_before_shutdown(runtime)
        assert proof is not None and runtime._native_dictation is None
        assert entry.admission is None and not runtime._rewrite_test_requests
    else:
        rpc.publish_management_result(runtime, release_command, result)
        assert runtime._native_dictation is None and not controls.mutation_hold(runtime)
        assert wakeups == [runtime._flush_pending_input]
        await settle_inputs(runtime, 1)
        assert entry.state == "settled" and entry.admission is not None
        assert len(runtime._rewrite_test_requests) == 1
        rpc.publish_management_result(runtime, release_command, result)
        assert emitted[-1]["ok"] is False and len(runtime._rewrite_test_requests) == 1


async def test_release_ticket_rejects_another_task_and_mutated_result(armed):
    runtime, _ = armed
    review = await voice.dispatch(runtime, command(runtime, "review"))
    release_command = command(runtime, "cancel", review)
    result = await voice.dispatch(runtime, release_command)
    async def foreign():
        with pytest.raises(ValueError, match="publication"):
            work.prepare_publication(runtime, release_command, result)
    await asyncio.create_task(foreign())
    ticket = work.prepare_publication(runtime, release_command, result)
    result["text"] = "Mutated after publication preparation"
    with pytest.raises(ValueError, match="publication"):
        work.finish_publication(runtime, ticket)
    assert voice.blocking(runtime)
    await work.settle_before_shutdown(runtime)
    assert runtime._native_dictation is None


@pytest.mark.parametrize("stage", ["review", "start"])
async def test_dictation_waits_for_exact_runtime_control_readback(armed, monkeypatch, stage):
    from pathlib import Path

    from test_rust_runtime_controls import cmd as control_command
    from test_rust_runtime_controls import snapshot_review
    from test_rust_scheduled_publication import published_rpc

    from litetui import gui_rpc
    from litetui import native_runtime_controls as controls
    assert Path(voice.__file__).resolve() == Path(__file__).resolve().parents[1] / "src/litetui/native_dictation.py"
    runtime, _ = armed
    async def controlled_capture(capture):
        capture.status = "empty"
    monkeypatch.setattr(voice, "_run", controlled_capture)
    prior = await gui_rpc.async_dispatch(runtime, command(runtime, "review")) if stage == "start" else None
    _, review = await snapshot_review(runtime, monkeypatch, "tools_enabled", not runtime.tools_enabled)
    applied = await published_rpc(runtime, monkeypatch, {
        "type": controls.PREFIX + "apply", "id": "held-runtime-apply", "review": review})
    assert controls.mutation_hold(runtime)
    original_capture = getattr(runtime, "_native_dictation", None)
    try:
        with pytest.raises(ValueError, match="runtime control"):
            await gui_rpc.async_dispatch(runtime, command(runtime, stage, prior))
        assert getattr(runtime, "_native_dictation", None) is original_capture
        assert original_capture is None or original_capture.task is None
        await published_rpc(runtime, monkeypatch, control_command(runtime, "readback",
            action_id=review["action_id"], review_token=review["review_token"],
            apply_operation_id=applied["apply_operation_id"], readback_token=applied["readback_token"],
            result_sha256=work.digest(work.encoded(applied))))
        assert not controls.mutation_hold(runtime)
        accepted = await gui_rpc.async_dispatch(runtime, command(runtime, stage, prior))
        assert accepted["status"] == ("review" if stage == "review" else "starting")
        if stage == "start":
            assert voice.blocking(runtime)
            await runtime._native_dictation.task
            status = await gui_rpc.async_dispatch(runtime, command(runtime, "status", accepted))
            assert status["status"] == "empty"
        cancel = command(runtime, "cancel", accepted)
        result = await gui_rpc.async_dispatch(runtime, cancel)
        assert voice.blocking(runtime)
        work.finish_publication(runtime, work.prepare_publication(runtime, cancel, result))
        assert runtime._native_dictation is None
    finally:
        await voice.retire(runtime)


@pytest.mark.parametrize("transcript", ["word\u0080word", "word\u009fword"])
async def test_c1_transcript_controls_become_typed_failure(armed, monkeypatch, transcript):
    from pathlib import Path

    from litetui import gui_rpc

    runtime, _ = armed
    def completed_helper(_command, **kwargs):
        (Path(kwargs["cwd"]) / "result.json").write_bytes(
            work.encoded({"status": "ready", "text": transcript}))
        return 0
    monkeypatch.setattr(voice.ttyguard, "run_owned_writer", completed_helper)
    review = await gui_rpc.async_dispatch(runtime, command(runtime, "review"))
    started = await gui_rpc.async_dispatch(runtime, command(runtime, "start", review))
    await runtime._native_dictation.task
    result = await gui_rpc.async_dispatch(runtime, command(runtime, "status", started))
    assert result["status"] == "failed" and result["text"] == "" and result["text_sha256"] is None
    assert runtime._native_dictation.failure == "Invalid transcript"
    cancel = command(runtime, "cancel", result)
    released = await gui_rpc.async_dispatch(runtime, cancel)
    work.finish_publication(runtime, work.prepare_publication(runtime, cancel, released))
    assert runtime._native_dictation is None


async def pin_capture_directory(capture):
    """A contained, device-free test process pins only this private cwd.

    The product's drained job cannot own this holder: it simulates the directory
    reference that can outlive the helper process. The test always joins it.
    """
    marker = capture.directory / "pin-ready"
    holder = REAL_POPEN([sys.executable, "-B", "-c",
        "import pathlib,sys,time;pathlib.Path(sys.argv[1]).touch();time.sleep(30)",
        str(marker)], cwd=capture.directory, creationflags=subprocess.CREATE_NO_WINDOW)
    def release():
        if holder.poll() is None:
            holder.kill()
        holder.wait(timeout=5)
    try:
        for _ in range(200):
            if marker.exists():
                return release
            assert holder.poll() is None, "Private directory holder exited before readiness"
            await asyncio.sleep(0.025)
        raise AssertionError("Private directory holder did not become ready")
    except BaseException:
        release()
        raise


async def ready_capture(runtime, monkeypatch):
    async def private_result(capture):
        capture.status, capture.transcript = "ready", "Private completed dictation"
    monkeypatch.setattr(voice, "_run", private_result)
    review = await voice.dispatch(runtime, command(runtime, "review"))
    await voice.dispatch(runtime, command(runtime, "start", review))
    await runtime._native_dictation.task
    return await voice.dispatch(runtime, command(runtime, "status", review))


@pytest.mark.parametrize("action", ["apply", "cancel", "shutdown"])
async def test_dictation_outlasts_a_late_helper_directory_release(armed, monkeypatch, action):
    runtime, _ = armed
    result = await ready_capture(runtime, monkeypatch)
    capture = runtime._native_dictation
    temporary = capture.temporary
    release_pin = await pin_capture_directory(capture)
    # Establish the actual failure of the old one-shot cleanup before exercising
    # the public release path. A second TemporaryDirectory.cleanup is supported.
    with pytest.raises(PermissionError) as original:
        temporary.cleanup()
    assert original.value.winerror == 32
    released = asyncio.Event()
    async def late_release():
        await asyncio.sleep(0.15)
        assert runtime._native_dictation is capture and capture.temporary is temporary
        assert voice.blocking(runtime) and capture.release_result is None
        release_pin()
        released.set()
    holder = asyncio.create_task(late_release())
    try:
        if action == "shutdown":
            assert await work.settle_before_shutdown(runtime) is not None
            assert runtime._native_dictation is None
        else:
            request = command(runtime, action, result)
            accepted = await voice.dispatch(runtime, request)
            assert accepted["status"] == ("applied" if action == "apply" else "cancelled")
            assert runtime._native_dictation is capture and voice.blocking(runtime)
            work.finish_publication(runtime, work.prepare_publication(runtime, request, accepted))
            assert runtime._native_dictation is None
        assert released.is_set() and capture.task.done() and not capture.unsettled
        assert capture.temporary is None and not capture.directory.exists()
        await voice.retire(runtime)  # Already retired is idempotent.
    finally:
        release_pin()
        await holder


async def test_dictation_retains_failed_cleanup_until_joined_shutdown_retry(armed, monkeypatch):
    runtime, _ = armed
    result = await ready_capture(runtime, monkeypatch)
    capture = runtime._native_dictation
    temporary = capture.temporary
    release_pin = await pin_capture_directory(capture)
    monkeypatch.setattr(voice, "DIRECTORY_RELEASE_WAIT_S", 0.06)
    try:
        with pytest.raises(PermissionError) as refused:
            await voice.dispatch(runtime, command(runtime, "cancel", result))
        assert refused.value.winerror == 32
        assert capture.task.done() and not capture.unsettled
        assert runtime._native_dictation is capture and capture.temporary is temporary
        assert capture.directory.exists() and voice.blocking(runtime)
        assert capture.release_result is None and capture.publication is None
    finally:
        release_pin()
    assert await work.settle_before_shutdown(runtime) is not None
    assert runtime._native_dictation is None and capture.temporary is None
    assert not capture.directory.exists()


async def test_dictation_cleanup_does_not_retry_unrelated_permission_errors(monkeypatch):
    from types import SimpleNamespace

    attempts = []
    denied = PermissionError(13, "controlled access denial")
    denied.winerror = 5
    def cleanup():
        attempts.append(True)
        raise denied
    with pytest.raises(PermissionError) as failure:
        await voice._remove_directory(SimpleNamespace(cleanup=cleanup))
    assert failure.value is denied and attempts == [True]
