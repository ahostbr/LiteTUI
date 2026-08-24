# T065 — APPROVED PRODUCER LIST (Sentinel ruling, 2026-08-24)

**This file exists because BoldChip is being `/clear`ed and his reasoning does not survive it.
The list below is APPROVED. Commit this file with the work.**

## VERDICT: APPROVED, with ONE revision (#13) and TWO standing constraints.

15 calls at 16 sites, all scalar bounded metadata, aimed at the four blind spots the review
named: persistence, harness, MCP, swallowed plugin failures. The exclusions are right —
theme/settings display catches, normal disconnects and successful high-frequency lifecycle
events are noise and would dilute the signal.

| # | site | event |
|---|---|---|
| 1 | `conversation.py:257` `_raise_to_app` | `persistence_failure` |
| 2 | `app.py:1828` inbox registration | `harness_registration_failed` |
| 3 | `app.py:2408` `_sync_seat_identity` | `harness_rebind_failed` |
| 4 | `mcp_client.py:132` reader loop | `mcp_reader_failed` |
| 5 | `mcp_client.py:307` config load | `mcp_config_failed` |
| 6 | `mcp_client.py:321` server start | `mcp_server_start_failed` |
| 7 | `mcp_client.py:360` tool dispatch | `mcp_tool_failed` |
| 8 | `plugins/__init__.py:333` observer | `plugin_observer_failed` |
| 9 | `plugins/__init__.py:348` finalizer | `plugin_finalizer_failed` |
| 10 | `plugins/__init__.py:456` import | `plugin_import_failed` |
| 11 | `plugins/__init__.py:470` register | `plugin_register_failed` |
| 12 | `plugins/__init__.py:487` activate | `plugin_activate_failed` |
| 13 | `app.py:1914` scheduler due | `scheduler_tick_failed` |
| 14 | `app.py:2768` backend connect | `backend_connect_failed` |
| 15 | `app.py:4237` + `:4339` stream | `turn_stream_failed` |

## 🔴 REVISION — #13: BIND THE EXCEPTION, NEVER WIDEN IT

The proposal says #13 *"requires `except Exception as e`"*. **Read the existing clause first.**

- Already `except Exception:` → adding `as e` is inert. **Do it.**
- Currently **narrower** (`except KeyError:`, `except OSError:` …) → **DO NOT BROADEN IT.**
  Bind and log within the existing type. Widening an except clause changes control flow and
  makes the app swallow failures it currently propagates. **A logging change must never alter
  what the program catches** — that turns an observability task into a behaviour change, in
  the one file where a swallowed error is hardest to notice.

Apply the same test at **every** site: if adding the log requires editing the exception
*type*, stop and report instead of editing.

## STANDING CONSTRAINTS

1. **Names are identifiers, and the sanitizer still bounds them.** `server=`, `plugin=`,
   `name=` carry user-configurable strings from MCP config and plugin manifests. They are
   legitimate metadata, but they route through the same fail-closed cap as everything else —
   no field bypasses the sanitizer because it "is only a name".
2. **`error_type` is `type(e).__name__` and NEVER `str(e)`.** The message text is a body. This
   is the single highest-risk line in the whole task: it is one character of difference and it
   silently exfiltrates prompts, paths and payloads into an always-on log.

## COVERAGE STATEMENT REQUIRED AT COMPLETION

Report which of the four blind spots are now covered and which are not. The row closes only
with that statement attached. Do not let the log's existence stand in for coverage.
