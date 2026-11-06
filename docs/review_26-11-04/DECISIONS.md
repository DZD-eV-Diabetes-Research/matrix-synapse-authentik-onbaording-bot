# Decisions taken during the review

Maintainer decisions that shape the follow-up sessions. Date: 2026-11-04.

## D1 Leaked tokens in git history (SUP-01)

Both leaked `syt_` tokens (`@dzd-bot`, 2023, and a personal operator token, 2024) are known to be
invalid already, per the maintainer. No revocation step is planned. The BATTLE_PLAN Phase 1 rotation
item can be closed with that note.

## D2 History scrub (SUP-01)

The history will be scrubbed anyway, as its own session: `git filter-repo`, force-push of `main` and
all tags, re-created tags, GitHub Support request to purge cached commits. See the scrub plan in
`02-supply-chain.md`.

## D3 Room identity (REC-04, SEC-02, REC-13)

Rooms are tied to Authentik groups by a persisted mapping `group_pk -> room_id` (for group rooms,
lobbies, the space and the control room), not by canonical alias. The alias becomes presentation
only. Existing deployments need a one-time migration that adopts current rooms by alias plus the
onbot state marker, and then never looks them up by alias again.

## D4 Guard for destructive actions (REC-01, REC-02, LIF-08, MNT-01)

Three layers:

1. **Input plausibility.** An empty user or group list from Authentik aborts the pass. So does a drop
   of more than a configurable percentage against the last known good counts (persisted).
2. **Per-pass threshold for reversible destructive actions** (kick, MAS lock, room teardown). When a
   pass would exceed it, those actions are skipped, logged loudly and reported in the admin room.
3. **Confirmation for irreversible actions** (deactivate, erase) above a small number per pass: they
   wait for `!confirm` in the admin control room.

## D5 Legacy cutover (ONB-05)

The production cutover from the legacy bot has already happened. No adoption code. Only a short docs
note for other operators that a cutover creates fresh notice boards and re-sends the welcome.

## D6 `chatroom_params` (SEC-05)

Stays as it is: on by default, unvalidated. Whoever may edit group attributes in Authentik is trusted.
Only sharpen the docs (name the concrete consequences: `additional_creators`, public rooms,
`power_level_content_override`). One small exception that is a robustness fix, not a policy change:
a room the bot cannot fully manage (for example an extra creator it cannot kick) must not abort the
whole pass (covered by the per-room error isolation, REC-03).

## D7 Erase semantics (LIF-11)

New option `gdpr_erase_on_delete`, default `false`, applied identically by both effectors: MAS gets
`{"skip_erase": not gdpr_erase_on_delete}`, the Synapse admin path gets `erase=gdpr_erase_on_delete`.
Media deletion stays a separate switch.

## D8 Identity across renames (SEC-07, MNT-13)

Renames in Authentik (marriage, name change) must be supported: the person keeps their existing
Matrix account, because MAS fixes the localpart at first login. Consequence: identity is anchored on
the immutable Authentik user (`pk`/`uid`), not on the mutable username attribute.

Proposed design, to confirm at the start of that session:

- With `mas_admin` configured, resolve each Authentik user to the real MXID through MAS (upstream
  OAuth link by subject, then the MAS user's localpart). This is authoritative.
- Persist the binding `authentik pk -> MXID` (first seen) and keep using it when the attribute value
  changes. Without MAS this binding is the only source after the first sight.
- Report binding changes and duplicate MXIDs in the admin room; duplicates fail closed (the affected
  users are skipped, never merged).
- One function computes the MXID (today it is written four times, MNT-13).

## D9 Users who leave the sync scope during offboarding (S1)

A user who still exists in Authentik but is no longer in the sync scope while onbot has them locked or
in the offboarding pipeline is frozen: no unlock, no further destructive step. The bot reports it in
the admin room and an admin decides manually. If the user comes back into scope as active, the normal
re-enable applies.

## D10 Withdraw power levels (S2, REC-08, G8.4)

When a user loses the group membership that granted an elevated power level, the bot withdraws it.
It withdraws only entries it set itself (tracked by the bot); levels set by humans stay untouched.

## D11 Avatar fetching (S13, SEC-03)

Private, loopback and link-local destinations are blocked by default; a config allowlist can release
specific internal hosts. Plus a size cap, a total timeout, and redirects re-checked against the same
rules.

## D12 CI dependency updates and formatting (S15)

Dependabot for GitHub Actions and the Docker base image. CI only checks `ruff format` and fails; it
no longer commits formatting fixes to `main`.

Python dependencies (2026-11-06): no extra monitoring. Dependabot cannot update `pdm.lock`, and the
maintainer accepts that: the bot has no inbound port, talks only to trusted upstreams and has six
hash-pinned runtime packages. Packages are updated by hand with `pdm update`; `docs/development.md`
says so, so the gap is deliberate. No pip-audit job, no Renovate.

## D13 Guard confirmation (D4 layers 1 to 3, decided 2026-11-06)

A tripped guard never just blocks. It pauses only the destructive part of the pass (onboarding,
invites and room upkeep go on), records the would-be actions as a pending batch with its own stable
id and posts a summary to the admin room (what dropped, from how many to how many, which users or
rooms would be affected). An allowlisted admin releases exactly that batch with `!confirm <id>`;
`!pending` lists all open batches. There is no `!confirm` without an id, so parallel incidents never
release each other. A batch expires after 72 hours and is proposed again on a later pass if it still
applies. After a confirmed drop the new counts become the last known good.

- Ids are 6 characters from an alphabet without look-alikes (no `0`/`O`, `1`/`l`/`I`).
- Confirmation re-checks against the current state: only actions that still apply are executed (a
  user who is active again is skipped). If the set of actions grew, the confirmation does not cover
  the new part; a new batch is proposed. A confirmation never covers more than the admin saw.
- `!reject <id>` closes a batch explicitly: no execution, no reminders, not proposed again for the
  same incident (for example a broken token that has since been replaced).
- A rejected batch ends when the condition that created it is gone (layer 1: the metric is no longer
  tripped; layer 2: the pass no longer exceeds the limit with those actions), at the latest at its
  expiry. Its remaining actions are then judged fresh, so a single genuinely departed user is
  offboarded normally. `!pending` lists rejected batches too (added 2026-11-06 after the wave 2
  review, which found a rejected batch could hold a valid lock forever).
- An unreadable guard state fails closed: destructive actions are held and the problem is reported;
  it never starts over empty (wave 2 review).

Defaults (configurable):

- Layer 1: a drop of more than 20 % in users in scope or in mapped groups against the last known good
  counts needs confirmation. The baseline moves with every normal pass, so gradual removals never
  trip it. An empty list always needs confirmation.
- Layer 2: more than 10 reversible destructive actions (kick, MAS lock, room teardown, withdrawal of
  bot-granted power levels) in one pass need confirmation.
- Layer 3: 3 or more irreversible actions (deactivate, erase) in one pass need confirmation; 1 or 2
  run as today.

Placement: S3 builds the batch store, `!confirm`, `!pending` and the expiry, and uses them for layers
1 and 2. S5 only adds layer 3 on top.

## D14 Known user disabled and out of scope at the same time (wave 1 review, decided 2026-11-06)

A user with a `KnownUsers` binding (positively mapped by onbot earlier) who is disabled in Authentik is
offboarded normally even when they left the sync scope in the same interval (other path, removed from
the synced group, opt-in attribute removed). This restores the pre-S1 behaviour for the common
"disable, archive and strip groups" flow. D9 still applies to users who leave the scope while active
and already locked or in the pipeline, and accounts onbot never mapped stay untouched (REC-14).

## D15 Confirmations of merged wave 1 behaviour (2026-11-06)

- S13: config combinations that do nothing (`delete_disabled_rooms` without disabling rooms, onboarding
  rooms in a space without a configured space) stay hard startup errors (exit 2).
- S15: releases only from tags on `main`, gated on the full CI including the integration suite.
- S15: the image moves from Debian bookworm to trixie (small separate change with an integration run).
- S10: the fixed liveness thresholds stay fixed. The deployment does not restart unhealthy containers
  (plain docker compose, status only), so a long Authentik outage causes no restart loop.

## D16 Manually closed lobby (S7, REC-10, decided 2026-11-06)

A lobby that a human closed (for example join rule changed to invite) stays closed. The bot detects
that the change was not its own, logs it at WARNING and reports it once in the admin room, and stops
reverting it. An allowlisted admin can withdraw the manual close with a control room command (for
example `!lobby reopen <group or room>`); afterwards the bot manages the lobby from the config again.

## D17 CI duration (decided 2026-11-06)

The repository is public, so GitHub-hosted runner minutes are free; the concern is waiting time. The
integration suite stays a required gate, but:

- It runs only when code, tests, dependencies, the Dockerfile or the workflows change (path filter);
  docs-only commits and PRs run lint, unit tests and the secret scan only.
- A release reuses a green CI run of the tagged SHA when one exists and runs the CI only otherwise.
- Speeding up the suite itself (cached stack start, parallel tests) is part of S14.

## D18 HTTP layer (S11, decided 2026-11-06)

- `synapse_server.oauth2` is removed: MAS 0.14 does not grant the Matrix API scopes to
  `client_credentials`, so the option could never work. A config that still sets it fails at startup
  with exit 2 and a specific message pointing to `bot_access_token` (compatibility token).
- A paginated listing that stays inconsistent after one re-read fails the whole pass (fail closed);
  the next tick retries.

## D19 Wave 2 decisions (S3, S4, decided 2026-11-06)

- **Test stack versions:** the deployment (`infra-node-conf-com`, not productive yet) runs MAS and
  Synapse as `:latest`, so the integration stack pins the current releases of both by digest instead
  of MAS 0.14.1. Code still reads the MAS capabilities at runtime (S4) and degrades as below.
- **Erase on a MAS without `skip_erase`** while `gdpr_erase_on_delete` is false: refused and logged as
  FAILED with backoff (fail closed, never erase more than configured).
- **LIF-09:** when the homeserver delegates logins to MAS but `mas_admin` is missing, offboarding is
  refused (no lock, no erase through the Synapse fallback), reported in the admin room and log.
  Onboarding and rooms run normally.
- **Ledger retention:** lock records of deleted users with erase disabled are dropped after 90 days,
  a constant (the MAS lock stays).
- **Admin room on by default:** `admin_room.enabled` defaults to true so `!confirm` is available. An
  enabled admin room needs at least one admin (`admin_user_ids` or
  `authentik_group_pks_granting_bot_admin`), otherwise startup fails with a clear config error;
  deployments without an admin room disable it explicitly and steer the guard through its limits.
- **S5:** deleting a room (`delete_disabled_rooms`) counts as irreversible (layer 3, confirmation from
  3 per pass); closing or detaching a room stays layer 2.
