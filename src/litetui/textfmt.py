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

import re
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


#: Every refusal LiteTUI can hand back in place of a tool result, and the
#: placeholders each one MUST carry. These constants are the floor: prompts/
#: is editable, and a refusal is the one message that has to survive a broken
#: edit of its own source. See `tool_denied`.
TOOL_DENIED_REQUIRED: dict[str, tuple[str, ...]] = {
    "unknown-tool": ("name",),
    "no-metadata": ("name",),
    "profile": ("name", "reason"),
    "by-user": ("name",),
    "tool-disabled": ("name",),
    "tools-off": (),
}

TOOL_DENIED_FALLBACK: dict[str, str] = {
    "unknown-tool": "[error] unknown tool: {name}",
    "no-metadata": "[policy denied] {name}: no capability metadata",
    "profile": (
        "[policy denied] {name}: {reason}. Nothing ran and nothing changed. "
        "This is the active authority profile refusing, not the user — do not "
        "ask them to approve it and do not retry."
    ),
    "by-user": (
        "[policy denied by user] {name} — the user was asked and refused, so "
        "nothing ran and nothing changed. The turn ended there, by their "
        "choice. Do not retry and wait for their next message."
    ),
    "tool-disabled": (
        "[disabled] The user has switched `{name}` OFF in the tool list, so "
        "nothing ran and nothing changed. Only the user can switch it back on "
        "(/tools). Do not retry it, and do not reach for a different tool to "
        "accomplish the same thing — they turned this one off on purpose."
    ),
    "tools-off": (
        "[disabled] Tools are turned OFF in LiteTUI, so nothing ran and "
        "nothing changed. Only the user can turn them on: Ctrl+T, or Settings "
        "-> Agent loop -> Tools enabled. Tell them that in plain language, "
        "then answer as best you can without tools. Do not retry and do not "
        "try another tool."
    ),
}


def _tool_denied_sections() -> dict[str, str]:
    """`## key` sections of prompts/tool-denied.md, comments stripped."""
    text = (paths.PROMPTS_DIR / "tool-denied.md").read_text(encoding="utf-8")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out: dict[str, str] = {}
    key = None
    buf: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if key is not None:
                out[key] = "\n".join(buf).strip()
            key, buf = line[3:].strip(), []
        elif key is not None:
            buf.append(line)
    if key is not None:
        out[key] = "\n".join(buf).strip()
    return {k: v for k, v in out.items() if v}


def validate_tool_denied() -> None:
    """RAISE on a broken prompts/tool-denied.md.  Called by the tests.

    The loud half of the contract lives HERE, where a failure costs a red test
    and not a refusal. `tool_denied` never raises for the same faults, because
    the moment a refusal is needed is the worst possible moment to throw.
    """
    sections = _tool_denied_sections()
    for key, required in TOOL_DENIED_REQUIRED.items():
        if key not in sections:
            raise ValueError(f"prompts/tool-denied.md: missing section '## {key}'")
        missing = [p for p in required if "{" + p + "}" not in sections[key]]
        if missing:
            raise ValueError(
                f"prompts/tool-denied.md: section '## {key}' lost placeholder(s) "
                + ", ".join("{" + m + "}" for m in missing)
            )


def tool_denied(key: str, **variables: object) -> str:
    """One refusal, rendered.  NEVER raises for a bad file, NEVER goes silent.

    A refusal is the one message that must survive its own source being
    broken. Three faults fall back to `TOOL_DENIED_FALLBACK[key]`:

      * the file is missing or unreadable,
      * the `## key` section was renamed or deleted,
      * the section lost a placeholder it is required to carry.

    THE THIRD IS THE POINT. Rendering a literal `{name}` at the model is worse
    than the hardcoded string this replaced — it turns a refusal into
    something that reads like a bug, and the model may treat it as one. So a
    section that cannot name the tool is not used at all.

    An unknown `key` DOES raise: that is a programmer error, not a user edit,
    and every key is exercised by the tests.
    """
    if key not in TOOL_DENIED_REQUIRED:
        raise KeyError(f"no such tool-denied key: {key!r}")
    template = TOOL_DENIED_FALLBACK[key]
    try:
        section = _tool_denied_sections().get(key)
    except OSError:
        section = None
    if section and all(
        "{" + p + "}" in section for p in TOOL_DENIED_REQUIRED[key]
    ):
        template = section
    for name, value in variables.items():
        template = template.replace("{" + name + "}", str(value))
    return " ".join(template.split())


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


def token_count_text(tokens: int) -> str:
    """Pure: the token-count field, one format for every surface (the footer,
    the thinking header) — the same arrangement `tps_text` has, and for the
    same reason.

    Comma-grouped to match the readout Ryan is comparing against: LM Studio
    prints "2,775 tok" and a bare 2775 beside it reads as a different quantity.

    THE CALLER DECIDES WHETHER TO SHOW IT AT ALL. Zero is a real answer — a
    turn that produced a tool call and no prose generated no output tokens —
    but it is not a useful one to paint, and it is indistinguishable on screen
    from "not counted yet". Both surfaces treat 0 as absence, which is the same
    rule `tps_text` follows for None.
    """
    return f"{tokens:,} tok"


def thinking_header_text(marker: str, t0: float, now: float,
                         tps: float | None, tokens: int | None = None) -> str:
    """Pure: the thinking block header while the trace is streaming.
    '<marker> Thinking · 12.3s ... · 24.1 tok/s' — the elapsed part is
    render_progress (no ETA: a reasoning trace has no token count until
    it ends, and a confidently wrong number is worse than none), the
    tok/s part is tps_text (the footer's own format, one source). tps
    None -> elapsed only; the field is never rendered as 0.0 tok/s.
    `marker` is the expand glyph, so a collapsed block keeps its own
    state in the same string.

    `tokens` is the count of REASONING deltas this turn (T079, Ryan: "total
    tokens thinking that turn"). It sits between the elapsed time and the rate
    so the line reads as quantity-then-speed. Zero or None renders as absence,
    never "0 tok" — the docstring above already refuses that for tok/s and the
    same argument applies: a rendered zero is a claim about a number that has
    not been produced.

    ⚠️ It is OUR delta count, not `usage.completion_tokens`. The server reports
    ONE figure covering reasoning AND output together, so it cannot answer the
    question this field asks. Ryan sanctioned the approximation explicitly:
    "even if we calc it ourself"."""
    text = f"{marker} Thinking · {render_progress(t0, now)}"
    if tokens:
        text += f" · {token_count_text(tokens)}"
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
