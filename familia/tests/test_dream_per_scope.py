"""Unit tests for Familia Dream memory.

Automatic compaction receives one server-resolved private owner and exposes
only atomic profile, memory, and delete operations.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.policy import Decision, PolicyContext
from familia.policy.engine import load_engine
from familia.tools.dream_memory import DreamMemorySetTool


REPO_ROOT = Path(__file__).resolve().parents[1] / "src" / "familia" / "config"


# --- archive ownership -----------------------------------------------------

def test_archive_history_records_private_owner_via_memx_catalog(monkeypatch) -> None:
    from familia.nanobot_extension import runtime_services

    client = MagicMock()
    client.get.side_effect = lambda key: {
        "value:private_index": json.dumps([{"name": "memory:archive-owner", "tags": []}]),
        "memory:archive-owner": json.dumps(
            [{"role": "user", "content": "private fact", "actor": "member_a"}]
        ),
    }.get(key)
    monkeypatch.setattr(runtime_services, "_private_memory_client", lambda owner: client)

    selected = runtime_services._read_archive_batch("member_a")

    assert selected is not None
    _client, archive_ids, entries = selected
    assert archive_ids == ["archive-owner"]
    assert [entry["content"] for entry in entries] == ["private fact"]
    assert client.get.call_args_list[0].args == ("value:private_index",)


# --- DreamMemorySetTool: automatic-operation boundary ----------------------

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
    from familia.tools import dream_memory as dream_memory_mod

    assert {
        "_resolve_full_key",
        "_merge_memory_document",
        "httpx",
        "CONSOLIDATOR_KEY_ENV",
    }.isdisjoint(vars(dream_memory_mod))


# --- DreamMemorySetTool: policy gate --------------------------------------

@pytest.fixture(scope="module")
def policy_engine():
    # Reuse the real policy.yaml so the dream_consolidator rule is tested
    # against the actual deployed rules, not a local fiction.
    return load_engine(REPO_ROOT / "policy.yaml")


@pytest.fixture
def allow_dream_memory_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate successful writer tests from the process-wide default policy."""
    from familia.tools import memory as memory_mod

    class _AllowDreamMemoryWrite:
        def evaluate(self, context: PolicyContext) -> SimpleNamespace:
            assert context.action == "memory.write"
            assert context.actor == "dream_consolidator"
            return SimpleNamespace(decision=Decision.ALLOW, reason="")

    monkeypatch.setattr(
        memory_mod,
        "get_engine",
        lambda: _AllowDreamMemoryWrite(),
    )


@pytest.fixture
def known_member(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    registry = PrincipalRegistry(
        [
            Principal(
                id="member_a",
                display_name="Member A",
                identities=[Identity(channel="test", sender_id="member-a")],
                memx_key="member-a",
                roles=[],
            ),
            Principal(
                id="member_b",
                display_name="Member B",
                identities=[Identity(channel="test", sender_id="member-b")],
                memx_key="member-b",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)
    return registry


def test_dream_consolidator_allowed_for_private_member_a(policy_engine) -> None:
    r = policy_engine.evaluate(
        PolicyContext(
            action="memory.write", actor="dream_consolidator",
            to_chat="private:member_a:secret",
        )
    )
    assert r.decision is Decision.ALLOW
    assert r.rule and "dream_consolidator" in r.rule.name


def test_dream_consolidator_denied_for_memory_read(policy_engine) -> None:
    r = policy_engine.evaluate(
        PolicyContext(
            action="memory.read", actor="dream_consolidator",
            to_chat="private:member_a:secret",
        )
    )
    assert r.decision is Decision.DENY


@pytest.mark.asyncio
async def test_dream_memory_set_tool_delegates_without_own_http_writer(
    allow_dream_memory_write: None,
) -> None:
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: stored")
    server_principal_getter = MagicMock(return_value="member_a")
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=server_principal_getter,
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    result = await tool.execute(
        kind="memory",
        fact_id="fact-17",
        value="worried about deadline",
    )

    assert result == "committed: stored"
    server_principal_getter.assert_called_once_with()
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "memory",
            "fact_id": "fact-17",
            "value": "worried about deadline",
        },
    )


@pytest.mark.asyncio
async def test_dream_memory_set_tool_deletes_exact_private_fact(
    allow_dream_memory_write: None,
) -> None:
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="deleted: removed")
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=MagicMock(return_value="member_a"),
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    result = await tool.execute(
        kind="delete",
        fact_id="employment.current",
    )

    assert result == "deleted: removed"
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "delete",
            "fact_id": "employment.current",
        },
    )


@pytest.mark.asyncio
async def test_dream_batch_context_fixes_private_owner(
    known_member: PrincipalRegistry,
    allow_dream_memory_write: None,
) -> None:
    from familia.bootstrap import (
        make_dream_batch_context,
        make_dream_server_context_resolver,
        make_dream_turn_context,
    )
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value="committed: stored")
    owner = make_dream_server_context_resolver()
    tool = DreamMemorySetTool(
        ingestor=ingestor,
        server_principal_getter=owner,
    )
    set_current_actor(CONSOLIDATOR_ACTOR)

    assert owner() is None
    with make_dream_turn_context()(), make_dream_batch_context()("member_a"):
        result = await tool.execute(
            kind="memory",
            fact_id="employment.current",
            value="works at Example",
        )

    assert result == "committed: stored"
    assert owner() is None
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={
            "kind": "memory",
            "fact_id": "employment.current",
            "value": "works at Example",
        },
    )


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
    make_dream_tool_installers(server_principal_getter=lambda: "member_a")[0](registry, None)

    assert registry.unregistered == ["read_file", "edit_file", "write_file"]
    assert [getattr(tool, "name") for tool in registry.tools] == ["dream_memory_set"]


def test_bootstrap_wires_current_context_and_dream_adapters(tmp_path: Path) -> None:
    from familia import bootstrap
    from familia.nanobot_extension.context import FamiliaContextBuilder

    builder = bootstrap._context_builder_factory(tmp_path, None, None)
    assert isinstance(builder, FamiliaContextBuilder)
    assert callable(bootstrap._runtime_context_provider)
    assert callable(bootstrap.make_dream_turn_context())
    assert callable(bootstrap.make_dream_batch_context())
    assert callable(bootstrap.make_dream_server_context_resolver())
    restore_policy = bootstrap.make_dream_restore_policy()
    assert callable(restore_policy)
    assert isinstance(restore_policy(["SOUL.md"]), str)
    assert restore_policy(["USER.md", "memory/MEMORY.md"]) is None


def test_bootstrap_wires_private_session_owner_resolver(tmp_path: Path) -> None:
    from familia.bootstrap import make_private_session_owner_resolver
    from familia.private_session_owner import PrivateSessionOwnerResolver

    assert isinstance(make_private_session_owner_resolver(), PrivateSessionOwnerResolver)
