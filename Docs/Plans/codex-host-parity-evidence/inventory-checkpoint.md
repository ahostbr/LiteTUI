# C8 inventory foundation, not acceptance

The installed 0.154.0 schema exposes dynamicTools only on ThreadStartParams.
ThreadResumeParams and ThreadForkParams have no dynamicTools field. Resume history
is explicitly marked unstable, Codex Cloud only, do not use. No history injection
or silent thread replacement has been introduced.

Registration now has deterministic ordering and canonical schema fingerprints.
Equivalent duplicate definitions coalesce; conflicting same-name definitions fail
before registration. Active and deferred tools are both excluded when the global
tools switch is off. Saved provider metadata records the actual registered inventory
fingerprint and schema signatures; reconnect preserves that snapshot rather than
claiming newly requested schemas were registered. No tool arguments/results enter
this inventory metadata.

Validation: PYTHONPATH=src `python -m pytest tests/test_codex_inventory.py tests/test_codex_app_server.py tests/test_codex_async_questions.py -q`: **30 passed in 2.66s**.
Tests cover ordering, global disable, conflicting names, changed schemas, deferred
promotion, and reconnect without silent thread reset. No provider calls.

Remaining: live tool updates, legacy-thread migration, full enabled/disabled
and installed-tool lifecycle, and supported native migration/update mechanism.
The official app-server README documents config/mcpServer/reload as applying refresh
at a thread's next active turn, and installed ClientRequest includes the method.
That alone does not prove dynamicTools can update or that unchanged MCP configuration
reloads tool lists. The MCP bridge route requires a behavioral probe before adoption.
Reference: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md

This checkpoint does not close C8 and does not grant release clearance.

## Dispatch drift follow-up

For threads with a recorded registration snapshot, dynamic tool requests now compare
the registered signature to the live gated registry before entering the shared
executor. Removed, disabled, changed, and unregistered tools return a failed tool
result without executing. Unchanged and deferred-to-active promoted definitions pass.
A task-local registration context also rechecks at the shared execution door after
human authorization and before-hooks, closing the ordinary approval-wait drift gap.
The context does not affect other backends. Existing threads with no recorded
snapshot remain explicitly outside this guarantee until migration is implemented.

Validation: PYTHONPATH=src `python -m pytest tests/test_codex_inventory.py tests/test_codex_app_server.py tests/test_codex_async_questions.py tests/test_codex_steering.py tests/test_codex_tool_ui.py tests/test_codex_questions.py tests/test_codex_native_policy.py -q`: **70 passed in 4.05s**.
The actual shared LiteTUI._execute_tool door is exercised with schema/disable/remove
changes during authorization. Existing synthetic dispatch fixtures were corrected
to provide their live registry and register the tool they invoke, rather than
bypassing the new inventory guarantee. Ruff passes the five changed focused
source/test files; the app.py edit is four lines and covered by the shared-door tests.
No provider calls or conversation resets. These checks do not prove supported live
inventory refresh or close C8.
