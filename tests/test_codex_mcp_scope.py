import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest

from litetui.codex_mcp import PROJECT_SERVERS, config_overrides, project_config


def declare(root):
    root.mkdir(parents=True, exist_ok=True)
    servers = {name: {"command": "python", "args": [f"{name}.py"],
                      "env": {"VALUE": 'spaces and \\"quotes"'},
                      "autoApprove": ["harmless"]} for name in PROJECT_SERVERS}
    (root / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    return servers


def test_non_project_disables_only_inherited_sots(tmp_path):
    assert config_overrides(tmp_path) == tuple(
        f"mcp_servers.{name}.enabled=false" for name in PROJECT_SERVERS)


def test_project_and_descendant_use_json_inputs_without_mutation(tmp_path):
    servers = declare(tmp_path)
    original = (tmp_path / ".mcp.json").read_bytes()
    child = tmp_path / "nested"
    child.mkdir()
    assert config_overrides(child) == config_overrides(tmp_path)
    for name, override in zip(PROJECT_SERVERS, config_overrides(child)):
        key, value = override.split("=", 1)
        cfg = tomllib.loads(f"value = {value}")["value"]
        assert key == f"mcp_servers.{name}"
        assert cfg == {k: v for k, v in servers[name].items() if k != "autoApprove"} | {
            "cwd": str(tmp_path), "enabled": True}
    assert (tmp_path / ".mcp.json").read_bytes() == original


def test_linked_worktree_reads_common_repository(tmp_path):
    repo = tmp_path / "repo"
    declare(repo)
    metadata = repo / ".git" / "worktrees" / "seat"
    metadata.mkdir(parents=True)
    (metadata / "commondir").write_text("../..", encoding="utf-8")
    seat = tmp_path / "outside" / "seat"
    seat.mkdir(parents=True)
    (seat / ".git").write_text(f"gitdir: {metadata}", encoding="utf-8")
    assert project_config(seat) == repo / ".mcp.json"
    assert config_overrides(seat) == config_overrides(repo)


def test_unrelated_project_mcp_does_not_enable_sots(tmp_path):
    (tmp_path / ".mcp.json").write_text('{"mcpServers":{"other":{"command":"python"}}}')
    assert all(override.endswith("enabled=false") for override in config_overrides(tmp_path))


@pytest.mark.parametrize("has_config", [True, False])
def test_own_repo_does_not_inherit_partial_sots_from_parent(tmp_path, has_config):
    servers = declare(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        name: servers[name] for name in ("SOTS_MCP_CORE", "VibeUE")}}))
    repo = tmp_path / "Suite"
    (repo / ".git").mkdir(parents=True)
    if has_config:
        (repo / ".mcp.json").write_text('{"mcpServers":{"litesuite-tools":{"command":"lst"}}}')
    child = repo / "nested"
    child.mkdir()
    assert all(o.endswith("enabled=false") for o in config_overrides(child))


def test_disabled_project_server_stays_disabled(tmp_path):
    servers = declare(tmp_path)
    servers[PROJECT_SERVERS[0]]["disabled"] = True
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    value = config_overrides(tmp_path)[0].split("=", 1)[1]
    assert tomllib.loads(f"value={value}")["value"]["enabled"] is False


def test_invalid_project_entry_refuses_instead_of_using_global(tmp_path):
    servers = declare(tmp_path)
    servers[PROJECT_SERVERS[0]] = None
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}))
    with pytest.raises(ValueError, match="needs a stdio command"):
        config_overrides(tmp_path)


def test_partial_declaration_enables_only_declared_server(tmp_path):
    servers = declare(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "VibeUE": servers["VibeUE"]}}))
    overrides = config_overrides(tmp_path)
    assert overrides[:2] == ("mcp_servers.SOTS_MCP_CORE.enabled=false",
                             "mcp_servers.SOTS_BPGEN.enabled=false")
    assert tomllib.loads("value=" + overrides[2].split("=", 1)[1])["value"]["enabled"]


@pytest.mark.parametrize("content", ["broken", "[]", '{"mcpServers":[]}'])
def test_bad_json_fails_closed_with_path_diagnostic(tmp_path, content):
    (tmp_path / ".mcp.json").write_text(content)
    with pytest.warns(RuntimeWarning, match="cannot load project MCP config; SOTS disabled"):
        assert all(o.endswith("enabled=false") for o in config_overrides(tmp_path))


def test_unreadable_json_fails_closed(tmp_path, monkeypatch):
    declare(tmp_path)
    original = Path.read_text

    def read(path, *args, **kwargs):
        if path.name == ".mcp.json":
            raise PermissionError("test denied")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    with pytest.warns(RuntimeWarning, match="test denied"):
        assert all(o.endswith("enabled=false") for o in config_overrides(tmp_path))


@pytest.mark.asyncio
async def test_app_server_launch_applies_scope_after_caller_overrides(tmp_path, monkeypatch):
    import asyncio

    from litetui.codex_app_server import AppServer
    from litetui.model_transport import ProviderError

    server = AppServer(config_overrides=("mcp_servers.SOTS_MCP_CORE.enabled=true", 'model="unchanged"'))
    server.execution_workspace = tmp_path
    captured = {}

    async def capture(*args, **kwargs):
        captured.update(args=args, kwargs=kwargs)
        raise ProviderError("captured launch")

    monkeypatch.setattr("litetui.codex_app_server.shutil.which", lambda name: "codex.exe")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    with pytest.raises(ProviderError, match="captured launch"):
        await server.start()
    assert captured["args"][2:] == (
        "-c", "mcp_servers.SOTS_MCP_CORE.enabled=true", "-c", 'model="unchanged"',
        "-c", "mcp_servers.SOTS_MCP_CORE.enabled=false",
        "-c", "mcp_servers.SOTS_BPGEN.enabled=false", "-c", "mcp_servers.VibeUE.enabled=false")


def test_installed_codex_project_process_inputs_win_over_global(tmp_path):
    binary = shutil.which("codex")
    if not binary or Path(binary).suffix.lower() in (".cmd", ".ps1", ".bat"):
        pytest.skip("installed executable Codex needed for config-only receipt")
    home = tmp_path / "codex-home"
    home.mkdir()
    (home / "config.toml").write_text(
        '[mcp_servers.SOTS_MCP_CORE]\ncommand="old"\nargs=["old"]\n'
        'startup_timeout_sec=120\n[mcp_servers.SOTS_MCP_CORE.env]\n'
        'VALUE="old"\nEXTRA_GLOBAL="retained"\n', encoding="utf-8")
    project = tmp_path / "project"
    servers = declare(project)
    args = [binary, "mcp", "get", "SOTS_MCP_CORE", "--json"]
    for override in config_overrides(project):
        args.extend(["-c", override])
    result = subprocess.run(args, capture_output=True, text=True, check=True, timeout=30,
                            env={**os.environ, "CODEX_HOME": str(home)},
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    native = json.loads(result.stdout)
    actual = native["transport"]
    source = servers["SOTS_MCP_CORE"]
    assert actual["command"] == source["command"]
    assert actual["args"] == source["args"]
    assert actual["env"]["VALUE"] == source["env"]["VALUE"]
    assert actual["env"]["EXTRA_GLOBAL"] == "retained"  # recursive overlay, not replacement
    assert native["startup_timeout_sec"] == 120

