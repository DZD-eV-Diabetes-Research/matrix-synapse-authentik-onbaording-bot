"""Quarantined lifecycle against the live stack (ADR-0005, GOALS G9/G12).

Asserts the two safety-critical properties end to end: dry-run (the default) performs no destructive
action, and an explicit opt-in revokes the session only after the cooldown has elapsed.
"""

from __future__ import annotations

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
