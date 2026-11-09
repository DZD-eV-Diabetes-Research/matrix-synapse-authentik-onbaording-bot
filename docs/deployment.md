# Deployment

Onbot ships as the [`dzdde/onbot`](https://hub.docker.com/r/dzdde/onbot) image on Docker Hub. It
runs as a non-root user and carries only runtime dependencies, no crypto stack, because the bot
operates outside encrypted rooms (see [ADR-0009](adr/0009-e2ee-stance.md)).

## Run with Docker

Mount your config and run:

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:X.Y.Z
```

**Pin a version.** Replace `X.Y.Z` with the release you have read the [changelog](../CHANGELOG.md)
for (the tags are listed on [Docker Hub](https://hub.docker.com/r/dzdde/onbot/tags)), or pin the
image digest. Avoid `latest`: the bot kicks users, locks accounts and deletes rooms, and with `latest`
a plain restart can bring a release whose destructive behaviour changed without anyone reading its
notes.

The image sets `ONBOT_CONFIG_FILE_PATH=/config/config.yml` and defaults to the `run` command, so
the two together start the long-lived service. A built-in `HEALTHCHECK` calls `onbot healthcheck`,
which checks the service's own heartbeat (see [Healthcheck](#healthcheck)). `tini` runs as PID 1 and
forwards signals, so `docker stop` ends the bot cleanly (see [Stopping](#stopping)).

**The config file must be readable by uid 10001,** the user the image runs as. It holds the bot's
access token and API keys, so do not make it world-readable (`chmod 644`) to get there. Give it to
that uid instead:

```bash
sudo chown 10001 config.yml && chmod 600 config.yml
```

Or keep the secrets out of the file altogether, see [Config without a file on disk](#config-without-a-file-on-disk).

## docker-compose

```yaml
services:
  onbot:
    image: dzdde/onbot:X.Y.Z
    restart: unless-stopped
    volumes:
      - ./config.yml:/config/config.yml:ro
    # Hardening: onbot writes nothing but its heartbeat file (in /tmp) and needs no capabilities.
    read_only: true
    tmpfs: [/tmp]
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

The hardening lines are optional but supported as they are. With `read_only: true` the heartbeat file
needs a writable place: the `tmpfs` above, or point `ONBOT_HEARTBEAT_FILE` at a writable volume.

## Config without a file on disk

Every setting can come from the environment instead (prefix `ONBOT_`, nest with `__`). This keeps
secrets off disk:

```yaml
services:
  onbot:
    image: dzdde/onbot:X.Y.Z
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
onbot reconcile-once --dry-run  # log what one pass would change, change nothing
onbot broadcast "..."   # send one notice to every mapped user's onboarding room; exit 1 if a room failed or nobody is mapped
onbot generate-config   # print a minimal config template (config.example.yml is the rich one)
onbot healthcheck       # is the running service alive? reads its heartbeat file; exit 0 healthy, 1 not
onbot healthcheck --deep  # probe Synapse/Authentik/MAS with the real credentials; exit 0 healthy, 1 not
```

The one-shot commands do only what they need. `reconcile-once` does the same startup work as the
service, once (register the bot's device, lift its send rate limit, set its avatar, provision the admin
room and invite new admins), then runs one pass, so a cron-only deployment stays complete. It does not
read the sync stream, so commands in the admin room are only answered by a running service. That
includes `!confirm <id>` for removals the [guard](features.md#guard-against-mass-removals) paused: a
cron-only deployment can report a paused batch but needs the service (or higher limits) to release it.

`reconcile-once --dry-run` is the preview: it reads Authentik and Matrix, writes every change the pass
would make to the log with a `[dry-run]` prefix (rooms it would create, members it would add or kick,
power levels, offboarding steps, what the guard would pause), and changes nothing. It skips every
startup step except version negotiation, saves no bookkeeping and posts nothing. It is safe to run
while the service runs.
`broadcast` registers the device, lifts the bot's send rate limit and sends; it sets no avatar and does
not touch the admin room.

Do not run `reconcile-once` against a homeserver while `onbot run` serves it. The two processes do not
coordinate, and a user whom both welcome at the same moment can end up with two notice boards. Stop
the service first; a running service reconciles by itself every `server_tick_rate_sec`, and at once
when Authentik changes.

`broadcast` reaches every user the bot has onboarded, so treat it with the caution that deserves. It
performs no permission check of its own: whoever can run it can already read the bot's access token
from the config, and the bot's onboarding rooms are read-only notice boards that only it may post to.

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:X.Y.Z broadcast "Maintenance window tonight at 22:00 UTC"
```

For example, a one-shot reconcile:

```bash
docker run --rm \
  -v "$PWD/config.yml:/config/config.yml:ro" \
  dzdde/onbot:X.Y.Z reconcile-once
```

## Healthcheck

`onbot healthcheck` is what the container's `HEALTHCHECK` runs. It judges the **running bot**, not its
dependencies: the service writes a small JSON heartbeat file, and the check reads it. It loads no
config, makes no network call and mints no token, so running it every minute costs nothing.

The service updates the file after every reconcile pass and every sync response. The check fails when:

| Part | Unhealthy when | Default |
|---|---|---|
| `reconcile` | no pass has succeeded for 3 × `server_tick_rate_sec` + 300 s (counted from process start until the first success) | 20 minutes at the default 300 s tick |
| `sync` | no sliding-sync response for 300 s. A healthy pump answers at least every 30 s (the long-poll), and its error backoff is capped at 60 s. Not checked on a homeserver without sliding sync. | 5 minutes |
| process | the process that wrote the file is gone | |
| file | missing or unreadable | |

The service writes both limits into the file itself, so the check always agrees with the running
config. It also lists startup steps that are still being retried (for example while Synapse is down);
those are informational and do not fail the check.

The file is `/tmp/onbot-heartbeat.json` by default. Set `ONBOT_HEARTBEAT_FILE` (for both `onbot run`
and the check, so in the container environment) or pass `--heartbeat-file` to move it, for example on
a read-only root filesystem. Only `onbot run` writes it; a one-shot command never touches it.

A reconcile pass fails while Authentik or Synapse is unreachable, so an outage longer than the
reconcile limit turns the container unhealthy, and healthy again after the next successful pass.
Plain Docker and Compose only report the state; they do not restart an unhealthy container. If a loop
of the service crashes outright, the process exits with code 1, which `restart: unless-stopped` does
act on.

### Checking the dependencies

`onbot healthcheck --deep` is the operator's connectivity check. It issues one authenticated request to
each dependency and exits non-zero if any is unreachable or rejects the credentials:

- the Matrix Client-Server API (`/whoami`),
- the Synapse admin API,
- the Authentik API,
- the MAS admin API, when `mas_admin` is configured.

Each dependency logs its own line, distinguishing unreachable from auth-rejected. The `matrix-cs`
line also flags a token or `bot_user_id` mismatch. With `mas_admin` every run mints a fresh
MAS token, which is why this is not the container healthcheck any more.

```bash
docker exec onbot onbot healthcheck --deep
```

## Upgrade notes

Read the [changelog](../CHANGELOG.md) before every upgrade; entries marked "Check before upgrading"
need a look at your configuration. The ones that stop the bot or cost a first start noticeably more:

- **Offboarding delays renamed (after 0.0.11).** In
  `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix`,
  `deactivate_after_n_sec` is now `lock_after_n_sec` and `delete_after_n_sec` is now
  `remove_after_n_sec` (the first stage locks the account, the second removes it for good). The old
  names are no longer read: a config or `ONBOT_...` environment variable that still uses one stops the
  bot at startup with exit code 2 and a message naming the new key. Rename the keys in the same
  deployment as the image update.
- **The first start after upgrading from 0.0.11 or older does some work once.** Every onbot marker
  written by an older version is rewritten once (logged at INFO as "the onbot marker was
  incomplete; the bot restored it"): older markers miss fields or still carry the Authentik URL
  (SEC-11). And each notice board from before the avatar's image hash was recorded has its current
  avatar downloaded once and compared with the bot avatar, so a board that already wears the picture
  only gets the record and no new upload. Expect a longer first pass and a burst of media downloads
  on a large server; later passes are back to reads only.

## Upgrading from the legacy bot

The rewrite (0.0.1 and later) does not take over the per-user rooms of the legacy bot (the code in
`legacy/`). The legacy bot found a user's welcome room by scanning for its own state event and never
recorded it in `m.direct`, and it counted sent messages by their position in the list. The new bot
looks a user's room up in `m.direct` only and records messages by content. So on its first pass after
the cutover it:

- **opens a fresh notice board for every user** (read-only, unencrypted, the user force-joined) and
- **sends the whole welcome sequence again,** to everybody at once.

The old welcome rooms stay as they were: encrypted, and writable by their user. Onbot does not touch
them; a server admin can delete them once the new notice boards are in place.

Before the cutover:

1. **Stop the legacy bot for good.** Two bots acting on the same server fight over rooms and members.
2. **Decide what everybody should receive again.** If the full sequence would be noise for existing
   users, shorten `welcome_new_users_messages` for the first run to what still matters (or one line
   pointing at the new room), and restore the full list for new users afterwards. Give the messages
   [ids](features.md#message-ids) so later edits do not re-send them.
3. **Translate the config.** The settings changed; start from `config.example.yml` and
   [configuration.md](configuration.md), not from the legacy file. Unknown keys stop the bot at
   startup with a list of the offending fields.
4. **Run a preview first:** `onbot reconcile-once --dry-run` logs every room, membership and
   offboarding change the first pass would make, without changing anything.

Group rooms are a separate question: whether the new bot adopts an existing room depends on who
created it and on its marker, see "Upgrading from a version before room identity" in
[features.md](features.md#group-rooms).

## Startup

Onbot starts its loops at once and does the startup work against the homeserver in the background:
CS-API version negotiation, registering the bot's device (needed for message sends under MAS), lifting
the bot's send rate limit, setting the bot avatar and provisioning the admin room. When Synapse or MAS
is still booting (a host reboot that starts everything together), each step is retried with a growing
delay, up to 60 s apart, until it succeeds; no restart is needed. A step the server refuses for good
(a 4xx such as 403) is logged as an error and not retried.

Welcomes wait for the device registration and then start right away: the bot runs an extra reconcile
pass at that moment, so nobody waits a full tick.

## Stopping

On SIGTERM (what `docker stop` sends) or SIGINT the bot stops its loops: the sync long-poll and the
Authentik poll are abandoned at once, and work in progress (a reconcile pass, a welcome, a startup
step) gets 6 seconds to finish before it is cancelled. A second signal cancels at once. `onbot run`
then exits with code 0; a one-shot command that had to be cancelled exits with `128 + signal` (143
for SIGTERM).

That fits Docker's default stop timeout of 10 s. Do not set `stop_grace_period` (Compose) or
`--stop-timeout` below 8 s. Measured with the image against the integration stack:

| Moment of `docker stop` | Time to exit | Exit code |
|---|---|---|
| Steady state, 17 to 52 s after start (5 runs) | 0.35 to 0.59 s | 0 |
| During startup, Synapse down, 4 s after start | 1.6 s | 0 |
| While the interpreter is still loading (0 to 1 s after start, 6 runs) | 0.2 to 0.3 s | 143 |

The image runs `tini` as PID 1. The kernel drops a signal sent to a PID 1 that has no handler for it,
and the bot can only install its handlers once Python has imported it (up to about a second), so
without an init the earliest stops would wait for the SIGKILL. If you override the entrypoint, keep
`tini` (or use `docker run --init` / Compose `init: true`).
