from typing import List, Dict, Union
import json
import requests
import logging

log = logging.getLogger(__name__)


class AuthentikApiClient:
    def __init__(self, access_token: str, server_api_base_url: str):
        self.access_token = access_token
        self.server_api_base_url = (
            server_api_base_url
            if server_api_base_url.endswith("/")
            else f"{server_api_base_url}/"
        )

    def list_users(
        self,
        filter_groups_by_name: Union[str, List[str]] = None,
        filter_groups_by_pk: Union[str, List[str]] = None,
        filter_by_attribute: Dict = None,
        filter_is_superuser: bool = None,
        filter_is_active: bool = True,
    ) -> Dict:
        query = {
            "groups_by_name": filter_groups_by_name,
            "groups_by_pk": filter_groups_by_pk,
            "attributes": json.dumps(filter_by_attribute),
            "is_superuser": filter_is_superuser,
            "is_active": filter_is_active,
        }
        return self._get("/core/users/", query)["results"]

    def list_groups(
        self,
        filter_members_by_username: Union[str, List[str]] = None,
        filter_members_by_pk: Union[str, List[str]] = None,
        filter_by_attribute: Dict = None,
        filter_is_superuser: bool = None,
    ) -> Dict:
        query = {
            "members_by_username": filter_members_by_username,
            "members_by_pk": filter_members_by_pk,
            "attributes": json.dumps(filter_by_attribute),
            "is_superuser": filter_is_superuser,
        }
        return self._get("/core/groups/", query)["results"]

    def _build_api_call_url(self, path: str):
        if path.startswith("/"):
            path = path.lstrip("/")
        return f"{self.server_api_base_url}{path}"

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


ac = AuthentikApiClient(
    access_token="Bearer xxx",
    server_api_base_url="https://auth.dzd-ev.org/api/v3",
)
print(ac.list_users(filter_by_attribute={"first-name": "Tim"}))
