from typing import List, Dict, Union, Any, Literal, TYPE_CHECKING
import logging
import json
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

from onbot.config import OnbotConfig
from onbot.authentik_api_client import AuthentikApiClient
from onbot.synapse_admin_api_client import SynapseAdminApiClient
from onbot.matrix_api_client import MatrixApiClient
from onbot.config import OnbotConfig
from onbot.utils import get_nested_dict_val_by_path

log = logging.getLogger(__name__)


class MatrixRoomAttributes(BaseModel):
    alias: str
    canonical_alias: str
    id: str = None
    name: str = None
    topic: str = None
    room_params: dict = None


class UserMap(BaseModel):
    # https://your-authentik.company/api/v3/#get-/core/users/ object
    authentik_api_obj: Dict = None

    # https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts object
    matrix_api_obj: Dict = None

    generated_matrix_id: str = None


class Group2RoomMap(BaseModel):
    # https://your-authentik.company/api/v3/#get-/core/groups/ object
    authentik_api_obj: Dict = None

    # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api object
    matrix_api_obj: Dict = None

    generated_matrix_room_attr: MatrixRoomAttributes = None


"""
Authentik
Matrix
Sync
Bot


"""


class Bot:
    def __init__(
        self,
        config: OnbotConfig,
        authentik_client: AuthentikApiClient,
        synapse_admin_api_client: SynapseAdminApiClient,
        matrix_api_client: MatrixApiClient,
        server_tick_wait_time_sec_int: int = 60,
    ):
        self.config = config
        self.authentik_client = authentik_client
        self.synapse_admin_client = synapse_admin_api_client
        self.matrix_api_client = matrix_api_client
        self.server_tick_wait_time_sec_int = server_tick_wait_time_sec_int
        self._space_cache: Dict = None

    def start(self):
        """
        from urllib.parse import quote

        room_id = self.matrix_api_client.resolve_alias(
            quote("#dea3e18eccf147e6b2fc7d27fe7bce9a:dzd-ev.org")
        )
        print(room_id)
        print(self.synapse_admin_client.room_details(room_id))
        print(
            self.synapse_admin_client.delete_room(
                room_id,
                purge=True,
                force_purge=True,
            )
        )
        """
        # todo you are here
        # DZDChatUsers
        result = self.matrix_api_client._call_nio_client(
            nio_func=MatrixNioClient.room_put_state,
            params={
                "room_id": "!MNOXBIICSTScjIlkFE:dzd-ev.org",
                "event_type": "m.onbot.created",
                "content": {"m.onbot": "based onm group authentik bla"},
                "state_key": "iuewaf32iod327iub",
            },
        )
        print(type(result), result)
        result = self.matrix_api_client._call_nio_client(
            nio_func=MatrixNioClient.room_get_state_event,
            params={
                "room_id": "!MNOXBIICSTScjIlkFE:dzd-ev.org",
                "event_type": "m.onbot.created",
                "state_key": "iuewaf32iod327iub",
            },
        )
        print(type(result), result)
        exit()

        log.debug("DEEEBUG")
        while True:
            self.server_tik()

    def server_tik(self):
        self.create_matrix_rooms_based_on_authentik_groups()
        self.sync_users_and_rooms()
        log.debug(f"Wait {self.server_tick_wait_time_sec_int} for next servertick")
        time.sleep(self.server_tick_wait_time_sec_int)

    def create_matrix_rooms_based_on_authentik_groups(self):
        parent_room_space = self.get_parent_space_if_needed()

        for group_room_map in self.get_authentik_groups_that_need_synapse_room():
            if group_room_map.matrix_api_obj is None:
                self.create_room(group_room_map, parent_room_space)

    def create_room(self, room_info: Group2RoomMap, space: Dict = None) -> Dict:
        room_create_response: RoomCreateResponse = self.matrix_api_client.create_room(
            alias=room_info.generated_matrix_room_attr.alias,
            canonical_alias=room_info.generated_matrix_room_attr.canonical_alias,
            name=room_info.generated_matrix_room_attr.name,
            topic=room_info.generated_matrix_room_attr.topic,
            room_params=room_info.generated_matrix_room_attr.room_params,
            parent_space_id=space["room_id"],
        )
        log.debug(
            f"Created room with id '{room_create_response.room_id}'. Response (type:{type(room_create_response)}): {room_create_response}"
        )
        room_info.matrix_api_obj = self.list_rooms(
            search_term=room_create_response.room_id, in_space_with_id=space["room_id"]
        )[0]
        return room_info.matrix_api_obj

    def sync_users_and_rooms(self):
        mapped_users: List[
            UserMap
        ] = self.get_authentik_accounts_with_mapped_synapse_account()
        mapped_rooms: List[
            Group2RoomMap
        ] = self.get_authentik_groups_that_need_synapse_room()
        print("len:mapped_users", len(mapped_users))
        print("mapped_users", mapped_users)
        for room in mapped_rooms:
            print("room.matrix_api_obj", room.matrix_api_obj)
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
                user_is_member: bool = bool(
                    next(
                        (
                            member
                            for member in room_members
                            if member == user.matrix_api_obj["name"]
                        ),
                        False,
                    )
                )
                log.debug(
                    " ".join(
                        [
                            "SYNC USER",
                            user.matrix_api_obj["name"],
                            "GROUP",
                            room.matrix_api_obj["room_id"],
                            "user_is_member:",
                            str(user_is_member),
                            "user_should_be_member:",
                            str(user_should_be_member),
                        ]
                    )
                )
                print()
                if user_should_be_member and not user_is_member:
                    self.synapse_admin_client.add_user_to_room(
                        room.matrix_api_obj["room_id"], user.matrix_api_obj["name"]
                    )
                elif (
                    not user_should_be_member
                    and user_is_member
                    and self.config.sync_authentik_users_with_matrix_rooms.kick_matrix_room_members_not_in_mapped_authentik_group_anymore
                ):
                    self.matrix_api_client.room_kick_user(
                        user.matrix_api_obj["name"],
                        room.matrix_api_obj["room_id"],
                        "Automatically removed because of missing group membership in central user directory.",
                    )

    def get_parent_space_if_needed(self) -> Dict | None:
        if self._space_cache is not None:
            return self._space_cache
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
            self._space_cache = space
            return space
        if (
            not self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled
        ):
            raise ValueError(
                f"Can not find space with canonical_alias '{target_space_canonical_alias}' and 'config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists' is disabled. Please make sure the room exists or allow me to create it with 'config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled=true'"
            )

        # we need to create the space
        self.matrix_api_client.create_space(
            alias=self.config.create_matrix_rooms_in_a_matrix_space.alias,
            name=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name,
            topic=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic,
            space_params=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.space_params,
        )
        # now the room icreated we can just recall the function, as it will return the new space now
        return self.get_parent_space_if_needed()

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
        space = self.get_parent_space_if_needed()
        matrix_rooms: List[Dict] = self.list_rooms(
            in_space_with_id=space["room_id"] if space else None
        )

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
                    == matrix_room_api_obj["canonical_alias"]
                ):
                    matched_room_index = index
                    break
            if matched_room_index is not None:
                group_map.matrix_api_obj = matrix_rooms.pop(matched_room_index)

        return group_maps

    def get_matrix_room_attrs_from_authentik_group(
        self, group: Dict
    ) -> MatrixRoomAttributes:
        room_settings: OnbotConfig.MatrixDynamicRoomSettings = None
        room_default_settings = self.config.matrix_room_default_settings
        if group["pk"] in self.config.per_authentik_group_pk_matrix_room_settings:
            room_settings = OnbotConfig.MatrixDynamicRoomSettings.parse_obj(
                room_default_settings.dict()
                | self.config.per_authentik_group_pk_matrix_room_settings[
                    group["pk"]
                ].dict()
            )
        else:
            room_settings = room_default_settings
        group_name = group[room_settings.matrix_alias_from_authentik_attribute]
        alias = f"{room_settings.alias_prefix if room_settings.alias_prefix is not None else ''}{group_name if group_name is not None else ''}"

        name = get_nested_dict_val_by_path(
            data=group,
            path=room_settings.matrix_name_from_authentik_attribute.split("."),
            fallback_val=None,
        )
        name = f"{room_settings.name_prefix if not None else ''}{name if name is not None else ''}"

        topic = get_nested_dict_val_by_path(
            data=group,
            path=room_settings.matrix_topic_from_authentik_attribute.split("."),
            fallback_val=None,
        )

        topic = f"{room_settings.topic_prefix if room_settings.topic_prefix is not None else ''}{topic if topic is not None else ''}"

        room_create_params: Dict = room_settings.default_room_create_params
        if room_settings.matrix_room_create_params_from_authentik_attribute:
            custom_room_attr_raw_json: str = get_nested_dict_val_by_path(
                group,
                room_settings.matrix_room_create_params_from_authentik_attribute,
                fallback_val=None,
            )
            if custom_room_attr_raw_json:
                custom_room_attr = json.loads(custom_room_attr_raw_json)
                room_create_params = room_create_params | custom_room_attr

        alias = alias.replace("-", "")
        return MatrixRoomAttributes(
            alias=alias,
            canonical_alias=self.get_canonical_alias(alias, "#"),
            name=name,
            topic=topic,
            room_params=room_create_params,
        )

    def get_authentik_accounts_with_mapped_synapse_account(self) -> List[UserMap]:
        allowed_authentik_user_pathes = (
            self.config.sync_authentik_users_with_matrix_rooms.sync_only_users_in_authentik_pathes
        )
        allowed_authentik_user_pathes = (
            allowed_authentik_user_pathes if allowed_authentik_user_pathes else [None]
        )

        required_user_authentik_attributes = (
            self.config.sync_authentik_users_with_matrix_rooms.sync_only_users_with_authentik_attributes
        )

        allowed_authentik_user_group_pks = (
            self.config.sync_authentik_users_with_matrix_rooms.sync_only_users_of_groups_with_id
        )

        authentik_users: List[UserMap] = []
        for path in allowed_authentik_user_pathes:
            for user in self.authentik_client.list_users(
                filter_by_path=path,
                filter_by_attribute=required_user_authentik_attributes,
                filter_groups_by_pk=allowed_authentik_user_group_pks,
            ):
                if user["username"] in self.config.authentik_user_ignore_list:
                    continue
                authentik_users.append(
                    UserMap(
                        authentik_api_obj=user,
                        generated_matrix_id=self.get_matrix_user_id(user, None),
                    )
                )
            print("authentik_users", authentik_users)

        matched_users: List[UserMap] = []
        for matrix_user in self.synapse_admin_client.list_users():
            if matrix_user["name"] in self.config.matrix_user_ignore_list:
                continue
            # matrix_user is object from https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts
            for authentik_user in authentik_users:
                print("authentik_user", type(authentik_user), authentik_user)
                if authentik_user.generated_matrix_id == matrix_user["name"]:
                    authentik_user.matrix_api_obj = matrix_user
                    matched_users.append(authentik_user)
        print("matched_users", matched_users)
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
            authentik_attr_val = get_nested_dict_val_by_path(
                authentik_user_api_object, attr_path_keys
            )
        except KeyError:
            log.debug(
                f"Can not determine matrix name for authentik user {authentik_user_api_object}. Missing attributes."
            )
            if fallback_value:
                return fallback_value
            else:
                raise
        return (
            self.get_canonical_alias(authentik_attr_val, "@")
            if canonical
            else authentik_attr_val
        )

    def list_rooms(
        self, in_space_with_id: str = None, search_term: str = None
    ) -> List[Dict]:
        all_rooms = self.synapse_admin_client.list_room(search_term=search_term)
        if in_space_with_id is None:
            return all_rooms
        elif (
            not isinstance(in_space_with_id, str)
            or in_space_with_id
            and not in_space_with_id.startswith("!")
        ):
            raise ValueError(
                f"Expected room_id of space like '!<room_id>:<your-synapse-server-name>' got '{in_space_with_id}'"
            )
        else:
            result: List[Dict] = []
            space_rooms = self.matrix_api_client.space_list_rooms(in_space_with_id)
            for room in space_rooms:
                r = next(
                    (r for r in all_rooms if r["room_id"] == room["room_id"]), None
                )
                if r is not None:
                    result.append(r)
        return result

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
        return f"{prefix}{local_name}:{self.config.synapse_server.server_name}"
