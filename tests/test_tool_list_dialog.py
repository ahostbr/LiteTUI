"""The `/tools` dialog — the surface over T076's mechanism.

Ryan asked for three things and each has a test that fails if it regresses:
a full list of the tools, a checkbox each, and ONE global toggle that is the
same switch as the settings page.

🔴 THE POINT OF THE FILE IS THAT THE CHECKBOXES ARE WIRED TO THE ENGINE, not to
themselves. Every UI assertion here is followed through to `tool_specs()` or to
`settings.tools_disabled` — a dialog whose boxes tick beautifully and change
nothing is the exact defect Ryan asked us to remove, and it would pass any test
that only looked at the widgets.
"""
from __future__ import annotations

import pytest

from litetui.app import LiteTUI
from litetui.side_panel import show_dialog
from litetui.tool_list import ToolListBody, tool_rows
from litetui.tool_policy import READ_POLICY, WRITE_POLICY


def _spec(name, desc="a test tool"):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": desc,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _app(monkeypatch):
    app = LiteTUI()
    app._connect = lambda: None
    app.settings.tools_disabled = []
    monkeypatch.setattr("litetui.tool_list.settings_mod.save", lambda s, root=None: None)
    monkeypatch.setattr("litetui.app.settings_mod.save", lambda s, root=None: None)
    return app


# ── the rows ───────────────────────────────────────────────────────────────

def test_every_registered_tool_gets_a_row(monkeypatch):
    """Derived from the live registry, so a tool added by a plugin appears with
    no second list to update — the same reasoning as the profile dropdown."""
    app = _app(monkeypatch)
    registered = [e.name for e in app.plugins.tools]
    assert [r["name"] for r in tool_rows(app)] == registered
    assert len(registered) > 5, "the harness registered almost nothing; rows prove little"


def test_a_row_states_the_authority_so_gating_is_explicable(monkeypatch):
    app = _app(monkeypatch)
    by_name = {r["name"]: r for r in tool_rows(app)}
    assert "process_execution" in by_name["bash"]["authority"]
    assert by_name["read"]["authority"] == "read_only"


def test_a_tool_whose_authority_depends_on_ARGUMENTS_says_so(monkeypatch):
    """`write` declares NOTHING statically — workspace_write vs external_write
    is decided by the path at call time. An empty string here would read as
    'this tool has no authority', which is the opposite of true."""
    app = _app(monkeypatch)
    assert WRITE_POLICY.capabilities == frozenset(), "premise changed; retest"
    row = {r["name"]: r for r in tool_rows(app)}["write"]
    assert row["authority"] == "set by the arguments"
    assert row["authority"].strip()


def test_a_row_carries_the_MODEL_S_description_not_a_second_copy(monkeypatch):
    """The description shown is the schema's own, so it cannot drift from what
    the model actually reads."""
    app = _app(monkeypatch)
    app.plugins.add_tool("t", _spec("probe", "the exact schema text"), lambda a: "x",
                         None, READ_POLICY)
    row = {r["name"]: r for r in tool_rows(app)}["probe"]
    assert row["description"] == "the exact schema text"


# ── the wiring: a checkbox must reach the engine ───────────────────────────

@pytest.mark.asyncio
async def test_unticking_a_box_withholds_the_schema_AND_persists(monkeypatch):
    app = _app(monkeypatch)
    app.plugins.add_tool("t", _spec("probe"), lambda a: "x", None, READ_POLICY)
    async with app.run_test(size=(120, 45)) as pilot:
        app.run_worker(show_dialog(app, ToolListBody), group="tl")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query("ToolListBody"):
                break
        assert app.screen.query("ToolListBody"), "the dialog never mounted"

        box = app.screen.query_one("#tl-tool-probe")
        assert box.value is True, "CONTROL: it starts ticked"
        assert "probe" in [s["function"]["name"] for s in app.plugins.tool_specs()]

        box.value = False
        await pilot.pause()

        assert app.settings.tools_disabled == ["probe"], "the box did not reach settings"
        assert "probe" not in [
            s["function"]["name"] for s in app.plugins.tool_specs()
        ], "the box ticked but the schema still reached the model"

        box.value = True
        await pilot.pause()
        assert app.settings.tools_disabled == [], "re-ticking did not restore it"
        assert "probe" in [s["function"]["name"] for s in app.plugins.tool_specs()]


@pytest.mark.asyncio
async def test_an_already_disabled_tool_shows_UNTICKED(monkeypatch):
    """The dialog reads the same field it writes, so it cannot open showing a
    state that disagrees with the engine."""
    app = _app(monkeypatch)
    app.plugins.add_tool("t", _spec("probe"), lambda a: "x", None, READ_POLICY)
    app.settings.tools_disabled = ["probe"]
    async with app.run_test(size=(120, 45)) as pilot:
        app.run_worker(show_dialog(app, ToolListBody), group="tl")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query("ToolListBody"):
                break
        assert app.screen.query_one("#tl-tool-probe").value is False
        assert app.screen.query_one("#tl-tool-read").value is True, "CONTROL: others still ticked"


# ── the global switch: one verb, not a copy of it ──────────────────────────

@pytest.mark.asyncio
async def test_the_global_switch_calls_the_SAME_verb_as_ctrl_T(monkeypatch):
    """Not a hand-rolled `tools_enabled = x`. `action_toggle_tools` also
    rebuilds the system prompt, updates the header, tells the user and
    persists — four things a copy would silently skip."""
    app = _app(monkeypatch)
    async with app.run_test(size=(120, 45)) as pilot:
        app.tools_enabled = True
        app.settings.tools_enabled = True
        app.conversation = [{"role": "system", "content": "seed"}]

        app.run_worker(show_dialog(app, ToolListBody), group="tl")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query("ToolListBody"):
                break

        app.screen.query_one("#tl-global-switch").value = False
        await pilot.pause()

        assert app.tools_enabled is False
        assert app.settings.tools_enabled is False, "the switch did not persist"
        # the tell that the real verb ran rather than a copy of the flag write
        assert app.conversation[0]["content"] != "seed", (
            "the system prompt was not rebuilt — a hand-rolled flag write, not the verb"
        )


@pytest.mark.asyncio
async def test_opening_the_dialog_does_not_itself_toggle_anything(monkeypatch):
    """The switch is constructed with the CURRENT value. If that construction
    fired Changed, merely opening /tools would flip tools off — and the old
    /tools DID toggle, so that regression would look like the previous
    behaviour and be easy to excuse."""
    app = _app(monkeypatch)
    async with app.run_test(size=(120, 45)) as pilot:
        app.tools_enabled = True
        app.settings.tools_enabled = True
        before = list(app.settings.tools_disabled)

        app.run_worker(show_dialog(app, ToolListBody), group="tl")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query("ToolListBody"):
                break
        for _ in range(5):
            await pilot.pause()

        assert app.tools_enabled is True, "opening the list switched tools OFF"
        assert app.settings.tools_enabled is True
        assert app.settings.tools_disabled == before, "opening the list changed the denylist"


@pytest.mark.asyncio
async def test_the_command_shows_the_list_and_no_longer_toggles(monkeypatch):
    """Ryan: 'remove the func of /tools switching on and off and make it show
    this list please.' The old behaviour flipping back on is the regression."""
    from litetui.plugins.misc import _cmd_tools

    app = _app(monkeypatch)
    async with app.run_test(size=(120, 45)) as pilot:
        app.tools_enabled = True
        _cmd_tools(app, "/tools", "")
        for _ in range(20):
            await pilot.pause()
            if app.screen.query("ToolListBody"):
                break
        assert app.screen.query("ToolListBody"), "/tools did not open the list"
        assert app.tools_enabled is True, "/tools still toggled — it must only show"
