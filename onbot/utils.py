"""Small, dependency-free helpers.

Ported from legacy ``onbot/utils.py`` — we keep the nested-dict helpers (used pervasively to read
dotted attribute paths out of Authentik objects) and drop the legacy ``synchronize_async_helper``
sync/async bridge (AD-7: async everything) and the ``requests``-based media download (Phase 6,
authenticated media). The ``Any``-as-sentinel hack is replaced with an explicit ``_MISSING`` marker.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final


class _Missing:
    """Sentinel distinguishing 'no fallback given' from a ``None`` fallback."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "<MISSING>"


_MISSING: Final = _Missing()


def get_nested_dict_val_by_path(
    data: dict[str, Any],
    key_path: Sequence[str],
    fallback_val: Any = _MISSING,
) -> Any:
    """Read a nested value via a key path (e.g. ``["attributes", "matrix_name"]``).

    Raises ``KeyError`` if the path is absent and no ``fallback_val`` was provided.
    """
    current: Any = data
    for key in key_path:
        try:
            current = current[key]
        except KeyError, TypeError:
            if fallback_val is _MISSING:
                raise KeyError(key) from None
            return fallback_val
    return current


def dict_has_nested_attr(
    data: dict[str, Any],
    key_path: Sequence[str],
    *,
    must_have_val: bool = False,
) -> bool:
    """Return whether ``data`` has the nested ``key_path`` (and a truthy value if ``must_have_val``)."""
    current: Any = data
    for key in key_path:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    if must_have_val:
        return bool(current)
    return True
