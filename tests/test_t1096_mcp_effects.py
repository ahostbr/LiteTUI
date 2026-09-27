"""T1096: narrow MCP read declarations; editor effects still require a spawner."""
from pathlib import Path

import pytest

from litetui import tool_policy as tp

ROOT = Path(__file__).parent

# The test owns the contract; reading the production sets at collection time
# would prevent a RED run against the pre-fix source from collecting at all.
SOTS_READS = ("sots_server_info", "sots_search", "sots_read_file", "sots_list_dir",
              "sots_help", "sots_read_image")
VIBEUE_READS = ("vibeue_list_tools", "vibeue_tool_schema", "vibeue_check_connection")


@pytest.mark.parametrize("name", SOTS_READS)
def test_sots_read_has_no_confirm(name):
    policy = tp.mcp_policy_for(f"mcp__SOTS_MCP_CORE__{name}")
    assert tp.evaluate(tp.INTERACTIVE, policy, {}, ROOT).action == tp.ALLOW


@pytest.mark.parametrize("name", VIBEUE_READS)
def test_vibeue_read_has_no_confirm(name):
    policy = tp.mcp_policy_for(f"mcp__VibeUE__{name}")
    assert tp.evaluate(tp.INTERACTIVE, policy, {}, ROOT).action == tp.ALLOW


@pytest.mark.parametrize("name", [
    "mcp__VibeUE__execute_python_code", "mcp__SOTS_BPGEN__bpgen_apply",
    # Screenshot launches ffmpeg and writes an image (readOnlyHint:false upstream).
    "mcp__SOTS_MCP_CORE__sots_view_screenshot",
    "mcp__SOTS_MCP_CORE__sots_rag_index", "mcp__Other__sots_read_file",
    "mcp__VibeUE__vibeue_new_unknown", "mcp__litesuite-tools__tasks",
])
def test_mutations_and_unknown_names_still_confirm(name):
    policy = tp.mcp_policy_for(name)
    assert policy is tp.MCP_UNKNOWN_POLICY
    assert tp.evaluate(tp.INTERACTIVE, policy, {}, ROOT).action == tp.CONFIRM


@pytest.mark.parametrize("args,expected", [
    ({"action": "query", "query": "ninja"}, tp.ALLOW),
    ({"action": "status"}, tp.ALLOW),
    ({"action": "index"}, tp.CONFIRM),
    ({"action": "read", "payload": {"op": "index"}}, tp.CONFIRM),
    ({"action": "create", "payload": {"op": "query"}}, tp.CONFIRM),
    ({"action": "run", "payload": {"op": "query"}}, tp.CONFIRM),
    ({"action": "bogus"}, tp.CONFIRM),
    ({}, tp.CONFIRM),
])
def test_sots_rag_router_checks_the_actual_operation(args, expected):
    policy = tp.mcp_policy_for("mcp__SOTS_MCP_CORE__sots_rag")
    assert tp.evaluate(tp.INTERACTIVE, policy, args, ROOT).action == expected


@pytest.mark.parametrize("args,expected", [
    ({"action": "help"}, tp.ALLOW),
    ({"action": "get", "payload": {"op": "server_info"}}, tp.ALLOW),
    ({"action": "create", "payload": {"op": "server_info"}}, tp.CONFIRM),
    ({"action": "smoketest_all"}, tp.CONFIRM),
    ({"action": "other"}, tp.CONFIRM),
    ({}, tp.CONFIRM),
])
def test_sots_meta_router_checks_the_actual_operation(args, expected):
    policy = tp.mcp_policy_for("mcp__SOTS_MCP_CORE__sots_meta")
    assert tp.evaluate(tp.INTERACTIVE, policy, args, ROOT).action == expected
