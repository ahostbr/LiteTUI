"""Native metadata reviews exercise the real leased app, provenance and no-rollback commit."""

import json

import pytest
from test_plugin_reload_ui import _reg_with, _spec
from test_rust_context_reader import app as owner_app
from test_rust_context_reader import armed as owner_armed
from test_rust_context_reader import switch_app as owner_switch

from litetui import background_input as work
from litetui import gui_rpc, tool_schemas
from litetui import native_plugin_reload as reload
from litetui.plugin_reload_provenance import capture_baseline

app, armed, switch_app = owner_app, owner_armed, owner_switch
pytestmark = pytest.mark.asyncio


def call(app, action, **fields):
    return gui_rpc.dispatch(
        app,
        {
            "version": 1,
            "id": action + "-id",
            "type": reload.PREFIX + action,
            **work.identity(app),
            "work_instance": work.bridge(app).instance,
            **fields,
        },
    )


@pytest.fixture
def schema(tmp_path, monkeypatch):
    dir = tmp_path / "schemas"
    dir.mkdir()
    monkeypatch.setattr(tool_schemas, "_package_dir", lambda: dir)
    monkeypatch.setattr(tool_schemas, "_legacy_dir", lambda: tmp_path / "absent")
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    return dir / "bash.json"


def boot(app, path):
    app.plugins = _reg_with()
    app._plugin_source_baseline = None
    capture_baseline(app)
    path.write_text(
        json.dumps(_spec(description="new description\nsecond line")), encoding="utf-8"
    )


async def test_real_review_publish_and_independent_registry_read(armed, schema):
    boot(armed, schema)
    registry = armed.plugins
    assert call(armed, "list")["names"] == ["bash"]
    review = call(armed, "review", name="bash")
    assert (
        review["before"] == "original desc"
        and review["after"] == "new description\nsecond line"
    )
    assert armed.plugins is registry
    args = {"name": "bash", "token": review["token"]}
    result = call(armed, "apply", **args)
    assert (
        armed.plugins is not registry
        and armed.plugins.tools[0].run is registry.tools[0].run
    )
    assert call(armed, "apply", **args) == result
    receipt = call(armed, "readback", **args, apply_id=result["apply_id"])
    assert (
        receipt["kind"] == "plugin_verified"
        and receipt["description"] == review["after"]
    )
    armed.plugins.tools[0].spec["function"]["description"] = "drift"
    with pytest.raises(ValueError, match="independently verified"):
        call(armed, "readback", **args, apply_id=result["apply_id"])


async def test_stale_disk_contract_native_backend_provenance_and_children_refuse(
    armed, schema, monkeypatch
):
    boot(armed, schema)
    initial = armed.plugins
    review = call(armed, "review", name="bash")
    args = {"name": "bash", "token": review["token"]}
    schema.write_text(json.dumps(_spec(description="raced")), encoding="utf-8")
    with pytest.raises(ValueError, match="changed since review"):
        call(armed, "apply", **args)
    for desc, parameters, reason in [
        ("has {exe}", None, "Templated"),
        (
            "different",
            {"type": "object", "properties": {"x": {"type": "string"}}},
            "contract",
        ),
        ("original desc", None, "already matches"),
    ]:
        schema.write_text(
            json.dumps(_spec(description=desc, params=parameters)), encoding="utf-8"
        )
        with pytest.raises(ValueError, match=reason):
            call(armed, "review", name="bash")
    schema.write_text(json.dumps(_spec(description="new")), encoding="utf-8")
    with monkeypatch.context() as m:
        m.setattr(armed.backend, "app_server", object(), raising=False)
        with pytest.raises(ValueError, match="ownership changed|restart"):
            call(armed, "review", name="bash")
        with pytest.raises(ValueError, match="restart"):
            reload._checked(armed, initial, "bash")
    for bad in ("powershell", "../bash", "skills"):
        with pytest.raises(ValueError):
            call(armed, "review", name=bad)
    with pytest.raises(ValueError):
        call(armed, "review", name="bash", work_instance="foreign")
    review = call(armed, "review", name="bash")
    with monkeypatch.context() as m:
        m.setattr(reload.source, "children_pending", lambda *args: True)
        with pytest.raises(ValueError, match="deferred"):
            call(armed, "apply", name="bash", token=review["token"])
    with monkeypatch.context() as m:
        m.setattr(armed, "_plugin_source_baseline", {})
        with pytest.raises(ValueError, match="restart"):
            call(armed, "review", name="bash")
    assert (
        armed.plugins is initial
        and initial.tools[0].spec["function"]["description"] == "original desc"
    )


async def test_exact_transport_worker_only_sibling_and_inherited_task_remain_busy(
    armed, schema
):
    import asyncio

    boot(armed, schema)
    started = asyncio.Event()
    release = asyncio.Event()

    async def sibling():
        started.set()
        await release.wait()

    async def inspect():
        token = gui_rpc.OPERATION_CONTEXT.set("owned")
        try:
            own = reload._activity(armed, {"id": "owned"})
            wrong = reload._activity(armed, {"id": "another"})

            async def inherited():
                return reload._activity(armed, {"id": "owned"})

            inherited_result = await asyncio.create_task(inherited())
            other = armed.run_worker(sibling(), group="gui-management")
            await started.wait()
            try:
                both = reload._activity(armed, {"id": "owned"})
            finally:
                release.set()
                await other.wait()
            return (
                own.management_active,
                wrong.management_active,
                inherited_result.management_active,
                both.management_active,
            )
        finally:
            gui_rpc.OPERATION_CONTEXT.reset(token)

    worker = armed.run_worker(inspect(), group="gui-management")
    assert await worker.wait() == (False, True, True, True)


async def test_static_schema_byte_limit_and_hard_link_refuse_before_publication(
    armed, schema, tmp_path
):
    import os

    boot(armed, schema)
    registry = armed.plugins
    schema.write_bytes(b" " * (reload.MAX_SCHEMA + 1))
    with pytest.raises(ValueError, match="limit"):
        call(armed, "review", name="bash")
    schema.unlink()
    outside = tmp_path / "foreign.json"
    outside.write_text(json.dumps(_spec(description="not allowed")), encoding="utf-8")
    os.link(outside, schema)
    with pytest.raises(ValueError, match="Linked"):
        call(armed, "review", name="bash")
    assert armed.plugins is registry
