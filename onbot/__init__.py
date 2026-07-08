"""Onbot — keep a Matrix homeserver in sync with Authentik and onboard new users.

Clean-slate rebuild targeting modern Matrix (MAS / next-gen auth, authenticated media,
sliding sync). See ``BATTLE_PLAN.md`` for the architecture and ``GOALS.md`` for intent.
"""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("onbot")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0+unknown"
