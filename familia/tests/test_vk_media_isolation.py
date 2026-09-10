import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanobot.agent.tools.path_utils import materialize_message_media
from nanobot.bus.queue import MessageBus
from nanobot.security.workspace_access import (
    bind_workspace_scope,
    build_workspace_scope,
    reset_workspace_scope,
)


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
        lambda channel_name=None: media_root if channel_name is None else media_root / channel_name,
    )
    actor_a = tmp_path / "actor-a"
    actor_b = tmp_path / "actor-b"
    scope_a = build_workspace_scope(
        actor_a,
        "restricted",
        source_channel="vk",
        allow_shared_extras=False,
    )
    scope_b = build_workspace_scope(
        actor_b,
        "restricted",
        source_channel="vk",
        allow_shared_extras=False,
    )

    token = bind_workspace_scope(scope_a)
    try:
        copied_a = materialize_message_media([path_a])
    finally:
        reset_workspace_scope(token)
    token = bind_workspace_scope(scope_b)
    try:
        copied_b = materialize_message_media([path_b])
    finally:
        reset_workspace_scope(token)

    assert len(copied_a) == len(copied_b) == 1
    assert Path(copied_a[0]).read_bytes() == b"bytes-a"
    assert Path(copied_b[0]).read_bytes() == b"bytes-b"
    assert Path(copied_a[0]).parent != Path(copied_b[0]).parent
