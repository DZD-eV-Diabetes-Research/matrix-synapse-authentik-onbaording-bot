from typing import List, Dict, Union, Awaitable, BinaryIO, Literal, Optional
import logging
import requests
import mimetypes
import uuid
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
    RoomSendResponse,
    RoomSendError,
    LoginError,
    RoomGetStateError,
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


class ApiClientMatrix:
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
        self.api_base_url = f"{server_url.rstrip('/')}/_matrix/"
        self.server_name = server_name
        self.state_store_path = state_store_path
        self.state_store_encryption_key = state_store_encryption_key

        self._client_instance_enrypted_cache: AsyncClient = None
        self._client_instance_unenrypted_cache: AsyncClient = None

    def _call_nio_client(
        self, nio_func: Awaitable, params: Dict, encrypted_mode: bool = False
    ):
        """This whole construct is a problably more cumbersome atm as it should be. Its a wrapper to call the ayncio nio-matrix client from the sync code of this bot
        TODO: refactor this"""
        nio_client = (
            self._client_instance_enrypted_cache
            if encrypted_mode
            else self._client_instance_unenrypted_cache
        )
        if nio_client is None:
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
            nio_client.user_id = self.user
            if encrypted_mode:
                nio_client.load_store()
                self._client_instance_enrypted_cache = nio_client
            else:
                self._client_instance_unenrypted_cache = nio_client

        # TODO: Password based login, if not a token is provided
        """
        res = synchronize_async_helper(
            nio_client.login(
                device_name=self.device_id, password=
            )
        )
        
        if isinstance(res, LoginError):
            raise SynapseApiError.from_nio_response_error(res)
        """
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
        invite: List[str] = None,
    ) -> RoomCreateResponse:
        if parent_space_id is not None and (
            not isinstance(parent_space_id, str)
            or parent_space_id
            and not parent_space_id.startswith("!")
        ):
            raise ValueError(
                f"Expected room_id of parent space as string (`str`) in Matrix ID format e.g. '!<room_id>:<your-synapse-server-name>' got type {type(parent_space_id)} with content '{parent_space_id}'"
            )
        room_params = {} if room_params is None else room_params
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
                "invite": [] if invite is None else invite,
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

    def set_room_name(self, room_id: str, room_name: str):
        # https://element-hq.github.io/synapse/develop/admin_api/user_admin_api.html#list-room-memberships-of-a-user
        return self._put(f"v3/rooms/{room_id}/state/m.room.name", {"name": room_name})

    def set_room_topic(self, room_id: str, room_topic: str):
        # https://element-hq.github.io/synapse/develop/admin_api/user_admin_api.html#list-room-memberships-of-a-user
        return self._put(
            f"v3/rooms/{room_id}/state/m.room.topic", {"topic": room_topic}
        )

    def set_room_avatar_url(self, room_id: str, room_avatar_url: str):
        # https://element-hq.github.io/synapse/develop/admin_api/user_admin_api.html#list-room-memberships-of-a-user
        return self._put(
            f"v3/rooms/{room_id}/state/m.room.avatar", {"url": room_avatar_url}
        )

    def get_user_profile(self, user_id: str) -> Dict[str, str]:
        # https://spec.matrix.org/v1.11/client-server-api/#get_matrixclientv3profileuserid
        return self._get(f"v3/profile/{user_id}")

    def set_user_avatar_url(self, user_id: str, user_avatar_url: str):
        # https://spec.matrix.org/v1.2/client-server-api/#put_matrixclientv3profileuseridavatar_url
        return self._put(
            f"v3/profile/{user_id}/avatar_url", {"avatar_url": user_avatar_url}
        )

    def resolve_alias(self, alias: str) -> str:
        # https://spec.matrix.org/v1.2/client-server-api/#room-aliases
        # /_matrix/client/v3/directory/room/{roomAlias}
        return self._get(f"v3/directory/room/{alias}")["room_id"]

    def put_room_state_event(
        self, room_id: str, event_type: str, content: Dict, state_key: str = ""
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

    def room_send(
        self,
        room_id: str,
        content: Dict,
        message_type: str = "m.room.message",
        tx_id: str = None,
    ) -> RoomSendResponse:
        res: RoomSendResponse | RoomSendError = self._call_nio_client(
            nio_func=AsyncClient.room_send,
            params={
                "room_id": room_id,
                "message_type": message_type,
                "content": content,
                "tx_id": tx_id,
            },
        )
        if isinstance(res, RoomSendError):
            raise SynapseApiError.from_nio_response_error(res)
        else:
            return res

    def get_room_power_levels(self, room_id: str) -> Dict | None:
        # https://spec.matrix.org/legacy/client_server/r0.2.0.html#m-room-power-levels -> content
        return self.get_room_state_event(
            room_id=room_id, event_type="m.room.power_levels"
        )["content"]

    def set_room_power_levels(self, room_id: str, power_levels: Dict):
        # https://spec.matrix.org/legacy/client_server/r0.2.0.html#m-room-power-levels -> content
        self.put_room_state_event(
            room_id=room_id, event_type="m.room.power_levels", content=power_levels
        )

    def get_room_state_event(
        self,
        room_id: str,
        event_type: str | List[str],
        state_key: str = "",
        raise_error: bool = True,
    ) -> None | Dict | RoomGetStateError:
        # TODO: hackyhackhack. we fetch all room states and collect out the first with a matching "event_type". i am not sure if that violates the logic of matrix states but works for now
        # state_key falls under the table in this case, because i did not understand what it is anyway. i dont need at it the moment, thats for sure.
        # everything is fine if is just tag a "TODO" on the problem!
        # AsyncClient.room_get_state_event, the correct funtion in this case would nto work for me it never found anything. investigate!
        """
        res: RoomGetStateResponse | RoomGetStateEventError = self._call_nio_client(
            nio_func=AsyncClient.room_get_state_event,
            params={
                "room_id": room_id,
                "event_type": event_type,
                "state_key": "",
            },
        )
        if isinstance(res, RoomGetStateEventError) and raise_error:
            raise SynapseApiError.from_nio_response_error(res)
        else:
            return res
        """
        if isinstance(event_type, str):
            event_type = [event_type]
        if state_key:
            # see "TODO" above
            raise NotImplementedError

        res: RoomGetStateResponse | RoomGetStateError = self._call_nio_client(
            nio_func=AsyncClient.room_get_state,
            params={
                "room_id": room_id,
            },
        )
        if isinstance(res, RoomGetStateEventError):
            if raise_error:
                raise SynapseApiError.from_nio_response_error(res)
            else:
                return res
        else:
            return next((r for r in res.events if r["type"] in event_type), None)

    def upload_media(
        self,
        content: Path | BinaryIO,
        filename: Optional[str] = None,
    ) -> str:
        """_summary_

        Args:
            content (Path | BinaryIO): _description_
            filename (str, optional): _description_. Defaults to None.

        Returns:
            str: the content uri like 'mxc://example.com/AQwafuaFswefuhsfAFAgsw'
        """
        # https://spec.matrix.org/v1.11/client-server-api/#post_matrixmediav3upload
        # https://stackoverflow.com/a/14448953/12438690

        headers = {
            "Authorization": self.access_token,
            "Content-Type": "application/octet-stream",
        }
        url = self._build_api_call_url("/v3/upload", subapi="media")
        files = {}
        if isinstance(content, Path):
            if filename is None:
                filename = content.name
            with open(content, "rb") as image_file:
                r = requests.post(
                    url,
                    data=image_file.read(),
                    headers=headers,
                )
        else:
            if filename is None:
                filename = str(uuid.uuid4())
            r = requests.post(
                url,
                params={"filename": filename},
                data=content,
                headers=headers,
            )
        return self._http_call_response_handler(r)["content_uri"]

    def _build_api_call_url(
        self, path: str, subapi: Literal["client", "media"] = "client"
    ):
        return f"{self.api_base_url.rstrip('/')}/{subapi}/{path.lstrip('/')}"

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

    def _put(self, path: str, json_body: Dict = None) -> Dict:
        if json_body is None:
            json_body = {}
        r = requests.put(
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
