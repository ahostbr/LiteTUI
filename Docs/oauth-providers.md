# OAuth providers in LiteTUI

Use `/backend codex`, then `/model`. Sign in first using `codex login` and start
Codex once so its model metadata cache exists. `/backend` displays login status.
LiteTUI reads the official CLI's `auth.json` and `models_cache.json` under
`CODEX_HOME` (default `~/.codex`). It never writes either file, launches Codex,
refreshes a token, or falls back to an API key. After credentials expire, sign in
through the CLI again and use `/reconnect`.

`/modelcfg` offers the model's supported reasoning efforts. Local load/unload,
sampling, context-size and output-token controls do not apply to Codex: its
response limits are provider-managed. The context gauge uses the CLI model
metadata's effective window. Backend switches retain LiteTUI's transcript and
are refused while a chat/compaction turn is running.

Normal chat, persistence-first compaction, result summarization and subagents
share the inference boundary. Only LiteTUI executes tools and schedules further
requests. Subagents remain one-shot and do not receive parent history or tools;
on Codex, `think=false` selects its lowest supported effort and says so in the
result. To override their model, choose an ID in the active provider's catalogue.

Provider-specific opaque reasoning is stored alongside an assistant message and
replayed only to the same provider/model. Other providers receive the ordinary
conversation and tool history without those opaque blocks.

## Claude status

Claude is not offered in this version. The bounded direct Messages/OAuth probe
returned HTTP 429 on 2026-09-10 before a successful custom-tool round trip. This
is not proof of incompatibility. The exploratory transport and synthetic probe
remain available for development, but the production factory refuses `claude`
instead of silently choosing a local backend. The probe preserves LiteTUI's
system prompt; it does not prepend a Claude Code identity. `--bare` is not used.

## Verification

`e2e/oauth_probe.py codex gpt-5.5` sends synthetic context, receives a custom
tool call, supplies its result on a separate request and checks continuation.
It executes no tools and never rotates tokens. `e2e/oauth_tui_smoke.py` exercises
the actual Textual chat and compaction consumers with temporary conversation
state and exports a screenshot. Both are explicit opt-in live scripts, outside
default pytest collection. They consume subscription usage.

Implementation reference: Pi checkout 421c03efb, provider wire formats, MIT
license. LiteSuite's runtime-delegating adapters were inspected but are not used.
