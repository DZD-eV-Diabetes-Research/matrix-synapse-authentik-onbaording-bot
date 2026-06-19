"""Centralized Matrix CS-API version + feature knowledge (Phase 6).

The Matrix wire protocol moves under us — endpoints get promoted from ``unstable`` to versioned,
and capabilities like authenticated media or Simplified Sliding Sync only exist past a certain spec
version. Rather than sprinkle version strings and unstable-feature flags across the clients, the bot
negotiates once against ``GET /_matrix/client/versions`` and answers capability questions from the
result (:class:`ServerVersions`). This is the single source of truth for "can the server do X".

References:
* https://spec.matrix.org/latest/client-server-api/#get_matrixclientversions
* Authenticated media (MSC3916) is stable since spec **v1.11**.
* Simplified Sliding Sync (MSC4186) is still unstable: ``org.matrix.simplified_msc3575``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# GET path, relative to the ApiClientMatrix base (".../_matrix/client").
CLIENT_VERSIONS_PATH = "versions"

# Unstable feature flag advertising Simplified Sliding Sync support (MSC4186 / MSC3575).
SIMPLIFIED_SLIDING_SYNC_FEATURE = "org.matrix.simplified_msc3575"

# First spec version in which authenticated media (MSC3916) is stable.
AUTHENTICATED_MEDIA_SINCE = "v1.11"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse a spec version like ``"v1.11"`` into ``(1, 11)`` for ordering; ``()`` if unparsable."""
    try:
        return tuple(int(part) for part in version.removeprefix("v").split("."))
    except ValueError:
        return ()


@dataclass(slots=True)
class ServerVersions:
    """Parsed ``/versions`` response, answering capability questions for the rest of the bot."""

    versions: list[str] = field(default_factory=list)
    unstable_features: dict[str, bool] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, payload: dict[str, object] | None) -> ServerVersions:
        payload = payload or {}
        versions = payload.get("versions")
        features = payload.get("unstable_features")
        return cls(
            versions=[v for v in versions if isinstance(v, str)] if isinstance(versions, list) else [],
            unstable_features=(
                {k: bool(v) for k, v in features.items()} if isinstance(features, dict) else {}
            ),
        )

    def supports_simplified_sliding_sync(self) -> bool:
        return self.unstable_features.get(SIMPLIFIED_SLIDING_SYNC_FEATURE, False)

    def supports_authenticated_media(self) -> bool:
        threshold = _version_tuple(AUTHENTICATED_MEDIA_SINCE)
        return any(_version_tuple(v) >= threshold for v in self.versions if _version_tuple(v))
