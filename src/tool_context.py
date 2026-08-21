"""What a tool's output contributes to the conversation.

THE PROBLEM. `self.conversation` IS the prompt (app.py builds `"messages":
self.conversation`) and it is ALSO the store — `_resume` does
`self.conversation = msgs` straight from convo.jsonl. Measured on a real
store: 5,907,122 bytes, of which 5,849,541 (99.0%) are `msg` records. Tool
output is the bulk of that, and it is paid for on every turn AND again on
every resume.

THREE MODES, chosen from settings so the comparison can be made by measuring
rather than by argument:

  off             the raw output goes into the conversation. What LiteTUI has
                  always done. The baseline the other two are measured against.

  llm-tool-mask   the AgentPatterns "observation masking" route: the output is
                  replaced by a short placeholder naming what it was. No model
                  call, no latency, no tokens spent producing it.

  llm-tool-summ   a side call summarises the output toward the task; only the
                  summary enters the conversation. Costs one prompt-eval of the
                  raw, in a throwaway context that is never persisted — so the
                  main conversation NEVER holds the raw, not even once.

The literature genuinely disagrees about which wins. arXiv 2508.21433 argues
simple masking matches LLM summarisation; AgentPatterns reports hard masking
costs ~10% solve rate with extended thinking enabled, which is exactly the
qwen3.8-reasoning case this runs on. Hence a switch, not a decision.

⚠️ THE RAW IS NEVER DESTROYED, IN EITHER MODE. Both write it to a sidecar file
and put a dereferenceable pointer in the conversation. That is deliberate on
two counts. It keeps the A/B honest — the modes then differ ONLY in what they
put in context, not in what survives. And it is what stops this from becoming
lossy compression: an agent that cannot tell "the output did not contain X"
from "X was summarised away" will report absence as fact, and absence is the
answer that stops you looking.
"""

from __future__ import annotations

from dataclasses import dataclass

OFF = "off"
MASK = "llm-tool-mask"
SUMM = "llm-tool-summ"
MODES = (OFF, MASK, SUMM)

#: Default size above which a result is worth processing. Below it the raw is
#: smaller than the machinery around it, and a summary could easily be LONGER
#: than what it replaces.
DEFAULT_THRESHOLD_CHARS = 2000

#: Tools whose output must never be masked or summarised, whatever its size.
#: `view_image` returns image content, not prose — there is nothing to
#: summarise and a summary would silently destroy the payload.
NEVER_PROCESS = frozenset({"view_image"})

VERBATIM = "verbatim"
ROUTE_MASK = "mask"
ROUTE_SUMMARISE = "summarise"


@dataclass(frozen=True)
class ToolResultPlan:
    """What to do with one tool result. Pure data — no I/O, no model."""

    route: str
    #: Why this route was chosen. Always populated, including for verbatim:
    #: a mode that silently does nothing looks identical to a mode that is
    #: broken, and this is the field that tells them apart in a log.
    reason: str
    #: Write the raw to the sidecar. False only when the raw is going into the
    #: conversation whole, where a second copy on disk buys nothing.
    store_raw: bool
    #: The text to put in the conversation, or None when a summary is still
    #: needed — the caller makes the side call, then uses render_summary().
    context_text: str | None


def plan_tool_result(
    mode: str,
    tool_name: str,
    raw: str,
    threshold_chars: int = DEFAULT_THRESHOLD_CHARS,
) -> ToolResultPlan:
    """Decide what this tool result contributes to the conversation.

    Unknown modes fall back to `off`. A typo in a settings file must not
    silently start discarding tool output — the safe direction is the one that
    keeps everything.
    """
    if mode not in MODES:
        return ToolResultPlan(VERBATIM, f"unknown mode {mode!r}, treated as off", False, raw)

    if mode == OFF:
        return ToolResultPlan(VERBATIM, "mode is off", False, raw)

    if tool_name in NEVER_PROCESS:
        return ToolResultPlan(VERBATIM, f"{tool_name} output is never processed", False, raw)

    if len(raw) < threshold_chars:
        return ToolResultPlan(
            VERBATIM, f"{len(raw)} chars, under the {threshold_chars} threshold", False, raw
        )

    if mode == MASK:
        return ToolResultPlan(ROUTE_MASK, f"masking {len(raw)} chars", True, None)

    return ToolResultPlan(ROUTE_SUMMARISE, f"summarising {len(raw)} chars", True, None)


def render_mask(tool_name: str, raw: str, pointer: str) -> str:
    """The placeholder that replaces a masked result.

    It names the tool, the size, and where the full copy is. All three matter:
    without the size the agent cannot tell a big result from an empty one, and
    without the pointer masking is destructive.
    """
    return (
        f"[{tool_name} output masked — {len(raw)} chars, {_line_count(raw)} lines. "
        f"Full output: {pointer}]"
    )


def render_summary(tool_name: str, raw: str, pointer: str, summary: str) -> str:
    """The summary that replaces a summarised result, with its provenance.

    The pointer line is not decoration. It is the difference between a lossy
    compression and a cache eviction: whatever the summary dropped is still
    fetchable, and the agent can see that it is reading a reduction rather than
    the thing itself.
    """
    return (
        f"[{tool_name} output summarised — {len(raw)} chars, {_line_count(raw)} lines. "
        f"Full output: {pointer}]\n{summary.strip()}"
    )


def summarise_prompt(task: str, tool_name: str, raw: str) -> str:
    """The side-call prompt.

    Anchored to the TASK, not to the previous message. Relevance judged against
    the question already asked drops whatever the NEXT question needs — a
    `git status` reduced to "3 files changed" is fine until two turns later
    when the filenames are gone.

    Identifiers are preserved verbatim by instruction because they are what a
    later turn needs and they are cheap; prose is what is expensive.
    """
    return (
        f"Summarise this {tool_name} output for an agent working on:\n{task}\n\n"
        "Keep VERBATIM every identifier a later step could need: file paths, line "
        "numbers, names, ids, hashes, versions, counts, exit codes, error text. "
        "Drop narrative, repetition and formatting. If nothing in the output is "
        "relevant to the task, say so in one line rather than inventing relevance.\n\n"
        f"--- output ---\n{raw}"
    )


def _line_count(raw: str) -> int:
    return raw.count("\n") + 1 if raw else 0
