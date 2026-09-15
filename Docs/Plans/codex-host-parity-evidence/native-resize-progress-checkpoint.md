# Native progress and resize acceptance

Task: REL-20260914-CODEX-PARITY

The real LiteTUI Textual layout now has an inference-free lifecycle acceptance
test at terminal widths 110, 48 and 55. It drives a native command through live
output and failed completion, verifies a bounded live buffer with a visible
omission notice, strips ANSI controls, and retains the full authoritative final
result. Completion replaces the preview rather than duplicating it. Duration
and failure state remain available after resize. Collapsed and expanded states
survive resize; progress/completion do not move a reader who scrolled away from
the tail with autoscroll enabled. App-server startup is forbidden in this test.

MCP progress messages previously ran together without separators. They now use
line boundaries, while command output deltas still concatenate exactly. Empty
or non-string updates do not print a fabricated None value. A focused RPC test
checks both paths and cleanup of pending render timers.

Validation: 26 tests passed across test_codex_resize.py, test_codex_tool_ui.py
and test_collapsible_tool_cards.py; scoped Ruff and git diff --check passed.
An existing elapsed-time assertion depended on scheduler latency and failed at
3.4s instead of 3.3s. It now freezes the clock only around the synchronous tick,
without freezing asyncio or weakening the expected displayed duration.

These are source/runtime UI tests using synthetic events, not live native event
capture, screenshot review, packaged application acceptance, or full C4 closure.
The broader release and C2/C8 lifecycle gates remain open.
