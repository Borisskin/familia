import asyncio

import pytest

from nanobot.bus.events import CallbackEvent
from nanobot.bus.queue import MessageBus


@pytest.mark.asyncio
async def test_callback_dispatcher_routes_to_registered_handler() -> None:
    from nanobot.bus.callbacks import CallbackDispatcher

    bus = MessageBus()
    handled: list[CallbackEvent] = []

    async def handle_callback(evt: CallbackEvent) -> bool:
        handled.append(evt)
        return True

    dispatcher = CallbackDispatcher(bus, [handle_callback])
    await dispatcher.start()
    await bus.publish_callback(
        CallbackEvent(
            channel="telegram",
            sender_id="sender_a",
            chat_id="chat_a",
            payload={"action": "test"},
            actor="principal_a",
        )
    )

    for _ in range(20):
        if handled:
            break
        await asyncio.sleep(0.01)
    await dispatcher.stop()

    assert [evt.payload for evt in handled] == [{"action": "test"}]
