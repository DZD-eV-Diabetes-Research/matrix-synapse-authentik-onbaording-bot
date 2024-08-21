from typing import List, Dict, TYPE_CHECKING, Literal, Optional
from pydantic import BaseModel
import copy

if TYPE_CHECKING:
    from onbot.bot import UserMap, Bot, Group2RoomMap

from onbot.utils import get_nested_dict_val_by_path, dict_has_nested_attr
from onbot.config import OnbotConfig


class AuthentikPowerLevelGroup(BaseModel):
    authentik_api_group_obj: Dict
    group_members_matrix_id: List[str] = None
    power_level: int = 0


class RoomPowerLevelState(BaseModel):

    old: Dict
    new: Optional[Dict] = None

    def get_current(self):
        if self.new is None:
            return self.old
        return self.new

    @property
    def have_changed(self):
        if self.new != self.old:
            return True
        return False


class AuthenikGroupMatrixRoomPowerLevelManager:

    def __init__(
        self,
        config: OnbotConfig,
        parent_bot: "Bot",
        authentik_group_rooms: Dict[str, "Group2RoomMap"],
    ):
        self.config = config
        self.bot = parent_bot
        self.room_power_levels: Dict[str, RoomPowerLevelState] = None
        self.synced_users: List["UserMap"] = (
            self.bot.get_authentik_accounts_with_mapped_synapse_account()
        )
        self.synced_super_users: List["UserMap"] = [
            u for u in self.synced_users if u.authentik_api_obj["is_superuser"] == True
        ]
        self.authentik_group_rooms: List["Group2RoomMap"] = authentik_group_rooms
        self.authentik_group_api_obj_with_power_level_definition = self.bot.api_client_authentik.list_groups(
            filter_has_non_empty_attributes="attributes."
            + self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
        )
        # sort groups that the group with the highest power level will be last in the list
        self.authentik_group_api_obj_with_power_level_definition = sorted(
            self.authentik_group_api_obj_with_power_level_definition,
            key=lambda x: (
                x["attributes"][
                    self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
                ]
            ),
            reverse=False,
        )

    def set_power_levels(self):
        self.room_power_levels = {}
        for (
            power_level_group
        ) in self.authentik_group_api_obj_with_power_level_definition:
            for room in self.authentik_group_rooms:
                self.calculate_power_level_for_authentik_group_in_room(
                    power_level_group, room
                )
        self.commit_new_power_levels()

    def calculate_power_level_for_authentik_group_in_room(
        self, power_level_authentik_group: Dict, room: "Group2RoomMap"
    ):
        room_members: List[UserMap] = (
            self.bot.get_authentik_accounts_with_mapped_synapse_account(
                from_matrix_room_id=room.generated_matrix_room_attr
            )
        )
        authentik_group_default_power_level = get_nested_dict_val_by_path(
            power_level_authentik_group,
            (
                "attributes."
                + self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
            ).split("."),
            fallback_val=None,
        )
        for room_member in room_members:
            # power level dict structure: https://spec.matrix.org/v1.11/client-server-api/#mroompower_levels
            if room.matrix_obj.room_id not in self.room_power_levels:
                self.room_power_levels[room.matrix_obj.room_id] = RoomPowerLevelState(
                    old=self.bot.api_client_matrix.get_room_power_levels(
                        room_id=room.matrix_obj.room_id
                    )
                )

            current_power_levels = self.room_power_levels[
                room.matrix_obj.room_id
            ].get_current()
            new_power_levels = copy.deepcopy(current_power_levels)
            if (
                room_member.authentik_api_obj["pk"]
                == power_level_authentik_group["users"]
            ):
                new_power_levels["users"][
                    room_member.matrix_api_obj["name"]
                ] = authentik_group_default_power_level
        if new_power_levels != current_power_levels:
            self.room_power_levels[room.matrix_obj.room_id].new = new_power_levels

    def commit_new_power_levels(self):
        for room_id, power_levels in self.room_power_levels.items():
            if power_levels.have_changed():
                self.bot.api_client_matrix.set_room_power_levels(
                    room_id=room_id, power_levels=power_levels.new
                )

        return
        room_members: List[str] = self.bot.api_client_synapse_admin.list_room_members(
            room.matrix_obj.room_id
        )
        for member_matrix_id in room_members:
            if member_matrix_id:
                pass

        authentik_group_default_power_level = get_nested_dict_val_by_path(
            authentik_group_room.authentik_api_obj,
            (
                "attributes."
                + self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
            ).split("."),
            fallback_val=None,
        )
        group_room_members: List["UserMap"] = [
            g.generated_matrix_id
            for g in self.bot.get_authentik_accounts_with_mapped_synapse_account(
                from_matrix_room_id=authentik_group_room.generated_matrix_room_attr
            )
        ]
        # the bot always has full power
        user_power_levels: Dict[str, str] = {
            self.config.synapse_server.bot_user_id: 100
        }

    def set_room_power_levels_according_to_authentik_attr(self):
        """Set synapse user power levels per room based on Authentik custom attributes"""
        # ToDo: This function a waaay to large and complex. but a large refactor of this whole bot class is necessary anyway. Just a poc at the moment

        authentik_power_level_groups: List[AuthentikPowerLevelGroup] = []
        if (
            self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
        ):
            for group in self.bot.api_client_authentik.list_groups():
                power_level = get_nested_dict_val_by_path(
                    group,
                    (
                        "attributes."
                        + self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
                    ).split("."),
                    fallback_val=None,
                )
                if power_level is not None:
                    power_level_group = AuthentikPowerLevelGroup(
                        authentik_api_group_obj=group, power_level=power_level
                    )
                    power_level_group.group_members_matrix_id = [
                        self.bot._get_matrix_user_id(u)
                        for u in power_level_group.authentik_api_group_obj["users_obj"]
                    ]
                    authentik_power_level_groups.append(power_level_group)
            authentik_power_level_groups.sort(key=lambda x: x.power_level)

        ## Set power levels per room
        for group_room in self.bot._get_authentik_groups_that_need_synapse_room():
            group_room_members_matrix_id = [
                g.generated_matrix_id
                for g in self.bot.get_authentik_accounts_with_mapped_synapse_account(
                    from_matrix_room_id=group_room.generated_matrix_room_attr
                )
            ]
            user_power_levels: Dict[str, str] = {
                self.config.synapse_server.bot_user_id: 100
            }
            room_id: str = group_room.matrix_obj["room_id"]
            room_members: List[str] = (
                self.bot.api_client_synapse_admin.list_room_members(room_id=room_id)
            )
            ## Set power levels based on power level groups
            if authentik_power_level_groups:
                for power_level_group in authentik_power_level_groups:
                    room_members_with_power_level_group_membership = [
                        u
                        for u in room_members
                        if u in power_level_group.group_members_matrix_id
                    ]
                    for (
                        power_level_user_matrix_id
                    ) in room_members_with_power_level_group_membership:
                        user_power_levels[power_level_user_matrix_id] = (
                            power_level_group.power_level
                        )

            ## Set all authentik power user as room admins
            if (
                self.config.sync_matrix_rooms_based_on_authentik_groups.make_authentik_superusers_matrix_room_admin
            ):
                for matrix_username in room_members:
                    if matrix_username in [
                        u.matrix_api_obj["name"] for u in synced_super_users
                    ]:
                        user_power_levels[matrix_username] = 100

            for mapped_room_member in group_room_members_matrix_id:
                if mapped_room_member not in user_power_levels:
                    user_power_levels[mapped_room_member] = 0
            power_levels = self.bot.api_client_matrix.get_room_power_levels(
                room_id=room_id
            )
            power_levels["users"] = power_level["users"] | user_power_levels
            self.api_client_matrix.set_room_power_levels(
                room_id=room_id, power_levels=power_levels
            )
