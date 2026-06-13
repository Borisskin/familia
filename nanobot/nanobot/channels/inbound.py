"""Neutral inbound message enrichment extension point for channels."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from nanobot.bus.events import InboundMessage


InboundEnricher = Callable[[InboundMessage], Awaitable[None]]


async def noop_inbound_enricher(message: InboundMessage) -> None:
    """Default standalone behavior: leave inbound messages unchanged."""
    del message
