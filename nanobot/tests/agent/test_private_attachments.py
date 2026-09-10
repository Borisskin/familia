from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.filesystem import ReadFileTool
from nanobot.agent.tools.path_utils import materialize_message_media
from nanobot.agent.tools.sandbox import wrap_command
from nanobot.bus.events import InboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ChannelsConfig
from nanobot.providers.base import LLMResponse
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)


PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def _loop(workspace: Path, *, extract_document_text: bool = True) -> AgentLoop:
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    provider.chat_with_retry = AsyncMock(return_value=LLMResponse(content="ok"))
    return AgentLoop(
        bus=MessageBus(),
        provider=provider,
        workspace=workspace,
        model="test-model",
        channels_config=ChannelsConfig(extract_document_text=extract_document_text),
    )


def _media_dirs(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    from nanobot.agent.tools import path_utils

    monkeypatch.setattr(
        path_utils,
        "get_media_dir",
        lambda channel=None: root / channel if channel else root,
    )


@pytest.mark.asyncio
async def test_admitted_media_is_private_for_file_tools_and_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    media_root = tmp_path / "shared-media"
    telegram_root = media_root / "telegram"
    telegram_root.mkdir(parents=True)
    image = telegram_root / "accepted.png"
    image.write_bytes(PNG_BYTES)
    document = telegram_root / "accepted.txt"
    document.write_text("accepted document", encoding="utf-8")

    foreign_root = tmp_path / "actors" / "foreign" / "tool"
    foreign_root.mkdir(parents=True)
    foreign = foreign_root / "secret.txt"
    foreign.write_text("foreign actor", encoding="utf-8")
    link = telegram_root / "link.txt"
    try:
        link.symlink_to(foreign)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    _media_dirs(monkeypatch, media_root)
    actor_root = tmp_path / "actors" / "owner" / "tool"
    scope = build_workspace_scope(
        actor_root,
        "restricted",
        source_channel="telegram",
        allow_shared_extras=False,
        sandbox_mask_root=tmp_path,
    )
    token = bind_workspace_scope(scope)
    try:
        accepted = materialize_message_media(
            [str(image), str(document), str(foreign), str(link)]
        )
        assert len(accepted) == 2
        assert all(Path(path).is_relative_to(scope.project_path) for path in accepted)
        assert not any("foreign" in path or "link" in path for path in accepted)

        loop = _loop(tmp_path / "loop-state")
        content, image_paths = loop._prepare_message_media("inspect", [str(image), str(document)])
        assert "accepted document" in content
        assert len(image_paths) == 1
        assert Path(image_paths[0]).is_relative_to(scope.project_path)

        doc_copy = next(path for path in accepted if path.endswith("accepted.txt"))
        read_tool = ReadFileTool(workspace=scope.project_path, restrict_to_workspace=True)
        assert "accepted document" in await read_tool.execute(path=doc_copy)
        assert "outside allowed directory" in await read_tool.execute(path=str(document))
        assert "outside allowed directory" in await read_tool.execute(path=str(foreign))
        assert "outside allowed directory" in await read_tool.execute(path=str(link))

        session = loop.sessions.get_or_create("telegram:chat")
        message = InboundMessage(
            channel="telegram",
            sender_id="owner",
            chat_id="chat",
            content=content,
            media=image_paths,
        )
        assert loop._persist_user_message_early(message, session)
        assert session.messages[-1]["media"] == image_paths
        assert all(Path(path).is_relative_to(scope.project_path) for path in session.messages[-1]["media"])
    finally:
        reset_workspace_scope(token)


def test_plain_nanobot_keeps_shared_media_paths() -> None:
    media = ["/runtime/media/telegram/accepted.png"]
    assert materialize_message_media(media) == media


@pytest.mark.skipif(
    not os.environ.get("FAMILIA_RUN_BWRAP_CANARY") or shutil.which("bwrap") is None,
    reason="set FAMILIA_RUN_BWRAP_CANARY=1 in the cap+unconf Docker container",
)
@pytest.mark.parametrize("actor", ["owner", "member"])
def test_bwrap_reads_private_copy_but_not_shared_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    actor: str,
) -> None:
    media_root = tmp_path / "shared-media"
    telegram_root = media_root / "telegram"
    telegram_root.mkdir(parents=True)
    source = telegram_root / f"accepted-{actor}.txt"
    source.write_text("private attachment", encoding="utf-8")
    _media_dirs(monkeypatch, media_root)

    actor_root = tmp_path / "actors" / actor / "tool"
    scope = build_workspace_scope(
        actor_root,
        "restricted",
        source_channel="telegram",
        allow_shared_extras=False,
        sandbox_mask_root=tmp_path,
    )
    token = bind_workspace_scope(scope)
    try:
        private = Path(materialize_message_media([str(source)])[0])
        command = (
            f"cat {shlex.quote(str(private))}; "
            f"test ! -e {shlex.quote(str(source))}"
        )
        wrapped = wrap_command("bwrap", command, str(scope.project_path), str(scope.project_path))
        result = subprocess.run(
            shlex.split(wrapped),
            check=False,
            capture_output=True,
            text=True,
        )
    finally:
        reset_workspace_scope(token)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "private attachment"
