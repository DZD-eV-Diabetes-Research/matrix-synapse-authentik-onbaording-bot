import os
from typing import List, Dict, Optional, Annotated, Literal
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
import inspect
from pathlib import Path, PurePath
import yaml


def get_config() -> "OnbotConfig":
    yaml_file = get_config_file_path()
    if yaml_file:
        with open(yaml_file) as yaml_file_reader:
            return OnbotConfig.model_validate(yaml.safe_load(yaml_file_reader.read()))
    else:
        return OnbotConfig()


def get_config_file_path(not_exists_ok: bool = False) -> Optional[Path]:
    yaml_file = Path(os.environ.get("ONBOT_CONFIG_FILE_PATH", "../config.dev.yml"))

    if (yaml_file.exists() and yaml_file.is_file) or not_exists_ok:
        return yaml_file


def generate_config_file():
    from psyplus import YamlSettingsPlus

    y = YamlSettingsPlus(
        OnbotConfig, file_path=get_config_file_path(not_exists_ok=True)
    )
    y.generate_config_file(overwrite_existing=True)


class OnbotConfig(BaseSettings):
    log_level: Literal["INFO", "DEBUG"] = "INFO"
    storage_dir: str = Field(
        description="A directory to story any states for the bot. Only for saving encryption keys/state at the moment.",
        default_factory=lambda: str(Path(PurePath(Path().home(), ".config/onbot/"))),
    )
    storage_encryption_key: Annotated[
        Optional[str],
        Field(
            description="A passphrase that will be used to encrypt end to end encryption keys https://github.com/poljar/matrix-nio/blob/2632a72e7acee401c4354646a40f31db04db4258/nio/client/base_client.py#L145"
        ),
    ] = None
    server_tick_rate_sec: Annotated[
        Optional[int],
        Field(
            description="The bot poll every n seconds to sync the status between Authentik and Synapse."
        ),
    ] = 20

    class SynapseServer(BaseModel):
        server_name: Annotated[
            str,
            Field(
                description=inspect.cleandoc(
                    """Synapse's public facing domain https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name 
                    This is not necessarily the domain under which the Synapse server is reachable. See the docs and your configuration."""
                ),
                examples=["company.org"],
            ),
        ]

        server_url: Annotated[
            str,
            Field(
                description=inspect.cleandoc(
                    """Url to reach the synapse server. This can (and should) be an internal url. This will prevent you from make your synapse admin api public.
                But the bot will work with the public URL as well fi you want to."""
                ),
                examples=["https://internal.matrix"],
            ),
        ]

        bot_user_id: Annotated[
            str,
            Field(
                description="The full Matrix user ID for an existing matrix user account. The Bot will interact as this account.",
                examples=["@welcome-bot:company.org"],
            ),
        ]

        bot_device_id: Annotated[
            str,
            Field(
                description=inspect.cleandoc(
                    """A device ID the Bot account can provide, to access the API. You will get an device_id via https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3login
                Here is an curl example to get data.
                ```bash
                curl -XPOST -d '{"type":"m.login.password", "user":"my-bot-user", "password":"superSecrectPW"}' "https://matrix.company.org/_matrix/client/v3/login"
                ```
                """
                ),
                examples=["ZSIBBRS"],
            ),
        ]

        bot_access_token: Annotated[
            str,
            Field(
                description=inspect.cleandoc(
                    """A Bearer token to authorize the Bot access to the Synapse APIs. You will get an Bearer token via https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3login 
                Here is an curl example to get data.
                ```bash
                curl -XPOST -d '{"type":"m.login.password", "user":"my-bot-user", "password":"superSecrectPW"}' "https://matrix.company.org/_matrix/client/v3/login"
                ```
                """
                ),
                examples=[
                    "Bearer q7289zhwoieuhrfq279ugdfq3_ONLY_A_EXMAPLE_TOKEN_sadaw4"
                ],
            ),
        ]
        bot_avatar_url: Annotated[
            Optional[str],
            Field(
                description=inspect.cleandoc(
                    """An http url to an picture. The bot will set it as its own avatar on start.
                """
                ),
                examples=["https://sillyimages.com/face.png"],
            ),
        ] = None
        admin_api_path: Annotated[
            str,
            Field(
                description="If your Synapse server admin API is reachable in a subpath you can adapt this here. If you dont know that this is for; keep the default value.",
                examples=["_synapse/admin/"],
            ),
        ] = "_synapse/admin/"

    synapse_server: Annotated[
        SynapseServer,
        Field(
            title="Synapse Server Configuration",
            description="To manage users on the Synapse server, the bot need access to the Matrix and Admin Api. The authorization data will be configured in this chapter.",
        ),
    ]

    class AuthentikServer(BaseModel):
        url: Annotated[
            str,
            Field(
                description="The URL to reach your Authentik server.",
                examples=["https://authentik.company.org/"],
            ),
        ]
        api_key: Annotated[
            str,
            Field(
                description=inspect.cleandoc(
                    """The Bearer token access your Authentik server.
                You can generate a new token for your existing Authentik user at https://authentik.company.org/if/admin/#/core/tokens"""
                ),
                examples=[
                    "Bearer yEl4tFqeIBQwoHAd9hajmkm2PBjSAirY_THIS_IS_JUST_AN_EXAMPLE_i57e"
                ],
            ),
        ]

    authentik_server: AuthentikServer

    # The bot will invite the new user to a direct chat and send following message
    welcome_new_users_messages: Optional[List[str]] = Field(
        default=[
            "Welcome to the company chat. I am the company bot. I will invite you to the groups you are assigned too. If you have any technical questions write a message to @admin-person:matrix.company.org.",
            "If you need some guidance on how to use this chat have a look at the official documentations. For the basic have a look at https://matrix.org/docs/chat_basics/matrix-for-im/ and for more details see https://element.io/user-guide",
            "🛑 🔐 The Chat software will ask you to setup a 'Security Key Backup'. <b>This is very important<b>. Save the file on a secure location. Otherwise you could lose access to older enrypted messages later. Please follow the request.",
        ]
    )

    class SyncAuthentikUsersWithMatrix(BaseModel):
        enabled: bool = True
        # the source of the username (@<username>:matrix.company.org) part in the matrix ID (MXID)
        # can be json path (seperated by ".")
        # The default value in authentik is username. but also can be a custom attribute e.g. "attribute.my_matrix_account_name" if you have a custom configuration
        authentik_username_mapping_attribute: str = "username"

        kick_matrix_room_members_not_in_mapped_authentik_group_anymore: bool = True

        # only sync user from specific pathes.
        # e.g. '["users"]'
        sync_only_users_in_authentik_pathes: Optional[List[str]] = None

        # works only for custom attributes in the authentik "attribute"-field. must be provided as dict/json.
        # e.g. '{"is_chat_user":true}'
        sync_only_users_with_authentik_attributes: Optional[Dict] = None

        sync_only_users_of_groups_with_id: List[str] = None

        class DeactivateDisabledAuthentikUsersInMatrix(BaseModel):
            # https://element-hq.github.io/synapse/develop/admin_api/user_admin_api.html#deactivate-account
            enabled: Annotated[
                bool,
                Field(
                    description="If enabled users with no matching Authentik account will be logged out of Synapse with the next server tick. As they would need a working Authenik account to re-login they are locked out of Synapse."
                ),
            ] = True

            deactivate_after_n_sec: Annotated[
                int,
                Field(
                    description="Deactivate account as in https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#deactivate-account after a certain amount of days. A delay can help to mitigate minor mistakes e.g. when the Authentik user was disabled accidently"
                ),
            ] = (
                60 * 60 * 24
            )
            delete_after_n_sec: Annotated[
                int | None,
                Field(
                    description="Delete account as in https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#deactivate-account (with `erase` flag) after a certain amount of days. A delay can help to mitigate minor mistakes e.g. when the Authentik user was disabled accidently"
                ),
            ] = (
                60 * 60 * 24 * 365
            )

            include_user_media_on_delete: Annotated[
                bool,
                Field(
                    description="Delete all uploaded media as in https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#delete-media-uploaded-by-a-user This may help meet your local data protection rules but can also alter chat histories with other users."
                ),
            ] = False

        deactivate_disabled_authentik_users_in_matrix: (
            DeactivateDisabledAuthentikUsersInMatrix
        ) = DeactivateDisabledAuthentikUsersInMatrix()

    sync_authentik_users_with_matrix_rooms: SyncAuthentikUsersWithMatrix = (
        SyncAuthentikUsersWithMatrix()
    )

    class CreateMatrixRoomsInAMatrixSpace(BaseModel):
        enabled: bool = Field(
            default=True,
            description="If set to true all authentik group rooms will be created in an extra space.",
        )
        alias: str = Field(
            default="OnBotSpace",
            description='The name part of a "canonical_alias". e.g. if the room canonical alias is (or should be if `create_matrix_space_if_not_exists.enable=true`) "#MyCompanySpace:matrix.company.org", enter "MyCompanySpace" here.',
            examples=["myspace", "companyspace"],
        )

        class CreateMatrixSpaceIfNotExists(BaseModel):
            enabled: bool = Field(
                default=True,
                description="If set to true, a the space, for the on authenik group-rooms, will be created if it not exists.",
            )
            name: str = Field(
                default="OnBotSpace",
                description="Display name of the space to create all rooms.",
                examples=["My Company", "AuthentikRooms"],
            )
            topic: str = Field(
                default="Space for authentik group rooms",
                description="Matrix Topic for the space. this will be a tagline for the space.",
                examples=[
                    "Gather here authentik users",
                    "No skating, food or drinks allowed here.",
                ],
            )
            avatar_url: Optional[str] = Field(
                default=None,
                description="The avatar picture for the Bot Matrix space. Can be any unauthenticated http/s url that points to an picture.",
                examples=[
                    "https://upload.wikimedia.org/wikipedia/en/e/e8/SnakePlissken.jpeg"
                ],
            )

            # https://spec.matrix.org/v1.6/client-server-api/#post_matrixclientv3createroom
            # all available params at https://matrix-nio.readthedocs.io/en/latest/nio.html?highlight=room_create#nio.AsyncClient.room_create
            #
            # preset:
            # enum. one of [public_chat,private_chat,trusted_private_chat]
            # preset: private_chat
            #
            # visibility
            # A public visibility indicates that the room will be shown in the published room list. A private visibility will hide the room from the published room list.
            # enum. One of: [public,private]
            # visibility: private
            #
            # default_room_params

            space_params: Dict = Field(
                default={
                    "preset": "private_chat",
                    "visibility": "private",
                },
                description="Any extra parameters (as a dict/object) you want to assign to the space. Have a look at https://matrix-nio.readthedocs.io/en/latest/nio.html#nio.AsyncClient.room_create for all possible parameters.",
            )

        create_matrix_space_if_not_exists: CreateMatrixSpaceIfNotExists = Field(
            default=CreateMatrixSpaceIfNotExists(),
            description="This chapter will define if and how the authentik-group-rooms are created in an designated Matrix space",
        )

    create_matrix_rooms_in_a_matrix_space: CreateMatrixRoomsInAMatrixSpace = Field(
        default=CreateMatrixRoomsInAMatrixSpace(),
        description="If you want all authentik-group-rooms in a designated space, the following chapter will let you configure this.",
    )

    class SyncMatrixRoomsBasedOnAuthentikGroups(BaseModel):
        enabled: bool = True
        only_for_children_of_groups_with_uid: Optional[List[str]] = Field(
            default_factory=list
        )
        only_groups_with_attributes: Annotated[
            Optional[Dict],
            Field(
                description=inspect.cleandoc(
                    """Define an Authentik custom attribute (as a json or yaml key value pair) to match groups that should be synced. 
                If unset, all Authentik groups will be mirrored as a Synapse room. 
                https://goauthentik.io/docs/user-group/group#attributes"""
                ),
                examples=[{"is_chatroom": True}],
                default_factory=list,
            ),
        ]
        room_avatar_url_attribute: Annotated[
            Optional[Dict],
            Field(
                description=inspect.cleandoc(
                    """Define an Authentik custom room attribute that can contain an url to a media that will be configured as the rooms avatar"""
                ),
                examples=["chatroom_avatar_url"],
                default="chatroom_avatar_url",
            ),
        ]
        only_for_groupnames_starting_with: Optional[str] = Field(default_factory=list)
        disable_rooms_when_mapped_authentik_group_disappears: Annotated[
            bool,
            Field(
                description=inspect.cleandoc(
                    """If a previously mapped authentik room disappers (e.g. it was deleted or lost its `only_groups_with_attributes` attribute)
                onbot will kick out all users and block the room."""
                )
            ),
        ] = False
        delete_disabled_rooms: bool = False
        make_authentik_superusers_matrix_room_admin: bool = True
        authentik_group_attr_for_matrix_power_level: Annotated[
            str,
            Field(
                description=inspect.cleandoc(
                    """Define an Authentik group custom attribute path (elements seperated by '.') that contains an integer from 0-100. 
                    Members of this group will get this integer applied as Matrix power level in the rooms they are member of(https://matrix.org/docs/communities/moderation/)
                    e.g. you could create an Authentik group named "Matrix-Moderators" with `{"attributes":{"chat-powerlevel":50}}`. All members of this group will get Matrix power level 50 in their onbot group rooms
                    If a user gets admin via `sync_matrix_rooms_based_on_authentik_groups.make_authentik_superusers_matrix_room_admin` `authentik_group_attr_for_matrix_power_level` will be ignored.
                    If a user has multiple group memberships with different Matrix power level definitions, the highest value will be aplied
                    WARNING: Once a user has elevated power levels via a authentik group membership, the new power level will not be withdrawn with losing the authentik group membership. this maybe implemented in a future version of this bot"""
                ),
                examples=["matrix-userpowerlevel", "synapse-options.chat-powerlevel"],
            ),
        ] = "chat-powerlevel"

    sync_matrix_rooms_based_on_authentik_groups: (
        SyncMatrixRoomsBasedOnAuthentikGroups
    ) = SyncMatrixRoomsBasedOnAuthentikGroups()

    class MatrixDynamicRoomSettings(BaseModel):
        alias_prefix: Optional[str] = None
        matrix_alias_from_authentik_attribute: str = "pk"
        name_prefix: Optional[str] = None
        matrix_name_from_authentik_attribute: str = "name"
        topic_prefix: Optional[str] = None
        matrix_topic_from_authentik_attribute: Optional[str] = (
            "attributes.chatroom_topic"
        )
        end2end_encryption_enabled: Annotated[
            bool,
            Field(
                description="If set to true this will enable end2end encryption in the Authentik group mapped Matrix rooms."
            ),
        ] = True

        # https://spec.matrix.org/v1.6/client-server-api/#post_matrixclientv3createroom
        # enum. one of [public_chat,private_chat,trusted_private_chat]
        # preset: private_chat
        # A public visibility indicates that the room will be shown in the published room list. A private visibility will hide the room from the published room list.
        # enum. One of: [public,private]
        # visibility: private
        default_room_create_params: Optional[Dict] = {
            "preset": "private_chat",
            "visibility": "private",
        }

        # An authentik attribute that can contains parameters for the "room_create" event.
        # see https://matrix-nio.readthedocs.io/en/latest/nio.html#nio.AsyncClient.room_create for possible params
        # params need to be provided as json
        # e.g. '{"preset": "private_chat", "visibility": "private", "federate": false}'
        matrix_room_create_params_from_authentik_attribute: Optional[str] = (
            "attribute.chatroom_params"
        )

        keep_updating_matrix_attributes_from_authentik: Annotated[
            Optional[bool],
            Field(
                description="Should the bot update the Matrix room name/topic if they changed in authentik? If set to true the bot will overwrite any room topic/name that differs from the Authentik source group"
            ),
        ] = True

    matrix_room_default_settings: MatrixDynamicRoomSettings = (
        MatrixDynamicRoomSettings()
    )

    per_authentik_group_pk_matrix_room_settings: Annotated[
        Optional[Dict[str, MatrixDynamicRoomSettings]],
        Field(
            description="Override default room settings and define custom room settings for a specific authentikgroup based matrix room group. Specify the authentik group by its primary key ('pk')",
            examples=[
                {
                    "80439f0d-d936-4118-8017-52a95d6dd1bc": MatrixDynamicRoomSettings(
                        matrix_alias_from_authentik_attribute="attribute.custom",
                        topic_prefix="TOPIC PREFIX FOR SPECIFIC ROOM:",
                    )
                }
            ],
            default_factory=dict,
        ),
    ]

    matrix_user_ignore_list: Annotated[
        Optional[List[str]],
        Field(examples=[{"@admin:company.org", "@root:company.org"}]),
    ] = []

    authentik_user_ignore_list: Annotated[
        Optional[List[str]], Field(examples=[["admin", "internal_account_alex"]])
    ] = []
    authentik_group_id_ignore_list: Annotated[
        Optional[List[str]], Field(examples=[["1120a6e1124f309bbe96c8be5fb09eab"]])
    ] = []

    class DeactivateDisabledAuthentikUsersInMatrix(BaseModel):
        # https://element-hq.github.io/synapse/develop/admin_api/user_admin_api.html#deactivate-account
        enabled: Annotated[
            bool,
            Field(
                description="If enabled users with no matching Authentik account will be logged out of Synapse with the next server tick. As they would need a working Authenik account to re-login they are locked out of Synapse."
            ),
        ] = True

        deactivate_after_n_sec: Annotated[
            int,
            Field(
                description="Deactivate account as in https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#deactivate-account after a certain amount of days. A delay can help to mitigate minor mistakes e.g. when the Authentik user was disabled accidently"
            ),
        ] = (
            60 * 60 * 24
        )
        delete_after_n_sec: Annotated[
            int | None,
            Field(
                description="Delete account as in https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#deactivate-account (with `erase` flag) after a certain amount of days. A delay can help to mitigate minor mistakes e.g. when the Authentik user was disabled accidently"
            ),
        ] = (
            60 * 60 * 24 * 365
        )

        include_user_media_on_delete: Annotated[
            bool,
            Field(
                description="Delete all uploaded media as in https://element-hq.github.io/synapse/latest/admin_api/user_admin_api.html#delete-media-uploaded-by-a-user This may help meet your local data protection rules but can also alter chat histories with other users."
            ),
        ] = False

    deactivate_disabled_authentik_users_in_matrix: (
        DeactivateDisabledAuthentikUsersInMatrix
    ) = DeactivateDisabledAuthentikUsersInMatrix()

    class Config:
        # (meta)config class for pydantic-settings https://docs.pydantic.dev/latest/usage/settings/
        env_prefix: str = "ONBOT_"
        env_nested_delimiter: str = "__"
