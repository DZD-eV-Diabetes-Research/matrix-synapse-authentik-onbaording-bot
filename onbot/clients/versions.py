"""Centralized Matrix CS-API version + feature knowledge (Phase 6).

The Matrix wire protocol moves under us: endpoints get promoted from ``unstable`` to versioned, and
capabilities only exist past a certain spec version. Rather than sprinkle version strings and
unstable-feature flags across the clients, the bot negotiates once against
``GET /_matrix/client/versions`` and answers capability questions from the result
(:class:`ServerVersions`). This is the single source of truth for "can the server do X".

The only question asked today is whether the server offers Simplified Sliding Sync (MSC4186, still
unstable: ``org.matrix.simplified_msc3575``), which gates the sync stream. Media downloads always use
the authenticated endpoint (MSC3916, stable since spec v1.11), so no check is made for it (RUN-16).

Reference: https://spec.matrix.org/latest/client-server-api/#get_matrixclientversions
"""

from __future__ import annotations

from dataclasses import dataclass, field

# GET path, relative to the ApiClientMatrix base (".../_matrix/client").
CLIENT_VERSIONS_PATH = "versions"

# Unstable feature flag advertising Simplified Sliding Sync support (MSC4186 / MSC3575).
SIMPLIFIED_SLIDING_SYNC_FEATURE = "org.matrix.simplified_msc3575"


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
