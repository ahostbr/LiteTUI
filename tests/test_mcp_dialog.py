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
    # Drive the inbox seat monitor to its (wrapped, idle) poll/terminal phase at
    # once so a mutation gate can reach genuine idle without blocking the suite
    # on the real 2s settle. _connect is stubbed below, so there is nothing to
    # settle — the wait's condition is already met.
    monkeypatch.setattr("litetui.app._INBOX_SETTLE_S", 0.0)
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
    """Let the action's async re-render land."""
    for _ in range(n):
        await pilot.pause()


async def _reach_idle(app, pilot, body, tries: int = 120):
    """Advance to a GENUINELY idle app: materialise the staged conversation (the
    real born step — store.pending stays a blocker, so we clear it honestly, not
    by faking the flag) and pause until the boot infra (inbox seat monitor) has
    left its blocking registration and the dialog's own gate is clear."""
    app._materialise_convo()
    for _ in range(tries):
        if body._gate(app) is None:
            return
        await pilot.pause()
    raise AssertionError("app never reached idle; gate says: " + str(body._gate(app)))


async def _drain(app, pilot, tries: int = 120):
    """Wait for a dispatched dialog mutation to settle (its worker clears
    _mcp_maintenance in _settle_maintenance), then a couple ticks for the
    completion re-render."""
    for _ in range(tries):
        await pilot.pause()
        if not getattr(app, "_mcp_maintenance", False):
            await pilot.pause()
            await pilot.pause()
            return


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
        await _reach_idle(app, pilot, body)
        srv = app.mcp.servers["web"]
        assert body._rows[0]["state"] == "connected", "CONTROL: it starts connected"

        body.query_one("#mcp-act-0-disconnect").press()
        await _drain(app, pilot)

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

        await _reach_idle(app, pilot, body)
        body.query_one("#mcp-act-0-connect").press()
        await _drain(app, pilot)

        assert "web" in app.mcp.servers
        assert body._rows[0]["state"] == "connected"


@pytest.mark.asyncio
async def test_remove_deletes_the_entry_from_disk(monkeypatch, tmp_path):
    """The one irreversible action in the dialog, so it is followed all the way
    to the file rather than to the manager's in-memory view."""
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        await _reach_idle(app, pilot, body)
        body.query_one("#mcp-act-0-remove").press()
        await _drain(app, pilot)

        doc = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
        assert doc["mcpServers"] == {}
        assert body._rows == []


@pytest.mark.asyncio
async def test_add_writes_the_entry_and_connects_it(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, {})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        await _reach_idle(app, pilot, body)
        body.query_one("#mcp-add-name").value = "files"
        body.query_one("#mcp-add-target").value = "npx -y srv"
        body.query_one("#mcp-add-go").press()
        await _drain(app, pilot)

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
        await _reach_idle(app, pilot, body)
        body.query_one("#mcp-add-name").value = "bad"
        body.query_one("#mcp-add-target").value = '{"command": "x", "_fail": true}'
        body.query_one("#mcp-add-go").press()
        await _drain(app, pilot)

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



# ── the narrow idle-infra classification, proven in the real mounted app ──────
@pytest.mark.asyncio
async def test_gate_blocks_before_the_conversation_is_materialised(monkeypatch, tmp_path):
    """A fresh app's staged conversation is unborn (store.pending) — the gate
    MUST block until boot completes, and the button must not mutate."""
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        assert body._gate(app) is not None            # blocked: store still pending
        body.query_one("#mcp-act-0-disconnect").press()
        await _settle(pilot)
        assert app.mcp.servers["web"].stopped == 0     # did NOT reach the manager


@pytest.mark.asyncio
async def test_idle_background_phases_allow_a_mutation(monkeypatch, tmp_path):
    """cron.monitor and the inbox poll are in their WRAPPED idle phase and the
    conversation is materialised — the gate is clear even though those infra
    workers exist and are nonterminal."""
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        await _reach_idle(app, pilot, body)
        assert body._gate(app) is None


@pytest.mark.asyncio
async def test_a_different_mcp_worker_blocks_the_dialog(monkeypatch, tmp_path):
    """The dialog excludes only its OWN host worker. A DIFFERENT mcp-group
    worker (not idle-registered) still blocks — maintenance alone does not
    replace that signal."""
    import asyncio
    app = _app(monkeypatch, tmp_path, {"web": {"url": "http://h/mcp"}})
    async with app.run_test(size=(120, 45)) as pilot:
        body = await _open(app, pilot)
        await _reach_idle(app, pilot, body)
        assert body._gate(app) is None                # control: idle
        release = asyncio.Event()

        async def _busy():
            await release.wait()

        app.run_worker(_busy(), group="mcp", exclusive=False)  # a foreign mcp worker
        await pilot.pause()
        assert body._gate(app) is not None            # it blocks
        release.set()
        await pilot.pause()


# ── _own_modal_screen: exclude only a REAL owned modal hosting this body ───────
from types import SimpleNamespace as _NS


def test_own_modal_screen_none_when_docked_on_base():
    base = object()
    body = _NS(screen=base, app=_NS(screen_stack=[base]))
    assert MCPListBody._own_modal_screen(body) is None      # docked → not ours to exclude


def test_own_modal_screen_returns_the_modal_that_hosts_this_body():
    base = object()
    class Scr:
        def walk_children(self, with_self=False):
            return [self, body]
    body = _NS()
    modal = Scr()
    body.screen = modal
    body.app = _NS(screen_stack=[base, modal])
    assert MCPListBody._own_modal_screen(body) is modal


def test_own_modal_screen_none_for_a_foreign_overlay_not_hosting_body():
    base = object()
    class Scr:
        def walk_children(self, with_self=False):
            return [self]                                   # does NOT contain body
    body = _NS()
    body.screen = Scr()
    body.app = _NS(screen_stack=[base, body.screen])
    assert MCPListBody._own_modal_screen(body) is None      # containment mismatch → excluded


@pytest.mark.asyncio
async def test_run_action_uses_the_passed_app_not_self_app():
    """The widget can be removed before the coro's first step; _run_action must
    use the app captured at scheduling, never self.app (which would raise)."""
    import asyncio
    calls = []
    app = _NS(_mcp_maintenance=True, _mcp_maintenance_done=asyncio.Event(),
              convo_id="c1", backend=object(), system_message=calls.append,
              rebuild_mcp_dispatch=lambda: None)

    class Detached:
        is_mounted = False
        @property
        def app(self):
            raise RuntimeError("NoActiveApp")

    await MCPListBody._run_action(Detached(), app, lambda: None, lambda r: "done",
                                  "c1", app.backend)
    assert calls and "done" in calls[0]           # reported via the passed app
    assert app._mcp_maintenance is False           # settled, not stranded
