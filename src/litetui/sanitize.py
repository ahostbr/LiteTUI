"""Terminal hygiene for tool results: strip escape bytes, re-assert modes.

Two failures of one family, both observed on this machine. A subprocess
spawned by a tool call (PowerShell, through the `bash` tool) emitted SGR
mouse reports — ESC[<35;95;34M per mouse move — into its stdout. Nothing
sanitized anything: the bytes went into the widget AND into the model's
context, the screen repainted garbage down the terminal, and the context
counter climbed on noise the model cannot use.

1. strip_escapes removes every well-formed ANSI/VT sequence and every stray
   control byte. It is applied at the ONE place every tool result passes —
   the dispatch loop in app.py — not per tool, so a tool cannot forget it
   and a new tool (static, skill, harness, MCP) cannot bypass it.

2. A child may also change the terminal's own state — enable mouse tracking
   (?1003h) and die without disabling it. The terminal then keeps emitting
   reports at whatever is in the foreground: this TUI. reset_terminal_modes
   re-asserts the exact mode set Textual's Windows driver enables
   (textual/drivers/windows_driver.py: _enable_mouse_support), so after any
   tool call the state is Textual's, whatever a child left behind.
"""

from __future__ import annotations

import re
import sys

#: One pattern, alternatives ordered so the strongest match wins at each
#: position: a well-formed OSC or DCS must never be mis-read as a stray ESC
#: plus text. Every alternative is a COMPLETE sequence — ESC through its
#: terminator — or a single control byte:
#:
#:   OSC   ESC ] payload (ST|BEL)?   set title etc.
#:   DCS   ESC P payload ST?         device control string
#:   CSI   ESC [ params interim F    SGR colours, cursor moves, ?mode
#:                                       changes, and the SGR mouse reports
#:                                       that shredded the TUI (ESC[<35;95;34M)
#:   SS3   ESC O F                   application keypad
#:   3B    ESC I I F                 charset designation (ESC ( B), ...
#:   2B    ESC F                     simple (ESC 7 save cursor, ESC c reset)
#:   C0/C1 single bytes              keep \t \n \r, drop the rest
_SEQUENCES = re.compile(
    r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
    r"|\x1bP[^\x1b]*(?:\x1b\\)?"
    r"|\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1bO[@-~]"
    r"|\x1b[\x20-\x2f][\x20-\x2f][\x30-\x7e]"
    r"|\x1b[\x20-\x2f][\x30-\x7e]"
    r"|\x1b[\x30-\x7e]"
    r"|[\x00-\x08\x0b\x0c\x0e-\x1f\x80-\x9f]"
)


def strip_escapes(text: str) -> str:
    """Remove every terminal control sequence from *text*.

    Total and conservative: it either removes a complete, well-formed
    sequence or a control byte; it never rewrites payload bytes that are
    not a sequence, and text without ESC or control bytes comes back
    byte-identical — test_sanitize asserts that, it does not eyeball it.

    Deliberate consequence: a literal ESC[ that a user pasted is stripped by
    the SAME rule. A pasted sequence is indistinguishable from a live one,
    and the rule is "no escape bytes survive to the terminal or the model",
    not "guess which escapes were meant to be text".
    """
    return _SEQUENCES.sub("", text)


#: What replaces a secret's value. The KEY is always left visible: a tool result
#: that silently loses a line teaches nobody anything, and the model still needs
#: to know the variable was set.
REDACTED_MARKER = "[redacted]"

#: The keywords that mark a name as a secret's name.
#:
#: 🔴 THE KEYWORD MUST TERMINATE THE NAME, and that is not a style choice.
#: Derived from every secret-shaped variable actually present in this machine's
#: environment (names only): CLAUDE_CODE_MESSAGING_TOKEN, LITESUITE_JWT_SECRET,
#: OPENAI_API_KEY, OPENCLAW_GATEWAY_TOKEN, STITCH_API_KEY. All five END with the
#: keyword.
#:
#: ⚠️ A "contains TOKEN" RULE WOULD SHRED THIS APP'S OWN OUTPUT. LiteTUI is full
#: of token ACCOUNTING — 32 uses of `prompt_tokens`, 29 of `max_tokens`, 16 of
#: `max_tokens_tools`, 8 of `completion_tokens`, 4 of `first_token_s`. Anchoring
#: at the end excludes every one: TOKENS is not TOKEN, and `first_token_s` ends
#: in `_s`.
#:     A REDACTOR THAT EATS THE TOKEN COUNTERS IS WORSE THAN NONE — IT CORRUPTS
#:     EVERY TURN INSTEAD OF LEAKING ON THE RARE ONE.
#: `KEY` alone is deliberately absent: `key=value` is ordinary output everywhere.
_SECRET_KEYWORDS = (
    r"SECRET|TOKEN|API[_-]?KEY|APIKEY|ACCESS[_-]?KEY|PRIVATE[_-]?KEY"
    r"|PASSWORD|PASSWD|CREDENTIALS?|AUTH"
)

#: A name whose final component is one of those keywords.
_SECRET_NAME = rf"[A-Za-z0-9_.-]*(?:{_SECRET_KEYWORDS})"

#: `NAME=value` / `NAME: value` — env listings, .env files, yaml, ini, prose.
#: The value runs to end of line: an env value may contain anything, and the
#: alternative (stopping at the first space) leaks the tail of every secret
#: containing one.
_ASSIGNMENT = re.compile(
    rf"(?P<key>(?<![A-Za-z0-9_.-]){_SECRET_NAME})(?P<sep>\s*[:=]\s*)(?P<val>[^\r\n]+)",
    re.IGNORECASE,
)

#: `"name": "value"` — the JSON form, kept separate because the value is
#: delimited by quotes rather than by the line, and redacting to end of line
#: would swallow the rest of the object.
_JSON_PAIR = re.compile(
    rf"(?P<key>\"{_SECRET_NAME}\"\s*:\s*)(?P<q>\")(?P<val>[^\"]*)(?P=q)",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Replace the VALUES of secret-named variables, keeping their names.

    🔴 THE ONE PATH EVERY TOOL RESULT CROSSES SAW ONLY ESCAPE BYTES. On
    2026-09-03 an env listing through the `bash` tool put `LITESUITE_JWT_SECRET`
    and `OPENAI_API_KEY` verbatim into the transcript, the model's context and a
    screenshot; the keys were rotated by hand. `strip_escapes` was already
    applied at that exact point and had no reason to look at the payload.

    ⬜ ANCHORED TO NAMES, NOT TO ENTROPY. A free-floating high-entropy sweep was
    considered and rejected: ordinary output here is full of git shas, hashes and
    base64, and redacting those makes every tool result unreadable — the failure
    would be constant where the leak is rare.

    ⚠️ NOT A SECURITY BOUNDARY, AND MUST NOT BE SOLD AS ONE. It catches the
    shapes a secret takes when a tool prints an environment: `NAME=value`,
    `NAME: value`, and `"name": "value"`. A secret that arrives with no name
    beside it — a bare key pasted into a file, a base64 blob — passes through
    untouched, and nothing here can tell it from data.
    """
    if not text:
        return text
    text = _JSON_PAIR.sub(lambda m: f'{m.group("key")}"{REDACTED_MARKER}"', text)
    return _ASSIGNMENT.sub(lambda m: f'{m.group("key")}{m.group("sep")}{REDACTED_MARKER}', text)


#: The exact mode set Textual's Windows driver enables — measured from
#: .venv/Lib/site-packages/textual/drivers/windows_driver.py,
#: _enable_mouse_support (SET_VT200, SET_ANY_EVENT, SET_VT200_HIGHLIGHT,
#: SET_SGR_EXT). Off first, then on: the final state is Textual's, whatever
#: a child left behind.
_MOUSE_OFF = "\x1b[?1000l\x1b[?1003l\x1b[?1015l\x1b[?1006l"
_MOUSE_ON = "\x1b[?1000h\x1b[?1003h\x1b[?1015h\x1b[?1006h"


def reset_terminal_modes() -> None:
    """Re-assert the terminal's mouse mode after a subprocess tool call.

    Best effort: a tool result must never fail because the terminal refused
    a mode sequence, and a redirected stdout (tests, pipes) makes the write
    meaningless anyway.
    """
    stream = getattr(sys, "__stdout__", None)
    if stream is None:
        stream = sys.stdout
    try:
        stream.write(_MOUSE_OFF + _MOUSE_ON)
        stream.flush()
    except Exception:
        pass
