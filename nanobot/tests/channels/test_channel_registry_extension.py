from pathlib import Path
from types import SimpleNamespace

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel


class AdapterChannel(BaseChannel):
    name = "adapter"

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, msg: OutboundMessage) -> None:
        pass


def _config() -> SimpleNamespace:
    return SimpleNamespace(
        channels=SimpleNamespace(
            adapter=SimpleNamespace(enabled=True),
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


def test_channel_manager_accepts_adapter_channel_classes() -> None:
    from nanobot.channels.manager import ChannelManager

    manager = ChannelManager(
        _config(),
        MessageBus(),
        channel_classes={"adapter": AdapterChannel},
    )

    assert isinstance(manager.channels["adapter"], AdapterChannel)


def test_vk_channel_lives_outside_nanobot_core() -> None:
    root = Path(__file__).resolve().parents[3]
    path = root / "nanobot" / "nanobot" / "channels" / "vk.py"

    assert not path.exists()
