# ADR-0003 — Seat identity is derived from the conversation, and the NAME is not reliable

- **Dates:** rejected design 2026-08-21 · roster measured 2026-08-19 · pid fix 2026-08-20 · retraction 2026-08-23
- **Code:** `src/litetui/harness.py` — `agent_id_for_convo()`, `register()`, `Seat.rebind()`, `heartbeat()`
- **Status:** accepted (supersedes the per-process design)

## Context

### The design that was rejected

The seat's agent id was minted **per PROCESS** and persisted nowhere. That was tried and
measured: **one conversation minted three ids in an evening**, two of them heartbeating at
nothing, and a **dispatched task landed in a dead mailbox while `send` exited 0**.

Ryan rejected that design on **2026-08-21**.

### What ships instead

`agent_id_for_convo()` **derives** the id from the conversation — `uuid5` over
`"litetui:seat:" + convo_id` (commit `0852dab`). That function's docstring holds the full
reasoning; it is deliberately not duplicated, because a second copy is a second thing that can
drift.

The rejected design's one correct observation survives the change: **identity does shift within
a process** on `/new` and `/resume`, and two windows on the same conversation now share one id
and so poll one mailbox. That is a *consequence* of the ruling, not an argument against it —
silent misdelivery was the worse cost. The shift is handled: `Seat.rebind()` retires the old row
and re-registers under the new id in one transition.

### The NAME is a separate problem, and it is not solved

Without `--takeover` the name cannot carry across a restart: the previous process still holds
`"LiteTUI"` in the registry, so the name is refused and a random one is generated instead.

Measured on the live roster **2026-08-19 — SIX rows for one seat**: `LiteTUI`, `BlackGrid`,
`HazeCrypt`, `PrimeWard`, `HotPack`, `CyanWedge`. Anyone who wrote down a name held a stale
pointer one restart later.

`--takeover` is *documented* to evict only a ghost and to refuse a genuinely live holder.
**That guard does not protect this seat**, and it was measured rather than assumed: two live
probes, and the second took the name from the first.

`_agent_record_live` reads `presence.session_pid` and treats a falsy one as NOT live.
**✅ FIXED 2026-08-20:** the CLI grew an opt-in `--session-pid`, `_presence_argv()` passes ours,
and a restored seat was measured reading `[active]` rather than `[ghost]`. (The comment this ADR
replaces previously said the field was unreachable from `liteharness.cli register` and that the
seat "always reads as a ghost" — both were true when written and are not now.)

Two windows at once still trade the NAME. Mail is addressed by `agent_id`, so nothing is
misdelivered.

**A live pid is not enough on its own.** `last_seen` is written once at registration, so a seat
with a correct live pid still decays to `[ghost]` — measured at 10 minutes. `heartbeat()` is
what keeps it on the roster.

## Decision

- The agent id is **derived from the conversation**, never minted per process.
- The **name** is best-effort. Address mail by `agent_id`; never treat a seat name as a stable
  handle.
- Keep `heartbeat()` running: presence needs it independently of a correct pid.

## Consequences

- Two windows on one conversation share an id and a mailbox. Accepted deliberately, because the
  alternative was silent misdelivery.
- A written-down seat name goes stale across a restart. Anything that needs to reach this seat
  must resolve the id, not the name.
- **The rejected design regenerates.** A reader who finds no trace of it re-derives it from
  first principles and arrives back at per-process ids, which is why the code keeps a short
  retraction at the call site pointing here rather than saying nothing.

## The reason the original comment insisted on staying in place

Its own argument, preserved because it generalises beyond this decision:

> **In this repo A COMMENT IS A HYPOTHESIS.** Three prose-contradicts-code defects were found
> here in one day — this one, `_sync_seat_identity`'s "the next heartbeat re-registers", and
> `mcp_client`'s "every wait is bounded". **Follow the control flow, not the prose.**
