"""RP-050 semantic commit and observable corruption contract."""

from __future__ import annotations

import asyncio
import inspect
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


if "fastapi" not in sys.modules:
    fastapi_stub = types.ModuleType("fastapi")

    class _FastAPI:
        @staticmethod
        def _route(*_args, **_kwargs):
            return lambda function: function

        get = _route
        post = _route
        delete = _route
        websocket = _route

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail: object = None) -> None:
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail

    fastapi_stub.FastAPI = _FastAPI
    fastapi_stub.WebSocket = object
    fastapi_stub.Request = object
    fastapi_stub.HTTPException = _HTTPException
    sys.modules["fastapi"] = fastapi_stub

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


class _DeletePipeline:
    def __init__(self, raw: str | None) -> None:
        self.raw = raw
        self.delete_called = False

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def watch(self, *_keys: str) -> None:
        return None

    def get(self, _key: str) -> str | None:
        return self.raw

    def unwatch(self) -> None:
        return None

    def multi(self) -> None:
        return None

    def delete(self, _key: str) -> None:
        self.delete_called = True

    def execute(self) -> None:
        if self.delete_called:
            self.raw = None

    def reset(self) -> None:
        return None


class _DeleteRedis:
    def __init__(self, raw: str | None) -> None:
        self.pipe = _DeletePipeline(raw)

    def pipeline(self) -> _DeletePipeline:
        return self.pipe


def test_delete_value_removes_matching_revision(monkeypatch: pytest.MonkeyPatch) -> None:
    import store

    redis = _DeleteRedis(json.dumps({"value": "old", "ts": 41.0}))
    monkeypatch.setattr(store, "_redis", redis)

    result = store.delete_value("private:alice:memory:employment", expected_ts=41.0)

    assert result.as_payload() == {
        "ok": True,
        "status": "deleted",
        "committed": True,
        "updated": True,
        "retryable": False,
        "version": 41.0,
    }
    assert redis.pipe.raw is None


def test_delete_value_reports_retryable_revision_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import store

    redis = _DeleteRedis(json.dumps({"value": "old", "ts": 41.0}))
    monkeypatch.setattr(store, "_redis", redis)

    result = store.delete_value("private:alice:memory:employment", expected_ts=40.0)

    assert result.as_payload() == {
        "ok": True,
        "status": "conflict",
        "committed": False,
        "updated": False,
        "retryable": True,
        "version": 41.0,
    }
    assert redis.pipe.raw is not None


def test_delete_value_treats_missing_key_as_successful_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import store

    redis = _DeleteRedis(None)
    monkeypatch.setattr(store, "_redis", redis)

    result = store.delete_value("private:alice:memory:employment", expected_ts=None)

    assert result.as_payload() == {
        "ok": True,
        "status": "absent",
        "committed": True,
        "updated": False,
        "retryable": False,
        "version": None,
    }
    assert redis.pipe.delete_called is False


def test_delete_endpoint_checks_acl_and_publishes_actual_deletion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as memx_main
    import store

    request = SimpleNamespace(
        state=SimpleNamespace(namespaced_key="tenant:private:alice:memory:employment"),
        json=AsyncMock(
            return_value={
                "key": "private:alice:memory:employment",
                "expected_ts": 41.0,
            }
        ),
    )
    validate = AsyncMock()
    delete_value = Mock(
        return_value=store.MutationResult(
            status="deleted",
            committed=True,
            updated=True,
            retryable=False,
            version=41.0,
        )
    )
    publish = AsyncMock()
    monkeypatch.setattr(memx_main.validate_api, "validate_api_key", validate)
    monkeypatch.setattr(memx_main, "delete_value", delete_value, raising=False)
    monkeypatch.setattr(memx_main, "publish", publish)

    result = asyncio.run(memx_main.delete(request))

    validate.assert_awaited_once_with(
        request,
        "private:alice:memory:employment",
        action="write",
    )
    delete_value.assert_called_once_with(
        "tenant:private:alice:memory:employment",
        expected_ts=41.0,
    )
    publish.assert_awaited_once_with(
        "tenant:private:alice:memory:employment",
        {
            "event": "delete",
            "key": "tenant:private:alice:memory:employment",
        },
        event="value",
    )
    assert result == {
        "ok": True,
        "status": "deleted",
        "committed": True,
        "updated": True,
        "retryable": False,
        "version": 41.0,
    }


def test_delete_endpoint_does_not_publish_missing_repeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as memx_main
    import store

    request = SimpleNamespace(
        state=SimpleNamespace(namespaced_key="tenant:private:alice:memory:missing"),
        json=AsyncMock(
            return_value={
                "key": "private:alice:memory:missing",
                "expected_ts": None,
            }
        ),
    )
    monkeypatch.setattr(
        memx_main.validate_api,
        "validate_api_key",
        AsyncMock(),
    )
    monkeypatch.setattr(
        memx_main,
        "delete_value",
        Mock(
            return_value=store.MutationResult(
                status="absent",
                committed=True,
                updated=False,
                retryable=False,
                version=None,
            )
        ),
        raising=False,
    )
    publish = AsyncMock()
    monkeypatch.setattr(memx_main, "publish", publish)

    result = asyncio.run(memx_main.delete(request))

    publish.assert_not_awaited()
    assert result["status"] == "absent"


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
                    "status": "conflict",
                    "committed": False,
                    "retryable": True,
                    "version": 42.0,
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
                    "version": 42.0,
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
    from familia import principals
    from familia.tools import memory

    principal = SimpleNamespace(id="alice", memx_key="alice-key")
    registry = SimpleNamespace(ids=("alice",), get=lambda actor: principal if actor == "alice" else None)
    monkeypatch.setattr(memory, "get_current_actor", lambda: "alice")
    monkeypatch.setattr(memory, "get_registry", lambda: registry)
    monkeypatch.setattr(principals, "_registry", registry)
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

        def __init__(self, body: object):
            self.body = body

        def json(self):
            return self.body

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response({"value": "previous", "ts": 41.0})

        async def post(self, *_args, **_kwargs):
            return Response(payload)

    monkeypatch.setattr(memory.httpx, "AsyncClient", lambda **_kwargs: Client())
    result = asyncio.run(
        memory.MemorySetTool(base_url="http://memx.invalid").execute(
            fact_id="semantic-state",
            value="value",
        )
    )
    assert result.startswith(prefix), result
