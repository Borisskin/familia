from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
import nanobot.agent.memory as memory_module
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.session.manager import Session


def _make_loop(tmp_path, *, estimated_tokens: int, context_window_tokens: int) -> AgentLoop:
    from nanobot.providers.base import GenerationSettings
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    provider.estimate_prompt_tokens.return_value = (estimated_tokens, "test-counter")
    _response = LLMResponse(content="ok", tool_calls=[])
    provider.chat_with_retry = AsyncMock(return_value=_response)
    provider.chat_stream_with_retry = AsyncMock(return_value=_response)

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        context_window_tokens=context_window_tokens,
    )
    loop.tools.get_definitions = MagicMock(return_value=[])
    loop.consolidator._SAFETY_BUFFER = 0
    return loop


def test_agent_loop_without_archive_callbacks_keeps_standalone_archive_path(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=0)

    assert loop.consolidator.archive_sink_enabled is False
    assert loop.consolidator._archive_sink is None
    assert loop.consolidator._private_session_owner_resolver is None


@pytest.mark.asyncio
async def test_agent_loop_forwards_archive_callbacks_to_consolidator(tmp_path) -> None:
    from nanobot.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    archive_sink = AsyncMock()
    private_session_owner_resolver = AsyncMock()

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        archive_sink=archive_sink,
        private_session_owner_resolver=private_session_owner_resolver,
    )

    assert loop.consolidator._archive_sink is archive_sink
    assert loop.consolidator._private_session_owner_resolver is private_session_owner_resolver


@pytest.mark.asyncio
async def test_agent_loop_uses_dream_when_only_owner_resolver_is_configured(
    tmp_path,
) -> None:
    from nanobot.providers.base import GenerationSettings

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.generation = GenerationSettings(max_tokens=0)
    owner_resolver = AsyncMock(return_value="principal_alpha")

    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        private_session_owner_resolver=owner_resolver,
    )

    assert loop.consolidator.archive_sink_enabled is True
    assert loop.consolidator._archive_sink == loop.dream.archive_private
    assert loop.consolidator._private_session_owner_resolver is owner_resolver


@pytest.mark.asyncio
async def test_prompt_below_threshold_does_not_consolidate(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    loop.consolidator.archive.assert_not_awaited()


@pytest.mark.asyncio
async def test_prompt_above_threshold_triggers_consolidation(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]
    session = loop.sessions.get_or_create("cli:test")
    for role, content, timestamp in (
        ("user", "u1", "2026-01-01T00:00:00"),
        ("assistant", "a1", "2026-01-01T00:00:01"),
        ("user", "u2", "2026-01-01T00:00:02"),
    ):
        session.add_message(role, content, timestamp=timestamp)
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _message: 500)

    await loop.process_direct("hello", session_key="cli:test")

    assert loop.consolidator.archive.await_count >= 1


@pytest.mark.asyncio
async def test_prompt_above_threshold_archives_until_next_user_boundary(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=1000, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    for role, content, timestamp in (
        ("user", "u1", "2026-01-01T00:00:00"),
        ("assistant", "a1", "2026-01-01T00:00:01"),
        ("user", "u2", "2026-01-01T00:00:02"),
        ("assistant", "a2", "2026-01-01T00:00:03"),
        ("user", "u3", "2026-01-01T00:00:04"),
    ):
        session.add_message(role, content, timestamp=timestamp)
    loop.sessions.save(session)

    token_map = {"u1": 120, "a1": 120, "u2": 120, "a2": 120, "u3": 120}
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda message: token_map[message["content"]])

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    archived_chunk = loop.consolidator.archive.await_args.args[0]
    assert [message["content"] for message in archived_chunk] == ["u1", "a1", "u2", "a2"]
    assert session.last_consolidated == 4


@pytest.mark.asyncio
async def test_token_limit_archive_passes_session_key(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    session = loop.sessions.get_or_create("telegram:1001")
    session.add_message(
        "user",
        "archive user",
        channel="telegram",
        chat_id="1001",
    )
    session.add_message("assistant", "archive assistant")
    session.add_message(
        "user",
        "keep user",
        channel="telegram",
        chat_id="1001",
    )
    session.add_message("assistant", "keep assistant")
    expected_chunk = [dict(message) for message in session.messages[:2]]
    loop.consolidator.estimate_session_prompt_tokens = MagicMock(
        side_effect=[(1000, "test-counter"), (0, "test-counter")]
    )
    loop.consolidator.pick_consolidation_boundary = MagicMock(return_value=(2, 500))
    loop.consolidator.archive = AsyncMock(return_value="summary")  # type: ignore[method-assign]

    try:
        await loop.consolidator.maybe_consolidate_by_tokens(session)

        archive_call = loop.consolidator.archive.await_args
        assert archive_call.args[0] == expected_chunk
        assert archive_call.kwargs == {"session_key": "telegram:1001"}
    finally:
        await loop.close_mcp()


@pytest.mark.asyncio
async def test_token_consolidation_selects_current_session_under_consolidator_lock(
    tmp_path,
) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    key = "cli:test"
    stale = Session(key=key)
    stale.add_message("user", "stale user")
    stale.add_message("assistant", "stale assistant")
    current = Session(key=key)
    current.add_message("user", "current user")
    current.add_message("assistant", "current assistant")
    lock = loop.consolidator.get_lock(key)
    selection_lock_states: list[bool] = []

    def get_current(session_key):
        assert session_key == key
        selection_lock_states.append(lock.locked())
        return current

    archived_chunks: list[list[dict]] = []

    async def archive(messages, *, session_key):
        assert session_key == key
        archived_chunks.append([dict(message) for message in messages])

    loop.sessions.get_or_create = MagicMock(side_effect=get_current)  # type: ignore[method-assign]
    loop.consolidator.estimate_session_prompt_tokens = MagicMock(
        side_effect=[(1000, "test-counter"), (0, "test-counter")]
    )
    loop.consolidator.pick_consolidation_boundary = MagicMock(return_value=(2, 500))
    loop.consolidator.archive = archive  # type: ignore[method-assign]

    await loop.consolidator.maybe_consolidate_by_tokens(stale)

    assert selection_lock_states == [True]
    assert [message["content"] for message in archived_chunks[0]] == [
        "current user",
        "current assistant",
    ]


@pytest.mark.asyncio
async def test_token_consolidation_save_failure_keeps_session_state(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "keep user")
    session.add_message("assistant", "keep assistant")
    expected_messages = [dict(message) for message in session.messages]
    expected_last_consolidated = session.last_consolidated
    expected_summary = ("previous summary", session.updated_at)
    loop.auto_compact._summaries[session.key] = expected_summary
    loop.consolidator.estimate_session_prompt_tokens = MagicMock(
        side_effect=[(1000, "test-counter"), (0, "test-counter")]
    )
    loop.consolidator.pick_consolidation_boundary = MagicMock(return_value=(2, 500))
    loop.consolidator.archive = AsyncMock(return_value="new summary")  # type: ignore[method-assign]
    loop.sessions.save = MagicMock(side_effect=RuntimeError("save failed"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="save failed"):
        await loop.consolidator.maybe_consolidate_by_tokens(session)

    assert session.messages == expected_messages
    assert session.last_consolidated == expected_last_consolidated
    assert loop.auto_compact._summaries[session.key] == expected_summary


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exit_case",
    ["disabled", "empty", "zero_estimate", "below_budget", "consolidated"],
)
async def test_token_consolidation_returns_current_session_on_normal_exit(
    tmp_path,
    exit_case,
) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    key = "cli:test"
    stale = Session(key=key)
    stale.add_message("user", "stale user")
    current = Session(key=key)
    lock = loop.consolidator.get_lock(key)
    selection_lock_states: list[bool] = []

    def get_current(session_key):
        assert session_key == key
        selection_lock_states.append(lock.locked())
        return current

    loop.sessions.get_or_create = MagicMock(side_effect=get_current)  # type: ignore[method-assign]

    if exit_case != "empty":
        current.add_message("user", "current user")
        current.add_message("assistant", "current assistant")
    if exit_case == "disabled":
        loop.consolidator.context_window_tokens = 0
    elif exit_case == "zero_estimate":
        loop.consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(0, "test-counter")
        )
    elif exit_case == "below_budget":
        loop.consolidator.estimate_session_prompt_tokens = MagicMock(
            return_value=(100, "test-counter")
        )
    elif exit_case == "consolidated":
        loop.consolidator.estimate_session_prompt_tokens = MagicMock(
            side_effect=[(1000, "test-counter"), (0, "test-counter")]
        )
        loop.consolidator.pick_consolidation_boundary = MagicMock(
            return_value=(2, 500)
        )
        loop.consolidator.archive = AsyncMock(return_value="summary")  # type: ignore[method-assign]

    result = await loop.consolidator.maybe_consolidate_by_tokens(stale)

    assert selection_lock_states == [True]
    assert result is current


@pytest.mark.asyncio
async def test_normal_message_uses_session_returned_by_preflight_consolidation(
    tmp_path,
) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    key = "cli:test"
    stale = loop.sessions.get_or_create(key)
    current = Session(key=key)
    loop.auto_compact.prepare_session = MagicMock(return_value=(stale, None))  # type: ignore[method-assign]
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(  # type: ignore[method-assign]
        return_value=current
    )
    seen_sessions = []

    async def stop_after_session_check(*args, session, **kwargs):
        seen_sessions.append(session)
        raise RuntimeError("stop after session check")

    loop._run_agent_loop = stop_after_session_check  # type: ignore[method-assign]
    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="test",
        content="hello",
    )

    with pytest.raises(RuntimeError, match="stop after session check"):
        await loop._process_message(msg, session_key=key)

    assert seen_sessions == [current]


@pytest.mark.asyncio
async def test_system_message_uses_session_returned_by_preflight_consolidation(
    tmp_path,
) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    key = "cli:test"
    stale = loop.sessions.get_or_create(key)
    current = Session(key=key)
    loop.auto_compact.prepare_session = MagicMock(return_value=(stale, None))  # type: ignore[method-assign]
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(  # type: ignore[method-assign]
        return_value=current
    )
    seen_sessions = []

    async def stop_after_session_check(*args, session, **kwargs):
        seen_sessions.append(session)
        raise RuntimeError("stop after session check")

    loop._run_agent_loop = stop_after_session_check  # type: ignore[method-assign]
    msg = InboundMessage(
        channel="system",
        sender_id="scheduler",
        chat_id=key,
        content="background result",
    )

    with pytest.raises(RuntimeError, match="stop after session check"):
        await loop._process_message(msg)

    assert seen_sessions == [current]


@pytest.mark.asyncio
async def test_consolidation_loops_until_target_met(tmp_path, monkeypatch) -> None:
    """Verify maybe_consolidate_by_tokens keeps looping until under threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    for role, content, timestamp in (
        ("user", "u1", "2026-01-01T00:00:00"),
        ("assistant", "a1", "2026-01-01T00:00:01"),
        ("user", "u2", "2026-01-01T00:00:02"),
        ("assistant", "a2", "2026-01-01T00:00:03"),
        ("user", "u3", "2026-01-01T00:00:04"),
        ("assistant", "a3", "2026-01-01T00:00:05"),
        ("user", "u4", "2026-01-01T00:00:06"),
    ):
        session.add_message(role, content, timestamp=timestamp)
    loop.sessions.save(session)

    call_count = [0]
    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (300, "test")
        return (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    assert loop.consolidator.archive.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_consolidation_continues_below_trigger_until_half_target(tmp_path, monkeypatch) -> None:
    """Once triggered, consolidation should continue until it drops below half threshold."""
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value=True)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    for role, content, timestamp in (
        ("user", "u1", "2026-01-01T00:00:00"),
        ("assistant", "a1", "2026-01-01T00:00:01"),
        ("user", "u2", "2026-01-01T00:00:02"),
        ("assistant", "a2", "2026-01-01T00:00:03"),
        ("user", "u3", "2026-01-01T00:00:04"),
        ("assistant", "a3", "2026-01-01T00:00:05"),
        ("user", "u4", "2026-01-01T00:00:06"),
    ):
        session.add_message(role, content, timestamp=timestamp)
    loop.sessions.save(session)

    call_count = [0]

    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        if call_count[0] == 2:
            return (150, "test")
        return (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 100)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    assert loop.consolidator.archive.await_count == 2
    assert session.last_consolidated == 6


@pytest.mark.asyncio
async def test_consolidation_persists_summary_for_next_prepare_session(tmp_path, monkeypatch) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)
    loop.consolidator.archive = AsyncMock(return_value="User discussed project status.")  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    for role, content, timestamp in (
        ("user", "u1", "2026-01-01T00:00:00"),
        ("assistant", "a1", "2026-01-01T00:00:01"),
        ("user", "u2", "2026-01-01T00:00:02"),
    ):
        session.add_message(role, content, timestamp=timestamp)
    loop.sessions.save(session)

    call_count = [0]

    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        if call_count[0] == 1:
            return (500, "test")
        return (80, "test")

    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 150)

    await loop.consolidator.maybe_consolidate_by_tokens(session)

    reloaded = loop.sessions.get_or_create("cli:test")
    meta = reloaded.metadata.get("_last_summary")
    assert meta is not None
    assert meta["text"] == "User discussed project status."

    reloaded, pending = loop.auto_compact.prepare_session(reloaded, "cli:test")
    assert pending is not None
    assert "User discussed project status." in pending
    assert "_last_summary" not in reloaded.metadata


@pytest.mark.asyncio
async def test_preflight_consolidation_receives_pending_summary(tmp_path) -> None:
    loop = _make_loop(tmp_path, estimated_tokens=100, context_window_tokens=200)
    session = loop.sessions.get_or_create("cli:test")
    loop.auto_compact.prepare_session = MagicMock(
        return_value=(session, "Previous conversation summary: earlier context")
    )  # type: ignore[method-assign]
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(return_value=session)  # type: ignore[method-assign]
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    loop.consolidator.maybe_consolidate_by_tokens.assert_awaited_once_with(
        session,
        session_summary="Previous conversation summary: earlier context",
    )


@pytest.mark.asyncio
async def test_preflight_consolidation_before_llm_call(tmp_path, monkeypatch) -> None:
    """Verify preflight consolidation runs before the LLM call in process_direct."""
    order: list[str] = []

    loop = _make_loop(tmp_path, estimated_tokens=0, context_window_tokens=200)

    async def track_consolidate(messages, *, session_key=None):
        order.append("consolidate")
        return True
    loop.consolidator.archive = track_consolidate  # type: ignore[method-assign]

    async def track_llm(*args, **kwargs):
        order.append("llm")
        return LLMResponse(content="ok", tool_calls=[])
    loop.provider.chat_with_retry = track_llm
    loop.provider.chat_stream_with_retry = track_llm
    loop._schedule_background = lambda coro: coro.close()  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    for role, content, timestamp in (
        ("user", "u1", "2026-01-01T00:00:00"),
        ("assistant", "a1", "2026-01-01T00:00:01"),
        ("user", "u2", "2026-01-01T00:00:02"),
    ):
        session.add_message(role, content, timestamp=timestamp)
    loop.sessions.save(session)
    monkeypatch.setattr(memory_module, "estimate_message_tokens", lambda _m: 500)

    call_count = [0]
    def mock_estimate(_session, *, session_summary=None):
        call_count[0] += 1
        return (1000 if call_count[0] <= 1 else 80, "test")
    loop.consolidator.estimate_session_prompt_tokens = mock_estimate  # type: ignore[method-assign]

    await loop.process_direct("hello", session_key="cli:test")

    assert "consolidate" in order
    assert "llm" in order
    assert order.index("consolidate") < order.index("llm")
