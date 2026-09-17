---
name: catalog-web-library
description: Catalog a web library/marketplace into local artifacts — navigate every asset card via chrome-bridge, extract embedded JSON + screenshot per item, download all images in parallel, then build a human HTML door + LLM-friendly index. Triggers on 'catalog my library', 'crawl this site', 'index these assets', 'save all the descriptions and images'. Proven end-to-end on Fab.com (815 assets, ~4,300 images, zero failures).
---

# Catalog a web library into local artifacts

Turn any browsable web collection (marketplace library, gallery, dashboard) into:
per-item records (jsonl), downloaded media, a human-browsable HTML site, and an
LLM-friendly index. Proven on Fab.com's /library (815 assets). Working reference
implementation lives in `C:\Projects\LiteTUI\temp-working-dir\fab-library\`
(`crawl.py`, `download_images.py`, `build_site.py`, `finalize.py`) — adapt it,
don't rewrite from scratch.

## Phase 0 — prep (do all of this before touching the browser)

1. **Ask the user to scroll the target page to the bottom.** Lazy-loaded pages
   only render cards that have been scrolled into view; count what's in the DOM
   AFTER full scroll or you'll crawl a fraction of the library.
2. **Start the persistent relay** so your crawler and your own `chrome` tool can
   coexist (direct-mode scripts hold port 7461 exclusively — see gotchas):
   ```
   python C:\Projects\LiteTUI\tools\chrome-bridge\bridge.py serve
   ```
3. **Identify the target tab** (`c.tabs()`) and note its `tab_id`. The crawler
   will navigate THAT tab, so warn the user not to browse it mid-crawl.

## Phase 1 — discover cards (one page read)

On the fully-scrolled page, enumerate every card link in one JS pass:
```python
c.content(selector="a[href*='/listings/'], a[href*='/assets/']", tab_id=tab_id)
# or grab all <a> hrefs and filter by pattern; dedupe on uuid
```
Record the total count — it's your progress denominator.

## Phase 2 — crawl each card (checkpoint per item)

For every card: `navigate(url, tab_id)` → read the page's **embedded JSON blob**
(Next.js-style `__NEXT_DATA__` or equivalent script tag) rather than scraping DOM
text — titles/seller/price/rating/tags/description are all in there. Then a
viewport screenshot (`c.screenshot(path=..., tab_id=tab_id, format="jpeg",
quality=70)`). **Append each record to `records.jsonl` immediately** (one JSON
object per line) — that checkpoint is what makes the crawl resumable after any
crash: on restart, load existing ids and skip them.

Pacing: navigate + extract + screenshot at ~9–17s/item needs no throttling
(300+ items observed clean). Log progress every 25 items with a running count.

## Phase 3 — parallel image downloader (separate process)

A second script polls `records.jsonl`, and for each new record downloads the
largest rendition of each media item into `images/<uuid8>/`. Track completed
URLs in `img_manifest.json` so restarts skip work; log failures to a separate
file. Auto-exit when records are stable ~10 min with nothing pending.

## Phase 4 — finalize (auto)

A third script polls every 60s for: crawl log contains `DONE` (or crawler process
gone + records stable 10 min = crash safety net, ship partial) AND downloader
exited → run the site builder once → write `final_status.json`. The site builder
emits three artifacts into the user's artifacts dir:

- **index.html** — human door: dark theme, search box, category/seller/price
  filters, sort, responsive card grid with local thumbnails (JS onerror fallback
  to CDN URL for not-yet-downloaded images), header stats line.
- **llm-index.jsonl** — one compact JSON object per asset (title, id, url, price,
  free flag, rating, tags, description truncated ~600 chars) for LLM reference.
- **INDEX.md** — human-readable summary of the above.

## Launch everything detached (survive app restarts)

Write a `.bat` per job with absolute paths + `>> log 2>&1`, then:
```
schtasks /create /f /tn <Name> /tr "C:\...\run.bat" /sc once /st 00:00
schtasks /run /tn <Name>
```
(The `/ST earlier than current time` warning is harmless.) Verify with netstat +
PID checks, not just "command returned".

## Gotchas (measured — do not re-litigate)

- **Direct-mode port contention**: a script binding 7461 does NOT listen on 7462;
  every other client fails `port in use but no relay answered`. Always run the
  relay for long jobs. Verify coexistence: `chrome` tool ping → `"mode": "relay"`.
- **`Chrome()` does not auto-start**: you MUST call `.start()` (or `with Chrome()`)
  or every call waits 15s then raises the misleading "extension is not connected".
- **MV3 eviction**: after killing a direct server, the extension's service worker
  may take up to ~30s (alarms tick) to redial 7461. A relay-client call failing
  right after can be transient — retry once before debugging. Check netstat for an
  ESTABLISHED pair (relay pid ↔ chrome.exe network-service pid).
- **Background-tab screenshots**: the `chrome` tool's shot captures only the ACTIVE
  tab; use bridge.py directly with `tab_id=` (`restore=True` default puts the old
  active tab back, so the user's focus is preserved).
- **Unreliable booleans in embedded JSON**: e.g. Fab's `listing.isFree` was falsy
  for EVERY asset including $0 ones — derive flags from ground truth (price == 0)
  and cross-check against titles before trusting any single field.
- **Two card kinds**: marketplace pages often mix `/listings/<uuid>` and
  `/library/assets/<uuid>` cards; the same payload can be top-level OR nested
  under `.listing`. Handle both or half your records come back empty.
- **Stale browser tabs**: after rebuilding a local site file, the open tab shows
  the old version — `navigate` to the same URL in that tab before screenshotting
  to verify.

## Verify before claiming done

Screenshot the actual rendered page (not just "build succeeded"), check header
stats against record counts, confirm image dir size matches manifest count, and
only then report green. Partial snapshots mid-crawl are fine for browsing — say
explicitly that artifacts are partial until finalize runs.
