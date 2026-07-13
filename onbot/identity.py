"""MXID / localpart mapping (AD-6 — the critical MAS integration contract).

Under MAS, accounts are auto-provisioned on first login and the MXID localpart is derived by MAS
from an upstream Authentik claim. The bot does **not** create accounts, but it must compute the
*same* MXID to match users to their Matrix accounts. The localpart source is configurable
(``sync_authentik_users_with_matrix_rooms.authentik_username_mapping_attribute``) and MUST agree
with the template MAS uses, otherwise users won't match (BATTLE_PLAN §7 Q2).

Shared by the reconciler (membership, power levels) and, later, onboarding (Phase 4).
"""

from __future__ import annotations

from typing import Any, Literal

from onbot.utils import get_nested_dict_val_by_path

SigilT = Literal["@", "#"]


def build_canonical(local_part: str, server_name: str, sigil: SigilT = "@") -> str:
    """Build a fully-qualified Matrix identifier: ``<sigil><local_part>:<server_name>``.

    ``@`` → user ID, ``#`` → room alias. **Not for room IDs.** A user ID and a room *alias* are
    ``localpart:server_name`` and keep their ``:server`` component. A room *ID* (sigil ``!``) does
    **not**: since room version 12 it is a hash of the ``m.room.create`` event with no ``:domain``
    part at all (e.g. ``!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ``). Room IDs are opaque tokens
    the server hands us — never construct one from parts, and never split one on ``:`` to recover a
    server name (there is none). See ``docs/adr/0011-room-version-12.md``.
    """
    return f"{sigil}{local_part}:{server_name}"


def compute_mxid(
    authentik_user: dict[str, Any],
    *,
    username_attribute: str,
    server_name: str,
) -> str:
    """Compute the deterministic MXID for an Authentik user (G1.2, AD-6).

    ``username_attribute`` is a dotted path into the Authentik user object (e.g. ``username`` or
    ``attributes.matrix_name``). Raises ``KeyError`` if the attribute is missing — a user we cannot
    map deterministically must surface loudly, never be silently skipped.
    """
    local_part = get_nested_dict_val_by_path(authentik_user, username_attribute.split("."))
    if local_part is None or local_part == "":
        raise KeyError(username_attribute)
    return build_canonical(str(local_part), server_name, "@")
