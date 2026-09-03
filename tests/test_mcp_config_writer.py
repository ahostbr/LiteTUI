"""Adding and removing MCP servers writes .mcp.json — safely.

Nothing in LiteTUI has ever written an MCP config: `mcp.json` was read at boot
and that was the whole story, so "add a server" meant "quit, hand-edit JSON,
restart". These tests pin the three ways a config writer can do damage that the
user only discovers later:

  1. clobbering keys it does not understand (another tool's settings, or a
     server it did not put there),
  2. leaving a half-written file that makes EVERY server in it fail to parse,
  3. writing a name that a higher-precedence file already claims, so the write
     is real on disk and invisible in the app.

The third is specific to this repo: read_server_configs gives `mcp.json` the
win over `.mcp.json`, and `.mcp.json` is the only file /mcp writes.
"""

from __future__ import annotations

import json

import pytest

from litetui.mcp_client import MCPManager, validate_entry

#: This file drives MCPManager.load itself against its own tmp configs, so
#: the conftest stub that keeps every OTHER test off the network must not
#: apply here. See tests/conftest.py::_never_dial_out_from_a_constructor.
pytestmark = pytest.mark.real_mcp_load


class StubServer:
    def __init__(self, name, cfg, cwd, log_handle, fail=False):
        self.name, self.cfg, self.tools, self.stopped = name, cfg, [], 0
        self._fail = fail

    def start(self):
        if self._fail:
            raise RuntimeError("boom")
        self.tools = [{"name": "alpha"}]

    def stop(self):
        self.stopped += 1


@pytest.fixture
def mgr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        MCPManager,
        "_build",
        lambda self, name, sc: StubServer(name, sc, self.root, None, bool(sc.get("_fail"))),
    )
    return MCPManager(tmp_path)


def _doc(tmp_path) -> dict:
    return json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))


# ── validate_entry ──────────────────────────────────────────────────────────
def test_an_entry_with_neither_command_nor_url_is_rejected():
    """The case worth catching: it parses, it reads like a server, it can never start.

    Validation mirrors the ONE rule `_build` dispatches on, so anything that
    validates here is guaranteed to classify there.
    """
    assert validate_entry({"name": "x"}) is not None
    assert validate_entry({"command": "python"}) is None
    assert validate_entry({"url": "http://localhost:1/mcp"}) is None


def test_args_and_env_shapes_are_checked():
    assert "args" in (validate_entry({"command": "x", "args": "not-a-list"}) or "")
    assert "env" in (validate_entry({"command": "x", "env": []}) or "")


# ── add ─────────────────────────────────────────────────────────────────────
def test_add_writes_the_entry_and_connects_it(mgr, tmp_path):
    assert mgr.add("files", {"command": "npx", "args": ["-y", "srv"]}) is None
    assert _doc(tmp_path)["mcpServers"]["files"]["command"] == "npx"
    assert "files" in mgr.servers
    assert mgr.describe()[0]["state"] == "connected"


def test_add_preserves_other_servers_and_unknown_top_level_keys(mgr, tmp_path):
    """🔴 READ-MODIFY-WRITE ON THE WHOLE DOCUMENT.

    A config file can carry keys this app has never heard of — another tool
    sharing the file, or a comment-ish field. Regenerating the file from
    `mcpServers` alone would delete them, and the user would find out much
    later, with no way to tell which tool did it.
    """
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"old": {"url": "http://x/mcp"}}, "somethingElse": {"keep": 1}}),
        encoding="utf-8",
    )
    assert mgr.add("new", {"command": "y"}) is None
    doc = _doc(tmp_path)
    assert set(doc["mcpServers"]) == {"old", "new"}
    assert doc["somethingElse"] == {"keep": 1}


def test_a_rejected_entry_never_reaches_disk(mgr, tmp_path):
    """Validation runs BEFORE the write, so a typo needs no hand-repair."""
    assert not (tmp_path / ".mcp.json").exists()
    err = mgr.add("bad", {"nothing": "useful"})
    assert err is not None
    assert not (tmp_path / ".mcp.json").exists()


def test_add_refuses_a_name_that_is_already_declared(mgr, tmp_path):
    mgr.add("dup", {"command": "x"})
    err = mgr.add("dup", {"command": "y"})
    assert err is not None and "already declared" in err and ".mcp.json" in err
    assert _doc(tmp_path)["mcpServers"]["dup"]["command"] == "x"


def test_add_refuses_a_name_mcp_json_already_claims_and_names_that_file(mgr, tmp_path):
    """⚠️ THE INVISIBLE-WRITE CASE.

    read_server_configs lets the EARLIER file win, and /mcp only writes
    `.mcp.json`. Without this guard the entry lands on disk, the app keeps using
    mcp.json's version, and the user is left comparing a file that says one
    thing with an app that does another. Refusing has to name the offending
    file or the message is unactionable.
    """
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"shadowed": {"command": "from-mcp-json"}}}), encoding="utf-8"
    )
    err = mgr.add("shadowed", {"command": "from-dot-mcp-json"})
    assert err is not None
    assert "mcp.json" in err and "precedence" in err
    assert not (tmp_path / ".mcp.json").exists()


def test_add_can_declare_without_connecting(mgr, tmp_path):
    assert mgr.add("later", {"command": "x"}, connect=False) is None
    assert "later" in mgr.configs and "later" not in mgr.servers
    assert mgr.describe()[0]["state"] == "stopped"


def test_add_reports_a_start_failure_but_keeps_the_declaration(mgr, tmp_path):
    """A server that fails to start is still declared — that is how you fix it.

    Rolling the write back on a failed start would delete the entry the user
    needs to edit, and make the dialog's Add button look like it did nothing.
    """
    err = mgr.add("bad", {"command": "x", "_fail": True})
    assert err is not None and "boom" in err
    assert "bad" in mgr.configs
    assert _doc(tmp_path)["mcpServers"]["bad"]["command"] == "x"


# ── remove ──────────────────────────────────────────────────────────────────
def test_remove_stops_it_first_then_deletes_the_entry(mgr, tmp_path):
    """Order matters: deleting first would leave a process with no config —
    an orphan, which is a state describe() has to invent a name for."""
    mgr.add("gone", {"command": "x"})
    srv = mgr.servers["gone"]
    assert mgr.remove("gone") is None
    assert srv.stopped == 1
    assert "gone" not in mgr.servers and "gone" not in mgr.configs
    assert _doc(tmp_path)["mcpServers"] == {}


def test_remove_clears_a_recorded_failure(mgr, tmp_path):
    mgr.add("bad", {"command": "x", "_fail": True})
    assert "bad" in mgr.failures
    assert mgr.remove("bad") is None
    assert "bad" not in mgr.failures


def test_remove_refuses_what_it_did_not_write_and_says_where_it_lives(mgr, tmp_path):
    """/mcp writes one file. A server declared elsewhere must be refused by
    NAME OF FILE, not silently ignored — otherwise remove reports success and
    the server comes back on the next boot."""
    (tmp_path / "mcp.json").write_text(
        json.dumps({"mcpServers": {"elsewhere": {"command": "x"}}}), encoding="utf-8"
    )
    mgr.reload_configs()
    err = mgr.remove("elsewhere")
    assert err is not None and "mcp.json" in err


def test_remove_of_an_unknown_name_is_an_error_not_a_silent_success(mgr, tmp_path):
    mgr.add("real", {"command": "x"})
    err = mgr.remove("ghost")
    assert err is not None and "ghost" in err


# ── durability ──────────────────────────────────────────────────────────────
def test_the_write_is_atomic_and_leaves_no_temp_file(mgr, tmp_path):
    """os.replace, not open-and-truncate.

    read_server_configs treats an unparseable file as an error for EVERY server
    in it, so a torn write does not lose one server — it disconnects all of
    them. A leftover .tmp would also be picked up by nothing and puzzle whoever
    found it.
    """
    mgr.add("a", {"command": "x"})
    mgr.add("b", {"url": "http://localhost:2/mcp"})
    assert list(tmp_path.glob("*.tmp")) == []
    assert set(_doc(tmp_path)["mcpServers"]) == {"a", "b"}


def test_a_written_config_survives_a_fresh_manager(mgr, tmp_path, monkeypatch):
    """The point of writing at all: it is there on the next boot."""
    mgr.add("persisted", {"command": "x"})
    monkeypatch.setattr(
        MCPManager, "_build", lambda self, name, sc: StubServer(name, sc, self.root, None)
    )
    fresh = MCPManager(tmp_path)
    fresh.load()
    assert "persisted" in fresh.servers
