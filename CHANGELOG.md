# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> Versions are not yet published. The first tagged release is **blocked on the Phase 1 security
> hand-off** (rotate leaked credentials + scrub git history — see `BATTLE_PLAN.md` §5 and the README).

## [Unreleased]

### Added
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

### Removed
- Dropped the vestigial `storage_dir` and `storage_encryption_key` config fields — they backed the
  libolm key store removed in ADR-0009 and were unused. **Breaking for existing config files:** the
  model forbids unknown keys, so remove these two settings if present.

### Security
- ⚠️ **Unresolved release blocker:** the Phase 1 maintainer security items (rotate leaked
  credentials, scrub git history) remain open. No image/release should be published until they are
  done.
