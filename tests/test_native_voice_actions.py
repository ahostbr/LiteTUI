"""All Voice action boundaries use private fake processes or explicit pure stubs."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pytest
from test_rust_runtime_controls import app as _app
from test_rust_runtime_controls import armed as _armed
from test_rust_runtime_controls import switch_app as _switch_app

from litetui import background_input as work
from litetui import gui_rpc, rpc
from litetui import native_runtime_controls as controls
from litetui import native_voice_actions as actions
from litetui import native_voice_actions_worker as worker
from litetui import native_voice_preferences as preferences

app, armed, switch_app = _app, _armed, _switch_app
REAL_POPEN = subprocess.Popen
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(sys.platform != "win32", reason="owned Windows helpers"),
]


def params():
    return {
        "engine": "pyttsx3",
        "voice": "Private voice",
        "timeout": 30,
        "model": "base.en",
    }


def plan(kind, tmp_path, preferences=None, install=False):
    preferences = preferences or params()
    exe = sys.executable
    argv = (
        [
            exe,
            "-E",
            "-B",
            "-m",
            "pip",
            "install",
            "--prefix",
            str(tmp_path),
            *worker.PACKAGES[kind],
        ]
        if install
        else []
    )
    return {
        "version": 1,
        "action": kind,
        "interpreter": exe,
        "launcher": exe,
        "install": install,
        "argv": argv,
        "packages": worker.PACKAGES[kind],
        "prefix": str(tmp_path),
        "cache": str(tmp_path / "cache") if kind == "stt" else None,
        "model": preferences["model"] if kind == "stt" else None,
        "engine": preferences["engine"] if kind == "fallback" else None,
        "voice": preferences["voice"] if kind == "fallback" else None,
        "timeout": preferences["timeout"]
        if kind == "fallback"
        else 240
        if kind == "edge"
        else 600,
        "phrase": worker.PHRASE if kind == "fallback" else None,
        "fingerprints": {exe: "1" * 64},
        "modules": worker.MODULES[preferences["engine"]]
        if kind == "fallback"
        else worker.MODULES[kind],
        "network": kind != "fallback" or preferences["engine"] == "edge",
    }


def command(owner, action, previous=None, kind="fallback"):
    bridge = work.bridge(owner)
    revision = (
        previous["before"]
        if previous
        else preferences._snapshot(
            owner, work.identity(owner) | {"work_instance": bridge.instance}
        )[0]["revision"]
    )
    return {
        "type": actions.OPERATION,
        "values": getattr(
            getattr(owner, "_native_voice_action", None), "selection", None
        )
        or {},
        "id": work.ident(),
        "version": 1,
        **bridge.identity,
        "work_instance": bridge.instance,
        "action": action,
        "run": previous["run"] if previous else None,
        "task": previous["task"] if previous else kind,
        "revision": revision,
        "plan_sha256": previous["plan_sha256"] if previous else None,
    }


def helper(owner, tmp_path, monkeypatch, *, slow=False):
    script = tmp_path / "fake_voice_action.py"
    script.write_text(
        "import json,pathlib,sys,time,subprocess\np=pathlib.Path(sys.argv[1]);v=json.loads((p/'parameters.json').read_text(encoding='utf8'))\n"
        + "if v['phase']=='review':\n"
        + "    result={'plan':json.loads((p.parent/'private-plan.json').read_text(encoding='utf8'))}\n"
        + "else:\n"
        + ("    (p/'applying').touch();time.sleep(15)\n" if slow else "")
        + "    plan=v['plan'];result={'result':{'kind':{'fallback':'fallback_finished','edge':'edge_ready','stt':'stt_cache_ready'}[v['action']],'interpreter':plan['interpreter'],'cache':plan['cache'],'model':plan['model'],'dependencies_verified':True}}\n"
        + "(p/'result.json').write_text(json.dumps(result),encoding='utf8')\n",
        encoding="utf8",
    )
    expected = lambda directory: [sys.executable, "-B", str(script), str(directory)]
    monkeypatch.setattr(actions, "_command", expected)

    def launch(argv, **kw):
        assert argv == expected(owner._native_voice_action.directory)
        assert kw["stdout"] == kw["stderr"] == subprocess.DEVNULL
        return REAL_POPEN(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", launch)


async def wait(owner, result, status):
    for _ in range(500):
        result = await gui_rpc.async_dispatch(owner, command(owner, "status", result))
        if result["status"] == status:
            return result
        assert result["status"] not in ("failed", "cancelled"), result
        await asyncio.sleep(0.01)
    raise AssertionError("Private helper did not complete")


async def prepare(owner, tmp_path, monkeypatch, kind="fallback", slow=False):
    helper(owner, tmp_path, monkeypatch, slow=slow)
    snapshot = preferences._snapshot(
        owner, work.identity(owner) | {"work_instance": work.bridge(owner).instance}
    )[0]
    selected = actions._preferences(snapshot, kind)
    directory = owner._agent_session.store.data_root / "recordings"
    directory.mkdir(exist_ok=True)
    worker.write(
        directory / "private-plan.json",
        plan(kind, tmp_path, selected, install=kind != "fallback"),
    )
    result = await gui_rpc.async_dispatch(owner, command(owner, "review", kind=kind))
    assert result["status"] == "preparing" and not result["applied"]
    return await wait(owner, result, "review")


async def close(owner, result):
    cmd = command(owner, "close", result)
    result = await gui_rpc.async_dispatch(owner, cmd)
    assert actions.blocking(owner)
    work.finish_publication(owner, work.prepare_publication(owner, cmd, result))
    assert not actions.blocking(owner)
    return result


@pytest.mark.parametrize("kind", ["fallback", "edge", "stt"])
async def test_review_apply_independent_result_and_exact_close(
    armed, tmp_path, monkeypatch, kind
):
    owner, _ = armed
    path = owner._agent_session.store.data_root / "settings.json"
    before = path.read_bytes(), list(owner.conversation)
    review = await prepare(owner, tmp_path, monkeypatch, kind)
    assert review["plan_sha256"] == work.digest(work.encoded(review["plan"]))
    assert not (owner._native_voice_action.directory / "applying").exists()
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(
            owner, command(owner, "apply", review) | {"plan_sha256": "0" * 64}
        )
    applied = await gui_rpc.async_dispatch(owner, command(owner, "apply", review))
    assert applied["status"] == "running" and applied["applied"]
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(owner, command(owner, "apply", review))
    verified = await wait(owner, applied, "verified")
    assert verified["result"][
        "dependencies_verified"
    ] is True and controls.mutation_hold(owner)
    hold = owner._native_voice_action
    await close(owner, verified)
    assert hold.task.done() and not hold.directory.exists()
    assert (path.read_bytes(), list(owner.conversation)) == before


async def test_stale_preferences_and_changed_plan_refuse_before_apply(
    armed, tmp_path, monkeypatch
):
    owner, _ = armed
    review = await prepare(owner, tmp_path, monkeypatch)
    path = owner._agent_session.store.data_root / "settings.json"
    data = path.read_bytes()
    path.write_text(
        json.dumps({**json.loads(data), "tts_timeout": 99}), encoding="utf8"
    )
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(owner, command(owner, "apply", review))
    assert not owner._native_voice_action.applied
    path.write_bytes(data)
    for key, value in [
        ("run", work.ident()),
        ("task", "stt"),
        ("revision", "0" * 64),
        ("work_instance", work.ident()),
    ]:
        with pytest.raises(ValueError):
            await gui_rpc.async_dispatch(
                owner, command(owner, "apply", review) | {key: value}
            )
    await close(owner, review)


@pytest.mark.parametrize("shutdown", [False, True])
async def test_close_and_quit_cancel_join_running_action(
    armed, tmp_path, monkeypatch, shutdown
):
    owner, _ = armed
    review = await prepare(owner, tmp_path, monkeypatch, "stt", slow=True)
    applied = await gui_rpc.async_dispatch(owner, command(owner, "apply", review))
    hold = owner._native_voice_action
    for _ in range(300):
        if (hold.directory / "applying").exists():
            break
        await asyncio.sleep(0.01)
    assert (hold.directory / "applying").exists()
    if shutdown:
        await work.settle_before_shutdown(owner)
    else:
        result = await close(owner, applied)
        assert result["applied"] and result["result"] is None
    assert (
        not actions.blocking(owner)
        and hold.task.done()
        and not hold.unsettled
        and not hold.directory.exists()
    )


async def test_failed_close_publication_holds_action_until_quit(
    armed, tmp_path, monkeypatch
):
    owner, _ = armed
    review = await prepare(owner, tmp_path, monkeypatch)
    cmd = command(owner, "close", review)
    result = await gui_rpc.async_dispatch(owner, cmd)

    def emit(event):
        if event.get("ok"):
            raise OSError("controlled publication failure")

    monkeypatch.setattr(rpc, "rpc_emit", emit)
    with pytest.raises(OSError):
        rpc.publish_management_result(owner, cmd, result)
    assert controls.mutation_hold(owner)
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(owner, command(owner, "close", review))
    await work.settle_before_shutdown(owner)
    assert not actions.blocking(owner)


@pytest.mark.parametrize("kind", ["fallback", "edge", "stt"])
async def test_worker_uses_reviewed_targets_and_independent_verifier(
    tmp_path, monkeypatch, kind
):
    selected = params()
    review = plan(kind, tmp_path, selected, install=kind != "fallback")
    monkeypatch.setattr(worker, "fingerprint", lambda _: "1" * 64)
    calls = []
    probes = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0

    def probe(exe, modules, directory):
        probes.append((exe, modules))
        return {"prefix": str(tmp_path), "cache": str(tmp_path / "cache")}

    monkeypatch.setattr(worker, "bounded_run", run)
    monkeypatch.setattr(worker, "probe", probe)
    result = worker.execute(review, selected, tmp_path)
    assert result["dependencies_verified"] and probes == [
        (sys.executable, [] if review["install"] else review["modules"]),
        (sys.executable, review["modules"]),
    ]
    assert all(argv[0] in review["fingerprints"] for argv, _ in calls)
    if kind == "fallback":
        assert len(calls) == 1 and worker.PHRASE in (tmp_path / "speech.py").read_text()
        assert "litesuite" not in (tmp_path / "speech.py").read_text().lower()
    else:
        assert calls[0][0] == review["argv"] and "--prefix" in calls[0][0]
    if kind == "stt":
        assert len(calls) == 3 and "local_files_only=True" not in calls[1][0][-1]
        assert (
            "local_files_only=True" in calls[2][0][-1]
            and calls[2][1]["environment"]["HF_HUB_OFFLINE"] == "1"
        )
    monkeypatch.setattr(worker, "fingerprint", lambda _: "2" * 64)
    with pytest.raises(ValueError, match="executable changed"):
        worker.execute(review, selected, tmp_path)


@pytest.mark.parametrize("kind", ["edge", "stt"])
async def test_prepare_uses_system_first_and_exact_install_target(
    tmp_path, monkeypatch, kind
):
    candidates = [str(tmp_path / "system.exe"), sys.executable]
    seen = []
    monkeypatch.setattr(worker.optional_python, "candidates", lambda: candidates)
    monkeypatch.setattr(worker.optional_python, "install_target", lambda: candidates[0])
    monkeypatch.setattr(worker.shutil, "which", lambda _: None)
    monkeypatch.setattr(worker, "fingerprint", lambda _: "1" * 64)

    def probe(exe, modules, directory):
        seen.append((exe, modules))
        return (
            None
            if modules
            else {"prefix": str(tmp_path), "cache": str(tmp_path / "cache")}
        )

    monkeypatch.setattr(worker, "probe", probe)
    review = worker.prepare(kind, params(), tmp_path)
    assert seen == [(exe, worker.MODULES[kind]) for exe in candidates] + [
        (candidates[0], [])
    ]
    assert (
        review["interpreter"] == candidates[0]
        and review["install"]
        and review["argv"][0] == candidates[0]
    )
    changed = {**review, "argv": review["argv"][:-1] + ["unreviewed-package"]}
    with pytest.raises(ValueError):
        worker.validate(changed, kind, params())


@pytest.mark.parametrize("kind", ["fallback", "edge", "stt"])
async def test_preapply_interpreter_or_cache_drift_never_runs_effect(
    tmp_path, monkeypatch, kind
):
    review = plan(kind, tmp_path, install=kind != "fallback")
    monkeypatch.setattr(worker, "fingerprint", lambda _: "1" * 64)
    monkeypatch.setattr(
        worker,
        "probe",
        lambda *_: {
            "prefix": str(tmp_path / "changed"),
            "cache": str(tmp_path / "cache"),
        },
    )

    def forbidden(*_, **__):
        raise AssertionError("Drifted plan reached an effect")

    monkeypatch.setattr(worker, "bounded_run", forbidden)
    with pytest.raises(ValueError, match="context changed"):
        worker.execute(review, params(), tmp_path)


async def test_output_reader_drains_large_private_output_and_joins(
    tmp_path, monkeypatch
):
    script = tmp_path / "large_output.py"
    script.write_text(
        "import sys\nsys.stdout.write('private output '*10000)\nsys.stderr.write('private error '*10000)\n",
        encoding="utf8",
    )
    expected = [sys.executable, "-B", str(script)]

    def launch(argv, **kw):
        assert (
            argv == expected
            and kw["stdout"] == subprocess.PIPE
            and kw["stderr"] == subprocess.STDOUT
        )
        return REAL_POPEN(argv, **kw)

    monkeypatch.setattr(subprocess, "Popen", launch)
    assert worker.bounded_run(expected, timeout=3) == 0
    import threading

    assert not any(
        thread.name == "voice-action-output" for thread in threading.enumerate()
    )


@pytest.mark.parametrize(
    "kind,values",
    [("fallback", {"tts_voice": "Draft voice Ω"}), ("stt", {"stt_model": "small.en"})],
)
async def test_reviewed_draft_values_are_bound_without_saving(
    armed, tmp_path, monkeypatch, kind, values
):
    owner, _ = armed
    helper(owner, tmp_path, monkeypatch)
    snapshot = preferences._snapshot(
        owner, work.identity(owner) | {"work_instance": work.bridge(owner).instance}
    )[0]
    selected = actions._preferences(snapshot, kind, values)
    directory = owner._agent_session.store.data_root / "recordings"
    directory.mkdir(exist_ok=True)
    worker.write(
        directory / "private-plan.json",
        plan(kind, tmp_path, selected, install=kind != "fallback"),
    )
    path = owner._agent_session.store.data_root / "settings.json"
    before = path.read_bytes()
    initial = await gui_rpc.async_dispatch(
        owner, command(owner, "review", kind=kind) | {"values": values}
    )
    review = await wait(owner, initial, "review")
    assert review["plan"]["voice" if kind == "fallback" else "model"] == next(
        iter(values.values())
    )
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(
            owner, command(owner, "apply", review) | {"values": {}}
        )
    result = await gui_rpc.async_dispatch(owner, command(owner, "apply", review))
    await close(owner, await wait(owner, result, "verified"))
    assert path.read_bytes() == before


async def test_optional_probe_timeout_is_unavailable_not_a_target_switch(
    tmp_path, monkeypatch
):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(worker, "bounded_run", timeout)
    assert worker.probe(sys.executable, ["pyttsx3"], tmp_path) is None
