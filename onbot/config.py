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

    class SyncAuthentikUsersWithMatrix(BaseModel):
        enabled: bool = True
        # the source of the username (@<username>:matrix.company.org) part in the matrix ID (MXID)
        # The default value in authentik is username. but also can be a custom attribute e.g. "attribute.my_matrix_account_name" if you have a custom configuration
        authentik_username_mapping_attribute: str = "username"

        kick_matrix_room_members_not_in_mapped_authentik_group_anymore: bool = True
        sync_users_with_attributes: Dict = None
        sync_users_of_groups_with_id: List[str]

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

    class CreateMatrixRoomsBasedOnAuthentikGroups(BaseModel):
        enabled: bool = True
        only_for_children_of_groups_with_uid: List[str] = None
        only_groups_with_attributes: Dict = {"attribute.chatroom": True}
        only_for_groupnames_starting_with: str = None

    create_matrix_rooms_based_on_authentik_groups: CreateMatrixRoomsBasedOnAuthentikGroups = (
        CreateMatrixRoomsBasedOnAuthentikGroups()
    )

    class MatrixRoomSettings(BaseModel):
        alias_prefix: str = None
        alias_from_authentik_attribute: str = "pk"
        name_prefix: str = None
        name_from_authentik_attribute: str = "name"
        topic_prefix: str = None
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
            topic_prefix="TOPIC PREFIX FOR ALL ROOMS:",
        )
    }

    matrix_user_ignore_list: List[str] = ["@admin:matrix.company.org"]
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


c = ConfigDefaultModel()
print(yaml.dump(c.dict(), sort_keys=False))
