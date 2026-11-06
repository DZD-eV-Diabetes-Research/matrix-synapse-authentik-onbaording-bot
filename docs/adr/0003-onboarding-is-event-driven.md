# ADR-0003 — The onboarding bot is event-driven

- **Status:** Accepted (2026-06-19)
- **Context:** Welcoming users is inherently reactive — it should happen the moment a user
  appears, not on the next reconcile tick.

## Decision

The onboarding/welcome flow reacts to Matrix events via `/sync` (Simplified Sliding Sync,
MSC4186): a user appearing/joining triggers onboarding. It is triggered by the reconciler's
"user provisioned/new" signal and/or membership events.

## Consequences

- Instant onboarding with no tick latency; the natural Matrix programming model.
- Requires a Matrix CS client with a sliding-sync stream (Phase 4) and a clean boundary with
  the reconciler via the internal signal bus (`events.py`, see ADR-0004).
- The welcome flow must be idempotent (G4.3), enforced via onbot room-state so a message is
  sent once per user and never re-sent. Precisely (review finding ONB-09): a message is recorded
  right after its send, and the send carries a transaction id derived from the room and the
  message, so a send repeated before the record landed is deduplicated by Synapse for about a day.
  Outside that window it is at least once, not exactly once.

## Amendment (2026-11, review findings SEC-06, ONB-13, RUN-08, RUN-01)

- **Only mapped users are welcomed.** The reconciler hands over, once per pass, the set of accounts
  it mapped (`Signal.users_mapped`). A join seen on the sync stream only *accelerates* the welcome of
  a user in that set; anybody else who joins a room the bot is in is ignored. Before the first pass
  nobody is welcomed, which also makes the replay of every member on a fresh sync connection free.
- **Welcomes run beside the pass.** Signal handlers only enqueue; a few workers deliver the queue
  (one user at a time per worker, never one user twice at once). A slow or failing welcome no longer
  stalls convergence. `reconcile-once` drains the queue after its pass.
- **`m.direct` has one writer at a time.** Concurrent welcomes of different users serialise the
  read-modify-write of the bot's `m.direct` behind one lock.
