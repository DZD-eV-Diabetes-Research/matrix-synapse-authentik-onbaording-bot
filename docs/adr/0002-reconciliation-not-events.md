# ADR-0002 — Reconciliation (level-triggered), not events, for Authentik→Matrix

- **Status:** Accepted (2026-06-19)
- **Context:** Keeping Matrix state in sync with Authentik is a convergence problem, not a
  stream-processing one.

## Decision

Model the Authentik→Matrix sync as a Kubernetes-style controller: compute **desired** state
(from Authentik) and **actual** state (Matrix/Synapse), then apply the diff. The reconcile is
**idempotent and convergent**. Run it on a schedule **and** on demand; any event is only a
trigger that runs the same reconcile sooner. This replaces the legacy `while True: sleep` loop.

## Rationale (why not events here)

- Authentik provides no reliable, ordered, complete event stream.
- The bot must converge on restart and heal out-of-band drift.
- Destructive actions must derive from **current** state, never a possibly-stale event.

## Consequences

- One code path serves both scheduled and on-demand runs (`reconcile-once` CLI mode).
- Correctness hinges on the desired/actual diff being complete — pagination across all clients
  is mandatory (a legacy gap, see `BATTLE_PLAN.md` §3).
