# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Versions are not yet published. The first tagged release is **blocked on the Phase 1 security
> hand-off** (rotate leaked credentials + scrub git history — see `BATTLE_PLAN.md` §5 and the README).

## [Unreleased]

### Added
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
- **`onbot healthcheck` checks the bot's own liveness (review S10):** the reconcile loop and the sync
  pump write a heartbeat file (`ONBOT_HEARTBEAT_FILE`, default `/tmp/onbot-heartbeat.json`, or
  `--heartbeat-file`); the default healthcheck reads it and needs no config, network or token. The
  previous upstream probe is `onbot healthcheck --deep`. A long Authentik outage now marks the
  container unhealthy.
- **The image runs `tini` as PID 1** and the `HEALTHCHECK` uses a 10 s timeout and a 60 s start period.
- **`reconcile-once` and `broadcast` skip startup side effects** they do not need (bot avatar, admin
  room). `onbot run` exits 1 when one of its loops crashes.
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
- Dropped the vestigial `storage_dir` and `storage_encryption_key` config fields — they backed the
  libolm key store removed in ADR-0009 and were unused. **Breaking for existing config files:** the
  model forbids unknown keys, so remove these two settings if present.

### Security
- ⚠️ **Unresolved release blocker:** the Phase 1 maintainer security items (rotate leaked
  credentials, scrub git history) remain open. No image/release should be published until they are
  done.
