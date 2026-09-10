"""Canonical logical identity for Familia private sessions.

The ``familia:`` prefix is only a namespace marker.  Parsing a key never
grants access; callers must still resolve the returned principal in the
current registry.
"""

from __future__ import annotations

import re
from typing import Final

_PREFIX: Final = "familia:"
_PRINCIPAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _canonical_principal(value: object) -> str | None:
    """Return a principal only when it is already in canonical form."""
    if not isinstance(value, str) or not _PRINCIPAL_ID.fullmatch(value):
        return None
    # The codec validates syntax only; registration and authorization stay with
    # the caller that resolves the principal in its current registry.
    return value


def make_private_session_key(actor: str, original_session_key: str) -> str:
    """Build ``familia:<principal>:<original-session-key>``.

    The original route is opaque and remains byte-for-byte unchanged.  It may
    contain colons; the parser splits only the delimiter after the principal.
    Newlines and NUL are rejected because a key crosses file and log boundaries.
    """
    principal = _canonical_principal(actor)
    if principal is None:
        raise ValueError("actor must be a canonical principal id")
    if (
        not isinstance(original_session_key, str)
        or not original_session_key
        or any(char in original_session_key for char in "\x00\r\n")
    ):
        raise ValueError("original_session_key must be a non-empty route")
    return f"{_PREFIX}{principal}:{original_session_key}"


def parse_private_session_key(key: str) -> tuple[str, str] | None:
    """Parse a private key, without making an authorization decision."""
    if not isinstance(key, str) or not key.startswith(_PREFIX):
        return None
    principal, separator, original = key[len(_PREFIX) :].partition(":")
    if not separator or not original:
        return None
    if any(char in key for char in "\x00\r\n"):
        return None
    if _canonical_principal(principal) is None:
        return None
    return principal, original


__all__ = ["make_private_session_key", "parse_private_session_key"]
