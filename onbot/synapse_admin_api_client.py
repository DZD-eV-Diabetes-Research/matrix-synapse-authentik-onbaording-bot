from typing import Dict, List
import logging
import requests

log = logging.getLogger(__name__)


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

    def list_room(self, in_space: str = None) -> List[Dict]:
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Args:
            in_space (_type_): _description_
        """
        rooms = []

        for room in self._get("rooms")["rooms"]:
            if room["room_type"] != "m.space":
                rooms.append(room)
        return rooms

    def list_space(self) -> List[Dict]:
        """https://matrix-org.github.io/synapse/latest/admin_api/rooms.html#list-room-api

        Returns:
            List[Dict]: _description_
        """
        spaces = []
        for room in self._get("rooms")["rooms"]:
            if room["room_type"] == "m.space":
                spaces.append(room)
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
