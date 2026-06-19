# ADR-0005 — Quarantine the destructive lifecycle

- **Status:** Accepted (2026-06-19)
- **Context:** Account deactivate/delete (with cooldowns) is the scariest code in the repo: it
  is irreversible and acts on real users.

## Decision

Isolate the lifecycle domain in its own module (`lifecycle/`) with:

- **dry-run by default** and an **audit log by default**;
- a separate credentials boundary;
- the highest test rigor of any module;
- invocation only by the reconciler's desired-vs-actual result, never ad hoc.

## Consequences

- Operators must explicitly opt in to real destructive actions (G12.2); cooldown delays absorb
  accidental upstream disables (G9.3, G12.3).
- The exact responsibility split with MAS is an open question (whether MAS already revokes
  sessions/locks accounts when Authentik disables a user) — verified in Phase 5. The module is
  the enforcement backstop either way.
