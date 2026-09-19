# Owed Items — LiteTUI + fleet (2026-09-19)

Sentinel handoff tracker. Companion to `branch-census-2026-09-19.md` (that doc = every
branch; this doc = the open *work*). Every row carries a decided state or names what it
waits on and who owes it. No row is left "undecided."

State key: **LIVE** (a seat is on it now) · **HELD** (finished/ready, waiting on Ryan) ·
**PARKED** (blocked on a gate — VRAM, a decision) · **BANKED** (queued, not started) ·
**MERGED/DROP** (resolved). All shipped work below is **UNVERIFIED by Ryan end-to-end**.

---

## 1. CyanBrace 3-item order — **LIVE** (`c33ce9cf`)

Ryan authorized ("cyanbrace is g2g", msg `88b3bbdf`). Building, nothing pushed to `main`
yet (tip still `9d7bf74`). Told to `git pull e3957d9` first (items 2/3 build on it) and fold
its own `app.py` WIP.

| # | item | what |
|---|---|---|
| 1 | agent-loop **loop-breaker** | N identical `tool_name`+args (or same result twice) → block + nudge |
| 2 | **stateful/hashed chrome shot** | "identical to previous screenshot" — hash, skip re-attach |
| 3 | **chrome `scroll` action** + panel text dump | new action, builds on shot pipeline |

**Owed by me:** review-merge each on its report — **intent gate** (pull Ryan's exact words
from the JSONL, map each clause to the diff, send back anything unmapped). Added to
stop-watch (`c33ce9cf → b874c569`) so an idle CyanBrace is visible.

---

## 2. Six unmerged LiteTUI branches — **HELD** (Ryan's merge/drop call)

Verified 2026-09-19 vs `origin/main` (patch-id aware; all 6 present, none an ancestor).
Recommendation is mine; **the decision is Ryan's.**

| branch | uniq | last | what it is | my read |
|---|---:|---|---|---|
| `ci/advisory-scope` | 1 | 08-24 `a8c5be7` | pin both advisory lint tools to one path list | **DROP** — superseded by the T855/T876 sweep work on develop |
| `probe/t632-real-child` | 1 | 09-11 `6149cbe` | a test the T632 real-child arm shipped without (abort releases a live parked ask) | **REVIEW→MERGE** — it's a missing test for shipped code, cheap to land |
| `wip/ryan-0914` | 1 | 09-16 `16a278d` | **71-file dirty-tree bank "as found"** — 60 files, +18,881/−499 (YouTube tools, pccontrol, thinking-caps tests, chrome-bridge scripts) | **ORPHANED — do NOT bulk-drop.** It's a raw snapshot, not a feature. Needs a per-area triage: some may already be on `main`, some may be live work. Rescue the wanted hunks, then drop. |
| `drive/t863-ninfer-free-column` | 1 | 09-17 `817e921` | one NInfer FREE-column state, "without touching anything real" | **REVIEW→MERGE** if the NInfer column is wanted; else DROP |
| `feat/t806b-ninfer-wiring` | 1 | 09-17 `029fec5` | **local-only** `wip(t806): delta before the ownership split` | **HELD** — a mid-split WIP delta; confirm whose it is before touching. Local, unpushed. |
| `feat/subagent-sol-high` | 3 | 09-12 `4b0cfd7` | subagent explicit reasoning-effort + codex Fast-mode-off + tier wire contract | **REVIEW→MERGE** — 3 real commits, standalone feature |

⚠️ Correction to the pre-compaction note: `wip/ryan-0914` is **not** cleanly "absorbed by
feat/t753" — it's an 18k-line as-found dirty-tree grab. Treat as ORPHANED-rescue, not DROP.

---

## 3. LiteBench small-tier front-seat bench — **PARKED** (VRAM-gated)

Ryan's original ask: find the **smallest / least-VRAM** LM Studio model that reliably does
the middleman role = simple tool-calling + short TLDR + LiteSuite tooling + can ping Sentinel
("essentially all a system prompt"). Each LM Studio worker also drags a Whisper+TTS (3
models/3 hops) — which is why Venus (3-in-1) matters; that cost goes in Axis 2.

- **Wired + pre-screened.** LiteBench (`C:/Projects/LiteBench`) is endpoint-agnostic;
  `agent-harness.ts` native-tool gate handicaps the distilled-4b (a known, wanted signal).
- **Blocked on:** a **Sentinel-approved VRAM load** — hard rule, no model loads without
  `lms ps` empty + explicit approval. Last check: `lms ps` empty, 32 GB free.
- Bar to beat: Ninfer qwen — ~150 tok/s 27B, 500+ tok/s 35B-A3B (Ryan's measured numbers).
- Proposal: `LiteSuite/Docs/Plans/realtime-voice/ws-f-realtime-venus-proposal.md`
  (two-axis scoring: Axis 1 worker capability on LiteBench; Axis 2 full spoken-turn stack
  cost vs Venus). **UNVERIFIED, VRAM-gated.**

**Owed:** Ryan's go on the load, then I run it.

---

## 4. @aiDotEngineer transcript batch pipeline — **BANKED** (not started)

From OpenBolt (`782ab539`). Batch-download + summarize the @aiDotEngineer talk transcripts.
- Scripts: `LiteTUI/temp-working-dir/batch_download.py` + `batch_summary.py`.
- Model `qwen3_6_35b_a3b` on `:52107` (Ninfer, already the running engine — no new load).
- Docs: `LiteTUI/.convos/.../memories/direct-timedtext-pipeline.md`.

**Owed:** Ryan's priority call — start it or leave banked.

---

## 5. Shared-checkout hygiene — **note, not a task**

`C:/Projects/LiteTUI` working tree still holds **CyanBrace's uncommitted WIP** (`app.py`,
`prompts/systemprompt.md`, `pyproject.toml`, `uv.lock`) — left untouched; all my commits were
staged by hunk. Stray `refs/remotes/localmain` is harmless (no such remote). Don't `git add -A`
or `git checkout` broadly here while CyanBrace is live.
