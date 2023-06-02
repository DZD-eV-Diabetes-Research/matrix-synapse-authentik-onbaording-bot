from typing import List, Dict, Union
import logging
import requests
import asyncio
from nio import (
    AsyncClient,
    ErrorResponse,
    RoomCreateResponse,
    RoomCreateError,
    RoomVisibility,
    RoomPreset,
    MatrixRoom,
    RoomPutStateError,
)

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
        access_token: str,
        device_id: str,
        server_domain: str,
        server_name: str,
        api_base_path: str = "/_matrix/client/v1",
        protocol: str = "http",
    ):
        self.access_token = access_token
        self.device_id = device_id
        self.api_base_url = f"{protocol}://{server_domain}{api_base_path}/"
        self.server_name = server_name
        self.nio_client = AsyncClient(
            user="@dzd-bot:dzd-ev.org", homeserver="https://matrix.dzd-ev.org"
        )
        self.nio_client.access_token = "syt_ZHpkLWJvdA_oLEkQCHJiSNvzcZJAfdh_0i2HHn"
        self.nio_client.device_id = "WKWVHESTWC"

    def space_list_rooms(self, space_id) -> List[Dict]:
        # https://matrix.org/docs/api/#get-/_matrix/client/v1/rooms/-roomId-/hierarchy
        rooms: List[Dict] = self._get(f"rooms/{space_id}/hierarchy")["rooms"]
        return [room for room in rooms if room["room_id"] != space_id]

    def room_kick_user(self, user_id: str, room_id: str, reason: str = None):
        async def room_kick():
            return await self.nio_client.room_kick(
                room_id=user_id,
                user_id=room_id,
                reason=reason,
            )

        asyncio.run(room_kick())

    def create_room(
        self,
        alias: str,
        canonical_alias: str,
        name: str,
        topic: str,
        room_params: Dict,
        parent_space_id: str = None,
    ) -> RoomCreateResponse:
        async def create_room():
            inital_state = None
            if parent_space_id:
                # https://spec.matrix.org/v1.2/client-server-api/#mspaceparent
                inital_state = [
                    {
                        "type": "m.space.parent",
                        "state_key": parent_space_id,
                        "content": {
                            "canonical": True,
                            "via": [self.server_name],
                        },
                    }
                ]
            room_response = await self.nio_client.room_create(
                space=False,
                alias=alias,
                name=name,
                topic=topic,
                inital_state=inital_state,
                **room_params,
            )
            if parent_space_id and not type(room_response) == RoomCreateError:
                state_update = await self.nio_client.room_put_state(
                    parent_space_id,
                    "m.space.child",
                    {
                        "suggested": True,
                        "via": [self.server_name],
                    },
                    state_key=room.room_id,
                )
                if type(state_update) == RoomPutStateError:
                    log.error(
                        f"Could not add room with the alias '{canonical_alias}' as child to space {parent_space_id}. Don't know what to do. Here is the error:"
                    )
                    raise SynapseApiError.from_nio_response_error(room)
            return room_response

        room: Union[RoomCreateResponse, RoomCreateError] = asyncio.run(create_room())
        if type(room) == RoomCreateError:
            log.error(
                f"Could not create room with the alias '{canonical_alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(room)
        return room

    def create_space(self, alias: str, name: str, topic: str, space_params: Dict):
        # we need to create the space
        async def create_space():
            return await self.nio_client.room_create(
                space=True,
                alias=alias,
                name=name,
                topic=topic,
                **space_params,
            )

        space: Union[RoomCreateResponse, RoomCreateError] = asyncio.run(create_space())
        if type(space) == RoomCreateError:
            log.error(
                f"Could not create the parent space with the alias '{alias}'. Don't know what to do. Here is the error:"
            )
            raise SynapseApiError.from_nio_response_error(space.message)
        return space

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
