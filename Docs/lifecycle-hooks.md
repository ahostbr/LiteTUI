# Lifecycle hooks

Open **Settings → Hooks** or `/hooks` to create, edit, reorder, enable, disable, and test scripts. Choose a scope first. **Save hooks** saves that scope without running a script; **Test script** executes the selected form using LiteTUI's ordinary approval policy and an editable sample event.

Global definitions live in `hooks.json` under the LiteTUI data root. Project definitions live in `.litetui/hooks.json` under the directory from which LiteTUI was launched. Project IDs replace matching global IDs, including disabled definitions. Unreplaced global hooks run first, followed by project hooks, in file order. To override a global entry, select it in Global and click **Override global**, then save Project. Deleting and saving that override reveals the global definition again.

Hooks can observe application startup/shutdown, conversation creation/resume/leave, prompts, tools, and normal completions. Gate mode is available for `prompt_before`, `tool_before`, and `completion_before`. Compaction and the synthetic continuation immediately following it bypass hooks entirely.

Example project file:

```json
{
  "version": 1,
  "hooks": [
    {
      "id": "check-tools",
      "enabled": true,
      "mode": "gate",
      "events": ["tool_before"],
      "executable": "python",
      "argv": ["checks/tool_hook.py"],
      "timeout": 10,
      "sources": [],
      "tools": ["bash", "powershell"]
    }
  ]
}
```

Executables are launched directly with an argument array. Optional `cwd` resolves relative to the launch workspace; optional `env` is a JSON map of environment overrides. The Arguments field uses JSON array syntax. Sources are `typed`, `queued`, `interrupted`, `rpc`, `scheduled`, and `harness`. Empty source/tool filters match all; tool matching is case sensitive and supports `*` and `?`.

Scripts receive one UTF-8 JSON document on stdin with `version`, `event`, `event_id`, `invocation_id`, `workspace`, `source`, `conversation_id`, `turn_id`, `data`, and `truncation`. Data contains the relevant prompt, tool name/arguments/result, or proposed answer. Text is limited to 256 KiB per event; images are represented by metadata. Full conversation history is never sent.

A gate must exit zero and print one JSON object to stdout:

```python
import json
import sys

event = json.load(sys.stdin)
print(json.dumps({"decision": "allow"}))
# To refuse: {"decision": "deny", "reason": "Explain what needs correcting."}
```

Gate crashes, invalid output, timeouts, and policy refusal block the action. An observer's failure is reported without vetoing work; observer output is never added as model instructions. Each output pipe is limited to 64 KiB while remaining bytes are drained. Oversized gate output fails. Timeout defaults to 10 seconds and can be set from 1–300 seconds; timeout/cancellation terminates the owned process tree.

Hook processes use the existing capability policy and approval UI/RPC. A changed execution configuration has a new approval identity. Hook denials allow corrective work; a person's denial still stops the turn. Completion gates allow three internal corrective continuations, then pause. Rejected drafts remain visible. Rejected prompts remain in the current app's `/hooks` list for inspection and explicit resubmission; they are not automatically retried.

Configuration is read for each event into an immutable snapshot. Saves use an atomic replacement under a file lock and merge independent edits; same-entry conflicts require reload. Invalid configuration remains visible and blocks gates. The editor shows a raw JSON repair panel for malformed files and refuses a repair if another process changed the file since reload. `LITETUI_HOOKS=off` disables loading/execution and displays that status. There are no conversation-level overrides.

App shutdown hooks are best effort and cannot open a new approval prompt. Background `tool_after` means actual terminal outcome, not background launch acceptance. Hook status/output stays in the UI; this implementation adds no runtime-log payloads.
