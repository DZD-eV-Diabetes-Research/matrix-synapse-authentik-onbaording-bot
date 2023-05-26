from typing import List, Dict, Union
import logging
import asyncio
from nio import (
    HttpClient as MatrixClient,
    RoomCreateResponse,
    RoomCreateError,
    RoomVisibility,
    RoomPreset,
    MatrixRoom,
)

from onbot.config import ConfigDefaultModel
from onbot.authentik_api_client import AuthentikApiClient
from onbot.synapse_admin_api_client import SynapseAdminApiClient

log = logging.getLogger(__name__)


class Bot:
    def __init__(
        self,
        config: ConfigDefaultModel,
        authentik_client: AuthentikApiClient,
        synapse_admin_client: SynapseAdminApiClient,
        synapse_client: MatrixClient,
    ):
        self.config = config
        self.authentik_client = authentik_client
        self.synapse_admin_client = synapse_admin_client
        self.synapse_client = synapse_client

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
        async def create_room():
            return await self.synapse_client.room_create(
                space=True,
                alias=self.config.create_matrix_rooms_in_a_matrix_space.alias,
                name=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name,
                topic=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic,
                **self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.extra_params,
            )

        room: Union[RoomCreateResponse, RoomCreateError] = asyncio.run(create_room())
        if type(room) == RoomCreateError:
            log.error(
                f"Could not create the parent space with the alias '{target_space_canonical_alias}'. Dont know what to do. Here is the error:"
            )
            raise RoomCreateError
        else:
            # we now can just recall the function because the room exists now.
            return self.get_parent_space_if_needed()

    def get_synapse_accounts_with_authentik_account(self) -> List[Dict]:
        # todo
        self.authentik_client.list_users()

    def create_room_test(self, name):
        self.synapse_client.room_create
