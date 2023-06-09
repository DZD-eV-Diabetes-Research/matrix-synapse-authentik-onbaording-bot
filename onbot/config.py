from typing import List, Dict, Optional, Annotated, Literal, Set, Tuple
import yaml
import datetime
from pydantic import BaseModel, Field, BaseSettings, fields


class ConfigDefaultModel(BaseSettings):
    class SynapseServer(BaseModel):
        server_name: Annotated[
            str,
            Field(
                max_length=100,
                description="Synapse's public facing domain https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name",
                example="company.org",
            ),
        ]

        server_url: Annotated[
            str,
            Field(
                description="Url to reach the synapse server. This can (and should) be an internal url. This will prevent you from make your synapse admin api public. But the bot will work with the public URL as well.",
                example="http://internal.matrix",
            ),
        ] = None
        api_path: Annotated[
            Optional[str],
            Field(
                description="If your Synapse server API is reachable in a subpath you can adapt this here. If you dont know that this is for; keep the default value.",
                example="_synapse/admin/",
            ),
        ] = "_matrix/"
        admin_api_path: Annotated[
            str,
            Field(
                description="If your Synapse server admin API is reachable in a subpath you can adapt this here. If you dont know that this is for; keep the default value.",
                example="_synapse/admin/",
            ),
        ] = "_synapse/admin/"

        bot_user_id: Annotated[
            str,
            Field(
                description="The full Matrix user ID for an existing matrix user account. The Bot will interact as this account.",
                example="@welcome-bot:company.org",
            ),
        ]

        # TODO: provide an curl example to get a devide id and access token
        bot_device_id: Annotated[
            str,
            Field(
                description="A device ID the Bot account can provide, to access the API. You will get an device_id via https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3login",
                example="ZSIBBRS",
            ),
        ] = None
        bot_access_token: Annotated[
            str,
            Field(
                description="A Bearer token to authorize the Bot access to the Synapse APIs. You will get an Bearer token via https://spec.matrix.org/latest/client-server-api/#post_matrixclientv3login",
                example="Bearer q7289zhwoieuhrfq279ugdfq3_ONLY_A_EXMAPLE_TOKEN_sadaw4",
            ),
        ] = None

    synapse_server: Annotated[
        SynapseServer,
        Field(
            title="### Synapse Server Configuration ###",
            description="DESC FOR SYNAPSE SERVER",
        ),
    ]

    class AuthentikServer(BaseModel):
        public_api_url: str = "https://authentik.company.org/api/v3"
        api_key: str = "xxx"
        sync_interval_seconds: int = 120
        account_pathes: List[str] = ["users"]

    authentik_server: AuthentikServer = AuthentikServer()

    # The bot will invite the new user to a direct chat and send following message
    welcome_new_users_messages: List[str] = [
        "Welcome to the company chat. I am the company bot. I will invite you to the groups you are assigned. If you have any technical questions write a message to @admin-person:matrix.company.org.",
        "The Chat software will ask you to setup a Security Key Backup. This is very important. Otherwise you can lose access older messages later. Please follow the request.",
    ]

    class SyncAuthentikUsersWithMatrix(BaseModel):
        enabled: bool = True
        # the source of the username (@<username>:matrix.company.org) part in the matrix ID (MXID)
        # can be json path (seperated by ".")
        # The default value in authentik is username. but also can be a custom attribute e.g. "attribute.my_matrix_account_name" if you have a custom configuration
        authentik_username_mapping_attribute: str = "username"

        kick_matrix_room_members_not_in_mapped_authentik_group_anymore: bool = True

        # only sync user from specific pathes.
        # e.g. '["users"]'
        sync_only_users_in_authentik_pathes: List[str] = None

        # works only for custom attributes in the authentik "attribute"-field. must be provided as dict/json.
        # e.g. '{"is_chat_user":true}'
        sync_only_users_with_authentik_attributes: Dict = None

        sync_only_users_of_groups_with_id: List[str] = None

    sync_authentik_users_with_matrix_rooms: SyncAuthentikUsersWithMatrix = (
        SyncAuthentikUsersWithMatrix()
    )

    class CreateMatrixRoomsInAMatrixSpace(BaseModel):
        enabled: bool = True
        alias: str = "MyCompanySpace"  # the name part of a "canonical_alias". e.g. if the room canonical alias is (or should be) "#MyCompanySpace:matrix.company.org", enter "MyCompanySpace" here

        class CreateMatrixSpaceIfNotExists(BaseModel):
            enabled: bool = True
            name: str = "Our cozy space"
            topic: str = "The Company Space"

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

            space_params: Dict = {
                "preset": "private_chat",
                "visibility": "private",
            }

        create_matrix_space_if_not_exists: CreateMatrixSpaceIfNotExists = (
            CreateMatrixSpaceIfNotExists()
        )

    create_matrix_rooms_in_a_matrix_space: CreateMatrixRoomsInAMatrixSpace = (
        CreateMatrixRoomsInAMatrixSpace()
    )

    class CreateMatrixRoomsBasedOnAuthentikGroups(BaseModel):
        enabled: bool = True
        only_for_children_of_groups_with_uid: List[str] = None
        only_groups_with_attributes: Dict = {"attribute.chatroom": True}
        only_for_groupnames_starting_with: str = None

    create_matrix_rooms_based_on_authentik_groups: CreateMatrixRoomsBasedOnAuthentikGroups = (
        CreateMatrixRoomsBasedOnAuthentikGroups()
    )

    class MatrixDynamicRoomSettings(BaseModel):
        alias_prefix: Optional[str] = None
        matrix_alias_from_authentik_attribute: Optional[str] = "pk"
        name_prefix: Optional[str] = None
        matrix_name_from_authentik_attribute: Optional[str] = "name"
        topic_prefix: Optional[str] = None
        matrix_topic_from_authentik_attribute: Optional[
            str
        ] = "attributes.chatroom_topic"

        # An authentik attribute that can contains parameters for the "room_create" event.
        # see https://matrix-nio.readthedocs.io/en/latest/nio.html#nio.AsyncClient.room_create for possible params
        # params need to be provided as json
        # e.g. '{"preset": "private_chat", "visibility": "private", "federate": false}'
        matrix_room_create_params_from_authentik_attribute: Optional[
            str
        ] = "attribute.chatroom_params"

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

    matrix_room_default_settings: MatrixDynamicRoomSettings = (
        MatrixDynamicRoomSettings()
    )

    per_authentik_group_pk_matrix_room_settings: Annotated[
        Optional[Dict[str, MatrixDynamicRoomSettings]],
        Field(
            example={
                "80439f0d-d936-4118-8017-52a95d6dd1bc": MatrixDynamicRoomSettings(
                    matrix_alias_from_authentik_attribute="attribute.custom",
                    topic_prefix="TOPIC PREFIX FOR SPECIFIC ROOM:",
                )
            }
        ),
    ] = {}

    matrix_user_ignore_list: Annotated[
        Optional[List[str]], Field(example={"@admin:company.org", "@root:company.org"})
    ] = []

    authentik_user_ignore_list: List[str] = ["admin"]
    authentik_group_ignore_list: List[str] = ["internal_company_group"]

    class DeactivateDisabledAuthentikUsersInMatrix(BaseModel):
        # https://matrix-org.github.io/synapse/develop/admin_api/user_admin_api.html#deactivate-account
        enabled: bool = True
        # if erase is set to False, disabled authentik users will be logged out of all matrix devices. If disabled in authtentik, the user wont be able to re-login. It is possible to re-activate the account, by enabled the account in authnetik. This can be used if you do not re-cycle usernames and do not have to be gdpr compliant.
        # if erase is set to True, the account will be deactivate as in https://matrix-org.github.io/synapse/develop/admin_api/user_admin_api.html#deactivate-account At the moment there is no way of re-activating the account. This can be made gdpr compliant (with the option 'gdpr-erase') and the username will be burned.
        erase: bool = False
        gdpr_erase: bool = True

    deactivate_disabled_authentik_users_in_matrix: DeactivateDisabledAuthentikUsersInMatrix = (
        DeactivateDisabledAuthentikUsersInMatrix()
    )
