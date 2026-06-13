import pytest

from nanobot.agent.outbound import OutboundDecision, OutboundRequest
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import OutboundMessage


@pytest.mark.asyncio
async def test_message_tool_returns_error_when_no_target_context() -> None:
    tool = MessageTool()
    result = await tool.execute(content="test")
    assert result == "Error: No target channel/chat specified"


@pytest.mark.asyncio
async def test_message_tool_uses_configured_outbound_guard_before_publish() -> None:
    published: list[OutboundMessage] = []
    requests: list[OutboundRequest] = []

    async def publish(outbound: OutboundMessage) -> None:
        published.append(outbound)

    async def deny_guard(request: OutboundRequest) -> OutboundDecision:
        requests.append(request)
        return OutboundDecision.deny("blocked-by-test")

    tool = MessageTool(send_callback=publish, outbound_guard=deny_guard)
    tool.set_context("telegram", "principal_a_chat")

    result = await tool.execute(content="secret")

    assert result == "Policy denied message.send to telegram:principal_a_chat: blocked-by-test"
    assert published == []
    assert len(requests) == 1
    request = requests[0]
    assert request.action == "message.send"
    assert request.outbound.content == "secret"
    assert request.inbound_channel == "telegram"
    assert request.inbound_chat_id == "principal_a_chat"


@pytest.mark.asyncio
async def test_message_tool_publishes_after_outbound_guard_allows() -> None:
    published: list[OutboundMessage] = []

    async def publish(outbound: OutboundMessage) -> None:
        published.append(outbound)

    async def allow_guard(request: OutboundRequest) -> OutboundDecision:
        assert request.outbound.chat_id == "principal_b_chat"
        return OutboundDecision.allow()

    tool = MessageTool(send_callback=publish, outbound_guard=allow_guard)

    result = await tool.execute(
        content="hello",
        channel="telegram",
        chat_id="principal_b_chat",
    )

    assert result == "Message sent to telegram:principal_b_chat"
    assert [message.content for message in published] == ["hello"]
