"""End-to-end onboarding: a provisioned user gets a welcome DM, sent exactly once (idempotent,
GOALS G4.1/G4.3). The welcome fires via the reconciler's user-synced signal during reconcile."""

from __future__ import annotations

import pytest

from onbot.app import run_reconcile_once
from onbot.clients.matrix import ApiClientMatrix
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


async def test_welcome_dm_sent_once(
    make_config, authentik_admin: S.AuthentikAdmin, matrix_client: ApiClientMatrix
) -> None:
    _user, login = S.provision(authentik_admin, S.uniq("welcome"))
    config = make_config()
    assert config.welcome_new_users_messages  # the default welcome set

    # Two reconcile passes: onboarding must be idempotent across repeated triggers.
    await run_reconcile_once(config)
    await run_reconcile_once(config)

    # Exactly one DM room is recorded for the user in the bot's m.direct account data.
    direct = await matrix_client.get_account_data(S.BOT_USER_ID, "m.direct")
    rooms = direct.get(login.mxid) or []
    assert len(rooms) == 1
    room_id = rooms[0]

    # Each configured welcome message was sent exactly once (no duplicates on the second pass).
    resp = await matrix_client.get_json(f"v3/rooms/{room_id}/messages", params={"dir": "b", "limit": 100})
    bot_messages = [
        ev
        for ev in resp.get("chunk", [])
        if ev.get("type") == "m.room.message" and ev.get("sender") == S.BOT_USER_ID
    ]
    assert len(bot_messages) == len(config.welcome_new_users_messages)
