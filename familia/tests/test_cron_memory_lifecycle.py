"""Regression tests for Familia's runtime archive boundary."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("policy_error", "ingest_result", "committed"),
    [
        ("Error: denied", None, False),
        (None, "committed: stored", True),
    ],
)
async def test_archive_policy_gate_preserves_private_memory_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    policy_error: str | None,
    ingest_result: str | None,
    committed: bool,
) -> None:
    from familia import bootstrap
    from familia import principals as principals_mod
    from familia.principals import Identity, Principal, PrincipalRegistry
    from familia.tools import memory as memory_tools

    registry = PrincipalRegistry(
        [
            Principal(
                id="recipient",
                identities=[Identity(channel="telegram", sender_id="recipient")],
                memx_key="recipient-key",
                roles=[],
            )
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)
    policy = MagicMock(return_value=policy_error)
    monkeypatch.setattr(memory_tools, "_check_memory_write_policy", policy)
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value=ingest_result)
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.PrincipalMemoryIngestor",
        MagicMock(return_value=ingestor),
    )

    messages = [{"role": "user", "content": "private", "actor": "recipient"}]
    result = await bootstrap._archive_messages("recipient", messages)

    assert result.committed is committed
    assert result.retryable is False
    assert policy.call_args.kwargs["actor"] == "dream_consolidator"
    assert policy.call_args.kwargs["full_key"].startswith(
        "private:recipient:memory:archive-"
    )
    if policy_error is not None:
        ingestor.ingest.assert_not_awaited()
        return

    ingestor.ingest.assert_awaited_once()
    ingest_kwargs = ingestor.ingest.await_args.kwargs
    assert ingest_kwargs["server_principal"] == "recipient"
    assert ingest_kwargs["server_topic"] is None
    assert ingest_kwargs["operation"]["kind"] == "memory"
    assert ingest_kwargs["operation"]["fact_id"].startswith("archive-")
    assert json.loads(ingest_kwargs["operation"]["value"]) == messages
