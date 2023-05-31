from typing import List, Dict, Union
import logging
import asyncio
from pydantic import BaseModel
from nio import (
    AsyncClient as MatrixNioClient,
    ErrorResponse,
    RoomCreateResponse,
    RoomCreateError,
    RoomVisibility,
    RoomPreset,
    MatrixRoom,
)

from onbot.config import ConfigDefaultModel
from onbot.authentik_api_client import AuthentikApiClient
from onbot.synapse_admin_api_client import SynapseAdminApiClient
from onbot.matrix_api_client import MatrixApiClient
from onbot.config import ConfigDefaultModel

log = logging.getLogger(__name__)


class MatrixRoomAttributes(BaseModel):
    alias: str
    id: str = None
    name: str = None
    topic: str = None
    extra_params: dict = None

    def get_canonical_alias(self, server_name: str):
        return f"#{self.alias}:{server_name}"


class SynapseApiError(Exception):
    @classmethod
    def from_nio_response_error(cls, nio_response_error: ErrorResponse):
        # Tim Todo: add_note with python 3.11. will awesome!
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
    ):
        self.config = config
        self.authentik_client = authentik_client
        self.synapse_admin_client = synapse_admin_api_client
        self.synapse_client = matrix_nio_client
        self.synpase_api_client = matrix_api_client

    def server_tik(self):
        ### Create rooms if needed
        space = self.get_parent_space_if_needed()

        existing_matrix_rooms: List[Dict] = self.synapse_admin_client.list_room(
            in_space_with_canonical_alias=space
        )
        for authentik_group in self.get_authentik_groups_that_need_synapse_room():
            room_attr: MatrixRoomAttributes = (
                self.get_matrix_room_attr_from_authentik_group(authentik_group)
            )
            if not room_attr.get_canonical_alias(
                self.config.synapse_server.server_name
            ) in [r["room_id"] for r in existing_matrix_rooms]:
                self.create_room(room_attr=room_attr)
        # refresh existing rooms
        existing_matrix_rooms: List[Dict] = self.synapse_admin_client.list_room(
            in_space_with_canonical_alias=space
        )
        ### distribute users to rooms
        for user in self.get_synapse_accounts_with_authentik_account():
            pass

    def sync_users_and_rooms(self):
        for user in self.get_synapse_accounts_with_authentik_account():
            pass

    def get_authentik_groups_that_need_synapse_room(self) -> List[Dict]:
        if not self.config.create_matrix_rooms_based_on_authentik_groups.enabled:
            return []
        query_attributes = {}
        if (
            self.config.create_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes
        ):
            query_attributes = (
                self.config.create_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes
            )
        groups: List[Dict] = self.authentik_client.list_groups(
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
        return groups

    def get_matrix_room_attr_from_authentik_group(
        self, group: Dict
    ) -> MatrixRoomAttributes:
        room_settings: ConfigDefaultModel.MatrixRoomSettings = None

        if group["pk"] in self.config.per_authentik_group_pk_matrix_room_settings:
            room_settings = self.config.per_authentik_group_pk_matrix_room_settings[
                group["pk"]
            ]
        else:
            room_settings = self.config.matrix_room_default_settings
        alias = f"{room_settings.alias_prefix if not None else ''}{group[room_settings.alias_from_authentik_attribute]}"

        name = None
        if room_settings.name_from_authentik_attribute in group:
            name = f"{group[room_settings.name_from_authentik_attribute]}"
        name = f"{room_settings.name_prefix if not None else ''}{name}"

        topic = None
        if room_settings.topic_from_authentik_attribute in group:
            topic = group[room_settings.topic_from_authentik_attribute]
        topic = f"{room_settings.topic_prefix if not None else ''}{topic}"

        return MatrixRoomAttributes(
            alias=alias, name=name, topic=topic, extra_params=room_settings.extra_params
        )

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

    def create_room(self, room_attr: MatrixRoomAttributes) -> RoomCreateResponse:
        async def create_room():
            return await self.synapse_client.room_create(
                space=False,
                alias=room_attr.alias,
                name=room_attr.name,
                topic=room_attr.topic,
                **room_attr.extra_params,
            )

        target_room_canonical_alias = room_attr.get_canonical_alias(
            self.config.synapse_server.server_name
        )
        room: Union[RoomCreateResponse, RoomCreateError] = asyncio.run(create_room())
        if type(room) == RoomCreateError:
            log.error(
                f"Could not create room with the alias '{target_room_canonical_alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(room.message)
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

    def get_synapse_accounts_with_authentik_account(self) -> List[Dict]:
        self.config.sync_authentik_users_with_matrix_rooms.enabled
        self.config.sync_authentik_users_with_matrix_rooms
        self.authentik_client.list_users()

    def create_room_test(self, name):
        self.synapse_client.room_create
