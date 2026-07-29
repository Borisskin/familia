"""RP-040 owner/private and decoded retrieval matrix."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace
from typing import Any

import pytest


class _Response:
    status_code = 200
    text = ""

    def __init__(self, value: Any) -> None:
        self._value = value

    def json(self) -> dict[str, Any]:
        return {
            "value": self._value,
            "ts": 1.0,
            "record_version": 1,
        }


class _AsyncClient:
    def __init__(
        self,
        *,
        encoded_value: str,
        family_graph: dict[str, Any],
        topics_graph: dict[str, Any],
    ) -> None:
        self._encoded_value = encoded_value
        self._family_graph = family_graph
        self._topics_graph = topics_graph

    async def __aenter__(self) -> "_AsyncClient":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def get(self, *args: Any, **kwargs: Any) -> _Response:
        key = kwargs["params"]["key"]
        if key == "shared:family.graph":
            return _Response(self._family_graph)
        if key == "shared:topics.graph":
            return _Response(self._topics_graph)
        if key == "private:bob:value:private_index":
            return _Response(
                json.dumps(
                    [{"name": "memory:fact-shared", "tags": ["topic_shared"]}]
                )
            )
        return _Response(self._encoded_value)


def _registry() -> SimpleNamespace:
    principals = {
        "alice": SimpleNamespace(
            id="alice",
            memx_key="alice-key",
            display_name="Alice",
            role="user",
        ),
        "bob": SimpleNamespace(
            id="bob",
            memx_key="bob-key",
            display_name="Bob",
            role="user",
        ),
    }
    return SimpleNamespace(ids=tuple(principals), get=principals.get)


def _valid_graphs() -> tuple[dict[str, Any], dict[str, Any]]:
    family_graph = {
        "nodes": [
            {"id": "alice", "type": "principal"},
            {"id": "bob", "type": "principal"},
        ],
        "edges": [
            {"from": "alice", "to": "bob", "rel": "spouse_of"},
        ],
        "updated_at_ms": 1,
    }
    topics_graph = {
        "nodes": [
            {"id": "topic_shared", "type": "topic"},
        ],
        "edges": [
            {
                "from": "topic_shared",
                "to": "alice",
                "rel": "concerns",
                "concerns_as": "spouse_of",
            },
            {
                "from": "topic_shared",
                "to": "bob",
                "rel": "concerns",
                "concerns_as": "spouse_of",
            },
        ],
        "updated_at_ms": 1,
    }
    return family_graph, topics_graph


def _patch_private_read(
    monkeypatch: pytest.MonkeyPatch,
    *,
    encoded_value: str,
    peer: bool,
    family_graph: dict[str, Any] | None = None,
    topics_graph: dict[str, Any] | None = None,
) -> tuple[Any, Any, list[dict[str, Any]]]:
    from familia.acl import graph_io, peers, principal_memory
    from familia.tools import memory

    monkeypatch.setattr(
        memory,
        "get_current_actor",
        lambda: "alice",
    )
    monkeypatch.setattr(memory, "get_registry", _registry)
    monkeypatch.setattr(peers, "is_peer", lambda _a, _b: peer)
    monkeypatch.setattr(memory, "is_peer", lambda _a, _b: peer)
    monkeypatch.setattr(graph_io, "resolve_admin_key", lambda: "admin-key")
    monkeypatch.setattr(memory, "resolve_admin_key", lambda: "admin-key")
    default_family, default_topics = _valid_graphs()
    family_graph = family_graph or default_family
    topics_graph = topics_graph or default_topics
    monkeypatch.setattr(
        graph_io,
        "load_graph_value",
        lambda key, **_kwargs: (
            family_graph
            if key == "shared:family.graph"
            else topics_graph
        ),
    )
    monkeypatch.setattr(
        principal_memory,
        "get_raw",
        lambda key, *, api_key: (
            json.dumps(
                [{"name": "memory:fact-shared", "tags": ["topic_shared"]}]
            )
            if key == "private:bob:value:private_index"
            else encoded_value
        ),
    )
    monkeypatch.setattr(
        memory.httpx,
        "AsyncClient",
        lambda *args, **kwargs: _AsyncClient(
            encoded_value=encoded_value,
            family_graph=family_graph,
            topics_graph=topics_graph,
        ),
    )

    audit_events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        memory.audit,
        "log_event",
        lambda event_type, **kwargs: audit_events.append(
            {"event_type": event_type, **kwargs}
        ),
    )
    return principal_memory, memory, audit_events


@pytest.mark.parametrize(
    ("allowed", "reason"),
    [
        (True, "family_common_topic"),
        (False, "no_common_topic"),
    ],
)
def test_automatic_and_explicit_peer_reads_share_one_canonical_decision(
    monkeypatch: pytest.MonkeyPatch,
    allowed: bool,
    reason: str,
) -> None:
    from familia.acl import codec
    from familia.tools.memory import MemoryGetTool

    encoded = codec.encode("shared-value", ["topic_shared"])
    principal_memory, memory, audit_events = _patch_private_read(
        monkeypatch,
        encoded_value=encoded,
        peer=True,
    )

    decision_calls: list[dict[str, Any]] = []

    def decide_memory_read(**kwargs: Any) -> SimpleNamespace:
        decision_calls.append(
            deepcopy(
                {
                    field: kwargs[field]
                    for field in (
                        "reader",
                        "owner",
                        "scope",
                        "key",
                        "tags",
                        "family_graph",
                        "topics_graph",
                        "static_policy",
                    )
                }
            )
        )
        return SimpleNamespace(allowed=allowed, reason=reason)

    monkeypatch.setattr(
        principal_memory,
        "decide_memory_read",
        decide_memory_read,
        raising=False,
    )
    monkeypatch.setattr(
        memory,
        "decide_memory_read",
        decide_memory_read,
        raising=False,
    )

    def legacy_policy_must_not_run() -> None:
        raise AssertionError("canonical peer read must not consult legacy policy")

    monkeypatch.setattr(
        principal_memory,
        "get_engine",
        legacy_policy_must_not_run,
        raising=False,
    )
    monkeypatch.setattr(memory, "get_engine", legacy_policy_must_not_run)

    automatic = principal_memory.PrincipalMemoryClient(
        "alice",
        "alice-key",
    ).get_other("bob", "memory:fact-shared")
    explicit = asyncio.run(
        MemoryGetTool().execute(
            "private",
            "memory:fact-shared",
            actor="bob",
        )
    )

    expected_value = "shared-value" if allowed else None
    assert automatic == expected_value
    if allowed:
        assert explicit == "shared-value"
    else:
        assert explicit.startswith("(no value stored")

    assert len(decision_calls) == 2
    assert decision_calls[0] == decision_calls[1]
    assert {
        field: decision_calls[0][field]
        for field in ("reader", "owner", "scope", "key", "tags", "static_policy")
    } == {
        "reader": "alice",
        "owner": "bob",
        "scope": "private",
        "key": "memory:fact-shared",
        "tags": ("topic_shared",),
        "static_policy": "no_matching_rule",
    }
    assert len(audit_events) == 1
    audit_event = audit_events[0]
    assert audit_event["event_type"] == "peer_private_read"
    assert audit_event["actor"] == "alice"
    assert audit_event["peer"] == "bob"
    assert audit_event["key"] == "private:bob:memory:fact-shared"
    assert audit_event["decision"] == ("allow" if allowed else "deny")
    assert audit_event["reason"] == reason


@pytest.mark.parametrize(
    "corruption",
    [
        "non_mapping_node",
        "incomplete_node",
        "non_mapping_edge",
        "incomplete_edge",
    ],
)
def test_automatic_and_explicit_reads_reject_the_same_malformed_raw_graph(
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    from familia.acl import codec, principal_memory
    from familia.tools.memory import MemoryGetTool

    family_graph, topics_graph = _valid_graphs()
    if corruption == "non_mapping_node":
        family_graph["nodes"].append("broken-node")
    elif corruption == "incomplete_node":
        topics_graph["nodes"].append({"id": "broken-topic"})
    elif corruption == "non_mapping_edge":
        topics_graph["edges"].append("broken-edge")
    else:
        family_graph["edges"].append(
            {"from": "alice", "rel": "spouse_of"}
        )

    encoded = codec.encode("must-not-leak", ["topic_shared"])
    _principal_memory, _memory, audit_events = _patch_private_read(
        monkeypatch,
        encoded_value=encoded,
        peer=True,
        family_graph=family_graph,
        topics_graph=topics_graph,
    )

    automatic = principal_memory.PrincipalMemoryClient(
        "alice",
        "alice-key",
    ).get_other("bob", "memory:fact-shared")
    explicit = asyncio.run(
        MemoryGetTool().execute(
            "private",
            "memory:fact-shared",
            actor="bob",
        )
    )

    assert automatic is None
    assert explicit.startswith("(no value stored")
    assert len(audit_events) == 1
    audit_event = audit_events[0]
    assert audit_event["event_type"] == "peer_private_read"
    assert audit_event["decision"] == "deny"
    assert audit_event["reason"] == "invalid_graph"


def test_memory_get_description_matches_the_canonical_read_rules() -> None:
    from familia.tools.memory import MemoryGetTool

    description = MemoryGetTool().description.casefold()

    for required in (
        "family relation",
        "exact common topic",
        "exact owner catalog",
        "only `private`",
        "owner-only service",
        "internal transaction",
        "secret",
        "`shared` and `pair`",
    ):
        assert required in description

    for forbidden in (
        "reads any",
        "every `private:` record is peer-readable",
        "tagged 'secret' are filtered",
        "legacy untagged",
        "static deny do not override",
    ):
        assert forbidden not in description


@pytest.mark.parametrize(
    ("peer", "key", "reason"),
    [(False, "memory:fact-shared", "topic_without_family_relation")],
)
def test_denied_peer_private_read_emits_one_stable_audit_reason(
    monkeypatch: pytest.MonkeyPatch,
    peer: bool,
    key: str,
    reason: str,
) -> None:
    from familia.acl import codec
    from familia.tools.memory import MemoryGetTool

    encoded = codec.encode("must-not-leak", ["topic_shared"])
    _principal_memory, memory, audit_events = _patch_private_read(
        monkeypatch,
        encoded_value=encoded,
        peer=peer,
    )

    decision_calls: list[dict[str, Any]] = []

    def decide_memory_read(**kwargs: Any) -> SimpleNamespace:
        decision_calls.append(dict(kwargs))
        return SimpleNamespace(allowed=False, reason=reason)

    monkeypatch.setattr(
        memory,
        "decide_memory_read",
        decide_memory_read,
        raising=False,
    )

    result = asyncio.run(
        MemoryGetTool().execute(
            "private",
            key,
            actor="bob",
        )
    )

    assert result.startswith("(no value stored")
    assert len(decision_calls) == 1
    assert len(audit_events) == 1
    audit_event = audit_events[0]
    assert audit_event["event_type"] == "peer_private_read"
    assert audit_event["actor"] == "alice"
    assert audit_event["peer"] == "bob"
    assert audit_event["key"] == f"private:bob:{key}"
    assert audit_event["decision"] == "deny"
    assert audit_event["reason"] == reason


def test_explicit_peer_service_read_is_owner_only_before_fact_fetch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from familia.acl import codec
    from familia.tools.memory import MemoryGetTool

    encoded = codec.encode("must-not-leak", ["topic_shared"])
    _principal_memory, memory, audit_events = _patch_private_read(
        monkeypatch,
        encoded_value=encoded,
        peer=True,
    )
    decision_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        memory,
        "decide_memory_read",
        lambda **kwargs: (
            decision_calls.append(dict(kwargs))
            or SimpleNamespace(allowed=False, reason="owner_only_service_key")
        ),
        raising=False,
    )

    result = asyncio.run(
        MemoryGetTool().execute(
            "private",
            "value:user_profile",
            actor="bob",
        )
    )

    assert result.startswith("(no value stored")
    assert decision_calls == []
    assert not [
        event
        for event in audit_events
        if event.get("decision") == "allow"
    ]


def test_memory_get_private_missing_target_is_denied(monkeypatch):
    from familia.tools import memory

    alice = SimpleNamespace(id="alice", memx_key="alice-key")
    registry = SimpleNamespace(
        ids=("alice",),
        get=lambda principal_id: alice if principal_id == "alice" else None,
    )
    monkeypatch.setattr(
        memory,
        "get_current_actor",
        lambda: "alice",
    )
    monkeypatch.setattr(memory, "get_registry", lambda: registry)

    result = asyncio.run(
        memory.MemoryGetTool().execute(
            "private",
            "value:memory",
            actor="ghost",
        )
    )
    assert result.startswith("(no value stored")
