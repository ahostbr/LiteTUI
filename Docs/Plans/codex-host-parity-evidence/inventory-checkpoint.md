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

Remaining: live tool updates, schema-drift execution refusal, full enabled/disabled
and installed-tool lifecycle, and supported native migration/update mechanism.
The official app-server README documents config/mcpServer/reload as applying refresh
at a thread's next active turn, and installed ClientRequest includes the method.
That alone does not prove dynamicTools can update or that unchanged MCP configuration
reloads tool lists. The MCP bridge route requires a behavioral probe before adoption.
Reference: https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md

This checkpoint does not close C8 and does not grant release clearance.
