"""Focused checks for asynchronous archive-before-trim session commits."""

import pytest

from nanobot.runtime_adapters import ArchiveResult
from nanobot.session.manager import Session, SessionManager


def _messages(count: int) -> list[dict[str, str]]:
    return [{"role": "user", "content": f"m-{idx}"} for idx in range(count)]


@pytest.mark.asyncio
async def test_async_file_cap_commits_archive_before_trim() -> None:
    session = Session(key="familia:owner:vk:chat", messages=_messages(5))
    seen: list[list[dict[str, str]]] = []

    async def archive(chunk: list[dict[str, str]]) -> ArchiveResult:
        seen.append(chunk)
        return ArchiveResult(committed=True)

    assert await session.enforce_file_cap_async(on_archive=archive, limit=2)
    assert [message["content"] for message in seen[0]] == ["m-0", "m-1", "m-2"]
    assert [message["content"] for message in session.messages] == ["m-3", "m-4"]


@pytest.mark.asyncio
async def test_async_file_cap_refusal_restores_messages_and_cursor() -> None:
    session = Session(
        key="familia:owner:vk:chat",
        messages=_messages(5),
        last_consolidated=1,
    )
    before = list(session.messages)

    async def refuse(_chunk: list[dict[str, str]]) -> ArchiveResult:
        return ArchiveResult(committed=False)

    assert not await session.enforce_file_cap_async(on_archive=refuse, limit=2)
    assert session.messages == before
    assert session.last_consolidated == 1


@pytest.mark.asyncio
async def test_save_async_archives_once_before_persisting_file_cap(tmp_path) -> None:
    manager = SessionManager(tmp_path)
    session = manager.get_or_create("familia:owner:vk:chat")
    session.messages = _messages(2001)
    calls: list[tuple[str, int]] = []

    async def archive(chunk: list[dict[str, str]], *, session_key: str) -> ArchiveResult:
        calls.append((session_key, len(chunk)))
        return ArchiveResult(committed=True)

    manager.set_file_cap_archiver(archive)
    await manager.save_async(session)

    assert calls == [(session.key, 1)]
    persisted = manager._load(session.key)
    assert persisted is not None
    assert len(persisted.messages) == 2000
