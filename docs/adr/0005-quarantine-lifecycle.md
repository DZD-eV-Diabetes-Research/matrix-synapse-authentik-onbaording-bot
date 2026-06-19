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
  excluded, so the destructive path can never touch unrelated admin/service accounts.

## Consequences

- Operators must explicitly opt in to real destructive actions (G12.2); cooldown delays absorb
  accidental upstream disables (G9.3, G12.3).
- **Open question (BATTLE_PLAN §7 Q1) — to be answered empirically (decided 2026-06-19):** whether
  MAS itself revokes sessions / locks the account when Authentik disables a user upstream will be
  settled by a dedicated experiment in the Phase 7 integration harness (real Synapse+MAS+Authentik),
  not by guesswork; this module's exact responsibility (redundant backstop vs. enforcement path) is
  finalized from the observed facts. Either way the design is safe — the module never *grants*
  access, only removes it, behind dry-run.
