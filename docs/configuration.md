# Configuration

Onbot is configured with a single YAML file, validated by a
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) model in
[`onbot/config.py`](../onbot/config.py). Every setting can also be supplied, or overridden, with an
environment variable.

Start from the [Minimal config](../README.md#minimal-config) in the README, then reach here when you
need the full set of options.

## Where config comes from

Onbot reads its config from the path in `ONBOT_CONFIG_FILE_PATH` (the Docker image defaults this to
`/config/config.yml`). Individual `ONBOT_*` environment variables override anything in that file.

The env-var scheme is:

- Prefix every name with `ONBOT_`.
- Nest into blocks with a double underscore `__`.

For example, `synapse_server.bot_access_token` becomes
`ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN`. This lets you keep the non-secret shape in `config.yml`
and inject secrets from your orchestrator's secret store, or skip the file entirely and set
everything from the environment.

> Never commit a real config. `config*.yml` is gitignored (only `config.example.yml` is tracked).

## Bot credentials (pick one)

Onbot authenticates to Synapse as a bot user. Under MAS you choose exactly one of these:

| Option | Field | When to use |
|---|---|---|
| Compatibility token | `synapse_server.bot_access_token` | Near-term. Issue one with `mas-cli manage issue-compatibility-token` and provide the bare token. |
| OAuth2 client-credentials | `synapse_server.oauth2` | Forward-looking. The bot is a confidential MAS client and refreshes its own tokens. |

The same identity drives both the Synapse Admin API and the Client-Server API.

## The MXID localpart contract

This is the single most important thing to get right.

Onbot computes each user's MXID (`@<localpart>:server_name`) from an Authentik attribute named by
`sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute`. That value **must
match the localpart template MAS uses** when it provisions accounts from the same Authentik claim.

If the two disagree, Onbot's computed MXIDs will not correspond to real accounts, so no membership
changes take effect and nobody is added to rooms. The integration suite has a dedicated
localpart-contract test guarding this.

## Offboarding and the lifecycle

Onbot runs a quarantined offboarding lifecycle. Two things to know:

- **`mas_admin` is required to actually offboard.** When Authentik disables a user, MAS blocks new
  logins but existing Matrix sessions keep working. The Synapse admin API cannot revoke a MAS-issued
  session, only MAS can. Configure the `mas_admin` block so Onbot can. Omit it only on non-MAS
  deployments, where offboarding against live sessions is a no-op.
- **The lifecycle defaults to `dry_run: true`.** Nothing destructive happens until you opt in. Until
  then it only logs intended actions to the `onbot.lifecycle.audit` channel.

## The full reference

Two artifacts are generated directly from the config model and kept in sync by CI:

- [docs/CONFIG_REFERENCE.md](CONFIG_REFERENCE.md) is every field, its type, default, description, and
  `ONBOT_*` env-var name.
- [config.example.yml](../config.example.yml) is a commented, fillable YAML template covering room
  mapping rules, power levels, welcome messages, lifecycle defaults, ignore lists, and everything
  else. Copy it to `config.yml` and fill in the required values.

Regenerate both after editing [`onbot/config.py`](../onbot/config.py):

```bash
pdm run gen-config-docs      # rewrite docs/CONFIG_REFERENCE.md + config.example.yml
pdm run check-config-docs    # fail if they drift from the model (runs in CI)
```
