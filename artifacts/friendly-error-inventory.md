# User-facing diagnostics inventory (LT-FRIENDLY-ERRORS)

Source survey on branch `agent/cobaltridge-friendly-errors`. Line references are source locations (not rendered-line offsets); dynamic text may vary. `system_message` (app.py:5146-5174) is the shared chat-log boundary for app/plugin system lines; `friendly_errors.display_error` recognizes MCP connection/config failures there, retains unrecognized messages verbatim, and logs rewritten original lines through `runtime_log.record_error` (runtime_log.py:283). Full Detail bypasses rewriting. This is a **surface inventory**, not a claim that each individual error string has a bespoke rewrite.

| Surface | Source locations | Current handling / risk |
| --- | --- | --- |
| Startup MCP bridge connect | app.py:1964-1990; mcp_client.py:331-344 | `[!] mcp name: MCPError: cannot reach URL: reason`; Plain Talk explains offline bridge, chat continuity, startup action. |
| MCP management command and status | plugins/mcp_manage.py:45-77, 129-195 | Status rows and `Could not connect/reconnect`, declaration errors; command notices pass through shared chat boundary. Status row is **not** a system-message classification target when embedded in multi-line listings. |
| Backend initial connect/model availability | app.py:4110-4237, llm_backend.py:940-965 | Exception logged raw and summarized by `_plain_backend_error` (app.py:415-485), backend-specific recovery hint. |
| Streaming/backend turn failure | app.py:7099-7140, 7343-7389 | Error card uses `_plain_backend_error`; raw exception recorded separately. Card is outside `system_message`, so Full Detail does **not** change card copy yet. |
| Tool execution/argument errors | app.py:2678-2683, 7510-7517, 8143-8151; plugins/__init__.py:460-498 | Tool-result/card and plugin status channels, not generic chat lines; technical result stays available to model. |
| Context/model load and inference | app.py:4880-4914; plugins/model_switch.py:164-178, 286-312, 418-430, 546-595, 1075-1174 | Command and settings messages mostly through `system_message`; known backend wording already supplied at source. |
| Settings validation/save/runtime apply | settings_screen.py:254-263, 1410-1440, 1595-1630, 1650-1668 | Inline `#set-error` (not chat boundary), field-specific validation and persistence failure detail stays visible in both modes. |
| Auth/provider and plugin import | app.py:4180-4237; plugins/__init__.py:600-658 | Backend errors use backend-specific explanation; plugin status registry retains exception text, not rewritten by chat boundary. |
| Conversation persistence | app.py:2947-2965, 3150-3175 | Explicit memory-only warning; raw error recorded. |
| Harness registration/remote services | app.py:2050-2070, 3750-3800 | Existing offline message with raw error log; remains unchanged. |
| Paste/voice/media, theme | app.py:6014-6032, 7820-7840, 8350-8400; settings_screen.py:840-896 | Toasts or inline status, not chat system lines. |
| Missing prompt/context files | app.py:1795-1822 | Chat warning explains reduced model context; unknown patterns preserved. |

A source-wide search of `notify(`, `system_message(`, `_system(`, `#set-error`, `record_error`, `raise BackendError`, `raise MCPError`, and plugin status assignments locates additional command-specific variants. Unknown failures deliberately remain verbatim; inventing a generic recovery action would mislead. Follow-up required for Full Detail parity on direct cards, toasts, inline settings and multi-line MCP status, and a categorized mapping for their specific error families.
