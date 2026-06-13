import ast
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.outbound import OutboundDecision, OutboundRequest
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


@tool_parameters({"type": "object", "properties": {}})
class InstalledByTestTool(Tool):
    @property
    def name(self) -> str:
        return "installed_by_test"

    @property
    def description(self) -> str:
        return "test-only installer marker"

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _provider() -> MagicMock:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return provider


def test_agent_loop_runs_tool_installers_after_default_message_tool(tmp_path: Path) -> None:
    observed: list[bool] = []

    def install(loop: AgentLoop) -> None:
        observed.append(loop.tools.has("message"))
        loop.tools.register(InstalledByTestTool())

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        tool_installers=[install],
    )

    assert observed == [True]
    assert loop.tools.has("installed_by_test")


@pytest.mark.asyncio
async def test_agent_loop_uses_configured_outbound_guard_for_direct_reply() -> None:
    published: list[OutboundMessage] = []
    requests: list[OutboundRequest] = []

    async def publish(outbound: OutboundMessage) -> None:
        published.append(outbound)

    async def deny_guard(request: OutboundRequest) -> OutboundDecision:
        requests.append(request)
        return OutboundDecision.deny("blocked-by-test")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=Path("."),
        model="test-model",
        outbound_guard=deny_guard,
    )
    loop.bus.publish_outbound = publish
    inbound = InboundMessage(
        channel="telegram",
        sender_id="principal_a",
        chat_id="principal_a_chat",
        content="hi",
        actor="principal_a",
    )
    outbound = OutboundMessage(
        channel="telegram",
        chat_id="principal_b_chat",
        content="secret",
    )

    await loop._publish_reply_with_policy(inbound, outbound)

    assert published == []
    assert len(requests) == 1
    assert requests[0].action == "message.send"
    assert requests[0].inbound_channel == "telegram"
    assert requests[0].inbound_chat_id == "principal_a_chat"


@pytest.mark.asyncio
async def test_agent_loop_runs_inbound_enrichers_before_processing(tmp_path: Path) -> None:
    seen: list[InboundMessage] = []

    async def enrich(inbound: InboundMessage) -> None:
        seen.append(inbound)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        inbound_enrichers=[enrich],
    )
    loop.provider.chat_with_retry.return_value = None
    message = InboundMessage(
        channel="telegram",
        sender_id="principal_a",
        chat_id="principal_a_chat",
        content="hi",
        actor="principal_a",
    )

    with pytest.raises(Exception):
        await loop._process_message(message)

    assert seen == [message]


def test_loop_and_message_tool_do_not_import_familia_directly() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = [
        root / "nanobot" / "nanobot" / "agent" / "loop.py",
        root / "nanobot" / "nanobot" / "agent" / "tools" / "message.py",
    ]

    offenders: list[str] = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "familia":
                offenders.append(f"{path.name}:{node.lineno}:from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.split(".")[0] == "familia":
                        offenders.append(f"{path.name}:{node.lineno}:import {alias.name}")

    assert offenders == []
