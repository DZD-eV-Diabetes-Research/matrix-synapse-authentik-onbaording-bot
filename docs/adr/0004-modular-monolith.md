# ADR-0004 — Separate concerns as a modular monolith

- **Status:** Accepted (2026-06-19)
- **Context:** Three bounded domains — **reconciler**, **onboarding**, **lifecycle** — with
  different triggers and risk profiles, but operating on the same systems at modest scale.

## Decision

Keep all three domains in one repo and one process, sharing the async API clients (`clients/`)
and auth (`auth/`) behind clean internal boundaries. Coupling is explicit: the reconciler emits
a "new user" / "drift detected" signal on an internal bus (`events.py`); onboarding consumes it.
Each domain is designed so it could later be split into its own process.

## Rationale

Microservices here would pay the distributed-systems tax (deployment, observability, partial
failure) with no payoff at this scale. A modular monolith keeps deployment trivial while
preserving separability.

## Consequences

- Boundaries must be enforced by discipline (and structure), since nothing physically prevents
  cross-domain imports. The signal bus is the sanctioned coupling point.
