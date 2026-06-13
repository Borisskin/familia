import pytest

from nanobot.bus.events import CallbackEvent
from nanobot.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_familia_callback_handler_turns_unmatched_press_into_inbound(monkeypatch: pytest.MonkeyPatch) -> None:
    from familia import bootstrap

    monkeypatch.delenv("FAMILIA_OWNER_ACTOR", raising=False)
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
    inbound = await bus.consume_inbound()

    assert handled is True
    assert inbound.actor == "principal_a"
    assert inbound.metadata["callback"] is True
