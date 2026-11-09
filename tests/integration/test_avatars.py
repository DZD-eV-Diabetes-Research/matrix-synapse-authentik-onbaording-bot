"""Avatars follow the picture, not just its URL (ONB-15), against the live homeserver.

The bot's avatar is served from a small HTTP server on this machine (the bot runs in the test
process, so it reaches it on the loopback address, released through ``avatar_fetch``). Replacing the
image behind the unchanged URL must reach the bot's profile and the user's notice board on the next
start, and an unchanged image must not be set again.
"""

from __future__ import annotations

import struct
import threading
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from onbot.app import run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


def _png(red: int, green: int, blue: int) -> bytes:
    """A valid 1x1 PNG of one colour, so the homeserver gets a real image."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    pixels = zlib.compress(bytes([0, red, green, blue]))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", pixels) + chunk(b"IEND", b"")


class _ImageServer:
    def __init__(self, image: bytes) -> None:
        self.image = image
        self.requests = 0
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                owner.requests += 1
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(owner.image)))
                self.end_headers()
                self.wfile.write(owner.image)

            def log_message(self, *_: object) -> None:
                return None

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/bot.png"


@contextmanager
def _serving(image: bytes) -> Iterator[_ImageServer]:
    server = _ImageServer(image)
    server._thread.start()
    try:
        yield server
    finally:
        server._httpd.shutdown()
        server._httpd.server_close()


async def _board_avatar(client: ApiClientMatrix, room_id: str) -> bytes:
    content = await client.get_room_state_event(room_id, "m.room.avatar")
    assert content is not None
    return await client.download_media(content["url"])


async def test_a_new_picture_behind_the_same_url_reaches_the_bot_and_the_notice_board(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    prefix = S.uniq("avatar")
    group = authentik_admin.create_group(f"{prefix}-scope")
    _u, user = S.provision(authentik_admin, S.uniq("avataruser"), groups=[group["pk"]])
    red, blue = _png(255, 0, 0), _png(0, 0, 255)

    with _serving(red) as server:
        config = make_config()
        config.sync_matrix_rooms_based_on_authentik_groups.only_for_groupnames_starting_with = prefix
        config.sync_authentik_users_with_matrix_rooms.sync_only_users_of_groups_with_id = [group["pk"]]
        config.synapse_server.bot_avatar_url = server.url
        config.avatar_fetch.allowed_internal_destinations = ["127.0.0.1"]

        await run_reconcile_once(config)
        board = (await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct"))[user.mxid][0]
        first_profile = await matrix_client.get_user_avatar(S.BOT_USER_ID)
        assert first_profile is not None
        assert await matrix_client.download_media(first_profile) == red
        assert await _board_avatar(matrix_client, board) == red

        server.image = blue  # a rebrand: same URL, new picture
        await run_reconcile_once(config)

        second_profile = await matrix_client.get_user_avatar(S.BOT_USER_ID)
        assert second_profile is not None and second_profile != first_profile
        assert await matrix_client.download_media(second_profile) == blue
        assert await _board_avatar(matrix_client, board) == blue
        board_event = await matrix_client.get_room_state_event(board, "m.room.avatar")

        await run_reconcile_once(config)  # nothing changed: nothing is set again

        assert await matrix_client.get_user_avatar(S.BOT_USER_ID) == second_profile
        assert await matrix_client.get_room_state_event(board, "m.room.avatar") == board_event
