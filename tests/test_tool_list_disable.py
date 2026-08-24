"""Per-tool disable: the boxes unticked in `/tools`.

Ryan: "there should be a full list of tools available to the model with
individual toggle checkmarks."

TWO MECHANISMS, AND NEITHER IS SUFFICIENT ALONE — which is why there are two
sets of controls here rather than one.

  1. THE SCHEMA IS WITHHELD from `tool_specs()`. `prompts/tools.md` deliberately
     does not enumerate tools, so the schemas ARE the model's only inventory and
     a withheld tool is one it never learns exists. Verified live against
     qwen3.8-27b that withholding a SUBSET does not reproduce T072's
     prose-markup failure: the model reasoned about its remaining inventory and
     refused in plain language.

  2. THE CALL IS REFUSED AT THE DOOR. `dispatch_for` does not consult gates —
     "dispatch has never been gated, only the offer is" — so a remembered name
     still reaches the host.

🔴 THE CASE THAT MAKES (2) NECESSARY AND NOT MERELY PRUDENT (Sentinel's, and
neither of us saw it first): a tool used earlier in THIS conversation and
switched off mid-session. Its call and its `role: "tool"` result are already in
the transcript, so withholding the schema removes it from the inventory while
leaving a WORKED EXAMPLE OF USING IT in the history — and a model imitating its
own transcript is precisely the T072 mechanism. Nothing about (1) reaches that.
`test_a_tool_disabled_MID_CONVERSATION_cannot_run_again` is that case.

Every "is absent / is refused" assertion is paired with the enabled control.
Without those, a build that offered NOTHING and refused EVERYTHING would pass
this whole file.
"""
from __future__ import annotations

import pytest

from litetui.app import LiteTUI
from litetui.plugins import PluginRegistry
from litetui.textfmt import tool_denied
from litetui.tool_policy import READ_POLICY


def _spec(name):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": f"the {name} tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _names(reg):
    return [s["function"]["name"] for s in reg.tool_specs()]


# ── mechanism 1: the schema is withheld ────────────────────────────────────

def test_a_disabled_tool_is_ABSENT_from_tool_specs():
    """Sentinel's named gate. Not 'unchecked in the UI' — absent from what
    reaches the model."""
    reg = PluginRegistry()
    reg.add_tool("t", _spec("alpha"), lambda a: "a", None, READ_POLICY)
    reg.add_tool("t", _spec("beta"), lambda a: "b", None, READ_POLICY)

    assert _names(reg) == ["alpha", "beta"]          # CONTROL: both offered
    reg.tools_disabled = lambda: frozenset({"alpha"})
    assert _names(reg) == ["beta"], "the disabled schema still reached the model"


def test_the_denylist_is_read_LIVE_not_snapshotted():
    """Unticking a box must take effect on the next request, not the next
    restart. A snapshot taken at construction cannot do that."""
    reg = PluginRegistry()
    reg.add_tool("t", _spec("alpha"), lambda a: "a", None, READ_POLICY)
    off = set()
    reg.tools_disabled = lambda: frozenset(off)

    assert _names(reg) == ["alpha"]
    off.add("alpha")                                  # no rebuild, no restart
    assert _names(reg) == []
    off.clear()
    assert _names(reg) == ["alpha"], "re-ticking did not restore the tool"


def test_dynamic_specs_honour_the_denylist_too():
    """MCP tools are normally governed per-SERVER and the list offers no
    per-MCP checkbox — but a name that lands in the denylist must be honoured
    wherever it came from. A denylist that silently ignores half its entries is
    worse than not having one."""
    reg = PluginRegistry()
    reg.add_dynamic("mcp", lambda: [_spec("mcp_one"), _spec("mcp_two")], lambda n: None)

    assert _names(reg) == ["mcp_one", "mcp_two"]      # CONTROL
    reg.tools_disabled = lambda: frozenset({"mcp_one"})
    assert _names(reg) == ["mcp_two"]


def test_an_unknown_name_in_the_denylist_hides_nothing():
    """A stale entry — a tool that was renamed or removed — must not take an
    unrelated tool with it, and must not raise."""
    reg = PluginRegistry()
    reg.add_tool("t", _spec("alpha"), lambda a: "a", None, READ_POLICY)
    reg.tools_disabled = lambda: frozenset({"ghost", "alpha_", "ALPHA"})
    assert _names(reg) == ["alpha"], "a near-miss name hid a real tool"


# ── mechanism 2: refused at the authorization door ─────────────────────────

def _app_with_probe():
    app = LiteTUI()
    app._connect = lambda: None
    app.settings.tools_disabled = []
    app.ran = []
    app.plugins.add_tool(
        "t", _spec("probe"), lambda a: app.ran.append(a) or "ran", None, READ_POLICY
    )
    return app


@pytest.mark.asyncio
async def test_a_disabled_tool_is_REFUSED_AT_DISPATCH_when_called_by_name():
    """Sentinel's second gate. The schema is gone, but the NAME still arrives —
    from a transcript, or a guess. It must not execute."""
    app = _app_with_probe()
    async with app.run_test(size=(100, 35)):
        ok_result, ok_flag = await app._execute_tool("probe", {})
        assert (ok_result, ok_flag) == ("ran", True), "CONTROL: enabled tool did not run"
        assert app.ran == [{}]

        app.settings.tools_disabled = ["probe"]
        result, flag = await app._execute_tool("probe", {})

    assert flag is False, "ok=True would let a caller record a write that never happened"
    assert result == tool_denied("tool-disabled", name="probe")
    assert "probe" in result and "{" not in result
    assert app.ran == [{}], "the disabled tool executed anyway"


@pytest.mark.asyncio
async def test_a_tool_disabled_MID_CONVERSATION_cannot_run_again():
    """🔴 THE HIGH-RISK CASE, AND IT IS THE MOST ORDINARY ONE.

    Ryan unticks something he just watched work. The call and its result are
    already in the transcript — a worked example the model can imitate — and
    withholding the schema does nothing about that. Only the door does.
    """
    app = _app_with_probe()
    async with app.run_test(size=(100, 35)):
        first, _ = await app._execute_tool("probe", {"q": 1})
        assert first == "ran"
        # exactly what the agent loop records, so the transcript really does
        # contain a worked example of the tool being used
        app.conversation.append(
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "probe", "arguments": "{}"}}]}
        )
        app.conversation.append({"role": "tool", "tool_call_id": "c1", "content": "ran"})

        app.settings.tools_disabled = ["probe"]

        # the model has seen it work; the schema is gone but the name is not
        assert "probe" not in [
            s["function"]["name"] for s in app.plugins.tool_specs()
        ], "the schema was still offered after the tool was switched off"
        second, flag = await app._execute_tool("probe", {"q": 2})

    assert flag is False
    assert "disabled" in second.lower()
    assert app.ran == [{"q": 1}], "a tool switched off mid-conversation ran again"


@pytest.mark.asyncio
async def test_disabling_one_tool_does_not_disable_the_others():
    """The control that stops 'refuse everything' from passing this file."""
    app = _app_with_probe()
    app.plugins.add_tool(
        "t", _spec("other"), lambda a: app.ran.append("other") or "other",
        None, READ_POLICY,
    )
    async with app.run_test(size=(100, 35)):
        app.settings.tools_disabled = ["probe"]
        refused, refused_flag = await app._execute_tool("probe", {})
        allowed, allowed_flag = await app._execute_tool("other", {})

    assert refused_flag is False and allowed_flag is True
    assert allowed == "other"
    # Membership, not equality: a real LiteTUI registers its 12 production
    # tools too, and pinning the whole list would fail on every tool added.
    offered = [s["function"]["name"] for s in app.plugins.tool_specs()]
    assert "probe" not in offered
    assert "other" in offered
    assert len(offered) > 1, "everything vanished — that is not this feature working"


@pytest.mark.asyncio
async def test_the_global_toggle_still_wins_over_the_per_tool_list():
    """Tools OFF refuses everything, regardless of which boxes are ticked —
    and it must say TOOLS ARE OFF, not 'that one tool is off', or the user is
    sent to fix the wrong control."""
    app = _app_with_probe()
    async with app.run_test(size=(100, 35)):
        app.tools_enabled = False
        app.settings.tools_disabled = []
        result, flag = await app._execute_tool("probe", {})

    assert flag is False
    assert result == tool_denied("tools-off")
    assert "Ctrl+T" in result, "the global refusal must name the global control"
    assert app.ran == []
