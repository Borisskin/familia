"""Async message queue for decoupled channel-agent communication."""

import asyncio
import inspect
from collections.abc import Callable
from typing import Any

from nanobot.agent.outbound import OutboundDecision, OutboundGuard, OutboundRequest
from nanobot.agent.tools.context import (
    RUNTIME_REQUEST_CONTEXT_KEY,
    RequestContext,
    current_request_context,
)
from nanobot.bus.events import CallbackEvent, InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(
        self,
        outbound_guard: OutboundGuard | None = None,
        callback_handler: Callable[[CallbackEvent], Any] | None = None,
    ):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()
        self.callbacks: asyncio.Queue[CallbackEvent] = asyncio.Queue()
        self.outbound_guard = outbound_guard
        self.callback_handler = callback_handler

    def set_outbound_guard(self, guard: OutboundGuard | None) -> None:
        """Install the single server-side guard used before outbound enqueue."""
        self.outbound_guard = guard

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        return await self.inbound.get()

    async def publish_callback(self, event: CallbackEvent) -> None:
        """Queue one typed channel callback without altering its payload."""
        if not isinstance(event, CallbackEvent):
            raise TypeError("callback queue accepts CallbackEvent")
        await self.callbacks.put(event)

    async def consume_callback(self) -> CallbackEvent:
        """Consume the next channel callback (blocks until one is queued)."""
        return await self.callbacks.get()

    def set_callback_handler(
        self,
        handler: Callable[[CallbackEvent], Any] | None,
    ) -> None:
        """Install the optional adapter callback callable."""
        self.callback_handler = handler

    async def dispatch_callback(self, event: CallbackEvent | None = None) -> Any:
        """Run the configured callback handler for one queued/event payload."""
        if event is None:
            event = await self.consume_callback()
        if self.callback_handler is None:
            return None
        result = self.callback_handler(event)
        return await result if inspect.isawaitable(result) else result

    async def publish_outbound(
        self,
        msg: OutboundMessage,
        *,
        action: str | None = None,
        request_context: RequestContext | None = None,
    ) -> None:
        """Publish a response from the agent to channels.

        ``action`` is a server-owned keyword.  Outbound metadata can cross
        untrusted channel boundaries and therefore never selects policy.
        """
        guard = self.outbound_guard
        if guard is not None:
            metadata = dict(msg.metadata or {})
            trusted = request_context
            if not isinstance(trusted, RequestContext):
                trusted = metadata.get(RUNTIME_REQUEST_CONTEXT_KEY)
            if not isinstance(trusted, RequestContext):
                trusted = current_request_context()
            if not isinstance(trusted, RequestContext):
                trusted = None
            resolved_action = action.strip() if isinstance(action, str) else ""
            if not resolved_action:
                resolved_action = "message.send"
            request = OutboundRequest(
                action=resolved_action,
                outbound=msg,
                actor=trusted.actor if trusted is not None else None,
                inbound_channel=trusted.channel if trusted is not None else None,
                inbound_chat_id=trusted.chat_id if trusted is not None else None,
                metadata=metadata,
                publish_outbound=self._publish_unchecked,
            )
            decision = guard(request)
            if inspect.isawaitable(decision):
                decision = await decision
            if not isinstance(decision, OutboundDecision):
                raise TypeError("outbound guard must return OutboundDecision")
            if decision.kind != "allow":
                return
            msg = decision.outbound or msg
        await self.outbound.put(self._strip_runtime_metadata(msg))

    async def _publish_unchecked(self, msg: OutboundMessage) -> None:
        """Enqueue a guard-generated approval notice without recursive guarding."""
        await self.outbound.put(self._strip_runtime_metadata(msg))

    @staticmethod
    def _strip_runtime_metadata(msg: OutboundMessage) -> OutboundMessage:
        """Remove server-only provenance before a channel or serializer sees it."""
        metadata = dict(msg.metadata or {})
        for key in (
            RUNTIME_REQUEST_CONTEXT_KEY,
            "_runtime_actor",
            "_runtime_inbound_channel",
            "_runtime_inbound_chat_id",
            "_runtime_action",
        ):
            metadata.pop(key, None)
        if metadata == (msg.metadata or {}):
            return msg
        return OutboundMessage(
            channel=msg.channel,
            chat_id=msg.chat_id,
            content=msg.content,
            reply_to=msg.reply_to,
            media=list(msg.media),
            metadata=metadata,
            buttons=[list(row) for row in msg.buttons],
            event=msg.event,
        )

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        return await self.outbound.get()

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()

    @property
    def callback_size(self) -> int:
        """Number of pending callback events."""
        return self.callbacks.qsize()
