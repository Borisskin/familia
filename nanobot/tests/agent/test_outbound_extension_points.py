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
async def test_agent_loop_uses_configured_outbound_guard_for_direct_reply(
    tmp_path: Path,
) -> None:
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
        workspace=tmp_path,
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


@pytest.mark.parametrize(
    ("channel", "chat_id", "expected_proof"),
    [
        pytest.param(
            "telegram",
            "chat_a",
            {
                "private_mode": True,
                "is_group": False,
                "topic_id": None,
            },
            id="telegram-private",
        ),
        pytest.param(
            "vk",
            "2001",
            {
                "private_mode": True,
                "peer_id": "2001",
                "from_id": "2001",
            },
            id="vk-private",
        ),
    ],
)
@pytest.mark.parametrize("explicit_actor", [None, "principal_a"])
@pytest.mark.asyncio
async def test_process_direct_uses_trusted_actor_and_private_mode_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    chat_id: str,
    expected_proof: dict[str, Any],
    explicit_actor: str | None,
) -> None:
    resolved: list[tuple[str, str]] = []
    captured: list[InboundMessage] = []

    def resolve_actor(channel: str, chat_id: str) -> str | None:
        resolved.append((channel, chat_id))
        return "principal_a"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        direct_actor_resolver=resolve_actor,
    )

    async def connect_mcp() -> None:
        return None

    async def process_message(msg: InboundMessage, **_kwargs: Any) -> None:
        captured.append(msg)
        return None

    monkeypatch.setattr(loop, "_connect_mcp", connect_mcp)
    monkeypatch.setattr(loop, "_process_message", process_message)

    await loop.process_direct(
        "scheduled task",
        channel=channel,
        chat_id=chat_id,
        actor=explicit_actor,
    )

    assert resolved == [(channel, chat_id)]
    assert captured[0].actor == "principal_a"
    assert captured[0].sender_id == "principal_a"
    assert captured[0].metadata == {
        "private_mode_proof": expected_proof,
    }


@pytest.mark.parametrize("channel", ["telegram", "vk"])
@pytest.mark.asyncio
async def test_process_direct_rejects_explicit_actor_mismatch_before_processing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
) -> None:
    resolved: list[tuple[str, str]] = []
    captured: list[InboundMessage] = []

    def resolve_actor(resolved_channel: str, chat_id: str) -> str | None:
        resolved.append((resolved_channel, chat_id))
        return "principal_a"

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        direct_actor_resolver=resolve_actor,
    )

    async def connect_mcp() -> None:
        return None

    async def process_message(msg: InboundMessage, **_kwargs: Any) -> None:
        captured.append(msg)
        return None

    monkeypatch.setattr(loop, "_connect_mcp", connect_mcp)
    monkeypatch.setattr(loop, "_process_message", process_message)

    with pytest.raises(ValueError, match="trusted direct actor"):
        await loop.process_direct(
            "scheduled task",
            channel=channel,
            chat_id="chat_a",
            actor="principal_b",
        )

    assert resolved == [(channel, "chat_a")]
    assert captured == []


@pytest.mark.parametrize("channel", ["telegram", "vk"])
@pytest.mark.asyncio
async def test_process_direct_without_trusted_actor_keeps_private_proof_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
) -> None:
    captured: list[InboundMessage] = []

    async def archive_sink(_owner: str, _messages: list[dict]) -> None:
        return None

    async def resolve_private_session_owner(
        _session_key: str,
        _messages: list[dict],
    ) -> str | None:
        return None

    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        archive_sink=archive_sink,
        private_session_owner_resolver=resolve_private_session_owner,
    )
    original_process_message = loop._process_message

    async def connect_mcp() -> None:
        return None

    async def process_message(
        msg: InboundMessage,
        **kwargs: Any,
    ) -> OutboundMessage | None:
        captured.append(msg)
        return await original_process_message(msg, **kwargs)

    monkeypatch.setattr(loop, "_connect_mcp", connect_mcp)
    monkeypatch.setattr(loop, "_process_message", process_message)

    with pytest.raises(ValueError, match="private_mode_proof"):
        await loop.process_direct(
            "scheduled task",
            channel=channel,
            chat_id="unknown",
        )

    assert len(captured) == 1
    assert captured[0].actor is None
    assert captured[0].metadata == {}


@pytest.mark.parametrize("channel", ["cli", "api", "unknown"])
@pytest.mark.asyncio
async def test_process_direct_does_not_prove_non_server_channels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
) -> None:
    captured: list[InboundMessage] = []
    loop = AgentLoop(
        bus=MessageBus(),
        provider=_provider(),
        workspace=tmp_path,
        model="test-model",
        direct_actor_resolver=lambda _channel, _chat_id: "principal_a",
    )

    async def connect_mcp() -> None:
        return None

    async def process_message(msg: InboundMessage, **_kwargs: Any) -> None:
        captured.append(msg)
        return None

    monkeypatch.setattr(loop, "_connect_mcp", connect_mcp)
    monkeypatch.setattr(loop, "_process_message", process_message)

    await loop.process_direct(
        "direct task",
        channel=channel,
        chat_id="direct",
    )

    assert captured[0].actor == "principal_a"
    assert captured[0].metadata == {}


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
