# Onbot

Onbot keeps a **Matrix (Synapse)** homeserver in sync with an
**[Authentik](https://goauthentik.io/)** identity provider and gives every new user a friendly
welcome into the right rooms.

Authentik is the source of truth. Onbot mirrors it into Matrix: each Authentik group becomes a room,
group membership becomes room membership, and roles become power levels. When a new user shows up,
they get a guided 1:1 welcome message.

Onbot is built for **Matrix 2.0**. It assumes a
[**Matrix Authentication Service (MAS)**](https://element-hq.github.io/matrix-authentication-service/)
deployment with Authentik as the upstream identity provider.

## What Onbot does and does not do

Onbot **does not create accounts**. MAS provisions a Matrix account the first time a user logs in
through Authentik. Onbot's job is projection: turn Authentik groups into rooms, group membership
into room membership, attributes into power levels, and drive the offboarding lifecycle when a user
is disabled.

## Quick start with Docker

The published image is [`dzdde/onbot`](https://hub.docker.com/r/dzdde/onbot) on Docker Hub. It runs
as a non-root user and needs one thing from you: a config file.

1. Create a `config.yml` (see [Minimal config](#minimal-config) below).

2. Run it:

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:latest
```

The image defaults to reading `/config/config.yml` and running the long-lived `onbot run` service.
It also ships a built-in `HEALTHCHECK`.

### docker-compose

```yaml
services:
  onbot:
    image: dzdde/onbot:latest
    restart: unless-stopped
    volumes:
      - ./config.yml:/config/config.yml:ro
```

More deployment detail (env-only config, CLI commands, healthcheck) lives in
[docs/deployment.md](docs/deployment.md).

## Minimal config

Configuration is a single YAML file. Copy this, fill in the values, save it as `config.yml`:

```yaml
synapse_server:
  server_name: company.org                  # your Matrix domain (the part after the ':')
  server_url: https://internal.matrix       # how the bot reaches Synapse (an internal URL is fine)
  bot_user_id: "@welcome-bot:company.org"
  bot_access_token: syt_REPLACE_ME          # or an `oauth2:` block instead

authentik_server:
  url: https://authentik.company.org/
  api_key: REPLACE_ME                        # an Authentik API token

# Required to enforce offboarding under MAS (omit on non-MAS deployments):
mas_admin:
  url: https://auth.company.org              # the MAS base URL
  client_id: REPLACE_ME                      # a MAS admin client (in policy.data.admin_clients)
  client_secret: REPLACE_ME

sync_authentik_users_with_matrix_rooms:
  authentik_username_mapping_attribute: username   # MUST agree with MAS's localpart template
```

Two settings above are easy to get wrong and worth calling out:

- **`authentik_username_mapping_attribute` must match the localpart template MAS uses.** Onbot
  computes each user's MXID from this Authentik attribute. If it disagrees with MAS, the computed
  MXIDs will not match the real accounts and nobody gets added to rooms.
- **`mas_admin` is required to actually offboard disabled users.** The Synapse admin API cannot
  revoke a MAS-issued session, only MAS can. Without this block, offboarding silently does nothing
  to live sessions.

Every setting can also be supplied via an environment variable (prefix `ONBOT_`, nest with `__`),
for example `ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN=syt_…`.

> Never commit a real config. `config*.yml` is gitignored (only `config.example.yml` is tracked) and
> the image carries no secrets. Provide config at runtime.

For the full picture (bot credential options, the MAS auth topology, and every field), see the docs
below.

## Documentation

- [docs/features.md](docs/features.md) explains, in plain language, what each Onbot feature does —
  for both users and admins.
- [docs/configuration.md](docs/configuration.md) walks through every config block, the bot
  credential choices, and how to generate the reference.
- [docs/CONFIG_REFERENCE.md](docs/CONFIG_REFERENCE.md) lists every field, its type, default,
  description, and `ONBOT_*` env-var name (generated from the model).
- [docs/deployment.md](docs/deployment.md) covers running with Docker and compose, env-only config,
  the CLI commands, and the healthcheck.
- [docs/architecture.md](docs/architecture.md) explains the Matrix client to MAS to Authentik auth
  topology and links the architecture decision records.
- [docs/development.md](docs/development.md) is the setup, build, and release guide for contributors.
- [docs/testing.md](docs/testing.md) describes the unit, contract, and integration suites.
- [docs/troubleshooting.md](docs/troubleshooting.md) is a symptom to cause table for common issues.
- [docs/project/GOALS.md](docs/project/GOALS.md) captures project intent and [docs/project/BATTLE_PLAN.md](docs/project/BATTLE_PLAN.md) the build plan.

## License

MIT, see [LICENSE](LICENSE).
