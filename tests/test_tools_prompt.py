"""The tools prompt cannot go stale the way it just did.

It said "You have four tools: bash, read, write, web_fetch" while the app
offered eleven — view_image, pccontrol, chrome, ask_user_question, studio and
skill were all missing, so the model was told capabilities it had did not
exist. That text had been a Python constant since before those tools were
written, and nothing connected the two.

The fix is structural, not a correction: the prompt no longer carries an
inventory at all. Every tool's exact name, parameters and limits already reach
the model as JSON schemas in the same request — prose re-listing them is a
SECOND copy of a fact, and the second copy is the one that rots. The prompt now
says how to use tools; the schemas say which exist.

These gates enforce that split. Both fail against the version this replaced.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import app as m
from litetui import paths


def _prompt() -> str:
    return paths.TOOLS_PROMPT_FILE.read_text(encoding="utf-8")


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.jobs[:] = []
    return a


def test_the_prompt_states_no_tool_COUNT() -> None:
    """"You have four tools" was wrong the moment a fifth was added."""
    text = _prompt().lower()
    bad = re.findall(
        r"\b(?:you have\s+)?(one|two|three|four|five|six|seven|eight|nine|ten|eleven|\d+)\s+tools\b",
        text,
    )
    assert not bad, (
        f"the prompt hardcodes a tool count {bad!r} — it is wrong the next time "
        "a tool is added, and nothing will say so"
    )


@pytest.mark.asyncio
async def test_every_tool_the_prompt_NAMES_actually_exists() -> None:
    """Naming a tool is allowed; naming one that is gone is not.

    This is the gate that would have caught the original drift from the other
    direction — a renamed or removed tool still being advertised.
    """
    a = make_app()
    async with a.run_test(size=(100, 30)) as pilot:
        await pilot.pause()
        offered = {
            t.get("function", {}).get("name")
            for t in a._all_tools()
            if isinstance(t, dict)
        }
        # Deferred tools (7b6640f) EXIST without being loaded: their schemas sit
        # behind `tool_search` and dispatch activates them on first call, so the
        # prompt may still name one. What this gate must catch is a tool that is
        # GONE, and a deferred tool is not gone.
        offered |= {
            t.get("function", {}).get("name")
            for t in a.plugins.deferred_specs()
            if isinstance(t, dict)
        }
        offered.discard(None)

    text = _prompt()
    # Only treat a word as a tool NAME when it is written as one: bare English
    # words like "read" and "write" appear in ordinary prose, and flagging
    # those would make this gate unusable.
    named = set(re.findall(r"`([a-z_][a-z0-9_]*)`", text))
    unknown = named - offered
    assert not unknown, (
        f"the prompt names tool(s) that are not offered: {sorted(unknown)}. "
        f"Offered: {sorted(offered)}"
    )


@pytest.mark.asyncio
async def test_the_model_is_told_to_read_the_schemas() -> None:
    """With no inventory in the prompt, the schemas must be pointed at."""
    assert "schema" in _prompt().lower(), (
        "the prompt dropped its tool list without telling the model where the "
        "real one is — that is worse than the stale list"
    )


def test_the_prompt_is_still_substantial() -> None:
    """A gate that passes on an empty file is not a gate.

    The TOOLS section is skipped entirely when the file is missing, so 'no
    stale content' is trivially satisfiable by deleting the guidance.
    """
    body = _prompt().strip()
    assert len(body) > 400, f"the tools prompt is only {len(body)} chars — did it get gutted?"
