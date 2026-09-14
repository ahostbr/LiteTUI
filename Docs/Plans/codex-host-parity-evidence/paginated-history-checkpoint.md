# C3 paginated native history

Installed 0.154.0 generated schemas define Thread.historyMode as legacy/paginated,
default legacy, and ThreadTurnsListParams supports ascending pagination with
itemsView=full. ThreadReadParams marks full hydration deprecated for paginated
threads. The reader now first obtains thread metadata without turns. Legacy
threads use the supported full read; paginated threads follow every nextCursor
with full item detail in native ascending order. Resume responses for paginated
threads use the same hydration path before display reconciliation.

Repeated/invalid cursors, malformed pages, and summary/notLoaded item views raise
ProviderError rather than committing partial history as complete. Repeated turn
identities retain one row with the latest page's content. Exact native thread ID
is validated on both legacy read responses. None of these requests sends input.

Validation: 40 history/pagination/transport/GUI-response tests pass in 2.63s.
Coverage includes two full pages, all page-failure cases, legacy identity drift,
and actual AppServerTransport resume under both history contracts without sending
recovered content as input. Scoped Ruff and diff checks pass.

The broader test run exposed a test timing race: its initial scroll request had
not settled before sampling the expected position. The rendered regression now
uses the existing bounded settle_until helper for the requested position and
completed redraw, preserving the original exact-position assertion.

This is schema/source plus synthetic protocol evidence. A live multi-page history
journey remains unverified. The earlier live legacy restart evidence is retained;
the changed reader now issues an additional metadata-only read for legacy threads.
No live delegation or inference was run for this checkpoint. Other full-plan and
client gates remain open.
