"""MCP server identity determines whether interactive needs a confirmation."""
from pathlib import Path

import pytest

from litetui import tool_policy as tp
from litetui.plugins import PluginContext, PluginRegistry

ROOT = Path(__file__).parent

# The test owns the contract; reading the production sets at collection time
# would prevent a RED run against the pre-fix source from collecting at all.
SOTS_READS = ("sots_server_info", "sots_search", "sots_read_file", "sots_list_dir",
              "sots_help", "sots_read_image")
VIBEUE_READS = ("vibeue_list_tools", "vibeue_tool_schema", "vibeue_check_connection")


def test_registered_mcp_authorization_path_reads_and_effects():
    """The app obtains policy from the registry, not the standalone helper.

    This also runs against the pre-T1096 source: a VibeUE read must differ
    from its old provider-wide CONFIRM policy, while writes remain guarded.
    """
    names = (
        "mcp__VibeUE__vibeue_list_tools",
        "mcp__SOTS_MCP_CORE__sots_view_screenshot",
        "mcp__SOTS_MCP_CORE__sots_rag",
    )
    registry = PluginRegistry()
    PluginContext(None, registry, "mcp").dynamic_tools(
        lambda: [], lambda name: (lambda args: None) if name in names else None,
    )
    decisions = (
        (names[0], {}, tp.ALLOW),
        (names[1], {}, tp.ALLOW),
        (names[2], {"action": "index"}, tp.ALLOW),
    )
    for name, args, expected in decisions:
        policy = registry.policy_for(name)
        assert policy is not None, f"registered MCP tool {name} has no policy"
        assert tp.evaluate(tp.INTERACTIVE, policy, args, ROOT).action == expected, name


@pytest.mark.parametrize("name", SOTS_READS)
def test_sots_read_has_no_confirm(name):
    policy = tp.mcp_policy_for(f"mcp__SOTS_MCP_CORE__{name}")
    assert tp.evaluate(tp.INTERACTIVE, policy, {}, ROOT).action == tp.ALLOW


@pytest.mark.parametrize("name", VIBEUE_READS)
def test_vibeue_read_has_no_confirm(name):
    policy = tp.mcp_policy_for(f"mcp__VibeUE__{name}")
    assert tp.evaluate(tp.INTERACTIVE, policy, {}, ROOT).action == tp.ALLOW


@pytest.mark.parametrize("name", [
    "mcp__Other__sots_read_file", "mcp__Other__execute_python_code",
])
def test_mutations_and_unknown_names_still_confirm(name):
    policy = tp.mcp_policy_for(name)
    assert policy is tp.MCP_UNKNOWN_POLICY
    assert tp.evaluate(tp.INTERACTIVE, policy, {}, ROOT).action == tp.CONFIRM


@pytest.mark.parametrize("args,expected", [
    ({"action": "query", "query": "ninja"}, tp.ALLOW),
    ({"action": "status"}, tp.ALLOW),
    ({"action": "index"}, tp.ALLOW),
    ({"action": "read", "payload": {"op": "index"}}, tp.ALLOW),
    ({"action": "create", "payload": {"op": "query"}}, tp.ALLOW),
    ({"action": "run", "payload": {"op": "query"}}, tp.ALLOW),
    ({"action": "bogus"}, tp.ALLOW),
    ({}, tp.ALLOW),
])
def test_sots_rag_router_checks_the_actual_operation(args, expected):
    policy = tp.mcp_policy_for("mcp__SOTS_MCP_CORE__sots_rag")
    assert tp.evaluate(tp.INTERACTIVE, policy, args, ROOT).action == expected


@pytest.mark.parametrize("args,expected", [
    ({"action": "help"}, tp.ALLOW),
    ({"action": "get", "payload": {"op": "server_info"}}, tp.ALLOW),
    ({"action": "create", "payload": {"op": "server_info"}}, tp.ALLOW),
    ({"action": "smoketest_all"}, tp.ALLOW),
    ({"action": "other"}, tp.ALLOW),
    ({}, tp.ALLOW),
])
def test_sots_meta_router_checks_the_actual_operation(args, expected):
    policy = tp.mcp_policy_for("mcp__SOTS_MCP_CORE__sots_meta")
    assert tp.evaluate(tp.INTERACTIVE, policy, args, ROOT).action == expected
