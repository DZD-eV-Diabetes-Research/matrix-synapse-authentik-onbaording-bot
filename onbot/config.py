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

Loading: :func:`load_config` reads the YAML at ``ONBOT_CONFIG_FILE_PATH`` (env overrides still apply
via the ``ONBOT_`` prefix and ``__`` nesting delimiter); :func:`generate_example_config` dumps the
default model to YAML (G11.2).
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path, PurePath
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_FILE_ENV_VAR = "ONBOT_CONFIG_FILE_PATH"


class SynapseServer(BaseModel):
    server_name: Annotated[
        str,
        Field(
            description=inspect.cleandoc(
                """Synapse's public facing domain
                https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name
                This is not necessarily the domain under which the Synapse server is reachable."""
            ),
            examples=["company.org"],
        ),
    ]
    server_url: Annotated[
        str,
        Field(
            description=inspect.cleandoc(
                """URL to reach the Synapse server. This can (and should) be an internal URL, so the
                Synapse admin API need not be public. The bot works with a public URL too."""
            ),
            examples=["https://internal.matrix"],
        ),
    ]
    bot_user_id: Annotated[
        str,
        Field(
            description="Full Matrix user ID of an existing account; the bot acts as this user.",
            examples=["@welcome-bot:company.org"],
        ),
    ]
    bot_access_token: Annotated[
        str,
        Field(
            description=inspect.cleandoc(
                """Access token authorising the bot against the Synapse APIs. Under MAS this is a
                compatibility token issued via ``mas-cli manage issue-compatibility-token`` (AD-6).
                Provide the bare token; do not prefix it with ``Bearer`` (the client adds that)."""
            ),
            examples=["syt_ONLY_AN_EXAMPLE_TOKEN_sadaw4"],
        ),
    ]
    bot_avatar_url: Annotated[
        str | None,
        Field(
            description="HTTP URL to a picture; the bot sets it as its own avatar on start.",
            examples=["https://sillyimages.com/face.png"],
        ),
    ] = None
    admin_api_path: Annotated[
        str,
        Field(
            description="Sub-path the Synapse admin API is served under. Keep the default if unsure.",
            examples=["_synapse/admin/"],
        ),
    ] = "_synapse/admin/"


class AuthentikServer(BaseModel):
    url: Annotated[
        str,
        Field(
            description="URL to reach your Authentik server.",
            examples=["https://authentik.company.org/"],
        ),
    ]
    api_key: Annotated[
        str,
        Field(
            description=inspect.cleandoc(
                """API token for your Authentik server. Generate one at
                ``https://<authentik>/if/admin/#/core/tokens``. Provide the bare token; the client
                adds the ``Bearer`` prefix."""
            ),
            examples=["yEl4tFqeIBQwoHAd9hajmkm2PBjSAirY_THIS_IS_JUST_AN_EXAMPLE_i57e"],
        ),
    ]


class DeactivateDisabledAuthentikUsersInMatrix(BaseModel):
    """Lifecycle settings (Phase 5 — quarantined, dry-run/audit default; AD-5)."""

    enabled: Annotated[
        bool,
        Field(
            description="Lock out Matrix accounts whose Authentik account was disabled/deleted.",
        ),
    ] = True
    deactivate_after_n_sec: Annotated[
        int,
        Field(
            description="Cooldown before deactivation, to absorb accidental upstream disables.",
        ),
    ] = 60 * 60 * 24
    delete_after_n_sec: Annotated[
        int | None,
        Field(
            description="Further cooldown before erase/delete. ``null`` disables deletion.",
        ),
    ] = 60 * 60 * 24 * 365
    include_user_media_on_delete: Annotated[
        bool,
        Field(
            description="Also delete media uploaded by the user on account deletion (data protection).",
        ),
    ] = False


class SyncAuthentikUsersWithMatrix(BaseModel):
    enabled: bool = True
    authentik_username_mapping_attribute: Annotated[
        str,
        Field(
            description=inspect.cleandoc(
                """Source of the localpart of the Matrix ID (``@<localpart>:server``). A dotted path
                into the Authentik user object (e.g. ``username`` or ``attributes.matrix_name``).
                Under MAS this MUST agree with the localpart template MAS derives from the upstream
                claim, or provisioned users will not match (AD-6)."""
            ),
        ),
    ] = "username"
    kick_matrix_room_members_not_in_mapped_authentik_group_anymore: bool = True
    sync_only_users_in_authentik_pathes: list[str] | None = None
    sync_only_users_with_authentik_attributes: dict[str, Any] | None = None
    sync_only_users_of_groups_with_id: list[str] | None = None
    deactivate_disabled_authentik_users_in_matrix: DeactivateDisabledAuthentikUsersInMatrix = Field(
        default_factory=DeactivateDisabledAuthentikUsersInMatrix
    )


class CreateMatrixSpaceIfNotExists(BaseModel):
    enabled: bool = Field(
        default=True,
        description="Create the parent space if it does not exist.",
    )
    name: str = Field(default="OnBotSpace", description="Display name of the space.")
    topic: str = Field(
        default="Space for authentik group rooms",
        description="Matrix topic (tagline) for the space.",
    )
    avatar_url: str | None = Field(
        default=None,
        description="HTTP(S) URL to a picture used as the space avatar.",
    )
    space_params: dict[str, Any] = Field(
        default_factory=lambda: {"preset": "private_chat", "visibility": "private"},
        description="Extra parameters passed to the space-creation call.",
    )


class CreateMatrixRoomsInAMatrixSpace(BaseModel):
    enabled: bool = Field(
        default=True,
        description="Gather all Authentik-group rooms under a dedicated parent space.",
    )
    alias: str = Field(
        default="OnBotSpace",
        description='Localpart of the space canonical alias (e.g. "#<alias>:server").',
        examples=["myspace", "companyspace"],
    )
    create_matrix_space_if_not_exists: CreateMatrixSpaceIfNotExists = Field(
        default_factory=CreateMatrixSpaceIfNotExists,
        description="Whether/how the parent space is created.",
    )


class SyncMatrixRoomsBasedOnAuthentikGroups(BaseModel):
    enabled: bool = True
    only_for_children_of_groups_with_uid: list[str] | None = None
    only_groups_with_attributes: Annotated[
        dict[str, Any] | None,
        Field(
            description=inspect.cleandoc(
                """Only mirror Authentik groups carrying these custom attributes. If unset, all
                groups become rooms. https://goauthentik.io/docs/user-group/group#attributes"""
            ),
            examples=[{"is_chatroom": True}],
        ),
    ] = None
    room_avatar_url_attribute: Annotated[
        str | None,
        Field(
            description="Authentik group attribute holding a URL used as the room avatar.",
            examples=["chatroom_avatar_url"],
        ),
    ] = "chatroom_avatar_url"
    only_for_groupnames_starting_with: str | None = None
    disable_rooms_when_mapped_authentik_group_disappears: Annotated[
        bool,
        Field(
            description=inspect.cleandoc(
                """If a mapped Authentik group disappears (deleted or lost its matching attribute),
                kick all members and block the room."""
            ),
        ),
    ] = False
    delete_disabled_rooms: bool = False
    make_authentik_superusers_matrix_room_admin: bool = True
    authentik_group_attr_for_matrix_power_level: Annotated[
        str,
        Field(
            description=inspect.cleandoc(
                """Authentik group attribute (dotted path) holding an integer 0-100. Members of the
                group get that Matrix power level in their onbot rooms. Superusers made admin (see
                ``make_authentik_superusers_matrix_room_admin``) ignore this. On conflicting values
                across multiple group memberships the highest wins."""
            ),
            examples=["matrix-userpowerlevel", "synapse-options.chat-powerlevel"],
        ),
    ] = "chat-systemwide-powerlevel"


class MatrixDynamicRoomSettings(BaseModel):
    alias_prefix: str | None = None
    matrix_alias_from_authentik_attribute: str = "pk"
    name_prefix: str | None = None
    matrix_name_from_authentik_attribute: str = "name"
    topic_prefix: str | None = None
    matrix_topic_from_authentik_attribute: str | None = "attributes.chatroom_topic"
    end2end_encryption_enabled: Annotated[
        bool,
        Field(description="Enable end-to-end encryption in the group-mapped Matrix rooms."),
    ] = True
    default_room_create_params: dict[str, Any] | None = Field(
        default_factory=lambda: {"preset": "private_chat", "visibility": "private"}
    )
    matrix_room_create_params_from_authentik_attribute: str | None = "attributes.chatroom_params"
    keep_updating_matrix_attributes_from_authentik: Annotated[
        bool,
        Field(
            description="Keep room name/topic in sync with Authentik, overwriting drift.",
        ),
    ] = True


class OnbotConfig(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ONBOT_", env_nested_delimiter="__")

    log_level: Literal["INFO", "DEBUG"] = "INFO"
    storage_dir: str = Field(
        description="Directory for persisted runtime state (e.g. encryption key store).",
        default_factory=lambda: str(Path(PurePath(Path().home(), ".config/onbot/"))),
    )
    storage_encryption_key: Annotated[
        str | None,
        Field(description="Optional passphrase used to encrypt the persisted key store."),
    ] = None
    server_tick_rate_sec: Annotated[
        int,
        Field(description="Reconcile on this interval (seconds), in addition to on-demand triggers."),
    ] = 20

    synapse_server: Annotated[
        SynapseServer,
        Field(
            title="Synapse Server Configuration",
            description="Authorization/connection data for the Matrix CS and Synapse admin APIs.",
        ),
    ]
    authentik_server: AuthentikServer

    welcome_new_users_messages: list[str] | None = Field(
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

    sync_authentik_users_with_matrix_rooms: SyncAuthentikUsersWithMatrix = Field(
        default_factory=SyncAuthentikUsersWithMatrix
    )
    create_matrix_rooms_in_a_matrix_space: CreateMatrixRoomsInAMatrixSpace = Field(
        default_factory=CreateMatrixRoomsInAMatrixSpace,
        description="Configure the designated parent space for Authentik-group rooms.",
    )
    sync_matrix_rooms_based_on_authentik_groups: SyncMatrixRoomsBasedOnAuthentikGroups = Field(
        default_factory=SyncMatrixRoomsBasedOnAuthentikGroups
    )
    matrix_room_default_settings: MatrixDynamicRoomSettings = Field(default_factory=MatrixDynamicRoomSettings)
    per_authentik_group_pk_matrix_room_settings: Annotated[
        dict[str, MatrixDynamicRoomSettings],
        Field(
            description="Per-group room-setting overrides, keyed by Authentik group primary key (pk).",
        ),
    ] = Field(default_factory=dict)

    matrix_user_ignore_list: Annotated[
        list[str],
        Field(examples=[["@admin:company.org", "@root:company.org"]]),
    ] = Field(default_factory=list)
    authentik_user_ignore_list: Annotated[
        list[str],
        Field(examples=[["admin", "internal_account_alex"]]),
    ] = Field(default_factory=list)
    authentik_group_id_ignore_list: Annotated[
        list[str],
        Field(examples=[["1120a6e1124f309bbe96c8be5fb09eab"]]),
    ] = Field(default_factory=list)


def get_config_file_path(*, not_exists_ok: bool = False) -> Path | None:
    """Return the YAML config path from ``ONBOT_CONFIG_FILE_PATH`` (default ``config.yml``)."""
    yaml_file = Path(os.environ.get(CONFIG_FILE_ENV_VAR, "config.yml"))
    if yaml_file.is_file() or not_exists_ok:
        return yaml_file
    return None


def load_config() -> OnbotConfig:
    """Load config from YAML if present; env vars (``ONBOT_*``) override either way."""
    yaml_file = get_config_file_path()
    if yaml_file is not None:
        with yaml_file.open() as reader:
            data = yaml.safe_load(reader) or {}
        return OnbotConfig.model_validate(data)
    # No file: required fields are supplied from the environment by pydantic-settings.
    return OnbotConfig()  # type: ignore[call-arg]


def generate_example_config() -> str:
    """Render a YAML document of the default config model (G11.2).

    Required fields that have no default are emitted as ``null`` placeholders so the result is a
    fillable template rather than a validation error.
    """
    model = OnbotConfig(
        synapse_server=SynapseServer(server_name="", server_url="", bot_user_id="", bot_access_token=""),
        authentik_server=AuthentikServer(url="", api_key=""),
    )
    data = model.model_dump(mode="json")
    for key in ("server_name", "server_url", "bot_user_id", "bot_access_token"):
        data["synapse_server"][key] = None
    data["authentik_server"]["url"] = None
    data["authentik_server"]["api_key"] = None
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
