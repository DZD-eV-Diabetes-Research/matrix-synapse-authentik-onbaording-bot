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
        server_url: str,
        api_base_path: str = "/_synapse/admin",
    ):
        self.access_token = access_token
        self.api_base_url = f"{server_url}{api_base_path}/"

    def list_users(self) -> Dict:
        # https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#list-accounts
        return self._get("v2/users")["users"]

    def list_room_and_space(self) -> List[Dict]:
        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api
        return self._get("v1/rooms")["rooms"]

    def list_room(self, search_term: str = None) -> List[Dict]:
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Args:
            in_space (_type_): _description_
        """

        query = {"search_term": search_term} if search_term else None
        rooms = []

        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api
        for room in self._get("v1/rooms", query=query)["rooms"]:
            if room["room_type"] != "m.space":
                rooms.append(room)
        return rooms

    def list_room_members(self, room_id: str):
        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#room-members-api
        return self._get(f"/v1/rooms/{room_id}/members")["members"]

    def list_room_state(self, room_id: str):
        # TODO: Can be removed?
        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#room-state-api
        return self._get(f"v1/rooms/{room_id}/state")["state"]

    def list_space(self) -> List[Dict]:
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Returns:
            List[Dict]: _description_
        """
        spaces = []
        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api
        for room in self._get("v1/rooms")["rooms"]:
            if room["room_type"] == "m.space":
                spaces.append(room)
        return spaces

    def add_user_to_room(self, room_id: str, user_id: str):
        # https://matrix-org.github.io/synapse/latest/admin_api/room_membership.html
        if not room_id in self.get_user_rooms_joined(user_id):
            self._post(f"v1/join/{room_id}", json_body={"user_id": user_id})

    def get_user_rooms_joined(self, user_id: str):
        # https://matrix-org.github.io/synapse/develop/admin_api/user_admin_api.html#list-room-memberships-of-a-user
        return self._get(f"v1/users/{user_id}/joined_rooms")["joined_rooms"]

    def set_user_server_admin_state(self, user_id: str, admin: bool):
        # https://matrix-org.github.io/synapse/develop/admin_api/user_admin_api.html#change-whether-a-user-is-a-server-administrator-or-not
        raise NotImplementedError()

    def deactivate_account(self, user_id: str, gdpr_erease: bool):
        # https://matrix-org.github.io/synapse/develop/admin_api/user_admin_api.html#deactivate-account
        self._post(f"deactivate/{user_id}", json_body={"erase": gdpr_erease})

    def logout_account(self, user_id) -> List[Dict]:
        # https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#list-all-devices
        # https://matrix-org.github.io/synapse/latest/admin_api/user_admin_api.html#delete-a-device
        devices = self._get(f"v2/users/{user_id}/devices")
        for device in devices:
            self._delete(f"v2/users/{user_id}/devices/{device['device_id']}")

    def delete_room(
        self,
        room_id: str,
        purge: bool = False,
        force_purge: bool = False,
        message: str = None,
    ):
        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#delete-room-api
        body = {"purge": purge, "force_purge": force_purge}
        if message:
            body["message"] = message
        return self._delete(f"v1/rooms/{room_id}")

    def room_details(self, room_id: str):
        # https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#room-details-api
        return self._get(f"v1/rooms/{room_id}")

    def _build_api_call_url(self, path: str):
        return f"{self.api_base_url.rstrip('/')}/{path.lstrip('/')}"

    def _get(self, path: str, query: Dict = None) -> Dict:
        if query is None:
            query = {}
        r = requests.get(
            self._build_api_call_url(path),
            params=query,
            headers={
                "Authorization": self.access_token,
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return self._http_call_response_handler(r)

    def _post(self, path: str, json_body: Dict = None) -> Dict:
        if json_body is None:
            json_body = {}
        r = requests.post(
            self._build_api_call_url(path),
            json=json_body,
            headers={
                "Authorization": self.access_token,
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return self._http_call_response_handler(r)

    def _delete(self, path: str, json_body: Dict = None) -> Dict:
        if json_body is None:
            json_body = {}
        r = requests.delete(
            self._build_api_call_url(path),
            json=json_body,
            headers={
                "Authorization": self.access_token,
                "Accept": "application/json",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        return self._http_call_response_handler(r)

    def _http_call_response_handler(self, r: requests.Response):
        try:
            r.raise_for_status()
        except:
            log.error(f"Error for {r.request.method}-request at '{r.url}'")
            try:
                #  if possible log the payload, there may be some helpfull informations for debuging. lets output it before raising the error
                log.error(r.json())
            except:
                pass
            raise
        return r.json()
