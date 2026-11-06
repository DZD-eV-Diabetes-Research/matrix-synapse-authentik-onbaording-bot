"""Quarantined lifecycle against the live stack (ADR-0005, GOALS G9/G12).

Asserts the safety-critical properties end to end: dry-run (the default) performs no destructive
action, an explicit opt-in revokes the session only after the cooldown has elapsed, and access comes
back only through a real re-enable (review S1, live check 1).
"""

from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote

import httpx
import pytest

from onbot.app import run_reconcile_once
from onbot.auth.token_provider import OAuth2ClientCredentialsTokenProvider
from onbot.clients.mas_admin import SESSION_KINDS, ApiClientMasAdmin, MasCapabilities
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


def _lifecycle(config):
    return config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix


def _mas_client() -> ApiClientMasAdmin:
    return ApiClientMasAdmin(
        mas_url=S.MAS_URL,
        token_provider=OAuth2ClientCredentialsTokenProvider(
            token_endpoint=f"{S.MAS_URL}/oauth2/token",
            client_id=S.MAS_ADMIN_CLIENT_ID,
            client_secret=S.MAS_ADMIN_CLIENT_SECRET,
            scope="urn:mas:admin",
        ),
    )


async def _mas_capabilities() -> MasCapabilities:
    """What the stack's MAS can do, as onbot reads it at runtime."""
    client = _mas_client()
    try:
        return await client.capabilities()
    finally:
        await client.aclose()


async def test_stack_mas_can_end_sessions_and_deactivate_without_erasing() -> None:
    """The stack pins a MAS (1.28.0) whose admin API has every operation the offboarding uses.

    The tests below exercise the capable paths only (sessions ended on lock, deactivation without
    GDPR erasure). A downgrade of the stack to a MAS without them (0.14, for example) fails here
    instead of turning those tests into checks of the degraded path, which is unit-tested.
    """
    caps = await _mas_capabilities()
    assert caps.known, "the stack's MAS must serve /api/spec.json"
    assert caps.missing_finish == [], f"the stack's MAS cannot finish {caps.missing_finish}"
    assert caps.deactivate_skip_erase, "the stack's MAS cannot deactivate without GDPR erasure"


async def _active_sessions(username: str) -> dict[str, int]:
    client = _mas_client()
    try:
        uid = await client.get_user_id_by_username(username)
        assert uid is not None
        return {kind: len(await client.list_active_session_ids(kind, uid)) for kind in SESSION_KINDS}
    finally:
        await client.aclose()


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


def _scope_anchors(authentik_admin: S.AuthentikAdmin, count: int, **kwargs) -> list[dict]:
    """Active Authentik users who stay in a narrow sync scope (no Matrix account needed)."""
    return [
        authentik_admin.create_user(S.uniq("anchor"), password=S.DEFAULT_PASSWORD, **kwargs)
        for _ in range(count)
    ]


def _live_lifecycle(config, *, delete_after: int | None = None):
    life = _lifecycle(config)
    life.enabled = True
    life.dry_run = False
    life.deactivate_after_n_sec = 0
    life.delete_after_n_sec = delete_after
    return life  # gdpr_erase_on_delete stays at its default (false); its semantics have their own test


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
    assert await _wait_deactivated(admin_client, login.mxid)
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
    make_config, authentik_admin: S.AuthentikAdmin, caplog: pytest.LogCaptureFixture
) -> None:
    """The legitimate way back: a real re-enable in Authentik unlocks the account (G9.6).

    LIF-07: the lock ended the pre-lock session, so it stays dead after the unlock and the user logs
    in afresh.
    """
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
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        await run_reconcile_once(config)
    assert S.mas_user(username)["locked_at"] is None
    reenabled = [r.getMessage() for r in caplog.records if r.getMessage().startswith("re-enabled")]
    assert len(reenabled) == 1 and "come back on unlock" not in reenabled[0]
    assert not S.wait_whoami_ok(login.access_token, timeout=150)  # past the token cache
    assert S.whoami_status(S.mas_login(username, S.DEFAULT_PASSWORD).access_token) == 200


async def test_lock_ends_the_users_sessions_where_mas_can(
    make_config, authentik_admin: S.AuthentikAdmin, caplog: pytest.LogCaptureFixture
) -> None:
    """LIF-07: after the lock MAS lists no active session for the user."""
    username = S.uniq("lifesess")
    user, _login = S.provision(authentik_admin, username)
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    _live_lifecycle(config)
    await run_reconcile_once(config)  # mark
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        await run_reconcile_once(config)  # lock
    locked = [r.getMessage() for r in caplog.records if r.getMessage().startswith("locked out")]
    assert len(locked) == 1 and "MasLifecycleEffectors" in locked[0]
    assert "only suspended" not in locked[0]
    assert await _active_sessions(username) == dict.fromkeys(SESSION_KINDS, 0)


@pytest.mark.parametrize("gdpr", [True, False])
async def test_erase_stage_honours_gdpr_erase_on_delete(
    gdpr: bool,
    make_config,
    authentik_admin: S.AuthentikAdmin,
    admin_client,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """LIF-11 (D7): the MAS erase GDPR-erases exactly when configured."""
    username = S.uniq("liferase")
    user, login = S.provision(authentik_admin, username)
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    life = _live_lifecycle(config, delete_after=0)
    life.gdpr_erase_on_delete = gdpr
    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # lock
    with caplog.at_level(logging.INFO, logger="onbot.lifecycle.audit"):
        await run_reconcile_once(config)  # erase
    audit = [r.getMessage() for r in caplog.records if r.name == "onbot.lifecycle.audit"]
    assert not any(line.startswith("FAILED") for line in audit)
    assert await _wait_deactivated(admin_client, login.mxid)
    synapse_user = await admin_client.get_json(f"v2/users/{quote(login.mxid, safe='@:')}")
    assert bool(synapse_user.get("erased")) is gdpr
    assert any(line.startswith("erased") and f"GDPR erase={gdpr}" in line for line in audit)


async def test_erase_deletes_more_than_one_page_of_media(
    make_config, authentik_admin: S.AuthentikAdmin, admin_client
) -> None:
    """LIF-12: Synapse deletes 100 files per request; onbot keeps going until none is left."""
    username = S.uniq("lifemedia")
    user, login = S.provision(authentik_admin, username)
    uploads = 130
    with httpx.Client(timeout=30.0) as http:
        for i in range(uploads):
            r = http.post(
                f"{S.SYNAPSE_URL}/_matrix/media/v3/upload",
                params={"filename": f"f{i}.txt"},
                content=f"file {i}".encode(),
                headers={"Authorization": f"Bearer {login.access_token}", "Content-Type": "text/plain"},
            )
            r.raise_for_status()
    assert await admin_client.count_user_media(login.mxid) == uploads
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    life = _live_lifecycle(config, delete_after=0)
    life.include_user_media_on_delete = True
    life.gdpr_erase_on_delete = True  # works on every MAS version, so this test is about media only
    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # lock
    await run_reconcile_once(config)  # erase: deactivate, then every page of media
    assert await admin_client.count_user_media(login.mxid) == 0


async def test_synapse_fallback_under_mas_is_reported_and_audited_exactly(
    make_config, authentik_admin: S.AuthentikAdmin, caplog: pytest.LogCaptureFixture
) -> None:
    """LIF-09: without mas_admin on a MAS-backed homeserver onbot says so, at startup and per action."""
    username = S.uniq("lifefallback")
    user, login = S.provision(authentik_admin, username)
    authentik_admin.set_active(user["pk"], False)

    config = make_config()
    config.mas_admin = None
    _live_lifecycle(config)
    with caplog.at_level(logging.INFO):
        await run_reconcile_once(config)  # startup check, mark
        await run_reconcile_once(config)  # "lock" through the Synapse admin API
    errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("delegates logins to" in line and "mas_admin" in line for line in errors)
    locked = [r.getMessage() for r in caplog.records if r.getMessage().startswith("locked out")]
    assert len(locked) == 1
    assert "AdminApiLifecycleEffectors" in locked[0] and "MAS sessions stay valid" in locked[0]
    # And it is true: the MAS session survives the Synapse-side logout (§7 Q1).
    assert S.mas_user(username)["locked_at"] is None
    assert S.whoami_status(login.access_token) == 200


async def test_locked_user_who_leaves_the_scope_is_frozen(
    make_config, authentik_admin: S.AuthentikAdmin, admin_client
) -> None:
    """D9: losing the opt-in attribute must neither unlock nor advance offboarding."""
    username = S.uniq("lifescope")
    user, login = S.provision(authentik_admin, username, attributes={"chat": True})
    authentik_admin.set_active(user["pk"], False)
    # Somebody active stays in the scope: a scope with nobody active in it is an implausible answer
    # that the destructive-action guard pauses (D13).
    _scope_anchors(authentik_admin, 1, attributes={"chat": True})

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
    # Four colleagues stay in the scope, so one person leaving it is a 20 % drop: plausible input for
    # the destructive-action guard (D13), not a collapsed answer.
    if variant == "path":
        staff = f"staff-{username}"
        authentik_admin.set_path(user["pk"], staff)
        sync.sync_only_users_in_authentik_pathes = [staff]
        for anchor in _scope_anchors(authentik_admin, 4):
            authentik_admin.set_path(anchor["pk"], staff)
    else:
        group = authentik_admin.create_group(f"{username}-staff")
        authentik_admin.add_user_to_group(group["pk"], user["pk"])
        sync.sync_only_users_of_groups_with_id = [str(group["pk"])]
        _scope_anchors(authentik_admin, 4, groups=[group["pk"]])
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
