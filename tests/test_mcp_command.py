"""`/mcp` verbs, and the dispatch rebuild that makes them real.

🔴 THE WIRING THESE TESTS EXIST FOR. `mcp_plugin` registers MCP as a dynamic
provider: its SPECS callable is `lambda: app.mcp.tool_specs()`, evaluated every
turn, so a newly connected server's tools reach the model on their own. Its
DISPATCH callable reads `app.mcp_dispatch`, a map cached once in __init__.

So a connect that forgets `rebuild_mcp_dispatch()` produces the worst possible
shape: the model is offered a tool, calls it, and the loop cannot route it. The
user sees "unknown tool" and blames the model. Nothing else in the system goes
red — which is exactly why the assertion is on the rebuild COUNT rather than on
some downstream symptom.

The verbs are tested through a fake app rather than a running Textual app: the
property under test is "which manager call, and did it rebuild", and a real app
would add a screen, an event loop and a model server to prove none of it.
"""

from __future__ import annotations

import json

import pytest

from litetui.mcp_client import MCPManager
from litetui.plugins import mcp_manage
from litetui.plugins.mcp_manage import _cmd_mcp, _entry_from_words

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
        self.tools = [{"name": "alpha"}, {"name": "beta"}]

    def stop(self):
        self.stopped += 1


class FakeApp:
    def __init__(self, root, monkeypatch):
        monkeypatch.setattr(
            MCPManager,
            "_build",
            lambda self, name, sc: StubServer(name, sc, self.root, None, bool(sc.get("_fail"))),
        )
        self.mcp = MCPManager(root)
        self.said: list[str] = []
        self.rebuilds = 0

    def system_message(self, text: str) -> None:
        self.said.append(text)

    def rebuild_mcp_dispatch(self) -> None:
        self.rebuilds += 1

    @property
    def last(self) -> str:
        return self.said[-1] if self.said else ""


@pytest.fixture
def app(tmp_path, monkeypatch):
    return FakeApp(tmp_path, monkeypatch)


def _config(tmp_path, servers):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")


# ── the entry parser ────────────────────────────────────────────────────────
def test_a_url_becomes_an_http_entry_and_a_word_becomes_a_command():
    """Told apart by SHAPE, the same rule `_build` uses — no flag to remember."""
    cfg, err = _entry_from_words(["http://localhost:7423/mcp"])
    assert err is None and cfg == {"type": "http", "url": "http://localhost:7423/mcp"}

    cfg, err = _entry_from_words(["npx", "-y", "@modelcontextprotocol/server-filesystem", "."])
    assert err is None
    assert cfg["command"] == "npx" and cfg["args"][-1] == "."


def test_a_json_blob_is_passed_through_for_env_and_cwd():
    cfg, err = _entry_from_words(['{"command": "python", "env": {"K": "v"}}'])
    assert err is None and cfg["env"] == {"K": "v"}


def test_broken_json_is_reported_not_swallowed():
    cfg, err = _entry_from_words(["{not json"])
    assert cfg is None and "valid JSON" in err


def test_a_url_with_trailing_words_is_refused():
    """`/mcp add x http://h/mcp --flag` is a mistake worth naming: the flag
    would be silently dropped, and the server would look misconfigured later."""
    cfg, err = _entry_from_words(["http://h/mcp", "--flag"])
    assert cfg is None and "no extra arguments" in err


# ── list ────────────────────────────────────────────────────────────────────
def test_mcp_list_reports_state_transport_and_tool_count(app, tmp_path):
    _config(tmp_path, {"web": {"url": "http://h/mcp"}, "off": {"command": "x", "disabled": True}})
    app.mcp.load()
    _cmd_mcp(app, "/mcp", "list")
    out = app.last
    assert "web" in out and "connected" in out and "http" in out
    assert "off" in out and "disabled" in out
    assert "2 server(s)" in out


def test_an_empty_config_says_how_to_add_one(app, tmp_path):
    _cmd_mcp(app, "/mcp", "list")
    assert "No MCP servers declared" in app.last
    assert "/mcp add" in app.last


def test_list_does_not_rebuild_dispatch(app, tmp_path):
    """Reading is not mutating. A list that rebuilt would make `/mcp` a command
    with a side effect, and hide a missing rebuild elsewhere behind it."""
    _config(tmp_path, {"a": {"command": "x"}})
    app.mcp.load()
    _cmd_mcp(app, "/mcp", "list")
    assert app.rebuilds == 0


# ── the verbs, and the rebuild ──────────────────────────────────────────────
@pytest.mark.parametrize("verb", ["connect", "disconnect", "reconnect"])
def test_every_lifecycle_verb_rebuilds_the_dispatch_map(app, tmp_path, verb):
    """🔴 THE LOAD-BEARING ASSERTION OF THIS FILE. See the module docstring."""
    _config(tmp_path, {"a": {"command": "x"}})
    app.mcp.load()
    _cmd_mcp(app, "/mcp", f"{verb} a")
    assert app.rebuilds == 1


def test_add_and_remove_rebuild_too(app, tmp_path):
    _cmd_mcp(app, "/mcp", "add a http://h/mcp")
    assert app.rebuilds == 1
    _cmd_mcp(app, "/mcp", "remove a")
    assert app.rebuilds == 2


def test_connect_reports_the_error_rather_than_a_bare_failure(app, tmp_path):
    _config(tmp_path, {"a": {"command": "x", "_fail": True}})
    app.mcp.reload_configs()
    _cmd_mcp(app, "/mcp", "connect a")
    assert "Could not connect" in app.last and "boom" in app.last


def test_disconnect_says_it_stays_declared(app, tmp_path):
    """The message has to answer "is it gone?" — because the answer is no, and
    the difference between disconnect and remove is the whole safety of one."""
    _config(tmp_path, {"a": {"command": "x"}})
    app.mcp.load()
    _cmd_mcp(app, "/mcp", "disconnect a")
    assert "stays declared" in app.last and "/mcp connect a" in app.last


def test_disconnecting_something_not_running_says_so(app, tmp_path):
    _config(tmp_path, {"a": {"command": "x"}})
    app.mcp.reload_configs()
    _cmd_mcp(app, "/mcp", "disconnect a")
    assert "was not running" in app.last


def test_reconnect_rereads_the_config_first(app, tmp_path):
    """⚠️ THE REASON PEOPLE RECONNECT IS THAT THEY JUST EDITED THE FILE.

    Reconnecting to the entry loaded at boot would start the OLD command and
    look like the edit did nothing — the exact complaint that
    "Applies on next /reconnect" was already generating.
    """
    _config(tmp_path, {"a": {"command": "old"}})
    app.mcp.load()
    _config(tmp_path, {"a": {"command": "new"}})
    _cmd_mcp(app, "/mcp", "reconnect a")
    assert app.mcp.servers["a"].cfg["command"] == "new"


# ── add / remove through the command ────────────────────────────────────────
def test_add_writes_and_connects(app, tmp_path):
    _cmd_mcp(app, "/mcp", "add files npx -y srv")
    doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert doc["mcpServers"]["files"]["args"] == ["-y", "srv"]
    assert "files" in app.mcp.servers
    assert "Added and connected" in app.last


def test_add_that_fails_to_start_still_reports_the_declaration(app, tmp_path):
    """Both halves in one sentence: the write happened, the start did not.

    A bare "failed" would hide the write, and the user's next act would be to
    add it again — which now refuses as a duplicate, for a reason they were
    never told about.
    """
    _cmd_mcp(app, "/mcp", 'add bad {"command": "x", "_fail": true}')
    assert "Declared" in app.last and "did not start" in app.last
    assert "bad" in app.mcp.configs


def test_remove_reports_which_file_it_touched(app, tmp_path):
    _cmd_mcp(app, "/mcp", "add gone http://h/mcp")
    _cmd_mcp(app, "/mcp", "remove gone")
    assert ".mcp.json" in app.last
    assert "gone" not in app.mcp.configs


def test_an_unknown_verb_shows_the_usage(app, tmp_path):
    _cmd_mcp(app, "/mcp", "frobnicate a")
    assert "Unknown /mcp verb" in app.last and "/mcp connect" in app.last


def test_a_verb_without_a_name_shows_its_usage(app, tmp_path):
    _cmd_mcp(app, "/mcp", "connect")
    assert "Usage: /mcp connect <name>" in app.last


# ── registration ────────────────────────────────────────────────────────────
def test_the_command_is_registered_under_slash_mcp():
    """A handler nothing routes to is dead code; this pins the token."""
    seen = {}

    class Ctx:
        def command(self, tokens, handler, **kw):
            seen[tokens] = handler

    mcp_manage._register(Ctx())
    assert ("/mcp",) in seen
    assert seen[("/mcp",)] is _cmd_mcp
