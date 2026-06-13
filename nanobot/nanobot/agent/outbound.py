"""Neutral outbound policy extension point for agent sends."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal

from nanobot.bus.events import OutboundMessage


@dataclass(frozen=True)
class OutboundRequest:
    """Context passed to an outbound guard before a message is published."""

    action: str
    outbound: OutboundMessage
    inbound_channel: str | None = None
    inbound_chat_id: str | None = None
    publish_outbound: Callable[[OutboundMessage], Awaitable[None]] | None = None


@dataclass(frozen=True)
class OutboundDecision:
    """Guard decision understood by nanobot without policy-specific imports."""

    kind: Literal["allow", "deny", "asked"]
    reason: str = ""
    approvers_label: str = ""

    @classmethod
    def allow(cls) -> OutboundDecision:
        return cls(kind="allow")

    @classmethod
    def deny(cls, reason: str) -> OutboundDecision:
        return cls(kind="deny", reason=reason)

    @classmethod
    def asked(cls, reason: str, approvers_label: str) -> OutboundDecision:
        return cls(kind="asked", reason=reason, approvers_label=approvers_label)


OutboundGuard = Callable[[OutboundRequest], Awaitable[OutboundDecision]]


async def allow_outbound(request: OutboundRequest) -> OutboundDecision:
    """Default standalone behavior: no external policy, publish normally."""
    del request
    return OutboundDecision.allow()
