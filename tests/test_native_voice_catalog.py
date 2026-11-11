"""Explicit discovery uses private contained fixture helpers, never devices."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from contextlib import ExitStack

import pytest
from test_rust_runtime_controls import app as _app
from test_rust_runtime_controls import armed as _armed
from test_rust_runtime_controls import switch_app as _switch_app
from test_rust_voice_preferences import block_audio

from litetui import background_input as work
from litetui import gui_rpc, rpc
from litetui import native_runtime_controls as controls
from litetui import native_voice_catalog as catalog
from litetui import native_voice_catalog_worker as worker
from litetui import native_voice_preferences as preferences

app, armed, switch_app = _app, _armed, _switch_app
REAL_POPEN = subprocess.Popen
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform != "win32", reason="owned Windows helper"),
]


def command(owner, action, previous=None, field="stt_mic"):
    bridge = work.bridge(owner)
    revision = (
        previous["before"]
        if previous
        else preferences._snapshot(
            owner, bridge.identity | {"work_instance": bridge.instance}
        )[0]["revision"]
    )
    return {
        "type": catalog.OPERATION,
        "id": work.ident(),
        "version": 1,
        **bridge.identity,
        "work_instance": bridge.instance,
        "action": action,
        "catalog": previous["catalog"] if previous else None,
        "field": previous["field"] if previous else field,
        "revision": revision,
    }


def helper(owner, tmp_path, monkeypatch, code):
    script = tmp_path / "private_catalog.py"
    script.write_text(
        "import json,pathlib,sys,time,subprocess\np=pathlib.Path(sys.argv[1])\n" + code,
        encoding="utf8",
    )

    def expected(directory):
        return [sys.executable, "-B", str(script), str(directory)]

    monkeypatch.setattr(catalog, "_command", expected)

    def launch(cmd, **kw):
        assert cmd == expected(owner._native_voice_catalog.directory)
        assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
        return REAL_POPEN(cmd, **kw)

    monkeypatch.setattr(subprocess, "Popen", launch)


async def wait(owner, result):
    for _ in range(300):
        result = await gui_rpc.async_dispatch(owner, command(owner, "status", result))
        if result["status"] != "starting":
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("Private catalog helper did not complete")


async def close(owner, result):
    cmd = command(owner, "close", result)
    result = await gui_rpc.async_dispatch(owner, cmd)
    ticket = work.prepare_publication(owner, cmd, result)
    assert controls.mutation_hold(owner)
    work.finish_publication(owner, ticket)
    assert not catalog.blocking(owner)
    return result


@pytest.mark.parametrize("field", ["stt_mic", "tts_voice"])
async def test_explicit_discovery_names_only_then_exact_close_publication(
    armed, tmp_path, monkeypatch, field
):
    owner, _ = armed
    helper(
        owner,
        tmp_path,
        monkeypatch,
        "(p/'result.json').write_text(json.dumps({'items':['Fixture Δ','Fixture Ω']}),encoding='utf8')\n",
    )
    path = owner._agent_session.store.data_root / "settings.json"
    before = path.read_bytes(), list(owner.conversation)
    with ExitStack() as guard:
        block_audio(guard)
        snapshot = preferences._snapshot(
            owner, work.identity(owner) | {"work_instance": work.bridge(owner).instance}
        )[0]
        assert not catalog.blocking(owner)
        initial = await gui_rpc.async_dispatch(
            owner, command(owner, "start", field=field)
        )
        assert (
            initial["status"] == "starting"
            and initial["before"] == snapshot["revision"]
        )
        ready = await wait(owner, initial)
        assert ready["status"] == "ready" and ready["items"] == [
            "Fixture Δ",
            "Fixture Ω",
        ]
        hold = owner._native_voice_catalog
        assert controls.mutation_hold(owner)
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(owner, command(owner, "start"))
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(
                owner, {"type": preferences.PREFIX + "preferences"}
            )
        for changed_field, value in [
            ("catalog", work.ident()),
            ("revision", "0" * 64),
            ("field", "tts_timeout"),
            ("work_instance", work.ident()),
        ]:
            with pytest.raises(ValueError):
                await gui_rpc.async_dispatch(
                    owner, command(owner, "status", ready) | {changed_field: value}
                )
        cmd = command(owner, "close", ready)
        result = await gui_rpc.async_dispatch(owner, cmd)
        assert (
            hold.task.done()
            and not hold.directory.exists()
            and controls.mutation_hold(owner)
        )
        for changed_cmd, changed_result in [
            (cmd | {"id": work.ident()}, result),
            (cmd, dict(result)),
        ]:
            with pytest.raises(ValueError):
                work.prepare_publication(owner, changed_cmd, changed_result)
        ticket = work.prepare_publication(owner, cmd, result)

        async def foreign():
            work.finish_publication(owner, ticket)

        with pytest.raises(ValueError):
            await asyncio.create_task(foreign())
        assert controls.mutation_hold(owner)
        work.finish_publication(owner, ticket)
        assert not controls.mutation_hold(owner)
        with pytest.raises(ValueError):
            work.finish_publication(owner, ticket)
    assert (path.read_bytes(), list(owner.conversation)) == before


@pytest.mark.parametrize(
    "items",
    [[], ["duplicate", "duplicate"], ["bad\nname"], ["x" * 513], ["x"] * 65, [False]],
)
async def test_empty_and_malformed_private_inventory_is_bounded(
    armed, tmp_path, monkeypatch, items
):
    owner, _ = armed
    helper(
        owner,
        tmp_path,
        monkeypatch,
        f"(p/'result.json').write_text({json.dumps({'items': items})!r},encoding='utf8')\n",
    )
    result = await wait(
        owner, await gui_rpc.async_dispatch(owner, command(owner, "start"))
    )
    assert result["status"] == ("ready" if items == [] else "failed")
    assert result["items"] == []
    await close(owner, result)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_close_and_quit_join_child_and_grandchild(
    armed, tmp_path, monkeypatch, shutdown
):
    owner, _ = armed
    marker, late = tmp_path / "started", tmp_path / "late"
    child = tmp_path / "grandchild.py"
    child.write_text(
        f"from pathlib import Path\nimport time\nPath({str(marker)!r}).touch()\ntime.sleep(1.5)\nPath({str(late)!r}).touch()\n",
        encoding="utf8",
    )
    helper(
        owner,
        tmp_path,
        monkeypatch,
        f"subprocess.Popen([sys.executable,'-B',{str(child)!r}])\ntime.sleep(15)\n",
    )
    result = await gui_rpc.async_dispatch(owner, command(owner, "start"))
    hold = owner._native_voice_catalog
    for _ in range(300):
        if marker.exists():
            break
        await asyncio.sleep(0.01)
    assert marker.exists()
    if shutdown:
        assert await work.settle_before_shutdown(owner) is not None
    else:
        await close(owner, result)
    assert hold.task.done() and not hold.unsettled and not hold.directory.exists()
    assert not catalog.blocking(owner)
    await asyncio.sleep(1.6)
    assert not late.exists()


async def test_timeout_and_lost_close_publication_retain_owner_until_quit(
    armed, tmp_path, monkeypatch
):
    owner, _ = armed
    helper(owner, tmp_path, monkeypatch, "time.sleep(15)\n")
    monkeypatch.setattr(catalog, "TIMEOUT", 0.1)
    result = await wait(
        owner, await gui_rpc.async_dispatch(owner, command(owner, "start"))
    )
    assert result["status"] == "failed"
    hold = owner._native_voice_catalog
    cmd = command(owner, "close", result)
    closed = await gui_rpc.async_dispatch(owner, cmd)

    def emit(_):
        assert controls.mutation_hold(owner)
        if _.get("ok"):
            raise OSError("Private publication failure")

    monkeypatch.setattr(rpc, "rpc_emit", emit)
    with pytest.raises(OSError):
        rpc.publish_management_result(owner, cmd, closed)
    assert controls.mutation_hold(owner) and hold.task.done()
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(owner, command(owner, "close", result))
    assert await work.settle_before_shutdown(owner) is not None
    assert not catalog.blocking(owner)


@pytest.mark.parametrize(
    "field,module,function",
    [
        ("stt_mic", "stt_backend", "list_mics"),
        ("tts_voice", "voice_backend", "list_sapi_voices"),
    ],
)
async def test_worker_routes_catalog_without_audio(
    tmp_path, monkeypatch, field, module, function
):
    from importlib import import_module

    source = import_module("litetui." + module)
    monkeypatch.setattr(source, function, lambda: ["Fixture Δ", "Fixture Δ", "Second"])
    (tmp_path / "parameters.json").write_text(
        json.dumps({"field": field}), encoding="utf8"
    )
    worker.run(tmp_path)
    assert json.loads((tmp_path / "result.json").read_bytes()) == {
        "items": ["Fixture Δ", "Second"]
    }


async def test_probe_bounds_stderr_and_joins_reader(tmp_path, monkeypatch):
    script = tmp_path / "probe.py"
    script.write_text("import sys\nsys.stderr.write('x'*70000)\n", encoding="utf8")
    expected = [sys.executable, "-B", str(script)]

    def launch(cmd, **kw):
        assert cmd == expected
        return REAL_POPEN(cmd, **kw)

    monkeypatch.setattr(subprocess, "Popen", launch)
    with pytest.raises(ValueError, match="output exceeds"):
        worker.bounded_probe(expected, 2)
    import threading

    assert not any(t.name == "voice-catalog-stderr" for t in threading.enumerate())


async def test_start_rejects_stale_override_and_pending_work_before_launch(
    armed, monkeypatch
):
    owner, _ = armed

    def forbidden(_):
        raise AssertionError("Rejected discovery launched a helper")

    monkeypatch.setattr(catalog, "_command", forbidden)
    original = command(owner, "start")
    for updates in (
        {"revision": "0" * 64},
        {"catalog": work.ident()},
        {"field": "tts_enabled"},
        {"agent_id": work.ident()},
    ):
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(owner, original | updates)
        assert not catalog.blocking(owner)
    monkeypatch.setattr(owner, "_launch_overrides", {"stt_mic": "Pinned microphone"})
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(owner, command(owner, "start"))
    assert not catalog.blocking(owner)
    monkeypatch.setattr(owner, "_launch_overrides", {})
    owner._pending_input.append(object())
    try:
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(owner, command(owner, "start"))
        assert not catalog.blocking(owner)
    finally:
        owner._pending_input.clear()
