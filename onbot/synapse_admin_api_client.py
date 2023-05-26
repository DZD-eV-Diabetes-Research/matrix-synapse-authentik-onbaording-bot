from typing import Dict, List
import logging
import requests

log = logging.getLogger(__name__)


class SynapseAdminApiError(Exception):
    pass


class SynapseAdminApiClient:
    def __init__(
        self,
        access_token: str,
        server_domain: str,
        api_base_path: str = "/_synapse/admin/v1",
        protocol: str = "http",
    ):
        self.access_token = access_token
        self.api_base_url = f"{protocol}://{server_domain}{api_base_path}/"

    def list_room(self, in_space_with_canonical_alias: str = None) -> List[Dict]:
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Args:
            in_space (_type_): _description_
        """
        if (
            in_space_with_canonical_alias
            and not in_space_with_canonical_alias.startswith("#")
        ):
            raise ValueError(
                f"Expected canonical space name like '#<your-alias>:<your-synapse-server-name>' got '{in_space_with_canonical_alias}'"
            )
        rooms = []

        for room in self._get("rooms")["rooms"]:
            # if in_space_with_canonical_alias and
            if room["room_type"] != "m.space":
                rooms.append(room)
        return rooms

    def list_room_members(self, room_id: str):
        return self._get(f"/rooms/{room_id}/members")["members"]

    def list_room_state(self, room_id: str):
        return self._get(f"/rooms/{room_id}/state")["state"]

    def list_space(self) -> List[Dict]:
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Returns:
            List[Dict]: _description_
        """
        spaces = []
        for room in self._get("rooms")["rooms"]:
            if 1 == 1:  # room["room_type"] == "m.space":
                if room["room_id"] in [
                    "!DbJRSjtmVTxctLHYVX:dzd-ev.org",
                    "!WVHWIMQpGbFbQXRRAY:dzd-ev.org",
                ]:
                    print("#######")
                    print("ROOM NAME", room["name"])
                    print("ROOM MEMBERS", self.list_room_members(room["room_id"]))
                    print("ROOM STATE", self.list_room_state(room["room_id"]))

                spaces.append(room)
        exit()
        return spaces

    def _build_api_call_url(self, path: str):
        if path.startswith("/"):
            path = path.lstrip("/")
        return f"{self.api_base_url}{path}"

    def _get(self, path: str, query: Dict = None) -> Dict:
        if query is None:
            query = {}
        print(self._build_api_call_url(path))
        r = requests.get(
            self._build_api_call_url(path),
            params=query,
            headers={
                "Authorization": self.access_token,
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        try:
            r.raise_for_status()
        except:
            try:
                #  if possible Authentik puts some helpful debuging info into the payload. lets output it before raising the error
                log.error(r.json())
            except:
                pass
            raise
        return r.json()
