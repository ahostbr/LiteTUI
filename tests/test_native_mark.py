"""Mark acceptance uses private synthetic pixels and contained Python helpers only."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import subprocess
import sys

import pytest
from PIL import Image
from test_rust_runtime_controls import app as _app
from test_rust_runtime_controls import armed as _armed
from test_rust_runtime_controls import switch_app as _switch_app

from litetui import background_input as work
from litetui import command_surface, gui_rpc, native_image
from litetui import native_mark as mark
from litetui import native_runtime_controls as controls

app, armed, switch_app = _app, _armed, _switch_app
REAL_POPEN = subprocess.Popen
pytestmark = [pytest.mark.asyncio, pytest.mark.skipif(sys.platform != "win32", reason="owned Windows helper")]


def png(width=80, height=40):
    stream = io.BytesIO()
    Image.new("RGB", (width, height), (30, 180, 220)).save(stream, format="PNG")
    return stream.getvalue()


def command(app, action, previous=None, draft="Keep this draft", **extra):
    bridge = work.bridge(app)
    return {"type": mark.OPERATION, "id": work.ident(), "version": 1, **bridge.identity,
            "work_instance": bridge.instance, "action": action,
            "capture": previous["capture"] if previous else None,
            "draft": draft if action == "review" else None, "draft_sha256": mark.digest(draft),
            "previous": previous["previous"] if previous else mark._prior(bridge),
            "result_sha256": previous["result_sha256"] if action == "apply" else None, **extra}


def helper(app, tmp_path, monkeypatch, *, override=None, slow=False, width=80, height=40):
    script = tmp_path / "controlled_marker.py"
    data = base64.b64encode(png(width, height)).decode("ascii")
    script.write_text(
        "import base64,json,pathlib,sys,time,subprocess\np=pathlib.Path(sys.argv[1])\n"
        + "(p/'started').touch()\n"
        + ("child=subprocess.Popen([sys.executable,'-B','-c','import time;time.sleep(30)']);(p/'child.pid').write_text(str(child.pid));time.sleep(30)\n" if slow else "")
        + f"(p/'mark.png').write_bytes(base64.b64decode({data!r}))\n"
        + "v={'x':-1900,'y':25,'mon':1,'mon_x':20,'mon_y':25,'png':str(p/'mark.png'),'text':'Look at this Δ\\r\\nsecond line'}\n"
        + f"v.update({(override or {})!r})\n"
        + "(p/'mark.json').write_text(json.dumps(v),encoding='utf-8-sig')\n",
        encoding="utf-8",
    )
    marker = tmp_path / "reviewed-marker.ps1"
    marker.write_text("Private test marker, never executed", encoding="utf-8")
    monkeypatch.setattr(mark.plugin, "MARK_SCRIPT", marker)
    monkeypatch.setattr(mark, "_launcher", lambda: sys.executable)
    expected = lambda capture: [sys.executable, "-B", str(script), str(capture.directory)]
    monkeypatch.setattr(mark, "_command", expected)

    def launch(argv, **kwargs):
        assert argv == expected(app._native_mark)
        assert kwargs["stdout"] == kwargs["stderr"] == subprocess.DEVNULL
        return REAL_POPEN(argv, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    return marker


async def wait(app, result, draft="Keep this draft"):
    for _ in range(600):
        result = await gui_rpc.async_dispatch(app, command(app, "status", result, draft))
        if result["status"] != "running":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Controlled marker did not finish")


async def close(app, result, draft="Keep this draft"):
    cmd = command(app, "close", result, draft)
    result = await gui_rpc.async_dispatch(app, cmd)
    assert mark.blocking(app)
    work.finish_publication(app, work.prepare_publication(app, cmd, result))
    assert not mark.blocking(app)
    return result


async def prepare(app, tmp_path, monkeypatch, **kwargs):
    helper(app, tmp_path, monkeypatch, **kwargs)
    result = await gui_rpc.async_dispatch(app, command(app, "review"))
    assert result["status"] == "review" and not result["applied"]
    assert app._native_mark.directory is None
    assert controls.mutation_hold(app) and work.prompt_refusal(app)
    result = await gui_rpc.async_dispatch(app, command(app, "start", result))
    return await wait(app, result)


async def test_two_reviews_note_and_coordinates_apply_only_unsent_image(armed, tmp_path, monkeypatch):
    app, _ = armed
    bridge = work.bridge(app)
    before = app.convo_path.read_bytes(), list(app.conversation), list(app._pending_input)
    result = await prepare(app, tmp_path, monkeypatch)
    capture = app._native_mark
    assert result["status"] == "ready"
    assert mark._prior(bridge) is None
    assert result["result"]["note"] == "Look at this Δ\nsecond line"
    assert result["result"]["coordinates"]["x"] == -1900
    assert "My note: Look at this Δ second line" in result["result"]["message"]
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(app, command(app, "apply", result, result_sha256="0" * 64))
    applied = await gui_rpc.async_dispatch(app, command(app, "apply", result))
    assert applied["applied"] and bridge.composer_image == capture.candidate
    closed = await close(app, applied)
    assert closed["status"] == "closed" and closed["applied"]
    assert capture.task.done() and not capture.directory.exists()
    assert (app.convo_path.read_bytes(), list(app.conversation), list(app._pending_input)) == before


@pytest.mark.parametrize("phase", ["review", "ready"])
async def test_cancel_preserves_previous_image_and_clipboard_candidate_byte_for_byte(armed, tmp_path, monkeypatch, phase):
    app, _ = armed
    bridge = work.bridge(app)
    path = tmp_path / "prior.png"
    path.write_bytes(png())
    before_review = native_image.prepare(app, {"type": native_image.OPERATION, "id": work.ident(), "version": 1,
        **bridge.identity, "work_instance": bridge.instance, "path": str(path), "clear": None})
    prior = bridge.composer_image
    clipboard = (dict(before_review, token="f" * 64), prior[1])
    bridge.clipboard_candidate = clipboard
    helper(app, tmp_path, monkeypatch)
    result = await gui_rpc.async_dispatch(app, command(app, "review"))
    assert result["previous"] == before_review
    if phase == "ready":
        result = await wait(app, await gui_rpc.async_dispatch(app, command(app, "start", result)))
    await close(app, result)
    assert bridge.composer_image is prior and bridge.clipboard_candidate is clipboard


@pytest.mark.parametrize("bad", [
    {"png": "C:\\outside.png"}, {"mon_x": 80}, {"mon_y": -1}, {"x": True},
    {"x": 2**31}, {"mon": -1}, {"mon": 256}, {"text": "\x1b[2J"},
    {"text": "\u202ereversed"}, {"text": "x" * 4097}, {"extra": "ignored"},
])
async def test_invalid_handoff_never_replaces_the_composer(armed, tmp_path, monkeypatch, bad):
    app, _ = armed
    result = await prepare(app, tmp_path, monkeypatch, override=bad)
    assert result["status"] == "failed" and result["result"] is None
    assert mark._prior(work.bridge(app)) is None
    await close(app, result)


async def test_script_change_prevents_launch_and_full_prompt_budget_is_checked(armed, tmp_path, monkeypatch):
    app, _ = armed
    marker = helper(app, tmp_path, monkeypatch)
    review = await gui_rpc.async_dispatch(app, command(app, "review"))
    marker.write_text("Changed after review", encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        await gui_rpc.async_dispatch(app, command(app, "start", review))
    assert app._native_mark.task is None
    await close(app, review)
    ready = await prepare(app, tmp_path, monkeypatch)
    seen = []
    def budget(_app, encoded, text):
        seen.append((encoded, text))
        raise ValueError("Controlled whole composer budget refusal")
    monkeypatch.setattr(native_image, "check_inline_budget", budget)
    with pytest.raises(ValueError, match="whole composer"):
        await gui_rpc.async_dispatch(app, command(app, "apply", ready))
    assert seen[0][1] == "Keep this draft " + ready["result"]["message"]
    assert mark._prior(work.bridge(app)) is None
    await close(app, ready)


async def test_scaled_whole_monitor_keeps_original_bounds_and_scaled_marker_text(armed, tmp_path, monkeypatch):
    app, _ = armed
    ready = await prepare(app, tmp_path, monkeypatch, width=2000, height=1000, override={"mon_x": 1900, "mon_y": 900})
    result = ready["result"]
    assert result["image"]["width"] == 2000 and result["image"]["sent_width"] == 1536
    assert result["coordinates"]["mon_x"] == 1900
    assert "(1459,691)" in result["message"]
    await close(app, ready)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_cancel_or_quit_joins_contained_helper_tree(armed, tmp_path, monkeypatch, shutdown):
    app, _ = armed
    helper(app, tmp_path, monkeypatch, slow=True)
    review = await gui_rpc.async_dispatch(app, command(app, "review"))
    running = await gui_rpc.async_dispatch(app, command(app, "start", review))
    capture = app._native_mark
    for _ in range(500):
        if (capture.directory / "child.pid").exists():
            break
        await asyncio.sleep(0.01)
    assert (capture.directory / "child.pid").exists()
    if shutdown:
        await work.settle_before_shutdown(app)
        assert not mark.blocking(app)
    else:
        await close(app, running)
    assert capture.task.done() and not capture.unsettled and not capture.directory.exists()


async def test_failed_retirement_retains_owner_and_publication_is_exact(armed, tmp_path, monkeypatch):
    app, _ = armed
    ready = await prepare(app, tmp_path, monkeypatch)
    capture = app._native_mark
    capture.unsettled = True
    with pytest.raises(RuntimeError, match="retirement"):
        await gui_rpc.async_dispatch(app, command(app, "close", ready))
    assert app._native_mark is capture and capture.directory.exists()
    capture.unsettled = False
    await work.settle_before_shutdown(app)
    assert not capture.directory.exists()


async def test_close_publication_failure_keeps_admission_hold_until_quit(armed, tmp_path, monkeypatch):
    app, _ = armed
    ready = await prepare(app, tmp_path, monkeypatch)
    cmd = command(app, "close", ready)
    result = await gui_rpc.async_dispatch(app, cmd)
    ticket = work.prepare_publication(app, cmd, result)
    ticket.result["status"] = "changed"
    with pytest.raises(ValueError, match="publication"):
        work.finish_publication(app, ticket)
    assert mark.blocking(app) and work.prompt_refusal(app)
    await work.settle_before_shutdown(app)
    assert not mark.blocking(app)


async def test_mark_route_is_registered_bounded_and_never_executes_plugin(armed, monkeypatch):
    app, _ = armed
    monkeypatch.setattr(mark.plugin, "start_mark", lambda _app: pytest.fail("legacy overlay must not run"))
    catalog = command_surface.catalog(app, {"session_id": app.convo_id})
    row = next(row for row in catalog["entries"] if row["tokens"] == ["/mark"])
    assert row["native_action"] == "mark" and not row["accepts_arguments"]
    base = {"session_id": app.convo_id, "version": 1, "agent_id": catalog["agent_id"],
            "snapshot": catalog["snapshot"], "command": "/mark"}
    assert command_surface.resolve(app, base)["native_action"] == "mark"
    with pytest.raises(ValueError, match="no arguments"):
        command_surface.resolve(app, {**base, "command": "/mark unexpected"})


async def test_hard_link_is_rejected_before_image_decode(tmp_path):
    original = tmp_path / "original.png"
    original.write_bytes(png())
    linked = tmp_path / "linked.png"
    os.link(original, linked)
    with pytest.raises(ValueError, match="unlinked"):
        mark.read(linked, native_image.MAX_FILE)

async def test_registered_owner_replacement_and_stale_draft_refuse_apply(armed, tmp_path, monkeypatch):
    from dataclasses import replace

    app, _ = armed
    ready = await prepare(app, tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="stale"):
        await gui_rpc.async_dispatch(app, command(app, "apply", ready, draft_sha256="0" * 64))
    entry = app.plugins.commands["/mark"]
    # Even an equivalent new registration is not the original reviewed owner.
    app.plugins.commands["/mark"] = replace(entry)
    with pytest.raises(ValueError, match="changed"):
        await gui_rpc.async_dispatch(app, command(app, "apply", ready))
    assert mark._prior(work.bridge(app)) is None
    await close(app, ready)


async def test_full_composer_can_review_capture_but_apply_refuses_overflow(armed, tmp_path, monkeypatch):
    app, _ = armed
    helper(app, tmp_path, monkeypatch)
    draft = "x" * mark.MAX_DRAFT
    review = await gui_rpc.async_dispatch(app, command(app, "review", draft=draft))
    running = await gui_rpc.async_dispatch(app, command(app, "start", review, draft))
    ready = await wait(app, running, draft)
    assert ready["status"] == "ready"
    with pytest.raises(ValueError, match="complete marked composer"):
        await gui_rpc.async_dispatch(app, command(app, "apply", ready, draft))
    assert mark._prior(work.bridge(app)) is None
    await close(app, ready, draft)


async def test_system_executable_hardlink_and_extended_local_path_are_stamped(tmp_path):
    original = tmp_path / "system.exe"
    original.write_bytes(b"private executable fixture; never run")
    linked = tmp_path / "system-copy.exe"
    os.link(original, linked)
    assert mark._file(linked, 1024, system_executable=True)["sha256"]
    with pytest.raises(ValueError, match="unlinked"):
        mark._file(linked, 1024)
    extended = "\\\\?\\" + str(linked)
    assert mark._file(extended, 1024, system_executable=True)["sha256"] == mark._file(linked, 1024, system_executable=True)["sha256"]


async def test_timeout_joins_owned_helper_before_returning_failed_status(armed, tmp_path, monkeypatch):
    app, _ = armed
    helper(app, tmp_path, monkeypatch, slow=True)
    monkeypatch.setattr(mark, "SECONDS", 0.5)
    review = await gui_rpc.async_dispatch(app, command(app, "review"))
    running = await gui_rpc.async_dispatch(app, command(app, "start", review))
    result = await wait(app, running)
    capture = app._native_mark
    assert result["status"] == "failed" and capture.task.done() and not capture.unsettled
    await close(app, result)
    assert not capture.directory.exists()
