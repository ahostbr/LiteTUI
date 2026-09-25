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
_SRC = Path(__file__).resolve().parent.parent / "src" / "litetui"
APP_SRC = (_SRC / "app.py").read_text(encoding="utf-8")
WIDGETS_SRC = (_SRC / "widgets.py").read_text(encoding="utf-8")
SOURCES = {"app.py": APP_SRC, "widgets.py": WIDGETS_SRC}


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
#: (id, FILE, wrapped-form-that-must-exist, raw-form-that-must-be-gone)
#:
#: 🔴 THREE OF THESE WENT RED WITHOUT ANYTHING BECOMING UNSAFE. The table
#: read `app.py` only, and three of the four sites MOVED INTO widgets.py
#: when the bubbles became widgets. Every wrap moved with them — checked
#: site by site before touching this table, because 'the guard is stale' and
#: 'the wrap is gone' look identical from the failure message and only one
#: of them is an emergency:
#:
#:   user-bubble        app.py `ChatMessage(Text(...))`
#:                   -> widgets.py `UserMessage.__init__`, `Static(Text(text))`
#:   finalize-fallback  app.py `widget.body.content = Text(text_full)`
#:   resume-fallback    app.py `w.body.content = Text(text)`
#:                   -> BOTH are now one site: `AssistantMessage.set_answer`
#:                      in widgets.py. app.py's finalize (6499) and resume
#:                      (2952) both call it, and its except branch is the
#:                      fallback both entries used to name separately.
#:
#: ⬜ THE PRIMARY PATH IS SAFE TOO AND IS NOT LISTED, deliberately: it is
#: `self.content = _markdown_to_text(src, width)`, and textfmt's
#: `_markdown_to_text(src, width) -> Text` BUILDS a Text from rendered
#: segments. A source-substring entry for a call that returns Text would
#: pin the spelling of a helper rather than the property that matters.
#:
#: `streaming-cursor` (app.py `widget.body.content = Text(text_full + " ▌")`)
#: is GONE, not unwrapped: since 2026-09-25 every streamed answer is drawn by
#: stream_sink.StreamSink through that same primary `set_markdown` path. The
#: behaviour is pinned in test_stream_sink.py
#: (test_streamed_brackets_render_literally), not by source spelling.
SITES = [
    ("set-answer-fallback", "widgets.py",
     "self.body.content = Text(text)",
     "self.body.content = text" + chr(10)),
    ("user-bubble", "widgets.py",
     "self.body = Static(Text(text))",
     "self.body = Static(text)"),
]


@pytest.mark.parametrize("site_id, filename, wrapped, raw", SITES,
                         ids=[s[0] for s in SITES])
def test_site_is_wrapped_and_raw_form_is_gone(site_id, filename, wrapped, raw):
    """The file is part of the fixture now. A site that MOVES should make
    this fail with the name of the file it is no longer in, rather than
    reading as "the wrap is gone"."""
    src = SOURCES[filename]
    assert wrapped in src, f"{site_id}: wrapped form missing from {filename}"
    assert raw not in src, f"{site_id}: raw form still present in {filename}"
