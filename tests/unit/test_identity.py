"""Unit tests for MXID / localpart mapping (AD-6)."""

import pytest

from onbot.clients.mas_admin import mxid_localpart
from onbot.identity import build_canonical, compute_mxid
from onbot.models import MatrixRoom

# A room version 12 room ID: a hash of the m.room.create event, with NO ":domain" component.
V12_ROOM_ID = "!Nhcu5BS-UMnFX7hBVfVSoXiD7OgH6iRT-xyIuqDnpYQ"


def test_build_canonical_sigils() -> None:
    assert build_canonical("alice", "company.org", "@") == "@alice:company.org"
    assert build_canonical("Room", "company.org", "#") == "#Room:company.org"


def test_a_v12_domainless_room_id_is_stored_verbatim() -> None:
    # Room IDs are opaque server-minted tokens; a v12 one has no domain to recover. Nothing must try
    # to split it or attach a server name — it round-trips through the model unchanged.
    room = MatrixRoom.from_admin_api({"room_id": V12_ROOM_ID})
    assert room.room_id == V12_ROOM_ID
    assert ":" not in room.room_id  # the whole point: no ":domain" to be tempted into parsing


def test_mxid_localpart_splits_mxids_not_room_ids() -> None:
    # The only ":"-splitter in the codebase is MXID-only. MXIDs keep their domain, so it works; a
    # domainless v12 room ID has no ":" and must never be fed to it.
    assert mxid_localpart("@alice:company.org") == "alice"
    assert ":" not in V12_ROOM_ID


def test_compute_mxid_from_username() -> None:
    user = {"username": "alice", "pk": 1}
    assert (
        compute_mxid(user, username_attribute="username", server_name="company.org") == "@alice:company.org"
    )


def test_compute_mxid_from_nested_attribute() -> None:
    user = {"attributes": {"matrix_name": "bob"}}
    assert (
        compute_mxid(user, username_attribute="attributes.matrix_name", server_name="company.org")
        == "@bob:company.org"
    )


def test_compute_mxid_missing_attribute_raises() -> None:
    with pytest.raises(KeyError):
        compute_mxid({"username": ""}, username_attribute="username", server_name="company.org")
    with pytest.raises(KeyError):
        compute_mxid({}, username_attribute="attributes.x", server_name="company.org")
