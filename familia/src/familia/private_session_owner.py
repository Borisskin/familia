"""Resolve the owner of a supported private session before consolidation."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .principals import PrincipalRegistry


class PrivateSessionOwnerResolver:
    """Resolve one private session to its single registered principal."""

    def __init__(self, registry_getter: Callable[[], PrincipalRegistry]) -> None:
        self._registry_getter = registry_getter

    async def __call__(
        self,
        session_key: str,
        messages: list[dict[str, Any]],
        session_context: dict[str, Any] | None = None,
    ) -> str | None:
        if not isinstance(session_key, str) or ":" not in session_key:
            return None
        channel, chat_id = session_key.split(":", 1)
        if channel == "cron":
            # A cron key is an internal service session, not a chat identity.
            # Only the saved delivery route may prove a private owner; the
            # job creator is deliberately not accepted as a substitute.
            if not isinstance(session_context, dict):
                return None
            route_channel = session_context.get("channel")
            route_chat_id = session_context.get("chat_id")
            if route_channel not in {"telegram", "vk"} or not route_chat_id:
                return None
            channel = route_channel
            chat_id = str(route_chat_id)
        if channel not in {"telegram", "vk"} or not chat_id:
            return None

        owner = self._registry_getter().resolve(channel, chat_id)
        if owner is None:
            return None
        if session_key.startswith("cron:"):
            expected_actor = session_context.get("target_actor")
            if expected_actor is not None and expected_actor != owner:
                return None

        for message in messages:
            if message.get("role") != "user":
                continue
            actor = message.get("actor")
            if actor and actor != owner:
                return None
        return owner
