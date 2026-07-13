"""RP-040 owner/private and decoded retrieval matrix."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest


def test_principal_client_decodes_own_values_and_never_reads_peer_private(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from familia.acl import codec
    from familia.acl import principal_memory

    calls: list[tuple[str, str]] = []

    def fake_get_raw(key: str, *, api_key: str):
        calls.append((key, api_key))
        return codec.encode("decoded-value", ["secret"])

    monkeypatch.setattr(principal_memory, "get_raw", fake_get_raw)

    client = principal_memory.PrincipalMemoryClient("alice", "alice-key")
    assert client.get("value:memory") == "decoded-value"
    calls.clear()
    assert client.get_other("bob", "value:memory") is None
    assert calls == []


def test_memory_get_private_is_owner_only_even_for_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    from familia.tools import memory

    principals = {
        "alice": SimpleNamespace(id="alice", memx_key="alice-key"),
        "bob": SimpleNamespace(id="bob", memx_key="bob-key"),
    }
    registry = SimpleNamespace(ids=tuple(principals), get=principals.get)
    monkeypatch.setattr(memory, "get_current_actor", lambda: "alice")
    monkeypatch.setattr(memory, "get_registry", lambda: registry)

    async def leak_if_called(**_kwargs):
        return "peer-private-leak"

    tool = memory.MemoryGetTool(base_url="http://unused.invalid")
    monkeypatch.setattr(tool, "_read_peer_private", leak_if_called)
    result = asyncio.run(tool.execute("private", "value:memory", actor="bob"))

    assert result == "(no value stored)"


def test_missing_unknown_actor_and_scope_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    from familia.tools import memory

    monkeypatch.setattr(memory, "get_current_actor", lambda: None)
    result = asyncio.run(memory.MemoryGetTool().execute("private", "value:memory"))
    assert result.startswith("Error: no actor in context")

    registry = SimpleNamespace(ids=("alice",), get=lambda principal_id: None)
    monkeypatch.setattr(memory, "get_registry", lambda: registry)
    full_key, error = memory._resolve_full_key("unknown", "key", "ghost")
    assert full_key is None
    assert error and "unknown" in error.lower()
