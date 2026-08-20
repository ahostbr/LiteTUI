# TASK — strip control sequences from tool output (assigned to BlackGrid by Sentinel)

This file exists because the first dispatch was delivered, consumed, and then lost in your
compaction. `harness check` cannot re-serve it: the poller had already moved it to
`inbox/done/`, so the conversation was the only copy. A file is not.

## 1. FIRST — commit what you already have

`harness.py` and `test_harness_tool.py` are modified in the working tree with the
`--priority` fix. That fix exists in exactly one place. Commit it with the trailer
convention (`Agent-Name: BlackGrid`, `Agent-Tier: worker`, **no** `Co-Authored-By`).
Also decide on the untracked `.mcp.json`: committed config, or gitignored.

## 2. THEN — the raw-output bug (this is the one that shredded your own screen)

Ryan asked you to check your MCP. The `bash` tool ran PowerShell, the output carried SGR
mouse-tracking reports (`ESC[<35;95;34M`, one per mouse move), and the TUI came apart: the
input box repainted four times down the terminal, screenfuls of escape bytes, context
counter climbing while rendering garbage.

**Cause:** `tool_bash` ends `return out or "(no output)"`, and `set_result()` ->
`_update_display()` puts that string straight into the widget. Nothing sanitizes anything.
The escape bytes reach the terminal and drive it.

**Required:**
- Strip ANSI / CSI / OSC and other control sequences from EVERY tool result **before it is
  rendered AND before it is sent to the model**. They are noise in the transcript and they
  burn context you do not have to spare.
- Applies equally to `bash`, `read`, `web_fetch`, `harness`, `skill` and every MCP result.
  Sanitize at the one place all results pass through, not per-tool.
- Second part: a subprocess can leave the terminal in mouse-reporting mode. If a child
  enables tracking and exits without disabling it, the terminal keeps emitting reports at
  whatever is in the foreground -- you. Reset terminal modes after a tool call, or run
  children with mouse reporting suppressed.

**Do not keep the raw bytes for the model "just in case".** The model cannot use them and
they are the thing that costs you context.

## 3. Verification bar — set by the bug you just fixed

Your `--priority` bug survived 30 green checks because the test replaced `Seat.send` with a
lambda: the real subprocess line never executed once. Do not accept a green test that never
ran the real path.

- Exercise the sanitizer on **real captured bytes** -- run an actual command that emits SGR
  mouse reports and feed what it actually produced -- not a hand-typed escape string.
- Include a negative control: text containing a literal `ESC[` sequence that a user
  legitimately pasted should be handled by the same rule, and normal text must come through
  byte-identical. Assert the byte-identity, do not eyeball it.

## 4. Report

Reply to Sentinel (`ba736bd4-d249-42c0-b1ed-04b597d753f0`) with the sha and what you
measured. Full item text is also at the top of `C:/Projects/LiteSuite/TODO.md` under
"LITETUI -- RAW TOOL OUTPUT CORRUPTS THE TUI".
