# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Versions are not yet published. The first tagged release is **blocked on the Phase 1 security
> hand-off** (rotate leaked credentials + scrub git history — see `BATTLE_PLAN.md` §5 and the README).

## [Unreleased]

### Added
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
- **Markdown welcome messages:** every entry of `welcome_new_users_messages` is rendered from
  CommonMark to the `org.matrix.custom.html` `formatted_body` a client displays, so links, emphasis,
  lists, headings and bare URLs arrive formatted. The markdown source stays in `body` as the fallback,
  raw HTML in a message is not passed through, and an unformatted message is still sent as plain text.
- **Placeholders in welcome messages:** `{space_link}` (a `matrix.to` link to the parent space),
  `{space_alias}`, `{space_name}` and `{user_id}` are filled in per recipient. A message using a
  `space_*` placeholder while `create_matrix_rooms_in_a_matrix_space` is disabled is held back with a
  warning instead of being sent with a gap in it. Substitution does not affect the once-per-user
  bookkeeping, which still keys on the configured text.
- **`onbot broadcast "<message>"`:** sends one `m.notice` into every user's onboarding room, fanned
  out from the bot's `m.direct` account data with bounded concurrency. Exits non-zero if any room
  could not be reached, naming them. The bot's Synapse send rate limit is lifted at startup
  (best-effort) so a large fan-out is not throttled.
- **Admin control room (G4.6, [ADR-0010](docs/adr/0010-admin-control-room.md)):** an opt-in Matrix
  room (`admin_room.enabled`) where allowlisted administrators command the bot with `!announce`,
  `!status` and `!help`. Unencrypted and unfederated by construction; authorisation is an explicit
  MXID allowlist (`admin_room.admin_user_ids`) checked on every command, never the sender's room
  power level. Messages without the `!` prefix are ignored, so admins can talk in the room. The
  command handler carries replay protection — an origin-timestamp floor plus a persisted ring buffer
  of handled event ids — so a bot restart cannot re-announce to every user.

- **Packaging (Phase 8):** multi-stage `Dockerfile` (digest-pinned base, non-root user, runtime-only
  deps, no crypto stack) with a `HEALTHCHECK` that runs `onbot healthcheck`; `.dockerignore`.
- **`onbot healthcheck` command:** probes the Matrix CS API, the Synapse admin API, the Authentik
  API, and (when configured) the MAS admin API with the real credentials, exiting non-zero on any
  unreachable or unauthorized dependency — suitable for container/orchestrator health probes.
- **Generated config docs:** `docs/CONFIG_REFERENCE.md` (markdown reference) and `config.example.yml`
  (annotated YAML template) are generated from the pydantic-settings model with
  [psyplus](https://pypi.org/project/psyplus/) via `pdm run gen-config-docs`, with a drift guard
  (`pdm run check-config-docs`) wired into CI.
- **Release pipeline:** `.github/workflows/release.yml` runs on published GitHub Releases and forks on
  the pre-release flag: a pre-release publishes the DockerHub image (`dzdde/onbot`) as `beta` + version and uploads the
  (PEP 440 pre-release) version to PyPI; a full release publishes `latest` + version/major.minor/major
  and the stable version to PyPI (OCI labels, build provenance + SBOM; PyPI via `PYPI_API_TOKEN`).
  This `CHANGELOG.md`.

### Changed
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
- **The sliding-sync loop moved out of the onboarding listener** into `onbot/sync.py` as `SyncPump`,
  which owns the stream position and fans each slice out to registered handlers. Onboarding and the
  admin control room now share one sync connection instead of opening two. No behaviour change.
- **Config metadata overhaul (`onbot/config.py`):** every field now carries a meaningful description
  and example so the generated reference/template are self-documenting; container fields annotated;
  typing tightened.
- **README** rewritten for the MAS era: the Authentik-as-upstream-to-MAS topology, the MXID/localpart
  contract, the bot-credential options (compatibility token vs OAuth2 client-credentials), the
  `mas_admin` requirement for lifecycle enforcement, Docker/compose deploy, the config reference, and
  troubleshooting.

### Fixed
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
- **Group room topics were rewritten on every reconcile pass.** The pass compared the desired topic
  against `MatrixRoom.topic`, which the Synapse admin *List Room* API never populates (it returns the
  room's name and canonical alias, but no topic), so the comparison always found a difference. The
  current topic is now read from `m.room.topic` room state, the way the admin control room already
  repaired its own, and the topic field is gone from `MatrixRoom` so nothing can compare against it
  again. Adding `chatroom_topic` to a group whose room already exists still applies on the next pass;
  what stops is the redundant state write per room per tick.

### Removed
- **`synapse_server.oauth2` (breaking for configs that set it, review S11, RUN-11):** MAS does not grant
  the Matrix API or Synapse admin scopes to `client_credentials`, so the option never worked. A config
  that still sets it stops at startup with exit code 2 and says so. Use
  `synapse_server.bot_access_token` with a MAS compatibility token (`scripts/provision-bot-account.sh`),
  which is now required. `mas_admin` is unchanged.
- Dropped the vestigial `storage_dir` and `storage_encryption_key` config fields — they backed the
  libolm key store removed in ADR-0009 and were unused. **Breaking for existing config files:** the
  model forbids unknown keys, so remove these two settings if present.

### Security
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
