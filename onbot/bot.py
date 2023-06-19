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
    RoomGetStateEventError,
    RoomGetStateResponse,
)
import uuid
from onbot.config import OnbotConfig
from onbot.authentik_api_client import AuthentikApiClient
from onbot.synapse_admin_api_client import SynapseAdminApiClient
from onbot.matrix_api_client import MatrixApiClient, SynapseApiError
from onbot.config import OnbotConfig
from onbot.utils import get_nested_dict_val_by_path, synchronize_async_helper
from enum import Enum

log = logging.getLogger(__name__)


class OnbotRoomStateEvents(str, Enum):
    create_onbot_room = "create_onbot_room"
    send_authentik_user_welcome_messages = "send_authentik_user_welcome_messages"
    create_space = "create_space"


class OnbotRoomTypes(str, Enum):
    group_room = "group_room"
    direct_room = "direct_welcome_room"


class MatrixRoomCreateAttributes(BaseModel):
    alias: str
    canonical_alias: str
    id: str = None
    name: str = None
    topic: str = None
    room_params: dict = None
    encrypted: bool = True


class TopicUnfetched:
    # topic values dont come with via the https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api endpoint
    # to signal that is not unconditional None but unkown/not yet fetched we need a custom type
    pass


class MatrixRoomAttributes(BaseModel):
    room_id: str
    canonical_alias: str = None
    name: str = None
    topic: str = TopicUnfetched
    is_space: bool = False
    room_type: OnbotRoomTypes = OnbotRoomTypes.group_room
    direct_room_user_id: str = None
    group_room_mapped_authentik_pk: str = None

    @classmethod
    def from_synapse_admin_api_obj(cls, obj: Dict, is_space=False):
        print("# obj", obj)
        return cls(
            room_id=obj["room_id"],
            canonical_alias=obj["canonical_alias"]
            if "canonical_alias" in obj
            else None,
            name=obj["name"] if "name" in obj else None,
            topic=obj["topic"] if "topic" in obj else None,
            is_space=is_space,
        )


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
    matrix_obj: MatrixRoomAttributes = None

    generated_matrix_room_attr: MatrixRoomCreateAttributes = None


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
        self._space_cache: MatrixRoomAttributes = None

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

        log.debug("DEEEBUG")
        while True:
            self.server_tik()

    def server_tik(self):
        # self.create_matrix_group_rooms_based_on_authentik_groups()
        # self.sync_users_and_space()
        self.create_direct_room_with_new_users_and_send_welcome_messages()
        # self.sync_users_and_rooms()

        log.debug(f"Wait {self.server_tick_wait_time_sec_int} for next servertick")
        time.sleep(self.server_tick_wait_time_sec_int)

    def create_direct_room_with_new_users_and_send_welcome_messages(self):
        mapped_users: List[
            UserMap
        ] = self.get_authentik_accounts_with_mapped_synapse_account()
        all_direct_onbot_rooms: List[MatrixRoomAttributes] = self._list_rooms(
            in_space_with_id=self._get_parent_space_if_needed().room_id,
            onbot_room_type=OnbotRoomTypes.direct_room,
        )

        for user in mapped_users:
            direct_chat_room = None
            existing_room_index = None
            for index, room in enumerate(all_direct_onbot_rooms):
                if room.direct_room_user_id == user.generated_matrix_id:
                    direct_chat_room = room
                    existing_room_index = index
                    break
            if direct_chat_room is not None:
                # lets remove the room from the list to speed up following loops
                all_direct_onbot_rooms.pop(existing_room_index)
            else:
                direct_chat_room = self._create_direct_room(
                    user.generated_matrix_id, space=self._get_parent_space_if_needed()
                )
            self._send_welcome_messages_if_not_done(direct_chat_room)

    def _send_welcome_messages_if_not_done(self, direct_room: MatrixRoomAttributes):
        room_state: Dict | RoomGetStateEventError = (
            self.matrix_api_client.get_room_state_event(
                room_id=direct_room.room_id,
                event_type=self._gen_event_type_name(
                    OnbotRoomStateEvents.send_authentik_user_welcome_messages
                ),
                raise_error=False,
            )
        )
        if isinstance(room_state, Dict):
            room_state_content: Dict[int, str] = room_state["content"]
        else:
            room_state_content: Dict[int, str] = {"send_msgs": {}}
        for index, message in enumerate(self.config.welcome_new_users_messages):
            if str(index) not in room_state_content["send_msgs"]:
                message_content = {
                    "msgtype": "m.text",
                    "body": message,
                }
                self.matrix_api_client.room_send(
                    room_id=direct_room.room_id,
                    content=message_content,
                    message_type="m.room.message",
                )
                room_state_content["send_msgs"][index] = message
                self.matrix_api_client.put_room_state_event(
                    direct_room.room_id,
                    event_type=self._gen_event_type_name(
                        OnbotRoomStateEvents.send_authentik_user_welcome_messages
                    ),
                    content=room_state_content,
                )

    def create_matrix_group_rooms_based_on_authentik_groups(self):
        parent_room_space = self._get_parent_space_if_needed()

        for group_room_map in self._get_authentik_groups_that_need_synapse_room():
            if group_room_map.matrix_obj is None:
                self._create_group_room(group_room_map, parent_room_space)

    def delete_matrix_rooms_mapped_to_extinguished_authentik_group(self):
        if (
            not self.config.sync_matrix_rooms_based_on_authentik_groups.disable_rooms_when_mapped_authentik_group_disappears
        ):
            return
        req_matrix_rooms_from_authentik_groups: List[
            Group2RoomMap
        ] = self._get_authentik_groups_that_need_synapse_room()
        req_matrix_rooms_from_authentik_group_ids = [
            r.authentik_api_obj["pk"] for r in req_matrix_rooms_from_authentik_groups
        ]
        for room in self._list_rooms(
            in_space_with_id=self._get_parent_space_if_needed(),
            onbot_room_type=OnbotRoomTypes.group_room,
        ):
            if (
                room.group_room_mapped_authentik_pk
                not in req_matrix_rooms_from_authentik_group_ids
            ):
                # room is obsolete.
                self.synapse_admin_client.delete_room(
                    room.room_id,
                    message=f"Room '{room.name}' was mapped to Authentik user group that is not synced to matrix anymore.",
                    purge=self.config.sync_matrix_rooms_based_on_authentik_groups.delete_disabled_rooms,
                )

    def _create_direct_room(
        self,
        user_id: str,
        space: MatrixRoomAttributes = None,
    ) -> MatrixRoomAttributes:
        room_create_response: RoomCreateResponse = self.matrix_api_client.create_room(
            parent_space_id=space.room_id, is_direct=True, invite=[user_id]
        )
        self.matrix_api_client.put_room_state_event(
            room_id=room_create_response.room_id,
            event_type=self._gen_event_type_name(
                OnbotRoomStateEvents.create_onbot_room
            ),
            content={
                "room_type": OnbotRoomTypes.direct_room,
                "direct_room_user_id": user_id,
            },
            state_key=uuid.uuid4().hex,
        )
        log.debug(
            f"Created direct room with id '{room_create_response.room_id}' for user {user_id}. Response (type:{type(room_create_response)}): {room_create_response}"
        )
        return self._list_rooms(
            search_term=room_create_response.room_id, in_space_with_id=space.room_id
        )[0]

    def _create_group_room(
        self, room_info: Group2RoomMap, space: MatrixRoomAttributes = None
    ) -> MatrixRoomAttributes:
        room_create_response: RoomCreateResponse = self.matrix_api_client.create_room(
            alias=room_info.generated_matrix_room_attr.alias,
            canonical_alias=room_info.generated_matrix_room_attr.canonical_alias,
            name=room_info.generated_matrix_room_attr.name,
            topic=room_info.generated_matrix_room_attr.topic,
            encrypted=room_info.generated_matrix_room_attr.encrypted,
            room_params=room_info.generated_matrix_room_attr.room_params,
            parent_space_id=space.room_id,
            is_direct=False,
        )
        log.debug(
            f"Created room with id '{room_create_response.room_id}'. Response (type:{type(room_create_response)}): {room_create_response}"
        )
        self.matrix_api_client.put_room_state_event(
            room_id=room_create_response.room_id,
            event_type=self._gen_event_type_name(
                OnbotRoomStateEvents.create_onbot_room
            ),
            content={
                "room_type": OnbotRoomTypes.group_room,
                "group_room_mapped_authentik_pk": room_info.authentik_api_obj["pk"],
            },
            state_key=uuid.uuid4().hex,
        )
        room_info.matrix_obj = self._list_rooms(
            search_term=room_create_response.room_id, in_space_with_id=space.room_id
        )[0]
        return room_info.matrix_obj

    def sync_users_and_space(self):
        space = self._get_parent_space_if_needed()
        if space is not None:
            current_members = self.synapse_admin_client.list_room_members(space.room_id)
            for user in self.get_authentik_accounts_with_mapped_synapse_account():
                user_name: str = user.matrix_api_obj["name"]
                if user_name not in current_members:
                    self.synapse_admin_client.add_user_to_room(space.room_id, user_name)

    def sync_users_and_rooms(self):
        mapped_users: List[
            UserMap
        ] = self.get_authentik_accounts_with_mapped_synapse_account()
        mapped_rooms: List[
            Group2RoomMap
        ] = self._get_authentik_groups_that_need_synapse_room()
        for room in mapped_rooms:
            room_members = self.synapse_admin_client.list_room_members(
                room.matrix_obj.room_id
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
                    f'SYNC USER {user.matrix_api_obj["name"]} GROUP {room.matrix_obj.room_id} user_is_member: {str(user_is_member),} user_should_be_member: {str(user_should_be_member)}'
                )
                if user_should_be_member and not user_is_member:
                    self.synapse_admin_client.add_user_to_room(
                        room.matrix_obj.room_id, user.matrix_api_obj["name"]
                    )
                elif (
                    not user_should_be_member
                    and user_is_member
                    and self.config.sync_authentik_users_with_matrix_rooms.kick_matrix_room_members_not_in_mapped_authentik_group_anymore
                ):
                    self.matrix_api_client.room_kick_user(
                        user.matrix_api_obj["name"],
                        room.matrix_obj.room_id,
                        "Automatically removed because of missing/revoked group membership in central user directory.",
                    )

    def _get_parent_space_if_needed(self) -> MatrixRoomAttributes | None:
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
            print("SPACE IS THERE", space)
            self._space_cache = self.synapse_admin_client.get_room_details(
                space["room_id"]
            )
            self._space_cache = MatrixRoomAttributes.from_synapse_admin_api_obj(
                space, is_space=True
            )
            return self._space_cache
        if (
            not self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled
        ):
            raise ValueError(
                f"Can not find space with canonical_alias '{target_space_canonical_alias}' and 'config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists' is disabled. Please make sure the room exists or allow me to create it with 'config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.enabled=true'"
            )

        # we need to create the space
        space_response: RoomCreateResponse = self.matrix_api_client.create_space(
            alias=self.config.create_matrix_rooms_in_a_matrix_space.alias,
            name=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.name,
            topic=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.topic,
            space_params=self.config.create_matrix_rooms_in_a_matrix_space.create_matrix_space_if_not_exists.space_params,
        )
        self.matrix_api_client.put_room_state_event(
            room_id=space_response.room_id,
            event_type=self._gen_event_type_name(OnbotRoomStateEvents.create_space),
            content={
                "created_by_authentik_server": self.config.authentik_server.public_api_url
            },
            state_key=uuid.uuid4().hex,
        )
        # now the room icreated we can just recall the function, as it will return the new space now
        return self._get_parent_space_if_needed()

    def _get_authentik_groups_that_need_synapse_room(self) -> List[Group2RoomMap]:
        if not self.config.sync_matrix_rooms_based_on_authentik_groups.enabled:
            return []
        query_attributes = {}
        if (
            self.config.sync_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes
        ):
            query_attributes = (
                self.config.sync_matrix_rooms_based_on_authentik_groups.only_groups_with_attributes
            )

        groups: Dict = self.authentik_client.list_groups(
            filter_by_attribute=query_attributes
        )

        if (
            self.config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with
        ):
            groups = [
                g
                for g in groups
                if g["name"].startswith(
                    self.config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with
                )
            ]

        if (
            self.config.sync_matrix_rooms_based_on_authentik_groups.only_for_children_of_groups_with_uid
        ):
            groups = [
                g
                for g in groups
                if g["parent"]
                in self.config.sync_matrix_rooms_based_on_authentik_groups.only_for_children_of_groups_with_uid
            ]
        space = self._get_parent_space_if_needed()
        matrix_rooms: List[MatrixRoomAttributes] = self._list_rooms(
            in_space_with_id=space.room_id if space else None
        )

        group_maps: List[Group2RoomMap] = [
            Group2RoomMap(
                authentik_api_obj=g,
                generated_matrix_room_attr=self._get_matrix_room_attrs_from_authentik_group(
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
                    == matrix_room_api_obj.canonical_alias
                ):
                    matched_room_index = index
                    break
            if matched_room_index is not None:
                group_map.matrix_obj = matrix_rooms.pop(matched_room_index)

        return group_maps

    def _get_matrix_room_attrs_from_authentik_group(
        self, group: Dict
    ) -> MatrixRoomCreateAttributes:
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
            key_path=room_settings.matrix_name_from_authentik_attribute.split("."),
            fallback_val=None,
        )
        name = f"{room_settings.name_prefix if not None else ''}{name if name is not None else ''}"

        topic = get_nested_dict_val_by_path(
            data=group,
            key_path=room_settings.matrix_topic_from_authentik_attribute.split("."),
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
        encrypted = room_settings.end2end_encryption_enabled
        alias = alias.replace("-", "")
        return MatrixRoomCreateAttributes(
            alias=alias,
            canonical_alias=self._get_canonical_alias(alias, "#"),
            name=name,
            topic=topic,
            room_params=room_create_params,
            encrypted=encrypted,
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
                        generated_matrix_id=self._get_matrix_user_id(user, None),
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

    def _get_matrix_user_id(
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
            self._get_canonical_alias(authentik_attr_val, "@")
            if canonical
            else authentik_attr_val
        )

    def _list_rooms(
        self,
        in_space_with_id: str = None,
        search_term: str = None,
        onbot_room_type: OnbotRoomTypes = None,
    ) -> List[MatrixRoomAttributes]:
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
            result_raw: List[Dict] = []
            space_rooms = self.matrix_api_client.space_list_rooms(in_space_with_id)
            for room in space_rooms:
                r = next(
                    (r for r in all_rooms if r["room_id"] == room["room_id"]), None
                )
                if r is not None:
                    result_raw.append(r)
        result: List[MatrixRoomAttributes] = [
            MatrixRoomAttributes.from_synapse_admin_api_obj(o) for o in result_raw
        ]
        for room in result:
            self._attach_onbot_room_type(room)
        return [
            r
            for r in result
            if r.room_type == onbot_room_type or onbot_room_type is None
        ]

    def _attach_onbot_room_type(self, room: MatrixRoomAttributes) -> Dict | None:
        print("room.room_id", room.room_id)
        event: Dict | RoomGetStateEventError = (
            self.matrix_api_client.get_room_state_event(
                room_id=room.room_id,
                event_type=self._gen_event_type_name(
                    OnbotRoomStateEvents.create_onbot_room
                ),
                raise_error=False,
            )
        )
        if isinstance(event, RoomGetStateEventError):
            if event.status_code == "M_NOT_FOUND":
                return room
            else:
                raise SynapseApiError.from_nio_response_error(event)

        log.debug(f"QUERY CREATE EVENT in room {room} {type(event)} {event}")
        if "content" in event:
            for key, val in event["content"].items():
                if key in list(room.dict().keys()):
                    setattr(room, key, val)
        return room

    def _gen_event_type_name(self, value: str):
        # return e.g. org.company.onbot.
        return f"{'.'.join(reversed(self.config.synapse_server.server_name.split('.')))}.onbot.{value}"

    def _get_canonical_alias(
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
