# C8 unknown registration checkpoint

Legacy metadata without a tool_inventory field now preserves an earlier verified
fingerprint only while the native thread ID remains unchanged. A different thread
resets the snapshot; current host schemas are never substituted as proof of what
the resumed native thread registered.

Native dynamic calls with no snapshot are refused before host execution. A
ContextVar sentinel distinguishes ordinary non-Codex calls from native calls with
unknown registration, so the shared gateway still works for other providers.
Refused calls do not drain or attach previously staged tool images.

The output-cleanup test now proves the registered tool actually executes, succeeds,
stages its image during execution, and returns sanitized text. Previously the test
could pass on a refusal response because it asserted only absence of a secret.

Validation: 59 tests pass across test_codex_inventory.py, test_codex_app_server.py,
test_codex_native_policy.py, test_codex_history.py and test_codex_history_pages.py.
Scoped Ruff and git diff --check pass. These are offline transport/gateway/history
checks, not a new live inference or packaged-client run.

This closes the unknown-registration bypass. Live inventory replacement and full
legacy thread migration remain open; this checkpoint does not claim C8 complete.
