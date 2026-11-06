"""Quarantined lifecycle against the live stack (ADR-0005, GOALS G9/G12).

Asserts the safety-critical properties end to end: dry-run (the default) performs no destructive
action, an explicit opt-in revokes the session only after the cooldown has elapsed, and access comes
back only through a real re-enable (review S1, live check 1).
"""

from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import pytest

from onbot.app import run_reconcile_once
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


def _lifecycle(config):
    return config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix


async def test_dry_run_does_not_revoke(make_config, authentik_admin: S.AuthentikAdmin) -> None:
    user, login = S.provision(authentik_admin, S.uniq("lifedry"))
    assert S.whoami_status(login.access_token) == 200
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    life = _lifecycle(config)
    life.enabled = True
    life.dry_run = True  # quarantine default
    life.deactivate_after_n_sec = 0

    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # would log out — but dry-run only audits
    assert S.whoami_status(login.access_token) == 200


async def test_cooldown_then_session_revoked(make_config, authentik_admin: S.AuthentikAdmin) -> None:
    user, login = S.provision(authentik_admin, S.uniq("lifelive"))
    assert S.whoami_status(login.access_token) == 200
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    life = _lifecycle(config)
    life.enabled = True
    life.dry_run = False  # explicit opt-in to destructive action (ADR-0005)
    life.deactivate_after_n_sec = 0  # no cooldown wait in the test
    life.delete_after_n_sec = None  # stop at logout; do not erase

    await run_reconcile_once(config)  # first pass only marks (records the cooldown start)
    assert S.whoami_status(login.access_token) == 200
    await run_reconcile_once(config)  # cooldown elapsed -> revoke all sessions (via MAS)
    assert S.wait_revoked(login.access_token)


# --- review S1, live check 1 (docs/review_26-11-04/08-live-verification.md) ----------------------
#
# Lock and unlock reach an existing token only after Synapse's introspection cache lapses (about two
# minutes, LIV-03). The tests therefore assert the lock state on MAS itself, which is authoritative
# and immediate, and poll the token only where the token is the property under test.


def _live_lifecycle(config, *, delete_after: int | None = None):
    life = _lifecycle(config)
    life.enabled = True
    life.dry_run = False
    life.deactivate_after_n_sec = 0
    life.delete_after_n_sec = delete_after
    return life


async def _deactivated(admin_client, mxid: str) -> bool:
    """Whether Synapse reports the account deactivated (MAS propagates its deactivation there).

    MAS's admin API only lists ``deactivated_at`` once it is set, so Synapse is the stable place to
    look.
    """
    user = await admin_client.get_json(f"v2/users/{quote(mxid, safe='@:')}")
    return bool(user.get("deactivated"))


async def _wait_deactivated(admin_client, mxid: str, *, timeout: float = 60.0) -> bool:
    """Poll :func:`_deactivated`: MAS hands the deactivation to Synapse through a background job."""
    deadline = time.monotonic() + timeout
    while True:
        if await _deactivated(admin_client, mxid):
            return True
        if time.monotonic() > deadline:
            return False
        await asyncio.sleep(1)


async def test_locked_user_deleted_in_authentik_stays_locked_and_is_erased(
    make_config, authentik_admin: S.AuthentikAdmin, admin_client
) -> None:
    """LIF-01 / SEC-01 / LIF-02: deleting a locked user must not unlock them; offboarding goes on."""
    username = S.uniq("lifedel")
    user, login = S.provision(authentik_admin, username)
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    life = _live_lifecycle(config)
    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # lock
    assert S.mas_user(username)["locked_at"] is not None
    assert S.wait_revoked(login.access_token)

    authentik_admin.delete_user(user["pk"])
    await run_reconcile_once(config)
    await run_reconcile_once(config)
    mas = S.mas_user(username)
    assert mas["locked_at"] is not None, "a deleted Authentik user was unlocked in MAS"
    assert mas.get("deactivated_at") is None  # erase is still disabled
    assert not await _deactivated(admin_client, login.mxid)
    assert S.whoami_status(login.access_token) == 401

    life.delete_after_n_sec = 0  # the deleted user proceeds through the state machine
    await run_reconcile_once(config)
    assert await _deactivated(admin_client, login.mxid)
    assert S.whoami_status(login.access_token) == 401


async def test_known_user_deleted_without_disable_is_offboarded(
    make_config, authentik_admin: S.AuthentikAdmin
) -> None:
    """LIF-02: a user deleted directly (no disable phase) is locked after the cooldown."""
    username = S.uniq("lifedirect")
    user, login = S.provision(authentik_admin, username)

    config = make_config()
    _live_lifecycle(config)
    await run_reconcile_once(config)  # active and mapped: recorded as a known user
    assert S.mas_user(username)["locked_at"] is None

    authentik_admin.delete_user(user["pk"])
    await run_reconcile_once(config)  # mark (deleted)
    await run_reconcile_once(config)  # lock
    assert S.mas_user(username)["locked_at"] is not None
    assert S.wait_revoked(login.access_token)


async def test_locked_user_reenabled_in_authentik_is_unlocked(
    make_config, authentik_admin: S.AuthentikAdmin
) -> None:
    """The legitimate way back: a real re-enable in Authentik unlocks the account (G9.6)."""
    username = S.uniq("lifeback")
    user, login = S.provision(authentik_admin, username)
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    _live_lifecycle(config)
    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # lock
    assert S.mas_user(username)["locked_at"] is not None
    assert S.wait_revoked(login.access_token)

    authentik_admin.set_active(user["pk"], True)
    await run_reconcile_once(config)
    assert S.mas_user(username)["locked_at"] is None
    # MAS brings the very same session back on unlock (LIF-07, a later review session ends it).
    assert S.wait_whoami_ok(login.access_token)


async def test_locked_user_who_leaves_the_scope_is_frozen(
    make_config, authentik_admin: S.AuthentikAdmin, admin_client
) -> None:
    """D9: losing the opt-in attribute must neither unlock nor advance offboarding."""
    username = S.uniq("lifescope")
    user, login = S.provision(authentik_admin, username, attributes={"chat": True})
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    config.sync_authentik_users_with_matrix_rooms.sync_only_users_with_authentik_attributes = {"chat": True}
    _live_lifecycle(config, delete_after=0)
    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # lock
    assert S.mas_user(username)["locked_at"] is not None

    authentik_admin.set_attributes(user["pk"], {})
    await run_reconcile_once(config)
    await run_reconcile_once(config)
    mas = S.mas_user(username)
    assert mas["locked_at"] is not None
    assert mas.get("deactivated_at") is None  # frozen: no erase despite delete_after_n_sec = 0
    assert not await _deactivated(admin_client, login.mxid)


@pytest.mark.parametrize("variant", ["path", "group"])
async def test_known_user_disabled_and_moved_out_of_scope_is_offboarded(
    variant: str, make_config, authentik_admin: S.AuthentikAdmin, admin_client
) -> None:
    """D14: "disable, archive, strip the groups" in one go still locks and erases a known user."""
    username = S.uniq(f"lifearch{variant}")
    user, login = S.provision(authentik_admin, username)
    config = make_config()
    sync = config.sync_authentik_users_with_matrix_rooms
    if variant == "path":
        staff = f"staff-{username}"
        authentik_admin.set_path(user["pk"], staff)
        sync.sync_only_users_in_authentik_pathes = [staff]
    else:
        group = authentik_admin.create_group(f"{username}-staff")
        authentik_admin.add_user_to_group(group["pk"], user["pk"])
        sync.sync_only_users_of_groups_with_id = [str(group["pk"])]
    life = _live_lifecycle(config)
    await run_reconcile_once(config)  # active, in scope and mapped: recorded as a known user
    assert S.mas_user(username)["locked_at"] is None

    authentik_admin.set_active(user["pk"], False)
    if variant == "path":
        authentik_admin.set_path(user["pk"], f"archive-{username}")
    else:
        authentik_admin.remove_user_from_group(group["pk"], user["pk"])
    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # lock
    assert S.mas_user(username)["locked_at"] is not None
    assert S.wait_revoked(login.access_token)

    life.delete_after_n_sec = 0
    await run_reconcile_once(config)
    assert await _wait_deactivated(admin_client, login.mxid)
