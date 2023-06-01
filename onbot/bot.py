from typing import List, Dict, Union, Any, Literal, TYPE_CHECKING
import logging
import asyncio
from pydantic import BaseModel
import time
from nio import (
    AsyncClient as MatrixNioClient,
    ErrorResponse,
    RoomCreateResponse,
    RoomCreateError,
    RoomVisibility,
    RoomPreset,
    MatrixRoom,
    RoomPutStateError,
)

from onbot.config import ConfigDefaultModel
from onbot.authentik_api_client import AuthentikApiClient
from onbot.synapse_admin_api_client import SynapseAdminApiClient
from onbot.matrix_api_client import MatrixApiClient
from onbot.config import ConfigDefaultModel
from onbot.utils import walk_attr_path

log = logging.getLogger(__name__)


class MatrixRoomAttributes(BaseModel):
    alias: str
    canonical_alias: str
    id: str = None
    name: str = None
    topic: str = None
    extra_params: dict = None

    def get_canonical_alias(self, server_name: str):
        return f"#{self.alias}:{server_name}"


class UserMap(BaseModel):
    # https://authentik.company/api/v3/#get-/core/users/ object
    authentik_api_obj: Dict = None

    # https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts object
    matrix_api_obj: Dict = None

    generated_matrix_id: str = None


class Group2RoomMap(BaseModel):
    # https://authentik.company/api/v3/#get-/core/groups/ object
    authentik_api_obj: Dict = None

    # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api object
    matrix_api_obj: Dict = None

    generated_matrix_room_attr: MatrixRoomAttributes = None


class SynapseApiError(Exception):
    @classmethod
    def from_nio_response_error(cls, nio_response_error: ErrorResponse):
        # Tim Todo: add_note with python 3.11. will be awesome!
        # https://docs.python.org/3.11/library/exceptions.html#BaseException.add_note
        return cls(
            f"{type(nio_response_error)} - status_code: '{nio_response_error.status_code}' Message: '{nio_response_error.message}'"
        )


class Bot:
    def __init__(
        self,
        config: ConfigDefaultModel,
        authentik_client: AuthentikApiClient,
        synapse_admin_api_client: SynapseAdminApiClient,
        matrix_nio_client: MatrixNioClient,
        matrix_api_client: MatrixApiClient,
        server_tick_wait_time_sec_int: int = 60,
    ):
        self.config = config
        self.authentik_client = authentik_client
        self.synapse_admin_client = synapse_admin_api_client
        self.synapse_client = matrix_nio_client
        self.synpase_api_client = matrix_api_client
        self.server_tick_wait_time_sec_int = server_tick_wait_time_sec_int

    def start(self):
        while True:
            self.server_tik()

    def server_tik(self):
        self.create_matrix_rooms_based_on_authentik_groups()
        self.sync_users_and_rooms()
        time.sleep(self.server_tick_wait_time_sec_int)

    def create_matrix_rooms_based_on_authentik_groups(self):
        parent_room_space = self.get_parent_space_if_needed()
        for group_room_map in self.get_authentik_groups_that_need_synapse_room():
            if group_room_map.matrix_api_obj is not None:
                self.create_room(
                    room_attr=group_room_map.generated_matrix_room_attr,
                    parent_space_id=parent_room_space["room_id"]
                    if parent_room_space
                    else None,
                )

    def sync_users_and_rooms(self):
        mapped_users: List[
            UserMap
        ] = self.get_authentik_accounts_with_mapped_synapse_account()
        mapped_rooms: List[
            Group2RoomMap
        ] = self.get_authentik_groups_that_need_synapse_room()

        for room in mapped_rooms:
            room_members = self.synapse_admin_client.list_room_members(
                room.matrix_api_obj["room_id"]
            )
            for user in mapped_users:
                user_should_be_member: bool = bool(
                    next(
                        (
                            grp
                            for grp in user.authentik_api_obj["groups_obj"]
                            if grp["pk"] in room.authentik_api_obj["pk"]
                        ),
                        False,
                    )
                )
                user_is_member: bool = next(
                    (
                        member
                        for member in room_members
                        if member == user.matrix_api_obj["name"]
                    )
                )
            if user_should_be_member and not user_is_member:
                self.synapse_admin_client.add_user_to_room(
                    room.matrix_api_obj["room_id"], user.matrix_api_obj["name"]
                )
            elif (
                not user_should_be_member
                and user_is_member
                and self.config.sync_authentik_users_with_matrix_rooms.kick_matrix_room_members_not_in_mapped_authentik_group_anymore
            ):
                self.synapse_client.room_kick(
                    room.matrix_api_obj["room_id"],
                    user.matrix_api_obj["name"],
                    "Automatically removed by leaving group membership in central user directory.",
                )

    def sync_user_to_room(self, user: UserMap, room: Group2RoomMap):
        pass

    def get_parent_space_if_needed(self) -> Dict | None:
        if not self.config.create_matrix_rooms_in_a_matrix_space.enabled:
            return None
        existing_spaces: List[Dict] = self.synapse_admin_client.list_space()
        target_space_canonical_alias = f"#{self.config.create_matrix_rooms_in_a_matrix_space.alias}:{self.config.synapse_server.server_name}"

        space = next(
            (
                space
                for space in existing_spaces
                if space["canonical_alias"] == target_space_canonical_alias
            ),
            None,
        )
        if space is not None:
            return space
        if (
            not self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled
        ):
            raise ValueError(
                f"Can not find space with canonical_alias '{target_space_canonical_alias}' and 'config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists' is disabled. Please make sure the room exists or allow me to create it with 'config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled=true'"
            )

        # we need to create the space
        async def create_space():
            return await self.synapse_client.room_create(
                space=True,
                alias=self.config.create_matrix_rooms_in_a_matrix_space.alias,
                name=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name,
                topic=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic,
                **self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.extra_params,
            )

        space: Union[RoomCreateResponse, RoomCreateError] = asyncio.run(create_space())
        if type(space) == RoomCreateError:
            log.error(
                f"Could not create the parent space with the alias '{target_space_canonical_alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(space.message)
        else:
            # we now can just recall the function because the room exists now.
            return self.get_parent_space_if_needed()

    def create_room(
        self, room_attr: MatrixRoomAttributes, parent_space_id: str = None
    ) -> RoomCreateResponse:
        # todo: migrate function to  onbot.matrix_api_client.MatrixApiClient? Its way too complex for bot-business logic
        async def create_room():
            inital_state = None
            if parent_space_id:
                # https://spec.matrix.org/v1.2/client-server-api/#mspaceparent
                inital_state = [
                    {
                        "type": "m.space.parent",
                        "state_key": parent_space_id,
                        "content": {
                            "canonical": True,
                            "via": [self.config.synapse_server.server_name],
                        },
                    }
                ]
            room_response = await self.synapse_client.room_create(
                space=False,
                alias=room_attr.alias,
                name=room_attr.name,
                topic=room_attr.topic,
                inital_state=inital_state,
                **room_attr.extra_params,
            )
            if parent_space_id and not type(room_response) == RoomCreateError:
                state_update = await self.synapse_client.room_put_state(
                    parent_space_id,
                    "m.space.child",
                    {
                        "suggested": True,
                        "via": [self.config.synapse_server.server_name],
                    },
                    state_key=room.room_id,
                )
                if type(state_update) == RoomPutStateError:
                    log.error(
                        f"Could not add room with the alias '{room_attr.canonical_alias}' as child to space {parent_space_id}. Don't know what to do. Here is the error:"
                    )
                    raise SynapseApiError.from_nio_response_error(room)
            return room_response

        room: Union[RoomCreateResponse, RoomCreateError] = asyncio.run(create_room())
        if type(room) == RoomCreateError:
            log.error(
                f"Could not create room with the alias '{room_attr.canonical_alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(room)
        else:
            # we now can just recall the function because the room exists now.
            return RoomCreateResponse

    def get_room_and_create_if_not_exists(
        self, room_attr: MatrixRoomAttributes
    ) -> Dict:
        space = None
        if self.config.create_matrix_rooms_in_a_matrix_space.enabled:
            space = self.get_parent_space_if_needed()
        existent_rooms: List[Dict] = self.synapse_admin_client.list_room(space)

        room = next(
            (
                room
                for room in existent_rooms
                if room["canonical_alias"]
                == room_attr.get_canonical_alias(self.config.synapse_server.server_name)
            ),
            None,
        )
        if room is not None:
            return room
        else:
            self.create_room(room_attr)
            return self.get_room_and_create_if_not_exists()

    def get_authentik_groups_that_need_synapse_room(self) -> List[Group2RoomMap]:
        if not self.config.create_matrix_rooms_based_on_authentik_groups.enabled:
            return []
        query_attributes = {}
        if (
            self.config.create_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes
        ):
            query_attributes = (
                self.config.create_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes
            )

        groups: Dict = self.authentik_client.list_groups(
            filter_by_attribute=query_attributes
        )

        if (
            self.config.create_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with
        ):
            groups = [
                g
                for g in groups
                if g["name"].startswith(
                    self.config.create_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with
                )
            ]

        if (
            self.config.create_matrix_rooms_based_on_authentik_groups.only_for_children_of_groups_with_uid
        ):
            groups = [
                g
                for g in groups
                if g["parent"]
                in self.config.create_matrix_rooms_based_on_authentik_groups.only_for_children_of_groups_with_uid
            ]
        matrix_rooms: List[Dict] = self.synapse_admin_client.list_room()
        group_maps: List[Group2RoomMap] = [
            Group2RoomMap(
                authentik_api_obj=g,
                generated_matrix_room_attr=self.get_matrix_room_attrs_from_authentik_group(
                    g
                ),
            )
            for g in groups
        ]
        # add matrix api room obj to map if available
        for group_map in group_maps:
            matched_room_index: int = None
            for index, matrix_room_api_obj in enumerate(matrix_rooms):
                if (
                    group_map.generated_matrix_room_attr.canonical_alias
                    in matrix_room_api_obj["canonical_alias"]
                ):
                    matched_room_index = index
                    break
            if matched_room_index:
                group_map.matrix_api_obj = matrix_rooms.pop(matched_room_index)

        return group_maps

    def get_matrix_room_attrs_from_authentik_group(
        self, group: Dict
    ) -> MatrixRoomAttributes:
        room_settings: ConfigDefaultModel.MatrixRoomSettings = None

        if group["pk"] in self.config.per_authentik_group_pk_matrix_room_settings:
            room_settings = self.config.per_authentik_group_pk_matrix_room_settings[
                group["pk"]
            ]
        else:
            room_settings = self.config.matrix_room_default_settings
        alias = f"{room_settings.alias_prefix if not None else ''}{group[room_settings.matrix_alias_from_authentik_attribute]}"

        name = None
        if room_settings.matrix_name_from_authentik_attribute in group:
            name = f"{group[room_settings.matrix_name_from_authentik_attribute]}"
        name = f"{room_settings.name_prefix if not None else ''}{name}"

        topic = None
        if room_settings.matrix_topic_from_authentik_attribute in group:
            topic = group[room_settings.matrix_topic_from_authentik_attribute]
        topic = f"{room_settings.topic_prefix if not None else ''}{topic}"

        return MatrixRoomAttributes(
            alias=alias,
            canonical_alias=self.get_canonical_alias(alias, "#"),
            name=name,
            topic=topic,
            extra_params=room_settings.extra_params,
        )

    def get_authentik_accounts_with_mapped_synapse_account(self) -> List[UserMap]:
        authentik_users: List[UserMap] = [
            UserMap(
                authentik_api_obj=user,
                generated_matrix_id=self.get_matrix_user_id(user, None),
            )
            for user in self.authentik_client.list_users()
            if user["username"] not in self.config.authentik_user_ignore_list
        ]
        matched_users: List[UserMap] = []
        for matrix_user in self.synapse_admin_client.list_users():
            if matrix_user["name"] in self.config.matrix_user_ignore_list:
                continue
            # matrix_user is object from https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts
            user: UserMap | None = next(
                (
                    a_user
                    for a_user in authentik_users
                    if a_user.generated_matrix_id == matrix_user["name"]
                ),
                None,
            )
            if user:
                user.matrix_api_obj = user
                matched_users.append(user)
        return matched_users

    def get_matrix_user_id(
        self,
        authentik_user_api_object: Dict,
        fallback_value: Any,
        canonical: bool = True,
    ) -> str:
        attr_path_keys: List[
            str
        ] = self.config.sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute.split(
            "."
        )

        try:
            authentik_attr_val = walk_attr_path(
                authentik_user_api_object, attr_path_keys
            )
        except KeyError:
            if fallback_value:
                return fallback_value
            else:
                raise
        return (
            self.get_canonical_alias(authentik_attr_val, "@")
            if canonical
            else authentik_attr_val
        )

    def get_canonical_alias(
        self, local_name: str, prefix: Literal["#", "!", "@"] = "@"
    ) -> str:
        """_summary_

        Args:
            local_name (str): _description_
            prefix (Literal[, optional): '#' initiates a groupnames, '!' a space and '@' a username. Defaults to "@".

        Returns:
            str: _description_
        """
        return f"#{local_name}:{self.config.synapse_server.server_name}"
