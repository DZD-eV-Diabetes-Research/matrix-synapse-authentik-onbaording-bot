from typing import List, Dict, Union
import logging
import asyncio
from pydantic import BaseModel
from nio import (
    HttpClient as MatrixNioClient,
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

log = logging.getLogger(__name__)


class MatrixRoomAttributes(BaseModel):
    canonical_alias: str
    name: str = None
    topic: str = None
    extra_params: dict = None


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
        pass

    def get_authentik_groups_that_need_synapse_room(self) -> List[Dict]:
        if not self.config.create_matrix_rooms_based_on_authentik_groups:
            return []
        query_attributes = {}
        if (
            self.config.create_matrix_rooms_only_for_authentik_groups_with_attribute.enabled
        ):
            query_attributes[
                self.config.create_matrix_rooms_only_for_authentik_groups_with_attribute.attribute_key
            ] = (
                self.config.create_matrix_rooms_only_for_authentik_groups_with_attribute.attribute_val
            )
        groups: List[Dict] = self.authentik_client.list_groups(
            filter_by_attribute=query_attributes
        )
        if (
            self.config.create_matrix_rooms_only_for_authentik_groups_starting_with.enabled
            and self.config.create_matrix_rooms_only_for_authentik_groups_starting_with.value
        ):
            groups_filtered = []
            for group in groups:
                if group["name"].startswith(
                    self.config.create_matrix_rooms_only_for_authentik_groups_starting_with.value
                ):
                    groups_filtered.append(group)
            groups = groups_filtered
        return groups

    def get_matrix_room_attr_from_authentik_group(
        self, group: Dict
    ) -> MatrixRoomAttributes:
        if group["pk"] in self.config.per_authentik_group_pk_matrix_room_settings:
            room_settings = self.config.per_authentik_group_pk_matrix_room_settings[
                group["pk"]
            ]
        else:
            room_settings = self.config.matrix_room_default_settings
        # todo

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

    def get_room_and_create_if_not_exists(self, alias: str) -> Dict:
        existent_rooms: List[Dict] = None
        if self.config.create_matrix_rooms_in_a_matrix_space.enabled:
            self.synapse_admin_client.list_room(
                in_space=self.config.create_matrix_rooms_in_a_matrix_space.alias
            )

        async def create_room():
            return await self.synapse_client.room_create(
                space=False,
                alias=self.config.create_matrix_rooms_in_a_matrix_space.alias,
                name=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name,
                topic=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic,
                **self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.extra_params,
            )

    def get_synapse_accounts_with_authentik_account(self) -> List[Dict]:
        # todo
        self.authentik_client.list_users()

    def create_room_test(self, name):
        self.synapse_client.room_create
