from __future__ import annotations

import pytest

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.contracts import ChannelManagementSpec
from nanobot.channels.manager import ChannelManager
from nanobot.channels.plugin import ChannelPlugin
from nanobot.channels.registry import discover_plugins
from nanobot.config.loader import load_config, save_config
from nanobot.config.schema import Config
from nanobot.runtime_adapters import RuntimeAdapters


class _ExternalChannel(BaseChannel):
    name = "external_mock"
    display_name = "External mock"

    @classmethod
    def default_config(cls) -> dict[str, object]:
        return {"enabled": False}

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def send(self, _msg: OutboundMessage) -> None:
        pass


def _external_plugin() -> ChannelPlugin:
    return ChannelPlugin(
        name=_ExternalChannel.name,
        display_name=_ExternalChannel.display_name,
        runtime=f"{__name__}:_ExternalChannel",
        management=ChannelManagementSpec(default_config=_ExternalChannel.default_config),
        default_enabled=False,
    )


def test_stock_discovery_has_no_external_plugin() -> None:
    assert _ExternalChannel.name not in discover_plugins()


def test_external_plugin_is_discovered_before_manager_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = _external_plugin()
    monkeypatch.setattr(
        ChannelPlugin,
        "load_channel_class",
        lambda self: (
            _ExternalChannel
            if self is plugin
            else (_ for _ in ()).throw(AssertionError())
        ),
    )
    adapters = RuntimeAdapters(
        channel_plugins=lambda _enabled: {plugin.name: plugin},
    )
    config = Config.model_validate({"channels": {plugin.name: {"enabled": True}}})

    manager = ChannelManager(config, MessageBus(), runtime_adapters=adapters)

    assert set(manager.channels) == {plugin.name}
    assert manager._channel_owners[plugin.name] == plugin.name


def test_external_plugin_name_collision_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plugin = ChannelPlugin(
        name="telegram",
        display_name="Duplicate",
        runtime=f"{__name__}:_ExternalChannel",
    )
    monkeypatch.setattr(
        "nanobot.channels.registry._channel_package_names",
        lambda: ["telegram"],
    )
    monkeypatch.setattr(
        "nanobot.channels.registry.load_channel_package",
        lambda _name: plugin,
    )

    with pytest.raises(ValueError, match="collision"):
        discover_plugins(external_loader=lambda _enabled: {"telegram": plugin})


def test_config_roundtrip_preserves_unknown_nested_values_and_known_validation(
    tmp_path,
) -> None:
    path = tmp_path / "config.json"
    config = Config.model_validate(
        {
            "agents": {
                "familia_fallback": {
                    "primary": "primary-model",
                    "secondary": ["backup-model"],
                },
            },
            "gateway": {
                "heartbeat": {
                    "target_actor": "owner",
                    "product": {"enabled": True, "limits": [1, 2]},
                },
            },
        }
    )

    save_config(config, path)
    loaded = load_config(path)

    assert loaded.agents.familia_fallback == {
        "primary": "primary-model",
        "secondary": ["backup-model"],
    }
    assert loaded.gateway.heartbeat.target_actor == "owner"
    assert loaded.gateway.heartbeat.product == {"enabled": True, "limits": [1, 2]}
    with pytest.raises(ValueError):
        Config.model_validate({"channels": {"sendMaxRetries": 11}})
