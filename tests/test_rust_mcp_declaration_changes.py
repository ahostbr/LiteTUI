"""Add and remove shared MCP declarations through the real reviewed writer. No MCP I/O ever."""

import json

import pytest
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_context
from test_rust_context_reader import switch_app as owned_switch_app
from test_rust_mcp_declarations import call

app, switch_app, armed = owned_app, owned_switch_app, owned_context
pytestmark = pytest.mark.asyncio

SECRET = "super-secret-value-do-not-emit"


def forbid_runtime(app, monkeypatch):
    """A declaration change must never reach a transport, a process or the network."""
    import subprocess
    import urllib.request

    from litetui import mcp_client

    def unexpected(*_a, **_k):
        pytest.fail("a declaration change touched the MCP runtime")

    for verb in (
        "load",
        "connect",
        "reconnect",
        "disconnect",
        "reconcile",
        "reload_configs",
        "add",
        "remove",
        "_connect_locked",
        "_build",
    ):
        monkeypatch.setattr(app.mcp, verb, unexpected)
    monkeypatch.setattr(mcp_client.MCPServer, "start", unexpected)
    monkeypatch.setattr(mcp_client.HTTPMCPServer, "start", unexpected)
    monkeypatch.setattr(mcp_client.ttyguard, "popen", unexpected)
    monkeypatch.setattr(subprocess, "Popen", unexpected)
    monkeypatch.setattr(urllib.request, "urlopen", unexpected)


def stdio(name="added", **kw):
    spec = {
        "transport": "stdio",
        "name": name,
        "command": "npx",
        "args": ["-y", "pkg"],
        "env": {"TOKEN": SECRET},
    }
    return {**spec, **kw}


def remote(name="remote", **kw):
    spec = {
        "transport": "http",
        "name": name,
        "url": "https://api.example.com/v1/mcp?key=" + SECRET,
        "headers": {"Authorization": "Bearer " + SECRET},
    }
    return {**spec, **kw}


def commit(app, review_op, **kw):
    snapshot = call(app, "list")
    review = call(app, review_op, revision=snapshot["revision"], **kw)
    applied = call(app, "apply", token=review["token"])
    proof = call(app, "readback", token=review["token"], apply_id=applied["apply_id"])
    return snapshot, review, applied, proof


async def test_add_stdio_and_http_write_readback_without_echoing_secrets(
    armed, monkeypatch
):
    app = armed
    forbid_runtime(app, monkeypatch)
    dot = app.mcp.root / ".mcp.json"
    assert not dot.exists()
    snapshot, review, applied, proof = commit(app, "add_review", spec=stdio())
    receipts = [review, applied, proof]
    assert review["kind"] == "mcp_config_add_review"
    assert applied["kind"] == "mcp_config_add_applied"
    assert proof["kind"] == "mcp_config_add_saved" and proof["file"] == ".mcp.json"
    assert review["entry"] == {
        "transport": "stdio",
        "target": "npx",
        "args": 2,
        "env": ["TOKEN"],
        "headers": [],
        "omitted": 0,
    }
    assert json.loads(dot.read_bytes()) == {
        "mcpServers": {
            "added": {"command": "npx", "args": ["-y", "pkg"], "env": {"TOKEN": SECRET}}
        }
    }
    assert proof["config"]["rows"][-1]["name"] == "added"
    assert proof["after"] == proof["config"]["revision"]
    assert proof["before"] == snapshot["revision"]
    _, review, applied, proof = commit(app, "add_review", spec=remote())
    receipts += [review, applied, proof]
    assert review["entry"] == {
        "transport": "http",
        "target": "https://api.example.com",
        "args": 0,
        "env": [],
        "headers": ["Authorization"],
        "omitted": 0,
    }
    written = json.loads(dot.read_bytes())["mcpServers"]
    assert list(written) == ["added", "remote"]
    assert written["remote"] == {
        "type": "http",
        "url": "https://api.example.com/v1/mcp?key=" + SECRET,
        "headers": {"Authorization": "Bearer " + SECRET},
    }
    assert SECRET not in json.dumps(receipts) + json.dumps(call(app, "list"))
    assert [r["name"] for r in proof["config"]["rows"]] == ["added", "remote"]
    assert all(r["editable"] and r["removable"] for r in proof["config"]["rows"])


async def test_add_preserves_foreign_keys_and_review_alone_writes_nothing(
    armed, monkeypatch
):
    app = armed
    forbid_runtime(app, monkeypatch)
    dot = app.mcp.root / ".mcp.json"
    original = {"keep": SECRET, "mcpServers": {"old": {"command": "x"}}}
    dot.write_text(json.dumps(original))
    review = call(app, "add_review", revision=call(app, "list")["revision"], spec=stdio())
    assert json.loads(dot.read_bytes()) == original
    assert list(dot.parent.glob(".mcp-edit-*")) == []
    call(app, "apply", token=review["token"])
    result = json.loads(dot.read_bytes())
    assert result["keep"] == SECRET and list(result["mcpServers"]) == ["old", "added"]


def refuse(app, match, **kw):
    revision = kw.pop("revision", None) or call(app, "list")["revision"]
    with pytest.raises(ValueError, match=match) as raised:
        call(app, "add_review", revision=revision, **kw)
    assert SECRET not in str(raised.value)


async def test_add_refuses_with_the_source_reasons(armed, monkeypatch):
    app = armed
    forbid_runtime(app, monkeypatch)
    root = app.mcp.root
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"pinned": {"command": "a"}}})
    )
    dot = root / ".mcp.json"
    dot.write_text(json.dumps({"mcpServers": {"taken": {"command": "b"}}}))
    refuse(app, "mcp.json already declares 'pinned' and wins on precedence", spec=stdio("pinned"))
    refuse(app, "'taken' is already declared in .mcp.json", spec=stdio("taken"))
    refuse(app, "changed", spec=stdio(), revision="0" * 64)
    refuse(app, "Command", spec=stdio(command=""))
    refuse(app, "http:// or https://", spec=remote(url="ftp://example.com"))
    refuse(app, "http:// or https://", spec=remote(url="https://example.com:notaport"))
    refuse(app, "server name", spec=stdio("bad name"))
    refuse(app, "server name", spec=stdio("../escape"))
    refuse(app, "Environment contains an invalid name", spec=stdio(env={"BAD NAME": "v"}))
    refuse(app, "printable", spec=stdio(args=["ok\nline"]))
    refuse(app, "printable", spec=remote(headers={"X-Key": "a\r\nInjected: 1"}))
    refuse(app, "Arguments", spec=stdio(args="not a list"))
    refuse(app, "unexpected fields", spec={**stdio(), "cwd": "x"})
    refuse(app, "unexpected fields", spec={**stdio(), "url": "https://example.com"})
    refuse(app, "stdio or http", spec={**stdio(), "transport": "sse"})
    assert json.loads(dot.read_bytes()) == {"mcpServers": {"taken": {"command": "b"}}}
    dot.write_text(json.dumps({"servers": {"legacy": {"command": "c"}}}))
    refuse(app, "legacy `servers` block", spec=stdio())
    (root / "mcp.json").unlink()
    dot.write_text(
        json.dumps({"mcpServers": {str(i): {"command": "c"} for i in range(64)}})
    )
    refuse(app, "64-row", spec=stdio())


async def test_changed_files_between_review_and_apply_fail_closed(armed, monkeypatch):
    app = armed
    forbid_runtime(app, monkeypatch)
    dot = app.mcp.root / ".mcp.json"
    dot.write_text('{"mcpServers":{"keep":{"command":"a"}}}')
    review = call(app, "add_review", revision=call(app, "list")["revision"], spec=stdio())
    dot.write_text('{"mcpServers":{"keep":{"command":"edited-elsewhere"}}}')
    changed = dot.read_bytes()
    with pytest.raises(ValueError, match="changed"):
        call(app, "apply", token=review["token"])
    assert dot.read_bytes() == changed
    with pytest.raises(ValueError, match="stale"):
        call(app, "apply", token=review["token"])
    app.mcp.servers["running"] = object()
    try:
        with pytest.raises(ValueError):
            call(app, "add_review", revision=call(app, "list")["revision"], spec=stdio())
    finally:
        app.mcp.servers.pop("running")


async def test_remove_round_trip_keeps_everything_else(armed, monkeypatch):
    app = armed
    forbid_runtime(app, monkeypatch)
    dot = app.mcp.root / ".mcp.json"
    dot.write_text(
        json.dumps(
            {
                "keep": SECRET,
                "mcpServers": {
                    "first": {
                        "command": "/opt/tools/runner",
                        "args": [SECRET],
                        "env": {"TOKEN": SECRET},
                    },
                    "second": {
                        "type": "http",
                        "url": "https://u:" + SECRET + "@host.example:8443/p?q=" + SECRET,
                        "headers": {"Authorization": SECRET},
                    },
                },
            }
        )
    )
    _, review, applied, proof = commit(app, "remove_review", name="first")
    assert review["kind"] == "mcp_config_remove_review"
    assert proof["kind"] == "mcp_config_remove_saved"
    assert review["entry"] == {
        "transport": "stdio",
        "target": "runner",
        "args": 1,
        "env": ["TOKEN"],
        "headers": [],
        "omitted": 0,
    }
    assert SECRET not in json.dumps([review, applied, proof])
    document = json.loads(dot.read_bytes())
    assert list(document["mcpServers"]) == ["second"] and document["keep"] == SECRET
    assert [r["name"] for r in proof["config"]["rows"]] == ["second"]
    _, review, _, proof = commit(app, "remove_review", name="second")
    assert review["entry"]["target"] == "https://host.example:8443"
    assert SECRET not in json.dumps(review)
    assert json.loads(dot.read_bytes()) == {"keep": SECRET, "mcpServers": {}}
    assert proof["config"]["rows"] == []


async def test_remove_refuses_shadowed_missing_and_ambiguous_entries(armed, monkeypatch):
    app = armed
    forbid_runtime(app, monkeypatch)
    root = app.mcp.root
    (root / "mcp.json").write_text(
        json.dumps({"mcpServers": {"both": {"command": "a"}, "primary": {"command": "p"}}})
    )
    dot = root / ".mcp.json"
    dot.write_text(
        json.dumps({"mcpServers": {"both": {"command": "b"}, "mine": {"command": "m"}}})
    )
    revision = call(app, "list")["revision"]
    for name, match in (
        ("both", "'both' is declared in mcp.json, which /mcp does not write"),
        ("primary", "'primary' is declared in mcp.json, which /mcp does not write"),
        ("ghost", "no server named 'ghost' in .mcp.json"),
        ("../x", "visible"),
    ):
        with pytest.raises(ValueError, match=match):
            call(app, "remove_review", revision=revision, name=name)
    with pytest.raises(ValueError, match="changed"):
        call(app, "remove_review", revision="f" * 64, name="mine")
    dot.write_text(json.dumps({"servers": {"legacy": {"command": "c"}}}))
    before = dot.read_bytes()
    with pytest.raises(ValueError, match="no server named 'legacy'"):
        call(app, "remove_review", revision=call(app, "list")["revision"], name="legacy")
    assert dot.read_bytes() == before


async def test_malformed_entry_is_removable_even_though_it_is_not_editable(
    armed, monkeypatch
):
    app = armed
    forbid_runtime(app, monkeypatch)
    dot = app.mcp.root / ".mcp.json"
    dot.write_text(
        json.dumps({"mcpServers": {"broken": {"args": [SECRET], "env": "oops"}}})
    )
    row = call(app, "list")["rows"][0]
    assert row["editable"] is False and row["removable"] is True
    _, review, _, proof = commit(app, "remove_review", name="broken")
    assert review["entry"]["transport"] == "unknown"
    assert SECRET not in json.dumps(review) and proof["config"]["rows"] == []


async def test_readback_detects_a_file_that_no_longer_matches(armed):
    app = armed
    dot = app.mcp.root / ".mcp.json"
    dot.write_text('{"mcpServers":{"keep":{"command":"a"}}}')
    review = call(app, "add_review", revision=call(app, "list")["revision"], spec=stdio())
    applied = call(app, "apply", token=review["token"])
    dot.write_text('{"mcpServers":{"keep":{"command":"a"}}}')
    with pytest.raises(ValueError, match="changed after apply"):
        call(app, "readback", token=review["token"], apply_id=applied["apply_id"])
