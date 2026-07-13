"""RP-050 semantic commit and observable corruption contract."""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest


if "redis" not in sys.modules:
    redis_stub = types.ModuleType("redis")

    class _WatchError(Exception):
        pass

    class _Redis:
        @staticmethod
        def from_url(*_args, **_kwargs):
            return SimpleNamespace()

    redis_stub.WatchError = _WatchError
    redis_stub.Redis = _Redis
    sys.modules["redis"] = redis_stub
if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.ModuleType("websockets")


REPO_ROOT = Path(__file__).resolve().parents[2]
MEMX_ROOT = REPO_ROOT / "memx"
SDK_ROOT = MEMX_ROOT / "sdk"
for path in (str(MEMX_ROOT), str(SDK_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)


def test_corrupt_value_is_observable_not_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    import store

    error_type = getattr(store, "CorruptRecordError", None)
    assert inspect.isclass(error_type), "memX corruption has no typed observable state"

    class CorruptRedis:
        def get(self, _key: str) -> str:
            return "{not-json"

    monkeypatch.setattr(store, "_redis", CorruptRedis())
    with pytest.raises(error_type, match="corrupt"):
        store.get_value("private:alice:value:memory")


def test_sdk_exposes_updated_false_as_retryable_semantic_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memx_sdk import client as sdk

    result_type = getattr(sdk, "SemanticWriteResult", None)
    assert inspect.isclass(result_type), "SDK does not expose semantic write state"

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self):
            return {
                "ok": True,
                "updated": False,
                "status": "not_updated",
                "committed": False,
                "retryable": True,
            }

    monkeypatch.setattr(sdk.httpx, "post", lambda *_args, **_kwargs: Response())
    result = sdk.memxContext("key", "http://memx.invalid").set("k", "v")
    assert isinstance(result, result_type)
    assert result.status == "not_updated"
    assert result.committed is False
    assert result.retryable is True


@pytest.mark.parametrize(
    ("payload", "prefix"),
    [
        (
            {
                "ok": True,
                "updated": False,
                "status": "not_updated",
                "committed": False,
                "retryable": True,
            },
            "retryable_failure:",
        ),
        (
            {
                "ok": True,
                "updated": True,
                "status": "committed",
                "committed": True,
                "retryable": False,
            },
            "committed:",
        ),
    ],
)
def test_memory_set_reports_semantic_state_not_transport_success(
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, object],
    prefix: str,
) -> None:
    from familia.policy import Decision
    from familia.tools import memory

    principal = SimpleNamespace(id="alice", memx_key="alice-key")
    registry = SimpleNamespace(ids=("alice",), get=lambda actor: principal if actor == "alice" else None)
    monkeypatch.setattr(memory, "get_current_actor", lambda: "alice")
    monkeypatch.setattr(memory, "get_registry", lambda: registry)
    monkeypatch.setattr(
        memory,
        "get_engine",
        lambda: SimpleNamespace(
            evaluate=lambda _context: SimpleNamespace(decision=Decision.ALLOW, reason=None)
        ),
    )

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return payload

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(memory.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(
        memory.MemorySetTool(base_url="http://memx.invalid").execute(
            "private", "value:memory", "value"
        )
    )
    assert result.startswith(prefix), result
