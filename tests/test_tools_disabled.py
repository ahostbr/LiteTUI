"""T073 — with tools OFF, a tool call must be REFUSED, not turned into prose.

THE BUG THIS FIXES. With `tools_enabled=False` the schemas were withheld, and
qwen3.8 then emitted literal tool-call markup as PLAIN REPLY TEXT and the turn
died there: the only tool handling anywhere is the structured
`delta.tool_calls` path, and nothing parses the text form. The conversation
history still carried earlier `tool_calls` and `role:"tool"` results from when
tools were ON, so the model was imitating its own transcript.

THE FIX, and the two halves are inseparable:
  1. the schemas are ADVERTISED even when the toggle is off, which keeps the
     model on the structured path;
  2. every call is then REFUSED at the authorization door, before resolution.

🔴 WHAT MUST NOT REGRESS INTO A "FIX": the disabled path is deliberately NOT
executable and the text form is deliberately NOT parsed. Detect-and-execute
would defeat the one guarantee this setting exists to make, so the test below
asserts the dispatch is never even LOOKED UP.
"""

from types import SimpleNamespace

import pytest

import litetui.app as app_mod
from litetui.turn_engine import TurnEngine

SPECS = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]


class _Refused:
    """Only what the disabled branch may legitimately touch.

    Every other member raises. That is the point: if the refusal ever starts
    resolving a tool, consulting a policy, or prompting for approval, this
    stub fails instead of quietly permitting it.
    """

    def __init__(self, enabled: bool):
        self.tools_enabled = enabled
        self.looked_up: list[str] = []
        # T076 added a PER-TOOL denylist between the global refusal and
        # dispatch, so the ENABLED path now legitimately reads settings.
        # Empty, so this stub still exercises the enabled path all the way to
        # _dispatch_for and fails there as designed.
        #
        # ⚠️ THE DISABLED PATH'S GUARANTEE IS UNWEAKENED, and that is why this
        # is safe to add: the global refusal returns BEFORE the per-tool check,
        # so with enabled=False this attribute is never touched at all. If that
        # ordering is ever inverted, the tools-off refusal starts depending on
        # a field it should not need.
        self.settings = SimpleNamespace(tools_disabled=[])

    def _dispatch_for(self, name):          # noqa: D102
        self.looked_up.append(name)
        raise AssertionError(
            "the disabled path resolved a tool — it must refuse before this"
        )

    def __getattr__(self, item):
        raise AssertionError(f"disabled path reached for {item!r}")


async def _run(app, name="bash", args=None):
    return await app_mod.LiteTUI._execute_tool(app, name, args or {})


# ── the refusal ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_disabled_tool_is_refused_without_being_resolved():
    app = _Refused(enabled=False)
    result, ok = await _run(app)
    assert ok is False, "ok=True would let a caller record a write that never happened"
    assert result == app_mod.tool_denied("tools-off")
    assert app.looked_up == [], "the tool was resolved — refusal must come first"


@pytest.mark.asyncio
async def test_the_refusal_is_not_reachable_when_tools_are_on():
    # THE NEGATIVE CONTROL. Without it this file would pass against a build
    # that refuses every tool call unconditionally.
    app = _Refused(enabled=True)
    with pytest.raises(AssertionError, match="resolved a tool"):
        await _run(app)


def test_the_refusal_names_controls_that_actually_exist():
    # A refusal that sends the user somewhere that does not exist is worse than
    # no refusal. Ctrl+T is the app's own toggle (`action_toggle_tools`); the
    # switch is on the "Agent loop" tab of the settings screen.
    # Reads the SHIPPED prompts/tool-denied.md, not a constant: this test
    # is what catches an edit to that file deleting the controls it names.
    text = app_mod.tool_denied("tools-off")
    assert "Ctrl+T" in text
    assert "Tools enabled" in text and "Agent loop" in text
    assert "disabled" in text.lower()


# ── the advertisement: the half that stops the markup appearing ───────────────

def test_schemas_are_sent_even_when_tools_are_disabled():
    kwargs = TurnEngine.chat_request(
        model_id="m", messages=[], tools_enabled=False,
        max_tokens_tools=100, max_tokens_chat=50,
        request_overrides={}, thinking_level=None, tools=SPECS,
    )
    assert kwargs["tools"] == SPECS, (
        "withholding the schemas is what made the model type tool markup as prose"
    )
    # …and the budget still follows the toggle: that is a separate concern.
    assert kwargs["max_tokens"] == 50


def test_no_tools_key_when_there_are_no_tools_to_send():
    kwargs = TurnEngine.chat_request(
        model_id="m", messages=[], tools_enabled=False,
        max_tokens_tools=100, max_tokens_chat=50,
        request_overrides={}, thinking_level=None, tools=None,
    )
    assert "tools" not in kwargs, "None must stay absent, not be sent as null"


def test_compact_also_advertises_while_disabled():
    kwargs = TurnEngine.compact_request(
        model_id="m", messages=[], tools_enabled=False,
        max_tokens=100, thinking_level=None, tools=SPECS,
    )
    assert kwargs["tools"] == SPECS


# ── the prompt: neither all of tools.md nor silence ──────────────────────────

def test_the_disabled_prompt_says_it_is_refused_and_who_can_enable_it():
    text = app_mod.TOOLS_DISABLED_PROMPT
    assert text.startswith("\n") and text.endswith("\n"), (
        "compose_prompt glues sections with a bare concatenation, so this "
        "section carries its own separators — see the TOOLS section comment"
    )
    assert "Ctrl+T" in text and "Tools enabled" in text
    assert "refused" in text
    # It must NOT be the full instruction file: offering the tools with the
    # complete how-to while every call is refused is the confusing state.
    assert len(text) < 1000, "this is the short note, not tools.md"
