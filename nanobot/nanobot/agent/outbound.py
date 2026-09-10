"""Neutral outbound policy request and decision types."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from nanobot.agent.tools.context import RUNTIME_REQUEST_CONTEXT_KEY

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage


@dataclass(frozen=True)
class OutboundRequest:
    """Server-owned context passed to a guard before publication."""

    action: str
    outbound: OutboundMessage
    actor: str | None = None
    inbound_channel: str | None = None
    inbound_chat_id: str | None = None
    metadata: Mapping[str, object] | None = None
    publish_outbound: Callable[[OutboundMessage], Awaitable[None]] | None = None


@dataclass(frozen=True)
class OutboundDecision:
    """Policy result understood by nanobot and product adapters."""

    kind: Literal["allow", "deny", "asked"]
    reason: str = ""
    approvers_label: str = ""
    outbound: OutboundMessage | None = None

    @classmethod
    def allow(cls, outbound: OutboundMessage | None = None) -> OutboundDecision:
        return cls(kind="allow", outbound=outbound)

    @classmethod
    def deny(cls, reason: str) -> OutboundDecision:
        return cls(kind="deny", reason=reason)

    @classmethod
    def asked(cls, reason: str, approvers_label: str) -> OutboundDecision:
        return cls(kind="asked", reason=reason, approvers_label=approvers_label)


OutboundGuard = Callable[[OutboundRequest], Awaitable[OutboundDecision]]


async def allow_outbound(request: OutboundRequest) -> OutboundDecision:
    """Standalone default: permit publication without product policy."""
    del request
    return OutboundDecision.allow()


def replace_outbound(request: OutboundRequest, outbound: OutboundMessage) -> OutboundRequest:
    """Return the same authorization request with a guarded message."""
    return replace(request, outbound=outbound)


__all__ = [
    "OutboundDecision",
    "OutboundGuard",
    "OutboundRequest",
    "RUNTIME_REQUEST_CONTEXT_KEY",
    "allow_outbound",
    "replace_outbound",
]
