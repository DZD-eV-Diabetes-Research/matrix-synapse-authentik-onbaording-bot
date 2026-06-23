# Onbot

A bot that keeps a **Matrix (Synapse)** homeserver continuously in sync with an
**[Authentik](https://goauthentik.io/)** identity provider and onboards every new user into the
right rooms with a friendly welcome. Authentik is the source of truth; Matrix mirrors it
(group → room, group membership → room membership, power levels), and new users get guided in with a
1:1 welcome DM.

Onbot targets **Matrix 2.0**: it assumes a [**Matrix Authentication Service
(MAS)**](https://element-hq.github.io/matrix-authentication-service/) deployment with Authentik as
an *upstream identity provider*, uses authenticated media, and drives the Client-Server and admin
APIs over a single async HTTP client (no `matrix-nio`).

See [`GOALS.md`](GOALS.md) for intent, [`BATTLE_PLAN.md`](BATTLE_PLAN.md) for the build plan, and
[`docs/adr/`](docs/adr/) for the architecture decisions.

> ⚠️ **Release blocker (maintainer):** the Phase 1 security items are **not done** — leaked
> credentials in git history still need rotating and the history scrubbing
> ([`BATTLE_PLAN.md`](BATTLE_PLAN.md) §5 Phase 1). **Do not publish an image or tag a release until
> those are complete.** The packaging below is ready; the security hand-off is the gate.

---

## How it works — the MAS auth topology

The auth chain is **Matrix client → MAS → Authentik** (ADR-[0006](docs/adr/0006-auth-topology-mas-authentik.md)):

```
                   logs in via                      upstream IdP
   Matrix client ───────────────▶  MAS  ◀───────────────────────  Authentik
        ▲                           │  (provisions Matrix accounts   (source of truth:
        │                           │   on first login)               users & groups)
        │ welcome DM,               │
        │ room membership      ┌────┴─────┐
        └──────────────────────│  Onbot   │── reads users/groups ──▶ Authentik API
                               └────┬─────┘
                                    └── Synapse Admin API + CS API ──▶ Synapse  ◀─ MAS
```

Consequences that shape how you configure Onbot:

- **Onbot does not create accounts.** MAS auto-provisions a Matrix account the first time a user
  logs in through Authentik. Onbot's job is **projection**: turn Authentik groups into rooms, group
  membership into room membership, and group/role attributes into power levels — plus the
  quarantined offboarding lifecycle.
- **The MXID localpart contract is critical.** Onbot computes a user's MXID
  (`@<localpart>:server_name`) from an Authentik attribute, and it **must match the localpart
  template MAS uses** when it provisions accounts from the same Authentik claim. Set
  [`sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute`](docs/CONFIG_REFERENCE.md)
  to agree with MAS — get it wrong and Onbot's computed MXIDs won't match the real accounts, so
  nobody gets added to rooms. (Verified by the integration suite's localpart-contract test.)
- **Lifecycle enforcement requires MAS.** When Authentik disables a user, MAS blocks *new* logins
  but **existing Matrix sessions keep working**, and the *Synapse* admin API cannot revoke a
  MAS-issued session — only MAS can (ADR-[0005](docs/adr/0005-quarantine-lifecycle.md), §7 Q1, proven
  empirically). So to actually offboard a disabled user you must configure the
  [`mas_admin`](docs/CONFIG_REFERENCE.md) block. Without it, offboarding is a **no-op against live
  sessions** (it silently fails to revoke).

---

## Configuration

Configuration is a single YAML file validated by a [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/)
model ([`onbot/config.py`](onbot/config.py)). Every setting can also be supplied (or overridden) via
an environment variable: prefix `ONBOT_`, nest with `__`. E.g.
`ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN=syt_…`.

- **Full reference:** [`docs/CONFIG_REFERENCE.md`](docs/CONFIG_REFERENCE.md) — every field, its type,
  default, description and `ONBOT_*` env-var name.
- **Annotated template:** [`config.example.yml`](config.example.yml) — a commented, fillable YAML
  template. Copy it to `config.yml` and fill in the required values.

Both are **generated from the model** (with [psyplus](https://pypi.org/project/psyplus/)) and kept in
sync by CI — regenerate after editing `onbot/config.py`:

```bash
pdm run gen-config-docs      # rewrite docs/CONFIG_REFERENCE.md + config.example.yml
pdm run check-config-docs    # fail if they drift from the model (runs in CI)
```

> 🔐 **Never commit a real config.** `config*.yml` is gitignored (only `config.example.yml` is
> tracked) and the Docker image carries no secrets — provide config at runtime.

### Bot credentials (pick one)

Onbot authenticates to Synapse as a bot user. Under MAS, choose one of:

| Option | Field | When |
|---|---|---|
| **Compatibility token** | `synapse_server.bot_access_token` | Near-term. Issue with `mas-cli manage issue-compatibility-token`. Provide the bare token. |
| **OAuth2 client-credentials** | `synapse_server.oauth2` | Forward-looking. The bot is a confidential MAS client and refreshes tokens automatically. |

Provide **exactly one**. The same identity drives both the Synapse Admin API and the Client-Server
API.

### Minimal `config.yml`

```yaml
synapse_server:
  server_name: company.org                  # your Matrix domain (the part after the ':')
  server_url: https://internal.matrix       # how the bot reaches Synapse (internal URL is fine)
  bot_user_id: "@welcome-bot:company.org"
  bot_access_token: syt_REPLACE_ME          # or use an `oauth2:` block instead

authentik_server:
  url: https://authentik.company.org/
  api_key: REPLACE_ME                        # Authentik API token

# Required to enforce offboarding under MAS (omit on non-MAS deployments):
mas_admin:
  url: https://auth.company.org              # the MAS base URL
  client_id: REPLACE_ME                      # a MAS admin client (in policy.data.admin_clients)
  client_secret: REPLACE_ME

sync_authentik_users_with_matrix_rooms:
  authentik_username_mapping_attribute: username   # MUST agree with MAS's localpart template
```

See [`config.example.yml`](config.example.yml) for everything else (room mapping rules, power
levels, welcome messages, the dry-run lifecycle defaults, ignore lists, …).

---

## Run with Docker

The published image runs as a non-root user and ships only runtime dependencies (no crypto stack —
the bot operates outside encrypted rooms, ADR-[0009](docs/adr/0009-e2ee-stance.md)).

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  ghcr.io/dzd-ev-diabetes-research/matrix-synapse-authentik-onbaording-bot:latest
```

The image defaults to `ONBOT_CONFIG_FILE_PATH=/config/config.yml` and the `run` command. It has a
built-in `HEALTHCHECK` that calls `onbot healthcheck` (see below).

### docker-compose

```yaml
services:
  onbot:
    image: ghcr.io/dzd-ev-diabetes-research/matrix-synapse-authentik-onbaording-bot:latest
    restart: unless-stopped
    volumes:
      - ./config.yml:/config/config.yml:ro
    # Or skip the file and supply everything via env (no secrets on disk):
    # environment:
    #   ONBOT_SYNAPSE_SERVER__SERVER_NAME: company.org
    #   ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN: ${ONBOT_BOT_TOKEN}
```

### CLI commands

```
onbot run               # long-running service: reconcile loop + event-driven onboarding (default)
onbot reconcile-once    # one idempotent reconcile pass, then exit
onbot generate-config   # print a minimal config template (use config.example.yml for the rich one)
onbot healthcheck       # probe Synapse/Authentik/MAS with the real credentials; exit 0 healthy, 1 not
```

`onbot healthcheck` is what the container's `HEALTHCHECK` runs: it issues one authenticated request
to the Matrix CS API (`/whoami`), the Synapse admin API, the Authentik API, and — when `mas_admin`
is configured — the MAS admin API, and exits non-zero if any is unreachable or rejects the
credentials.

---

## Develop

Requires **Python 3.14** and **[PDM](https://pdm-project.org/)**.

```bash
pdm install                 # create the venv and install deps (incl. dev + docs)
pdm run pre-commit install  # enable lint + secret-scan hooks
```

### Run & test

```bash
pdm run onbot --help
pdm run onbot reconcile-once

pdm run pytest -m "not integration"     # fast unit + contract suite
pdm run pytest                          # full suite incl. live Synapse+MAS+Authentik (needs Docker)
pdm run ruff check .                     # lint
pdm run ruff format --check .            # formatting
pdm run mypy onbot                       # type check
```

The `run_*.sh` helper scripts at the repo root wrap the same PDM commands.

### Build the image locally

```bash
docker build -t onbot:dev .
docker run --rm onbot:dev --help
```

---

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Users never get added to rooms | The MXID localpart contract is broken — `authentik_username_mapping_attribute` doesn't match MAS's localpart template, so computed MXIDs don't exist. |
| Disabled users keep their Matrix access | `mas_admin` is not configured (the Synapse admin API can't revoke a MAS session), or lifecycle `dry_run` is still `true` (the default). |
| Nothing destructive ever happens | Expected by default — the lifecycle is quarantined (`dry_run: true`); it only logs to the `onbot.lifecycle.audit` channel until you opt in. |
| Welcome DM send fails with a 500 | The bot device isn't registered yet — Onbot registers it on startup (`ensure_device_registered`); check startup logs. |
| `healthcheck` reports a dependency FAIL | Read the per-dependency log line; it distinguishes unreachable from auth-rejected. The `matrix-cs` line also flags a token/`bot_user_id` mismatch. |
| Sliding sync unavailable | The homeserver doesn't advertise MSC4186; Onbot falls back to the reconciler signal path automatically. |

---

## Releasing

Versioned images are published to GHCR by the [release workflow](.github/workflows/release.yml) when
a `v*` tag is pushed. See [`CHANGELOG.md`](CHANGELOG.md) and the workflow's header comment for the
tag/version flow. **(Blocked on the Phase 1 security hand-off — see the note at the top.)**

## License

MIT — see [`LICENSE`](LICENSE).
