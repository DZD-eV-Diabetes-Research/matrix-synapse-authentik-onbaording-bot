"""Configuration model (pydantic-settings).

Ported and refined from the legacy ``onbot/config.py`` (AD-1, reuse-the-config decision).
Changes from legacy:

* pydantic v2 / ``pydantic-settings`` v2 (``SettingsConfigDict``) and modern ``X | None`` typing.
* Removed the duplicated top-level ``DeactivateDisabledAuthentikUsersInMatrix`` block — the
  lifecycle settings live solely under :class:`SyncAuthentikUsersWithMatrix` where they are read.
* Fixed legacy type/default bugs (``sync_only_users_of_groups_with_id`` defaulted to ``None`` on a
  non-optional ``list``; ``only_groups_with_attributes`` / ``only_for_groupnames_starting_with``
  carried list defaults on non-list fields).
* No ``matrix-nio`` references — the Matrix client library is a Phase 6 decision (AD, BATTLE_PLAN §5).
* Phase 8: dropped the vestigial ``storage_dir`` / ``storage_encryption_key`` fields — they backed
  the libolm key store, which ADR-0009 removed (the bot operates outside encrypted rooms and keeps
  no on-disk crypto state). They were unused everywhere in the codebase; the lifecycle ledger lives
  in Matrix account data (ADR-0001, no DB).

Field metadata is the source of the generated user documentation: ``scripts/gen_config_docs.py``
renders ``docs/CONFIG_REFERENCE.md`` and ``config.example.yml`` from this model with psyplus, which
reads each field's ``title``, ``description`` and ``examples`` (type, default, required-ness and the
``ONBOT_*`` env-var name are derived automatically). Two conventions follow from that:

* ``description`` is read by operators who have never seen this repository. Keep internal shorthand
  (AD-n, ADR-nnnn, BATTLE_PLAN §x, phase numbers) out of it and put it in the class docstrings
  below, which psyplus does not render.
* Use single backticks in ``description``: they are a code span in the generated Markdown and stay
  legible in the generated YAML comments.

Loading: :func:`load_config` builds the model from, in descending precedence, ``ONBOT_*`` environment
variables (``ONBOT_`` prefix, ``__`` nesting delimiter) and then the YAML file at
``ONBOT_CONFIG_FILE_PATH``. The file is the *lowest*-priority source, which is what lets a
deployment commit a fully documented config and inject only the credentials through the
environment; the sources are deep-merged, so ``ONBOT_MAS_ADMIN__CLIENT_SECRET`` fills in one key of
a ``mas_admin`` block the file otherwise defines. :func:`generate_example_config` dumps the default
model to YAML (G11.2), deliberately with every source switched off.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

CONFIG_FILE_ENV_VAR = "ONBOT_CONFIG_FILE_PATH"


class MatrixOAuth2(BaseModel):
    """OAuth2 client-credentials auth against MAS (AD-6, Phase 6).

    The forward-looking alternative to a static compatibility token: the bot is a confidential
    OAuth2 client of MAS and mints short-lived access tokens that refresh transparently. When this
    block is set it takes precedence over ``bot_access_token``.
    """

    token_endpoint: Annotated[
        str,
        Field(
            title="MAS token endpoint",
            description=inspect.cleandoc(
                """The `token_endpoint` advertised by the Matrix Authentication Service, as listed in
                its OpenID discovery document at `/.well-known/openid-configuration`."""
            ),
            examples=["https://auth.company.org/oauth2/token"],
        ),
    ]
    client_id: Annotated[
        str,
        Field(
            title="OAuth2 client id",
            description=inspect.cleandoc(
                """Client id of the OAuth2 client registered for the bot in the Matrix
                Authentication Service. The client must be allowed to use the `client_credentials`
                grant."""
            ),
            examples=["01HXQ3B9ZK7Y2QW8N4V6M0EXAMPLE"],
        ),
    ]
    client_secret: Annotated[
        str,
        Field(
            title="OAuth2 client secret",
            description=inspect.cleandoc(
                """Client secret belonging to `client_id`. Provide the bare secret — the bot builds
                the HTTP authorization header itself. Treat this as a credential: prefer supplying it
                through the `ONBOT_SYNAPSE_SERVER__OAUTH2__CLIENT_SECRET` environment variable rather
                than committing it to the config file."""
            ),
            examples=["ONLY_AN_EXAMPLE_SECRET_pMv1kZ8sQ0"],
        ),
    ]
    scope: Annotated[
        str | None,
        Field(
            title="Requested OAuth2 scopes",
            description=inspect.cleandoc(
                """Space-separated scopes to request with the token. The bot needs the Matrix
                client-server API scope, plus the Synapse admin scope for the room and account
                management it performs. `null` requests the client's default scopes."""
            ),
            examples=["urn:matrix:org.matrix.msc2967.client:api:* urn:synapse:admin:*"],
        ),
    ] = None


class MasAdmin(BaseModel):
    """MAS admin API credentials for lifecycle enforcement (ADR-0005/0006, §7 Q1).

    Under MAS the Matrix token is owned by MAS, so the Synapse admin API cannot revoke a live
    session — only MAS can (lock/deactivate). When this block is set, the lifecycle module enforces
    lockout through the MAS admin API using an OAuth2 ``client_credentials`` token with the
    ``urn:mas:admin`` scope (the client must be listed in MAS ``policy.data.admin_clients``). Leave
    unset to fall back to the Synapse-admin effectors (which do NOT revoke MAS sessions).
    """

    url: Annotated[
        str,
        Field(
            title="MAS base URL",
            description=inspect.cleandoc(
                """Base URL of the Matrix Authentication Service, without a trailing path. As with
                `synapse_server.server_url` this may be an internal URL."""
            ),
            examples=["https://auth.company.org"],
        ),
    ]
    client_id: Annotated[
        str,
        Field(
            title="MAS admin client id",
            description=inspect.cleandoc(
                """Client id of an OAuth2 client that may request the `urn:mas:admin` scope. The
                client must use the `client_credentials` grant and be listed in the MAS
                `policy.data.admin_clients` allowlist, otherwise MAS refuses the token."""
            ),
            examples=["01HXQ3B9ZK7Y2QW8N4V6M0EXAMPLE"],
        ),
    ]
    client_secret: Annotated[
        str,
        Field(
            title="MAS admin client secret",
            description=inspect.cleandoc(
                """Client secret belonging to `client_id`. This credential can lock and deactivate
                any account on the homeserver — prefer supplying it through the
                `ONBOT_MAS_ADMIN__CLIENT_SECRET` environment variable rather than committing it to
                the config file."""
            ),
            examples=["ONLY_AN_EXAMPLE_SECRET_pMv1kZ8sQ0"],
        ),
    ]


class AdminRoom(BaseModel):
    """The operator control room (ADR-0010).

    A single Matrix room the bot joins, invites the admins to, and listens in for prefixed commands
    (``!announce``, ``!help``, ``!status``). It is an operator *interface*, not a state source: no
    command here feeds the reconciler's desired state, which is why reading messages does not
    contradict ADR-0002.

    Authorisation is an allowlist, checked against the event sender — never the sender's room power
    level. The power levels are the fence; the allowlist is the gate. The allowlist is the union of
    ``admin_user_ids`` and the members of the Authentik groups in
    ``authentik_group_pks_granting_bot_admin``; an empty union means nobody may command the bot.
    """

    enabled: Annotated[
        bool,
        Field(
            title="Enable the admin control room",
            description=inspect.cleandoc(
                """Create and listen in a control room where administrators can command the bot. Off
                by default: the room lets anyone on the bot's admin allowlist send a message to every
                user on the server, so it should be turned on deliberately. The `onbot broadcast`
                command-line tool does the same job without a room."""
            ),
        ),
    ] = False
    alias: Annotated[
        str,
        Field(
            title="Control room alias localpart",
            description=inspect.cleandoc(
                """Localpart of the control room's canonical alias, i.e. the `<alias>` in
                `#<alias>:<server_name>`. This is how the bot finds the room again, so changing it
                later makes the bot create a second, empty control room rather than rename the
                first."""
            ),
            examples=["onbot-admin", "bot-control"],
        ),
    ] = "onbot-admin"
    name: Annotated[
        str,
        Field(
            title="Control room name",
            description="Display name of the control room. Only read when the room is created.",
            examples=["Onbot Admin"],
        ),
    ] = "Onbot Admin"
    topic: Annotated[
        str,
        Field(
            title="Control room topic",
            description=inspect.cleandoc(
                """Matrix topic (tagline) of the control room. The bot keeps this in sync with the
                commands it supports, so a one-line reminder is visible without scrolling."""
            ),
            examples=["Bot control room. !help for commands."],
        ),
    ] = "Onbot control room — say !help for the available commands."
    admin_user_ids: Annotated[
        list[str],
        Field(
            title="Administrators allowed to command the bot",
            description=inspect.cleandoc(
                """Full Matrix IDs permitted to run commands in the control room, listed by hand.
                These are added to whatever `authentik_group_pks_granting_bot_admin` resolves to, and
                this list is the right home for accounts Authentik has never heard of — a break-glass
                admin, another bot. It is also the floor the bot falls back on: it needs no Authentik
                call, so these administrators keep their commands when Authentik is unreachable.
                Everyone not in the union of the two lists is refused, even if they are somehow in the
                room and even if they hold a high power level there, because a command like
                `!announce` reaches every user on the server. The bot invites these users to the room
                when it creates it."""
            ),
            examples=[["@admin:company.org", "@ops-lead:company.org"]],
        ),
    ] = Field(default_factory=list)
    authentik_group_pks_granting_bot_admin: Annotated[
        list[str],
        Field(
            title="Authentik groups whose members may command the bot",
            description=inspect.cleandoc(
                """Primary keys (`pk`) of Authentik groups whose members may run commands in the
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
                with an empty `admin_user_ids`, means nobody may command the bot."""
            ),
            examples=[["1120a6e1124f309bbe96c8be5fb09eab"]],
        ),
    ] = Field(default_factory=list)


class SynapseServer(BaseModel):
    """Where the homeserver lives and how the bot authenticates against it."""

    server_name: Annotated[
        str,
        Field(
            title="Matrix server name",
            description=inspect.cleandoc(
                """Synapse's public facing domain — the part after the colon in a Matrix ID such as
                `@alice:company.org`. This is not necessarily the domain under which the Synapse
                server is reachable; that is `server_url`. See
                https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name"""
            ),
            examples=["company.org"],
        ),
    ]
    server_url: Annotated[
        str,
        Field(
            title="Synapse base URL",
            description=inspect.cleandoc(
                """URL the bot uses to reach the Synapse server. This can (and should) be an internal
                URL, so the Synapse admin API need not be exposed publicly. A public URL works too."""
            ),
            examples=["https://internal.matrix", "https://matrix.company.org"],
        ),
    ]
    bot_user_id: Annotated[
        str,
        Field(
            title="Bot Matrix ID",
            description=inspect.cleandoc(
                """Full Matrix user ID of an existing account; the bot acts as this user. The account
                must already exist — the bot does not register itself — and its localpart must be on
                `server_name`."""
            ),
            examples=["@welcome-bot:company.org"],
        ),
    ]
    bot_access_token: Annotated[
        str | None,
        Field(
            title="Bot access token",
            description=inspect.cleandoc(
                """Access token authorising the bot against the Synapse APIs. On a homeserver fronted
                by the Matrix Authentication Service this is a compatibility token, issued with
                `mas-cli manage issue-compatibility-token`. Provide the bare token; do not prefix it
                with `Bearer` (the client adds that). Leave unset (`null`) when using `oauth2`
                instead. Prefer supplying it through the
                `ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN` environment variable rather than committing
                it to the config file."""
            ),
            examples=["syt_ONLY_AN_EXAMPLE_TOKEN_sadaw4"],
        ),
    ] = None
    oauth2: Annotated[
        MatrixOAuth2 | None,
        Field(
            title="OAuth2 authentication (alternative to the access token)",
            description=inspect.cleandoc(
                """Authenticate as an OAuth2 client of the Matrix Authentication Service instead of
                carrying a static token. When set, this is used in place of `bot_access_token` and
                the short-lived tokens it mints refresh automatically. Provide exactly one of
                `bot_access_token` or `oauth2`."""
            ),
        ),
    ] = None
    bot_avatar_url: Annotated[
        str | None,
        Field(
            title="Bot avatar",
            description=inspect.cleandoc(
                """HTTP(S) URL to a picture the bot sets as its own avatar on start. The image is
                downloaded and re-uploaded to the homeserver's media repository, and only re-uploaded
                when the URL changes. `null` leaves the bot's current avatar untouched."""
            ),
            examples=["https://sillyimages.com/face.png"],
        ),
    ] = None
    admin_api_path: Annotated[
        str,
        Field(
            title="Synapse admin API sub-path",
            description=inspect.cleandoc(
                """Sub-path the Synapse admin API is served under, relative to `server_url`. Only
                needs changing if a reverse proxy remounts the admin API. Keep the default if
                unsure."""
            ),
            examples=["_synapse/admin/"],
        ),
    ] = "_synapse/admin/"


class AuthentikServer(BaseModel):
    """Where the upstream identity provider lives and how the bot authenticates against it."""

    url: Annotated[
        str,
        Field(
            title="Authentik base URL",
            description="URL the bot uses to reach your Authentik server. May be an internal URL.",
            examples=["https://authentik.company.org/"],
        ),
    ]
    api_key: Annotated[
        str,
        Field(
            title="Authentik API token",
            description=inspect.cleandoc(
                """API token for your Authentik server. Generate one under
                `https://<authentik>/if/admin/#/core/tokens`. The token only ever needs to *read*
                users and groups. Provide the bare token; the client adds the `Bearer` prefix. Prefer
                supplying it through the `ONBOT_AUTHENTIK_SERVER__API_KEY` environment variable
                rather than committing it to the config file."""
            ),
            examples=["yEl4tFqeIBQwoHAd9hajmkm2PBjSAirY_THIS_IS_JUST_AN_EXAMPLE_i57e"],
        ),
    ]


class DeactivateDisabledAuthentikUsersInMatrix(BaseModel):
    """Lifecycle settings (Phase 5 — quarantined, dry-run/audit default; AD-5)."""

    enabled: Annotated[
        bool,
        Field(
            title="Enable offboarding",
            description=inspect.cleandoc(
                """Lock out Matrix accounts whose Authentik account was disabled or deleted. With
                `dry_run` left at its default this only produces an audit trail; see `dry_run` before
                turning this into a destructive action."""
            ),
        ),
    ] = True
    dry_run: Annotated[
        bool,
        Field(
            title="Dry run (audit only)",
            description=inspect.cleandoc(
                """Quarantine switch. While `true` the bot only records bookkeeping and logs what it
                *would* do to the `onbot.lifecycle.audit` channel — no session is revoked and no
                account is deactivated or deleted. Set `false` to actually perform destructive
                lifecycle actions. Defaults to `true` so destructive offboarding is always opt-in;
                run with the default first and read the audit log before switching it off."""
            ),
        ),
    ] = True
    deactivate_after_n_sec: Annotated[
        int,
        Field(
            title="Grace period before deactivation",
            description=inspect.cleandoc(
                """Seconds a user must stay disabled in Authentik before their Matrix account is
                deactivated. Absorbs accidental upstream disables: re-enabling the Authentik account
                within this window cancels the pending deactivation. The default is 24 hours."""
            ),
            examples=[86400, 3600],
        ),
    ] = 60 * 60 * 24
    delete_after_n_sec: Annotated[
        int | None,
        Field(
            title="Grace period before deletion",
            description=inspect.cleandoc(
                """Seconds a user must stay disabled in Authentik before their Matrix account is
                erased, counted from the same point as `deactivate_after_n_sec` (so it should be the
                larger of the two). `null` disables deletion entirely and leaves accounts
                deactivated. The default is 365 days."""
            ),
            examples=[31536000, None],
        ),
    ] = 60 * 60 * 24 * 365
    include_user_media_on_delete: Annotated[
        bool,
        Field(
            title="Delete user media on deletion",
            description=inspect.cleandoc(
                """Also delete media the user uploaded when their account is erased. Useful to honour
                data-protection requests, but the media is unrecoverable and may still be referenced
                by messages in rooms the user posted in."""
            ),
        ),
    ] = False


class SyncAuthentikUsersWithMatrix(BaseModel):
    """How Authentik users/groups are projected into Matrix room membership (the core sync)."""

    enabled: Annotated[
        bool,
        Field(
            title="Enable user synchronisation",
            description="Master switch for projecting Authentik group membership into Matrix rooms.",
        ),
    ] = True
    authentik_username_mapping_attribute: Annotated[
        str,
        Field(
            title="Attribute holding the Matrix localpart",
            description=inspect.cleandoc(
                """Source of the localpart of the Matrix ID (`@<localpart>:server`). A dotted path
                into the Authentik user object, e.g. `username` or `attributes.matrix_name`. On a
                homeserver fronted by the Matrix Authentication Service this MUST agree with the
                localpart template MAS derives from the upstream claim, or provisioned users will
                never match the accounts they own."""
            ),
            examples=["username", "attributes.matrix_name"],
        ),
    ] = "username"
    kick_matrix_room_members_not_in_mapped_authentik_group_anymore: Annotated[
        bool,
        Field(
            title="Kick members who left the Authentik group",
            description=inspect.cleandoc(
                """When a user leaves an Authentik group, kick them from the corresponding Matrix
                room so membership stays a faithful mirror. Disable to let the bot only ever *add*
                members and never remove them — users then keep access to rooms after losing the
                group that granted it."""
            ),
        ),
    ] = True
    sync_only_users_in_authentik_pathes: Annotated[
        list[str] | None,
        Field(
            title="Filter: Authentik directory paths",
            description=inspect.cleandoc(
                """Only sync users that live under one of these Authentik directory paths. Paths are
                matched exactly, so a parent path does not imply its children — list both if you want
                both. `null` syncs users regardless of path."""
            ),
            examples=[["users", "users/staff"]],
        ),
    ] = None
    sync_only_users_with_authentik_attributes: Annotated[
        dict[str, Any] | None,
        Field(
            title="Filter: Authentik user attributes",
            description=inspect.cleandoc(
                """Only sync users carrying all of these custom Authentik attributes, compared by
                exact value. A convenient way to let users opt into chat. `null` syncs every user.
                See https://docs.goauthentik.io/docs/users-sources/user/user_ref#attributes"""
            ),
            examples=[{"is_chat_user": True}],
        ),
    ] = None
    sync_only_users_of_groups_with_id: Annotated[
        list[str] | None,
        Field(
            title="Filter: Authentik group membership",
            description=inspect.cleandoc(
                """Only sync users who belong to at least one of these Authentik groups, identified by
                the group's primary key (`pk`). `null` applies no group filter. Note this filters
                *which users exist* for the bot; it does not by itself decide which groups become
                rooms — that is `sync_matrix_rooms_based_on_authentik_groups`."""
            ),
            examples=[["1120a6e1124f309bbe96c8be5fb09eab"]],
        ),
    ] = None
    deactivate_disabled_authentik_users_in_matrix: Annotated[
        DeactivateDisabledAuthentikUsersInMatrix,
        Field(
            title="Offboarding: lock out users disabled upstream",
            description=inspect.cleandoc(
                """What happens to a Matrix account once its Authentik account is disabled or
                deleted. Defaults to an audit-only dry run — see the `dry_run` field below."""
            ),
        ),
    ] = Field(default_factory=DeactivateDisabledAuthentikUsersInMatrix)


class CreateMatrixSpaceIfNotExists(BaseModel):
    """Identity of the parent space the bot creates when it is missing."""

    enabled: Annotated[
        bool,
        Field(
            title="Create the space if missing",
            description=inspect.cleandoc(
                """Create the parent space when no space with the configured alias exists. Disable if
                you want to create and curate the space yourself; the bot then only adds rooms to
                it."""
            ),
        ),
    ] = True
    name: Annotated[
        str,
        Field(
            title="Space display name",
            description="Display name of the space, as shown in the client's room list.",
            examples=["Company Chat", "OnBotSpace"],
        ),
    ] = "OnBotSpace"
    topic: Annotated[
        str,
        Field(
            title="Space topic",
            description="Matrix topic (tagline) for the space.",
            examples=["Space for authentik group rooms"],
        ),
    ] = "Space for authentik group rooms"
    avatar_url: Annotated[
        str | None,
        Field(
            title="Space avatar",
            description=inspect.cleandoc(
                """HTTP(S) URL to a picture used as the space avatar (icon). The image is downloaded
                and re-uploaded to the homeserver's media repository on every reconcile, but only
                when the URL changed — so this also updates an already existing space. `null` leaves
                the space without an avatar."""
            ),
            examples=["https://sillyimages.com/space.png"],
        ),
    ] = None
    space_params: Annotated[
        dict[str, Any],
        Field(
            title="Space creation parameters",
            description=inspect.cleandoc(
                """Extra parameters merged into the Matrix `POST /createRoom` call that creates the
                space. Only read when the space is actually created; changing them later does not
                rewrite an existing space. See the Client-Server API `POST /createRoom`."""
            ),
            examples=[{"preset": "private_chat", "visibility": "private"}],
        ),
    ] = Field(default_factory=lambda: {"preset": "private_chat", "visibility": "private"})


class CreateMatrixRoomsInAMatrixSpace(BaseModel):
    """Whether the group rooms are gathered under one parent space, and which one."""

    enabled: Annotated[
        bool,
        Field(
            title="Group rooms into a space",
            description=inspect.cleandoc(
                """Gather all Authentik-group rooms under a dedicated parent space. Disable to leave
                the rooms unparented at the top of the user's room list."""
            ),
        ),
    ] = True
    alias: Annotated[
        str,
        Field(
            title="Space alias localpart",
            description=inspect.cleandoc(
                """Localpart of the space's canonical alias, i.e. the `<alias>` in
                `#<alias>:<server_name>`. This is how the bot finds an existing space, so changing it
                later makes the bot create a second, empty space rather than rename the first."""
            ),
            examples=["myspace", "companyspace"],
        ),
    ] = "OnBotSpace"
    create_matrix_space_if_not_exists: Annotated[
        CreateMatrixSpaceIfNotExists,
        Field(
            title="Space creation",
            description="Whether and how the parent space is created when it does not exist yet.",
        ),
    ] = Field(default_factory=CreateMatrixSpaceIfNotExists)


class SyncMatrixRoomsBasedOnAuthentikGroups(BaseModel):
    """Which Authentik groups become Matrix rooms, and how their power levels are derived."""

    enabled: Annotated[
        bool,
        Field(
            title="Enable room synchronisation",
            description="Master switch for creating and maintaining one Matrix room per Authentik group.",
        ),
    ] = True
    only_for_children_of_groups_with_uid: Annotated[
        list[str] | None,
        Field(
            title="Filter: child groups of these parents",
            description=inspect.cleandoc(
                """Only mirror Authentik groups that are direct children of one of these parent
                groups, identified by the parent's primary key (`pk`). Only the immediate children
                match, not grandchildren. `null` considers all groups."""
            ),
            examples=[["a1b2c3d4parentgroupuid"]],
        ),
    ] = None
    only_groups_with_attributes: Annotated[
        dict[str, Any] | None,
        Field(
            title="Filter: Authentik group attributes",
            description=inspect.cleandoc(
                """Only mirror Authentik groups carrying all of these custom attributes, compared by
                exact value. A convenient way to opt a group into chat. `null` turns every group into
                a room. See https://goauthentik.io/docs/user-group/group#attributes"""
            ),
            examples=[{"is_chatroom": True}],
        ),
    ] = None
    room_avatar_url_attribute: Annotated[
        str | None,
        Field(
            title="Group attribute holding the room avatar URL",
            description=inspect.cleandoc(
                """Key inside an Authentik group's custom `attributes` holding an HTTP(S) URL used as
                that group's room avatar (icon). The image is downloaded and re-uploaded to the
                homeserver's media repository on every reconcile, but only when the URL changed.
                `null` disables per-room avatars."""
            ),
            examples=["chatroom_avatar_url"],
        ),
    ] = "chatroom_avatar_url"
    only_for_groupnames_starting_with: Annotated[
        str | None,
        Field(
            title="Filter: group name prefix",
            description=inspect.cleandoc(
                """Only mirror Authentik groups whose name starts with this prefix — a lightweight way
                to opt specific groups into chat without custom attributes. The prefix stays part of
                the group name and therefore of the room name unless you override the name template.
                `null` disables the filter."""
            ),
            examples=["chat-", "matrix_"],
        ),
    ] = None
    disable_rooms_when_mapped_authentik_group_disappears: Annotated[
        bool,
        Field(
            title="Disable orphaned rooms",
            description=inspect.cleandoc(
                """If a mapped Authentik group disappears — deleted, or it no longer passes the
                filters above — kick all members from the corresponding room and block it. Note that
                loosening a filter therefore re-enables nothing: the room stays blocked."""
            ),
        ),
    ] = False
    delete_disabled_rooms: Annotated[
        bool,
        Field(
            title="Delete disabled rooms",
            description=inspect.cleandoc(
                """When a room is disabled (see `disable_rooms_when_mapped_authentik_group_disappears`)
                also delete it through the Synapse admin API, rather than only blocking it. This
                destroys the room's history irreversibly — leave `false` unless you are sure."""
            ),
        ),
    ] = False
    make_authentik_superusers_matrix_room_admin: Annotated[
        bool,
        Field(
            title="Authentik superusers become room admins",
            description=inspect.cleandoc(
                """Grant Authentik superusers the Matrix admin power level (100) in the rooms they are
                members of. Takes precedence over `authentik_group_attr_for_matrix_power_level`."""
            ),
        ),
    ] = True
    authentik_group_attr_for_matrix_power_level: Annotated[
        str,
        Field(
            title="Group attribute holding the Matrix power level",
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) holding an integer from 0 to 100. Members
                of that group receive the value as their Matrix power level in the rooms this bot
                manages. When a user's groups disagree, the highest value wins. Superusers promoted
                by `make_authentik_superusers_matrix_room_admin` ignore this attribute."""
            ),
            examples=["matrix-userpowerlevel", "synapse-options.chat-powerlevel"],
        ),
    ] = "chat-systemwide-powerlevel"


class MatrixDynamicRoomSettings(BaseModel):
    """Template for how a Matrix room's identity (alias/name/topic/encryption/create params) is
    derived from its Authentik group. Used as the default for all rooms and overridable per group
    via ``per_authentik_group_pk_matrix_room_settings``."""

    alias_prefix: Annotated[
        str | None,
        Field(
            title="Room alias prefix",
            description=inspect.cleandoc(
                """Prefix prepended to the room's canonical alias localpart, useful to keep bot-managed
                rooms in their own namespace. `null` for no prefix."""
            ),
            examples=["authentik-", "grp-"],
        ),
    ] = None
    matrix_alias_from_authentik_attribute: Annotated[
        str,
        Field(
            title="Group attribute used as the room alias",
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) used as the room's alias localpart. The
                default `pk` is the most stable choice, because it survives a group being renamed —
                an alias derived from the group name would not, and a Matrix alias cannot be changed
                once the room is created."""
            ),
            examples=["pk", "attributes.chatroom_alias"],
        ),
    ] = "pk"
    name_prefix: Annotated[
        str | None,
        Field(
            title="Room name prefix",
            description="Prefix prepended to the room's display name. `null` for no prefix.",
            examples=["[Chat] "],
        ),
    ] = None
    matrix_name_from_authentik_attribute: Annotated[
        str,
        Field(
            title="Group attribute used as the room name",
            description="Authentik group attribute (dotted path) used as the room's display name.",
            examples=["name", "attributes.chatroom_name"],
        ),
    ] = "name"
    topic_prefix: Annotated[
        str | None,
        Field(
            title="Room topic prefix",
            description="Prefix prepended to the room's topic. `null` for no prefix.",
            examples=["Group chat — "],
        ),
    ] = None
    matrix_topic_from_authentik_attribute: Annotated[
        str | None,
        Field(
            title="Group attribute used as the room topic",
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) used as the room's topic. `null` leaves the
                topic unset."""
            ),
            examples=["attributes.chatroom_topic"],
        ),
    ] = "attributes.chatroom_topic"
    end2end_encryption_enabled: Annotated[
        bool,
        Field(
            title="Enable end-to-end encryption",
            description=inspect.cleandoc(
                """Enable end-to-end encryption in the group-mapped Matrix rooms. The bot itself stays
                outside encryption: it writes room *state* (membership, power levels, name, topic),
                which is never encrypted, but it does not read or post message content in encrypted
                rooms. Welcome messages are sent in a separate, unencrypted direct room. Encryption
                cannot be turned off again for a room once it is on."""
            ),
        ),
    ] = True
    default_room_create_params: Annotated[
        dict[str, Any] | None,
        Field(
            title="Room creation parameters",
            description=inspect.cleandoc(
                """Parameters merged into the Matrix `POST /createRoom` call for group rooms — preset,
                visibility, federation, and so on. Only read when a room is actually created; changing
                them later does not rewrite existing rooms. See the Client-Server API
                `POST /createRoom`."""
            ),
            examples=[{"preset": "private_chat", "visibility": "private"}],
        ),
    ] = Field(default_factory=lambda: {"preset": "private_chat", "visibility": "private"})
    matrix_room_create_params_from_authentik_attribute: Annotated[
        str | None,
        Field(
            title="Group attribute holding extra creation parameters",
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) holding a mapping of extra `createRoom`
                parameters, merged over `default_room_create_params` for that group. `null` disables
                per-group overrides."""
            ),
            examples=["attributes.chatroom_params"],
        ),
    ] = "attributes.chatroom_params"
    keep_updating_matrix_attributes_from_authentik: Annotated[
        bool,
        Field(
            title="Keep room name and topic in sync",
            description=inspect.cleandoc(
                """Re-apply the room's name and topic from Authentik on every reconcile, overwriting
                any drift. Disable to let room admins rename rooms in their client and keep the
                change."""
            ),
        ),
    ] = True


class OnbotConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONBOT_", env_nested_delimiter="__")

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Layer the YAML file *under* the environment, so a secret never has to enter the file.

        Sources are consulted in order of descending priority. The YAML file is last, which is what
        lets an operator commit a fully documented config and inject only ``bot_access_token`` and
        the Authentik ``api_key`` through ``ONBOT_*`` variables at runtime.
        """
        sources = [init_settings, env_settings, dotenv_settings, file_secret_settings]
        yaml_file = get_config_file_path()
        if yaml_file is not None:
            sources.append(YamlConfigSettingsSource(settings_cls, yaml_file=yaml_file))
        return tuple(sources)

    log_level: Annotated[
        Literal["INFO", "DEBUG"],
        Field(
            title="Logging verbosity",
            description=inspect.cleandoc(
                """How much the bot logs. `DEBUG` is noisy — it includes every API call it makes — but
                it is the fastest way to see why a user or group is not being picked up while wiring
                the bot up. The `--log-level` command-line flag overrides this."""
            ),
        ),
    ] = "INFO"
    server_tick_rate_sec: Annotated[
        int,
        Field(
            title="Reconcile interval (seconds)",
            description=inspect.cleandoc(
                """How often, in seconds, the bot re-converges Authentik state onto Matrix. Each
                reconcile is idempotent, so this is a safety net that repairs drift rather than the
                only path — onboarding still reacts to live events between ticks. Lower it to react
                to Authentik changes sooner, at the cost of more API calls against both servers."""
            ),
            examples=[20, 300],
        ),
    ] = 20

    synapse_server: Annotated[
        SynapseServer,
        Field(
            title="Synapse server",
            description=inspect.cleandoc(
                """Connection data and credentials for the Matrix client-server and Synapse admin
                APIs. Authenticate with either `bot_access_token` or `oauth2`, not both."""
            ),
        ),
    ]
    authentik_server: Annotated[
        AuthentikServer,
        Field(
            title="Authentik server",
            description=inspect.cleandoc(
                """Connection data and API token for the upstream Authentik identity provider, which
                the bot treats as the single source of truth for users and groups."""
            ),
        ),
    ]

    mas_admin: Annotated[
        MasAdmin | None,
        Field(
            title="Matrix Authentication Service admin API",
            description=inspect.cleandoc(
                """Admin credentials for the Matrix Authentication Service. Required for the
                offboarding module to actually revoke sessions and deactivate accounts on a
                MAS-fronted homeserver: there the Matrix session is owned by MAS, and the Synapse
                admin API cannot terminate it. Leave unset (`null`) on homeservers that do not use
                MAS — the bot then enforces offboarding through the Synapse admin API alone."""
            ),
        ),
    ] = None

    welcome_new_users_messages: Annotated[
        list[str] | None,
        Field(
            title="Welcome messages",
            description=inspect.cleandoc(
                """Messages the bot sends, in order, in the 1:1 welcome direct room it opens with each
                newly onboarded user. Each message is sent at most once per user — they are matched by
                content, so editing a message here re-sends that one message to everyone. `null` or an
                empty list disables the welcome direct room entirely."""
            ),
            examples=[["Welcome aboard! I will invite you to the rooms for your groups."]],
        ),
    ] = Field(
        default_factory=lambda: [
            "Welcome to the company chat. I am the company bot. I will invite you to the groups you "
            "are assigned to. If you have any technical questions write a message to "
            "@admin-person:matrix.company.org.",
            "If you need guidance on how to use this chat have a look at the official documentation: "
            "https://matrix.org/docs/chat_basics/matrix-for-im/ and https://element.io/user-guide",
            "🛑 🔐 The chat software will ask you to set up a 'Security Key Backup'. This is very "
            "important. Save the file in a secure location, otherwise you could lose access to older "
            "encrypted messages later. Please follow the request.",
        ]
    )

    onboarding_room_name: Annotated[
        str,
        Field(
            title="Welcome room name",
            description=inspect.cleandoc(
                """Display name of the 1:1 welcome room the bot opens with each user. The room needs a
                name because the bot joins the user directly instead of waiting for them to accept an
                invitation, and a Matrix client only tags a room as a direct message when its user
                accepted such an invitation — without a name the room would appear in their room list
                as an untitled room. Only read when a room is created; renaming later does not rewrite
                existing rooms."""
            ),
            examples=["Announcements", "Company Chat Bot"],
        ),
    ] = "Announcements"
    onboarding_room_topic: Annotated[
        str,
        Field(
            title="Welcome room topic",
            description=inspect.cleandoc(
                """Matrix topic (tagline) of the 1:1 welcome room. A good place to say where a user
                should turn with questions, since they cannot ask them in this room — it is read-only.
                Only read when a room is created."""
            ),
            examples=["Notices from the onboarding bot. You cannot write here."],
        ),
    ] = "Notices from the onboarding bot. This room is read-only — you cannot write here."
    force_join_onboarding_room: Annotated[
        bool,
        Field(
            title="Force users into the welcome room",
            description=inspect.cleandoc(
                """Join users into their welcome room directly, through the Synapse admin API, instead
                of leaving them an invitation they have to accept. On by default: the room is a notice
                board the bot posts to, so an unaccepted invitation means a user who never receives
                the welcome messages. The join happens exactly once, when the room is created — a user
                who then leaves the room is not dragged back in. Turn this off to send a plain
                invitation instead; the bot also falls back to the invitation when the join fails."""
            ),
        ),
    ] = True

    admin_room: Annotated[
        AdminRoom,
        Field(
            title="Admin control room",
            description=inspect.cleandoc(
                """A Matrix room in which listed administrators can command the bot — most notably
                announcing a message to every user. Disabled by default."""
            ),
        ),
    ] = Field(default_factory=AdminRoom)

    place_onboarding_rooms_in_space: Annotated[
        bool,
        Field(
            title="Put welcome rooms in the space",
            description=inspect.cleandoc(
                """Gather the 1:1 onboarding/welcome rooms under the managed parent space as well. Off
                by default — whether direct rooms belong in a space is a matter of taste. Only applies
                when `create_matrix_rooms_in_a_matrix_space` is enabled."""
            ),
        ),
    ] = False

    sync_authentik_users_with_matrix_rooms: Annotated[
        SyncAuthentikUsersWithMatrix,
        Field(
            title="User synchronisation",
            description=inspect.cleandoc(
                """The core Authentik-to-Matrix sync: which users are considered, how they map onto
                Matrix IDs, and what happens when they are disabled upstream."""
            ),
        ),
    ] = Field(default_factory=SyncAuthentikUsersWithMatrix)
    create_matrix_rooms_in_a_matrix_space: Annotated[
        CreateMatrixRoomsInAMatrixSpace,
        Field(
            title="Parent space",
            description="The designated parent space that collects the Authentik-group rooms.",
        ),
    ] = Field(default_factory=CreateMatrixRoomsInAMatrixSpace)
    sync_matrix_rooms_based_on_authentik_groups: Annotated[
        SyncMatrixRoomsBasedOnAuthentikGroups,
        Field(
            title="Room synchronisation",
            description=inspect.cleandoc(
                """The group-to-room projection: which Authentik groups become Matrix rooms, what
                happens when a group disappears, and how power levels are derived."""
            ),
        ),
    ] = Field(default_factory=SyncMatrixRoomsBasedOnAuthentikGroups)
    matrix_room_default_settings: Annotated[
        MatrixDynamicRoomSettings,
        Field(
            title="Default room settings",
            description=inspect.cleandoc(
                """The room identity template applied to every group room: how its alias, name, topic
                and creation parameters are derived from the Authentik group."""
            ),
        ),
    ] = Field(default_factory=MatrixDynamicRoomSettings)
    per_authentik_group_pk_matrix_room_settings: Annotated[
        dict[str, MatrixDynamicRoomSettings],
        Field(
            title="Per-group room setting overrides",
            description=inspect.cleandoc(
                """Overrides for `matrix_room_default_settings`, keyed by the Authentik group's primary
                key (`pk`). Only the keys you list are overridden; the rest fall back to the defaults.
                Use this to give a single group a different name prefix, or to leave one room
                unencrypted."""
            ),
            examples=[
                {
                    "1120a6e1124f309bbe96c8be5fb09eab": {
                        "name_prefix": "[Public] ",
                        "end2end_encryption_enabled": False,
                    }
                }
            ],
        ),
    ] = Field(default_factory=dict)

    matrix_user_ignore_list: Annotated[
        list[str],
        Field(
            title="Ignored Matrix users",
            description=inspect.cleandoc(
                """Full Matrix IDs the bot never touches: it will not onboard them, invite them,
                change their power level, kick them from a room, or deactivate them. Use it to protect
                accounts that exist only in Matrix — server admins, other bots, bridge users — from a
                sync that would otherwise see them as unknown to Authentik. The bot's own
                `synapse_server.bot_user_id` is always ignored and need not be listed."""
            ),
            examples=[["@admin:company.org", "@root:company.org"]],
        ),
    ] = Field(default_factory=list)
    authentik_user_ignore_list: Annotated[
        list[str],
        Field(
            title="Ignored Authentik users",
            description=inspect.cleandoc(
                """Authentik usernames the bot never syncs into Matrix. Matched against the raw
                `username` field, not against `authentik_username_mapping_attribute`. Use it to keep
                service and break-glass accounts out of chat."""
            ),
            examples=[["admin", "internal_account_alex"]],
        ),
    ] = Field(default_factory=list)
    authentik_group_id_ignore_list: Annotated[
        list[str],
        Field(
            title="Ignored Authentik groups",
            description=inspect.cleandoc(
                """Authentik groups that never become a Matrix room, identified by the group's primary
                key (`pk`). Applied before every other group filter, so a listed group is skipped even
                if it carries the attributes or name prefix that would otherwise select it."""
            ),
            examples=[["1120a6e1124f309bbe96c8be5fb09eab"]],
        ),
    ] = Field(default_factory=list)


def get_config_file_path(*, not_exists_ok: bool = False) -> Path | None:
    """Return the YAML config path from ``ONBOT_CONFIG_FILE_PATH`` (default ``config.yml``)."""
    yaml_file = Path(os.environ.get(CONFIG_FILE_ENV_VAR, "config.yml"))
    if yaml_file.is_file() or not_exists_ok:
        return yaml_file
    return None


def load_config() -> OnbotConfig:
    """Load config from YAML if present; env vars (``ONBOT_*``) override either way.

    Both cases go through ``BaseSettings``: the YAML file, when it exists, is merely the
    lowest-priority source (see :meth:`OnbotConfig.settings_customise_sources`).
    """
    return OnbotConfig()  # type: ignore[call-arg]


def generate_example_config() -> str:
    """Render a YAML document of the default config model (G11.2).

    Required fields that have no default are emitted as ``null`` placeholders so the result is a
    fillable template rather than a validation error.
    """

    # The template must show the *defaults*. Every normal construction path consults the settings
    # sources, so an ambient config.yml or ONBOT_* variable would otherwise leak into the generated
    # example — and from there into the committed config.example.yml. Take the sources away.
    class _DefaultsOnly(OnbotConfig):
        @classmethod
        def settings_customise_sources(
            cls,
            settings_cls: type[BaseSettings],
            init_settings: PydanticBaseSettingsSource,
            env_settings: PydanticBaseSettingsSource,
            dotenv_settings: PydanticBaseSettingsSource,
            file_secret_settings: PydanticBaseSettingsSource,
        ) -> tuple[PydanticBaseSettingsSource, ...]:
            return (init_settings,)

    model = _DefaultsOnly(
        synapse_server=SynapseServer(server_name="", server_url="", bot_user_id="", bot_access_token=""),
        authentik_server=AuthentikServer(url="", api_key=""),
    )
    data = model.model_dump(mode="json")
    for key in ("server_name", "server_url", "bot_user_id", "bot_access_token"):
        data["synapse_server"][key] = None
    data["authentik_server"]["url"] = None
    data["authentik_server"]["api_key"] = None
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
