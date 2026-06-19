# ADR-0007 — Async everything, one HTTP base client

- **Status:** Accepted (2026-06-19)
- **Context:** The legacy bot mixed sync and async via a `synchronize_async_helper` bridge and
  created clients per call, with no shared retry/pagination/error handling.

## Decision

Go fully `async` and drop the sync/async bridge. Provide **one** pooled `httpx.AsyncClient`
base client (`clients/base.py`) handling auth injection, retries (tenacity), pagination, and
typed errors. The Authentik, Synapse-Admin, and Matrix-CS clients are built on this base.

## Consequences

- Consistent retry/pagination/error semantics across all integrations; pagination is handled
  once, fixing the legacy silent-truncation bug (`BATTLE_PLAN.md` §3).
- Connection pooling instead of per-call client churn.
- The Matrix client **library** decision (keep `matrix-nio`, drive the CS API via this base
  client, or use `mautrix-python`) is deferred to a Phase 6 ADR; it must fit this async base and
  MAS/OAuth + sliding-sync needs. No Matrix library is pinned in `pyproject.toml` yet.
