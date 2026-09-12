# LiteTUI configurable lifecycle hooks

## Summary

Add a native hooks system with a full settings UI, global defaults, and project overrides. Reuse LiteTUI's existing authorization, approval, plugin, persistence, and process infrastructure.

Hooks can observe lifecycle events and gate incoming prompts, tool execution, and completion. Hook denials allow agent recovery. **Compaction is entirely excluded.**

Grounded in checkout `eba3f41` and Sentinel's configuration recommendation. Planning consultation with Sentinel was completed. This document records the finalized small Quizmaster plan; implementation has not started. Recheck the current checkout and ownership before implementation.

## Configuration and script contract

- Store global definitions in `<paths.data_root()>/hooks.json` and project definitions in `<launch-workspace>/.litetui/hooks.json`. Capture the launch workspace once; do not use the application's installation root as the project.
- Load global entries followed by project entries. Project entries replace matching global IDs, including disabled entries that suppress a global hook. Remaining global hooks run first, then project hooks, in file order.
- No conversation-specific definitions or overrides. No hooks configured means unchanged behavior.
- Use versioned JSON with ordered hook entries containing: ID, enabled state, events, `observe` or `gate` mode, executable, argument array, optional working directory/environment overrides, timeout, source selections, and tool-name patterns.
- Default timeout: **10 seconds**, configurable from **1–300 seconds**. Match tools with case-sensitive exact names or `*`/`?` wildcards; empty filters match all. No argument expressions or content matching.
- One configuration owner handles validation, loading, and saving. Reuse atomic replacement and baseline-merge persistence, adding per-file cross-process serialization. Preserve unrelated hook edits; report conflicting edits to the same hook instead of silently overwriting.
- Saves and explicit reloads apply to the next event; each event uses an immutable configuration snapshot. Invalid configuration stays visible and prevents gated actions until corrected; never silently discard broken gates.
- Provide `LITETUI_HOOKS=off` for explicit host/test isolation, visibly reported in the UI.

Launch executables directly with argument arrays and `shell=False`. Send a versioned UTF-8 JSON document on stdin containing event/invocation IDs, conversation/turn IDs when available, source, workspace, and relevant event data.

Gate scripts must exit successfully and return one JSON object:

```json
{"decision": "allow"}
```

```json
{"decision": "deny", "reason": "Run the required checks first."}
```

A denial requires a nonempty reason. Nonzero exit, timeout, malformed output, or invalid verdict blocks the guarded action. Observation hooks use exit status for success and cannot supply decisions or model instructions.

Capture stdout and stderr separately, capped at 64 KiB each while continuing to drain pipes. Oversized gate output is invalid. Truncate oversized event text at 256 KiB with explicit truncation metadata; include image metadata rather than embedded image bytes. Runtime logs remain metadata-only.

## Runtime integration

### Events and execution boundaries

| Events | Behavior |
|---|---|
| `app_start`, `app_shutdown` | Observe once per process lifecycle; shutdown is best effort during graceful exit. |
| `conversation_start`, `conversation_resume`, `conversation_leave` | Observe actual conversation transitions, without creating empty conversations solely for hooks. |
| `prompt_before` | Observe or gate external input before it enters model context. |
| `tool_before` | Observe or gate an otherwise eligible tool invocation. |
| `tool_after` | Observe the terminal outcome of an executed tool, including failure or cancellation. |
| `completion_before` | Observe or gate a proposed normal completion. |
| `completion_after` | Observe accepted completion only. |

- Route typed, queued, interrupted, RPC, scheduled, and harness inputs through one prompt-admission helper. Evaluate once at delivery, preserving existing queue ordering and tool-result pairing. A rejected prompt is shown with its reason and retained for inspection or resubmission; it is not automatically retried.
- Internal corrective continuations bypass prompt hooks. Compaction requests, tools executed during compaction, and the synthetic post-compaction wake invoke no hooks.
- Integrate tool gates into the existing `_execute_tool` boundary. Keep tool toggles, resolution, policy denials, standing rules, and human approvals authoritative. Hooks may further restrict an action; they cannot grant permissions or rewrite arguments.
- For background tools, run the before hook once before launch and the after hook once when the work actually finishes. Background acceptance is not completion.
- Completion gates run before existing completion finalizers and successful RPC completion signals. Keep rejected drafts in the transcript with a visible rejection status.
- Feed completion-denial reasons back through a tagged internal continuation. Permit **three corrective continuations per external turn**, subject to existing iteration/cancellation limits; another rejection pauses visibly. Accepted completion runs existing finalizers once.

### Shared authorization and process handling

- Extract the reusable authorization portion of the existing tool boundary into one helper used by tools and hook processes. Preserve `tool_policy.evaluate`, capability classification, standing-rule storage, `ToolApproval`, and TUI/RPC approval routing.
- Authorize hook processes using the existing shell/process policy and the action's effective profile. Do not create another approval dialog or permission store.
- Give each hook a stable approval identity incorporating its scope, ID, and execution configuration, so changing its command does not inherit approval for an unrelated command.
- Runtime hook execution bypasses hook dispatch itself, preventing recursive hook invocation. This does not bypass authorization.
- Reuse `ttyguard` and process-tree termination. Keep execution off the UI thread, capture output, prevent Windows console flashes, and cancel children on timeout or user cancellation.
- A policy-refused gate is a failed gate. A policy-refused observer is reported and skipped. Graceful shutdown observers cannot open new approval prompts.
- Run matching hooks sequentially. Aggregate gate refusals with deny-wins semantics; observation failures do not veto actions. Explicit user cancellation stops further execution.
- Return hook refusals as structured internal results that existing tool-result rendering can expose. Preserve human-denial stop semantics; do not reuse that stop flag for recoverable hook refusals.

## Settings UI

Add a **Hooks** tab using the existing settings/modal/sidebar infrastructure, with `/hooks` opening the same manager.

- Show configured and effective hooks, scope, overrides, enabled state, event selection, mode, timeout, and last-run status.
- Support create, edit, duplicate, delete, enable/disable, reorder within scope, save, and reload.
- Provide executable and argument-list controls, source/tool filters, working directory, and environment overrides. Validate inline; unsupported event/mode combinations cannot be saved.
- Clearly distinguish editing a global definition from creating a project override. Deleting an override reveals the inherited global entry.
- Saving configuration does not execute it. Failed saves preserve the previous file and keep the editor open.
- The **Test** panel provides an editable sample event and runs the selected hook through the real authorization and process runner. Show verdict, reason, exit code, elapsed time, stdout, stderr, timeout, and truncation.
- Test results do not modify conversation context or trigger lifecycle events. Label the action as executing the configured script.
- Reuse existing status and notification surfaces for failures. Do not add a separate telemetry system.

## Verification and execution workflow

Implement in an isolated worktree after reconciling current ownership and obtaining the required task ID. Preserve existing dirty/untracked work.

1. Write focused failing tests for configuration precedence, gating, event delivery, completion recovery, and UI persistence.
2. Implement configuration/runtime modules and the shared authorization extraction, then wire lifecycle boundaries and the editor.
3. Debug failures through the actual callers; verify, obtain independent review, and finish the branch with required task trailers. Leave merging to the leader.

Acceptance requires:

- A real fixture script receives stdin JSON and can permit or prevent a tool side effect.
- Existing policy denials remain denials; human denials still stop; hook denials reach the model and permit recovery.
- Prompt gates cover every external input path exactly once.
- Completion rejection produces corrective work, respects the three-continuation limit, and never reports rejected work as successful completion.
- Compaction and its tool calls produce zero hook invocations.
- Background completion fires once; cancellation and timeout terminate fixture child processes.
- Two scopes, disabled overrides, reload failures, concurrent edits, and isolated data roots behave as specified.
- UI mouse and keyboard tests cover authoring, validation, saving, testing, and modal/sidebar operation.
- Tests never read live hook configuration, register live harness seats, or contact a model service.

Run the focused tests first, then `uv run --locked pytest` and `uv run --locked python tools/tool_door_gate.py`. Run the project's required lint/type checks before committing. Complete a rendered TUI inspection with harmless local scripts; build success alone does not establish that hooks are reachable.
