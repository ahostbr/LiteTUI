# C3 native MCP binary display correction

Native mcpToolCall results previously serialized standard MCP image/audio data and
embedded binary resource blobs directly into the display result. They could then
enter tool cards, RPC tool_result text and saved display_trace records as base64.

The shared mapper now replaces those standard binary content entries with attachment
descriptions. It preserves MIME type, embedded resource URI/metadata, ordinary text,
text resources and structured content. It does not mutate the native result object
that Codex owns. Shared escaping and secret sanitization still apply afterward.
New native events and reconciliation through the same mapper use this behavior.

Validation: 43 tests pass across tool UI, history and app-server transport. The new
test covers image, audio, binary/text resource and ordinary text in one result,
asserting no binary sentinel reaches saved display records or RPC output and that
the original native result stays identical. Scoped Ruff passes. No provider calls
are needed for this presentation change.

This is not a full attachment viewer/export implementation, nor a claim that every
historical persisted trace has been migrated. Those C3 acceptance requirements
remain separate.
