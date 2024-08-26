from typing import List, Dict, TYPE_CHECKING, Literal, Optional
from pydantic import BaseModel
import copy
import logging

if TYPE_CHECKING:
    from onbot.bot import UserMap, Bot, Group2RoomMap

from onbot.utils import get_nested_dict_val_by_path, dict_has_nested_attr
from onbot.config import OnbotConfig

log = logging.getLogger(__name__)


class RoomPowerLevelState(BaseModel):

    old: Dict
    new: Optional[Dict] = None

    def get_current(self):
        if self.new is None:
            return self.old
        return self.new

    @property
    def has_changed(self):
        if self.new and self.new != self.old:
            return True
        return False


class AuthenikGroupMatrixRoomPowerLevelManager:

    def __init__(
        self,
        config: OnbotConfig,
        parent_bot: "Bot",
        authentik_group_rooms: List["Group2RoomMap"],
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
        print(
            "self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level",
            self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level,
        )
        self.authentik_group_api_obj_with_power_level_definition = self.bot.api_client_authentik.list_groups(
            filter_has_non_empty_attributes=[
                self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
            ]
        )
        log.debug(
            f"self.authentik_group_api_obj_with_power_level_definition {self.authentik_group_api_obj_with_power_level_definition}"
        )
        # sort groups that the group with the highest power level will be last in the list
        # this way when we need to overwrite contradicting power level rules the higher power level will always win.
        self.authentik_group_api_obj_with_power_level_definition = sorted(
            self.authentik_group_api_obj_with_power_level_definition,
            key=lambda x: (
                x["attributes"][
                    self.config.sync_matrix_rooms_based_on_authentik_groups.authentik_group_attr_for_matrix_power_level
                ]
            ),
            reverse=False,
        )
        log.debug(
            f"self.authentik_group_api_obj_with_power_level_definition {self.authentik_group_api_obj_with_power_level_definition}"
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

        log.debug(f"---Calculate room powers for room {room.matrix_obj.name}")

        room_members: List[UserMap] = (
            self.bot.get_authentik_accounts_with_mapped_synapse_account(
                from_matrix_room_id=room.matrix_obj.room_id
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
        log.debug(
            f"authentik_group_default_power_level:{authentik_group_default_power_level}"
        )
        log.debug(f"room_members:{room_members}")
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
                in power_level_authentik_group["users"]
            ):
                log.debug(
                    f"Set power level for user '{room_member}' to {authentik_group_default_power_level}"
                )
                new_power_levels["users"][
                    room_member.matrix_api_obj["name"]
                ] = authentik_group_default_power_level
            if new_power_levels != current_power_levels:
                self.room_power_levels[room.matrix_obj.room_id].new = new_power_levels

    def commit_new_power_levels(self):

        for room_id, power_levels in self.room_power_levels.items():
            if power_levels.has_changed:
                log.debug(f"Set new power level: {power_levels.new}")
                self.bot.api_client_matrix.set_room_power_levels(
                    room_id=room_id, power_levels=power_levels.new
                )
