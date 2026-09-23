# User-facing diagnostics inventory (LT-FRIENDLY-ERRORS)

Source survey on branch `agent/cobaltridge-friendly-errors`. `present` is the shared presentation mapper: recognized Plain Talk copy is logged with its raw source; Full Detail returns exact originals. An unchanged known message may already be actionable at its source. Unknown error families are not assigned an invented recovery step.

| Surface | Source locations | Presentation |
| --- | --- | --- |
| Startup MCP bridge connect | app.py:1964-1990; mcp_client.py:331-344 | Chat mapper explains offline bridge and recovery. |
| MCP management command, multiline status and dialog | plugins/mcp_manage.py:45-195; mcp_list.py:85-250 | Chat messages, each status row, and dialog status/errors map independently. |
| Backend initial connect/model availability | app.py:4110-4237; llm_backend.py:940-965 | Source-authored Plain Talk and backend recovery; raw exception in Full Detail on connect failure. |
| Streaming/backend turn failure | app.py:7099-7140, 7343-7389 | Card maps source-authored Plain Talk and shows exception in Full Detail. |
| Tool execution/argument errors | app.py:2678-2683, 7510-7555, 8143-8167; plugins/__init__.py:460-498 | Known invalid arguments map on cards; other errors remain specific. Tool result sent to model stays raw. Plugin status listing maps known failures. |
| Context/model load and inference | app.py:4880-4914; plugins/model_switch.py:164-178, 286-312, 418-430, 546-595, 1075-1174 | Existing actionable source copy passes through chat mapper; unknown messages unchanged. |
| Settings validation/save/runtime apply | settings_screen.py:1410-1440, 1605-1685 | Validation keeps field-specific reason; persistence and runtime failures map inline, Full Detail preserves original. |
| Auth/provider and plugin import | app.py:4180-4237; plugins/__init__.py:600-658 | Backend source explanations; plugin status mapped only when rendered, raw status retained in registry. |
| Conversation persistence | app.py:2947-2965, 3150-3175 | Existing memory-only warning passes through chat mapper, raw logged at source. |
| Harness registration/remote services | app.py:2050-2070, 3750-3800 | Existing offline/recovery wording passes through chat mapper, raw logged at source. |
| Paste/voice/media, theme | app.py:6014-6032, 7820-7845, 8350-8400; settings_screen.py:840-896 | Paste and custom theme toasts map; existing source-authored voice/media validation remains unchanged. |
| Missing prompt/context files | app.py:1795-1822 | Existing context warning passes through chat mapper. |

## Coverage after mapping

Plain Talk and Full Detail now share `friendly_errors.present` at the chat boundary, backend turn cards, settings persistence/runtime inline feedback, MCP command and dialog status rows, plugin status listing, custom-theme and paste toasts, and invalid tool-argument cards. The tool result returned to the model remains raw. Backend initial connection and model/context load already provide source-owned actionable sentences in Plain Talk; Full Detail exposes the initial exception and turn-card exception. Conversation persistence, harness registration, missing prompt, model-load guidance, and known voice/media validation are source-authored actionable messages, so the mapper intentionally returns them unchanged. Unknown diagnostics remain unchanged instead of guessing a remedy.

`present` logs rewritten originals in the runtime error sink; Full Detail bypasses rewriting. Settings validation stays field-specific instead of being masked by generic save advice.
