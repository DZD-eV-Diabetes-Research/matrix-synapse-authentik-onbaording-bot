from typing import List, Dict, Union, Awaitable
import logging
import requests
import asyncio
from nio import (
    AsyncClient,
    AsyncClientConfig,
    ErrorResponse,
    RoomCreateResponse,
    RoomCreateError,
    RoomVisibility,
    RoomPreset,
    MatrixRoom,
    RoomPutStateError,
    RoomKickResponse,
    RoomKickError,
    RoomPutStateResponse,
    RoomPutStateError,
    RoomGetStateEventError,
    RoomGetStateResponse,
    RoomEncryptionEvent,
)

from onbot.utils import synchronize_async_helper
from pathlib import Path

log = logging.getLogger(__name__)


class SynapseApiError(Exception):
    @classmethod
    def from_nio_response_error(cls, nio_response_error: ErrorResponse):
        # Tim Todo: add_note with python 3.11. will be awesome!
        # https://docs.python.org/3.11/library/exceptions.html#BaseException.add_note
        return cls(
            f"{type(nio_response_error)} - status_code: '{nio_response_error.status_code}' Message: '{nio_response_error.message}'"
        )


class MatrixApiClient:
    def __init__(
        self,
        user: str,
        access_token: str,
        device_id: str,
        server_url: str,
        server_name: str,
        state_store_path: Path,
        state_store_encryption_key: str = None,
    ):
        self.user = user
        self.access_token = access_token
        self.device_id = device_id
        self.server_url = server_url.rstrip("/")
        self.api_base_url = f"{server_url.rstrip('/')}/_matrix/client/"
        self.server_name = server_name
        self.state_store_path = state_store_path
        self.state_store_encryption_key = state_store_encryption_key

    def _call_nio_client(
        self, nio_func: Awaitable, params: Dict, encrypted_mode: bool = False
    ):
        config = None
        if encrypted_mode:
            config = AsyncClientConfig(
                encryption_enabled=True,
                store_sync_tokens=True,
                pickle_key=self.state_store_encryption_key,
            )
        nio_client = AsyncClient(
            user=self.user,
            homeserver=self.server_url,
            store_path=self.state_store_path if encrypted_mode else None,
            config=config,
        )

        nio_client.access_token = self.access_token.lstrip("Bearer ")
        nio_client.device_id = self.device_id
        nio_client.user_id = f"@{self.user}:{self.server_name}"
        if encrypted_mode:
            nio_client.load_store()
        nio_client.login(device_name=self.device_id, token=self.access_token)
        result = synchronize_async_helper(nio_func(nio_client, **params))
        synchronize_async_helper(nio_client.close())
        return result

    def space_list_rooms(self, space_id) -> List[Dict]:
        # https://matrix.org/docs/api/#get-/_matrix/client/v1/rooms/-roomId-/hierarchy
        rooms: List[Dict] = self._get(f"v1/rooms/{space_id}/hierarchy")["rooms"]
        return [room for room in rooms if room["room_id"] != space_id]

    def room_kick_user(self, user_id: str, room_id: str, reason: str = None):
        log.info(f"Kick user {user_id} from room {room_id}. reason: '{reason}' ")
        result: RoomKickError | RoomKickResponse = self._call_nio_client(
            AsyncClient.room_kick,
            {"room_id": room_id, "user_id": user_id, "reason": reason},
        )
        if isinstance(result, RoomKickError):
            raise SynapseApiError.from_nio_response_error(result)

    def create_room(
        self,
        alias: str = None,
        canonical_alias: str = None,
        name: str = None,
        topic: str = None,
        encrypted: bool = True,
        room_params: Dict = None,
        parent_space_id: str = None,
        is_direct: bool = False,
    ) -> RoomCreateResponse:
        if parent_space_id is not None and (
            not isinstance(parent_space_id, str)
            or parent_space_id
            and not parent_space_id.startswith("!")
        ):
            raise ValueError(
                f"Expected room_id of parent space as string (`str`) in Matrix ID format e.g. '!<room_id>:<your-synapse-server-name>' got type {type(parent_space_id)} with content '{parent_space_id}'"
            )

        if "visibility" in room_params:
            room_params["visibility"] = RoomVisibility(room_params["visibility"])
        if "preset" in room_params:
            room_params["preset"] = RoomPreset(room_params["preset"])
        initial_state = []
        if parent_space_id:
            # https://spec.matrix.org/v1.2/client-server-api/#mspaceparent
            initial_state.append(
                {
                    "type": "m.space.parent",
                    "state_key": parent_space_id,
                    "content": {
                        "canonical": True,
                        "via": [self.server_name],
                    },
                }
            )
        params = {
            key: val
            for key, val in locals().items()
            if key in ["alias", "name", "topic", "initial_state", "room_params"]
        }
        if encrypted:
            initial_state.append(
                {
                    "type": "m.room.encryption",
                    "content": {"algorithm": "m.megolm.v1.aes-sha2"},
                }
            )
        log.debug(f"Create room with params: {params}")
        room_response: RoomCreateResponse | RoomCreateError = self._call_nio_client(
            AsyncClient.room_create,
            {
                "space": False,
                "alias": alias,
                "name": name,
                "topic": topic,
                "initial_state": initial_state,
                "is_direct": is_direct,
                **room_params,
            },
            encrypted_mode=encrypted,
        )
        if parent_space_id and not isinstance(room_response, RoomCreateError):
            room_put_state_response = self._call_nio_client(
                AsyncClient.room_put_state,
                {
                    "room_id": parent_space_id,
                    "event_type": "m.space.child",
                    "content": {
                        "suggested": True,
                        "via": [self.server_name],
                    },
                    "state_key": room_response.room_id,
                },
            )

            if type(room_put_state_response) == RoomPutStateError:
                log.error(
                    f"Could not add room with the alias '{canonical_alias}' as child to space {parent_space_id}. Don't know what to do. Here is the error:"
                )
                raise SynapseApiError.from_nio_response_error(room_put_state_response)

        if type(room_response) == RoomCreateError:
            log.error(
                f"Could not create room with the alias '{canonical_alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(room_response)
        return room_response

    def create_space(
        self, alias: str, name: str, topic: str, space_params: Dict
    ) -> RoomCreateResponse:
        if "visibility" in space_params:
            space_params["visibility"] = RoomVisibility(space_params["visibility"])
        if "preset" in space_params:
            space_params["preset"] = RoomPreset(space_params["preset"])
        space_params = {
            "space": True,
            "alias": alias,
            "name": name,
            "topic": topic,
            **space_params,
        }
        log.debug(f"Create space with params: {space_params}")
        space_create_response: RoomCreateResponse | RoomCreateError = (
            self._call_nio_client(
                AsyncClient.room_create,
                space_params,
            )
        )
        if isinstance(space_create_response, RoomCreateError):
            log.error(
                f"Could not create the parent space with the alias '{alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(space_create_response.message)
        return space_create_response

    def resolve_alias(self, alias: str) -> str:
        # https://spec.matrix.org/v1.2/client-server-api/#room-aliases
        # /_matrix/client/v3/directory/room/{roomAlias}
        return self._get(f"v3/directory/room/{alias}")["room_id"]

    def put_room_state_event(
        self, room_id: str, event_type: str, content: Dict, state_key: str
    ) -> RoomPutStateResponse:
        res: RoomPutStateResponse | RoomPutStateError = self._call_nio_client(
            nio_func=AsyncClient.room_put_state,
            params={
                "room_id": room_id,
                "event_type": event_type,
                "content": content,
                "state_key": state_key,
            },
        )
        if isinstance(res, RoomPutStateError):
            raise SynapseApiError.from_nio_response_error(res)
        else:
            return res

    def get_room_state_event(
        self,
        room_id: str,
        event_type: str,
        state_key: str = None,
        raise_error: bool = True,
    ) -> RoomGetStateResponse:
        res: RoomGetStateResponse | RoomGetStateEventError = self._call_nio_client(
            nio_func=AsyncClient.room_get_state_event,
            params={
                "room_id": room_id,
                "event_type": event_type,
                "state_key": state_key,
            },
        )
        if isinstance(res, RoomGetStateEventError) and raise_error:
            raise SynapseApiError.from_nio_response_error(res)
        else:
            return res

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

    """
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
    """
