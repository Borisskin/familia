"""Behavioral contract for Familia's one automatic principal-memory writer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry


def _ingestor_class():
    try:
        from familia.principal_memory_ingestor import PrincipalMemoryIngestor
    except ModuleNotFoundError:
        pytest.fail(
            "familia.principal_memory_ingestor.PrincipalMemoryIngestor is missing",
            pytrace=False,
        )
    return PrincipalMemoryIngestor


def _response(payload, *, status_code: int = 200):
    response = MagicMock(status_code=status_code, text="response")
    response.json.return_value = payload
    return response


def _committed_response():
    return _response(
        {
            "ok": True,
            "status": "committed",
            "committed": True,
            "updated": True,
            "retryable": False,
            "version": 1.0,
        }
    )


def _deleted_response():
    return _response(
        {
            "ok": True,
            "status": "deleted",
            "committed": True,
            "updated": True,
            "retryable": False,
            "version": 41.0,
        }
    )


def _absent_response():
    return _response(
        {
            "ok": True,
            "status": "absent",
            "committed": True,
            "updated": False,
            "retryable": False,
            "version": None,
        }
    )


@pytest.fixture
def principal_registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    registry = PrincipalRegistry(
        [
            Principal(
                id="member_a",
                display_name="Member A",
                identities=[Identity(channel="test", sender_id="member-a")],
                memx_key="member-a",
                roles=[],
            ),
            Principal(
                id="member_b",
                display_name="Member B",
                identities=[Identity(channel="test", sender_id="member-b")],
                memx_key="member-b",
                roles=[],
            ),
        ]
    )
    monkeypatch.setattr(principals_mod, "_registry", registry)
    return registry


def test_memory_set_schema_requires_fact_id_and_keeps_value_for_write() -> None:
    import re

    from familia.tools.memory import MemorySetTool

    tool = MemorySetTool()
    properties = tool.parameters["properties"]

    assert set(properties) == {"fact_id", "value", "topic"}
    assert properties["fact_id"] == {
        "type": "string",
        "description": "Stable identifier of one atomic fact",
    }
    assert properties["value"]["type"] == "string"
    assert properties["topic"] == {
        "type": ["string", "null"],
        "description": "Optional existing shared topic requested by the person",
    }
    assert tool.parameters["required"] == ["fact_id"]
    assert tool.parameters["additionalProperties"] is False

    value_description = properties["value"]["description"].lower()
    tool_description = tool.description.lower()
    missing_value = (
        r"(?:\b(?:omit\w*|absent|missing|without|no)\b.{0,20}\bvalue\b"
        r"|\bvalue\b.{0,20}\b(?:omit\w*|absent|missing)\b)"
    )
    exact_delete = (
        r"(?:\b(?:delet\w*|remov\w*)\b.{0,40}\bexact\b.{0,20}\bfact_id\b"
        r"|\bexact\b.{0,20}\bfact_id\b.{0,40}\b(?:delet\w*|remov\w*)\b)"
    )
    value_delete_relation = re.compile(
        rf"(?:{missing_value}).{{0,80}}(?:{exact_delete})"
        rf"|(?:{exact_delete}).{{0,80}}(?:{missing_value})",
        re.DOTALL,
    )

    unused_topic = (
        r"(?:\btopic\b.{0,30}(?:not\s+used|ignored|unused)"
        r"|(?:ignor\w*|not\s+us\w*).{0,30}\btopic\b)"
    )
    delete_context = (
        r"\b(?:for|on|when|during)\b.{0,20}\b(?:delet\w*|remov\w*)\b"
    )
    topic_delete_relation = re.compile(
        rf"(?:{unused_topic}).{{0,30}}(?:{delete_context})"
        rf"|(?:{delete_context}).{{0,30}}(?:{unused_topic})",
        re.DOTALL,
    )
    assert value_delete_relation.search(value_description)
    assert value_delete_relation.search(tool_description)
    assert topic_delete_relation.search(tool_description)


@pytest.mark.parametrize(
    "ingestor_result",
    [
        "deleted: Removed 'private:member_a:memory:employment.current'",
        "absent: Already removed 'private:member_a:memory:employment.current'",
    ],
)
@pytest.mark.asyncio
async def test_memory_set_deletes_exact_fact_without_value_or_topic(
    monkeypatch: pytest.MonkeyPatch,
    principal_registry: PrincipalRegistry,
    ingestor_result: str,
) -> None:
    from familia.principals import get_current_actor, set_current_actor
    from familia.tools import memory as memory_mod
    from familia.tools.memory import MemorySetTool

    topic_write_state = AsyncMock()
    monkeypatch.setattr(
        memory_mod,
        "_topic_write_state",
        topic_write_state,
        raising=False,
    )
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value=ingestor_result)
    previous_actor = get_current_actor()
    set_current_actor("member_a")
    try:
        result = await MemorySetTool(
            base_url="http://mock-memx:8000",
            ingestor=ingestor,
        ).execute(fact_id="employment.current")
    finally:
        set_current_actor(previous_actor)

    assert result == ingestor_result
    topic_write_state.assert_not_awaited()
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=None,
        operation={"kind": "delete", "fact_id": "employment.current"},
    )


@pytest.mark.parametrize(
    "topic_nodes,topic_edges,expected_state",
    [
        ([], [], "unavailable"),
        (
            [{"id": "family-topic", "type": "topic", "kind": "abstract"}],
            [],
            "isolated",
        ),
        (
            [{"id": "family-topic", "type": "topic", "kind": "abstract"}],
            [
                {
                    "from": "family-topic",
                    "to": "member_b",
                    "rel": "concerns",
                    "concerns_as": "spouse_of",
                }
            ],
            "unavailable",
        ),
        (
            [{"id": "family-topic", "type": "topic", "kind": "abstract"}],
            [
                {
                    "from": "family-topic",
                    "to": "member_a",
                    "rel": "concerns",
                    "concerns_as": "spouse_of",
                },
                {
                    "from": "family-topic",
                    "to": "member_b",
                    "rel": "concerns",
                    "concerns_as": "spouse_of",
                },
            ],
            "shared",
        ),
    ],
)
@pytest.mark.asyncio
async def test_topic_write_state_uses_only_admin_managed_graph(
    monkeypatch: pytest.MonkeyPatch,
    principal_registry: PrincipalRegistry,
    topic_nodes: list[dict[str, str]],
    topic_edges: list[dict[str, str]],
    expected_state: str,
) -> None:
    from familia.acl.schema import Graph
    from familia.tools import memory as memory_mod

    graphs = {
        "shared:topics.graph": Graph.from_dict(
            {
                "nodes": topic_nodes,
                "edges": topic_edges,
                "updated_at_ms": 1,
            }
        ),
        "shared:family.graph": Graph.from_dict(
            {
                "nodes": [
                    {"id": "member_a", "type": "principal"},
                    {"id": "member_b", "type": "principal"},
                ],
                "edges": [],
                "updated_at_ms": 1,
            }
        ),
    }
    fetch_graph = AsyncMock(
        side_effect=lambda _api_key, key, _base_url: graphs[key]
    )
    monkeypatch.setattr(memory_mod, "_fetch_graph", fetch_graph)
    monkeypatch.setattr(memory_mod, "_is_admin", lambda _actor: False)

    state = await memory_mod._topic_write_state(
        "member_a",
        "member-a",
        "family-topic",
        "http://mock-memx:8000",
    )

    assert state == expected_state


@pytest.mark.parametrize(
    "topic_state,expected_topic,expected_notice",
    [
        ("shared", "family-topic", None),
        (
            "isolated",
            "family-topic",
            "у топика 'family-topic' нет общих связей",
        ),
        (
            "unavailable",
            None,
            "топик 'family-topic' недоступен и не настроен",
        ),
    ],
)
@pytest.mark.asyncio
async def test_memory_set_always_writes_actor_private_fact_and_topic_is_only_tag(
    monkeypatch: pytest.MonkeyPatch,
    principal_registry: PrincipalRegistry,
    topic_state: str,
    expected_topic: str | None,
    expected_notice: str | None,
) -> None:
    from familia.principals import get_current_actor, set_current_actor
    from familia.tools import memory as memory_mod
    from familia.tools.memory import MemorySetTool

    topic_write_state = AsyncMock(return_value=topic_state)
    monkeypatch.setattr(
        memory_mod,
        "_topic_write_state",
        topic_write_state,
        raising=False,
    )
    own_http_writer = MagicMock()
    monkeypatch.setattr(memory_mod.httpx, "AsyncClient", own_http_writer)
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(
        return_value="committed: Stored at 'private:member_a:memory:work-status'"
    )
    previous_actor = get_current_actor()
    set_current_actor("member_a")
    try:
        result = await MemorySetTool(
            base_url="http://mock-memx:8000",
            ingestor=ingestor,
        ).execute(
            fact_id="work-status",
            value="Перестал работать в июле 2026 года",
            topic="family-topic",
        )
    finally:
        set_current_actor(previous_actor)

    topic_write_state.assert_awaited_once_with(
        "member_a",
        "member-a",
        "family-topic",
        "http://mock-memx:8000",
    )
    ingestor.ingest.assert_awaited_once_with(
        server_principal="member_a",
        server_topic=expected_topic,
        operation={
            "kind": "memory",
            "fact_id": "work-status",
            "value": "Перестал работать в июле 2026 года",
        },
    )
    own_http_writer.assert_not_called()
    if expected_notice is None:
        assert result == (
            "committed: Stored at 'private:member_a:memory:work-status'"
        )
    else:
        assert expected_notice in result
        assert "сохранено в личную память" in result


@pytest.mark.parametrize(
    "topic_state,ingestor_result,expected_notice",
    [
        (
            "isolated",
            "denied_invalid: rejected",
            "нет общих связей",
        ),
        (
            "unavailable",
            "error: memX unavailable",
            "недоступен и не настроен",
        ),
    ],
)
@pytest.mark.asyncio
async def test_memory_set_topic_notice_does_not_claim_failed_write_was_saved(
    monkeypatch: pytest.MonkeyPatch,
    principal_registry: PrincipalRegistry,
    topic_state: str,
    ingestor_result: str,
    expected_notice: str,
) -> None:
    from familia.principals import get_current_actor, set_current_actor
    from familia.tools import memory as memory_mod
    from familia.tools.memory import MemorySetTool

    monkeypatch.setattr(
        memory_mod,
        "_topic_write_state",
        AsyncMock(return_value=topic_state),
        raising=False,
    )
    ingestor = MagicMock()
    ingestor.ingest = AsyncMock(return_value=ingestor_result)
    previous_actor = get_current_actor()
    set_current_actor("member_a")
    try:
        result = await MemorySetTool(
            base_url="http://mock-memx:8000",
            ingestor=ingestor,
        ).execute(
            fact_id="work-status",
            value="Перестал работать в июле 2026 года",
            topic="family-topic",
        )
    finally:
        set_current_actor(previous_actor)

    assert ingestor_result in result
    assert expected_notice in result
    assert "сохранено" not in result.lower()


@pytest.mark.parametrize(
    "operation,expected_key",
    [
        (
            {"kind": "profile", "value": "updated profile"},
            "private:member_a:value:user_profile",
        ),
        (
            {"kind": "memory", "fact_id": "fact-17", "value": "updated fact"},
            "private:member_a:memory:fact-17",
        ),
    ],
)
@pytest.mark.asyncio
async def test_ingestor_uses_server_principal_and_conditional_semantic_write(
    principal_registry: PrincipalRegistry,
    operation: dict[str, str],
    expected_key: str,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    get_response = _response({"value": "previous", "ts": 41.0})
    post_response = _committed_response()

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=get_response)
        client.post = AsyncMock(return_value=post_response)
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation=operation,
        )

    assert result.startswith("committed:")
    client.get.assert_awaited_once_with(
        "http://mock-memx:8000/get",
        headers={"x-api-key": "automatic-writer-key"},
        params={"key": expected_key},
    )
    client.post.assert_awaited_once_with(
        "http://mock-memx:8000/set",
        headers={"x-api-key": "automatic-writer-key"},
        json={
            "key": expected_key,
            "value": operation["value"],
            "expected_ts": 41.0,
        },
    )


@pytest.mark.asyncio
async def test_ingestor_stores_two_memory_facts_as_two_atomic_keys(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[_response(None), _response(None)])
        client.post = AsyncMock(side_effect=[_committed_response(), _committed_response()])
        client_cls.return_value.__aenter__.return_value = client

        for fact_id, value in (("fact-a", "first fact"), ("fact-b", "second fact")):
            result = await ingestor.ingest(
                server_principal="member_a",
                server_topic=None,
                operation={"kind": "memory", "fact_id": fact_id, "value": value},
            )
            assert result.startswith("committed:")

    written_keys = [call.kwargs["json"]["key"] for call in client.post.await_args_list]
    assert written_keys == [
        "private:member_a:memory:fact-a",
        "private:member_a:memory:fact-b",
    ]
    assert all(not key.endswith(":value:memory") for key in written_keys)


@pytest.mark.asyncio
async def test_ingestor_deletes_exact_private_fact_with_current_revision(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    full_key = "private:member_a:memory:employment.current"

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_response({"value": "works here", "ts": 41.0})
        )
        client.post = AsyncMock(return_value=_deleted_response())
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={"kind": "delete", "fact_id": "employment.current"},
        )

    assert result == f"deleted: Removed '{full_key}'"
    client.get.assert_awaited_once_with(
        "http://mock-memx:8000/get",
        headers={"x-api-key": "automatic-writer-key"},
        params={"key": full_key},
    )
    client.post.assert_awaited_once_with(
        "http://mock-memx:8000/delete",
        headers={"x-api-key": "automatic-writer-key"},
        json={"key": full_key, "expected_ts": 41.0},
    )


@pytest.mark.asyncio
async def test_ingestor_treats_absent_private_fact_as_successful_delete_repeat(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_response(None, status_code=404))
        client.post = AsyncMock(return_value=_absent_response())
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={"kind": "delete", "fact_id": "employment.current"},
        )

    assert result == "absent: Already removed 'private:member_a:memory:employment.current'"
    assert client.post.await_args.kwargs["json"]["expected_ts"] is None


@pytest.mark.asyncio
async def test_ingestor_rereads_delete_after_semantic_conflict(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    conflict = _response(
        {
            "ok": True,
            "status": "conflict",
            "committed": False,
            "updated": False,
            "retryable": True,
            "version": 42.0,
        }
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                _response({"value": "old", "ts": 41.0}),
                _response({"value": "concurrent", "ts": 42.0}),
            ]
        )
        client.post = AsyncMock(side_effect=[conflict, _deleted_response()])
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={"kind": "delete", "fact_id": "employment.current"},
        )

    assert result.startswith("deleted:")
    assert [
        pending.kwargs["json"]["expected_ts"]
        for pending in client.post.await_args_list
    ] == [41.0, 42.0]


@pytest.mark.asyncio
async def test_ingestor_encodes_verified_topic_only_in_atomic_private_value(
    principal_registry: PrincipalRegistry,
) -> None:
    from familia.acl import codec

    server_topic_validator = MagicMock(return_value=True)
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
        server_topic_validator=server_topic_validator,
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_response(None))
        client.post = AsyncMock(return_value=_committed_response())
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic="confirmed_household_topic",
            operation={"kind": "memory", "fact_id": "fact-topic", "value": "topic fact"},
        )

    assert result.startswith("committed:")
    payload = client.post.await_args.kwargs["json"]
    assert payload["key"] == "private:member_a:memory:fact-topic"
    assert payload["value"] == codec.encode("topic fact", ["confirmed_household_topic"])
    assert "confirmed_household_topic" not in payload["key"]
    server_topic_validator.assert_called_once_with("confirmed_household_topic")


@pytest.mark.parametrize(
    "server_topic,operation",
    [
        (None, {"kind": "profile", "value": "   "}),
        ("   ", {"kind": "profile", "value": "valid profile"}),
    ],
)
@pytest.mark.asyncio
async def test_ingestor_rejects_whitespace_value_or_topic_before_http(
    principal_registry: PrincipalRegistry,
    server_topic: str | None,
    operation: dict[str, str],
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_response(None))
        client.post = AsyncMock(return_value=_committed_response())
        client_cls.return_value.__aenter__.return_value = client
        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=server_topic,
            operation=operation,
        )

    assert result.startswith("denied_invalid:")
    client_cls.assert_not_called()


@pytest.mark.parametrize("validator", [None, MagicMock(return_value=False)])
@pytest.mark.asyncio
async def test_ingestor_rejects_non_null_topic_without_positive_server_validator(
    principal_registry: PrincipalRegistry,
    validator,
) -> None:
    kwargs = {
        "base_url": "http://mock-memx:8000",
        "api_key": "automatic-writer-key",
    }
    if validator is not None:
        kwargs["server_topic_validator"] = validator
    ingestor = _ingestor_class()(**kwargs)

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_response(None))
        client.post = AsyncMock(return_value=_committed_response())
        client_cls.return_value.__aenter__.return_value = client
        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic="forged_or_unknown_topic",
            operation={"kind": "memory", "fact_id": "fact-topic", "value": "fact"},
        )

    assert result.startswith("denied_invalid:")
    client_cls.assert_not_called()
    if validator is not None:
        validator.assert_called_once_with("forged_or_unknown_topic")


@pytest.mark.asyncio
async def test_ingestor_cas_conflict_rereads_new_ts_before_second_commit(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    conflict = _response(
        {
            "ok": True,
            "status": "conflict",
            "committed": False,
            "updated": False,
            "retryable": True,
            "version": 42.0,
        }
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                _response({"value": "old", "ts": 41.0}),
                _response({"value": "concurrent", "ts": 42.0}),
            ]
        )
        client.post = AsyncMock(side_effect=[conflict, _committed_response()])
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={"kind": "memory", "fact_id": "fact-cas", "value": "new"},
        )

    assert result.startswith("committed:")
    assert client.get.await_count == 2
    assert [
        pending.kwargs["json"]["expected_ts"]
        for pending in client.post.await_args_list
    ] == [41.0, 42.0]


@pytest.mark.asyncio
async def test_ingestor_retries_canonical_not_updated_after_rereading_ts(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    full_key = "private:member_a:memory:fact-not-updated"
    not_updated = _response(
        {
            "ok": True,
            "status": "not_updated",
            "committed": False,
            "updated": False,
            "retryable": True,
            "version": 41.0,
        }
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                _response({"value": "old", "ts": 41.0}),
                _response({"value": "concurrent", "ts": 42.0}),
            ]
        )
        client.post = AsyncMock(
            side_effect=[not_updated, _committed_response()]
        )
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={
                "kind": "memory",
                "fact_id": "fact-not-updated",
                "value": "new",
            },
        )

    assert result.startswith("committed:")
    assert [
        pending.kwargs["params"]["key"]
        for pending in client.get.await_args_list
    ] == [full_key, full_key]
    assert [
        (
            pending.kwargs["json"]["key"],
            pending.kwargs["json"]["expected_ts"],
        )
        for pending in client.post.await_args_list
    ] == [
        (full_key, 41.0),
        (full_key, 42.0),
    ]


@pytest.mark.parametrize("status", ["conflict", "not_updated"])
@pytest.mark.asyncio
async def test_ingestor_exhausts_conditional_retries_with_one_generic_failure(
    principal_registry: PrincipalRegistry,
    status: str,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    full_key = "private:member_a:memory:fact-exhausted"
    revisions = [41.0, 42.0, 43.0]

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            side_effect=[
                _response({"value": f"current-{revision}", "ts": revision})
                for revision in revisions
            ]
        )
        client.post = AsyncMock(
            side_effect=[
                _response(
                    {
                        "ok": True,
                        "status": status,
                        "committed": False,
                        "updated": False,
                        "retryable": True,
                        "version": revision,
                    }
                )
                for revision in revisions
            ]
        )
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={
                "kind": "memory",
                "fact_id": "fact-exhausted",
                "value": "new",
            },
        )

    assert result == (
        "retryable_failure: memX conditional commit failed after 3 attempts"
    )
    assert [
        pending.kwargs["params"]["key"]
        for pending in client.get.await_args_list
    ] == [full_key, full_key, full_key]
    assert [
        (
            pending.kwargs["json"]["key"],
            pending.kwargs["json"]["expected_ts"],
        )
        for pending in client.post.await_args_list
    ] == [
        (full_key, 41.0),
        (full_key, 42.0),
        (full_key, 43.0),
    ]


@pytest.mark.parametrize(
    ("status", "version", "operation"),
    [
        (
            [],
            41.0,
            {"kind": "memory", "fact_id": "fact-invalid", "value": "new"},
        ),
        (
            {},
            41.0,
            {"kind": "memory", "fact_id": "fact-invalid", "value": "new"},
        ),
        (
            "not_updated",
            41.0,
            {"kind": "delete", "fact_id": "fact-invalid"},
        ),
        (
            "conflict",
            True,
            {"kind": "memory", "fact_id": "fact-invalid", "value": "new"},
        ),
        (
            "conflict",
            "41.0",
            {"kind": "memory", "fact_id": "fact-invalid", "value": "new"},
        ),
        (
            "conflict",
            {"ts": 41.0},
            {"kind": "memory", "fact_id": "fact-invalid", "value": "new"},
        ),
    ],
    ids=[
        "status-list",
        "status-object",
        "delete-not-updated",
        "version-bool",
        "version-string",
        "version-object",
    ],
)
@pytest.mark.asyncio
async def test_ingestor_fails_fast_on_noncanonical_conditional_result(
    principal_registry: PrincipalRegistry,
    status: object,
    version: object,
    operation: dict[str, str],
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    semantic_result = _response(
        {
            "ok": True,
            "status": status,
            "committed": False,
            "updated": False,
            "retryable": True,
            "version": version,
        }
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_response({"value": "current", "ts": 41.0})
        )
        client.post = AsyncMock(return_value=semantic_result)
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation=operation,
        )

    assert result == "error: memX did not confirm the conditional commit"
    assert not result.startswith("retryable_failure:")
    client.get.assert_awaited_once()
    client.post.assert_awaited_once()


@pytest.mark.parametrize(
    "server_principal,operation",
    [
        ("member_a", {"kind": "memory", "fact_id": "f", "value": "v", "scope": "shared"}),
        ("member_a", {"kind": "memory", "fact_id": "f", "value": "v", "scope": "pair"}),
        ("member_a", {"kind": "memory", "fact_id": "f", "value": "v", "owner": "member_b"}),
        ("member_a", {"kind": "memory", "fact_id": "f", "value": "v", "topic": "untrusted"}),
        ("missing_member", {"kind": "memory", "fact_id": "f", "value": "v"}),
    ],
)
@pytest.mark.asyncio
async def test_ingestor_rejects_model_routing_and_unknown_server_principal_without_http(
    principal_registry: PrincipalRegistry,
    server_principal: str,
    operation: dict[str, str],
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        result = await ingestor.ingest(
            server_principal=server_principal,
            server_topic=None,
            operation=operation,
        )

    assert result.startswith("denied_invalid:")
    client_cls.assert_not_called()


@pytest.mark.asyncio
async def test_ingestor_does_not_report_non_committed_semantic_result_as_success(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )
    not_updated = _response(
        {
            "ok": True,
            "status": "not_updated",
            "committed": False,
            "updated": False,
            "retryable": False,
            "version": None,
        }
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(return_value=_response({"value": "old", "ts": 9.0}))
        client.post = AsyncMock(return_value=not_updated)
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={"kind": "profile", "value": "new"},
        )

    assert result == "error: memX did not confirm the conditional commit"
    assert not result.startswith("retryable_failure:")
    client.get.assert_awaited_once()
    client.post.assert_awaited_once()
    assert client.post.await_args.kwargs["json"]["expected_ts"] == 9.0


@pytest.mark.asyncio
async def test_ingestor_rejects_resolved_write_without_http(
    principal_registry: PrincipalRegistry,
) -> None:
    ingestor = _ingestor_class()(
        base_url="http://mock-memx:8000",
        api_key="automatic-writer-key",
    )

    with patch("familia.principal_memory_ingestor.httpx.AsyncClient") as client_cls:
        client = AsyncMock()
        client.get = AsyncMock(
            return_value=_response({"value": "previous", "ts": 41.0})
        )
        client.post = AsyncMock(return_value=_committed_response())
        client_cls.return_value.__aenter__.return_value = client

        result = await ingestor.ingest(
            server_principal="member_a",
            server_topic=None,
            operation={
                "kind": "resolved_write",
                "full_key": "shared:household-note",
                "stored_value": "arbitrary stored value",
                "index_update": None,
            },
        )

    assert result.startswith("denied_invalid:")
    client.get.assert_not_awaited()
    client.post.assert_not_awaited()
