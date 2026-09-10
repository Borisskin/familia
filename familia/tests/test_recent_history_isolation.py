"""Familia archive ownership and prompt isolation contracts."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from nanobot.agent.context import ContextBuilder
from nanobot.agent.tools.context import RequestContext

from familia import bootstrap
from familia.nanobot_extension import runtime_services


OWNER_A = "owner_a"
OWNER_B = "owner_b"


class _ArchiveClient:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = values
        self.get_calls: list[str] = []

    def get(self, key: str) -> str | None:
        self.get_calls.append(key)
        return self.values.get(key)


def _request_context(actor: str) -> RequestContext:
    return RequestContext(
        "telegram",
        f"chat-{actor}",
        actor=actor,
        session_key=f"familia:{actor}:telegram:chat-{actor}",
    )


def test_familia_context_messages_use_request_context_owner_scope(tmp_path: Path, monkeypatch) -> None:
    """The real Familia builder filters adapter history by the bound actor."""
    monkeypatch.setattr(
        "familia.nanobot_extension.context.FamiliaContextExtension.build_sections",
        lambda self, **kwargs: [],
    )
    builder = bootstrap._context_builder_factory(tmp_path, None, None)
    history = [
        {"role": "user", "content": "A_PRIVATE", "metadata": {"actor": OWNER_A}},
        {"role": "assistant", "content": "B_PRIVATE", "metadata": {"actor": OWNER_B}},
        {"role": "assistant", "content": "UNKNOWN_PRIVATE", "actor": "ghost"},
    ]

    with bootstrap._turn_scope(_request_context(OWNER_A)):
        messages = builder.build_messages(
            history=history,
            current_message="A_CURRENT",
            channel="telegram",
            chat_id="chat-owner_a",
        )

    contents = "\n".join(str(message.get("content", "")) for message in messages)
    assert "A_PRIVATE" in contents
    assert "A_CURRENT" in contents
    assert "B_PRIVATE" not in contents
    assert "UNKNOWN_PRIVATE" not in contents


def test_familia_archive_selection_is_private_and_quarantines_foreign_batches(monkeypatch) -> None:
    owner_client = _ArchiveClient(
        {
            "value:private_index": json.dumps(
                [
                    {"name": "memory:archive-owner-good", "tags": []},
                    {"name": "memory:archive-owner-mixed", "tags": []},
                    {"name": "memory:archive-owner-unknown", "tags": []},
                ]
            ),
            "memory:archive-owner-good": json.dumps(
                [{"role": "user", "content": "A_PRIVATE", "actor": OWNER_A}]
            ),
            "memory:archive-owner-mixed": json.dumps(
                [
                    {"role": "user", "content": "A_MIXED", "actor": OWNER_A},
                    {"role": "assistant", "content": "B_MIXED", "actor": OWNER_B},
                ]
            ),
            "memory:archive-owner-unknown": json.dumps(
                [{"role": "user", "content": "GHOST", "actor": "ghost"}]
            ),
        }
    )
    member_client = _ArchiveClient(
        {
            "value:private_index": json.dumps(
                [{"name": "memory:archive-member-good", "tags": []}]
            ),
            "memory:archive-member-good": json.dumps(
                [{"role": "user", "content": "B_PRIVATE", "actor": OWNER_B}]
            ),
        }
    )
    clients = {OWNER_A: owner_client, OWNER_B: member_client}
    monkeypatch.setattr(runtime_services, "_private_memory_client", clients.get)

    owner_batch = runtime_services._read_archive_batch(OWNER_A)
    member_batch = runtime_services._read_archive_batch(OWNER_B)

    assert owner_batch is not None and member_batch is not None
    _owner_client, owner_ids, owner_entries = owner_batch
    _member_client, member_ids, member_entries = member_batch
    assert owner_ids == ["archive-owner-good"]
    assert member_ids == ["archive-member-good"]
    assert [entry["content"] for entry in owner_entries] == ["A_PRIVATE"]
    assert [entry["content"] for entry in member_entries] == ["B_PRIVATE"]
    assert "B_PRIVATE" not in {entry["content"] for entry in owner_entries}
    assert "GHOST" not in {entry["content"] for entry in owner_entries}


def test_familia_archive_rejects_foreign_batches_without_commit(monkeypatch) -> None:
    from familia import principals as principals_mod
    from familia.principals import Principal, PrincipalRegistry

    monkeypatch.setattr(
        principals_mod,
        "_registry",
        PrincipalRegistry(
            [
                Principal(id=OWNER_A, memx_key="key-owner-a"),
                Principal(id=OWNER_B, memx_key="key-owner-b"),
            ]
        ),
    )
    ingested: list[object] = []

    class Ingestor:
        def __init__(self, **kwargs: object) -> None:
            pass

        async def ingest(self, **kwargs: object) -> str:
            ingested.append(kwargs)
            return "committed: unexpected"

    monkeypatch.setattr(
        "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
        Ingestor,
    )
    batches = (
        [
            {"role": "user", "content": "A_OK", "actor": OWNER_A},
            {"role": "assistant", "content": "B_FORGED", "actor": OWNER_B},
        ],
        [{"role": "user", "content": "GHOST", "actor": "ghost"}],
    )

    for batch in batches:
        result = asyncio.run(bootstrap._archive_messages(OWNER_A, batch))
        assert result.committed is False
        assert result.retryable is False

    assert ingested == []


def test_missing_actor_fails_closed(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "familia.nanobot_extension.context.FamiliaContextExtension.build_sections",
        lambda self, **kwargs: [],
    )
    builder = bootstrap._context_builder_factory(tmp_path, None, None)
    request = RequestContext("telegram", "chat-owner_a", actor=None, session_key=None)

    with bootstrap._turn_scope(request):
        messages = builder.build_messages(
            history=[
                {"role": "user", "content": "A_PRIVATE", "actor": OWNER_A},
                {"role": "user", "content": "B_PRIVATE", "actor": OWNER_B},
            ],
            current_message="CURRENT",
            channel="telegram",
        )

    contents = "\n".join(str(message.get("content", "")) for message in messages)
    assert "A_PRIVATE" not in contents
    assert "B_PRIVATE" not in contents


def test_explicit_standalone_actorless_compatibility(tmp_path: Path) -> None:
    builder = ContextBuilder(tmp_path)
    builder.memory.append_history("LOCAL_ACTORLESS")

    prompt = builder.build_system_prompt()

    assert "# Recent History" in prompt
    assert "LOCAL_ACTORLESS" in prompt
