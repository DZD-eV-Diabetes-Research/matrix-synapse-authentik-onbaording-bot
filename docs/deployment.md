# Deployment

Onbot ships as the [`dzdde/onbot`](https://hub.docker.com/r/dzdde/onbot) image on Docker Hub. It
runs as a non-root user and carries only runtime dependencies, no crypto stack, because the bot
operates outside encrypted rooms (see [ADR-0009](adr/0009-e2ee-stance.md)).

## Run with Docker

Mount your config and run:

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:latest
```

The image sets `ONBOT_CONFIG_FILE_PATH=/config/config.yml` and defaults to the `run` command, so
the two together start the long-lived service. A built-in `HEALTHCHECK` calls `onbot healthcheck`.

## docker-compose

```yaml
services:
  onbot:
    image: dzdde/onbot:latest
    restart: unless-stopped
    volumes:
      - ./config.yml:/config/config.yml:ro
```

## Config without a file on disk

Every setting can come from the environment instead (prefix `ONBOT_`, nest with `__`). This keeps
secrets off disk:

```yaml
services:
  onbot:
    image: dzdde/onbot:latest
    restart: unless-stopped
    environment:
      ONBOT_SYNAPSE_SERVER__SERVER_NAME: company.org
      ONBOT_SYNAPSE_SERVER__SERVER_URL: https://internal.matrix
      ONBOT_SYNAPSE_SERVER__BOT_USER_ID: "@welcome-bot:company.org"
      ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN: ${ONBOT_BOT_TOKEN}
      ONBOT_AUTHENTIK_SERVER__URL: https://authentik.company.org/
      ONBOT_AUTHENTIK_SERVER__API_KEY: ${ONBOT_AUTHENTIK_API_KEY}
```

See [docs/configuration.md](configuration.md) for the full env-var scheme.

## CLI commands

The image runs `onbot run` by default. You can override the command to run any of these:

```
onbot run               # long-running service: reconcile loop + event-driven onboarding (default)
onbot reconcile-once    # one idempotent reconcile pass, then exit
onbot broadcast "..."   # send one notice to every user's onboarding room; exit 1 if a room failed
onbot generate-config   # print a minimal config template (config.example.yml is the rich one)
onbot healthcheck       # probe Synapse/Authentik/MAS with the real credentials; exit 0 healthy, 1 not
```

`broadcast` reaches every user the bot has onboarded, so treat it with the caution that deserves. It
performs no permission check of its own: whoever can run it can already read the bot's access token
from the config, and the bot's onboarding rooms are read-only notice boards that only it may post to.

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:latest broadcast "Maintenance window tonight at 22:00 UTC"
```

For example, a one-shot reconcile:

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:latest reconcile-once
```

## Healthcheck

`onbot healthcheck` is what the container's `HEALTHCHECK` runs. It issues one authenticated request
to each dependency and exits non-zero if any is unreachable or rejects the credentials:

- the Matrix Client-Server API (`/whoami`),
- the Synapse admin API,
- the Authentik API,
- the MAS admin API, when `mas_admin` is configured.

Each dependency logs its own line, distinguishing unreachable from auth-rejected. The `matrix-cs`
line also flags a token or `bot_user_id` mismatch.
