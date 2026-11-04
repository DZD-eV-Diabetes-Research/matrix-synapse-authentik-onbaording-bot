# 04 Lifecycle (offboarding, MAS and Synapse admin effectors, ledger)

Summary: the destructive direction is well guarded (orphans must be positively *disabled* in Authentik,
dry-run defaults on, two cooldown stages, bot and ignore lists excluded), so an empty or broken Authentik
answer cannot mass-erase anyone. The weak side is the *security* direction: the bot fails open. Any reason
a locked user drops out of the disabled-user query (Authentik deletion, a stripped scope attribute, an
ignore-list edit, an empty API answer) triggers a MAS unlock, and per the MAS spec an unlock brings every
pre-existing session back. Deleted Authentik users are never offboarded at all, one failing user stalls
the whole lifecycle, and several silent no-op paths are written to the audit log as successes.

Experiments: `/home/tim/.cache/claude-tmp/claude-1000/-home-tim-Repos-github-com-DZD-eV-Diabetes-Research-matrix-synapse-authentik-onbaording-bot/693247bf-1211-43a8-98ff-5b10b099a453/scratchpad/lif/test_lif_experiments.py`
(8 tests, all pass against commit `2c2e3a1`; run with
`PYTHONPATH=. uv run pytest -p no:cacheprovider --rootdir=. -c pyproject.toml <path>` from the repo root).
MAS semantics were checked against the official admin API spec
(`https://element-hq.github.io/matrix-authentication-service/api/spec.json`), Synapse semantics against
the Synapse user admin API docs.

| ID | Severity | Title |
|---|---|---|
| LIF-01 | critical | Fail-open re-enable: any drop out of the orphan set unlocks the MAS user and revives old sessions |
| LIF-02 | high | Users deleted in Authentik are never offboarded |
| LIF-03 | high | One permanently failing user stalls offboarding for everyone; all-or-nothing ledger save repeats irreversible erases |
| LIF-04 | medium | Offboarding only runs if every earlier reconcile step succeeded |
| LIF-05 | medium | Scope asymmetry between "active" and "disabled" queries: wrong-target lock and silent escapes |
| LIF-06 | medium | Dry-run is not side-effect free: it performs live MAS unlocks, including locks onbot never set |
| LIF-07 | medium | MAS lock does not end sessions; re-enable restores every old session (stolen device scenario), docs say "revoked" |
| LIF-08 | medium | No blast-radius guard for mass disables or for flipping `dry_run` off |
| LIF-09 | medium | Silent Synapse-admin fallback under MAS; audit log claims sessions were revoked |
| LIF-10 | medium | MAS lookup 404 is a silent no-op that advances the state machine and is audited as success |
| LIF-11 | medium | Erase semantics differ per effector: MAS always GDPR-erases, Synapse path ties erase to the media flag |
| LIF-12 | medium | Media deletion stops after 100 files and is never retried |
| LIF-13 | medium | Possible lock/unlock oscillation if a MAS lock hides the user from the Synapse user list |
| LIF-14 | low | Ledger robustness: corrupt blob stalls lifecycle forever, lost ledger forgets locks, unbounded growth |
| LIF-15 | low | Docs drift: cooldown base, "deactivate" naming, ADR and module docstrings out of date |
| LIF-16 | low | Test gaps: no unit tests for `MasLifecycleEffectors`, partial failures, or re-enable under MAS |

## Critical

### LIF-01 Fail-open re-enable: any drop out of the orphan set unlocks the MAS user and revives old sessions

- **Severity:** critical
- **Category:** security
- **Location:** onbot/lifecycle/accounts.py:77-79, onbot/lifecycle/accounts.py:245-249, onbot/lifecycle/accounts.py:266-272, onbot/reconciler/engine.py:640-663
- **Status:** confirmed (experiments E3 and E5; MAS spec for lock semantics)
- **Effort:** M

`reconcile_accounts` decides for `orphaned_mxids | ledger.entries`, and `decide_account_action` returns
`reenable` for every ledger entry that is not in the orphan set this pass. The orphan set is not "users
known to be disabled", it is "users that the disabled-user query returned, that map cleanly, are not on an
ignore list, and still appear in the Synapse user list". So a ledger entry is re-enabled (MAS `unlock`,
entry cleared, audit line "re-enabled ... Authentik account active again") whenever any of these happens
while the user is still disabled or even gone:

1. The Authentik user is **deleted** (standard practice: disable on the last day, delete after N days).
2. The user loses an attribute in `sync_only_users_with_authentik_attributes` (offboarding checklists
   commonly remove the chat opt-in attribute). The disabled query applies that filter server-side
   (engine.py:640-643). Reproduced end to end through `ReconcilerEngine` in E5.
3. Someone adds the user to `authentik_user_ignore_list` / `matrix_user_ignore_list` (meant as "never
   touch", acts as "unlock").
4. `authentik_username_mapping_attribute` changes, or the user's mapping attribute is removed (the
   `KeyError` is swallowed silently at engine.py:656-657).
5. The Authentik token loses view permission on users: Authentik filters list endpoints by object
   permissions and answers `200` with an empty list, so every tracked user is unlocked in one pass.
6. The Matrix account disappears from `GET /_synapse/admin/v2/users` (deactivated, or locked, see LIF-13).

Per the MAS admin API spec, `POST /users/{id}/lock` "DOES NOT invalidate any existing session, meaning that
all their existing sessions will work again as soon as they get unlocked". So in scenario 1 a former
employee who was correctly locked on day 1 gets every Element session on every device back on the day HR
deletes the Authentik account, and the ledger forgets that onbot ever touched them. E3 reproduces the
lock, then the unlock after `reconcile_accounts(set())`.

Fix sketch: re-enable only on positive evidence. Store the Authentik `pk` in the ledger entry, and before
unlocking fetch that user (`GET core/users/{pk}/`) and require `is_active is True` (404 means deleted, so
keep the entry and treat it as orphaned, see LIF-02). Never unlock an entry that only drops out because of
ignore lists, mapping errors or a missing Matrix account; log and keep it instead. Only call `unlock` when
onbot itself set the lock (see LIF-06).

## High

### LIF-02 Users deleted in Authentik are never offboarded

- **Severity:** high
- **Category:** security
- **Location:** onbot/reconciler/engine.py:629-663, onbot/config.py:416, docs/features.md:411
- **Status:** confirmed (code reading; the only source of orphans is `list_users(filter_is_active=False)`)
- **Effort:** M

Config (`enabled`: "Lock out Matrix accounts whose Authentik account was disabled or deleted") and
features.md:411 promise that deleted users are offboarded. The code only ever asks Authentik for
*disabled* users. A user deleted directly (without a disable phase, or disabled and deleted within one
tick) is in neither the active nor the disabled list, so nothing happens. ADR-0005 (Q1 result) established
that MAS does not propagate upstream state and that existing sessions persist indefinitely, so a deleted
user keeps chat access for as long as their refresh token lives. Combined with LIF-01, a deleted user who
had already been locked is actively unlocked.

Fix sketch: detecting deletion needs a positive list of Authentik-backed Matrix accounts. Under MAS this
exists: the MAS admin API lists upstream OAuth links (`/api/admin/v1/upstream-oauth-links`, filter by the
Authentik provider) with the upstream `subject`. A Matrix user linked to the Authentik provider whose
subject (or stored pk) no longer exists in Authentik is "deleted" and enters the same cooldown. Without
MAS, record the pk when first seeing an active mapped user. Until then, fix the docs to say "disabled".

### LIF-03 One permanently failing user stalls offboarding for everyone; all-or-nothing ledger save repeats irreversible erases

- **Severity:** high
- **Category:** robustness
- **Location:** onbot/lifecycle/accounts.py:236-262 (save only at 260-261), onbot/lifecycle/accounts.py:175-180
- **Status:** confirmed (experiments E1 and E2); the media/re-enable variant is plausible (Synapse docs)
- **Effort:** S

The manager iterates users in sorted order, calls effectors inline and saves the ledger once at the end.
Any exception aborts the loop and discards every ledger change of the pass.

- E1: `@aaa` is due for logout and MAS answers 500 for that user every time (or `deactivate` keeps
  failing). Over 30 simulated daily passes, `@zzz`, disabled on day 0, is never even *marked*, because
  marks of later users are recomputed each pass and never saved (`store.saves == 0`). One bad account
  disables offboarding for the whole organisation, visible only as "reconcile pass failed" in the log.
- E2: `@a0` is erased (MAS deactivate, irreversible), then `@aaa` fails. The pop of `@a0` is not saved, so
  every later pass erases `@a0` again (3 erases in 3 passes). Same on SIGTERM or a crash between the
  effector call and the save.
- Partial erase: MAS `deactivate` succeeds, then `delete_user_media` fails. MAS asks Synapse to deactivate
  the user, Synapse excludes deactivated users from `v2/users` by default, so on the next pass the user is
  no longer in `matrix_mxids`, falls out of the orphan set and hits LIF-01: `unlock` is called, the entry is
  cleared with the audit line "re-enabled", and the media deletion (possibly a GDPR request) is dropped.

A lifecycle failure also prevents `last_reconcile_at` and `Signal.reconcile_completed` (engine.py:150-154),
so `!status` and admin-room re-invites go stale too.

Fix sketch: wrap each user's `_apply` in `try/except`, log with the MXID, count consecutive failures in
the entry, and continue. Persist after every destructive step (save before the effector call with a
"pending" marker, or immediately after it). Make erase two-phase in the ledger (`erased_ts`, `media_done`)
so a retry resumes instead of re-deactivating or re-enabling.

## Medium

### LIF-04 Offboarding only runs if every earlier reconcile step succeeded

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/reconciler/engine.py:133-154, onbot/reconciler/engine.py:104-115
- **Status:** confirmed (experiment E8)
- **Effort:** S

`_converge_lifecycle` is the last step of `reconcile_once`, and room creation, membership and power-level
steps mostly do not catch per-room errors. E8: a single `add_user_to_room` that raises (a 403 on one room)
aborts the pass and the disabled user is never fed to the lifecycle manager. A persistent cosmetic
problem (a room the bot lost power in, a `ConfigurationError` for a missing space) therefore silently
disables security enforcement until someone fixes rooms.

Fix sketch: run the lifecycle pass independently (its own `try`, or before the room work, since it only
needs `matrix_users` and the active MXID set), and let a failure there not suppress the rest.

### LIF-05 Scope asymmetry between "active" and "disabled" queries: wrong-target lock and silent escapes

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/reconciler/engine.py:640-663, onbot/reconciler/engine.py:156-186
- **Status:** confirmed (experiments E5 and E6)
- **Effort:** S

The active set (`active_mxids`) is built with the path, group and attribute filters. The disabled query
uses only the attribute filter, and the guard against hitting a live account is `mxid in active_mxids`,
that is "an *in-scope* active user maps here".

- Wrong target (E6): with `authentik_username_mapping_attribute: attributes.matrix_name` and
  `sync_only_users_in_authentik_pathes: [staff]`, a returning employee gets a new active account under
  path `contractors` with `matrix_name: frank`, while the old account `frank.old` (same `matrix_name`) is
  disabled. `@frank` is orphaned and the live user is locked and, a year later, erased. Authentik does not
  enforce uniqueness of custom attributes, so this does not need a bug, only a reused value.
- Escape: the attribute filter on the disabled query lets users escape offboarding (and get unlocked, see
  LIF-01) by losing the attribute. Disabled users whose mapping attribute is missing are skipped without
  any log line (engine.py:656-657), unlike the active path which warns.
- The docstring and features.md:446-448 ("can only ever touch accounts Onbot provisioned") overstate it:
  any Matrix account whose localpart equals a disabled Authentik user's mapped value in *any* path is in
  scope, including service or bridge accounts that happen to share a name.

Fix sketch: compute the guard set from *all* active Authentik users (no scope filters), drop the attribute
filter from the disabled query (scope filters should decide who gets synced, not who escapes offboarding),
and warn on unmappable disabled users. Consider also excluding Synapse server admins unless explicitly
opted in.

### LIF-06 Dry-run is not side-effect free: it performs live MAS unlocks, including locks onbot never set

- **Severity:** medium
- **Category:** security
- **Location:** onbot/lifecycle/accounts.py:264-272, onbot/lifecycle/accounts.py:170-173
- **Status:** confirmed (experiment E4)
- **Effort:** S

The `reenable` branch runs regardless of `dry_run` ("restorative, only ever grants access back"), and it
runs for every entry that has *any* state, including entries that were only marked. In dry-run onbot never
locks anybody, yet it calls `POST /users/{id}/unlock` in MAS. E4: the security team locks `@y` in MAS
(compromised account) while `@y` happens to be briefly disabled in Authentik; when Authentik re-enables
`@y`, the "audit only" bot unlocks the account. The same happens in live mode for an entry that was only
marked. Granting access back is a write with security impact; ADR-0005 and the config text promise that
dry-run touches nothing.

Fix sketch: record `locked_by_onbot_ts` when onbot actually locks, call `unlock` only when it is set, and
log "DRY-RUN: would unlock" otherwise. Optionally check the MAS user's `locked_at` equals the time onbot
locked it before unlocking.

### LIF-07 MAS lock does not end sessions; re-enable restores every old session, docs say "revoked"

- **Severity:** medium
- **Category:** security
- **Location:** onbot/lifecycle/accounts.py:144-173, onbot/clients/mas_admin.py:8, onbot/clients/mas_admin.py:61-66, docs/adr/0005-quarantine-lifecycle.md:63
- **Status:** confirmed (MAS admin API spec)
- **Effort:** M

The MAS spec says lock "DOES NOT invalidate any existing session, meaning that all their existing sessions
will work again as soon as they get unlocked". Code comments, ADR-0005 and the audit line ("logged out
... revoked all sessions") describe lock as revoking sessions. That is fine for blocking, but on a
legitimate re-enable every session comes back, including the one on the stolen laptop that may have been
the reason for the disable. Admins who disable a user, reset the password and re-enable will be surprised.

Fix sketch: on re-enable after a real lock, finish the user's sessions before unlocking (MAS admin API:
list `compat-sessions` and `oauth2-sessions` filtered by user and `status=active`, then `POST .../finish`),
behind a config switch defaulting to "end sessions". Fix the wording in ADR-0005, `mas_admin.py` and the
audit message to "locked (sessions suspended)".

### LIF-08 No blast-radius guard for mass disables or for flipping `dry_run` off

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/lifecycle/accounts.py:236-262
- **Status:** plausible (code reading)
- **Effort:** S

There is no threshold anywhere. If an LDAP source sync marks most users inactive, an admin bulk-disables
the wrong group, or a policy expires accounts over a weekend, every affected user (including global room
admins, documented as not exempt) is locked after 24 h. The erase stage is a year later and locks are
reversible, so this is an outage, not data loss, but it lands on everyone at once. The same mass event
happens on purpose when an operator flips `dry_run: false` after months of dry-run: every user marked long
ago is locked in the first live pass, with no preview of the count. Meanwhile dry-run logs a
"DRY-RUN: would log out" warning per user per tick indefinitely, so the audit channel is mostly noise by
the time the operator reads it.

Fix sketch: before acting, compare the number of users that would be logged out or erased this pass with
the number of mapped active users; above a configurable ratio or absolute count (for example 10 % or
20 users) refuse all destructive actions in that pass, log an error and surface it in `!status`. Log each
dry-run "would" line once per stage transition, not per tick.

### LIF-09 Silent Synapse-admin fallback under MAS; audit log claims sessions were revoked

- **Severity:** medium
- **Category:** robustness
- **Location:** onbot/app.py:177-193, onbot/lifecycle/accounts.py:119-141, onbot/lifecycle/accounts.py:284-294
- **Status:** confirmed (code reading: no log statement for the effector choice or `dry_run` anywhere outside `accounts.py`)
- **Effort:** S

When `mas_admin` is unset, `app.py` silently picks `AdminApiLifecycleEffectors`. Under MAS these delete
devices and deactivate in Synapse but, per ADR-0005 Q1, leave the MAS token valid. With `dry_run: false`
the audit channel then records "logged out @x (revoked all sessions)" while nothing was revoked. Nothing
at startup states which effector or mode is active. The bot can tell it is behind MAS: `synapse_server.oauth2`
set is a strong hint, and `GET /_matrix/client/v1/auth_metadata` (or the unstable `auth_issuer`) answers
definitively.

Fix sketch: log one startup line with `enabled`, `dry_run`, effector type and cooldowns. If MAS is
detected and `mas_admin` is missing while `dry_run` is false, log an error and refuse destructive actions
(or fail startup). Include the effector name in each audit line.

### LIF-10 MAS lookup 404 is a silent no-op that advances the state machine and is audited as success

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/clients/mas_admin.py:50-59, onbot/lifecycle/accounts.py:157-180, onbot/healthcheck.py:70-74
- **Status:** confirmed for the "user not found" path (experiment E7); route-level 404 needs live stack
- **Effort:** S

`get_user_id_by_username` maps every 404 to `None`; `_resolve` logs a warning and the effector returns
normally. The manager then stamps `disabled_user_timestamp` and writes "logged out ... revoked all
sessions" (E7), and a year later "erased" while only media was deleted. A 404 is not only "no such user":
if the MAS listener behind `mas_admin.url` does not mount the `adminapi` resource (or a reverse proxy does
not route `/api/admin`), every lookup 404s while the token request at `/oauth2/token` still succeeds. Then
all enforcement is a no-op. The healthcheck has the same blind spot: it treats a 404 for the bot user as
"admin API authorized", although the bot is itself a MAS user and must resolve.

Fix sketch: in the effectors raise (or return a result the manager checks) when the MAS user cannot be
resolved, so the state does not advance and the audit says "FAILED". Distinguish MAS's JSON error body from
a route 404. Make the healthcheck require a 200 for the bot's own username.

### LIF-11 Erase semantics differ per effector: MAS always GDPR-erases, Synapse path ties erase to the media flag

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/clients/mas_admin.py:68-70, onbot/lifecycle/accounts.py:138-141, onbot/config.py:460-470
- **Status:** confirmed (MAS spec: `DeactivateUserRequest.skip_erase` defaults to `false`); whether MAS accepts the body-less POST needs live stack
- **Effort:** S

`deactivate_user` posts with no body, so MAS uses `skip_erase: false` and asks Synapse to GDPR-erase the
user (display name, avatar and profile removed, account marked erased) on every erase, regardless of
`include_user_media_on_delete`. The Synapse-admin effector instead passes `erase=delete_media`, coupling
GDPR erasure to the media switch. The config only documents media deletion. Operators get different,
undocumented and irreversible behaviour depending on which effector is active. Also, no integration test
runs the MAS erase path, so it is unverified that MAS accepts the request without a JSON body.

Fix sketch: add an explicit `gdpr_erase_on_delete` option, send `{"skip_erase": not gdpr_erase}` to MAS
and `erase=gdpr_erase` to Synapse, document both, and cover the erase stage in the integration suite.

### LIF-12 Media deletion stops after 100 files and is never retried

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/clients/synapse_admin.py:154-157, onbot/lifecycle/accounts.py:304-305
- **Status:** confirmed (Synapse docs: "If no `limit` is set the API deletes `100` files per request")
- **Effort:** S

`delete_user_media` issues a single `DELETE /_synapse/admin/v1/users/<id>/media`. A user with more than
100 uploads keeps the rest, the ledger entry is popped and the audit says "erased ... media=True". For an
operator who enabled the option to honour a data-protection request this is a silent partial erasure.

Fix sketch: loop until the response's `total` is 0 (or pass a large `limit` and loop on a full page), and
record completion in the ledger (ties in with the two-phase erase in LIF-03).

### LIF-13 Possible lock/unlock oscillation if a MAS lock hides the user from the Synapse user list

- **Severity:** medium
- **Category:** bug
- **Location:** onbot/clients/synapse_admin.py:56-66, onbot/reconciler/engine.py:644-661
- **Status:** needs live stack
- **Effort:** S

`list_users` does not pass `locked=true` or `deactivated=true`, and Synapse excludes locked and deactivated
users by default (`locked` since 1.93). If a MAS lock is reflected in Synapse's `locked` flag, the locked
user vanishes from `matrix_users`, is no longer orphaned, and the next pass unlocks them (LIF-01). They are
re-marked, re-locked after the cooldown, unlocked again: an endless cycle with the user regaining access
for one tick each time. The integration test stops after the lock pass, so it would not notice. Whether
MAS propagates locks to Synapse could not be settled from documentation.

Fix sketch: pass `locked=true` (and `deactivated=true`, then filter explicitly) in the user listing used
for lifecycle, and add a third reconcile pass to `tests/integration/test_lifecycle.py` asserting the token
stays revoked.

## Low

### LIF-14 Ledger robustness: corrupt blob stalls lifecycle forever, lost ledger forgets locks, unbounded growth

- **Severity:** low
- **Category:** robustness
- **Location:** onbot/lifecycle/accounts.py:91-102, onbot/lifecycle/accounts.py:190-205, onbot/lifecycle/accounts.py:274-276
- **Status:** plausible (code reading)
- **Effort:** S

- `load()` calls `model_validate` with no fallback; any malformed content (manual edit, a future schema)
  raises every pass and offboarding stops (with LIF-04, the error is just "reconcile pass failed").
  `schema_version` is stored but never checked.
- If the account data is lost (bot user recreated, `server_name` changed, which changes the type key),
  cooldowns restart, which is the safe direction, but onbot forgets the locks it set: a locked user later
  re-enabled in Authentik is never unlocked and nobody knows why.
- With `delete_after_n_sec: null` logged-out users are never removed, so the blob grows forever and is sent
  in every initial sync of the bot.
- A `mark` replaces the whole entry, so an entry with `disabled_user_timestamp` but no mark loses its lock
  record.
- `onbot reconcile-once` run while the service is running gives two processes writing the same blob,
  last writer wins.
Timestamps are epoch floats from `time.time()` with no string parsing, so there is no timezone issue; a
forward clock jump larger than `delete_after_n_sec` would erase already-locked users in one pass.

Fix sketch: on validation error, log loudly, keep a copy under a backup key and refuse destructive actions
(not re-enables either) until fixed. Add `locked_by_onbot` and pk fields (LIF-01/06) and prune entries for
users absent from both Authentik and Synapse after a retention period.

### LIF-15 Docs drift: cooldown base, "deactivate" naming, ADR and module docstrings out of date

- **Severity:** low
- **Category:** docs
- **Location:** onbot/config.py:435-458, onbot/lifecycle/accounts.py:23-25, onbot/lifecycle/accounts.py:86, docs/features.md:425-428, docs/adr/0005-quarantine-lifecycle.md:63
- **Status:** confirmed (code reading)
- **Effort:** S

- `delete_after_n_sec` says it is "counted from the same point as `deactivate_after_n_sec` (so it should be
  the larger of the two)". The code counts it from the logout timestamp (`now - disabled_ts`), so erase
  happens at `deactivate + delete`. Following the advice to make it "the larger" is harmless but wrong.
- `deactivate_after_n_sec` ("before their Matrix account is deactivated") actually triggers a MAS lock;
  `delete_after_n_sec` triggers the MAS deactivation. The names are inverted relative to MAS terms.
- `accounts.py` still says Q1 "is an open question" although ADR-0005 resolved it.
- ADR-0005 and `mas_admin.py` call lock a "session revocation" (see LIF-07); features.md promises
  offboarding for deleted users (LIF-02).

Fix sketch: correct the descriptions; consider renaming to `lock_after_n_sec` / `erase_after_n_sec` with
the old names as aliases.

### LIF-16 Test gaps: no unit tests for `MasLifecycleEffectors`, partial failures, or re-enable under MAS

- **Severity:** low
- **Category:** test
- **Location:** tests/unit/test_lifecycle.py, tests/integration/test_lifecycle.py:37-52
- **Status:** confirmed (grep: no test references `MasLifecycleEffectors`, `lock_user` or `unlock_user`)
- **Effort:** M

The "highest test rigor" module tests the state machine and the Synapse effectors, but the effector that
production uses under MAS has no unit test, there is no test for an effector raising mid-pass, for a ledger
entry dropping out of the orphan set for non-reactivation reasons, or for dry-run re-enable. The integration
suite covers mark and lock only: no unlock, no erase (`delete_after_n_sec=None`), no third pass. The
experiments E1 to E8 in the scratchpad file are good seeds for regression tests once the fixes land.
