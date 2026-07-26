from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from nanobot.bus.events import CallbackEvent
from nanobot.bus.queue import MessageBus


def test_familia_channel_manager_kwargs_registers_vk_channel() -> None:
    from familia.bootstrap import make_channel_manager_kwargs
    from familia.channels.vk import VKChannel

    kwargs = make_channel_manager_kwargs()

    assert kwargs["channel_classes"]["vk"] is VKChannel


@pytest.mark.asyncio
async def test_vk_callback_event_resolves_actor(monkeypatch) -> None:
    from familia.channels import vk as vk_channel

    bus = MessageBus()
    channel = vk_channel.VKChannel(
        SimpleNamespace(
            enabled=True,
            group_id=1,
            access_token="token_a",
            api_version="5.199",
            allow_from=["sender_a"],
            long_poll_wait=25,
            streaming=False,
            proxy="",
            media_proxy="",
        ),
        bus,
    )
    calls: list[tuple[str, str]] = []

    async def api_stub(method: str, **params: object) -> object:
        assert method == "messages.sendMessageEventAnswer"
        return {}

    async def collapse_stub(
        peer_id: int,
        conversation_message_id: int,
        label: str,
        wrapped_payload: object,
    ) -> None:
        assert peer_id == 200
        assert conversation_message_id == 300
        assert label == "Choose A"

    def resolve_actor(channel_name: str, sender_id: str) -> str | None:
        calls.append((channel_name, sender_id))
        return "principal_a"

    monkeypatch.setattr(channel, "_api", api_stub)
    monkeypatch.setattr(channel, "_collapse_keyboard", collapse_stub)
    monkeypatch.setattr(vk_channel, "resolve_actor", resolve_actor)

    await channel._handle_message_event(
        {
            "event_id": "event_a",
            "user_id": 100,
            "peer_id": 200,
            "conversation_message_id": 300,
            "payload": '{"_l":"Choose A","_p":{"action":"approve"},"_cid":"corr_a"}',
        }
    )

    event = await bus.consume_callback()

    assert isinstance(event, CallbackEvent)
    assert event.channel == "vk"
    assert event.sender_id == "100"
    assert event.chat_id == "200"
    assert event.actor == "principal_a"
    assert event.payload == {"action": "approve"}
    assert event.metadata["correlation_id"] == "corr_a"
    assert calls == [("vk", "100")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("from_id", "peer_id", "private_mode"),
    [(100, 100, True), (100, 200, False)],
)
async def test_vk_message_new_preserves_private_mode_proof(
    monkeypatch, from_id: int, peer_id: int, private_mode: bool
) -> None:
    from familia.channels.vk import VKChannel

    channel = VKChannel(
        SimpleNamespace(
            enabled=True,
            group_id=1,
            access_token="token_a",
            api_version="5.199",
            allow_from=["*"],
            long_poll_wait=25,
            streaming=False,
            proxy="",
            media_proxy="",
        ),
        MessageBus(),
    )
    handled = []

    async def capture_handle(**kwargs) -> None:
        handled.append(kwargs)

    monkeypatch.setattr(channel, "_handle_message", capture_handle)
    monkeypatch.setattr(channel, "_start_typing", lambda _chat_id: None)
    monkeypatch.setattr(
        channel,
        "_download_attachments",
        AsyncMock(return_value=([], [])),
    )

    await channel._handle_update(
        {
            "type": "message_new",
            "object": {
                "message": {
                    "from_id": from_id,
                    "peer_id": peer_id,
                    "text": "hello",
                    "attachments": [],
                }
            },
        }
    )

    assert len(handled) == 1
    assert handled[0]["sender_id"] == str(from_id)
    assert handled[0]["chat_id"] == str(peer_id)
    assert handled[0]["metadata"]["private_mode_proof"] == {
        "private_mode": private_mode,
        "peer_id": str(peer_id),
        "from_id": str(from_id),
    }
