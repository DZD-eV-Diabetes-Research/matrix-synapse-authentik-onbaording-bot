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
  sent once per user and never re-sent.
