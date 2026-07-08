<!-- GENERATED FILE — do not edit by hand.
     Regenerate with `pdm run gen-config-docs` after changing onbot/config.py. -->

# Configuration Reference — `OnbotConfig`

This document is auto-generated from the pydantic-settings model. All settings can be provided via the YAML config file or overridden with environment variables.

---

## `log_level`

Logging verbosity. ``DEBUG`` is noisy but useful while wiring up the bot.

| Property | Value |
|---|---|
| Type | Enum |
| Required | No |
| Default | `"INFO"` |
| Allowed values | `INFO` · `DEBUG` |
| Environment variable | `ONBOT_LOG_LEVEL` |

**Examples:**

*Example 1:*

```yaml
log_level: INFO
```

*Example 2:*

```yaml
log_level: DEBUG
```

---

## `server_tick_rate_sec`

How often (seconds) the reconciler re-converges Authentik→Matrix state, in
addition to on-demand triggers. The reconcile is idempotent (AD-2), so this is a
safety net for drift, not the only path — onboarding still reacts to live events.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `20` |
| Environment variable | `ONBOT_SERVER_TICK_RATE_SEC` |

**Examples:**

*Example 1:*

```yaml
server_tick_rate_sec: 20
```

*Example 2:*

```yaml
server_tick_rate_sec: 300
```

---

## `synapse_server`

*Synapse Server Configuration*

Authorization/connection data for the Matrix CS and Synapse admin APIs.

| Property | Value |
|---|---|
| Type | Object (SynapseServer) |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER` |

---

### `synapse_server.server_name`

Synapse's public facing domain
https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name
This is not necessarily the domain under which the Synapse server is reachable.

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

URL to reach the Synapse server. This can (and should) be an internal URL, so the
Synapse admin API need not be public. The bot works with a public URL too.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__SERVER_URL` |

**Examples:**

```yaml
server_url: https://internal.matrix
```

---

### `synapse_server.bot_user_id`

Full Matrix user ID of an existing account; the bot acts as this user.

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

Access token authorising the bot against the Synapse APIs. Under MAS this is a
compatibility token issued via ``mas-cli manage issue-compatibility-token`` (AD-6).
Provide the bare token; do not prefix it with ``Bearer`` (the client adds that).
Leave unset (``null``) when using ``oauth2`` instead.

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

Optional OAuth2 client-credentials auth against MAS (AD-6). When set, it is used
instead of ``bot_access_token`` and tokens refresh automatically. Provide exactly one
of ``bot_access_token`` or ``oauth2``.

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

MAS OAuth2 token endpoint (the ``token_endpoint`` from MAS discovery).

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

OAuth2 client id registered for the bot in MAS.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_ID` |

---

#### `synapse_server.oauth2.client_secret`

OAuth2 client secret for the bot client. Provide the bare secret.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_SECRET` |

---

#### `synapse_server.oauth2.scope`

Optional space-separated scopes to request (e.g. the Synapse admin scope).

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

HTTP URL to a picture; the bot sets it as its own avatar on start.

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

Sub-path the Synapse admin API is served under. Keep the default if unsure.

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

## `authentik_server`

Connection data and API token for the upstream Authentik IdP (source of truth).

| Property | Value |
|---|---|
| Type | Object (AuthentikServer) |
| Required | **Yes** |
| Environment variable | `ONBOT_AUTHENTIK_SERVER` |

---

### `authentik_server.url`

URL to reach your Authentik server.

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

API token for your Authentik server. Generate one at
``https://<authentik>/if/admin/#/core/tokens``. Provide the bare token; the client
adds the ``Bearer`` prefix.

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

Optional MAS admin API credentials. Required for the lifecycle module to actually
revoke sessions / deactivate accounts under MAS — the Synapse admin API cannot
(ADR-0005, BATTLE_PLAN §7 Q1). Leave unset on non-MAS deployments.

| Property | Value |
|---|---|
| Type | Object (MasAdmin) |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_MAS_ADMIN` |

---

### `mas_admin.url`

Base URL of the Matrix Authentication Service.

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

OAuth2 client id of the bot's MAS admin client (client_credentials).

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_MAS_ADMIN__CLIENT_ID` |

---

### `mas_admin.client_secret`

OAuth2 client secret of the bot's MAS admin client.

| Property | Value |
|---|---|
| Type | str |
| Required | **Yes** |
| Environment variable | `ONBOT_MAS_ADMIN__CLIENT_SECRET` |

---

## `welcome_new_users_messages`

Messages the bot sends, in order, in the 1:1 welcome DM to each newly onboarded
user. Each message is sent once (content-hashed, idempotent). ``null`` or an empty
list disables the welcome DM entirely.

| Property | Value |
|---|---|
| Type | List of str |
| Required | No |
| Environment variable | `ONBOT_WELCOME_NEW_USERS_MESSAGES` |

---

## `place_onboarding_rooms_in_space`

Gather the 1:1 onboarding/welcome rooms under the managed parent space (G4.5).
Off by default — whether direct rooms belong in a space is a matter of taste. Only
applies when the parent space is enabled.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_PLACE_ONBOARDING_ROOMS_IN_SPACE` |

---

## `sync_authentik_users_with_matrix_rooms`

User→room-membership projection (the core Authentik→Matrix sync).

| Property | Value |
|---|---|
| Type | Object (SyncAuthentikUsersWithMatrix) |
| Required | No |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS` |

---

### `sync_authentik_users_with_matrix_rooms.enabled`

Master switch for projecting Authentik group membership into Matrix rooms.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__ENABLED` |

---

### `sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute`

Source of the localpart of the Matrix ID (``@<localpart>:server``). A dotted path
into the Authentik user object (e.g. ``username`` or ``attributes.matrix_name``).
Under MAS this MUST agree with the localpart template MAS derives from the upstream
claim, or provisioned users will not match (AD-6).

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"username"` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__AUTHENTIK_USERNAME_MAPPING_ATTRIBUTE` |

---

### `sync_authentik_users_with_matrix_rooms.kick_matrix_room_members_not_in_mapped_authentik_group_anymore`

When a user leaves an Authentik group, kick them from the corresponding Matrix
room so membership stays a faithful mirror. Disable to let the bot only ever *add*
members (never remove).

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__KICK_MATRIX_ROOM_MEMBERS_NOT_IN_MAPPED_AUTHENTIK_GROUP_ANYMORE` |

---

### `sync_authentik_users_with_matrix_rooms.sync_only_users_in_authentik_pathes`

Restrict syncing to users under these Authentik directory paths. ``null`` syncs
users regardless of path.

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

Only sync users carrying all of these Authentik attributes (exact match).
``null`` syncs every user.

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

Only sync users who belong to at least one of these Authentik groups (by pk).
``null`` applies no group filter.

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

Quarantined lifecycle settings: lock out Matrix accounts disabled upstream.

| Property | Value |
|---|---|
| Type | Object (DeactivateDisabledAuthentikUsersInMatrix) |
| Required | No |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix` — `DeactivateDisabledAuthentikUsersInMatrix` schema

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.enabled`

Lock out Matrix accounts whose Authentik account was disabled/deleted.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__ENABLED` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.dry_run`

Quarantine switch (AD-5): while ``true`` the bot only records bookkeeping and logs
what it *would* do to the ``onbot.lifecycle.audit`` channel — no session is revoked
and no account is deactivated. Set ``false`` to actually perform destructive
lifecycle actions. Defaults to ``true`` so destructive offboarding is opt-in.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__DRY_RUN` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.deactivate_after_n_sec`

Cooldown before deactivation, to absorb accidental upstream disables.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `86400` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__DEACTIVATE_AFTER_N_SEC` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.delete_after_n_sec`

Further cooldown before erase/delete. ``null`` disables deletion.

| Property | Value |
|---|---|
| Type | int |
| Required | No |
| Default | `31536000` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__DELETE_AFTER_N_SEC` |

---

#### `sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix.include_user_media_on_delete`

Also delete media uploaded by the user on account deletion (data protection).

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_SYNC_AUTHENTIK_USERS_WITH_MATRIX_ROOMS__DEACTIVATE_DISABLED_AUTHENTIK_USERS_IN_MATRIX__INCLUDE_USER_MEDIA_ON_DELETE` |

---

## `create_matrix_rooms_in_a_matrix_space`

Configure the designated parent space for Authentik-group rooms.

| Property | Value |
|---|---|
| Type | Object (CreateMatrixRoomsInAMatrixSpace) |
| Required | No |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE` |

---

### `create_matrix_rooms_in_a_matrix_space.enabled`

Gather all Authentik-group rooms under a dedicated parent space.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__ENABLED` |

---

### `create_matrix_rooms_in_a_matrix_space.alias`

Localpart of the space canonical alias (e.g. "#<alias>:server").

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

Whether/how the parent space is created.

| Property | Value |
|---|---|
| Type | Object (CreateMatrixSpaceIfNotExists) |
| Required | No |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists` — `CreateMatrixSpaceIfNotExists` schema

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled`

Create the parent space if it does not exist.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__ENABLED` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name`

Display name of the space.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"OnBotSpace"` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__NAME` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic`

Matrix topic (tagline) for the space.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `"Space for authentik group rooms"` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__TOPIC` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.avatar_url`

HTTP(S) URL to a picture used as the space avatar (icon). Applied on every reconcile and re-uploaded only when the URL changes, so it also updates an existing space.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__AVATAR_URL` |

---

#### `create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.space_params`

Extra parameters passed to the space-creation call.

| Property | Value |
|---|---|
| Type | Dictionary of (str) |
| Required | No |
| Environment variable | `ONBOT_CREATE_MATRIX_ROOMS_IN_A_MATRIX_SPACE__CREATE_MATRIX_SPACE_IF_NOT_EXISTS__SPACE_PARAMS` |

---

## `sync_matrix_rooms_based_on_authentik_groups`

Group→room projection rules (which groups become rooms, power levels).

| Property | Value |
|---|---|
| Type | Object (SyncMatrixRoomsBasedOnAuthentikGroups) |
| Required | No |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS` |

---

### `sync_matrix_rooms_based_on_authentik_groups.enabled`

Master switch for creating/maintaining one Matrix room per Authentik group.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__ENABLED` |

---

### `sync_matrix_rooms_based_on_authentik_groups.only_for_children_of_groups_with_uid`

Only mirror Authentik groups that are children of one of these parent groups (by
uid/pk). ``null`` considers all groups.

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

Only mirror Authentik groups carrying these custom attributes. If unset, all
groups become rooms. https://goauthentik.io/docs/user-group/group#attributes

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

Key inside an Authentik group's custom ``attributes`` holding an HTTP(S) URL used as
that group's room avatar (icon). Applied on every reconcile and re-uploaded only when
the URL changes. ``null`` disables per-room avatars.

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

Only mirror Authentik groups whose name starts with this prefix — a lightweight way
to opt specific groups into chat without custom attributes. ``null`` disables the
filter.

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

If a mapped Authentik group disappears (deleted or lost its matching attribute),
kick all members and block the room.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__DISABLE_ROOMS_WHEN_MAPPED_AUTHENTIK_GROUP_DISAPPEARS` |

---

### `sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms`

When a room is disabled (its Authentik group disappeared, see
``disable_rooms_when_mapped_authentik_group_disappears``), also delete it via the
Synapse admin API rather than only blocking it. Irreversible — leave ``false`` unless
you are sure.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `false` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__DELETE_DISABLED_ROOMS` |

---

### `sync_matrix_rooms_based_on_authentik_groups.make_authentik_superusers_matrix_room_admin`

Grant Authentik superusers the Matrix admin power level (100) in the rooms they are
members of. Takes precedence over ``authentik_group_attr_for_matrix_power_level``.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_SYNC_MATRIX_ROOMS_BASED_ON_AUTHENTIK_GROUPS__MAKE_AUTHENTIK_SUPERUSERS_MATRIX_ROOM_ADMIN` |

---

### `sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level`

Authentik group attribute (dotted path) holding an integer 0-100. Members of the
group get that Matrix power level in their onbot rooms. Superusers made admin (see
``make_authentik_superusers_matrix_room_admin``) ignore this. On conflicting values
across multiple group memberships the highest wins.

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

Default room identity template applied to every group room.

| Property | Value |
|---|---|
| Type | Object (MatrixDynamicRoomSettings) |
| Required | No |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS` |

---

### `matrix_room_default_settings.alias_prefix`

Prefix prepended to the room's canonical alias localpart. ``null`` for none.

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

Authentik group attribute (dotted path) used as the room alias localpart. The
default ``pk`` is the most stable choice (it never changes when a group is renamed).

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

Prefix prepended to the room's display name. ``null`` for none.

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

Authentik group attribute (dotted path) used as the room display name.

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

Prefix prepended to the room topic. ``null`` for none.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__TOPIC_PREFIX` |

---

### `matrix_room_default_settings.matrix_topic_from_authentik_attribute`

Authentik group attribute (dotted path) used as the room topic. ``null`` leaves the
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

Enable end-to-end encryption in the group-mapped Matrix rooms. The bot itself stays
outside encryption (ADR-0009): it writes room *state* (membership, power levels) but
does not read/post message content in encrypted rooms — welcome DMs are plaintext.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__END2END_ENCRYPTION_ENABLED` |

---

### `matrix_room_default_settings.default_room_create_params`

Parameters merged into the Matrix ``createRoom`` call for group rooms (preset,
visibility, federation, …). See the Client-Server API ``POST /createRoom``.

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

Authentik group attribute (dotted path) holding a dict of extra ``createRoom``
params, merged over ``default_room_create_params`` for that group. ``null`` to
disable per-group overrides.

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

Keep room name/topic in sync with Authentik, overwriting drift.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_MATRIX_ROOM_DEFAULT_SETTINGS__KEEP_UPDATING_MATRIX_ATTRIBUTES_FROM_AUTHENTIK` |

---

## `per_authentik_group_pk_matrix_room_settings`

Per-group room-setting overrides, keyed by Authentik group primary key (pk).

| Property | Value |
|---|---|
| Type | Dictionary of (str, Object (MatrixDynamicRoomSettings)) |
| Required | No |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS` |

---

### `per_authentik_group_pk_matrix_room_settings[*]` — `MatrixDynamicRoomSettings` schema

---

### `per_authentik_group_pk_matrix_room_settings[*].alias_prefix`

Prefix prepended to the room's canonical alias localpart. ``null`` for none.

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

Authentik group attribute (dotted path) used as the room alias localpart. The
default ``pk`` is the most stable choice (it never changes when a group is renamed).

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

Prefix prepended to the room's display name. ``null`` for none.

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

Authentik group attribute (dotted path) used as the room display name.

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

Prefix prepended to the room topic. ``null`` for none.

| Property | Value |
|---|---|
| Type | str |
| Required | No |
| Default | `null` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__TOPIC_PREFIX` |

---

### `per_authentik_group_pk_matrix_room_settings[*].matrix_topic_from_authentik_attribute`

Authentik group attribute (dotted path) used as the room topic. ``null`` leaves the
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

Enable end-to-end encryption in the group-mapped Matrix rooms. The bot itself stays
outside encryption (ADR-0009): it writes room *state* (membership, power levels) but
does not read/post message content in encrypted rooms — welcome DMs are plaintext.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__END2END_ENCRYPTION_ENABLED` |

---

### `per_authentik_group_pk_matrix_room_settings[*].default_room_create_params`

Parameters merged into the Matrix ``createRoom`` call for group rooms (preset,
visibility, federation, …). See the Client-Server API ``POST /createRoom``.

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

Authentik group attribute (dotted path) holding a dict of extra ``createRoom``
params, merged over ``default_room_create_params`` for that group. ``null`` to
disable per-group overrides.

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

Keep room name/topic in sync with Authentik, overwriting drift.

| Property | Value |
|---|---|
| Type | bool |
| Required | No |
| Default | `true` |
| Environment variable | `ONBOT_PER_AUTHENTIK_GROUP_PK_MATRIX_ROOM_SETTINGS[*]__KEEP_UPDATING_MATRIX_ATTRIBUTES_FROM_AUTHENTIK` |

---

## `matrix_user_ignore_list`

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
