"""Dependency-independent barriers for memX multi-key authorization."""

from __future__ import annotations

import asyncio
import inspect
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
if str(MEMX_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMX_ROOT))


class _Request:
    def __init__(self, body: dict[str, Any]) -> None:
        self._body = body
        self.headers = {"x-api-key": "actor-key"}
        self.state = SimpleNamespace()

    async def json(self) -> dict[str, Any]:
        return self._body


class _SupabaseResult:
    def __init__(self, record: dict[str, Any]) -> None:
        self.data = [record]


class _SupabaseQuery:
    def __init__(self, owner: "_SupabaseGrant") -> None:
        self._owner = owner

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def execute(self) -> _SupabaseResult:
        self._owner.loads += 1
        return _SupabaseResult(self._owner.record)


class _SupabaseGrant:
    def __init__(self, patterns: list[str]) -> None:
        self.loads = 0
        self.record = {
            "user_id": "owner-space",
            "scopes": {"read": patterns, "write": patterns},
        }

    def from_(self, _table: str) -> _SupabaseQuery:
        return _SupabaseQuery(self)


def _call(function, *args, **kwargs):
    result = function(*args, **kwargs)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _memory_body(
    *,
    fact_id: str = "work",
    index_key: str = "private:owner:value:private_index",
    entry_name: str | None = None,
    maximum: int = 256,
) -> dict[str, Any]:
    return {
        "key": f"private:owner:memory:{fact_id}",
        "value": "encoded fact",
        "expected_ts": None,
        "index_update": {
            "key": index_key,
            "entry": {
                "name": entry_name or f"memory:{fact_id}",
                "tags": ["topic_work"],
            },
            "max_entries": maximum,
        },
    }


def test_set_loads_one_grant_and_namespaces_each_authorized_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as memx_main
    import validate_api
    from store import MutationResult

    grant = _SupabaseGrant(["owner-sp:private:owner:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    captured: list[tuple[str, Any, dict[str, Any]]] = []

    def set_value(key: str, value: Any, **kwargs: Any) -> MutationResult:
        captured.append((key, value, kwargs))
        return MutationResult("committed", True, True, False, 7.0)

    async def publish(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(memx_main, "set_value", set_value)
    monkeypatch.setattr(memx_main, "validate_schema", lambda *_args: None)
    monkeypatch.setattr(memx_main, "publish", publish)

    payload = _call(memx_main.set, _Request(_memory_body()))

    assert payload["status"] == "committed"
    assert grant.loads == 1
    assert captured == [
        (
            "owner-sp:private:owner:memory:work",
            "encoded fact",
            {
                "expected_ts": None,
                "index_update": {
                    "key": "owner-sp:private:owner:value:private_index",
                    "entry": {
                        "name": "memory:work",
                        "tags": ["topic_work"],
                    },
                    "max_entries": 256,
                },
            },
        )
    ]


@pytest.mark.parametrize(
    ("index_key", "entry_name", "maximum"),
    (
        ("private:owner:value:heartbeat", "memory:work", 256),
        ("private:other:value:private_index", "memory:work", 256),
        ("shared:value:private_index", "memory:work", 256),
        ("pair:owner_other:value:private_index", "memory:work", 256),
        ("shared:family.graph", "memory:work", 256),
        ("private:owner:value:private_index", "memory:other", 256),
        ("private:owner:value:private_index", "memory:work", 255),
        ("private:owner:value:private_index", "memory:work", 257),
    ),
)
def test_set_rejects_noncanonical_auxiliary_operation_before_store(
    monkeypatch: pytest.MonkeyPatch,
    index_key: str,
    entry_name: str,
    maximum: int,
) -> None:
    import main as memx_main
    import validate_api

    grant = _SupabaseGrant(["owner-sp:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    store_calls: list[object] = []
    monkeypatch.setattr(
        memx_main,
        "set_value",
        lambda *_args, **_kwargs: store_calls.append((_args, _kwargs)),
    )
    monkeypatch.setattr(memx_main, "validate_schema", lambda *_args: None)

    with pytest.raises(Exception) as error:
        _call(
            memx_main.set,
            _Request(
                _memory_body(
                    index_key=index_key,
                    entry_name=entry_name,
                    maximum=maximum,
                )
            ),
        )

    assert getattr(error.value, "status_code", None) == 400
    assert grant.loads == 0
    assert store_calls == []


def test_second_key_acl_denial_does_not_mutate_main_key_or_request_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as memx_main
    import validate_api

    grant = _SupabaseGrant(["owner-sp:private:owner:memory:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    request = _Request(_memory_body())
    store_calls: list[object] = []
    monkeypatch.setattr(
        memx_main,
        "set_value",
        lambda *_args, **_kwargs: store_calls.append((_args, _kwargs)),
    )
    monkeypatch.setattr(memx_main, "validate_schema", lambda *_args: None)

    with pytest.raises(Exception) as error:
        _call(memx_main.set, request)

    assert getattr(error.value, "status_code", None) == 403
    assert grant.loads == 1
    assert store_calls == []
    assert request.state.api_key is grant.record
    assert request.state.namespaced_key == (
        "owner-sp:private:owner:memory:work"
    )


@pytest.mark.parametrize(
    ("handler_name", "body"),
    (
        (
            "set",
            {
                "key": "private:owner:memory:work",
                "value": "encoded fact",
                "expected_ts": None,
            },
        ),
        (
            "delete",
            {
                "key": "private:owner:memory:work",
                "expected_ts": 7.0,
            },
        ),
    ),
)
def test_memory_mutation_requires_its_catalog_operation_before_acl_or_store(
    monkeypatch: pytest.MonkeyPatch,
    handler_name: str,
    body: dict[str, Any],
) -> None:
    import main as memx_main
    import validate_api

    grant = _SupabaseGrant(["owner-sp:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    store_calls: list[object] = []
    store_name = "set_value" if handler_name == "set" else "delete_value"
    monkeypatch.setattr(
        memx_main,
        store_name,
        lambda *_args, **_kwargs: store_calls.append((_args, _kwargs)),
    )
    monkeypatch.setattr(memx_main, "validate_schema", lambda *_args: None)

    with pytest.raises(Exception) as error:
        _call(getattr(memx_main, handler_name), _Request(body))

    assert getattr(error.value, "status_code", None) == 400
    assert grant.loads == 0
    assert store_calls == []


def test_delete_loads_one_grant_and_namespaces_fact_and_catalog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as memx_main
    import validate_api
    from store import MutationResult

    grant = _SupabaseGrant(["owner-sp:private:owner:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    captured: list[tuple[str, dict[str, Any]]] = []

    def delete_value(key: str, **kwargs: Any) -> MutationResult:
        captured.append((key, kwargs))
        return MutationResult("deleted", True, True, False, 7.0)

    async def publish(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(memx_main, "delete_value", delete_value)
    monkeypatch.setattr(memx_main, "publish", publish)
    request = _Request(
        {
            "key": "private:owner:memory:work",
            "expected_ts": 7.0,
            "index_remove": {
                "key": "private:owner:value:private_index",
                "name": "memory:work",
            },
        }
    )

    payload = _call(memx_main.delete, request)

    assert payload["status"] == "deleted"
    assert grant.loads == 1
    assert captured == [
        (
            "owner-sp:private:owner:memory:work",
            {
                "expected_ts": 7.0,
                "index_remove": {
                    "key": "owner-sp:private:owner:value:private_index",
                    "name": "memory:work",
                },
            },
        )
    ]


@pytest.mark.parametrize(
    ("index_key", "name"),
    (
        ("private:owner:value:heartbeat", "memory:work"),
        ("private:other:value:private_index", "memory:work"),
        ("shared:value:private_index", "memory:work"),
        ("pair:owner_other:value:private_index", "memory:work"),
        ("shared:family.graph", "memory:work"),
        ("private:owner:value:private_index", "memory:other"),
    ),
)
def test_delete_rejects_noncanonical_catalog_operation_before_acl_or_store(
    monkeypatch: pytest.MonkeyPatch,
    index_key: str,
    name: str,
) -> None:
    import main as memx_main
    import validate_api

    grant = _SupabaseGrant(["owner-sp:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    store_calls: list[object] = []
    monkeypatch.setattr(
        memx_main,
        "delete_value",
        lambda *_args, **_kwargs: store_calls.append((_args, _kwargs)),
    )
    request = _Request(
        {
            "key": "private:owner:memory:work",
            "expected_ts": 7.0,
            "index_remove": {"key": index_key, "name": name},
        }
    )

    with pytest.raises(Exception) as error:
        _call(memx_main.delete, request)

    assert getattr(error.value, "status_code", None) == 400
    assert grant.loads == 0
    assert store_calls == []


def test_delete_secondary_acl_denial_stops_before_store_and_keeps_primary_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import main as memx_main
    import validate_api

    grant = _SupabaseGrant(["owner-sp:private:owner:memory:*"])
    monkeypatch.setattr(validate_api, "supabase", grant)
    request = _Request(
        {
            "key": "private:owner:memory:work",
            "expected_ts": 7.0,
            "index_remove": {
                "key": "private:owner:value:private_index",
                "name": "memory:work",
            },
        }
    )
    store_calls: list[object] = []
    monkeypatch.setattr(
        memx_main,
        "delete_value",
        lambda *_args, **_kwargs: store_calls.append((_args, _kwargs)),
    )

    with pytest.raises(Exception) as error:
        _call(memx_main.delete, request)

    assert getattr(error.value, "status_code", None) == 403
    assert grant.loads == 1
    assert store_calls == []
    assert request.state.api_key is grant.record
    assert request.state.namespaced_key == (
        "owner-sp:private:owner:memory:work"
    )
