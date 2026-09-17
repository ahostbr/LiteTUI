---
name: minecraft-agent
description: Play Minecraft through the mcbridge MCP tools, and consult the Direwolf20 transcript corpus for era-correct modded-tech guidance. Use when asked to play, build, mine, automate, or operate machines in Ryan's F:\MC packs, or when a question needs "how did this actually work back then" for BuildCraft, IC2, Forestry, RedPower, Railcraft, EE2/ProjectE, AE2, Logistics Pipes or Mekanism.
---

# Minecraft agent

Two separate assets that work together. **The corpus tells you what to do; the
bridge lets you do it.** They are independent — the corpus is useful with no
game running, and the bridge works with no corpus.

---

## Part 1 — The Direwolf20 corpus

`F:\MC\DW20-Guides\` — 209 transcripts of Direwolf20 mod spotlights and
playthroughs, chosen to match the *specific mods* and *specific eras* of the
packs in `F:\MC`. He was the pre-FTB distribution channel for modded
Minecraft; these spotlights are how these packs were learned in the first
place.

```
GUIDE.md      routing: version-match table, per-pack listings, task routes
INDEX.json    one record per transcript (machine-readable)
transcripts/  spotlights-og2012/ ftb-retro-1.2.5/ spotlights-1.12/ lets-play-s9-1.12/
```

### The one rule that matters: filter by era first

Every record carries `pack` and `era`. **Use them before you read anything.**

| Question about | Filter to |
|---|---|
| `MC1.1-2012_OG` | `pack == "MC1.1-2012_OG"` (98 records) |
| `1.12.2-Legacy` | `pack == "1.12.2-Legacy"` (111 records) |

Same mod, different decade, different recipes and different blocks. A 1.12
answer to a 2012 question is worse than no answer, because it is confidently
wrong. Filter, then read.

```python
import json
rec = json.load(open(r"F:\MC\DW20-Guides\INDEX.json", encoding="utf-8"))
hits = [r for r in rec if r["pack"] == "MC1.1-2012_OG" and "bees" in r["topics"]]
```

Fields: `mod`, `pack`, `era`, `versions`, `topics`, `path`, `video_id`, `url`, `chars`.
Topics are `power mining pipes sorting ic2proc bees farming rail emc redpower menet
autocraft storage`.

### Reading a transcript

Plain UTF-8, timestamped `[HH:MM:SS]` per caption cue. Cite the timestamp and
the `url` so Ryan can jump to the moment in the video.

> ⚠️ **These are YouTube auto-captions, not human transcripts.** Block and mod
> names are mangled throughout — expect creative spellings of every proper
> noun. **Search loosely; exact-string grep will miss things.** Retrieval and
> fuzzy matching work fine.

Coverage follows what Direwolf20 actually made, so it is uneven on purpose.
Forestry and IC2 are deep. Rei's Minimap, the IC2 Thermometer and zipline have
no spotlight at all — no amount of searching produces one.

---

## Part 2 — Playing, via the mcbridge MCP

A companion NeoForge mod runs **inside Minecraft's JVM** and exposes a
loopback HTTP API; an MCP server wraps it as tools. Because it runs in-process
it sees real modded blocks, real block entities and real container slots — not
a vanilla approximation.

```
you ──MCP──▶ server\mc_mcp.py ──http://127.0.0.1:25585──▶ mcbridge mod ──▶ game
```

**Source:** `F:\MC\mc-agent-bridge\` (`mod/` Java, `server/mc_mcp.py` Python).
Target is `1.21.1-Modern` — NeoForge 21.1.248 on Java 21.

### Before anything: check it is alive

```
mc_health()
```

This is the one call that distinguishes the three failure states, which look
identical from every other tool:

| Response | Meaning |
|---|---|
| cannot reach the bridge | Minecraft is not running, or the mod is not installed |
| `in_game: false` | it is running, but sitting on the title screen |
| `in_game: true` | ready |

### The loop

**Observe → aim → act → read the result.** Never act blind.

```
mc_observe(radius=12)          # budgeted snapshot
mc_look_at(x, y, z)            # aim at the target
mc_mine(ticks=40)              # hold attack; vanilla handles breaking speed
```

Every mutating tool already returns a compact `now` block — position, health,
held item, crosshair target, whether a screen is open. **Do not call
`mc_observe` after every action**; the answer is usually already in your hand.

### Observation is budgeted, and says so

`mc_observe` never dumps the world. Bulk terrain arrives as a histogram; only
notable blocks — block entities, ores, fluids, machines — get coordinates.
Lists are capped, and the response reports `notable_omitted` / `omitted` /
`_types_omitted` when it trimmed.

**Those fields are load-bearing.** `notable_omitted: 40` means "there is more
there", which is a different world from their absence. Raise `budget` or
narrow `radius` when you see them, rather than assuming you saw everything.

### Modded GUIs: the generic lever

This is the part that makes a heavy pack tractable. Every mod's container
screen is an `AbstractContainerScreen` with indexed slots, so **one pair of
tools drives all of them** — AE2 terminals, Mekanism machines, vanilla chests,
anything — with no mod-specific code:

```
mc_look_at(x, y, z)  →  mc_use()          # open it
mc_inspect_screen()                        # slot indices + contents
mc_click_slot(slot=13, mode="QUICK_MOVE")  # shift-click that slot
mc_close_screen()
```

`mode` is `PICKUP` (plain click), `QUICK_MOVE` (shift), `SWAP`, `CLONE`,
`THROW`, `QUICK_CRAFT`, `PICKUP_ALL`. `button` is 0 left, 1 right.

### When the structured path goes blind

```
mc_screenshot(monitor=0)    # then read the path with a vision tool
```

The tools above cannot see custom rendering — Create contraption overlays, JEI
panes, a mod drawing straight to the screen. When `mc_observe` and
`mc_inspect_screen` come back empty but something is clearly happening, look at
the pixels. **Fallback, not default:** it is slow and burns tokens.

### Safety

- Every movement intent **expires on its own** (capped at 6000 ticks). Nothing
  latches forever, so a dropped connection does not leave the player sprinting.
- `mc_stop()` releases every held input immediately. Reach for it the moment
  something looks wrong.
- The bridge binds `127.0.0.1` and refuses non-loopback callers. It can move
  Ryan's player; do not expose it.
- **Play on a throwaway world unless told otherwise.** The `MC1.1-2012_OG`
  save is not replaceable.

---

## Combining the two

The corpus is the reason the bridge is worth having. When Ryan asks for
something era-specific, the honest loop is:

1. `mc_observe` — what is actually here, what is in inventory
2. Filter `INDEX.json` to the right `pack`, find the mod and topic
3. Read that transcript for the actual procedure
4. Act, and cite the transcript timestamp + URL for what you did

Direwolf20's builds are **demonstration, not specification.** They are his
worlds and his choices. Where a transcript and the live game disagree, the
game wins — mods got patched, and the captions were never authoritative.

---

## Status — read this before promising anything

🔴 **The mod has not been verified running in-game by a human.** It compiles or
it does not; that is a separate question from whether it behaves. Until Ryan
has watched it move a player, treat the bridge as **open, unverified
functionality** — say so plainly rather than reporting it as working.

The corpus is different: those 209 files are on disk and were integrity-checked
(no leftover markup, no duplicate lines, every record carrying a video id).
Use it freely.
