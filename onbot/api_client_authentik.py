from typing import List, Dict, Union
import json
import requests
import logging

from onbot.utils import dict_has_nested_attr

log = logging.getLogger(__name__)


class AuthentikApiError(Exception):
    pass


class ApiClientAuthentik:
    def __init__(self, access_token: str, url: str):
        self.access_token = access_token
        self.url = url if url.endswith("/") else f"{url}/"

    def list_users(
        self,
        filter_groups_by_name: Union[str, List[str]] = None,
        filter_groups_by_pk: Union[str, List[str]] = None,
        filter_by_path: str = None,
        filter_by_attribute: Union[str, Dict] = None,
        filter_is_superuser: bool = None,
        filter_is_active: bool = True,
    ) -> Dict:
        if isinstance(filter_by_attribute, dict):
            filter_by_attribute = json.dumps(filter_by_attribute)

        query = {
            "groups_by_name": filter_groups_by_name,
            "groups_by_pk": filter_groups_by_pk,
            "attributes": filter_by_attribute,
            "is_superuser": filter_is_superuser,
            "is_active": filter_is_active,
            "path": filter_by_path,
        }
        return self._get("/core/users/", query)["results"]

    def list_groups(
        self,
        filter_members_by_username: Union[str, List[str]] = None,
        filter_members_by_pk: Union[str, List[str]] = None,
        filter_by_attribute: Dict = None,
        filter_has_attributes: List[str] = None,
        filter_has_non_empty_attributes: List[str] = None,
        filter_is_superuser: bool = None,
        include_inactive_users_obj: bool = False,
    ) -> Dict:
        query = {
            "members_by_username": filter_members_by_username,
            "members_by_pk": filter_members_by_pk,
            "attributes": (
                json.dumps(filter_by_attribute) if filter_by_attribute else None
            ),
            "is_superuser": filter_is_superuser,
        }
        # https://auth.mycompany.org/api/v3/#get-/core/groups/
        groups = self._get("/core/groups/", query)["results"]
        if not include_inactive_users_obj:
            for group in groups:
                self._remove_inactive_users_from_group_user_list(group)
        if filter_has_attributes:
            new_group_list = []
            for group in groups:
                for fattr in filter_has_attributes:
                    if dict_has_nested_attr(
                        group["attributes"], fattr.split("."), must_have_val=False
                    ):
                        new_group_list.append(group)
            groups = new_group_list
        if filter_has_non_empty_attributes:
            print("HERE WE GO", filter_has_non_empty_attributes)
            new_group_list = []
            for group in groups:
                print(group["name"], group["attributes"])
                for fattr in filter_has_non_empty_attributes:

                    if dict_has_nested_attr(
                        group["attributes"], fattr.split("."), must_have_val=True
                    ):
                        new_group_list.append(group)
            groups = new_group_list
        return groups

    def _remove_inactive_users_from_group_user_list(self, group_obj: Dict) -> Dict:
        if not isinstance(group_obj, dict) or "users_obj" not in group_obj:
            raise ValueError(
                f"Expected Authentik API group object as dict like in {self._build_api_call_url('#get-/core/groups/')} got type {type(group_obj)} with content: {group_obj}"
            )
        else:
            group_obj["users_obj"] = [
                u for u in group_obj["users_obj"] if u["is_active"] is True
            ]

    def _build_api_call_url(self, path: str):
        if path.startswith("/"):
            path = path.lstrip("/")
        return f"{self.url}api/v3/{path.lstrip('/')}"

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
