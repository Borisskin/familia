"""Familia inbound enrichment adapter for nanobot channels."""

from __future__ import annotations

from typing import Any


class FamiliaInboundEnricher:
    """Resolve familia actor ids for neutral nanobot inbound messages."""

    async def __call__(self, message: Any) -> None:
        from familia.principals import resolve_actor

        # Actor resolution remains in familia; nanobot only consumes the
        # enriched ``actor`` field on its neutral InboundMessage event.
        message.actor = resolve_actor(message.channel, str(message.sender_id))
