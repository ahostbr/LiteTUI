"""T1306-D: message 0 and the tools array on a prefix-cache backend (audit C:/Projects/.scratch/prefix-audit/REPORT.md, P5 + P7).

P5. The DEFERRED_TOOLS names list in message 0 and the `tools` array were recomposed on every request, so the first
`tool_search` hit, the first by-name call of a deferred tool, a lazy MCP server starting or a tool whose gate opened after
the first request changed both. The server then re-reads everything from that point (measured on NInfer instance 171605,
2026-11-11 11:50: tools 22 -> 23 between requests 1 and 3, 21k tokens re-read in 24 s). On a prefix-cache backend both are
now frozen per conversation: message 0 keeps the list it was first sent with, a tool activated later is APPENDED to the
array in activation order, and a tool that only became available later is withheld until it is activated (tool_search or
a call by name), exactly like a deferred tool.

P7. The MEMORY section (this conversation's id and store path) was the first per-conversation byte, at slot 10, ahead of
TOOLS, DEFERRED_TOOLS and SKILLS_INDEX. Every other conversation or seat on the same server diverged there (llama.cpp
forced full re-reads at lcp 3339 / 3808 / 4935; NInfer `/new` re-read its 17.4k system+tools prefix). It now follows them.

Every test builds the bytes the app would send: TurnEngine.chat_request with the same arguments the stream worker passes
(app.py, `tools=self._remember_chat_tools()`), serialised with json.dumps as the OpenAI client does. Each fix has a
CONTROL arm that shows the measurement can see the old behaviour.
"""
import json

import pytest
from _legacy_slot import compose_at_legacy_slot
from test_owner_resume import build as _build

from litetui import skills as skills_mod
from litetui.plugins import PROMPT_ORDER
from litetui.turn_engine import TurnEngine

build = _build

STORE_HEADER = "## Your conversation store"


def _spec(name: str, description: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": description,
                                             "parameters": {"type": "object", "properties": {}}}}


def _wire(a) -> tuple[str, list[str]]:
    """Message 0 and each advertised tool, serialised as they leave the process."""
    kw = TurnEngine.chat_request(
        model_id=a.model_id, messages=a._request_messages(), tools_enabled=a.tools_enabled,
        max_tokens_tools=a.settings.max_tokens_tools, max_tokens_chat=a.settings.max_tokens_chat,
        request_overrides=a._effective_request_overrides(), thinking_level=a.thinking_level,
        tools=a._remember_chat_tools(), backend_name=a.backend.name,
        graded_thinking_models=a.settings.lmstudio_graded_thinking_models)
    return (json.dumps(kw["messages"][0], ensure_ascii=False),
            [json.dumps(t, ensure_ascii=False) for t in kw["tools"]])


def _name(serialised: str) -> str:
    return json.loads(serialised)["function"]["name"]


class _Probe:
    """A fake MCP provider: deferred like every dynamic tool, plus a lazy (not running) server list."""

    def __init__(self, a):
        self.specs = [_spec("mcp__probe__alpha", "alpha probe"), _spec("mcp__probe__beta", "beta probe")]
        self.lazy = ["coldserver"]
        a.plugins.add_dynamic("mcp-probe", lambda: list(self.specs),
                              lambda n: (lambda args: "ok") if n in {s["function"]["name"] for s in self.specs} else None)
        a.mcp.lazy_names = lambda: list(self.lazy)


def _seat(build, *, prefix_cache: bool, late_gate: list | None = None):
    a = build(None)
    a._system = lambda *_: None
    # Pinned both ways (T1306 integration): since T1306-C the predicate reads the `prefix_cache_backends` setting, whose
    # default lists the fake seat's local backend, so "no prefix cache" must be said, not assumed. A prefix-cache seat
    # models NInfer, as these tests were written: built-in deferred tools resident from request 1.
    a._prefix_cache_backend = lambda: prefix_cache
    a._resident_builtins_backend = lambda: prefix_cache
    probe = _Probe(a)
    if late_gate is not None:
        a.plugins.add_tool("late-probe", _spec("late_probe", "a late gated probe"), lambda args: "late ok",
                           gate=lambda: late_gate[0])
    a.skills = [skills_mod.Skill(name="probe-skill", description="a probe", path=None, source="probe")]
    a._materialise_convo()
    assert a.tools_enabled
    return a, probe


# ── P5: activation ────────────────────────────────────────────────────────────

def test_p5_a_tool_search_activation_keeps_message_zero_and_appends_the_tool(build):
    a, _ = _seat(build, prefix_cache=True)
    m0_first, tools_first = _wire(a)
    assert "mcp__probe__<tool>: alpha, beta" in m0_first, "precondition: the deferred names ride in message 0"

    assert a.plugins.search_tools("select:mcp__probe__beta"), "tool_search loads beta"
    m0_second, tools_second = _wire(a)

    assert m0_second == m0_first, "message 0 must be byte-identical after an activation"
    assert tools_second[:len(tools_first)] == tools_first, "the advertised prefix must not move"
    assert [_name(t) for t in tools_second[len(tools_first):]] == ["mcp__probe__beta"]


def test_p5_activations_append_in_activation_order_not_registration_order(build):
    a, _ = _seat(build, prefix_cache=True)
    _, tools_first = _wire(a)
    a.plugins.search_tools("select:mcp__probe__beta")
    _wire(a)
    assert a.plugins.dispatch_for("mcp__probe__alpha") is not None, "a call by name activates alpha"
    m0_third, tools_third = _wire(a)
    assert tools_third[:len(tools_first)] == tools_first
    assert [_name(t) for t in tools_third[len(tools_first):]] == ["mcp__probe__beta", "mcp__probe__alpha"]


def test_p5_control_without_a_prefix_cache_message_zero_follows_the_activation(build):
    a, _ = _seat(build, prefix_cache=False)
    m0_first, _ = _wire(a)
    a.plugins.search_tools("select:mcp__probe__beta")
    m0_second, tools_second = _wire(a)
    assert m0_second != m0_first, "CONTROL: the live list drops beta, so the measurement does see a change"
    assert "mcp__probe__<tool>: alpha" in m0_second and "beta" not in m0_second.split("## Deferred tools")[1]
    assert "mcp__probe__beta" in [_name(t) for t in tools_second]


def test_p5_a_lazy_server_starting_does_not_rewrite_message_zero(build):
    a, probe = _seat(build, prefix_cache=True)
    m0_first, tools_first = _wire(a)
    assert "Lazy MCP servers (not running): coldserver" in m0_first
    probe.lazy = []                                              # the server started...
    probe.specs.append(_spec("mcp__coldserver__gamma", "gamma"))  # ...and its tools landed, deferred
    m0_second, tools_second = _wire(a)
    assert m0_second == m0_first and tools_second == tools_first
    assert any(s["function"]["name"] == "mcp__coldserver__gamma" for s in a.plugins.search_tools("gamma")), \
        "a tool that landed later is still found by tool_search"


def test_p5_the_frozen_block_says_a_listed_tool_may_already_be_loaded(build):
    a, _ = _seat(build, prefix_cache=True)
    m0, _ = _wire(a)
    assert "may already be loaded" in m0.split("## Deferred tools")[1]


# ── P5: a tool that becomes available after request 1 ─────────────────────────

def test_p5_a_late_tool_is_withheld_then_found_by_tool_search_and_appended(build):
    gate = [False]
    a, _ = _seat(build, prefix_cache=True, late_gate=gate)
    m0_first, tools_first = _wire(a)
    assert "late_probe" not in m0_first

    gate[0] = True                                   # e.g. view_image once model_type stops reading "llm"
    m0_second, tools_second = _wire(a)
    assert (m0_second, tools_second) == (m0_first, tools_first), "a late tool must not change a started conversation"

    hits = [s["function"]["name"] for s in a.plugins.search_tools("late gated")]
    assert hits == ["late_probe"], "the withheld tool stays discoverable through tool_search"
    m0_third, tools_third = _wire(a)
    assert m0_third == m0_first
    assert tools_third[:len(tools_first)] == tools_first and [_name(t) for t in tools_third[len(tools_first):]] == ["late_probe"]


def test_p5_a_late_tool_called_by_name_is_appended(build):
    gate = [False]
    a, _ = _seat(build, prefix_cache=True, late_gate=gate)
    _, tools_first = _wire(a)
    gate[0] = True
    assert a.plugins.dispatch_for("late_probe") is not None, "dispatch by name still reaches it"
    _, tools_second = _wire(a)
    assert tools_second[:len(tools_first)] == tools_first and _name(tools_second[-1]) == "late_probe"


def test_p5_view_image_arriving_late_does_not_change_the_array(build):
    """The 22 -> 23 case. By elimination the late tool was view_image (gate: model_type != "llm"): message 0 of that
    conversation (4c192911) shows the seat registered (harness) and the skills index present (skill) at request 1."""
    a, _ = _seat(build, prefix_cache=True)
    a.model_type = "llm"
    _, tools_first = _wire(a)
    assert "view_image" not in [_name(t) for t in tools_first]
    a.model_type = None                                  # NInfer's model_info reports no type
    _, tools_second = _wire(a)
    assert tools_second == tools_first


def test_p5_control_view_image_arriving_late_changes_the_array_without_a_prefix_cache(build):
    a, _ = _seat(build, prefix_cache=False)
    a.model_type = "llm"
    _, tools_first = _wire(a)
    a.model_type = None
    _, tools_second = _wire(a)
    assert len(tools_second) == len(tools_first) + 1, "CONTROL: the 22 -> 23 shape the NInfer log recorded"


# ── P5: what resets the freeze and what does not ──────────────────────────────

def test_p5_a_rebuild_of_message_zero_takes_a_fresh_snapshot(build):
    a, _ = _seat(build, prefix_cache=True)
    _, tools_first = _wire(a)
    a.plugins.search_tools("select:mcp__probe__beta")
    _wire(a)
    a.action_toggle_tools()
    a.action_toggle_tools()                              # Ctrl+T twice: message 0 rebuilt by the single builder
    m0, tools = _wire(a)
    deferred = m0.split("## Deferred tools")[1]
    assert "alpha" in deferred and "beta" not in deferred, "the rebuilt list reads the live deferral"
    names = [_name(t) for t in tools]
    assert names.index("mcp__probe__beta") == len(names) - 1


def test_p5_a_new_conversation_takes_a_fresh_snapshot(build):
    gate = [False]
    a, _ = _seat(build, prefix_cache=True, late_gate=gate)
    _wire(a)
    gate[0] = True
    a.conversation.clear()
    a._new_convo()
    a._load_system_prompt()
    a._materialise_convo()
    _, tools = _wire(a)
    assert "late_probe" in [_name(t) for t in tools], "a new conversation advertises what is available now"


# ── P7: per-conversation identity last ────────────────────────────────────────

def _common(x: str, y: str) -> int:
    n = 0
    for p, q in zip(x, y):
        if p != q:
            break
        n += 1
    return n


def _two_conversations(a) -> tuple[str, str]:
    first = a._compose_system_text()
    a.conversation.clear()
    a._new_convo()
    a._load_system_prompt()
    a._materialise_convo()
    return first, a._compose_system_text()


def test_p7_two_conversations_share_the_prefix_through_the_skills_index(build):
    a, _ = _seat(build, prefix_cache=True)
    one, two = _two_conversations(a)
    assert one != two, "precondition: the two conversations have different ids"
    for text in (one, two):
        header = text.index(STORE_HEADER)
        assert text.index("## Deferred tools") < header and text.index("probe-skill") < header, \
            "TOOLS, DEFERRED_TOOLS and SKILLS_INDEX come before the store block"
    assert _common(one, two) >= max(one.index(STORE_HEADER), two.index(STORE_HEADER))


def test_p7_control_the_old_slot_diverges_before_the_tools_section(build):
    a, _ = _seat(build, prefix_cache=True)
    _put_memory_back(a)
    one, two = _two_conversations(a)
    assert _common(one, two) < one.index("## Deferred tools"), \
        "CONTROL: at slot 10 the conversation id splits the two prompts ahead of the tools text"


def test_p7_memory_slot_is_after_skills_index():
    assert PROMPT_ORDER["MEMORY"] > PROMPT_ORDER["SKILLS_INDEX"] > PROMPT_ORDER["DEFERRED_TOOLS"] > PROMPT_ORDER["TOOLS"]
    assert len(set(PROMPT_ORDER.values())) == len(PROMPT_ORDER)


def _put_memory_back(a) -> None:
    """Compose as develop 2310d1f5 did: the host MEMORY section at slot 10."""
    compose_at_legacy_slot(a)


# ── Resume: a v2 conversation composed in the old order ───────────────────────

def test_resume_of_a_v2_conversation_saved_in_the_old_order(build):
    first, _ = _seat(build, prefix_cache=True)
    _put_memory_back(first)
    first.conversation.clear()
    first._load_system_prompt()
    first._request_messages()
    first._append({"role": "user", "content": "PRIOR USER"})
    first._snapshot("v2 snapshot in the old section order")
    saved = first.conversation[0]
    assert saved["litetui_prompt_version"] == 2
    old_base = saved["litetui_prompt_base"]
    assert old_base.index(STORE_HEADER) < old_base.index("## Deferred tools"), "precondition: saved in the old order"
    suffix = saved["content"][len(old_base):]
    path = first.convo_path
    first.store.release()
    first._agent_session.release()

    resumed = build(None)
    resumed._system = lambda *_: None
    resumed._render_resumed = lambda _: None
    resumed._prefix_cache_backend = lambda: True
    assert resumed._resume(path, startup=True)
    outgoing = resumed._request_messages()            # no PromptError
    m0 = outgoing[0]["content"]
    assert m0.endswith(suffix), "the frozen index/lore/store suffix is kept"
    tools_text = resumed._ordinary_prompt_text("tools").strip()[:80]
    assert tools_text and m0.index(tools_text) < m0.index(STORE_HEADER), "the prefix is recomposed in the new order"
    assert outgoing[1] == {"role": "user", "content": "PRIOR USER"}


@pytest.mark.asyncio
async def test_p5_the_snapshot_survives_a_compaction(monkeypatch):
    from test_compaction_ui import _app as compaction_app
    from test_ninfer_prefix_cache import _capture
    from test_wake_after_compact import _settle

    a = compaction_app()                                  # a seat whose fake backend serves the model
    a._prefix_cache_backend = lambda: True
    _Probe(a)
    a._materialise_convo()
    for i in range(3):
        a._append({"role": "user", "content": f"question {i}"})
        a._append({"role": "assistant", "content": f"answer {i}"})
    _wire(a)
    a.plugins.search_tools("select:mcp__probe__beta")
    m0_before, tools_before = _wire(a)
    seen = _capture(monkeypatch)
    said = []
    a._system = lambda text, *_, **__: said.append(str(text))
    async with a.run_test() as pilot:
        a._compact()
        await _settle(a, pilot)
        assert any(purpose == "compaction" for purpose, _ in seen), f"precondition: the compaction ran; app said {said}"
        assert len(a.conversation) < 8, "precondition: the history was folded"
        a.plugins.search_tools("select:mcp__probe__alpha")
        m0_after, tools_after = _wire(a)
    # A reset at compaction would re-capture the names list without beta and alpha; live composition would too.
    assert m0_after == m0_before, "compaction keeps message 0, so the snapshot stays"
    assert tools_after[:len(tools_before)] == tools_before and _name(tools_after[-1]) == "mcp__probe__alpha"
