"""Admin applies channel and principal edits by sending SIGHUP to the gateway."""

from __future__ import annotations

import asyncio
import os
import signal
from pathlib import Path

import pytest
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.channels.manager import ChannelManager
from nanobot.channels.plugin import ChannelPlugin
from nanobot.config.schema import Config


class _ReloadChannel(BaseChannel):
    name = "reloadprobe"
    display_name = "ReloadProbe"

    def __init__(self, config, bus):
        super().__init__(config, bus)
        self.started = asyncio.Event()
        self.stopped = asyncio.Event()

    async def start(self):
        self._running = True
        self.started.set()
        await self.stopped.wait()

    async def stop(self):
        self._running = False
        self.stopped.set()

    async def send(self, msg):  # pragma: no cover - not used
        raise AssertionError("send should not be called")


def _config(enabled: bool) -> Config:
    return Config.model_validate(
        {"channels": {"websocket": {"enabled": False}, "reloadprobe": {"enabled": enabled}}}
    )


@pytest.mark.asyncio
async def test_reload_from_disk_applies_channel_changes(monkeypatch, tmp_path: Path) -> None:
    plugin = ChannelPlugin(
        name="reloadprobe",
        display_name="ReloadProbe",
        runtime=f"{__name__}:_ReloadChannel",
    )
    monkeypatch.setattr(
        "nanobot.channels.registry.discover_plugins",
        lambda enabled_names=None: {"reloadprobe": plugin},
    )
    on_disk = iter([_config(True), _config(False)])
    monkeypatch.setattr("nanobot.config.loader.load_config", lambda _path=None: next(on_disk))
    manager = ChannelManager(_config(False), MessageBus(), config_path=tmp_path / "config.json")
    assert manager.channels == {}

    await manager.reload_from_disk()
    channel = manager.channels["reloadprobe"]
    await asyncio.wait_for(channel.started.wait(), timeout=1)

    task = await manager.reload_from_disk()
    assert channel.stopped.is_set()
    assert manager.channels == {}
    await asyncio.wait_for(task, timeout=1)


@pytest.mark.skipif(not hasattr(signal, "SIGHUP"), reason="POSIX only")
@pytest.mark.asyncio
async def test_sighup_reloads_instead_of_terminating() -> None:
    from nanobot.cli.gateway_runtime import _install_gateway_reload_handler

    reloads = 0
    reloaded = asyncio.Event()

    async def reload() -> None:
        nonlocal reloads
        reloads += 1
        reloaded.set()

    restore = _install_gateway_reload_handler(asyncio.get_running_loop(), reload)
    try:
        os.kill(os.getpid(), signal.SIGHUP)
        await asyncio.wait_for(reloaded.wait(), timeout=1)
    finally:
        restore()
    assert reloads == 1


def test_familia_adapters_reload_principals(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from familia import bootstrap

    config = SimpleNamespace(
        workspace_path=tmp_path / "workspace",
        runtime_data_dir=tmp_path / "runtime",
    )
    adapters = bootstrap.make_runtime_adapters(config, MessageBus())

    assert adapters.reload_runtime is bootstrap.reload_runtime_registry
