# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Releases 0.0.1 to 0.0.11 of the rewrite are published on PyPI (`onbot`) and Docker Hub
> (`dzdde/onbot`). Their sections below were reconstructed from the git history in November 2026;
> the changelog itself was only kept from 0.0.1-beta.1 on, and until then it listed everything under
> "Unreleased". The leaked-token release blocker of BATTLE_PLAN Phase 1 is closed (review decisions
> D1 and D2, see "Security" below).

## [Unreleased]

### Added
- **Docs (review S14):** deployment examples pin a version and show container hardening (SUP-18); a
  section on upgrading from the legacy bot (ONB-05, D5); changelog sections for the published releases
  0.0.1 to 0.0.11.
- **`!room adopt lobby <group pk> <room id or alias>` (review W6R, D25):** hands a visitor lobby whose
  marker was removed or rewritten back to the bot, with the same rules as for group rooms (allowlisted
  admins, rooms the bot created only, D20); refused when the group has no lobby in the configuration.
- **The admin control room is repaired like the other managed rooms (review W6R-08):** a removed or
  rewritten marker, a lowered marker lock and a removed alias are restored at startup and after every
  pass, and reported once.
- **`!pending` lists held repeated announcements (review W6R-04)** beside the guard's batches.
- **`!room adopt <group pk> <room id or alias>` (review S18, W4R-06):** an allowlisted admin hands a
  group room that lost its marker, or carries one somebody else wrote, back to the bot. Only rooms the
  bot created (D20), only for a group it keeps a room for that has no other room; otherwise it refuses
  and changes nothing.
- **Avatars are re-checked about once a day (review S18, ONB-15):** with a conditional request (ETag,
  Last-Modified, else a content compare), so a new picture behind an unchanged URL reaches the bot
  profile, rooms, lobbies, the space and notice boards without a restart. A failed re-check keeps the
  current picture.
- **Announcements render markdown (review S12, ONB-10),** like the welcome messages; plain text stays
  a plain message.
- **`!lobby reopen <group pk | room id | alias>`** hands a manually closed lobby back to the bot (review
  S7, D16); "Rooms:" reports in the admin room; live tests for room teardown and deletion.
- **Welcome messages can carry an `id`** so their text can be corrected without re-sending; whitespace-only
  edits no longer re-send (review S8, ONB-18).
- **An encrypted control room or notice board is reported** in the log and the room (ONB-06; Synapse's
  `encryption_enabled_by_default_for_room_type` cannot be opted out of at creation).
- **`mas_admin.upstream_subject_attribute` (default `uid`) and `mas_admin.upstream_provider_id`** for
  resolving users through MAS upstream links (review S9, D8).
- **`gdpr_erase_on_delete` offboarding option (review S4, D7, default `false`)**, applied identically
  through MAS (`skip_erase`) and the Synapse admin API (`erase`).
- **"Offboarding check" startup step (LIF-09):** logs the offboarding setup and what the configured
  MAS can do, and reports a MAS-backed homeserver without `mas_admin`.
- **Admin commands for paused destructive actions (review S3, D13):** `!pending`, `!confirm <id>` and
  `!reject <id>`; `!status` shows the number of paused batches. The admin room reports paused and
  closed batches, offboarding attention (frozen, failed, aborted users) and rooms that did not
  converge.
- **`onbot reconcile-once --dry-run` (MNT-09):** reads from the live servers and logs every write it
  would make, offboarding included, without changing anything or saving state.

### Changed
- **Group pks match in any UUID form everywhere (review S18 and W6R, W5R-02, W6R-01). Check before
  upgrading.** `authentik_group_pks_granting_bot_admin`, `sync_only_users_of_groups_with_id`,
  `authentik_group_id_ignore_list`, `only_for_children_of_groups_with_uid`,
  `per_authentik_group_pk_matrix_room_settings` and `!lobby reopen` accept the 32-hex form, the
  hyphenated form and upper case. Before, a pk written in the 32-hex or upper-case form silently
  matched nothing in the ignore list, the parent filter and the room settings. After the upgrade such
  entries take effect, which can be destructive: a group in the ignore list that has had a room until
  now counts as gone, so with `disable_rooms_when_mapped_authentik_group_disappears` its room is
  closed and its members kicked, and with `delete_disabled_rooms` the room is deleted (the guard asks
  only above its limits); a parent filter now matches children; a room settings override starts to
  apply (lobby, encryption, levels). If any of these settings uses a non-hyphenated form, run
  `onbot reconcile-once --dry-run` after the upgrade and before the first real pass, and check the
  planned actions.
- **The integration suite runs in parallel workers (review S14, D17):**
  `ONBOT_ITEST_WORKERS=<k> ./run_integration_tests.sh` gives every pytest-xdist worker its own stack.
  CI uses two workers, about 12 minutes instead of 22 on a workstation.
  `./run_integration_tests.sh --down` removes every stack of an instance.
- **Coverage gates (review S14, MNT-15):** the fast suite needs 90 %, the full suite 92 % (was 88).
- **The control room help text uses plain punctuation (review S14);** the bot pins it once more after
  the upgrade.
- **`!room adopt` checks what kind of room it takes (review W6R-05):** it refuses spaces, users' notice
  boards and, as a group room, rooms with a lobby's `restricted` join rule. If the marker lock was
  raised but writing the marker failed, the reply says so.
- **Held announcement ids are matched in any case (review W6R-04)** like guard batch ids, and never
  collide with an open batch id.
- **The control room help text changed** (the `!room adopt [lobby]` line), so it is posted and pinned
  once more after the upgrade.
- **The same `!announce` twice within 60 seconds asks first (review S18, ONB-11):** the bot answers
  with an id; `!confirm <id>` from the same admin within 10 minutes sends it, `!reject <id>` drops it.
- **A shutdown interrupts an announcement visibly (review S18, W5R-04):** an `!announce` arriving while
  the bot stops is not sent, and a broadcast cut off by the stop posts how many rooms it reached.
- **Rooms blocked by someone other than the bot are a hold (wave 4 review, W4R-02, D23):** server
  admin moderation, legal hold, or a block whose author is unknown. When their group disappears the bot
  neither kicks the members nor deletes the room while the block exists, even with
  `delete_disabled_rooms`; it reports the room once and the room does not count against the guard.
  After the block is lifted the normal teardown applies.
- **A closed room with members the bot cannot kick stays closed (wave 4 review, W4R-01):** extra v12
  creators or PL 100 humans in older rooms no longer make the bot tear the room down again on every
  pass, so such rooms no longer count against guard layer 2 on every pass.
- **A recorded binding wins without MAS (wave 4 review, D21):** when MAS is not configured or links
  the account to nobody, a user with a recorded binding keeps the account against users who only name
  it by the mapping attribute; those are skipped and reported. Before, both were frozen. With MAS
  configured but unreadable it still fails closed for that pass.
- **`!announce` runs beside the sync stream (review S12, ONB-03):** it answers "Sending the
  announcement to N rooms" and posts the result when done. A second `!announce` while one runs is
  refused, not queued; a broadcast still running at shutdown is cancelled.
- **Broadcasts follow the homeserver's rate limit instead of failing (review S12, ONB-03):** once
  throttled they send one message at a time at the pace Synapse asks (each wait capped at 60 s) and
  give up only after 5 minutes without progress. Each room keeps one transaction id across attempts.
  `!status` and the result say whether the bot's rate limit override is in effect.
- **Shorter broadcast results (review S12, ONB-04):** the result names at most 20 failed rooms and
  counts failures by cause, without request URLs. Every room is in the log; a result that cannot be
  posted is replaced by a short note.
- **Broadcasts with nobody in the sync scope are refused (wave 4 review, W4R-05):** `onbot broadcast`
  and `!announce` say "no users are mapped" (CLI exit 1) when Authentik returns nobody in the sync
  scope, instead of reporting "sent to 0 rooms".
- **An admin who declines or leaves the control room is not invited again (review S12, ONB-12):**
  removing them from the admin set and adding them again sends a new invitation. Admins who left
  before the upgrade get one more invitation.
- **Avatars are keyed by image content as well as URL (review S12, ONB-15):** a new picture behind an
  unchanged URL reaches the bot profile, rooms, lobbies, the space and notice boards on the next start.
  Each avatar URL is fetched once per start. The first start after the upgrade rewrites every onbot
  room marker once (new `avatar_sha256` field).
- **Rooms are repaired on every pass, not only at creation (review S7, REC-09):** the space child and
  parent links; the onbot marker (restored when removed, rewritten, unreadable or written by somebody
  else, and completed once for older markers, which drops the legacy `authentik_server` key); the
  marker's lock; a removed alias (recreated when it is free) and an empty main address. An alias
  another room holds is reported once. A changed alias setting also adds the new alias to the
  existing room when it is free.
- **A visitor lobby whose feature is switched off is closed (join rule `invite`) and removed from the
  space, without kicks (REC-10, D19);** closing is a reversible destructive action under the guard's
  layer 2. Switching it on again reopens it.
- **A visitor lobby a human closed stays closed (D16);** it is logged at WARNING and reported once.
- **`onbot broadcast` reads Authentik to know who is mapped** and exits 1 when nobody can be mapped;
  `!announce` is refused until the first pass. With user sync off there are no welcomes and no
  broadcasts (review S8).
- **Welcomes run in a background queue (4 at a time) beside the reconcile pass** instead of inside it
  (RUN-08).
- **A user who left their notice board is invited back once;** broadcasts skip and count rooms whose
  user left (ONB-07).
- **Guard batches expire more conservatively (review W3R, pending maintainer confirmation):** erasures
  and room deletions in a rejected batch stay held until the batch expires, even when fewer than
  `confirm_irreversible_actions_from` remain. A confirmed batch that cannot run within
  `confirmation_expiry_sec` of its confirmation (for example a room deletion waiting for its teardown)
  is closed and reported, and its actions are judged again.
- **Users are resolved through MAS (upstream link by subject) when `mas_admin` is set, else through the
  `authentik pk -> MXID` binding recorded at first sight (review S9, D8, SEC-07);** the mapping
  attribute only applies to users never seen. A renamed user, or a changed
  `authentik_username_mapping_attribute`, no longer causes kicks, demotions, locks or new welcomes; the
  change is reported in the admin room.
- **Several Authentik users resolving to one Matrix account (D21):** a user linked to it through MAS
  keeps it and those who only name it by the mapping attribute are skipped. Otherwise (two MAS links,
  two recorded bindings, a MAS link against another user's binding, a binding against an attribute
  guess) none of them is joined, kicked, re-levelled, locked or erased. Either way it is reported in
  the admin room.
- **An unreadable `known_users` record pauses membership and power-level management** (and
  offboarding, as before) until repaired.
- **A `global_room_admins` entry whose account is on `matrix_user_ignore_list` is no longer granted PL
  100** (MNT-13).
- **Tests: the integration stack's Authentik provider sends the hashed user ID as `sub`.**
- **Obsolete-room teardown only acts on bot-created rooms whose locked marker names a vanished group**
  (review S6), and waits while any bot room is unreadable. Unadopted legacy rooms are never torn
  down. Groups with colliding aliases get no new room (REC-13); rooms upgraded by a human are
  reported and skipped. Changing `alias_prefix` or the alias attribute keeps existing group rooms; a
  changed space or control-room alias still creates a new one. Notice-board setup interrupted after
  creation is completed later (ONB-02).
- **Upgrade note for operators (review S6):** on the first pass every existing marked room the bot
  created is adopted at its configured alias ("adopted ... found at its alias" in the log). Stranded
  marked rooms and rooms created by another account are reported, not adopted.
- **Room deletion is its own irreversible step (review S5, D19):** closing a room (block and kick)
  stays under `max_reversible_actions_per_pass`. A room is only deleted in a pass that also closes it,
  and never while that closing waits for a confirmation.
- **`!reject <id>` holds a batch only while the condition that created it lasts** (review W2R, D13),
  at the latest until it expires; then its actions are judged again and the end is reported.
  `!pending` and `!status` list rejected batches.
- **`admin_room.enabled` now defaults to `true` (breaking for configs without admins, review W2F,
  D19):** the admin room and `!confirm` are available out of the box. An enabled admin room needs at
  least one admin in `admin_room.admin_user_ids` or `admin_room.authentik_group_pks_granting_bot_admin`,
  otherwise onbot refuses to start (exit 2). Configs without admins must add one or set
  `admin_room.enabled: false`.
- **Tests run against MAS 1.28.0 and Synapse 1.164.2**, pinned by digest, with Synapse's stable
  `matrix_authentication_service` config. The lifecycle integration tests assert and exercise ending
  sessions and deactivating without GDPR erasure.
- **Erasure no longer GDPR-erases unless `gdpr_erase_on_delete: true` (review S4, D7).** Before, MAS
  always erased, and the Synapse path tied erasure to `include_user_media_on_delete`. On a MAS that
  always erases (0.14), an erase with `gdpr_erase_on_delete: false` is refused and logged as FAILED.
- **Offboarding audit lines say exactly what each effector did** (LIF-09).
- **Switching offboarding `dry_run` off goes through the per-pass limit** of the guard, and dry-run
  audit lines are written once per stage instead of every tick (LIF-08).
- **httpx and httpcore request lines are logged only at `log_level: DEBUG`** (RUN-15).
- **`onbot healthcheck --deep` fails when MAS does not know the bot's own user** (LIF-10).
- **Shorter CI waits (review decision D17):** pushes and PRs that change documentation only skip the
  integration suite (about 20 minutes); any other path, and `CHANGELOG.md`, still runs it. A release
  reuses a green CI run of exactly the tagged commit on `main` (all four jobs passed) and runs the full
  CI only when there is none. The secret scan now covers the whole git history.
- **Release and build pipeline (review S15):** image and PyPI package share one PEP 440 version
  (`scripts/release_version.py`); pre-release image tags use the normalised form (`0.2.0b1`), and a
  non-release tag fails the build instead of shipping `0.0.0`. The image is built reproducibly from
  `pdm.lock` with hash-checked wheels; tini is a checksummed static binary; the base image is
  `python:3.14.10-slim-bookworm` pinned by digest. Runtime dependencies are capped below their next
  major version, and the sdist no longer ships the tests. Dependabot proposes updates for GitHub
  Actions, the base image and `pyproject.toml` ranges (lockfile updates stay manual, `pdm update`).
- **Dev scripts:** `provision-bot-account.sh` never puts the token in argv or prints it in full, can
  end previous sessions with `--revoke-previous`, and no longer truncates MAS tokens at an underscore;
  without a terminal it needs `--quiet` or `--write-token-to`. The integration stack binds to
  127.0.0.1, and `build_dev_env.sh` no longer leaks `set -e` into the calling shell.
- **Stricter config validation (review S13), may reject configs that loaded before:** unknown keys are
  rejected at every level (typos fail loudly); exactly one bot credential (`bot_access_token` xor
  `oauth2`); URLs must be http(s); MXIDs must be complete, the bot's on `server_name`; alias fields are
  localparts only (no `#`, no `:server`); `server_tick_rate_sec > 0` and other intervals `>= 0`;
  `delete_disabled_rooms` without disabling rooms, and onboarding rooms inside a space without a space,
  are rejected; `log_level` accepts all standard levels. An invalid config exits with code 2 and lists
  one line per field, without values.
- **`reconcile-once` runs every startup step of the service once** (device, rate-limit override, bot
  avatar, admin room and its admin invites), then one pass. A cron-driven deployment never runs the
  service, so it needs all of them; earlier in this cycle the avatar and the admin room were skipped.
- **Offboarding of a known user who is disabled and leaves the scope at once (D14):** a user onbot
  has seen active, in scope and mapped is now offboarded when disabled in Authentik even if they left
  the sync scope at the same time (moved to another path, removed from the synced group, opt-in
  attribute removed). Before, nothing happened and nothing was reported. Users who leave the scope
  while active stay frozen (D9), and accounts onbot never mapped stay untouched.
- **The Docker image is based on Debian trixie** (`python:3.14.10-slim-trixie`) instead of bookworm.
- **`sync_authentik_users_with_matrix_rooms.enabled: false` no longer kicks anyone (review S2):** it
  stops membership and power-level management instead of treating every member as unwanted. A pass in
  which no group room has any desired member skips all kicks and reports it.
- **Power levels the bot granted are withdrawn when the granting membership ends (D10, G8.4).** Only
  entries the bot set itself (tracked in the bot's per-room account data) are withdrawn; levels set by
  humans stay.
- **Rooms older than version 12 get at most power level 99** from the bot, so it can always take the
  level back. Users already at 100 and extra room creators are reported, not fought over.
- **Deleted Authentik users are offboarded (review S1):** a user onbot has seen active and in scope
  who is later deleted from Authentik goes through the usual lock, deactivate and erase cooldowns.
  Matrix accounts onbot never mapped are never touched. Tracked users who leave the sync scope are
  frozen (no unlock, no further step) and reported. An empty Authentik user list skips the lifecycle
  step.
- **Dry-run calls no lifecycle effector at all**, unlock included, and onbot only unlocks locks it set
  itself.
- **`onbot healthcheck` checks the bot's own liveness (review S10):** the reconcile loop and the sync
  pump write a heartbeat file (`ONBOT_HEARTBEAT_FILE`, default `/tmp/onbot-heartbeat.json`, or
  `--heartbeat-file`); the default healthcheck reads it and needs no config, network or token. The
  previous upstream probe is `onbot healthcheck --deep`. A long Authentik outage now marks the
  container unhealthy.
- **The image runs `tini` as PID 1** and the `HEALTHCHECK` uses a 10 s timeout and a 60 s start period.
- **`broadcast` skips startup side effects** it does not need (bot avatar, admin room). `onbot run`
  exits 1 when one of its loops crashes.
- **Development tooling (review S0):** `pdm.lock` is the only lockfile and CI fails when it is stale;
  the gitleaks secret scan runs in CI again (tree plus new commits); the pre-commit hooks run again and
  take ruff, mypy and the config-docs check from the pdm environment.
- **Tests (review S0):** the suite ignores a local `config.yml` and `ONBOT_*` variables; an integration
  stack that cannot start fails the run instead of skipping it; `ONBOT_ITEST_INSTANCE=<n>` runs an
  isolated stack so several checkouts can run the integration suite at the same time.

### Fixed
- **One download per picture (review W6R-02):** an avatar host whose bytes differ per request was
  downloaded once per room and pass and never set; the image just fetched is now the one uploaded.
- **The "shutting down, not sent" reply to `!announce` is posted (review W6R-03);** before, the second
  stop cancelled it.
- **Notice boards welcomed while the bot avatar could not be fetched get the picture once it can be
  fetched (review W6R-07),** without a restart.
- **A lost room DELETE no longer fails while Synapse still has the deletion scheduled (review S18):**
  the delete status is asked again on 404.
- **A room that wore a picture from before image hashes were recorded gets the current picture when it
  changed meanwhile (review S18, W5R-03).**
- **The image cache keeps no image bytes (review S18, W5R-05),** and a failed fetch no longer re-raises
  one ever-growing exception.
- **A room whose block status cannot be read is left alone for that pass (wave 4 review, W4R-04)**
  instead of being fully managed (REC-11 failed open).
- **A refused repair no longer blocks other work (wave 4 review, W4R-07):** space upkeep is its own
  step, so a refused space repair no longer keeps new users out of the space; each room repair runs on
  its own.
- **Markers are restored from what the bot wrote (wave 4 review, W4R-08):** a restored marker keeps
  `global_admins`, `avatar_source_url` and `avatar_sha256`; a marker someone else wrote is never
  re-signed by the bot.
- **A notice board the bot was removed from is replaced (wave 4 review, W4R-03):** detected by the
  bot's own membership; before, every welcome failed with a 403.
- **A group room whose marker was removed or rewritten while the bot was stopped is reported with
  instructions (wave 4 review, W4R-06);** a marker restored as the bot account counts from the next
  pass.
- **A room the bot already closed is no longer torn down again on every pass.** Before, from 11 closed
  rooms on, every kick and teardown waited for a confirmation (review S7).
- **Concurrent welcomes no longer lose `m.direct` entries,** which caused duplicate notice boards and
  re-sent welcomes after a restart (review S8, RUN-01, ONB-01).
- **Welcome sends use deterministic transaction ids,** so a repeated send is deduplicated by Synapse
  (ONB-09).
- **A notice board the bot is no longer in is replaced** instead of failing every pass; unreadable
  notice-board state is reported once (ONB-08).
- **After following a bot-created room upgrade, the next pass no longer reports the old and new room as
  a conflict** (review W3R).
- **When protecting a new room's marker fails at creation, the next pass retries it** instead of
  treating the room as protected.
- **The media deletion that continues an erasure over later passes no longer asks for a new
  confirmation every pass;** only an implausible Authentik answer (layer 1) still pauses it.
- **An unreadable lifecycle ledger is reported once again** instead of every pass failing (a
  regression of the guard work, review W2R).
- **Room deletion uses Synapse's asynchronous v2 API and waits for it,** so a long purge is no longer
  reported as a failed teardown.
- **A lock or erase that became due after the pass was planned is logged as deferred,** not as waiting
  for an admin.
- **Media deletion removes all of a user's uploads** over several passes, not just the first 100
  (review S4, LIF-12).
- **A freeze from `authentik_user_ignore_list` no longer ends when the user is deleted in Authentik.**
- **The offboarding ledger is robust (LIF-14):** a corrupt entry is quarantined instead of stopping
  offboarding for everyone, an unreadable ledger stops the step without overwriting it, a missing
  ledger while MAS has locked users raises an alert, and lock records of deleted users with erase
  disabled are dropped after 90 days (the MAS lock stays).
- **Config descriptions and ADR-0005 match the offboarding code** (LIF-15).
- **Retries never duplicate effects (review S11, RUN-05):** non-idempotent requests (createRoom, kick,
  force-join) are no longer blindly resent after a timeout or 5xx. A room created despite a lost
  response is found by a creation marker and reused instead of being created twice; kick and join
  check the membership before resending.
- **OAuth2 token refresh is single-flight (RUN-06, SEC-10):** concurrent callers share one MAS
  exchange and a rotated refresh token is presented once. MAS 5xx and unreachable errors are retried,
  a 401 replaces the token once, and only `invalid_grant` drops the refresh token.
- **429 responses wait as long as `retry_after_ms` or `Retry-After` asks** (capped at 30 s). Matrix
  calls time out after 30 s; only the sync long-poll waits longer (RUN-10).
- **A paginated Authentik or Synapse listing that changes while it is read is re-read (RUN-14);** if it
  stays inconsistent the pass fails instead of acting on a list with gaps (a missing user would be
  kicked).
- **Startup behind a reverse proxy:** a 403 or 404 without a Matrix error body while the homeserver
  boots no longer makes startup give up device registration and open onboarding without a device;
  such answers are retried.
- **The avatar fetcher's HTTP client is closed on shutdown.**
- **`provision-bot-account.sh --revoke-previous`** no longer ends all sessions before it knows a new
  token can be issued: it issues a probe token first and changes nothing if that fails. If the final
  token then fails, it says that all sessions were ended and to rerun without `--revoke-previous`.
- **Users on `matrix_user_ignore_list` were kicked** from group rooms; the bot's own MXID could be
  written into the power-level users map; a failed lookup of the global admin group kicked and demoted
  the admins everywhere; a per-group `keep_updating_matrix_attributes_from_authentik` override was
  ignored (review S2).
- **Offboarding no longer stalls on one user (review S1):** a failing user backs off instead of
  blocking everyone, the ledger is saved per user, and an erase resumes without repeating the
  deactivation.
- **A MAS lookup 404 is a failure, not "nothing to do":** only MAS's own not-found answer means "no such
  user"; a 404 from a missing or unrouted admin API fails the step and `onbot healthcheck --deep`. The
  lock audit line no longer says "erase in Nones".
- **A cold start heals by itself (review S10):** startup steps (device registration, admin room,
  rate-limit override, bot avatar) retry in the background until Synapse and MAS are up; welcomes
  start only after the device is registered. Previously the bot ran degraded until restarted.
- **The sync pump recovers from `M_UNKNOWN_POS`** and similar unknown-connection errors by opening a
  fresh connection, so the admin room no longer goes deaf after a sync connection expires.
- **`docker stop` exits cleanly within about a second:** SIGTERM is handled from process start with a
  6 s grace period; a second signal cancels immediately. Previously early stops ended in SIGKILL.
- **`--log-level` after the subcommand** (`onbot reconcile-once --log-level DEBUG`) failed with an
  argparse error. It is now accepted before and after the subcommand.

### Removed
- **Unused Synapse admin client methods (review S14, MNT-10),** among them
  `set_user_server_admin_state`, which could make any user a server admin; also the unused
  `drift_detected` signal.
- **`synapse_server.oauth2` (breaking for configs that set it, review S11, RUN-11):** MAS does not grant
  the Matrix API or Synapse admin scopes to `client_credentials`, so the option never worked. A config
  that still sets it stops at startup with exit code 2 and says so. Use
  `synapse_server.bot_access_token` with a MAS compatibility token (`scripts/provision-bot-account.sh`),
  which is now required. `mas_admin` is unchanged.

### Security
- **The bot no longer re-signs control room marker content somebody above the marker lock wrote
  (review W6R-08, D20).**
- **A room avatar chosen by a human, possibly on another server, is no longer handed to every room that
  wears the same picture (review W6R-06).**
- **The sync scope's group filter is re-checked on each user object (review S18, SEC-09):** users
  whose own groups name none of `sync_only_users_of_groups_with_id` are left out and logged as an
  error. If Authentik stops naming groups, the empty scope is held by guard layer 1 and nobody is
  offboarded.
- **The bot never re-signs a marker it read in the room (review S18, W5R-01):** before, after a failed
  marker repair, the avatar and power-level steps wrote back a marker somebody above the lock had
  rewritten to another group, and after a restart the bot could tear the room down as that group's
  room. The avatar step is also skipped after a failed marker repair.
- **Edits are never commands (review S12, ONB-11, SEC-12):** an `m.replace` event is not executed; an
  admin who edits a command is told to send a new message.
- **Only allowlisted admins' commands enter the replay buffer (review S12, SEC-12):** a non-admin
  member can no longer push real commands out of it.
- **Bot admin group membership is re-checked on each Authentik user object (review S12, SEC-09)**
  instead of trusting the `groups_by_pk` filter; users the filter should have excluded are dropped and
  logged as an error.
- **The bot unblocks only rooms it blocked itself (review S7, REC-11):** a room a server admin blocked
  stays blocked and unmanaged and is reported once, and teardown keeps an existing foreign block.
  Lobbies closed by a teardown are now unblocked when their group returns.
- **Only users the reconciler maps get a notice board and receive broadcasts (review S8, SEC-06,
  ONB-13):** active Authentik users in the sync scope with a Matrix account and no identity conflict.
  Joining a room the bot is in no longer enrols anyone. Notice boards of deactivated or foreign
  accounts are dropped from `m.direct`.
- **No attribute guess takes an account MAS links to somebody else (review W3R, D21):** also when that
  owner is disabled, outside the sync scope or never seen by onbot; the guesser is skipped and
  reported.
- **While MAS cannot be read, users onbot has never seen are skipped** and the accounts their mapping
  attribute names are left alone (no join, kick, lock or erase); no binding is recorded from an
  unchecked guess.
- **A room's onbot marker counts only when the bot wrote it (review W3R):** a marker rewritten by a
  moderator (legacy unlocked rooms) or by a user at the bot's level (rooms older than version 12) is
  reported, and the room is neither adopted nor torn down.
- **Every URL path segment in the Synapse, MAS and Matrix clients is percent-encoded (review S9,
  SEC-08),** so a localpart such as `x/../victim` can no longer redirect a MAS lock to another account;
  absolute URLs are only followed to the client's own origin.
- **Rooms are recognised by creator plus a protected onbot marker, no longer by canonical alias
  (review S6, REC-04, SEC-02, D20):** a room another user created at a group's alias is never adopted;
  the group is skipped and reported, nobody is force-joined into it, no second room is built. A
  moderator changing or removing an alias no longer breaks every pass or creates a second room. The
  marker event types are locked at room creation, so moderators cannot rewrite `group_id`.
- **The admin control room is only a room the bot created with its marker;** a foreign room at the
  alias is never taken over (ONB-17).
- **Markers no longer store the Authentik URL, and the teardown kick reason no longer names it**
  (SEC-11).
- **Guard layer 3 for irreversible actions (review S5, D4, D13):** from
  `destructive_action_guard.confirm_irreversible_actions_from` (default 3) irreversible actions in one
  pass (account erasures, room deletions under `delete_disabled_rooms`), they wait for `!confirm <id>`
  in the admin control room. One or two run as before. The batch report marks them as irreversible.
- **An unreadable guard state fails closed (review W2R):** it no longer starts empty (which dropped
  the last known good counts and rejected batches, so implausible removals could run). It pauses
  every destructive action, is reported once in the admin room and is never overwritten. `!guard
  reset` replaces it and keeps a verbatim copy in the bot's account data.
- **Retention no longer forgets lock records while the guard distrusts the pass's user list.**
- **Power-level demotions of members who stay** (for example 100 to 50 when a power-level group drops
  out) go through the guard like withdrawals.
- **No fake offboarding on a MAS-backed homeserver without `mas_admin` (review W2F, LIF-09, D19):**
  onbot used to "lock" users through the Synapse admin API there, which left every MAS session valid.
  It now refuses to lock or erase, logs it at ERROR and reports it once in the admin room. Disabled
  users are still marked, so the locks follow once `mas_admin` is configured. Until the homeserver
  has said whether it uses MAS, the Synapse fallback locks nobody. Onboarding and rooms are not
  affected.
- **An offboarding lock ends all of the user's MAS sessions (review S4, LIF-07):** compat, OAuth 2.0 and
  browser sessions; an unlock ends any left first, so re-enabling a user no longer revives pre-lock
  sessions. Needs a MAS with the session finish admin endpoints; on older MAS the audit line says the
  sessions are only suspended. Synapse may accept an ended token for about two more minutes (LIV-03).
- **The avatar fetch guard refuses to start if httpx changes the internals it relies on;** `httpcore` is a
  declared dependency (review S11).
- **Releases are gated (review S15, SUP-02):** the release workflow runs the full CI on the tagged
  commit, requires the tag to be on `main` and publishes from a `release` environment. All workflows
  run with read-only defaults, pin every action by commit SHA, never persist checkout credentials, and
  CI checks out the PR head SHA and only checks formatting instead of committing fixes.
- **Avatars are fetched only from public addresses (review S13, SEC-03):** private, loopback,
  link-local and similar destinations are refused unless listed in
  `avatar_fetch.allowed_internal_destinations`; the checked address is the one connected to (no DNS
  rebinding), redirects are re-checked per hop (at most 5), downloads are capped at
  `avatar_fetch.max_size_bytes` (5 MiB) and `avatar_fetch.timeout_sec` (30 s) and must be images.
- **Config errors never print values (RUN-07)**; previously a validation error could print a short
  secret in clear text. All credentials are held as `SecretStr`.
- **One failing room no longer stops the pass (review S2, REC-03):** each room converges in isolation,
  failures are reported once per room and pass, and the offboarding step always runs. Previously a
  single room the bot could not manage stopped every pass, including offboarding.
- **Offboarding fails closed (review S1, LIF-01):** onbot never unlocks a user unless the Authentik
  user it is bound to is present, active and in scope. Previously deleting a locked user in Authentik,
  stripping a scope attribute, an ignore-list edit or an empty API answer unlocked the account in MAS
  and revived its old sessions.
- **Leaked tokens in git history (SUP-01):** both old `syt_` tokens were already invalid (review
  decision D1); the history was scrubbed and force-pushed on 2026-11-06 (S16), so every commit SHA
  changed. Re-clone instead of pulling.
- **Destructive actions go through a guard (review S3, D4, D13, fixes REC-01):** an empty answer from
  Authentik, a drop of more than 20 % in active users in scope (pauses every removal) or in mapped
  groups (pauses kicks, withdrawals and teardowns), or more than 10 reversible removals in one pass
  pause those actions. Paused actions become a batch reported in the admin room; `!confirm <id>`
  releases exactly that batch (only what still applies), `!reject <id>` discards it, and batches
  expire after 72 h. Configurable under `destructive_action_guard`. Before, a narrowed Authentik token
  kicked everyone and locked every known user, and a collapsed answer withdrew every bot-granted power
  level. Replaces the S2 kick heuristic.

## [0.0.11] - 2026-08-04

### Changed
- Renaming a group room and setting its topic from Authentik are logged at INFO.

## [0.0.10] - 2026-08-04

### Changed
- **The room name comes from the group's `chatroom_name` attribute by default** and falls back to the
  group's own name when the attribute is missing or empty (`matrix_name_from_authentik_attribute`
  now defaults to `attributes.chatroom_name`; set it to `name` for the old behaviour). A numeric
  attribute value is turned into text.

### Fixed
- **Visitor lobbies follow a renamed group room:** with `keep_updating_matrix_attributes_from_authentik`
  the lobby's name and topic are re-applied on every pass, like the group room's.

## [0.0.9] - 2026-08-04

### Fixed
- **A reconcile pass no longer aborts on the first room the bot is not in.** Looking for obsolete group
  rooms read the state of every room on the server, and the 403 for a room a user created for
  themselves stopped the pass before space and room membership were synced.

## [0.0.8] - 2026-08-04

### Fixed
- **The bot's profile picture is no longer re-set on every restart,** which posted a "changed their
  profile picture" event into every room the bot shares. The bot records the source URL and the
  uploaded picture in its account data; on the first start after the upgrade it compares the image
  bytes instead and adopts a matching picture.

## [0.0.7] - 2026-08-04

### Added
- **`global_room_admins`:** Authentik usernames that are joined to every group room, visitor lobby and
  the space at power level 100 regardless of their groups, and never kicked by the membership sync.
- **`docs/authentik-groups.md`:** how to set up Authentik groups and their attributes for onbot.

### Fixed
- **Group room topics were rewritten on every reconcile pass,** because the Synapse room list never
  returns a topic. The topic is now read from the room's `m.room.topic` state before it is written.

## [0.0.6] - 2026-08-04

### Added
- **Markdown welcome messages** with the placeholders `{space_link}`, `{space_alias}`, `{space_name}`
  and `{user_id}`. A message using a space placeholder while no space is configured is held back with
  a warning.
- **Visitor lobbies wear their group room's avatar.**

## [0.0.5] - 2026-07-13

### Added
- **Visitor lobby rooms ([ADR-0012](docs/adr/0012-lobby-rooms.md)):** an optional second room per
  group that every member of the space can find and join, next to the unchanged private group room
  (`visitor_lobby_*` settings, per group via `matrix_room_visitor_lobby_from_authentik_attribute`).
- **Room version 12 support ([ADR-0011](docs/adr/0011-room-version-12.md)):** power levels and room
  IDs are read correctly in v12 rooms, where the creator holds an infinite level. New setting
  `synapse_server.room_version` (default: the homeserver's default version).

## [0.0.4] - 2026-07-10

### Fixed
- **New bot admins are invited to the admin control room** on every reconcile pass, not only at
  startup.

## [0.0.3] - 2026-07-10

### Added
- **`authentik_poll_rate_sec` (default 15):** a cheap poll asks Authentik whether users or groups
  changed and runs a reconcile right away when they did. New users no longer wait for the full pass.

### Changed
- **`server_tick_rate_sec` now defaults to 300 instead of 20,** since the poll above handles new users;
  the full pass only repairs drift made inside Matrix.

### Fixed
- **A new user could receive the welcome messages twice** when the reconcile signal and the user's own
  join event arrived together. Welcomes for one user are serialised, and each sent message is recorded
  right away.

## [0.0.2] - 2026-07-10

### Added
- **The onboarding room is a read-only notice board:** created as a private room in which only the
  bot may post, with the user force-joined once (`force_join_onboarding_room`), and with its own name,
  topic and avatar. Its power levels are repaired when they drift. Rooms created by earlier versions
  stay writable for their user; see [docs/troubleshooting.md](docs/troubleshooting.md).
- **`onbot broadcast "<message>"`** sends a notice into every user's onboarding room.
- **Admin control room ([ADR-0010](docs/adr/0010-admin-control-room.md)):** allowlisted admins send
  `!announce`, `!status` and `!help`. Admins come from `admin_room.admin_user_ids` and from the
  members of the Authentik groups in `admin_room.authentik_group_pks_granting_bot_admin`.
- **Obsolete group rooms are torn down:** a room whose Authentik group disappeared is blocked and
  emptied (`disable_rooms_when_mapped_authentik_group_disappears`) and optionally deleted
  (`delete_disabled_rooms`).

### Changed
- **The sliding-sync loop moved into `onbot/sync.py` (`SyncPump`),** shared by onboarding and the
  admin control room.

### Fixed
- **`ONBOT_*` environment variables override the config file again;** they were ignored as soon as a
  config file was present. The file and the environment are merged, so a single secret can come from
  the environment.
- **`log_level` from the config file takes effect** (before, only `--log-level` and `ONBOT_LOG_LEVEL`
  did).

## [0.0.1] - 2026-07-08

First stable release of the rewrite. It contains everything from the 0.0.1 pre-releases plus:

### Added
- **Space and group room avatars** are applied on every pass and re-uploaded only when the URL
  changes, so they also reach existing rooms.
- **`scripts/provision-bot-account.sh`** registers the bot in MAS and issues its compatibility token.
- **Docs:** `docs/architecture.md`, `configuration.md`, `deployment.md`, `development.md`,
  `testing.md` and `troubleshooting.md`; `BATTLE_PLAN.md` and `GOALS.md` moved to `docs/project/`.

### Fixed
- The PyPI upload of the release workflow, and the package version derived from the git tag.

## [0.0.1-beta.3] - 2026-07-08

### Changed
- The release workflow publishes the image to Docker Hub (`dzdde/onbot`) instead of GHCR.

## [0.0.1-beta.2] - 2026-07-08

### Changed
- **Release workflow:** runs on published GitHub Releases. A pre-release publishes the image as `beta`
  plus the version and uploads the pre-release to PyPI; a full release publishes `latest`, the
  version, major.minor and major, and the stable version on PyPI.

## [0.0.1-beta.1] - 2026-06-23

First pre-release of the rewrite. The legacy bot (kept read-only in `legacy/`) was replaced by a new
async code base for Matrix 2.0 with MAS and Authentik as its upstream identity provider (ADR-0001 to
ADR-0009). Operators coming from the legacy bot: read
[Upgrading from the legacy bot](docs/deployment.md#upgrading-from-the-legacy-bot) first.

### Added
- **Reconciler:** Authentik groups become rooms (optionally inside a space), group membership becomes
  room membership and power levels, idempotently on a timer.
- **Onboarding:** new users get a welcome room with the configured messages, triggered by the
  reconciler and by the sync stream (sliding sync).
- **Offboarding lifecycle:** disabled Authentik users are locked out and later erased after
  configurable cooldowns, through the MAS admin API when `mas_admin` is set; `dry_run` is on by default.
- **Matrix 2.0:** MAS compatibility tokens, authenticated media, CS API version negotiation.
- **`onbot healthcheck`**, a multi-stage Docker image with a healthcheck, and the generated config
  reference (`docs/CONFIG_REFERENCE.md`, `config.example.yml`).
- **Tests:** unit and contract tests, and an integration suite against a live Synapse, MAS, Postgres
  and Authentik stack.

### Removed
- The libolm crypto stack: the bot works outside encrypted rooms (ADR-0009), so `storage_dir` and
  `storage_encryption_key` are gone. In this release unknown keys below the top level were ignored;
  since the review (S13) every unknown key is rejected, so remove both settings from old configs.

[Unreleased]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.11...HEAD
[0.0.11]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.10...0.0.11
[0.0.10]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.9...0.0.10
[0.0.9]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.8...0.0.9
[0.0.8]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.7...0.0.8
[0.0.7]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.6...0.0.7
[0.0.6]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.5...0.0.6
[0.0.5]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.4...0.0.5
[0.0.4]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.3...0.0.4
[0.0.3]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.2...0.0.3
[0.0.2]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.1...0.0.2
[0.0.1]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.1-beta.3...0.0.1
[0.0.1-beta.3]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.1-beta.2...0.0.1-beta.3
[0.0.1-beta.2]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/compare/0.0.1-beta.1...0.0.1-beta.2
[0.0.1-beta.1]: https://github.com/DZD-eV-Diabetes-Research/matrix-synapse-authentik-onbaording-bot/releases/tag/0.0.1-beta.1
