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

## Implementation (Phase 5, 2026-06-19)

`lifecycle/accounts.py` ships:

- A **pure state machine** (`decide_account_action`) — the only place the cooldown logic lives,
  exhaustively unit-tested. Stages: detect → `mark` (start clock, no destructive action) →
  `logout` after `deactivate_after_n_sec` (revoke all sessions, G9.2) → `erase` after a further
  `delete_after_n_sec` (deactivate account + optional media, G9.4/G9.5). A user who reappears in
  Authentik before erasure is `reenable`d (G9.6).
- **Two-stage semantics.** Stage one logs the user out rather than hard-deactivating: under the MAS
  topology (ADR-0006) re-login is gated by MAS→Authentik, so a session revoke is the effective
  lock-out *and* stays reversible, preserving the G9.6 window. Erasure is the irreversible stage.
- A **`dry_run` config flag, defaulting to `true`** (`deactivate_disabled_authentik_users_in_matrix.dry_run`).
  While dry-run, only non-destructive bookkeeping timestamps are persisted; every would-be
  destructive action is written to the dedicated `onbot.lifecycle.audit` log channel instead of
  executed. Operators opt in explicitly (G12.2).
- **No database (ADR-0001):** the per-user ledger is one versioned blob in the bot's Matrix account
  data, keyed by MXID — decoupled from the onboarding DM.
- **Scoped orphan detection:** the reconciler only feeds the manager MXIDs that map to a *disabled*
  Authentik user and that already have a Matrix account; the bot user and ignore lists (G12.1) are
  excluded, so the destructive path can never touch unrelated admin/service accounts. (Superseded
  by the review S1 update below: this detection missed deleted users and re-enabled on absence.)

## §7 Q1 resolved empirically (Phase 7b, 2026-06-23)

The live Synapse + MAS + Authentik harness (`tests/integration/`) answered the open question. When an
Authentik user is disabled **upstream**:

1. **Existing Matrix sessions persist.** MAS does **not** propagate the upstream disable — a token
   minted before the disable keeps passing `/whoami` indefinitely. (No automatic session revocation
   or account lock.)
2. **New logins are blocked.** Authentik denies the disabled user at its auth flow
   (`ak-stage-access-denied`), so MAS can never mint a *new* session for them.
3. **The Synapse admin API cannot revoke a MAS session.** Deleting the user's devices *and*
   `POST /_synapse/admin/v1/deactivate` both leave the MAS-issued token valid — Synapse delegates
   token validation to MAS, which still considers the session active. Only **MAS** can revoke it
   (`POST /api/admin/v1/users/{id}/lock` or `/deactivate`; equivalently `mas-cli manage
   kill-sessions`/`lock-user`).

**Conclusion — this module is the enforcement path, not a redundant backstop.** Because existing
sessions survive an upstream disable, onbot is the *only* thing that revokes them, and it must do so
through MAS. The Synapse-admin effectors are insufficient under the MAS topology.

## Implementation update (Phase 7b)

- `clients/mas_admin.py` (`ApiClientMasAdmin`) wraps the MAS admin API (`by-username` lookup,
  `lock`/`unlock`/`deactivate`), authenticated by an OAuth2 `client_credentials` token with the
  `urn:mas:admin` scope (config: `mas_admin`; the bot's client must be in MAS `policy.data.admin_clients`).
- `MasLifecycleEffectors` maps the state machine to MAS: `logout` → **lock** (reversible session
  revocation), `reenable` → **unlock** (G9.6), `erase` → **deactivate** (+ media via Synapse admin).
  `app.py` selects it whenever `mas_admin` is configured, else falls back to
  `AdminApiLifecycleEffectors` (correct only on non-MAS Synapse).
- The lifecycle protocol gained a restorative `reenable` op so a returning user is unlocked.

## Implementation update (review S1, 2026-11-04): fail closed

The 2026-11-04 review found the module failing *open* (LIF-01, SEC-01, MNT-02): every tracked user who
dropped out of the "disabled users" query was re-enabled, including users deleted from Authentik,
users who lost a scope attribute or landed on an ignore list, and every user at once when the
Authentik token stopped seeing users. Under MAS the unlock brought every old session back. The
decision rules are now:

- **Re-enable needs positive evidence.** The pure state machine takes a status per account
  (`active`, `orphaned`, `frozen`, `untracked`) instead of "orphaned or not". Only `active` (the
  bound Authentik user is present, active and in the sync scope) leads to `reenable`, and the unlock
  is only called for a lock onbot itself set (`disabled_user_timestamp`). Dry-run calls no effector
  at all, the unlock included.
- **Known users and deletion.** onbot records `authentik pk -> MXID` (first seen) for every active,
  in-scope user with a Matrix account, in its own account data next to the ledger; ledger entries
  carry the pk too. A recorded user who disappears from Authentik is `orphaned` ("deleted") and goes
  through the same cooldowns. Accounts never mapped are `untracked` and never touched. The binding is
  never rewritten; renames and duplicates are reported (ADR follow-up: D8, review session S9).
- **One scope.** The lifecycle receives every Authentik user plus the scope verdict from the same
  query helper the room sync uses, so an account is only offboarded for a disabled user inside the
  scope, and never while any active Authentik user (in any scope) maps to it.
- **Frozen (D9).** A tracked user who leaves the scope, lands on an ignore list or collides with
  another active user gets no unlock and no further step; it is reported once.
- **Isolation.** Each user is handled on its own with exponential backoff, the ledger is saved after
  every user, an unresolvable MAS user is a failure (LIF-10), and erase records deactivation and
  media deletion separately so a retry never repeats the deactivation.
- **Minimal plausibility guard.** An empty Authentik user list skips the step. The configurable
  guards of D4 (drop thresholds, per-pass limits, confirmation) follow in later review sessions.

## Consequences

- Operators must explicitly opt in to real destructive actions (G12.2); cooldown delays absorb
  accidental upstream disables (G9.3, G12.3).
- Under MAS, configure `mas_admin` or **lifecycle enforcement is a no-op against live sessions**
  (the Synapse-admin path silently fails to revoke). The module only ever gives access back by
  lifting a lock it set itself, for a user positively active and in scope, and never in dry-run.
