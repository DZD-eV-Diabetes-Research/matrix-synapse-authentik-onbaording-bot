"""Authenticated media (MSC3916, Phase 6): the bot uploads bytes and downloads them back over the
authenticated endpoint with its MAS-issued token (GOALS G10)."""

from __future__ import annotations

import pytest

from onbot.clients.matrix import ApiClientMatrix
from tests.integration import stack_api as S  # noqa: F401 (keeps the integration import surface uniform)

pytestmark = pytest.mark.integration


async def test_authenticated_media_roundtrip(matrix_client: ApiClientMatrix) -> None:
    data = b"onbot integration media \x00\x01\x02 payload"
    mxc = await matrix_client.upload_media(
        data, content_type="application/octet-stream", filename="probe.bin"
    )
    assert mxc.startswith("mxc://")

    downloaded = await matrix_client.download_media(mxc)
    assert downloaded == data
