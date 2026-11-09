# Configuration

Onbot is configured with a single YAML file, validated by a
[pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) model in
[`onbot/config.py`](../onbot/config.py). Every setting can also be supplied, or overridden, with an
environment variable.

Start from the [Minimal config](../README.md#minimal-config) in the README, then reach here when you
need the full set of options.

> Looking for what to put on an **Authentik group** so it becomes a room (topic, icon, power level,
> lobby)? That is the other half of the setup and lives in
> [authentik-groups.md](authentik-groups.md).

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

## Bot credentials

Onbot authenticates to Synapse as a bot user with `synapse_server.bot_access_token`: under MAS a
compatibility token, issued with `mas-cli manage issue-compatibility-token` (or
`scripts/provision-bot-account.sh`, which does that for you). Provide the bare token. The same identity
drives both the Synapse Admin API and the Client-Server API.

`synapse_server.oauth2` (the bot as a confidential MAS client that mints its own tokens) was removed. It
never worked: tested against MAS 0.14 and Synapse with MSC3861, MAS refuses the Matrix API scope
(`urn:matrix:org.matrix.msc2967.client:api:*`) and the Synapse admin scope for the
`client_credentials` grant (`invalid_scope`), even for a client listed in `policy.data.admin_clients`,
and Synapse rejects a client token without those scopes (`401 M_UNKNOWN_TOKEN`). A config that still
sets it stops at startup (exit code 2) with a message saying so. `mas_admin` keeps using
`client_credentials`; MAS grants its admin scope to that grant.

## Validation: what Onbot refuses at startup

The config is checked completely before Onbot talks to any server. A config that does not pass stops
the bot with exit code `2` (a runtime failure exits with `1`) and one line per problem on stderr:

```text
onbot: invalid configuration (2 errors):
  mas_admin.client_id: Field required
  sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.dry_rn: Extra inputs are not permitted
```

Each line names the field by its YAML path and says what is expected. The value you gave is never
printed, because it may be a credential and container logs usually end up in a log aggregator. The
same applies to a config file that is not valid YAML (only the line and column are named) and to an
`ONBOT_*` variable that cannot be parsed.

What is checked, beyond types and required fields:

- **Unknown keys are errors at every level**, so a typo such as `dry_rn: false` fails instead of
  leaving the default in place. This includes keys inside `per_authentik_group_pk_matrix_room_settings`
  and nested `ONBOT_*__*` variables. Remove keys an older Onbot version used.
- **A bot credential**: `synapse_server.bot_access_token` is required and must not be empty. The
  removed `synapse_server.oauth2` block is refused with its own message.
- **URLs** (`server_url`, `mas_admin.url`, `authentik_server.url` and the avatar
  URLs) need an `http://` or `https://` scheme and a host.
- **Matrix IDs** (`bot_user_id`, `admin_room.admin_user_ids`, `matrix_user_ignore_list`) must be full
  IDs such as `@alice:company.org`. A bare username in `matrix_user_ignore_list` would protect nobody.
  `bot_user_id` must be on `server_name`.
- **Alias fields** (`create_matrix_rooms_in_a_matrix_space.alias`, `admin_room.alias`, `alias_prefix`,
  `visitor_lobby_alias_suffix`) take the localpart only: `myspace`, not `#myspace:company.org`.
- **Ranges**: `server_tick_rate_sec` must be greater than 0, `authentik_poll_rate_sec` and the
  offboarding grace periods must not be negative.
- **Settings that cannot take effect**: `delete_disabled_rooms` without
  `disable_rooms_when_mapped_authentik_group_disappears`, `place_onboarding_rooms_in_space` without the
  parent space, and a visitor lobby without the parent space.
- **An admin room nobody may command**: `admin_room.enabled` is `true` by default, and an enabled
  admin room needs at least one entry in `admin_room.admin_user_ids` or
  `admin_room.authentik_group_pks_granting_bot_admin`. Set `admin_room.enabled: false` to run without
  the room; reports then only go to the log, and the destructive-action guard's pauses can only be
  lifted by raising its limits.
- **`log_level`** accepts `DEBUG`, `INFO`, `WARNING`, `ERROR` and `CRITICAL` (any case), as does
  the `--log-level` flag.

## Credentials stay out of logs

The bot access token, the MAS admin client secret and the Authentik API key are held as secret
values: printing or logging the config shows `**********` in their place, and so does a JSON dump of
it. Supplying them through `ONBOT_*` variables instead of the file is still the recommended pattern.

## Avatar downloads

The bot's avatar (`synapse_server.bot_avatar_url`), the space avatar and every group room's avatar
(the `chatroom_avatar_url` group attribute) are downloaded by the bot and re-uploaded to the
homeserver. Group room avatars are chosen by whoever may edit a group in Authentik, so every one of
these downloads is restricted (the `avatar_fetch` block):

- Only public internet addresses. Private, loopback, link-local, multicast, carrier-grade NAT and other
  special-purpose addresses are refused, also when a public URL redirects there. The host is resolved
  once per connection and the bot connects to exactly the address it checked, so a DNS answer that
  changes in between does not get around the check.
- At most five redirects, each checked like the original URL.
- Only `image/*` responses, at most `avatar_fetch.max_size_bytes` (default 5 MiB), and the whole
  download including redirects within `avatar_fetch.timeout_sec` (default 30 seconds).
- `HTTP_PROXY` and the other proxy variables are not used for these downloads: behind a proxy the bot
  could not see where a request really goes.

To use an image on an internal server, list the host or its network:

```yaml
avatar_fetch:
  allowed_internal_destinations:
    - assets.internal.company.org   # a hostname, matched exactly, whatever it resolves to
    - 10.20.0.0/16                  # an IP address or CIDR network
```

A refused avatar is logged with the reason. A group room keeps its previous avatar and is retried on
the next reconcile; a refused bot avatar is not retried until the next start.

Each avatar URL is downloaded once per start, and Onbot records a hash of the image next to every
avatar it sets (the bot's profile, rooms, the space, notice boards). A new picture at the same URL is
therefore picked up on the next restart, everywhere it is worn. While the URL cannot be fetched, the
current pictures stay as they are.

## The MXID localpart contract

This is the single most important thing to get right.

Onbot computes each user's MXID (`@<localpart>:server_name`) from an Authentik attribute named by
`sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute`. That value **must
match the localpart template MAS uses** when it provisions accounts from the same Authentik claim.

If the two disagree, Onbot's computed MXIDs will not correspond to real accounts, so no membership
changes take effect and nobody is added to rooms. The integration suite has a dedicated
localpart-contract test guarding this.

### Renamed users keep their account

MAS fixes a user's localpart at the first login and links the account to the upstream subject
(Authentik's `sub` claim). A later rename in Authentik (marriage, a name change) does not change the
Matrix account, so Onbot does not follow the mapping attribute blindly. It resolves every Authentik
user in this order:

1. **MAS**, when `mas_admin` is configured: the upstream link of the user's subject and the MAS user
   it belongs to. This is authoritative. `mas_admin.upstream_subject_attribute` says where Authentik's
   `sub` is found on the user object; it must match the subject mode of the Authentik OAuth2 provider
   MAS uses:

   | Authentik provider "Subject mode" | `upstream_subject_attribute` | Survives a rename |
   |---|---|---|
   | Based on the User's hashed ID (default) | `uid` (default) | yes |
   | Based on the User's ID | `pk` | yes |
   | Based on the User's username | `username` | no |
   | Based on the User's Email | `email` | no |

   With several upstream providers in MAS, set `mas_admin.upstream_provider_id` to the Authentik one.
2. **The recorded binding.** The first time Onbot sees a user active, in the sync scope and with an
   existing account, it records `Authentik pk -> MXID` in its account data (the `known_users` record
   the offboarding lifecycle also uses). After that the binding wins over the attribute.
3. **The mapping attribute**, only for users never seen before.

What this means in practice:

- A renamed user keeps their account, rooms and power levels. Nothing is kicked, locked, re-invited
  or welcomed again. The rename is reported once in the admin room.
- Changing `authentik_username_mapping_attribute` on a running deployment moves nobody who was
  already seen; it only affects users who appear later. The changed mappings are reported.
- When several Authentik users resolve to the same Matrix account (for example a new user whose
  `attributes.matrix_name` names somebody else's account):
  - if MAS links the account to exactly one of them and the others only name it by the mapping
    attribute (never seen, no MAS link), the MAS-linked owner is managed as usual and the others are
    skipped;
  - the same holds for the one user Onbot recorded a binding for, when MAS cannot say otherwise:
    `mas_admin` is not configured, or MAS links that account to nobody. The bound owner (also when
    disabled or outside the sync scope) keeps the account, and the users who only name it by the
    mapping attribute are skipped;
  - otherwise (two MAS links, two recorded bindings, a MAS link against another user's binding, or
    a binding against an attribute guess while MAS cannot be read or links the account to somebody
    else) Onbot skips all of them: no join, no kick, no power-level change, no lock and no erase of
    that account.

  Either way the conflict is reported in the admin room. There is no bot command for it; resolve it
  by hand (see below).
- A user whose mapping attribute names an account MAS links to somebody else is skipped and
  reported, even when that owner is disabled or outside the sync scope and Onbot never saw them.
- If MAS cannot be read, Onbot resolves users with a recorded binding from it and reports the
  outage. Users it has never seen are skipped until MAS answers again: they are not joined or bound,
  and the account their mapping attribute names is left alone for that pass (no join, kick,
  power-level change, lock or erase). Recording an unchecked guess as a binding would later count
  against the account's real, MAS-linked owner. (A user whose account does not exist yet is not
  affected.) If the binding record itself cannot be read, Onbot leaves memberships and power levels
  alone and offboards nobody until it is repaired.

#### Resolving a conflict or a wrong binding by hand

Usually the fix belongs in Authentik: correct the mapping attribute of the user who names the wrong
account (a duplicate `matrix_name`, a typo). The next pass picks it up.

When a recorded binding itself is wrong, edit it in the bot's account data. The record is the
account-data event `<reversed server name>.onbot.known_users` of the bot user (for example
`org.company.onbot.known_users`), shaped `{"schema_version": ..., "users": {"<Authentik pk>":
{"mxid": "@alice:company.org", "first_seen": <unix time>}}}`. Read it and write it back with the
bot's access token:

```bash
curl -H "Authorization: Bearer $BOT_TOKEN" \
  "https://matrix.company.org/_matrix/client/v3/user/@onbot:company.org/account_data/org.company.onbot.known_users"
# edit the JSON, then
curl -X PUT -H "Authorization: Bearer $BOT_TOKEN" -d @known_users.json \
  "https://matrix.company.org/_matrix/client/v3/user/@onbot:company.org/account_data/org.company.onbot.known_users"
```

Remove or correct the entry of the wrong pk; keep the JSON valid, since an unreadable record pauses
user sync and offboarding. With `mas_admin` configured, MAS overrides a binding that disagrees with
its upstream link anyway.

## Offboarding and the lifecycle

Onbot runs a quarantined offboarding lifecycle. Two things to know:

- **`mas_admin` is required to actually offboard.** When Authentik disables a user, MAS blocks new
  logins but existing Matrix sessions keep working. The Synapse admin API cannot end a MAS-issued
  session, only MAS can. Configure the `mas_admin` block so Onbot can. Omit it only on non-MAS
  deployments: on a MAS-backed homeserver without it Onbot refuses to lock or erase anybody (the
  Synapse admin API would leave every MAS session valid), logs that at ERROR and reports it once in
  the admin room. Onboarding and rooms keep working. Ending sessions
  on lock and erasing without GDPR erasure need a MAS whose admin API offers them; the startup log
  lists what the configured MAS can do (see [features.md](features.md#offboarding)).
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
