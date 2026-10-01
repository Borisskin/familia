"""VK delivers only what the user asked for: replies, and /compact feedback."""

import asyncio
from types import SimpleNamespace

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.events import ContextCompactionEvent

from familia.channels import vk as vk_channel


def _channel() -> vk_channel.VKChannel:
    channel = vk_channel.VKChannel(
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
    channel._client = object()  # type: ignore[assignment]
    return channel


def _compaction(phase: str, *, notify: bool) -> OutboundMessage:
    event = ContextCompactionEvent(compaction_id="c1", phase=phase, notify=notify)  # type: ignore[arg-type]
    return OutboundMessage(
        channel="vk",
        chat_id="200",
        content="Compressing context…" if phase == "started" else "Context compacted.",
        event=event,
    )


def _sent(monkeypatch, channel: vk_channel.VKChannel) -> list[dict]:
    calls: list[dict] = []

    async def api_stub(method: str, **params: object) -> object:
        calls.append({"method": method, **params})
        return {}

    monkeypatch.setattr(channel, "_api", api_stub)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["started", "succeeded", "failed", "cancelled"])
async def test_vk_drops_automatic_compaction_notice(monkeypatch, phase: str) -> None:
    channel = _channel()
    calls = _sent(monkeypatch, channel)

    await channel.send(_compaction(phase, notify=False))

    assert calls == []


@pytest.mark.asyncio
async def test_vk_delivers_explicit_compact_notice(monkeypatch) -> None:
    channel = _channel()
    calls = _sent(monkeypatch, channel)

    await channel.send(_compaction("succeeded", notify=True))

    assert [c["message"] for c in calls] == ["Context compacted."]


@pytest.mark.asyncio
async def test_vk_delivers_notice_when_channel_opts_in(monkeypatch) -> None:
    channel = _channel()
    channel.show_compaction_notices = True
    calls = _sent(monkeypatch, channel)

    await channel.send(_compaction("succeeded", notify=False))

    assert [c["message"] for c in calls] == ["Context compacted."]


@pytest.mark.asyncio
async def test_vk_dropped_notice_keeps_typing_indicator(monkeypatch) -> None:
    channel = _channel()
    _sent(monkeypatch, channel)
    typing = asyncio.create_task(asyncio.sleep(60))
    channel._typing_tasks["200"] = typing

    await channel.send(_compaction("started", notify=False))
    await asyncio.sleep(0)

    assert not typing.cancelled()
    typing.cancel()


@pytest.mark.asyncio
async def test_vk_final_reply_stops_typing_indicator(monkeypatch) -> None:
    channel = _channel()
    _sent(monkeypatch, channel)
    typing = asyncio.create_task(asyncio.sleep(60))
    channel._typing_tasks["200"] = typing

    await channel.send(OutboundMessage(channel="vk", chat_id="200", content="ответ"))
    await asyncio.sleep(0)

    assert typing.cancelled()


@pytest.mark.asyncio
async def test_vk_typing_indicator_outlives_three_minutes(monkeypatch) -> None:
    """Replies with several searches run longer than the old 180 s cap."""
    channel = _channel()
    calls = _sent(monkeypatch, channel)
    clock = {"now": 0.0}
    real_sleep = asyncio.sleep

    async def fake_sleep(seconds: float) -> None:
        clock["now"] += seconds
        await real_sleep(0)

    monkeypatch.setattr(vk_channel.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        vk_channel.asyncio,
        "get_event_loop",
        lambda: SimpleNamespace(time=lambda: clock["now"]),
    )

    task = asyncio.create_task(channel._typing_loop(200))
    while clock["now"] < 200 and not task.done():
        await real_sleep(0)

    assert not task.done()
    assert any(c["method"] == "messages.setActivity" for c in calls)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
