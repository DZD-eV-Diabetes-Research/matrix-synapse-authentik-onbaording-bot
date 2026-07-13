<!-- GENERATED FILE — do not edit by hand.
     Regenerate with `./gen_config_docs.sh` after changing onbot/config.py. -->

# Configuration Reference — `OnbotConfig`

This document is auto-generated from the pydantic-settings model. All settings can be provided via the YAML config file or overridden with environment variables.

---

## `log_level`

*Logging verbosity*

How much the bot logs. `DEBUG` is noisy — it includes every API call it makes — but
it is the fastest way to see why a user or group is not being picked up while wiring
the bot up. The `--log-level` command-line flag overrides this.

| Property | Value |
|---|---|
| Type | Enum |
| Required | No |
| Default | `"INFO"` |
| Allowed values | `INFO` · `DEBUG` |
| Environment variable | `ONBOT_LOG_LEVEL` |

---

## `server_tick_rate_sec`

*Reconcile interval (seconds)*

How often, in seconds, the bot re-converges Authentik state onto Matrix. A
reconcile reads the whole Matrix side — every managed room's members, power levels
and state — so it is the expensive operation, and this is the interval that costs
Synapse traffic.

It does not set how quickly new users are onboarded. A reconcile also runs on demand
whenever `authentik_poll_rate_sec` notices Authentik has changed, so this is a safety
net that repairs drift somebody caused *inside* Matrix (a manually kicked member, an
edited power level). Minutes, not seconds, is the right order of magnitude.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `300` |
| Environment variable | `ONBOT_SERVER_TICK_RATE_SEC` |

**Examples:**

*Example 1:*

```yaml
server_tick_rate_sec: 300
```

*Example 2:*

```yaml
server_tick_rate_sec: 900
```

---

## `authentik_poll_rate_sec`

*Authentik poll interval (seconds)*

How often, in seconds, the bot asks Authentik whether anything changed — a new
user, a group membership, a renamed room group. When something did, it runs a
reconcile immediately; when nothing did, it does nothing at all.

This is what sets onboarding latency, and it is cheap: two Authentik requests per
poll and zero against Synapse. Set it to `0` to disable the poll entirely, in which
case new users wait for the next `server_tick_rate_sec` reconcile.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `15` |
| Environment variable | `ONBOT_AUTHENTIK_POLL_RATE_SEC` |

**Examples:**

*Example 1:*

```yaml
authentik_poll_rate_sec: 15
```

*Example 2:*

```yaml
authentik_poll_rate_sec: 60
```

*Example 3:*

```yaml
authentik_poll_rate_sec: 0
```

---

## `synapse_server`

*Synapse server*

Connection data and credentials for the Matrix client-server and Synapse admin
APIs. Authenticate with either `bot_access_token` or `oauth2`, not both.

| Property | Value |
|---|---|
| Type | Object (SynapseServer) |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER` |

---

### `synapse_server.server_name`

*Matrix server name*

Synapse's public facing domain — the part after the colon in a Matrix ID such as
`@alice:company.org`. This is not necessarily the domain under which the Synapse
server is reachable; that is `server_url`. See
https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__SERVER_NAME` |

**Examples:**

```yaml
server_name: company.org
```

---

### `synapse_server.server_url`

*Synapse base URL*

URL the bot uses to reach the Synapse server. This can (and should) be an internal
URL, so the Synapse admin API need not be exposed publicly. A public URL works too.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__SERVER_URL` |

**Examples:**

*Example 1:*

```yaml
server_url: https://internal.matrix
```

*Example 2:*

```yaml
server_url: https://matrix.company.org
```

---

### `synapse_server.bot_user_id`

*Bot Matrix ID*

Full Matrix user ID of an existing account; the bot acts as this user. The account
must already exist — the bot does not register itself — and its localpart must be on
`server_name`.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__BOT_USER_ID` |

**Examples:**

```yaml
bot_user_id: '@welcome-bot:company.org'
```

---

### `synapse_server.bot_access_token`

*Bot access token*

Access token authorising the bot against the Synapse APIs. On a homeserver fronted
by the Matrix Authentication Service this is a compatibility token, issued with
`mas-cli manage issue-compatibility-token`. Provide the bare token; do not prefix it
with `Bearer` (the client adds that). Leave unset (`null`) when using `oauth2`
instead. Prefer supplying it through the
`ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN` environment variable rather than committing
it to the config file.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN` |

**Examples:**

```yaml
bot_access_token: syt_ONLY_AN_EXAMPLE_TOKEN_sadaw4
```

---

### `synapse_server.oauth2`

*OAuth2 authentication (alternative to the access token)*

Authenticate as an OAuth2 client of the Matrix Authentication Service instead of
carrying a static token. When set, this is used in place of `bot_access_token` and
the short-lived tokens it mints refresh automatically. Provide exactly one of
`bot_access_token` or `oauth2`.

| Property | Value |
|---|---|
| Type | Object (MatrixOAuth2) |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2` |

---

#### `synapse_server.oauth2` — `MatrixOAuth2` schema

---

#### `synapse_server.oauth2.token_endpoint`

*MAS token endpoint*

The `token_endpoint` advertised by the Matrix Authentication Service, as listed in
its OpenID discovery document at `/.well-known/openid-configuration`.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2__TOKEN_ENDPOINT` |

**Examples:**

```yaml
token_endpoint: https://auth.company.org/oauth2/token
```

---

#### `synapse_server.oauth2.client_id`

*OAuth2 client id*

Client id of the OAuth2 client registered for the bot in the Matrix
Authentication Service. The client must be allowed to use the `client_credentials`
grant.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_ID` |

**Examples:**

```yaml
client_id: 01HXQ3B9ZK7Y2QW8N4V6M0EXAMPLE
```

---

#### `synapse_server.oauth2.client_secret`

*OAuth2 client secret*

Client secret belonging to `client_id`. Provide the bare secret — the bot builds
the HTTP authorization header itself. Treat this as a credential: prefer supplying it
through the `ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_SECRET` environment variable rather
than committing it to the config file.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_SECRET` |

**Examples:**

```yaml
client_secret: ONLY_AN_EXAMPLE_SECRET_pMv1kZ8sQ0
```

---

#### `synapse_server.oauth2.scope`

*Requested OAuth2 scopes*

Space-separated scopes to request with the token. The bot needs the Matrix
client-server API scope, plus the Synapse admin scope for the room and account
management it performs. `null` requests the client's default scopes.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2__SCOPE` |

**Examples:**

```yaml
scope: urn:matrix:org.matrix.msc2967.client:api:* urn:synapse:admin:*
```

---

### `synapse_server.bot_avatar_url`

*Bot avatar*

HTTP(S) URL to a picture the bot sets as its own avatar on start. The image is
downloaded and re-uploaded to the homeserver's media repository, and only re-uploaded
when the URL changes. `null` leaves the bot's current avatar untouched.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNAPSE_SERVER__BOT_AVATAR_URL` |

**Examples:**

```yaml
bot_avatar_url: https://sillyimages.com/face.png
```

---

### `synapse_server.admin_api_path`

*Synapse admin API sub-path*

Sub-path the Synapse admin API is served under, relative to `server_url`. Only
needs changing if a reverse proxy remounts the admin API. Keep the default if
unsure.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"_synapse/admin/"` |
| Environment variable | `ONBOT_SYNAPSE_SERVER__ADMIN_API_PATH` |

**Examples:**

```yaml
admin_api_path: _synapse/admin/
```

---

### `synapse_server.room_version`

*Room version for rooms the bot creates*

Room version passed to `POST /createRoom` for every room and space the bot
creates. Leave unset (`null`) — the default — so new rooms inherit the homeserver's
own default room version, which the Matrix spec says SHOULD now be `12`. Room
version 12 changes two things the bot depends on: the room creator (the bot) holds an
infinite power level and is deliberately absent from `m.room.power_levels`, and room
IDs are a hash with no `:domain` component. The bot is written for that world, so
pinning a number here is only for two cases: an operator whose Synapse is too old to
default to a version this bot needs (it requires at least version 8 for the features
it uses; `restricted` join rules need 8 and `knock_restricted` needs 10), or a test
that must force a specific version. Do not pin a number to freeze the bot behind the
ecosystem — prefer upgrading Synapse.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNAPSE_SERVER__ROOM_VERSION` |

**Examples:**

*Example 1:*

```yaml
room_version: '12'
```

*Example 2:*

```yaml
room_version: '11'
```

---

## `authentik_server`

*Authentik server*

Connection data and API token for the upstream Authentik identity provider, which
the bot treats as the single source of truth for users and groups.

| Property | Value |
|---|---|
| Type | Object (AuthentikServer) |
| Required | **Yes** |
| Environment variable | `ONBOT_AUTHENTIK_SERVER` |

---

### `authentik_server.url`

*Authentik base URL*

URL the bot uses to reach your Authentik server. May be an internal URL.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_AUTHENTIK_SERVER__URL` |

**Examples:**

```yaml
url: https://authentik.company.org/
```

---

### `authentik_server.api_key`

*Authentik API token*

API token for your Authentik server. Generate one under
`https://<authentik>/if/admin/#/core/tokens`. The token only ever needs to *read*
users and groups. Provide the bare token; the client adds the `Bearer` prefix. Prefer
supplying it through the `ONBOT_AUTHENTIK_SERVER__API_KEY` environment variable
rather than committing it to the config file.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_AUTHENTIK_SERVER__API_KEY` |

**Examples:**

```yaml
api_key: yEl4tFqeIBQwoHAd9hajmkm2PBjSAirY_THIS_IS_JUST_AN_EXAMPLE_i57e
```

---

## `mas_admin`

*Matrix Authentication Service admin API*

Admin credentials for the Matrix Authentication Service. Required for the
offboarding module to actually revoke sessions and deactivate accounts on a
MAS-fronted homeserver: there the Matrix session is owned by MAS, and the Synapse
admin API cannot terminate it. Leave unset (`null`) on homeservers that do not use
MAS — the bot then enforces offboarding through the Synapse admin API alone.

| Property | Value |
|---|---|
| Type | Object (MasAdmin) |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_MAS_ADMIN` |

---

### `mas_admin.url`

*MAS base URL*

Base URL of the Matrix Authentication Service, without a trailing path. As with
`synapse_server.server_url` this may be an internal URL.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_MAS_ADMIN__URL` |

**Examples:**

```yaml
url: https://auth.company.org
```

---

### `mas_admin.client_id`

*MAS admin client id*

Client id of an OAuth2 client that may request the `urn:mas:admin` scope. The
client must use the `client_credentials` grant and be listed in the MAS
`policy.data.admin_clients` allowlist, otherwise MAS refuses the token.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_MAS_ADMIN__CLIENT_ID` |

**Examples:**

```yaml
client_id: 01HXQ3B9ZK7Y2QW8N4V6M0EXAMPLE
```

---

### `mas_admin.client_secret`

*MAS admin client secret*

Client secret belonging to `client_id`. This credential can lock and deactivate
any account on the homeserver — prefer supplying it through the
`ONBOT_MAS_ADMIN__CLIENT_SECRET` environment variable rather than committing it to
the config file.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_MAS_ADMIN__CLIENT_SECRET` |

**Examples:**

```yaml
client_secret: ONLY_AN_EXAMPLE_SECRET_pMv1kZ8sQ0
```

---

## `welcome_new_users_messages`

*Welcome messages*

Messages the bot sends, in order, in the 1:1 welcome direct room it opens with each
newly onboarded user. Each message is sent at most once per user — they are matched by
content, so editing a message here re-sends that one message to everyone. `null` or an
empty list disables the welcome direct room entirely.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_WELCOME_NEW_USERS_MESSAGES` |

**Examples:**

```yaml
welcome_new_users_messages:
- Welcome aboard! I will invite you to the rooms for your groups.
```

---

## `onboarding_room_name`

*Welcome room name*

Display name of the 1:1 welcome room the bot opens with each user. The room needs a
name because the bot joins the user directly instead of waiting for them to accept an
invitation, and a Matrix client only tags a room as a direct message when its user
accepted such an invitation — without a name the room would appear in their room list
as an untitled room. Only read when a room is created; renaming later does not rewrite
existing rooms.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Announcements"` |
| Environment variable | `ONBOT_ONBOARDING_ROOM_NAME` |

**Examples:**

*Example 1:*

```yaml
onboarding_room_name: Announcements
```

*Example 2:*

```yaml
onboarding_room_name: Company Chat Bot
```

---

## `onboarding_room_topic`

*Welcome room topic*

Matrix topic (tagline) of the 1:1 welcome room. A good place to say where a user
should turn with questions, since they cannot ask them in this room — it is read-only.
Only read when a room is created.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Notices from the onboarding bot. This room is read-only \u2014 you cannot write here."` |
| Environment variable | `ONBOT_ONBOARDING_ROOM_TOPIC` |

**Examples:**

```yaml
onboarding_room_topic: Notices from the onboarding bot. You cannot write here.
```

---

## `force_join_onboarding_room`

*Force users into the welcome room*

Join users into their welcome room directly, through the Synapse admin API, instead
of leaving them an invitation they have to accept. On by default: the room is a notice
board the bot posts to, so an unaccepted invitation means a user who never receives
the welcome messages. The join happens exactly once, when the room is created — a user
who then leaves the room is not dragged back in. Turn this off to send a plain
invitation instead; the bot also falls back to the invitation when the join fails.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_FORCE_JOIN_ONBOARDING_ROOM` |

---

## `admin_room`

*Admin control room*

A Matrix room in which listed administrators can command the bot — most notably
announcing a message to every user. Disabled by default.

| Property | Value |
|---|---|
| Type | Object (AdminRoom) |
| Required | No |
| Environment variable | `ONBOT_ADMIN_ROOM` |

---

### `admin_room.enabled`

*Enable the admin control room*

Create and listen in a control room where administrators can command the bot. Off
by default: the room lets anyone on the bot's admin allowlist send a message to every
user on the server, so it should be turned on deliberately. The `onbot broadcast`
command-line tool does the same job without a room.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_ADMIN_ROOM__ENABLED` |

---

### `admin_room.alias`

*Control room alias localpart*

Localpart of the control room's canonical alias, i.e. the `<alias>` in
`#<alias>:<server_name>`. This is how the bot finds the room again, so changing it
later makes the bot create a second, empty control room rather than rename the
first.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"onbot-admin"` |
| Environment variable | `ONBOT_ADMIN_ROOM__ALIAS` |

**Examples:**

*Example 1:*

```yaml
alias: onbot-admin
```

*Example 2:*

```yaml
alias: bot-control
```

---

### `admin_room.name`

*Control room name*

Display name of the control room. Only read when the room is created.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Onbot Admin"` |
| Environment variable | `ONBOT_ADMIN_ROOM__NAME` |

**Examples:**

```yaml
name: Onbot Admin
```

---

### `admin_room.topic`

*Control room topic*

Matrix topic (tagline) of the control room. The bot keeps this in sync with the
commands it supports, so a one-line reminder is visible without scrolling.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Onbot control room \u2014 say !help for the available commands."` |
| Environment variable | `ONBOT_ADMIN_ROOM__TOPIC` |

**Examples:**

```yaml
topic: Bot control room. !help for commands.
```

---

### `admin_room.admin_user_ids`

*Administrators allowed to command the bot*

Full Matrix IDs permitted to run commands in the control room, listed by hand.
These are added to whatever `authentik_group_pks_granting_bot_admin` resolves to, and
this list is the right home for accounts Authentik has never heard of — a break-glass
admin, another bot. It is also the floor the bot falls back on: it needs no Authentik
call, so these administrators keep their commands when Authentik is unreachable.
Everyone not in the union of the two lists is refused, even if they are somehow in the
room and even if they hold a high power level there, because a command like
`!announce` reaches every user on the server. The bot invites these users to the room
when it creates it, and re-checks on every reconcile, so somebody added here or to the
group later is invited without a restart.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_ADMIN_ROOM__ADMIN_USER_IDS` |

**Examples:**

```yaml
admin_user_ids:
- '@admin:company.org'
- '@ops-lead:company.org'
```

---

### `admin_room.authentik_group_pks_granting_bot_admin`

*Authentik groups whose members may command the bot*

Primary keys (`pk`) of Authentik groups whose members may run commands in the
control room, so the allowlist can be maintained where the rest of your access
control already lives. Members are mapped to Matrix IDs with the same
`authentik_username_mapping_attribute` the reconciler uses, and the result is unioned
with `admin_user_ids`. Inactive users, users on `authentik_user_ignore_list`, and
users the bot cannot map to a Matrix ID are never granted anything. Membership is
re-read periodically, so removing somebody from the group revokes their commands
without restarting the bot.

Authentik *superusers* are deliberately not admins of this bot, and there is no
option to make them so: people are made superusers to administer an identity
provider, not to page the whole company, and a capability that reaches every user on
the server must not widen silently because somebody was granted an unrelated role
upstream. Create a group, put people in it, and name it here. An empty list, together
with an empty `admin_user_ids`, means nobody may command the bot.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_ADMIN_ROOM__AUTHENTIK_GROUP_PKS_GRANTING_BOT_ADMIN` |

**Examples:**

```yaml
authentik_group_pks_granting_bot_admin:
- 1120a6e1124f309bbe96c8be5fb09eab
```

---

## `place_onboarding_rooms_in_space`

*Put welcome rooms in the space*

Gather the 1:1 onboarding/welcome rooms under the managed parent space as well. Off
by default — whether direct rooms belong in a space is a matter of taste. Only applies
when `create_matrix_rooms_in_a_matrix_space` is enabled.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_PLACE_ONBOARDING_ROOMS_IN_SPACE` |

---

## `sync_authentik_users_with_matrix_rooms`

*User synchronisation*

The core Authentik-to-Matrix sync: which users are considered, how they map onto
Matrix IDs, and what happens when they are disabled upstream.

| Property | Value |
|---|---|
| Type | Object (SyncAuthentikUsersWithMatrix) |
| Required | No |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS` |

---

### `sync_authentik_users_with_matrix_rooms.enabled`

*Enable user synchronisation*

Master switch for projecting Authentik group membership into Matrix rooms.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__ENABLED` |

---

### `sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute`

*Attribute holding the Matrix localpart*

Source of the localpart of the Matrix ID (`@<localpart>:server`). A dotted path
into the Authentik user object, e.g. `username` or `attributes.matrix_name`. On a
homeserver fronted by the Matrix Authentication Service this MUST agree with the
localpart template MAS derives from the upstream claim, or provisioned users will
never match the accounts they own.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"username"` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__AUTHENTIK_USERNAME_MAPPING_ATTRIBUTE` |

**Examples:**

*Example 1:*

```yaml
authentik_username_mapping_attribute: username
```

*Example 2:*

```yaml
authentik_username_mapping_attribute: attributes.matrix_name
```

---

### `sync_authentik_users_with_matrix_rooms.kick_matrix_room_members_not_in_mapped_authentik_group_anymore`

*Kick members who left the Authentik group*

When a user leaves an Authentik group, kick them from the corresponding Matrix
room so membership stays a faithful mirror. Disable to let the bot only ever *add*
members and never remove them — users then keep access to rooms after losing the
group that granted it.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__KICK_MATRIX_ROOM_MEMBERS_NOT_IN_MAPPED_AUTHENTIK_GROUP_ANYMORE` |

---

### `sync_authentik_users_with_matrix_rooms.sync_only_users_in_authentik_pathes`

*Filter: Authentik directory paths*

Only sync users that live under one of these Authentik directory paths. Paths are
matched exactly, so a parent path does not imply its children — list both if you want
both. `null` syncs users regardless of path.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__SYNC_ONLY_USERS_IN_AUTHENTIK_PATHES` |

**Examples:**

```yaml
sync_only_users_in_authentik_pathes:
- users
- users/staff
```

---

### `sync_authentik_users_with_matrix_rooms.sync_only_users_with_authentik_attributes`

*Filter: Authentik user attributes*

Only sync users carrying all of these custom Authentik attributes, compared by
exact value. A convenient way to let users opt into chat. `null` syncs every user.
See https://docs.goauthentik.io/docs/users-sources/user/user_ref#attributes

| Property | Value |
|---|---|
| Type | Dictionary of (str) |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__SYNC_ONLY_USERS_WITH_AUTHENTIK_ATTRIBUTES` |

**Examples:**

```yaml
sync_only_users_with_authentik_attributes:
  is_chat_user: true
```

---

### `sync_authentik_users_with_matrix_rooms.sync_only_users_of_groups_with_id`

*Filter: Authentik group membership*

Only sync users who belong to at least one of these Authentik groups, identified by
the group's primary key (`pk`). `null` applies no group filter. Note this filters
*which users exist* for the bot; it does not by itself decide which groups become
rooms — that is `sync_matrix_rooms_based_on_authentik_groups`.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__SYNC_ONLY_USERS_OF_GROUPS_WITH_ID` |

**Examples:**

```yaml
sync_only_users_of_groups_with_id:
- 1120a6e1124f309bbe96c8be5fb09eab
```

---

### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix`

*Offboarding: lock out users disabled upstream*

What happens to a Matrix account once its Authentik account is disabled or
deleted. Defaults to an audit-only dry run — see the `dry_run` field below.

| Property | Value |
|---|---|
| Type | Object (DeactivateDisabledAuthentikUsersInMatrix) |
| Required | No |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix` — `DeactivateDisabledAuthentikUsersInMatrix` schema

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.enabled`

*Enable offboarding*

Lock out Matrix accounts whose Authentik account was disabled or deleted. With
`dry_run` left at its default this only produces an audit trail; see `dry_run` before
turning this into a destructive action.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__ENABLED` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.dry_run`

*Dry run (audit only)*

Quarantine switch. While `true` the bot only records bookkeeping and logs what it
*would* do to the `onbot.lifecycle.audit` channel — no session is revoked and no
account is deactivated or deleted. Set `false` to actually perform destructive
lifecycle actions. Defaults to `true` so destructive offboarding is always opt-in;
run with the default first and read the audit log before switching it off.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__DRY_RUN` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.deactivate_after_n_sec`

*Grace period before deactivation*

Seconds a user must stay disabled in Authentik before their Matrix account is
deactivated. Absorbs accidental upstream disables: re-enabling the Authentik account
within this window cancels the pending deactivation. The default is 24 hours.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `86400` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__DEACTIVATE_AFTER_N_SEC` |

**Examples:**

*Example 1:*

```yaml
deactivate_after_n_sec: 86400
```

*Example 2:*

```yaml
deactivate_after_n_sec: 3600
```

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.delete_after_n_sec`

*Grace period before deletion*

Seconds a user must stay disabled in Authentik before their Matrix account is
erased, counted from the same point as `deactivate_after_n_sec` (so it should be the
larger of the two). `null` disables deletion entirely and leaves accounts
deactivated. The default is 365 days.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `31536000` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__DELETE_AFTER_N_SEC` |

**Examples:**

*Example 1:*

```yaml
delete_after_n_sec: 31536000
```

*Example 2:*

```yaml
delete_after_n_sec: null
```

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.include_user_media_on_delete`

*Delete user media on deletion*

Also delete media the user uploaded when their account is erased. Useful to honour
data-protection requests, but the media is unrecoverable and may still be referenced
by messages in rooms the user posted in.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__INCLUDE_USER_MEDIA_ON_DELETE` |

---

## `create_matrix_rooms_in_a_matrix_space`

*Parent space*

The designated parent space that collects the Authentik-group rooms.

| Property | Value |
|---|---|
| Type | Object (CreateMatrixRoomsInAMatrixSpace) |
| Required | No |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE` |

---

### `create_matrix_rooms_in_a_matrix_space.enabled`

*Group rooms into a space*

Gather all Authentik-group rooms under a dedicated parent space. Disable to leave
the rooms unparented at the top of the user's room list.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__ENABLED` |

---

### `create_matrix_rooms_in_a_matrix_space.alias`

*Space alias localpart*

Localpart of the space's canonical alias, i.e. the `<alias>` in
`#<alias>:<server_name>`. This is how the bot finds an existing space, so changing it
later makes the bot create a second, empty space rather than rename the first.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"OnBotSpace"` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__ALIAS` |

**Examples:**

*Example 1:*

```yaml
alias: myspace
```

*Example 2:*

```yaml
alias: companyspace
```

---

### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists`

*Space creation*

Whether and how the parent space is created when it does not exist yet.

| Property | Value |
|---|---|
| Type | Object (CreateMatrixSpaceIfNotExists) |
| Required | No |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists` — `CreateMatrixSpaceIfNotExists` schema

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled`

*Create the space if missing*

Create the parent space when no space with the configured alias exists. Disable if
you want to create and curate the space yourself; the bot then only adds rooms to
it.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__ENABLED` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name`

*Space display name*

Display name of the space, as shown in the client's room list.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"OnBotSpace"` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__NAME` |

**Examples:**

*Example 1:*

```yaml
name: Company Chat
```

*Example 2:*

```yaml
name: OnBotSpace
```

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic`

*Space topic*

Matrix topic (tagline) for the space.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Space for authentik group rooms"` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__TOPIC` |

**Examples:**

```yaml
topic: Space for authentik group rooms
```

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.avatar_url`

*Space avatar*

HTTP(S) URL to a picture used as the space avatar (icon). The image is downloaded
and re-uploaded to the homeserver's media repository on every reconcile, but only
when the URL changed — so this also updates an already existing space. `null` leaves
the space without an avatar.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__AVATAR_URL` |

**Examples:**

```yaml
avatar_url: https://sillyimages.com/space.png
```

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.space_params`

*Space creation parameters*

Extra parameters merged into the Matrix `POST /createRoom` call that creates the
space. Only read when the space is actually created; changing them later does not
rewrite an existing space. See the Client-Server API `POST /createRoom`.

| Property | Value |
|---|---|
| Type | Dictionary of (str) |
| Required | No |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__SPACE_PARAMS` |

**Examples:**

```yaml
space_params:
  preset: private_chat
  visibility: private
```

---

## `sync_matrix_rooms_based_on_authentik_groups`

*Room synchronisation*

The group-to-room projection: which Authentik groups become Matrix rooms, what
happens when a group disappears, and how power levels are derived.

| Property | Value |
|---|---|
| Type | Object (SyncMatrixRoomsBasedOnAuthentikGroups) |
| Required | No |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS` |

---

### `sync_matrix_rooms_based_on_authentik_groups.enabled`

*Enable room synchronisation*

Master switch for creating and maintaining one Matrix room per Authentik group.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__ENABLED` |

---

### `sync_matrix_rooms_based_on_authentik_groups.only_for_children_of_groups_with_uid`

*Filter: child groups of these parents*

Only mirror Authentik groups that are direct children of one of these parent
groups, identified by the parent's primary key (`pk`). Only the immediate children
match, not grandchildren. `null` considers all groups.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__ONLY_FOR_CHILDREN_OF_GROUPS_WITH_UID` |

**Examples:**

```yaml
only_for_children_of_groups_with_uid:
- a1b2c3d4parentgroupuid
```

---

### `sync_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes`

*Filter: Authentik group attributes*

Only mirror Authentik groups carrying all of these custom attributes, compared by
exact value. A convenient way to opt a group into chat. `null` turns every group into
a room. See https://goauthentik.io/docs/user-group/group#attributes

| Property | Value |
|---|---|
| Type | Dictionary of (str) |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__ONLY_GROUPS_WITH_ATTRIBUTES` |

**Examples:**

```yaml
only_groups_with_attributes:
  is_chatroom: true
```

---

### `sync_matrix_rooms_based_on_authentik_groups.room_avatar_url_attribute`

*Group attribute holding the room avatar URL*

Key inside an Authentik group's custom `attributes` holding an HTTP(S) URL used as
that group's room avatar (icon). The image is downloaded and re-uploaded to the
homeserver's media repository on every reconcile, but only when the URL changed.
`null` disables per-room avatars.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"chatroom_avatar_url"` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__ROOM_AVATAR_URL_ATTRIBUTE` |

**Examples:**

```yaml
room_avatar_url_attribute: chatroom_avatar_url
```

---

### `sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with`

*Filter: group name prefix*

Only mirror Authentik groups whose name starts with this prefix — a lightweight way
to opt specific groups into chat without custom attributes. The prefix stays part of
the group name and therefore of the room name unless you override the name template.
`null` disables the filter.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__ONLY_FOR_GROUPNAMES_STARTING_WITH` |

**Examples:**

*Example 1:*

```yaml
only_for_groupnames_starting_with: chat-
```

*Example 2:*

```yaml
only_for_groupnames_starting_with: matrix_
```

---

### `sync_matrix_rooms_based_on_authentik_groups.disable_rooms_when_mapped_authentik_group_disappears`

*Disable orphaned rooms*

If a mapped Authentik group disappears — deleted, or it no longer passes the
filters above — kick all members from the corresponding room and block it. Note that
loosening a filter therefore re-enables nothing: the room stays blocked.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__DISABLE_ROOMS_WHEN_MAPPED_AUTHENTIK_GROUP_DISAPPEARS` |

---

### `sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms`

*Delete disabled rooms*

When a room is disabled (see `disable_rooms_when_mapped_authentik_group_disappears`)
also delete it through the Synapse admin API, rather than only blocking it. This
destroys the room's history irreversibly — leave `false` unless you are sure.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__DELETE_DISABLED_ROOMS` |

---

### `sync_matrix_rooms_based_on_authentik_groups.make_authentik_superusers_matrix_room_admin`

*Authentik superusers become room admins*

Grant Authentik superusers the Matrix admin power level (100) in the rooms they are
members of. Takes precedence over `authentik_group_attr_for_matrix_power_level`.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__MAKE_AUTHENTIK_SUPERUSERS_MATRIX_ROOM_ADMIN` |

---

### `sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level`

*Group attribute holding the Matrix power level*

Authentik group attribute (dotted path) holding an integer from 0 to 100. Members
of that group receive the value as their Matrix power level in the rooms this bot
manages. When a user's groups disagree, the highest value wins. Superusers promoted
by `make_authentik_superusers_matrix_room_admin` ignore this attribute.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"chat-systemwide-powerlevel"` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__AUTHENTIK_GROUP_ATTR_FOR_MATRIX_POWER_LEVEL` |

**Examples:**

*Example 1:*

```yaml
authentik_group_attr_for_matrix_power_level: matrix-userpowerlevel
```

*Example 2:*

```yaml
authentik_group_attr_for_matrix_power_level: synapse-options.chat-powerlevel
```

---

## `matrix_room_default_settings`

*Default room settings*

The room identity template applied to every group room: how its alias, name, topic
and creation parameters are derived from the Authentik group.

| Property | Value |
|---|---|
| Type | Object (MatrixDynamicRoomSettings) |
| Required | No |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS` |

---

### `matrix_room_default_settings.alias_prefix`

*Room alias prefix*

Prefix prepended to the room's canonical alias localpart, useful to keep bot-managed
rooms in their own namespace. `null` for no prefix.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__ALIAS_PREFIX` |

**Examples:**

*Example 1:*

```yaml
alias_prefix: authentik-
```

*Example 2:*

```yaml
alias_prefix: grp-
```

---

### `matrix_room_default_settings.matrix_alias_from_authentik_attribute`

*Group attribute used as the room alias*

Authentik group attribute (dotted path) used as the room's alias localpart. The
default `pk` is the most stable choice, because it survives a group being renamed —
an alias derived from the group name would not, and a Matrix alias cannot be changed
once the room is created.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"pk"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__MATRIX_ALIAS_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

*Example 1:*

```yaml
matrix_alias_from_authentik_attribute: pk
```

*Example 2:*

```yaml
matrix_alias_from_authentik_attribute: attributes.chatroom_alias
```

---

### `matrix_room_default_settings.name_prefix`

*Room name prefix*

Prefix prepended to the room's display name. `null` for no prefix.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__NAME_PREFIX` |

**Examples:**

```yaml
name_prefix: '[Chat] '
```

---

### `matrix_room_default_settings.matrix_name_from_authentik_attribute`

*Group attribute used as the room name*

Authentik group attribute (dotted path) used as the room's display name.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"name"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__MATRIX_NAME_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

*Example 1:*

```yaml
matrix_name_from_authentik_attribute: name
```

*Example 2:*

```yaml
matrix_name_from_authentik_attribute: attributes.chatroom_name
```

---

### `matrix_room_default_settings.topic_prefix`

*Room topic prefix*

Prefix prepended to the room's topic. `null` for no prefix.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__TOPIC_PREFIX` |

**Examples:**

```yaml
topic_prefix: "Group chat \u2014 "
```

---

### `matrix_room_default_settings.matrix_topic_from_authentik_attribute`

*Group attribute used as the room topic*

Authentik group attribute (dotted path) used as the room's topic. `null` leaves the
topic unset.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"attributes.chatroom_topic"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__MATRIX_TOPIC_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

```yaml
matrix_topic_from_authentik_attribute: attributes.chatroom_topic
```

---

### `matrix_room_default_settings.end2end_encryption_enabled`

*Enable end-to-end encryption*

Enable end-to-end encryption in the group-mapped Matrix rooms. The bot itself stays
outside encryption: it writes room *state* (membership, power levels, name, topic),
which is never encrypted, but it does not read or post message content in encrypted
rooms. Welcome messages are sent in a separate, unencrypted direct room. Encryption
cannot be turned off again for a room once it is on.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__END2END_ENCRYPTION_ENABLED` |

---

### `matrix_room_default_settings.default_room_create_params`

*Room creation parameters*

Parameters merged into the Matrix `POST /createRoom` call for group rooms — preset,
visibility, federation, and so on. Only read when a room is actually created; changing
them later does not rewrite existing rooms. See the Client-Server API
`POST /createRoom`.

| Property | Value |
|---|---|
| Type | Dictionary of (str) |
| Required | No |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__DEFAULT_ROOM_CREATE_PARAMS` |

**Examples:**

```yaml
default_room_create_params:
  preset: private_chat
  visibility: private
```

---

### `matrix_room_default_settings.matrix_room_create_params_from_authentik_attribute`

*Group attribute holding extra creation parameters*

Authentik group attribute (dotted path) holding a mapping of extra `createRoom`
parameters, merged over `default_room_create_params` for that group. `null` disables
per-group overrides.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"attributes.chatroom_params"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__MATRIX_ROOM_CREATE_PARAMS_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

```yaml
matrix_room_create_params_from_authentik_attribute: attributes.chatroom_params
```

---

### `matrix_room_default_settings.keep_updating_matrix_attributes_from_authentik`

*Keep room name and topic in sync*

Re-apply the room's name and topic from Authentik on every reconcile, overwriting
any drift. Disable to let room admins rename rooms in their client and keep the
change.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__KEEP_UPDATING_MATRIX_ATTRIBUTES_FROM_AUTHENTIK` |

---

### `matrix_room_default_settings.visitor_lobby_enabled`

*Open a visitor lobby beside the group room*

Maintain a second, open room — a lobby — beside each private group room. Every
member of the parent space can find the lobby in the space listing and join it of
their own accord, and nobody is ever kicked from it, so it is a front door in front of
the closed group room. The group room itself is untouched: same members, same history,
same privacy. Off by default — a lobby is a deliberate decision, not a migration.

Requires the parent space (`create_matrix_rooms_in_a_matrix_space`), because a lobby is
joinable precisely by space members and to nobody else; enabling a lobby without a
space is rejected at startup. A single group can opt in without a config change through
`matrix_room_visitor_lobby_from_authentik_attribute`.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__VISITOR_LOBBY_ENABLED` |

---

### `matrix_room_default_settings.visitor_lobby_name_suffix`

*Lobby name suffix*

Appended to the group room's name to name its lobby — `Düsseldorf` becomes
`Düsseldorf (Lobby)`. The suffix is the whole user-facing explanation of what the
second room is, so it should say *this is a door*, not *these are more people*.
Alternatives worth a deliberate choice: ` (Foyer)` (reads naturally to a
German-speaking org), ` (Open)` (shortest, states the property not the metaphor), or
` & Guests` (if the room should feel like the group hosting). ` & Friends` was
rejected: the ampersand reads as a statement about membership.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `" (Lobby)"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__VISITOR_LOBBY_NAME_SUFFIX` |

**Examples:**

*Example 1:*

```yaml
visitor_lobby_name_suffix: ' (Lobby)'
```

*Example 2:*

```yaml
visitor_lobby_name_suffix: ' (Foyer)'
```

*Example 3:*

```yaml
visitor_lobby_name_suffix: ' (Open)'
```

---

### `matrix_room_default_settings.visitor_lobby_alias_suffix`

*Lobby alias suffix*

Appended to the group room's alias localpart to form the lobby's alias —
`#duesseldorf` gets a `#duesseldorf-lobby` beside it. Unlike the group alias, dashes
here are kept. A Matrix alias cannot be changed after the room is created, so changing
this later makes the bot build a second, empty lobby rather than rename the first.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"-lobby"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__VISITOR_LOBBY_ALIAS_SUFFIX` |

**Examples:**

*Example 1:*

```yaml
visitor_lobby_alias_suffix: -lobby
```

*Example 2:*

```yaml
visitor_lobby_alias_suffix: -foyer
```

*Example 3:*

```yaml
visitor_lobby_alias_suffix: -open
```

---

### `matrix_room_default_settings.visitor_lobby_topic_template`

*Lobby topic template*

Topic set on the lobby, with `{name}` replaced by the group room's name. State the
arrangement in the one place every visitor looks, so a visitor who reads it never
wonders why the room is quiet or which room to post in.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Open lobby for {name} \u2014 anyone in the space may join. The group's working room is private."` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__VISITOR_LOBBY_TOPIC_TEMPLATE` |

**Examples:**

```yaml
visitor_lobby_topic_template: "Open lobby for {name} \u2014 anyone in the space may\
  \ join. The group's working room is private."
```

---

### `matrix_room_default_settings.visitor_lobby_end2end_encryption_enabled`

*Encrypt the lobby*

Enable end-to-end encryption in the lobby. Off by default, and deliberately weaker
than the group room's own encryption default: a lobby is open to the whole space by
construction, so encryption buys it little, while it guarantees every visitor's first
impression is a screen of `unable to decrypt`. Encryption cannot be turned off again
once a room has it, so this default is chosen for you to be able to change it before
the lobby exists rather than after.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__VISITOR_LOBBY_END2END_ENCRYPTION_ENABLED` |

---

### `matrix_room_default_settings.visitor_lobby_inject_group_members`

*Seed the lobby with the group's members*

Join the group's members into the lobby as well, so a visitor who walks in finds
somebody there. On by default: an empty lobby is a dead lobby. This is a social bet —
it works when the group room is where the group *works* and the lobby is where the
group is *reachable*, and it fails, producing two half-dead rooms and doubled
notifications, when both are general-purpose. Turn it off for a large group that would
rather staff an empty lobby deliberately. Visitors are never injected anywhere — they
joined on purpose — and members are only ever added, never kicked.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__VISITOR_LOBBY_INJECT_GROUP_MEMBERS` |

---

### `matrix_room_default_settings.matrix_room_visitor_lobby_from_authentik_attribute`

*Group attribute that opts a group into a lobby*

Authentik group attribute (dotted path) holding a boolean that turns the lobby on or
off for that one group, overriding `visitor_lobby_enabled`. This lets whoever owns the
group in Authentik open a lobby without a config deploy. A value that is not a boolean
is ignored with a warning and the configured default applies. `null` disables the
per-group override, leaving `visitor_lobby_enabled` in sole charge.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"attributes.chatroom_visitor_lobby"` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__MATRIX_ROOM_VISITOR_LOBBY_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

```yaml
matrix_room_visitor_lobby_from_authentik_attribute: attributes.chatroom_visitor_lobby
```

---

## `per_authentik_group_pk_matrix_room_settings`

*Per-group room setting overrides*

Overrides for `matrix_room_default_settings`, keyed by the Authentik group's primary
key (`pk`). Only the keys you list are overridden; the rest fall back to the defaults.
Use this to give a single group a different name prefix, or to leave one room
unencrypted.

| Property | Value |
|---|---|
| Type | Dictionary of (str, Object (MatrixDynamicRoomSettings)) |
| Required | No |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS` |

**Examples:**

```yaml
per_authentik_group_pk_matrix_room_settings:
  1120a6e1124f309bbe96c8be5fb09eab:
    name_prefix: '[Public] '
    end2end_encryption_enabled: false
```

---

### `per_authentik_group_pk_matrix_room_settings[*]` — `MatrixDynamicRoomSettings` schema

---

### `per_authentik_group_pk_matrix_room_settings[*].alias_prefix`

*Room alias prefix*

Prefix prepended to the room's canonical alias localpart, useful to keep bot-managed
rooms in their own namespace. `null` for no prefix.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__ALIAS_PREFIX` |

**Examples:**

*Example 1:*

```yaml
alias_prefix: authentik-
```

*Example 2:*

```yaml
alias_prefix: grp-
```

---

### `per_authentik_group_pk_matrix_room_settings[*].matrix_alias_from_authentik_attribute`

*Group attribute used as the room alias*

Authentik group attribute (dotted path) used as the room's alias localpart. The
default `pk` is the most stable choice, because it survives a group being renamed —
an alias derived from the group name would not, and a Matrix alias cannot be changed
once the room is created.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"pk"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__MATRIX_ALIAS_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

*Example 1:*

```yaml
matrix_alias_from_authentik_attribute: pk
```

*Example 2:*

```yaml
matrix_alias_from_authentik_attribute: attributes.chatroom_alias
```

---

### `per_authentik_group_pk_matrix_room_settings[*].name_prefix`

*Room name prefix*

Prefix prepended to the room's display name. `null` for no prefix.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__NAME_PREFIX` |

**Examples:**

```yaml
name_prefix: '[Chat] '
```

---

### `per_authentik_group_pk_matrix_room_settings[*].matrix_name_from_authentik_attribute`

*Group attribute used as the room name*

Authentik group attribute (dotted path) used as the room's display name.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"name"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__MATRIX_NAME_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

*Example 1:*

```yaml
matrix_name_from_authentik_attribute: name
```

*Example 2:*

```yaml
matrix_name_from_authentik_attribute: attributes.chatroom_name
```

---

### `per_authentik_group_pk_matrix_room_settings[*].topic_prefix`

*Room topic prefix*

Prefix prepended to the room's topic. `null` for no prefix.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__TOPIC_PREFIX` |

**Examples:**

```yaml
topic_prefix: "Group chat \u2014 "
```

---

### `per_authentik_group_pk_matrix_room_settings[*].matrix_topic_from_authentik_attribute`

*Group attribute used as the room topic*

Authentik group attribute (dotted path) used as the room's topic. `null` leaves the
topic unset.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"attributes.chatroom_topic"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__MATRIX_TOPIC_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

```yaml
matrix_topic_from_authentik_attribute: attributes.chatroom_topic
```

---

### `per_authentik_group_pk_matrix_room_settings[*].end2end_encryption_enabled`

*Enable end-to-end encryption*

Enable end-to-end encryption in the group-mapped Matrix rooms. The bot itself stays
outside encryption: it writes room *state* (membership, power levels, name, topic),
which is never encrypted, but it does not read or post message content in encrypted
rooms. Welcome messages are sent in a separate, unencrypted direct room. Encryption
cannot be turned off again for a room once it is on.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__END2END_ENCRYPTION_ENABLED` |

---

### `per_authentik_group_pk_matrix_room_settings[*].default_room_create_params`

*Room creation parameters*

Parameters merged into the Matrix `POST /createRoom` call for group rooms — preset,
visibility, federation, and so on. Only read when a room is actually created; changing
them later does not rewrite existing rooms. See the Client-Server API
`POST /createRoom`.

| Property | Value |
|---|---|
| Type | Dictionary of (str) |
| Required | No |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__DEFAULT_ROOM_CREATE_PARAMS` |

**Examples:**

```yaml
default_room_create_params:
  preset: private_chat
  visibility: private
```

---

### `per_authentik_group_pk_matrix_room_settings[*].matrix_room_create_params_from_authentik_attribute`

*Group attribute holding extra creation parameters*

Authentik group attribute (dotted path) holding a mapping of extra `createRoom`
parameters, merged over `default_room_create_params` for that group. `null` disables
per-group overrides.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"attributes.chatroom_params"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__MATRIX_ROOM_CREATE_PARAMS_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

```yaml
matrix_room_create_params_from_authentik_attribute: attributes.chatroom_params
```

---

### `per_authentik_group_pk_matrix_room_settings[*].keep_updating_matrix_attributes_from_authentik`

*Keep room name and topic in sync*

Re-apply the room's name and topic from Authentik on every reconcile, overwriting
any drift. Disable to let room admins rename rooms in their client and keep the
change.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__KEEP_UPDATING_MATRIX_ATTRIBUTES_FROM_AUTHENTIK` |

---

### `per_authentik_group_pk_matrix_room_settings[*].visitor_lobby_enabled`

*Open a visitor lobby beside the group room*

Maintain a second, open room — a lobby — beside each private group room. Every
member of the parent space can find the lobby in the space listing and join it of
their own accord, and nobody is ever kicked from it, so it is a front door in front of
the closed group room. The group room itself is untouched: same members, same history,
same privacy. Off by default — a lobby is a deliberate decision, not a migration.

Requires the parent space (`create_matrix_rooms_in_a_matrix_space`), because a lobby is
joinable precisely by space members and to nobody else; enabling a lobby without a
space is rejected at startup. A single group can opt in without a config change through
`matrix_room_visitor_lobby_from_authentik_attribute`.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__VISITOR_LOBBY_ENABLED` |

---

### `per_authentik_group_pk_matrix_room_settings[*].visitor_lobby_name_suffix`

*Lobby name suffix*

Appended to the group room's name to name its lobby — `Düsseldorf` becomes
`Düsseldorf (Lobby)`. The suffix is the whole user-facing explanation of what the
second room is, so it should say *this is a door*, not *these are more people*.
Alternatives worth a deliberate choice: ` (Foyer)` (reads naturally to a
German-speaking org), ` (Open)` (shortest, states the property not the metaphor), or
` & Guests` (if the room should feel like the group hosting). ` & Friends` was
rejected: the ampersand reads as a statement about membership.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `" (Lobby)"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__VISITOR_LOBBY_NAME_SUFFIX` |

**Examples:**

*Example 1:*

```yaml
visitor_lobby_name_suffix: ' (Lobby)'
```

*Example 2:*

```yaml
visitor_lobby_name_suffix: ' (Foyer)'
```

*Example 3:*

```yaml
visitor_lobby_name_suffix: ' (Open)'
```

---

### `per_authentik_group_pk_matrix_room_settings[*].visitor_lobby_alias_suffix`

*Lobby alias suffix*

Appended to the group room's alias localpart to form the lobby's alias —
`#duesseldorf` gets a `#duesseldorf-lobby` beside it. Unlike the group alias, dashes
here are kept. A Matrix alias cannot be changed after the room is created, so changing
this later makes the bot build a second, empty lobby rather than rename the first.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"-lobby"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__VISITOR_LOBBY_ALIAS_SUFFIX` |

**Examples:**

*Example 1:*

```yaml
visitor_lobby_alias_suffix: -lobby
```

*Example 2:*

```yaml
visitor_lobby_alias_suffix: -foyer
```

*Example 3:*

```yaml
visitor_lobby_alias_suffix: -open
```

---

### `per_authentik_group_pk_matrix_room_settings[*].visitor_lobby_topic_template`

*Lobby topic template*

Topic set on the lobby, with `{name}` replaced by the group room's name. State the
arrangement in the one place every visitor looks, so a visitor who reads it never
wonders why the room is quiet or which room to post in.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Open lobby for {name} \u2014 anyone in the space may join. The group's working room is private."` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__VISITOR_LOBBY_TOPIC_TEMPLATE` |

**Examples:**

```yaml
visitor_lobby_topic_template: "Open lobby for {name} \u2014 anyone in the space may\
  \ join. The group's working room is private."
```

---

### `per_authentik_group_pk_matrix_room_settings[*].visitor_lobby_end2end_encryption_enabled`

*Encrypt the lobby*

Enable end-to-end encryption in the lobby. Off by default, and deliberately weaker
than the group room's own encryption default: a lobby is open to the whole space by
construction, so encryption buys it little, while it guarantees every visitor's first
impression is a screen of `unable to decrypt`. Encryption cannot be turned off again
once a room has it, so this default is chosen for you to be able to change it before
the lobby exists rather than after.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__VISITOR_LOBBY_END2END_ENCRYPTION_ENABLED` |

---

### `per_authentik_group_pk_matrix_room_settings[*].visitor_lobby_inject_group_members`

*Seed the lobby with the group's members*

Join the group's members into the lobby as well, so a visitor who walks in finds
somebody there. On by default: an empty lobby is a dead lobby. This is a social bet —
it works when the group room is where the group *works* and the lobby is where the
group is *reachable*, and it fails, producing two half-dead rooms and doubled
notifications, when both are general-purpose. Turn it off for a large group that would
rather staff an empty lobby deliberately. Visitors are never injected anywhere — they
joined on purpose — and members are only ever added, never kicked.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__VISITOR_LOBBY_INJECT_GROUP_MEMBERS` |

---

### `per_authentik_group_pk_matrix_room_settings[*].matrix_room_visitor_lobby_from_authentik_attribute`

*Group attribute that opts a group into a lobby*

Authentik group attribute (dotted path) holding a boolean that turns the lobby on or
off for that one group, overriding `visitor_lobby_enabled`. This lets whoever owns the
group in Authentik open a lobby without a config deploy. A value that is not a boolean
is ignored with a warning and the configured default applies. `null` disables the
per-group override, leaving `visitor_lobby_enabled` in sole charge.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"attributes.chatroom_visitor_lobby"` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__MATRIX_ROOM_VISITOR_LOBBY_FROM_AUTHENTIK_ATTRIBUTE` |

**Examples:**

```yaml
matrix_room_visitor_lobby_from_authentik_attribute: attributes.chatroom_visitor_lobby
```

---

## `matrix_user_ignore_list`

*Ignored Matrix users*

Full Matrix IDs the bot never touches: it will not onboard them, invite them,
change their power level, kick them from a room, or deactivate them. Use it to protect
accounts that exist only in Matrix — server admins, other bots, bridge users — from a
sync that would otherwise see them as unknown to Authentik. The bot's own
`synapse_server.bot_user_id` is always ignored and need not be listed.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_MATRIX_USER_IGNORE_LIST` |

**Examples:**

```yaml
matrix_user_ignore_list:
- '@admin:company.org'
- '@root:company.org'
```

---

## `authentik_user_ignore_list`

*Ignored Authentik users*

Authentik usernames the bot never syncs into Matrix. Matched against the raw
`username` field, not against `authentik_username_mapping_attribute`. Use it to keep
service and break-glass accounts out of chat.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_AUTHENTIK_USER_IGNORE_LIST` |

**Examples:**

```yaml
authentik_user_ignore_list:
- admin
- internal_account_alex
```

---

## `authentik_group_id_ignore_list`

*Ignored Authentik groups*

Authentik groups that never become a Matrix room, identified by the group's primary
key (`pk`). Applied before every other group filter, so a listed group is skipped even
if it carries the attributes or name prefix that would otherwise select it.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_AUTHENTIK_GROUP_ID_IGNORE_LIST` |

**Examples:**

```yaml
authentik_group_id_ignore_list:
- 1120a6e1124f309bbe96c8be5fb09eab
```

---
