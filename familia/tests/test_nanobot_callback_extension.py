import asyncio

import pytest

from nanobot.bus.events import CallbackEvent
from nanobot.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_familia_callback_handler_turns_unmatched_press_into_inbound(monkeypatch: pytest.MonkeyPatch) -> None:
    from familia import bootstrap
    from familia.principals import Identity, Principal, PrincipalRegistry

    monkeypatch.delenv("FAMILIA_OWNER_ACTOR", raising=False)
    registry = PrincipalRegistry([
        Principal(
            id="principal_a",
            display_name="Principal A",
            identities=[Identity(channel="telegram", sender_id="sender_a")],
        ),
    ])
    monkeypatch.setattr(
        "familia.bus.callback_dispatcher.get_registry", lambda: registry,
    )
    monkeypatch.setattr(
        "familia.bus.callback_dispatcher.resolve_actor", registry.resolve,
    )
    bus = MessageBus()

    handlers = bootstrap.make_callback_handlers(bus)

    assert len(handlers) == 1
    handled = await handlers[0].handle_callback(
        CallbackEvent(
            channel="telegram",
            sender_id="sender_a",
            chat_id="chat_a",
            payload={"choice": "a"},
            actor="principal_a",
        )
    )
    inbound = await asyncio.wait_for(bus.consume_inbound(), timeout=1)

    assert handled is True
    assert inbound.actor == "principal_a"
    assert inbound.metadata["callback"] is True


@pytest.mark.asyncio
async def test_familia_callback_handler_rejects_unknown_sender(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from familia import bootstrap
    from familia.principals import Identity, Principal, PrincipalRegistry

    registry = PrincipalRegistry([
        Principal(
            id="principal_a",
            display_name="Principal A",
            identities=[Identity(channel="telegram", sender_id="sender_a")],
        ),
    ])
    monkeypatch.setattr(
        "familia.bus.callback_dispatcher.get_registry", lambda: registry,
    )
    monkeypatch.setattr(
        "familia.bus.callback_dispatcher.resolve_actor", registry.resolve,
    )
    bus = MessageBus()
    handler = bootstrap.make_callback_handlers(bus)[0]

    await handler.handle_callback(
        CallbackEvent(
            channel="telegram",
            sender_id="unknown_sender",
            chat_id="chat_a",
            payload={"choice": "a"},
            actor="principal_a",
        )
    )

    assert bus.inbound_size == 0
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(bus.consume_inbound(), timeout=0.1)
