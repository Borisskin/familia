from __future__ import annotations

import asyncio

from familia import bootstrap


class _ToolRegistry:
    def __init__(self) -> None:
        self.registered = []

    def register(self, tool) -> None:
        self.registered.append(tool)


class _Bus:
    async def publish_outbound(self, message) -> None:
        return None


class _Loop:
    def __init__(self) -> None:
        self.bus = _Bus()
        self.tools = _ToolRegistry()


def test_install_tools_does_not_require_current_event_loop(monkeypatch) -> None:
    def fail_get_event_loop():
        raise AssertionError("install_tools must not call deprecated get_event_loop")

    monkeypatch.setattr(asyncio, "get_event_loop", fail_get_event_loop)

    loop = _Loop()
    bootstrap.install_tools(loop)

    assert loop.tools.registered
