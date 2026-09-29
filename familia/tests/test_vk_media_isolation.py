import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.context import RequestContext
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.path_utils import materialize_message_media
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.providers.base import LLMResponse
from nanobot.runtime_adapters import RuntimeAdapters
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)
from nanobot.session.keys import UNIFIED_SESSION_KEY


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class _AsyncClient:
    def __init__(self, payloads: dict[str, bytes], **_kwargs: object) -> None:
        self._payloads = payloads

    async def __aenter__(self) -> "_AsyncClient":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def get(self, url: str, *, follow_redirects: bool) -> _Response:
        assert follow_redirects is True
        await asyncio.sleep(0)
        return _Response(self._payloads[url])


def _make_channel():
    from familia.channels.vk import VKChannel

    return VKChannel(
        SimpleNamespace(
            enabled=True,
            group_id=1,
            access_token="token",
            api_version="5.199",
            allow_from=["*"],
            long_poll_wait=25,
            streaming=False,
            proxy="",
            media_proxy="",
        ),
        MessageBus(),
    )


@pytest.mark.asyncio
async def test_vk_downloads_are_unique_safe_and_materialize_per_actor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from familia.channels import vk as vk_module
    from nanobot.agent.tools import path_utils

    payloads = {"a": b"bytes-a", "b": b"bytes-b"}
    monkeypatch.setattr(
        vk_module.httpx,
        "AsyncClient",
        lambda **kwargs: _AsyncClient(payloads, **kwargs),
    )
    media_root = tmp_path / "media"
    media_dir = media_root / "vk"
    media_dir.mkdir(parents=True)
    channel = _make_channel()

    path_a, path_b, path_repeat = await asyncio.gather(
        channel._download_to("a", media_dir, "report.pdf"),
        channel._download_to("b", media_dir, "report.pdf"),
        channel._download_to("a", media_dir, "report.pdf"),
    )

    assert path_a is not None
    assert path_b is not None
    assert path_repeat is not None
    paths = [Path(path_a), Path(path_b), Path(path_repeat)]
    assert len(paths) == 3
    assert len({path.name for path in paths}) == 3
    assert all(path.parent == media_dir for path in paths)
    assert all(path.suffix == ".pdf" for path in paths)
    assert Path(path_a).read_bytes() == b"bytes-a"
    assert Path(path_b).read_bytes() == b"bytes-b"
    assert Path(path_repeat).read_bytes() == b"bytes-a"

    for name, suffix in (
        ("../escape.pdf", ".pdf"),
        (r"..\escape.ogg", ".ogg"),
        (str(tmp_path / "outside.txt"), ".txt"),
    ):
        path = await channel._download_to("a", media_dir, name)
        assert path is not None
        assert Path(path).parent == media_dir
        assert Path(path).suffix == suffix
    assert not (tmp_path / "outside.txt").exists()

    monkeypatch.setattr(
        path_utils,
        "get_media_dir",
        lambda channel_name=None: (
            media_root if channel_name is None else media_root / channel_name
        ),
    )
    actor_a = tmp_path / "actor-a"
    actor_b = tmp_path / "actor-b"
    foreign_dir = media_root / "telegram"
    foreign_dir.mkdir()
    foreign_path = foreign_dir / "foreign.pdf"
    foreign_path.write_bytes(b"foreign")
    symlink_candidate = media_dir / "linked.pdf"
    symlink_path: Path | None
    try:
        symlink_candidate.symlink_to(path_a)
    except OSError:
        symlink_path = None
    else:
        symlink_path = symlink_candidate
    scope_a = build_workspace_scope(
        actor_a,
        "restricted",
        source_channel="vk",
    )
    scope_b = build_workspace_scope(
        actor_b,
        "restricted",
        source_channel="vk",
    )

    token = bind_workspace_scope(scope_a)
    try:
        copied_a = materialize_message_media([path_a])
        denied_foreign = materialize_message_media([str(foreign_path)])
        denied_symlink = (
            materialize_message_media([str(symlink_path)])
            if symlink_path is not None
            else []
        )
    finally:
        reset_workspace_scope(token)
    token = bind_workspace_scope(scope_b)
    try:
        copied_b = materialize_message_media([path_b])
        denied_private = await ReadFileTool(
            workspace=actor_b,
            allowed_dir=actor_b,
        ).execute(path=copied_a[0])
        denied_shared = await ReadFileTool(
            workspace=actor_b,
            allowed_dir=actor_b,
        ).execute(path=path_a)
    finally:
        reset_workspace_scope(token)

    assert len(copied_a) == len(copied_b) == 1
    assert Path(copied_a[0]).read_bytes() == b"bytes-a"
    assert Path(copied_b[0]).read_bytes() == b"bytes-b"
    assert Path(copied_a[0]).parent != Path(copied_b[0]).parent
    assert getattr(denied_private, "is_error", False)
    assert getattr(denied_shared, "is_error", False)
    assert denied_foreign == []
    if symlink_path is not None:
        assert denied_symlink == []


@pytest.mark.asyncio
async def test_materialize_rejects_preexisting_attachments_symlink(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nanobot.agent.tools import path_utils

    media_root = tmp_path / "media"
    media_dir = media_root / "vk"
    media_dir.mkdir(parents=True)
    source = media_dir / "incoming.pdf"
    source.write_bytes(b"incoming")
    monkeypatch.setattr(
        path_utils,
        "get_media_dir",
        lambda channel_name=None: media_root if channel_name is None else media_root / channel_name,
    )

    owner_root = tmp_path / "owner"
    owner_root.mkdir()
    foreign_root = tmp_path / "foreign"
    foreign_channel = foreign_root / ".attachments" / "vk"
    foreign_channel.mkdir(parents=True)
    foreign_file = foreign_channel / "already.txt"
    foreign_file.write_bytes(b"foreign")
    attachments_link = owner_root / ".attachments"
    attachments_link.symlink_to(foreign_root / ".attachments", target_is_directory=True)

    scope = build_workspace_scope(owner_root, "restricted", source_channel="vk")
    token = bind_workspace_scope(scope)
    try:
        copied = materialize_message_media([str(source)])
        denied_foreign = await ReadFileTool(
            workspace=owner_root,
            allowed_dir=owner_root,
        ).execute(path=str(attachments_link / "vk" / foreign_file.name))
    finally:
        reset_workspace_scope(token)

    assert copied == []
    assert list(foreign_channel.iterdir()) == [foreign_file]
    assert getattr(denied_foreign, "is_error", False)


@pytest.mark.asyncio
async def test_vk_media_unified_session_routes_each_actor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from nanobot.agent.tools import path_utils

    media_root = tmp_path / "media"
    media_dir = media_root / "vk"
    media_dir.mkdir(parents=True)
    owner_source = media_dir / "owner.pdf"
    owner_source.write_bytes(b"owner-bytes")
    member_source = media_dir / "member.pdf"
    member_source.write_bytes(b"member-bytes")
    monkeypatch.setattr(
        path_utils,
        "get_media_dir",
        lambda channel_name=None: media_root if channel_name is None else media_root / channel_name,
    )

    actor_roots = {
        "owner": tmp_path / "owner",
        "member": tmp_path / "member",
    }

    @contextmanager
    def actor_scope(request: RequestContext):
        token = bind_workspace_scope(
            build_workspace_scope(
                actor_roots[request.actor or ""],
                "restricted",
                source_channel=request.channel,
            )
        )
        try:
            yield request
        finally:
            reset_workspace_scope(token)

    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    responses = 0
    first_started = asyncio.Event()
    allow_first = asyncio.Event()
    second_finished = asyncio.Event()
    captured: list[list[dict]] = []

    async def chat_stream_with_retry(
        *, messages: list[dict], **_kwargs: object
    ) -> LLMResponse:
        nonlocal responses
        responses += 1
        captured.append([dict(message) for message in messages])
        if responses == 1:
            first_started.set()
            await allow_first.wait()
        elif responses == 2:
            second_finished.set()
        return LLMResponse(content=f"answer-{responses}", tool_calls=[], usage=None)

    provider.chat_stream_with_retry = AsyncMock(side_effect=chat_stream_with_retry)
    loop = AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        unified_session=True,
        runtime_adapters=RuntimeAdapters(turn_scope=actor_scope),
    )
    loop.tools.get_definitions = MagicMock(return_value=[])

    run_task = asyncio.create_task(loop.run())
    try:
        await loop.bus.publish_inbound(
            InboundMessage(
                channel="vk",
                sender_id="owner",
                chat_id="chat",
                content="owner message",
                media=[str(owner_source)],
                actor="owner",
            )
        )
        await asyncio.wait_for(first_started.wait(), timeout=5)
        await loop.bus.publish_inbound(
            InboundMessage(
                channel="vk",
                sender_id="member",
                chat_id="chat",
                content="member message",
                media=[str(member_source)],
                actor="member",
            )
        )
        for _ in range(1000):
            pending = loop._pending_queues.get(UNIFIED_SESSION_KEY)
            if pending is not None and pending.qsize():
                break
            await asyncio.sleep(0)
        assert pending is not None and pending.qsize() == 1
        allow_first.set()
        await asyncio.wait_for(second_finished.wait(), timeout=5)
    finally:
        loop.stop()
        await asyncio.wait_for(run_task, timeout=5)

    owner_media = actor_roots["owner"] / ".attachments" / "vk"
    member_media = actor_roots["member"] / ".attachments" / "vk"
    assert [p.read_bytes() for p in owner_media.glob("*")] == [b"owner-bytes"]
    assert [p.read_bytes() for p in member_media.glob("*")] == [b"member-bytes"]
    captured_text = [
        message["content"]
        for messages in captured
        for message in messages
        if message.get("role") == "user" and isinstance(message.get("content"), str)
    ]
    assert any(f"[Attachment: {owner_media}" in text for text in captured_text)
    assert any(f"[Attachment: {member_media}" in text for text in captured_text)
