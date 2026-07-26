"""Tests for the lightweight Consolidator — append-only to HISTORY.md."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import nanobot.agent.memory as memory_module
from nanobot.agent.memory import Consolidator, MemoryStore
from nanobot.session.manager import Session


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path)


@pytest.fixture
def mock_provider():
    p = MagicMock()
    p.chat_with_retry = AsyncMock()
    return p


def _make_consolidator(store, mock_provider, **kwargs):
    sessions = MagicMock()
    sessions.save = MagicMock()
    return Consolidator(
        store=store,
        provider=mock_provider,
        model="test-model",
        sessions=sessions,
        context_window_tokens=1000,
        build_messages=MagicMock(return_value=[]),
        get_tool_definitions=MagicMock(return_value=[]),
        max_completion_tokens=100,
        **kwargs,
    )


def test_removed_archive_protocol_symbols_are_absent() -> None:
    for name in (
        "ArchiveSource",
        "ArchivePart",
        "ArchiveManifest",
        "build_archive_range_id",
        "build_archive_source_id",
        "build_session_archive_source",
        "_freeze_private_mode_proof",
    ):
        assert not hasattr(memory_module, name), name


@pytest.fixture
def consolidator(store, mock_provider):
    return _make_consolidator(store, mock_provider)


class TestConsolidatorSummarize:
    async def test_summarize_appends_to_history(self, consolidator, mock_provider, store):
        """Consolidator should call LLM to summarize, then append to HISTORY.md."""
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="User fixed a bug in the auth module."
        )
        messages = [
            {"role": "user", "content": "fix the auth bug"},
            {"role": "assistant", "content": "Done, fixed the race condition."},
        ]
        result = await consolidator.archive(messages)
        assert result == "User fixed a bug in the auth module."
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1

    async def test_summarize_raw_dumps_on_llm_failure(self, consolidator, mock_provider, store):
        """On LLM failure, raw-dump messages to HISTORY.md."""
        mock_provider.chat_with_retry.side_effect = Exception("API error")
        messages = [{"role": "user", "content": "hello"}]
        result = await consolidator.archive(messages)
        assert result is None  # no summary on raw dump fallback
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["content"].startswith("[RAW idempotency=")

    async def test_summarize_skips_empty_messages(self, consolidator):
        result = await consolidator.archive([])
        assert result is None


class TestConsolidatorArchiveErrorHandling:
    """archive() must fall back to raw_archive when the LLM returns an error
    response (finish_reason == 'error'), e.g. overloaded / quota exceeded.
    See https://github.com/HKUDS/nanobot/issues/3244
    """

    async def test_archive_falls_back_on_error_finish_reason(self, consolidator, mock_provider, store):
        """LLM returning finish_reason='error' should trigger raw_archive, not write error text."""
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="Error: {'type': 'error', 'error': {'type': 'overloaded_error', 'message': 'overloaded_error (529)'}}",
            finish_reason="error",
        )
        messages = [
            {"role": "user", "content": "fix the auth bug"},
            {"role": "assistant", "content": "Done, fixed the race condition."},
        ]
        result = await consolidator.archive(messages)
        assert result is None
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert entries[0]["content"].startswith("[RAW idempotency=")
        assert "Error:" not in entries[0]["content"]

    async def test_archive_preserves_summary_on_success(self, consolidator, mock_provider, store):
        """Normal LLM response should still produce a proper summary entry."""
        mock_provider.chat_with_retry.return_value = MagicMock(
            content="User fixed a bug in the auth module.",
            finish_reason="stop",
        )
        messages = [
            {"role": "user", "content": "fix the auth bug"},
            {"role": "assistant", "content": "Done."},
        ]
        result = await consolidator.archive(messages)
        assert result == "User fixed a bug in the auth module."
        entries = store.read_unprocessed_history(since_cursor=0)
        assert len(entries) == 1
        assert not entries[0]["content"].startswith("[RAW")


class TestConsolidatorTokenBudget:
    async def test_prompt_below_threshold_does_not_consolidate(self, consolidator):
        """No consolidation when tokens are within budget."""
        session = MagicMock()
        session.last_consolidated = 0
        session.messages = [{"role": "user", "content": "hi"}]
        session.key = "test:key"
        consolidator.estimate_session_prompt_tokens = MagicMock(return_value=(100, "tiktoken"))
        consolidator.archive = AsyncMock(return_value=True)
        consolidator.sessions.get_or_create.return_value = session
        await consolidator.maybe_consolidate_by_tokens(session)
        consolidator.archive.assert_not_called()

    async def test_chunk_cap_preserves_user_turn_boundary(self, consolidator):
        """Chunk cap should rewind to the last user boundary within the cap."""
        consolidator._SAFETY_BUFFER = 0
        session = Session(key="test:key")
        for i in range(70):
            session.add_message(
                "user" if i in {0, 50, 61} else "assistant",
                f"m{i}",
            )
        consolidator.estimate_session_prompt_tokens = MagicMock(
            side_effect=[(1200, "tiktoken"), (400, "tiktoken")]
        )
        consolidator.pick_consolidation_boundary = MagicMock(return_value=(61, 999))
        consolidator.archive = AsyncMock(return_value=True)
        consolidator.sessions.get_or_create.return_value = session

        await consolidator.maybe_consolidate_by_tokens(session)

        archived_chunk = consolidator.archive.await_args.args[0]
        assert len(archived_chunk) == 50
        assert archived_chunk[0]["content"] == "m0"
        assert archived_chunk[-1]["content"] == "m49"
        assert session.last_consolidated == 50

    async def test_chunk_cap_skips_when_no_user_boundary_within_cap(self, consolidator):
        """If the cap would cut mid-turn, consolidation should skip that round."""
        consolidator._SAFETY_BUFFER = 0
        session = MagicMock()
        session.last_consolidated = 0
        session.key = "test:key"
        session.messages = [
            {
                "role": "user" if i in {0, 61} else "assistant",
                "content": f"m{i}",
            }
            for i in range(70)
        ]
        consolidator.estimate_session_prompt_tokens = MagicMock(return_value=(1200, "tiktoken"))
        consolidator.pick_consolidation_boundary = MagicMock(return_value=(61, 999))
        consolidator.archive = AsyncMock(return_value=True)
        consolidator.sessions.get_or_create.return_value = session

        await consolidator.maybe_consolidate_by_tokens(session)

        consolidator.archive.assert_not_awaited()
        assert session.last_consolidated == 0


@pytest.mark.asyncio
async def test_private_archive_boundary_is_only_session_owner_and_messages(
    store,
    mock_provider,
):
    events: list[str] = []
    messages = [
        {"role": "user", "content": "личный факт"},
        {"role": "assistant", "content": "ответ"},
    ]

    async def resolve_owner(session_key, actual_messages):
        events.append("resolver")
        assert session_key == "telegram:1001"
        assert actual_messages is messages
        return "principal_alpha"

    async def archive_sink(principal, actual_messages):
        events.append("sink")
        assert principal == "principal_alpha"
        assert actual_messages is messages

    consolidator = _make_consolidator(
        store,
        mock_provider,
        archive_sink=archive_sink,
        private_session_owner_resolver=resolve_owner,
    )

    result = await consolidator.archive(
        messages,
        session_key="telegram:1001",
    )

    assert result is None
    assert events == ["resolver", "sink"]


def _overflowing_private_session() -> Session:
    session = Session(key="telegram:1001")
    session.add_message("user", "fact", actor="principal_alpha")
    session.add_message("assistant", "answer")
    session.add_message("user", "next", actor="principal_alpha")
    session.add_message("assistant", "next answer")
    return session


@pytest.mark.asyncio
async def test_sink_return_without_exception_advances_boundary(
    store,
    mock_provider,
):
    session = _overflowing_private_session()
    resolver = AsyncMock(return_value="principal_alpha")
    sink = AsyncMock(return_value=None)
    consolidator = _make_consolidator(
        store,
        mock_provider,
        archive_sink=sink,
        private_session_owner_resolver=resolver,
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = MagicMock(
        side_effect=[(1200, "test"), (400, "test")]
    )
    consolidator.pick_consolidation_boundary = MagicMock(
        return_value=(2, 1000)
    )
    consolidator.sessions.get_or_create.return_value = session

    await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 2
    consolidator.sessions.save.assert_called_once_with(session)
    resolver.assert_awaited_once_with(
        "telegram:1001",
        session.messages[:2],
    )
    sink.assert_awaited_once_with(
        "principal_alpha",
        session.messages[:2],
    )


@pytest.mark.asyncio
async def test_unknown_owner_keeps_boundary_and_messages(
    store,
    mock_provider,
):
    session = _overflowing_private_session()
    original_messages = list(session.messages)
    resolver = AsyncMock(return_value=None)
    sink = AsyncMock(return_value=None)
    consolidator = _make_consolidator(
        store,
        mock_provider,
        archive_sink=sink,
        private_session_owner_resolver=resolver,
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = MagicMock(
        return_value=(1200, "test")
    )
    consolidator.pick_consolidation_boundary = MagicMock(
        return_value=(2, 1000)
    )
    consolidator.sessions.get_or_create.return_value = session

    with pytest.raises(RuntimeError, match="session owner is unavailable"):
        await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 0
    assert session.messages == original_messages
    sink.assert_not_awaited()
    consolidator.sessions.save.assert_not_called()


@pytest.mark.asyncio
async def test_sink_exception_keeps_boundary_and_messages(
    store,
    mock_provider,
):
    session = _overflowing_private_session()
    original_messages = list(session.messages)
    resolver = AsyncMock(return_value="principal_alpha")
    sink = AsyncMock(side_effect=RuntimeError("memX unavailable"))
    consolidator = _make_consolidator(
        store,
        mock_provider,
        archive_sink=sink,
        private_session_owner_resolver=resolver,
    )
    consolidator._SAFETY_BUFFER = 0
    consolidator.estimate_session_prompt_tokens = MagicMock(
        return_value=(1200, "test")
    )
    consolidator.pick_consolidation_boundary = MagicMock(
        return_value=(2, 1000)
    )
    consolidator.sessions.get_or_create.return_value = session

    with pytest.raises(RuntimeError, match="memX unavailable"):
        await consolidator.maybe_consolidate_by_tokens(session)

    assert session.last_consolidated == 0
    assert session.messages == original_messages
    consolidator.sessions.save.assert_not_called()
