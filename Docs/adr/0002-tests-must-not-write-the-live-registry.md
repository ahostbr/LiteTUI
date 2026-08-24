# ADR-0002 — A test run must not write the live harness registry

- **Date:** 2026-08-20 (measured)
- **Code:** `src/litetui/harness.py` — the `LITETUI_NO_REGISTER` constant and `register()`
- **Status:** accepted

## Context

Constructing `LiteTUI` registers the seat with the harness, and registration passes
`--takeover`. `--takeover` is *documented* to refuse a live holder. **It measurably does not**
(see `register()` and ADR-0003).

So the suite had a side effect nobody asked for: **every `python tests/run_all.py` took the
name "LiteTUI" from Ryan's running instance** and moved its registry row to
`~/.liteharness/.ghost_evicted_*`.

Measured 2026-08-20: the live app was **pid 474900** and its record was in the graveyard, while
the roster's only `LiteTUI` row named a **dead test process**.

This is the same family as the `lms load` on connect and the `.convos` pollution: **the app's
own startup path reaching live shared state from inside a test.**

## Decision

`LITETUI_NO_REGISTER` — set to any non-empty value — makes registration a no-op, and the test
environment sets it.

**The registry is live shared state and is not this repo's to write during a test run.**

## Consequences

- Tests exercise construction without touching the fleet. A seat that must test registration
  itself has to do so explicitly, against a fixture, rather than getting it as a side effect of
  building the app.
- The failure this prevents is silent from inside the suite: the tests all pass while the
  developer's running app quietly leaves the roster. Nothing in a green run would have shown it.
- This is a guard around a defect that lives elsewhere. If `--takeover` is ever fixed to
  genuinely refuse a live holder, this remains correct — a test still should not write the
  registry — but the blast radius of forgetting it would shrink.
