from __future__ import annotations

from types import SimpleNamespace


def test_heartbeat_source_reader_uses_principal_memx(monkeypatch) -> None:
    from familia.nanobot_extension import cron

    class _Registry:
        def get(self, actor_id: str):
            assert actor_id == "principal_a"
            return SimpleNamespace(memx_key="mem_key_a")

    class _Client:
        def __init__(self, actor_id: str, memx_key: str) -> None:
            assert actor_id == "principal_a"
            assert memx_key == "mem_key_a"

        def get(self, key: str) -> str:
            assert key == "value:heartbeat"
            return "check shared calendar"

    monkeypatch.setattr(cron, "get_registry", lambda: _Registry())
    monkeypatch.setattr(cron, "PrincipalMemoryClient", _Client)

    reader = cron.make_heartbeat_source_reader("principal_a")

    assert reader() == ("check shared calendar", "memx")


def test_heartbeat_source_reader_fails_closed_without_memx_key(monkeypatch) -> None:
    from familia.nanobot_extension import cron

    class _Registry:
        def get(self, actor_id: str):
            assert actor_id == "principal_a"
            return SimpleNamespace(memx_key=None)

    monkeypatch.setattr(cron, "get_registry", lambda: _Registry())

    reader = cron.make_heartbeat_source_reader("principal_a")

    assert reader() == (None, None)


def test_make_dream_tool_installers_registers_dream_memory_tool() -> None:
    from familia.nanobot_extension import cron

    class _Registry:
        def __init__(self) -> None:
            self.tools = []

        def register(self, tool) -> None:
            self.tools.append(tool)

    registry = _Registry()
    installers = cron.make_dream_tool_installers()

    assert len(installers) == 1
    installers[0](registry, SimpleNamespace(workspace=None))
    assert [tool.name for tool in registry.tools] == ["dream_memory_set"]
