"""End-to-end recall through the real ingestor, memX API/store and context."""

from __future__ import annotations

import json
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

if "websockets" not in sys.modules:
    sys.modules["websockets"] = types.ModuleType("websockets")


REPO_ROOT = Path(__file__).resolve().parents[2]
MEMX_ROOT = REPO_ROOT / "memx"
if str(MEMX_ROOT) not in sys.path:
    sys.path.insert(0, str(MEMX_ROOT))

from test_memory_index_consistency import InProcessRedis  # noqa: E402


class _Request:
    def __init__(self, *, api_key: str, body: object | None = None) -> None:
        self.headers = {"x-api-key": api_key}
        self.state = SimpleNamespace()
        self._body = body

    async def json(self) -> object:
        return self._body


class _Response:
    def __init__(self, payload: object, *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self) -> object:
        return self._payload


class _MemxTransport:
    def __init__(self, memx_main, *, api_key: str) -> None:
        self._main = memx_main
        self._api_key = api_key

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, _url: str, *, headers=None, params=None) -> _Response:
        request = _Request(api_key=(headers or {}).get("x-api-key", self._api_key))
        try:
            payload = await self._main.get(params["key"], request)
        except Exception as exc:  # memX HTTP boundary
            return _Response(
                getattr(exc, "detail", None),
                status_code=getattr(exc, "status_code", 500),
            )
        return _Response(payload, status_code=404 if payload is None else 200)

    async def post(self, url: str, *, headers=None, json=None) -> _Response:
        request = _Request(
            api_key=(headers or {}).get("x-api-key", self._api_key),
            body=json,
        )
        handler = self._main.delete if url.endswith("/delete") else self._main.set
        try:
            payload = await handler(request)
        except Exception as exc:  # memX HTTP boundary
            return _Response(
                getattr(exc, "detail", None),
                status_code=getattr(exc, "status_code", 500),
            )
        return _Response(payload)


@pytest.mark.asyncio
async def test_new_context_recalls_owner_catalog_and_only_authorized_peer_names(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import main as memx_main
    import store
    import validate_api
    from familia import principals as principals_mod
    from familia.acl import graph_io, principal_memory
    from familia.nanobot_extension.context import FamiliaContextExtension
    from familia.principal_memory_ingestor import PrincipalMemoryIngestor
    from familia.principals import Principal, PrincipalRegistry

    backend = InProcessRedis()
    monkeypatch.setattr(store, "_redis", backend)
    monkeypatch.setattr(validate_api, "supabase", None)
    monkeypatch.setitem(
        validate_api.LOCAL_ACL,
        "automatic-writer-key",
        ["private:*"],
    )
    monkeypatch.setattr(memx_main, "validate_schema", lambda *_args: None)

    async def publish(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(memx_main, "publish", publish)
    registry = PrincipalRegistry(
        [
            Principal(id="owner", display_name="Owner", memx_key="owner-key"),
            Principal(id="reader", display_name="Reader", memx_key="reader-key"),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)

    family_graph = {
        "nodes": [
            {"id": "owner", "type": "principal"},
            {"id": "reader", "type": "principal"},
        ],
        "edges": [{"from": "owner", "to": "reader", "rel": "spouse_of"}],
        "updated_at_ms": 1,
    }
    topics_graph = {
        "nodes": [
            {"id": "topic_common", "type": "topic"},
            {"id": "topic_owner_only", "type": "topic"},
        ],
        "edges": [
            {"from": "topic_common", "to": "owner", "rel": "concerns"},
            {"from": "topic_common", "to": "reader", "rel": "concerns"},
            {"from": "topic_owner_only", "to": "owner", "rel": "concerns"},
        ],
        "updated_at_ms": 1,
    }
    store.set_value("shared:family.graph", json.dumps(family_graph))
    store.set_value("shared:topics.graph", json.dumps(topics_graph))

    def raw_value(key: str, **_kwargs):
        record = store.get_value(key)
        return None if record is None else record["value"]

    def graph_value(key: str, **_kwargs):
        raw = raw_value(key)
        return json.loads(raw) if isinstance(raw, str) else raw

    monkeypatch.setattr(graph_io, "get_raw", raw_value)
    monkeypatch.setattr(principal_memory, "get_raw", raw_value)
    monkeypatch.setattr(graph_io, "load_graph_value", graph_value)
    monkeypatch.setattr(graph_io, "resolve_admin_key", lambda: "internal-admin")

    transport = _MemxTransport(memx_main, api_key="automatic-writer-key")
    monkeypatch.setattr(
        "familia.principal_memory_ingestor.httpx.AsyncClient",
        lambda **_kwargs: transport,
    )
    ingestor = PrincipalMemoryIngestor(
        base_url="http://memx.test",
        api_key="automatic-writer-key",
        server_topic_validator=lambda topic: topic
        in {"topic_common", "topic_owner_only"},
    )
    operations = (
        (
            "topic_common",
            {
                "kind": "memory",
                "fact_id": "shared-work",
                "value": "SHARED_VALUE_MUST_NOT_BE_PROJECTED",
            },
        ),
        (
            None,
            {
                "kind": "memory",
                "fact_id": "owner-private",
                "value": "OWNER_PRIVATE_VALUE",
            },
        ),
        (
            None,
            {
                "kind": "memory",
                "fact_id": "legacy-history",
                "value": "LEGACY_HISTORY_VALUE",
            },
        ),
        (
            "topic_owner_only",
            {
                "kind": "memory",
                "fact_id": "isolated",
                "value": "ISOLATED_VALUE",
            },
        ),
        (
            "topic_common",
            {
                "kind": "memory",
                "fact_id": "deleted-fact",
                "value": "DELETED_VALUE",
            },
        ),
    )
    for topic, operation in operations:
        result = await ingestor.ingest(
            server_principal="owner",
            server_topic=topic,
            operation=operation,
        )
        assert result.startswith("committed:"), result

    profile_result = await ingestor.ingest(
        server_principal="owner",
        server_topic="topic_common",
        operation={"kind": "profile", "value": "FOREIGN_PROFILE_VALUE"},
    )
    assert profile_result.startswith("committed:"), profile_result
    deleted_result = await ingestor.ingest(
        server_principal="owner",
        server_topic=None,
        operation={"kind": "delete", "fact_id": "deleted-fact"},
    )
    assert deleted_result.startswith("deleted:"), deleted_result
    from familia.acl import codec

    store.set_value(
        "private:owner:memory:raw-without-catalog",
        codec.encode("RAW_WITHOUT_CATALOG_VALUE", ["topic_common"]),
    )

    owner_prompt = "\n\n".join(
        FamiliaContextExtension(tmp_path).build_sections(
            actor="owner",
            channel="test",
        )
    )
    reader_prompt = "\n\n".join(
        FamiliaContextExtension(tmp_path).build_sections(
            actor="reader",
            channel="test",
        )
    )

    assert "memory:shared-work" in owner_prompt
    assert "memory:owner-private" in owner_prompt
    assert "memory:legacy-history" in owner_prompt
    assert "memory:isolated" in owner_prompt
    assert "memory:deleted-fact" not in owner_prompt
    assert "memory:raw-without-catalog" not in owner_prompt
    assert "memory:shared-work" in reader_prompt
    assert "SHARED_VALUE_MUST_NOT_BE_PROJECTED" not in reader_prompt
    assert "memory:owner-private" not in reader_prompt
    assert "OWNER_PRIVATE_VALUE" not in reader_prompt
    assert "memory:legacy-history" not in reader_prompt
    assert "LEGACY_HISTORY_VALUE" not in reader_prompt
    assert "memory:isolated" not in reader_prompt
    assert "ISOLATED_VALUE" not in reader_prompt
    assert "memory:deleted-fact" not in reader_prompt
    assert "DELETED_VALUE" not in reader_prompt
    assert "memory:raw-without-catalog" not in reader_prompt
    assert "RAW_WITHOUT_CATALOG_VALUE" not in reader_prompt
    assert "FOREIGN_PROFILE_VALUE" not in reader_prompt
