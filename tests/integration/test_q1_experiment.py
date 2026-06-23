"""§7 Q1 experiment (BATTLE_PLAN.md): does MAS revoke sessions / lock the Matrix account when
Authentik disables a user upstream, or must the lifecycle module enforce it?

Run live against the real Synapse + MAS + Authentik stack. The findings are recorded in
docs/adr/0005-quarantine-lifecycle.md and BATTLE_PLAN.md §7 Q1. This test pins the observed facts so
the conclusion (the lifecycle module is the enforcement path, not a redundant backstop) stays true.
"""

from __future__ import annotations

import pytest

from onbot.app import run_reconcile_once
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


async def test_q1_upstream_disable_propagation(make_config, authentik_admin: S.AuthentikAdmin) -> None:
    user, login = S.provision(authentik_admin, S.uniq("q1"))
    assert S.whoami_status(login.access_token) == 200  # session live before disable

    authentik_admin.set_active(user["pk"], False)

    # FINDING 1: MAS/Synapse do NOT propagate the upstream disable — the existing Matrix session
    # keeps working indefinitely (no automatic session revocation / account lock).
    assert S.whoami_status(login.access_token) == 200

    # FINDING 2: a *fresh* login is blocked at the upstream — Authentik denies the disabled user,
    # so MAS can never mint a new session for them.
    with pytest.raises(S.LoginError):
        S.mas_login(user["username"], S.DEFAULT_PASSWORD)

    # CONCLUSION: because existing sessions persist, onbot's lifecycle module is the *enforcement
    # path* for revoking them (not merely a redundant backstop). With an explicit opt-in it does so:
    config = make_config()
    life = config.sync_authentik_users_with_matrix_rooms.deactivate_disabled_authentik_users_in_matrix
    life.enabled = True
    life.dry_run = False
    life.deactivate_after_n_sec = 0
    life.delete_after_n_sec = None

    await run_reconcile_once(config)  # mark
    await run_reconcile_once(config)  # logout — onbot enforces what MAS does not
    assert S.wait_revoked(login.access_token)
