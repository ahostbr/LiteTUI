"""Model and user text must NEVER be markup-parsed on its way to the screen.

The crash this guards, seen live 2026-08-21: the model was writing key-probe
code and streamed text containing `[key=`. The streaming repaint appended the
cursor glyph and assigned the RAW STRING to `.content` — Textual markup-parses
a bare str — and the parser died mid-turn:

    Error: Expected markup value (found '<cursor>').

It ended the seat's turn. The trigger is topic-dependent: text about key
handling, escape sequences, or Rich markup itself is exactly the text that
matches tag grammar, so an agent working on keyboard code is the one that
crashes.

THE TRAP THAT HID IT FOR WEEKS: most bracket text does NOT match tag grammar.
`[inbox from ba736bd4 · normal]` fails the grammar and renders literally, so
bubbles full of brackets worked every day. `[key=` MATCHES the grammar (tag
with a key=value pair), then hits end-of-input where the VALUE should be.
Bracket text working is not evidence bracket text is safe — the grammar
decides, per string.

The fix is the shape the codebase already used in three places (thinking
block, system bubbles, error bubbles): wrap in rich.text.Text, which renders
literally with no markup pass.
"""
import io
from pathlib import Path

import pytest
from rich.text import Text
from textual.content import Content
from textual.markup import MarkupError

CUR = chr(0x258C)  # the streaming cursor glyph
APP_SRC = (Path(__file__).resolve().parent.parent / "src" / "app.py").read_text(encoding="utf-8")


# --- the hazard is real in the installed toolkit ----------------------------
def test_the_crash_reproduces_on_a_raw_string():
    """POSITIVE CONTROL for the whole file. If this stops raising, the
    toolkit changed and the source gates below are guarding a ghost."""
    with pytest.raises(MarkupError):
        Content.from_markup("[key=" + CUR)


def test_the_crash_needs_no_cursor_either():
    """The cursor is incidental — any text after `[word=` that fails the value
    grammar dies too. The bug is streaming raw model text, not the glyph."""
    with pytest.raises(MarkupError):
        Content.from_markup('self.log(f"[key={event.key}") ')


def test_text_wrapping_is_immune():
    """The fix, proven on the exact failing input."""
    c = Content.from_rich_text(Text("[key=" + CUR))
    assert "[key=" in str(c)


def test_most_bracket_text_parses_fine_which_is_why_this_hid():
    """Documents the near-miss: bubbles full of brackets rendered daily
    because THIS shape fails tag grammar and falls back to literal. Anyone
    tempted to remove the Text() wraps because 'brackets always worked'
    should read this test's name again."""
    c = Content.from_markup("[inbox from ba736bd4 · normal] hello")
    assert "inbox from" in str(c)


# --- the four sites stay wrapped--------------------------------------------
# Probes are built with chr(92) so no shell/heredoc layer can eat a backslash
# on its way into this file — the exact failure mode that corrupted the first
# version of this very test. They match the SOURCE text verbatim, escapes and
# all.
BSLASH = chr(92)
SITES = [
    # (id, wrapped-form-that-must-exist, raw-form-that-must-be-gone)
    ("streaming-cursor",
     'widget.body.content = Text(text_full + " ' + BSLASH + 'u258c")',
     'widget.body.content = text_full + " ' + BSLASH + 'u258c"'),
    ("finalize-fallback",
     "widget.body.content = Text(text_full)",
     "widget.body.content = text_full" + chr(10)),
    ("resume-fallback",
     "w.body.content = Text(text)",
     "w.body.content = text" + chr(10)),
    ("user-bubble",
     'ChatMessage(Text("' + BSLASH + 'n".join(parts)), classes="user-msg")',
     'ChatMessage("' + BSLASH + 'n".join(parts), classes="user-msg")'),
]


@pytest.mark.parametrize("site_id, wrapped, raw", SITES, ids=[s[0] for s in SITES])
def test_site_is_wrapped_and_raw_form_is_gone(site_id, wrapped, raw):
    assert wrapped in APP_SRC, f"{site_id}: wrapped form missing"
    assert raw not in APP_SRC, f"{site_id}: raw form still present"
