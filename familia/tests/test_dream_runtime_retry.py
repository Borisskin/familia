"""Focused Familia Dream runtime and private-memory retry regressions."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from familia import principals as principals_mod
from familia.nanobot_extension import runtime_services
from familia.principals import (
    Identity,
    Principal,
    PrincipalRegistry,
    get_current_actor,
    get_current_channel,
    set_current_actor,
    set_current_channel,
)
from familia.tools import dream_memory as dream_memory_mod
from familia.tools.dream_memory import DreamMemorySetTool


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    value = PrincipalRegistry(
        [
            Principal(
                id="actor_alpha",
                display_name="Actor Alpha",
                identities=[Identity(channel="test", sender_id="alpha")],
                memx_key="alpha-key",
                roles=[],
            ),
            Principal(
                id="actor_beta",
                display_name="Actor Beta",
                identities=[Identity(channel="test", sender_id="beta")],
                memx_key="beta-key",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", value)
    return value


def _archive_client() -> MagicMock:
    client = MagicMock()
    client.get_profile_snapshot = AsyncMock(
        return_value={"value": "profile", "version": 41.0}
    )
    client.get.side_effect = lambda key: {
        "value:user_profile": "profile",
        "value:memory": "memory",
    }.get(key)
    return client


def _install_runtime_dream_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    tool_results: list[Any],
    process: Any,
    delete: Any,
) -> None:
    client = _archive_client()
    monkeypatch.setattr(
        runtime_services,
        "_read_archive_batch",
        lambda owner: (
            client,
            ["archive-owner"],
            [{"role": "user", "content": "private fact", "actor": owner}],
        ),
    )
    monkeypatch.setattr(runtime_services, "_dream_tools", Mock(side_effect=tool_results))
    monkeypatch.setattr(runtime_services, "_process_internal_turn", process)
    monkeypatch.setattr(runtime_services, "_delete_archive_facts", delete)


def _completed_turn(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
    return SimpleNamespace(metadata={"_stop_reason": "completed"})


def test_archive_history_records_private_owner_via_memx_catalog(monkeypatch) -> None:
    client = MagicMock()
    client.get.side_effect = lambda key: {
        "value:private_index": json.dumps(
            [{"name": "memory:archive-owner", "tags": []}]
        ),
        "memory:archive-owner": json.dumps(
            [{"role": "user", "content": "private fact", "actor": "actor_alpha"}]
        ),
    }.get(key)
    monkeypatch.setattr(runtime_services, "_private_memory_client", lambda owner: client)

    selected = runtime_services._read_archive_batch("actor_alpha")

    assert selected is not None
    _client, archive_ids, entries = selected
    assert archive_ids == ["archive-owner"]
    assert [entry["content"] for entry in entries] == ["private fact"]
    assert client.get.call_args_list[0].args == ("value:private_index",)


def test_dream_memory_tool_schema_excludes_model_routing_fields() -> None:
    tool = DreamMemorySetTool()
    properties = tool.parameters["properties"]

    assert set(properties) == {"kind", "fact_id", "value"}
    assert {
        "source_cursor",
        "scope",
        "actor",
        "other",
        "owner",
        "topic",
    }.isdisjoint(properties)
    assert tool.parameters["additionalProperties"] is False


def test_dream_memory_module_has_no_legacy_writer_branch() -> None:
    assert {
        "_resolve_full_key",
        "_merge_memory_document",
        "httpx",
        "CONSOLIDATOR_KEY_ENV",
    }.isdisjoint(vars(dream_memory_mod))


@pytest.mark.asyncio
async def test_runtime_dream_provider_error_keeps_archive_and_restores_context(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    process = AsyncMock(side_effect=RuntimeError("provider down"))
    delete = AsyncMock(return_value=True)
    _install_runtime_dream_fakes(
        monkeypatch,
        tool_results=[
            SimpleNamespace(
                _familia_dream_results=[("dream_memory_set", "committed: stored")]
            )
        ],
        process=process,
        delete=delete,
    )
    set_current_actor("caller")
    set_current_channel("caller-channel")

    result = await runtime_services.run_dream("actor_alpha", object())

    assert result is None
    process.assert_awaited_once()
    delete.assert_not_awaited()
    assert get_current_actor() == "caller"
    assert get_current_channel() == "caller-channel"
    set_current_actor(None)
    set_current_channel(None)


@pytest.mark.asyncio
async def test_runtime_dream_denied_archive_retries_then_commits(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    process = AsyncMock(side_effect=_completed_turn)
    delete = AsyncMock(return_value=True)
    _install_runtime_dream_fakes(
        monkeypatch,
        tool_results=[
            SimpleNamespace(
                _familia_dream_results=[
                    ("dream_memory_set", "Error: denied_invalid: rejected")
                ]
            ),
            SimpleNamespace(
                _familia_dream_results=[("dream_memory_set", "committed: stored")]
            ),
        ],
        process=process,
        delete=delete,
    )
    set_current_actor("caller")
    set_current_channel("caller-channel")

    first = await runtime_services.run_dream("actor_alpha", object())
    second = await runtime_services.run_dream("actor_alpha", object())

    assert first is None
    assert second == "Dream completed for actor_alpha; archived group consumed."
    assert process.await_count == 2
    delete.assert_awaited_once_with("actor_alpha", ["archive-owner"])
    assert get_current_actor() == "caller"
    assert get_current_channel() == "caller-channel"
    set_current_actor(None)
    set_current_channel(None)


@pytest.mark.asyncio
async def test_runtime_dream_commit_cleanup_failure_retries_without_loss(
    monkeypatch: pytest.MonkeyPatch,
    registry: PrincipalRegistry,
) -> None:
    process = AsyncMock(side_effect=_completed_turn)
    delete = AsyncMock(side_effect=[False, True])
    committed = SimpleNamespace(
        _familia_dream_results=[("dream_memory_set", "committed: stored")]
    )
    _install_runtime_dream_fakes(
        monkeypatch,
        tool_results=[committed, committed],
        process=process,
        delete=delete,
    )
    set_current_actor("caller")
    set_current_channel("caller-channel")

    first = await runtime_services.run_dream("actor_alpha", object())
    second = await runtime_services.run_dream("actor_alpha", object())

    assert first is None
    assert second == "Dream completed for actor_alpha; archived group consumed."
    assert delete.await_count == 2
    assert get_current_actor() == "caller"
    assert get_current_channel() == "caller-channel"
    set_current_actor(None)
    set_current_channel(None)


def test_familia_phase2_prompt_omits_protected_file_directives() -> None:
    prompt = runtime_services._dream_prompt(
        "actor_alpha",
        [{"role": "user", "content": "private fact", "timestamp": "now"}],
        profile="profile",
        memory="memory",
    )

    assert "actor_alpha" in prompt
    assert "private fact" in prompt
    assert "dream_memory_set" in prompt
    assert "Update memory files" not in prompt
    assert "edit_file" not in prompt
    assert "SOUL.md" not in prompt
    assert "USER.md" not in prompt
    assert "memory/MEMORY.md" not in prompt
    assert "source_cursor" not in prompt


def test_familia_dream_installer_removes_protected_file_editors(monkeypatch) -> None:
    from familia.nanobot_extension.cron import make_dream_tool_installers

    monkeypatch.setattr("familia.memx_client.memx_base_url", lambda: "http://memx")
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
        MagicMock(return_value=MagicMock()),
    )

    class Registry:
        def __init__(self) -> None:
            self.unregistered: list[str] = []
            self.tools: list[object] = []

        def unregister(self, name: str) -> None:
            self.unregistered.append(name)

        def register(self, tool: object) -> None:
            self.tools.append(tool)

    registry = Registry()
    make_dream_tool_installers(server_principal_getter=lambda: "actor_alpha")[0](
        registry, None
    )

    assert registry.unregistered == ["read_file", "edit_file", "write_file"]
    assert [getattr(tool, "name") for tool in registry.tools] == ["dream_memory_set"]


def test_dream_memory_has_no_legacy_private_document_router() -> None:
    assert not hasattr(dream_memory_mod, "_resolve_full_key")
