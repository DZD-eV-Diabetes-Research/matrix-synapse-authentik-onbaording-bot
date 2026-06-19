"""Unit tests for MXID / localpart mapping (AD-6)."""

import pytest

from onbot.identity import build_canonical, compute_mxid


def test_build_canonical_sigils() -> None:
    assert build_canonical("alice", "company.org", "@") == "@alice:company.org"
    assert build_canonical("Room", "company.org", "#") == "#Room:company.org"


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
