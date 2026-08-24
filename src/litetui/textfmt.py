"""Pure text and progress formatting — no Textual, no app state.

Lifted out of `app.py` under T070 step O0. Every function here was already
pure: it takes values and returns a string or a tuple, touches no `self`, and
imports nothing from the UI. They lived in `app.py` only because that is where
they were first written.

🔴 `app.py` RE-EXPORTS ALL OF THESE, and that is deliberate, not laziness.
Tests import them as `from litetui.app import render_progress, ...` and reach
them as `app_mod.tps_text`; 24 references to `render_progress` alone exist
outside this file. Moving the definitions without the re-export would have been
a rename dressed as a refactor.

⚠️ SO BE HONEST ABOUT WHAT THIS BUYS: it reduces app.py's LINE COUNT. It does
not reduce its API surface, its method count, or the plugin reach-through — the
names are still reachable through `litetui.app`. See PLAN.md §6.
"""

from __future__ import annotations

import time
from pathlib import Path
from litetui import paths
from litetui.fmt import fmt_dur
from rich.console import Console
from rich.markdown import Markdown
from rich.text import Text


TOOL_NAME_DEFAULT = "#e8a33d"


def load_prompt(name: str, **variables: object) -> str:
    """A prompt from prompts/<name>.md, with {name} placeholders substituted.

    Replacement, not str.format(): these files are meant to be EDITED, and a
    stray brace in hand-edited prose must not crash the app — only the
    placeholders that are actually passed get touched.

    A missing file raises FileNotFoundError naming the path, at import time
    for the module-level prompts — a prompt that silently loads empty would
    be a model quietly running without its instructions, which is worse than
    not booting.
    """
    text = (paths.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    for key, value in variables.items():
        text = text.replace("{" + key + "}", str(value))
    return text


def memory_prompt(convo_id: str, folder: Path) -> str:
    """The block appended to the system prompt so the agent can find its own
    store. Body lives in prompts/conversation-store.md — read PER CALL, so
    edits take effect on the next conversation without a restart."""
    return load_prompt(
        "conversation-store",
        convo_id=convo_id,
        store_path=str(folder).replace("\\", "/"),
    )


def _markdown_to_text(src: str, width: int) -> Text:
    """Markdown rendered to a styled Text.

    Rich renders the Markdown to SEGMENTS; rebuilding those as a Text keeps
    every style (bold, bullets, code colour) while giving Textual a visual type
    it can select from. Falls back to the raw source if rendering ever fails —
    unstyled but readable and selectable beats an exception mid-turn.
    """
    try:
        console = Console(width=max(1, width), highlight=False)
        out = Text()
        for seg in console.render(Markdown(src), console.options.update(width=max(1, width))):
            if seg.text:
                out.append(seg.text, seg.style)
        return out
    except Exception:
        return Text(src)


def midturn_action(enter_interrupts: bool, alt_chord: bool) -> str:
    """Pure: what a mid-turn submission does — "queue" or "interrupt".

    ONE mapping with two ends and a boolean that swaps them (Ryan, 2026-08-21:
    "swapping the default behavior between those two in the settings page").
    NOT a queue path plus a hardcoded interrupt chord — that shape, under the
    swapped setting, leaves the user with no way to queue at all.

        enter_interrupts=False:  Enter -> queue      chord -> interrupt
        enter_interrupts=True:   Enter -> interrupt  chord -> queue
    """
    return "interrupt" if (enter_interrupts != alt_chord) else "queue"


def render_progress(t0: float, now: float, prompt_tokens=None, learned_rate=None) -> str:
    """Pure: the in-flight bubble text while no answer token has arrived yet.
    Elapsed since t0, always, with a trailing '…' to signal still working.
    An ETA is appended ONLY when there is both a learned prompt-eval rate and a
    prompt token count to apply it to; a None or zero rate (or a missing token
    count) yields elapsed-only and never divides by zero. `prompt_tokens` is an
    ESTIMATE (the previous turn's count), so the ETA is a projection, not a
    measurement of this turn. Inputs are optional so the pre-ETA callers
    (tool_display_parts, the pre-token answer bubble) keep working unchanged."""
    base = f"{fmt_dur(now - t0)} …"
    if (learned_rate is not None and learned_rate > 0
            and prompt_tokens is not None and prompt_tokens > 0):
        eta_s = prompt_tokens / learned_rate
        return f"{base} · est ~{fmt_dur(eta_s)}"
    return base


def is_reliable_rate_sample(prompt_tokens, first_token_s, floor: float = 0.25) -> bool:
    """Pure: the KV-cache gate. A rate sample is only trustworthy when the turn
    demonstrably REPROCESSED the prompt, which surfaces as a first-token latency
    at or above `floor` (default 0.25s). A cache-hit turn returns well under the
    floor and its prompt_tokens/latency is a misleading rate (fixed overhead
    dominates when few tokens are reprocessed), so admitting it into the median
    would make a large post-compact turn predict minutes. Requires BOTH a positive
    token count AND a first-token latency at the floor — either missing/zero
    means the sample is not usable."""
    return (prompt_tokens is not None and prompt_tokens > 0
            and first_token_s is not None and first_token_s >= floor)


def tps_text(tps: float) -> str:
    """Pure: the tok/s field, one format for every surface (the footer,
    the thinking header). The caller decides whether to show it at all:
    a None reactive means "no number yet" and renders as absence, never
    a rendered 0.0, which would be a lie about a number that does not
    exist."""
    return f"{tps:.1f} tok/s"


def thinking_header_text(marker: str, t0: float, now: float,
                         tps: float | None) -> str:
    """Pure: the thinking block header while the trace is streaming.
    '<marker> Thinking · 12.3s ... · 24.1 tok/s' — the elapsed part is
    render_progress (no ETA: a reasoning trace has no token count until
    it ends, and a confidently wrong number is worse than none), the
    tok/s part is tps_text (the footer's own format, one source). tps
    None -> elapsed only; the field is never rendered as 0.0 tok/s.
    `marker` is the expand glyph, so a collapsed block keeps its own
    state in the same string."""
    text = f"{marker} Thinking · {render_progress(t0, now)}"
    if tps is not None:
        text += f" · {tps_text(tps)}"
    return text


def tool_display_parts(tool, max_lines: int = 12, name_color: str | None = None) -> list:
    """Pure: the (text, style) parts for a ToolMessage's display from its state.
    Testable without a Textual app (no widget.content / console involved).
    `tool` needs: tool_name, _args, _result, _ok, _t0, _took.

    `name_color` is the theme's $tool-text. Optional, and defaulted, so this
    stays a pure function that a test can call with nothing but a stub tool."""
    arg_line = tool._args.replace("\n", " ")
    if len(arg_line) > 110:
        arg_line = arg_line[:107] + "..."
    parts = [(f"\U0001F527 {tool.tool_name}", f"bold {name_color or TOOL_NAME_DEFAULT}")]
    if arg_line:
        parts.append(("  " + arg_line, "#8b95a7"))
    if tool._result is not None:
        lines = tool._result.split("\n")
        shown = "\n".join(lines[:max_lines])
        if len(lines) > max_lines:
            shown += f"\n\u2026 ({len(lines) - max_lines} more lines, {len(tool._result)} chars total)"
        style = "bold #e5534b" if not tool._ok else "#7d8799"
        parts.append(("\n" + shown, style))
    if tool._result is None:
        parts.append(("\n  \u23f1 " + render_progress(tool._t0, time.monotonic()), "#8b95a7"))
    else:
        parts.append(("\n  ⏱ " + fmt_dur(tool._took or 0.0), "#5c6470"))
    return parts
