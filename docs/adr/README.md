# Architecture Decision Records

Lightweight ADRs capturing the agreed design principles for the Onbot revival. They are the
authoritative, promoted form of `BATTLE_PLAN.md` §1. New significant decisions get a new
numbered file; superseded ones are marked, not deleted.

| ADR | Title | Status |
|-----|-------|--------|
| [0001](0001-clean-slate-reuse-logic.md) | Clean slate — reuse logic, not structure | Accepted |
| [0002](0002-reconciliation-not-events.md) | Reconciliation (level-triggered), not events, for Authentik→Matrix | Accepted |
| [0003](0003-onboarding-is-event-driven.md) | The onboarding bot is event-driven | Accepted |
| [0004](0004-modular-monolith.md) | Separate concerns as a modular monolith | Accepted |
| [0005](0005-quarantine-lifecycle.md) | Quarantine the destructive lifecycle | Accepted |
| [0006](0006-auth-topology-mas-authentik.md) | Auth topology — Authentik as upstream IdP to MAS | Accepted |
| [0007](0007-async-one-http-base-client.md) | Async everything, one HTTP base client | Accepted |
| [0008](0008-matrix-client-library.md) | Matrix client library — drive the CS API over httpx | Accepted |
| [0009](0009-e2ee-stance.md) | E2EE stance — bot operates outside encrypted rooms | Accepted |
