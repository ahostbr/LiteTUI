"""The `/mcp` dialog — the surface over the lifecycle verbs.

🔴 THE POINT OF THIS FILE IS THAT THE BUTTONS REACH THE MANAGER, not themselves.
A dialog whose rows render beautifully and change nothing is the exact defect
the `/tools` list was rewritten to remove, and it passes any test that only
looks at widgets. So every UI assertion here is followed through to
`app.mcp.servers` — what is actually running — or to `.mcp.json` on disk.

The second thing pinned here is the re-render. The dialog holds no state of its
own about a server, so after an action the rows must be rebuilt from
`describe()`. A dialog that acted correctly and then went on showing the old
state would be worse than one that did nothing: the user would press Connect
again on a row that says "stopped" and is not.
"""
from __future__ import annotations

import json

import pytest

from litetui.app import LiteTUI
from litetui.mcp_client import MCPManager
from litetui.mcp_list import ACTIONS_FOR, MCPListBody
from litetui.side_panel import show_dialog

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


def _app(monkeypatch, tmp_path, servers):
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    monkeypatch.setattr(
        MCPManager,
        "_build",
        lambda self, name, sc: StubServer(name, sc, self.root, None, bool(sc.get("_fail"))),
    )
    app = LiteTUI()
    app._connect = lambda: None
    # Point the manager at the tmp repo root; the app built its own against the
    # real one during __init__, which must not be written to by a test.
    app.mcp = MCPManager(tmp_path)
    app.mcp.load()
    app.rebuild_mcp_dispatch()
    return app


async def _open(app, pilot):
    app.run_worker(show_dialog(app, MCPListBody), group="mcp")
    for _ in range(20):
        await pilot.pause()
        if app.screen.query("MCPListBody"):
            break
    assert app.screen.query("MCPListBody"), "the dialog never mounted"
    return app.screen.query_one(MCPListBody)


async def _settle(pilot, n: int = 8):
    """Let the action's async re-render land.

    The button handler is `async`: pressing posts a message, the handler runs on
    a later tick, and only then does `_rerender()` rebuild the rows. A single
    pause() is enough for the manager call and NOT for the redraw, which is
    exactly the gap that would make these assertions flaky rather than wrong.
    """
    for _ in range(n):
        await pilot.pause()


# ── what a row offers ───────────────────────────────────────────────────────
def test_a_row_offers_only_actions_that_can_work():
    """A control that lies is worse than one that is visibly absent — the
    /tools doctrine. Remove is absent for an orphan because there is no
    declaration left to delete, and Connect is absent for something already
    connected."""
    assert "remove" not in ACTIONS_FOR["orphan"]
    assert "connect" not in ACTIONS_FOR["connected"]
    assert "disconnect" in ACTIONS_FOR["connected"]
    for state in ("stopped", "disabled", "failed"):
        assert "connect" in ACTIONS_FOR[state], state


@pytest.mark.asyncio
async def test_the_dialog_lists_every_declared_server_running_or_not(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, {
        "web": {"url": "http://h/mcp"},
        "off": {"command": "x", "disabled": True},
    })
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        names = [r["name"] for r in body._rows]
        assert names == ["web", "off"]
        assert [r["state"] for r in body._rows] == ["connected", "disabled"]


# ── the wiring: a button must reach the manager ─────────────────────────────
@pytest.mark.asyncio
async def test_disconnect_stops_the_real_server_and_the_row_updates(monkeypatch, tmp_path):
    """Both halves. Stopping without re-rendering leaves a row that says
    connected over a server that is not, and the next press acts on a lie."""
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        srv = app.mcp.servers["web"]
        assert body._rows[0]["state"] == "connected", "CONTROL: it starts connected"

        body.query_one("#mcp-act-0-disconnect").press()
        await _settle(pilot)

        assert srv.stopped == 1, "the button did not reach the manager"
        assert "web" not in app.mcp.servers
        assert body._rows[0]["state"] == "stopped", "the row did not re-render"


@pytest.mark.asyncio
async def test_connect_starts_it_again_from_the_same_dialog(monkeypatch, tmp_path):
    # ⚠️ THE DISCONNECT MOVED INSIDE `run_test`, AND ONLY THE SETUP CHANGED.
    # It used to run before mounting, which was equivalent while the boot
    # connect happened in `LiteTUI.__init__`. T239 moved that connect to an
    # `on_mount` worker, so a server disconnected BEFORE the mount is simply
    # reconnected by boot — correctly: boot connects what is declared and not
    # denied. The subject of this test is "connect starts it again from the
    # dialog"; how the row got to `stopped` is setup, and this is now the
    # honest way to express "the user disconnected it".
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        app.mcp.disconnect("web")
        await body._rerender()
        await _settle(pilot)
        assert body._rows[0]["state"] == "stopped"

        body.query_one("#mcp-act-0-connect").press()
        await _settle(pilot)

        assert "web" in app.mcp.servers
        assert body._rows[0]["state"] == "connected"


@pytest.mark.asyncio
async def test_remove_deletes_the_entry_from_disk(monkeypatch, tmp_path):
    """The one irreversible action in the dialog, so it is followed all the way
    to the file rather than to the manager's in-memory view."""
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        body.query_one("#mcp-act-0-remove").press()
        await _settle(pilot)

        doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        assert doc["mcpServers"] == {}
        assert body._rows == []


@pytest.mark.asyncio
async def test_add_writes_the_entry_and_connects_it(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, {})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        body.query_one("#mcp-add-name").value = "files"
        body.query_one("#mcp-add-target").value = "npx -y srv"
        body.query_one("#mcp-add-go").press()
        await _settle(pilot)

        doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        assert doc["mcpServers"]["files"]["args"] == ["-y", "srv"]
        assert "files" in app.mcp.servers
        assert [r["name"] for r in body._rows] == ["files"]


@pytest.mark.asyncio
async def test_add_without_a_name_says_so_and_writes_nothing(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, {})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        body.query_one("#mcp-add-target").value = "http://h/mcp"
        body.query_one("#mcp-add-go").press()
        await _settle(pilot)

        assert "name" in str(body.query_one("#mcp-status").render())
        assert not (tmp_path / ".mcp.json").read_text(encoding="utf-8").count("http://h/mcp")


@pytest.mark.asyncio
async def test_a_failed_add_still_shows_the_row_it_declared(monkeypatch, tmp_path):
    """The declaration survives a start failure, so the row has to appear —
    that row is how the user gets to Remove or fix it."""
    app = _app(monkeypatch, tmp_path, {})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        body.query_one("#mcp-add-name").value = "bad"
        body.query_one("#mcp-add-target").value = '{"command": "x", "_fail": true}'
        body.query_one("#mcp-add-go").press()
        await _settle(pilot)

        assert "did not start" in str(body.query_one("#mcp-status").render())
        assert [r["name"] for r in body._rows] == ["bad"]
        assert body._rows[0]["state"] == "failed"


@pytest.mark.asyncio
async def test_the_add_form_survives_a_host_swap(monkeypatch, tmp_path):
    """get_state/set_state carry the half-typed entry. Losing it on
    "Sidebar / popup" is the kind of small betrayal that stops people using
    the swap at all."""
    app = _app(monkeypatch, tmp_path, {})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        body.query_one("#mcp-add-name").value = "half"
        body.query_one("#mcp-add-target").value = "npx -y "
        state = body.get_state()

    fresh = MCPListBody()
    fresh.set_state(state)
    assert fresh._pending == ("half", "npx -y ")


# ── the buttons obey the same gate as the command (they must not bypass it) ────
@pytest.mark.asyncio
async def test_a_button_during_maintenance_is_refused_and_does_not_mutate(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        srv = app.mcp.servers["web"]
        app._mcp_maintenance = True                      # a reconcile is in flight
        body.query_one("#mcp-act-0-disconnect").press()
        await _settle(pilot)
        assert srv.stopped == 0                          # the manager was NOT touched
        assert "web" in app.mcp.servers
        assert "maintenance is in progress" in str(body.query_one("#mcp-status").render())


@pytest.mark.asyncio
async def test_a_button_on_native_codex_is_refused(monkeypatch, tmp_path):
    from types import SimpleNamespace
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        srv = app.mcp.servers["web"]
        app.backend = SimpleNamespace(app_server=object())   # native Codex thread
        body.query_one("#mcp-act-0-disconnect").press()
        await _settle(pilot)
        assert srv.stopped == 0
        assert "restart" in str(body.query_one("#mcp-status").render()).lower()
