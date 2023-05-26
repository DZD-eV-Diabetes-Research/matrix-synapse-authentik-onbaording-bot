from typing import List, Dict, Optional
import yaml

from pydantic import BaseModel, Field


class ConfigDefaultModel(BaseModel):
    class SynapseServer(BaseModel):
        server_name: str = Field(
            default=None,
            title="Synapse's public facing domain https://matrix-org.github.io/synapse/latest/usage/configuration/config_documentation.html#server_name",
            example="matrix.company.org",
            default_deactivated=True,
        )
        server_host: str = "matrix.internal"
        api_path: str = "_synapse/admin/"
        admin_api_path: str = "_synapse/admin/"
        bot_user_id: str = "@welcome-bot:matrix.company.org"
        bot_device_id: str = "ZSIBBRS"
        bot_access_token: str = "Bearer xxx"

    synapse_server: SynapseServer = SynapseServer()

    class AuthentikServer(BaseModel):
        public_api_url: str = "https://authentik.company.org/api/v3"
        api_key: str = "xxx"
        sync_interval_seconds: int = 120
        account_pathes: List[str] = ["users"]

    authentik_server: AuthentikServer = AuthentikServer()

    # The bot will invite the new user to a direct chat and send following message
    welcome_new_users_message: str = "Welcome to the company chat. I am the company bot. I will invite you to the groups you are assigned. If you have any technical questions write a message to @admin-person:matrix.company.org."
    create_matrix_rooms_based_on_authentik_groups: bool = True
    invite_group_members_to_matrix_room: bool = True
    kick_users_from_matrix_room_if_removed_from_authentik_group: bool = True

    # the source of the username (@<username>:matrix.company.org) part in the matrix ID (MXID)
    # The default value in authentik is username. but also can be a custom attribute e.g. "attribute.account_name" if you have a custom configuration
    authentik_username_mapping_attribute: str = "username"

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
            # extra_params

            extra_params: Dict = {
                "preset": "private_chat",
                "visibility": "private",
            }

        create_matrix_space_if_not_exists: CreateMatrixSpaceIfNotExists = (
            CreateMatrixSpaceIfNotExists()
        )

    create_matrix_rooms_in_a_matrix_space: CreateMatrixRoomsInAMatrixSpace = (
        CreateMatrixRoomsInAMatrixSpace()
    )

    class CreateMatrixRoomsOnlyForAuthentikGroupsWithAttribute(BaseModel):
        enabled: bool = True
        attribute_key: str = "chatroom"
        attribute_val: bool = True

    create_matrix_rooms_only_for_authentik_groups_with_attribute: CreateMatrixRoomsOnlyForAuthentikGroupsWithAttribute = (
        CreateMatrixRoomsOnlyForAuthentikGroupsWithAttribute()
    )

    class CreateMatrixRoomsOnlyForAuthentikGroupsStartingWith(BaseModel):
        enabled: bool = False
        value: str = "chatgroup"
        reverse_rule: bool = False

    create_matrix_rooms_only_for_authentik_groups_starting_with: CreateMatrixRoomsOnlyForAuthentikGroupsStartingWith = (
        CreateMatrixRoomsOnlyForAuthentikGroupsStartingWith()
    )

    class MatrixRoomSettings(BaseModel):
        alias_prefix: str = None
        alias_from_authentik_attribute: str = "pk"
        name_prefix: str = None
        name_from_authentik_attribute: str = "name"
        topic_default_prefix: str = None
        topic_from_authentik_attribute: str = "attributes.chatroom_topic"

        # https://spec.matrix.org/v1.6/client-server-api/#post_matrixclientv3createroom
        # enum. one of [public_chat,private_chat,trusted_private_chat]
        # preset: private_chat
        # A public visibility indicates that the room will be shown in the published room list. A private visibility will hide the room from the published room list.
        # enum. One of: [public,private]
        # visibility: private
        extra_params: Dict = {
            "preset": "private_chat",
            "visibility": "private",
        }

    matrix_room_default_settings: MatrixRoomSettings = MatrixRoomSettings()

    per_authentik_group_pk_matrix_room_settings: Dict[str, MatrixRoomSettings] = {
        "80439f0d-d936-4118-8017-52a95d6dd1bc": MatrixRoomSettings(
            alias_from_authentik_attribute="attribute.custom",
            topic_default_prefix="TOPIC PREFIX FOR ALL ROOMS:",
        )
    }

    matrix_user_ignore_list: List[str] = ["@admin:matrix.company.org"]
    authentik_user_ignore_list: List[str] = ["admin"]
    authentik_group_ignore_list: List[str] = ["internal_company_group"]


c = ConfigDefaultModel()
print(yaml.dump(c.dict(), sort_keys=False))
