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
import ipaddress
import os
import re
from pathlib import Path
from typing import Annotated, Any, Literal, get_args
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

CONFIG_FILE_ENV_VAR = "ONBOT_CONFIG_FILE_PATH"


# --- validation building blocks (MNT-03, SEC-11) ------------------------------------------------
#
# Error messages here must name what is expected and must never echo the offending value: the CLI
# prints them verbatim, and some of the values are credentials (RUN-07).


class _StrictModel(BaseModel):
    """Base of every nested config block: an unknown key is an error, not silently ignored.

    A typo such as ``dry_rn: false`` must fail loudly instead of leaving the destructive default in
    place (MNT-03). The top-level model forbids unknown keys as well (see :class:`OnbotConfig`).
    """

    model_config = ConfigDict(extra="forbid")


def _check_http_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        host = parts.hostname
    except ValueError:
        host = None
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not host:
        raise ValueError("must be an http:// or https:// URL with a host, such as https://matrix.company.org")
    return value


_MXID_RE = re.compile(r"^@[^:\s]+:\S+$")


def _check_mxid(value: str) -> str:
    if not _MXID_RE.match(value):
        raise ValueError(
            "must be a full Matrix user ID with the leading @ and the server, such as @alice:company.org"
        )
    return value


def _check_alias_part(value: str) -> str:
    if any(ch in value for ch in "#:") or any(ch.isspace() for ch in value):
        raise ValueError(
            "must be part of an alias localpart only, without '#', ':' or spaces: write myspace for "
            "#myspace:<server_name>"
        )
    return value


def _check_alias_localpart(value: str) -> str:
    if not value:
        raise ValueError(
            "must not be empty: give the alias localpart, such as myspace for #myspace:<server_name>"
        )
    return _check_alias_part(value)


_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9_]([A-Za-z0-9_.-]*[A-Za-z0-9_])?\.?$")


def _check_destination(value: str) -> str:
    value = value.strip()
    try:
        ipaddress.ip_network(value, strict=False)
    except ValueError:
        if not _HOSTNAME_RE.match(value):
            raise ValueError(
                "must be a hostname (without scheme, port or path), an IP address or a CIDR network "
                "such as 10.0.0.0/8"
            ) from None
    return value


LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LOG_LEVELS: tuple[str, ...] = get_args(LogLevel)


# ``synapse_server.oauth2`` (the bot as a MAS client_credentials client on the Synapse APIs) was removed
# after the live test in review session S11: MAS refuses the Matrix API and Synapse admin scopes for that
# grant, and Synapse refuses a client token without them.
OAUTH2_REMOVED_MESSAGE = (
    "synapse_server.oauth2 is no longer supported: MAS does not grant the Matrix API scopes to "
    "client_credentials; set synapse_server.bot_access_token to a compatibility token "
    "(scripts/provision-bot-account.sh)"
)


class MasAdmin(_StrictModel):
    """MAS admin API credentials for lifecycle enforcement (ADR-0005/0006, §7 Q1).

    Under MAS the Matrix token is owned by MAS, so the Synapse admin API cannot end a live
    session; only MAS can (lock, finish sessions, deactivate). When this block is set, the lifecycle
    module enforces lockout through the MAS admin API using an OAuth2 ``client_credentials`` token
    with the ``urn:mas:admin`` scope (the client must be listed in MAS ``policy.data.admin_clients``).
    Leave unset to fall back to the Synapse-admin effectors, which do NOT end MAS sessions and
    therefore refuse to lock or erase when the homeserver turns out to be MAS-backed (LIF-09).
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
        SecretStr,
        Field(
            title="MAS admin client secret",
            description=inspect.cleandoc(
                """Client secret belonging to `client_id`. This credential can lock and deactivate
                any account on the homeserver, so prefer supplying it through the
                `ONBOT_MAS_ADMIN__CLIENT_SECRET` environment variable rather than committing it to
                the config file."""
            ),
            examples=["ONLY_AN_EXAMPLE_SECRET_pMv1kZ8sQ0"],
        ),
    ]
    upstream_subject_attribute: Annotated[
        str | None,
        Field(
            title="Authentik attribute MAS links users by",
            description=inspect.cleandoc(
                """Where the subject (`sub` claim) that Authentik sends to MAS is found on the
                Authentik user object, as a dotted path. With it onbot asks MAS which Matrix account
                is linked to each Authentik user, so a user renamed in Authentik keeps their account
                (D8). It must match the subject mode of the Authentik OAuth2 provider MAS uses:
                `uid` for "Based on the User's hashed ID" (Authentik's default), `pk` for "Based on
                the User's ID", `username` for "Based on the User's username", `email` for "Based on
                the User's Email". Only the hashed ID and the ID survive a rename. `null` turns the
                lookup off; users are then resolved from onbot's recorded bindings and the mapping
                attribute."""
            ),
            examples=["uid", "pk"],
        ),
    ] = "uid"
    upstream_provider_id: Annotated[
        str | None,
        Field(
            title="MAS upstream provider id of Authentik",
            description=inspect.cleandoc(
                """The id (a ULID) of the Authentik provider in the MAS `upstream_oauth2` config.
                Only links of this provider are used. Leave unset when MAS has a single upstream
                provider; with several, an Authentik subject that is linked to different Matrix
                accounts by different providers is treated as a conflict."""
            ),
            examples=["01HFS67GJ145HCM9ZASYS9DC3J"],
        ),
    ] = None

    @field_validator("url")
    @classmethod
    def _v_url(cls, value: str) -> str:
        return _check_http_url(value)


class AdminRoom(_StrictModel):
    """The operator control room (ADR-0010).

    A single Matrix room the bot joins, invites the admins to, and listens in for prefixed commands
    (``!announce``, ``!help``, ``!status``). It is an operator *interface*, not a state source: no
    command here feeds the reconciler's desired state, which is why reading messages does not
    contradict ADR-0002.

    Authorisation is an allowlist, checked against the event sender — never the sender's room power
    level. The power levels are the fence; the allowlist is the gate. The allowlist is the union of
    ``admin_user_ids`` and the members of the Authentik groups in
    ``authentik_group_pks_granting_bot_admin``. An enabled room needs at least one of the two lists
    filled; a room nobody may command would leave the guard's pending batches unconfirmable (D19).
    """

    enabled: Annotated[
        bool,
        Field(
            title="Enable the admin control room",
            description=inspect.cleandoc(
                """Create and listen in a control room where administrators can command the bot. On
                by default, because it is where the bot reports what needs a human (paused mass
                removals, offboarding problems) and where an administrator releases paused removals
                with `!confirm <id>`. It also lets everyone on the admin allowlist send a message to
                every user on the server (`!announce`). An enabled room needs at least one
                administrator in `admin_user_ids` or `authentik_group_pks_granting_bot_admin`, or the
                bot refuses to start. Set this to `false` to run without the room; the bot then only
                logs its reports, the destructive-action guard's pauses can only be lifted by raising
                its limits, and `onbot broadcast` sends announcements from the command line."""
            ),
        ),
    ] = True
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
                when it creates it, and re-checks on every reconcile, so somebody added here or to the
                group later is invited without a restart."""
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
                upstream. Create a group, put people in it, and name it here. While the admin room is
                enabled, this list or `admin_user_ids` must not be empty."""
            ),
            examples=[["1120a6e1124f309bbe96c8be5fb09eab"]],
        ),
    ] = Field(default_factory=list)

    @field_validator("alias")
    @classmethod
    def _v_alias(cls, value: str) -> str:
        return _check_alias_localpart(value)

    @field_validator("admin_user_ids")
    @classmethod
    def _v_admin_user_ids(cls, value: list[str]) -> list[str]:
        return [_check_mxid(v) for v in value]

    @property
    def has_admins(self) -> bool:
        """Whether the configuration names anybody who may command the bot."""
        return bool(self.admin_user_ids or self.authentik_group_pks_granting_bot_admin)


class SynapseServer(_StrictModel):
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
        SecretStr,
        Field(
            title="Bot access token",
            description=inspect.cleandoc(
                """Access token authorising the bot against the Synapse APIs. On a homeserver fronted
                by the Matrix Authentication Service this is a compatibility token, issued with
                `mas-cli manage issue-compatibility-token` (`scripts/provision-bot-account.sh` does
                that for you). Provide the bare token; do not prefix it with `Bearer` (the client
                adds that). Prefer supplying it through the `ONBOT_SYNAPSE_SERVER__BOT_ACCESS_TOKEN`
                environment variable rather than committing it to the config file."""
            ),
            examples=["syt_ONLY_AN_EXAMPLE_TOKEN_sadaw4"],
        ),
    ]
    bot_avatar_url: Annotated[
        str | None,
        Field(
            title="Bot avatar",
            description=inspect.cleandoc(
                """HTTP(S) URL to a picture the bot sets as its own avatar on start. The image is
                downloaded and re-uploaded to the homeserver's media repository, and the profile is
                only rewritten when the picture actually changed, so a restart does not announce
                "changed their profile picture" in every room. `null` leaves the bot's current
                avatar untouched."""
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
    room_version: Annotated[
        str | None,
        Field(
            title="Room version for rooms the bot creates",
            description=inspect.cleandoc(
                """Room version passed to `POST /createRoom` for every room and space the bot
                creates. Leave unset (`null`) — the default — so new rooms inherit the homeserver's
                own default room version, which the Matrix spec says SHOULD now be `12`. Room
                version 12 changes two things the bot depends on: the room creator (the bot) holds an
                infinite power level and is deliberately absent from `m.room.power_levels`, and room
                IDs are a hash with no `:domain` component. The bot is written for that world, so
                pinning a number here is only for two cases: an operator whose Synapse is too old to
                default to a version this bot needs (it requires at least version 8 for the features
                it uses; `restricted` join rules need 8 and `knock_restricted` needs 10), or a test
                that must force a specific version. Do not pin a number to freeze the bot behind the
                ecosystem — prefer upgrading Synapse."""
            ),
            examples=["12", "11"],
        ),
    ] = None

    @model_validator(mode="before")
    @classmethod
    def _oauth2_was_removed(cls, data: Any) -> Any:
        """Name the removed ``oauth2`` block instead of the generic unknown-key error (RUN-11).

        Only the key is looked at, never its values, so no secret reaches the error message.
        """
        if isinstance(data, dict) and "oauth2" in data:
            raise ValueError(OAUTH2_REMOVED_MESSAGE)
        return data

    @field_validator("bot_access_token", mode="before")
    @classmethod
    def _token_not_blank(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("must not be empty: set it to the bot's compatibility token")
        return value

    @model_validator(mode="after")
    def _check_bot_server(self) -> SynapseServer:
        """The bot account lives on ``server_name`` (MNT-03)."""
        bot_server = self.bot_user_id.split(":", 1)[1]
        if bot_server.lower() != self.server_name.lower():
            raise ValueError(
                "bot_user_id must be an account on server_name: the part after ':' must equal server_name"
            )
        return self

    @field_validator("server_url")
    @classmethod
    def _v_server_url(cls, value: str) -> str:
        return _check_http_url(value)

    @field_validator("bot_avatar_url")
    @classmethod
    def _v_bot_avatar_url(cls, value: str | None) -> str | None:
        return None if value is None else _check_http_url(value)

    @field_validator("bot_user_id")
    @classmethod
    def _v_bot_user_id(cls, value: str) -> str:
        return _check_mxid(value)


class AuthentikServer(_StrictModel):
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
        SecretStr,
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

    @field_validator("url")
    @classmethod
    def _v_url(cls, value: str) -> str:
        return _check_http_url(value)


class DeactivateDisabledAuthentikUsersInMatrix(_StrictModel):
    """Lifecycle settings (Phase 5: quarantined, dry-run/audit default; AD-5, ADR-0005)."""

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
                *would* do to the `onbot.lifecycle.audit` channel: no account is locked, unlocked
                or erased and no session is ended. Set `false` to actually perform destructive
                lifecycle actions. Defaults to `true` so destructive offboarding is always opt-in;
                run with the default first and read the audit log before switching it off."""
            ),
        ),
    ] = True
    deactivate_after_n_sec: Annotated[
        int,
        Field(
            title="Grace period before the lock",
            description=inspect.cleandoc(
                """Seconds a user must stay disabled in (or deleted from) Authentik before their
                Matrix account is locked. Despite the name nothing is deactivated at this stage: with
                `mas_admin` the MAS user is locked (no new logins) and its sessions are ended, without
                it the user's Synapse devices are deleted. Reversible: re-enabling the Authentik
                account unlocks it. Within this window a re-enable simply cancels the pending lock.
                Tokens already in use stop working only once Synapse's token cache lapses (about two
                minutes under MAS). The default is 24 hours."""
            ),
            examples=[86400, 3600],
            ge=0,
        ),
    ] = 60 * 60 * 24
    delete_after_n_sec: Annotated[
        int | None,
        Field(
            title="Grace period before erasure",
            description=inspect.cleandoc(
                """Seconds after the lock before the Matrix account is erased, that is deactivated
                for good (irreversible). Counted from the lock, so erasure happens
                `deactivate_after_n_sec + delete_after_n_sec` after the user was first seen disabled
                or deleted. Whether the profile is GDPR-erased as well is
                `gdpr_erase_on_delete`, whether uploaded media goes too is
                `include_user_media_on_delete`. `null` disables erasure entirely and leaves accounts
                locked. The default is 365 days. From
                `destructive_action_guard.confirm_irreversible_actions_from` irreversible actions in
                one pass on, the erasures wait for an administrator's `!confirm <id>`."""
            ),
            examples=[31536000, None],
            ge=0,
        ),
    ] = 60 * 60 * 24 * 365
    gdpr_erase_on_delete: Annotated[
        bool,
        Field(
            title="GDPR-erase on erasure",
            description=inspect.cleandoc(
                """Also GDPR-erase the account when it is erased: Synapse removes the display name
                and avatar and hides the user's messages from users who join rooms later. Applied
                the same way with and without `mas_admin`. Older MAS releases (0.14, for example)
                always GDPR-erase on deactivation; with such a MAS and this option `false` onbot
                refuses the erasure (logged as FAILED) instead of erasing more than configured."""
            ),
        ),
    ] = False
    include_user_media_on_delete: Annotated[
        bool,
        Field(
            title="Delete user media on erasure",
            description=inspect.cleandoc(
                """Also delete the media the user uploaded when their account is erased. Useful to
                honour data-protection requests, but the media is unrecoverable and may still be
                referenced by messages in rooms the user posted in. Large uploads are deleted over
                several passes until none is left."""
            ),
        ),
    ] = False


class SyncAuthentikUsersWithMatrix(_StrictModel):
    """How Authentik users/groups are projected into Matrix room membership (the core sync)."""

    enabled: Annotated[
        bool,
        Field(
            title="Enable user synchronisation",
            description=inspect.cleandoc(
                """Master switch for projecting Authentik group membership into Matrix rooms.

                With `false` the bot does not manage membership at all: it maps no users, so it
                joins nobody, kicks nobody (also with
                `kick_matrix_room_members_not_in_mapped_authentik_group_anymore`), leaves power
                levels and the space's membership alone, and no welcome messages are sent (only
                mapped users are welcomed, and broadcasts reach only them).
                Turning it off never empties a room. The rooms themselves still converge (created,
                named, topic, lobby join rule, avatars), and offboarding
                (`deactivate_disabled_authentik_users_in_matrix`) runs on its own switch, with the
                same scope filters below."""
            ),
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
                never match the accounts they own. It is only asked for users onbot has never seen:
                with `mas_admin` MAS says which account a user owns, and once a user was seen with
                an account onbot records that binding and keeps it when the attribute changes (a
                rename). Such a change is reported in the admin room."""
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
                deleted. Defaults to an audit-only dry run, see the `dry_run` field below."""
            ),
        ),
    ] = Field(default_factory=DeactivateDisabledAuthentikUsersInMatrix)


class CreateMatrixSpaceIfNotExists(_StrictModel):
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

    @field_validator("avatar_url")
    @classmethod
    def _v_avatar_url(cls, value: str | None) -> str | None:
        return None if value is None else _check_http_url(value)


class CreateMatrixRoomsInAMatrixSpace(_StrictModel):
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

    @field_validator("alias")
    @classmethod
    def _v_alias(cls, value: str) -> str:
        return _check_alias_localpart(value)


class SyncMatrixRoomsBasedOnAuthentikGroups(_StrictModel):
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
                destroys the room's history irreversibly: leave `false` unless you are sure. A room is
                deleted only in a pass that also blocks it and kicks its members, and from
                `destructive_action_guard.confirm_irreversible_actions_from` irreversible actions in
                one pass on, the deletions wait for an administrator's `!confirm <id>`."""
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


class MatrixDynamicRoomSettings(_StrictModel):
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
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) used as the room's display name. When the
                attribute is absent or empty the group's own `name` is used, so a group only needs
                `chatroom_name` when it wants a room name that differs from the group name. Set this
                to `name` to ignore the attribute entirely."""
            ),
            examples=["attributes.chatroom_name", "name"],
        ),
    ] = "attributes.chatroom_name"
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
                """Authentik group attribute (dotted path) holding extra `createRoom` parameters,
                merged over `default_room_create_params` for that group. The attribute's value must be
                a *string* containing JSON, because Authentik stores attributes as YAML and a mapping
                written there is rejected with a warning. `null` disables per-group overrides.

                **Niche.** This is a raw escape hatch into the Matrix API and most deployments never
                set the attribute on a single group. Shape every room with
                `default_room_create_params` instead, and reach for this only when exactly one group
                must differ from the rest. The values are unvalidated, read once at room creation and
                never again, and some of them (federation, encryption, room version) cannot be undone
                for the life of that room. See docs/authentik-groups.md."""
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
    visitor_lobby_enabled: Annotated[
        bool,
        Field(
            title="Open a visitor lobby beside the group room",
            description=inspect.cleandoc(
                """Maintain a second, open room — a lobby — beside each private group room. Every
                member of the parent space can find the lobby in the space listing and join it of
                their own accord, and nobody is ever kicked from it, so it is a front door in front of
                the closed group room. The group room itself is untouched: same members, same history,
                same privacy. Off by default — a lobby is a deliberate decision, not a migration.

                Requires the parent space (`create_matrix_rooms_in_a_matrix_space`), because a lobby is
                joinable precisely by space members and to nobody else; enabling a lobby without a
                space is rejected at startup. A single group can opt in without a config change through
                `matrix_room_visitor_lobby_from_authentik_attribute`."""
            ),
        ),
    ] = False
    visitor_lobby_name_suffix: Annotated[
        str,
        Field(
            title="Lobby name suffix",
            description=inspect.cleandoc(
                """Appended to the group room's name to name its lobby — `Düsseldorf` becomes
                `Düsseldorf (Lobby)`. The suffix is the whole user-facing explanation of what the
                second room is, so it should say *this is a door*, not *these are more people*.
                Alternatives worth a deliberate choice: ` (Foyer)` (reads naturally to a
                German-speaking org), ` (Open)` (shortest, states the property not the metaphor), or
                ` & Guests` (if the room should feel like the group hosting). ` & Friends` was
                rejected: the ampersand reads as a statement about membership."""
            ),
            examples=[" (Lobby)", " (Foyer)", " (Open)"],
        ),
    ] = " (Lobby)"
    visitor_lobby_alias_suffix: Annotated[
        str,
        Field(
            title="Lobby alias suffix",
            description=inspect.cleandoc(
                """Appended to the group room's alias localpart to form the lobby's alias —
                `#duesseldorf` gets a `#duesseldorf-lobby` beside it. Unlike the group alias, dashes
                here are kept. A Matrix alias cannot be changed after the room is created, so changing
                this later makes the bot build a second, empty lobby rather than rename the first."""
            ),
            examples=["-lobby", "-foyer", "-open"],
        ),
    ] = "-lobby"
    visitor_lobby_topic_template: Annotated[
        str,
        Field(
            title="Lobby topic template",
            description=inspect.cleandoc(
                """Topic set on the lobby, with `{name}` replaced by the group room's name. State the
                arrangement in the one place every visitor looks, so a visitor who reads it never
                wonders why the room is quiet or which room to post in."""
            ),
            examples=[
                "Open lobby for {name} — anyone in the space may join. The group's working room is private."
            ],
        ),
    ] = "Open lobby for {name} — anyone in the space may join. The group's working room is private."
    visitor_lobby_end2end_encryption_enabled: Annotated[
        bool,
        Field(
            title="Encrypt the lobby",
            description=inspect.cleandoc(
                """Enable end-to-end encryption in the lobby. Off by default, and deliberately weaker
                than the group room's own encryption default: a lobby is open to the whole space by
                construction, so encryption buys it little, while it guarantees every visitor's first
                impression is a screen of `unable to decrypt`. Encryption cannot be turned off again
                once a room has it, so this default is chosen for you to be able to change it before
                the lobby exists rather than after."""
            ),
        ),
    ] = False
    visitor_lobby_inject_group_members: Annotated[
        bool,
        Field(
            title="Seed the lobby with the group's members",
            description=inspect.cleandoc(
                """Join the group's members into the lobby as well, so a visitor who walks in finds
                somebody there. On by default: an empty lobby is a dead lobby. This is a social bet —
                it works when the group room is where the group *works* and the lobby is where the
                group is *reachable*, and it fails, producing two half-dead rooms and doubled
                notifications, when both are general-purpose. Turn it off for a large group that would
                rather staff an empty lobby deliberately. Visitors are never injected anywhere — they
                joined on purpose — and members are only ever added, never kicked."""
            ),
        ),
    ] = True
    matrix_room_visitor_lobby_from_authentik_attribute: Annotated[
        str | None,
        Field(
            title="Group attribute that opts a group into a lobby",
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) holding a boolean that turns the lobby on or
                off for that one group, overriding `visitor_lobby_enabled`. This lets whoever owns the
                group in Authentik open a lobby without a config deploy. A value that is not a boolean
                is ignored with a warning and the configured default applies. `null` disables the
                per-group override, leaving `visitor_lobby_enabled` in sole charge."""
            ),
            examples=["attributes.chatroom_visitor_lobby"],
        ),
    ] = "attributes.chatroom_visitor_lobby"

    @field_validator("alias_prefix")
    @classmethod
    def _v_alias_prefix(cls, value: str | None) -> str | None:
        return None if value is None else _check_alias_part(value)

    @field_validator("visitor_lobby_alias_suffix")
    @classmethod
    def _v_visitor_lobby_alias_suffix(cls, value: str) -> str:
        return _check_alias_localpart(value)


class AvatarFetch(_StrictModel):
    """Limits for downloading avatar images (SEC-03, decision D11).

    Room avatar URLs come from Authentik group attributes, so they are chosen by whoever may edit a
    group. The download therefore refuses internal destinations unless released here, and is capped
    in size and time. Enforced by :mod:`onbot.safe_fetch`.
    """

    allowed_internal_destinations: Annotated[
        list[str],
        Field(
            title="Internal hosts avatars may be downloaded from",
            description=inspect.cleandoc(
                """Avatar images (the bot's, the space's and every group room's) are only downloaded
                from public internet addresses. Private, loopback, link-local, multicast and other
                special-purpose addresses are refused, because a room avatar URL is set by whoever
                may edit an Authentik group, and the bot would otherwise fetch internal endpoints
                (cloud metadata, MAS, Authentik) and publish the response as an image in the room.

                List the exceptions here: a hostname (matched exactly, whatever it resolves to), an
                IP address, or a network in CIDR notation. Write no scheme, port or path. Redirects
                are checked against the same rules on every hop. Environment proxy settings are not
                used for these downloads."""
            ),
            examples=[["assets.internal.company.org", "10.20.0.0/16"]],
        ),
    ] = Field(default_factory=list)
    max_size_bytes: Annotated[
        int,
        Field(
            title="Maximum avatar size (bytes)",
            description=inspect.cleandoc(
                """Largest avatar image the bot downloads. A larger file is refused and the download
                is stopped as soon as the limit is crossed. The default is 5 MiB."""
            ),
            examples=[5242880],
            gt=0,
        ),
    ] = 5 * 1024 * 1024
    timeout_sec: Annotated[
        float,
        Field(
            title="Avatar download deadline (seconds)",
            description=inspect.cleandoc(
                """Time limit for one avatar download as a whole, redirects included. A server that
                sends its response too slowly is cut off when it runs out, so it cannot hold up the
                reconcile pass."""
            ),
            examples=[30],
            gt=0,
        ),
    ] = 30.0

    @field_validator("allowed_internal_destinations")
    @classmethod
    def _v_allowed_internal_destinations(cls, value: list[str]) -> list[str]:
        return [_check_destination(v) for v in value]


class DestructiveActionGuard(_StrictModel):
    """Limits of the destructive-action guard (D4 layers 1 to 3, D13; enforced by :mod:`onbot.guard`).

    Layer 1 compares the pass's input counts with the last known good ones, layer 2 counts the
    reversible destructive actions of one pass, layer 3 the irreversible ones. Each pauses the
    destructive part of the pass into a batch that an admin releases with ``!confirm <id>`` in the
    admin control room.
    """

    max_drop_percent: Annotated[
        float,
        Field(
            title="Largest plausible drop in Authentik's answer (percent)",
            description=inspect.cleandoc(
                """Before a pass removes anybody, the bot compares what Authentik returned with the
                last answer it trusted: the number of active users in the sync scope and the number
                of groups mapped to rooms. If either dropped by more than this percentage, or came
                back empty, the bot treats the answer as implausible (an API token that lost its
                permissions, a broken filter). Fewer users pauses every destructive action of the
                pass: kicks, power-level withdrawals, room teardowns and deletions, account locks and
                erasures. Fewer groups pauses what happens in rooms (kicks, withdrawals, teardowns,
                deletions); locks and
                erasures do not depend on the groups. Rooms are still created and users still added
                and welcomed.

                The paused actions are reported in the admin control room as a batch with a short id,
                and an administrator releases exactly that batch with `!confirm <id>`. After that,
                the new numbers count as normal. The comparison moves with every normal pass, so
                removing people a few at a time never trips it. `100` switches the percentage check
                off; an empty answer still needs a confirmation."""
            ),
            examples=[20, 50],
            gt=0,
            le=100,
        ),
    ] = 20.0
    max_reversible_actions_per_pass: Annotated[
        int,
        Field(
            title="Most removals in one pass without confirmation",
            description=inspect.cleandoc(
                """How many reversible destructive actions one pass may carry out on its own: kicks
                from group rooms, account locks (offboarding), teardowns of rooms whose group
                disappeared, and withdrawals of power levels the bot granted. A pass that would do
                more pauses all of them and reports them in the admin control room as a batch for an
                administrator to release with `!confirm <id>`. This also covers switching
                offboarding's `dry_run` off after a long dry run, when every user marked meanwhile
                would be locked at once."""
            ),
            examples=[10, 50],
            ge=0,
        ),
    ] = 10
    confirmation_expiry_sec: Annotated[
        int,
        Field(
            title="Time to confirm a paused batch (seconds)",
            description=inspect.cleandoc(
                """How long a paused batch waits for `!confirm <id>` or `!reject <id>` before it
                expires. Its actions stay paused meanwhile. If they still apply after it expired, a
                later pass proposes them again under a new id. A rejected batch ends at this expiry at
                the latest (earlier when the condition that created it is gone); its remaining
                actions are then judged again. The default is 72 hours."""
            ),
            examples=[259200],
            gt=0,
        ),
    ] = 72 * 60 * 60
    confirm_irreversible_actions_from: Annotated[
        int,
        Field(
            title="Irreversible actions in one pass that need a confirmation",
            description=inspect.cleandoc(
                """From how many irreversible actions in one pass on the bot asks first: erasing
                accounts (offboarding's `delete_after_n_sec`) and deleting rooms
                (`delete_disabled_rooms`). A pass with this many or more pauses all of them and
                reports them in the admin control room as a batch for an administrator to release with
                `!confirm <id>`; fewer run on their own. Locking an account, kicking members and
                closing a room are reversible and count under `max_reversible_actions_per_pass`
                instead. The media deletion that continues an erasure over later passes needs no new
                confirmation. `1` asks for every irreversible action."""
            ),
            examples=[3, 1],
            ge=1,
        ),
    ] = 3


class WelcomeMessage(_StrictModel):
    """A welcome message with a stable id, so its text can be corrected without re-sending it (ONB-18)."""

    id: Annotated[
        str,
        Field(
            title="Message id",
            description=inspect.cleandoc(
                """Identifies the message in the per-user bookkeeping instead of its text. As long as
                the id stays the same, editing the text sends nothing again; a new id sends the
                message to every onboarded user once more. Ids must be unique in the list."""
            ),
            examples=["intro"],
            min_length=1,
        ),
    ]
    text: Annotated[
        str,
        Field(
            title="Message text",
            description="The Markdown text, with the same placeholders as a plain list entry.",
            examples=["Welcome aboard, {user_id}!"],
        ),
    ]


class OnbotConfig(BaseSettings):
    # Unknown top-level keys fail as well (pydantic-settings' default, spelled out here). The env
    # source only looks up ONBOT_* names that belong to a field, so harness or runtime variables
    # such as ONBOT_CONFIG_FILE_PATH, ONBOT_HEARTBEAT_FILE or ONBOT_ITEST_* never trip it.
    model_config = SettingsConfigDict(env_prefix="ONBOT_", env_nested_delimiter="__", extra="forbid")

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
        LogLevel,
        Field(
            title="Logging verbosity",
            description=inspect.cleandoc(
                """How much the bot logs. `DEBUG` is noisy — it includes every API call it makes — but
                it is the fastest way to see why a user or group is not being picked up while wiring
                the bot up. `WARNING` keeps a quiet production log. The `--log-level` command-line
                flag overrides this."""
            ),
        ),
    ] = "INFO"

    @field_validator("matrix_user_ignore_list")
    @classmethod
    def _v_matrix_user_ignore_list(cls, value: list[str]) -> list[str]:
        return [_check_mxid(v) for v in value]

    @field_validator("welcome_new_users_messages")
    @classmethod
    def _v_welcome_message_ids(
        cls, value: list[str | WelcomeMessage] | None
    ) -> list[str | WelcomeMessage] | None:
        ids = [m.id for m in value or [] if isinstance(m, WelcomeMessage)]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise ValueError(f"welcome message ids must be unique; repeated: {', '.join(duplicates)}")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _log_level_upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value

    server_tick_rate_sec: Annotated[
        int,
        Field(
            title="Reconcile interval (seconds)",
            description=inspect.cleandoc(
                """How often, in seconds, the bot re-converges Authentik state onto Matrix. A
                reconcile reads the whole Matrix side — every managed room's members, power levels
                and state — so it is the expensive operation, and this is the interval that costs
                Synapse traffic.

                It does not set how quickly new users are onboarded. A reconcile also runs on demand
                whenever `authentik_poll_rate_sec` notices Authentik has changed, so this is a safety
                net that repairs drift somebody caused *inside* Matrix (a manually kicked member, an
                edited power level). Minutes, not seconds, is the right order of magnitude."""
            ),
            examples=[300, 900],
            gt=0,
        ),
    ] = 300
    authentik_poll_rate_sec: Annotated[
        int,
        Field(
            title="Authentik poll interval (seconds)",
            description=inspect.cleandoc(
                """How often, in seconds, the bot asks Authentik whether anything changed — a new
                user, a group membership, a renamed room group. When something did, it runs a
                reconcile immediately; when nothing did, it does nothing at all.

                This is what sets onboarding latency, and it is cheap: two Authentik requests per
                poll and zero against Synapse. Set it to `0` to disable the poll entirely, in which
                case new users wait for the next `server_tick_rate_sec` reconcile."""
            ),
            examples=[15, 60, 0],
            ge=0,
        ),
    ] = 15

    synapse_server: Annotated[
        SynapseServer,
        Field(
            title="Synapse server",
            description=inspect.cleandoc(
                """Connection data and credentials for the Matrix client-server and Synapse admin
                APIs. The bot authenticates with `bot_access_token`."""
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
                offboarding module to actually lock users out, end their sessions and erase accounts
                on a MAS-fronted homeserver: there the Matrix session is owned by MAS, and the
                Synapse admin API cannot end it. Ending sessions and erasing without GDPR erasure
                need a MAS whose admin API offers them (0.14 does not); the startup log lists what
                the configured MAS can do. Leave unset (`null`) on homeservers that do not use MAS:
                the bot then enforces offboarding through the Synapse admin API alone. If the
                homeserver turns out to delegate logins to MAS, the bot refuses to lock or erase
                anybody (the Synapse admin API would leave every MAS session valid), logs that at
                ERROR and reports it in the admin room; onboarding and rooms keep working."""
            ),
        ),
    ] = None

    welcome_new_users_messages: Annotated[
        list[str | WelcomeMessage] | None,
        Field(
            title="Welcome messages",
            description=inspect.cleandoc(
                """Messages the bot sends, in order, in the 1:1 welcome direct room it opens with each
                user the reconciler maps (so it needs `sync_authentik_users_with_matrix_rooms`).
                Each message is sent at most once per user. A plain entry is matched by its text:
                **editing it re-sends that one message to every onboarded user** (whitespace at line
                ends and blank lines around the text do not count). To correct a message without
                re-sending it, give it an id: `{id: intro, text: "..."}` is matched by the id, so its
                text can change freely. Turning a plain entry into one with an id sends nothing
                again. `null` or an empty list disables the welcome direct room entirely.

                Each message is **Markdown** (CommonMark): `[text](url)`, `**bold**`, `*italic*`,
                `` `code` ``, lists, headings and quotes all render in the client, and a bare URL
                becomes a link on its own. Embedded HTML is not passed through.

                A message may also use these placeholders, filled in as it is sent:

                - `{space_link}` — a `matrix.to` link to the parent space, ready to put in
                  `[our space]({space_link})`
                - `{space_alias}` — the space's alias, e.g. `#myspace:matrix.company.org`
                - `{space_name}` — the space's display name
                - `{user_id}` — the recipient's Matrix ID

                The three space placeholders need `create_matrix_rooms_in_a_matrix_space` to be
                enabled; a message using one while it is disabled is held back (and logged) rather
                than sent with a gap in it. Any other `{...}` is left as written."""
            ),
            examples=[
                [
                    "Welcome aboard! I will invite you to the rooms for your groups.",
                    "Everything else lives in **[{space_name}]({space_link})** — have a look around.",
                ],
                [
                    {
                        "id": "intro",
                        "text": "Welcome aboard! I will invite you to the rooms for your groups.",
                    },
                ],
            ],
        ),
    ] = Field(
        default_factory=lambda: list[str | WelcomeMessage](
            [
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
                """A Matrix room in which listed administrators command the bot (confirm paused
                removals, announce a message to every user) and read its reports. Enabled by default;
                it needs at least one administrator, or `enabled: false`."""
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

    global_room_admins: Annotated[
        list[str],
        Field(
            title="Global room admins",
            description=inspect.cleandoc(
                """Authentik usernames that are seeded into **every** group room and visitor lobby the
                bot manages, and into the parent space, regardless of their Authentik group
                membership. Each one is held at power level 100 (room admin) in those rooms, so they
                can read, moderate and repair any room without being a member of the group behind it.
                In rooms older than room version 12 the bot only grants up to one below its own level
                (99 when the bot holds 100), so that it can still demote or remove the account later;
                such an admin can moderate but cannot change the bot's own level.

                Matched against the raw `username` field, exactly like `authentik_user_ignore_list`,
                not against `authentik_username_mapping_attribute` (that attribute is still what maps
                the account to its Matrix ID once the user is found).

                These accounts are never kicked by the membership sync, so they stay put even with
                `kick_matrix_room_members_not_in_mapped_authentik_group_anymore` enabled. They are
                *not* granted the right to command the bot: that is a separate allowlist,
                `admin_room.admin_user_ids`.

                Four kinds of entry are dropped rather than obeyed, each with a warning, because a
                room admin everywhere is too much power to hand out on a guess: a username that does
                not exist in Authentik, one whose account is inactive, one that is also on
                `authentik_user_ignore_list`, and one with no Matrix account yet (the bot never
                creates accounts, so an admin who has not logged in once cannot be joined anywhere).
                A disabled Authentik account listed here is still offboarded normally by
                `deactivate_disabled_authentik_users_in_matrix`, being named here does not exempt it.

                **This bypasses the group model.** Every private room becomes readable by these
                accounts, including rooms whose members expect the group to be the boundary. Leave it
                empty unless somebody genuinely needs that."""
            ),
            examples=[["alex.mueller"], ["ops-lead", "compliance"]],
        ),
    ] = Field(default_factory=list)

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

    avatar_fetch: Annotated[
        AvatarFetch,
        Field(
            title="Avatar downloads",
            description=inspect.cleandoc(
                """Where avatar images may be downloaded from, and how large and slow such a
                download may be. Applies to `synapse_server.bot_avatar_url`, the space avatar and
                every group room avatar."""
            ),
        ),
    ] = Field(default_factory=AvatarFetch)

    destructive_action_guard: Annotated[
        DestructiveActionGuard,
        Field(
            title="Guard against mass removals",
            description=inspect.cleandoc(
                """When a pass looks like it would remove far more than usual, the bot pauses the
                removals and asks an administrator in the admin control room (`!pending`,
                `!confirm <id>`, `!reject <id>`) instead of carrying them out. Without the admin
                control room the paused actions are only logged; raise these limits to let them
                through."""
            ),
        ),
    ] = Field(default_factory=DestructiveActionGuard)

    @model_validator(mode="after")
    def _no_settings_without_effect(self) -> OnbotConfig:
        """Reject switches that cannot take effect because the switch they depend on is off (MNT-03)."""
        rooms = self.sync_matrix_rooms_based_on_authentik_groups
        if rooms.delete_disabled_rooms and not rooms.disable_rooms_when_mapped_authentik_group_disappears:
            raise ValueError(
                "sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms only acts on rooms "
                "that disable_rooms_when_mapped_authentik_group_disappears disabled: enable that too, "
                "or set delete_disabled_rooms to false"
            )
        if self.place_onboarding_rooms_in_space and not self.create_matrix_rooms_in_a_matrix_space.enabled:
            raise ValueError(
                "place_onboarding_rooms_in_space requires create_matrix_rooms_in_a_matrix_space to be "
                "enabled: there is no space to place the rooms in"
            )
        return self

    @model_validator(mode="after")
    def _admin_room_needs_an_admin(self) -> OnbotConfig:
        """An enabled control room nobody may command is a misconfiguration, not a default (D19).

        Checked here rather than on :class:`AdminRoom`, so the default ``AdminRoom()`` (enabled, no
        admins yet) can still be built for the example config.
        """
        if self.admin_room.enabled and not self.admin_room.has_admins:
            raise ValueError(
                "the admin room is enabled (the default) but nobody may command the bot: set "
                "admin_room.admin_user_ids or admin_room.authentik_group_pks_granting_bot_admin, or set "
                "admin_room.enabled: false to run without an admin room"
            )
        return self

    @model_validator(mode="after")
    def _visitor_lobby_requires_a_space(self) -> OnbotConfig:
        """A lobby's ``restricted`` join rule needs a space to be restricted *to*.

        Reject a lobby enabled — in the defaults or in any per-group override — while the parent space
        is switched off, rather than silently create a lobby that stays invite-only and joinable by
        nobody. The per-group Authentik attribute can still enable a lobby at runtime; the engine
        skips lobby creation when no space is resolved, so that case degrades safely rather than at
        startup.
        """
        if self.create_matrix_rooms_in_a_matrix_space.enabled:
            return self
        lobby_enabled = self.matrix_room_default_settings.visitor_lobby_enabled or any(
            s.visitor_lobby_enabled for s in self.per_authentik_group_pk_matrix_room_settings.values()
        )
        if lobby_enabled:
            raise ValueError(
                "visitor_lobby_enabled requires create_matrix_rooms_in_a_matrix_space to be enabled: "
                "a lobby is joinable precisely by parent-space members, so there must be a space."
            )
        return self


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
    # example, and from there into the committed config.example.yml. model_construct consults no
    # source and runs no validation, which the placeholders (empty, so not valid values) also need.
    model = OnbotConfig.model_construct(
        synapse_server=SynapseServer.model_construct(
            server_name="", server_url="", bot_user_id="", bot_access_token=SecretStr("")
        ),
        authentik_server=AuthentikServer.model_construct(url="", api_key=SecretStr("")),
    )
    data = model.model_dump(mode="json")
    for key in ("server_name", "server_url", "bot_user_id", "bot_access_token"):
        data["synapse_server"][key] = None
    data["authentik_server"]["url"] = None
    data["authentik_server"]["api_key"] = None
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
