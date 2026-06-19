# ADR-0008 — Matrix client library: drive the CS API over our httpx base client

- **Status:** Accepted (2026-06-19)
- **Context:** ADR-0007 deferred the Matrix client-**library** choice to Phase 6: keep
  `matrix-nio`, drive the CS API directly over the shared async httpx base client, or adopt
  `mautrix-python`. The decision must fit the MAS topology (ADR-0006): MAS-issued tokens /
  OAuth2, Simplified Sliding Sync (MSC4186), authenticated media (MSC3916), and a bot that is
  **stateless on disk** and sends only plaintext while managing room state.

## Decision

**Drive the Matrix Client-Server API directly over `clients/base.py` (httpx).** Do not adopt
`matrix-nio` or `mautrix-python`. The CS-API surface the bot needs is small and already
implemented in `clients/matrix.py` (room/space create, membership, room state, power levels,
messaging, account data, media, sliding sync).

## Rationale

- **MAS / OAuth2 fit.** Our `auth/token_provider.py` injects a per-request bearer token (static
  compat token *or* OAuth2 client-credentials with refresh) straight into the base client.
  `matrix-nio` assumes it owns auth/login and has no first-class MAS/OAuth2 client-credentials
  path; bending it around an externally-minted, rotating token is friction with no payoff.
- **Sliding sync.** Simplified Sliding Sync (MSC4186) is unstable and evolving. We already
  normalise it to `SyncResult` behind one method and gate it on version negotiation (ADR-pending
  versions module). Libraries lag the unstable endpoint; owning the thin wire mapping is simpler.
- **No e2ee need (see ADR-0009).** The main thing a heavier library buys us — Olm/Megolm crypto —
  is explicitly out of scope. Pulling in `matrix-nio[e2e]`/libolm or the rust-sdk would add a
  deprecated or heavy native dependency we do not use.
- **Async + maintenance.** Everything is already `async` on one pooled client with shared
  retries/pagination/typed errors (ADR-0007). A library would duplicate or fight that.

## Consequences

- We own the (small) CS-API mapping and must track spec drift — mitigated by centralised version
  negotiation (`clients/versions.py`) and contract tests (`respx`).
- No Matrix library is pinned in `pyproject.toml`; runtime deps stay minimal (httpx + tenacity +
  pydantic).
- **Revisit triggers:** if the bot must operate *inside* end-to-end-encrypted rooms (ADR-0009
  reversal), or the CS-API surface grows substantially, re-evaluate adopting
  `matrix-rust-sdk`/`mautrix` for crypto + higher-level state handling.
