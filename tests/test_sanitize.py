"""Sanitizer: real captured bytes in, clean text out.

The verification bar comes from the --priority bug: 30 green checks with the
subprocess mocked out, so the real command line never executed once. A green
test that never ran the real path is not green. So the main fixture here is a
REAL subprocess — spawned the same way tool_bash spawns it — that emits SGR
mouse reports, CSI colours, OSC titles, a DCS string and stray control bytes,
and the test feeds the bytes it ACTUALLY produced to strip_escapes. Not a
hand-typed string.
"""
import io
import sys
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import sanitize
from app import tool_bash

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


print("=== the fixture actually emitted escapes (real subprocess, real capture) ===")
raw = tool_bash({"command": f"{sys.executable} {Path(__file__).parent / '_emit_escapes.py'}"})
chk("capture produced ESC bytes (fixture is not a tautology)", "\x1b" in raw)

print("\n=== the real captured bytes come out clean ===")
clean = sanitize.strip_escapes(raw)
chk("no ESC survives", "\x1b" not in clean)
chk("no C1 ESC survives", "\x9b" not in clean)
chk("the SGR mouse reports are gone", "35;95;34" not in clean)
chk("the OSC payloads are gone", "window title" not in clean and "st title" not in clean)
chk("the DCS payload is gone", "1;2;3" not in clean)
chk("the non-sequence payload survives: hello", "hello" in clean)
chk("...world", "world" in clean)
chk("...plain", "plain" in clean)

print("\n=== negative control: a pasted literal ESC[ is the SAME rule ===")
pasted = "user pasted \x1b[31m and \x1b[<35;95;34M into the prompt"
clean2 = sanitize.strip_escapes(pasted)
chk("a literal ESC[ a user pasted is stripped by the same rule", "\x1b" not in clean2)
chk("the surrounding text survives", "user pasted" in clean2 and "into the prompt" in clean2)

print("\n=== normal text is byte-identical (asserted, not eyeballed) ===")
text = "plain text with \ttabs, an em \u2014 dash, [brackets] that are not escapes.\nLine two.\r\n"
clean3 = sanitize.strip_escapes(text)
chk("string-identical", clean3 == text)
chk("byte-identical (utf-8)", clean3.encode("utf-8") == text.encode("utf-8"))

print("\n=== reset re-asserts Textual's mouse modes, off then on ===")
buf = io.StringIO()
real = sys.__stdout__
sys.__stdout__ = buf
try:
    sanitize.reset_terminal_modes()
finally:
    sys.__stdout__ = real
written = buf.getvalue()
chk("writes the off sequences first", written.startswith(sanitize._MOUSE_OFF))
chk("...then the on sequences (final state is Textual's)", written.endswith(sanitize._MOUSE_ON))
chk("exactly the mode set, nothing else", written == sanitize._MOUSE_OFF + sanitize._MOUSE_ON)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
