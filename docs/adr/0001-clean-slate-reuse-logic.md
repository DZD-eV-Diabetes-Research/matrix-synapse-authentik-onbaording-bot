# ADR-0001 — Clean slate: reuse logic, not structure

- **Status:** Accepted (2026-06-19)
- **Context:** The legacy bot predates Matrix 2.0 and is a single ~1000-line god-object with a
  sync/async bridge, per-call client churn, committed secrets, and no tests.

## Decision

Rebuild from a deliberately designed architecture. Port valuable **business logic** only —
group→room mapping rules, power-level computation, the custom-room-state persistence idea, and
the pydantic config model. Discard the old plumbing (sync/async bridge, god-object `Bot`,
per-call client creation). Legacy code lives in `legacy/` as a read-only porting reference and
is deleted module-by-module as its logic is ported.

## Consequences

- A short period where `legacy/` and the new `onbot/` coexist; `legacy/` is excluded from
  build, lint, type-check and tests.
- Every ported behaviour is re-validated by tests rather than trusted as-is (the legacy bugs in
  `BATTLE_PLAN.md` §3 must not be carried over).
