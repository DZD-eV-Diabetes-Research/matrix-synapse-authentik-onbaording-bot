"""MAS-aware authentication (AD-6).

Token providers (:mod:`onbot.auth.token_provider`): a static/compat token or OAuth2
client-credentials with transparent refresh, behind one :class:`TokenProvider` protocol.
"""

from onbot.auth.token_provider import (
    OAuth2ClientCredentialsTokenProvider,
    OAuth2TokenError,
    StaticTokenProvider,
    TokenProvider,
)

__all__ = [
    "OAuth2ClientCredentialsTokenProvider",
    "OAuth2TokenError",
    "StaticTokenProvider",
    "TokenProvider",
]
