"""Tests for CS-API version/feature parsing (ServerVersions)."""

from __future__ import annotations

from onbot.clients.versions import ServerVersions


def test_parses_versions_and_features() -> None:
    sv = ServerVersions.from_payload(
        {
            "versions": ["v1.1", "v1.11", "v1.13"],
            "unstable_features": {"org.matrix.simplified_msc3575": True},
        }
    )
    assert sv.supports_simplified_sliding_sync() is True
    assert sv.supports_authenticated_media() is True


def test_no_sliding_sync_feature() -> None:
    sv = ServerVersions.from_payload({"versions": ["v1.10"], "unstable_features": {}})
    assert sv.supports_simplified_sliding_sync() is False
    # v1.10 predates authenticated media (stable since v1.11).
    assert sv.supports_authenticated_media() is False


def test_tolerates_garbage_payload() -> None:
    sv = ServerVersions.from_payload({"versions": "nope", "unstable_features": 5})  # type: ignore[dict-item]
    assert sv.versions == []
    assert sv.unstable_features == {}
    assert sv.supports_authenticated_media() is False


def test_empty_payload() -> None:
    sv = ServerVersions.from_payload(None)
    assert sv.supports_simplified_sliding_sync() is False
