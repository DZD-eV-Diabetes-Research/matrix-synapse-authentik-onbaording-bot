import asyncio
from nio import (
    AsyncClient as MatrixAsyncClient,
    RoomCreateError,
    RoomVisibility,
    RoomPreset,
)
from onbot.api_client_synapse_admin import ApiClientSynapseAdmin
from onbot.api_client_matrix import ApiClientMatrix

sa = ApiClientSynapseAdmin(
    access_token="Bearer syt_ZHpkLWJvdA_oLEkQCHJiSNvzcZJAfdh_0i2HHn",
    server_url="matrix.dzd-ev.org",
    protocol="https",
)

mc = ApiClientMatrix(
    access_token="Bearer syt_ZHpkLWJvdA_oLEkQCHJiSNvzcZJAfdh_0i2HHn",
    server_url="matrix.dzd-ev.org",
    protocol="https",
)
print(sa.list_space())
print(sa.list_room())
print(mc.space_list_rooms("!DbJRSjtmVTxctLHYVX:dzd-ev.org"))
sa.add_user_to_room("!DbJRSjtmVTxctLHYVX:dzd-ev.org", user_id="@dzd-admin:dzd-ev.org")
exit()

c = MatrixAsyncClient(
    user="@dzd-bot:dzd-ev.org", homeserver="https://matrix.dzd-ev.org"
)
c.access_token = "syt_ZHpkLWJvdA_oLEkQCHJiSNvzcZJAfdh_0i2HHn"
c.device_id = "WKWVHESTWC"


async def room_kick():
    return await c.room_kick(
        room_id="!DbJRSjtmVTxctLHYVX:dzd-ev.org", user_id="@dzd-admin:dzd-ev.org"
    )


room = asyncio.run(room_kick())
c.close()
exit()


async def create_room():
    return await c.room_create(
        visibility=RoomVisibility.private,
        preset=RoomPreset.private_chat,
        space=True,
        name="TEST1",
        alias="TEST2",
    )


room = asyncio.run(create_room())
asyncio.run(c.close())
print(type(room), room)
