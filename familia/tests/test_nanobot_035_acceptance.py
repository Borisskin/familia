from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.memory import Consolidator, MemoryStore
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.command.builtin import cmd_new
from nanobot.command.router import CommandContext
from nanobot.session.manager import Session, SessionManager
from nanobot.agent.turn_delivery import TurnDeliveryFactory


def _sessions(tmp_path: Path) -> SessionManager:
    return SessionManager(
        tmp_path / "workspace",
        sessions_root=tmp_path / "sessions",
    )


def _new_context(session: Session, sessions: SessionManager, archive: AsyncMock) -> CommandContext:
    loop = SimpleNamespace(
        sessions=sessions,
        consolidator=SimpleNamespace(archive_session=archive),
        _cancel_active_tasks=AsyncMock(return_value=0),
        runtime_for_session=lambda _session: SimpleNamespace(),
        discard_session_file_state=lambda _key: None,
    )
    message = InboundMessage("vk", "alice", "shared", "/new")
    return CommandContext(
        msg=message,
        session=session,
        key=session.key,
        raw="/new",
        loop=loop,
        runtime=SimpleNamespace(),
    )


@pytest.mark.asyncio
async def test_new_requires_archive_and_save_before_confirming_and_can_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions(tmp_path)
    session = sessions.get_or_create("familia:alice:vk:shared")
    session.add_message("user", "keep this history")
    sessions.save(session)
    events: list[str] = []
    archive_calls = 0

    async def archive_session(*_args, **_kwargs) -> str | None:
        nonlocal archive_calls
        archive_calls += 1
        events.append("archive")
        return "saved summary" if archive_calls > 1 else None

    archive = AsyncMock(side_effect=archive_session)
    save = sessions.save

    def record_save(value: Session) -> None:
        events.append("save")
        save(value)

    monkeypatch.setattr(sessions, "save", record_save)
    context = _new_context(session, sessions, archive)

    refused = await cmd_new(context)
    assert "history was kept" in refused.content
    assert events == ["archive"]
    assert session.messages[0]["content"] == "keep this history"
    assert _sessions(tmp_path).get_or_create(session.key).messages == session.messages

    def fail_when_clearing(value: Session) -> None:
        events.append("save")
        if not value.messages:
            raise OSError("synthetic session write failure")
        save(value)

    monkeypatch.setattr(sessions, "save", fail_when_clearing)
    failed_save = await cmd_new(context)
    assert "history was kept" in failed_save.content
    assert events[-2:] == ["archive", "save"]
    assert session.messages[0]["content"] == "keep this history"
    assert _sessions(tmp_path).get_or_create(session.key).messages == session.messages

    monkeypatch.setattr(sessions, "save", record_save)
    events.clear()
    succeeded = await cmd_new(context)
    assert succeeded.content == "New session started."
    assert events == ["archive", "save"]
    assert archive.await_count == 3
    assert _sessions(tmp_path).get_or_create(session.key).messages == []


@pytest.mark.asyncio
async def test_file_cap_archives_before_pruning_and_keeps_full_file_on_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = _sessions(tmp_path)
    session = sessions.get_or_create("familia:alice:vk:shared")
    for turn in range(1002):
        session.add_message("user", f"question {turn}")
        session.add_message("assistant", f"answer {turn}")
    original_messages = list(session.messages)
    sessions.save(session)

    consolidator = Consolidator(
        MemoryStore(tmp_path / "memory"),
        sessions,
        build_messages=lambda **_kwargs: [],
        get_tool_definitions=lambda: [],
    )
    archive_ends: list[int] = []

    async def archive(_session, *, archive_end: int, runtime) -> str | None:
        del _session, runtime
        archive_ends.append(archive_end)
        return None if len(archive_ends) == 1 else "earlier conversation summary"

    consolidator.archive_session = archive  # type: ignore[method-assign]
    assert not await consolidator.enforce_file_cap(session.key, runtime=SimpleNamespace())
    assert _sessions(tmp_path).get_or_create(session.key).messages == original_messages

    save = sessions.save

    def fail_after_archive(value: Session) -> None:
        if len(value.messages) < len(original_messages):
            raise OSError("synthetic atomic save failure")
        save(value)

    monkeypatch.setattr(sessions, "save", fail_after_archive)
    with pytest.raises(OSError, match="synthetic atomic save failure"):
        await consolidator.enforce_file_cap(session.key, runtime=SimpleNamespace())
    assert _sessions(tmp_path).get_or_create(session.key).messages == original_messages

    monkeypatch.setattr(sessions, "save", save)
    assert await consolidator.enforce_file_cap(session.key, runtime=SimpleNamespace())
    retained = _sessions(tmp_path).get_or_create(session.key)
    assert len(retained.messages) <= 2000
    assert retained.last_archived == 0
    assert retained.metadata["_last_summary"]["text"] == "earlier conversation summary"
    assert archive_ends == [6, 6, 6]


@pytest.mark.parametrize("marker", ["pending_user_turn", "runtime_checkpoint"])
@pytest.mark.asyncio
async def test_file_cap_keeps_history_when_recovery_marker_exists_or_appears(
    tmp_path: Path,
    marker: str,
) -> None:
    sessions = _sessions(tmp_path)
    session = sessions.get_or_create("familia:alice:vk:recovery")
    for turn in range(1001):
        session.add_message("user", f"question {turn}")
        session.add_message("assistant", f"answer {turn}")
    original_messages = list(session.messages)
    session.metadata[marker] = {"phase": "before-archive"}
    sessions.save(session)

    consolidator = Consolidator(
        MemoryStore(tmp_path / "memory"),
        sessions,
        build_messages=lambda **_kwargs: [],
        get_tool_definitions=lambda: [],
    )
    archive = AsyncMock()
    consolidator.archive_session = archive  # type: ignore[method-assign]

    assert not await consolidator.enforce_file_cap(session.key, runtime=SimpleNamespace())
    archive.assert_not_awaited()
    assert _sessions(tmp_path).get_or_create(session.key).messages == original_messages

    session = sessions.get_or_create(session.key)
    session.metadata.pop(marker, None)
    sessions.save(session)

    async def archive_with_marker(current, *, archive_end: int, runtime) -> str:
        del archive_end, runtime
        current.metadata[marker] = {"phase": "during-archive"}
        sessions.save(current)
        return "durable summary"

    consolidator.archive_session = archive_with_marker  # type: ignore[method-assign]
    assert not await consolidator.enforce_file_cap(session.key, runtime=SimpleNamespace())
    retained = _sessions(tmp_path).get_or_create(session.key)
    assert retained.messages == original_messages
    assert retained.metadata[marker]["phase"] == "during-archive"


@pytest.mark.asyncio
async def test_aclose_drains_an_independent_background_task(tmp_path: Path) -> None:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    loop = AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        turn_delivery_factory=TurnDeliveryFactory(bus),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def background_task() -> None:
        started.set()
        await release.wait()
        completed.set()

    loop.schedule_background(background_task())
    await asyncio.wait_for(started.wait(), timeout=2)
    close_task = asyncio.create_task(loop.aclose())
    try:
        await asyncio.sleep(0)
        assert not close_task.done()
    finally:
        release.set()
    await asyncio.wait_for(close_task, timeout=2)
    assert completed.is_set()
