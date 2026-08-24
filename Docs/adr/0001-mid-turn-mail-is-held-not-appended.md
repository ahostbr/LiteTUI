# ADR-0001 — Mid-turn mail is HELD and flushed as a real turn, never appended into context

- **Date:** 2026-08-21 (measured)
- **Code:** `src/litetui/app.py` — the inbox monitor's mid-turn delivery path
- **Status:** accepted

## Context

When mail arrives from another agent while a turn is already running, there are two ways to get
it in front of the model: **append** it into the running context, or **hold** it and deliver it
as a genuine user turn once the turn ends.

Appending was tried, and it fails in a way that looks like nothing at all.

An appended mid-turn message lands **between an assistant message and its tool results**, where
nothing announces it. Measured 2026-08-21: the text sat in context for **four turns** while the
model's own inbox tool reported `(no new messages)` — truthfully, because this monitor had
already claimed the mail. So the model **trusted the tool over its own context** and never
acted on the message.

**Inert injection is not delivery.** Text that is present but unannounced, contradicted by a
tool the model trusts more, is indistinguishable to the model from text that was never sent.

## Decision

Mid-turn mail is **HELD**, and flushed as a **real user turn** the model cannot miss.

Inbox mail always **QUEUES**. Another agent's mail must never cancel work in flight.

## Consequences

- Delivery is later than an append would be, by at most the remainder of the current turn.
  That latency is the price of the message actually being read.
- The monitor claiming mail is what makes the inbox tool answer `(no new messages)`. That
  answer is correct; it is the *appending* that made it misleading. Holding removes the
  contradiction rather than papering over it.
- Because mail queues rather than interrupts, a sender cannot use mail to stop a running turn.
  Interruption is a separate, deliberate action — see the mid-turn input mapping in
  `src/litetui/settings.py`.
