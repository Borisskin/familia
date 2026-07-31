import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.bus.events import InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class DummyChannel(BaseChannel):
    name = "dummy"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> None:
        pass


async def _next_inbound(bus: MessageBus) -> InboundMessage:
    return await bus.consume_inbound()


@pytest.mark.asyncio
async def test_handle_message_runs_inbound_enricher_before_publish() -> None:
    bus = MessageBus()
    channel = DummyChannel(SimpleNamespace(allow_from=["principal_a_sender"]), bus)

    async def enrich(message: InboundMessage) -> None:
        message.actor = "principal_a"

    channel.set_inbound_enrichers([enrich])

    await channel._handle_message(
        sender_id="principal_a_sender",
        chat_id="principal_a_chat",
        content="hello",
    )

    published = await _next_inbound(bus)
    assert published.actor == "principal_a"
    assert published.channel == "dummy"
    assert published.sender_id == "principal_a_sender"


@pytest.mark.asyncio
async def test_handle_message_drops_known_actor_removed_from_allowlist() -> None:
    bus = MessageBus()
    channel = DummyChannel(SimpleNamespace(allow_from=["principal_b_sender"]), bus)

    async def enrich(message: InboundMessage) -> None:
        message.actor = "principal_a"

    channel.set_inbound_enrichers([enrich])

    await channel._handle_message(
        sender_id="principal_a_sender",
        chat_id="principal_a_chat",
        content="hello",
    )

    assert bus.inbound.empty()


@pytest.mark.asyncio
async def test_handle_message_forwards_unknown_sender_for_pending_flow() -> None:
    bus = MessageBus()
    channel = DummyChannel(SimpleNamespace(allow_from=["principal_b_sender"]), bus)

    async def enrich(message: InboundMessage) -> None:
        message.actor = None

    channel.set_inbound_enrichers([enrich])

    await channel._handle_message(
        sender_id="principal_a_sender",
        chat_id="principal_a_chat",
        content="hello",
    )

    published = await _next_inbound(bus)
    assert published.actor is None
    assert published.sender_id == "principal_a_sender"


@pytest.mark.asyncio
async def test_handle_message_enriches_unknown_sender_once() -> None:
    bus = MessageBus()
    channel = DummyChannel(SimpleNamespace(allow_from=["principal_b_sender"]), bus)
    calls = 0

    async def enrich(message: InboundMessage) -> None:
        nonlocal calls
        calls += 1
        message.actor = None

    channel.set_inbound_enrichers([enrich])

    await channel._handle_message(
        sender_id="principal_a_sender",
        chat_id="principal_a_chat",
        content="hello",
    )

    assert calls == 1


@pytest.mark.asyncio
async def test_should_drop_inbound_runs_enricher_for_prefilter_call() -> None:
    bus = MessageBus()
    channel = DummyChannel(SimpleNamespace(allow_from=["principal_b_sender"]), bus)

    async def enrich(message: InboundMessage) -> None:
        message.actor = "principal_a"

    channel.set_inbound_enrichers([enrich])

    assert await channel.should_drop_inbound("principal_a_sender") is True


def test_channel_base_does_not_import_familia_directly() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "nanobot" / "nanobot" / "channels" / "base.py"

    offenders: list[str] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "familia":
            offenders.append(f"{path.name}:{node.lineno}:from {node.module}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "familia":
                    offenders.append(f"{path.name}:{node.lineno}:import {alias.name}")

    assert offenders == []


def test_channel_manager_passes_inbound_enrichers_to_channels(monkeypatch) -> None:
    from nanobot.channels.manager import ChannelManager

    bus = MessageBus()

    async def enrich(message: InboundMessage) -> None:
        message.actor = "principal_a"

    config = SimpleNamespace(
        channels=SimpleNamespace(
            dummy=SimpleNamespace(enabled=True),
            transcription_provider="groq",
            transcription_audio_budget_s=300,
            transcription_lang="",
        ),
        providers=SimpleNamespace(
            groq=SimpleNamespace(api_key="", api_base=""),
            openai=SimpleNamespace(api_key="", api_base=""),
            yandex=SimpleNamespace(api_key="", api_base="", folder_id=""),
        ),
    )

    monkeypatch.setattr("nanobot.channels.registry.discover_all", lambda: {"dummy": DummyChannel})

    manager = ChannelManager(config, bus, inbound_enrichers=[enrich])

    assert manager.channels["dummy"]._inbound_enrichers == [enrich]
