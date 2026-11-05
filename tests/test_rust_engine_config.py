"""Current-engine preference saves use the canonical conversation store, without engine activity."""

from __future__ import annotations

import json

import pytest
from test_rust_native_engine import engine_app as _engine_app
from test_rust_native_engine import switch_app as _switch_app

from litetui import gui_rpc, native_engine, native_engine_config, settings_runtime

engine_app, switch_app = _engine_app, _switch_app
pytestmark = pytest.mark.asyncio


def request(app, action, **values):
    current = native_engine.state(app)
    return {
        "type": "gui.engine.native_config_" + action,
        "id": action,
        "version": 1,
        "management_instance": current.instance,
        "agent_id": current.agent_id,
        "session_id": current.session_id,
        "backend": current.backend.name,
        "desired_model": current.desired_model,
        **values,
    }


async def test_owned_save_and_independent_readback_defer_engine_change(engine_app):
    app = engine_app
    backend, before = app.backend, app.settings.strata_max_context
    initial = await gui_rpc.async_dispatch(app, request(app, "snapshot"))
    assert set(initial["fields"]) == set(native_engine_config.FIELDS["strata"])
    service = settings_runtime.service_for(app)
    paths = service._paths(app.convo_id)
    target = paths["conversation"]
    global_before = paths["global"].read_bytes() if paths["global"].exists() else None
    old = json.loads(target.read_bytes())
    r = await gui_rpc.async_dispatch(
        app,
        request(
            app,
            "review",
            snapshot=initial["snapshot"],
            field="strata_max_context",
            value=12345,
        ),
    )
    assert json.loads(target.read_bytes()) == old
    a = await gui_rpc.async_dispatch(app, request(app, "save", review=r["review"]))
    assert a["saved"] == 12345 and a["effective"] == before and a["timing"] == "restart"
    assert (
        app.backend is backend
        and app.backend._owned is None
        and a["admission"] == "management_only"
    )
    assert json.loads(target.read_bytes())["execution"]["strata_max_context"] == 12345
    assert (
        paths["global"].read_bytes() if paths["global"].exists() else None
    ) == global_before
    read = request(app, "readback", review=r["review"], save_id="save")
    assert (await gui_rpc.async_dispatch(app, read))["kind"] == "engine_config_readback"
    with pytest.raises(ValueError, match="Review"):
        await gui_rpc.async_dispatch(app, request(app, "save", review=r["review"]))
    target.write_text(
        json.dumps({**old, "strata_max_context": 23456}), encoding="utf-8"
    )
    with pytest.raises(ValueError, match="changed after save"):
        await gui_rpc.async_dispatch(app, read)


async def test_refuse_stale_locked_foreign_and_wrong_fields(engine_app, monkeypatch):
    app = engine_app
    snap = await gui_rpc.async_dispatch(app, request(app, "snapshot"))
    for field, value in [
        ("strata_max_context", False),
        ("strata_max_context", -1),
        ("ninfer_artifact", "bad"),
        ("strata_config", "../\x00path"),
        ("strata_host", "executable://path"),
    ]:
        with pytest.raises((ValueError, TypeError)):
            await gui_rpc.async_dispatch(
                app,
                request(
                    app, "review", snapshot=snap["snapshot"], field=field, value=value
                ),
            )
    with pytest.raises(ValueError):
        await gui_rpc.async_dispatch(
            app, {**request(app, "snapshot"), "management_instance": "x"}
        )
    with monkeypatch.context() as patch:
        patch.setenv("LITETUI_STRATA_CONFIG", "env-owned")
        locked = await gui_rpc.async_dispatch(app, request(app, "snapshot"))
        assert locked["fields"]["strata_config"]["locked"]
        with pytest.raises(ValueError, match="override"):
            await gui_rpc.async_dispatch(
                app,
                request(
                    app,
                    "review",
                    snapshot=locked["snapshot"],
                    field="strata_config",
                    value="other",
                ),
            )
    reviewed = await gui_rpc.async_dispatch(
        app,
        request(
            app,
            "review",
            snapshot=snap["snapshot"],
            field="strata_host",
            value="http://127.0.0.1:9000",
        ),
    )
    path = settings_runtime.service_for(app)._paths(app.convo_id)["conversation"]
    changed = {**json.loads(path.read_bytes()), "foreign-extension": True}
    path.write_text(json.dumps(changed), encoding="utf-8")
    refusal = await gui_rpc.async_dispatch(
        app, request(app, "save", review=reviewed["review"])
    )
    assert refusal["kind"] == "engine_config_refused" and refusal["mutated"] is False
    assert refusal["review"] == reviewed["review"] and "changed" in refusal["reason"]
    assert json.loads(path.read_bytes()) == changed


async def test_every_ninfer_input_rejects_unsupported_ranges_without_process_access(
    engine_app,
):
    for field, value in [
        ("ninfer_max_context", 0),
        ("ninfer_max_concurrency", 9),
        ("ninfer_kv_dtype", "fp5"),
        ("ninfer_kv_capacity", "auto-ish"),
        ("ninfer_host_kv_mib", False),
        ("ninfer_artifact", "a" * 4097),
    ]:
        with pytest.raises(ValueError):
            native_engine_config._checked(field, value)
    for field, value in [
        ("ninfer_executable", ""),
        ("ninfer_artifact", "model.ninfer"),
        ("ninfer_max_context", 32000),
        ("ninfer_max_concurrency", 8),
        ("ninfer_kv_capacity", "auto"),
        ("ninfer_kv_dtype", "k8v4"),
        ("ninfer_host_kv_mib", None),
        ("ninfer_host", "http://127.0.0.1:9876"),
    ]:
        assert native_engine_config._checked(field, value) == value


async def test_publication_that_raises_after_writing_closes_engine_admission(
    engine_app, monkeypatch
):
    app = engine_app
    snapshot = await gui_rpc.async_dispatch(app, request(app, "snapshot"))
    review = await gui_rpc.async_dispatch(
        app,
        request(
            app,
            "review",
            snapshot=snapshot["snapshot"],
            field="strata_max_context",
            value=12346,
        ),
    )
    service = settings_runtime.service_for(app)
    save_patch = type(service).save_patch

    def fail_after_write(store, *args, **kwargs):
        assert save_patch(store, *args, **kwargs).fully_saved
        raise OSError("simulated failure after publication")

    monkeypatch.setattr(type(service), "save_patch", fail_after_write)
    with pytest.raises(RuntimeError, match="uncertain"):
        await gui_rpc.async_dispatch(app, request(app, "save", review=review["review"]))
    path = service._paths(app.convo_id)["conversation"]
    assert json.loads(path.read_bytes())["execution"]["strata_max_context"] == 12346
    assert native_engine.state(app).fatal and app._owned_launch_error
