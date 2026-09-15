# Native async question notification accepted

Task: REL-20260914-CODEX-PARITY

scripts/codex_async_question_probe.py made one bounded Astra Medium turn in a
temporary Codex home. A verified trusted PreToolUse gate allowed only the async
question tool and tool search; delegation was disabled. The native app-server
emitted item/completed with delivery=async and the exact requested synthetic
question/options. The turn completed without receiving an answer.

native-async-question-live.json records accepted=true, the verified hook trust,
exact question comparison, turn completion, process close and temporary-home
cleanup. Operational evidence contains booleans rather than conversation text.
Scoped Ruff passes for the new script.

This confirms the native event contract previously supported only by source and
synthetic host tests. It does not exercise host question rendering, user submission,
durable answer delivery, disk reload or GUI/Suite behavior. Those remain separate
C7 acceptance gates. The command-launch policy blocker was not bypassed: this
probe requested no shell/file actions and did not rerun background commands.

Native handler reference:
https://github.com/openai/codex/blob/rust-v0.154.0/codex-rs/core/src/tools/handlers/request_user_input_async.rs

No merge, push, deployment or release occurred. Full-plan acceptance remains open.
