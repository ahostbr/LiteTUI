from types import SimpleNamespace as NS

import pytest

from litetui.codex_inventory import build_inventory
from litetui.model_transport import ProviderError


def spec(name, schema=None):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "Synthetic tool",
            "parameters": schema or {"type": "object", "properties": {}},
        },
    }


def app(deferred, enabled=True):
    return NS(tools_enabled=enabled, plugins=NS(deferred_specs=lambda: deferred))


def test_disabled_global_tools_exposes_neither_active_nor_deferred_specs():
    tools, inventory = build_inventory([spec("active")], app([spec("secret")], False))
    assert tools == [] and inventory["schemas"] == {}


def test_order_and_dictionary_order_do_not_churn_registration_or_fingerprint():
    a = spec("a", {"properties": {}, "type": "object"})
    b = spec("b")
    assert build_inventory([b, a], app([spec("z"), spec("y")])) == build_inventory(
        [spec("a"), b], app([spec("y"), spec("z")])
    )


def test_changed_schema_or_exposure_changes_digest_but_promotion_preserves_schema_identity():
    _, before = build_inventory([], app([spec("a")]))
    _, promoted = build_inventory([spec("a")], app([]))
    _, changed = build_inventory([spec("a", {"type": "string"})])
    assert before["schemas"] == promoted["schemas"]
    assert before["digest"] != promoted["digest"]
    assert promoted["schemas"] != changed["schemas"]
    assert promoted["digest"] != changed["digest"]


def test_identical_duplicates_coalesce_but_overlapping_different_schemas_refuse_registration():
    tools, _ = build_inventory([spec("a"), spec("a")], app([spec("a")]))
    assert len(tools) == 1 and tools[0]["name"] == "litetui_a"
    with pytest.raises(ProviderError, match="Conflicting"):
        build_inventory([spec("a")], app([spec("a", {"type": "string"})]))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change", ["schema", "disabled", "removed", "unregistered", "unchanged", "promoted"]
)
async def test_native_dispatch_refuses_drift_before_shared_tool_execution(change):
    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServerTransport

    host = app([spec("a")] if change == "promoted" else [])
    active = [] if change == "promoted" else [spec("a")]
    host.plugins.tool_specs = lambda: active
    registered = build_inventory(active, host)[1]
    if change == "schema":
        active = [spec("a", {"type": "string"})]
    elif change == "disabled":
        host.tools_enabled = False
    elif change == "removed":
        active = []
    elif change == "promoted":
        active = [spec("a")]
        host.plugins.deferred_specs = list
    calls = []

    async def execute(name, arguments):
        calls.append(name)
        return "executed", True

    host._execute_tool = execute
    host._rpc = True
    server = Server()
    transport = AppServerTransport(server, host)
    transport.registered_inventory = registered
    await transport._server_request(
        {
            "id": 1,
            "method": "item/tool/call",
            "params": {
                "tool": "litetui_b" if change == "unregistered" else "litetui_a",
                "arguments": {},
            },
        }
    )
    allowed = change in ("unchanged", "promoted")
    assert bool(calls) is allowed
    assert server.replies[-1]["result"]["success"] is allowed


@pytest.mark.asyncio
async def test_resume_preserves_registered_fingerprint_instead_of_claiming_new_tools_were_registered():
    from test_codex_app_server import Server

    from litetui.codex_app_server import AppServerTransport

    server = Server()
    transport = AppServerTransport(server)
    messages = [{"role": "user", "content": "synthetic first"}]
    first = await transport.create(
        model="gpt-6-astra", messages=messages, tools=[spec("a")]
    )
    original = first.choices[0].message.provider_metadata
    expected = build_inventory([spec("a")])[1]
    assert original["tool_inventory"] == expected
    messages.extend(
        [
            {"role": "assistant", "content": "OK", "provider_metadata": original},
            {"role": "user", "content": "synthetic next"},
        ]
    )
    # A fresh transport simulates reconnect to the persisted native thread.
    resumed = AppServerTransport(server)
    second = await resumed.create(
        model="gpt-6-astra", messages=messages, tools=[spec("b")]
    )
    assert second.choices[0].message.provider_metadata["tool_inventory"] == expected
    assert (
        len([method for method, _ in server.requests if method == "thread/start"]) == 1
    )
    assert (
        len([method for method, _ in server.requests if method == "thread/resume"]) == 1
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["schema", "disabled", "removed"])
async def test_shared_tool_door_rechecks_after_human_authorization(change):
    from litetui.app import LiteTUI
    from litetui.codex_inventory import current_dispatch_denial, registered_tools

    host = app([])
    active = [spec("a")]
    host.plugins.tool_specs = lambda: active
    host.plugins.policy_for = lambda _: object()
    host.settings = NS(tools_disabled=[])
    host._rpc_emit = lambda _: None
    calls = []
    host._dispatch_for = lambda _: lambda args: calls.append(args)
    registered = build_inventory(active, host)[1]

    async def authorize(*args, **kwargs):
        nonlocal active
        if change == "schema":
            active = [spec("a", {"type": "string"})]
        elif change == "disabled":
            host.tools_enabled = False
        else:
            active = []

    host._authorize_action = authorize
    with registered_tools(registered):
        _, success = await LiteTUI._execute_tool(host, "a", {})
    assert not success and not calls
    assert current_dispatch_denial(host, "a") is None  # native scope does not leak
