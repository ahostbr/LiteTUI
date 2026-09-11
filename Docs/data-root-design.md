# T618 — opt-in durable data root

## Decision and boundaries

Introduce **`LITETUI_DATA_ROOT`**, resolved once by the shared implementation `paths.data_root()`. Set it before starting LiteTUI. A nonempty value expands `~` and resolves relative paths against startup working directory. Unset or empty preserves the existing `paths.ROOT` fallback exactly. There is no automatic migration, default relocation, or CLI flag in this change. Ryan must approve any future default change.

Three locations are deliberately distinct:

- **Bundled resources:** package prompts/schemas and existing per-file prompt overrides. Their anchors do not change.
- **Durable application data/config:** the override currently owns settings.json, .convos (transcripts, memory, goals), background-tasks.json and background task output paths. The T618 fence extension also includes scheduler jobs.json and runtime diagnostic logs. `settings_path(root=...)` retains explicit caller precedence.
- **Tool workspace:** cwd/--cwd, workspace authorization, skill discovery and MCP configuration anchors remain unchanged. `paths.ROOT` is NOT repurposed.

This is not a complete application sandbox. **Incomplete migration:** generated llama configuration/logs, skill caches, fleet/inbox state and MCP configuration still follow their existing locations. The subprocess-test isolation card must disable these paths or obtain a separately reviewed extension per root; setting the variable alone does not prove isolation.

## Compatibility and migration policy

Existing checkout users do nothing: for Ryan's checkout the computed background ledger remains `C:/Projects/LiteTUI/background-tasks.json`. Tests assert that path arithmetically and never open, recreate or delete that live file. Installed packages also retain their legacy default until an explicit choice is made; this change does not present that default as an appropriate long-term distribution policy.

Opt-in users must stop all writers, back up the old settings/.convos/background ledger and associated output, copy them to the chosen root, then launch with the environment variable. No implicit copy, deletion, merge, or overwrite occurs. Stored absolute output paths may still refer to the old tree and require inspection before moving/removing it. Rollback is unset the variable and return to the retained old data, acknowledging any divergence created after the copy. A future default migration needs discovery, dry-run/conflict policy, backup, confirmation and rollback design approved by Ryan.

## Acceptance

1. With the variable unset, assert all changed default anchors equal their previous paths; no live-store I/O.
2. With a temporary override, create settings and a conversation in one process, exit, start a second process and verify settings plus transcript replay.
3. Assert package prompts and ROOT did not move in either arm; verify explicit settings root still wins.
4. Verify background load/start/finish/save/tail all use the same resolver. Neither arm may load a model or call a service.
5. Before declaring installed-package lifecycle complete, repeat create → persist → restart → resume against a built wheel in a clean environment. The source-subprocess regression here is not that distribution acceptance.
