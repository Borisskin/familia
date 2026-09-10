import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.context import ContextBuilder
from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Consolidator
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.session.manager import Session


def _mk_loop() -> AgentLoop:
    loop = AgentLoop.__new__(AgentLoop)
    from nanobot.config.schema import AgentDefaults

    loop.max_tool_result_chars = AgentDefaults().max_tool_result_chars
    return loop


def _make_full_loop(tmp_path: Path) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(bus=MessageBus(), provider=provider, workspace=tmp_path, model="test-model")


async def _return_session(session: Session, **_kwargs) -> Session:
    return session


def _replace_consolidator_with_archive_sink(
    loop: AgentLoop,
    *,
    archive_sink: AsyncMock,
    owner_resolver: AsyncMock,
) -> Consolidator:
    consolidator = Consolidator(
        store=loop.context.memory,
        provider=loop.provider,
        model=loop.model,
        sessions=loop.sessions,
        context_window_tokens=loop.context_window_tokens,
        build_messages=loop.context.build_messages,
        get_tool_definitions=loop.tools.get_definitions,
        max_completion_tokens=loop.consolidator.max_completion_tokens,
        archive_sink=archive_sink,
        private_session_owner_resolver=owner_resolver,
    )
    loop.consolidator = consolidator
    loop.auto_compact.consolidator = consolidator
    return consolidator


def test_save_turn_skips_multimodal_user_when_only_runtime_context() -> None:
    loop = _mk_loop()
    session = Session(key="test:runtime-only")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{"role": "user", "content": [{"type": "text", "text": runtime}]}],
        skip=0,
    )
    assert session.messages == []


def test_save_turn_keeps_image_placeholder_with_path_after_runtime_strip() -> None:
    loop = _mk_loop()
    session = Session(key="test:image")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}, "_meta": {"path": "/media/feishu/photo.jpg"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image: /media/feishu/photo.jpg]"}]


def test_save_turn_keeps_image_placeholder_without_meta() -> None:
    loop = _mk_loop()
    session = Session(key="test:image-no-meta")
    runtime = ContextBuilder._RUNTIME_CONTEXT_TAG + "\nCurrent Time: now (UTC)"

    loop._save_turn(
        session,
        [{
            "role": "user",
            "content": [
                {"type": "text", "text": runtime},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        }],
        skip=0,
    )
    assert session.messages[0]["content"] == [{"type": "text", "text": "[image]"}]


def test_save_turn_keeps_tool_results_under_16k() -> None:
    loop = _mk_loop()
    session = Session(key="test:tool-result")
    content = "x" * 12_000

    loop._save_turn(
        session,
        [{"role": "tool", "tool_call_id": "call_1", "name": "read_file", "content": content}],
        skip=0,
    )

    assert session.messages[0]["content"] == content


def test_save_turn_appends_after_existing_message() -> None:
    loop = _mk_loop()
    session = Session(key="test:save-turn")
    session.add_message("user", "question")
    messages = [
        {"role": "user", "content": "question"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "read_file",
            "content": "result",
        },
    ]

    loop._save_turn(session, messages, skip=1)

    assert [message["role"] for message in session.messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert [message["content"] for message in session.messages] == [
        "question",
        "",
        "result",
    ]


def test_restore_runtime_checkpoint_rehydrates_completed_and_pending_tools() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint",
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is None
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"
    assert "interrupted before this tool finished" in session.messages[2]["content"].lower()


def test_restore_runtime_checkpoint_appends_after_existing_message() -> None:
    loop = _mk_loop()
    session = Session(key="test:checkpoint-append")
    session.add_message("user", "question")
    session.metadata[AgentLoop._RUNTIME_CHECKPOINT_KEY] = {
        "assistant_message": {
            "role": "assistant",
            "content": "working",
            "tool_calls": [
                {
                    "id": "call-done",
                    "type": "function",
                    "function": {"name": "read_file", "arguments": "{}"},
                },
                {
                    "id": "call-pending",
                    "type": "function",
                    "function": {"name": "exec", "arguments": "{}"},
                },
            ],
        },
        "completed_tool_results": [
            {
                "role": "tool",
                "tool_call_id": "call-done",
                "name": "read_file",
                "content": "done",
            }
        ],
        "pending_tool_calls": [
            {
                "id": "call-pending",
                "type": "function",
                "function": {"name": "exec", "arguments": "{}"},
            }
        ],
    }

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert [message["role"] for message in session.messages] == [
        "user",
        "assistant",
        "tool",
        "tool",
    ]
    assert session.messages[0]["content"] == "question"
    assert session.messages[2]["tool_call_id"] == "call-done"
    assert session.messages[3]["tool_call_id"] == "call-pending"


def test_restore_pending_user_turn_appends_interruption_message() -> None:
    loop = _mk_loop()
    session = Session(key="test:pending-user")
    session.add_message("user", "question")
    session.metadata[AgentLoop._PENDING_USER_TURN_KEY] = True

    restored = loop._restore_pending_user_turn(session)

    assert restored is True
    assert session.messages[0]["role"] == "user"
    assert session.messages[0]["content"] == "question"
    assert session.messages[1]["role"] == "assistant"
    assert "interrupted" in session.messages[1]["content"].lower()


def test_restore_runtime_checkpoint_dedupes_overlapping_tail() -> None:
    loop = _mk_loop()
    session = Session(
        key="test:checkpoint-overlap",
        messages=[
            {
                "role": "assistant",
                "content": "working",
                "tool_calls": [
                    {
                        "id": "call_done",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_done",
                "name": "read_file",
                "content": "ok",
            },
        ],
        metadata={
            AgentLoop._RUNTIME_CHECKPOINT_KEY: {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            }
        },
    )

    restored = loop._restore_runtime_checkpoint(session)

    assert restored is True
    assert session.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is None
    assert len(session.messages) == 3
    assert session.messages[0]["role"] == "assistant"
    assert session.messages[1]["tool_call_id"] == "call_done"
    assert session.messages[2]["tool_call_id"] == "call_pending"


@pytest.mark.asyncio
async def test_process_message_persists_user_message_before_turn_completes(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]

    msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c1", content="persist me", actor="test_actor")
    with pytest.raises(RuntimeError, match="boom"):
        await loop._process_message(msg)

    loop.sessions.invalidate("feishu:c1")
    persisted = loop.sessions.get_or_create("feishu:c1")
    assert [m["role"] for m in persisted.messages] == ["user"]
    assert persisted.messages[0]["content"] == "persist me"
    assert persisted.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is True
    assert persisted.updated_at >= persisted.created_at


@pytest.mark.asyncio
async def test_standalone_early_persist_keeps_cli_user_on_exact_key_legacy_path(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop._current_actor_getter = lambda: "principal_alpha"
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    msg = InboundMessage(
        channel="cli",
        sender_id="user",
        chat_id="test",
        content="standalone legacy",
        actor="principal_alpha",
    )

    try:
        with pytest.raises(RuntimeError, match="boom"):
            await loop._process_message(msg)

        loop.sessions.invalidate("cli:test")
        persisted = loop.sessions.get_or_create("cli:test")
        assert persisted.key == "cli:test"
        assert len(persisted.messages) == 1
        user_message = persisted.messages[0]
        assert user_message["sender_id"] == "user"
        assert user_message["actor"] == "principal_alpha"
        assert "channel" not in user_message
        assert "chat_id" not in user_message
        assert "private_mode_proof" not in user_message
    finally:
        await loop.close_mcp()


@pytest.mark.asyncio
async def test_early_persist_preserves_server_user_provenance_without_model_leak(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop._current_actor_getter = lambda: "principal_alpha"
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
    proof = {
        "private_mode": True,
        "is_group": False,
        "topic_id": None,
    }
    msg = InboundMessage(
        channel="telegram",
        sender_id="1001|alice",
        chat_id="1001",
        content="persist provenance",
        actor="principal_alpha",
        metadata={"private_mode_proof": proof},
    )

    with pytest.raises(RuntimeError, match="boom"):
        await loop._process_message(msg)

    cached = loop.sessions.get_or_create("telegram:1001")
    cached_message = cached.messages[0]
    assert {
        key: cached_message[key]
        for key in (
            "role",
            "content",
            "channel",
            "chat_id",
            "sender_id",
            "private_mode_proof",
            "actor",
        )
    } == {
        "role": "user",
        "content": "persist provenance",
        "channel": "telegram",
        "chat_id": "1001",
        "sender_id": "1001|alice",
        "private_mode_proof": {
            "private_mode": True,
            "is_group": False,
            "topic_id": None,
        },
        "actor": "principal_alpha",
    }

    proof["private_mode"] = False
    proof["topic_id"] = 42
    assert cached_message["private_mode_proof"] == {
        "private_mode": True,
        "is_group": False,
        "topic_id": None,
    }

    loop.sessions.invalidate("telegram:1001")
    reloaded = loop.sessions.get_or_create("telegram:1001")
    reloaded_message = reloaded.messages[0]
    assert {
        key: reloaded_message[key]
        for key in (
            "channel",
            "chat_id",
            "sender_id",
            "private_mode_proof",
            "actor",
        )
    } == {
        "channel": "telegram",
        "chat_id": "1001",
        "sender_id": "1001|alice",
        "private_mode_proof": {
            "private_mode": True,
            "is_group": False,
            "topic_id": None,
        },
        "actor": "principal_alpha",
    }
    assert reloaded.get_history(max_messages=0) == [
        {"role": "user", "content": "persist provenance"}
    ]


@pytest.mark.parametrize("channel", ["telegram", "vk"])
@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({}, id="missing-proof"),
        pytest.param({"private_mode_proof": None}, id="none-proof"),
    ],
)
@pytest.mark.asyncio
async def test_sink_enabled_rejects_missing_or_none_private_mode_proof_before_persist(
    tmp_path: Path,
    channel: str,
    metadata: dict,
) -> None:
    loop = _make_full_loop(tmp_path)
    loop._current_actor_getter = lambda: "principal_alpha"
    archive_sink = AsyncMock()
    owner_resolver = AsyncMock(return_value="principal_alpha")
    _replace_consolidator_with_archive_sink(
        loop,
        archive_sink=archive_sink,
        owner_resolver=owner_resolver,
    )
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(
        return_value=(
            "done",
            None,
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "invalid provenance"},
                {"role": "assistant", "content": "done"},
            ],
            "stop",
            False,
        )
    )  # type: ignore[method-assign]
    key = f"{channel}:1001"
    msg = InboundMessage(
        channel=channel,
        sender_id="1001|alice",
        chat_id="1001",
        content="invalid provenance",
        actor="principal_alpha",
        metadata=metadata,
    )

    try:
        with pytest.raises(ValueError, match="private_mode_proof"):
            await loop._process_message(msg)

        loop._run_agent_loop.assert_not_awaited()
        archive_sink.assert_not_awaited()
        owner_resolver.assert_not_awaited()
        loop.sessions.invalidate(key)
        assert loop.sessions.get_or_create(key).messages == []
    finally:
        await loop.close_mcp()


@pytest.mark.parametrize("channel", ["telegram", "vk"])
@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({}, id="missing-proof"),
        pytest.param({"private_mode_proof": None}, id="none-proof"),
    ],
)
@pytest.mark.parametrize(
    ("content", "media"),
    [
        pytest.param("", [], id="empty"),
        pytest.param(" \t\n", [], id="whitespace"),
        pytest.param("", ["image-only.bin"], id="media-only"),
    ],
)
@pytest.mark.asyncio
async def test_sink_enabled_rejects_proof_bypass_before_model_or_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    channel: str,
    metadata: dict,
    content: str,
    media: list[str],
) -> None:
    loop = _make_full_loop(tmp_path)
    archive_sink = AsyncMock()
    owner_resolver = AsyncMock(return_value="principal_alpha")
    _replace_consolidator_with_archive_sink(
        loop,
        archive_sink=archive_sink,
        owner_resolver=owner_resolver,
    )
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(
        return_value=(
            "done",
            None,
            [
                {"role": "system", "content": "system"},
                {"role": "assistant", "content": "done"},
            ],
            "stop",
            False,
        )
    )  # type: ignore[method-assign]
    extract_documents = MagicMock(return_value=("", []))
    monkeypatch.setattr("nanobot.agent.loop.extract_documents", extract_documents)
    loop.sessions.save = MagicMock()  # type: ignore[method-assign]
    msg = InboundMessage(
        channel=channel,
        sender_id="1001|alice",
        chat_id="1001",
        content=content,
        media=media,
        metadata=metadata,
    )

    try:
        with pytest.raises(ValueError, match="private_mode_proof"):
            await loop._process_message(msg)

        loop._run_agent_loop.assert_not_awaited()
        loop.sessions.save.assert_not_called()
        archive_sink.assert_not_awaited()
        owner_resolver.assert_not_awaited()
        if media:
            extract_documents.assert_not_called()
        else:
            extract_documents.assert_not_called()
    finally:
        await loop.close_mcp()


@pytest.mark.asyncio
async def test_existing_session_rejects_invalid_proof_before_consolidation(
    tmp_path: Path,
) -> None:
    loop = _make_full_loop(tmp_path)
    archive_sink = AsyncMock()
    owner_resolver = AsyncMock(return_value="principal_alpha")
    _replace_consolidator_with_archive_sink(
        loop,
        archive_sink=archive_sink,
        owner_resolver=owner_resolver,
    )
    key = "telegram:1001"
    existing = loop.sessions.get_or_create(key)
    existing.add_message("assistant", "existing turn")
    loop.sessions.save(existing)
    original_save = loop.sessions.save
    loop.sessions.save = MagicMock(wraps=original_save)  # type: ignore[method-assign]
    maybe_consolidate = AsyncMock(side_effect=_return_session)
    loop.consolidator.maybe_consolidate_by_tokens = maybe_consolidate  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock()  # type: ignore[method-assign]
    msg = InboundMessage(
        channel="telegram",
        sender_id="1001|alice",
        chat_id="1001",
        content="invalid proof",
        metadata={"private_mode_proof": None},
    )

    try:
        with pytest.raises(ValueError, match="private_mode_proof"):
            await loop._process_message(msg)

        loop._run_agent_loop.assert_not_awaited()
        loop.sessions.save.assert_not_called()
        loop.sessions.invalidate(key)
        persisted = loop.sessions.get_or_create(key)
        assert [message["content"] for message in persisted.messages] == [
            "existing turn"
        ]
        maybe_consolidate.assert_not_awaited()
        archive_sink.assert_not_awaited()
        owner_resolver.assert_not_awaited()
    finally:
        await loop.close_mcp()


@pytest.mark.asyncio
async def test_process_message_does_not_duplicate_early_persisted_user_message(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop._run_agent_loop = AsyncMock(return_value=(
        "done",
        None,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "done"},
        ],
        "stop",
        False,
    ))  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c2", content="hello", actor="test_actor")
    )

    assert result is not None
    assert result.content == "done"
    session = loop.sessions.get_or_create("feishu:c2")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "done"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata


@pytest.mark.asyncio
async def test_next_turn_after_crash_closes_pending_user_turn_before_new_input(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]
    loop.provider.chat_with_retry = AsyncMock(return_value=MagicMock())  # unused because _run_agent_loop is stubbed

    session = loop.sessions.get_or_create("feishu:c3")
    session.add_message("user", "old question")
    session.metadata[AgentLoop._PENDING_USER_TURN_KEY] = True
    loop.sessions.save(session)

    loop._run_agent_loop = AsyncMock(return_value=(
        "new answer",
        None,
        [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "Error: Task interrupted before a response was generated."},
            {"role": "user", "content": "new question"},
            {"role": "assistant", "content": "new answer"},
        ],
        "stop",
        False,
    ))  # type: ignore[method-assign]

    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c3", content="new question", actor="test_actor")
    )

    assert result is not None
    assert result.content == "new answer"
    session = loop.sessions.get_or_create("feishu:c3")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "Error: Task interrupted before a response was generated."},
        {"role": "user", "content": "new question"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata


@pytest.mark.asyncio
async def test_stop_preserves_runtime_checkpoint_for_next_turn(tmp_path: Path) -> None:
    # Direct ``loop._cancel_active_tasks`` (formerly behind ``cmd_stop``)
    # so the checkpoint-preservation guarantee is still exercised after
    # the slash-command router was removed.
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]

    checkpoint_saved = asyncio.Event()

    async def interrupted_run_agent_loop(_initial_messages, *, session=None, **_kwargs):
        assert session is not None
        loop._set_runtime_checkpoint(
            session,
            {
                "assistant_message": {
                    "role": "assistant",
                    "content": "working",
                    "tool_calls": [
                        {
                            "id": "call_done",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "call_pending",
                            "type": "function",
                            "function": {"name": "exec", "arguments": "{}"},
                        },
                    ],
                },
                "completed_tool_results": [
                    {
                        "role": "tool",
                        "tool_call_id": "call_done",
                        "name": "read_file",
                        "content": "ok",
                    }
                ],
                "pending_tool_calls": [
                    {
                        "id": "call_pending",
                        "type": "function",
                        "function": {"name": "exec", "arguments": "{}"},
                    }
                ],
            },
        )
        checkpoint_saved.set()
        await asyncio.Event().wait()

    loop._run_agent_loop = interrupted_run_agent_loop  # type: ignore[method-assign]

    first_msg = InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="keep progress", actor="test_actor")
    task = asyncio.create_task(loop._process_message(first_msg))
    loop._active_tasks[first_msg.session_key] = [task]
    await asyncio.wait_for(checkpoint_saved.wait(), timeout=1.0)

    # Cancel the running task directly — equivalent of the old
    # ``cmd_stop`` priority command path.
    cancelled = 0
    for active in loop._active_tasks.get(first_msg.session_key, []):
        if not active.done():
            active.cancel()
            cancelled += 1
    try:
        await task
    except (asyncio.CancelledError, BaseException):
        pass

    assert cancelled == 1
    assert task.done()

    loop.sessions.invalidate("feishu:c4")
    interrupted = loop.sessions.get_or_create("feishu:c4")
    assert interrupted.metadata.get(AgentLoop._PENDING_USER_TURN_KEY) is True
    assert interrupted.metadata.get(AgentLoop._RUNTIME_CHECKPOINT_KEY) is not None

    async def resumed_run_agent_loop(initial_messages, **_kwargs):
        return (
            "next answer",
            None,
            [*initial_messages, {"role": "assistant", "content": "next answer"}],
            "stop",
            False,
        )

    loop._run_agent_loop = resumed_run_agent_loop  # type: ignore[method-assign]
    result = await loop._process_message(
        InboundMessage(channel="feishu", sender_id="u1", chat_id="c4", content="continue here", actor="test_actor")
    )

    assert result is not None
    assert result.content == "next answer"

    session = loop.sessions.get_or_create("feishu:c4")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content", "tool_call_id", "name"}}
        for m in session.messages
    ] == [
        {"role": "user", "content": "keep progress"},
        {"role": "assistant", "content": "working"},
        {"role": "tool", "tool_call_id": "call_done", "name": "read_file", "content": "ok"},
        {
            "role": "tool",
            "tool_call_id": "call_pending",
            "name": "exec",
            "content": "Error: Task interrupted before this tool finished.",
        },
        {"role": "user", "content": "continue here"},
        {"role": "assistant", "content": "next answer"},
    ]
    assert AgentLoop._PENDING_USER_TURN_KEY not in session.metadata
    assert AgentLoop._RUNTIME_CHECKPOINT_KEY not in session.metadata


@pytest.mark.asyncio
async def test_system_subagent_followup_is_persisted_before_prompt_assembly(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]

    session = loop.sessions.get_or_create("cli:test")
    session.add_message("user", "question")
    session.add_message("assistant", "working")
    loop.sessions.save(session)

    seen: dict[str, list[dict]] = {}

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        seen["initial_messages"] = initial_messages
        return (
            "done",
            [],
            [*initial_messages, {"role": "assistant", "content": "done"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    await loop._process_message(
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:test",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        )
    )

    non_system = [m for m in seen["initial_messages"] if m.get("role") != "system"]
    assert [m["content"] for m in non_system[:2]] == ["question", "working"]
    assert non_system[2]["content"].count("subagent result") == 1
    assert "Current Time:" in non_system[2]["content"]

    loop.sessions.invalidate("cli:test")
    persisted = loop.sessions.get_or_create("cli:test")
    assert [
        {k: v for k, v in m.items() if k in {"role", "content", "injected_event", "subagent_task_id"}}
        for m in persisted.messages
    ] == [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "working"},
        {
            "role": "assistant",
            "content": "subagent result",
            "injected_event": "subagent_result",
            "subagent_task_id": "sub-1",
        },
        {"role": "assistant", "content": "done"},
    ]


@pytest.mark.asyncio
async def test_multiple_subagent_followups_all_persist_as_standalone_history(tmp_path: Path) -> None:
    loop = _make_full_loop(tmp_path)
    loop.consolidator.maybe_consolidate_by_tokens = AsyncMock(side_effect=_return_session)  # type: ignore[method-assign]

    async def fake_run_agent_loop(initial_messages, **_kwargs):
        return (
            "ack",
            [],
            [*initial_messages, {"role": "assistant", "content": "ack"}],
            "stop",
            False,
        )

    loop._run_agent_loop = fake_run_agent_loop  # type: ignore[method-assign]

    for idx in range(3):
        await loop._process_message(
            InboundMessage(
                channel="system",
                sender_id="subagent",
                chat_id="cli:multi",
                content=f"subagent result {idx}",
                metadata={"subagent_task_id": f"sub-{idx}"},
            )
        )

    loop.sessions.invalidate("cli:multi")
    persisted = loop.sessions.get_or_create("cli:multi")
    followups = [m for m in persisted.messages if m.get("injected_event") == "subagent_result"]
    assert [m["content"] for m in followups] == [
        "subagent result 0",
        "subagent result 1",
        "subagent result 2",
    ]


def test_prompt_merge_does_not_replace_standalone_subagent_history_entry(tmp_path: Path) -> None:
    loop = _mk_loop()
    session = Session(key="cli:merge")
    session.add_message("assistant", "previous assistant")

    inserted = loop._persist_subagent_followup(
        session,
        InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="cli:merge",
            content="subagent result",
            metadata={"subagent_task_id": "sub-1"},
        ),
    )

    assert inserted is True

    builder = ContextBuilder(tmp_path)
    projected = builder.build_messages(
        history=session.get_history(max_messages=0),
        current_message="",
        current_role="assistant",
        channel="cli",
        chat_id="merge",
    )

    non_system = [m for m in projected if m.get("role") != "system"]
    assert len(non_system) == 2
    assert "subagent result" in non_system[-1]["content"]
    assert session.messages[-1]["content"] == "subagent result"
    assert session.messages[-1]["injected_event"] == "subagent_result"


def test_subagent_followup_dedupes_by_task_id() -> None:
    loop = _mk_loop()
    session = Session(key="cli:dedupe")
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="cli:dedupe",
        content="subagent result",
        metadata={"subagent_task_id": "sub-1"},
    )

    assert loop._persist_subagent_followup(session, msg) is True
    assert loop._persist_subagent_followup(session, msg) is False
    assert len(session.messages) == 1


def test_subagent_followup_skips_empty_content() -> None:
    loop = _mk_loop()
    session = Session(key="cli:empty")
    msg = InboundMessage(
        channel="system",
        sender_id="subagent",
        chat_id="cli:empty",
        content="",
        metadata={"subagent_task_id": "sub-empty"},
    )

    assert loop._persist_subagent_followup(session, msg) is False
    assert session.messages == []
