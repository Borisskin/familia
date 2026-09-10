from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.agent.outbound import OutboundDecision


@pytest.mark.asyncio
async def test_outbound_action_is_server_keyword_not_metadata() -> None:
    seen = []

    async def guard(request):
        seen.append(request)
        return OutboundDecision.allow()

    bus = MessageBus(outbound_guard=guard)
    message = OutboundMessage(
        channel="cli",
        chat_id="direct",
        content="hello",
        metadata={"_runtime_action": "ask.send"},
    )

    await bus.publish_outbound(message)
    await bus.publish_outbound(message, action="message.send")

    assert [request.action for request in seen] == ["message.send", "message.send"]


@pytest.mark.asyncio
async def test_process_direct_preserves_session_and_trusted_context_before_admission() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_locks = {}
    loop._connect_mcp = AsyncMock()
    loop._process_message = AsyncMock(return_value=None)
    loop._publish_outbound = AsyncMock()
    events = SimpleNamespace(
        run_status_changed=AsyncMock(),
        clear_turn=lambda _key: None,
    )
    loop._runtime_events = lambda: events

    request_context = RequestContext(
        channel="service",
        chat_id="chat-1",
        session_key="owner:service:chat-1",
        actor="owner",
    )
    admitted = InboundMessage(
        channel="service",
        sender_id="sender-1",
        chat_id="chat-1",
        content="hello",
        session_key_override="owner:service:chat-1",
        actor="owner",
    )
    loop._admit_message = AsyncMock(return_value=(admitted, None))

    await loop.process_direct(
        "hello",
        session_key="owner:service:chat-1",
        channel="service",
        chat_id="chat-1",
        sender_id="sender-1",
        request_context=request_context,
    )

    admitted_input = loop._admit_message.await_args.args[0]
    assert admitted_input.session_key_override == "owner:service:chat-1"
    assert admitted_input.metadata["_runtime_request_context"] is request_context
    assert loop._process_message.await_args.kwargs["session_key"] == "owner:service:chat-1"
    assert loop._process_message.await_args.kwargs["request_context"].actor == "owner"
    loop._connect_mcp.assert_awaited_once()


@pytest.mark.asyncio
async def test_process_direct_rejection_does_not_connect_mcp() -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop._session_locks = {}
    loop._connect_mcp = AsyncMock()
    loop._process_message = AsyncMock()
    loop._publish_outbound = AsyncMock()
    loop._admit_message = AsyncMock(
        return_value=(
            None,
            OutboundMessage(channel="service", chat_id="chat-1", content="denied"),
        )
    )

    result = await loop.process_direct(
        "hello",
        session_key="owner:service:chat-1",
        channel="service",
        chat_id="chat-1",
    )

    assert result is None
    loop._connect_mcp.assert_not_awaited()
    loop._publish_outbound.assert_awaited_once()
