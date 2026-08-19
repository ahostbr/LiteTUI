"""Emit the escape families the sanitizer must strip.

Run by test_sanitize.py through a REAL subprocess (the same spawn shape as
tool_bash), so strip_escapes is exercised on bytes that went through actual
capture and decoding — not on a string a test author typed. The set is the
observed incident plus its neighbours: SGR mouse reports (the bytes that
shredded the TUI), CSI SGR styling, OSC titles (BEL- and ST-terminated), a
DCS string, a charset designation, stray C0/C1 bytes and a lone trailing
ESC.
"""
import sys

payload = (
    b"\x1b[<35;95;34M"            # SGR mouse release — the incident bytes
    b"\x1b[<35;96;34M"            # SGR mouse release, second event
    b"\x1b[1m"                    # CSI: bold on
    b"hello "
    b"\x1b[0m"                    # CSI: reset
    b"\x1b]0;window title\x07"    # OSC: set title, BEL-terminated
    b"\x1b]2;st title\x1b\\"      # OSC: set title, ST-terminated
    b"world "
    b"\x1bP1;2;3\x1b\\"           # DCS: device control string
    b"\x1b(B"                     # charset designation: ASCII
    b"\x07"                       # stray BEL
    b"\x9b"                       # C1 ESC
    b"plain"
    b"\x1b"                       # lone trailing ESC
)
sys.stdout.buffer.write(payload)
sys.stdout.buffer.flush()
