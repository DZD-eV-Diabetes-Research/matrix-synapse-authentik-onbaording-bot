from typing import List, Dict
import logging
import requests

log = logging.getLogger(__name__)


class MatrixApiClient:
    def __init__(
        self,
        access_token: str,
        server_domain: str,
        api_base_path: str = "/_matrix/client/v1",
        protocol: str = "http",
    ):
        self.access_token = access_token
        self.api_base_url = f"{protocol}://{server_domain}{api_base_path}/"

    def list_space_rooms(self, space_id) -> List[Dict]:
        # https://matrix.org/docs/api/#get-/_matrix/client/v1/rooms/-roomId-/hierarchy
        rooms: List[Dict] = self._get(f"rooms/{space_id}/hierarchy")["rooms"]
        return [room for room in rooms if room["room_id"] != space_id]

    def _build_api_call_url(self, path: str):
        if path.startswith("/"):
            path = path.lstrip("/")
        return f"{self.api_base_url}{path}"

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
