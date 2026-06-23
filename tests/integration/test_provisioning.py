"""Account provisioning through a real MAS login (ADR-0006) and the MXID localpart contract (Q2)."""

from __future__ import annotations

import pytest

from onbot.identity import compute_mxid
from tests.integration import stack_api as S

pytestmark = pytest.mark.integration


def test_real_mas_login_provisions_matching_localpart(authentik_admin: S.AuthentikAdmin) -> None:
    """A real login provisions @<username>:server and onbot's mapping computes the same MXID."""
    username = S.uniq("smoke")
    authentik_admin.create_user(username, password="pw-Compl3x-123")

    result = S.mas_login(username, "pw-Compl3x-123")

    # MAS derives the localpart from preferred_username (= Authentik username).
    assert result.mxid == f"@{username}:{S.SERVER_NAME}"
    # AD-6 / §7 Q2: onbot's identity mapping MUST match MAS's localpart template exactly.
    expected = compute_mxid({"username": username}, username_attribute="username", server_name=S.SERVER_NAME)
    assert result.mxid == expected
